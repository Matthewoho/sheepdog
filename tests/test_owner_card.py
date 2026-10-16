"""7.48 发给主人的消息一律发卡片。

Matthew 10-16 16:1x：「sheepdog给我发消息的时候也要发成卡片，因为普通文本消息不渲染md的，但是模型喜欢返回md」。

读回的样子按 10-16 只读核过的正式账本造（FakeLark._card_text）：
- 主人身份搜索拉到的卡片：有 header 是 <card title="标题">，没有是 <card>；各 markdown 元素换行分开；
  <at id=ou_x></at> 变成 @名字(ou_x)；最后一行 </card>。
- 机器人身份的实时事件里，卡片只有「[interactive card]」（账本里机器人独收的卡片都是这样）；机器人自己发的消息
  事件里收不到（7.26 上线以来 690 条全是主人身份搜到的），兜底照样测。

A 卡片本身：第一行进 header、正文原样、@ 另起一个元素；读回还原成「标题\\n正文」；不是卡片原样返回。
B 四条路都发卡片：ask 新起 / 回到话题里（带 @）、report、系统通知（OwnerDM）、「待办」清单、结案那一句。记账的正文是纯文本，
  防重编号和纯文本时一样。
C 读回：卡片登记成「需要你定」、认出会话；引用回复、话题回复都送回对的会话；机器人身份先收到的卡片搜到正文后补认。
D 太长退回纯文本；飞书拒了卡片同一个编号改发纯文本；超时不改。
E 开关 [notify] owner_card：默认开；关掉回到纯文本；常驻进程热加载。
F 测试环境（[threads] owner_chat_id）：系统通知发进测试群，不私聊主人。
"""

import json
from types import SimpleNamespace

from sheepdog import card as cardmod, cli, outbound
from sheepdog.card import build_owner_card, card_text, plain_text
from sheepdog.config import EscalationConfig, RoutingConfig
from sheepdog.engine import BUS_TOPIC_ID, Collector
from sheepdog.models import Message
from sheepdog.router import route, RouteContext
from sheepdog.source.lark import parse_message

from test_identity import IdentityBase
from test_roster import ME
from test_threads import ESC_CHAT, TEST_GROUP, ThreadsBase, _card_text, _opt, sent_text

# 正式配置里的两条正则（~/.config/sheepdog/config.toml [escalation]）
PROD_HEADER_RX = r"^🐕 (?=.*需要你定).*?\[[^\[\]]*?(?P<topic>tp_[a-z0-9_.-]+)\]"
PROD_ATTR_RX = r"\[[^\[\]]*?(?P<topic>tp_[a-z0-9_.-]+)\]"
MD = "**事情**：Alice 要周五上线\n\n### 选项\n1. A：周五\n2. B：下周一\n\n```\nkubectl get pods\n```"


def card_of(call: list[str]) -> dict:
    return json.loads(_opt(call, "--content"))


