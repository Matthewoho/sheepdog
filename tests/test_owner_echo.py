"""7.52 主人的话一定有回音。

数据照正式账本的样子造（7.45 / 7.48 的假飞书）：主人在找主人聊天里引用提问回复、在提问的话题里回复；主人在群里 @ 机器人
（mentions 里是 app_id）。会话交回执写 receipts/<topic>/<批次>.json（summary 必填，正式账本 317 条回执没有空的）。

覆盖：
1 回执带回：交回执、从送到起没 ask / report → 一张卡片；自己 report 过 → 不带回；插话那几句跟下一次回执带回；
  needs_decision / waiting_external 写状态；发到哪（话题里、引用主人那句、群里 @ 的指令 → 找主人聊天新起一条）；
  每批一次；开关关掉不带回；晚到的回执也带回；带回卡片拉回来认得出会话、不登记「需要你定」。
1b 推迟带回（owner_relay_after_minutes）：每句只绑送它进去那一批的回执（插话：送到后的第一份），推迟期间后来的回执不改它；
  推迟期间会话 report 了就不发；转交以后绑过的作废、按新会话那一批重绑；同一份回执、同一个去处有一句到点就一张卡片全带上。
2 回音账本：入账记欠账；没送进会话当场报、排队超时报、送到没回音报，各一次、不重复；牧羊犬自己处理的「待办」不记账；
  转交以后跟着换会话。
"""

import json
from datetime import timedelta
from unittest import mock

from sheepdog import session as sm
from sheepdog.config import ConfigError, load_config
from sheepdog.engine import BUS_TOPIC_ID, forward_messages, write_receipt
from sheepdog.notify import OwnerDM

from test_group_threads import BETA_P2P, BOB, OPS, GroupBase
from test_identity import APP
from test_roster import ME, later
from test_threads import ALICE, ALPHA_P2P, ESC_CHAT, now8


class FakeDM:
    """给主人的通知渠道（同 OwnerDM.send 的参数）：只记下来。"""
    ledger = True

    def __init__(self):
        self.calls: list[dict] = []

    def send(self, text, idempotency_key="", reply_to="", in_thread=False):
        self.calls.append({"text": text, "key": idempotency_key, "reply_to": reply_to, "in_thread": in_thread})

    def texts(self, word: str = "") -> list[str]:
        return [c["text"] for c in self.calls if word in c["text"]]


class EchoBase(GroupBase):
    def setUp(self):
        super().setUp()
        self.lark.chat_types.update({ESC_CHAT: "p2p", ALPHA_P2P: "p2p"})
        self.dm = FakeDM()
        self.d.notifier = self.dm

    def relays(self) -> list[dict]:
        return [c for c in self.dm.calls if c["text"].startswith("✅ ")]

    def asked(self, topic="tp_alpha", key="PR #1", title="发版时间", text="周五还是下周一？") -> str:
        before = set(self.lark.msgs)
        rc, out, err = self.ask("--key", key, topic=topic, title=title, text=text)
        self.assertEqual(rc, 0, out + err)
        mid = next(m for m in self.lark.msgs if m not in before)
        self.pull(mid)
        self.assertIsNotNone(self.store.get_escalation(mid))
        return mid

    def owner(self, text="周五发", quote="", thread_root="") -> str:
        mid = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content=text, chat_type="p2p", quote=quote,
                            thread_root=thread_root, at=now8())
        self.pull(mid)
        return mid

    def receipt(self, tid="tp_alpha", status="handled", summary="已按周五排期，发版单已提交"):
        t = self.store.get_topic(tid)
        write_receipt(self.cfg, tid, t["pending_batch_id"], {"status": status, "summary": summary})
        return t["pending_batch_id"]

    def echo(self, mid):
        return self.store.echo_get(mid)


