"""会话发起的 agentapi 动作交给常驻进程执行（7.12）。

实测：会话终端里直接调 agentapi 给别的项目的会话发消息会被拒（project_id 不匹配），只有 sidecar 常驻进程能跨项目投递。
所以 spawn / new-session / push-rules / init / retire 不在常驻进程里执行时只入队，由 `sheepdog run` 每轮开始时执行。

「我是不是常驻进程」：`sheepdog run` 启动时在本进程内打一个标记，其他进程一律视为不是。
（App 给 sidecar 注入的环境变量没有可靠、可核实的 sidecar 标识；会话终端里同样有 agentapi 的环境变量，分不出来。）
sidecar 的启动命令是 `sheepdog init && exec sheepdog run`：其中的 init 也会入队，紧接着由 run 的第一轮执行，效果相同。
"""

from __future__ import annotations

import json
import logging
import os

from .store import Store

log = logging.getLogger("sheepdog")

KINDS = ("init", "spawn", "new_session", "push_rules", "retire")
QUEUED_HINT = "已提交，sheepdog 下一轮执行，结果见 `sheepdog actions`"

_resident = False


def mark_resident() -> None:
    """只由 `sheepdog run` 调用：本进程是常驻进程，可以直接调 agentapi。"""
    global _resident
    _resident = True


def is_resident() -> bool:
    return _resident


def requested_by() -> str:
    return f"pid={os.getpid()} cwd={os.getcwd()}"


def enqueue(store: Store, kind: str, args: dict) -> int:
    if kind not in KINDS:
        raise ValueError(f"未知动作 {kind}")
    return store.add_action(kind, args, requested_by())


def execute(dispatcher, kind: str, args: dict) -> object:
    """在常驻进程里执行一条动作，返回结果（写进 actions.result）。失败抛异常。"""
    d = dispatcher
    if kind == "init":
        return d.init()
    d.sync_roster()
    if kind == "spawn":
        return d.spawn(args["key"])
    if kind == "new_session":
        return d.new_session(args["key"], args["title"], args["duty"], [tuple(c) for c in args.get("chats", [])],
                             args.get("message_ids", []), args.get("note", ""))
    if kind == "push_rules":
        return d.push_rules(args.get("topic") or None)
    if kind == "retire":
        return d.retire(args["target"], args.get("successor_title", ""))
    raise ValueError(f"未知动作 {kind}")


def run_pending(dispatcher) -> int:
    """按 id 顺序执行排队的动作；单条失败记 error，不阻塞后续，也不重试。返回执行条数。"""
    n = 0
    for a in dispatcher.store.pending_actions():
        n += 1
        try:
            result = execute(dispatcher, a["kind"], json.loads(a["args_json"] or "{}"))
            dispatcher.store.finish_action(a["id"], result=json.dumps(result, ensure_ascii=False, default=str))
            log.info("动作 #%s %s 完成", a["id"], a["kind"])
        except Exception as e:  # 单条失败不影响后续
            dispatcher.store.finish_action(a["id"], error=f"{type(e).__name__}: {e}"[:1000])
            log.warning("动作 #%s %s 失败: %s", a["id"], a["kind"], e)
    return n
