"""8-1N 循环的状态与转移定义（v2，按实机 walkthrough 测量重设计）。

设计原则（TODO + 两轮完整 walkthrough 实测）：
1. 每个转移 = 动作序列 + 必填 post_check 状态检查；重试 + 幂等守卫对抗
   点击被吞（战斗屏/选人屏/过场屏均实测有吞点击现象）。
2. 战斗锚点用 bar_end（结束回合按钮，战斗屏恒在），不用 妖精 面板
   （仅单位被选中时出现、且 OCR 不稳定）；妖精 只用于"战后 1队自动
   选中"（postbattle）与"战后未选中"（battle_no2）的区分。
3. 机场框族：战前框（squad1_box/squad2_box）与战后 1队框（t1_wd_popup）
   是同一种全屏"选择梯队"面板，识别特征完全一致（撤离+取消；开始作战
   /结束回合 按钮文字被框按钮盖住、OCR 恒 miss——2026-10-09 实机复现，
   框屏检查禁用 start）→ 三者不可区分，靠流程上下文 + 宽容转移；
   战中 2队框（t2_box）独有 补给 按钮 → 用 !supply 把框族与它分开。
4. 计划 AP 实测 7 → 1（录入 2 个计划点后），旧 3/2/1 假设作废：
   plan 族只区分"AP=1 已达成"（plan_done），中间值不检查。
5. 入口弹药检查：直接开 1队框读"弹药 X/Y"行（OCR \\b0/），不用
   2队代理推断（代理仅稳定态有效：上轮补给过 2队 ⇒ 本轮 2队满）；
   稳定态检查 = 2队地图标记弹药徽章（Gnewfullammo，先查"有"，
   Gnewnoammo 在地图左侧有已知误报）。
6. 选人屏（charselect）服务器通信重、点击最易被吞：每步
   前置状态检查 + post_check + 重试；收藏筛选 toggle 幂等
   （仅"显示全部"=筛选关闭时点"收藏"，"选择中"=已开启则跳过）。
7. 过场屏（loading/结算）点击完全无效：执行计划后 WaitUntil(妖精)
   等战斗结束（loading→结算→战后，最多 10 分钟），不在过场中点击。

状态检查节点见 assets/resource/pipeline/state_machine/8-1n_st.json。
坐标映射自 assets/resource/pipeline/tasks/Farm/Experience/8-1n/
（进入/部署/编队/战斗/重置/地图初始化.json）+ 实测修正。
"""
from __future__ import annotations

from .core import (
    ActionSpec,
    CheckSpec,
    State,
    Transition,
)

C = CheckSpec  # 简写


# ======================================================================================
# 状态
# ======================================================================================

# ---- 地图（部署）状态族：精确区分"部署了哪几队"，保证幂等与可定位 ----
# 加 !bar_end 守卫：战斗屏同样命中 map/init.png 模板（实测），必须排除。
S_MAP = State(
    name="map",
    checks=[C("SM81N_map"), C("SM81N_map_t1", inverted=True),
            C("SM81N_map_t2", inverted=True), C("SM81N_map_t3", inverted=True),
            C("SM81N_bar_end", inverted=True)],
    desc="8-1N 地图（未部署）",
    locate_priority=30,
)
S_MAP_T1 = State(
    name="map_t1",
    checks=[C("SM81N_map"), C("SM81N_map_t1"),
            C("SM81N_map_t2", inverted=True), C("SM81N_map_t3", inverted=True),
            C("SM81N_bar_end", inverted=True)],
    desc="8-1N 地图（1队已部署）",
    locate_priority=29,
)
S_MAP_T12 = State(
    name="map_t12",
    checks=[C("SM81N_map"), C("SM81N_map_t1"), C("SM81N_map_t2"),
            C("SM81N_map_t3", inverted=True),
            C("SM81N_bar_end", inverted=True)],
    desc="8-1N 地图（1/2队已部署）",
    locate_priority=28,
)
S_MAP_FULL = State(
    name="map_full",
    checks=[C("SM81N_map"), C("SM81N_map_t1"), C("SM81N_map_t2"),
            C("SM81N_map_t3"), C("SM81N_start"),
            C("SM81N_bar_end", inverted=True)],
    desc="8-1N 地图（三队齐备、可见开始作战）",
    locate_priority=27,
)