# ======================= A：卡片本身 =======================
class CardUnitTest(ThreadsBase):
    def test_build_header_body_at(self):
        c = build_owner_card("🐕 需要你定 · 发版时间 [tp_alpha]\n" + MD, at_open_id=ME)
        self.assertEqual(c["schema"], "2.0")
        self.assertEqual(c["header"]["title"], {"tag": "plain_text", "content": "🐕 需要你定 · 发版时间 [tp_alpha]"})
        self.assertEqual(c["header"]["template"], "orange")
        els = c["body"]["elements"]
        self.assertEqual(els[0], {"tag": "markdown", "content": MD})          # markdown 原样
        self.assertEqual(els[-1], {"tag": "markdown", "content": f"<at id={ME}></at>"})
        self.assertEqual(len(els), 2)
        r = build_owner_card("🐕 汇报 · 进度 [tp_alpha]\n已合入")
        self.assertEqual(r["header"]["template"], "blue")
        self.assertEqual(len(r["body"]["elements"]), 1)

    def test_single_line_has_no_header(self):
        c = build_owner_card("✅ 已结案：办完了（Alpha 需求）")
        self.assertNotIn("header", c)
        self.assertEqual(c["body"]["elements"], [{"tag": "markdown", "content": "✅ 已结案：办完了（Alpha 需求）"}])
        with self.assertRaises(ValueError):
            build_owner_card("  \n ")

    def test_title_quotes_become_fullwidth(self):
        c = build_owner_card('🐕 汇报 · 说 "好" [tp_alpha]\n正文')
        self.assertEqual(c["header"]["title"]["content"], "🐕 汇报 · 说 ＂好＂ [tp_alpha]")

    def test_card_text_real_shapes(self):
        # 正式账本里的三种开头（10-16 只读核过）
        self.assertEqual(card_text("<card>\n收到。\n[🐕Sheepdog Reply]\n</card>"), "收到。\n[🐕Sheepdog Reply]")
        self.assertEqual(card_text('<card title="✅ v2 预生产结论" subtitle="本次按人工确认放行">\n正文\n</card>'),
                         "✅ v2 预生产结论\n正文")
        self.assertEqual(card_text('<card title="Re: Rate Limit & Inquiry">\n正文\n</card>'), "Re: Rate Limit & Inquiry\n正文")
        self.assertEqual(card_text('<card title="🐕 需要你定 · 标题 [tp_alpha]">\n</card>'), "🐕 需要你定 · 标题 [tp_alpha]")
        # 不是卡片：原样
        for s in ("🐕 需要你定 · x [tp_a]\n正文", "", "<cardigan>", '{"text": "hi"}', "<card"):
            self.assertIsNone(card_text(s))
            self.assertEqual(plain_text(s), s)

    def test_round_trip_through_real_readback_format(self):
        text = "🐕 需要你定 · 发版时间 [tp_alpha]\n" + MD
        back = _card_text(json.dumps(build_owner_card(text)))
        self.assertTrue(back.startswith('<card title="🐕 需要你定 · 发版时间 [tp_alpha]">\n'))
        self.assertEqual(plain_text(back), text)
        with_at = plain_text(_card_text(json.dumps(build_owner_card(text, ME))))
        self.assertEqual(with_at, text + f"\n@主人({ME})")
        self.assertRegex(with_at, PROD_HEADER_RX)

    def test_size_estimate(self):
        small = build_owner_card("🐕 汇报 · x [tp_a]\n" + "字" * 100)
        self.assertTrue(cardmod.card_fits(small))
        # 中文 3 字节；9000 个字 ≈ 27 KB 正文，加上转义超过上限
        big = build_owner_card("🐕 汇报 · x [tp_a]\n" + "字" * 9500)
        self.assertFalse(cardmod.card_fits(big))
        self.assertGreater(cardmod.card_request_bytes(big), cardmod.OWNER_CARD_MAX_BYTES)


class RouterTest(ThreadsBase):
    def test_drop_prefix_matches_card_title(self):
        cfg = RoutingConfig(drop_bot_message_prefixes=["🐕 需要你定", "🐕 汇报"])
        ctx = RouteContext(ME)
        card = Message(message_id="om_test_c1", chat_id="oc_test_grp", chat_name="", chat_type="group",
                       sender_id="cli_test_x", sender_name="", sender_type="app",
                       content='<card title="🐕 汇报 · 进度 [tp_alpha]">\n正文\n</card>', msg_type="interactive",
                       create_time="")
        self.assertEqual((route(card, ctx, cfg).route, route(card, ctx, cfg).reason), ("drop", "self_escalation"))
        other = Message(message_id="om_test_c2", chat_id="oc_test_grp", chat_name="", chat_type="group",
                        sender_id="cli_test_x", sender_name="", sender_type="app",
                        content='<card title="告警 · 5xx">\n正文\n</card>', msg_type="interactive", create_time="")
        self.assertNotEqual(route(other, ctx, cfg).reason, "self_escalation")


