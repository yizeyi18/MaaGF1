"""MCP HTTP 服务器（streamable-HTTP，Bearer API Key 鉴权）。

在 agent 进程内以守护线程运行；启动失败不影响 agent 主流程。
端点：<bind>:<port>/mcp
"""
from __future__ import annotations

import base64
import threading
import traceback
from typing import Any, Dict, List, Optional, Tuple, Union

from .config import MaaMcpConfig, default_conf_path
from .hub import GameHub, MaaMcpError
from . import tools as T

# 模块级单例（start_mcp_server 时赋值）
HUB: Optional[GameHub] = None
_CFG: Optional[MaaMcpConfig] = None
_thread: Optional[threading.Thread] = None
_started = False


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


_IMPL: Dict[str, Any] = {
    "get_info": T.tool_get_info,
    "list_sessions": T.tool_list_sessions,
    "create_session": T.tool_create_session,
    "kill_session": T.tool_kill_session,
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
    app = server.streamable_http_app(stateless_http=stateless)

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


def start_mcp_server(project_root: str, executable_dir: str) -> bool:
    """启动 MCP 服务器（守护线程）。返回是否启动成功。幂等。"""
    global HUB, _CFG, _thread, _started
    if _started:
        return True
    try:
        _CFG = MaaMcpConfig.load(default_conf_path(executable_dir))
    except Exception as e:
        print(f"[maa_mcp] 配置加载失败: {e}")
        traceback.print_exc()
        return False
    if not _CFG.enable:
        print("[maa_mcp] 已禁用（maa_mcp.conf enable=false）")
        return False

    HUB = GameHub(project_root, _CFG)

    try:
        app = _build_app(_CFG.api_key)
    except Exception as e:
        print(f"[maa_mcp] MCP 服务器构建失败（需要 pip install mcp，且仅 Windows 测试机可运行）: {e}")
        traceback.print_exc()
        return False

    import uvicorn

    config = uvicorn.Config(app, host=_CFG.bind, port=_CFG.port, log_level="warning")
    uv_server = uvicorn.Server(config)
    _thread = threading.Thread(target=uv_server.run, daemon=True, name="maa-mcp-http")
    _thread.start()
    _started = True
    print(f"[maa_mcp] MCP server 已启动: {_CFG.url} (bind={_CFG.bind}:{_CFG.port})")
    print(f"[maa_mcp] API Key: {_CFG.api_key}")
    print(f"[maa_mcp] 配置文件: {_CFG._path}")
    return True
