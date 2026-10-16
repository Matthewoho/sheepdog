"""7.45 群话题直达与主人 @ 助理（7.25 + 7.44 合并；话题归属的思路来自 PR #1 @Jerry030-1，issue #2）。

假的飞书沿用 test_identity.IdentityLark（只在内存里，绝不起真实进程），按 7.45 动手前在正式账本副本里只读核实的格式造数据：
- 群话题：开头那条 thread_message_position "-1"、回复从 "0" 起；普通群、话题群都一样（7.22 说的「话题群开头 0」不对）；
- 开头那条入账时还没有话题（搜索结果里只有有回复的消息才带 thread_id），所以账本里开头那条多半没有 thread_id；
- 搜索 / mget 里的话题回复只有 thread_id 和序号，没有 reply_to、root_id；引用回复（不在话题里）搜索结果带 reply_to；
- 机器人实时事件（lark-cli event consume 拍平后）：话题回复带 thread_id、root_id（开头那条）、reply_to（直接回复的那条，
  没点「回复」某条时就是开头那条），没有序号；
- 主人在群里 @ 机器人：搜索 / mget 的 mentions 里机器人是 app_id（cli_…，正式账本 10-13 起 40 条全是这样）；事件的
  mentions 按 lark-cli 定义「和 mget 对齐」，没抓到过原样，两种 id 都测。

覆盖 7.45：
一 三类群消息：A 主人 @ 机器人 = 指令（话题 / 引用 → 名册 → 总线，插队，不答「需要你定」，确认表情同 owner_reply）；
   B 主人没 @ = 背景（话题 / 引用 → 名册 → 最近处理过 → 不送）；C 别人的话题回复 → 话题归属会话。
二 归属的四种来源（名册绑定、已记下的、开头那条送到过 / 转交给 / 代发、引用）、mget 候选（一次最多 50、核过的不再核）、
   过期 72 小时、不抢 watch。
三 机器人事件到了就处理：等事件或到点先到先处理、小轮次只入账 + 投递不跑搜索、热改成关。
三之二 收工查回执 hook 的调用日志：按天滚动、只留 7 天、写不了不影响判断和输出。
四 三个开关（含热改成关）。
"""

import io
import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli, engine, hook
from sheepdog import session as sm
from sheepdog.ack import Acker
from sheepdog.config import AckConfig, ConfigError, load_config
from sheepdog.engine import BUS_TOPIC_ID, Collector, Dispatcher, forward_messages
from sheepdog.models import Message
from sheepdog.prompts import OWNER_CONTEXT_LABEL, OWNER_INSTRUCTION_LABEL
from sheepdog.roster import parse_roster
from sheepdog.source.bot_events import BotEventConsumer
from sheepdog.source.lark import parse_event, parse_message

from test_identity import APP, BOT_OID, GRP, FakeConsumer, IdentityBase, IdentityLark
from test_roster import ME, roster_data
from test_stop_hook import BATCH, BUS_CID, HookBase, stop_input
from test_threads import ALICE, ESC_CHAT, MARK, TZ8, now8

OPS = "oc_test_ops_grp"        # 名册里没人负责的群（机器人在）
TOPIC_GRP = "oc_test_topic_grp"  # 话题群（chat_type = topic），名册里没人负责
BETA_P2P = "oc_test_beta_p2p"
BOB = "ou_test_bob"


