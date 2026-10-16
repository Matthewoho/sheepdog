"""7.51 长期跟踪的话题放进专门的话题群（改版 v1）。

假的飞书沿用 test_threads.FakeLark（按 10-12 22:5x 和 7.45 在真实飞书 / 正式账本上核过的格式），话题群这几样按 7.51 实现侧
10-16 只读核正式账本的结果补：
- 话题群（chat_type = topic）里每条顶层消息都是一个话题的开头：搜索 / mget 带 thread_id，开头那条 thread_message_position
  是 "-1"、回复从 "0" 起；不带 parent_id / root_id（正式账本 1563 条话题群消息，reply_to 全是空的）；
- message_app_link 是话题链接：https://applink.larksuite.com/client/thread/open?open_chat_id=…&open_thread_id=…&
  openchatid=…&openthreadid=…&thread_position=N（正式账本 5787 条话题消息都是这个样子）；
- 机器人实时事件（lark-cli event consume im.message.receive_v1）：chat_type 只有 p2p / group 两种（schema 的 enum），
  没有序号、没有群名；话题里的回复带 reply_to = 话题开头那条（正式账本私聊话题里机器人先收到的 179 条都带，指向开头）；
- 群成员（im +chat-members-list）：users[] 是 open_id（member_id），bots[] 带 app_id；
- 改卡（im messages patch）、置顶（im pins create）返回空对象。

A 三个入口：--track（第一次用这把钥匙）、私聊整句「放群里」、主人在群里新开顶层话题（交给总线、转交时编号带过去）。
B 群里的回复：没引用时抄送给每个有未答提问的会话、引用了哪条送哪条、一个未答的都没有送负责会话；机器人事件那一份同样送对。
C 整句命令：办完了 / 关掉 / 移出群 / 待办；不是整句的照常送。
D 钥匙归一和别名；归档后重开 / 超过 14 天算新事。
E 索引卡：按证据分栏、内容不变不改卡、变了整张覆盖、满 13 天重发并置顶；话题里只在三种变化时发一行。
F 保留期：事项、钥匙、它指向的私聊话题不跟 7 天走，归档满 30 天才删。
G 读回：牧羊犬自己在群里发的一律丢弃，不登记「需要你定」、不进 bot_outbox。
H 退路：群发不进去、成员核对发现机器人或主人不在 → 退回私聊加系统通知；恢复；有别人进群 / 别人说话 → 提醒、不分派。
I 开关：没设 chat_id 和以前一模一样；enabled = false；热加载；规则指纹没配时不变。
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import outbound
from sheepdog import topicgroup as tg
from sheepdog.config import TopicGroupConfig, load_config
from sheepdog.engine import BUS_TOPIC_ID, OWN_SEND_KINDS, Collector, Dispatcher
from sheepdog.playbook import Playbook
from sheepdog.prompts import interface_section
from sheepdog.roster import parse_roster
from sheepdog.source.lark import parse_event, parse_message
from sheepdog.store import Store

from test_roster import ME, roster_data
from test_threads import BOT, ESC_CHAT, TZ8, FakeLark, ThreadsBase, _opt, now8, sent_text

TG = "oc_test_topic_group"
STRANGER = "ou_test_stranger"
BOT_APP = "cli_test_bot"


class FakeLarkTG(FakeLark):
    """在 FakeLark 上补话题群的几样：真格式的话题链接、改卡、置顶、群成员。"""

    def __init__(self):
        super().__init__()
        self.chat_types[TG] = "topic"
        self.chat_names[TG] = "🐕 牧羊犬 · 长期跟踪"
        self.members = {"users": [ME], "bots": [BOT_APP]}
        self.patched: dict[str, list[dict]] = {}
        self.pins: list[str] = []
        self.unpins: list[str] = []

    def raw(self, mid: str) -> dict:
        out = super().raw(mid)
        m = self.msgs[mid]
        if m["thread_id"]:
            out["message_app_link"] = (f"https://applink.larksuite.com/client/thread/open?open_chat_id={m['chat_id']}"
                                       f"&open_thread_id={m['thread_id']}&openchatid={m['chat_id']}"
                                       f"&openthreadid={m['thread_id']}&thread_position={m['pos']}")
        else:
            out["message_app_link"] = (f"https://applink.larksuite.com/client/chat/open?openChatId={m['chat_id']}"
                                       f"&position={out.get('message_position', '1')}")
        if m.get("mentions"):   # 搜索 / mget 里 @ 机器人是 app_id（7.45 核过）
            out["mentions"] = [{"id": i, "key": f"@_user_{n}", "name": "x"} for n, i in enumerate(m["mentions"], 1)]
        return out

    def __call__(self, argv, capture_output=True, text=True, timeout=None):
        args = list(argv[1:])
        if args[:2] == ["im", "messages"] and args[2] == "patch":
            self.calls.append(args)
            mode = self.plan["patch"].pop(0) if self.plan.get("patch") else None
            if mode == "error":
                return self._out({"ok": False, "error": {"type": "api", "message": "假的失败"}}, 1)
            mid = json.loads(_opt(args, "--params"))["message_id"]
            card = json.loads(json.loads(_opt(args, "--data"))["content"])
            self.patched.setdefault(mid, []).append(card)
            return self._out({"ok": True, "data": {}})
        if args[:2] == ["im", "pins"]:
            self.calls.append(args)
            if args[2] == "create":
                self.pins.append(json.loads(_opt(args, "--data"))["message_id"])
            elif args[2] == "delete":
                self.unpins.append(json.loads(_opt(args, "--params"))["message_id"])
            return self._out({"ok": True, "data": {}})
        if args[:2] == ["im", "+chat-members-list"]:
            self.calls.append(args)
            if self.plan.get("members"):
                self.plan["members"].pop(0)
                return self._out({"ok": False, "error": {"type": "api", "message": "假的失败"}}, 1)
            return self._out({"ok": True, "data": {
                "chat_id": _opt(args, "--chat-id"),
                "users": [{"member_id": u, "member_id_type": "open_id", "name": "x"} for u in self.members["users"]],
                "bots": [{"app_id": b, "name": "牧羊犬"} for b in self.members["bots"]],
                "user_total": len(self.members["users"]), "bot_total": len(self.members["bots"])}})
        return super().__call__(argv, capture_output, text, timeout)

    def tg_sent(self) -> list[list[str]]:
        """往话题群发的（顶层 +messages-send --chat-id TG，或回到群里某条下面的）。"""
        out = []
        for c in self.sent():
            if c[1] == "+messages-send" and _opt(c, "--chat-id") == TG:
                out.append(c)
            elif c[1] == "+messages-reply" and self.msgs.get(_opt(c, "--message-id"), {}).get("chat_id") == TG:
                out.append(c)
        return out

    def event(self, mid: str, with_thread: bool = True) -> dict:
        """这条消息在机器人实时事件里的样子（拍平；chat_type 只有 group；话题回复的 reply_to 是开头那条；没有序号）。"""
        m = self.msgs[mid]
        raw = {"message_id": mid, "id": mid, "chat_id": m["chat_id"],
               "chat_type": "group" if m["chat_type"] in ("group", "topic") else m["chat_type"],
               "sender_id": m["sender"]["id"], "sender_type": "bot" if m["sender"]["sender_type"] == "app" else "user",
               "content": m["content"], "message_type": m["msg_type"],
               "create_time": str(int(m["at"].timestamp() * 1000)), "type": "im.message.receive_v1"}
        if m["thread_root"]:
            raw["reply_to"] = m["thread_root"]
            raw["root_id"] = m["thread_root"]
        if with_thread and m["thread_id"]:
            raw["thread_id"] = m["thread_id"]
        if m["thread_root"]:
            raw["root_id"] = m["thread_root"]
        if m.get("mentions"):
            raw["mentions"] = [{"id": i, "key": f"@_user_{n}", "name": "x"} for n, i in enumerate(m["mentions"], 1)]
        return raw


class TGBase(ThreadsBase):
    def setUp(self):
        super().setUp()
        self.lark = FakeLarkTG()
        p = mock.patch("sheepdog.source.lark.subprocess.run", self.lark)
        p.start()
        self.addCleanup(p.stop)
        # 第二个专属会话 beta（抄送要两个会话）
        data = roster_data()
        data["session"].append({"key": "beta", "mode": "managed", "conversation_id": "conv_test_beta",
                                "title": "Beta 发版", "duty": "跟 Beta 的发版", "chats": []})
        self.roster = parse_roster(data)
        self.d = self.disp()
        self.d.dispatch_once()
        self.store.update_topic("tp_beta", onboarded_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        self.cfg.topic_group = TopicGroupConfig(chat_id=TG)
        self.store.set_meta("identity_bot_app_id", BOT_APP)
        with self.cfg_file.open("a", encoding="utf-8") as f:
            f.write(f'[topic_group]\nchat_id = "{TG}"\n')
        self.d.source = self.source

    # ---------- 小工具 ----------
    def item(self, key: str):
        return tg.find_by_key(self.store, key)

    def owner_tg(self, text: str, root: str = "", at: datetime | None = None) -> str:
        """主人在话题群里说话：root 为空 = 新开顶层话题，否则在那个话题里回复。"""
        return self.lark.add(chat_id=TG, sender_id=ME, content=text, chat_type="topic", thread_root=root, at=at)

    def owner_dm_thread(self, root: str, text: str) -> str:
        return self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content=text, thread_root=root)

    def round(self):
        """常驻进程一轮：拉取 + 投递（含话题群这一轮）。"""
        self.poll()
        return self.d.dispatch_once()

    def batch_for(self, conv: str) -> str:
        got = self.sink.to(conv)
        return got[-1] if got else ""

    def sys_rows(self, topic_id: str, reason: str) -> list:
        return self.store.conn.execute("SELECT * FROM messages WHERE topic_id=? AND reason=? AND chat_type='sheepdog'",
                                       (topic_id, reason)).fetchall()


# ======================= A：三个入口 =======================
class EntryTest(TGBase):
    def test_track_first_use_opens_item_with_title_card_and_ask_in_thread(self):
        rc, out, err = self.ask("--key", "SSL-CERT-HANDOVER", "--track", title="证书交接", text="谁接手？A / B")
        self.assertEqual(rc, 0, err)
        self.assertIn("新开了事项 T-1", out)
        title, ask = self.lark.tg_sent()
        self.assertEqual((title[1], _opt(title, "--chat-id"), _opt(title, "--as")), ("+messages-send", TG, "bot"))
        self.assertTrue(sent_text(title).startswith("🗂 T-1 · 证书交接"), sent_text(title))
        card = json.loads(_opt(title, "--content"))
        self.assertTrue(card["header"]["subtitle"]["content"].startswith("钥匙 SSL-CERT-HANDOVER · Alpha 需求开的 · "))
        it = self.item("SSL-CERT-HANDOVER")
        self.assertEqual((it["origin"], it["owner_topic"], it["root_message_id"]), ("track", "tp_alpha",
                                                                                  self.lark.idem[_opt(title, "--idempotency-key")]))
        # 话题编号发出时不知道（返回里没有），mget 一次补上
        self.assertEqual(it["thread_id"], self.lark.msgs[it["root_message_id"]]["thread_id"])
        self.assertEqual((ask[1], _opt(ask, "--message-id")), ("+messages-reply", it["root_message_id"]))
        self.assertIn("--reply-in-thread", ask)
        self.assertTrue(sent_text(ask).startswith("🐕 需要你定 · 证书交接 [tp_alpha]"))
        self.assertIn(f"<at id={ME}></at>", sent_text(ask))      # 群里只有 ask @ 主人
        self.assertNotIn("<at", sent_text(title))
        kinds = [r[0] for r in self.store.conn.execute("SELECT kind FROM sends ORDER BY rowid")]
        self.assertEqual(kinds, ["tg_title", "ask"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM owner_threads").fetchone()[0], 0)  # 不进私聊
        # 同一把钥匙换个写法（-、_、空格不分），不带 --track：照样进群里这个话题；report 不 @
        rc, out, _ = self.ask("--key", "ssl cert_handover", kind="report", title="进度", text="A 接了")
        self.assertEqual(rc, 0)
        self.assertIn("发到了话题群里的事项 T-1", out)
        rep = self.lark.tg_sent()[-1]
        self.assertEqual(_opt(rep, "--message-id"), it["root_message_id"])
        self.assertNotIn("<at", sent_text(rep))
        # 事项编号本身也是钥匙
        rc, _, _ = self.ask("--key", "T-1", title="再问", text="定了吗")
        self.assertEqual(_opt(self.lark.tg_sent()[-1], "--message-id"), it["root_message_id"])
        # 读回：会话的 ask 照旧登记「需要你定」（chat = 话题群），标题卡不登记
        self.poll()
        esc = self.store.conn.execute("SELECT chat_id FROM escalations").fetchall()
        self.assertEqual([r[0] for r in esc], [TG, TG])
        self.assertEqual(self.row(it["root_message_id"])["reason"], "sheepdog_tg_title")

    def test_track_ignored_when_key_already_has_dm_thread(self):
        self.ask("--key", "PR #7")
        rc, out, _ = self.ask("--key", "PR #7", "--track", text="还是放群里吧")
        self.assertEqual(rc, 0)
        self.assertIn("--track 只在第一次用这把钥匙时有效", out)
        self.assertEqual(self.lark.tg_sent(), [])
        self.assertIsNone(self.item("PR #7"))

    def test_without_track_goes_to_dm_and_track_needs_key(self):
        self.ask("--key", "K9")
        self.assertEqual(self.lark.tg_sent(), [])
        self.assertIsNotNone(self.store.thread_by_key("tp_alpha", "K9"))
        rc, _, err = self.ask("--track")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertIn("--track 要和 --key 一起用", err)

    def test_owner_move_from_dm_thread(self):
        self.ask("--key", "VPS-3X-UI", title="VPS 面板", text="要不要换端口？")
        self.poll()
        root = self.store.thread_by_key("tp_alpha", "VPS-3X-UI")["root_message_id"]
        mid = self.owner_dm_thread(root, " 放群里 ")
        self.poll()
        r = self.row(mid)
        self.assertEqual((r["route"], r["reason"]), ("drop", "tg_move"))   # 在路由前拦下，不当普通回复送走
        it = self.item("vps_3x_ui")
        self.assertEqual((it["origin"], it["owner_topic"], it["dm_root_message_id"]), ("owner_move", "tp_alpha", root))
        title = self.lark.tg_sent()[0]
        self.assertIn(" · 你从私聊放进群 · ", json.loads(_opt(title, "--content"))["header"]["subtitle"]["content"])
        # 私聊旧话题里贴上链接（kind = tg_moved，读回丢弃）
        note = [c for c in self.lark.sent("+messages-reply") if _opt(c, "--message-id") == root][-1]
        self.assertIn("这件事已经放进话题群：T-1", sent_text(note))
        self.assertIn(f"open_chat_id={TG}", sent_text(note))
        # 告诉负责会话
        self.assertEqual(len(self.sys_rows("tp_alpha", "topic_group")), 1)
        self.assertIn("命令不用改", self.sys_rows("tp_alpha", "topic_group")[0]["content"])
        # 之后同一把钥匙的消息发进群；旧话题里的回复照 7.22 送回会话
        self.ask("--key", "VPS-3X-UI", kind="report", title="换了", text="已换 8443")
        self.assertEqual(_opt(self.lark.tg_sent()[-1], "--message-id"), it["root_message_id"])
        old = self.owner_dm_thread(root, "端口别用 8443")
        self.poll()
        self.assertEqual((self.row(old)["reason"], self.row(old)["topic_id"]), ("owner_reply", "tp_alpha"))
        # 机器人那句「已经放进话题群」读回：丢弃，不当会话的消息
        moved = self.store.conn.execute("SELECT message_id FROM sends WHERE kind='tg_moved'").fetchone()[0]
        self.assertEqual(self.row(moved)["route"], "drop")
        self.assertIsNone(self.store.get_bot_outbox(moved))

    def test_owner_move_by_quote_and_without_target(self):
        self.ask("--key", "LIC-1", text="续费吗")
        self.poll()
        root = self.store.thread_by_key("tp_alpha", "LIC-1")["root_message_id"]
        q = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="放群里")
        self.lark.msgs[q]["reply_to"] = root
        self.pull_with_reply(q, root)
        self.assertIsNotNone(self.item("LIC-1"))
        # 顶层整句「放群里」、没引用：不知道放哪件，回一句提示
        lone = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="放群里")
        self.poll()
        self.assertEqual(self.row(lone)["reason"], "tg_move")
        hint = [c for c in self.lark.sent("+messages-reply") if _opt(c, "--message-id") == lone]
        self.assertIn("要把哪件事放进群", sent_text(hint[0]))
        self.assertEqual(len(self.store.list_items((tg.ACTIVE,))), 1)

    def pull_with_reply(self, mid: str, reply_to: str):
        raw = self.lark.raw(mid)
        raw["parent_id"] = reply_to
        return Collector(self.cfg, self.store, self.source, self.roster).ingest([parse_message(raw)])

    def test_owner_new_topic_goes_to_bus_then_forward_hands_off(self):
        mid = self.owner_tg("帮我盯一下 B2B 白名单的进度\n下周要给客户")
        self.assertEqual(self.lark.raw(mid)["thread_message_position"], "-1")   # 话题群开头 -1
        self.poll()
        r = self.row(mid)
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), ("dispatch", "owner_reply", BUS_TOPIC_ID))
        it = self.store.item_by_root(mid)
        self.assertEqual((it["origin"], it["owner_topic"], tg.label(it["item_no"])), ("owner_topic", BUS_TOPIC_ID, "T-1"))
        self.assertIn("事项 T-1", r["note"])
        self.assertIn("forward --topic <会话>", r["note"])
        batch = self.d.dispatch_once()
        self.assertIn("T-1", "\n".join(self.sink.to(self.store.get_topic(BUS_TOPIC_ID)["conversation_id"])))
        # 总线转交给 tp_alpha：负责会话改、编号带过去，话题里说一句「已交给 …」
        from sheepdog.engine import forward_messages
        forward_messages(self.store, "tp_alpha", [mid], self_open_id=ME)
        it = self.store.get_item(it["item_no"])
        self.assertEqual((it["owner_topic"], it["handoff_to"]), ("tp_alpha", "tp_alpha"))
        relay = self.sys_rows("tp_alpha", "topic_group")
        self.assertIn("--key T-1", relay[0]["content"])
        self.assertNotIn("escalation:", self.row(mid)["tags_json"])   # 主人开的话题是指令，不当成回答哪条提问
        self.d.dispatch_once()
        hand = [c for c in self.lark.tg_sent() if "已交给" in sent_text(c)]
        self.assertEqual(len(hand), 1)
        self.assertEqual(_opt(hand[0], "--message-id"), mid)
        self.assertIsNone(self.store.get_item(it["item_no"])["handoff_to"])
        # 会话用编号当钥匙汇报：回到主人开的那个话题
        rc, out, _ = self.ask("--key", "T-1", kind="report", title="白名单", text="提了工单")
        self.assertEqual(rc, 0)
        self.assertEqual(_opt(self.lark.tg_sent()[-1], "--message-id"), mid)


# ======================= B：群里的回复怎么送 =======================
class GroupReplyTest(TGBase):
    def two_asks(self):
        self.ask("--key", "REL-1", "--track", title="发版", text="今晚发吗？")
        self.ask("--key", "REL-1", topic="tp_beta", title="回滚", text="回滚窗口？")
        self.poll()
        it = self.item("REL-1")
        asks = [r[0] for r in self.store.conn.execute("SELECT message_id FROM sends WHERE kind='ask' ORDER BY rowid")]
        return it, asks

    def test_no_quote_cc_every_session_with_open_ask(self):
        it, (a1, a2) = self.two_asks()
        mid = self.owner_tg("今晚发，窗口 30 分钟", root=it["root_message_id"])
        self.assertEqual(self.lark.raw(mid)["thread_message_position"], "2")
        self.poll()
        r = self.row(mid)
        tags = json.loads(r["tags_json"])
        self.assertEqual((r["reason"], r["topic_id"]), ("owner_reply", "tp_beta"))   # 最近一条提问的会话拿原件
        self.assertIn("escalation:" + a2, tags)
        self.assertNotIn("escalation:" + a1, tags)
        cc = self.store.conn.execute("SELECT * FROM messages WHERE topic_id='tp_alpha' AND reason='owner_reply' "
                                     "AND chat_type='sheepdog'").fetchall()
        self.assertEqual(len(cc), 1)
        self.assertIn("抄送", cc[0]["content"])
        self.assertIn("escalation:" + a1, json.loads(cc[0]["tags_json"]))
        self.assertIn("tp_alpha", r["note"])
        # 投递以后两个会话各自关掉自己的那条（插队送到）
        self.d.dispatch_once()
        self.assertEqual({e["message_id"]: e["close_reason"] for e in self.store.conn.execute(
            "SELECT message_id, close_reason FROM escalations")}, {a1: "answered", a2: "answered"})
        self.assertIn("今晚发，窗口 30 分钟", self.batch_for("conv_test_beta"))
        self.assertIn("抄送", self.batch_for("conv_test_alpha"))

    def test_quote_goes_only_to_quoted_session(self):
        it, (a1, a2) = self.two_asks()
        mid = self.owner_tg("发", root=it["root_message_id"])
        raw = self.lark.raw(mid)
        raw["parent_id"] = a1      # 引用了 alpha 那条
        Collector(self.cfg, self.store, self.source, self.roster).ingest([parse_message(raw)])
        r = self.row(mid)
        self.assertEqual(r["topic_id"], "tp_alpha")
        self.assertEqual([t for t in json.loads(r["tags_json"]) if t.startswith("escalation:")], ["escalation:" + a1])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_type='sheepdog' "
                                                 "AND reason='owner_reply'").fetchone()[0], 0)

    def test_no_open_ask_goes_to_owner_session(self):
        self.ask("--key", "OPS-2", "--track", kind="report", title="巡检", text="正常")
        self.poll()
        it = self.item("OPS-2")
        mid = self.owner_tg("好的，下周再看", root=it["root_message_id"])
        self.poll()
        self.assertEqual((self.row(mid)["reason"], self.row(mid)["topic_id"]), ("owner_reply", "tp_alpha"))
        # 负责会话收掉了：送总线
        self.store.update_item(it["item_no"], owner_topic="tp_gone")
        mid2 = self.owner_tg("还在吗", root=it["root_message_id"])
        self.poll()
        self.assertEqual(self.row(mid2)["topic_id"], BUS_TOPIC_ID)

    def test_bot_event_copy_routes_the_same(self):
        it, (a1, a2) = self.two_asks()
        mid = self.owner_tg("今晚发", root=it["root_message_id"])
        ev = parse_event(self.lark.event(mid))
        self.assertEqual((ev.chat_type, ev.thread_position, ev.reply_to), ("group", "", it["root_message_id"]))
        c = Collector(self.cfg, self.store, self.source, self.roster)
        c.ingest([ev], received_by="bot")
        r = self.row(mid)
        self.assertEqual((r["reason"], r["topic_id"]), ("owner_reply", "tp_beta"))   # 回复对象是开头那条 = 没引用
        self.assertEqual(len(self.store.conn.execute("SELECT * FROM messages WHERE topic_id='tp_alpha' "
                                                     "AND chat_type='sheepdog' AND reason='owner_reply'").fetchall()), 1)

    def test_bot_event_without_thread_id_still_finds_item_by_root(self):
        it, _ = self.two_asks()
        mid = self.owner_tg("今晚发", root=it["root_message_id"])
        self.store.update_item(it["item_no"], thread_id=None)
        ev = parse_event(self.lark.event(mid, with_thread=False))
        Collector(self.cfg, self.store, self.source, self.roster).ingest([ev], received_by="bot")
        self.assertEqual(self.row(mid)["reason"], "owner_reply")
        self.assertIn("item:T-1", self.row(mid)["tags_json"])

    def test_unknown_thread_item_found_by_mget(self):
        it, _ = self.two_asks()
        self.store.update_item(it["item_no"], thread_id=None)
        # 账本里还没有这个话题的任何消息（开头那条没拉到）：mget 事项开头那条认出话题
        self.store.conn.execute("DELETE FROM messages WHERE chat_id=?", (TG,))
        self.store.conn.commit()
        mid = self.owner_tg("发", root=it["root_message_id"])
        self.pull(mid)
        self.assertIn("item:T-1", self.row(mid)["tags_json"])
        self.assertIn("+messages-mget", self.lark.subcommands())
        self.assertEqual(self.store.get_item(it["item_no"])["thread_id"], self.lark.msgs[mid]["thread_id"])

    def test_index_thread_reply_with_number_goes_to_owner(self):
        it, _ = self.two_asks()
        self.d.dispatch_once()   # 发索引卡
        idx = tg.index_info(self.store)["message_id"]
        mid = self.owner_tg("t-1 先别发", root=idx)
        self.poll()
        r = self.row(mid)
        self.assertEqual((r["reason"], r["topic_id"]), ("owner_reply", "tp_alpha"))   # 负责会话，不抄送、不猜
        self.assertNotIn("escalation:", r["tags_json"])
        mid2 = self.owner_tg("这些都不急", root=idx)
        self.poll()
        self.assertEqual((self.row(mid2)["route"], self.row(mid2)["reason"]), ("drop", "tg_index_reply"))
        hint = [c for c in self.lark.sent("+messages-reply") if _opt(c, "--message-id") == mid2]
        self.assertIn("写上它的编号", sent_text(hint[0]))


# ======================= C：整句命令 =======================
class CommandTest(TGBase):
    def setUp(self):
        super().setUp()
        self.ask("--key", "B2B-V2", "--track", title="白名单", text="开哪几个？")
        self.poll()
        self.it = self.item("B2B-V2")

    def test_done_and_close_are_whole_sentence(self):
        not_cmd = self.owner_tg("办完了吗？", root=self.it["root_message_id"])
        self.poll()
        self.assertEqual(self.row(not_cmd)["reason"], "owner_reply")
        self.assertEqual(self.store.get_item(self.it["item_no"])["state"], "active")
        mid = self.owner_tg(" 办完了 ", root=self.it["root_message_id"])
        self.poll()
        self.assertEqual((self.row(mid)["route"], self.row(mid)["reason"]), ("drop", "tg_done"))
        it = self.store.get_item(self.it["item_no"])
        self.assertEqual((it["state"], it["done_by"]), ("done", "owner"))
        self.assertIn("标成办完了", self.sys_rows("tp_alpha", "topic_group")[-1]["content"])
        # 「关掉」同样算
        self.ask("--key", "B2B-V2", title="再问", text="?")
        self.assertEqual(self.store.get_item(self.it["item_no"])["state"], "active")   # 同一把钥匙再发：重开
        mid2 = self.owner_tg("关掉", root=self.it["root_message_id"])
        self.poll()
        self.assertEqual(self.row(mid2)["reason"], "tg_done")

    def test_remove_from_group_then_dm(self):
        mid = self.owner_tg("移出群", root=self.it["root_message_id"])
        self.poll()
        self.assertEqual(self.row(mid)["reason"], "tg_remove")
        self.assertEqual(self.store.get_item(self.it["item_no"])["state"], "removed")
        self.assertIn("已移出话题群", sent_text(self.lark.tg_sent()[-1]))
        n = len(self.lark.tg_sent())
        rc, out, _ = self.ask("--key", "B2B-V2", kind="report", title="之后", text="私聊里说")
        self.assertEqual(len(self.lark.tg_sent()), n)
        self.assertIsNotNone(self.store.thread_by_key("tp_alpha", "B2B-V2"))

    def test_pending_in_group_points_to_index(self):
        mid = self.owner_tg("待办")
        self.poll()
        self.assertEqual((self.row(mid)["route"], self.row(mid)["reason"]), ("drop", "pending_query"))
        self.assertEqual(len(self.store.list_items((tg.ACTIVE,))), 1)   # 没另开事项
        self.assertIn("看置顶的索引卡", sent_text(self.lark.tg_sent()[-1]))

    def test_track_done_command(self):
        rc, out, err = self.cli("track", "done", "--topic", "tp_alpha", "--key", "b2b v2", "--why", "白名单已开")
        self.assertEqual(rc, 0, err)
        it = self.store.get_item(self.it["item_no"])
        self.assertEqual((it["state"], it["done_by"], it["done_why"]), ("done", "tp_alpha", "白名单已开"))
        self.d.dispatch_once()
        lines = [sent_text(c) for c in self.lark.tg_sent() if _opt(c, "--message-id") == self.it["root_message_id"]]
        self.assertTrue(any(x.startswith("✅ 办完了：白名单已开（Alpha 需求）") for x in lines), lines)
        rc, _, err = self.cli("track", "done", "--topic", "tp_alpha", "--key", "NOPE", "--why", "x")
        self.assertEqual(rc, outbound.EXIT_INVALID)


# ======================= D：钥匙、别名、重开 =======================
class KeyTest(TGBase):
    def test_track_norm(self):
        for a, b in (("SSL-CERT-HANDOVER", "ssl_cert handover"), ("VPS-3X-UI", "vps 3x_ui"), ("T-12", "t12")):
            self.assertEqual(tg.track_norm(a), tg.track_norm(b))
        self.assertEqual(tg.parse_item_no("t_12"), 12)
        self.assertIsNone(tg.parse_item_no("TT-12"))

    def test_alias(self):
        self.ask("--key", "SSL-CERT", "--track", text="?")
        rc, out, err = self.cli("track", "alias", "--topic", "tp_beta", "--item", "T-1", "--key", "SSL证书")
        self.assertEqual(rc, 0, err)
        self.ask("--key", "SSL证书", topic="tp_beta", kind="report", title="证书", text="续上了")
        self.assertEqual(_opt(self.lark.tg_sent()[-1], "--message-id"), self.item("T-1")["root_message_id"])
        self.ask("--key", "OTHER", "--track", text="?")
        rc, _, err = self.cli("track", "alias", "--topic", "tp_beta", "--item", "T-2", "--key", "ssl_cert")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertIn("已经是 T-1 的钥匙", err)

    def test_archive_then_reopen_within_14_days_and_new_item_after(self):
        self.ask("--key", "DEP-9", "--track", kind="report", title="部署", text="开始")
        it = self.item("DEP-9")
        tg.close(self.store, it, "tp_alpha", "部署完了")
        later = datetime.now().astimezone() + timedelta(hours=73)
        with mock.patch("sheepdog.engine._now", return_value=later):
            self.d.dispatch_once()
        it = self.store.get_item(it["item_no"])
        self.assertEqual(it["state"], "archived")
        # 14 天内又用到：重开原来那件，话题里发「重开」
        self.ask("--key", "DEP-9", kind="report", title="又出问题", text="回滚")
        it = self.store.get_item(it["item_no"])
        self.assertEqual((it["state"], tg.label(it["item_no"])), ("active", "T-1"))
        self.d.dispatch_once()
        self.assertTrue(any(sent_text(c) == "🔁 重开" for c in self.lark.tg_sent()))
        # 归档超过 14 天又用到：新的一件，标题卡附「可能相关」
        tg.close(self.store, it, "tp_alpha", "好了")
        old = (datetime.now().astimezone() - timedelta(days=15)).isoformat(timespec="seconds")
        self.store.update_item(it["item_no"], state="archived", archived_at=old)
        rc, out, _ = self.ask("--key", "DEP-9", "--track", kind="report", title="新一轮部署", text="开始")
        self.assertIn("新开了事项 T-2", out)
        new = self.item("DEP-9")
        self.assertEqual((new["item_no"], new["related_item"]), (2, 1))
        title = [c for c in self.lark.tg_sent() if sent_text(c).startswith("🗂 T-2")][0]
        self.assertIn("可能相关：T-1", sent_text(title))


# ======================= E：索引卡和变化说明 =======================
class IndexTest(TGBase):
    def card_text(self, card: dict) -> str:
        title = ((card.get("header") or {}).get("title") or {}).get("content", "")
        return "\n".join([title] + [e.get("content", "") for e in card["body"]["elements"]])

    def test_index_posted_pinned_patched_and_renewed(self):
        self.ask("--key", "A-1", "--track", title="要你定的", text="?")
        self.poll()              # 读回登记「需要你定」
        self.d.dispatch_once()
        info = tg.index_info(self.store)
        idx = info["message_id"]
        self.assertEqual(self.lark.pins, [idx])
        sent = [c for c in self.lark.tg_sent() if _opt(c, "--message-id") == "" and "📌" in sent_text(c)]
        self.assertEqual(len(sent), 1)
        self.assertEqual(_opt(sent[0], "--msg-type"), "interactive")
        text = sent_text(sent[0])
        self.assertIn("**等你定 · 1 件**", text)
        self.assertIn("**T-1 · 要你定的** [打开](https://applink.larksuite.com/client/thread/open?open_chat_id=" + TG, text)
        # 内容没变：不改卡（同一时刻再对账一次；「已等多久」这类时间变了才算变）
        fixed = datetime.now().astimezone()
        with mock.patch("sheepdog.engine._now", return_value=fixed):
            tg.mark_dirty(self.store)
            self.d.dispatch_once()
            first = len(self.lark.patched.get(idx, []))
            tg.mark_dirty(self.store)
            self.d.dispatch_once()
        self.assertEqual(len(self.lark.patched.get(idx, [])), first)
        self.lark.patched.clear()
        # 状态变了（会话结案那条提问 → 进行中）：整张覆盖
        self.poll()
        e = self.store.conn.execute("SELECT message_id FROM escalations").fetchone()[0]
        self.store.resolve_escalation(e, "tp_alpha", "主人别处答了")
        tg.mark_dirty(self.store)
        self.d.dispatch_once()
        self.assertEqual(len(self.lark.patched[idx]), 1)
        self.assertIn("**进行中 · 1 件**", self.card_text(self.lark.patched[idx][0]))
        self.assertEqual(self.lark.patched[idx][0]["header"]["template"], "blue")    # 没有等你定的：蓝色
        # 满 13 天：重发一张、置顶新的，旧卡改成「已停用」，默认不取消旧卡置顶（要 --yes）
        info = tg.index_info(self.store)
        info["sent_at"] = (datetime.now().astimezone() - timedelta(days=13, minutes=1)).isoformat(timespec="seconds")
        self.store.set_meta(tg.META_INDEX, json.dumps(info))
        tg.mark_dirty(self.store)
        self.d.dispatch_once()
        new = tg.index_info(self.store)["message_id"]
        self.assertNotEqual(new, idx)
        self.assertEqual(self.lark.pins, [idx, new])
        self.assertIn("已停用", self.card_text(self.lark.patched[idx][-1]))
        self.assertEqual(self.lark.unpins, [])
        # 打开 unpin_old_index 才取消旧卡置顶（带 --yes）
        self.cfg.topic_group.unpin_old_index = True
        info = tg.index_info(self.store)
        info["sent_at"] = (datetime.now().astimezone() - timedelta(days=14)).isoformat(timespec="seconds")
        self.store.set_meta(tg.META_INDEX, json.dumps(info))
        tg.mark_dirty(self.store)
        self.d.dispatch_once()
        self.assertEqual(self.lark.unpins, [new])
        self.assertIn("--yes", [c for c in self.lark.calls if c[:3] == ["im", "pins", "delete"]][0])

    def test_sections_by_evidence(self):
        cfg, st = self.cfg, self.store
        self.ask("--key", "DECIDE", "--track", title="等你定的", text="?")
        self.ask("--key", "DOING", "--track", kind="report", title="在做的", text="进行中")
        self.ask("--key", "WAIT", "--track", kind="report", title="等别人的", text="等法务")
        self.ask("--key", "SLEEP", "--track", kind="report", title="睡着的", text="..")
        self.ask("--key", "EXPIRED", "--track", title="过期的", text="?")
        self.ask("--key", "DONE", "--track", kind="report", title="办完的", text="..")
        self.poll()
        self.cli("work", "set", "--topic", "tp_alpha", "--key", "WAIT", "--title", "等法务", "--state", "waiting_other",
                 "--waiting-on", "法务")
        st.update_item(self.item("SLEEP")["item_no"],
                       last_activity_at=(datetime.now().astimezone() - timedelta(days=8)).isoformat(timespec="seconds"))
        exp = st.conn.execute("SELECT s.message_id FROM sends s WHERE s.kind='ask' AND s.key='EXPIRED'").fetchone()[0]
        st.expire_escalation(exp)
        tg.close(st, self.item("DONE"), "tp_alpha", "交付了")
        data = tg.build_index(cfg, st)
        got = {k: [x["title"] for x in v] for k, v in data["sections"].items()}
        self.assertEqual(got, {"decide": ["等你定的"], "expired": ["过期的"], "doing": ["在做的"], "waiting_other": ["等别人的"],
                               "sleeping": ["睡着的"], "done": ["办完的"]})
        self.assertEqual(data["per_session"], {"Alpha 需求": 5})
        text = tg.render_index(cfg, data)
        for word in ("等你定 · 1 件", "过期未定 · 1 件", "进行中 · 1 件", "等别人 · 1 件", "沉睡 · 1 件", "最近办完 · 1 件",
                     "各会话跟踪：Alpha 需求 5", "Alpha 需求 · 最近动静 8 天前", "已过期没答", "办完"):
            self.assertIn(word, text)
        self.assertNotIn("建议放进群", text)                                     # 没有建议时那一栏不出现
        # 话题里只在三种变化时发一行：等你定、办完、重开（进行中、等别人、沉睡、过期不发）
        self.d.dispatch_once()
        lines = [r[0] for r in st.conn.execute("SELECT text FROM sends WHERE kind='tg_change' ORDER BY rowid")]
        self.assertEqual(sorted(x.split("：")[0] for x in lines), ["✅ 办完了", "🟠 等你定"])
        n = len(self.lark.tg_sent())
        self.d.dispatch_once()   # 状态没变：不再发
        self.assertEqual(len([c for c in self.lark.tg_sent()[n:] if c[1] == "+messages-reply"]), 0)

    def test_suggest_column(self):
        # 私聊里长期在跟的：主人回过 2 次、或拖了 8 小时以上；两个会话用同一把钥匙不算（10-16 18:0x 收紧）
        self.ask("--key", "LONG-1", text="?")
        self.ask("--key", "long_1", topic="tp_beta", text="?")
        self.ask("--key", "SHORT", text="?")
        self.poll()
        self.assertEqual(tg.build_index(self.cfg, self.store)["suggest"], [])
        root = self.store.thread_by_key("tp_alpha", "SHORT")["root_message_id"]
        self.owner_dm_thread(root, "a")
        self.poll()
        self.assertEqual(tg.build_index(self.cfg, self.store)["suggest"], [])       # 回过 1 次不够
        self.owner_dm_thread(root, "b")
        self.poll()
        sug = tg.build_index(self.cfg, self.store)["suggest"]
        self.assertEqual([s["norm_key"] for s in sug], ["SHORT"])
        self.assertEqual(sug[0]["why"], ["你回过 2 次"])
        # 放进群以后不再建议
        self.owner_dm_thread(root, "放群里")
        self.poll()
        self.assertEqual(tg.build_index(self.cfg, self.store)["suggest"], [])

    def age_thread(self, root: str, hours: float, created_hours: float | None = None) -> None:
        """把这个私聊话题整个挪到 hours 小时以前（话题表、会话发的、主人在里面的回复）；created_hours 单独把开出来的时间挪更早。"""
        now = datetime.now().astimezone()
        at = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
        made = (now - timedelta(hours=created_hours if created_hours is not None else hours)).isoformat(timespec="seconds")
        thr = self.store.thread_by_root(root)
        c = self.store.conn
        c.execute("UPDATE owner_threads SET created_at=?, last_post_at=? WHERE root_message_id=?", (made, at, root))
        c.execute("UPDATE sends SET sent_at=? WHERE root_message_id=?", (at, root))
        if thr["thread_id"]:
            c.execute("UPDATE messages SET create_time=? WHERE thread_id=?", (at, thr["thread_id"]))
        c.commit()

    def test_suggest_span_and_active_window(self):
        g = self.cfg.topic_group
        self.assertEqual((g.suggest_after_hours, g.suggest_owner_replies, g.suggest_active_hours, g.suggest_max),
                         (8, 2, 24, 10))
        self.ask("--key", "SPAN", text="?")
        self.poll()
        root = self.store.thread_by_key("tp_alpha", "SPAN")["root_message_id"]
        self.age_thread(root, hours=1, created_hours=9)           # 开出来 9 小时、1 小时前还有动静：拖过 8 小时
        sug = tg.build_index(self.cfg, self.store)["suggest"]
        self.assertEqual([s["norm_key"] for s in sug], ["SPAN"])
        self.assertEqual(sug[0]["why"], ["拖了 8 小时"])
        self.age_thread(root, hours=25, created_hours=40)         # 25 小时没动静：不列
        self.assertEqual(tg.build_index(self.cfg, self.store)["suggest"], [])
        # 主人回过 2 次、回复也都在 24 小时以前：照样不列
        self.owner_dm_thread(root, "a")
        self.owner_dm_thread(root, "b")
        self.poll()
        self.age_thread(root, hours=25, created_hours=40)
        self.assertEqual(tg.build_index(self.cfg, self.store)["suggest"], [])
        # 主人刚在里面回了一句：最近动静算主人的回复，重新列进来
        self.owner_dm_thread(root, "c")
        self.poll()
        self.assertEqual([s["norm_key"] for s in tg.build_index(self.cfg, self.store)["suggest"]], ["SPAN"])
        # 热加载：窗口改大，25 小时以前的也算
        g.suggest_active_hours = 48
        self.age_thread(root, hours=25, created_hours=40)
        self.assertEqual([s["norm_key"] for s in tg.build_index(self.cfg, self.store)["suggest"]], ["SPAN"])

    def test_suggest_at_most_n_newest_first(self):
        keys = [f"K{i:02d}" for i in range(12)]
        for i, k in enumerate(keys):
            self.ask("--key", k, text="?")
        self.poll()
        for i, k in enumerate(keys):
            root = self.store.thread_by_key("tp_alpha", k)["root_message_id"]
            self.age_thread(root, hours=12 - i, created_hours=20)   # K11 最新
        data = tg.build_index(self.cfg, self.store)
        self.assertEqual([s["key"] for s in data["suggest"]], list(reversed(keys)))
        v = tg.index_view(self.cfg, data)
        self.assertEqual([x["meta"].split(" · ")[1] for x in v["suggest"]], [f"钥匙 {k}" for k in reversed(keys)][:10])
        self.assertEqual((v["n_suggest"], v["more_suggest"]), (12, 2))
        text = tg.index_text(v)
        self.assertIn("建议放进群 · 12 件\n" + tg.SUGGEST_HOW, text)
        self.assertIn("- 另有 2 件", text)
        self.assertIn("另有 2 件", json.dumps(tg.index_card(v), ensure_ascii=False))
        # 热加载：改成最多 3 件
        self.cfg.topic_group.suggest_max = 3
        v = tg.index_view(self.cfg, tg.build_index(self.cfg, self.store))
        self.assertEqual((len(v["suggest"]), v["more_suggest"]), (3, 9))


# ======================= F：保留期 =======================
class RetentionTest(TGBase):
    def test_items_keys_and_dm_thread_survive_7_day_prune(self):
        self.ask("--key", "KEEP", text="?")
        self.poll()
        root = self.store.thread_by_key("tp_alpha", "KEEP")["root_message_id"]
        self.owner_dm_thread(root, "放群里")
        self.ask("--key", "GONE", text="?")   # 没进群的私聊话题照旧清
        self.poll()
        old = (datetime.now().astimezone() - timedelta(days=9)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE owner_threads SET created_at=?, last_post_at=?", (old, old))
        self.store.conn.execute("UPDATE tracked_items SET opened_at=?, last_activity_at=?", (old, old))
        self.store.conn.execute("UPDATE tracked_keys SET added_at=?", (old,))
        self.store.conn.commit()
        out = self.store.prune(7)
        self.assertEqual(out["tracked_items"], 0)
        self.assertIsNotNone(self.store.thread_by_root(root))
        self.assertIsNone(self.store.thread_by_key("tp_alpha", "GONE"))
        self.assertIsNotNone(self.item("KEEP"))
        self.assertEqual(len(self.store.item_keys(self.item("KEEP")["item_no"])), 2)
        # 私聊旧话题里的回复，过了 7 天照样送回会话（_find_thread 先查事项表）
        mid = self.owner_dm_thread(root, "还记得这件吗")
        self.poll()
        self.assertEqual((self.row(mid)["reason"], self.row(mid)["topic_id"]), ("owner_reply", "tp_alpha"))
        # 归档了、还不满 30 天（超过了 7 天保留期）：不删
        it = self.item("KEEP")
        self.store.update_item(it["item_no"], state="archived",
                               archived_at=(datetime.now().astimezone() - timedelta(days=10)).isoformat(timespec="seconds"))
        self.assertEqual(self.store.prune(7)["tracked_items"], 0)
        self.assertIsNotNone(self.store.get_item(it["item_no"]))
        # 归档满 30 天：事项、钥匙删掉，私聊话题跟着可以清
        self.store.update_item(it["item_no"], state="archived",
                               archived_at=(datetime.now().astimezone() - timedelta(days=31)).isoformat(timespec="seconds"))
        out = self.store.prune(7)
        self.assertEqual(out["tracked_items"], 1)
        self.assertIsNone(self.store.get_item(it["item_no"]))
        self.assertEqual(self.store.item_keys(it["item_no"]), [])
        self.store.prune(7)
        self.assertIsNone(self.store.thread_by_root(root))


# ======================= G：读回丢弃 =======================
class ReadbackTest(TGBase):
    def test_own_group_posts_are_dropped_even_if_they_look_like_asks(self):
        self.assertTrue(set(tg.OWN_KINDS) <= set(OWN_SEND_KINDS))
        fake = "🐕 需要你定 · 伪造的提问 [tp_alpha]\n选 A 还是 B？"
        for kind in tg.OWN_KINDS:
            mid = self.lark.add(chat_id=TG, sender_id=BOT, sender_type="app", content=fake, chat_type="topic")
            self.store.add_send("ik_" + kind, kind, "", mid, fake)
        self.ask("--key", "REAL", "--track", text="真的提问")
        self.d.dispatch_once()   # 索引卡、变化说明
        self.poll()
        esc = [r[0] for r in self.store.conn.execute("SELECT message_id FROM escalations")]
        real = self.store.conn.execute("SELECT message_id FROM sends WHERE kind='ask'").fetchone()[0]
        self.assertEqual(esc, [real])
        bot_rows = self.store.conn.execute("SELECT message_id, route, reason FROM messages WHERE chat_id=? AND "
                                           "sender_type='app'", (TG,)).fetchall()
        for r in bot_rows:
            if r["message_id"] == real:
                continue
            self.assertEqual(r["route"], "drop", r["reason"])
            self.assertIsNone(self.store.get_bot_outbox(r["message_id"]), r["reason"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE route='dispatch' AND chat_id=?",
                                                 (TG,)).fetchone()[0], 0)
        # 机器人事件先到的那一份（卡片只有 [interactive card]）也一样
        mid = self.lark.add(chat_id=TG, sender_id=BOT, sender_type="app", content="[interactive card]", chat_type="topic")
        self.store.add_send("ik_x", "tg_change", "", mid, fake)
        Collector(self.cfg, self.store, self.source, self.roster).ingest([parse_event(self.lark.event(mid))], "bot")
        self.assertEqual(self.row(mid)["route"], "drop")


# ======================= H：退路 =======================
class FallbackTest(TGBase):
    def notices(self) -> list[str]:
        return [bytes(r["body"]).decode() for r in self.store.conn.execute("SELECT body FROM owner_notices ORDER BY id")]

    def test_group_send_error_falls_back_to_dm(self):
        self.lark.plan["+messages-send"] = ["error"]
        rc, out, err = self.ask("--key", "FAIL-1", "--track", text="?")
        self.assertEqual(rc, 0, err)
        self.assertIn("话题群发不进去", out)
        self.assertIsNone(self.item("FAIL-1"))                       # 没发成的事项删掉
        self.assertIsNotNone(self.store.thread_by_key("tp_alpha", "FAIL-1"))   # 这条进了私聊
        self.assertIsNotNone(tg.down_info(self.store))
        self.assertIn("话题群不可用", self.notices()[-1])
        # 退回期间：带 --track 的也进私聊，提示一句
        n = len(self.lark.tg_sent())   # 刚才失败的那一次（标题卡）也算调用过
        rc, out, _ = self.ask("--key", "FAIL-2", "--track", text="?")
        self.assertIn("话题群暂时不可用", out)
        self.assertEqual(len(self.lark.tg_sent()), n)
        # 成员核对正常：恢复，发一条系统通知
        self.d.dispatch_once()
        self.assertIsNone(tg.down_info(self.store))
        self.assertIn("话题群恢复了", self.notices()[-1])

    def test_member_check_bot_or_owner_missing(self):
        self.lark.members = {"users": [], "bots": [BOT_APP]}
        self.d.dispatch_once()
        self.assertIn("没有主人", tg.down_info(self.store)["why"])
        self.ask("--key", "X-1", "--track", text="?")
        self.assertEqual(self.lark.tg_sent(), [])                     # 退回私聊
        # 10 分钟内不再查；查询本身失败不改状态
        self.lark.members = {"users": [ME], "bots": []}
        self.d.dispatch_once()
        self.assertIn("没有主人", tg.down_info(self.store)["why"])
        later = datetime.now().astimezone() + timedelta(minutes=11)
        self.lark.plan["members"] = ["error"]
        with mock.patch("sheepdog.engine._now", return_value=later):
            self.d.dispatch_once()
        self.assertIsNotNone(tg.down_info(self.store))
        with mock.patch("sheepdog.engine._now", return_value=later + timedelta(minutes=11)):
            self.d.dispatch_once()
        self.assertIsNotNone(tg.down_info(self.store))                # 机器人不在：还是退回
        self.lark.members = {"users": [ME], "bots": [BOT_APP]}
        with mock.patch("sheepdog.engine._now", return_value=later + timedelta(minutes=22)):
            self.d.dispatch_once()
        self.assertIsNone(tg.down_info(self.store))

    def test_stranger_joins_or_speaks(self):
        self.lark.members = {"users": [ME, STRANGER], "bots": [BOT_APP]}
        self.d.dispatch_once()
        self.assertIn(STRANGER, self.notices()[-1])
        self.assertIsNone(tg.down_info(self.store))
        mid = self.lark.add(chat_id=TG, sender_id=STRANGER, content="我也来说两句", chat_type="topic")
        self.poll()
        self.assertEqual((self.row(mid)["route"], self.row(mid)["reason"]), ("drop", "topic_group_stranger"))
        self.assertEqual(len(self.notices()), 1)    # 同一个人只提醒一次
        other = "ou_test_other"
        mid2 = self.lark.add(chat_id=TG, sender_id=other, content="hi", chat_type="topic")
        self.poll()
        self.assertIn(other, self.notices()[-1])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM tracked_items").fetchone()[0], 0)


# ======================= I：开关、热加载、规则指纹 =======================
class SwitchTest(TGBase):
    def test_no_chat_id_is_exactly_as_before(self):
        self.cfg.topic_group = TopicGroupConfig()
        text = self.cfg_file.read_text(encoding="utf-8").replace(f'chat_id = "{TG}"', 'chat_id = ""')
        self.cfg_file.write_text(text, encoding="utf-8")
        rc, out, _ = self.ask("--key", "PLAIN", "--track", text="?")
        self.assertIn("没配话题群", out)
        self.assertEqual(self.lark.tg_sent(), [])
        self.assertIsNotNone(self.store.thread_by_key("tp_alpha", "PLAIN"))
        self.d.dispatch_once()
        self.assertFalse([c for c in self.lark.calls if c[:2] in (["im", "pins"], ["im", "+chat-members-list"])])
        pb = Playbook.from_config(self.cfg)
        self.assertFalse(pb.topic_group)
        self.assertEqual(interface_section("tp_alpha", bus=False, remind=[15], expire=60, topic_group=pb.topic_group),
                         interface_section("tp_alpha", bus=False, remind=[15], expire=60))
        self.assertNotIn("话题群", interface_section("tp_alpha", bus=False, remind=[15], expire=60))
        self.assertIn("--track", interface_section("tp_alpha", bus=False, remind=[15], expire=60, topic_group=True))
        self.assertTrue(Playbook.from_config(load_config(self.cfg_file)).base_vars.get("topic_group_enabled") is None)

    def test_disabled_drops_group_messages_and_uses_dm(self):
        self.ask("--key", "ON", "--track", text="?")
        self.cfg.topic_group.enabled = False         # 热加载：Collector / Dispatcher 用的是同一份 cfg，下一轮生效
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8") + "enabled = false\n", encoding="utf-8")
        it = self.item("ON")
        mid = self.owner_tg("在吗", root=it["root_message_id"])
        self.poll()
        self.assertEqual((self.row(mid)["route"], self.row(mid)["reason"]), ("drop", "topic_group_off"))
        n = len(self.lark.tg_sent())
        self.ask("--key", "ON", kind="report", title="关了", text="回私聊")
        self.assertEqual(len(self.lark.tg_sent()), n)
        self.d.dispatch_once()
        self.assertEqual(len(self.lark.tg_sent()), n)   # 不发索引卡

    def test_config_validation(self):
        bad = Path(self.tmp.name) / "bad.toml"
        for body, word in ((f'[topic_group]\nchat_id = "{ESC_CHAT}"\n[escalation]\nchat_ids = ["{ESC_CHAT}"]\n'
                            "header_regex = '(?P<topic>tp_x)'\n", "同一个"),
                           ('[topic_group]\nchat_id = "x"\n', "oc_"),
                           ('[topic_group]\nchat_id = "oc_a"\nindex_refresh_minutes = 0\n', "正数"),
                           ('[topic_group]\nchat_id = "oc_a"\nsuggest_max = 2.5\n', "正整数"),
                           ('[topic_group]\nchat_id = "oc_a"\nsuggest_active_hours = 0\n', "正数"),
                           ('[topic_group]\nchat_id = "oc_a"\n[source]\nonly_chat_ids = ["oc_b"]\n', "only_chat_ids")):
            bad.write_text(body, encoding="utf-8")
            with self.assertRaises(Exception) as cm:
                load_config(bad)
            self.assertIn(word, str(cm.exception))

    def test_owner_side_chat_rules(self):
        """话题群算「找主人那一侧」：sheepdog reply 不能回群里的消息、主人在群里说话不当别处的背景。"""
        self.ask("--key", "S-1", "--track", text="?")
        self.poll()
        it = self.item("S-1")
        rc, _, err = self.cli("reply", "--topic", "tp_alpha", "--to", it["root_message_id"], "--text", "x")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertIn("找主人", err)
        self.assertIn(TG, self.cfg.owner_side_chat_ids)
        self.assertNotIn(TG, self.cfg.owner_chat_ids)

    def test_topic_group_create_dry_run(self):
        rc, out, _ = self.cli("topic-group", "create", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn(f"lark-cli im +chat-create --chat-mode topic --type private --name '🐕 牧羊犬 · 长期跟踪' --owner {ME} "
                      f"--users {ME} --set-bot-manager --format json --as bot", out)
        self.assertIn('"add_member_permission": "only_owner"', out)
        self.assertEqual(self.lark.calls, [c for c in self.lark.calls if c[:2] != ["im", "+chat-create"]])


# ======================= 其他：会话侧命令、热加载、先后 =======================
class MiscTest(TGBase):
    def test_resolve_and_thread_by_key_work_for_group_items(self):
        self.ask("--key", "RES-1", "--track", title="要不要续", text="?")
        self.ask("--key", "res_1", topic="tp_beta", kind="report", title="beta 也在跟", text="..")
        self.poll()
        it = self.item("RES-1")
        rc, out, err = self.cli("resolve", "--topic", "tp_alpha", "--key", "RES-1", "--why", "主人在别处答了")
        self.assertEqual(rc, 0, err)
        notice = self.lark.tg_sent()[-1]
        self.assertEqual(_opt(notice, "--message-id"), it["root_message_id"])   # 那句话回在群里的话题
        self.assertEqual(self.store.conn.execute("SELECT close_reason FROM escalations").fetchone()[0], "resolved")
        self.poll()
        self.assertEqual(self.row(self.lark.idem[_opt(notice, "--idempotency-key")])["route"], "drop")
        # 别的会话也能看这件事的整个话题（一件事跨会话）
        rc, out, err = self.cli("thread", "--topic", "tp_beta", "--key", "T-1")
        self.assertEqual(rc, 0, err)
        self.assertIn("要不要续", out)
        self.assertIn("beta 也在跟", out)

    def test_owner_edit_in_group_goes_back_as_correction(self):
        self.ask("--key", "ED-1", "--track", text="?")
        self.poll()
        it = self.item("ED-1")
        mid = self.owner_tg("今晚发", root=it["root_message_id"])
        self.poll()
        self.d.dispatch_once()
        self.ack("tp_alpha")
        self.lark.msgs[mid]["content"] = "明晚发"
        self.pull(mid)
        r = self.row(mid)
        self.assertEqual((r["reason"], r["dispatch_state"], r["topic_id"]), ("edited", "pending", "tp_alpha"))
        self.assertTrue(self.d._owner_correction(r))     # 主人改口插队，同找主人聊天

    def test_hot_reload_turns_group_on(self):
        from sheepdog import cli as cli_mod
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8").replace(f'chat_id = "{TG}"', 'chat_id = ""'),
                                 encoding="utf-8")
        env = {"SHEEPDOG_CONFIG": str(self.cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        c = Collector(self.cfg, self.store, self.source, self.roster)
        args = mock.Mock(dry_run=False, interval=0, verbose=False)
        with mock.patch.dict("os.environ", env):
            new = cli_mod._reload_config(self.cfg, args, self.store, self.source, c, self.d)
        self.assertFalse(new.topic_group.active)
        self.assertIs(c.cfg, new)
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8").replace('chat_id = ""', f'chat_id = "{TG}"'),
                                 encoding="utf-8")
        with mock.patch.dict("os.environ", env):
            new = cli_mod._reload_config(new, args, self.store, self.source, c, self.d)
        self.assertTrue(new.topic_group.active)
        self.assertTrue(self.d.playbook.topic_group)          # 接口说明跟着变（规则指纹这时才变）
        mid = self.owner_tg("新开一件")
        c.poll_once()
        self.assertEqual(self.row(mid)["reason"], "owner_reply")
        self.assertIsNotNone(self.store.item_by_root(mid))

    def test_card_refresh_runs_after_delivery(self):
        """发提问、发通知、投递在前，改卡排在后面。"""
        self.ask("--key", "ORD-1", "--track", text="?")
        self.poll()
        it = self.item("ORD-1")
        mid = self.owner_tg("好", root=it["root_message_id"])
        self.poll()
        seen = {}
        real = tg.dispatcher_round

        def spy(disp):
            seen.setdefault("delivered", self.row(mid)["dispatch_state"])   # 这一轮第一次对账时
            return real(disp)

        with mock.patch.object(tg, "dispatcher_round", spy):
            self.d.dispatch_once()
        self.assertIn(seen["delivered"], ("acked", "delivered"))


# ======================= 和 7.45 合并后的三处交叉 =======================
class MergeWith745Test(TGBase):
    """7.51 叠在 7.45 上：A 类（主人 @ 机器人 = 指令）不作用在话题群；机器人事件的小轮次照样走话题群分支、不对账索引卡；
    话题群的抄送和 7.45 的指令都插队，插话没回音提醒都起算。"""

    def setUp(self):
        super().setUp()
        from sheepdog.config import IdentityConfig
        self.cfg.identity = IdentityConfig(enabled=True, bot_open_id="ou_test_botself", bot_app_id=BOT_APP)

    def at_bot(self, chat: str, text: str, root: str = "", chat_type: str = "topic") -> str:
        mid = self.lark.add(chat_id=chat, sender_id=ME, content=text, chat_type=chat_type, thread_root=root)
        self.lark.msgs[mid]["mentions"] = [BOT_APP]
        return mid

    def test_a_class_never_applies_in_topic_group(self):
        from sheepdog.engine import OWNER_INSTRUCTION
        self.ask("--key", "AC-1", "--track", text="?")
        self.poll()
        it = self.item("AC-1")
        c = Collector(self.cfg, self.store, self.source, self.roster)
        # 新开顶层话题、@ 了机器人：仍是话题群入口 3（事项 + 交总线），不是 7.45 的指令
        top = self.at_bot(TG, "@牧羊犬 帮我盯一下这个")
        c.ingest([parse_message(self.lark.raw(top))])
        r = self.row(top)
        self.assertEqual((r["reason"], r["topic_id"]), ("owner_reply", BUS_TOPIC_ID))
        self.assertIsNotNone(self.store.item_by_root(top))
        self.assertNotIn(OWNER_INSTRUCTION, r["tags_json"])
        # 在事项话题里 @ 机器人回复，机器人事件那一份先到：照样按话题群分支送负责会话
        rep = self.at_bot(TG, "@牧羊犬 选 A", root=it["root_message_id"])
        ev = parse_event(self.lark.event(rep))
        self.assertEqual((ev.root_id, ev.mentions[0].id), (it["root_message_id"], BOT_APP))
        c.ingest([ev], received_by="bot")
        r = self.row(rep)
        self.assertEqual((r["reason"], r["topic_id"]), ("owner_reply", "tp_alpha"))
        self.assertIn("item:T-1", r["tags_json"])
        self.assertNotIn(OWNER_INSTRUCTION, r["tags_json"])
        self.assertFalse(c._group_chat(parse_message(self.lark.raw(rep))))
        # 对照：普通群里主人 @ 机器人，7.45 的 A 类照常
        grp = self.at_bot("oc_test_alpha_grp", "@牧羊犬 看一下", chat_type="group")
        c.ingest([parse_message(self.lark.raw(grp))])
        self.assertEqual(self.row(grp)["reason"], OWNER_INSTRUCTION)

    def test_mini_cycle_routes_group_messages_and_does_not_refresh_index(self):
        from sheepdog import cli as cli_mod
        from test_identity import FakeConsumer
        self.ask("--key", "MC-1", "--track", text="?")
        self.poll()
        self.d.dispatch_once()                         # 完整轮次：索引卡发出、置顶
        it = self.item("MC-1")
        idx = tg.index_info(self.store)["message_id"]
        mid = self.owner_tg("选 B", root=it["root_message_id"])
        consumer = FakeConsumer()
        consumer.pending.append(self.lark.event(mid))
        c = Collector(self.cfg, self.store, self.source, self.roster)
        c.bot_events = consumer
        before = len(self.lark.calls)
        stats = cli_mod._mini_cycle(c, self.d)
        self.assertEqual(stats["fetched"], 1)
        r = self.row(mid)
        self.assertEqual((r["reason"], r["topic_id"]), ("owner_reply", "tp_alpha"))   # 小轮次也走话题群分支
        self.assertIn(r["dispatch_state"], ("delivered", "acked"))                    # 并且当轮投出去了
        tg_calls = [x for x in self.lark.calls[before:] if x[:2] in (["im", "messages"], ["im", "pins"],
                                                                     ["im", "+chat-members-list"])
                    or (x[1:2] == ["+messages-send"] and _opt(x, "--chat-id") == TG)]
        self.assertEqual(tg_calls, [])                                                 # 小轮次不对账索引卡、不查成员
        self.assertEqual(self.lark.patched, {})
        later = datetime.now().astimezone() + timedelta(minutes=11)
        with mock.patch("sheepdog.engine._now", return_value=later):
            self.d.dispatch_once()                                                     # 完整轮次：对账一次
            self.d.dispatch_once()
        self.assertEqual(len(self.lark.patched.get(idx, [])), 1)

    def test_cc_and_instruction_both_jump_the_queue_and_start_followups(self):
        from sheepdog.engine import OWNER_INSTRUCTION, PRIORITY_FOLLOWUP_PREFIX
        self.ask("--key", "PQ-1", "--track", title="发版", text="今晚？")
        self.ask("--key", "PQ-1", topic="tp_beta", title="回滚", text="窗口？")
        self.poll()
        # 两个会话都在跑上一批（没交回执）
        for tid in ("tp_alpha", "tp_beta"):
            self.store.add_system_message(tid, "bus_relay", "先做这件")
        self.d.dispatch_once()
        self.assertEqual({self.store.get_topic(t)["state"] for t in ("tp_alpha", "tp_beta")}, {"running"})
        it = self.item("PQ-1")
        reply = self.owner_tg("今晚发，窗口 30 分钟", root=it["root_message_id"])   # 没引用：beta 原件、alpha 抄送
        grp = self.at_bot("oc_test_alpha_grp", "@牧羊犬 顺便看下告警", chat_type="group")   # 7.45 A 类 → tp_alpha
        self.poll()
        self.assertEqual(self.row(grp)["reason"], OWNER_INSTRUCTION)
        rows = self.store.pending_dispatch()
        alpha = [r for r in rows if r["topic_id"] == "tp_alpha"]
        self.assertEqual({r["reason"] for r in self.d._priority_rows(alpha)}, {"owner_reply", OWNER_INSTRUCTION})
        self.d.dispatch_once()
        for tid in ("tp_alpha", "tp_beta"):
            self.assertTrue(self.store.get_topic(tid)["pending_batch_id"].startswith("b"))   # 原来那批还在等回执
        self.assertEqual(self.row(reply)["dispatch_state"], "acked")                 # 插队批次送达即记成已回执
        self.assertEqual(self.row(grp)["dispatch_state"], "acked")
        follow = {json.loads(v)["topic"] for _, v in self.store.meta_items(PRIORITY_FOLLOWUP_PREFIX)}
        self.assertEqual(follow, {"tp_alpha", "tp_beta"})                          # 插话没回音提醒都起算
        self.assertEqual({e["close_reason"] for e in self.store.conn.execute("SELECT close_reason FROM escalations")},
                         {"answered"})


# ======================= 叠在「待办」重排（6c9f811 / 4cb0bf7）上 =======================
class PendingRelayoutTest(TGBase):
    def test_dm_pending_keeps_new_card_group_pending_points_to_index(self):
        from sheepdog.pending import PENDING_TITLE
        self.ask("--key", "PD-1", "--track", text="?")
        self.poll()
        dm = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="待办")
        grp = self.owner_tg("待办")
        self.poll()
        dm_reply = [c for c in self.lark.sent("+messages-reply") if _opt(c, "--message-id") == dm]
        grp_reply = [c for c in self.lark.sent("+messages-reply") if _opt(c, "--message-id") == grp]
        self.assertEqual(len(dm_reply), 1)
        self.assertTrue(sent_text(dm_reply[0]).startswith(PENDING_TITLE), sent_text(dm_reply[0]))   # 私聊：新排版的卡片
        self.assertNotIn("索引卡", sent_text(dm_reply[0]))
        self.assertEqual(len(grp_reply), 1)
        self.assertIn("看置顶的索引卡", sent_text(grp_reply[0]))                                   # 群里：指向索引卡
        self.assertEqual((self.row(dm)["reason"], self.row(grp)["reason"]), ("pending_query", "pending_query"))

    def test_titles_use_markdown_free_excerpt(self):
        self.ask("--key", "MD-1", "--track", title="**证书**交接", text="?")
        title = self.lark.tg_sent()[0]
        self.assertEqual(json.loads(_opt(title, "--content"))["header"]["title"]["content"], "🗂 T-1 · 证书交接")
        mid = self.owner_tg("## 发版 **计划**\n- 先灰度\n- 再全量")
        self.poll()
        it = self.store.item_by_root(mid)
        self.assertEqual(it["title"], "发版 计划")
        self.d.dispatch_once()
        idx = [sent_text(c) for c in self.lark.tg_sent() if sent_text(c).startswith("📌")][0]
        self.assertIn("**T-2 · 发版 计划**", idx)
        self.assertIn("**T-1 · 证书交接**", idx)
        self.assertNotIn("**证书**", idx)


# ======================= 索引卡、标题卡的样子（10-17 00:3x 收干净，排版同「待办」卡片） =======================
class CardLookTest(TGBase):
    def index_card_now(self) -> tuple[dict, str]:
        v = tg.index_view(self.cfg, tg.build_index(self.cfg, self.store))
        return tg.index_card(v), tg.index_text(v)

    def test_index_header_subtitle_colour_and_two_line_rows(self):
        self.store.update_topic(BUS_TOPIC_ID, title="[managed] Lark 信号·总线")
        self.ask("--key", "LK-1", "--track", title="要你定的事", text="?")
        self.ask("--key", "LK-2", "--track", kind="report", title="在做的事", text="..", topic=BUS_TOPIC_ID)
        self.poll()
        card, text = self.index_card_now()
        h = card["header"]
        self.assertEqual((h["title"]["content"], h["template"]), ("📌 话题群索引", "orange"))   # 有等你定：橙色
        self.assertRegex(h["subtitle"]["content"], r"^\d\d:\d\d 更新 · 跟踪 2 件 · 等你定 1$")
        self.assertNotIn("北京时间", json.dumps(card, ensure_ascii=False))
        self.assertNotIn("对账", json.dumps(card, ensure_ascii=False))
        md = [e["content"] for e in card["body"]["elements"] if e["tag"] == "markdown"]
        self.assertEqual(md[0], "**等你定 · 1 件**")
        row = md[1].split("\n")
        self.assertEqual(len(row), 2)
        self.assertTrue(row[0].startswith("**T-1 · 要你定的事** [打开](https://"), row[0])
        self.assertRegex(row[1], r"^<font color='grey'>Alpha 需求 · \d\d:\d\d 问 · 已等 \d+ 分钟</font>$")
        bus_row = next(x for x in md if x.startswith("**T-2"))
        self.assertIn("<font color='grey'>Lark 信号·总线 · 最近动静 ", bus_row)          # 会话显示名去掉 [managed]
        self.assertNotIn("[managed]", json.dumps(card, ensure_ascii=False))
        self.assertIn({"tag": "hr"}, card["body"]["elements"])                            # 栏与栏、件与件之间分隔线
        self.assertNotIn("（无）", text)
        for word in ("过期未定", "等别人", "沉睡", "最近办完", "建议放进群"):                 # 空栏不显示
            self.assertNotIn(word, text)
        foot = card["body"]["elements"][-1]
        self.assertEqual(foot["text_size"], "notation")
        self.assertIn("<font color='grey'>" + tg.INDEX_FOOTER + "</font>", foot["content"])
        # 纯文本版同样的顺序
        self.assertTrue(text.startswith("📌 话题群索引 · "))
        self.assertLess(text.index("等你定 · 1 件"), text.index("进行中 · 1 件"))
        self.assertTrue(text.endswith(tg.INDEX_FOOTER))
        # 办完的写几点办完；没有等你定时蓝色
        tg.close(self.store, self.item("LK-1"), "tp_alpha", "定了")
        card, text = self.index_card_now()
        self.assertEqual(card["header"]["template"], "blue")
        self.assertRegex(text, r"Alpha 需求 · \d\d:\d\d 办完")

    def test_index_all_empty_and_suggest_only_when_present(self):
        card, text = self.index_card_now()
        self.assertEqual([e["content"] for e in card["body"]["elements"]], ["现在没有在跟的事"])
        self.assertEqual(card["header"]["template"], "blue")
        self.assertRegex(text, r"^📌 话题群索引 · \d\d:\d\d 更新 · 跟踪 0 件 · 等你定 0\n\n现在没有在跟的事$")
        # 有建议放进群的：出现那一栏，第一行灰色小字说怎么放
        self.ask("--key", "SG-1", text="?")
        self.poll()
        root = self.store.thread_by_key("tp_alpha", "SG-1")["root_message_id"]
        self.owner_dm_thread(root, "a")
        self.owner_dm_thread(root, "b")
        self.poll()
        card, text = self.index_card_now()
        md = [e["content"] for e in card["body"]["elements"] if e["tag"] == "markdown"]
        self.assertEqual(md[0], "现在没有在跟的事")
        self.assertEqual(md[1], f"**建议放进群 · 1 件**\n<font color='grey'>{tg.SUGGEST_HOW}</font>")
        self.assertRegex(md[2], r"^\*\*发版时间\*\* \[打开\]\(https://[^)]+\)\n<font color='grey'>Alpha 需求 · 钥匙 SG-1 · "
                                r"你回过 2 次 · 最近动静 \d+ 分钟前</font>$")

    def test_title_card_header_and_one_grey_line(self):
        self.store.update_topic(BUS_TOPIC_ID, title="[managed] Lark 信号·总线")
        self.ask("--key", "G2-TEST", "--track", title="G2 话题群测试：VPS 选型", text="?", topic=BUS_TOPIC_ID)
        call = self.lark.tg_sent()[0]
        card = json.loads(_opt(call, "--content"))
        self.assertEqual(card["header"]["title"]["content"], "🗂 T-1 · G2 话题群测试：VPS 选型")
        self.assertRegex(card["header"]["subtitle"]["content"], r"^钥匙 G2-TEST · Lark 信号·总线开的 · \d\d-\d\d \d\d:\d\d$")
        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual(card["body"]["elements"], [{"tag": "markdown", "content": f"<font color='grey'>{tg.TITLE_FOOTER}</font>",
                                                     "text_size": "notation"}])
        sent = self.store.conn.execute("SELECT text FROM sends WHERE kind='tg_title'").fetchone()[0]
        self.assertRegex(sent, r"^🗂 T-1 · G2 话题群测试：VPS 选型\n钥匙 G2-TEST · Lark 信号·总线开的 · \d\d-\d\d \d\d:\d\d\n"
                               + tg.TITLE_FOOTER + "$")                                    # 纯文本版（记账、卡片被拒时发它）
        # 「可能相关」放在灰色小字前面一行
        it = self.item("G2-TEST")
        tg.close(self.store, it, BUS_TOPIC_ID, "好了")
        self.store.update_item(it["item_no"], state="archived",
                               archived_at=(datetime.now().astimezone() - timedelta(days=15)).isoformat(timespec="seconds"))
        self.ask("--key", "G2-TEST", "--track", title="又一轮", text="?", topic=BUS_TOPIC_ID)
        card = json.loads(_opt([c for c in self.lark.tg_sent() if "--content" in c and "T-2" in _opt(c, "--content")][0],
                               "--content"))
        els = card["body"]["elements"]
        self.assertEqual(len(els), 2)
        self.assertTrue(els[0]["content"].startswith("可能相关：T-1 [打开](https://"), els[0]["content"])
        self.assertEqual(els[1]["content"], f"<font color='grey'>{tg.TITLE_FOOTER}</font>")
        # owner_card 关着：标题卡发纯文本
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8") + "[notify]\nowner_card = false\n", encoding="utf-8")
        self.ask("--key", "PLAIN-1", "--track", title="纯文本", text="?")
        plain = [c for c in self.lark.tg_sent() if _opt(c, "--chat-id") == TG][-1]
        self.assertNotIn("--content", plain)
        self.assertTrue(_opt(plain, "--text").startswith("🗂 T-3 · 纯文本\n钥匙 PLAIN-1 · Alpha 需求开的 · "))
