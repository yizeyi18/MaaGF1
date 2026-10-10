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
from typing import Callable, Dict, List, Optional, Sequence, Tuple


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

    kind: click | long_press | swipe | zoom_in | zoom_out | wait | pan_norm
    - 坐标：x,y（起点/中心）；swipe 用 x2,y2 作终点；duration 为毫秒
    - 锚点：anchor_node 非空时，先对该节点识别，命中框中心 (+dx,+dy) 作为点击/长按位置
    - 可选：if_node 非空时，仅当该识别命中才执行本动作（用于弹窗等可选分支）
    - 可选：unless_node 非空时，仅当该识别未命中才执行本动作（if_node 的反向，
      用于"弹层已关则先重开"这类恢复路径；与 if_node 同用以后者优先）
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
    unless_node: str = ""
    # kind="pan_norm" 专用：pan_node=地标节点，(x,y)=参考平移位 top-left。
    # 桥接层执行与 PanNormalize 相同的全屏地标搜索+拖动归一化。
    # 2026-10-10 实机：部署/长按会把地图相机平移走，转移内自带再归一化，
    # 否则后续固定 ROI 检查与锚点全部失效。
    pan_node: str = ""

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
        if self.unless_node:
            parts.append(f"unless={self.unless_node}")
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
    family: Optional[str] = None  # 状态家族（两阶段定位：家族判别链先筛后验，见 locate_state）

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
    # 连通导航代价（"基于连通步数的状态转移"）：
    # None = 动作边，不得用于自动连通导航（开战斗/补给/撤离/换人/筛选等有
    #       真实后果的转移，只能由流显式执行）；
    # int  = 可导航边（取消/返回/关框/部署/入场等安全或可恢复的转移），
    #       值越小越优先（BFS 步数导航在同层内按代价排序）。
    # 直接边 goto 不受此限制：(current->target) 有边就直接走。
    nav_cost: Optional[int] = None

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
    """条件分支：when 全部成立执行 then_steps，否则执行 else_steps。

    python_cond：可选的运行时条件（Runner -> bool）。识别检查之外补充
    "流内记忆"类条件，典型用途：入口编队与轮内稳定态编队互斥
    （一个战斗周期内至多轮换一次打手——run#8 双轮换故障修复，
    2026-10-10：入口轮换后 2队徽章即满弹，第 1 轮稳定态误判
    "上轮补给过"再次轮换，两次轮换之间没有战斗）。
    """
    when: List[CheckSpec]
    then_steps: List[FlowStep] = field(default_factory=list)
    else_steps: List[FlowStep] = field(default_factory=list)
    python_cond: Optional[Callable[["Runner"], bool]] = None


