"""MCP 工具实现（纯逻辑层，依赖 GameHub；平台无关，可用 FakeHub 单测）。

每个函数对应一个 MCP tool；抛 MaaMcpError 子类 → server 转结构化错误。
返回 dict（JSON）或 (dict, png_bytes)（截图类）。
"""
from __future__ import annotations

import base64
import copy
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .hub import ActiveLockError, InfraError, MaaMcpError, SessionNotFoundError
from utils.png import encode_png  # noqa: F401  (re-export for server)

# ======================================================================================
# 信息 / session
# ======================================================================================

def tool_get_info(hub: "object") -> Dict[str, Any]:
    import maa

    return {
        "framework": getattr(maa, "__version__", "unknown"),
        "resource_dir": hub.resource_dir,
        "resource_node_count": _node_count(hub),
        "window": hub.window_info(),
        "sessions": hub.list_sessions(),
        "active_session": _active_id(hub),
        "stop_sm": hub.stop_sm.is_set(),
    }


def _node_count(hub: "object") -> Optional[int]:
    try:
        return len(hub.resource.get_node_list())
    except Exception:
        return None


def _active_id(hub: "object") -> Optional[str]:
    with hub._lock:
        return hub._active_session


def tool_list_sessions(hub: "object") -> Dict[str, Any]:
    # 包一层 dict：MCP SDK 会把裸 list 拆成多个内容块，dict 才是单个 JSON 文本块
    return {"sessions": hub.list_sessions()}


def tool_create_session(hub: "object", name: str = "") -> Dict[str, Any]:
    s = hub.create_session(name)
    return s.info(False)


def tool_kill_session(hub: "object", session_id: str) -> Dict[str, Any]:
    return hub.kill_session(session_id)


def tool_list_windows(hub: "object", title_contains: str = "") -> Dict[str, Any]:
    """诊断：列出所有顶层窗口（标题/类名/句柄/可见性）。"""
    if os.name != "nt":
        raise MaaMcpError("仅 Windows 可用")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    out: List[Dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, lparam):
        try:
            n = user32.GetWindowTextW(hwnd, None, 0)
            buf = ctypes.create_unicode_buffer(max(n, 0) + 1)
            user32.GetWindowTextW(hwnd, buf, max(n, 0) + 1)
            title = buf.value
            m = user32.GetClassNameW(hwnd, None, 0)
            cbuf = ctypes.create_unicode_buffer(max(m, 0) + 1)
            user32.GetClassNameW(hwnd, cbuf, max(m, 0) + 1)
            if title:  # 只列有标题的窗口
                if title_contains and title_contains.lower() not in title.lower():
                    return True
                out.append({
                    "hwnd": int(hwnd),
                    "title": title,
                    "class": cbuf.value,
                    "visible": bool(user32.IsWindowVisible(hwnd)),
                })
        except Exception:
            pass
        return True

    user32.EnumWindows(_cb, 0)
    return {"count": len(out), "windows": out}


# ======================================================================================
# 基元：只读
# ======================================================================================

def _resolve_session(hub: "object", session_id: str) -> "object":
    if not session_id:
        return _default_session(hub)
    return hub.get_session(session_id)


def _default_session(hub: "object") -> "object":
    """未指定 session 时用一个常驻的 default session（懒创建）。"""
    if "default" in hub.sessions:
        return hub.sessions["default"]
    with hub._lock:
        if "default" not in hub.sessions:
            from .session import Session

            hub._ensure_infra()
            s = Session(hub, "default", "default")
            t = hub._new_tasker()
            if not t.bind(hub._resource, hub._controller):
                raise InfraError("Tasker.bind 失败（default session）")
            s.tasker = t
            hub.sessions["default"] = s
            return s
    return hub.sessions["default"]


