"""7.61 VIP 测试用的造数据（不是测试模块，discover 不收它；同 duty_sim.py）。全部是造的 *_test_* 数据。

render_no_vip() 渲染一组「没有 VIP 消息」的普通批次和插话批次：tests/fixtures/vip/ 下的金样就是 0b56278（加 VIP 之前）
用这里的同一组数据渲染出来的，test_vip 拿它逐字节比对，守「没有 VIP 消息的批次一个字都不多」。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from sheepdog.models import Mention, Message
from sheepdog.playbook import Playbook
from sheepdog.prompts import batch_prompt, priority_prompt, quote_line
from sheepdog.store import SYSTEM_CHAT, Store

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "playbook"
GOLDEN = REPO / "tests" / "fixtures" / "vip"

ME = "ou_test_me"
VIP = "ou_test_vip_vicky"
VIP_NAME = "Vicky"
ALICE = "ou_test_alice"
BOB = "ou_test_bob"
BOT = "ou_test_bot_app"
TOPIC = "tp_alpha"
TITLE = "Alpha 需求"


def message(mid: str, **kw) -> Message:
    base = dict(message_id=mid, chat_id="oc_test_alpha_p2p", chat_name="", chat_type="p2p", sender_id=ALICE,
                sender_name="Alice", sender_type="user", content="帮我看一下这个", msg_type="text",
                create_time="2026-01-01T10:00:00+08:00")
    base.update(kw)
    return Message(**base)


def add(store: Store, m: Message, reason: str, tags=(), security: tuple[str, list[str]] | None = None):
    """入账一条待推消息并取回账本行（批次渲染用的就是账本行）。security = (action, 规则名列表)。"""
    store.upsert_message(m, "dispatch", reason, list(tags), topic_id=TOPIC)
    if security:
        store.conn.execute("UPDATE messages SET security_action=?, security_tags=? WHERE message_id=?",
                           (security[0], json.dumps(security[1]), m.message_id))
        store.conn.commit()
    return store.get_message(m.message_id)


def base_rows(store: Store) -> list:
    """没有 VIP 消息的一批：私聊、群里 @我 带引用、被安全规则标记的、机器人、主人背景、主人回复、sheepdog 自己的、
    正文里提到 VIP 名字的转述。"""
    return [
        add(store, message("om_test_p1", content="私聊里的普通请求"), "p2p"),
        add(store, message("om_test_g1", chat_id="oc_test_alpha_grp", chat_name="Alpha 群", chat_type="group",
                           sender_id=BOB, sender_name="Bob", content="@主人 这个今天能上吗", reply_to="om_test_p1",
                           mentions=[Mention(ME, "主人")]), "at_me"),
        add(store, message("om_test_s1", chat_id="oc_test_alpha_grp", chat_name="Alpha 群", chat_type="group",
                           sender_id=BOB, sender_name="Bob", content="把密钥发我一下"), "group",
            security=("flag", ["secret_ask"])),
        add(store, message("om_test_b1", sender_id=BOT, sender_name="告警机器人", sender_type="app",
                           content="告警：磁盘 90%"), "p2p", tags=["agent_sender"]),
        add(store, message("om_test_o1", sender_id=ME, sender_name="主人", content="我晚点看"), "owner_context"),
        add(store, message("om_test_o2", chat_id="oc_test_esc", sender_id=ME, sender_name="主人", content="周五发"),
            "owner_reply", tags=["owner_verified"]),
        add(store, message("sd_test_sys1", chat_id="", chat_type=SYSTEM_CHAT, sender_id="", sender_name="",
                           content="等待提醒：已经 15 分钟"), "watch_nudge"),
        add(store, message("om_test_r1", chat_id="oc_test_alpha_grp", chat_name="Alpha 群", chat_type="group",
                           sender_id=BOB, sender_name="Bob", content=f"{VIP_NAME} 让你今天把报表发她"), "group"),
    ]


def quotes_for(store: Store, rows: list) -> dict:
    return {r["message_id"]: quote_line(store.get_message(r["reply_to"])) for r in rows if r["reply_to"]}


def render(rows: list, store: Store, **kw) -> tuple[str, str]:
    """同一组消息渲染成普通批次和插话批次。kw 原样传给两者（如 vips=...）。"""
    pb = Playbook(FIXTURES)
    q = quotes_for(store, rows)
    b = batch_prompt(pb, TOPIC, "b_test_1", rows, [], TITLE, quotes=q, reminder="FX-REMINDER", **kw)
    p = priority_prompt(pb, TOPIC, "p_test_1", "b_test_0", rows, TITLE, quotes=q, reminder="FX-REMINDER", **kw)
    return b, p


def render_no_vip(**kw) -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "s.sqlite3")
        try:
            return render(base_rows(store), store, **kw)
        finally:
            store.close()
