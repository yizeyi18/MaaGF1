"""maa_agent 可执行文件的薄冻结入口（bootstrap）。

设计（打包拆分）：PyInstaller 只冻结第三方依赖闭包
（maa / mcp / uvicorn / starlette / numpy / requests / ...），
一方代码（main / maa_mcp / sm / utils / action / my_reco）
一律以源码形式随构建分发在 <project>/agent/src/ 下。

运行时 bootstrap 把 agent/src 插入 sys.path 首位，再用
importlib 动态导入一方代码——静态分析看不到这些 import，
PyInstaller 不会把它们打进 PYZ。

好处：Python 侧改动只需覆盖 agent/src（几百 KB 源码），
无需重新 PyInstaller、无需重新下载 exe。exe 只在
第三方依赖变化（maafw 版本 / build.spec / bootstrap 本身）
时重建。

目录布局（部署后）：
    <project>/
        interface.json
        resource/
        runtimes/<arch>/native/      ← MFAAvalonia 运行时（Base 包）
        agent/
            agent.conf
            dist/maa_agent.exe       ← 本可执行文件（AgentRT 包）
            src/                     ← 一方源码（Agent 包，每次构建覆盖）
"""
from __future__ import annotations

import importlib
import os
import platform
import sys


def get_executable_dir() -> str:
    """获取执行文件所在目录（与 main.get_executable_dir 一致）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_project_root() -> str:
    """agent/dist → agent → 项目根（与 main.get_project_root 一致）"""
    current_dir = get_executable_dir()
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.dirname(current_dir))
    return os.path.dirname(current_dir)


def detect_arch() -> str:
    """检测当前系统架构并返回对应的目录名（与 main.detect_arch 一致）"""
    machine = platform.machine().lower()
    if "arm" in machine or "aarch64" in machine:
        return "win-arm64"
    return "win-x64"


def setup_dll_path() -> str:
    """设置 DLL 路径与环境变量（与 main.setup_dll_path 一致，精简打印）。"""
    project_root = get_project_root()
    target_arch = detect_arch()
    possible_paths = [
        os.path.join(project_root, "runtimes", target_arch, "native"),
        project_root,
    ]
    for search_path in possible_paths:
        if os.path.exists(os.path.join(search_path, "MaaFramework.dll")):
            os.environ["MAAFW_BINARY_PATH"] = search_path
            os.environ["MAA_LIBRARY_PATH"] = search_path
            os.environ["PATH"] = search_path + os.pathsep + os.environ.get("PATH", "")
            return search_path
    print(f"[bootstrap] 找不到 MaaFramework.dll，搜索路径: {possible_paths}")
    sys.exit(1)


def get_agent_src_dir() -> str:
    return os.path.join(get_project_root(), "agent", "src")


def main() -> int:
    setup_dll_path()

    src_dir = get_agent_src_dir()
    if not os.path.isdir(src_dir):
        print(f"[bootstrap] 一方源码目录不存在: {src_dir}")
        print("[bootstrap] 请部署 Agent 包（agent/src + resource + interface.json）")
        return 1
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    exe_dir = get_executable_dir()
    project_root = get_project_root()

    # 注意：模块名必须字符串拼接——PyInstaller 会静态解析
    # importlib.import_module 的字面量参数，字面量会把一方代码打进 PYZ。
    if "--mcp" in sys.argv:
        # MCP 伴生进程：只 import maa（完整框架模式），不 import maa.agent
        mod = importlib.import_module("maa_mcp" + ".mcp_main")
        return int(mod.mcp_coprocess_main(project_root, exe_dir))

    # agent 主体（AgentServer 模式）
    mod = importlib.import_module("m" + "ain")
    mod.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