# ======================= 1 回执带回 =======================
class RelayTest(EchoBase):
    def test_receipt_without_report_relays_one_card_quoting_owner(self):
        """回归 7.52：会话交了回执、没给主人发 ask / report，主人在飞书上收不到任何回音（10-14 起 26 条）。"""
        q = self.asked()
        a = self.owner("周五发", quote=q)
        self.assertEqual((self.row(a)["reason"], self.row(a)["topic_id"]), ("owner_reply", "tp_alpha"))
        self.assertEqual((self.echo(a)["stage"], self.echo(a)["kind"]), ("queued", "owner_reply"))
        self.d.dispatch_once()
        self.assertEqual(self.echo(a)["stage"], "delivered")
        batch = self.receipt()
        self.d.dispatch_once()
        (c,) = self.relays()
        lines = c["text"].split("\n")
        self.assertEqual(lines[0], "✅ Alpha 需求 处理了你的话 [tp_alpha]")
        hhmm = now8().strftime("%H:%M")
        self.assertIn(f"你 {hhmm} 说的「周五发」", lines)
        self.assertIn("已按周五排期，发版单已提交", lines)
        self.assertEqual(lines[-1], "状态：已处理")
        self.assertEqual((c["reply_to"], c["in_thread"]), (a, False))          # 回复主人那一条（带引用）
        self.assertEqual((self.echo(a)["stage"], self.echo(a)["settled_by"]), ("settled", "relay"))
        self.assertTrue(self.store.get_meta(f"owner_relay:{batch}:main:{ESC_CHAT}"))   # 每批、每个去处一次
        with later(30):
            self.d.dispatch_once()
        self.assertEqual(len(self.relays()), 1)                               # 每批一次，之后也不再报没回音
        self.assertEqual(self.dm.texts("还没回你"), [])

    def test_session_reported_no_relay(self):
        q = self.asked()
        a = self.owner("周五发", quote=q)
        self.d.dispatch_once()
        with later(1):
            rc, out, err = self.ask("--key", "PR #1", kind="report", title="进度", text="收到，周五发")
        self.assertEqual(rc, 0, out + err)
        self.store.conn.execute("UPDATE sends SET sent_at=? WHERE kind='report'",
                                ((now8() + timedelta(minutes=1)).isoformat(timespec="seconds"),))
        self.store.conn.commit()
        self.receipt()
        self.d.dispatch_once()
        self.assertEqual(self.relays(), [])
        self.assertEqual(self.echo(a)["settled_by"], "report")

    def test_thread_reply_relayed_into_the_thread(self):
        q = self.asked()
        a = self.owner("下周一吧", thread_root=q)
        self.assertEqual(self.row(a)["topic_id"], "tp_alpha")
        self.d.dispatch_once()
        self.receipt()
        self.d.dispatch_once()
        (c,) = self.relays()
        self.assertEqual((c["reply_to"], c["in_thread"]), (a, True))

    def test_needs_decision_and_waiting_status(self):
        for status, label in (("needs_decision", "等你定"), ("waiting_external", "在等别人")):
            with self.subTest(status=status):
                q = self.asked(key=f"K-{status}")
                self.owner(f"看看 {status}", quote=q)
                self.d.dispatch_once()
                self.receipt(status=status, summary=f"{status} 的说明")
                self.d.dispatch_once()
                c = self.relays()[-1]
                self.assertTrue(c["text"].endswith(f"状态：{label}"), c["text"])
                t = self.store.get_topic("tp_alpha")
                if t["state"] != sm.ACTIVE:
                    self.store.update_topic("tp_alpha", state=sm.ACTIVE)

    def test_priority_words_relayed_with_next_receipt(self):
        q = self.asked()
        busy = self.say(ALPHA_P2P, sender=ALICE, content="Alice 的需求")
        self.pull(busy)
        self.d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.RUNNING)
        a = self.owner("插一句：先别发", quote=q)
        res = self.d.dispatch_once()
        self.assertTrue(res["topics"]["tp_alpha"]["priority"].startswith("p"))
        self.assertEqual((self.echo(a)["stage"], self.echo(a)["priority"]), ("delivered", 1))
        self.receipt(summary="先暂停发版，等主人再说")                         # 原来那批的回执
        self.d.dispatch_once()
        (c,) = self.relays()
        self.assertIn("「插一句：先别发」", c["text"])
        self.assertIn("先暂停发版，等主人再说", c["text"])
        self.assertEqual(self.echo(a)["settled_by"], "relay")

    def test_group_instruction_relayed_to_owner_chat_not_the_group(self):
        q = self.say(content="构建挂了", at=now8() - timedelta(minutes=8))
        self.pull(q)
        bot = self.bot_reply_for("tp_beta", q)
        mid = self.say(sender=ME, content="@Mattheuu 让 cicd 看一下", quote=bot, mentions=[APP])
        self.pull(mid)
        self.assertEqual((self.echo(mid)["kind"], self.echo(mid)["topic_id"]), ("owner_instruction", "tp_beta"))
        self.d.dispatch_once()
        self.receipt("tp_beta", summary="已转给 cicd，等他回")
        self.d.dispatch_once()
        (c,) = self.relays()
        self.assertEqual(c["reply_to"], "")                                    # 新起一条（OwnerDM 发找主人聊天）
        self.assertTrue(c["text"].startswith("✅ Beta 处理了你的话 [tp_beta]"))

    def test_switch_off_no_relay(self):
        self.cfg.session.owner_receipt_relay = False
        q = self.asked()
        a = self.owner("周五发", quote=q)
        self.d.dispatch_once()
        self.receipt()
        self.d.dispatch_once()
        self.assertEqual(self.relays(), [])
        self.assertEqual(self.echo(a)["stage"], "delivered")                   # 还欠着：到点照样报没回音
        self.cfg.session.owner_receipt_relay = True                           # 热改回开：这一批的回执已经收过，不补
        self.d.dispatch_once()
        self.assertEqual(self.relays(), [])

    def test_relay_after_minutes_waits_and_skips_if_session_reports(self):
        self.cfg.session.owner_relay_after_minutes = 10
        q = self.asked()
        a = self.owner("周五发", quote=q)
        self.d.dispatch_once()
        self.receipt()
        self.d.dispatch_once()
        self.assertEqual(self.relays(), [])                                  # 回执到了，还没到送到后 10 分钟
        self.assertEqual(self.echo(a)["relay_status"], "handled")
        with later(11):
            self.d.dispatch_once()
        self.assertEqual(len(self.relays()), 1)
        # 另一句：等的这 10 分钟里会话自己 report 了 → 不带回
        q2 = self.asked(key="PR #2")
        b = self.owner("下周一", quote=q2)
        with later(12):
            self.d.dispatch_once()
        self.receipt()
        with later(13):
            self.d.dispatch_once()
        self.store.add_send("k_test_rep", "report", "tp_alpha", "om_test_rep", "收到")
        self.store.conn.execute("UPDATE sends SET sent_at=? WHERE idem_key='k_test_rep'",
                                ((now8() + timedelta(minutes=15)).isoformat(timespec="seconds"),))
        self.store.conn.commit()
        with later(30):
            self.d.dispatch_once()
        self.assertEqual(len(self.relays()), 1)
        self.assertEqual(self.echo(b)["settled_by"], "report")

    def test_late_receipt_relays(self):
        q = self.asked()
        a = self.owner("周五发", quote=q)
        self.d.dispatch_once()
        batch = self.store.get_topic("tp_alpha")["pending_batch_id"]
        with later(25):
            self.d.dispatch_once()                                            # 回执超时
        write_receipt(self.cfg, "tp_alpha", batch, {"status": "handled", "summary": "晚了点，处理完了"})
        with later(26):
            self.d.dispatch_once()
        c = self.relays()
        self.assertEqual(len(c), 1, self.dm.calls)
        self.assertIn("晚了点，处理完了", c[0]["text"])
        self.assertEqual(self.echo(a)["settled_by"], "relay")

    def test_relay_card_pulled_back_is_attributed_not_an_escalation(self):
        q = self.asked()
        self.owner("周五发", quote=q)
        self.d.dispatch_once()
        self.receipt()
        self.d.dispatch_once()
        (c,) = self.relays()
        back = self.lark.add(chat_id=ESC_CHAT, sender_id=APP, sender_type="app", chat_type="p2p",
                             content='<card title="✅ Alpha 需求 处理了你的话 [tp_alpha]">\n' + "\n".join(
                                 c["text"].split("\n")[1:]) + "\n</card>", msg_type="interactive")
        self.pull(back)
        r = self.row(back)
        self.assertEqual(r["reason"], "escalation_bot_message")
        self.assertEqual(self.store.get_bot_outbox(back)["topic_id"], "tp_alpha")
        self.assertIsNone(self.store.get_escalation(back))
        self.assertIsNone(self.echo(back))

    def test_owner_dm_reply_to_goes_through_source_reply(self):
        src = mock.Mock()
        OwnerDM(src, ME, self.cfg).send("✅ Alpha 需求 处理了你的话 [tp_alpha]\n你 10:00 说的「周五发」\n好了\n状态：已处理",
                                        "sdr-key", reply_to="om_test_owner", in_thread=True)
        args, kw = src.reply_to.call_args
        self.assertEqual(args, ("om_test_owner",))
        self.assertEqual((kw["identity"], kw["idem_key"], kw["in_thread"]), ("bot", "sdr-key", True))
        self.assertEqual(kw["card"]["header"]["title"]["content"], "✅ Alpha 需求 处理了你的话 [tp_alpha]")
        src.send_text.assert_not_called()


