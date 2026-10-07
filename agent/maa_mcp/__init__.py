"""maa_mcp —— MaaGF1 远程控制 MCP 服务器（运行在 agent 进程内）。

功能：
- 基元操作：截图 / OCR / 模板匹配 / 点击 / 长按 / 滑动（以 session 为单位）
- JSON 脚本解释器：MAA pipeline 片段注入 + 框架原生执行
- 内置成套任务：列出 / 执行 / 停止 / 状态查询
- session 管理：列出 / 创建 / 终止；全局仅一个 session 可执行互动操作（活跃锁）
- 资源热更新：推送文件到 resource 目录 + 重新加载
- 鉴权：HTTP Bearer API Key

配置：exe 同目录 maa_mcp.conf（JSON，首次启动自动生成 api_key）。
"""
from __future__ import annotations

__all__ = [
    "config",
    "hub",
    "session",
    "bridge",
    "tools",
    "server",
]