# ======================= B：四条路 =======================
class OwnerCardBase(ThreadsBase):
    def setUp(self):
        super().setUp()
        self.cfg.escalation = EscalationConfig(chat_ids=[ESC_CHAT], header_regex=PROD_HEADER_RX,
                                               attribution_regex=PROD_ATTR_RX)
        txt = self.cfg_file.read_text(encoding="utf-8")
        self.cfg_file.write_text(f'playbook_dir = "{self.cfg.playbook_dir}"\n' + txt.replace(
            "[escalation]\n", "[escalation]\n", 1), encoding="utf-8")

    def sends(self, sub=None):
        return self.lark.sent(sub)

    def ask_md(self, *extra, kind="ask", title="发版时间", text=MD):
        rc, out, err = self.ask(*extra, kind=kind, title=title, text=text)
        self.assertEqual(rc, 0, out + err)
        return out

    def set_cfg(self, line_section: str, key_line: str) -> None:
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8") + f"[{line_section}]\n{key_line}\n",
                                 encoding="utf-8")


class FourPathsTest(OwnerCardBase):
    def test_ask_new_is_a_card_and_ledger_keeps_plain_text(self):
        self.ask_md("--key", "K1")
        call = self.sends()[-1]
        self.assertEqual(call[1], "+messages-send")
        self.assertEqual((_opt(call, "--msg-type"), _opt(call, "--user-id"), _opt(call, "--as")),
                         ("interactive", ME, "bot"))
        self.assertNotIn("--text", call)
        c = card_of(call)
        body = outbound.compose(self.cfg, "ask", "tp_alpha", "发版时间", MD)
        self.assertEqual(c["header"]["title"]["content"], "🐕 需要你定 · 发版时间 [tp_alpha]")
        self.assertEqual(c["body"]["elements"], [{"tag": "markdown", "content": MD}])   # 第一条不 @
        mid = self.store.thread_by_key("tp_alpha", "K1")["root_message_id"]
        self.assertEqual(self.store.send_by_message(mid)["text"], body)            # sends 记纯文本
        self.assertEqual(self.store.get_bot_outbox(mid)["text"], body)
        batch = outbound.current_batch(self.store, "tp_alpha")
        self.assertEqual(_opt(call, "--idempotency-key"), outbound.idem_key("ask", "tp_alpha", "K1", batch, body))

    def test_ask_in_thread_has_at_element_and_same_key_as_plain(self):
        self.ask_md("--key", "K2")
        self.ask_md("--key", "K2", text="补充：**回滚**方案")
        self.ask_md("--key", "K2", kind="report", title="进度", text="- 已合入\n- 已部署")
        _, r1, r2 = self.sends()
        root = self.store.thread_by_key("tp_alpha", "K2")["root_message_id"]
        for r, head, tmpl in ((r1, "🐕 需要你定 · 发版时间 [tp_alpha]", "orange"), (r2, "🐕 汇报 · 进度 [tp_alpha]", "blue")):
            self.assertEqual((r[1], _opt(r, "--message-id"), _opt(r, "--msg-type")), ("+messages-reply", root, "interactive"))
            self.assertIn("--reply-in-thread", r)
            c = card_of(r)
            self.assertEqual((c["header"]["title"]["content"], c["header"]["template"]), (head, tmpl))
            self.assertEqual(c["body"]["elements"][-1]["content"], f"<at id={ME}></at>")   # 7.29：话题里 @ 主人
            self.assertEqual(sum("<at " in e["content"] for e in c["body"]["elements"]), 1)
        body = outbound.compose(self.cfg, "ask", "tp_alpha", "发版时间", "补充：**回滚**方案")
        batch = outbound.current_batch(self.store, "tp_alpha")
        self.assertEqual(_opt(r1, "--idempotency-key"), outbound.idem_key("ask", "tp_alpha", "K2", batch, body))
        mid = self.store.conn.execute("SELECT message_id FROM sends WHERE text=?", (body,)).fetchone()[0]
        self.assertEqual(self.store.get_bot_outbox(mid)["text"], body)          # 不含 @

    def test_resolve_notice_is_a_card_with_optional_at(self):
        self.ask_md("--key", "K3")
        q = self.store.thread_by_key("tp_alpha", "K3")["root_message_id"]
        self.pull(q)
        rc, _, err = self.cli("resolve", "--topic", "tp_alpha", "--key", "K3", "--why", "主人在私聊定了")
        self.assertEqual(rc, 0, err)
        call = self.sends("+messages-reply")[-1]
        self.assertEqual(_opt(call, "--msg-type"), "interactive")
        c = card_of(call)
        self.assertNotIn("header", c)                                            # 一句话，没有 header
        self.assertTrue(c["body"]["elements"][0]["content"].startswith("✅ 已结案：主人在私聊定了"))
        self.assertEqual(len(c["body"]["elements"]), 1)                          # 默认不 @
        s = self.store.send_by_message(self.store.conn.execute(
            "SELECT message_id FROM sends WHERE kind='resolve'").fetchone()[0])
        self.assertTrue(s["text"].startswith("✅ 已结案："))
        self.assertNotIn("<", s["text"])

    def test_resolve_notice_at_when_configured(self):
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8").replace(
            "[owner_context]", "resolve_notice_at = true\n[owner_context]"), encoding="utf-8")
        self.ask_md("--key", "K4")
        self.pull(self.store.thread_by_key("tp_alpha", "K4")["root_message_id"])
        self.assertEqual(self.cli("resolve", "--topic", "tp_alpha", "--key", "K4", "--why", "办完了")[0], 0)
        els = card_of(self.sends("+messages-reply")[-1])["body"]["elements"]
        self.assertEqual(els[-1]["content"], f"<at id={ME}></at>")

    def test_pending_list_is_a_card(self):
        self.ask_md("--key", "K5")
        self.pull(self.store.thread_by_key("tp_alpha", "K5")["root_message_id"])
        q = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="待办")
        self.pull(q)
        self.assertEqual(self.row(q)["reason"], "pending_query")
        call = self.sends("+messages-reply")[-1]
        self.assertEqual((_opt(call, "--message-id"), _opt(call, "--msg-type")), (q, "interactive"))
        self.assertNotIn("--reply-in-thread", call)
        c = card_of(call)
        self.assertTrue(c["header"]["title"]["content"].startswith("📋 待办清单"))
        s = self.store.conn.execute("SELECT text, message_id FROM sends WHERE kind='pending'").fetchone()
        self.assertTrue(s[0].startswith("📋 待办清单"))
        head, _, rest = s[0].partition("\n")
        self.assertEqual(sent_text(call), head + "\n" + rest.strip("\n"))   # 正文开头的空行不进卡片
        # 清单拉回来：认得出是牧羊犬自己的话
        self.pull(s[1])
        self.assertEqual(self.row(s[1])["reason"], "sheepdog_pending")
        self.assertIsNone(self.store.get_escalation(s[1]))

    def test_system_notice_is_a_card_and_notice_ledger_keeps_plain(self):
        self.d.notifier = cli._notifier(self.cfg, self.source, SimpleNamespace(dry_run=False))
        text = "🐕 汇报 · 会话中断了 [tp_bus]\n「Alpha 需求」（topic `tp_alpha`）**20 分钟**没交回执"
        self.assertTrue(self.d._notify_owner(text, "羊群看板通知", "tp_alpha"))
        call = self.sends()[-1]
        self.assertEqual((call[1], _opt(call, "--user-id"), _opt(call, "--msg-type")), ("+messages-send", ME, "interactive"))
        c = card_of(call)
        self.assertEqual(c["header"]["title"]["content"], "🐕 汇报 · 会话中断了 [tp_bus]")
        n = self.store.conn.execute("SELECT body, idem_key, state FROM owner_notices").fetchone()
        self.assertEqual(bytes(n[0]).decode("utf-8"), text)                       # 账本存纯文本
        self.assertEqual((_opt(call, "--idempotency-key"), n[2]), (n[1], "sent"))
        # 拉回来：按 header 认出是总线的消息，记进 bot_outbox（不是提问）
        mid = [m for m in self.lark.msgs if self.lark.msgs[m]["chat_id"] == ESC_CHAT][-1]
        self.pull(mid)
        self.assertEqual((self.row(mid)["reason"], self.store.get_bot_outbox(mid)["topic_id"]),
                         ("escalation_bot_message", BUS_TOPIC_ID))
        self.assertEqual(self.store.get_bot_outbox(mid)["text"], text)