@dataclass
class SetFlag(FlowStep):
    """设置运行时标志（Runner.flags），供 Branch.python_cond 读取。"""
    name: str
    value: bool = True


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
    # 两阶段定位的家族签名链（有序，早停）：(家族名, 签名检查列表)。
    # 签名 = 该家族屏幕的"指纹"（1-2 个检查，命中才算进入该家族候选）。
    # 定位时依次跑签名（全过即停），只对命中家族内的状态做完整检查；
    # 全部未命中 / 家族内无状态通过 → 回退全扫描（locate_state_full 等价，
    # 正确性兜底：签名是启发式提速，不改变最终定位结果的正确性边界——
    # 只要真状态所在家族的签名在其屏幕上命中，两阶段与全扫描结果一致，
    # 由 FakeGame 等价性测试逐屏验证）。
    family_discriminators: List[Tuple[str, List[CheckSpec]]] = field(default_factory=list)

    def __post_init__(self):
        names = [s.name for s in self.states]
        if self.entry_state not in names:
            raise ValueError(f"entry_state {self.entry_state} 不在流的状态集中")
        for t in self.transitions:
            if t.from_state not in names or t.to_state not in names:
                raise ValueError(f"转移 {t.name} 的端点不在流的状态集中")
            if not t.post_check:
                raise ValueError(f"转移 {t.name} 缺少 post_check（需求：每次转移必配状态检查）")
        for fam, _sig in self.family_discriminators:
            if not [s for s in self.states if s.family == fam]:
                raise ValueError(f"家族 {fam} 的签名已配置但没有成员状态")

    # ---------------- 转移图查询（路径规划接口） ----------------

    def transitions_from(self, state: str) -> List[Transition]:
        """从状态 state 出发的全部状态转移（可用转移）。"""
        return [t for t in self.transitions if t.from_state == state]

    def reachable(self, state: str) -> List[str]:
        """从 state 一步可达的状态名列表（去重、保序）。"""
        seen, out = set(), []
        for t in self.transitions_from(state):
            if t.to_state not in seen:
                seen.add(t.to_state)
                out.append(t.to_state)
        return out

    def shortest_path(self, a: str, b: str) -> Optional[List[str]]:
        """BFS 最短路径（状态名序列，含端点）；不可达返回 None。"""
        if a == b:
            return [a]
        from collections import deque

        adj: Dict[str, List[str]] = {}
        for t in self.transitions:
            adj.setdefault(t.from_state, []).append(t.to_state)
        prev: Dict[str, Optional[str]] = {a: None}
        q = deque([a])
        while q:
            cur = q.popleft()
            for nxt in adj.get(cur, ()):
                if nxt not in prev:
                    prev[nxt] = cur
                    if nxt == b:
                        path = [b]
                        while prev[path[-1]] is not None:
                            path.append(prev[path[-1]])
                        return path[::-1]
                    q.append(nxt)
        return None

    def nav_path(self, a: str, b: str) -> Optional[List[str]]:
        """连通导航路径：只走 nav_cost 非空的"安全边"（取消/返回/关框/
        部署/入场等），按步数 BFS、同层按代价小者优先。
        动作边（开战斗/补给/撤离/换人/筛选…）永不参与——它们有真实
        后果，只能由流显式执行。"""
        if a == b:
            return [a]
        from collections import deque

        adj: Dict[str, List[Tuple[int, str]]] = {}
        for t in self.transitions:
            if t.nav_cost is not None:
                adj.setdefault(t.from_state, []).append((t.nav_cost, t.to_state))
        for k in adj:
            adj[k].sort()
        prev: Dict[str, Optional[str]] = {a: None}
        q = deque([a])
        while q:
            cur = q.popleft()
            for _, nxt in adj.get(cur, ()):
                if nxt not in prev:
                    prev[nxt] = cur
                    if nxt == b:
                        path = [b]
                        while prev[path[-1]] is not None:
                            path.append(prev[path[-1]])
                        return path[::-1]
                    q.append(nxt)
        return None


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
        self.flags: Dict[str, bool] = {}  # 运行时标志（SetFlag / Branch.python_cond）

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

    def locate_state(self, image: object, skip: Optional[str] = None) -> Optional[str]:
        """两阶段定位（提速核心：2026-10-10 用户要求"快一点"）。

        阶段 1：家族判别链（有序、早停）——每家族 1 次识别（判别节点），
        命中即锁定候选家族；阶段 2：只对候选家族内的状态做完整检查
        （家族内按 locate_priority）。全部判别未命中 / 家族内无状态通过
        → 回退全扫描（与旧版逐状态扫描等价，正确性兜底）。

        旧版（v1 全扫描）对照：`locate_state_full`（保留供等价性测试）。
        skip：跳过该状态（其检查已知不通过，避免重复识别，见 goto 快路径）。
        """
        ordered = sorted(self.flow.states, key=lambda s: (s.locate_priority, s.name))
        tried: set = set()

        def scan(states) -> Optional[str]:
            for state in states:
                if skip is not None and state.name == skip:
                    continue
                if state.name in tried:
                    continue
                tried.add(state.name)
                if self._specs_pass(self._check_specs(state.checks, image)):
                    return state.name
            return None

        for fam, sig in self.flow.family_discriminators:
            if not self._specs_pass(self._check_specs(sig, image, label=f"family:{fam}")):
                continue
            members = [s for s in ordered if s.family == fam]
            self._log("DEBUG",
                      f"locate: 家族命中 {fam} "
                      f"({' '.join('!' + c.node if c.inverted else c.node for c in sig)})"
                      f" -> {len(members)} 候选")
            hit = scan(members)
            if hit:
                return hit
            self._log("WARNING",
                      f"locate: 家族 {fam} 命中但家族内无状态全过 → 全扫描回退")
            return scan(ordered)
        return scan(ordered)

    def locate_state_full(self, image: object, skip: Optional[str] = None) -> Optional[str]:
        """v1 全扫描定位（按优先级逐状态全检查）。保留供两阶段等价性测试。"""
        ordered = sorted(self.flow.states, key=lambda s: (s.locate_priority, s.name))
        for state in ordered:
            if skip is not None and state.name == skip:
                continue
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

    def goto(self, target: str, _reroute: int = 3) -> None:
        """转移到目标状态（幂等；基于连通步数的导航 + 异常恢复）。

        1) 快路径：目标检查通过 → 已到达（跳过）；
        2) 直连边：current -> target 有边 → 原样执行（流语义不变）；
        3) 连通导航：无直连边时，沿"安全边"（Transition.nav_cost 非空：
           取消/返回/关框/部署/入场等）BFS 最短路径逐跳执行；
           动作边（开战斗/补给/撤离/换人/筛选，有真实后果）永不参与；
        4) 异常恢复：某跳失败（重试耗尽/无法定位）→ 重新定位当前位置
           并重路由（最多 _reroute 次），而不是整个流直接中止。
        """
        self._abort_if_stopped()
        target_state = self._require_state(target)

        image = self.ctx.screenshot()
        if self._specs_pass(self._check_specs(target_state.checks, image, label=f"goto:{target}")):
            self._log("DEBUG", f"已在目标状态 {target}，跳过")
            return

        # 目标状态检查刚失败过：全扫描时跳过，避免重复识别同一状态
        current = self.locate_state(image, skip=target)
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
        if t is not None:
            self._log("INFO", f"goto {target}: 定位={current} | 直连边")
            try:
                self.run_transition(t)
                return
            except (TransitionFailedError, NoTransitionError,
                    StateMismatchError, ActionAnchorMissError) as e:
                # 直连边失败 ≠ 流终止（2026-10-10 run#9 教训）：锚点未命中
                # 常是"点击已生效但屏幕在过渡/识别竞态"——重新定位 + 走
                # 连通路径重路由，而不是把异常抛给流层。
                new_path = self._reroute_or_raise(e, target, _reroute)
                if new_path:
                    self._nav_walk(new_path, target, _reroute - 1)
                return

        # ---------- 连通导航（无直连边） ----------
        path = self.flow.nav_path(current, target)
        if path is None or len(path) < 2:
            frame = self._frame(f"no_route_{current}_to_{target}", image)
            direct = sorted(f"{a}->{b}" for (a, b) in self._transitions)
            nav = sorted(f"{t2.from_state}->{t2.to_state}"
                         for t2 in self.flow.transitions if t2.nav_cost is not None)
            raise NoTransitionError(
                f"没有 {current} -> {target} 的直连转移或安全连通路径。"
                f"全部直连边: {direct} | 可导航边: {nav} {frame}"
            )
        self._log("INFO",
                  f"goto {target}: 定位={current} | 连通导航 {' -> '.join(path)}")
        self._nav_walk(path, target, _reroute)

    def _reroute_or_raise(self, e: Exception, target: str,
                          reroute_left: int) -> List[str]:
        """转移失败后的统一恢复（异常恢复核心）：重新定位当前位置，
        重算安全连通路径。

        返回: 新路径（[0]=当前态）；[] = 已在目标态（按到达处理）。
        重路由预算耗尽 / 无安全路径 → 抛出原异常（NoTransitionError 包装）。
        """
        if reroute_left <= 0:
            self._log("ERROR", f"连通导航放弃: {e}")
            raise e
        image = self.ctx.screenshot()
        now = self.locate_state(image)
        if now == target:
            self._log("INFO",
                      f"异常恢复: 重定位后已在目标态 {target}，按到达处理")
            return []
        new_path = self.flow.nav_path(now, target) if now else None
        if not new_path or len(new_path) < 2:
            frame = self._frame(f"reroute_dead_{now}_to_{target}", image)
            raise NoTransitionError(
                f"重路由失败: 当前 {now} 无安全路径到 {target} {frame}") from e
        self._log("WARNING",
                  f"连通导航重路由（{e.__class__.__name__}）: "
                  f"当前={now} -> {' -> '.join(new_path)}")
        return new_path

    def _nav_walk(self, path: List[str], target: str, reroute_left: int) -> None:
        """逐跳执行连通路径；跳失败时重新定位 + 重路由（异常恢复）。"""
        i = 1
        while i < len(path):
            nxt = path[i]
            try:
                self.goto(nxt)  # 每跳重新定位（自纠正）；跳内仍可再导航
                i += 1
            except (TransitionFailedError, NoTransitionError,
                    StateMismatchError, ActionAnchorMissError) as e:
                new_path = self._reroute_or_raise(e, target, reroute_left)
                if not new_path:
                    return  # 已在目标态
                path, i = new_path, 1
                reroute_left -= 1

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
                hit = self._specs_pass(self._check_specs(step.when, image, label="branch"))
                if hit and step.python_cond is not None:
                    cond = step.python_cond(self)
                    if not cond:
                        self._log("INFO", f"分支识别命中但运行时条件不满足 -> else {frame}")
                        hit = False
                if hit:
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
            elif isinstance(step, SetFlag):
                self.flags[step.name] = step.value
                self._log("DEBUG", f"标志 {step.name} = {step.value}")
            else:
                raise SMError(f"未知流步骤类型: {type(step)}")

    def _require_state(self, name: str) -> State:
        state = self._states_by_name.get(name)
        if state is None:
            raise SMError(f"流中不存在状态 {name}")
        return state
