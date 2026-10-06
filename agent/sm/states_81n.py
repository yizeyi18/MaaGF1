"""8-1N 循环的状态与转移定义。

每一步都映射自现有 pipeline（assets/resource/pipeline/tasks/Farm/Experience/8-1n/
进入.json / 部署.json / 编队.json / 弹药检查new.json / 战斗.json / 重置.json / 地图初始化.json），
坐标、识别节点、延时保持一致；识别类步骤统一改为"识别节点 + 状态检查"。

状态检查节点见 assets/resource/pipeline/state_machine/8-1n_st.json。
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
# 状态（8-1N 专属 + 复用 states_common 的公共状态）
# ======================================================================================

# ---- 地图（部署）状态族：精确区分"部署了哪几队"，保证幂等与可定位 ----
S_MAP = State(
    name="map",
    checks=[C("SM81N_map"), C("SM81N_map_t1", inverted=True),
            C("SM81N_map_t2", inverted=True), C("SM81N_map_t3", inverted=True)],
    desc="8-1N 地图（未部署）",
    locate_priority=30,
)
S_MAP_T1 = State(
    name="map_t1",
    checks=[C("SM81N_map"), C("SM81N_map_t1"),
            C("SM81N_map_t2", inverted=True), C("SM81N_map_t3", inverted=True)],
    desc="8-1N 地图（1队已部署）",
    locate_priority=29,
)
S_MAP_T12 = State(
    name="map_t12",
    checks=[C("SM81N_map"), C("SM81N_map_t1"), C("SM81N_map_t2"),
            C("SM81N_map_t3", inverted=True)],
    desc="8-1N 地图（1/2队已部署）",
    locate_priority=28,
)
S_MAP_FULL = State(
    name="map_full",
    checks=[C("SM81N_map"), C("SM81N_map_t1"), C("SM81N_map_t2"),
            C("SM81N_map_t3"), C("SM81N_start")],
    desc="8-1N 地图（三队齐备、可见开始作战）",
    locate_priority=27,
)

# ---- 战斗相关状态 ----
S_BATTLE = State(
    name="battle",
    # 排除 2队弹窗（弹窗屏幕上 妖精/结束栏 同样可见）
    checks=[C("SM81N_battle"), C("SM81N_bar_end"),
            C("SM81N_supply", inverted=True),
            C("SM81N_popup_withdraw", inverted=True)],
    desc="战斗中（正常底栏，结束回合可见）",
    locate_priority=40,
)
S_BATTLE_POPUP_SUPPLY = State(
    name="battle_popup_supply",
    checks=[C("SM81N_supply")],
    desc="战斗中 2队弹窗（补给可见）",
    locate_priority=39,
)
S_BATTLE_POPUP_WITHDRAW = State(
    name="battle_popup_withdraw",
    # 2队"撤离"按钮与战后 1队"撤离"按钮位置接近，必须带"战斗中"上下文区分
    checks=[C("SM81N_popup_withdraw"), C("SM81N_battle")],
    desc="战斗中 2队弹窗（撤离可见）",
    locate_priority=38,
)
S_EQUIP_OVERFLOW = State(
    name="equip_overflow",
    checks=[C("SM81N_equip_overflow")],
    desc="装备溢出处理弹窗（装备强化选项）",
    locate_priority=37,
)

# ---- 计划模式状态族 ----
# 可观测事实 = 计划点数（行动点数）计数器：3(未录入) → 2(炸狗点1) → 1(炸狗点1+2)。
# "1队已选中"本身不可观测（无稳定模板），故与"录入炸狗点1"合并为一次转移。
S_PLAN = State(
    name="plan",
    checks=[C("SM81N_plan"), C("SM81N_ap1", inverted=True),
            C("SM81N_ap2", inverted=True)],
    desc="计划模式（未录入计划点，行动点数=3）",
    locate_priority=36,
)
S_PLAN_P1 = State(
    name="plan_p1",
    checks=[C("SM81N_ap2"), C("SM81N_ap1", inverted=True),
            C("SM81N_battle", inverted=True)],
    desc="计划模式（炸狗点1 已录入，行动点数=2）",
    locate_priority=35,
)
S_PLAN_2PTS = State(
    name="plan_2pts",
    checks=[C("SM81N_ap1"), C("SM81N_ap2", inverted=True),
            C("SM81N_battle", inverted=True)],
    desc="计划模式（炸狗点1+2 已录入，行动点数=1）",
    locate_priority=34,
)

# ---- 战后/重置状态族 ----
S_POSTBATTLE = State(
    name="postbattle",
    checks=[C("SM81N_map"), C("SM81N_map_t2", inverted=True),
            C("SM81N_battle", inverted=True), C("SM81N_start", inverted=True)],
    desc="战后地图（2队已撤离、战斗结束）",
    locate_priority=45,
)
S_T1_WD_POPUP = State(
    name="t1_wd_popup",
    # 战后上下文：妖精面板已消失（与 2队弹窗的"撤离"区分）
    checks=[C("SM81N_t1wd"), C("SM81N_battle", inverted=True)],
    desc="战后 1队弹窗（撤离可见）",
    locate_priority=44,
)
S_WITHDRAW_OK = State(
    name="withdraw_ok",
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


def states_81n() -> list[State]:
    return [
        S_MAP, S_MAP_T1, S_MAP_T12, S_MAP_FULL,
        S_BATTLE, S_BATTLE_POPUP_SUPPLY, S_BATTLE_POPUP_WITHDRAW, S_EQUIP_OVERFLOW,
        S_PLAN, S_PLAN_P1, S_PLAN_2PTS,
        S_POSTBATTLE, S_T1_WD_POPUP, S_WITHDRAW_OK, S_ENDMENU,
    ]


# ======================================================================================
# 动作简写
# ======================================================================================

def _click(x: int, y: int, **kw) -> ActionSpec:
    return ActionSpec(kind="click", x=x, y=y, **kw)


def _anchor(node: str, kind: str = "click", **kw) -> ActionSpec:
    """锚点动作：先识别 node，再对命中框中心执行 kind。"""
    return ActionSpec(kind=kind, anchor_node=node, **kw)


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
            post_check=[C("SM81N_map")],
            max_retries=3,
        ),

        # ---------- 部署（每队：点击港口(模板锚点) → 确定(OCR锚点) → 检查地图标记） ----------
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
            from_state="map_t12", to_state="map_full",
            actions=[
                _anchor("SM81N_porth", kind="long_press", duration=20),
                ActionSpec(kind="wait", ms=500),
                _anchor("SM81N_deploy_confirm"),
                ActionSpec(kind="wait", ms=1000),
            ],
            post_check=[C("SM81N_map_t3"), C("SM81N_start")],
            max_retries=3,
        ),

        # ---------- 弹药充足 → 编成 2队打手（映射 编队.json 全流程） ----------
        Transition(
            name="T_map_full__formation",
            from_state="map_full", to_state="formation",
            actions=[
                _anchor("SM81N_map_t2"),  # 点击地图上的 2队 标记
                ActionSpec(kind="wait", ms=500),
                _click(256, 629),         # 编成 按钮（原 OCR 锚点 roi [141,588,231,83] 中心）
            ],
            post_check=[C("SM81N_formation")],
            max_retries=3,
        ),
        Transition(
            name="T_formation__charselect",
            from_state="formation", to_state="charselect",
            actions=[_click(234, 339)],   # 2队打手槽位（原 target [193,278,82,123] 中心）
            post_check=[C("SM81N_charselect")],
            max_retries=3,
        ),
        Transition(
            name="T_charselect__charselect_shown",
            from_state="charselect", to_state="charselect_shown",
            actions=[_anchor("SM81N_charselect")],
            post_check=[C("SM81N_fav_filter")],
            max_retries=3,
        ),
        Transition(
            name="T_charselect_shown__formation",
            from_state="charselect_shown", to_state="formation",
            actions=[
                _anchor("SM81N_fav_filter"),   # 收藏 筛选
                _anchor("SM81N_fav_confirm"),  # 确认
            ],
            post_check=[C("SM81N_formation")],
            max_retries=3,
        ),
        Transition(
            name="T_formation__map_full",
            from_state="formation", to_state="map_full",
            actions=[
                _click(89, 656),                    # 队伍中的打手（原 target [64,636,50,41] 中心）
                ActionSpec(kind="wait", ms=200),
                _anchor("SM81N_team_char"),         # 选中角色卡
                ActionSpec(kind="wait", ms=500),
                _anchor("SM81N_formation_confirm"),  # 确定
                ActionSpec(kind="wait", ms=400),
                _anchor("SM81N_back2map"),          # 返回地图
                ActionSpec(kind="wait", ms=1500),
            ],
            post_check=[C("SM81N_map"), C("SM81N_map_t1"), C("SM81N_map_t2"),
                        C("SM81N_map_t3"), C("SM81N_start")],
            max_retries=3,
        ),

        # ---------- 开始作战（含装备溢出弹窗的可选处理） ----------
        Transition(
            name="T_map_full__battle",
            from_state="map_full", to_state="battle",
            actions=[
                _anchor("SM81N_start"),
                ActionSpec(kind="wait", ms=2000),
                # 可选：装备溢出处理弹窗（出现才点）
                _anchor("SM81N_equip_overflow", if_node="SM81N_equip_overflow"),
                ActionSpec(kind="wait", ms=2000),
            ],
            post_check=[C("SM81N_battle"), C("SM81N_bar_end"),
                        C("SM81N_equip_overflow", inverted=True)],
            max_retries=3,
        ),

        # ---------- 2队 单zas 补给 → 撤离（炸狗战术准备） ----------
        Transition(
            name="T_battle__battle_popup_supply",
            from_state="battle", to_state="battle_popup_supply",
            actions=[_anchor("SM81N_map_t2")],
            post_check=[C("SM81N_supply")],
            max_retries=3,
        ),
        Transition(
            name="T_battle_popup_supply__battle",
            from_state="battle_popup_supply", to_state="battle",
            actions=[_anchor("SM81N_supply")],
            post_check=[C("SM81N_battle")],
            max_retries=3,
        ),
        Transition(
            name="T_battle__battle_popup_withdraw",
            from_state="battle", to_state="battle_popup_withdraw",
            actions=[_anchor("SM81N_map_t2")],
            post_check=[C("SM81N_popup_withdraw")],
            max_retries=3,
        ),
        Transition(
            name="T_battle_popup_withdraw__battle",
            from_state="battle_popup_withdraw", to_state="battle",
            actions=[
                _anchor("SM81N_popup_withdraw"),
                ActionSpec(kind="wait", ms=300),
                _anchor("SM81N_confirm_dialog"),  # 撤离确认（全屏 OCR 确定）
            ],
            post_check=[C("SM81N_battle")],
            max_retries=3,
        ),

        # ---------- 计划模式：1队 → 炸狗点1 → 炸狗点2 → 执行 ----------
        Transition(
            name="T_battle__plan",
            from_state="battle", to_state="plan",
            actions=[
                _click(71, 624),  # 计划 按钮（原 target [60,621,23,7] 中心）
                ActionSpec(kind="wait", ms=200),
            ],
            post_check=[C("SM81N_plan"), C("SM81N_ap1", inverted=True),
                        C("SM81N_ap2", inverted=True)],
            max_retries=3,
        ),
        Transition(
            name="T_plan__plan_p1",
            from_state="plan", to_state="plan_p1",
            actions=[
                _click(292, 662),  # 计划栏 1队 图标（原 target [282,659,20,7] 中心）
                ActionSpec(kind="wait", ms=300),
                _anchor("SM81N_dog1"),  # 炸狗点1（dog1.png 锚点）
                ActionSpec(kind="wait", ms=300),
            ],
            post_check=[C("SM81N_ap2"), C("SM81N_ap1", inverted=True),
                        C("SM81N_battle", inverted=True)],
            max_retries=3, retry_wait_ms=1000,
        ),
        Transition(
            name="T_plan_p1__plan_2pts",
            from_state="plan_p1", to_state="plan_2pts",
            actions=[
                _anchor("SM81N_port2"),  # 炸狗点2 = 2队 港口格
                ActionSpec(kind="wait", ms=300),
            ],
            post_check=[C("SM81N_ap1"), C("SM81N_ap2", inverted=True),
                        C("SM81N_battle", inverted=True)],
            max_retries=3, retry_wait_ms=1000,
        ),
        Transition(
            name="T_plan_2pts__battle",
            from_state="plan_2pts", to_state="battle",
            actions=[
                _anchor("SM81N_plan"),  # 执行 按钮
                ActionSpec(kind="wait", ms=2000),
            ],
            post_check=[C("SM81N_battle"), C("SM81N_bar_end")],
            max_retries=5, retry_wait_ms=1500,
        ),

        # ---------- 战后重置：撤 1队 → 重新作战 ----------
        Transition(
            name="T_postbattle__t1_wd_popup",
            from_state="postbattle", to_state="t1_wd_popup",
            actions=[_click(590, 280)],  # 战后 1队 标记（原 target [585,271,10,19] 中心）
            post_check=[C("SM81N_t1wd")],
            max_retries=3,
        ),
        Transition(
            name="T_t1_wd_popup__withdraw_ok",
            from_state="t1_wd_popup", to_state="withdraw_ok",
            actions=[_anchor("SM81N_t1wd")],
            post_check=[C("SM81N_withdraw_confirm")],
            max_retries=3,
        ),
        Transition(
            name="T_withdraw_ok__endmenu",
            from_state="withdraw_ok", to_state="endmenu",
            actions=[
                _anchor("SM81N_withdraw_confirm"),
                ActionSpec(kind="wait", ms=500),
                _click(324, 39),  # 左上角菜单（原 target [298,23,53,32] 中心）
            ],
            post_check=[C("SM81N_redeploy")],
            max_retries=3,
        ),
        Transition(
            name="T_endmenu__map_full",
            from_state="endmenu", to_state="map_full",
            actions=[
                _anchor("SM81N_redeploy"),  # 重新作战
                ActionSpec(kind="wait", ms=2000),
            ],
            post_check=[C("SM81N_map"), C("SM81N_map_t1"), C("SM81N_map_t2"),
                        C("SM81N_map_t3"), C("SM81N_start")],
            max_retries=3,
        ),
    ]