# ---- 机场框（入口弹药检查 / 编队入口 / 战后撤 1队）----
# 框屏特征 = 撤离+取消 按钮（实机 OCR 验证命中）。注意：框屏上"开始作战"
# 按钮虽露出黄色底但文字被框按钮盖住，OCR 恒 miss（2026-10-09 实机复现）
# → 框屏检查一律不用 start；start 只在地图屏（按钮完整可见）使用。
# 战前框（1队/2队）与战后 1队框是同一种全屏"选择梯队"面板，四个 OCR
# 特征（撤离/取消/开始作战/结束回合）在三种框屏上表现完全一致 →
# 识别上不可区分，靠【流程上下文】区分（goto 目标检查短路 + 宽容转移）；
# locate 恒返回 squad1_box（优先级 26、名称序在前），所有框屏出边转移
# 都挂在 squad1_box 上（幂等守卫按目标状态检查，歧义无害）。
# !supply 排除战中 2队框（补给按钮是其独有特征 → 落到 t2_box）。
S_SQUAD1_BOX = State(
    name="squad1_box",
    checks=[C("SM81N_t1wd"), C("SM81N_box_cancel"),
            C("SM81N_supply", inverted=True)],
    desc="战前机场框（1队打开，撤离+取消可见）",
    locate_priority=26,
)
S_SQUAD2_BOX = State(
    name="squad2_box",
    checks=[C("SM81N_t1wd"), C("SM81N_box_cancel"),
            C("SM81N_supply", inverted=True)],
    desc="战前机场框（2队打开，撤离+取消可见）",
    locate_priority=26,
)

# ---- 战斗相关状态 ----
S_BATTLE = State(
    name="battle",
    # 战斗中、2队仍在场上（补给撤离前）。不含 妖精 要求：
    # 点过 2队 标记后单位处于选中态（妖精面板可能出现），不能做必要条件。
    checks=[C("SM81N_bar_end"), C("SM81N_supply", inverted=True),
            C("SM81N_map_t2")],
    desc="战斗中（2队在场，结束回合可见）",
    locate_priority=40,
)
S_BATTLE_NO2 = State(
    name="battle_no2",
    # 2队已撤离（标记消失）。!妖精 区分战后（妖精=1队被选中）
    checks=[C("SM81N_bar_end"), C("SM81N_supply", inverted=True),
            C("SM81N_map_t2", inverted=True),
            C("SM81N_battle", inverted=True)],
    desc="战斗中（2队已撤离）",
    locate_priority=41,
)
S_T2_BOX = State(
    name="t2_box",
    # 战斗中 2队机场框（补给+撤离同屏）。战前框有 开始作战 → 排除。
    checks=[C("SM81N_supply"), C("SM81N_start", inverted=True)],
    desc="战斗中 2队机场框（补给可见）",
    locate_priority=39,
)
S_EQUIP_OVERFLOW = State(
    name="equip_overflow",
    checks=[C("SM81N_equip_overflow")],
    desc="装备溢出处理弹窗（装备强化选项）",
    locate_priority=37,
)

# ---- 计划模式状态族 ----
# 可观测事实 = AP 计数器：实测 7（未录入）→ 1（录入炸狗点1+2）。
# 中间 AP 值依赖点数消耗规则，不作状态依据；只门控"AP=1"。
S_PLAN_DONE = State(
    name="plan_done",
    checks=[C("SM81N_plan"), C("SM81N_ap1")],
    desc="计划模式（炸狗点已录入，行动点数=1）",
    locate_priority=35,
)
S_PLAN = State(
    name="plan",
    checks=[C("SM81N_plan"), C("SM81N_ap1", inverted=True)],
    desc="计划模式（未录入/未达成 AP=1）",
    locate_priority=36,
)

