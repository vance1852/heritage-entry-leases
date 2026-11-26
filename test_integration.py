"""端到端联调：额度服务走真实 HTTP 接口，并可对接 partner.py 资料端。"""

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

import partner as partner_mod

from trail.client import ServiceClient, ServiceError, ServiceUnreachable
from trail.driver import ScenarioPlayer, load_fixture
from trail.httpapi import ServiceHTTPServer
from trail.service import QuotaService
from trail.verifier import GateVerifier

ROOT = Path(__file__).parent
FIXTURES = ROOT / "fixtures"


class HttpEndpointTest(unittest.TestCase):
    def setUp(self):
        self.service = QuotaService(
            windows=[{"id": "w1", "direction": "up", "start": 0,
                      "until": 900, "capacity": 10}],
            gates=[{"id": "A", "error_bound": 3, "max_offline": 100},
                   {"id": "B", "error_bound": 3, "max_offline": 100}])
        self.httpd = ServiceHTTPServer(("127.0.0.1", 0), self.service)
        self.worker = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True)
        self.worker.start()
        self.client = ServiceClient("127.0.0.1", self.httpd.server_port)

    def tearDown(self):
        self.httpd.shutdown()
        self.worker.join(timeout=3)
        self.httpd.server_close()

    def test_full_lifecycle_over_http(self):
        c = self.client
        c.allot(gate="A", window="w1", grant_id="g1", count=10, at=0)
        c.issue_permit(permit="p1", group="t1", gate="A",
                       grant_id="g1", count=6, at=10)
        bundle = c.gate_bundle("A")
        self.assertEqual([p["permit"] for p in bundle["permits"]], ["p1"])

        c.report_disconnect("A", clock_error=1, at=20)
        c.request_return(gate="A", grant_id="g1", count=4, at=30)
        # 断网时闸口照常本地核验，重连后补传
        c.report_sync("A", at=100)
        c.record_return(gate="A", grant_id="g1", returned=4,
                        last_use_no=1, reported_consumed=6, at=110)
        c.accept_use_record(gate="A", grant_id="g1", use_no=1,
                            permit="p1", count=6, local_time=50, at=120)
        view = c.gate_view("A")
        self.assertEqual(view["released_total"], 6)
        grant = view["grants"][0]
        self.assertEqual(grant["state"], "reconciled")
        self.assertEqual(grant["shadow"], 0)

    def test_conflict_and_404_mapped(self):
        with self.assertRaises(ServiceError) as ctx:
            self.client.get_permit("missing")
        self.assertEqual(ctx.exception.status, 404)
        self.client.allot(gate="A", window="w1", grant_id="g1",
                          count=10, at=0)
        with self.assertRaises(ServiceError) as ctx:
            self.client.allot(gate="A", window="w1", grant_id="g1",
                              count=1, at=1)
        self.assertEqual(ctx.exception.status, 409)

    def test_unreachable_raises_dedicated_error(self):
        dead = ServiceClient("127.0.0.1", port=1, timeout=1)
        with self.assertRaises(ServiceUnreachable):
            dead.overview()

    def test_overview_queryable(self):
        c = self.client
        c.allot(gate="A", window="w1", grant_id="g1", count=7, at=0)
        c.lend(grant_id="g1", to_gate="B", count=3, at=1,
               new_grant_id="g2")
        ov = c.overview()
        w1 = next(w for w in ov["windows"] if w["window"] == "w1")
        # 借出 3：A 持有 4、B 持有 3；窗口实际占用 7，另有 3 容量未分配
        self.assertEqual(w1["held"], 7)
        self.assertEqual(w1["free_capacity"], 3)
        gates = {g["gate"]: g for g in ov["gates"]}
        self.assertEqual(gates["B"]["reclaimable_total"], 3)
        self.assertEqual(gates["A"]["grants"][0]["earmarked"], 3)


class EndToEndFixtureTest(unittest.TestCase):
    """用合作方资料驱动完整流程（进程内服务 + 持久化核验程序）。"""

    def _play(self, name):
        facts, events = load_fixture(FIXTURES / name)
        with tempfile.TemporaryDirectory() as db_dir:
            player = ScenarioPlayer(
                facts, events,
                QuotaService(facts["windows"], facts["gates"]), db_dir)
            return player.run()

    def test_two_offline_gates(self):
        report = self._play("two-offline-gates.json")
        self.assertTrue(all(c["ok"] for c in report["checks"]),
                        msg=json.dumps(
                            [c for c in report["checks"] if not c["ok"]],
                            ensure_ascii=False))
        scans = [t for t in report["trace"] if t["kind"] == "scan"]
        self.assertEqual([s["verdict"]["allowed"] for s in scans],
                         [True, False, False])
        self.assertEqual([s["verdict"].get("reason") for s in scans][1:],
                         ["unknown_permit", "already_used"])
        move = next(t for t in report["trace"] if t["kind"] == "move")
        self.assertEqual(move["move"]["state"], "pending")
        grant = next(g for w in report["overview"]["windows"]
                     for g in w["grants"] if g["grant_id"] == "g-a")
        self.assertEqual(grant["state"], "reconciled")
        self.assertEqual(grant["released"], 7)
        self.assertEqual(grant["returned"], 5)
        # 停开三态
        annex = report["gates"]["annex"]["closures"][0]
        self.assertEqual(annex["state"], "closed")

    def test_uncertain_clock(self):
        report = self._play("uncertain-clock.json")
        self.assertTrue(all(c["ok"] for c in report["checks"]),
                        msg=json.dumps(
                            [c for c in report["checks"] if not c["ok"]],
                            ensure_ascii=False))
        scan = next(t for t in report["trace"] if t["kind"] == "scan")
        self.assertFalse(scan["verdict"]["allowed"])
        self.assertEqual(scan["verdict"]["reason"], "clock_out_of_bound")
        move = next(t for t in report["trace"] if t["kind"] == "move")
        self.assertEqual(move["move"]["state"], "confirmed")
        # 整笔交还 + 划转血缘
        g_c = next(g for w in report["overview"]["windows"]
                   for g in w["grants"] if g["grant_id"] == "g-c")
        self.assertEqual(g_c["earmarked"], 11)
        self.assertEqual(g_c["state"], "reconciled")


class PartnerServerIntegrationTest(unittest.TestCase):
    """真正启动 partner.py 资料端，通过 HTTP 拉取 facts/events 再回放。"""

    def setUp(self):
        self.scenario = partner_mod.load_scenario(
            FIXTURES / "two-offline-gates.json")
        self.server = partner_mod.PartnerServer(
            ("127.0.0.1", 0), self.scenario)
        self.worker = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.worker.join(timeout=3)
        self.server.server_close()

    def test_fetch_from_partner_and_replay(self):
        facts, events = load_fixture(None, partner=self.base)
        self.assertEqual(facts["windows"], self.scenario["facts"]["windows"])
        self.assertEqual(events, self.scenario["facts"] and
                         self.scenario["events"])
        with tempfile.TemporaryDirectory() as db_dir:
            player = ScenarioPlayer(
                facts, events,
                QuotaService(facts["windows"], facts["gates"]), db_dir)
            report = player.run()
        self.assertTrue(all(c["ok"] for c in report["checks"]))
        # 资料端 journal 只记录 exchanges 匹配请求；/fixture/* 拉取不入 journal
        with urllib.request.urlopen(
                f"{self.base}/fixture/journal", timeout=3) as resp:
            journal = json.loads(resp.read())
        self.assertEqual(journal, [])


if __name__ == "__main__":
    unittest.main()
