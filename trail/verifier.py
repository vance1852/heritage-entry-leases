"""闸口值班机的本地核验程序。

设计要点：

* 全部离线档案与放行事实写入本机 sqlite，进程重启后状态不丢；
* 核验严格按顺序判定：凭证归属 → 授权闸口 → 吊销状态 → 时钟误差界 →
  离线时长 → 窗口（计入误差）→ 停开 → 重复放行 → 授权余量；
* 闸机写下已放行事实后重启，重扫同一凭证命中 ``already_used``，不会二次放行；
* 时钟观测误差超过物料 ``error_bound`` 的闸口立即失去离线放行资格，
  自最后可信对时起超过 ``max_offline`` 秒同样停止离线放行；
* 放行记录按 (grant_id, use_no) 连续编号，重连后幂等补传服务。
"""

import sqlite3
import threading
from pathlib import Path

from .model import DenyReason

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    gate TEXT PRIMARY KEY,
    error_bound INTEGER NOT NULL,
    max_offline INTEGER NOT NULL,
    last_trusted_sync INTEGER,
    last_clock_error INTEGER NOT NULL DEFAULT 0,
    offline_capable INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS window_fact (
    id TEXT PRIMARY KEY, direction TEXT NOT NULL,
    start INTEGER NOT NULL, until INTEGER NOT NULL, capacity INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS grant_fact (
    grant_id TEXT PRIMARY KEY, gate TEXT NOT NULL, window TEXT NOT NULL,
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS permit_fact (
    permit TEXT PRIMARY KEY, grp TEXT NOT NULL, gate TEXT NOT NULL,
    grant_id TEXT NOT NULL, window TEXT NOT NULL, count INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'active', superseded_by TEXT
);
CREATE TABLE IF NOT EXISTS use_record (
    gate TEXT NOT NULL, grant_id TEXT NOT NULL, use_no INTEGER NOT NULL,
    permit TEXT NOT NULL, count INTEGER NOT NULL, local_time INTEGER NOT NULL,
    PRIMARY KEY (gate, grant_id, use_no)
);
CREATE TABLE IF NOT EXISTS permit_release (
    gate TEXT NOT NULL, permit TEXT NOT NULL, grant_id TEXT NOT NULL,
    use_no INTEGER NOT NULL, PRIMARY KEY (gate, permit)
);
CREATE TABLE IF NOT EXISTS closure_fact (
    gate TEXT NOT NULL, direction TEXT NOT NULL, state TEXT NOT NULL,
    at INTEGER NOT NULL, PRIMARY KEY (gate, direction)
);
CREATE TABLE IF NOT EXISTS denial_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at INTEGER NOT NULL, local_time INTEGER, permit TEXT,
    reason TEXT NOT NULL, detail TEXT
);
"""


class GateVerifier:
    def __init__(self, gate, db_path, error_bound, max_offline):
        self.gate = gate
        self.error_bound = error_bound
        self.max_offline = max_offline
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT gate FROM meta WHERE gate=?", (gate,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(gate, error_bound, max_offline) "
                    "VALUES (?,?,?)", (gate, error_bound, max_offline))
                self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ 离线档案同步
    def apply_bundle(self, bundle, at):
        """落地服务下发的离线档案（重连/初次部署时调用）。"""
        with self._lock:
            cur = self._conn
            cur.execute("DELETE FROM window_fact")
            for win in bundle.get("windows", []):
                cur.execute(
                    "INSERT INTO window_fact VALUES (?,?,?,?,?)",
                    (win["id"], win["direction"], win["start"],
                     win["until"], win["capacity"]))
            cur.execute("DELETE FROM grant_fact WHERE gate=?", (self.gate,))
            for grant in bundle.get("grants", []):
                cur.execute(
                    "INSERT INTO grant_fact VALUES (?,?,?,?)",
                    (grant["grant_id"], self.gate, grant["window"],
                     grant["count"]))
            for permit in bundle.get("permits", []):
                self._upsert_permit(permit)
            for closure in bundle.get("closures", []):
                if closure.get("gate_state"):
                    cur.execute(
                        "INSERT OR REPLACE INTO closure_fact VALUES (?,?,?,?)",
                        (self.gate, closure["direction"],
                         closure["gate_state"],
                         closure.get("gate_state_at")))
            self.mark_synced(at)
            cur.commit()

    def _upsert_permit(self, permit):
        row = self._conn.execute(
            "SELECT permit FROM permit_fact WHERE permit=?",
            (permit["permit"],)).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO permit_fact VALUES (?,?,?,?,?,?,?,?)",
                (permit["permit"], permit["group"], permit["gate"],
                 permit["grant_id"], permit["window"], permit["count"],
                 permit["state"], permit.get("superseded_by")))
        else:
            self._conn.execute(
                "UPDATE permit_fact SET state=?, superseded_by=? WHERE permit=?",
                (permit["state"], permit.get("superseded_by"), permit["permit"]))

    def install_permit(self, permit, at):
        """在线/现场登记一张凭证到本机档案（permit 事件使用）。"""
        with self._lock:
            self._upsert_permit(permit)
            self._conn.commit()
        return self.get_permit(permit["permit"])

    def get_permit(self, permit_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM permit_fact WHERE permit=?",
                (permit_id,)).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------ 对时与断网
    def mark_synced(self, at):
        """记录一次可信对时/同步，恢复离线资格。"""
        with self._lock:
            self._conn.execute(
                "UPDATE meta SET last_trusted_sync=?, last_clock_error=0, "
                "offline_capable=1 WHERE gate=?", (at, self.gate))
            self._conn.commit()

    def report_clock_error(self, observed, at):
        """断网时的失准观测：超界即永久失去离线资格，直到下次可信对时。"""
        with self._lock:
            capable = abs(observed) <= self.error_bound
            self._conn.execute(
                "UPDATE meta SET last_clock_error=?, offline_capable=? "
                "WHERE gate=?", (observed, 1 if capable else 0, self.gate))
            self._conn.commit()
            return capable

    def _meta(self):
        return dict(self._conn.execute(
            "SELECT * FROM meta WHERE gate=?", (self.gate,)).fetchone())

    def offline_status(self):
        with self._lock:
            meta = self._meta()
            return {
                "gate": self.gate,
                "error_bound": self.error_bound,
                "max_offline": self.max_offline,
                "last_trusted_sync": meta["last_trusted_sync"],
                "last_clock_error": meta["last_clock_error"],
                "offline_capable": bool(meta["offline_capable"]),
            }

    # ------------------------------------------------------------ 停开
    def mark_closure_seen(self, direction, at):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO closure_fact VALUES (?,?,?,?)",
                (self.gate, direction, "seen", at))
            self._conn.commit()

    def mark_closed(self, direction, at):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO closure_fact VALUES (?,?,?,?)",
                (self.gate, direction, "closed", at))
            self._conn.commit()

    # ------------------------------------------------------------ 核验主流程
    def scan(self, permit_id, local_time, at):
        """现场扫证核验。返回 {allowed, reason, use_no?, detail}。

        整个判定与放行事实写入在一个事务内完成，保证崩溃/重启下
        「同一凭证至多放行一次、use_no 不跳号」。
        """
        with self._lock:
            conn = self._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                verdict = self._verify(permit_id, local_time)
                if not verdict["allowed"]:
                    conn.execute(
                        "INSERT INTO denial_log(at, local_time, permit, reason, detail)"
                        " VALUES (?,?,?,?,?)",
                        (at, local_time, permit_id,
                         verdict["reason"], verdict.get("detail", "")))
                    conn.commit()
                    return verdict
                permit = conn.execute(
                    "SELECT * FROM permit_fact WHERE permit=?",
                    (permit_id,)).fetchone()
                next_no = self._next_use_no(permit["grant_id"])
                conn.execute(
                    "INSERT INTO use_record VALUES (?,?,?,?,?,?)",
                    (self.gate, permit["grant_id"], next_no,
                     permit_id, permit["count"], local_time))
                conn.execute(
                    "INSERT INTO permit_release VALUES (?,?,?,?)",
                    (self.gate, permit_id, permit["grant_id"], next_no))
                conn.commit()
                verdict["use_no"] = next_no
                verdict["count"] = permit["count"]
                verdict["grant_id"] = permit["grant_id"]
                verdict["local_time"] = local_time
                return verdict
            except Exception:
                conn.rollback()
                raise

    def _verify(self, permit_id, local_time):
        meta = self._meta()
        permit = self._conn.execute(
            "SELECT * FROM permit_fact WHERE permit=?",
            (permit_id,)).fetchone()
        if permit is None:
            return self._deny(DenyReason.UNKNOWN_PERMIT,
                              f"本机离线档案没有凭证 {permit_id}")
        if permit["gate"] != self.gate:
            return self._deny(DenyReason.WRONG_GATE,
                              f"凭证授权闸口 {permit['gate']}，本机 {self.gate}")
        if permit["state"] in ("superseded", "void"):
            return self._deny(DenyReason.REVOKED,
                              f"凭证状态 {permit['state']}"
                              + (f"，已由 {permit['superseded_by']} 替换"
                                 if permit["superseded_by"] else ""))
        if not meta["offline_capable"]:
            return self._deny(
                DenyReason.CLOCK_OUT_OF_BOUND,
                f"时钟观测误差 {meta['last_clock_error']} 超过误差界 "
                f"{self.error_bound}，闸口不得继续离线放行")
        last_sync = meta["last_trusted_sync"]
        if last_sync is not None and local_time - last_sync > self.max_offline:
            return self._deny(
                DenyReason.OFFLINE_LIMIT,
                f"离线 {local_time - last_sync}s 超过 max_offline "
                f"{self.max_offline}s（最后可信对时 {last_sync}）")
        win = self._conn.execute(
            "SELECT * FROM window_fact WHERE id=?",
            (permit["window"],)).fetchone()
        if win is None:
            return self._deny(DenyReason.UNKNOWN_PERMIT,
                              f"窗口 {permit['window']} 档案缺失")
        bound = self.error_bound
        # 计入闸机时钟正常误差的保守窗口判定：真实时刻落在
        # [local-bound, local+bound]，须整个区间都在左闭右开窗口内
        # （最早可信时刻已开窗、最晚可信时刻仍未到关窗）。
        if local_time - bound < win["start"] or local_time + bound >= win["until"]:
            return self._deny(
                DenyReason.WINDOW_CLOSED,
                f"按误差界 ±{bound}s 保守判定，{local_time} 不能确保落在窗口 "
                f"[{win['start']},{win['until']}) 内")
        closure = self._conn.execute(
            "SELECT * FROM closure_fact WHERE gate=? AND direction=?",
            (self.gate, win["direction"])).fetchone()
        if closure and closure["state"] == "closed":
            return self._deny(DenyReason.GATE_CLOSED,
                              f"方向 {win['direction']} 已实际停开")
        released = self._conn.execute(
            "SELECT use_no FROM permit_release WHERE gate=? AND permit=?",
            (self.gate, permit_id)).fetchone()
        if released is not None:
            return self._deny(DenyReason.ALREADY_USED,
                              f"凭证已在 use_no={released['use_no']} 放行，"
                              "重启后重扫不得重复放行")
        grant = self._conn.execute(
            "SELECT * FROM grant_fact WHERE grant_id=?",
            (permit["grant_id"],)).fetchone()
        if grant is None:
            return self._deny(DenyReason.UNKNOWN_PERMIT,
                              f"授权 {permit['grant_id']} 档案缺失")
        used = self._conn.execute(
            "SELECT COALESCE(SUM(count),0) AS n FROM use_record "
            "WHERE gate=? AND grant_id=?",
            (self.gate, permit["grant_id"])).fetchone()["n"]
        if used + permit["count"] > grant["count"]:
            return self._deny(
                DenyReason.GRANT_EXHAUSTED,
                f"授权 {grant['grant_id']} 额度 {grant['count']}，"
                f"本机已放行 {used}，无法再扣 {permit['count']}")
        return {"allowed": True, "reason": None, "detail": "核验通过"}

    def _next_use_no(self, grant_id):
        row = self._conn.execute(
            "SELECT COALESCE(MAX(use_no),0) AS m FROM use_record "
            "WHERE gate=? AND grant_id=?", (self.gate, grant_id)).fetchone()
        return row["m"] + 1

    @staticmethod
    def _deny(reason: DenyReason, detail):
        return {"allowed": False, "reason": reason.value, "detail": detail}

    # ------------------------------------------------------------ 重连补传
    def pending_records(self):
        """尚未确认补传成功的放行记录（按 grant 分组、use_no 排序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM use_record ORDER BY grant_id, use_no"
            ).fetchall()
            return [dict(r) for r in rows]

    def last_use_no(self, grant_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(use_no),0) AS m FROM use_record "
                "WHERE gate=? AND grant_id=?", (self.gate, grant_id)).fetchone()
            return row["m"]

    def local_consumed(self, grant_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(count),0) AS n FROM use_record "
                "WHERE gate=? AND grant_id=?",
                (self.gate, grant_id)).fetchone()
            return row["n"]

    def denials(self):
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM denial_log ORDER BY id").fetchall()]

    def rebase_from_service(self, bundle, at):
        """重连对账：刷新档案并以服务侧吊销状态覆盖本机凭证。"""
        self.apply_bundle(bundle, at)