# ---- 战后/重置状态族 ----
S_POSTBATTLE = State(
    name="postbattle",
    # 战斗结束：1队自动选中 → 妖精面板出现。2队已撤离（!t2 由 battle_no2
    # 语义覆盖，这里不重复要求）。!supply 排除框屏。
    checks=[C("SM81N_bar_end"), C("SM81N_battle"),
            C("SM81N_supply", inverted=True)],
    desc="战后（1队已选中，妖精面板可见）",
    locate_priority=45,
)
S_T1_WD_POPUP = State(
    name="t1_wd_popup",
    # 战后 1队框 = 与战前框同布局的"选择梯队"面板（撤离+取消可见、
    # 无补给）。识别上与 squad1_box 不可区分（见上）——此状态仅作为
    # 转移目标名（goto 目标检查短路 + T_*__t1_wd_popup 的动作来源），
    # locate 实际恒返回 squad1_box，出边走 T_squad1_box__withdraw_ok。
    checks=[C("SM81N_t1wd"), C("SM81N_box_cancel"),
            C("SM81N_supply", inverted=True)],
    desc="战后 1队框（撤离可见）",
    locate_priority=44,
)
S_WITHDRAW_OK = State(
    name="withdraw_ok",
    # 撤离确认对话框（战中 2队 / 战后 1队 同布局，同节点）
    checks=[C("SM81N_withdraw_confirm")],
    desc="撤离确认对话框",
    locate_priority=43,
)
S_ENDMENU = State(
    name="endmenu",
    checks=[C("SM81N_redeploy")],
    desc="左上角菜单（重新作战可见）",
    locate_priority=42,
)
S_SETTLEMENT = State(
    name="settlement",
    # 过场屏：执行计划 → 结算 的过渡（约数秒），点击无效。
    # 仅作 locate/日志用（WaitUntil 轮询会自然穿过），流不直接跳转它。
    checks=[C("SM81N_settlement")],
    desc="战斗结算过场屏",
    locate_priority=48,
)


def states_81n() -> list[State]:
    return [
        S_SQUAD1_BOX, S_SQUAD2_BOX,
        S_MAP, S_MAP_T1, S_MAP_T12, S_MAP_FULL,
        S_T2_BOX, S_BATTLE, S_BATTLE_NO2, S_EQUIP_OVERFLOW,
        S_PLAN_DONE, S_PLAN,
        S_POSTBATTLE, S_T1_WD_POPUP, S_WITHDRAW_OK, S_ENDMENU, S_SETTLEMENT,
    ]


# ======================================================================================
# 动作简写
# ======================================================================================

def _click(x: int, y: int, **kw) -> ActionSpec:
    return ActionSpec(kind="click", x=x, y=y, **kw)


def _anchor(node: str, kind: str = "click", **kw) -> ActionSpec:
    """锚点动作：先识别 node，再对命中框中心执行 kind。"""
    return ActionSpec(kind=kind, anchor_node=node, **kw)


def _swap_actions() -> list[ActionSpec]:
    """swap 动作链：编辑栏卡 → "编队中1"徽章卡（全图模板）→ 确定。"""
    return [
        _click(89, 656),  # 队伍中的打手 编辑栏卡（原 target [64,636,50,41] 中心）
        ActionSpec(kind="wait", ms=300),
        _anchor("SM81N_team_char", kind="long_press", duration=20),
        ActionSpec(kind="wait", ms=500),
        _anchor("SM81N_formation_confirm"),
        ActionSpec(kind="wait", ms=400),
    ]


