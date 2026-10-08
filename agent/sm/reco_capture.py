"""识别结果事件捕获 —— MAA v5.8.1 的 GetRecognitionDetail 失败绕过。

v5.8.1 的 RecognitionTask::run_impl 识别完成后只调了 set_node_detail +
事件通知，漏了 set_reco_detail——RuntimeCache 的 reco_details_ 缓存永远
为空，导致 MaaTaskerGetRecognitionDetail 对 post_recognition /
MaaContextRunRecognition 的结果**必然**返回失败（线上日志实锤：
"failed to get_reco_result [reco_id=...]"，而同一识别的
TemplateMatcher::analyze 正常产出 score）。新版 MAA 已在 Recognizer.cpp
补上 set_reco_detail，但部署的 Luna v5.8.1 没有。

而 Node.RecognitionNode.Succeeded/Failed 事件**始终**携带 reco_details
（RecoResult: reco_id/name/algorithm/box/detail），所以：
注册 tasker 事件 sink，按 reco_id / 节点名捕获 reco_details，作为
识别详情获取的兜底。官方 API 仍是第一选择（未来版本直接可用）。

两种消费方式：
- take(reco_id)      —— 精确匹配（MCP 直连 tasker 模式，reco_id 已知）
- take_by_name(...)  —— 按节点名 + 序号匹配（agent 模式：run_recognition
  不返回 reco_id，且事件经 IPC 有到达时延，SM 检查串行执行）
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from maa.event_sink import EventSink

#: take() 未找到对应 reco_id 的哨兵（box 本身可能为 None=未命中）
MISS: Any = object()

_MAX_EVENTS = 256


class _Captured:
    __slots__ = ("seq", "reco_id", "name", "box")

    def __init__(self, seq: int, reco_id: int, name: str, box: Optional[Tuple[int, int, int, int]]):
        self.seq = seq
        self.reco_id = reco_id
        self.name = name
        self.box = box


class RecoEventCapture(EventSink):
    """捕获 Node.RecognitionNode.* 事件的 reco_details（线程安全）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: "OrderedDict[int, _Captured]" = OrderedDict()
        self._seq = 0

    # ---------------- EventSink ----------------

    def _on_raw_notification(self, handle: Any, msg: str, details: Dict[str, Any]) -> None:
        if not msg.startswith("Node.RecognitionNode."):
            return
        reco = (details or {}).get("reco_details") or {}
        reco_id = reco.get("reco_id")
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
                self._seq, int(reco_id), str(details.get("name", "")), box,
            )
            while len(self._events) > _MAX_EVENTS:
                self._events.popitem(last=False)

    # ---------------- 消费 ----------------

    def take(self, reco_id: int):
        """按 reco_id 精确取（取出即删）。未找到返回 MISS。"""
        with self._lock:
            target_seq = None
            for seq, ev in self._events.items():
                if ev.reco_id == reco_id:
                    target_seq = seq
                    break
            if target_seq is None:
                return MISS
            box = self._events[target_seq].box
            del self._events[target_seq]
            return box

    def take_by_name(self, name: str, after_seq: int = 0):
        """取 seq>after_seq 的第一条 name 匹配事件（取出即删）。

        返回 (seq, box)；未找到返回 None。
        """
        with self._lock:
            for seq, ev in self._events.items():
                if ev.seq <= after_seq or ev.name != name:
                    continue
                box = ev.box
                del self._events[seq]
                return seq, box
        return None


class EventRecoDetail:
    """事件兜底的识别详情——仅含 check / 锚点动作所需字段（hit/box/reco_id）。"""

    __slots__ = ("reco_id", "box", "hit")

    def __init__(self, reco_id: int, box: Optional[Tuple[int, int, int, int]]):
        self.reco_id = reco_id
        self.box = box
        self.hit = box is not None
