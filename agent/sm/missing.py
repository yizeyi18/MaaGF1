"""缺失图片资源登记（占位机制）。

状态检查需要图片资源；项目里暂时没有的，在这里登记：
- 代码中的检查照常定义（指向占位的 pipeline 识别节点名）；
- bridge 在真正执行该检查前发现节点被标记为缺失，抛 MissingImageError 并给出清晰提示；
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


#: 当前缺失清单（2026-07 状态机改造 v1）
MISSING_CHECKS: dict[str, MissingCheck] = {
    "SM_main_check": MissingCheck(
        node="SM_main_check",
        what="主界面（主菜单）的标志性模板图，用于判定'当前在主界面'",
        where="states_common.S_main（全游戏通用根状态，8-1N 流 v1 未实际使用）",
        crop_hint="1280x720 主界面截图，裁剪一个主界面独有、其他界面不会出现的稳定区域"
                  "（如底部'出击'按钮或右上角资源栏左侧的标识），建议 80x60 以内",
    ),
}


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