# ======================= 1b 推迟带回：每句只绑送它进去那一批的回执 =======================
class DeferredRelayBindingTest(EchoBase):
    """10-17 04:59 正式环境：推迟 10 分钟带回，期间总线又交了 2 份回执，卡片引用 04:48 那句、正文却是 04:57 那份回执的
    summary。一句话的带回只绑送它进去那一批的回执（插话批次：送到后的第一份），绑上了后来的回执不改。"""

    def setUp(self):
        super().setUp()
        self.cfg.session.owner_relay_after_minutes = 10

    def unrelated_bus_batch(self, at: float, summary: str, status="handled") -> str:
        """一句和主人无关的话进总线、送成一批，总线交回执（at 分钟后送、at+1 分钟后收）。返回批次号。"""
        u = self.say(OPS, sender=ALICE, content=f"无关的事 {at}", mentions=[ME])
        self.pull(u)
        with later(at):
            self.d.dispatch_once()
        self.assertEqual(self.row(u)["topic_id"], BUS_TOPIC_ID)
        batch = self.receipt(BUS_TOPIC_ID, status=status, summary=summary)
        self.assertEqual(self.row(u)["batch_id"], batch)
        with later(at + 1):
            self.d.dispatch_once()
        return batch

    def test_regression_752_deferred_relay_keeps_its_own_batch_receipt(self):
        """回归 7.52：推迟带回的 10 分钟里，同一会话又交了 2 份不相干的回执，卡片里仍是送这句进去那一批的 summary 和状态。"""
        a = self.owner("可以跟 sammy 说一声，热修发布了")                      # 没引用 → 指令进总线（7.53）
        self.assertEqual((self.echo(a)["topic_id"], self.echo(a)["kind"]), (BUS_TOPIC_ID, "owner_instruction"))
        self.d.dispatch_once()
        b1 = self.store.get_topic(BUS_TOPIC_ID)["pending_batch_id"]
        self.assertEqual((self.echo(a)["stage"], self.echo(a)["batch_id"]), ("delivered", b1))
        self.receipt(BUS_TOPIC_ID, status="waiting_external", summary="已跟 Sammy 私聊说了，等他回")
        self.d.dispatch_once()
        self.assertEqual(self.relays(), [])                                  # 推迟：还没到送到后 10 分钟
        b2 = self.unrelated_bus_batch(3, "群绑定好了")
        b3 = self.unrelated_bus_batch(6, "收到别的会话的留言回执")
        self.assertEqual(len({b1, b2, b3}), 3)
        e = self.echo(a)
        self.assertEqual((e["relay_batch"], e["relay_status"], e["relay_summary"]),
                         (b1, "waiting_external", "已跟 Sammy 私聊说了，等他回"))   # 后来两份回执没改它
        with later(11):
            self.d.dispatch_once()
        (c,) = self.relays()
        self.assertIn("「可以跟 sammy 说一声，热修发布了」", c["text"])
        self.assertIn("已跟 Sammy 私聊说了，等他回", c["text"])
        self.assertTrue(c["text"].endswith("状态：在等别人"), c["text"])
        self.assertNotIn("群绑定好了", c["text"])
        self.assertNotIn("留言回执", c["text"])
        self.assertEqual(c["reply_to"], a)
        self.assertTrue(self.store.get_meta(f"owner_relay:{b1}:main:{ESC_CHAT}"))
        self.assertEqual(self.echo(a)["settled_by"], "relay")
        with later(30):
            self.d.dispatch_once()
        self.assertEqual(len(self.relays()), 1)

    def test_regression_752_priority_binds_first_receipt_after_delivery_only(self):
        """回归 7.52：插话批次没有自己的回执，送到后的第一份回执绑上它；推迟期间同一会话后来的回执不覆盖。"""
        q = self.asked()
        busy = self.say(ALPHA_P2P, sender=ALICE, content="Alice 的需求")
        self.pull(busy)
        self.d.dispatch_once()
        b1 = self.store.get_topic("tp_alpha")["pending_batch_id"]
        a = self.owner("插一句：先别发", quote=q)
        self.d.dispatch_once()
        self.assertTrue(self.echo(a)["batch_id"].startswith("p"))
        self.assertEqual(self.echo(a)["relay_batch"], None)                   # 插话送到时还没有回执可绑
        self.receipt(status="waiting_external", summary="先暂停发版，等运维确认窗口")   # 原来那批：送到后的第一份
        self.d.dispatch_once()
        self.assertEqual(self.echo(a)["relay_batch"], b1)
        self.assertEqual(self.relays(), [])
        later_q = self.say(ALPHA_P2P, sender=ALICE, content="Alice 的第二件事")
        self.pull(later_q)
        with later(3):
            self.d.dispatch_once()
        b2 = self.receipt(summary="Alice 的第二件事办了")
        self.assertNotEqual(b2, b1)
        with later(4):
            self.d.dispatch_once()
        e = self.echo(a)
        self.assertEqual((e["relay_batch"], e["relay_status"], e["relay_summary"]),
                         (b1, "waiting_external", "先暂停发版，等运维确认窗口"))
        with later(11):
            self.d.dispatch_once()
        (c,) = self.relays()
        self.assertIn("「插一句：先别发」", c["text"])
        self.assertIn("先暂停发版，等运维确认窗口", c["text"])
        self.assertTrue(c["text"].endswith("状态：在等别人"), c["text"])
        self.assertNotIn("Alice 的第二件事办了", c["text"])

    def test_regression_752_same_receipt_same_place_sent_together_when_first_is_due(self):
        """回归 7.52：同一份回执绑上的两句、回到同一个地方，到点时间不同（一句随这一批送到，一句是之后的插话）：先到点的
        那张卡片占了「每批、每个去处一次」，后到点的那句再也带不回，还被报「插话没回音」。要一张卡片带上两句。"""
        q = self.asked()
        a = self.owner("周五发", quote=q)
        self.d.dispatch_once()
        b1 = self.store.get_topic("tp_alpha")["pending_batch_id"]
        q2 = self.asked(key="PR #2", title="灰度比例", text="5% 还是 10%？")
        b = self.owner("10%", quote=q2)
        with later(4):
            self.d.dispatch_once()                                          # 插话：送到比 a 晚 4 分钟
        self.assertTrue(self.echo(b)["batch_id"].startswith("p"))
        self.receipt(summary="周五发、灰度 10%")
        with later(5):
            self.d.dispatch_once()
        self.assertEqual((self.echo(a)["relay_batch"], self.echo(b)["relay_batch"]), (b1, b1))
        with later(11):
            self.d.dispatch_once()                                          # a 到点，b 要到 14 分钟
        (c,) = self.relays()
        self.assertIn("「周五发」", c["text"])
        self.assertIn("「10%」", c["text"])
        self.assertIn("周五发、灰度 10%", c["text"])
        self.assertEqual(c["reply_to"], b)                                    # 同一个去处：引用最近的一句
        self.assertEqual((self.echo(a)["settled_by"], self.echo(b)["settled_by"]), ("relay", "relay"))
        for m in (15, 20, 40):
            with later(m):
                self.d.dispatch_once()
        self.assertEqual(len(self.relays()), 1)
        self.assertEqual(self.dm.texts("没回音"), [])

    def test_regression_752_session_reported_during_deferral_no_card(self):
        """回归 7.52：推迟期间来了不相干的回执，会话也自己 report 了 → 算还上，不发卡片（原有行为）。"""
        a = self.owner("可以跟 sammy 说一声")
        self.d.dispatch_once()
        b1 = self.receipt(BUS_TOPIC_ID, summary="已跟 Sammy 说了")
        self.d.dispatch_once()
        self.unrelated_bus_batch(3, "群绑定好了")
        self.assertEqual(self.echo(a)["relay_batch"], b1)
        self.store.add_send("k_test_rep_752", "report", BUS_TOPIC_ID, "om_test_rep_752", "跟 Sammy 说过了")
        self.store.conn.execute("UPDATE sends SET sent_at=? WHERE idem_key='k_test_rep_752'",
                                ((now8() + timedelta(minutes=5)).isoformat(timespec="seconds"),))
        self.store.conn.commit()
        with later(11):
            self.d.dispatch_once()
        self.assertEqual(self.relays(), [])
        self.assertEqual(self.echo(a)["settled_by"], "report")

    def test_regression_752_forward_after_binding_drops_the_old_receipt(self):
        """回归 7.52：已经绑上总线回执、还在推迟的那句，后来被转给 alpha：总线那份回执不再算它的，
        改绑 alpha 送它进去那一批的回执（绑上不改，只在重新送一次时作废）。"""
        a = self.owner("这句没引用")
        self.d.dispatch_once()
        b1 = self.receipt(BUS_TOPIC_ID, summary="先记下了")
        self.d.dispatch_once()
        self.assertEqual(self.echo(a)["relay_batch"], b1)
        forward_messages(self.store, "tp_alpha", [a], self_open_id=ME)          # 总线下一批里决定转给 alpha
        with later(2):
            self.d.dispatch_once()
        e = self.echo(a)
        self.assertEqual((e["topic_id"], e["stage"], e["relay_batch"], e["relay_summary"]),
                         ("tp_alpha", "delivered", None, None))
        with later(11):
            self.d.dispatch_once()
        self.assertEqual(self.relays(), [])                                  # alpha 还没交回执：不拿总线那份发
        a1 = self.receipt("tp_alpha", summary="alpha 办完了")
        with later(12):
            self.d.dispatch_once()
        self.assertEqual(self.echo(a)["relay_batch"], a1)
        with later(13):
            self.d.dispatch_once()
        (c,) = self.relays()
        self.assertTrue(c["text"].startswith("✅ Alpha 需求 处理了你的话 [tp_alpha]"), c["text"])
        self.assertIn("alpha 办完了", c["text"])
        self.assertNotIn("先记下了", c["text"])


