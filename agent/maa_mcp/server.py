"""MCP HTTP 服务器（streamable-HTTP，Bearer API Key 鉴权）。

进程模型（重要）：
- MCP 运行在**伴生进程**（maa_agent.exe --mcp，由 agent 主体自拉起），
  因为 agent 主体是 AgentServer 模式，框架禁止在其中创建
  Tasker/Controller/Resource（MaaAgentServer.dll 全是 NotImpl 桩）。
  伴生进程只 import maa（不 import maa.agent）→ 完整框架模式。
- agent 侧入口：spawn_mcp_coprocess()（清理旧实例 + 自拉起 + 等端口）。
- 伴生进程入口：start_mcp_server()（主线程阻塞运行 uvicorn）。
端点：<bind>:<port>/mcp
"""
from __future__ import annotations

import base64
import os
import signal
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .config import MaaMcpConfig, default_conf_path
from .hub import GameHub, MaaMcpError
from . import tools as T

# 模块级单例（start_mcp_server 时赋值）
HUB: Optional[GameHub] = None
_CFG: Optional[MaaMcpConfig] = None


def call_tool(hub: GameHub, tool: str, **kwargs: Any) -> Tuple[Dict[str, Any], Optional[bytes]]:
    """统一工具调用入口（server 与测试共用）。

    形参名用 tool（不用 name），避免与 create_session(name=...) 等工具参数冲突。
    返回 (meta_dict, png_bytes|None)。错误抛 MaaMcpError。
    """
    fn = _IMPL.get(tool)
    if fn is None:
        raise MaaMcpError(f"未知工具: {tool}（可用: {', '.join(sorted(_IMPL))}）")
    result = fn(hub, **kwargs)
    if isinstance(result, tuple):
        meta, png = result
        return meta, png
    return result, None


def _call(tool: str, **kwargs: Any) -> object:
    """MCP 工具函数统一出口：结构化错误 + 图片内容块。

    注意：形参名用 tool（不用 name），避免与 create_session(name=...)
    等工具参数冲突。
    """
    try:
        meta, png = call_tool(HUB, tool, **kwargs)
    except MaaMcpError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(limit=8)}
    if png is None:
        return meta
    from mcp.types import ImageContent, TextContent

    return [
        TextContent(type="text", text=str(meta)),
        ImageContent(type="image", data=base64.b64encode(png).decode(),
                     mime_type="image/png"),
    ]


# ======================================================================================
# MCP 工具（显式类型签名 → JSON schema）
# ======================================================================================

