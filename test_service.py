"""额度服务的单元测试：额度、凭证、改时、交还对账与运营视图。"""

import unittest

from trail.model import (
    ClosureState,
    Conflict,
    GrantState,
    MoveState,
    PermitState,
    ReconcileError,
)
from trail.service import QuotaService


def build():
    svc = QuotaService(
        windows=[
            {"id": "w1", "direction": "up", "start": 1000, "until": 1900,
             "capacity": 20},
            {"id": "w2", "direction": "up", "start": 1900, "until": 2800,
             "capacity": 10},
        ],
        gates=[
            {"id": "A", "error_bound": 5, "max_offline": 300},
            {"id": "B", "error_bound": 3, "max_offline": 200},
        ],
    )
    return svc


class AllotmentTest(unittest.TestCase):
    def test_capacity_bound_and_direction_window_binding(self):
        svc = build()
        svc.allot("A", "w1", "g1", 12, at=0)
        svc.allot("B", "w1", "g2", 8, at=1)
        with self.assertRaises(Conflict):
            svc.allot("B", "w1", "g3", 1, at=2)  # 20 已分满
        # 另一窗口独立核算
        svc.allot("A", "w2", "g4", 10, at=3)
        ov = svc.overview()
        self.assertEqual([w["held"] for w in ov["windows"]], [20, 10])

    def test_group_count_deducted_whole_on_grant(self):
        svc = build()
        svc.allot("A", "w1", "g1", 12, at=0)
        svc.issue_permit("p1", "team-x", "A", "g1", 7, at=10)
        view = svc.overview()
        grant = view["windows"][0]["grants"][0]
        self.assertEqual(grant["committed"], 7)
        self.assertEqual(grant["idle"], 5)
        with self.assertRaises(Conflict):
            svc.issue_permit("p2", "team-y", "A", "g1", 6, at=11)
        # 同一团体不能持有第二张有效凭证
        with self.assertRaises(Conflict):
            svc.issue_permit("p3", "team-x", "A", "g1", 1, at=12)

    def test_lend_only_idle_quota_same_window(self):
        svc = build()
        svc.allot("A", "w1", "g1", 12, at=0)
        svc.issue_permit("p1", "team-x", "A", "g1", 7, at=10)
        # 占用中的 7 人不能借出，只能借 idle 的 5 人
        with self.assertRaises(Conflict):
            svc.lend("g1", "B", 6, at=20)
        svc.lend("g1", "B", 5, at=20, new_grant_id="g2")
        ov = svc.overview()
        w1 = next(w for w in ov["windows"] if w["window"] == "w1")
        self.assertEqual(w1["held"], 12)             # 同窗口划转不改变窗口占用
        self.assertEqual(w1["free_capacity"], 8)     # 20-12
        g1 = next(g for g in w1["grants"] if g["grant_id"] == "g1")
        g2 = next(g for g in w1["grants"] if g["grant_id"] == "g2")
        self.assertEqual((g1["count"], g1["earmarked"]), (7, 5))
        self.assertEqual(g2["count"], 5)


