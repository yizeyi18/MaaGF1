"""全游戏通用的 UI 界面状态（可复用状态机的"公共层"）。

界面层次模型（来自需求）：
    主界面
    ├── 二级页面：战斗、据点、研发、工厂、仓库、编成 等
    └── 三级页面：工厂内子项、战斗内选关卡、仓库内筛选 等
    （仓库 / 梯队编成 可从多场景跳入 —— 它们是独立状态，允许多条转移边汇入）

v1 现状：8-1N 流用到 stage_list/stage_detail/formation/charselect 族；
main 已可用（'战斗'按钮双模板）；据点/研发/工厂/仓库等其余状态在
后续任务流化时逐个补齐（检查节点放 pipeline/state_machine/common_st.json）。
"""
from __future__ import annotations

from .core import CheckSpec, State

# ---------------- 界面状态（通用） ----------------

#: 主界面（根状态）。检查 = 主界面'战斗'按钮 图标+文字 双模板：
#: 节点 SM_main_battle_icon / SM_main_battle_text（common_st.json），
#: 模板在 assets/resource/image/common/，裁剪自 1280x720 主界面截图，
#: 避开了随活动变化的'活动'角标。
S_MAIN = State(
    name="main",
    checks=[CheckSpec("SM_main_battle_icon"), CheckSpec("SM_main_battle_text")],
    desc="主界面（根状态）",
    locate_priority=500,
)

#: 战斗-关卡选择列表页（含 8-1N 入口按钮）
#: 注意：8-1N 面板打开时，列表按钮只是被遮罩变暗，模板仍稳定命中
#: （实测 0.942），会把 stage_detail 遮蔽掉——必须配反向检查排除。
S_STAGE_LIST = State(
    name="stage_list",
    checks=[CheckSpec("SM81N_stage_list"),
            CheckSpec("SM81N_stage_detail", inverted=True)],
    desc="战斗关卡选择列表（可见 8-1N 入口按钮）",
    locate_priority=10,
)

#: 8-1N 关卡详情（"普通作战"按钮）
S_STAGE_DETAIL = State(
    name="stage_detail",
    checks=[CheckSpec("SM81N_stage_detail")],
    desc="8-1N 关卡详情（普通作战按钮）",
    locate_priority=11,
)

#: 编成界面
S_FORMATION = State(
    name="formation",
    checks=[CheckSpec("SM81N_formation")],
    desc="编成界面（阵型编成）",
    locate_priority=22,
)

#: 编成-角色选择·筛选弹层（点"显示种类"后打开的大弹层）——特征 =
#: 弹层橙色"确认"按钮模板（SM81N_popup_confirm，弹层打开时独有；
#: 基础屏"确定"按钮在 x>=1100 被节点 ROI 排除）。
#: 2026-10-09 实机教训：弹层检测最初用 OCR（fav_filter 文案 +
#: fav_confirm"确认"），同一屏 guard 命中、0.5s 后 unless 检查 miss
#: （OCR 抖动）→ 兜底重开动作误点"显示种类"把弹层关掉 → 转移全灭。
#: 改模板匹配（静态 UI 按钮，确定性识别）。
#: 弹层打开时基础屏的"显示种类"标签仍在右上可见（charselect 节点也命中），
#: 故本状态优先级必须高于 charselect/charselect_shown。
S_CHARSELECT_FILTER = State(
    name="charselect_filter",
    checks=[CheckSpec("SM81N_popup_confirm")],
    desc="编成-角色选择·筛选弹层（显示种类已展开）",
    locate_priority=18,
)

#: 编成-角色选择（收藏筛选生效中）——"显示种类"按钮标签="选择中"
#: （filter_all 不命中 ⇔ 筛选开）。筛选是持久设置，跨轮保持：
#: 第二轮进选人屏直接就是本状态，goto 短路跳过 toggle（幂等）。
S_CHARSELECT_SHOWN = State(
    name="charselect_shown",
    checks=[CheckSpec("SM81N_charselect"),
            CheckSpec("SM81N_filter_all", inverted=True)],
    desc="编成-角色选择（收藏筛选生效中，标签=选择中）",
    locate_priority=20,
)

#: 编成-角色选择·基础屏（弹层关闭，筛选开/关两态都算）——"显示种类"
#: 标签（显示全部/选择中 均可读）。goto 目标用它保证两种筛选态都能
#: 短路；筛选态的区分交给 charselect_shown（优先级更高，locate 先判）。
S_CHARSELECT = State(
    name="charselect",
    checks=[CheckSpec("SM81N_charselect")],
    desc="编成-角色选择·基础屏（弹层关闭）",
    locate_priority=21,
)


def common_states() -> list[State]:
    return [S_MAIN, S_STAGE_LIST, S_STAGE_DETAIL, S_FORMATION,
            S_CHARSELECT_FILTER, S_CHARSELECT, S_CHARSELECT_SHOWN]
