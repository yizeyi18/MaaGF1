"""MaaBridge —— 把 MaaFramework Python 绑定适配为状态机的 SMContext。

- 状态检查：context.run_recognition(node, image)（节点定义在 pipeline/state_machine/*.json）
- 动作：controller 的 post_click / post_swipe / touch 事件（长按、双指捏合缩放）
- 锚点动作：先识别后点击命中框中心（模板/OCR 通用），替代硬编码坐标
- 停止：外部传入 threading.Event（由 main.py 的 tasker 事件监听置位）
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Optional

from maa.context import Context

from .core import (
    ActionSpec,
    ActionAnchorMissError,
    CheckResult,
    CheckSpec,
    MissingImageError,
    SMContext,
    SMError,
)
from .missing import describe_missing, is_missing

try:  # 复用 agent 现有日志（agent/action/log.py），导入失败时退回 print
    from action.log import MaaLog_Debug, MaaLog_Info, MaaLog_Info as MaaLog_Warn, MaaLog_Info as MaaLog_Err
    _LOG = True
except Exception:  # pragma: no cover
    _LOG = False


def _log(level: str, msg: str) -> None:
    if _LOG:
        if level == "DEBUG":
            MaaLog_Debug(msg)
        elif level == "ERROR":
            MaaLog_Err(msg)
        else:
            MaaLog_Info(msg)
    else:  # pragma: no cover
        print(f"[{level}] {msg}")


# ---------------- 识别事件捕获（v5.8.1 GetRecognitionDetail 失败兜底） ----------------

_AGENT_CAPTURE: Optional[object] = None
_AGENT_CAPTURE_LOCK = threading.Lock()
_AGENT_LAST_SEQ = 0  # 已消费到的事件序号（SM 检查串行 → 全局单调安全）


def _agent_capture() -> Optional[object]:
    """agent 模式的事件捕获：全局注册一次，sink 事件由 AgentServer
    转发自主进程（GUI 进程）的 tasker。"""
    global _AGENT_CAPTURE
    if _AGENT_CAPTURE is None:
        with _AGENT_CAPTURE_LOCK:
            if _AGENT_CAPTURE is None:
                try:
                    from maa.agent.agent_server import AgentServer

                    from .reco_capture import RecoEventCapture

                    cap = RecoEventCapture()
                    AgentServer.add_tasker_sink(cap)  # _sink_holder 持引用防 GC
                    _AGENT_CAPTURE = cap
                except Exception:
                    _log("WARNING", "识别事件捕获注册失败（识别详情将不可用）")
    return _AGENT_CAPTURE


class MaaBridge(SMContext):
    def __init__(self, context: Context, stop_event: threading.Event,
                 debug_dir: str = "debug_sm", stop_file: str = ""):
        self._ctx = context
        self._ctrl = context.tasker.controller
        self._stop = stop_event
        self._debug_dir = debug_dir
        self._stop_file = stop_file
        self._missing_warned: set = set()

    def _warn_missing_once(self, node: str) -> None:
        if node not in self._missing_warned:
            self._missing_warned.add(node)
            self.log("WARNING", "缺失图片占位（按未命中处理）:\n" + describe_missing(node))

    # ---------------- SMContext ----------------

    @property
    def stop_requested(self) -> bool:
        if self._stop.is_set():
            return True
        # 停止文件：测试期兜底手段 —— 在 agent 工作目录创建该文件即可请求停止
        return bool(self._stop_file) and os.path.exists(self._stop_file)

    def log(self, level: str, msg: str) -> None:
        _log(level, msg)

    def screenshot(self) -> "object":
        try:
            job = self._ctrl.post_screencap()
            job.wait()
            img = job.get()  # BGR(A) ndarray
            import numpy as np

            img = np.asarray(img)
            if img.ndim == 3 and img.shape[2] == 4:
                # Win32 PrintWindow 捕获是 CV_8UC4(BGRA)，而 Python 绑定
                # ImageBuffer.set 硬编码 CV_8UC3——4 通道直接传给
                # run_recognition 会误读行数据。统一转 3 通道 BGR。
                img = np.ascontiguousarray(img[:, :, :3])
            return img
        except Exception as e:
            raise SMError(f"截图失败（框架/连接异常）: {e}") from e

    def check(self, spec: CheckSpec, image: Optional["object"] = None) -> CheckResult:
        # 缺失图片：按"未命中"处理并告警（不抛异常）。
        # 状态定位(locate)会对所有状态跑检查，若抛异常整个流会崩；
        # 而 v1 的 8-1N 流不依赖缺失检查，占位状态(main)自然永不匹配。
        # 注意 ok=spec.inverted（未命中：普通检查不过，inverted 检查过）——
        # 此前误写 ok=(not inverted)，把"没识别"当"命中"，任何屏都会
        # 误报第一个全普通检查的状态。
        if is_missing(spec.node):
            self._warn_missing_once(spec.node)
            return CheckResult(ok=spec.inverted, spec=spec, detail=None)
        if image is None:
            image = self.screenshot()
        detail = self._recognize(spec.node, image)
        if detail is None:
            # 识别失败/无详情 → 按"未命中"处理（与 MCP bridge 同语义）
            return CheckResult(ok=spec.inverted, spec=spec, detail=None)
        hit = bool(detail.hit)
        return CheckResult(ok=(not hit) if spec.inverted else hit, spec=spec, detail=detail)

    def _detail_from_event(self, node: str):
        """兜底：从 Node.RecognitionNode 事件取识别结果（见 sm/reco_capture）。

        agent 模式的 run_recognition 不返回 reco_id，且事件经 IPC 转发
        有到达时延；SM 检查串行执行 → 按节点名 + 事件序号匹配。
        """
        global _AGENT_LAST_SEQ
        from .reco_capture import EventRecoDetail

        cap = _agent_capture()
        if cap is None:
            return None
        deadline = time.time() + 2.0
        while True:
            got = cap.take_by_name(node, after_seq=_AGENT_LAST_SEQ)
            if got is not None:
                seq, box = got
                _AGENT_LAST_SEQ = seq
                return EventRecoDetail(seq, box)  # agent 模式无 reco_id，用 seq 标识
            if time.time() >= deadline:
                return None
            time.sleep(0.02)

    def do_action(self, action: ActionSpec) -> None:
        try:
            self._do_action(action)
        except SMError:
            raise
        except Exception as e:
            raise SMError(f"动作 {action.describe()} 执行失败: {e}\n{traceback.format_exc()}") from e

    def save_debug_image(self, tag: str, image: "object") -> str:
        os.makedirs(self._debug_dir, exist_ok=True)
        path = os.path.join(self._debug_dir, f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}.png")
        try:
            import numpy as np
            from utils.png import encode_png
            # 框架截图为 BGR(A)；encode_png 自动处理通道翻转与 alpha 丢弃
            with open(path, "wb") as f:
                f.write(encode_png(np.asarray(image), bgr=True))
        except Exception as e:
            _log("WARNING", f"调试截图保存失败 {path}: {e}")
            return ""
        return path

    # ---------------- 内部实现 ----------------

    def _do_action(self, a: ActionSpec) -> None:
        if a.kind == "wait":
            time.sleep(max(a.ms, 0) / 1000.0)
            return

        x, y = a.x, a.y
        image = None

        if a.if_node:
            image = self.screenshot()
            detail = self._recognize(a.if_node, image)
            if detail is None or not detail.hit:
                self.log("DEBUG", f"可选动作跳过（{a.if_node} 未命中）: {a.describe()}")
                return
            if a.anchor_node and a.anchor_node == a.if_node:
                x, y = self._center(detail, a)

        if a.anchor_node:
            if image is None:
                image = self.screenshot()
            detail = self._recognize(a.anchor_node, image)
            if detail is None or not detail.hit or detail.box is None:
                self.save_debug_image(f"anchor_miss_{a.anchor_node}", image)
                raise ActionAnchorMissError(
                    f"锚点识别未命中: {a.anchor_node}（动作 {a.describe()}）"
                )
            x, y = self._center(detail, a)

        if a.kind == "click":
            self._ctrl.post_click(x, y).wait()
        elif a.kind == "long_press":
            self._ctrl.post_touch_down(x, y, 0, 1).wait()
            time.sleep(max(a.duration, 10) / 1000.0)
            self._ctrl.post_touch_up(0).wait()
        elif a.kind == "swipe":
            self._ctrl.post_swipe(x, y, a.x2, a.y2, max(a.duration, 100)).wait()
        elif a.kind == "zoom_in":
            self._pinch(x, y, dist=abs(a.duration) or 60, inward=False)
        elif a.kind == "zoom_out":
            self._pinch(x, y, dist=abs(a.duration) or 60, inward=True)
        else:
            raise SMError(f"未知动作类型: {a.kind}")
        self.log("DEBUG", f"动作完成: {a.describe()}")

    @staticmethod
    def _center(detail: "object", a: ActionSpec) -> tuple[int, int]:
        from .boxutil import box_xywh

        x, y, w, h = box_xywh(detail.box)
        return x + w // 2 + a.dx, y + h // 2 + a.dy

    def _recognize(self, node: str, image: "object"):
        if is_missing(node):
            raise MissingImageError(describe_missing(node))
        try:
            detail = self._ctx.run_recognition(node, image)
        except Exception as e:
            raise SMError(f"识别 {node} 失败: {e}") from e
        if detail is None:
            # MAA v5.8.1：run_recognition 的详情获取
            # （GetRecognitionDetail）对 Context 识别必然失败 → 事件兜底
            detail = self._detail_from_event(node)
        return detail

    def _pinch(self, cx: int, cy: int, dist: int, inward: bool) -> None:
        """双指水平捏合缩放。inward=False 手指张开=放大，True 手指收拢=缩小。

        注意：仅 ADB（安卓模拟器）控制器支持双指捏合（contact=手指号）；
        Win32 控制器 contact=鼠标按键、且框架无滚轮事件，缩放暂不支持
        （v1 的 8-1N 流不使用缩放动作，不影响）。
        """
        d0, d1 = (30 + dist, 30) if inward else (30, 30 + dist)
        steps = 4
        self._ctrl.post_touch_down(cx - d0, cy, 0, 1).wait()
        self._ctrl.post_touch_down(cx + d0, cy, 1, 1).wait()
        for i in range(1, steps + 1):
            d = d0 + (d1 - d0) * i // steps
            self._ctrl.post_touch_move(cx - d, cy, 0, 1).wait()
            self._ctrl.post_touch_move(cx + d, cy, 1, 1).wait()
            time.sleep(0.03)
        self._ctrl.post_touch_up(0).wait()
        self._ctrl.post_touch_up(1).wait()