class MoveTest(unittest.TestCase):
    def _setup(self):
        svc = build()
        svc.allot("A", "w1", "g1", 12, at=0)
        svc.issue_permit("p1", "team-x", "A", "g1", 7, at=10)
        return svc

    def test_pending_while_old_right_may_still_be_used_offline(self):
        svc = self._setup()
        svc.report_disconnect("A", clock_error=1, at=100)  # 健康离线
        result = svc.move("team-x", "B", "w1", at=150)
        self.assertEqual(result["state"], MoveState.PENDING.value)
        # 等待期间不得签发任何新凭证
        self.assertEqual(
            svc.get_permit("p1")["state"], PermitState.ACTIVE.value)
        with self.assertRaises(Conflict):
            svc.decide_move("team-x", "confirm", at=160)
        cancel = svc.decide_move("team-x", "cancel", at=170)
        self.assertEqual(cancel["state"], MoveState.CANCELLED.value)

    def test_confirmed_when_clock_out_of_bound_voids_old_permit(self):
        svc = self._setup()
        svc.report_disconnect("A", clock_error=99, at=100)  # 超界
        result = svc.move("team-x", "B", "w1", at=150)
        self.assertEqual(result["state"], MoveState.CONFIRMED.value)
        old = svc.get_permit("p1")
        self.assertEqual(old["state"], PermitState.VOID.value)
        new = svc.get_permit(result["new_permit"])
        self.assertEqual(new["gate"], "B")
        self.assertEqual(new["count"], 7)

    def test_confirmed_after_gate_closed(self):
        svc = self._setup()
        svc.closure_publish("up", at=90)
        svc.report_disconnect("A", clock_error=1, at=100)
        self.assertEqual(svc.move("team-x", "B", "w1", at=110)["state"],
                         MoveState.PENDING.value)
        svc.closure_closed("A", "up", at=120)
        decision = svc.decide_move("team-x", "confirm", at=130)
        self.assertEqual(decision["state"], MoveState.CONFIRMED.value)

    def test_pending_closes_released_when_record_arrives(self):
        svc = self._setup()
        svc.report_disconnect("A", clock_error=1, at=100)
        svc.move("team-x", "B", "w1", at=110)
        svc.report_sync("A", at=200)
        svc.accept_use_record("A", "g1", 1, "p1", 7, 150, at=210)
        self.assertEqual(svc.moves["team-x"]["state"],
                         MoveState.CLOSED_RELEASED.value)
        self.assertEqual(svc.get_permit("p1")["state"],
                         PermitState.RELEASED.value)