# ======================= C：读回、送回 =======================
class ReadBackTest(OwnerCardBase):
    def test_card_ask_registers_escalation_with_plain_text(self):
        self.ask_md()
        mid = [m for m in self.lark.msgs if self.lark.msgs[m]["chat_id"] == ESC_CHAT][-1]
        self.assertTrue(self.lark.msgs[mid]["content"].startswith('<card title="🐕 需要你定 · 发版时间 [tp_alpha]">'))
        self.pull(mid)
        self.assertEqual((self.row(mid)["route"], self.row(mid)["reason"]), ("drop", "escalation_asked"))
        e = self.store.get_escalation(mid)
        self.assertEqual(e["topic_id"], "tp_alpha")
        self.assertEqual(e["text"], outbound.compose(self.cfg, "ask", "tp_alpha", "发版时间", MD))  # 不带 <card 外壳
        from sheepdog import pending as pend
        self.assertEqual(pend.ask_title(self.cfg, e["text"]), "发版时间")
        self.assertEqual(self.store.pending_dispatch(), [])

    def test_card_report_attributed_by_header(self):
        self.ask_md(kind="report", title="进度")
        mid = [m for m in self.lark.msgs if self.lark.msgs[m]["chat_id"] == ESC_CHAT][-1]
        self.store.conn.execute("DELETE FROM bot_outbox")       # 只靠读回认（别的进程发的、记账丢了）
        self.store.conn.commit()
        self.pull(mid)
        self.assertEqual(self.row(mid)["reason"], "escalation_bot_message")
        self.assertEqual(self.store.get_bot_outbox(mid)["topic_id"], "tp_alpha")
        self.assertIsNone(self.store.get_escalation(mid))

    def test_card_in_thread_with_at_is_not_at_me(self):
        self.ask_md("--key", "K6")
        self.ask_md("--key", "K6", text="第二个问题")
        root, q2 = [m for m in self.lark.msgs if self.lark.msgs[m]["chat_id"] == ESC_CHAT]
        self.pull(root)
        self.deliver()
        raw = self.lark.raw(q2)
        self.assertIn(f"@主人({ME})", raw["content"])
        raw["mentions"] = [{"id": ME, "key": "@_user_1", "name": "主人"}]   # 卡片里的 @ 也进 mentions（正式账本核过）
        Collector(self.cfg, self.store, self.source, self.roster).ingest([parse_message(raw)])
        self.assertEqual((self.row(q2)["route"], self.row(q2)["reason"]), ("drop", "escalation_asked"))
        self.assertEqual(self.store.get_escalation(q2)["topic_id"], "tp_alpha")
        self.assertEqual(self.store.pending_dispatch(), [])

    def test_quote_reply_on_card_goes_back(self):
        self.ask_md()
        self.ask(topic=BUS_TOPIC_ID, title="总线的事", text="总线问的")
        alpha_q, bus_q = [m for m in self.lark.msgs if self.lark.msgs[m]["chat_id"] == ESC_CHAT]
        self.pull(alpha_q, bus_q)
        self.deliver()
        ans = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="选 A")
        m = parse_message(self.lark.raw(ans))
        m.reply_to = alpha_q
        Collector(self.cfg, self.store, self.source, self.roster).ingest([m])
        self.assertEqual((self.row(ans)["topic_id"], self.row(ans)["reason"]), ("tp_alpha", "owner_reply"))
        self.assertIn("发版时间", self.row(ans)["note"])
        self.assertNotIn("<card", self.row(ans)["note"])
        got = self.deliver()
        self.assertIn("选 A", got)

    def test_thread_reply_on_card_goes_back_with_plain_root(self):
        self.ask_md("--key", "K7")
        root = self.store.thread_by_key("tp_alpha", "K7")["root_message_id"]
        self.pull(root)
        self.deliver()
        r = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="A", thread_root=root)
        self.pull(r)
        row = self.row(r)
        self.assertEqual((row["topic_id"], row["reason"]), ("tp_alpha", "owner_reply"))
        self.assertIn("🧵 主人在话题里回复", row["note"])
        self.assertIn("🐕 需要你定 · 发版时间 [tp_alpha]", row["note"])
        self.assertNotIn("<card", row["note"])

    def test_thread_root_card_recognized_without_any_records(self):
        # 话题表、sends、需要你定、bot_outbox 都没有这条（比如别的进程发的）：按开头那条卡片的 header 认出会话
        self.ask_md("--key", "K8")
        root = self.store.thread_by_key("tp_alpha", "K8")["root_message_id"]
        r = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="A", thread_root=root)
        for t in ("owner_threads", "sends", "bot_outbox", "escalations"):
            self.store.conn.execute(f"DELETE FROM {t}")
        self.store.conn.commit()
        self.store.upsert_message(parse_message(self.lark.raw(root)), "drop", "escalation_chat_other", [])
        self.pull(r)
        row = self.row(r)
        self.assertEqual(row["topic_id"], "tp_alpha")
        self.assertEqual(self.store.thread_by_root(root)["topic_id"], "tp_alpha")
        self.assertEqual(self.store.thread_by_root(root)["title"], "🐕 需要你定 · 发版时间 [tp_alpha]")

    def test_old_plain_text_still_recognized_when_switch_is_on(self):
        old = self.lark.add(chat_id=ESC_CHAT, sender_id="ou_test_bot", sender_type="app",
                            content="🐕 需要你定 · 老问题 [tp_alpha]\n还是纯文本")
        self.pull(old)
        self.assertEqual(self.store.get_escalation(old)["text"], "🐕 需要你定 · 老问题 [tp_alpha]\n还是纯文本")


