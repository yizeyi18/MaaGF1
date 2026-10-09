"""TaskerBridge —— 把 (Resource, Tasker, Controller) 适配为状态机的 SMContext。

与 sm/maa_bridge.py（AgentServer Context 版）的区别：
- 识别走 tasker.post_recognition + resource.get_node_data（standalone，无需 GUI 连接）
- 动作走共享 controller

识别节点参数转换：get_node_data 返回 {"recognition": {"type","param"}, ...}，
param 与 maa.pipeline 中的参数 dataclass 字段一一对应。
"""
from __future__ import annotations

import dataclasses
import os
import sys
import threading
import time
import traceback
from typing import Dict, Optional, Tuple

from sm.core import (
    ActionAnchorMissError,
    ActionSpec,
    CheckResult,
    CheckSpec,
    MissingImageError,
    SMContext,
    SMError,
)
from sm.missing import describe_missing, is_missing


def _install_stop_probe(ev: "threading.Event", name: str) -> None:
    """给停止事件包一层探针：谁调了 set() 就记谁（带调用栈）。

    诊断用（2026-10-09 23:08：流在超时前被'用户请求停止'中止，
    用户确认未手动停）。threading.Event 是纯 Python 类，实例属性
    可遮蔽类方法；包一次后标记，避免重入。
    """
    if getattr(ev, "_stop_probed", False):
        return
    try:
        ev._stop_probed = True
    except Exception:
        return
    orig = ev.set

    def _set(*args, **kwargs):
        import traceback

        stack = "".join(traceback.format_stack()[:-1])
        _sm_log("WARNING",
                f"[stop-probe] {name}.set() 被调用！调用栈：\n{stack}")
        return orig(*args, **kwargs)

    ev.set = _set


def _reco_param_classes() -> Dict[str, type]:
    """识别类型 → 参数 dataclass。动态探测：不同 MaaFramework 版本
    （如 5.2.6 无 JAnd/JOr，新版有）暴露的 dataclass 集合不同，
    只注册当前版本真实存在的类。"""
    from maa import pipeline as _p

    names = (
        "DirectHit", "TemplateMatch", "FeatureMatch", "ColorMatch", "OCR",
        "NeuralNetworkClassify", "NeuralNetworkDetect", "CustomRecognition",
        "And", "Or",
    )
    out: Dict[str, type] = {}
    for n in names:
        cls = getattr(_p, f"J{n}", None)
        if cls is not None:
            out[n] = cls
    return out


def node_to_reco(node_data: Dict) -> Tuple[str, "object"]:
    """pipeline 节点 dict → (recognition_type, 参数 dataclass 实例)。"""
    reco = (node_data or {}).get("recognition") or {}
    rtype = reco.get("type", "")
    param = reco.get("param") or {}
    cls = _reco_param_classes().get(rtype)
    if cls is None:
        raise SMError(f"不支持的识别类型: {rtype or '(空)'}")
    fields = {f.name for f in dataclasses.fields(cls)}
    kwargs = {k: v for k, v in param.items() if k in fields}
    return rtype, cls(**kwargs)


# ---------------- 状态机轨迹日志 ----------------
#
# 红线：MCP 伴生进程里【绝不 import action 包】（含 action.log）！
# action/log.py 模块级 `from maa.agent.agent_server import AgentServer` ——
# import maa.agent 会把框架切进 AgentServer 模式（MaaTaskerCreate /
# MaaWin32ControllerCreate / MaaResourceCreate 全部变 NotImpl 桩）。
# 伴生进程是完整框架模式，import 之后共享 controller 直接被破坏：
# 实测 run_sm_8_1n 首条 log 触发 action import（03:19:18 注册
# matlab/parametric 自定义动作），紧接着的截图即抛
# OverflowError，同路径截图在 import 前一直正常。
# 故此处用独立文件日志（read_logs source=maa_mcp.log(exe) 可读）。

_sm_log_lock = threading.Lock()


def _sm_log_path() -> str:
    # 伴生进程 CWD 固定为 exe 目录（PyInstaller onedir，sys.executable
    # 也位于该目录）；与 read_logs 的 maa_mcp.log(exe) 源对齐
    try:
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                            "maa_mcp.log")
    except Exception:
        return os.path.join(os.getcwd(), "maa_mcp.log")


