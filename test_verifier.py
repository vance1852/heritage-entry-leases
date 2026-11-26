"""闸口本地核验程序的单元测试：断网、时钟误差、重启防重放。"""

import tempfile
import unittest
from pathlib import Path

from trail.model import DenyReason
from trail.verifier import GateVerifier


class VerifierTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "gate.sqlite"
        self.v = GateVerifier("A", self.db, error_bound=5, max_offline=300)
        self.bundle = {
            "windows": [
                {"id": "w1", "direction": "up", "start": 1000,
                 "until": 1900, "capacity": 20}
            ],
            "grants": [
                {"grant_id": "g1", "gate": "A", "window": "w1", "count": 12}
            ],
            "permits": [
                {"permit": "p1", "group": "t1", "gate": "A",
                 "grant_id": "g1", "window": "w1", "count": 7,
                 "state": "active", "superseded_by": None}
            ],
            "closures": [],
        }
        self.v.apply_bundle(self.bundle, at=1000)

    def tearDown(self):
        self.v.close()
        self.tmp.cleanup()

    def _restart(self):
        """模拟闸机进程重启：同一 sqlite 重新拉起。"""
        self.v.close()
        self.v = GateVerifier("A", self.db, 5, 300)


class ScanHappyPathTest(VerifierTestBase):
    def test_offline_scan_writes_durable_use_record(self):
        out = self.v.scan("p1", local_time=1200, at=1200)
        self.assertTrue(out["allowed"])
        self.assertEqual(out["use_no"], 1)
        self.assertEqual(out["count"], 7)
        records = self.v.pending_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["permit"], "p1")

    def test_rescan_after_restart_is_denied_already_used(self):
        self.v.scan("p1", local_time=1200, at=1200)
        self._restart()
        out = self.v.scan("p1", local_time=1210, at=1210)
        self.assertFalse(out["allowed"])
        self.assertEqual(out["reason"], DenyReason.ALREADY_USED.value)
        # 没有产生第二条放行记录
        self.assertEqual(len(self.v.pending_records()), 1)

    def test_use_no_continuous_per_grant(self):
        self.bundle["permits"].append(
            {"permit": "p2", "group": "t2", "gate": "A", "grant_id": "g1",
             "window": "w1", "count": 3, "state": "active",
             "superseded_by": None})
        self.v.apply_bundle(self.bundle, at=1000)
        self.v.scan("p1", 1200, 1200)
        self.v.scan("p2", 1210, 1210)
        self.assertEqual(
            [r["use_no"] for r in self.v.pending_records()], [1, 2])


class DenyPathTest(VerifierTestBase):
    def test_unknown_permit_and_wrong_gate(self):
        out = self.v.scan("nope", 1200, 1200)
        self.assertEqual(out["reason"], DenyReason.UNKNOWN_PERMIT.value)
        other = GateVerifier("B", Path(self.tmp.name) / "b.sqlite", 5, 300)
        other.apply_bundle({**self.bundle, "grants": [], "permits": []}, 1000)
        # B 机没有 p1 档案
        self.assertEqual(other.scan("p1", 1200, 1200)["reason"],
                         DenyReason.UNKNOWN_PERMIT.value)
        other.close()

    def test_window_with_clock_uncertainty(self):
        # 1000 开窗，误差界 5：local_time=1003 时最早可信 998 < 1000，拒绝
        early = self.v.scan("p1", 1003, 1003)
        self.assertEqual(early["reason"], DenyReason.WINDOW_CLOSED.value)
        # 1005 时最早可信恰为 1000，放行
        ok = self.v.scan("p1", 1005, 1005)
        self.assertTrue(ok["allowed"])

    def test_clock_error_out_of_bound_kills_offline_mode(self):
        capable = self.v.report_clock_error(8, at=1000)  # 界为 5
        self.assertFalse(capable)
        out = self.v.scan("p1", 1200, 1200)
        self.assertEqual(out["reason"],
                         DenyReason.CLOCK_OUT_OF_BOUND.value)
        # 可信对时后恢复
        self.v.mark_synced(1100)
        ok = self.v.scan("p1", 1200, 1200)
        self.assertTrue(ok["allowed"])

    def test_offline_limit_enforced(self):
        # 最后对时 1000，max_offline 300：local_time 1301 即超界
        out = self.v.scan("p1", 1301, 1301)
        self.assertEqual(out["reason"], DenyReason.OFFLINE_LIMIT.value)

    def test_revoked_permit_refused(self):
        self.bundle["permits"][0]["state"] = "superseded"
        self.bundle["permits"][0]["superseded_by"] = "p1-new"
        self.v.apply_bundle(self.bundle, at=1000)
        out = self.v.scan("p1", 1200, 1200)
        self.assertEqual(out["reason"], DenyReason.REVOKED.value)

    def test_gate_closed_refuses_direction(self):
        self.v.mark_closed("up", at=950)
        out = self.v.scan("p1", 1200, 1200)
        self.assertEqual(out["reason"], DenyReason.GATE_CLOSED.value)

    def test_grant_exhausted(self):
        self.bundle["grants"][0]["count"] = 7
        self.bundle["permits"][0]["count"] = 7
        self.v.apply_bundle(self.bundle, at=1000)
        self.assertTrue(self.v.scan("p1", 1200, 1200)["allowed"])
        self.bundle["permits"][0]["permit"] = "p2"
        self.bundle["permits"][0]["group"] = "t2"
        self.v.apply_bundle(self.bundle, at=1000)
        out = self.v.scan("p2", 1210, 1210)
        self.assertEqual(out["reason"], DenyReason.GRANT_EXHAUSTED.value)

    def test_denials_are_audited(self):
        self.v.scan("ghost", 1200, 1200)
        denials = self.v.denials()
        self.assertEqual(len(denials), 1)
        self.assertEqual(denials[0]["reason"],
                         DenyReason.UNKNOWN_PERMIT.value)


class TwoGatesTest(unittest.TestCase):
    """两个闸口分别断网时，同一团体也不能被放行两次。"""

    def test_same_permit_only_lives_on_authorized_gate(self):
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name)
        a = GateVerifier("A", base / "a.sqlite", 5, 300)
        b = GateVerifier("B", base / "b.sqlite", 5, 300)
        bundle_a = {
            "windows": [{"id": "w1", "direction": "up", "start": 1000,
                         "until": 1900, "capacity": 20}],
            "grants": [{"grant_id": "g1", "gate": "A",
                        "window": "w1", "count": 12}],
            "permits": [{"permit": "p1", "group": "t1", "gate": "A",
                         "grant_id": "g1", "window": "w1", "count": 7,
                         "state": "active", "superseded_by": None}],
            "closures": [],
        }
        a.apply_bundle(bundle_a, 900)
        b.apply_bundle({**bundle_a, "grants": [], "permits": []}, 900)
        self.assertTrue(a.scan("p1", 1200, 1200)["allowed"])
        # B 闸从未获得该凭证的离线授权
        self.assertEqual(b.scan("p1", 1200, 1200)["reason"],
                         DenyReason.UNKNOWN_PERMIT.value)
        a.close()
        b.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