class BotEventFirstTest(IdentityBase):
    """机器人身份先收到自己发的卡片（正式账本里没出现过：机器人事件收不到自己发的消息）。
    事件那条路入账前会用主人身份 mget 取一份（和搜索同一套格式），取到了就和搜索一样认；
    mget 失败时事件里卡片只有「[interactive card]」：先认不出，主人身份搜到正文后补认、照常登记。"""

    def setUp(self):
        super().setUp()
        self.cfg.escalation = EscalationConfig(chat_ids=[ESC_CHAT], header_regex=PROD_HEADER_RX,
                                               attribution_regex=PROD_ATTR_RX)

    def two_asks(self):
        self.ask("--key", "K9")
        self.ask("--key", "K9", text="第二个问题")
        root, q2 = [k for k, v in self.lark.msgs.items() if v["chat_id"] == ESC_CHAT]
        self.pull(root)
        self.deliver()
        return root, q2

    def test_card_event_enriched_by_mget_is_recognized_at_once(self):
        _, q2 = self.two_asks()
        self.bot_hears(self.lark.event(q2, message_type="interactive", content="[interactive card]"))
        r = self.row(q2)
        self.assertEqual((r["reason"], r["received_by"]), ("escalation_asked", "bot"))
        self.assertTrue(r["content"].startswith("<card title="))
        self.assertEqual(self.store.pending_dispatch(), [])

    def test_card_event_then_search_registers(self):
        _, q2 = self.two_asks()
        self.lark.plan["+messages-mget"] = ["error"]             # 主人身份 mget 失败：只有事件里的「[interactive card]」
        self.bot_hears(self.lark.event(q2, message_type="interactive", content="[interactive card]"))
        self.assertEqual((self.row(q2)["reason"], self.row(q2)["received_by"]), ("escalation_chat_other", "bot"))
        self.assertIsNone(self.store.get_escalation(q2))
        self.pull(q2)                                            # 主人身份搜到 <card title=…> 那份
        r = self.row(q2)
        self.assertEqual((r["route"], r["reason"], r["received_by"]), ("drop", "escalation_asked", "both"))
        self.assertTrue(r["content"].startswith("<card title="))
        self.assertEqual(self.store.get_escalation(q2)["topic_id"], "tp_alpha")
        self.assertEqual(self.store.get_bot_outbox(q2)["topic_id"], "tp_alpha")
        self.assertEqual(self.store.pending_dispatch(), [])
        self.pull(q2)                                            # 再搜到一次：不变
        self.assertEqual(self.row(q2)["reason"], "escalation_asked")


