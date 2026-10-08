"""GameHub —— MCP 侧的游戏基础设施与 session 管理。

- 窗口发现：框架 Toolkit.find_desktop_windows() + 正则（正则来自 interface.json 的 controller 配置）
- 共享 Controller（Win32）与 Resource（bundle = resource 目录）
- Session 创建/列出/终止；全局"活跃锁"：同一时间只有一个 session
  能执行互动操作（点击/滑动/含动作的任务）——游戏进程同一机器只有一个
- 资源热更新：写文件 + post_bundle 重载（有任务在跑时拒绝）
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np


class MaaMcpError(Exception):
    """MCP 侧业务错误（工具层会转成结构化错误返回）。"""


class SessionNotFoundError(MaaMcpError):
    pass


class ActiveLockError(MaaMcpError):
    def __init__(self, holder: str):
        self.holder = holder
        super().__init__(
            f"session '{holder}' 当前持有活跃锁（互动操作互斥）。"
            f"等其任务结束/停止后再试，或 kill_session 释放。"
        )


class InfraError(MaaMcpError):
    pass


# ======================================================================================
# 窗口发现（仅 Windows；非 Windows 平台导入本模块不触发）
# ======================================================================================

def find_game_window(title_regex: str, class_regex: str = "") -> Optional[Tuple[int, str, str]]:
    """按标题/类名正则查找游戏窗口。返回 (handle, title, class_name)。

    复用框架 Toolkit.find_desktop_windows()（MaaToolkit C++ 实现，
    与 GUI 的窗口发现走同一代码路径），不再自己 ctypes EnumWindows。
    """
    if os.name != "nt":
        return None
    try:
        from maa.toolkit import Toolkit
        windows = Toolkit.find_desktop_windows()
    except Exception:
        return None
    for w in windows:
        title = w.window_name or ""
        if not title:
            continue
        if re.search(title_regex, title, re.IGNORECASE) and (
            not class_regex or re.search(class_regex, w.class_name or "")
        ):
            return (int(w.hwnd), title, w.class_name or "")
    return None


def list_desktop_windows() -> List[Dict[str, Any]]:
    """框架 Toolkit 的顶层窗口列表（诊断用，与 GUI 所见一致）。"""
    if os.name != "nt":
        return []
    try:
        from maa.toolkit import Toolkit
        windows = Toolkit.find_desktop_windows()
    except Exception:
        return []
    return [
        {"hwnd": int(w.hwnd), "title": w.window_name or "", "class": w.class_name or ""}
        for w in windows
    ]


# ======================================================================================
# GameHub
# ======================================================================================

def count_pipeline_nodes(resource_dir: str) -> Optional[int]:
    """统计磁盘上 pipeline JSON 的节点数。

    maafw 5.2.6 的 Resource 绑定没有节点列表 API（get_node_list 不存在），
    直接数 pipeline 文件。
    """
    root = os.path.join(resource_dir, "pipeline")
    if not os.path.isdir(root):
        return None
    n = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            if isinstance(d, dict):
                n += sum(1 for k in d if not k.startswith(("_", "$")))
    return n


class GameHub:
    def __init__(self, project_root: str, cfg: "object", exe_dir: str = ""):
        self.project_root = project_root
        self.exe_dir = exe_dir or project_root  # 冻结 exe 目录（日志/conf 所在）
        self.cfg = cfg
        self.resource_dir = self._find_resource_dir(project_root)
        self.interface = self._load_interface()
        self.controller_cfg = self._load_controller_cfg()

        self._lock = threading.RLock()
        self.sessions: Dict[str, Any] = {}
        self._seq = 0
        self._active_session: Optional[str] = None
        self.stop_sm = threading.Event()  # 状态机流的协作式停止

        self._controller = None
        self._resource = None
        self._infra_error = ""
        self._last_error = ""
        # Tasker 工厂（默认 maa.tasker.Tasker；可注入，便于测试/扩展）
        self.tasker_factory: "object" = None

    # ---------------- 基础设施（懒加载） ----------------

    def _find_resource_dir(self, project_root: str) -> str:
        for cand in (
            os.path.join(project_root, "resource"),
            os.path.join(project_root, "assets", "resource"),
        ):
            if os.path.isdir(cand):
                return cand
        raise InfraError(f"找不到 resource 目录（project_root={project_root}）")

    def _load_interface(self) -> Dict[str, Any]:
        for cand in (
            os.path.join(self.project_root, "interface.json"),
            os.path.join(self.project_root, "assets", "interface.json"),
        ):
            if os.path.isfile(cand):
                with open(cand, "r", encoding="utf-8") as f:
                    return json.load(f)
        return {}

    def _load_controller_cfg(self) -> Dict[str, Any]:
        ctrls = self.interface.get("controller") or []
        if ctrls and isinstance(ctrls[0], dict):
            c = ctrls[0]
            return c.get(c.get("type", "Win32").lower(), c.get("win32", {})) or {}
        return {}

    def _window_regexes(self) -> Tuple[str, str]:
        title = self.cfg.window_title_regex or self.controller_cfg.get("window_regex") or "少女前线"
        cls = self.cfg.window_class_regex or self.controller_cfg.get("class_regex") or ""
        return title, cls

    def _ensure_infra(self) -> None:
        """确保 controller/resource 就绪。

        只有"绑定不可用"这类永久错误才 latch 到 _infra_error；
        窗口未找到/控制器创建失败/资源加载失败都是瞬态的，
        每次调用都重新尝试（游戏可能在 agent 启动之后才打开）。
        _last_error 仅供展示（window_info），不参与 latch。
        """
        with self._lock:
            if self._controller is not None and self._resource is not None:
                return
            if self._infra_error:
                raise InfraError(self._infra_error)
            try:
                from maa.controller import Win32Controller
                from maa.resource import Resource
            except Exception as e:
                self._infra_error = f"框架 Python 绑定不可用（agent 应在 Windows 测试机运行）: {e}"
                raise InfraError(self._infra_error) from e

            if self._controller is None:
                title_re, class_re = self._window_regexes()
                hit = find_game_window(title_re, class_re)
                if hit is None:
                    self._last_error = (
                        f"找不到游戏窗口（title~'{title_re}' class~'{class_re or '.*'}'）。"
                        f"请确认游戏已启动，或修改 maa_mcp.conf 的 window_title_regex。"
                        f"（可用 list_windows 诊断）"
                    )
                    raise InfraError(self._last_error)
                h, title, cls = hit
                try:
                    self._controller = Win32Controller(
                        h,
                        screencap_method=int(self.controller_cfg.get("screencap", 16)),
                        mouse_method=int(self.controller_cfg.get("mouse", 1)),
                        keyboard_method=int(self.controller_cfg.get("keyboard", 1)),
                    )
                    # 连接（link）控制器——GUI 的 LinkStart() 即此 C API
                    # （MaaControllerPostConnection）。不连接则截图缓存为空
                    # （"Failed to get cached image."）。
                    conn = self._controller.post_connection()
                    conn.wait()
                    if not self._controller.connected:  # property，非方法
                        raise RuntimeError(f"controller 连接后 connected=False (hwnd={h})")
                except Exception as e:
                    self._controller = None  # 未连接成功，下次重新发现+重建
                    self._last_error = f"Win32Controller 创建/连接失败: {e}"
                    raise InfraError(self._last_error) from e

            if self._resource is None:
                res = Resource()
                job = res.post_bundle(self.resource_dir)
                job.wait()
                if not job.succeeded:
                    self._last_error = f"资源加载失败: {self.resource_dir}"
                    raise InfraError(self._last_error)
                self._resource = res
                self._last_error = ""

    @property
    def controller(self) -> Any:
        self._ensure_infra()
        return self._controller

    @property
    def resource(self) -> Any:
        self._ensure_infra()
        return self._resource

    def window_info(self) -> Dict[str, Any]:
        title_re, class_re = self._window_regexes()
        hit = find_game_window(title_re, class_re)
        return {
            "title_regex": title_re,
            "class_regex": class_re,
            "found": hit is not None,
            "title": hit[1] if hit else None,
            "class_name": hit[2] if hit else None,
            "controller_ready": self._controller is not None,
            "resource_ready": self._resource is not None,
            "resource_dir": self.resource_dir,
            "infra_error": self._infra_error or self._last_error or None,
        }

    # ---------------- session 管理 ----------------

    def _new_tasker(self) -> Any:
        if self.tasker_factory is not None:
            return self.tasker_factory()
        from maa.tasker import Tasker

        return Tasker()

    def create_session(self, name: str = "") -> Any:
        from .session import Session

        self._ensure_infra()
        with self._lock:
            self._seq += 1
            sid = f"s{self._seq}"
            s = Session(self, sid, name)
            t = self._new_tasker()
            if not t.bind(self._resource, self._controller):
                raise InfraError(f"Tasker.bind 失败（session {sid}）")
            s.tasker = t
            # 立即注册识别事件捕获——识别事件只发一次，若懒注册到
            # 第一次识别之后，该识别的事件就错过了（线上实证：首个
            # 走兜底的 match 必 MISS +2s，check_state 首个检查之后全快）
            s.reco_capture
            self.sessions[sid] = s
            return s

    def get_session(self, session_id: str) -> Any:
        s = self.sessions.get(session_id)
        if s is None:
            raise SessionNotFoundError(
                f"session '{session_id}' 不存在（现有: {list(self.sessions) or '无'}）"
            )
        return s

    def list_sessions(self) -> List[Dict[str, Any]]:
        return [s.info(self._active_session == s.id) for s in self.sessions.values()]

    def kill_session(self, session_id: str) -> Dict[str, Any]:
        s = self.get_session(session_id)
        stopped = False
        if s.task_status().get("running"):
            s.stop_task()
            stopped = True
            # 等 watcher 收尾（含释放活跃锁），最多 5s
            for _ in range(100):
                if not s.task_status().get("running"):
                    break
                time.sleep(0.05)
        with self._lock:
            if self._active_session == session_id:
                self._active_session = None
            self.sessions.pop(session_id, None)
        return {"killed": session_id, "task_stopped": stopped}

    # ---------------- 活跃锁 ----------------

    @contextlib.contextmanager
    def with_active(self, session: Any) -> Iterator[None]:
        """互动操作互斥锁。session 重复获取允许（重入）。"""
        with self._lock:
            if self._active_session is None or self._active_session == session.id:
                self._active_session = session.id
            else:
                raise ActiveLockError(self._active_session)
        try:
            yield
        finally:
            with self._lock:
                if self._active_session == session.id:
                    self._active_session = None

    def release_active(self, session_id: str) -> None:
        """任务 watcher 结束时调用（仅当仍是持有者）。"""
        with self._lock:
            if self._active_session == session_id:
                self._active_session = None

    def any_task_running(self) -> bool:
        return any(s.task_status().get("running") for s in self.sessions.values())

    # ---------------- 资源热更新 ----------------

    def update_resources(self, files: Dict[str, str], reload: bool = True) -> Dict[str, Any]:
        """写入文件并（可选）重载资源。files: {相对路径: base64}。"""
        import base64

        if self.any_task_running():
            raise MaaMcpError("有任务正在运行，先 stop_task 再更新资源")

        written: List[str] = []
        for rel, b64 in files.items():
            rel = rel.replace("\\", "/").lstrip("/")
            target = os.path.normpath(os.path.join(self.resource_dir, rel))
            if not target.startswith(os.path.normpath(self.resource_dir) + os.sep):
                raise MaaMcpError(f"非法路径（越出 resource 目录）: {rel}")
            data = base64.b64decode(b64)
            os.makedirs(os.path.dirname(target) or self.resource_dir, exist_ok=True)
            tmp = target + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
            written.append(rel)

        reload_ok, nodes = None, None
        if reload and written:
            job = self.resource.post_bundle(self.resource_dir)
            job.wait()
            reload_ok = bool(job.succeeded)
            nodes = count_pipeline_nodes(self.resource_dir)
        return {"written": written, "reloaded": reload_ok, "node_count": nodes}

    # ---------------- 截图 ----------------

    def screencap(self) -> "object":
        """截图，返回控制器原始输出（Win32 PrintWindow 为 BGRA 4 通道）。

        注意：不要把这张图直接回灌给 Python 绑定的 post_recognition /
        run_recognition——ImageBuffer.set 硬编码 CV_8UC3，4 通道会误读
        （上游 MaaFramework 的既有行为）。回灌前先 to_bgr()；
        一次性识别优先走框架原生路径（post_task 内部截图，见 tools）。
        """
        try:
            job = self.controller.post_screencap()
            job.wait()
            img = np.asarray(job.get())
            if img.size == 0:
                raise InfraError("截图为空（窗口最小化或截图方式不支持？可换 screencap 配置）")
            return img
        except MaaMcpError:
            raise
        except Exception as e:
            raise InfraError(f"截图失败: {e}") from e

    @staticmethod
    def to_bgr(img) -> "object":
        """BGRA(4 通道) → BGR(3 通道)，供 Python 绑定回灌识别（post_recognition
        / run_recognition 的 ImageBuffer.set 只接受 3 通道输入）。
        已是 3 通道的原样返回（保证 contiguous）。"""
        img = np.asarray(img)
        if img.ndim == 3 and img.shape[2] == 4:
            return np.ascontiguousarray(img[:, :, :3])
        return np.ascontiguousarray(img)