def _sm_log(level: str, msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}][{level}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with _sm_log_lock:
            with open(_sm_log_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


class TaskerBridge(SMContext):
    def __init__(self, hub: "object", session: "object", stop_event: threading.Event,
                 debug_dir: str = "debug_sm", frame_log: str = "key"):
        self._hub = hub
        self._session = session
        self._ctrl = hub.controller
        self._resource = hub.resource
        self._tasker = session.tasker
        self._stop = stop_event
        self._debug_dir = debug_dir
        self._missing_warned: set = set()
        self._frame_log = frame_log  # all=每次识别落帧 | key=仅未命中落帧 | off
        self._reco_n = 0
        # 停止探针（2026-10-09 23:08 实机：run#6 在 30min 超时前被
        # "用户请求停止"中止，用户确认未手动停 → 记录 set() 调用栈定位）
        _install_stop_probe(hub.stop_sm, "hub.stop_sm")
        _install_stop_probe(stop_event, "local stop_event")

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

    # ---------------- SMContext ----------------

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set() or self._hub.stop_sm.is_set()

    def log(self, level: str, msg: str) -> None:
        _sm_log(level, msg)

    def screenshot(self) -> "object":
        return self._hub.screencap()

    def check(self, spec: CheckSpec, image: "object" = None) -> CheckResult:
        if is_missing(spec.node):
            self._warn_missing_once(spec.node)
            return CheckResult(ok=spec.inverted, spec=spec, detail=None)
        if image is None:
            image = self.screenshot()
        detail = self._recognize(spec.node, image)
        hit = bool(detail.hit) if detail is not None else False
        frame = self._reco_frame(spec.node, image, hit)
        if frame:
            _sm_log("DEBUG", f"reco {spec.node} -> {'hit' if hit else 'miss'} frame={frame}")
        if detail is None:
            # 识别失败/无详情 → 按"未命中"处理（与 CheckResult.hit 一致）：
            # 普通检查（须命中）不通过，inverted 检查（须不命中）通过。
            # 此前误写 ok=(not inverted)——把识别失败当命中，基地屏上
            # 所有普通检查全"通过"，locate 必误报优先级最高的状态。
            return CheckResult(ok=spec.inverted, spec=spec, detail=None)
        return CheckResult(ok=(not hit) if spec.inverted else hit, spec=spec, detail=detail)

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

            arr = np.asarray(image)
            with open(path, "wb") as f:
                f.write(encode_png(arr, bgr=True))
        except Exception as e:
            self.log("WARNING", f"调试截图保存失败 {path}: {e}")
            return ""
        return path

    # ---------------- 内部实现 ----------------

    def _warn_missing_once(self, node: str) -> None:
        if node not in self._missing_warned:
            self._missing_warned.add(node)
            self.log("WARNING", "缺失图片占位（按未命中处理）:\n" + describe_missing(node))

    def _recognize(self, node: str, image: "object"):
        if is_missing(node):
            raise MissingImageError(describe_missing(node))
        node_data = self._resource.get_node_data(node)
        if node_data is None:
            raise SMError(f"识别节点不存在: {node}（资源未加载或节点名错误）")
        rtype, param = node_to_reco(node_data)
        # post_recognition 的图像入口只接受 3 通道（上游绑定的
        # ImageBuffer.set 硬编码 CV_8UC3）；控制器原始截图是 4 通道 BGRA。
        job = self._tasker.post_recognition(rtype, param, self._hub.to_bgr(image))
        job.wait()
        try:
            # 识别任务的 taskid 即 reco_id。官方 API（新版 MAA 正常；
            # v5.8.1 的 post_recognition 结果不入 runtime cache，此调用
            # 必然失败 → 落到事件兜底）
            detail = self._tasker.get_recognition_detail(job.job_id)
            if detail is not None:
                return detail
        except Exception:
            pass
        return self._detail_from_event(job.job_id)

    def _detail_from_event(self, task_id: int):
        """兜底：从 Node.RecognitionNode 事件取识别结果（sm/reco_capture）。

        按事件 task_id（== job.job_id）精确关联——不能用 reco_id：
        v5.8.1 里 C API 返回的是 tasker 任务号，而 RecoResult.reco_id
        是全局计数器（++s_global_reco_id），两者不同值。
        """
        from sm.reco_capture import EventRecoDetail, MISS

        cap = self._session.reco_capture
        if cap is None:
            return None
        # 事件回调理论上在 wait() 返回前已执行（notify 先于任务完成同步
        # 发出）；线程时序留 5s 轮询兜底（2026-10-09：guard 连发多次
        # 识别后事件转发突发延迟，2s 窗口偶发收不到 → 返回 None →
        # 条件动作误判；正常事件 <2s 到，余量只覆盖真延迟）。
        deadline = time.time() + 5.0
        while True:
            box = cap.take_by_taskid(task_id)
            if box is not MISS:
                self._session.reco_last_lookup = {
                    "task_id": task_id, "found": True,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                return EventRecoDetail(task_id, box)
            if time.time() >= deadline:
                # 未命中：留存诊断快照（reco_debug 工具可查）
                self._session.reco_last_lookup = {
                    "task_id": task_id, "found": False,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "capture": cap.snapshot(),
                }
                return None
            time.sleep(0.05)

    @staticmethod
    def _center(detail: "object", a: ActionSpec) -> Tuple[int, int]:
        from sm.boxutil import box_xywh

        x, y, w, h = box_xywh(detail.box)
        return x + w // 2 + a.dx, y + h // 2 + a.dy

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
            # 反向条件：unless_node 命中则跳过（用于"弹层已关则先重开"恢复）。
            # 2026-10-09 实机教训（run#3/#4/#5）：本分支缺失时 unless 被
            # 静默忽略 → "重开弹层"点击无条件执行 → 把已开弹层点关 →
            # 后续锚点全灭。必须与 sm/maa_bridge.py 语义一致。
            image = self.screenshot()
            detail = self._recognize(a.unless_node, image)
            if detail is None:
                # fail-closed：识别失败抛错重试，落帧留证
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
            from sm.boxutil import box_xywh

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
        elif a.kind in ("zoom_in", "zoom_out"):
            # Win32 控制器无捏合/滚轮事件（contact=鼠标按键）
            raise SMError("zoom 动作仅 ADB 控制器支持（当前为 Win32）")
        else:
            raise SMError(f"未知动作类型: {a.kind}")
        if not a.anchor_node:
            extra = f" -> ({a.x2},{a.y2})" if a.kind == "swipe" else ""
            self.log("INFO", f"动作 {a.kind} ({x},{y}){extra}")
        self.log("DEBUG", f"动作完成: {a.describe()}")