# ======================= D：太长、被拒 =======================
class FallbackTest(OwnerCardBase):
    def test_too_long_falls_back_to_plain_text_same_key(self):
        long = "字" * 9500
        self.ask_md("--key", "L1", text=long)
        call = self.sends()[-1]
        self.assertNotIn("--content", call)
        body = outbound.compose(self.cfg, "ask", "tp_alpha", "发版时间", long)
        self.assertEqual(_opt(call, "--text"), body)
        batch = outbound.current_batch(self.store, "tp_alpha")
        self.assertEqual(_opt(call, "--idempotency-key"), outbound.idem_key("ask", "tp_alpha", "L1", batch, body))
        # 话题里的长消息：纯文本照 7.29 加 <at user_id=…>
        self.ask_md("--key", "L1", text=long + "二")
        self.assertTrue(_opt(self.sends()[-1], "--text").endswith(f'<at user_id="{ME}"></at>'))

    def test_card_rejected_resends_plain_with_same_key(self):
        self.lark.plan["+messages-send"] = ["card_reject"]
        self.ask_md("--key", "R1")
        a, b = [c for c in self.lark.calls if c[1:2] == ["+messages-send"]][-2:]
        self.assertEqual(_opt(a, "--msg-type"), "interactive")
        self.assertNotIn("--content", b)
        self.assertEqual(_opt(a, "--idempotency-key"), _opt(b, "--idempotency-key"))
        self.assertEqual(len([m for m in self.lark.msgs if self.lark.msgs[m]["chat_id"] == ESC_CHAT]), 1)

    def test_other_errors_and_timeouts_do_not_fall_back(self):
        for mode in ("error", "timeout"):
            self.lark.plan["+messages-send"] = [mode]
            rc, _, _ = self.ask("--key", "E-" + mode, title="t", text=MD)
            self.assertEqual(rc, outbound.EXIT_SEND_FAILED)
            self.assertEqual(_opt(self.lark.calls[-1], "--msg-type"), "interactive")
        self.assertEqual(len([c for c in self.lark.calls if c[1:2] == ["+messages-send"]]), 2)

    def test_notice_card_rejected_resends_plain(self):
        self.d.notifier = cli._notifier(self.cfg, self.source, SimpleNamespace(dry_run=False))
        self.lark.plan["+messages-send"] = ["card_reject"]
        self.assertTrue(self.d._notify_owner("🐕 汇报 · x [tp_bus]\n正文", "测试通知"))
        a, b = [c for c in self.lark.calls if c[1:2] == ["+messages-send"]][-2:]
        self.assertEqual((_opt(a, "--msg-type"), _opt(b, "--text")), ("interactive", "🐕 汇报 · x [tp_bus]\n正文"))
        self.assertEqual(self.store.conn.execute("SELECT state FROM owner_notices").fetchone()[0], "sent")


