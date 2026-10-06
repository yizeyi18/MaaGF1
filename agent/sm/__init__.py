"""sm —— 状态机包（MaaGF1 agent）。

- core:        通用状态机核心（状态/转移/流/执行器）
- maa_bridge:  MaaFramework Python 绑定适配层
- missing:     缺失图片资源登记（占位机制）
- states_*:    状态定义（公共层 + 8-1N）
- flows_81n:   8-1N 循环流（v1 唯一落地任务）
"""
from .core import (
    ActionAnchorMissError,
    ActionSpec,
    Branch,
    CheckFailedError,
    CheckResult,
    CheckSpec,
    Flow,
    FlowAbortedError,
    FlowStep,
    GotoState,
    MissingImageError,
    NoTransitionError,
    Repeat,
    Runner,
    SMContext,
    SMError,
    State,
    StateMismatchError,
    Transition,
    TransitionFailedError,
    WaitUntil,
)
from .flows_81n import build_flow_81n

__all__ = [
    "ActionAnchorMissError", "ActionSpec", "Branch", "CheckFailedError",
    "CheckResult", "CheckSpec", "Flow", "FlowAbortedError", "FlowStep",
    "GotoState", "MissingImageError", "NoTransitionError", "Repeat",
    "Runner", "SMContext", "SMError", "State", "StateMismatchError",
    "Transition", "TransitionFailedError", "WaitUntil",
    "build_flow_81n",
]