class ReturnAndReconcileTest(unittest.TestCase):
    def _setup(self):
        svc = build()
        svc.allot("A", "w1", "g1", 12, at=0)
        svc.issue_permit("p1", "team-x", "A", "g1", 7, at=10)
        svc.issue_permit("p2", "team-y", "A", "g1", 3, at=11)
        return svc

    def test_return_before_records_keeps_shadow_and_blocks_reuse(self):
        svc = self._setup()
        svc.report_disconnect("A", clock_error=1, at=100)
        svc.request_return("A", "g1", 5, at=110)
        # 先报交还，use_record 尚未补传：可安全收回的只有未被凭证占用的 2 人
        out = svc.record_return("A", "g1", 5, last_use_no=1,
                                reported_consumed=7, at=200)
        self.assertEqual(out["booked_returned"], 2)
        self.assertEqual(out["exposed_return"], 3)
        self.assertEqual(out["state"], GrantState.PARTIAL.value)
        g1 = next(g for g in svc.overview()["windows"][0]["grants"]
                  if g["grant_id"] == "g1")
        self.assertEqual(g1["count"], 7)           # 12 - 安全 2 - 敞口 3
        self.assertEqual(g1["unaccounted"], 3)     # 超报 3 进独立敞口桶
        self.assertEqual(g1["shadow"], 10)         # active 7 + 敞口 3
        self.assertEqual(g1["reclaimable"], 0)     # shadow 不得挪给别处
        self.assertEqual(g1["missing_use_nos"], [1])
        self.assertGreaterEqual(
            svc.overview()["windows"][0]["free_capacity"], 0)
        # 申报多交还的 3 人对应凭证已无法安全履约
        stranded = {p["permit"]: p["stranded_reason"]
                    for p in svc.overview()["stranded_permits"]}
        self.assertTrue(any(s for s in stranded.values()))
        # 补传早先放行 7：3 由敞口重分类为放行，4 从持有转出；p2 仍 active
        svc.accept_use_record("A", "g1", 1, "p1", 7, 150, at=210)
        g1 = next(g for g in svc.overview()["windows"][0]["grants"]
                  if g["grant_id"] == "g1")
        self.assertEqual(g1["state"], GrantState.PARTIAL.value)
        self.assertEqual(g1["count"], 3)
        self.assertEqual(g1["released"], 7)
        self.assertEqual(g1["returned"], 2)
        self.assertEqual(g1["unaccounted"], 0)
        self.assertEqual(g1["shadow"], 3)          # 只剩 p2
        # 再交还 p2 那 3 人：编号连续性到齐、active 清空，最终对账把
        # p2 作为无法安全履约的线下凭证作废，敞口转为正式交还
        svc.record_return("A", "g1", 3, last_use_no=1,
                          reported_consumed=7, at=220)
        g1 = next(g for g in svc.overview()["windows"][0]["grants"]
                  if g["grant_id"] == "g1")
        self.assertEqual(g1["state"], GrantState.RECONCILED.value)
        self.assertEqual(g1["shadow"], 0)
        self.assertEqual(g1["count"], 0)
        self.assertEqual(g1["returned"], 5)        # 安全 2 + 敞口转交还 3
        self.assertEqual(g1["released"], 7)
        self.assertEqual(svc.permits["p2"].state.value, "void")

    def test_partial_return_bookkeeping(self):
        svc = self._setup()
        out = svc.record_return("A", "g1", 2, last_use_no=0,
                                reported_consumed=0, at=50)
        self.assertEqual(out["booked_returned"], 2)
        g1 = next(g for g in svc.overview()["windows"][0]["grants"]
                  if g["grant_id"] == "g1")
        self.assertEqual(g1["returned"], 2)
        self.assertEqual(g1["count"], 10)         # 12 - 2，只交回一部分
        # 仍有 active 凭证占用，即使 last_use_no=0 也保持 PARTIAL
        self.assertEqual(g1["state"], GrantState.PARTIAL.value)
        self.assertEqual(g1["missing_use_nos"], [])
        self.assertEqual(g1["shadow"], 10)        # 两凭证仍可能现场使用
        self.assertEqual(g1["reclaimable"], 0)    # 10 都被 active 凭证占用

    def test_reported_consumed_mismatch_raises(self):
        # 单凭证场景：全部交还后补传记录，连续性到齐即核对报扣口径
        svc = build()
        svc.allot("A", "w1", "g1", 7, at=0)
        svc.issue_permit("p1", "team-x", "A", "g1", 7, at=10)
        svc.record_return("A", "g1", 7, last_use_no=1,
                          reported_consumed=9, at=50)
        with self.assertRaises(ReconcileError):
            svc.accept_use_record("A", "g1", 1, "p1", 7, 40, at=60)

    def test_missing_use_no_prevents_reconcile(self):
        svc = self._setup()
        svc.record_return("A", "g1", 2, last_use_no=2,
                          reported_consumed=10, at=50)
        svc.accept_use_record("A", "g1", 2, "p2", 3, 40, at=60)
        g1 = svc.grants["g1"]
        self.assertNotEqual(g1.state, GrantState.RECONCILED)
        self.assertEqual(
            svc._grant_view(g1)["missing_use_nos"], [1])
        svc.accept_use_record("A", "g1", 1, "p1", 7, 30, at=70)
        self.assertEqual(g1.state, GrantState.RECONCILED)

    def test_use_record_idempotent_and_double_release_rejected(self):
        svc = self._setup()
        first = svc.accept_use_record("A", "g1", 1, "p1", 7, 30, at=60)
        again = svc.accept_use_record("A", "g1", 1, "p1", 7, 30, at=61)
        self.assertTrue(first["accepted"])
        self.assertTrue(again["duplicate"])
        with self.assertRaises(ReconcileError):
            svc.accept_use_record("A", "g1", 2, "p1", 7, 31, at=62)

    def test_reconcile_after_lent_quota_full_return(self):
        # 模拟 fixture2：先划转再整笔交还
        svc = build()
        svc.allot("A", "w1", "g1", 13, at=0)
        svc.issue_permit("p1", "team-x", "A", "g1", 11, at=10)
        svc.report_disconnect("A", clock_error=99, at=100)
        svc.move("team-x", "B", "w1", at=110)   # 旧凭证 VOID，11 人随划转
        svc.request_return("A", "g1", 13, at=120)
        out = svc.record_return("A", "g1", 13, last_use_no=0,
                                reported_consumed=0, at=200)
        self.assertEqual(out["booked_returned"], 2)   # 11 已随划转离开
        # 无核验记录且无 active 凭证（旧证已 VOID），连续性天然到齐 → reconciled
        self.assertEqual(out["state"], GrantState.RECONCILED.value)
        g1 = svc.grants["g1"]
        self.assertEqual((g1.count, g1.earmarked), (0, 11))


