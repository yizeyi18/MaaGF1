"""全游戏通用的 UI 界面状态（可复用状态机的"公共层"）。

界面层次模型（来自需求）：
    主界面
    ├── 二级页面：战斗、据点、研发、工厂、仓库、编成 等
    └── 三级页面：工厂内子项、战斗内选关卡、仓库内筛选 等
    （仓库 / 梯队编成 可从多场景跳入 —— 它们是独立状态，允许多条转移边汇入）

v1 现状：8-1N 流只用到其中少数状态；其余状态先声明骨架（检查节点指向
pipeline/state_machine/common_st.json，图片未齐前标记为缺失占位），
后续任务流化时逐步补齐。
"""
from __future__ import annotations

from .core import CheckSpec, State

# ---------------- 界面状态（通用） ----------------

#: 主界面。检查节点 SM_main_check 为占位（图片缺失，见 sm/missing.py）。
S_MAIN = State(
    name="main",
    checks=[CheckSpec("SM_main_check")],
    desc="主界面（根状态）",
    locate_priority=500,
)

#: 战斗-关卡选择列表页（含 8-1N 入口按钮）
S_STAGE_LIST = State(
    name="stage_list",
    checks=[CheckSpec("SM81N_stage_list")],
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

#: 编成-角色选择（已展开全部，可见 显示/收藏 筛选）—— 更具体，必须先于 charselect 尝试
S_CHARSELECT_SHOWN = State(
    name="charselect_shown",
    checks=[CheckSpec("SM81N_charselect"), CheckSpec("SM81N_fav_filter")],
    desc="编成-角色选择（显示/收藏 可见）",
    locate_priority=20,
)

#: 编成-角色选择
S_CHARSELECT = State(
    name="charselect",
    checks=[CheckSpec("SM81N_charselect")],
    desc="编成-角色选择（显示全部）",
    locate_priority=21,
)


def common_states() -> list[State]:
    return [S_MAIN, S_STAGE_LIST, S_STAGE_DETAIL, S_FORMATION, S_CHARSELECT, S_CHARSELECT_SHOWN]
