"""状态机核心 —— 通用有限状态机（不依赖 MaaFramework / 游戏具体逻辑）。

设计参照 MaaAssistantArknights 的 MaaCore/BlackFlow 分层思想：
- State      状态：名字 + 检查（如何判定"当前处于该状态"，检查为纯识别）
- Transition 转移：from→to，动作序列 + 强制的后置状态检查 + 失败重试
- Flow       流：某个具体"事由"预设的状态序列（支持条件分支 / 等待 / 重复）
- Runner     执行器：定位当前状态、执行转移、重试、停止

核心约定（来自需求）：
1. 每一次状态转移必须搭配一次状态检查（Transition.post_check 非空，执行后验证到达）；
2. 状态转移不成功时按 Transition.max_retries 重试；
3. 动作集合：点击 / 长按 / 滑动 / 缩放（双指捏合）/ 等待，
   以及"锚点动作"——先识别（模板/OCR）命中框、再点击其中心，替代硬编码坐标。

纯逻辑层：不 import maa，依赖通过 SMContext 协议注入（见 maa_bridge.py），便于单测。
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


# ======================================================================================
# 异常
# ======================================================================================

class SMError(Exception):
    """状态机错误基类。"""


class CheckFailedError(SMError):
    """某次状态检查失败（通常已被转移重试逻辑吸收，最终失败时抛出）。"""


class StateMismatchError(SMError):
    """无法定位当前状态（不在流的状态集中），或处于未预期的状态。"""


class NoTransitionError(SMError):
    """当前状态到目标状态之间没有定义转移。"""


class TransitionFailedError(SMError):
    """状态转移在重试耗尽后仍未通过后置检查。"""


class ActionAnchorMissError(SMError):
    """锚点动作的识别未命中（无法确定点击位置）。"""


class MissingImageError(SMError):
    """状态检查所需的图片资源缺失（占位未补齐）。"""


class FlowAbortedError(SMError):
    """用户停止 / 外部中止。"""


# ======================================================================================
# 状态检查
# ======================================================================================

@dataclass
class CheckSpec:
    """一次状态检查 = 对一个 pipeline 识别节点跑一次识别。

    节点定义在 assets/resource/pipeline/state_machine/*.json（纯识别节点）。
    inverted=True 表示"必须不命中"（如"妖精面板消失 = 战斗结束"）。
    """
    node: str
    inverted: bool = False

    def __repr__(self) -> str:
        return f"{'NOT ' if self.inverted else ''}[{self.node}]"


@dataclass
class CheckResult:
    ok: bool
    spec: CheckSpec
    detail: object = None  # 框架识别详情（RecognitionDetail），可能为 None

    @property
    def hit(self) -> bool:
        """原始命中情况（不含 inverted 语义）。"""
        if self.detail is None:
            return False
        return bool(getattr(self.detail, "hit", False))


# ======================================================================================
# 动作
# ======================================================================================

@dataclass
class ActionSpec:
    """一个原子动作。

    kind: click | long_press | swipe | zoom_in | zoom_out | wait
    - 坐标：x,y（起点/中心）；swipe 用 x2,y2 作终点；duration 为毫秒
    - 锚点：anchor_node 非空时，先对该节点识别，命中框中心 (+dx,+dy) 作为点击/长按位置
    - 可选：if_node 非空时，仅当该识别命中才执行本动作（用于弹窗等可选分支）
    """
    kind: str
    x: int = 0
    y: int = 0
    x2: int = 0
    y2: int = 0
    duration: int = 0
    ms: int = 0
    anchor_node: str = ""
    dx: int = 0
    dy: int = 0
    if_node: str = ""

    def describe(self) -> str:
        parts = [self.kind]
        if self.anchor_node:
            parts.append(f"anchor={self.anchor_node}")
        elif self.kind == "swipe":
            parts.append(f"({self.x},{self.y})->({self.x2},{self.y2}) {self.duration}ms")
        elif self.kind == "wait":
            parts.append(f"{self.ms}ms")
        elif self.kind in ("click", "long_press"):
            extra = f" {self.duration}ms" if self.kind == "long_press" else ""
            parts.append(f"({self.x},{self.y}){extra}")
        elif self.kind in ("zoom_in", "zoom_out"):
            parts.append(f"({self.x},{self.y})")
        if self.if_node:
            parts.append(f"if={self.if_node}")
        return " ".join(parts)


# ======================================================================================
# 状态 与 转移
# ======================================================================================

@dataclass
class State:
    name: str
    checks: List[CheckSpec]
    desc: str = ""
    locate_priority: int = 100  # 越小越先尝试（越"具体"的状态越靠前）

    def __repr__(self) -> str:
        return f"State({self.name}: {self.checks})"


@dataclass
class Transition:
    name: str
    from_state: str
    to_state: str
    actions: List[ActionSpec]
    post_check: List[CheckSpec]  # 必填：转移后的状态检查（需求：每次转移必配检查）
    pre_check: List[CheckSpec] = field(default_factory=list)
    max_retries: int = 3
    retry_wait_ms: int = 800
    post_wait_ms: int = 300  # 动作完成后等待 UI 稳定的时间

    def describe(self) -> str:
        acts = " ; ".join(a.describe() for a in self.actions) or "(无动作)"
        return f"{self.name}: {self.from_state} -> {self.to_state} | {acts} | 检查={self.post_check}"


# ======================================================================================
# 流
# ======================================================================================

class FlowStep:
    """流步骤基类。"""


@dataclass
class GotoState(FlowStep):
    """转移到相邻状态（必须存在 current->target 的转移；已在目标则跳过，幂等）。"""
    state: str


@dataclass
class WaitUntil(FlowStep):
    """等待一组检查全部成立（轮询），典型用途：等待战斗结束。"""
    checks: List[CheckSpec]
    timeout_ms: int
    poll_ms: int = 3000


@dataclass
class Branch(FlowStep):
    """条件分支：when 全部成立执行 then_steps，否则执行 else_steps。"""
    when: List[CheckSpec]
    then_steps: List[FlowStep] = field(default_factory=list)
    else_steps: List[FlowStep] = field(default_factory=list)


@dataclass
class Repeat(FlowStep):
    """重复执行子流。rounds=None 表示无限循环（直到外部停止）。"""
    rounds: Optional[int]
    steps: List[FlowStep]


@dataclass
class PanNormalize(FlowStep):
    """地图平移归一化（"制备状态"）：把可拖动的地图拖回参考平移位，
    让预制 ROI / 固定坐标的检查与动作重新生效。

    背景：战棋地图可被用户或误操作拖动（平移）。平移一旦偏离参考位，
    所有基于固定 ROI 的地图模板（背景/队徽章/港口）与固定坐标点击
    （重置点位/计划炸狗点/港口）全部失效。本步骤在"碰地图"之前执行：
      1. 识别背景地标（模板，全屏搜索）→ 相对参考位的偏移 o；
      2. |o| <= deadzone 即完成（幂等：已在参考位时一次识别即返回）；
      3. 否则从地标中心（必为空地、不碰单位）向 -o 方向慢速拖动，
         实测地图内容位移 ≈ 0.59 × 光标位移，gain=1.0 时每次消除约
         59% 残差，几何收敛（2-5 次进入 deadzone）；
      4. 地标未识别（非地图屏 / 缩放异常 / 被遮挡）→ 告警并跳过
         （best-effort：不抛错，让后续检查/锚点明确失败）。

    与"识别地图、点击适配拖动"（方案 B）的区别：本步骤不改变任何
    预制坐标，而是把地图状态制备回参考位——坐标全部保持有效。
    """
    landmark_node: str
    ref: Tuple[int, int]  # 参考地图中地标的 top-left
    gain: float = 1.0     # 光标位移 = -gain * offset（每轮消除 ~59% 残差）
    deadzone: int = 15    # |offset| 均小于此值视为已在参考位（px）
    max_iter: int = 5
    drag_ms: int = 1600   # 慢速拖动（实测 400ms 不被识别为拖动，1600ms 稳定）
    settle_ms: int = 1200  # 每次拖动后等待画面稳定


@dataclass
class Flow:
    name: str
    entry_state: str  # 流的"就绪状态"：所有步骤执行前应处于该状态
    states: List[State]
    transitions: List[Transition]
    steps: List[FlowStep]

    def __post_init__(self):
        names = [s.name for s in self.states]
        if self.entry_state not in names:
            raise ValueError(f"entry_state {self.entry_state} 不在流的状态集中")
        for t in self.transitions:
            if t.from_state not in names or t.to_state not in names:
                raise ValueError(f"转移 {t.name} 的端点不在流的状态集中")
            if not t.post_check:
                raise ValueError(f"转移 {t.name} 缺少 post_check（需求：每次转移必配状态检查）")


# ======================================================================================
# 执行上下文（由 maa_bridge 实现）
# ======================================================================================

class SMContext(abc.ABC):
    """状态机对运行环境的最小依赖。"""

    @abc.abstractmethod
    def screenshot(self) -> object:
        """截屏，返回框架图像对象（numpy ndarray）。"""

    @abc.abstractmethod
    def check(self, spec: CheckSpec, image: object = None) -> CheckResult:
        """执行一次状态检查。image 为空时内部截图；同一张图可复用于多次检查。"""

    @abc.abstractmethod
    def do_action(self, action: ActionSpec) -> None:
        """执行一个原子动作。失败（锚点未命中等）抛 SMError。"""

    @abc.abstractmethod
    def log(self, level: str, msg: str) -> None:
        """日志。level: DEBUG | INFO | WARNING | ERROR"""

    @abc.abstractmethod
    def save_debug_image(self, tag: str, image: object) -> str:
        """保存调试截图（失败现场），返回路径。"""

    def save_frame(self, tag: str, image: object) -> str:
        """保存"步骤帧"（tag 已含 Runner 的步骤序号），返回路径。

        默认复用 save_debug_image；桥接层可覆盖为独立的帧目录/编号策略。
        用途：把每步识别/点击所用的截图落盘，运行日志中引用其路径。
        """
        try:
            return self.save_debug_image(tag, image) or ""
        except Exception:
            return ""

    @property
    def frame_log(self) -> str:
        """步骤帧日志模式："all"=每次识别都落帧 | "key"=仅关键帧 | "off"=关闭。"""
        return "key"

    @property
    @abc.abstractmethod
    def stop_requested(self) -> bool:
        """外部（用户）是否请求停止。"""


# ======================================================================================
# 执行器
# ======================================================================================

class Runner:
    def __init__(self, flow: Flow, ctx: SMContext):
        self.flow = flow
        self.ctx = ctx
        self._states_by_name = {s.name: s for s in flow.states}
        self._transitions = {(t.from_state, t.to_state): t for t in flow.transitions}
        self.round_finished = 0
        self._seq = 0  # 步骤帧序号（日志与帧文件一一对应）

    # ---------------- 基础 ----------------

    def _abort_if_stopped(self) -> None:
        if self.ctx.stop_requested:
            raise FlowAbortedError("用户请求停止")

    def _log(self, level: str, msg: str) -> None:
        self.ctx.log(level, f"[{self.flow.name}] {msg}")

    def _frame_on(self) -> bool:
        try:
            return self.ctx.frame_log != "off"
        except Exception:
            return True

    def _frame(self, tag: str, image: object) -> str:
        """落盘一张步骤帧（带全局序号），返回路径；off 模式或失败返回空串。"""
        if not self._frame_on():
            return ""
        try:
            self._seq += 1
            return self.ctx.save_frame(f"{self._seq:04d}_{tag}", image) or ""
        except Exception:
            return ""

    @staticmethod
    def _fmt_box(box) -> str:
        if box is None:
            return ""
        try:
            from .boxutil import box_xywh

            x, y, w, h = box_xywh(box)
            return f"box=({x},{y},{w},{h})"
        except Exception:
            return f"box={box}"

    def _check_specs(self, specs: Sequence[CheckSpec], image: object,
                     label: str = "") -> List[CheckResult]:
        """执行一组检查，逐条 DEBUG 日志（节点/命中/框），供日志复盘。"""
        out: List[CheckResult] = []
        for spec in specs:
            r = self.ctx.check(spec, image)
            out.append(r)
            inv = "!" if spec.inverted else ""
            hit = "hit " if r.hit else "miss"
            box_s = self._fmt_box(r.detail.box) if r.hit else ""
            tag = f" {label}" if label else ""
            self._log("DEBUG", f"check {inv}{spec.node} -> {hit} {box_s} ok={r.ok}{tag}".rstrip())
        return out

    @staticmethod
    def _specs_pass(results: Sequence[CheckResult]) -> bool:
        return all(r.ok for r in results)

    # ---------------- 状态定位 ----------------

    def locate_state(self, image: object) -> Optional[str]:
        """在同一张截图上按优先级尝试各状态，返回第一个全部检查通过的状态名。"""
        ordered = sorted(self.flow.states, key=lambda s: (s.locate_priority, s.name))
        for state in ordered:
            results = self._check_specs(state.checks, image)
            if self._specs_pass(results):
                return state.name
        return None

    # ---------------- 转移执行 ----------------

    def run_transition(self, t: Transition) -> None:
        self._log("INFO", f"转移开始: {t.describe()}")
        last_detail = ""
        for attempt in range(1, t.max_retries + 1):
            self._abort_if_stopped()
            image = self.ctx.screenshot()
            pre_frame = self._frame(f"{t.name}_pre", image)
            self._log("INFO", f"转移 {t.name} 第{attempt}/{t.max_retries}次"
                              f"{' pre_frame=' + pre_frame if pre_frame else ''}")

            if t.pre_check and not self._specs_pass(
                    self._check_specs(t.pre_check, image, label=f"pre#{attempt}")):
                last_detail = f"pre_check 未通过: {t.pre_check}"
                self._log("WARNING", f"转移 {t.name} 第{attempt}/{t.max_retries}次: {last_detail} {pre_frame}")
                time.sleep(t.retry_wait_ms / 1000.0)
                continue

            # 幂等守卫：已在目标态就不重执行动作。上一次动作可能已生效、
            # 只是复检识别漏判触发了重试——此时再点一次会把 toggle 型
            # 控件关回去（实测：8-1N 面板打开后二次点击入口会关掉面板）。
            # 判定必须用【目标状态的完整检查集 + 转移 post_check】，
            # 不能用弱 post_check 子集（弱子集会把"弹窗还开着"误判成
            # "已在目标态"，跳过关弹窗动作——集成测试复现过）。
            target_state = self._require_state(t.to_state)
            guard_specs = list(target_state.checks) + list(t.post_check or [])
            if guard_specs and self._specs_pass(
                    self._check_specs(guard_specs, image, label=f"guard#{attempt}")):
                self._log("INFO", f"转移 {t.name}: 动作前已在目标态，跳过动作 {pre_frame}")
                return

            try:
                for action in t.actions:
                    self._abort_if_stopped()
                    self.ctx.do_action(action)
            except SMError as e:
                last_detail = f"动作执行失败: {e}"
                self._log("WARNING", f"转移 {t.name} 第{attempt}/{t.max_retries}次: {last_detail} {pre_frame}")
                time.sleep(t.retry_wait_ms / 1000.0)
                continue

            if t.post_wait_ms > 0:
                time.sleep(t.post_wait_ms / 1000.0)

            # 转移后状态检查（需求：每次转移必配一次状态检查）。
            # 先"延迟复检"（给 UI 动画/识别留时间）再考虑重新执行动作；
            # 若动作实际已生效但本轮复检仍漏判，下一轮循环顶部的幂等
            # 守卫会在重执行动作前兜住（防 toggle 重点击）。
            passed = False
            image = None
            for _ in range(5):
                image = self.ctx.screenshot()
                results = self._check_specs(t.post_check, image, label=f"post#{attempt}")
                if self._specs_pass(results):
                    passed = True
                    break
                self._abort_if_stopped()
                time.sleep(0.4)
            post_frame = self._frame(f"{t.name}_post{attempt}", image) if image is not None else ""
            if passed:
                self._log("INFO", f"转移成功: {t.name} (第{attempt}次) {post_frame}")
                return

            failed = [r for r in results if not r.ok]
            failed_names = [f"{'!' if r.spec.inverted else ''}{r.spec.node}" for r in failed]
            last_detail = f"post_check 未通过: {failed_names}"
            self._log("WARNING", f"转移 {t.name} 第{attempt}/{t.max_retries}次: {last_detail} {post_frame}")
            if attempt < t.max_retries:
                time.sleep(t.retry_wait_ms / 1000.0)

        raise TransitionFailedError(f"转移 {t.name} 重试{t.max_retries}次后仍失败: {last_detail}")

    def goto(self, target: str) -> None:
        """转移到目标状态（幂等）。"""
        self._abort_if_stopped()
        target_state = self._require_state(target)

        image = self.ctx.screenshot()
        if self._specs_pass(self._check_specs(target_state.checks, image, label=f"goto:{target}")):
            self._log("DEBUG", f"已在目标状态 {target}，跳过")
            return

        current = self.locate_state(image)
        if current is None:
            frame = self._frame(f"unknown_state_goto_{target}", image)
            raise StateMismatchError(
                f"无法定位当前状态（goto {target}）。"
                f"流的状态集: {[s.name for s in self.flow.states]} {frame}"
            )
        if current == target:
            self._log("DEBUG", f"定位={target} 但检查未通过（瞬态），按已到达处理")
            return

        t = self._transitions.get((current, target))
        if t is None:
            frame = self._frame(f"no_transition_{current}_to_{target}", image)
            available = sorted(f"{a}->{b}" for (a, b) in self._transitions)
            raise NoTransitionError(
                f"没有 {current} -> {target} 的转移。已定义: {available} {frame}"
            )
        self._log("INFO", f"goto {target}: 定位={current}")
        self.run_transition(t)

    def wait_until(self, step: WaitUntil) -> None:
        deadline = time.monotonic() + step.timeout_ms / 1000.0
        poll = step.poll_ms / 1000.0
        checks_label = " ".join(f"{'!' if c.inverted else ''}{c.node}" for c in step.checks)
        first_frame = ""
        n_polls = 0
        while True:
            self._abort_if_stopped()
            image = self.ctx.screenshot()
            n_polls += 1
            if not first_frame:
                first_frame = self._frame(f"wait_{checks_label}_p1", image)
            if self._specs_pass(self._check_specs(step.checks, image, label=f"wait#{n_polls}")):
                frame = self._frame(f"wait_{checks_label}_done_p{n_polls}", image)
                self._log("INFO", f"等待完成: [{checks_label}] 轮询{n_polls}次 {frame}")
                return
            if time.monotonic() >= deadline:
                frame = self._frame(f"wait_{checks_label}_timeout_p{n_polls}", image)
                raise CheckFailedError(
                    f"等待超时({step.timeout_ms}ms): [{checks_label}] 轮询{n_polls}次 {frame}")
            self._log("DEBUG", f"等待中: [{checks_label}] 第{n_polls}轮 每{int(poll*1000)}ms {first_frame}")
            time.sleep(poll)

    def run_pan_normalize(self, step: PanNormalize) -> None:
        """执行地图平移归一化（见 PanNormalize 文档）。"""
        from .boxutil import box_xywh

        spec = CheckSpec(step.landmark_node)
        refx, refy = step.ref
        for it in range(1, step.max_iter + 1):
            self._abort_if_stopped()
            image = self.ctx.screenshot()
            frame = self._frame(f"pan_norm_p{it}", image)
            r = self.ctx.check(spec, image)
            if r.detail is None or not r.hit or r.detail.box is None:
                self._log("WARNING",
                          f"平移归一化: 地标 {step.landmark_node} 未识别"
                          f"（非地图屏/缩放异常/被遮挡），跳过"
                          + (f" {frame}" if frame else ""))
                return
            x, y, w, h = box_xywh(r.detail.box)
            ox, oy = x - refx, y - refy
            if abs(ox) <= step.deadzone and abs(oy) <= step.deadzone:
                self._log("INFO",
                          f"平移归一化: 已在参考位 (offset=({ox},{oy}))"
                          + (f" {frame}" if frame else ""))
                return
            sx, sy = x + w // 2, y + h // 2
            ex = sx - step.gain * ox
            ey = sy - step.gain * oy
            self._log("INFO",
                      f"平移归一化 #{it}: offset=({ox},{oy}) "
                      f"拖动 ({sx},{sy})->({ex:.0f},{ey:.0f}) {step.drag_ms}ms"
                      + (f" {frame}" if frame else ""))
            self.ctx.do_action(ActionSpec(kind="swipe", x=sx, y=sy,
                                          x2=int(ex), y2=int(ey),
                                          duration=step.drag_ms))
            time.sleep(step.settle_ms / 1000.0)
        self._log("WARNING",
                  f"平移归一化: {step.max_iter} 次迭代后未完全收敛"
                  f"（继续执行，由后续状态检查把关）")

    # ---------------- 流执行 ----------------

    def run(self) -> None:
        self._log("INFO", f"流启动: {self.flow.name} (入口状态={self.flow.entry_state})")
        try:
            self._run_steps(self.flow.steps)
        except FlowAbortedError:
            self._log("WARNING", "流被用户停止")
            raise
        self._log("INFO", f"流完成: {self.flow.name} (完成轮次={self.round_finished})")

    def _run_steps(self, steps: List[FlowStep]) -> None:
        for step in steps:
            self._abort_if_stopped()
            if isinstance(step, GotoState):
                self.goto(step.state)
            elif isinstance(step, WaitUntil):
                self.wait_until(step)
            elif isinstance(step, Branch):
                image = self.ctx.screenshot()
                frame = self._frame(f"branch_{'_'.join('!' + c.node if c.inverted else c.node for c in step.when)}", image)
                when_s = " ".join(f"{'!' if c.inverted else ''}{c.node}" for c in step.when)
                if self._specs_pass(self._check_specs(step.when, image, label="branch")):
                    self._log("INFO", f"分支命中 [{when_s}] -> then {frame}")
                    self._run_steps(step.then_steps)
                else:
                    self._log("INFO", f"分支未命中 [{when_s}] -> else {frame}")
                    self._run_steps(step.else_steps)
            elif isinstance(step, Repeat):
                i = 0
                while step.rounds is None or i < step.rounds:
                    self._abort_if_stopped()
                    i += 1
                    self._log("INFO", f"重复第 {i} 轮 (共 {step.rounds if step.rounds else '无限'})")
                    self._run_steps(step.steps)
                    self.round_finished = max(self.round_finished, i)
            elif isinstance(step, PanNormalize):
                self.run_pan_normalize(step)
            else:
                raise SMError(f"未知流步骤类型: {type(step)}")

    def _require_state(self, name: str) -> State:
        state = self._states_by_name.get(name)
        if state is None:
            raise SMError(f"流中不存在状态 {name}")
        return state