def register_tools(server: "object") -> None:
    """把工具注册到 MCPServer 实例。"""

    @server.tool()
    def get_info() -> object:
        """系统信息：框架版本、resource 路径/节点数、游戏窗口状态、session 列表。"""
        return _call("get_info")

    @server.tool()
    def list_sessions() -> object:
        """列出所有 session（含活跃锁持有者与任务状态）。"""
        return _call("list_sessions")

    @server.tool()
    def create_session(name: str = "") -> object:
        """创建 session（独立 Tasker；互动操作全局仅一个 session 可执行）。"""
        return _call("create_session", name=name)

    @server.tool()
    def kill_session(session_id: str) -> object:
        """终止 session（先停其运行中的任务）。"""
        return _call("kill_session", session_id=session_id)

    @server.tool()
    def list_windows(title_contains: str = "") -> object:
        """诊断：列出所有顶层窗口（标题/类名/句柄/可见性）。title_contains 过滤子串。"""
        return _call("list_windows", title_contains=title_contains)

    @server.tool()
    def screenshot(session_id: str = "", roi: Optional[List[int]] = None) -> object:
        """截图（返回 PNG 图片 + 尺寸元信息）。只读，任意 session 可用。"""
        return _call("screenshot", session_id=session_id, roi=roi)

    @server.tool()
    def ocr(session_id: str = "", roi: Optional[List[int]] = None,
            expected: Optional[List[str]] = None, node: str = "") -> object:
        """OCR 识别。node=已加载识别节点名（推荐）；否则用全图/roi+expected 临时参数。"""
        return _call("ocr", session_id=session_id, roi=roi, expected=expected, node=node)

    @server.tool()
    def match(session_id: str = "", node: str = "", template: str = "",
              roi: Optional[List[int]] = None, threshold: float = 0.8) -> object:
        """模板匹配。node=已加载节点名；或 template=resource/image 下相对路径（可先 update_resources 推送）。"""
        return _call("match", session_id=session_id, node=node, template=template,
                     roi=roi, threshold=threshold)

    @server.tool()
    def click(session_id: str, x: int, y: int, contact: int = 0) -> object:
        """点击（需活跃锁）。"""
        return _call("click", session_id=session_id, x=x, y=y, contact=contact)

    @server.tool()
    def long_press(session_id: str, x: int, y: int, ms: int = 500) -> object:
        """长按（需活跃锁）。"""
        return _call("long_press", session_id=session_id, x=x, y=y, ms=ms)

    @server.tool()
    def swipe(session_id: str, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> object:
        """滑动（需活跃锁）。"""
        return _call("swipe", session_id=session_id, x1=x1, y1=y1, x2=x2, y2=y2, ms=ms)

    @server.tool()
    def wait(session_id: str, ms: int) -> object:
        """纯等待（不占活跃锁）。"""
        return _call("wait", session_id=session_id, ms=ms)

    @server.tool()
    def run_script(session_id: str, script: Dict[str, Any], wait: bool = True,
                   timeout_ms: int = 300000) -> object:
        """执行 JSON 编排的基元操作列（MAA pipeline 片段，框架原生解释执行）。

        script = {"entry": 入口节点名, "nodes": {节点名: 标准MAA节点定义, ...}}。
        节点按 session 命名空间注入，同名脚本可重复执行。含动作的脚本持有活跃锁直至结束。
        """
        return _call("run_script", session_id=session_id, script=script,
                     wait=wait, timeout_ms=timeout_ms)

    @server.tool()
    def list_tasks() -> object:
        """列出 interface.json 内置任务（名称/入口/文档）。"""
        return _call("list_tasks")

    @server.tool()
    def run_task(session_id: str, task: str, wait: bool = True,
                 timeout_ms: int = 600000) -> object:
        """执行内置成套任务（任务名或 '!' 入口；含动作，占活跃锁）。"""
        return _call("run_task", session_id=session_id, task=task,
                     wait=wait, timeout_ms=timeout_ms)

    @server.tool()
    def stop_task(session_id: str) -> object:
        """停止 session 运行中的任务。"""
        return _call("stop_task", session_id=session_id)

    @server.tool()
    def task_status(session_id: str = "") -> object:
        """查询 session 任务状态。"""
        return _call("task_status", session_id=session_id)

    @server.tool()
    def update_resources(files: Dict[str, str], reload: bool = True) -> object:
        """推送文件到 resource 目录并重新加载（热更新）。files = {相对路径: base64}。有任务在跑时拒绝。"""
        return _call("update_resources", files=files, reload=reload)

    @server.tool()
    def get_resource_info() -> object:
        """resource 信息（路径/节点数/任务数）。"""
        return _call("get_resource_info")

    @server.tool()
    def check_state(session_id: str, state_name: str = "") -> object:
        """定位当前界面状态（SM 状态表）；state_name 非空时只验证该状态。"""
        return _call("check_state", session_id=session_id, state_name=state_name)

    @server.tool()
    def run_sm_8_1n(session_id: str, rounds: Union[int, str] = 1,
                    timeout_ms: int = 3600000) -> object:
        """运行 8-1N 循环状态机流。rounds: 正整数轮数；-1 或 "inf" = 无限循环（stop_sm 停止）。"""
        return _call("run_sm_8_1n", session_id=session_id, rounds=rounds,
                     timeout_ms=timeout_ms)

    @server.tool()
    def stop_sm(session_id: str = "") -> object:
        """请求状态机流在下一个步骤边界停止（协作式）。"""
        return _call("stop_sm", session_id=session_id)

    @server.tool()
    def read_logs(source: str = "all", lines: int = 100, pattern: str = "") -> object:
        """读磁盘日志尾部（诊断任务/识别失败的关键工具）。

        source: all | framework | 具体文件 key（先不传参看 available 列表）。
        pattern: 行过滤（优先正则，如 ERR|__mcp_s1，非法正则按子串）。
        lines: 每来源最多返回行数（≤500，取过滤后尾部）。
        """
        return _call("read_logs", source=source, lines=lines, pattern=pattern)


_IMPL: Dict[str, Any] = {
    "get_info": T.tool_get_info,
    "list_sessions": T.tool_list_sessions,
    "create_session": T.tool_create_session,
    "kill_session": T.tool_kill_session,
    "list_windows": T.tool_list_windows,
    "screenshot": T.tool_screenshot,
    "ocr": T.tool_ocr,
    "match": T.tool_match,
    "click": T.tool_click,
    "long_press": T.tool_long_press,
    "swipe": T.tool_swipe,
    "wait": T.tool_wait,
    "run_script": T.tool_run_script,
    "list_tasks": T.tool_list_tasks,
    "run_task": T.tool_run_task,
    "stop_task": T.tool_stop_task,
    "task_status": T.tool_task_status,
    "update_resources": T.tool_update_resources,
    "get_resource_info": T.tool_get_resource_info,
    "check_state": T.tool_check_state,
    "run_sm_8_1n": T.tool_run_sm_8_1n,
    "stop_sm": T.tool_stop_sm,
    "read_logs": T.tool_read_logs,
}


# ======================================================================================
# HTTP 服务器
# ======================================================================================

def _build_app(api_key: str, stateless: bool = True) -> "object":
    import json

    from mcp.server.mcpserver import MCPServer
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    server = MCPServer(
        name="maagf1",
        version="1.0.0",
        instructions="MaaGF1 游戏远程控制：截图/OCR/点击/滑动/脚本/内置任务/资源热更新/状态机。"
                     "互动操作（click/swipe/run_script/run_task/run_sm_8_1n）需 session 持有活跃锁。",
    )
    register_tools(server)
    # 关闭 SDK 默认的 DNS 重绑定保护（它默认只放行 127.0.0.1/localhost，
    # 内网 IP 访问会被 421 拒绝）。本服务以 Bearer API Key 为鉴权边界。
    security = None
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    except ImportError:
        pass
    app = server.streamable_http_app(stateless_http=stateless, transport_security=security)

    class ApiKeyMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            auth = request.headers.get("authorization", "")
            if auth == f"Bearer {api_key}":
                return await call_next(request)
            # JSON-RPC 错误体（MCP 客户端能读到 401 的具体原因）
            rid = None
            try:
                raw = await request.body()
                if raw:
                    rid = json.loads(raw).get("id")
            except Exception:
                pass
            return JSONResponse(
                {"jsonrpc": "2.0", "id": rid,
                 "error": {"code": -32001, "message": "unauthorized: invalid or missing API key"}},
                status_code=401,
            )

    app.add_middleware(ApiKeyMiddleware)
    return app


def _log(msg: str, cfg_path: str = "") -> None:
    """同时输出到控制台与 maa_mcp.log（GUI 启动的 agent 没有控制台，日志必须落盘）。"""
    import time

    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    if cfg_path:
        try:
            with open(cfg_path + ".log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _wait_port(port: int, timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            continue
    return False


def _kill_stale_coprocess(executable_dir: str) -> None:
    """终止上一实例伴生进程（上次残留 / GUI 重启后旧 agent 拉起的）。"""
    pid_file = Path(executable_dir) / "maa_mcp.pid"
    try:
        if not pid_file.exists():
            return
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def spawn_mcp_coprocess(project_root: str, executable_dir: str) -> bool:
    """agent 侧入口：清理旧实例 → 自拉起 --mcp 伴生进程（同 exe）→ 等端口就绪。"""
    conf_path = default_conf_path(executable_dir)
    try:
        cfg = MaaMcpConfig.load(conf_path)
    except Exception:
        cfg = None
    if cfg is not None and not cfg.enable:
        _log("MCP 已禁用（maa_mcp.conf enable=false），不启动伴生进程", conf_path)
        return False
    port = cfg.port if cfg is not None else 8180

    _kill_stale_coprocess(executable_dir)

    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--mcp", project_root, executable_dir]
    else:  # 开发模式：python agent/main.py --mcp
        main_py = Path(__file__).resolve().parent.parent / "main.py"
        cmd = [sys.executable, str(main_py), "--mcp", project_root, executable_dir]

    kwargs: Dict[str, Any] = {"cwd": executable_dir}
    if os.name == "nt":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except Exception as e:
        _log(f"MCP 伴生进程拉起失败: {e}", conf_path)
        return False

    ok = _wait_port(port, timeout_s=45.0)
    _log(f"MCP 伴生进程已拉起，端口 {'就绪' if ok else '未就绪（详情见 maa_mcp.log）'}: 127.0.0.1:{port}", conf_path)
    return ok


def start_mcp_server(project_root: str, executable_dir: str) -> int:
    """伴生进程主流程（阻塞）：加载配置 → 构建应用 → 主线程运行 uvicorn。

    必须在完整框架模式进程里运行（只 import maa，不 import maa.agent）。
    """
    global HUB, _CFG
    conf_path = default_conf_path(executable_dir)
    try:
        _CFG = MaaMcpConfig.load(conf_path)
    except Exception as e:
        _log(f"配置加载失败: {e}", conf_path)
        with open(conf_path + ".log", "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
        return 1
    if not _CFG.enable:
        _log("已禁用（maa_mcp.conf enable=false）", conf_path)
        return 0

    HUB = GameHub(project_root, _CFG, exe_dir=executable_dir)

    try:
        app = _build_app(_CFG.api_key)
    except Exception as e:
        _log(f"MCP 服务器构建失败: {e}", conf_path)
        with open(conf_path + ".log", "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
        return 1

    _log(f"MCP server 启动中: {_CFG.url} (bind={_CFG.bind}:{_CFG.port})", conf_path)
    _log(f"API Key: {_CFG.api_key}", conf_path)
    _log(f"配置文件: {conf_path}", conf_path)
    _log(f"提示: 首次内网访问请在 Windows 防火墙弹窗中允许，或运行: "
         f"netsh advfirewall firewall add rule name=MaaGF1-MCP dir=in action=allow "
         f"protocol=TCP localport={_CFG.port}", conf_path)

    import uvicorn

    uvicorn.run(app, host=_CFG.bind, port=_CFG.port, log_level="warning")
    return 0
