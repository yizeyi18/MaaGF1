"""状态机自定义动作 —— 把状态机流挂到 MaaFramework pipeline 上执行。

用法（pipeline 节点）：
    {
        "recognition": "DirectHit",
        "action": "Custom",
        "custom_action": "sm_run_8_1n",
        "custom_action_param": { "rounds": 1 }
    }

rounds: 循环轮数（int）或 "inf"（无限，对应原"是否开启无限循环"）。
动作是阻塞的：流跑完才返回；期间每个步骤边界检查停止请求。
"""
from __future__ import annotations

import json
import os
import threading

from maa.custom_action import CustomAction
from maa.event_sink import NotificationType
from maa.tasker import TaskerEventSink

from .core import FlowAbortedError, Runner, SMError
from .flows_81n import build_flow_81n
from .maa_bridge import MaaBridge

SM_ACTION_NAME = "sm_run_8_1n"

# 停止信号：tasker 事件 + 停止文件（测试期兜底）
_stop_event = threading.Event()
STOP_FILE = os.path.join(os.getcwd(), "maa_sm_stop.txt")


class _SmTaskerStopSink(TaskerEventSink):
    """任务结束（成功/失败）时置停止位 —— 流内部在下一个步骤边界感知。"""

    def on_tasker_task(self, tasker, noti_type, detail):
        if noti_type in (NotificationType.Succeeded, NotificationType.Failed):
            _stop_event.set()


def register_sm() -> None:
    """在 agent 启动后调用：注册停止监听与状态机动作。"""
    from maa.agent.agent_server import AgentServer

    AgentServer.add_tasker_sink(_SmTaskerStopSink())
    AgentServer.register_custom_action(SM_ACTION_NAME, _SmRun81NAction())


class _SmRun81NAction(CustomAction):
    def run(self, context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        # 消费上一轮遗留的停止信号
        _stop_event.clear()
        if os.path.exists(STOP_FILE):
            try:
                os.remove(STOP_FILE)
            except OSError:
                pass

        try:
            param = json.loads(argv.custom_action_param or "{}")
        except (TypeError, ValueError):
            param = {}
        rounds = param.get("rounds", 1)
        if isinstance(rounds, str) and rounds.strip().lower() in ("inf", "infinite", "-1"):
            rounds = None
        elif not isinstance(rounds, int) or rounds < 1:
            rounds = 1
        rounds = rounds if rounds is None else int(rounds)

        bridge = MaaBridge(context, _stop_event, stop_file=STOP_FILE)
        flow = build_flow_81n(rounds)
        runner = Runner(flow, bridge)

        try:
            runner.run()
            return CustomAction.RunResult(success=True)
        except FlowAbortedError:
            return CustomAction.RunResult(success=False)
        except SMError as e:
            bridge.log("ERROR", f"状态机流失败: {e}")
            return CustomAction.RunResult(success=False)
        except Exception as e:  # 框架/连接异常等
            bridge.log("ERROR", f"状态机流异常: {e!r}")
            return CustomAction.RunResult(success=False)
