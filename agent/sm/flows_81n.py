"""8-1N 循环 流定义（状态机 v1 唯一落地的任务流，用于测试）。

流程（映射自 !81N总流程 及其各阶段 pipeline）：

    入口适配:  关卡列表 → 关卡详情 → 地图   （已在地图/已部分部署则自动跳过，幂等）
    部署:      缺哪队补哪队（1队/2队/3队，模板锚点点击 + 确定 + 地图标记检查）
    主循环 × N 轮:
      弹药检查:  2队弹药满 → 编成 2队打手（收藏筛选）→ 返回地图；  无弹药 → 跳过
      开始作战:  （可选：装备溢出弹窗处理）
      补给:      点2队 → 补给 → 再点2队 → 撤离（单zas 炸狗战术准备）
      计划:      计划模式 → 选1队+炸狗点1（行动点数 3→2）→ 炸狗点2（→1）→ 执行
      战斗:      等待战斗结束（妖精面板消失）
      重置:      撤1队 → 左上菜单 → 重新作战 → 回到"三队齐备"地图

开始界面要求（与原任务一致）：关卡列表页 / 8-1N 关卡详情页 / 8-1N 地图页。
"""
from __future__ import annotations

from .core import (
    Branch,
    CheckSpec,
    Flow,
    GotoState,
    Repeat,
    WaitUntil,
)
from .states_81n import states_81n, transitions_81n
from .states_common import common_states

C = CheckSpec


def build_flow_81n(rounds: int | None = 1) -> Flow:
    """rounds: 循环轮数；None = 无限循环（对应原"是否开启无限循环"选项）。"""
    steps = [
        # ---------- 入口适配（幂等） ----------
        Branch(
            when=[C("SM81N_stage_list")],
            then_steps=[GotoState("stage_detail"), GotoState("map")],
        ),
        Branch(
            when=[C("SM81N_stage_detail")],
            then_steps=[GotoState("map")],
        ),
        # ---------- 部署缺失梯队（幂等：缺哪队补哪队） ----------
        Branch(
            when=[C("SM81N_map"), C("SM81N_map_t1", inverted=True)],
            then_steps=[GotoState("map_t1")],
        ),
        Branch(
            when=[C("SM81N_map"), C("SM81N_map_t2", inverted=True)],
            then_steps=[GotoState("map_t12")],
        ),
        Branch(
            when=[C("SM81N_map"), C("SM81N_map_t3", inverted=True)],
            then_steps=[GotoState("map_full")],
        ),
        # ---------- 主循环 ----------
        Repeat(
            rounds,
            [
                # 弹药充足 → 编成 2队打手
                Branch(
                    when=[C("SM81N_ammo_full")],
                    then_steps=[
                        GotoState("formation"),
                        GotoState("charselect"),
                        GotoState("charselect_shown"),
                        GotoState("formation"),
                        GotoState("map_full"),
                    ],
                ),
                # 开始作战（装备溢出弹窗出现才处理）
                GotoState("battle"),
                # 2队 单zas 补给 → 撤离
                GotoState("battle_popup_supply"),
                GotoState("battle"),
                GotoState("battle_popup_withdraw"),
                GotoState("battle"),
                # 计划：选1队+炸狗点1（行动点数3→2）→ 炸狗点2（→1）→ 执行
                GotoState("plan"),
                GotoState("plan_p1"),
                GotoState("plan_2pts"),
                GotoState("battle"),
                # 等待战斗结束（最多 10 分钟，每 3 秒轮询）
                WaitUntil(
                    checks=[C("SM81N_battle", inverted=True), C("SM81N_map")],
                    timeout_ms=600000,
                    poll_ms=3000,
                ),
                # 重置：撤 1队 → 重新作战
                GotoState("t1_wd_popup"),
                GotoState("withdraw_ok"),
                GotoState("endmenu"),
                GotoState("map_full"),
            ],
        ),
    ]

    return Flow(
        name="8-1N循环(状态机)",
        entry_state="map_full",
        states=common_states() + states_81n(),
        transitions=transitions_81n(),
        steps=steps,
    )
