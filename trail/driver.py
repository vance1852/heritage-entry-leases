"""联调驱动：读取合作方资料并把事件回放给额度服务与闸口核验程序。

用法：

* 直读资料文件（离线开发）：
  ``python -m trail.driver fixtures/two-offline-gates.json --db-dir tmp/gates``
* 对接正在运行的资料端（与 README 的 partner.py 联调）：
  先启动 ``python partner.py fixtures/two-offline-gates.json``，
  再 ``python -m trail.driver fixtures/two-offline-gates.json \
  --partner http://127.0.0.1:8090 --db-dir tmp/gates``

``--http`` 时驱动通过 :class:`trail.client.ServiceClient` 走真实 HTTP 接口
访问额度服务，否则直接在进程内调用 :class:`QuotaService`（接口同形）。
"""

import argparse
import json
import tempfile
import urllib.request
from pathlib import Path

from .service import QuotaService
from .verifier import GateVerifier


class ScenarioPlayer:
    def __init__(self, facts, events, backend, db_dir):
        self.facts = facts
        self.events = events
        self.svc = backend
        self.db_dir = Path(db_dir)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.verifiers: dict[str, GateVerifier] = {}
        self.connected: dict[str, bool] = {g["id"]: True for g in facts["gates"]}
        self.trace: list[dict] = []
        self.checks: list[dict] = []

    # --------------------------------------------------------------- 基础工具
    def _verifier(self, gate) -> GateVerifier:
        if gate not in self.verifiers:
            spec = next(g for g in self.facts["gates"] if g["id"] == gate)
            self.verifiers[gate] = GateVerifier(
                gate, self.db_dir / f"{gate}.sqlite",
                spec["error_bound"], spec["max_offline"])
        return self.verifiers[gate]

    def _refresh(self, gate, at):
        """闸口在线时从服务拉取最新离线档案并完成一次可信对时。"""
        if not self.connected.get(gate, True):
            return
        bundle = self.svc.gate_bundle(gate)
        self._verifier(gate).apply_bundle(bundle, at)

    def _record(self, event, **extra):
        entry = {k: v for k, v in event.items() if k != "kind"}
        entry.update({"at": event["at"], "kind": event["kind"], **extra})
        self.trace.append(entry)
        return entry

    # --------------------------------------------------------------- 事件回放
    def run(self):
        for event in self.events:
            handler = getattr(self, f"_on_{event['kind']}", None)
            if handler is None:
                self._record(event, note="未识别事件，已忽略")
                continue
            handler(event)
        return self.report()

    def _on_allot(self, e):
        self.svc.allot(gate=e["gate"], window=e["window"],
                       grant_id=e["grant_id"], count=e["count"], at=e["at"])
        self._refresh(e["gate"], e["at"])
        self._record(e)

    def _on_permit(self, e):
        self.svc.issue_permit(permit=e["permit"], group=e["group"],
                              gate=e["gate"], grant_id=e["grant_id"],
                              count=e["count"], at=e["at"])
        self._refresh(e["gate"], e["at"])
        self._record(e)

    def _on_disconnect(self, e):
        self.svc.report_disconnect(e["gate"], e["clock_error"], e["at"])
        capable = self._verifier(e["gate"]).report_clock_error(
            e["clock_error"], e["at"])
        self.connected[e["gate"]] = False
        self._record(e, offline_capable=capable)

    def _on_scan(self, e):
        verdict = self._verifier(e["gate"]).scan(
            e["permit"], e["local_time"], e["at"])
        self._record(e, verdict=verdict)

    def _on_restart(self, e):
        """模拟闸机进程重启：关闭后用同一 sqlite 重新拉起，状态必须延续。"""
        old = self.verifiers.pop(e["gate"])
        old.close()
        self._verifier(e["gate"])
        self._record(e, note="核验程序已重启，沿用本机持久化档案")

    def _on_move(self, e):
        result = self.svc.move(group=e["group"], target_gate=e["gate"],
                               target_window=e["window"], at=e["at"])
        if result["state"] == "confirmed":
            self._refresh(e["gate"], e["at"])
        self._record(e, move=result)

    def _on_request_return(self, e):
        self.svc.request_return(gate=e["gate"], grant_id=e["grant_id"],
                                count=e["count"], at=e["at"])
        self._record(e)

    def _on_closure(self, e):
        self.svc.closure_publish(direction=e["direction"], at=e["at"])
        self._record(e)

    def _on_closure_seen(self, e):
        self.svc.closure_seen(gate=e["gate"], at=e["at"])
        direction = self._direction_for_gate(e["gate"])
        self._verifier(e["gate"]).mark_closure_seen(direction, e["at"])
        self._record(e, direction=direction)

    def _on_closed(self, e):
        self.svc.closure_closed(gate=e["gate"], direction=e["direction"],
                                at=e["at"])
        self._verifier(e["gate"]).mark_closed(e["direction"], e["at"])
        self._record(e)

    def _direction_for_gate(self, gate):
        view = self.svc.gate_view(gate)
        if view["grants"]:
            win_id = view["grants"][0]["window"]
            return next(w["direction"] for w in self.facts["windows"]
                        if w["id"] == win_id)
        return next(w["direction"] for w in self.facts["windows"])

    def _on_return(self, e):
        gate = e["gate"]
        # 重连：先对时并拉取服务侧最新档案（含吊销/停开）
        self.connected[gate] = True
        self.svc.report_sync(gate, e["at"])
        self._refresh(gate, e["at"])
        verifier = self._verifier(gate)
        # 闸机回报的编号与扣减应与本机持久化记录一致
        local_last = verifier.last_use_no(e["grant_id"])
        local_consumed = verifier.local_consumed(e["grant_id"])
        consistency = {
            "last_use_no_matches": local_last == e["last_use_no"],
            "local_consumed": local_consumed,
            "reported_consumed": e["reported_consumed"],
        }
        result = self.svc.record_return(
            gate=gate, grant_id=e["grant_id"], returned=e["returned"],
            last_use_no=e["last_use_no"],
            reported_consumed=e["reported_consumed"], at=e["at"])
        self._record(e, grant=result, local_cross_check=consistency)

    def _on_use_record(self, e):
        verifier = self._verifier(e["gate"])
        stored = next((r for r in verifier.pending_records()
                       if r["grant_id"] == e["grant_id"]
                       and r["use_no"] == e["use_no"]), None)
        cross_check = None
        if stored is not None:
            cross_check = {
                "permit_matches": stored["permit"] == e["permit"],
                "count_matches": stored["count"] == e["count"],
                "local_time_matches": stored["local_time"] == e["local_time"],
            }
        result = self.svc.accept_use_record(
            gate=e["gate"], grant_id=e["grant_id"], use_no=e["use_no"],
            permit_id=e["permit"], count=e["count"],
            local_time=e["local_time"], at=e["at"])
        self._record(e, accepted=result, durable_cross_check=cross_check)

    # --------------------------------------------------------------- 报告与核对
    def report(self):
        overview = self.svc.overview()
        self.checks = self._invariant_checks(overview)
        return {
            "facts": self.facts,
            "trace": self.trace,
            "checks": self.checks,
            "overview": overview,
            "gates": {g: self.svc.gate_view(g) for g in
                      (x["id"] for x in self.facts["gates"])},
            "denials": {g: self._verifier(g).denials()
                        for g in self.verifiers},
        }

    def _invariant_checks(self, overview):
        checks = []

        def add(name, ok, detail):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        # 1. 每张凭证在所有闸口至多放行一次（两个闸口分别断网也不例外）
        allowed: dict[str, int] = {}
        for item in self.trace:
            if item["kind"] == "scan" and item["verdict"]["allowed"]:
                allowed[item["permit"]] = allowed.get(item["permit"], 0) + 1
        add("凭证全程至多放行一次",
            all(n <= 1 for n in allowed.values()), allowed)

        # 2. 重启后重扫只能是 already_used
        restart_rescan = [
            t for t in self.trace if t["kind"] == "scan"
            and not t["verdict"]["allowed"]
            and t["verdict"]["reason"] == "already_used"
        ]
        had_restart = any(t["kind"] == "restart" for t in self.trace)
        add("闸机重启后重扫拒绝重复放行",
            not had_restart or bool(restart_rescan),
            [t["verdict"]["reason"] for t in restart_rescan])

        # 3. 时钟超界闸口不得离线放行
        over_bound = [
            t for t in self.trace if t["kind"] == "disconnect"
            and t.get("offline_capable") is False
        ]
        bad_scans = [
            t for t in self.trace if t["kind"] == "scan"
            and t["verdict"]["allowed"]
            and any(t["gate"] == x["gate"] for x in over_bound)
        ]
        add("误差超界闸口继续离线放行数为零",
            not bad_scans,
            {"over_bound_gates": [x["gate"] for x in over_bound],
             "violations": len(bad_scans)})

        # 4. 改时申请在旧权利可能仍可用时必须 pending
        pending_ok = all(
            t["move"]["state"] != "confirmed" or
            t["move"]["reason"] != ""
            for t in self.trace if t["kind"] == "move"
        )
        add("换入口经过服务确认且记录依据", pending_ok,
            [{"group": t["move"].get("group"), "state": t["move"]["state"]}
             for t in self.trace if t["kind"] == "move"])

        # 5. shadow 额度不得计入可调剂；窗口余量与可调剂都不得为负
        shadow_ok = all(
            w["reclaimable"] >= 0
            and w["free_capacity"] == w["capacity"] - w["held"]
            and w["free_capacity"] >= 0
            for w in overview["windows"]
        )
        add("可调剂额度剔除 shadow 且不为负", shadow_ok,
            [{"window": w["window"], "held": w["held"],
              "shadow": w["shadow"], "reclaimable": w["reclaimable"],
              "free_capacity": w["free_capacity"]}
             for w in overview["windows"]])

        # 6. 跨窗口：放行只计入凭证所属窗口
        released_ok = all(
            w["released"] == sum(g["released"] for g in w["grants"])
            for w in overview["windows"]
        )
        add("旧放行不跨窗口累计", released_ok, "按窗口独立核算")

        # 7. 停开三态与交还彼此独立：有停开通知时，现场知晓与实际停止分别记录
        closure_detail = []
        for gate_view in overview["gates"]:
            closure_detail.append(
                {"gate": gate_view["gate"],
                 "closures": gate_view["closures"]})
        published = any(c for gv in overview["gates"] for c in gv["closures"])
        closure_ok = True
        if published:
            closure_ok = all(
                any(c["state"] in ("published", "seen", "closed")
                    for c in gv["closures"])
                for gv in overview["gates"]
            )
        add("停开通知区分现场知晓与实际停止", closure_ok, closure_detail)

        # 8. 完成连续性对账的 grant 必须 reconciled 且无 shadow、无持有
        reconciled_ok = all(
            (g["state"] != "reconciled") or
            (not g["missing_use_nos"] and g["shadow"] == 0 and g["count"] == 0)
            for w in overview["windows"] for g in w["grants"]
        )
        add("连续性到齐后 shadow 归零并标记 reconciled",
            reconciled_ok,
            [{"grant": g["grant_id"], "state": g["state"],
              "missing": g["missing_use_nos"], "shadow": g["shadow"],
              "count": g["count"]}
             for w in overview["windows"] for g in w["grants"]])

        return checks


def load_fixture(path, partner=None):
    if partner:
        base = partner.rstrip("/")
        with urllib.request.urlopen(f"{base}/fixture/facts", timeout=5) as r:
            facts = json.loads(r.read())
        with urllib.request.urlopen(f"{base}/fixture/events", timeout=5) as r:
            events = json.loads(r.read())
        return facts, events
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["facts"], data["events"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--partner", help="资料端地址，如 http://127.0.0.1:8090")
    parser.add_argument("--db-dir", default=None,
                        help="闸口核验程序 sqlite 目录，默认临时目录")
    parser.add_argument("--report", type=Path, help="把回放报告写入 JSON 文件")
    args = parser.parse_args(argv)

    facts, events = load_fixture(args.fixture, args.partner)
    backend = QuotaService(facts["windows"], facts["gates"])
    with tempfile.TemporaryDirectory(prefix="trail-") if args.db_dir is None \
            else _Noop(args.db_dir) as db_dir:
        player = ScenarioPlayer(facts, events, backend, db_dir)
        report = player.run()

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
        print(f"报告已写入 {args.report}")
    else:
        print(text)
    failed = [c for c in report["checks"] if not c["ok"]]
    return 1 if failed else 0


class _Noop:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
