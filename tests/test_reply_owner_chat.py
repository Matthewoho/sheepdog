"""10-13 热修：会话误用 sheepdog reply 以主人身份把代回卡片发进了「找主人」聊天（主人和机器人的私聊），
这条代回又被当成主人亲自回复（owner_reply）投回会话。

1 sheepdog reply 的目标消息在找主人聊天里（[escalation] chat_ids、[threads] owner_chat_id）：拒绝，退出码 2（参数不对），
  stderr 说回主人用 sheepdog report --topic … --key …；账本不变、不调飞书。账本里没有目标消息时照旧。
2 找主人聊天里，发送人是主人、但正文带代回标记的消息：不算主人回复，不投递（self / agent_reply_in_owner_chat）。
"""

from datetime import timedelta

from sheepdog import outbound
from sheepdog.config import OwnerContextConfig

from test_roster import ME
from test_threads import ESC_CHAT, MARK, TEST_GROUP, ThreadsBase, diff_tables, now8, snapshot


class OwnerChatBase(ThreadsBase):
    def setUp(self):
        super().setUp()
        # 会话 tp_alpha 找过主人；主人在找主人聊天里回了一句，送回 tp_alpha
        rc, out, _ = self.ask("--key", "PR #12")
        self.assertEqual(rc, 0, out)
        self.ask_mid = next(k for k, v in self.lark.msgs.items() if v["chat_id"] == ESC_CHAT)
        self.pull(self.ask_mid)
        self.owner = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="周五发", at=now8() - timedelta(minutes=2))
        self.pull(self.owner)
        r = self.row(self.owner)
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), ("dispatch", "owner_reply", "tp_alpha"))
        self.deliver()
        self.lark.calls.clear()

    def reply(self, to: str, *extra) -> tuple[int, str, str]:
        return self.cli("reply", "--topic", "tp_alpha", "--to", to, "--card-text", "收到，周五发", *extra)


class RejectReplyInOwnerChatTest(OwnerChatBase):
    def test_reply_to_owner_in_owner_chat_is_rejected_and_changes_nothing(self):
        before = snapshot(self.cfg.db_path)
        for extra in ((), ("--even-if-owner-replied",)):
            rc, out, err = self.reply(self.owner, *extra)
            self.assertEqual((rc, out), (outbound.EXIT_INVALID, ""))
            self.assertIn("找主人", err)
            self.assertIn("sheepdog report --topic tp_alpha --key", err)
            self.assertIn("sheepdog reply 只用来以主人身份回别人", err)
        self.assertEqual(self.lark.calls, [])                              # 不发飞书，连发前检查都不做
        self.assertEqual(diff_tables(before, snapshot(self.cfg.db_path)), set())   # 账本一行不变

    def test_reply_to_bot_message_in_owner_chat_is_rejected_too(self):
        rc, _, err = self.reply(self.ask_mid)
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertIn("sheepdog report", err)
        self.assertEqual(self.lark.sent(), [])

    def test_outbound_raises_invalid(self):
        with self.assertRaises(outbound.OutboundError) as cm:
            outbound.reply(self.cfg, self.store, self.source, "tp_alpha", self.owner, card_text="好")
        self.assertEqual(cm.exception.code, outbound.EXIT_INVALID)

    def test_threads_owner_chat_id_counts_as_owner_chat(self):
        self.cfg.threads.owner_chat_id = TEST_GROUP
        q = self.lark.add(chat_id=TEST_GROUP, chat_type="group", sender_id=ME, content="测试群里主人说的")
        self.pull(q)
        with self.assertRaises(outbound.OutboundError) as cm:
            outbound.reply(self.cfg, self.store, self.source, "tp_alpha", q, card_text="好")
        self.assertEqual(cm.exception.code, outbound.EXIT_INVALID)
        self.assertEqual(self.lark.sent(), [])

    def test_test_env_rejects_too(self):
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8").replace(
            "[escalation]", '[source]\nenabled = false\n[notify]\nfile = "notices.md"\n[escalation]'), encoding="utf-8")
        rc, _, err = self.reply(self.owner)
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertIn("sheepdog report", err)
        self.assertFalse((self.cfg.state_dir / "notices.md").exists())

    def test_other_chats_and_unknown_targets_unchanged(self):
        # 别人的私聊照常代回
        q = self.lark.add(chat_id="oc_test_alpha_p2p", sender_id="ou_test_alice", content="能给结论吗",
                          at=now8() - timedelta(minutes=3))
        self.pull(q)
        rc, out, _ = self.reply(q)
        self.assertEqual(rc, 0, out)
        # 账本里没有目标消息：照旧（不带 --even-if-owner-replied 拒绝；带了照发）
        rc, _, err = self.reply("om_test_not_in_ledger")
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertIn("账本里没有", err)
        n = len(self.lark.sent())
        self.lark.add(chat_id="oc_test_x", sender_id="ou_test_alice", content="x", message_id="om_test_not_in_ledger")
        rc, out, _ = self.reply("om_test_not_in_ledger", "--even-if-owner-replied")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.lark.sent()), n + 1)


