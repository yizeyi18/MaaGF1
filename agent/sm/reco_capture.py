"""识别结果事件捕获 —— MAA v5.8.1 的 GetRecognitionDetail 失败绕过。

v5.8.1 的 RecognitionTask::run_impl 识别完成后只调了 set_node_detail +
事件通知，漏了 set_reco_detail——RuntimeCache 的 reco_details_ 缓存永远
为空，导致 MaaTaskerGetRecognitionDetail 对 post_recognition /
MaaContextRunRecognition 的结果**必然**返回失败（线上日志实锤：
"failed to get_reco_result"，而同一识别的 TemplateMatcher::analyze
正常产出 score）。新版 MAA 已在 Recognizer.cpp 补上 set_reco_detail，
但部署的 Luna v5.8.1 没有。

识别结果事件（线上日志实锤的字段形状）：
    msg = Node.Recognition.Succeeded / Node.Recognition.Failed
    details = {
        "focus": ..., "name": "recognition/{type}/{uuid}",  # 伪 entry
        "reco_details": {"reco_id", "name", "algorithm",
                         "box": {"x","y","width","height"}|null, "detail"},
        "reco_id": 400000041,   # 顶层，全局计数器 ++s_global_reco_id
        "task_id": 200000038,   # 顶层，Task 构造时生成的任务号
    }
注意三件事：
- 结果事件的 msg 前缀是 "Node.Recognition."（另有
  Node.RecognitionNode.Starting 节点级事件，无 reco_details）；
- name 是伪 entry 而非节点名，不能按节点名关联；
- task_id 与 reco_id 是**两个不同计数器**：MCP 直连模式 C API
  返回 task_id（== job.job_id），agent 模式 MaaContextRunRecognition
  返回 reco_id。

两种消费方式：
- take_by_taskid(task_id)  —— MCP 模式：task_id == job.job_id
- take_by_reco_id(reco_id) —— agent 模式：reco_id == run_recognition
  的 C API 返回值（绑定版 run_recognition 拿到详情失败后吞掉了
  reco_id 返回 None，故 maa_bridge 直接调 C API 取 reco_id）

官方 API 仍是第一选择（未来 MAA 版本直接可用），事件仅兜底。
"""
from __future__ import annotations

import threading
from collections import OrderedDict, deque
from typing import Any, Dict, Optional, Tuple

from maa.event_sink import EventSink

#: take_by_* 未找到对应事件的哨兵（box 本身可能为 None=未命中）
MISS: Any = object()

_MAX_EVENTS = 256


class _Captured:
    __slots__ = ("seq", "task_id", "reco_id", "box")

    def __init__(self, seq: int, task_id: int, reco_id: int,
                 box: Optional[Tuple[int, int, int, int]]):
        self.seq = seq
        self.task_id = task_id
        self.reco_id = reco_id
        self.box = box


class RecoEventCapture(EventSink):
    """捕获 Node.Recognition.* 结果事件的 reco_details（线程安全）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: "OrderedDict[int, _Captured]" = OrderedDict()
        self._seq = 0
        self._recent_msgs: "deque[str]" = deque(maxlen=32)  # 诊断用

    # ---------------- EventSink ----------------

    def _on_raw_notification(self, handle: Any, msg: str, details: Dict[str, Any]) -> None:
        with self._lock:
            self._recent_msgs.append(msg)
        # 覆盖 Node.Recognition.*（v5.8.1 结果事件）与
        # Node.RecognitionNode.*（新版消息族）；Starting 无 reco_details，
        # 会被下面的守卫跳过。
        if not msg.startswith("Node.Recognition"):
            return
        details = details or {}
        # 只有结果事件（Succeeded/Failed）带 reco_details；
        # Starting 事件顶层虽有 reco_id 但无 reco_details，必须排除
        reco = details.get("reco_details")
        if not reco:
            return
        reco_id = reco.get("reco_id") or details.get("reco_id")
        if not reco_id:
            return
        box = reco.get("box")
        if isinstance(box, dict):
            box = (
                int(box.get("x", 0)), int(box.get("y", 0)),
                int(box.get("width", 0)), int(box.get("height", 0)),
            )
        elif box is not None:
            try:
                box = tuple(int(v) for v in box)
            except (TypeError, ValueError):
                box = None
        with self._lock:
            self._seq += 1
            self._events[self._seq] = _Captured(
                self._seq, int(details.get("task_id") or 0), int(reco_id), box,
            )
            while len(self._events) > _MAX_EVENTS:
                self._events.popitem(last=False)

    # ---------------- 消费 ----------------

    def take_by_taskid(self, task_id: int):
        """按事件 task_id 精确取（MCP 模式：task_id == job.job_id）。

        返回 box（可能为 None=未命中）；未找到返回 MISS。
        """
        return self._take(lambda ev: ev.task_id == task_id)

    def take_by_reco_id(self, reco_id: int):
        """按事件 reco_id 精确取（agent 模式：reco_id == C API 返回值）。

        返回 box（可能为 None=未命中）；未找到返回 MISS。
        """
        return self._take(lambda ev: ev.reco_id == reco_id)

    def _take(self, match) -> Any:
        with self._lock:
            target_seq = None
            for seq, ev in self._events.items():
                if match(ev):
                    target_seq = seq
                    break
            if target_seq is None:
                return MISS
            box = self._events[target_seq].box
            del self._events[target_seq]
            return box

    def snapshot(self) -> Dict[str, Any]:
        """诊断快照：当前留存事件 + 最近收到的原始消息。"""
        with self._lock:
            return {
                "stored": [
                    {"seq": ev.seq, "task_id": ev.task_id, "reco_id": ev.reco_id, "box": ev.box}
                    for ev in self._events.values()
                ],
                "recent_msgs": list(self._recent_msgs),
            }


class EventRecoDetail:
    """事件兜底的识别详情——仅含 check / 锚点动作所需字段（hit/box/reco_id）。"""

    __slots__ = ("reco_id", "box", "hit")

    def __init__(self, reco_id: int, box: Optional[Tuple[int, int, int, int]]):
        self.reco_id = reco_id
        self.box = box
        self.hit = box is not None
