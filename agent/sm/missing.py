"""缺失图片资源登记（占位机制）。

状态检查需要图片资源；项目里暂时没有的，在这里登记：
- 代码中的检查照常定义（指向占位的 pipeline 识别节点名）；
- bridge 对"缺失"检查：状态检查按未命中处理并告警（不崩流）；
  锚点动作（需要命中框定位点击）则抛 MissingImageError；
- 同步维护工作目录下的临时清单文件（SM_缺失图片清单.md），由用户在后续输入中提供图片。

补齐方式：把用户提供的图片放入 assets/resource/image/ 对应目录，
更新对应 pipeline 识别节点的 template 字段，然后从 MISSING_CHECKS 删除该条目。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MissingCheck:
    node: str          # 占位的 pipeline 识别节点名
    what: str          # 需要截图/模板的内容
    where: str         # 使用位置（状态/转移）
    crop_hint: str     # 裁剪建议（位置/内容）


#: 当前缺失清单（2026-07 状态机改造 v1）—— 已空：
#: 8-1N 流所需图片全部齐备；main 状态检查已用'战斗'按钮双模板补齐
#: （common/main_battle_icon.png + common/main_battle_text.png，
#:  节点见 pipeline/state_machine/common_st.json）。
MISSING_CHECKS: dict[str, MissingCheck] = {}


def is_missing(node: str) -> bool:
    return node in MISSING_CHECKS


def describe_missing(node: str) -> str:
    m = MISSING_CHECKS.get(node)
    if m is None:
        return f"检查节点 {node} 的图片资源缺失（未登记）"
    return (
        f"检查节点 {node} 的图片资源缺失（占位）：{m.what}\n"
        f"  使用位置: {m.where}\n"
        f"  裁剪建议: {m.crop_hint}"
    )