# ======================= E：开关 =======================
class SwitchTest(OwnerCardBase):
    def test_default_on_and_validated(self):
        from sheepdog.config import ConfigError, load_config
        self.assertTrue(load_config(self.cfg_file).notify.owner_card)
        self.set_cfg("notify", "owner_card = 1")
        with self.assertRaisesRegex(ConfigError, r"owner_card"):
            load_config(self.cfg_file)

    def test_off_sends_plain_text_like_before(self):
        self.set_cfg("notify", "owner_card = false")
        self.ask_md("--key", "S1")
        self.ask_md("--key", "S1", text="补充")
        first, r = self.sends()
        self.assertNotIn("--content", first)
        self.assertTrue(_opt(first, "--text").startswith("🐕 需要你定 · 发版时间 [tp_alpha]\n"))
        self.assertTrue(_opt(r, "--text").endswith(f'<at user_id="{ME}"></at>'))

    def test_hot_reload_in_daemon(self):
        """常驻进程：改配置文件后下一轮（_reload_config）通知和「待办」清单都按新开关发。"""
        import os
        from unittest import mock
        args = SimpleNamespace(dry_run=False, interval=None)
        collector = Collector(self.cfg, self.store, self.source, self.roster)
        self.d.notifier = cli._notifier(self.cfg, self.source, args)
        env = {"SHEEPDOG_CONFIG": str(self.cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        self.set_cfg("notify", "owner_card = false")
        with mock.patch.dict(os.environ, env):
            new = cli._reload_config(self.cfg, args, self.store, self.source, collector, self.d)
        self.assertFalse(new.notify.owner_card)
        self.assertFalse(self.d.notifier.cfg.notify.owner_card)
        self.d._notify_owner("🐕 汇报 · 关了 [tp_bus]\n正文", "测试通知")
        self.assertNotIn("--content", self.sends()[-1])
        collector.cfg = new
        q = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="待办")
        collector.ingest([parse_message(self.lark.raw(q))])
        self.assertNotIn("--content", self.sends("+messages-reply")[-1])
        # 再打开
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8").replace("owner_card = false", "owner_card = true"),
                                 encoding="utf-8")
        with mock.patch.dict(os.environ, env):
            cli._reload_config(new, args, self.store, self.source, collector, self.d)
        self.d._notify_owner("🐕 汇报 · 开了 [tp_bus]\n正文", "测试通知")
        self.assertEqual(_opt(self.sends()[-1], "--msg-type"), "interactive")

    def test_doctor_line(self):
        lines = cli.threads_report(self.cfg)
        self.assertIn("  发给主人的消息（7.48）: 卡片（[notify] owner_card = true）", lines)
        self.assertFalse([x for x in lines if "✗" in x])
        self.cfg.escalation = EscalationConfig(chat_ids=[ESC_CHAT], header_regex=r"^🐕 需要你定 · (?P<topic>tp_x)",
                                               attribution_regex=PROD_ATTR_RX)
        self.assertTrue([x for x in cli.threads_report(self.cfg) if "发成卡片后读回" in x])