class GroupLark(IdentityLark):
    """IdentityLark + 引用回复（搜索结果带 reply_to）+ 事件按真实拍平格式带 root_id / reply_to / mentions。
    话题群顶层消息和普通群一样：有回复以后才带 thread_id，开头那条序号 "-1"。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.bot_chats |= {OPS, TOPIC_GRP}
        self.chat_types.update({OPS: "group", TOPIC_GRP: "topic", BETA_P2P: "p2p"})
        self.chat_names.update({OPS: "运维群", TOPIC_GRP: "话题群"})

    def add(self, *, quote: str = "", mentions=(), **kw) -> str:
        chat_type = kw.get("chat_type", "p2p")
        if chat_type == "topic" and not kw.get("thread_root"):
            kw["chat_type"] = "group"      # 顶层消息先按普通群造（不预先起话题），再改回 topic
            mid = super().add(**kw)
            self.msgs[mid]["chat_type"] = "topic"
        else:
            mid = super().add(**kw)
        if quote:
            self.msgs[mid]["reply_to"] = quote
        if mentions:
            self.msgs[mid]["mentions"] = list(mentions)
        return mid

    def raw(self, mid: str) -> dict:
        out = super().raw(mid)
        m = self.msgs[mid]
        if m.get("reply_to") and not m["thread_id"]:
            out["reply_to"] = m["reply_to"]            # 引用回复：搜索结果带 reply_to（话题回复不带）
        return out

    def event(self, mid: str, mention_form: str = "app_id", **over) -> dict:
        ev = super().event(mid)
        m = self.msgs[mid]
        if m["thread_root"]:
            ev["root_id"] = m["thread_root"]
            ev["reply_to"] = m.get("reply_to") or m["thread_root"]
        elif m.get("reply_to"):
            ev["reply_to"] = ev["root_id"] = m["reply_to"]
        ms = m.get("mentions") or []
        if ms:
            def form(i):
                if i in (APP, BOT_OID):
                    return APP if mention_form == "app_id" else BOT_OID
                return i
            ev["mentions"] = [{"id": form(i), "key": f"@_user_{n}", "name": "x"} for n, i in enumerate(ms, 1)]
        ev.update(over)
        return ev


def beta_roster(thread_binding: str = "") -> dict:
    data = roster_data()
    chats = [{"chat_id": BETA_P2P, "name": "Beta 私聊", "all_messages": True}]
    if thread_binding:
        chats.append({"chat_id": OPS, "name": "运维群·话题", "thread_id": thread_binding})
    data["session"].append({"key": "beta", "mode": "managed", "conversation_id": "conv_test_beta", "title": "Beta",
                            "duty": "跟进 Beta", "chats": chats})
    return data


class GroupBase(IdentityBase):
    def setUp(self):
        super().setUp()
        self.lark = GroupLark()
        p = mock.patch("sheepdog.source.lark.subprocess.run", self.lark)
        p.start()
        self.addCleanup(p.stop)
        self.use_roster(beta_roster())
        self.d.sync_roster()
        self.d.send_onboarding(self.store.get_topic("tp_beta"))   # 新会话 tp_beta：先登记
        self.ack("tp_beta")
        self.d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_beta")["state"], sm.ACTIVE)
        self.lark.calls.clear()

    def use_roster(self, data: dict) -> None:
        self.roster = parse_roster(data)
        self.d.roster = self.roster

    # ---------- 造场景 ----------
    def say(self, chat=OPS, sender=ALICE, content="大家看一下", thread_root="", quote="", mentions=(),
            at=None, sender_type="user") -> str:
        return self.lark.add(chat_id=chat, sender_id=sender, content=content, chat_type=self.lark.chat_types[chat],
                             thread_root=thread_root, quote=quote, mentions=mentions, at=at or now8(),
                             sender_type=sender_type)

    def ack_all(self, *tids) -> None:
        for tid in tids:
            t = self.store.get_topic(tid)
            if t["pending_batch_id"] and t["state"] == sm.RUNNING:
                self.ack(tid)

    def root_to(self, topic="tp_beta", chat=OPS, content="订单服务又报 502 了 @Matthew") -> str:
        """Alice 在群里 @主人 发一条（开头那条，入账时还没有话题）→ 总线 → 总线转交给 topic → 送到 topic。"""
        root = self.say(chat, content=content, mentions=[ME], at=now8() - timedelta(minutes=10))
        self.pull(root)
        self.d.dispatch_once()
        self.ack_all(BUS_TOPIC_ID)
        if topic != BUS_TOPIC_ID:
            forward_messages(self.store, topic, [root], self_open_id=ME)
            self.d.dispatch_once()
            self.ack_all(topic)
        self.d.dispatch_once()
        r = self.row(root)
        self.assertEqual((r["topic_id"], r["dispatch_state"], r["thread_id"] or ""), (topic, "acked", ""))
        return root

    def bot_reply_for(self, topic: str, target: str, text="已经在看了") -> str:
        """会话经 sheepdog reply 以机器人身份回了 target（sends 表记一行 reply，同正式账本）。"""
        mid = self.lark.add(chat_id=self.lark.msgs[target]["chat_id"], sender_id=APP, sender_type="app", content=text,
                            chat_type=self.lark.chat_types[self.lark.msgs[target]["chat_id"]],
                            at=now8() - timedelta(minutes=5))
        self.store.add_send(f"idem_{mid}", "reply", topic, mid, text, target_message_id=target, identity="bot")
        self.pull(mid)
        self.assertEqual(self.row(mid)["reason"], "self_bot")
        return mid

    def mgets(self) -> int:
        return sum(1 for c in self.lark.calls if c[1:2] == ["+messages-mget"])

    def deliver(self, tid="tp_beta") -> str:
        cid = self.store.get_topic(tid)["conversation_id"]
        n = len(self.sink.to(cid))
        self.d.dispatch_once()
        return "\n".join(self.sink.to(cid)[n:])


# ======================= C：别人的话题回复 =======================
class GroupThreadReplyTest(GroupBase):
    def test_reply_in_thread_goes_to_session_that_got_the_root(self):
        root = self.root_to("tp_beta")
        self.lark.calls.clear()
        rep = self.say(thread_root=root, content="我这边也复现了")
        raw = self.lark.raw(rep)
        self.assertEqual((raw["thread_message_position"], "reply_to" in raw), ("0", False))   # 真格式：没有 reply_to
        self.pull(rep)
        r = self.row(rep)
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), ("dispatch", "thread_owner", "tp_beta"))
        self.assertIn("group_thread:forwarded", json.loads(r["tags_json"]))
        self.assertIn("这是你处理过的「订单服务又报 502 了 @Matthew」下面的话题回复", r["note"])
        self.assertEqual(self.mgets(), 1)                 # 开头那条账本里没有话题：mget 候选认出来
        g = self.store.group_thread(self.lark.msgs[root]["thread_id"])
        self.assertEqual((g["topic_id"], g["source"], g["root_message_id"]), ("tp_beta", "forwarded", root))
        self.assertEqual(self.row(root)["thread_position"], "-1")   # 顺手把开头那条的话题补进账本
        text = self.deliver()
        self.assertIn("[话题归属]", text)
        self.assertIn("我这边也复现了", text)
        # 第二条回复：话题已记下，不再 mget
        self.ack_all("tp_beta")
        rep2 = self.say(thread_root=root, content="第二条")
        self.pull(rep2)
        self.assertEqual(self.row(rep2)["topic_id"], "tp_beta")
        self.assertIn("group_thread:recorded", json.loads(self.row(rep2)["tags_json"]))
        self.assertEqual(self.mgets(), 1)

    def test_bot_thread_reply_not_attributed_human_still_is(self):
        # 架构侧 10-16 17:4x：机器人 / 应用发的话题回复不按话题归属直达，照原来的路由走；人的照旧直达
        root = self.root_to("tp_beta")
        bot = self.say(thread_root=root, sender="ou_test_wizard", sender_type="app", content="自动巡检：一切正常")
        self.pull(bot)
        r = self.row(bot)
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), ("inbox", "group", None))
        self.assertNotIn("group_thread", r["tags_json"])
        self.assertIsNone(r["note"])
        # 机器人事件那条路（sender_type bot，入账时换成 app）同样不直达
        bot2 = self.say(thread_root=root, sender="ou_test_wizard", sender_type="app", content="第二次巡检")
        self.bot_hears(self.lark.event(bot2))
        self.assertEqual(self.row(bot2)["route"], "inbox")
        human = self.say(thread_root=root, content="人回的")
        self.pull(human)
        self.assertEqual((self.row(human)["reason"], self.row(human)["topic_id"]), ("thread_owner", "tp_beta"))

    def test_bot_thread_reply_at_owner_or_keyword_keeps_old_route(self):
        root = self.root_to("tp_beta")
        at = self.say(thread_root=root, sender="ou_test_wizard", sender_type="app", content="请 Matthew 看一下",
                      mentions=[ME])
        self.pull(at)
        r = self.row(at)
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), ("dispatch", "at_me", BUS_TOPIC_ID))  # 照原路由：总线
        self.assertNotIn("group_thread", r["tags_json"])
        self.cfg.routing.keyword_skip_bot_senders = False      # 让机器人也参与关键词
        kw = self.say(thread_root=root, sender="ou_test_wizard", sender_type="app", content="故障已恢复")
        self.pull(kw)
        r = self.row(kw)
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), ("dispatch", "keyword", BUS_TOPIC_ID))  # 不是 tp_beta
        self.assertNotIn("group_thread", r["tags_json"])
        # 同一个话题里人发的关键词消息：照旧直达归属会话（原因保留 keyword）
        hk = self.say(thread_root=root, content="故障我来跟")
        self.pull(hk)
        self.assertEqual((self.row(hk)["reason"], self.row(hk)["topic_id"]), ("keyword", "tp_beta"))

    def test_root_delivered_directly_and_bot_event_root_id_needs_no_mget(self):
        root = self.root_to(BUS_TOPIC_ID)               # 开头那条只送到了总线、总线没转交 → 归总线
        self.lark.calls.clear()
        rep = self.say(thread_root=root, content="收到")
        ev = self.lark.event(rep)
        self.assertEqual((ev["root_id"], ev["reply_to"]), (root, root))
        self.lark.plan["+messages-mget"] = ["error"]    # 事件那条路补齐失败也不要紧：root_id 直接认
        with self.assertLogs("sheepdog", "INFO") as logs:
            self.bot_hears(ev)
        r = self.row(rep)
        self.assertEqual((r["topic_id"], r["reason"]), (BUS_TOPIC_ID, "thread_owner"))
        self.assertIn("group_thread:delivered", json.loads(r["tags_json"]))
        self.assertTrue(any(f"开头那条 {root} 靠 event 找到" in x for x in logs.output))
        # 找开头那条没用 mget 候选（mget 只有事件补齐、和「回复的是不是主人」查发送人那两次，都只带这一两条）
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM thread_root_checks").fetchone()[0], 0)
        for c in self.lark.calls:
            if c[1:2] == ["+messages-mget"]:
                self.assertLessEqual(set(c[c.index("--message-ids") + 1].split(",")), {rep, root})

    def test_root_sent_by_session_via_sheepdog(self):
        q = self.say(content="能帮忙看下吗", at=now8() - timedelta(minutes=8))
        self.pull(q)
        bot = self.bot_reply_for("tp_beta", q, "我来看，稍等")
        rep = self.say(thread_root=bot, content="好的，等你")
        self.pull(rep)
        r = self.row(rep)
        self.assertEqual(r["topic_id"], "tp_beta")
        self.assertIn("group_thread:sent", json.loads(r["tags_json"]))

    def test_chatter_thread_unchanged_and_no_candidates_no_mget(self):
        root = self.say(chat=GRP, content="中午吃啥", at=now8() - timedelta(minutes=5))
        self.pull(root)
        rep = self.say(chat=GRP, thread_root=root, content="面")
        self.pull(rep)
        r = self.row(rep)
        self.assertEqual((r["route"], r["reason"]), ("inbox", "group"))      # 照旧
        self.assertEqual(self.mgets(), 0)                                    # 没有投出去过的候选：不 mget

    def test_unrecognized_thread_mget_once_then_cached(self):
        self.root_to("tp_beta")                                        # 群里有一条投出去过的候选
        other = self.say(content="另一件事", at=now8() - timedelta(minutes=4))
        self.pull(other)
        self.lark.calls.clear()
        rep = self.say(thread_root=other, content="闲聊回复")
        self.pull(rep)
        self.assertEqual(self.row(rep)["route"], "inbox")
        self.assertEqual(self.mgets(), 1)
        rep2 = self.say(thread_root=other, content="再一条")
        self.pull(rep2)
        self.assertEqual(self.row(rep2)["route"], "inbox")
        self.assertEqual(self.mgets(), 1)                             # 候选这个话题有回复以后核过：不再 mget

    def test_mget_at_most_50_candidates(self):
        for i in range(60):
            self.store.conn.execute(
                "INSERT INTO messages(message_id,chat_id,chat_type,sender_id,content,create_time,route,reason,"
                "topic_id,dispatch_state,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"om_test_c{i:03d}", OPS, "group", ALICE, "旧消息", (now8() - timedelta(minutes=30 + i)).isoformat(),
                 "dispatch", "at_me", "tp_beta", "acked", engine.now_iso(), engine.now_iso()))
        self.store.conn.commit()
        root = self.say(content="开头", at=now8() - timedelta(minutes=2))
        rep = self.say(thread_root=root, content="回复")
        self.pull(rep)
        (call,) = [c for c in self.lark.calls if c[1:2] == ["+messages-mget"]]
        self.assertEqual(len(call[call.index("--message-ids") + 1].split(",")), 50)

    def test_topic_chat_root_is_minus_one(self):
        root = self.root_to("tp_beta", chat=TOPIC_GRP, content="话题群里的开头 @Matthew")
        rep = self.say(chat=TOPIC_GRP, thread_root=root, content="话题群里的回复")
        self.assertEqual(self.lark.raw(rep)["thread_message_position"], "0")
        self.pull(rep)
        self.assertEqual(self.row(rep)["topic_id"], "tp_beta")
        self.assertEqual(self.row(root)["thread_position"], "-1")

    def test_expired_after_72_hours(self):
        root = self.root_to("tp_beta")
        rep = self.say(thread_root=root, content="第一条")
        self.pull(rep)
        tid = self.lark.msgs[root]["thread_id"]
        self.assertEqual(self.store.group_thread(tid)["topic_id"], "tp_beta")
        old = (datetime.now(TZ8) - timedelta(hours=73)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE messages SET create_time=? WHERE thread_id=? OR message_id=?", (old, tid, root))
        self.store.conn.execute("UPDATE group_threads SET last_activity_at=?", (old,))
        self.store.conn.commit()
        late = self.say(thread_root=root, content="三天后又有人回")
        self.pull(late)
        self.assertEqual((self.row(late)["route"], self.row(late)["reason"]), ("inbox", "group"))
        # 71 小时：还算
        recent = (datetime.now(TZ8) - timedelta(hours=71)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE messages SET create_time=? WHERE thread_id=?", (recent, tid))
        self.store.conn.execute("UPDATE group_threads SET last_activity_at=?", (recent,))
        self.store.conn.commit()
        later = self.say(thread_root=root, content="71 小时")
        self.pull(later)
        self.assertEqual(self.row(later)["topic_id"], "tp_beta")

    def test_auto_attribution_expired_when_root_is_old(self):
        root = self.root_to("tp_beta")
        old = (datetime.now(TZ8) - timedelta(hours=80)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE messages SET create_time=? WHERE message_id=?", (old, root))
        self.store.conn.commit()
        rep = self.say(thread_root=root, content="很久以后第一条回复")
        self.pull(rep)
        self.assertEqual(self.row(rep)["route"], "inbox")
        self.assertIsNone(self.store.group_thread(self.lark.msgs[root]["thread_id"]))

    def test_roster_thread_binding_overrides_and_never_expires(self):
        root = self.root_to("tp_beta")
        rep = self.say(thread_root=root, content="第一条")    # 先把话题编号造出来
        tid = self.lark.msgs[root]["thread_id"]
        data = beta_roster()
        data["session"][0]["chats"].append({"chat_id": OPS, "name": "运维群·话题", "thread_id": tid})   # 绑给 alpha
        self.use_roster(data)
        self.pull(rep)
        r = self.row(rep)
        self.assertEqual(r["topic_id"], "tp_alpha")
        self.assertIn("group_thread:roster", json.loads(r["tags_json"]))
        old = (datetime.now(TZ8) - timedelta(days=5)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE messages SET create_time=? WHERE thread_id=? OR message_id=?", (old, tid, root))
        self.store.conn.commit()
        late = self.say(thread_root=root, content="五天后")
        self.pull(late)
        self.assertEqual(self.row(late)["topic_id"], "tp_alpha")
        # 名册的话题绑定不算整群归属：同一个群里别的消息照旧
        other = self.say(content="别的话")
        self.pull(other)
        self.assertEqual(self.row(other)["route"], "inbox")

    def test_watch_of_other_session_does_not_steal(self):
        root = self.root_to("tp_beta")
        self.store.add_watch("tp_alpha", ALICE, "", "等 Alice 回别的事")        # 整群范围（不限聊天）的 watch
        rep = self.say(thread_root=root, content="回在 beta 的话题里")
        self.pull(rep)
        r = self.row(rep)
        self.assertEqual(r["topic_id"], "tp_beta")
        self.assertFalse(any(t.startswith("watch:") for t in json.loads(r["tags_json"])))
        (w,) = self.store.open_watches()
        self.assertNotEqual(w["status"], "replied")                            # alpha 的等待没被这条「回复」掉
        # 不在有归属的话题里：watch 照旧生效
        plain = self.say(content="普通发言")
        self.pull(plain)
        self.assertEqual(self.row(plain)["topic_id"], "tp_alpha")

    def test_watch_of_owner_session_is_kept(self):
        root = self.root_to("tp_beta")
        wid = self.store.add_watch("tp_beta", ALICE, OPS, "等 Alice 回")
        rep = self.say(thread_root=root, content="回复了")
        self.pull(rep)
        r = self.row(rep)
        self.assertEqual(r["topic_id"], "tp_beta")
        self.assertIn(f"watch:{wid}", json.loads(r["tags_json"]))
        self.assertEqual(self.store.get_watch(wid)["status"], "replied")

    def test_forward_of_thread_reply_repoints_thread(self):
        root = self.root_to(BUS_TOPIC_ID)
        rep = self.say(thread_root=root, content="谁来跟")
        self.pull(rep)
        self.assertEqual(self.row(rep)["topic_id"], BUS_TOPIC_ID)
        self.d.dispatch_once()
        self.ack_all(BUS_TOPIC_ID)
        forward_messages(self.store, "tp_beta", [rep], self_open_id=ME)
        g = self.store.group_thread(self.lark.msgs[root]["thread_id"])
        self.assertEqual((g["topic_id"], g["source"]), ("tp_beta", "forwarded"))
        nxt = self.say(thread_root=root, content="下一条")
        self.pull(nxt)
        self.assertEqual(self.row(nxt)["topic_id"], "tp_beta")

    def test_progressive_disclosure_on_long_thread(self):
        root = self.root_to("tp_beta")
        for i in range(4):
            x = self.say(thread_root=root, content=f"第 {i} 条讨论")
            self.pull(x)
        last = self.say(thread_root=root, content="最新这一条")
        self.pull(last)
        note = self.row(last)["note"]
        self.assertIn("话题里除了开头那条已有 5 条（含这一条），最近", note)
        self.assertIn("第 3 条讨论", note)
        self.assertIn(f"sheepdog thread --topic tp_beta --thread {self.lark.msgs[root]['thread_id']}", note)
        # sheepdog thread 认得这个群话题（记在 tp_beta 名下）
        rc, out, err = self.cli("thread", "--topic", "tp_beta", "--thread", self.lark.msgs[root]["thread_id"])
        self.assertEqual(rc, 0, err)
        self.assertIn("最新这一条", out)

    def test_agent_marker_and_ignored_chat_not_attributed(self):
        root = self.root_to("tp_beta")
        rep = self.say(thread_root=root, content=f"代回的\n{MARK}")
        self.pull(rep)
        self.assertNotIn("group_thread", self.row(rep)["tags_json"])
        self.cfg.routing.ignore_chat_ids.append(OPS)
        rep2 = self.say(thread_root=root, content="忽略的群")
        self.pull(rep2)
        self.assertEqual(self.row(rep2)["route"], "drop")

    def test_switch_off_is_old_behaviour_and_hot(self):
        root = self.root_to("tp_beta")
        self.cfg.threads.group_owner = False
        self.lark.calls.clear()
        rep = self.say(thread_root=root, content="关着")
        self.pull(rep)
        self.assertEqual((self.row(rep)["route"], self.row(rep)["reason"]), ("inbox", "group"))
        self.assertEqual(self.mgets(), 0)
        self.cfg.threads.group_owner = True                 # 热改回开：下一条就生效
        rep2 = self.say(thread_root=root, content="开着")
        self.pull(rep2)
        self.assertEqual(self.row(rep2)["topic_id"], "tp_beta")


# ======================= A：主人在群里 @ 机器人 =======================
class OwnerInstructionTest(GroupBase):
    def owner_at(self, content="@Mattheuu 让cicd哥看一下咋回事", chat=OPS, **kw) -> str:
        return self.say(chat, sender=ME, content=content, mentions=[APP], **kw)

    def test_quote_of_session_bot_reply_goes_to_that_session_as_instruction(self):
        # 10-14 19:15 / 10-15 14:44 / 18:37 那种：主人引用了某个会话以机器人身份代回的那条，@ 机器人
        q = self.say(content="这个构建怎么挂了", at=now8() - timedelta(minutes=8))
        self.pull(q)
        bot = self.bot_reply_for("tp_beta", q)
        mid = self.owner_at(quote=bot)
        self.pull(mid)
        r = self.row(mid)
        self.assertEqual((r["route"], r["reason"], r["topic_id"], r["dispatch_state"]),
                         ("dispatch", "owner_instruction", "tp_beta", "pending"))
        self.assertEqual(json.loads(r["tags_json"]), ["owner_instruction", "owner_at:quote_sent"])
        text = self.deliver()
        self.assertIn("[主人在群里 @ 你]", text)
        self.assertIn(OWNER_INSTRUCTION_LABEL, text)
        self.assertNotIn(OWNER_CONTEXT_LABEL, text)
        self.assertIn("↪ 回复的是", text)
        self.assertIn("处理完成后提交回执", text)             # 是指令：要回执
        t = self.store.get_topic("tp_beta")
        self.assertEqual(t["state"], sm.RUNNING)
        acker = Acker(AckConfig(enabled=True, emoji_type="Get", reasons=["at_me"], bot_reasons=["owner_reply"]),
                      self.store, None)
        self.assertEqual(acker.identity_for(self.row(mid)), "bot")   # 确认表情同 owner_reply：机器人身份

    def test_in_thread_goes_to_thread_owner(self):
        root = self.root_to("tp_beta")
        mid = self.owner_at(thread_root=root)
        self.pull(mid)
        r = self.row(mid)
        self.assertEqual((r["reason"], r["topic_id"]), ("owner_instruction", "tp_beta"))
        self.assertIn("owner_at:forwarded", json.loads(r["tags_json"]))
        self.assertIn("下面的主人的指令", r["note"])

    def test_roster_chat_then_bus(self):
        mid = self.owner_at(chat=GRP)                 # tp_alpha 负责 GRP（只推 dispatch 级）
        self.pull(mid)
        self.assertEqual((self.row(mid)["topic_id"], json.loads(self.row(mid)["tags_json"])[1]),
                         ("tp_alpha", "owner_at:roster_chat"))
        mid2 = self.owner_at(content="@Mattheuu 谁来看看")   # 没人负责的群、没引用、不在话题里 → 总线
        self.pull(mid2)
        r = self.row(mid2)
        self.assertEqual((r["topic_id"], r["reason"]), (BUS_TOPIC_ID, "owner_instruction"))
        self.assertIn(f"--message-ids {mid2}", r["note"])

    def test_priority_when_session_running_and_followup_starts(self):
        q = self.say(content="构建挂了", at=now8() - timedelta(minutes=8))
        self.pull(q)
        bot = self.bot_reply_for("tp_beta", q)
        busy = self.say(BETA_P2P, sender=BOB, content="beta 手上的活")
        self.pull(busy)
        self.d.dispatch_once()
        t = self.store.get_topic("tp_beta")
        self.assertEqual(t["state"], sm.RUNNING)
        b1 = t["pending_batch_id"]
        mid = self.owner_at(quote=bot)
        self.pull(mid)
        res = self.d.dispatch_once()
        pb = res["topics"]["tp_beta"]["priority"]
        self.assertTrue(pb.startswith("p"))
        text = self.sink.to("conv_test_beta")[-1]
        self.assertIn(f"主人插话批次 `{pb}`", text)
        self.assertIn(OWNER_INSTRUCTION_LABEL, text)
        t = self.store.get_topic("tp_beta")
        self.assertEqual((t["state"], t["pending_batch_id"]), (sm.RUNNING, b1))     # 手上那批照旧等回执
        self.assertEqual(self.row(mid)["dispatch_state"], "acked")
        e = self.store.echo_get(mid)                                            # 插话没回音提醒照常起算（7.52 回音账本）
        self.assertEqual((e["stage"], e["priority"], e["batch_id"], e["kind"]), ("delivered", 1, pb, "owner_instruction"))

    def test_not_an_answer_to_open_escalation_even_when_forwarded(self):
        rc, out, _ = self.ask("--key", "PR #3", topic="tp_beta")
        self.assertEqual(rc, 0, out)
        ask_mid = next(k for k, v in self.lark.msgs.items() if v["chat_id"] == ESC_CHAT)
        self.pull(ask_mid)
        self.assertEqual(len(self.store.open_escalations("tp_beta")), 1)
        mid = self.owner_at(content="@Mattheuu 这个先放一放")
        self.pull(mid)                                         # 先到总线
        self.d.dispatch_once()
        forward_messages(self.store, "tp_beta", [mid], self_open_id=ME)
        r = self.row(mid)
        self.assertIn("owner_instruction", json.loads(r["tags_json"]))
        self.assertNotIn("owner_verified", json.loads(r["tags_json"]))
        self.assertEqual(len(self.store.open_escalations("tp_beta")), 1)   # 没当成回答
        self.ack_all(BUS_TOPIC_ID)
        text = self.deliver()
        self.assertIn(OWNER_INSTRUCTION_LABEL, text)
        self.assertEqual(len(self.store.open_escalations("tp_beta")), 1)

    def test_bot_event_with_open_id_or_app_id_mentions(self):
        for form in ("open_id", "app_id"):
            with self.subTest(form=form):
                mid = self.owner_at(content=f"@Mattheuu 看看 {form}")
                self.lark.plan["+messages-mget"] = ["error", "error"]   # 只有事件这一份（mget 补不上）
                self.bot_hears(self.lark.event(mid, mention_form=form))
                self.assertEqual(self.row(mid)["reason"], "owner_instruction")

    def test_not_instruction_cases(self):
        marked = self.owner_at(content=f"@Mattheuu 代回的卡片\n{MARK}")          # 带代回标记
        self.pull(marked)
        self.assertNotEqual(self.row(marked)["reason"], "owner_instruction")
        plain = self.say(sender=ME, content="没 @ 机器人")
        self.pull(plain)
        self.assertNotEqual(self.row(plain)["reason"], "owner_instruction")
        p2p = self.lark.add(chat_id=BETA_P2P, sender_id=ME, content="@Mattheuu 私聊里", chat_type="p2p")
        self.lark.msgs[p2p]["mentions"] = [APP]
        self.pull(p2p)
        self.assertNotEqual(self.row(p2p)["reason"], "owner_instruction")
        esc = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="@Mattheuu 找主人聊天里")
        self.lark.msgs[esc]["mentions"] = [APP]
        self.pull(esc)
        self.assertNotEqual(self.row(esc)["reason"], "owner_instruction")
        self.cfg.identity.enabled = False                                         # 不知道机器人是谁
        off = self.owner_at(content="@Mattheuu identity 关着")
        self.pull(off)
        self.assertNotEqual(self.row(off)["reason"], "owner_instruction")

    def test_switch_off_falls_back_to_context_and_hot(self):
        q = self.say(content="构建挂了", at=now8() - timedelta(minutes=8))
        self.pull(q)
        bot = self.bot_reply_for("tp_beta", q)
        self.cfg.routing.owner_at_assistant = False
        mid = self.owner_at(quote=bot)
        self.pull(mid)
        self.assertEqual(self.row(mid)["reason"], "owner_context")
        self.cfg.routing.owner_at_assistant = True
        mid2 = self.owner_at(quote=bot, content="@Mattheuu 再说一次")
        self.pull(mid2)
        self.assertEqual(self.row(mid2)["reason"], "owner_instruction")


# ======================= B：主人没 @ 机器人 = 背景 =======================
class OwnerContextGroupTest(GroupBase):
    def test_in_thread_goes_to_thread_owner_as_context(self):
        root = self.root_to("tp_beta")
        mid = self.say(sender=ME, thread_root=root, content="我先看下日志")
        self.pull(mid)
        r = self.row(mid)
        self.assertEqual((r["reason"], r["topic_id"]), ("owner_context", "tp_beta"))
        self.assertIn("下面的话题里主人的发言", r["note"])
        text = self.deliver()
        self.assertIn(OWNER_CONTEXT_LABEL, text)

    def test_quote_of_delivered_message_goes_to_its_session(self):
        root = self.root_to("tp_beta")
        mid = self.say(sender=ME, quote=root, content="这个我知道")
        self.pull(mid)
        r = self.row(mid)
        self.assertEqual((r["reason"], r["topic_id"]), ("owner_context", "tp_beta"))
        self.assertIn("引用归属", r["note"])

    def test_roster_then_recent_then_nothing(self):
        mid = self.say(chat=GRP, sender=ME, content="说一句")
        self.pull(mid)
        self.assertEqual(self.row(mid)["topic_id"], "tp_alpha")
        self.root_to(BUS_TOPIC_ID)                                  # 总线最近处理过 OPS
        mid2 = self.say(sender=ME, content="普通发言")
        self.pull(mid2)
        self.assertEqual(self.row(mid2)["topic_id"], BUS_TOPIC_ID)
        never = self.lark.add(chat_id="oc_test_never", sender_id=ME, content="没人处理过", chat_type="group")
        self.lark.chat_types["oc_test_never"] = "group"
        self.pull(never)
        self.assertEqual(self.row(never)["route"], "self")


# ======================= 三：机器人事件到了就处理 =======================
class EventConsumerWaitTest(GroupBase):
    def test_wait_returns_on_event_and_poll_clears(self):
        c = BotEventConsumer("lark-cli", popen=lambda *a, **k: (_ for _ in ()).throw(OSError("不起真进程")))
        self.assertFalse(c.wait(0.01))
        c.queue.put(("event", None, {"message_id": "om_test_x"}))
        c.arrived.set()
        self.assertTrue(c.wait(0.01))
        self.assertEqual(c.poll(), [{"message_id": "om_test_x"}])
        self.assertFalse(c.wait(0.01))

    def test_reader_thread_sets_flag(self):
        c = BotEventConsumer("lark-cli")
        t = threading.Thread(target=c._read_stdout, args=(None, iter(['{"message_id": "om_test_y"}\n'])))
        t.start()
        t.join(2)
        self.assertTrue(c.wait(1))


class WakeConsumer(FakeConsumer):
    """FakeConsumer + wait()：按脚本依次返回「到了」（True，同时把事件放进 pending）或「到点」（False）。"""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.waits = []

    def wait(self, timeout):
        self.waits.append(timeout)
        if not self.script:
            return False
        evs = self.script.pop(0)
        if evs is None:
            return False
        self.pending += evs
        return True


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class WakeLoopTest(GroupBase):
    def collector(self) -> Collector:
        c = Collector(self.cfg, self.store, self.source, self.roster)
        c.bot_events = self.consumer
        return c

    def args(self):
        return mock.Mock(interval=60)

    def test_event_triggers_mini_round_ingest_and_deliver_without_search(self):
        q = self.say(content="构建挂了", at=now8() - timedelta(minutes=8))
        self.pull(q)
        bot = self.bot_reply_for("tp_beta", q)
        mid = self.say(sender=ME, content="@Mattheuu 看一下", quote=bot, mentions=[APP])
        self.consumer = WakeConsumer([[self.lark.event(mid)], None])
        clk = FakeClock()
        self.lark.calls.clear()
        n = cli._wait_next(self.args(), self.collector(), self.d, clock=clk.clock, sleep=clk.sleep)
        self.assertEqual(n, 1)
        self.assertEqual(clk.slept[0], cli.BOT_EVENT_SETTLE_SECONDS)      # 攒 2 秒
        self.assertEqual(self.row(mid)["reason"], "owner_instruction")
        self.assertEqual(self.row(mid)["dispatch_state"], "delivered")      # 小轮次里就送到了
        self.assertIn(OWNER_INSTRUCTION_LABEL, self.sink.to("conv_test_beta")[-1])
        self.assertNotIn("+messages-search", [c[1] for c in self.lark.calls])   # 不跑搜索
        self.assertEqual(len(self.consumer.waits), 2)
        self.assertAlmostEqual(self.consumer.waits[1], 60 - cli.BOT_EVENT_SETTLE_SECONDS)

    def test_mini_round_does_not_collect_receipts(self):
        root = self.root_to("tp_beta")
        busy = self.say(BETA_P2P, sender=BOB, content="beta 手上的活")
        self.pull(busy)
        self.d.dispatch_once()
        self.ack("tp_beta")                              # 回执交了，但小轮次不收回执
        nxt = self.say(thread_root=root, content="话题里又来一条")
        self.consumer = WakeConsumer([[self.lark.event(nxt)]])
        clk = FakeClock()
        cli._wait_next(self.args(), self.collector(), self.d, clock=clk.clock, sleep=clk.sleep)
        self.assertEqual(self.store.get_topic("tp_beta")["state"], sm.RUNNING)
        self.assertEqual(self.row(nxt)["dispatch_state"], "pending")      # 排队，等完整轮次收回执后再送
        self.d.dispatch_once()
        self.assertEqual(self.row(nxt)["dispatch_state"], "delivered")

    def test_switch_off_sleeps_full_interval_and_hot(self):
        self.cfg.run.wake_on_bot_event = False
        self.consumer = WakeConsumer([[{"message_id": "om_test_never"}]])
        clk = FakeClock()
        n = cli._wait_next(self.args(), self.collector(), self.d, clock=clk.clock, sleep=clk.sleep)
        self.assertEqual((n, clk.slept, self.consumer.waits), (0, [60], []))
        # 等到一半热改成关：下一次等就按睡满剩下的
        self.cfg.run.wake_on_bot_event = True
        mid = self.say(BETA_P2P, sender=BOB, content="一条")
        cons = WakeConsumer([[self.lark.event(mid)]])
        col = Collector(self.cfg, self.store, self.source, self.roster)
        col.bot_events = cons
        orig = cli._mini_cycle

        def mini(c, d):
            out = orig(c, d)
            self.cfg.run.wake_on_bot_event = False
            return out
        clk = FakeClock()
        with mock.patch.object(cli, "_mini_cycle", side_effect=mini):
            n = cli._wait_next(self.args(), col, self.d, clock=clk.clock, sleep=clk.sleep)
        self.assertEqual(n, 1)
        self.assertEqual(clk.slept, [cli.BOT_EVENT_SETTLE_SECONDS, 60 - cli.BOT_EVENT_SETTLE_SECONDS])
        self.assertEqual(len(cons.waits), 1)

    def test_no_consumer_sleeps(self):
        col = Collector(self.cfg, self.store, self.source, self.roster)
        clk = FakeClock()
        self.assertEqual(cli._wait_next(self.args(), col, self.d, clock=clk.clock, sleep=clk.sleep), 0)
        self.assertEqual(clk.slept, [60])

    def test_mini_round_error_does_not_break_wait(self):
        self.consumer = WakeConsumer([[{"message_id": "om_test_z"}], None])
        clk = FakeClock()
        with mock.patch.object(cli, "_mini_cycle", side_effect=RuntimeError("坏了")), self.assertLogs("root", "ERROR"):
            n = cli._wait_next(self.args(), self.collector(), self.d, clock=clk.clock, sleep=clk.sleep)
        self.assertEqual(n, 1)


# ======================= 四：开关的配置文件 =======================
class SwitchConfigTest(GroupBase):
    def test_defaults_and_parse_and_validate(self):
        p = Path(self.tmp.name) / "c.toml"
        p.write_text(f'self_open_id = "{ME}"\n', encoding="utf-8")
        cfg = load_config(p)
        self.assertEqual((cfg.threads.group_owner, cfg.threads.group_owner_hours, cfg.routing.owner_at_assistant,
                          cfg.run.wake_on_bot_event), (True, 72, True, True))
        p.write_text(f'self_open_id = "{ME}"\n[threads]\ngroup_owner = false\ngroup_owner_hours = 24\n'
                     "[routing]\nowner_at_assistant = false\n[run]\nwake_on_bot_event = false\n", encoding="utf-8")
        cfg = load_config(p)
        self.assertEqual((cfg.threads.group_owner, cfg.threads.group_owner_hours, cfg.routing.owner_at_assistant,
                          cfg.run.wake_on_bot_event), (False, 24, False, False))
        for bad in ('[threads]\ngroup_owner = "no"\n', "[threads]\ngroup_owner_hours = 0\n",
                    "[routing]\nowner_at_assistant = 1\n", '[run]\nwake_on_bot_event = "yes"\n'):
            with self.subTest(bad=bad):
                p.write_text(f'self_open_id = "{ME}"\n' + bad, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(p)


# ======================= 三之二：收工查回执 hook 记调用 =======================
class StopCallLogTest(HookBase):
    def log_lines(self, day: str = "2026-10-14") -> list[dict]:
        p = hook.call_log_dir(self.cfg.state_dir) / f"{day}.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]

    def test_each_call_logged_with_decision_reason_and_time(self):
        rc, out = self.run_main()
        self.assertEqual(json.loads(out)["decision"], "continue")
        self.run_main(stop_input(fully_idle=False))
        self.run_main(stop_input(cid="not-ours"))
        a, b, c = self.log_lines()
        self.assertEqual((a["decision"], a["managed"], a["topic"], a["batch"], a["conversation_id"]),
                         ("block", True, "tp_bus", BATCH, BUS_CID))
        self.assertIn("第 1 次拦", a["reason"])
        self.assertIsInstance(a["ms"], float)
        self.assertEqual(a["at"], self.now.isoformat(timespec="seconds"))
        self.assertEqual((b["decision"], b["managed"]), ("pass", None))
        self.assertIn("fullyIdle", b["reason"])
        self.assertEqual((c["decision"], c["managed"]), ("pass", False))
        self.assertIn("不是牧羊犬管的", c["reason"])

    def test_error_is_logged_and_still_silent(self):
        rc, out = self.run_main("not json")
        self.assertEqual((rc, out), (0, ""))
        (x,) = self.log_lines()
        self.assertEqual(x["decision"], "pass")
        self.assertIn("出错不拦", x["reason"])

    def test_rolls_daily_and_keeps_seven_days(self):
        d = hook.call_log_dir(self.cfg.state_dir)
        d.mkdir(parents=True)
        for day in ("2026-10-01", "2026-10-07", "2026-10-08", "2026-10-13"):
            (d / f"{day}.jsonl").write_text("{}\n", encoding="utf-8")
        self.run_main()
        self.assertEqual(sorted(p.stem for p in d.glob("*.jsonl")), ["2026-10-08", "2026-10-13", "2026-10-14"])
        nxt = self.now + timedelta(days=1)
        hook.stop_main(io.BytesIO(stop_input().encode()), io.StringIO(), self.cfg, nxt)
        self.assertEqual(sorted(p.stem for p in d.glob("*.jsonl")), ["2026-10-13", "2026-10-14", "2026-10-15"])

    def test_log_unwritable_does_not_change_decision_or_output(self):
        (self.cfg.state_dir / "hooks").mkdir()
        (self.cfg.state_dir / "hooks" / "stop-log").write_text("挡路的文件", encoding="utf-8")
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["decision"], "continue")    # 照样拦
        rc, out = self.run_main(stop_input(fully_idle=False))
        self.assertEqual((rc, out), (0, ""))


# ======================= 名册的话题绑定（PR #1 的格式） =======================
class RosterThreadBindingTest(GroupBase):
    def test_parse_and_validate(self):
        from sheepdog.roster import RosterError
        r = parse_roster(beta_roster(thread_binding="omt_test_x"))
        self.assertEqual(r.thread_owner(OPS, "omt_test_x")[0].topic_id, "tp_beta")
        self.assertIsNone(r.owner_of(OPS))                     # 话题绑定不等于整群归属
        self.assertIsNone(r.thread_owner(OPS, "omt_test_other"))
        for bad, why in (("x_1", "omt_"), ("", "不能为空")):
            d = beta_roster()
            d["session"][2]["chats"].append({"chat_id": OPS, "thread_id": bad})
            with self.subTest(bad=bad), self.assertRaisesRegex(RosterError, why):
                parse_roster(d)
        d = beta_roster(thread_binding="omt_test_x")
        d["session"][0]["chats"].append({"chat_id": OPS, "thread_id": "omt_test_x"})
        with self.assertRaisesRegex(RosterError, "只能属于一个"):
            parse_roster(d)
        # 同一个群：整群归 alpha、其中一个话题归 beta，可以
        d = beta_roster(thread_binding="omt_test_x")
        d["session"][0]["chats"].append({"chat_id": OPS})
        r = parse_roster(d)
        self.assertEqual((r.owner_of(OPS)[0].topic_id, r.thread_owner(OPS, "omt_test_x")[0].topic_id),
                         ("tp_alpha", "tp_beta"))

    def test_hash_unchanged_without_binding(self):
        self.assertEqual(parse_roster(roster_data()).content_hash(), parse_roster(roster_data()).content_hash())
        a = parse_roster(beta_roster())
        b = parse_roster(beta_roster(thread_binding="omt_test_x"))
        self.assertNotEqual(a.content_hash(), b.content_hash())
        from sheepdog.prompts import chats_text
        self.assertIn("只负责话题 `omt_test_x`", chats_text(b.by_key("beta")))
        self.assertEqual(chats_text(a.by_key("alpha")),
                         "- Alpha 私聊（`oc_test_alpha_p2p`，全部消息）\n- Alpha 群（`oc_test_alpha_grp`，仅 dispatch 级消息）")


# ======================= 7.48 卡片 × 7.45 读正文：先还原再认 =======================
class CardReadbackTest(GroupBase):
    """机器人发的卡片读回来是 <card title=…>…</card> / <card>…</card>：话题开头摘要、引用摘要、渐进附的最近几条、
    代回标记，都按还原后的「标题\\n正文」认，不出现 <card 外壳。"""

    def bot_card(self, topic: str, target: str, content: str) -> str:
        mid = self.lark.add(chat_id=OPS, sender_id=APP, sender_type="app", content=content, chat_type="group",
                            at=now8() - timedelta(minutes=5), msg_type="interactive")
        self.store.add_send(f"idem_{mid}", "reply", topic, mid, "（sends 记的纯文本）", target_message_id=target,
                            identity="bot")
        self.pull(mid)
        return mid

    def test_thread_under_bot_card_root_summary_is_plain(self):
        q = self.say(content="构建挂了", at=now8() - timedelta(minutes=8))
        self.pull(q)
        card = self.bot_card("tp_beta", q, f"<card>\n我来看，先回滚到上一版\n{MARK}\n</card>")
        rep = self.say(thread_root=card, content="好的")
        self.pull(rep)
        r = self.row(rep)
        self.assertEqual((r["topic_id"], r["reason"]), ("tp_beta", "thread_owner"))
        self.assertIn("这是你处理过的「我来看，先回滚到上一版」下面的话题回复", r["note"])
        self.assertNotIn("<card", r["note"])
        self.assertEqual(self.store.group_thread(self.lark.msgs[card]["thread_id"])["root_summary"], "我来看，先回滚到上一版")

    def test_titled_card_root_summary_uses_title(self):
        q = self.say(content="问一下进度", at=now8() - timedelta(minutes=8))
        self.pull(q)
        card = self.bot_card("tp_beta", q, '<card title="🐕 汇报 · 发版进度 [tp_beta]">\n已经切了 10%\n</card>')
        rep = self.say(thread_root=card, content="收到")
        self.pull(rep)
        self.assertIn("「🐕 汇报 · 发版进度 [tp_beta]」", self.row(rep)["note"])

    def test_owner_quotes_bot_card_is_instruction_with_plain_summary(self):
        q = self.say(content="构建挂了", at=now8() - timedelta(minutes=8))
        self.pull(q)
        card = self.bot_card("tp_beta", q, f"<card>\n正在排查 runner\n{MARK}\n</card>")
        mid = self.say(sender=ME, content="@Mattheuu 让cicd哥看一下", quote=card, mentions=[APP])
        self.pull(mid)
        r = self.row(mid)
        self.assertEqual((r["reason"], r["topic_id"]), ("owner_instruction", "tp_beta"))
        self.assertIn("「正在排查 runner」", r["note"])
        self.assertNotIn("<card", r["note"])

    def test_recent_items_in_long_thread_are_plain(self):
        root = self.root_to("tp_beta")
        for i in range(3):
            x = self.lark.add(chat_id=OPS, sender_id=APP, sender_type="app", chat_type="group", thread_root=root,
                              content=f"<card>\n机器人第 {i} 次回复\n{MARK}\n</card>", msg_type="interactive")
            self.pull(x)
        last = self.say(thread_root=root, content="人回的最新一条")
        self.pull(last)
        note = self.row(last)["note"]
        self.assertIn("机器人第 2 次回复", note)
        self.assertNotIn("<card", note)

    def test_owner_identity_reply_card_with_marker_is_not_instruction(self):
        mid = self.say(sender=ME, content=f"<card>\n@Mattheuu 代回的卡片\n{MARK}\n</card>", mentions=[APP])
        self.pull(mid)
        self.assertNotEqual(self.row(mid)["reason"], "owner_instruction")