def tool_screenshot(hub: "object", session_id: str = "", roi: Optional[List[int]] = None) -> Tuple[Dict[str, Any], bytes]:
    """截图。返回 (meta, png_bytes)。只读，不需要活跃锁。"""
    _resolve_session(hub, session_id)  # 保证基础设施就绪（顺带校验 session）
    img = hub.screencap()
    import numpy as np

    a = np.asarray(img)
    if a.ndim == 3 and a.shape[2] == 4:
        a = a[:, :, :3]
    full_h, full_w = a.shape[:2]
    if roi:
        x, y, w, h = (int(v) for v in roi)
        x = max(0, min(x, full_w))
        y = max(0, min(y, full_h))
        w = max(1, min(w, full_w - x))
        h = max(1, min(h, full_h - y))
        a = a[y:y + h, x:x + w]
    png = encode_png(a, bgr=True)
    meta = {
        "width": int(a.shape[1]),
        "height": int(a.shape[0]),
        "full_size": [full_w, full_h],
        "roi": [int(v) for v in roi] if roi else None,
    }
    return meta, png


def _recognize_on(hub: "object", s: "object", node: str, image: "object") -> "object":
    """按节点名识别（节点必须已加载在 hub.resource 里）。"""
    node_data = hub.resource.get_node_data(node)
    if node_data is None:
        raise MaaMcpError(f"识别节点不存在: {node}")
    from .bridge import node_to_reco

    rtype, param = node_to_reco(node_data)
    job = s.tasker.post_recognition(rtype, param, image)
    job.wait()
    return s.tasker.get_recognition_detail(job.job_id)


def tool_ocr(hub: "object", session_id: str = "", roi: Optional[List[int]] = None,
             expected: Optional[List[str]] = None, node: str = "") -> Dict[str, Any]:
    """OCR。node 非空时用指定节点（含其 roi/expected）；否则用临时参数（全图或给定 roi）。"""
    s = _resolve_session(hub, session_id)
    img = hub.screencap()
    if node:
        detail = _recognize_on(hub, s, node, img)
        rtype = "OCR"
    else:
        from maa.pipeline import JOCR

        param = JOCR(expected=expected or [], roi=tuple(roi) if roi else (0, 0, 0, 0))
        job = s.tasker.post_recognition("OCR", param, img)
        job.wait()
        detail = s.tasker.get_recognition_detail(job.job_id)
        rtype = "OCR"
    return _detail_to_dict(detail)


def tool_match(hub: "object", session_id: str = "", node: str = "",
               template: str = "", roi: Optional[List[int]] = None,
               threshold: float = 0.8) -> Dict[str, Any]:
    """模板匹配。node 用已加载节点；template 用 resource 目录下相对路径（可先 update_resources 推送）。"""
    s = _resolve_session(hub, session_id)
    img = hub.screencap()
    if node:
        detail = _recognize_on(hub, s, node, img)
    elif template:
        import os

        from maa.pipeline import JTemplateMatch

        tpath = os.path.join(hub.resource_dir, "image", template.lstrip("/"))
        if not os.path.isfile(tpath):
            raise MaaMcpError(f"模板文件不存在: {tpath}（路径相对 resource/image/）")
        # 相对 resource 根的路径才是模板名
        rel = os.path.relpath(tpath, os.path.join(hub.resource_dir, "image"))
        param = JTemplateMatch(
            template=[rel],
            roi=tuple(roi) if roi else (0, 0, 0, 0),
            threshold=[float(threshold)],
        )
        job = s.tasker.post_recognition("TemplateMatch", param, img)
        job.wait()
        detail = s.tasker.get_recognition_detail(job.job_id)
    else:
        raise MaaMcpError("必须指定 node 或 template")
    return _detail_to_dict(detail)