# ======================= F：测试环境 =======================
class TestEnvTest(OwnerCardBase):
    def test_notice_goes_to_test_group_not_owner_dm(self):
        self.cfg.threads.owner_chat_id = TEST_GROUP
        self.lark.chat_types[TEST_GROUP] = "group"
        self.d.notifier = cli._notifier(self.cfg, self.source, SimpleNamespace(dry_run=False))
        self.assertIn(TEST_GROUP, repr(self.d.notifier))
        self.assertTrue(self.d._notify_owner("🐕 汇报 · 会话中断了 [tp_bus]\n正文", "羊群看板通知", "tp_alpha"))
        call = self.sends()[-1]
        self.assertEqual((call[1], _opt(call, "--chat-id"), _opt(call, "--user-id")), ("+messages-send", TEST_GROUP, ""))
        self.assertEqual(_opt(call, "--msg-type"), "interactive")
        self.assertTrue(_opt(call, "--idempotency-key"))
        self.assertFalse([c for c in self.lark.calls if _opt(c, "--user-id")])   # 一条私聊都没发

    def test_dry_run_prints_where_and_how(self):
        self.cfg.threads.owner_chat_id = TEST_GROUP
        n = cli._notifier(self.cfg, self.source, SimpleNamespace(dry_run=True))
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            n("hi")
        self.assertIn(f"发进测试群 {TEST_GROUP}（卡片）", out.getvalue())
        self.assertEqual(self.lark.sent(), [])