# ======================================================================================
# 转移
# ======================================================================================
def transitions_81n() -> list[Transition]:
    return [
        # ---------- 进入地图 ----------
        Transition(
            name="T_stage_list__stage_detail",
            from_state="stage_list", to_state="stage_detail",
            actions=[_anchor("SM81N_stage_list")],
            post_check=[C("SM81N_stage_detail")],
            max_retries=3, post_wait_ms=1000,
        ),
        Transition(
            name="T_stage_detail__map",
            from_state="stage_detail", to_state="map",
            actions=[_anchor("SM81N_stage_detail"), ActionSpec(kind="wait", ms=3000)],
            post_check=[C("SM81N_map"), C("SM81N_bar_end", inverted=True)],
            max_retries=3,
        ),
        Transition(
            name="T_stage_detail__map_full",
            # 装备溢出恢复用：重进 8-1N 后地图保持已部署
            from_state="stage_detail", to_state="map_full",
            actions=[_anchor("SM81N_stage_detail"), ActionSpec(kind="wait", ms=3000)],
            post_check=[C("SM81N_map"), C("SM81N_map_t1"), C("SM81N_map_t2"),
                        C("SM81N_map_t3"), C("SM81N_start")],
            max_retries=3,
        ),

        # ---------- 部署（1队/2队：港口模板锚点 → 确定OCR锚点；3队：重装机场流程） ----------
        Transition(
            name="T_map__map_t1",
            from_state="map", to_state="map_t1",
            actions=[
                _anchor("SM81N_port1", kind="long_press", duration=20),
                ActionSpec(kind="wait", ms=500),
                _anchor("SM81N_deploy_confirm"),
            ],
            post_check=[C("SM81N_map_t1")],
            max_retries=3,
        ),
        Transition(
            name="T_map_t1__map_t12",
            from_state="map_t1", to_state="map_t12",
            actions=[
                _anchor("SM81N_port2", kind="long_press", duration=50),
                ActionSpec(kind="wait", ms=500),
                _anchor("SM81N_deploy_confirm"),
            ],
            post_check=[C("SM81N_map_t2")],
            max_retries=3,
        ),
        Transition(
            name="T_map_t12__map_full",
            # 3队=重装机场：porth → "选择重装部队"tab → 第一个单位 → 部署
            # （映射 部署.json 81N部署_部署3队/部署重装/部署重装选择/部署重装确定）
            from_state="map_t12", to_state="map_full",
            actions=[
                _anchor("SM81N_porth", kind="long_press", duration=20),
                ActionSpec(kind="wait", ms=500),
                _click(589, 90),  # "选择重装部队"tab（原 target [560,74,59,31] 中心）
                ActionSpec(kind="wait", ms=500),
                _anchor("SM81N_deploy_list_first", kind="long_press", duration=20),
                ActionSpec(kind="wait", ms=300),
                _anchor("SM81N_deploy_confirm"),
                ActionSpec(kind="wait", ms=1000),
            ],
            post_check=[C("SM81N_map_t3"), C("SM81N_start")],
            max_retries=3,
        ),

        # ---------- 入口弹药检查（直接查 1队） ----------
        Transition(
            name="T_map_full__squad1_box",
            from_state="map_full", to_state="squad1_box",
            actions=[
                _anchor("SM81N_map_t1"),  # 点击地图 1队 标记 → 机场框
                ActionSpec(kind="wait", ms=500),
            ],
            post_check=[C("SM81N_t1wd"), C("SM81N_box_cancel")],
            max_retries=5, retry_wait_ms=1000,
        ),
        Transition(
            name="T_squad1_box__map_full",
            from_state="squad1_box", to_state="map_full",
            actions=[
                _anchor("SM81N_box_cancel"),  # 取消 → 关框
                ActionSpec(kind="wait", ms=800),
            ],
            post_check=[C("SM81N_map"), C("SM81N_map_t1"), C("SM81N_map_t2"),
                        C("SM81N_map_t3"), C("SM81N_start")],
            max_retries=3,
        ),
        Transition(
            name="T_squad1_box__withdraw_ok",
            # 宽容转移：战后 1队框与战前框同布局，locate 恒返回 squad1_box
            # （见状态注释）——战后重置的"点撤离"从 squad1_box 出边执行。
            from_state="squad1_box", to_state="withdraw_ok",
            actions=[
                _anchor("SM81N_t1wd"),
                ActionSpec(kind="wait", ms=300),
            ],
            post_check=[C("SM81N_withdraw_confirm")],
            max_retries=5, retry_wait_ms=1000,
        ),
        Transition(
            name="T_squad1_box__endmenu",
            # 宽容转移：撤离确认对话框屏若被 locate 判成 squad1_box
            # （框按钮隔着半透明遮罩仍可 OCR），也要能"确定→菜单"。
            # 确定按钮 if_node 条件点（对话框已关时跳过，防重复点）。
            from_state="squad1_box", to_state="endmenu",
            actions=[
                _anchor("SM81N_withdraw_confirm",
                        if_node="SM81N_withdraw_confirm"),
                ActionSpec(kind="wait", ms=500),
                _click(324, 39),  # 左上角菜单（原 target [298,23,53,32] 中心）
                ActionSpec(kind="wait", ms=500),
            ],
            post_check=[C("SM81N_redeploy")],
            max_retries=3,
        ),

        # ---------- 编队链（入口 else 分支 / 稳定态 共用；从 2队框 进入） ----------
        Transition(
            name="T_map_full__squad2_box",
            from_state="map_full", to_state="squad2_box",
            actions=[
                _anchor("SM81N_map_t2"),  # 点击地图 2队 标记 → 机场框
                ActionSpec(kind="wait", ms=500),
            ],
            post_check=[C("SM81N_t1wd"), C("SM81N_box_cancel")],
            max_retries=5, retry_wait_ms=1000,
        ),
        Transition(
            name="T_squad1_box__formation",
            # squad1/squad2_box 同屏同优先级，locate 恒返回 squad1_box
            # （名称序在前）——编成入口转移必须挂在 squad1_box 上，
            # 两个框（1队入口检查 / 2队编队入口）共用同一动作。
            from_state="squad1_box", to_state="formation",
            actions=[
                _click(256, 629),  # 队伍编成 按钮（原 OCR 编成 roi [141,588,231,83] 中心）
                ActionSpec(kind="wait", ms=500),
            ],
            post_check=[C("SM81N_formation")],
            max_retries=3,
        ),
        Transition(
            name="T_formation__charselect",
            from_state="formation", to_state="charselect",
            actions=[
                _click(234, 339),  # 2队打手槽位卡（原 target [193,278,82,123] 中心）
                ActionSpec(kind="wait", ms=500),
            ],
            # 到达选人屏·基础屏（弹层必须关闭：弹层"确认"只在弹层内可见）
            post_check=[C("SM81N_charselect"),
                        C("SM81N_fav_confirm", inverted=True)],
            max_retries=3,
        ),
        Transition(
            name="T_charselect__charselect_filter",
            # 打开筛选弹层：点"显示种类"标签（基础屏右上，两种筛选态都可见）。
            from_state="charselect", to_state="charselect_filter",
            actions=[
                _anchor("SM81N_charselect"),
                ActionSpec(kind="wait", ms=400),
            ],
            # 弹层打开 ⇔ "仅显示收藏角色" + 弹层"确认" 同屏可见
            post_check=[C("SM81N_fav_filter"), C("SM81N_fav_confirm")],
            max_retries=5, retry_wait_ms=1000,
        ),
        Transition(
            name="T_charselect_filter__charselect_shown",
            # 弹层内应用收藏筛选：勾"仅显示收藏角色" → 点弹层"确认"
            # → 回基础屏且标签变"选择中"。
            # 首动作 unless_node 兜底：若弹层已关（上轮确认被吞/重试错位），
            # 先重开（重开的弹层复选框反映当前筛选态 → 后续勾选必然
            # 收敛；post_check 校验最终态，失败重试继续收敛）。
            # 复选框是 toggle：若已勾选（上次点中、确认被吞）会先取消，
            # 本轮 post_check 失败 → 重试时弹层已关 → 重开 → 重新勾选。
            from_state="charselect_filter", to_state="charselect_shown",
            actions=[
                _anchor("SM81N_charselect", unless_node="SM81N_fav_confirm"),
                ActionSpec(kind="wait", ms=300),
                _anchor("SM81N_fav_filter"),
                ActionSpec(kind="wait", ms=300),
                _anchor("SM81N_fav_confirm"),
                ActionSpec(kind="wait", ms=500),
            ],
            # 回基础屏 + 筛选生效（标签=选择中）+ 弹层已关
            post_check=[C("SM81N_charselect"),
                        C("SM81N_filter_all", inverted=True),
                        C("SM81N_fav_confirm", inverted=True)],
            max_retries=5, retry_wait_ms=1000,
        ),
        Transition(
            name="T_charselect_filter__charselect",
            # 弹层内"确认"（不改勾选）→ 回基础屏，筛选态保持原样
            # （恢复/退出弹层用；两状态间的检查边）。
            from_state="charselect_filter", to_state="charselect",
            actions=[
                _anchor("SM81N_fav_confirm"),
                ActionSpec(kind="wait", ms=500),
            ],
            post_check=[C("SM81N_charselect"),
                        C("SM81N_fav_confirm", inverted=True)],
            max_retries=5, retry_wait_ms=1000,
        ),
        Transition(
            name="T_charselect__formation",
            # 宽容转移：筛选未生效（charselect）时也要能完成 swap
            # （"编队中1"卡全图模板定位，未筛选列表里同样可点）
            from_state="charselect", to_state="formation",
            actions=_swap_actions(),
            post_check=[C("SM81N_formation")],
            max_retries=5, retry_wait_ms=1000,
        ),
        Transition(
            name="T_charselect_shown__formation",
            # swap：编辑栏卡 → "编队中1"徽章卡（模板定位，位置随排序变化）
            # → 确定 → 返回编成屏。选人屏点击最易被吞：retries=5。
            from_state="charselect_shown", to_state="formation",
            actions=_swap_actions(),
            post_check=[C("SM81N_formation")],
            max_retries=5, retry_wait_ms=1000,
        ),
        Transition(
            name="T_formation__map_full",
            from_state="formation", to_state="map_full",
            actions=[
                _anchor("SM81N_back2map"),  # 左上返回
                ActionSpec(kind="wait", ms=1500),
            ],
            post_check=[C("SM81N_map"), C("SM81N_map_t1"), C("SM81N_map_t2"),
                        C("SM81N_map_t3"), C("SM81N_start")],
            max_retries=3,
        ),

        # ---------- 开始作战 ----------
        Transition(
            name="T_map_full__battle",
            # 装备溢出弹窗在转移内自愈（if_node 链，映射 战斗.json
            # 选择装备处理方式 → 81N进入_点击8-1N 的恢复路径）：
            # 点"装备强化"关弹窗 → 回 8-1N 列表 → 点入口 → 普通作战
            # → 地图（保持部署）→ 再点开始作战（装备已处理，不再弹窗）。
            # 无弹窗时所有 if_node 步骤自动跳过。
            from_state="map_full", to_state="battle",
            actions=[
                _anchor("SM81N_start"),
                ActionSpec(kind="wait", ms=2000),
                _anchor("SM81N_equip_overflow", if_node="SM81N_equip_overflow"),
                ActionSpec(kind="wait", ms=1000),
                _anchor("SM81N_stage_list", if_node="SM81N_stage_list"),
                ActionSpec(kind="wait", ms=1500),
                _anchor("SM81N_stage_detail", if_node="SM81N_stage_detail"),
                ActionSpec(kind="wait", ms=3000),
                _anchor("SM81N_start", if_node="SM81N_start"),
                ActionSpec(kind="wait", ms=2000),
            ],
            post_check=[C("SM81N_bar_end")],
            max_retries=3, retry_wait_ms=1500, post_wait_ms=1500,
        ),
        Transition(
            name="T_equip_overflow__stage_detail",
            # 装备溢出恢复（映射 战斗.json 选择装备处理方式 → 81N进入_点击8-1N）：
            # 点"装备强化"选项关弹窗 → 点 8-1N 入口重进
            from_state="equip_overflow", to_state="stage_detail",
            actions=[
                _anchor("SM81N_equip_overflow"),
                ActionSpec(kind="wait", ms=1000),
                _anchor("SM81N_stage_list"),
                ActionSpec(kind="wait", ms=1500),
            ],
            post_check=[C("SM81N_stage_detail")],
            max_retries=3,
        ),

        # ---------- 补给准备：2队框 → 补给 → 再开框 → 撤离 → 确定 ----------
        Transition(
            name="T_battle__t2_box",
            # 点击 2队 标记：未选中时第一下仅选中（无框）→ 重试再点一次开框
            # （实测行为，原 pipeline 靠节点链重试实现同一效果）
            from_state="battle", to_state="t2_box",
            actions=[
                _anchor("SM81N_map_t2"),
                ActionSpec(kind="wait", ms=500),
            ],
            post_check=[C("SM81N_supply"), C("SM81N_start", inverted=True)],
            max_retries=5, retry_wait_ms=1000,
        ),
        Transition(
            name="T_t2_box__battle",
            from_state="t2_box", to_state="battle",
            actions=[
                _anchor("SM81N_supply"),
                ActionSpec(kind="wait", ms=800),
            ],
            post_check=[C("SM81N_bar_end"), C("SM81N_supply", inverted=True)],
            max_retries=3,
        ),
        Transition(
            name="T_t2_box__withdraw_ok",
            from_state="t2_box", to_state="withdraw_ok",
            actions=[
                _anchor("SM81N_popup_withdraw"),
                ActionSpec(kind="wait", ms=300),
            ],
            post_check=[C("SM81N_withdraw_confirm")],
            max_retries=3,
        ),
        Transition(
            name="T_withdraw_ok__battle_no2",
            # 战中 2队 撤离确定 → 2队标记离场（!t2）
            from_state="withdraw_ok", to_state="battle_no2",
            actions=[
                _anchor("SM81N_withdraw_confirm"),
                ActionSpec(kind="wait", ms=800),
            ],
            post_check=[C("SM81N_bar_end"), C("SM81N_supply", inverted=True),
                        C("SM81N_map_t2", inverted=True),
                        C("SM81N_t1wd", inverted=True)],
            max_retries=3,
        ),
        Transition(
            name="T_battle__battle_no2",
            # 宽容转移：撤离确认对话框盖在战斗屏上时（t2 标记可见、无补给
            # 按钮），locate 落到 battle——确定按钮按 if_node 条件点
            from_state="battle", to_state="battle_no2",
            actions=[
                _anchor("SM81N_withdraw_confirm", if_node="SM81N_withdraw_confirm"),
                ActionSpec(kind="wait", ms=800),
            ],
            post_check=[C("SM81N_bar_end"), C("SM81N_supply", inverted=True),
                        C("SM81N_map_t2", inverted=True),
                        C("SM81N_t1wd", inverted=True)],
            max_retries=3,
        ),

        # ---------- 计划模式：1队槽 → 炸狗点1 → 炸狗点2（AP 7→1）→ 执行 ----------
        Transition(
            name="T_battle_no2__plan",
            from_state="battle_no2", to_state="plan",
            actions=[
                _click(71, 624),  # 计划 按钮（原 target [60,621,23,7] 中心）
                ActionSpec(kind="wait", ms=500),
            ],
            post_check=[C("SM81N_plan"), C("SM81N_ap1", inverted=True)],
            max_retries=3,
        ),
        Transition(
            name="T_postbattle__plan",
            # 宽容转移：战后妖精 OCR 漏判时 locate 可能落到 postbattle
            from_state="postbattle", to_state="plan",
            actions=[
                _click(71, 624),
                ActionSpec(kind="wait", ms=500),
            ],
            post_check=[C("SM81N_plan"), C("SM81N_ap1", inverted=True)],
            max_retries=3,
        ),
        Transition(
            name="T_plan__plan_done",
            from_state="plan", to_state="plan_done",
            actions=[
                _click(292, 662),  # 计划栏 1队 图标（原 target [282,659,20,7] 中心）
                ActionSpec(kind="wait", ms=800),
                _click(265, 519),  # 炸狗点1 = 红色敌方基地（原 target [261,517,9,5]）
                ActionSpec(kind="wait", ms=800),
                _click(344, 199),  # 炸狗点2 = 2队港口格（原 target [338,193,13,12]）
                ActionSpec(kind="wait", ms=800),
            ],
            post_check=[C("SM81N_plan"), C("SM81N_ap1")],
            max_retries=3, retry_wait_ms=1500,
        ),
        Transition(
            name="T_plan_done__battle_no2",
            # 执行计划 → loading(794) → 战斗自动进行（3-5s 过渡）
            from_state="plan_done", to_state="battle_no2",
            actions=[
                _anchor("SM81N_plan"),  # 执行计划 按钮
                ActionSpec(kind="wait", ms=2500),
            ],
            post_check=[C("SM81N_bar_end"), C("SM81N_plan", inverted=True)],
            max_retries=10, retry_wait_ms=1500, post_wait_ms=1000,
        ),

        # ---------- 战后重置：撤 1队 → 终止作战菜单 → 重新作战 ----------
        Transition(
            name="T_postbattle__t1_wd_popup",
            # 战后 1队 被自动选中：一点即开框（原 target [585,271,10,19] 中心）
            from_state="postbattle", to_state="t1_wd_popup",
            actions=[
                _click(590, 280),
                ActionSpec(kind="wait", ms=800),
            ],
            post_check=[C("SM81N_t1wd"), C("SM81N_start", inverted=True)],
            max_retries=5, retry_wait_ms=1000,
        ),
        Transition(
            name="T_battle_no2__t1_wd_popup",
            # 宽容转移：战后妖精 OCR 漏判时 locate 落到 battle_no2
            from_state="battle_no2", to_state="t1_wd_popup",
            actions=[
                _click(590, 280),
                ActionSpec(kind="wait", ms=800),
            ],
            post_check=[C("SM81N_t1wd"), C("SM81N_start", inverted=True)],
            max_retries=5, retry_wait_ms=1000,
        ),
        Transition(
            name="T_t2_box__t1_wd_popup",
            # 宽容转移：战后 1队框若与战中 2队框同布局（补给按钮可见），
            # locate 落到 t2_box（优先级 39 < 44）——开框动作相同
            from_state="t2_box", to_state="t1_wd_popup",
            actions=[
                _click(590, 280),
                ActionSpec(kind="wait", ms=800),
            ],
            post_check=[C("SM81N_t1wd"), C("SM81N_start", inverted=True)],
            max_retries=5, retry_wait_ms=1000,
        ),
        Transition(
            name="T_t1_wd_popup__withdraw_ok",
            from_state="t1_wd_popup", to_state="withdraw_ok",
            actions=[
                _anchor("SM81N_t1wd"),
                ActionSpec(kind="wait", ms=300),
            ],
            post_check=[C("SM81N_withdraw_confirm")],
            max_retries=3,
        ),
        Transition(
            name="T_withdraw_ok__endmenu",
            # 确定（仅对话框仍开着时点——if_node 防重试时重复点）→ 左上角菜单
            from_state="withdraw_ok", to_state="endmenu",
            actions=[
                _anchor("SM81N_withdraw_confirm", if_node="SM81N_withdraw_confirm"),
                ActionSpec(kind="wait", ms=500),
                _click(324, 39),  # 左上角菜单（原 target [298,23,53,32] 中心）
                ActionSpec(kind="wait", ms=500),
            ],
            post_check=[C("SM81N_redeploy")],
            max_retries=3,
        ),
        Transition(
            name="T_postbattle__endmenu",
            # 宽容转移：战后对话框屏 locate 落到 postbattle（妖精可见）
            from_state="postbattle", to_state="endmenu",
            actions=[
                _anchor("SM81N_withdraw_confirm", if_node="SM81N_withdraw_confirm"),
                ActionSpec(kind="wait", ms=500),
                _click(324, 39),
                ActionSpec(kind="wait", ms=500),
            ],
            post_check=[C("SM81N_redeploy")],
            max_retries=3,
        ),
        Transition(
            name="T_battle_no2__endmenu",
            # 宽容转移：战后 1队撤离后选中态被清除（妖精消失）→ locate
            # 落到 battle_no2
            from_state="battle_no2", to_state="endmenu",
            actions=[
                _anchor("SM81N_withdraw_confirm", if_node="SM81N_withdraw_confirm"),
                ActionSpec(kind="wait", ms=500),
                _click(324, 39),
                ActionSpec(kind="wait", ms=500),
            ],
            post_check=[C("SM81N_redeploy")],
            max_retries=3,
        ),
        Transition(
            name="T_endmenu__map_full",
            # 重新作战：过渡 ~16s（实测），retries 覆盖
            from_state="endmenu", to_state="map_full",
            actions=[
                _anchor("SM81N_redeploy"),  # 重新作战（红色按钮）
                ActionSpec(kind="wait", ms=2500),
            ],
            post_check=[C("SM81N_map"), C("SM81N_map_t1"), C("SM81N_map_t2"),
                        C("SM81N_map_t3"), C("SM81N_start")],
            max_retries=12, retry_wait_ms=1500, post_wait_ms=1500,
        ),
    ]