def _detail_to_dict(detail: Optional["object"]) -> Dict[str, Any]:
    if detail is None:
        return {"hit": False, "box": None, "error": "识别未返回结果"}
    d: Dict[str, Any] = {
        "hit": bool(detail.hit),
        "algorithm": str(getattr(detail, "algorithm", "")),
        "box": _box_to_list(detail.box),
    }
    # OCR 文本等附加信息
    for r in list(getattr(detail, "filtered_results", None) or [])[:50]:
        t = getattr(r, "text", None)
        if t is not None:
            d.setdefault("texts", []).append(t)
    best = getattr(detail, "best_result", None)
    if best is not None:
        d["best"] = {
            "box": _box_to_list(getattr(best, "box", None)),
            "score": getattr(best, "score", None),
            "text": getattr(best, "text", None),
            "label": getattr(best, "label", None),
        }
    raw = getattr(detail, "raw_detail", None) or {}
    d["raw_detail"] = raw
    return d


def _box_to_list(box: Optional["object"]) -> Optional[List[int]]:
    if box is None:
        return None
    return [int(box.x), int(box.y), int(box.w), int(box.h)]


# ======================================================================================
# 基元：互动（需活跃锁）
# ======================================================================================

def tool_click(hub: "object", session_id: str, x: int, y: int, contact: int = 0) -> Dict[str, Any]:
    s = _resolve_session(hub, session_id)
    with hub.with_active(s):
        hub.controller.post_click(int(x), int(y), contact=int(contact)).wait()
    return {"clicked": [int(x), int(y)], "contact": int(contact), "session": s.id}


def tool_long_press(hub: "object", session_id: str, x: int, y: int, ms: int = 500) -> Dict[str, Any]:
    s = _resolve_session(hub, session_id)
    with hub.with_active(s):
        c = hub.controller
        c.post_touch_down(int(x), int(y), 0, 1).wait()
        time.sleep(max(int(ms), 10) / 1000.0)
        c.post_touch_up(0).wait()
    return {"long_press": [int(x), int(y)], "ms": int(ms), "session": s.id}


def tool_swipe(hub: "object", session_id: str, x1: int, y1: int, x2: int, y2: int,
               ms: int = 300) -> Dict[str, Any]:
    s = _resolve_session(hub, session_id)
    with hub.with_active(s):
        hub.controller.post_swipe(int(x1), int(y1), int(x2), int(y2), max(int(ms), 100)).wait()
    return {"swipe": [[int(x1), int(y1)], [int(x2), int(y2)]], "ms": int(ms), "session": s.id}


def tool_wait(hub: "object", session_id: str, ms: int) -> Dict[str, Any]:
    """纯等待（不占活跃锁）。"""
    _resolve_session(hub, session_id)
    time.sleep(max(int(ms), 0) / 1000.0)
    return {"waited_ms": int(ms)}


# ======================================================================================
# JSON 脚本解释器（MAA pipeline 片段 → 框架原生执行）
# ======================================================================================

