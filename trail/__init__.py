"""石阶古道分时通行：额度服务与闸口本地核验。

模块划分：

* :mod:`trail.model` —— 窗口、闸机、授权额度等基础值对象与错误类型
* :mod:`trail.service` —— 通行额度服务（分配、借出、凭证、改时、对账、运营查询）
* :mod:`trail.verifier` —— 闸口值班机的本地核验程序（断网可用、重启不重复放行）
* :mod:`trail.httpapi` —— 额度服务的 HTTP 接口
* :mod:`trail.client` —— 值班机侧的额度服务 HTTP 客户端
* :mod:`trail.driver` —— 读取合作方资料并回放事件的联调驱动
"""

from .model import (
    ClosureState,
    DenyReason,
    GrantState,
    MoveState,
    PermitState,
)
from .service import QuotaService
from .verifier import GateVerifier

__all__ = [
    "QuotaService",
    "GateVerifier",
    "DenyReason",
    "PermitState",
    "GrantState",
    "MoveState",
    "ClosureState",
]
