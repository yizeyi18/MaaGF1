"""8-1N 循环流（v2）：入口适配 + 入口直查 1队 + N 轮（稳定态代理检查 + 战斗 + 重置）。

轮内步骤（全部映射自实机 walkthrough 测量）：
  1. 稳定态弹药检查（2队地图徽章 Gnewfullammo，先查"有"）：
     满 ⇐ 上轮补给过 → 编队轮换（换"编队中1"打手进 2队槽）；
     空 → 跳过（稳定态不应发生，容错直走）。
  2. 开始作战（含装备溢出弹窗的可选处理/恢复）。
  3. 补给准备：点 2队 标记开框（未选中时首下仅选中，重试开框）
     → 补给 → 再点 2队 开框 → 撤离 → 确定（2队满弹离场）。
  4. 计划模式：1队槽 → 炸狗点1 → 炸狗点2（AP 7→1）→ 执行计划。
  5. 等待战斗结束：loading(794) → 结算 → 战后（1队自动选中、
     妖精面板出现；轮询最多 10 分钟，过场屏点击无效故不主动点）。
  6. 重置：点战后 1队 开框（自动选中，一点即开）→ 撤离 → 确定
     → 左上角菜单 → 重新作战 → 地图全部署（~16s 过渡）。

入口（循环前一次性，幂等）：
  关卡列表 → 关卡详情 → 地图 → 补部署 1/2/3队 →
  **入口弹药检查：直接开 1队框读"弹药 X/Y"行**（squad1_zero，
  不用 2队代理推断——代理仅在稳定态有效）：
    无 0（打手有弹）→ 关框，不编队；
    有 0（打手耗空）→ 关框 → 编队轮换 → 地图。

rounds=None = 无限循环（外部 stop 事件停止）。
"""
from __future__ import annotations

from .core import Branch, Flow, GotoState, PanNormalize, Repeat, WaitUntil, CheckSpec, SetFlag
from .states_81n import states_81n, transitions_81n
from .states_common import common_states

C = CheckSpec

# 地图平移归一化（"制备状态"方案）：地图可被用户/误操作拖动（平移），
# 偏离参考位后所有固定 ROI 地图模板与固定坐标动作失效。
# 地标 = 参考地图 (330,300) 起的 260×220 地形裁剪（纯背景，实测唯一、
# 跨平移匹配 0.96）；ref = 地标在参考地图中的 top-left。
# 实测拖动映射：1600ms 慢拖，地图内容位移 ≈ 0.59× 光标位移
# （400ms 快拖不被识别）。gain=1.0 → 每轮消除 ~59% 残差。
PAN_NORM = PanNormalize(landmark_node="SM81N_map_landmark", ref=(330, 300))

# 编队轮换链：2队框 → 编成 → 选人基础屏 →（筛选关则：弹层→勾收藏→确认）
# → swap → 编成 → 地图。
# 选人屏两状态（基础屏 / 筛选弹层）间转移各带检查（弹层"确认"+复选框
# 文案为弹层独有特征）；收藏筛选是持久设置，已生效时 Branch 条件
# filter_all 不命中 → 跳过弹层步，不会重复点"收藏"把筛选 toggle 关回去。
_FORMATION_CHAIN = [
    GotoState("squad2_box"),
    GotoState("formation"),
    GotoState("charselect"),
    Branch(when=[C("SM81N_filter_all")],
           then_steps=[GotoState("charselect_filter"),
                       GotoState("charselect_shown")]),
    GotoState("formation"),
    GotoState("map_full"),
]


