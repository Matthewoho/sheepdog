"""7.36 第一期第 1–3 件：会话结案、来源提醒、待办清单。

用 test_threads 的假飞书（FakeLark），绝不起真实进程。时间：假飞书里的消息只到分钟，提问和主人发言要错开一分钟以上。

A 账本：老账本（5140eda 结构）打开后自动加列、建表，原有数据不变。
B 结案（sheepdog resolve）：只结自己名下、还开着的；--key 结一个话题里所有开着的；话题里补一句「已结案」、默认不 @ 主人；
  超时重试只发一条；那句话拉回来不登记「需要你定」、不投给任何会话；看板上自动补的活跟着收掉。
C 来源：ask 记来源（--about / 这一批）；老提问按提问时间补；主人后来在来源聊天里说话 / 点表情 / 同来源另一条被答复或结案
  → 标可能已处理、提醒一次；快到期提醒一次；重复提问提示。
D 待办：sheepdog pending 的顺序和分组；主人发「待办」机器人直接回清单，清单拉回来不登记、不投递。
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import outbound, pending as pend
from sheepdog.engine import BUS_TOPIC_ID, Collector, open_escalations
from sheepdog.source.lark import parse_message
from sheepdog.store import Store

from test_roster import ME, REPO, later
from test_threads import ALICE, ALPHA_P2P, BOT, ESC_CHAT, MARK, FakeLark, ThreadsBase, _opt, now8

# 正式配置里 header_regex 的写法（10-14 只读核对）：结案那句、待办清单的第一行都不能被它认成「需要你定」
PROD_HEADER_RX = r"^🐕 (?=.*需要你定).*?\[[^\[\]]*?(?P<topic>tp_[a-z0-9_.-]+)\]"
OLD_SCHEMA = REPO / "tests" / "fixtures" / "ledger_5140eda_schema.sql"


def at(minutes: float):
    return now8() + timedelta(minutes=minutes)


class PendingBase(ThreadsBase):
    def setUp(self):
        super().setUp()
        self.cfg_file.write_text(f'playbook_dir = "{self.cfg.playbook_dir}"\n'
                                 + self.cfg_file.read_text(encoding="utf-8"), encoding="utf-8")

    # ---------- 造场景 ----------
    def alice_batch(self, text: str = "Alice：能不能周五上线？", chat: str = ALPHA_P2P, minutes: float = -10) -> str:
        """Alice 发一条，拉进来、投给 tp_alpha（成为它这一批的消息）。返回消息号。"""
        mid = self.lark.add(chat_id=chat, sender_id=ALICE, content=text, at=at(minutes))
        self.pull(mid)
        self.d.dispatch_once()
        return mid

    def asked(self, *extra, topic="tp_alpha", title="上线时间", text="周五还是下周一？") -> str:
        """ask 并把那条提问拉回来登记。返回提问的消息号。"""
        before = set(self.lark.msgs)
        rc, out, err = self.ask(*extra, topic=topic, title=title, text=text)
        self.assertEqual(rc, 0, out + err)
        new = [m for m in self.lark.msgs if m not in before]
        mid = new[0] if new else out.split("message_id ")[-1].split("。")[0].split("（")[0].strip()
        self.pull(mid)
        self.assertIsNotNone(self.store.get_escalation(mid))
        self.last_out = out
        return mid

    def ask_earlier(self, *mids: str, minutes: int = 2) -> None:
        """把提问时间往前挪几分钟。假飞书的时间只到分钟、结案 / 答复时间到秒：刚好跨分钟的第一秒里两者会相等，
        「晚于提问」判不出来（10-14 连跑 75 秒复现过 22 次）。"""
        for mid in mids:
            e = self.store.get_escalation(mid)
            t = datetime.fromisoformat(e["asked_at"]) - timedelta(minutes=minutes)
            self.store.conn.execute("UPDATE escalations SET asked_at=? WHERE message_id=?",
                                    (t.isoformat(timespec="seconds"), mid))
        self.store.conn.commit()

    def owner_says(self, text: str, chat: str = ALPHA_P2P, minutes: float = 2) -> str:
        mid = self.lark.add(chat_id=chat, sender_id=ME, content=text, at=at(minutes))
        self.pull(mid)
        return mid

    def system_msgs(self, reason: str, topic: str = "tp_alpha") -> list[str]:
        return [r[0] for r in self.store.conn.execute(
            "SELECT content FROM messages WHERE reason=? AND topic_id=? ORDER BY rowid", (reason, topic))]

    def resolve(self, *argv, topic="tp_alpha") -> tuple[int, str, str]:
        return self.cli("resolve", "--topic", topic, *argv)

    def bot_msgs_in_thread(self, root: str) -> list[str]:
        return [self.lark.msgs[m]["content"] for m in self.lark.in_thread(root)]


# ======================= A：账本迁移 =======================
class MigrationTest(PendingBase):
    def test_old_5140eda_ledger_migrates_in_place(self):
        path = Path(self.tmp.name) / "old.sqlite3"
        conn = sqlite3.connect(str(path))
        conn.executescript(OLD_SCHEMA.read_text(encoding="utf-8"))
        old_cols = {r[1] for r in conn.execute("PRAGMA table_info(escalations)")}
        self.assertNotIn("resolved_by", old_cols)
        conn.execute("INSERT INTO escalations(message_id,topic_id,chat_id,text,asked_at) VALUES(?,?,?,?,?)",
                     ("om_test_old1", "tp_alpha", ESC_CHAT, "🐕 需要你定 · 老问题 [tp_alpha]\n要不要？",
                      datetime.now().astimezone().isoformat(timespec="seconds")))
        conn.commit()
        conn.close()
        s = Store(path)
        try:
            cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(escalations)")}
            for c in ("resolved_by", "resolve_note", "possibly_handled_at", "possibly_handled_message_id",
                      "possibly_handled_reason", "possibly_handled_notified_at", "expiry_reminded_at"):
                self.assertIn(c, cols)
            src_cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(escalation_sources)")}
            self.assertTrue({"ask_message_id", "about_message_id", "about_chat_id", "origin"} <= src_cols)
            e = s.get_escalation("om_test_old1")
            self.assertEqual((e["topic_id"], e["closed_at"], e["possibly_handled_at"]), ("tp_alpha", None, None))
            self.assertEqual([x["message_id"] for x in open_escalations(s, 24)], ["om_test_old1"])
            self.assertTrue(s.resolve_escalation("om_test_old1", "tp_alpha", "依据"))
        finally:
            s.close()
        Store(path).close()   # 再开一次不出错（迁移幂等）
        # dry-run 的内存副本同样补上
        snap = Store.snapshot(path)
        self.assertIn("possibly_handled_at", {r["name"] for r in snap.conn.execute("PRAGMA table_info(escalations)")})
        snap.close()


# ======================= B：会话结案 =======================
class ResolveTest(PendingBase):
    def test_resolve_by_message_id_without_key_replies_in_thread_and_does_not_at_owner(self):
        q = self.asked()
        rc, out, err = self.resolve("--message-id", q, "--why", "主人 02:10 在 Alpha 私聊说下周一")
        self.assertEqual(rc, 0, err)
        e = self.store.get_escalation(q)
        self.assertEqual((e["close_reason"], e["resolved_by"], e["resolve_note"]),
                         ("resolved", "tp_alpha", "主人 02:10 在 Alpha 私聊说下周一"))
        call = self.lark.sent("+messages-reply")[-1]
        self.assertEqual((_opt(call, "--message-id"), _opt(call, "--as")), (q, "bot"))
        self.assertIn("--reply-in-thread", call)
        text = _opt(call, "--text")
        self.assertEqual(text, "✅ 已结案：主人 02:10 在 Alpha 私聊说下周一（Alpha 需求）")
        self.assertNotIn("<at", text)
        s = self.store.send_by_message(out.split("message_id ")[-1].split("）")[0])
        self.assertEqual((s["kind"], s["target_message_id"]), ("resolve", q))
        self.assertEqual(open_escalations(self.store, 24, "tp_alpha"), [])

    def test_resolve_notice_at_owner_when_configured(self):
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8").replace(
            "[owner_context]", "resolve_notice_at = true\n[owner_context]"), encoding="utf-8")
        q = self.asked()
        self.assertEqual(self.resolve("--message-id", q, "--why", "办完了")[0], 0)
        self.assertTrue(_opt(self.lark.sent("+messages-reply")[-1], "--text").endswith(f'<at user_id="{ME}"></at>'))

    def test_resolve_by_key_closes_all_open_in_the_thread(self):
        q1 = self.asked("--key", "VPN-1", text="给 Ivan 开 VPN 吗？")
        q2 = self.asked("--key", "VPN-1", text="再确认一下：开几天？")
        other = self.asked("--key", "OTHER", text="另一件事")
        root = self.store.thread_by_key("tp_alpha", "VPN-1")["root_message_id"]
        self.assertEqual(root, q1)
        rc, out, err = self.resolve("--key", "vpn-1", "--why", "主人已经亲自发了 VPN")
        self.assertEqual(rc, 0, err)
        self.assertIn("已结案 2 条", out)
        self.assertEqual({self.store.get_escalation(x)["close_reason"] for x in (q1, q2)}, {"resolved"})
        self.assertIsNone(self.store.get_escalation(other)["closed_at"])
        call = self.lark.sent("+messages-reply")[-1]
        self.assertEqual(_opt(call, "--message-id"), root)
        self.assertIn("✅ 已结案：主人已经亲自发了 VPN（Alpha 需求）", self.bot_msgs_in_thread(root))

    def test_only_own_and_open(self):
        mine = self.asked()
        theirs = self.asked(topic=BUS_TOPIC_ID, title="总线的事", text="总线问的")
        rc, _, err = self.resolve("--message-id", theirs, "--why", "x")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertIn("不是你的", err)
        rc, _, err = self.resolve("--message-id", mine, "--why", "   ")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        rc, _, err = self.resolve("--key", "没有这把", "--why", "x")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        rc, _, err = self.resolve("--message-id", "om_test_nope", "--why", "x")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        # 主人已经答了的不能再结
        self.store.answer_escalation(mine, "om_test_answer")
        rc, _, err = self.resolve("--message-id", mine, "--why", "x")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertIn("没有还开着的", err)
        self.assertEqual(self.lark.sent("+messages-reply"), [])
        self.assertIsNone(self.store.get_escalation(theirs)["closed_at"])

    def test_timeout_then_retry_sends_once_and_closes_once(self):
        q = self.asked()
        self.lark.plan["+messages-reply"] = ["lost"]
        rc, _, err = self.resolve("--message-id", q, "--why", "办完了")
        self.assertEqual(rc, outbound.EXIT_SEND_FAILED)
        self.assertIn("还没结案", err)
        self.assertIsNone(self.store.get_escalation(q)["closed_at"])
        rc, _, _ = self.resolve("--message-id", q, "--why", "办完了")
        self.assertEqual(rc, 0)
        calls = self.lark.sent("+messages-reply")
        self.assertEqual(len(calls), 2)
        self.assertEqual(_opt(calls[0], "--idempotency-key"), _opt(calls[1], "--idempotency-key"))
        self.assertEqual(len(self.lark.in_thread(q)), 1)
        rc, out, _ = self.resolve("--message-id", q, "--why", "办完了")   # 再跑一次：什么都不动
        self.assertEqual(rc, 0)
        self.assertIn("之前已经结过案", out)
        self.assertEqual(len(self.lark.sent("+messages-reply")), 2)

    def test_notice_pulled_back_is_not_an_escalation_and_goes_nowhere(self):
        self.cfg.escalation.header_regex = PROD_HEADER_RX
        q = self.asked("--key", "K1")
        # 故意把结案那句写成「🐕 需要你定 · … [tp_alpha]」开头：靠 sends 记账认出是自己的话，照样不登记
        pb = Path(self.tmp.name) / "pb"
        pb.mkdir()
        for f in Path(self.cfg.playbook_dir).iterdir():
            (pb / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        (pb / "resolve_notice.md").write_text("🐕 需要你定 · 已结案 [tp_alpha] {{why}}", encoding="utf-8")
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8").replace(
            f'playbook_dir = "{self.cfg.playbook_dir}"', f'playbook_dir = "{pb}"'), encoding="utf-8")
        self.d.dispatch_once()
        self.assertEqual(self.store.get_work("tp_alpha", "K1")["state"], "waiting_owner")
        self.assertEqual(self.resolve("--key", "K1", "--why", "办完了")[0], 0)
        notice = self.lark.in_thread(q)[-1]
        self.pull(notice)
        r = self.row(notice)
        self.assertEqual((r["route"], r["reason"]), ("drop", "sheepdog_resolve"))
        self.assertIsNone(self.store.get_escalation(notice))
        self.assertEqual(open_escalations(self.store, 24), [])
        self.assertNotIn(notice, self.deliver())
        # 看板上自动补的「等主人」跟着收掉，结果写结案依据
        w = self.store.get_work("tp_alpha", "K1")
        self.assertEqual((w["state"], w["result"]), ("done", "会话已结案：办完了"))

    def test_owner_reply_quoting_the_notice_goes_back_to_the_session(self):
        q = self.asked()
        self.resolve("--message-id", q, "--why", "办完了")
        notice = self.lark.in_thread(q)[-1]
        self.pull(notice)
        mid = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="还没办完", thread_root=q, at=at(3))
        self.pull(mid)
        self.assertEqual(self.row(mid)["topic_id"], "tp_alpha")

    def test_test_env_closes_without_sending(self):
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8") + "[source]\nenabled = false\n",
                                 encoding="utf-8")
        q = "om_test_q_env"
        self.store.add_escalation(q, "tp_alpha", ESC_CHAT, "🐕 需要你定 · x [tp_alpha]\ny",
                                  datetime.now().astimezone().isoformat(timespec="seconds"))
        rc, out, _ = self.resolve("--message-id", q, "--why", "测试")
        self.assertEqual(rc, 0)
        self.assertIn("没发飞书", out)
        self.assertEqual(self.store.get_escalation(q)["close_reason"], "resolved")
        self.assertEqual(self.lark.calls, [])


# ======================= C：来源、可能已处理、快到期、重复 =======================
class SourcesTest(PendingBase):
    def test_ask_records_batch_sources_without_system_or_owner_chat(self):
        a = self.alice_batch()
        self.store.add_system_message("tp_alpha", "duty_mail", "别的会话的留言")
        q = self.asked()
        rows = self.store.escalation_sources(q)
        self.assertEqual([(r["about_message_id"], r["about_chat_id"], r["origin"], r["topic_id"]) for r in rows],
                         [(a, ALPHA_P2P, "batch", "tp_alpha")])

    def test_owner_chat_messages_are_not_sources(self):
        a = self.alice_batch()
        esc = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="找主人聊天里的一句", at=at(-3))
        self.pull(esc)
        self.store.record_dispatch("b_test_mixed", "tp_alpha", [a, esc, "sd_test_x"])
        self.assertEqual(pend.batch_sources(self.cfg, self.store, "b_test_mixed"), [(a, ALPHA_P2P)])

    def test_multi_chat_batch_records_no_source_and_hints(self):
        # 7.38 第 2 条：这一批的候选消息来自好几个聊天就不记来源，ask 输出提示用 --about
        a = self.alice_batch()
        g = self.lark.add(chat_id="oc_test_alpha_grp", sender_id=ALICE, content="群里的一句", chat_type="group",
                          at=at(-8))
        self.pull(g)
        self.store.record_dispatch("b_test_two_chats", "tp_alpha", [a, g])
        rc, out, _ = self.ask("--batch", "b_test_two_chats")
        self.assertEqual(rc, 0)
        self.assertIn("这批消息来自 2 个聊天，用 --about 指明为哪条问的，否则不会有来源提醒", out)
        q = self.lark.sent()[-1]
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM escalation_sources").fetchone()[0], 0)
        # 同一个聊天的：记、不提示
        self.store.record_dispatch("b_test_one_chat", "tp_alpha", [a])
        rc, out, _ = self.ask("--batch", "b_test_one_chat", text="另一件")
        self.assertNotIn("个聊天", out)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM escalation_sources").fetchone()[0], 1)
        self.assertIsNotNone(q)

    def test_backfill_same_rule_multi_chat_records_nothing(self):
        a = self.alice_batch()
        g = self.lark.add(chat_id="oc_test_alpha_grp", sender_id=ALICE, content="群里的一句", chat_type="group",
                          at=at(-8))
        self.pull(g)
        self.ack("tp_alpha")
        self.store.record_dispatch("b_test_two_chats", "tp_alpha", [a, g])
        self.store.finish_dispatch("b_test_two_chats", "acked")
        ago = lambda sec: (datetime.now().astimezone() - timedelta(seconds=sec)).isoformat(timespec="seconds")   # noqa: E731
        for bid, sec in ((self.row(a)["batch_id"], 10), ("b_test_two_chats", 5)):   # 提问入账前几秒送到（不和入账同一秒）
            self.store.conn.execute("UPDATE dispatches SET sent_at=? WHERE batch_id=?", (ago(sec), bid))
        self.store.conn.commit()
        old = self.lark.add(chat_id=ESC_CHAT, sender_id=BOT, sender_type="app", at=at(1),
                            content="🐕 需要你定 · 老办法发的 [tp_alpha]\n要不要？")
        self.pull(old)
        self.assertEqual(self.store.escalation_sources(old), [])
        self.assertEqual(pend.followup_round(self.cfg, self.store, self.d.playbook)["backfilled"], 0)

    def test_ask_about_explicit_and_invalid(self):
        a = self.alice_batch()
        b = self.alice_batch("另一件事", chat="oc_test_alpha_grp")
        q = self.asked("--about", b)
        self.assertEqual([(r["about_message_id"], r["origin"]) for r in self.store.escalation_sources(q)],
                         [(b, "about")])
        n = len(self.lark.sent())
        rc, _, err = self.ask("--about", "om_test_not_in_ledger", text="别的问题")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertIn("账本里没有", err)
        rc, _, err = self.ask("--about", "not_an_id", text="别的问题")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertEqual(len(self.lark.sent()), n)   # 参数不对：什么都没发
        self.assertIsNotNone(a)

    def test_report_records_no_sources(self):
        self.alice_batch()
        self.ask(kind="report", text="汇报")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM escalation_sources").fetchone()[0], 0)

    def test_old_ask_backfilled_from_batch_before_ask_time(self):
        a = self.alice_batch()
        self.ack("tp_alpha")
        self.d.dispatch_once()
        # 真实世界里会话收到一批到提问再到入账至少隔几秒；同一秒入账按「同一轮先拉取后投递」算批次在后（pending.backfill_batch）
        self.store.conn.execute("UPDATE dispatches SET sent_at=? WHERE batch_id=?",
                                ((datetime.now().astimezone() - timedelta(seconds=5)).isoformat(timespec="seconds"),
                                 self.row(a)["batch_id"]))
        self.store.conn.commit()
        old = self.lark.add(chat_id=ESC_CHAT, sender_id=BOT, sender_type="app", at=at(1),
                            content="🐕 需要你定 · 老办法发的 [tp_alpha]\n要不要？")
        self.pull(old)
        rows = self.store.escalation_sources(old)
        self.assertEqual([(r["about_message_id"], r["origin"]) for r in rows], [(a, "backfill")])
        # 登记时补不出来的（没有更早的批次），每轮再试；有了就补上
        self.store.conn.execute("DELETE FROM escalation_sources")
        self.store.conn.commit()
        self.assertEqual(pend.followup_round(self.cfg, self.store, self.d.playbook)["backfilled"], 1)
        self.assertEqual(len(self.store.escalation_sources(old)), 1)

    def test_owner_spoke_in_source_chat_marks_and_reminds_once(self):
        self.alice_batch()
        q = self.asked()
        self.ack("tp_alpha")
        # 不算的：提问之前说的、代回的、别的聊天（找主人聊天里的话会被当成回答，另有测试）
        self.owner_says("提问前说的", minutes=-5)
        self.owner_says(f"代回卡片\n{MARK}", minutes=2)
        self.owner_says("别的聊天", chat="oc_test_other_grp", minutes=2)
        self.d.dispatch_once()
        self.assertIsNone(self.store.get_escalation(q)["possibly_handled_at"])
        said = self.owner_says("Alice 周五不行，下周一上")
        text = self.deliver()
        e = self.store.get_escalation(q)
        self.assertEqual((e["possibly_handled_reason"], e["possibly_handled_message_id"]), ("owner_spoke", said))
        self.assertTrue(e["possibly_handled_notified_at"])
        self.assertIn("🔎 你的「需要你定」可能已经处理了", text)
        self.assertIn("「上线时间」", text)
        self.assertIn("「Alice 周五不行，下周一上」", text)
        self.assertIn(f"FX-ESC-CHECK topic=tp_alpha ask={q}", text)
        self.assertIsNone(e["closed_at"])                 # 只提醒，不替它结案
        # 只提醒一次：主人再说、再跑几轮都不再提醒
        self.owner_says("再补一句", minutes=3)
        for _ in range(3):
            self.deliver()
        self.assertEqual(len(self.system_msgs("escalation_check")), 1)

    def group_source(self) -> tuple[str, str]:
        """群里的来源消息（不 @ 主人，进 Inbox），ask 用 --about 指它。返回 (来源, 提问)。"""
        src = self.lark.add(chat_id="oc_test_alpha_grp", sender_id=ALICE, content="群里：周五能上线吗？",
                            chat_type="group", at=at(-10))
        self.pull(src)
        return src, self.asked("--about", src)

    def test_group_source_plain_speech_does_not_count(self):
        # 7.38 第 3 条：来源是群的，主人在群里随便说话不算
        _, q = self.group_source()
        m = self.lark.add(chat_id="oc_test_alpha_grp", sender_id=ME, content="大家午饭吃什么", chat_type="group",
                          at=at(2))
        self.pull(m)
        self.d.dispatch_once()
        self.assertIsNone(self.store.get_escalation(q)["possibly_handled_at"])

    def test_group_source_quote_reply_counts(self):
        src, q = self.group_source()
        m = self.lark.add(chat_id="oc_test_alpha_grp", sender_id=ME, content="下周一", chat_type="group", at=at(2))
        msg = parse_message(self.lark.raw(m))
        msg.reply_to = src
        Collector(self.cfg, self.store, self.source, self.roster).ingest([msg])
        self.d.dispatch_once()
        e = self.store.get_escalation(q)
        self.assertEqual((e["possibly_handled_reason"], e["possibly_handled_message_id"]), ("owner_spoke", m))

    def test_group_source_thread_reply_counts_after_learning_root(self):
        # 来源消息拉进账本时还没有话题；主人后来在它下面开话题回复：mget 一次来源消息补上话题编号，认得出
        src, q = self.group_source()
        m = self.lark.add(chat_id="oc_test_alpha_grp", sender_id=ME, content="话题里：下周一", thread_root=src,
                          at=at(2))
        self.assertFalse(self.row(src)["thread_id"])
        self.pull(m)
        self.assertEqual(self.row(src)["thread_id"], self.lark.msgs[src]["thread_id"])
        mgets = [c for c in self.lark.calls if c[1] == "+messages-mget"]
        self.assertEqual([_opt(c, "--message-ids") for c in mgets], [src])
        self.d.dispatch_once()
        self.assertEqual(self.store.get_escalation(q)["possibly_handled_message_id"], m)
        # 别的话题里说的不算
        other = self.lark.add(chat_id="oc_test_alpha_grp", sender_id=ALICE, content="另一件事", chat_type="group",
                              at=at(-9))
        self.pull(other)
        q2 = self.asked("--about", src, title="第二问", text="还有一问")
        x = self.lark.add(chat_id="oc_test_alpha_grp", sender_id=ME, content="别的话题", thread_root=other, at=at(3))
        self.pull(x)
        self.assertNotEqual(pend.detect(self.cfg, self.store, self.store.get_escalation(q2)).message_id, x)

    def test_escalations_topic_filter(self):
        mine = self.asked()
        theirs = self.asked(topic=BUS_TOPIC_ID, title="总线的事", text="总线问的")
        rc, out, _ = self.cli("escalations", "--topic", "tp_alpha")
        self.assertEqual(rc, 0)
        self.assertIn(mine, out)
        self.assertNotIn(theirs, out)
        rc, out, _ = self.cli("escalations", "--topic", "alpha")      # 不带 tp_ 也认
        self.assertIn(mine, out)
        rc, out, _ = self.cli("escalations")
        self.assertIn(theirs, out)
        rc, out, _ = self.cli("escalations", "--all", "--topic", "tp_alpha")   # --all 也只列这个会话的
        self.assertIn(mine, out)
        self.assertNotIn(theirs, out)
        self.resolve("--message-id", mine, "--why", "办完了")
        rc, out, _ = self.cli("escalations", "--topic", "tp_alpha")
        self.assertIn("tp_alpha 名下没有未结的「需要你定」", out)

    def test_owner_reaction_on_source_marks(self):
        a = self.alice_batch()
        q = self.asked()
        self.store.add_owner_reaction(a, "THUMBSUP", ME, "rc_test_1", reported=False)   # 第一次检查的基线：不算
        self.d.dispatch_once()
        self.assertIsNone(self.store.get_escalation(q)["possibly_handled_at"])
        self.store.conn.execute("DELETE FROM owner_reactions")
        self.store.conn.commit()
        with later(5):
            self.store.add_owner_reaction(a, "THUMBSUP", ME, "rc_test_2", reported=True)
        self.store.conn.execute("UPDATE owner_reactions SET seen_at=?",
                                ((datetime.now().astimezone() + timedelta(minutes=5)).isoformat(timespec="seconds"),))
        self.store.conn.commit()
        self.d.dispatch_once()
        e = self.store.get_escalation(q)
        self.assertEqual((e["possibly_handled_reason"], e["possibly_handled_message_id"]), ("owner_reaction", a))
        self.assertIn("点了 THUMBSUP", self.system_msgs("escalation_check")[0])

    def test_sibling_answered_or_resolved_marks_the_other_session(self):
        a = self.alice_batch()
        q1 = self.asked("--about", a, title="Felix 接码")
        q2 = self.asked("--about", a, topic=BUS_TOPIC_ID, title="Felix 接码（总线）")
        self.assertIn("已经在问", self.last_out)          # 重复提问：照发、提示
        self.assertIn("tp_alpha", self.last_out)
        self.ask_earlier(q1, q2)
        # 主人引用回复 tp_alpha 那条
        ans = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="接好了", at=at(3))
        self.lark.msgs[ans]["thread_root"] = ""
        m = parse_message(self.lark.raw(ans))
        m.reply_to = q1
        Collector(self.cfg, self.store, self.source, self.roster).ingest([m])
        self.ack("tp_alpha")
        self.deliver()
        self.assertEqual(self.store.get_escalation(q1)["close_reason"], "answered")
        self.d.dispatch_once()
        e2 = self.store.get_escalation(q2)
        self.assertEqual((e2["possibly_handled_reason"], e2["possibly_handled_message_id"]), ("sibling_answered", q1))
        bus = self.system_msgs("escalation_check", BUS_TOPIC_ID)
        self.assertEqual(len(bus), 1)
        self.assertIn("同来源的另一条提问", bus[0])

    def test_sibling_resolved(self):
        a = self.alice_batch()
        q1 = self.asked("--about", a)
        q2 = self.asked("--about", a, topic=BUS_TOPIC_ID, title="同一件事")
        self.ask_earlier(q1, q2)
        self.resolve("--message-id", q1, "--why", "主人在私聊里定了")
        with later(2):
            self.d.dispatch_once()
        e2 = self.store.get_escalation(q2)
        self.assertEqual(e2["possibly_handled_reason"], "sibling_resolved")
        self.assertIn("主人在私聊里定了", self.system_msgs("escalation_check", BUS_TOPIC_ID)[0])

    def test_same_session_batch_sources_do_not_mark_each_other(self):
        self.alice_batch()
        q1 = self.asked(title="第一件", text="A？")
        q2 = self.asked(title="第二件", text="B？")
        self.ask_earlier(q1, q2)
        self.resolve("--message-id", q1, "--why", "办完了")
        with later(2):
            self.d.dispatch_once()
        self.assertIsNone(self.store.get_escalation(q2)["possibly_handled_at"])

    def test_expiring_reminder_once_and_off(self):
        q = self.asked()
        self.store.conn.execute("UPDATE escalations SET asked_at=? WHERE message_id=?",
                                ((datetime.now().astimezone() - timedelta(hours=22, minutes=30)).isoformat(
                                    timespec="seconds"), q))
        self.store.conn.commit()
        self.ack("tp_alpha")
        text = self.deliver()
        self.assertIn("⏰ 你的「需要你定」快到期了", text)
        self.assertIn(f"FX-ESC-EXPIRING topic=tp_alpha ask={q} hours=1.5", text)
        for _ in range(2):
            self.deliver()
        self.assertEqual(len(self.system_msgs("escalation_expiring")), 1)
        # 到期照旧关闭
        with later(120):
            self.d.dispatch_once()
        self.assertEqual(self.store.get_escalation(q)["close_reason"], "expired")

    def test_expiring_off_when_zero_or_not_due(self):
        self.cfg.escalation.remind_before_hours = 0
        q = self.asked()
        self.store.conn.execute("UPDATE escalations SET asked_at=? WHERE message_id=?",
                                ((datetime.now().astimezone() - timedelta(hours=23)).isoformat(timespec="seconds"), q))
        self.store.conn.commit()
        self.d.dispatch_once()
        self.assertEqual(self.system_msgs("escalation_expiring"), [])
        self.cfg.escalation.remind_before_hours = 2
        q2 = self.asked(title="新问题", text="刚问的")
        self.d.dispatch_once()
        got = self.system_msgs("escalation_expiring")
        self.assertEqual(len(got), 1)              # 打开以后只提醒到了点的那条，刚问的不提醒
        self.assertIn(q, got[0])
        self.assertNotIn(q2, got[0])

    def test_check_reminder_does_not_answer_the_escalation(self):
        self.alice_batch()
        q = self.asked()
        self.ack("tp_alpha")
        self.owner_says("下周一")
        self.deliver()
        self.deliver()
        e = self.store.get_escalation(q)
        self.assertEqual((e["closed_at"], e["answered_at"]), (None, None))


# ======================= D：待办清单 =======================
class PendingListTest(PendingBase):
    def scene(self) -> dict:
        a = self.alice_batch()
        q_dup1 = self.asked("--about", a, title="Felix 接码")
        q_dup2 = self.asked("--about", a, topic=BUS_TOPIC_ID, title="Felix 接码（总线）")
        b = self.alice_batch("Leon 要 GitHub App", chat="oc_test_alpha_grp")
        q_done = self.asked("--about", b, title="Leon GitHub App")
        self.owner_says("GitHub App 我已经批了", chat="oc_test_alpha_grp")
        q_plain = self.asked("--about", self.alice_batch("要不要延期"), title="延期")
        self.d.dispatch_once()
        self.cli("work", "set", "--topic", "tp_alpha", "--key", "AGIF-9", "--title", "等主人看方案", "--state", "等主人")
        self.cli("work", "set", "--topic", "tp_alpha", "--key", "AGIF-1", "--title", "老的等主人", "--state", "等主人")
        old = (datetime.now().astimezone() - timedelta(hours=5)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE work_items SET declared_at=?, updated_at=? WHERE key='AGIF-1'", (old, old))
        self.store.conn.commit()
        return {"dup": (q_dup1, q_dup2), "done": q_done, "plain": q_plain}

    def test_pending_single_section_with_check_line(self):
        # 7.38 第 1 条：可能已处理的不挪出「要你定的」，条目下加一行「↳ 你几点在哪说过：……，已提醒 <会话> 核实」
        ids = self.scene()
        rc, out, _ = self.cli("pending")
        self.assertEqual(rc, 0)
        first_line = out.splitlines()[0]
        self.assertTrue(first_line.startswith("待办清单（"))
        self.assertIn("要你定的 4 条（其中 1 条你后来在来源聊天说过话，已提醒会话核实）", first_line)
        self.assertNotIn("可能已经处理了", out)
        i1, i2 = out.index("一、要你定的（4）"), out.index("二、各会话在等你")
        first, second = out[i1:i2], out[i2:]
        self.assertIn("同一件事 2 个提问", first)
        self.assertIn("「Felix 接码」", first)
        self.assertIn("「延期」", first)
        self.assertIn("「Leon GitHub App」", first)
        leon = first[first.index("「Leon GitHub App」"):]
        self.assertIn("↳ 你 ", leon)
        self.assertIn("说过：「GitHub App 我已经批了」，已提醒 tp_alpha「Alpha 需求」核实", leon)
        self.assertEqual(first.count("↳ "), 1)
        self.assertNotIn("message_id", first)            # 给主人看的清单不带消息号
        self.assertIn("AGIF-9 等主人看方案", second)
        self.assertIn("AGIF-1 老的等主人", second)
        self.assertIn("⚠ 可能过期", second)
        self.assertNotIn("自动·需要你定", second)     # 自动补的「需要你定」就是第一部分那些，不重复列
        rc, js, _ = self.cli("pending", "--json")
        data = json.loads(js)
        self.assertNotIn("possibly_handled", data)
        self.assertEqual((data["decide_count"], data["possibly_handled_count"]), (4, 1))
        self.assertEqual([len(g["asks"]) for g in data["decide"]], [2, 1, 1])
        self.assertEqual({a["message_id"] for a in data["decide"][0]["asks"]}, set(ids["dup"]))
        leon_ask = data["decide"][1]["asks"][0]
        self.assertEqual(leon_ask["message_id"], ids["done"])
        self.assertEqual((leon_ask["possibly_handled"]["reason"], leon_ask["possibly_handled"]["notified"]),
                         ("owner_spoke", True))
        self.assertEqual([w["key"] for w in data["waiting_owner"]], ["AGIF-9", "AGIF-1"])

    def test_closed_session_ask_is_followed_by_the_bus(self):
        # 回放发现 tp_sammy（已收掉）名下还开着一条；7.38 合并第三轮：由总线跟进，提醒发给总线，topic_id 不改
        a = self.alice_batch()
        q = self.asked("--about", a)
        q2 = self.asked(title="第二件", text="另一件事")
        self.store.update_topic("tp_alpha", state="closed")
        self.store.conn.execute("DELETE FROM escalation_sources WHERE ask_message_id=?", (q2,))
        self.store.conn.commit()
        self.owner_says("这事我办了")
        pend.followup_round(self.cfg, self.store, self.d.playbook)   # 不走 dispatch_once：名册同步会把 tp_alpha 重新打开
        e = self.store.get_escalation(q)
        self.assertEqual(e["topic_id"], "tp_alpha")
        self.assertTrue(e["possibly_handled_notified_at"])
        self.assertEqual(self.system_msgs("escalation_check", "tp_alpha"), [])
        bus = self.system_msgs("escalation_check", BUS_TOPIC_ID)
        self.assertEqual(len(bus), 1)
        self.assertTrue(bus[0].startswith("🔎 tp_alpha「Alpha 需求」的「需要你定」可能已经处理了：它 "))
        self.assertIn("原提问会话 tp_alpha「Alpha 需求」已经收掉，这件事由你（总线）跟进", bus[0])
        self.assertIn(f"`sheepdog resolve --topic tp_bus --message-id {q} --why", bus[0])
        self.assertNotIn("FX-ESC-CHECK", bus[0])          # 通用说明里的 --key 总线用不了，不附
        rc, out, _ = self.cli("pending")
        self.assertIn("说过：「这事我办了」，已提醒总线核实（原会话已收掉）", out)
        self.assertIn("⚠ 原会话已收掉，由总线跟进", out)
        self.assertNotIn("会提醒 tp_alpha", out)
        self.assertIn("（其中 1 条你后来在来源聊天说过话，已提醒会话核实）", out.splitlines()[0])

    def test_closed_session_expiring_goes_to_the_bus(self):
        q = self.asked()
        self.store.conn.execute("UPDATE escalations SET asked_at=? WHERE message_id=?",
                                ((datetime.now().astimezone() - timedelta(hours=23)).isoformat(timespec="seconds"), q))
        self.store.update_topic("tp_alpha", state="closed")
        pend.followup_round(self.cfg, self.store, self.d.playbook)
        self.assertEqual(self.system_msgs("escalation_expiring", "tp_alpha"), [])
        bus = self.system_msgs("escalation_expiring", BUS_TOPIC_ID)
        self.assertEqual(len(bus), 1)
        self.assertIn("原提问会话 tp_alpha「Alpha 需求」已经收掉", bus[0])
        self.assertIn(f"--topic tp_bus --message-id {q}", bus[0])
        self.assertRegex(bus[0], r"约 (59 分钟|1 小时)后")

    def test_bus_resolves_only_orphaned_asks(self):
        orphan = self.asked()
        self.store.update_topic("tp_alpha", state="closed")
        rc, out, err = self.resolve("--message-id", orphan, "--why", "主人在私聊里定了", topic=BUS_TOPIC_ID)
        self.assertEqual(rc, 0, err)
        e = self.store.get_escalation(orphan)
        self.assertEqual((e["close_reason"], e["resolved_by"], e["topic_id"]), ("resolved", BUS_TOPIC_ID, "tp_alpha"))
        self.assertTrue(_opt(self.lark.sent("+messages-reply")[-1], "--text").startswith("✅ 已结案：主人在私聊里定了（"))
        # 原会话还在的：总线照旧不能结
        self.store.update_topic("tp_alpha", state="active")
        alive = self.asked(title="别的", text="还开着的")
        rc, _, err = self.resolve("--message-id", alive, "--why", "x", topic=BUS_TOPIC_ID)
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertIn("总线只能结原会话已收掉的", err)
        self.assertIsNone(self.store.get_escalation(alive)["closed_at"])

    def test_check_line_before_reminder_is_queued(self):
        # 还没轮到提醒（只读现算出来的）：写「下一轮会提醒」，不说已提醒
        self.alice_batch()
        self.asked()
        self.owner_says("下周一上")
        rc, out, _ = self.cli("pending")
        self.assertIn("，下一轮会提醒 tp_alpha「Alpha 需求」核实", out)
        self.assertIn("（其中 1 条你后来在来源聊天说过话，下一轮会提醒会话核实）", out.splitlines()[0])

    def test_same_session_same_batch_asks_are_not_grouped(self):
        # 10-13 的 kc 测试库和 Leon GitHub App：总线同一批里问的两件事，来源是同一批消息，不是同一件事
        self.alice_batch()
        self.asked(title="第一件", text="A？")
        self.asked(title="第二件", text="B？")
        data = json.loads(self.cli("pending", "--json")[1])
        self.assertEqual([len(g["asks"]) for g in data["decide"]], [1, 1])

    def test_pending_is_read_only(self):
        self.scene()
        from test_threads import diff_tables, snapshot
        before = snapshot(self.cfg.db_path)
        self.cli("pending")
        self.cli("pending", "--json")
        self.assertEqual(diff_tables(before, snapshot(self.cfg.db_path)), set())

    def owner_top(self, text: str) -> str:
        mid = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content=text, at=at(5))
        self.pull(mid)
        return mid

    def test_owner_says_daiban_bot_replies_list_and_nothing_is_dispatched(self):
        self.cfg.escalation.header_regex = PROD_HEADER_RX
        ids = self.scene()
        self.deliver()
        before_alpha = len(self.sink.to("conv_test_alpha"))
        before_bus = len(self.sink.to("conv_test_bus"))
        q = self.owner_top("待办")
        r = self.row(q)
        self.assertEqual((r["route"], r["reason"]), ("drop", "pending_query"))
        call = self.lark.sent("+messages-reply")[-1]
        self.assertEqual((_opt(call, "--message-id"), _opt(call, "--as")), (q, "bot"))
        self.assertNotIn("--reply-in-thread", call)
        text = _opt(call, "--text")
        self.assertTrue(text.startswith("📋 待办清单（"))
        self.assertIn("Felix 接码", text)
        self.assertIsNone(self.cfg.escalation.pattern.search(text.splitlines()[0]))
        # 清单拉回来：不登记「需要你定」、不投给任何会话；「待办」本身也没回答哪条提问
        reply = [m for m in self.lark.msgs if self.lark.msgs[m]["content"] == text][0]
        self.pull(reply)
        self.assertEqual((self.row(reply)["route"], self.row(reply)["reason"]), ("drop", "sheepdog_pending"))
        self.assertIsNone(self.store.get_escalation(reply))
        self.assertIsNone(self.store.get_bot_outbox(reply))
        for _ in range(2):
            self.deliver()
        self.assertEqual(len(self.sink.to("conv_test_alpha")), before_alpha)
        self.assertEqual(len(self.sink.to("conv_test_bus")), before_bus)
        self.assertEqual({self.store.get_escalation(x)["closed_at"] for x in ids["dup"]}, {None})
        # 主人引用这份清单回复：不会被当成回答哪条提问
        self.assertIsNone(self.store.get_escalation(reply))
        # 同一条「待办」再拉到一次：不再回
        self.pull(q)
        self.assertEqual(len([c for c in self.lark.sent("+messages-reply") if _opt(c, "--message-id") == q]), 1)

    def test_daiban_in_thread_or_quote_is_a_normal_reply(self):
        q = self.asked("--key", "K1")
        mid = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="待办", thread_root=q, at=at(3))
        self.pull(mid)
        self.assertEqual((self.row(mid)["reason"], self.row(mid)["topic_id"]), ("owner_reply", "tp_alpha"))
        self.assertEqual(self.lark.sent("+messages-reply"), [])

    def test_other_words_are_not_the_query(self):
        self.asked()
        mid = self.owner_top("待办的那件事按 A 办")
        self.assertEqual(self.row(mid)["reason"], "owner_reply")
        self.assertEqual(self.lark.sent("+messages-reply"), [])

    def test_empty_list(self):
        rc, out, _ = self.cli("pending")
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("（无）"), 2)
        self.assertIn("要你定的 0 条，各会话在等你的 0 件", out)
