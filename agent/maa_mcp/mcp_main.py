"""MCP 伴生进程入口（maa_agent.exe --mcp 模式）。

为什么必须独立进程：
- agent 主体进程 import 了 maa.agent（AgentServer 模式）。该模式下
  MaaAgentServer.dll 对 MaaTaskerCreate / MaaWin32ControllerCreate /
  MaaResourceCreate 全部是 NotImpl 桩（返回 nullptr），Toolkit 也明确
  不可用——框架禁止在 AgentServer 上下文里自建控制平面。
- 本进程只 import maa（不 import maa.agent），Library 保持完整框架模式：
  Tasker / Win32Controller / Resource / Toolkit 全部可用，窗口发现与
  GUI 走同一 C++ 代码路径（Toolkit.find_desktop_windows）。

由 agent 主体通过 spawn_mcp_coprocess() 自拉起（同一个 exe）。
"""
from __future__ import annotations

import os
from pathlib import Path


def mcp_coprocess_main(project_root: str, exe_dir: str) -> int:
    """伴生进程主流程（阻塞）。返回退出码。"""
    pid_file = Path(exe_dir) / "maa_mcp.pid"
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    # 初始化框架（完整框架模式）。DLL 目录已由 main.setup_dll_path 写入
    # MAAFW_BINARY_PATH 环境变量并被子进程继承。init_option 失败不致命
    # （窗口发现/Tasker 不依赖 toolkit 全局配置），仅记录。
    try:
        from maa.toolkit import Toolkit

        user_path = os.environ.get("MAAFW_BINARY_PATH") or exe_dir
        if not Toolkit.init_option(user_path):
            print(f"[maa_mcp] Toolkit.init_option returned False for {user_path}")
    except Exception as e:
        print(f"[maa_mcp] Toolkit.init_option failed (non-fatal): {e}")

    exit_code = 1
    try:
        from maa_mcp.server import start_mcp_server

        start_mcp_server(project_root, exe_dir)
        # start_mcp_server 内部 uvicorn.run 阻塞直到进程被终止
        exit_code = 0
    finally:
        try:
            if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_file.unlink()
        except Exception:
            pass
    return exit_code