def _namespace_script(session_id: str, script: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """给脚本节点加 session 前缀（避免与已加载节点冲突），返回 (entry, nodes)。

    script 形式：
      {"entry": "节点名", "nodes": {节点名: 节点定义, ...}}
      或裸节点 dict {节点名: 节点定义, ...}（必须配 entry 参数）
    节点定义 = 标准 MAA pipeline 节点（recognition/action/next/...）。
    """
    if not isinstance(script, dict) or not script:
        raise MaaMcpError("script 必须是非空 JSON 对象")
    if "nodes" in script:
        entry = str(script.get("entry", ""))
        nodes = script.get("nodes") or {}
    else:
        entry = ""
        nodes = script
    if not isinstance(nodes, dict) or not nodes:
        raise MaaMcpError("script.nodes 必须是非空对象")
    if not entry:
        raise MaaMcpError("script 缺少 entry（入口节点名；可用 '!' 前缀命名）")

    prefix = f"__mcp_{session_id}__"
    names = set(nodes.keys())
    new_nodes: Dict[str, Any] = {}
    for name, node in nodes.items():
        if not isinstance(node, dict):
            raise MaaMcpError(f"节点 {name} 定义必须是对象")
        new_nodes[prefix + str(name)] = _rewrite_node(copy.deepcopy(node), names, prefix)
    if entry in names:
        return prefix + entry, new_nodes
    return entry, new_nodes  # 引用已加载的既有节点


def _rewrite_node(node: Dict[str, Any], names: set, prefix: str) -> Dict[str, Any]:
    nxt = node.get("next")
    if isinstance(nxt, list):
        node["next"] = [(prefix + n) if (isinstance(n, str) and n in names) else n
                        for n in nxt]
    # And/Or 子识别里的节点名引用
    reco = node.get("recognition")
    if isinstance(reco, dict):
        param = reco.get("param")
        if isinstance(param, dict):
            for key in ("all_of", "any_of"):
                subs = param.get(key)
                if isinstance(subs, list):
                    param[key] = [(prefix + s) if (isinstance(s, str) and s in names) else s
                                  for s in subs]
    return node


def _task_result(st: "object") -> Dict[str, Any]:
    return {
        "success": bool(st.success),
        "entry": st.entry,
        "duration_ms": st.duration_ms,
        "error": st.error or None,
        "running": st.running,
    }


def tool_run_script(hub: "object", session_id: str, script: Dict[str, Any],
                    wait: bool = True, timeout_ms: int = 300000) -> Dict[str, Any]:
    """执行 JSON 编排的基元操作列（MAA pipeline 片段）。

    - 节点按 session 命名空间注入（可重复执行同名脚本）
    - 框架原生执行：识别/动作/重试/超时/next 全部由 MaaFramework 解释
    - 含动作的脚本持有活跃锁直至结束
    """
    s = _resolve_session(hub, session_id)
    entry, nodes = _namespace_script(s.id, script)
    if nodes and not hub.resource.override_pipeline(nodes):
        raise MaaMcpError(f"pipeline 注入失败: {entry}")
    with hub.with_active(s):
        st = s.start_task(entry)
        if wait:
            st = s.wait_task(int(timeout_ms))
    return _task_result(st)


# ======================================================================================
# 内置成套任务
# ======================================================================================

def tool_list_tasks(hub: "object") -> Dict[str, Any]:
    tasks = hub.interface.get("task") or []
    out = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        out.append({
            "name": t.get("name", ""),
            "entry": t.get("entry", ""),
            "doc": t.get("doc", ""),
            "repeatable": t.get("repeatable", False),
            "check": t.get("check", False),
        })
    return {"tasks": out}


def tool_run_task(hub: "object", session_id: str, task: str,
                  wait: bool = True, timeout_ms: int = 600000) -> Dict[str, Any]:
    """执行内置成套任务。task = interface.json 里的任务名或 '!' 入口。"""
    s = _resolve_session(hub, session_id)
    entry = task
    for t in hub.interface.get("task") or []:
        if isinstance(t, dict) and t.get("name") == task:
            entry = t.get("entry", task)
            break
    with hub.with_active(s):
        st = s.start_task(entry)
        if wait:
            st = s.wait_task(int(timeout_ms))
    return _task_result(st)


def tool_stop_task(hub: "object", session_id: str) -> Dict[str, Any]:
    s = _resolve_session(hub, session_id)
    ok = s.stop_task()
    return {"stopped": ok, "session": s.id}


def tool_task_status(hub: "object", session_id: str = "") -> Dict[str, Any]:
    s = _resolve_session(hub, session_id)
    return {"session": s.id, **s.task_status()}


# ======================================================================================
# 资源热更新
# ======================================================================================

def tool_update_resources(hub: "object", files: Dict[str, str], reload: bool = True) -> Dict[str, Any]:
    """推送文件到 resource 目录并重新加载。files: {相对路径: base64}。

    相对路径相对 resource 根（如 image/combat/foo.png、pipeline/xxx.json）。
    有任务在跑时拒绝。
    """
    if not isinstance(files, dict) or not files:
        raise MaaMcpError("files 必须是非空 {路径: base64}")
    return hub.update_resources(files, reload=bool(reload))


def tool_get_resource_info(hub: "object") -> Dict[str, Any]:
    return {
        "resource_dir": hub.resource_dir,
        "node_count": _node_count(hub),
        "interface_tasks": len(hub.interface.get("task") or []),
    }


# ======================================================================================
# 状态机（SM core 复用）
# ======================================================================================

def _sm_flow():
    from sm.flows_81n import build_flow_81n

    return build_flow_81n(rounds=1)


def tool_check_state(hub: "object", session_id: str, state_name: str = "") -> Dict[str, Any]:
    """定位当前界面状态（SM 状态表）。state_name 非空时只验证该状态。"""
    from sm.core import Runner

    from .bridge import TaskerBridge

    s = _resolve_session(hub, session_id)
    flow = _sm_flow()
    stop = threading.Event()
    bridge = TaskerBridge(hub, s, stop)
    img = bridge.screenshot()
    runner = Runner(flow, bridge)

    if state_name:
        st = runner._states_by_name.get(state_name)
        if st is None:
            raise MaaMcpError(f"状态不存在: {state_name}")
        results = {c.node: bridge.check(c, img).ok for c in st.checks}
        ok = all(results.values())
        return {"state": st.name, "matched": ok, "checks": results}

    located = runner.locate_state(img)  # 返回状态名或 None
    if located is None:
        return {"state": None, "matched": False,
                "candidates": [st.name for st in flow.states]}
    st = runner._states_by_name[located]
    return {"state": located, "matched": True, "desc": st.desc}


def tool_run_sm_8_1n(hub: "object", session_id: str, rounds: "object" = 1,
                     timeout_ms: int = 3600000) -> Dict[str, Any]:
    """运行 8-1N 循环状态机流（v1 唯一内置 SM 流）。

    rounds: 轮数（正整数）；-1/"inf"/"infinite" = 无限循环（用 stop_sm 停止）。
    timeout_ms: MCP 调用层超时；到点自动请求停止并返回当前进度。
    """
    from sm.core import FlowAbortedError, Runner, SMError

    from .bridge import TaskerBridge

    s = _resolve_session(hub, session_id)
    r = str(rounds).lower() if not isinstance(rounds, int) else rounds
    if isinstance(r, str) and r in ("inf", "infinite", "-1"):
        rounds_arg: Optional[int] = None
    else:
        rounds_arg = max(1, int(r))
    from sm.flows_81n import build_flow_81n

    flow = build_flow_81n(rounds=rounds_arg)
    stop = threading.Event()
    bridge = TaskerBridge(hub, s, stop)
    hub.stop_sm.clear()
    holder: Dict[str, Any] = {"runner": None, "error": None}

    def _work() -> None:
        runner = Runner(flow, bridge)
        holder["runner"] = runner
        try:
            runner.run()
        except SMError as e:
            holder["error"] = e

    with hub.with_active(s):
        t = threading.Thread(target=_work, daemon=True, name="mcp-sm-flow")
        t.start()
        deadline = time.time() + max(int(timeout_ms), 1000) / 1000.0
        while t.is_alive():
            if time.time() > deadline:
                hub.stop_sm.set()
                t.join(timeout=30)
                if t.is_alive():
                    return {"success": False, "rounds_done": _rounds(holder),
                            "error": "timeout：已请求停止，但流未能在 30s 内退出"}
        runner = holder["runner"]
        err = holder["error"]
        if err is None:
            return {"success": True, "rounds_done": _rounds(holder)}
        if isinstance(err, FlowAbortedError):
            return {"success": False, "rounds_done": _rounds(holder),
                    "error": f"停止/中止: {err}"}
        return {"success": False, "rounds_done": _rounds(holder), "error": str(err)[:3000]}


def _rounds(holder: Dict[str, Any]) -> int:
    runner = holder.get("runner")
    return int(getattr(runner, "round_finished", 0)) if runner else 0


def tool_stop_sm(hub: "object", session_id: str = "") -> Dict[str, Any]:
    """请求状态机流在下一个步骤边界停止（协作式）。"""
    _resolve_session(hub, session_id)
    hub.stop_sm.set()
    return {"stop_requested": True}