# ======================= 2 回音账本 =======================
class EchoLedgerTest(EchoBase):
    def test_undelivered_reported_on_the_spot_once(self):
        """回归 7.52：主人 @ 机器人的话没送进任何会话（10-16 19:41），谁也没收到、也没人告诉他。"""
        self.cfg.routing.owner_at_assistant = False                         # @ 机器人没当成指令（7.45 以前的样子）
        self.lark.chat_types["oc_test_never_grp"] = "group"                   # 没人负责、也没人处理过的群
        mid = self.lark.add(chat_id="oc_test_never_grp", sender_id=ME, content="@Mattheuu 绑定一下那个 session",
                            chat_type="group", mentions=[APP])
        self.pull(mid)
        self.assertEqual(self.row(mid)["route"], "self")
        e = self.echo(mid)
        self.assertEqual((e["stage"], e["kind"]), ("undelivered", "owner_instruction"))
        self.d.dispatch_once()
        told = self.dm.texts("没送到任何会话")
        self.assertEqual(len(told), 1)
        self.assertIn("「@Mattheuu 绑定一下那个 session」", told[0])
        self.assertIn("路由 self", told[0])
        with later(60):
            self.d.dispatch_once()
        self.assertEqual(len(self.dm.texts("没送到任何会话")), 1)

    def test_queued_then_delivered_then_silent_each_once(self):
        """回归 7.52：主人的话排队、送到后没回音，没有任何提醒（只有插话批次有）。"""
        self.cfg.session.owner_priority = False                              # 不插队：排队等原来那批
        q = self.asked()
        busy = self.say(ALPHA_P2P, sender=ALICE, content="Alice 的需求")
        self.pull(busy)
        self.d.dispatch_once()
        b1 = self.store.get_topic("tp_alpha")["pending_batch_id"]
        a = self.owner("周五发", quote=q)
        self.d.dispatch_once()
        self.assertEqual(self.echo(a)["stage"], "queued")
        with later(9):
            self.d.dispatch_once()
        self.assertEqual(self.dm.texts("还在排队"), [])
        with later(11):
            self.d.dispatch_once()
        (q_told,) = self.dm.texts("还在排队")
        self.assertIn("还在排队等「Alpha 需求」（它在跑批次 `" + b1 + "`", q_told)
        with later(12):
            self.d.dispatch_once()
        self.assertEqual(len(self.dm.texts("还在排队")), 1)
        # 原来那批交了回执（只说了 Alice 的事）：主人这句随下一批送到
        self.cfg.session.owner_receipt_relay = False                         # 只看提醒：不让回执带回把账还上
        write_receipt(self.cfg, "tp_alpha", b1, {"status": "handled", "summary": "Alice 的事办了"})
        with later(13):
            self.d.dispatch_once()
        self.assertEqual(self.echo(a)["stage"], "delivered")
        with later(27):
            self.d.dispatch_once()
        self.assertEqual(self.dm.texts("还没回你"), [])
        with later(29):
            self.d.dispatch_once()
        (r_told,) = self.dm.texts("还没回你")
        self.assertIn("「周五发」", r_told)
        self.assertIn("「Alpha 需求」在处理，15 分钟还没回你", r_told)
        with later(60):
            self.d.dispatch_once()
        self.assertEqual(len(self.dm.texts("还没回你")), 1)
        self.assertEqual(self.dm.texts("插话没回音"), [])                     # 不会再收到插话那种提醒
        self.assertEqual(set(self.echo(a)["alerted"].split(",")), {"queued", "reply"})

    def test_pending_shortcut_not_recorded(self):
        mid = self.owner("待办")
        self.assertEqual(self.row(mid)["reason"], "pending_query")
        self.assertIsNone(self.echo(mid))

    def test_forward_moves_the_debt(self):
        mid = self.owner("这句没引用")                                      # 没有开着的提问 → 总线
        self.assertEqual(self.echo(mid)["topic_id"], BUS_TOPIC_ID)
        self.d.dispatch_once()
        self.assertEqual(self.echo(mid)["stage"], "delivered")
        forward_messages(self.store, "tp_alpha", [mid], self_open_id=ME)
        self.d.dispatch_once()
        e = self.echo(mid)
        self.assertEqual((e["topic_id"], e["stage"]), ("tp_alpha", "delivered"))
        self.receipt(BUS_TOPIC_ID, summary="转给 alpha 了")
        self.d.dispatch_once()
        self.assertEqual(self.relays(), [])                                   # 总线的回执不替 alpha 还账
        self.receipt("tp_alpha", summary="alpha 办完了")
        self.d.dispatch_once()
        (c,) = self.relays()
        self.assertIn("alpha 办完了", c["text"])

    def test_zero_minutes_and_switches(self):
        self.cfg.session.owner_reply_alert_minutes = 0
        q = self.asked()
        a = self.owner("周五发", quote=q)
        self.d.dispatch_once()
        with later(60):
            self.d.dispatch_once()
        self.assertEqual(self.dm.texts("还没回你"), [])
        self.assertIn("reply", self.echo(a)["alerted"])

    def test_config_keys(self):
        from pathlib import Path
        p = Path(self.tmp.name) / "c.toml"
        p.write_text(f'self_open_id = "{ME}"\n', encoding="utf-8")
        s = load_config(p).session
        self.assertEqual((s.owner_receipt_relay, s.owner_undelivered_alert, s.owner_queue_alert_minutes,
                          s.owner_reply_alert_minutes, s.owner_priority_followup_minutes, s.owner_relay_after_minutes),
                         (True, True, 10, 15, 10, 0))
        for bad in ("owner_receipt_relay = 1", 'owner_undelivered_alert = "no"', "owner_queue_alert_minutes = -1",
                    "owner_reply_alert_minutes = 1.5", "owner_relay_after_minutes = -3"):
            with self.subTest(bad=bad):
                p.write_text(f'self_open_id = "{ME}"\n[session]\n{bad}\n', encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(p)

    def test_ignored_group_at_bot_not_recorded(self):
        """ignore 的群里主人 @ 机器人：7.56 一处判定说不管，回音账本也不记、不报「没送到」。"""
        self.cfg.routing.ignore_chat_ids.append(OPS)
        mid = self.say(sender=ME, content="@Mattheuu 测试群里随便说", mentions=[APP])
        self.pull(mid)
        self.assertIsNone(self.echo(mid))
        self.d.dispatch_once()
        self.assertEqual(self.dm.texts("没送到任何会话"), [])