class ClosureTest(unittest.TestCase):
    def test_three_states_distinct_from_return(self):
        svc = build()
        svc.allot("A", "w1", "g1", 12, at=0)
        svc.issue_permit("p1", "team-x", "A", "g1", 7, at=10)
        svc.closure_publish("up", at=90)
        info = svc.closures["up"]
        self.assertEqual(info["gates"].get("A"), None)
        svc.closure_seen("A", at=95)
        self.assertEqual(info["gates"]["A"][0], ClosureState.SEEN)
        svc.closure_closed("A", "up", at=100)
        self.assertEqual(info["gates"]["A"][0], ClosureState.CLOSED)
        # 停开本身不动任何额度
        g1 = svc.grants["g1"]
        self.assertEqual((g1.count, g1.returned), (12, 0))
        # 无法安全履约的凭证要在运营视图可见
        stranded = svc.overview()["stranded_permits"]
        self.assertEqual([p["permit"] for p in stranded], ["p1"])

    def test_cross_window_releases_not_mixed(self):
        svc = build()
        svc.allot("A", "w1", "g1", 10, at=0)
        svc.allot("B", "w2", "g2", 10, at=1)
        svc.issue_permit("p1", "team-x", "A", "g1", 4, at=10)
        svc.accept_use_record("A", "g1", 1, "p1", 4, 500, at=600)
        ov = {w["window"]: w for w in svc.overview()["windows"]}
        self.assertEqual(ov["w1"]["released"], 4)
        self.assertEqual(ov["w2"]["released"], 0)
        # w2 容量已全部分给 g2（free_capacity=0），但无人使用，
        # 10 人都可调剂给排队闸口
        self.assertEqual(ov["w2"]["free_capacity"], 0)
        self.assertEqual(ov["w2"]["reclaimable"], 10)
        # w1 放行 4 后持有剩 6，无凭证占用即可调剂
        self.assertEqual(ov["w1"]["held"], 6)
        self.assertEqual(ov["w1"]["reclaimable"], 6)


class PersistenceTest(unittest.TestCase):
    def test_snapshot_roundtrip(self):
        svc = build()
        svc.allot("A", "w1", "g1", 12, at=0)
        svc.issue_permit("p1", "team-x", "A", "g1", 7, at=10)
        svc.report_disconnect("A", 9, at=100)   # 超过 A 的误差界 5
        restored = QuotaService.from_dict(svc.to_dict())
        self.assertEqual(restored.get_permit("p1")["count"], 7)
        self.assertFalse(restored.gate_status["A"]["healthy"])
        self.assertEqual(restored.overview()["windows"][0]["held"], 12)

    def test_snapshot_preserves_ledger_buckets(self):
        svc = build()
        svc.allot("A", "w1", "g1", 12, at=0)
        svc.issue_permit("p1", "team-x", "A", "g1", 7, at=10)
        svc.issue_permit("p2", "team-y", "A", "g1", 3, at=11)
        svc.lend("g1", "B", 2, at=40, new_grant_id="g9")
        svc.record_return("A", "g1", 5, last_use_no=1,
                          reported_consumed=7, at=50)
        before = svc.overview()
        restored = QuotaService.from_dict(svc.to_dict())
        after = restored.overview()
        for key in ("held", "shadow", "unaccounted", "reclaimable",
                    "free_capacity", "released"):
            self.assertEqual(
                [w[key] for w in before["windows"]],
                [w[key] for w in after["windows"]])
        g1 = next(g for w in after["windows"] for g in w["grants"]
                  if g["grant_id"] == "g1")
        # 划转 2 后 idle 为 0，申报交还 5 全部形成敞口
        self.assertEqual(g1["unaccounted"], 5)
        self.assertEqual(g1["earmarked"], 2)


if __name__ == "__main__":
    unittest.main()