def build_flow_81n(rounds: int = 1) -> Flow:
    steps = [
        # ---------- 入口归一化（幂等）：地图可能被拖动，先制备回参考位 ----------
        # 非地图屏（关卡列表/详情）时地标未识别 → 告警跳过，无副作用。
        PAN_NORM,

        # ---------- 入口适配（幂等：已在某步则原地继续） ----------
        Branch(when=[C("SM81N_stage_list")],
               then_steps=[GotoState("stage_detail"), GotoState("map")]),
        Branch(when=[C("SM81N_stage_detail")],
               then_steps=[GotoState("map")]),
        Branch(when=[C("SM81N_map"), C("SM81N_map_t1", inverted=True)],
               then_steps=[GotoState("map_t1")]),
        Branch(when=[C("SM81N_map"), C("SM81N_map_t2", inverted=True)],
               then_steps=[GotoState("map_t12")]),
        Branch(when=[C("SM81N_map"), C("SM81N_map_t3", inverted=True)],
               then_steps=[GotoState("map_full")]),

        # 部署动画/过场可能让 locate 闪烁：先固化到 map_full 再开 1队框
        GotoState("map_full"),

        # ---------- 入口弹药检查：直接查 1队（打手弹药），不用 2队 代理 ----------
        # rotated_this_cycle 标志：本战斗周期是否已轮换打手。入口轮换与轮内
        # 稳定态轮换互斥（一个战斗周期至多一次）——run#8（2026-10-10）实测：
        # 入口轮换后 2队徽章即变满弹，第 1 轮稳定态误判"上轮补给过"再次轮换，
        # 两次轮换之间没有战斗（用户判为逻辑错误）。
        GotoState("squad1_box"),
        Branch(
            when=[C("SM81N_squad1_zero", inverted=True)],
            then_steps=[GotoState("map_full"),  # 打手有弹：关框，不编队
                        SetFlag("rotated_this_cycle", False)],
            else_steps=[GotoState("map_full"),  # 打手耗空：关框 → 编队轮换
                        *_FORMATION_CHAIN,
                        SetFlag("rotated_this_cycle", True)],
        ),

        # ---------- 循环体 ----------
        Repeat(rounds, [
            # 归一化（幂等）：上一轮"重新作战"回到地图后，先制备地图回
            # 参考平移位再碰任何固定坐标（地图可能在轮间被拖动）。
            PAN_NORM,

            # 稳定态弹药检查：2队地图徽章（Gnewfullammo 先查"有"；
            # Gnewnoammo 有已知误报，不用它做分支条件）。
            # python_cond：与入口轮换互斥——本周期已轮换（入口或上轮末）则跳过，
            # 防止"入口轮换 → 徽章满弹 → 第 1 轮稳定态再轮换"的双轮换故障。
            Branch(when=[C("SM81N_ammo_full")],
                   python_cond=lambda r: not r.flags.get("rotated_this_cycle", False),
                   then_steps=list(_FORMATION_CHAIN) + [SetFlag("rotated_this_cycle", True)]),

            # 开始作战（装备溢出弹窗在 T_map_full__battle 内自愈）
            GotoState("battle"),

            # 补给准备：2队框 → 补给 → 再开框 → 撤离 → 确定（2队离场）
            GotoState("t2_box"),
            GotoState("battle"),
            GotoState("t2_box"),
            GotoState("withdraw_ok"),
            GotoState("battle_no2"),

            # 计划：1队槽 → 炸狗点1 → 炸狗点2（AP 7→1）→ 执行
            GotoState("plan"),
            GotoState("plan_done"),
            GotoState("battle_no2"),

            # 等战斗结束（loading → 结算 → 战后：妖精面板=1队已选中）
            WaitUntil(checks=[C("SM81N_battle")],
                      timeout_ms=600000, poll_ms=3000),

            # 重置：撤 1队 → 重新作战 → 地图
            GotoState("t1_wd_popup"),
            GotoState("withdraw_ok"),
            GotoState("endmenu"),
            GotoState("map_full"),
            # 轮完成：清轮换标志，下一轮稳定态可再次轮换
            SetFlag("rotated_this_cycle", False),
        ]),
    ]

    return Flow(
        name="8-1N循环(状态机v2)",
        entry_state="map_full",
        states=common_states() + states_81n(),
        transitions=transitions_81n(),
        steps=steps,
        # 两阶段定位的家族签名链（有序、早停；全扫描兜底保证正确性）。
        # 排序原则：高频屏的家族靠前（地图屏占 locate 大头）；单状态特异
        # 家族（补给框/重新作战/撤离确认…）在其屏幕特征节点处快速命中；
        # 最不特异的 bar_end（多个战中屏共有）放最后。
        # 实测（run#8 日志）：v1 全扫描定位 30-110s/次；两阶段后
        # 地图屏 ~10-18s、框屏 ~18-24s、战中框 ~12-16s、结算屏 ~25-35s。
        family_discriminators=[
            ("stage", [C("SM81N_stage_list")]),
            ("map", [C("SM81N_map"), C("SM81N_bar_end", inverted=True)]),
            ("inbattle_box", [C("SM81N_supply")]),
            ("box", [C("SM81N_t1wd")]),
            ("charselect", [C("SM81N_charselect")]),
            ("planfam", [C("SM81N_plan")]),
            ("endmenu", [C("SM81N_redeploy")]),
            ("wd_popup", [C("SM81N_withdraw_confirm")]),
            ("formation", [C("SM81N_formation")]),
            ("equip", [C("SM81N_equip_overflow")]),
            ("settlement", [C("SM81N_settlement")]),
            ("battle", [C("SM81N_bar_end")]),
        ],
    )
