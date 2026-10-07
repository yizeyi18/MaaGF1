"""MCP 服务器配置。

配置文件：exe 同目录（开发模式为 agent 目录）的 maa_mcp.conf（JSON）：

    {
        "enable": true,
        "bind": "0.0.0.0",
        "port": 8180,
        "api_key": "<32 字节 urlsafe，首次启动自动生成>",
        "window_title_regex": "",
        "window_class_regex": ""
    }

- api_key 为空时自动生成并落盘；客户端请求头：Authorization: Bearer <api_key>
- window_*_regex 为空时回退到 interface.json 的 controller 配置
- 修改 enable/bind/port/api_key 后重启 agent（随 GUI 重启）生效
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class MaaMcpConfig:
    enable: bool = True
    bind: str = "0.0.0.0"
    port: int = 8180
    api_key: str = ""
    window_title_regex: str = ""
    window_class_regex: str = ""
    _path: str = field(default="", repr=False, compare=False)

    @classmethod
    def load(cls, path: str) -> "MaaMcpConfig":
        """读取配置；不存在时创建默认配置（生成 api_key）。"""
        cfg = cls(_path=path)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in data.items():
                    if hasattr(cfg, k) and k != "_path":
                        setattr(cfg, k, v)
            except Exception as e:  # 配置损坏 → 用默认值重建（保留原文件）
                print(f"[maa_mcp] 配置文件解析失败，使用默认值: {e}")
                try:
                    with open(path + ".broken", "w", encoding="utf-8") as f:
                        f.write(open(path, "r", encoding="utf-8").read())
                except Exception:
                    pass
        else:
            cfg.api_key = secrets.token_urlsafe(32)
            cfg.save()
        if not cfg.api_key:
            cfg.api_key = secrets.token_urlsafe(32)
            cfg.save()
        return cfg

    def save(self) -> bool:
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(
                    {k: v for k, v in asdict(self).items() if k != "_path"},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            try:
                os.chmod(self._path, 0o600)
            except Exception:
                pass  # Windows 无 chmod 语义，忽略
            return True
        except Exception as e:
            print(f"[maa_mcp] 配置保存失败: {e}")
            return False

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.bind in ("0.0.0.0", "::") else self.bind
        return f"http://{host}:{self.port}/mcp"


def default_conf_path(executable_dir: str) -> str:
    return os.path.join(executable_dir, "maa_mcp.conf")