class AgentReplyInOwnerChatTest(OwnerChatBase):
    def test_agent_reply_card_is_not_owner_reply(self):
        agent = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content=f"<card>\n收到，周五发\n{MARK}\n</card>")
        plain = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="再补一句：周五下午")
        self.pull(agent, plain)
        r = self.row(agent)
        self.assertEqual((r["route"], r["reason"], r["topic_id"], r["dispatch_state"]),
                         ("self", "agent_reply_in_owner_chat", None, None))
        self.assertEqual((self.row(plain)["route"], self.row(plain)["reason"]), ("dispatch", "owner_reply"))  # 主人亲自说的照旧
        self.deliver()
        self.d.dispatch_once()
        everything = "\n".join(c for _, c in self.sink.sent)
        self.assertIn("再补一句", everything)
        self.assertNotIn(f"收到，周五发\n{MARK}", everything)

    def test_recognized_by_reply_suffix_without_agent_markers(self):
        self.cfg.owner_context = OwnerContextConfig(enabled=True, agent_markers=[], reaction_check_minutes=0)
        agent = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content=f"收到\n{MARK}")
        self.pull(agent)
        self.assertEqual(self.row(agent)["reason"], "agent_reply_in_owner_chat")

    def test_scenario_session_misuses_reply_then_old_style_message_pulled(self):
        # 会话误用 reply 回主人：被拒、账本不变、不发飞书
        before = snapshot(self.cfg.db_path)
        rc, _, err = self.reply(self.owner)
        self.assertEqual(rc, outbound.EXIT_INVALID)
        self.assertEqual(self.lark.calls, [])
        self.assertEqual(diff_tables(before, snapshot(self.cfg.db_path)), set())
        # 旧版本发出去的那种代回（主人身份、带标记、回复主人的话）被拉到：不再投递
        old = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content=f"<card>\n收到，周五发\n{MARK}\n</card>")
        self.lark.msgs[old]["reply_to"] = self.owner
        self.poll()
        r = self.row(old)
        self.assertEqual((r["route"], r["reason"]), ("self", "agent_reply_in_owner_chat"))
        self.assertNotIn("收到，周五发", self.deliver())
        self.assertEqual(self.store.pending_dispatch(), [])

    def test_already_delivered_old_row_is_not_redelivered(self):
        # 修复前已入账、已投递的那种（dispatch / owner_reply）：再被拉到只 touch，不再投
        old = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content=f"<card>\n旧代回\n{MARK}\n</card>")
        from sheepdog.source.lark import parse_message
        self.store.upsert_message(parse_message(self.lark.raw(old)), "dispatch", "owner_reply", [], "tp_alpha")
        self.assertIn("旧代回", self.deliver())                          # 修复前那次投递
        self.pull(old)
        self.poll()
        self.assertNotIn("旧代回", self.deliver())
        self.assertEqual(self.store.pending_dispatch(), [])


class DocsTest(OwnerChatBase):
    def test_interface_and_example_playbook_say_report_for_owner(self):
        from sheepdog.prompts import interface_section
        from test_roster import REPO
        text = interface_section("tp_alpha", bus=False, remind=[15], expire=60)
        self.assertIn("回主人用 `sheepdog report --topic tp_alpha --key", text)
        common = (REPO / "examples" / "playbook" / "common.md").read_text(encoding="utf-8")
        self.assertIn("回主人不能用 reply", common)
        self.assertNotIn("代回一律用", common)
