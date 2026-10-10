"""识别框兼容助手。

Python 绑定的 RecognitionDetail.box 类型注解是 Rect 对象（.x/.y/.w/.h），
但实际由 RectBuffer.get() 的原始值填充——运行时是 [x, y, w, h] list。
这里统一两种形态，避免各处 hasattr 判断散落。
"""
from __future__ import annotations

from typing import Optional, Tuple


def box_xywh(box) -> Optional[Tuple[int, int, int, int]]:
    """返回 (x, y, w, h)；box 为空返回 None。"""
    if box is None:
        return None
    if hasattr(box, "x"):
        return int(box.x), int(box.y), int(box.w), int(box.h)
    v = list(box)
    if len(v) != 4:
        return None
    return int(v[0]), int(v[1]), int(v[2]), int(v[3])
