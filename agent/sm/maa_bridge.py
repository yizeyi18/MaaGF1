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
                    # Node.* 识别事件走 context 通知器——必须用
                    # add_context_sink（add_tasker_sink 只收 Tasker.Task.*）
                    AgentServer.add_context_sink(cap)  # _sink_holder 持引用防 GC
                    _AGENT_CAPTURE = cap
                except Exception:
                    _log("WARNING", "识别事件捕获注册失败（识别详情将不可用）")
    return _AGENT_CAPTURE


class MaaBridge(SMContext):
    def __init__(self, context: Context, stop_event: threading.Event,
                 debug_dir: str = "debug_sm", stop_file: str = "",
                 frame_log: str = "key"):
        self._ctx = context
        self._ctrl = context.tasker.controller
        self._stop = stop_event
        self._debug_dir = debug_dir
        self._stop_file = stop_file
        self._missing_warned: set = set()
        self._frame_log = frame_log  # all=每次识别落帧 | key=仅未命中落帧 | off
        self._reco_n = 0

    @property
    def frame_log(self) -> str:
        return self._frame_log

    def _reco_frame(self, node: str, image: "object", hit: bool) -> str:
        """按 frame_log 模式落识别帧（all=总是，key=仅未命中），返回路径。"""
        if self._frame_log == "off" or (self._frame_log == "key" and hit):
            return ""
        self._reco_n += 1
        tag = f"reco_{self._reco_n:05d}_{node}_{'hit' if hit else 'miss'}"
        try:
            return self.save_debug_image(tag, image) or ""
        except Exception:
            return ""

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
        hit = bool(detail.hit) if detail is not None else False
        frame = self._reco_frame(spec.node, image, hit)
        if frame:
            self.log("DEBUG", f"reco {spec.node} -> {'hit' if hit else 'miss'} frame={frame}")
        if detail is None:
            # 识别失败/无详情 → 按"未命中"处理（与 MCP bridge 同语义）
            return CheckResult(ok=spec.inverted, spec=spec, detail=None)
        return CheckResult(ok=(not hit) if spec.inverted else hit, spec=spec, detail=detail)

    def _detail_from_event(self, reco_id: int):
        """兜底：从 Node.Recognition.* 事件取识别结果（见 sm/reco_capture）。

        按事件顶层 reco_id 精确关联（== MaaContextRunRecognition 返回
        值）；事件经 IPC 转发有到达时延 → 短轮询。
        """
        from .reco_capture import EventRecoDetail, MISS

        cap = _agent_capture()
        if cap is None:
            return None
        # 5s：识别事件经 IPC 转发，guard 连发多次识别后突发延迟可能
        # 超过 2s（2026-10-09 实机：unless 条件识别事件迟于 2s 窗口 →
        # 返回 None → 误判"未命中"）。正常 hit/miss 事件 <2s 到，
        # 只有真延迟的事件需要这 3s 余量。
        deadline = time.time() + 5.0
        while True:
            box = cap.take_by_reco_id(reco_id)
            if box is not MISS:
                return EventRecoDetail(reco_id, box)
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
            if detail is None:
                # fail-closed：条件识别失败不能猜"未命中"（可能误执行动作）
                frame = self.save_debug_image(f"if_fail_{a.if_node}", image)
                raise SMError(f"if 条件识别失败: {a.if_node}（动作 {a.describe()}）"
                              + (f" frame={frame}" if frame else ""))
            if not detail.hit:
                frame = self._reco_frame(f"if_{a.if_node}", image, False)
                self.log("DEBUG", f"可选动作跳过（{a.if_node} 未命中）: {a.describe()}"
                         + (f" frame={frame}" if frame else ""))
                return
            if a.anchor_node and a.anchor_node == a.if_node:
                x, y = self._center(detail, a)
        elif a.unless_node:
            # 反向条件：unless_node 命中则跳过（用于"弹层已关则先重开"恢复）
            image = self.screenshot()
            detail = self._recognize(a.unless_node, image)
            if detail is None:
                # fail-closed：条件识别失败不能猜"未命中"。
                # 2026-10-09 实机教训：识别瞬时失败返回 None → 误执行
                # "重开弹层"点击 → 把已开弹层点关 → 后续锚点全灭。
                # 抛错让本次尝试失败重试（新截图重新判定）。
                frame = self.save_debug_image(f"unless_fail_{a.unless_node}", image)
                raise SMError(f"unless 条件识别失败: {a.unless_node}（动作 {a.describe()}）"
                              + (f" frame={frame}" if frame else ""))
            if detail.hit:
                frame = self._reco_frame(f"unless_{a.unless_node}", image, False)
                self.log("DEBUG", f"条件动作跳过（{a.unless_node} 命中）: {a.describe()}"
                          + (f" frame={frame}" if frame else ""))
                return
            frame = self._reco_frame(f"unless_{a.unless_node}", image, False)
            self.log("DEBUG", f"条件动作执行（{a.unless_node} 未命中）: {a.describe()}"
                      + (f" frame={frame}" if frame else ""))
            if a.anchor_node and a.anchor_node == a.unless_node:
                x, y = self._center(detail, a)

        if a.anchor_node:
            if image is None:
                image = self.screenshot()
            detail = self._recognize(a.anchor_node, image)
            if detail is None or not detail.hit or detail.box is None:
                frame = self.save_debug_image(f"anchor_miss_{a.anchor_node}", image)
                self.log("WARNING", f"锚点 {a.anchor_node} -> miss（动作 {a.describe()}）"
                         + (f" frame={frame}" if frame else ""))
                raise ActionAnchorMissError(
                    f"锚点识别未命中: {a.anchor_node}（动作 {a.describe()}）"
                )
            x, y = self._center(detail, a)
            from .boxutil import box_xywh

            bx, by, bw, bh = box_xywh(detail.box)
            self.log("INFO", f"锚点 {a.anchor_node} -> hit box=({bx},{by},{bw},{bh})"
                         f" -> {a.kind} ({x},{y})")

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
        if not a.anchor_node:
            extra = f" -> ({a.x2},{a.y2})" if a.kind == "swipe" else ""
            self.log("INFO", f"动作 {a.kind} ({x},{y}){extra}")
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
            # 直调 C API 拿 reco_id——绑定版 run_recognition 在
            # GetRecognitionDetail 失败（v5.8.1 必然）后吞掉 reco_id
            # 返回 None，事件兜底就失去关联键
            reco_id = self._post_reco(node, image)
        except Exception as e:
            raise SMError(f"识别 {node} 失败: {e}") from e
        if not reco_id:
            return None
        try:
            detail = self._ctx.tasker.get_recognition_detail(reco_id)
        except Exception:
            detail = None
        if detail is not None:
            return detail
        # MAA v5.8.1：Context 识别的 GetRecognitionDetail 必然失败 → 事件兜底
        return self._detail_from_event(reco_id)

    def _post_reco(self, node: str, image: "object") -> int:
        """MaaContextRunRecognition 直调，返回 reco_id（失败返回 0）。"""
        from maa.buffer import ImageBuffer
        from maa.library import Library

        buf = ImageBuffer()
        buf.set(image)
        return int(Library.framework().MaaContextRunRecognition(
            self._ctx._handle,
            *Context._gen_post_param(node, {}),
            buf._handle,
        ))

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
