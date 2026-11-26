"""通行额度服务。

线程安全的内存服务（可通过 :meth:`QuotaService.to_dict` 落盘恢复），实现：

* 额度按窗口+方向分配，团体人数整笔扣在对应 grant 上；
* 闲置额度在同窗口内跨闸口借出，保留划转血缘；
* 团体改时/换入口的安全确认：旧凭证仍可能在现场使用时只能等待；
* 交还通知与临时停开分离，停开三态分别跟踪；
* 重连对账：``last_use_no`` 连续性核对、``reported_consumed`` 与独立核验记录核对；
* 运营视图：每闸口放行、shadow（仍须留给可能发生放行的额度）、可调剂额度、
  无法安全履约的线下凭证。
"""

import threading
from dataclasses import asdict

from .model import (
    ClosureState,
    Conflict,
    Grant,
    GrantState,
    MoveState,
    NotFound,
    Permit,
    PermitState,
    ReconcileError,
    TrailError,
    Window,
    Gate,
)


class QuotaService:
    def __init__(self, windows, gates):
        self._lock = threading.RLock()
        self.windows: dict[str, Window] = {}
        for item in windows:
            win = Window(**item) if isinstance(item, dict) else item
            self.windows[win.id] = win
        self.gates: dict[str, Gate] = {}
        for item in gates:
            gate = Gate(**item) if isinstance(item, dict) else item
            self.gates[gate.id] = gate

        self.grants: dict[str, Grant] = {}
        self.permits: dict[str, Permit] = {}
        self.group_permit: dict[str, str] = {}          # group -> 当前凭证 id
        self.moves: dict[str, dict] = {}                # group -> 改时申请
        self.gate_status: dict[str, dict] = {}
        self.closures: dict[str, dict] = {}             # direction -> 通知状态
        self.transfer_ledger: list[dict] = []
        self.event_log: list[dict] = []

        for gate_id in self.gates:
            self.gate_status[gate_id] = {
                "online": True,
                "last_clock_error": 0,
                "healthy": True,
                "last_sync_at": None,
                "disconnect_at": None,
            }

    # ------------------------------------------------------------------ 基础查询
    def _grant(self, grant_id) -> Grant:
        try:
            return self.grants[grant_id]
        except KeyError:
            raise NotFound(f"授权额度 {grant_id} 不存在")

    def _window_grants(self, window_id):
        return [g for g in self.grants.values() if g.window == window_id]

    def _active_permits(self, grant_id=None, gate=None):
        items = self.permits.values()
        if grant_id is not None:
            items = (p for p in items if p.grant_id == grant_id)
        if gate is not None:
            items = (p for p in items if p.gate == gate)
        return [p for p in items if p.state == PermitState.ACTIVE]
    def _committed_count(self, grant: Grant) -> int:
        return sum(p.count for p in self._active_permits(grant_id=grant.grant_id))

    def _shadow_of(self, grant: Grant) -> int:
        """仍可能已离线放行、但核验记录尚未到齐的额度。

        上界为该授权上仍 active 的凭证人数；已被申报交还覆盖的敞口
        （unaccounted）同样不得挪用，计入 shadow。grant 完成连续性
        对账后 shadow 归零。
        """
        if grant.state == GrantState.RECONCILED:
            return 0
        committed = min(self._committed_count(grant), grant.count)
        return committed + grant.unaccounted

    def _over_committed(self, grant: Grant) -> int:
        """active 凭证超出名义持有额度的部分：已无法安全履约的线下敞口。"""
        if grant.state == GrantState.RECONCILED:
            return 0
        return max(0, self._committed_count(grant)
                   - grant.count - grant.unaccounted)

    # ------------------------------------------------------------------ 额度分配
    def allot(self, gate, window, grant_id, count, at):
        """登记一笔分配给闸口的授权额度。"""
        with self._lock:
            if gate not in self.gates:
                raise NotFound(f"闸口 {gate} 不存在")
            win = self.windows.get(window)
            if win is None:
                raise NotFound(f"窗口 {window} 不存在")
            if grant_id in self.grants:
                raise Conflict(f"授权额度 {grant_id} 已存在")
            held = sum(g.count for g in self._window_grants(window))
            if held + count > win.capacity:
                raise Conflict(
                    f"窗口 {window} 容量 {win.capacity} 已分配 {held}，"
                    f"再分配 {count} 超界"
                )
            grant = Grant(grant_id=grant_id, gate=gate, window=window,
                          allotment=count, count=count)
            self.grants[grant_id] = grant
            self._log("allot", at, gate=gate, window=window,
                      grant_id=grant_id, count=count)
            return self._grant_view(grant)

    def lend(self, grant_id, to_gate, count, at, new_grant_id=None):
        """把某闸口几乎用不掉的闲置额度借给正在排队的闸口（同窗口）。

        只允许借出未被任何 active 凭证占用的闲置部分；划转在窗口内进行，
        来源授权保留 ``earmarked`` 血缘，窗口总持有额度不变。
        """
        with self._lock:
            source = self._grant(grant_id)
            if to_gate not in self.gates:
                raise NotFound(f"闸口 {to_gate} 不存在")
            if source.gate == to_gate:
                raise Conflict("借出与借入闸口相同")
            idle = source.count - self._committed_count(source)
            if count <= 0 or count > idle:
                raise Conflict(
                    f"授权 {grant_id} 闲置额度 {idle}，无法借出 {count}"
                )
            target = next(
                (g for g in self._window_grants(source.window) if g.gate == to_gate
                 and g.state in (GrantState.ACTIVE, GrantState.PARTIAL)),
                None,
            )
            if target is None:
                if not new_grant_id:
                    raise Conflict(f"闸口 {to_gate} 在窗口 {source.window} 无授权，"
                                   "需提供 new_grant_id 新建")
                if new_grant_id in self.grants:
                    raise Conflict(f"授权额度 {new_grant_id} 已存在")
                target = Grant(grant_id=new_grant_id, gate=to_gate,
                               window=source.window, allotment=0, count=0)
                self.grants[new_grant_id] = target
            source.count -= count
            source.earmarked += count
            target.count += count
            target.allotment += count
            entry = {"from_grant": grant_id, "from_gate": source.gate,
                     "to_grant": target.grant_id, "to_gate": to_gate,
                     "window": source.window, "count": count}
            self.transfer_ledger.append({"at": at, **entry})
            self._log("lend", at, **entry)
            return {"from": self._grant_view(source),
                    "to": self._grant_view(target)}

    # ------------------------------------------------------------------ 凭证签发
    def issue_permit(self, permit, group, gate, grant_id, count, at):
        """登记团体通行凭证：人数整笔扣在对应授权上。"""
        with self._lock:
            if permit in self.permits:
                raise Conflict(f"凭证 {permit} 已存在")
            grant = self._grant(grant_id)
            if grant.gate != gate:
                raise Conflict(f"授权 {grant_id} 不属于闸口 {gate}")
            if grant.state in (GrantState.RECONCILED, GrantState.REVOKED):
                raise Conflict(f"授权 {grant_id} 已{grant.state.value}，不能再签凭证")
            current = self.group_permit.get(group)
            if current and self.permits[current].state == PermitState.ACTIVE:
                raise Conflict(f"团体 {group} 已持有有效凭证 {current}，"
                               "改时须走 move 申请")
            available = grant.count - self._committed_count(grant)
            if count <= 0 or count > available:
                raise Conflict(
                    f"授权 {grant_id} 剩余可扣 {available}，无法承接团体 {count} 人"
                )
            item = Permit(permit=permit, group=group, gate=gate,
                          grant_id=grant_id, window=grant.window, count=count)
            self.permits[permit] = item
            self.group_permit[group] = permit
            self._log("permit", at, permit=permit, group=group, gate=gate,
                      grant_id=grant_id, count=count)
            return self._permit_view(item)

    def get_permit(self, permit_id):
        """供闸口同步离线档案使用。"""
        with self._lock:
            item = self.permits.get(permit_id)
            if item is None:
                raise NotFound(f"凭证 {permit_id} 不存在")
            return self._permit_view(item)

    def gate_bundle(self, gate):
        """闸口（重）连时拉取的全部离线档案：本闸凭证、授权、停开通知。"""
        with self._lock:
            if gate not in self.gates:
                raise NotFound(f"闸口 {gate} 不存在")
            permits = [self._permit_view(p)
                       for p in self.permits.values() if p.gate == gate]
            grants = [self._grant_view(g)
                      for g in self._window_grants_of_gate(gate)]
            closures = []
            for direction, info in self.closures.items():
                state = info["gates"].get(gate)
                closures.append({
                    "direction": direction,
                    "published_at": info["published_at"],
                    "gate_state": state[0].value if state else None,
                    "gate_state_at": state[1] if state else None,
                })
            status = dict(self.gate_status[gate])
            return {"gate": gate, "permits": permits, "grants": grants,
                    "closures": closures, "gate_status": status,
                    "windows": [asdict(w) for w in self.windows.values()]}

    def _window_grants_of_gate(self, gate):
        return [g for g in self.grants.values() if g.gate == gate]

    # ------------------------------------------------------------------ 改时/换入口
    def move(self, group, target_gate, target_window, at, new_permit=None):
        """团体改时/临时换入口申请。

        旧时段通行权仍可能在现场使用时返回 ``pending``，不签发任何新权利；
        只有旧闸口的离线权确实停止（时钟超界、已 closed、或在线可送达吊销）
        且新窗口容量与授权足够时才 ``confirmed``。
        """
        with self._lock:
            old_id = self.group_permit.get(group)
            if not old_id:
                raise NotFound(f"团体 {group} 没有登记凭证")
            old = self.permits[old_id]
            if old.state == PermitState.RELEASED:
                return self._register_move(
                    group, target_gate, target_window, at,
                    MoveState.CLOSED_RELEASED,
                    "原凭证已在原闸口放行，不另发通行权", old)
            if old.state != PermitState.ACTIVE:
                raise Conflict(f"团体 {group} 凭证状态为 {old.state.value}，不能改时")
            if target_gate not in self.gates:
                raise NotFound(f"闸口 {target_gate} 不存在")
            if target_window not in self.windows:
                raise NotFound(f"窗口 {target_window} 不存在")

            blocker = self._offline_right_blocker(old)
            if blocker is not None:
                return self._register_move(
                    group, target_gate, target_window, at,
                    MoveState.PENDING, blocker, old)

            return self._confirm_move(old, group, target_gate,
                                      target_window, at, new_permit)

    def _offline_right_blocker(self, old_permit: Permit):
        """返回阻止立即换闸的原因；None 表示旧离线权已确实停止。"""
        status = self.gate_status[old_permit.gate]
        if status["online"]:
            return None  # 在线：吊销可实时送达，闸口不会再认旧证
        if not status["healthy"]:
            return None  # 时钟超界：该闸口不能继续离线放行
        direction = self.windows[old_permit.window].direction
        closure = self.closures.get(direction)
        if closure:
            state = closure["gates"].get(old_permit.gate)
            if state and state[0] == ClosureState.CLOSED:
                return None  # 已实际停止接受该方向通行权
        return ("旧闸口仍处于健康离线状态，原凭证可能在现场使用；"
                "需等待其重连对账或由值班员明确处置")

    def _register_move(self, group, target_gate, target_window, at,
                       state, reason, old_permit):
        entry = {
            "group": group,
            "from_gate": old_permit.gate,
            "from_window": old_permit.window,
            "from_permit": old_permit.permit,
            "to_gate": target_gate,
            "to_window": target_window,
            "state": state.value,
            "reason": reason,
            "at": at,
            "new_permit": None,
        }
        self.moves[group] = entry
        self._log("move", at, group=group, gate=target_gate,
                  window=target_window, state=state.value)
        return dict(entry)

    def _confirm_move(self, old_permit, group, target_gate,
                      target_window, at, new_permit):
        win = self.windows[target_window]
        # 跨窗口改时：团体人数整笔进入新窗口，需新窗口容量承接。
        if old_permit.window != target_window:
            held = sum(g.count for g in self._window_grants(target_window))
            if held + old_permit.count > win.capacity:
                raise Conflict(
                    f"窗口 {target_window} 容量 {win.capacity} 已持有 {held}，"
                    f"无法承接 {old_permit.count} 人")
        source = self._grant(old_permit.grant_id)
        target = next(
            (g for g in self._window_grants(target_window)
             if g.gate == target_gate and g.state in (GrantState.ACTIVE, GrantState.PARTIAL)),
            None,
        )
        # 目标闸口没有授权或余量不足时，从旧授权整笔划转（保留血缘），
        # 旧凭证随其人数一起离开旧授权。
        need = old_permit.count
        if target is None or target.count - self._committed_count(target) < need:
            if target is None:
                new_grant_id = f"{source.grant_id}-moved"
                while new_grant_id in self.grants:
                    new_grant_id += "-x"
                target = Grant(grant_id=new_grant_id, gate=target_gate,
                               window=target_window, allotment=0, count=0)
                self.grants[new_grant_id] = target
            shortfall = need - max(0, target.count - self._committed_count(target))
            if shortfall > source.count:
                raise Conflict(
                    f"旧授权 {source.grant_id} 持有 {source.count}，"
                    f"无法划出 {shortfall} 人到闸口 {target_gate}")
            source.count -= shortfall
            source.earmarked += shortfall
            target.count += shortfall
            target.allotment += shortfall
            self.transfer_ledger.append({
                "at": at, "from_grant": source.grant_id,
                "from_gate": source.gate, "to_grant": target.grant_id,
                "to_gate": target_gate, "window": target_window,
                "count": shortfall, "reason": "move",
            })
        new_id = new_permit or f"{old_permit.permit}-moved"
        if new_id in self.permits:
            raise Conflict(f"凭证 {new_id} 已存在")
        # 能确认换闸，说明旧离线权已确实停止（时钟超界/已 closed/在线吊销），
        # 旧凭证不可能再被放行，作废而不是保留为仍可能使用的 active。
        old_permit.state = PermitState.VOID
        old_permit.superseded_by = new_id
        old_permit.stranded_reason = None
        created = Permit(permit=new_id, group=group, gate=target_gate,
                         grant_id=target.grant_id, window=target_window,
                         count=old_permit.count)
        self.permits[new_id] = created
        self.group_permit[group] = new_id
        entry = {
            "group": group,
            "from_gate": old_permit.gate,
            "from_window": old_permit.window,
            "from_permit": old_permit.permit,
            "to_gate": target_gate,
            "to_window": target_window,
            "state": MoveState.CONFIRMED.value,
            "reason": "旧闸口离线权已停止，服务确认换闸并吊销旧凭证",
            "at": at,
            "new_permit": new_id,
        }
        self.moves[group] = entry
        self._log("move_confirmed", at, group=group, old_permit=old_permit.permit,
                  new_permit=new_id, gate=target_gate, window=target_window)
        return dict(entry)

    def decide_move(self, group, decision, at, new_permit=None):
        """值班员对 pending 改时申请的明确处置：confirm 或 cancel。"""
        with self._lock:
            entry = self.moves.get(group)
            if entry is None or entry["state"] != MoveState.PENDING.value:
                raise Conflict(f"团体 {group} 没有待决的改时申请")
            if decision == "cancel":
                entry["state"] = MoveState.CANCELLED.value
                entry["reason"] = "值班员取消，旅客继续使用原时段通行权"
                self._log("move_cancelled", at, group=group)
                return dict(entry)
            if decision != "confirm":
                raise TrailError("decision 只能是 confirm 或 cancel")
            old = self.permits[entry["from_permit"]]
            blocker = self._offline_right_blocker(old)
            if blocker is not None:
                raise Conflict(f"仍不能确认换闸：{blocker}")
            return self._confirm_move(old, group, entry["to_gate"],
                                      entry["to_window"], at, new_permit)

    # ------------------------------------------------------------------ 闸口状态
    def report_disconnect(self, gate, clock_error, at):
        with self._lock:
            if gate not in self.gates:
                raise NotFound(f"闸口 {gate} 不存在")
            status = self.gate_status[gate]
            status["online"] = False
            status["disconnect_at"] = at
            status["last_clock_error"] = clock_error
            bound = self.gates[gate].error_bound
            status["healthy"] = abs(clock_error) <= bound
            self._log("disconnect", at, gate=gate, clock_error=clock_error,
                      healthy=status["healthy"])
            if not status["healthy"]:
                # 时钟超界的闸口，其未使用凭证已无法安全履约
                for permit in self._active_permits(gate=gate):
                    permit.stranded_reason = (
                        f"clock_out_of_bound: 观测误差 {clock_error} "
                        f"超过误差界 {bound}"
                    )
            return dict(status)

    def report_sync(self, gate, at):
        with self._lock:
            status = self.gate_status[gate]
            status["online"] = True
            status["last_sync_at"] = at
            status["healthy"] = True
            self._log("sync", at, gate=gate)
            return dict(status)

    # ------------------------------------------------------------------ 停开通知
    def closure_publish(self, direction, at):
        """发布临时停开通知（不等同于额度收回）。"""
        with self._lock:
            if direction not in self.closures:
                self.closures[direction] = {
                    "published_at": at, "gates": {}
                }
            self._log("closure", at, direction=direction)
            return {"direction": direction, "published_at": at}

    def closure_seen(self, gate, at):
        with self._lock:
            direction = self._direction_of_gate_window(gate)
            info = self.closures.get(direction)
            if info is None:
                raise NotFound(f"方向 {direction} 没有停开通知")
            info["gates"][gate] = (ClosureState.SEEN, at)
            self._log("closure_seen", at, gate=gate)
            return {"gate": gate, "state": ClosureState.SEEN.value}

    def closure_closed(self, gate, direction, at):
        """闸口实际停止接受指定方向的通行权：离线权确实停止。"""
        with self._lock:
            info = self.closures.get(direction)
            if info is None:
                raise NotFound(f"方向 {direction} 没有停开通知")
            info["gates"][gate] = (ClosureState.CLOSED, at)
            for permit in self._active_permits(gate=gate):
                win = self.windows[permit.window]
                if win.direction == direction and permit.stranded_reason is None:
                    permit.stranded_reason = "gate_closed: 该方向已停止接受通行权"
            self._log("closed", at, gate=gate, direction=direction)
            return {"gate": gate, "direction": direction,
                    "state": ClosureState.CLOSED.value}

    def _direction_of_gate_window(self, gate):
        grants = self._window_grants_of_gate(gate)
        if not grants:
            raise NotFound(f"闸口 {gate} 没有授权，无法推断方向")
        return self.windows[grants[0].window].direction

    # ------------------------------------------------------------------ 交还与对账
    def request_return(self, gate, grant_id, count, at):
        """服务发出/登记交还通知：与停开是两件独立的事。

        交还通知按授权的原始责任口径（含已划转出去的额度），因为现场闸机
        持有的是原始授权编号，划转只发生在服务账本上。pending_return 只
        表达运营意图，不约束后续实际交还数量（可能只交回一部分）。
        """
        with self._lock:
            grant = self._grant(grant_id)
            if grant.gate != gate:
                raise Conflict(f"授权 {grant_id} 不属于闸口 {gate}")
            outstanding = (grant.allotment - grant.earmarked
                           - grant.returned - grant.released)
            if count <= 0 or count > outstanding + grant.earmarked:
                raise Conflict(
                    f"授权 {grant_id} 原始责任 {grant.allotment}，"
                    f"已划转 {grant.earmarked}、已还 {grant.returned}、"
                    f"已放行 {grant.released}，不能通知交还 {count}"
                )
            grant.pending_return += count
            self._log("request_return", at, gate=gate,
                      grant_id=grant_id, count=count)
            return self._grant_view(grant)

    def record_return(self, gate, grant_id, returned, last_use_no,
                      reported_consumed, at):
        """闸口重连补交的交还。

        可能先报交还再补传早先放行，也可能只交回一部分。申报交还分三类
        结算：已划转（earmarked）的额度在划转时即已离开本授权，只做记录；
        本授权 idle 部分可安全收回，从 count 扣减；覆盖到 active 凭证的
        部分计入 ``unaccounted`` 独立敞口桶（不得挪用、不可再调剂），
        对应凭证按整笔原则列入 stranded，待核验记录关闭或对账时处置。
        """
        with self._lock:
            grant = self._grant(grant_id)
            if grant.gate != gate:
                raise Conflict(f"授权 {grant_id} 不属于闸口 {gate}")
            liability = (grant.allotment - grant.earmarked
                         - grant.returned - grant.released
                         - grant.unaccounted)
            reclaimable = max(0, grant.count - self._committed_count(grant))
            # 申报交还里属于已划转额度的部分在划转时即已结算，只做记录、
            # 不再产生账上动作；其余按「可安全收回 / 覆盖凭证的敞口」分类。
            already_transferred = max(
                0, returned - max(0, liability))
            rest = returned - already_transferred
            safe = max(0, min(rest, reclaimable))
            exposed = max(0, rest - safe)
            if safe > grant.count:
                raise ReconcileError(
                    f"授权 {grant_id} 账目超界：持有 {grant.count}，"
                    f"本次申报安全交还 {safe}"
                )
            grant.count -= safe
            grant.returned += safe
            grant.unaccounted += exposed
            # 暴露敞口对应的额度立即移出持有（不得挪用、不可再调剂）
            grant.count -= exposed
            grant.pending_return = max(0, grant.pending_return - returned)
            grant.last_use_no = last_use_no
            grant.reported_consumed = reported_consumed
            if grant.state != GrantState.RECONCILED:
                grant.state = GrantState.PARTIAL
            if exposed > 0:
                # 申报交还覆盖到仍可能放行的凭证：标记为无法安全履约的
                # 重点关注对象（stranded_reason），但保留 active —— 核验
                # 记录可能随后到达并证明它确实已放行。是否最终作废，
                # 留待连续性到齐后的 _try_reconcile 决定。
                flagged = 0
                for permit in sorted(
                        self._active_permits(grant_id=grant_id),
                        key=lambda p: (abs(p.count - exposed), p.count)):
                    if flagged >= exposed:
                        break
                    permit.stranded_reason = (
                        f"over_returned: 闸口申报交还 {returned}，"
                        f"可安全收回仅 {reclaimable}，"
                        f"{exposed} 人额度可能已线下放行却被交还"
                    )
                    flagged += permit.count
            self._log("return", at, gate=gate, grant_id=grant_id,
                      returned=returned, booked=safe, exposed=exposed,
                      last_use_no=last_use_no,
                      reported_consumed=reported_consumed)
            self._try_reconcile(grant)
            result = self._grant_view(grant)
            result["booked_returned"] = safe
            result["exposed_return"] = exposed
            result["transferred_settled"] = already_transferred
            if already_transferred:
                result["note"] = (
                    f"申报交还 {returned} 中 {already_transferred} 人属于已划转"
                    "额度，划转时即已结算，本授权不再入账"
                )
            if exposed:
                result["note"] = (
                    f"申报交还 {returned}，可安全收回 {safe}；"
                    f"{exposed} 属于仍可能放行的 shadow，不得挪用，"
                    "已计入 unaccounted 并把相关凭证列入 stranded"
                )
            return result

    def accept_use_record(self, gate, grant_id, use_no, permit_id,
                          count, local_time, at):
        """接收闸机补传的独立核验记录（已放行事实）。

        * 同一 use_no 重复提交按幂等处理；
        * 同一凭证出现两个不同 use_no 视为重复放行，报对账错误；
        * 记录到齐后核对 1..last_use_no 的连续性与 reported_consumed。
        """
        with self._lock:
            grant = self._grant(grant_id)
            if grant.gate != gate:
                raise Conflict(f"授权 {grant_id} 不属于闸口 {gate}")
            if use_no in grant.use_seen:
                return {"grant_id": grant_id, "use_no": use_no,
                        "accepted": False, "duplicate": True}
            permit = self.permits.get(permit_id)
            if permit is None:
                raise ReconcileError(
                    f"use_no={use_no} 引用了未知凭证 {permit_id}"
                )
            if permit.grant_id != grant_id or permit.gate != gate:
                raise ReconcileError(
                    f"凭证 {permit_id} 与授权 {grant_id}/闸口 {gate} 不匹配"
                )
            if permit.state == PermitState.RELEASED and \
                    permit.released_use_no != use_no:
                raise ReconcileError(
                    f"凭证 {permit_id} 已在 use_no={permit.released_use_no} 放行，"
                    f"又出现 use_no={use_no}，疑似重复放行"
                )
            if permit.state == PermitState.SUPERSEDED:
                raise ReconcileError(
                    f"凭证 {permit_id} 已被替换，不应再出现核验记录"
                )
            grant.use_seen.append(use_no)
            grant.released += count
            # 已放行事实消费本授权持有额度：从 count 转出到 released。
            # 若该笔此前被申报交还并形成 unaccounted 敞口（额度已在独立
            # 敞口桶中），核验记录证明它其实是放行：直接把对应额度从
            # unaccounted 重分类到 released，不再触碰 count/returned。
            closed = min(grant.unaccounted, count)
            grant.unaccounted -= closed
            grant.count -= (count - closed)
            # 已放行事实优先于先前的安全作废推断：VOID 凭证转为 RELEASED
            permit.state = PermitState.RELEASED
            permit.released_use_no = use_no
            permit.stranded_reason = None
            move = self.moves.get(permit.group)
            if move and move["state"] == MoveState.PENDING.value:
                move["state"] = MoveState.CLOSED_RELEASED.value
                move["reason"] = "等待期间原凭证确认已在原闸放行，不另发权利"
            self._log("use_record", at, gate=gate, grant_id=grant_id,
                      use_no=use_no, permit=permit_id, count=count,
                      local_time=local_time)
            self._try_reconcile(grant)
            return {"grant_id": grant_id, "use_no": use_no,
                    "accepted": True, "duplicate": False,
                    "state": grant.state.value,
                    "grant": self._grant_view(grant)}

    def _try_reconcile(self, grant: Grant):
        """连续性到齐且本授权额度全部关闭后，对账完成、shadow 归零。

        额度关闭有三条路径：安全交还（returned）、核验放行（released）、
        划转（earmarked）。记录补传与交还的先后都可能出现（先报交还再
        补传，或先补传后交还），因此两个入口都会调用本方法。到齐后若仍有
        unaccounted 敞口，说明线下凭证已无法安全履约，标记为 stranded 关闭。
        """
        if grant.state == GrantState.RECONCILED:
            return
        if grant.last_use_no is None:
            return
        expected = set(range(1, grant.last_use_no + 1))
        if set(grant.use_seen) != expected:
            return
        # 仍有未被交还敞口覆盖的 active 凭证：这些凭证在现场仍可能被扫，
        # 保留 PARTIAL 与 shadow；已由 unaccounted 覆盖的承诺不算在内，
        # 它们将在本方法末尾按 stranded 处置。
        live_commitment = max(0, self._committed_count(grant)
                              - grant.unaccounted)
        if live_commitment > 0:
            return        # 账式恒等式：持有 + 放行 + 划转 + 已交还 + 敞口 = 原始分配
        total = (grant.count + grant.released + grant.earmarked
                 + grant.returned + grant.unaccounted)
        if total != grant.allotment:
            raise ReconcileError(
                f"授权 {grant.grant_id} 账目不平：分配 {grant.allotment} != "
                f"持有 {grant.count} + 放行 {grant.released} + "
                f"划转 {grant.earmarked} + 交还 {grant.returned} + "
                f"敞口 {grant.unaccounted}"
            )
        if grant.unaccounted:
            # 连续性到齐却仍有交还敞口：闸口声称交还、却无核验记录证明放行，
            # 相关线下凭证已无法安全履约。敞口额度正式转为交还，凭证作废。
            for permit in self._active_permits(grant_id=grant.grant_id):
                if permit.stranded_reason is None:
                    permit.stranded_reason = (
                        f"unreconciled: 连续性已到齐但仍有 "
                        f"{grant.unaccounted} 人交还敞口无核验记录，"
                        "线下凭证无法安全履约，额度按安全策略收回"
                    )
                permit.state = PermitState.VOID
            grant.returned += grant.unaccounted
            grant.unaccounted = 0
        # 连续性到齐、账目恒等、无敞口即对账完成；仍持有的 count 是
        # 已安全收回、可再调剂的额度（reconciled 状态下 shadow 为零）。
        grant.state = GrantState.RECONCILED
        if grant.reported_consumed is not None \
                and grant.reported_consumed != grant.released:
            raise ReconcileError(
                f"授权 {grant.grant_id} 对账不符：现场报扣 "
                f"{grant.reported_consumed}，独立核验记录合计 {grant.released}"
            )

    # ------------------------------------------------------------------ 运营视图
    def _grant_view(self, grant: Grant):
        committed = self._committed_count(grant)
        shadow = self._shadow_of(grant)
        return {
            "grant_id": grant.grant_id,
            "gate": grant.gate,
            "window": grant.window,
            "allotment": grant.allotment,
            "count": grant.count,                # 当前名义持有
            "state": grant.state.value,
            "committed": committed,              # active 凭证整笔占用
            "idle": max(0, grant.count - committed),
            "returned": grant.returned,
            "released": grant.released,
            "earmarked": grant.earmarked,
            "unaccounted": grant.unaccounted,    # 交还敞口（不得挪用）
            "pending_return": grant.pending_return,
            "shadow": shadow,                    # 须留给可能发生的离线放行
            "over_committed": self._over_committed(grant),
            "reclaimable": max(0, grant.count - committed),
            "reported_consumed": grant.reported_consumed,
            "last_use_no": grant.last_use_no,
            "use_seen": sorted(grant.use_seen),
            "missing_use_nos": self._missing_use_nos(grant),
        }

    @staticmethod
    def _missing_use_nos(grant: Grant):
        if grant.last_use_no is None:
            return []
        seen = set(grant.use_seen)
        return [n for n in range(1, grant.last_use_no + 1) if n not in seen]

    def _permit_view(self, permit: Permit):
        return {
            "permit": permit.permit,
            "group": permit.group,
            "gate": permit.gate,
            "grant_id": permit.grant_id,
            "window": permit.window,
            "count": permit.count,
            "state": permit.state.value,
            "superseded_by": permit.superseded_by,
            "released_use_no": permit.released_use_no,
            "stranded_reason": permit.stranded_reason,
        }

    def overview(self):
        """运营端总览：每个窗口/闸口的放行、shadow、可调额度与滞留凭证。"""
        with self._lock:
            windows = []
            for win in self.windows.values():
                grants = self._window_grants(win.id)
                held = sum(g.count for g in grants)
                released = sum(g.released for g in grants)
                shadow = sum(self._shadow_of(g) for g in grants)
                reclaimable = sum(
                    max(0, g.count - self._committed_count(g)) for g in grants
                )
                windows.append({
                    "window": win.id,
                    "direction": win.direction,
                    "start": win.start,
                    "until": win.until,
                    "capacity": win.capacity,
                    "held": held,                          # 当前各闸实际持有
                    "earmarked_out": sum(g.earmarked for g in grants),
                    "released": released,                  # 已确认放行（本窗口内核算）
                    "shadow": shadow,                      # 须留给可能发生的放行
                    "unaccounted": sum(g.unaccounted for g in grants),
                    # 可新增分配的窗口余量 vs 可立即转给排队闸口的额度，两者不同
                    "free_capacity": win.capacity - held,
                    "reclaimable": reclaimable,
                    "grants": [self._grant_view(g) for g in grants],
                })
            gates = [self.gate_view(gate_id) for gate_id in self.gates]
            stranded = [self._permit_view(p) for p in self.permits.values()
                        if p.stranded_reason is not None]
            return {
                "windows": windows,
                "gates": gates,
                "stranded_permits": stranded,
                "pending_moves": [m for m in self.moves.values()
                                  if m["state"] == MoveState.PENDING.value],
                "transfers": list(self.transfer_ledger),
            }

    def gate_view(self, gate):
        """单个入口的放行与剩余可调剂量。"""
        with self._lock:
            if gate not in self.gates:
                raise NotFound(f"闸口 {gate} 不存在")
            grants = [self._grant_view(g)
                      for g in self._window_grants_of_gate(gate)]
            permits = [self._permit_view(p)
                       for p in self.permits.values() if p.gate == gate]
            closures = []
            for direction, info in self.closures.items():
                state = info["gates"].get(gate)
                closures.append({
                    "direction": direction,
                    "published_at": info["published_at"],
                    "state": state[0].value if state else ClosureState.PUBLISHED.value,
                    "state_at": state[1] if state else None,
                })
            status = dict(self.gate_status[gate])
            return {
                "gate": gate,
                "gate_status": status,
                "error_bound": self.gates[gate].error_bound,
                "max_offline": self.gates[gate].max_offline,
                "closures": closures,
                "grants": grants,
                "permits": permits,
                "released_total": sum(g["released"] for g in grants),
                "shadow_total": sum(g["shadow"] for g in grants),
                # 可调剂 = 持有中未被任何凭证占用的 idle（shadow 是 committed
                # 的子集，本就不在 idle 内）
                "reclaimable_total": sum(g["reclaimable"] for g in grants),
                "idle_total": sum(g["idle"] for g in grants),
            }

    # ------------------------------------------------------------------ 持久化
    def to_dict(self):
        with self._lock:
            return {
                "windows": [asdict(w) for w in self.windows.values()],
                "gates": [asdict(g) for g in self.gates.values()],
                "grants": [asdict(g) for g in self.grants.values()],
                "permits": [asdict(p) for p in self.permits.values()],
                "group_permit": dict(self.group_permit),
                "moves": dict(self.moves),
                "gate_status": {k: dict(v) for k, v in self.gate_status.items()},
                "closures": {
                    d: {"published_at": info["published_at"],
                        "gates": {g: [s.value, t] for g, (s, t)
                                  in info["gates"].items()}}
                    for d, info in self.closures.items()
                },
                "transfer_ledger": list(self.transfer_ledger),
                "event_log": list(self.event_log),
            }

    @classmethod
    def from_dict(cls, data):
        svc = cls(data["windows"], data["gates"])
        with svc._lock:
            svc.grants = {}
            for raw in data["grants"]:
                raw = dict(raw)
                raw["state"] = GrantState(raw["state"])
                svc.grants[raw["grant_id"]] = Grant(**raw)
            svc.permits = {}
            for raw in data["permits"]:
                raw = dict(raw)
                raw["state"] = PermitState(raw["state"])
                svc.permits[raw["permit"]] = Permit(**raw)
            svc.group_permit = dict(data["group_permit"])
            svc.moves = dict(data["moves"])
            svc.gate_status = {k: dict(v) for k, v in data["gate_status"].items()}
            svc.closures = {
                d: {"published_at": info["published_at"],
                    "gates": {g: (ClosureState(s), t)
                              for g, (s, t) in info["gates"].items()}}
                for d, info in data.get("closures", {}).items()
            }
            svc.transfer_ledger = list(data.get("transfer_ledger", []))
            svc.event_log = list(data.get("event_log", []))
        return svc

    def _log(self, kind, at, **payload):
        self.event_log.append({"kind": kind, "at": at, **payload})
