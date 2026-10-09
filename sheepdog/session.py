"""Session 状态机（设计稿 7.4）。

状态只能通过 transition() 改变；非法迁移直接抛错，避免 Dispatcher 写出不一致状态。
"""

from __future__ import annotations

PROPOSED = "proposed"
ACTIVE = "active"
RUNNING = "running"
WAITING_HUMAN = "waiting_human"
BLOCKED = "blocked"
HUMAN_ATTACHED = "human_attached"
IDLE = "idle"
FAILED = "failed"
ATTENTION = "attention"
CLOSED = "closed"
MERGED = "merged"

# 事件 → {源状态: 目标状态}
TRANSITIONS: dict[str, dict[str, str]] = {
    "confirm": {PROPOSED: ACTIVE},
    "merge": {PROPOSED: MERGED, ACTIVE: MERGED, IDLE: MERGED},
    "dispatch": {
        ACTIVE: RUNNING,
        IDLE: RUNNING,
        WAITING_HUMAN: RUNNING,
        BLOCKED: RUNNING,
        FAILED: RUNNING,
    },
    "receipt_handled": {RUNNING: ACTIVE},
    "receipt_needs_decision": {RUNNING: WAITING_HUMAN},
    "receipt_waiting_external": {RUNNING: BLOCKED},
    "receipt_done": {RUNNING: CLOSED},
    # adopted 会话回执可选：超时不算失败、不重投，直接回到可投递
    "receipt_missed": {RUNNING: ACTIVE},
    "error": {RUNNING: FAILED},
    "retries_exhausted": {FAILED: ATTENTION},
    "human_attach": {
        ACTIVE: HUMAN_ATTACHED,
        IDLE: HUMAN_ATTACHED,
        WAITING_HUMAN: HUMAN_ATTACHED,
        BLOCKED: HUMAN_ATTACHED,
        RUNNING: HUMAN_ATTACHED,
    },
    "human_detach": {HUMAN_ATTACHED: ACTIVE},
    "go_idle": {ACTIVE: IDLE},
    "close": {ACTIVE: CLOSED, IDLE: CLOSED, WAITING_HUMAN: CLOSED, BLOCKED: CLOSED, ATTENTION: CLOSED},
    "reset": {ATTENTION: ACTIVE, FAILED: ACTIVE},
}

# 可以直接投递的状态
DISPATCHABLE = {ACTIVE, IDLE, WAITING_HUMAN, BLOCKED}
# 只排队、不投递的状态
QUEUE_ONLY = {RUNNING, HUMAN_ATTACHED, PROPOSED}
# 不再接收投递的状态
TERMINAL = {CLOSED, MERGED}

RECEIPT_EVENTS = {
    "handled": "receipt_handled",
    "needs_decision": "receipt_needs_decision",
    "waiting_external": "receipt_waiting_external",
    "done": "receipt_done",
}


class InvalidTransition(ValueError):
    pass


def transition(state: str, event: str) -> str:
    table = TRANSITIONS.get(event)
    if table is None:
        raise InvalidTransition(f"未知事件: {event}")
    if state not in table:
        raise InvalidTransition(f"状态 {state} 不允许事件 {event}")
    return table[state]


def can(state: str, event: str) -> bool:
    return state in TRANSITIONS.get(event, {})
