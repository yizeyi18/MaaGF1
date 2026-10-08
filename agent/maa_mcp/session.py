"""Session —— 一个逻辑 MAA 实例（独立 Tasker + 任务状态）。

- 同一 session 内执行 基元操作 / 脚本 / 成套任务
- 全局仅一个 session 可持有"活跃锁"（互动操作：点击/滑动/含动作的任务）
- 任务结束（完成/失败/停止）由 watcher 线程释放活跃锁并记录结果
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class _TaskState:
    entry: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    success: Optional[bool] = None
    error: str = ""
    job: Any = field(default=None, repr=False)
    watcher: Optional[threading.Thread] = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.success is None

    @property
    def duration_ms(self) -> int:
        end = self.finished_at or time.time()
        return int((end - self.started_at) * 1000)


#: reco_capture 属性：绑定不支持事件捕获时的占位（与 None=未初始化区分）
_RECO_DISABLED = object()


class Session:
    def __init__(self, hub: "object", sid: str, name: str = ""):
        self.hub = hub
        self.id = sid
        self.name = name or sid
        self.created_at = time.time()
        self.tasker: "object" = None  # 由 hub.create_session 绑定
        self._task: Optional[_TaskState] = None
        self._task_lock = threading.Lock()
        self._reco_capture: "object" = None
        self._reco_lock = threading.Lock()
        self.reco_last_lookup: "object" = None  # 诊断：最近一次事件兜底查询轨迹

    @property
    def reco_capture(self):
        """post_recognition 结果的事件捕获（每 tasker 只注册一次 sink）。

        MAA v5.8.1 的 post_recognition 结果不入 runtime cache，
        MaaTaskerGetRecognitionDetail 必然失败——识别详情改从
        Node.RecognitionNode 事件拿（见 sm/reco_capture.py）。
        """
        if self.tasker is None:
            return None
        cap = self._reco_capture
        if cap is None:
            with self._reco_lock:
                cap = self._reco_capture
                if cap is None:
                    try:
                        from sm.reco_capture import RecoEventCapture
                        cap = RecoEventCapture()
                    except Exception:
                        # 绑定无 maa.event_sink 等 → 兜底不可用
                        # （缓存失败避免每次重复 import；官方 API 仍第一选择）
                        self._reco_capture = _RECO_DISABLED
                        return None
                    # 必须挂 context sink（MaaTaskerAddContextSink）：
                    # Node.* 识别事件走 context_notifier_，add_sink 挂的
                    # 是 tasker 级通知器，只收 Tasker.Task.*（线上实证：
                    # tasker sink 的 recent_msgs 里全是 Tasker.Task.*，
                    # 一条 Node.Recognition.* 都没有）
                    try:
                        self.tasker.add_context_sink(cap)  # _sink_holder 持引用防 GC
                    except Exception:
                        pass  # tasker 不支持 context sink → 兜底不可用
                    self._reco_capture = cap
        return None if cap is _RECO_DISABLED else cap

    # ---------------- 任务 ----------------

    def start_task(self, entry: str) -> _TaskState:
        """提交任务（调用方需已持有活跃锁）。立即返回任务状态。"""
        with self._task_lock:
            if self._task is not None and self._task.running:
                raise RuntimeError(f"session {self.id} 已有任务在运行: {self._task.entry}")
            st = _TaskState(entry=entry, started_at=time.time())
            job = self.tasker.post_task(entry)
            st.job = job
            self._task = st
            st.watcher = threading.Thread(
                target=self._watch_task, args=(st,), daemon=True,
                name=f"mcp-task-{self.id}",
            )
            st.watcher.start()
            return st

    def _watch_task(self, st: _TaskState) -> None:
        try:
            st.job.wait()
            st.success = bool(st.job.succeeded)
            if not st.success:
                st.error = self._last_error(st)
        except Exception as e:
            st.success = False
            st.error = f"等待任务异常: {e}"
        finally:
            st.finished_at = time.time()
            self.hub.release_active(self.id)

    def _last_error(self, st: _TaskState) -> str:
        try:
            detail = self.tasker.get_task_detail(st.job.job_id)
            if detail is not None:
                parts = [f"status={getattr(detail, 'status', '?')}"]
                try:
                    failed = [
                        f"{getattr(n, 'name', i)}({'ok' if getattr(n, 'completed', False) else 'FAIL'})"
                        for i, n in enumerate(list(detail.nodes)[-5:])
                    ]
                    if failed:
                        parts.append("last_nodes=" + ",".join(failed))
                except Exception:
                    pass
                return " ".join(parts)[:2000]
        except Exception:
            pass
        return ""

    def wait_task(self, timeout_ms: int = 0) -> _TaskState:
        st = self._task
        if st is None:
            raise RuntimeError(f"session {self.id} 没有任务")
        deadline = time.time() + (timeout_ms / 1000.0 if timeout_ms else float("inf"))
        while st.running:
            if time.time() > deadline:
                raise TimeoutError(
                    f"任务 {st.entry} 在 {timeout_ms}ms 内未完成（仍在运行，可用 stop_task 停止）"
                )
            time.sleep(0.05)
        return st

    def stop_task(self) -> bool:
        with self._task_lock:
            st = self._task
        if st is None or not st.running:
            return False
        try:
            self.tasker.post_stop().wait()
            return True
        except Exception as e:
            raise RuntimeError(f"停止任务失败: {e}") from e

    def task_status(self) -> Dict[str, Any]:
        st = self._task
        if st is None:
            return {"running": False}
        return {
            "running": st.running,
            "entry": st.entry,
            "success": st.success,
            "error": st.error if st.error else None,
            "duration_ms": st.duration_ms,
            "started_at": st.started_at,
        }

    # ---------------- 信息 ----------------

    def info(self, active: bool) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "active": active,
            "task": self.task_status(),
        }
