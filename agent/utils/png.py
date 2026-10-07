"""零依赖 PNG 编码器（numpy + zlib + struct）。

agent 打包环境不保证带 Pillow，MCP 返回截图需要 PNG 字节，
这里自己实现 8-bit RGB PNG 编码（filter=0，单 IDAT）。
"""
from __future__ import annotations

import struct
import zlib


def encode_png(arr, bgr: bool = True) -> bytes:
    """编码图像为 PNG 字节。

    Args:
        arr: HxWx3 或 HxWx4 的 uint8 数组。
        bgr: 输入通道顺序是否为 BGR(A)（MaaFramework 截图是 BGR，默认 True）；
             4 通道时自动丢弃 alpha 并翻转前 3 通道为 RGB。

    Returns:
        bytes: 完整 PNG 文件内容
    """
    import numpy as np

    a = np.asarray(arr)
    if a.ndim != 3:
        raise ValueError(f"expect 3D array, got shape {a.shape}")
    if a.shape[2] not in (3, 4):
        raise ValueError(f"expect 3 or 4 channels, got {a.shape[2]}")
    if bgr:
        a = a[:, :, [2, 1, 0, 3]][:, :, :3] if a.shape[2] == 4 else a[:, :, [2, 1, 0]]
    if a.dtype != np.uint8:
        a = a.astype(np.uint8)

    h, w = a.shape[:2]
    raw = b"".join(b"\x00" + a[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
