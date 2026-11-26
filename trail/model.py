"""基础值对象、枚举与错误类型。

字段命名沿用合作方资料：窗口 ``windows``、闸机 ``gates``、授权额度
``grant_id``、通行凭证 ``permit``、本地核验编号 ``use_no``、
最后编号 ``last_use_no``、现场报扣 ``reported_consumed``、
临时停开 ``closure`` 等。时间统一为营运日开始后的整数秒，窗口左闭右开。
"""

from dataclasses import dataclass, field
from enum import Enum


class DenyReason(str, Enum):
    """本地核验拒绝原因，供值班机界面与日志直接使用。"""

    UNKNOWN_PERMIT = "unknown_permit"            # 凭证不在本机离线档案
    WRONG_GATE = "wrong_gate"                    # 凭证未授权在本闸口使用
    WINDOW_CLOSED = "window_closed"              # 计入时钟误差后仍不在有效窗口
    REVOKED = "permit_revoked"                   # 服务已吊销/替换该凭证
    GATE_CLOSED = "gate_closed"                  # 本方向已实际停止接受通行权
    CLOCK_OUT_OF_BOUND = "clock_out_of_bound"    # 时钟观测误差超过 error_bound
    OFFLINE_LIMIT = "offline_limit_exceeded"     # 离线时长超过 max_offline
    ALREADY_USED = "already_used"                # 该凭证已放行，重启后重扫
    GRANT_EXHAUSTED = "grant_exhausted"          # 授权额度已在本机用尽
    NOT_OFFLINE_CAPABLE = "not_offline_capable"  # 闸口未获准离线放行


class PermitState(str, Enum):
    """通行凭证在服务侧的生命周期。"""

    ACTIVE = "active"          # 有效，闸口可据此放行
    SUPERSEDED = "superseded"  # 团体改时换入口，旧凭证已被新凭证替换
    RELEASED = "released"      # 已收到该凭证的独立核验记录（已放行事实）
    VOID = "void"              # 确认未使用后作废（如旧闸时钟超界后改时）


class GrantState(str, Enum):
    """授权额度（grant）的状态。"""

    ACTIVE = "active"          # 闸口正常持有
    PARTIAL = "partial"        # 已交还部分额度，仍可能有线下放行
    RECONCILED = "reconciled"  # 最后一批核验记录到齐，连续性核对通过
    REVOKED = "revoked"        # 额度被服务收回（停开/借出划转）


class MoveState(str, Enum):
    """团体改时/换入口申请的状态。"""

    PENDING = "pending"        # 旧时段通行权仍可能在现场使用，只能等待
    CONFIRMED = "confirmed"    # 服务确认换闸，新凭证已签发，旧凭证已替换
    CLOSED_RELEASED = "closed_released"  # 旧凭证确认已在原闸放行，不再另发权利
    CANCELLED = "cancelled"    # 值班员明确取消


class ClosureState(str, Enum):
    """临时停开通知在单个闸口上的三态。

    发布通知、现场已知晓、实际停止接受通行权是三个不同事实，必须分别记录。
    """

    PUBLISHED = "published"    # 服务已发出停开通知
    SEEN = "seen"              # 闸口回报 closure_seen
    CLOSED = "closed"          # 闸口回报 closed，离线权确实停止


@dataclass(frozen=True)
class Window:
    """一个时间窗（通行方向 + 左闭右开秒区间 + 现场许可人数）。"""

    id: str
    direction: str
    start: int
    until: int
    capacity: int

    def contains(self, moment: int) -> bool:
        return self.start <= moment < self.until


@dataclass(frozen=True)
class Gate:
    """闸机的可信参数：时钟绝对误差界与最大离线秒数。"""

    id: str
    error_bound: int
    max_offline: int


@dataclass
class Grant:
    """分配给闸口的一笔授权额度，绑定窗口与通行方向。"""

    grant_id: str
    gate: str
    window: str
    allotment: int                      # 原始分配人数
    count: int                          # 当前实际持有额度（划转/交还后变化）
    state: GrantState = GrantState.ACTIVE
    returned: int = 0                   # 已交还人数
    released: int = 0                   # 独立核验记录确认的放行人数
    reported_consumed: int | None = None
    last_use_no: int | None = None      # 停止使用时回报的最后记录编号
    use_seen: list[int] = field(default_factory=list)  # 已到齐的记录编号
    earmarked: int = 0                  # 经此授权划出借给别闸的额度
    pending_return: int = 0             # 服务已发出交还通知、尚未收回的人数
    unaccounted: int = 0                # 已申报交还但可能已线下放行的敞口
                                        # （不得挪用，等核验记录或 stranded 关闭）


@dataclass
class Permit:
    """一个团体的通行凭证，绑定团体、人数、入口、方向与窗口。"""

    permit: str
    group: str
    gate: str
    grant_id: str
    window: str
    count: int
    state: PermitState = PermitState.ACTIVE
    superseded_by: str | None = None
    released_use_no: int | None = None
    stranded_reason: str | None = None  # 无法安全履约时的说明


class TrailError(Exception):
    """业务错误基类。"""

    status = 400


class NotFound(TrailError):
    status = 404


class Conflict(TrailError):
    """请求与当前额度/凭证状态冲突。"""

    status = 409


class ReconcileError(TrailError):
    """对账发现连续性或账目不一致，运营端须人工介入。"""

    status = 409
