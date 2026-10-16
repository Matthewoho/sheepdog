"""7.29 话题里找主人要 @ 主人。

Matthew 原话（10-13 19:5x）：「他在话题里补充回复 我看不到 我建议你给sheepdog回复我的时候增加@」。

- ask / report 往找主人聊天的话题里发（--reply-in-thread）时，正文末尾另起一行 <at user_id="主人"></at>；
  在主聊天起话题的第一条不加；[threads] owner_chat_id（测试群）一样。
- 第一行不变：header_regex / attribution_regex 照旧认得出。
- 口径（写死）：防重编号、sends.text、bot_outbox.text 都按不含 @ 那一行的 compose 结果；只有发给飞书的那一份带 @。
- 牧羊犬自己再拉到这条（正文渲染出 @主人、mentions 里有主人）：照旧认作机器人的提问 / 汇报，不当 at_me 派给会话。
  7.26 打开时机器人身份先收到事件的那条路也一样。

7.48 起发给主人的默认是卡片（@ 是卡片里另起的一个 markdown 元素，见 test_owner_card）。这里的用例跑在
[notify] owner_card = false 上：开关关掉回到纯文本时，7.29 的写法一字不变。
"""

import re
from datetime import timedelta

from sheepdog import outbound
from sheepdog.config import IdentityConfig
from sheepdog.source.lark import parse_message

from test_roster import ME
from test_threads import (ESC_CHAT, HEADER_RX, TEST_GROUP, ThreadsBase, _opt, now8)
from test_identity import IdentityBase

AT = f'<at user_id="{ME}"></at>'


def rendered(lark, mid: str) -> dict:
    """飞书把文本里的 <at user_id=…></at> 渲染成「@名字」、mentions 里带上被 @ 的人：按这个样子造拉取结果。"""
    raw = lark.raw(mid)
    if AT in raw["content"]:
        raw["content"] = raw["content"].replace(AT, "@主人")
        raw["mentions"] = [{"id": ME, "key": "@_user_1", "name": "主人"}]
    return raw


def plain_mode(case) -> None:
    """[notify] owner_card = false（纯文本）：常驻进程用的 cfg 和命令行现读的配置文件都关掉。"""
    case.cfg.notify.owner_card = False
    case.cfg_file.write_text(case.cfg_file.read_text(encoding="utf-8") + "[notify]\nowner_card = false\n", encoding="utf-8")


class ThreadAtOwnerTest(ThreadsBase):
    def setUp(self):
        super().setUp()
        plain_mode(self)

    def sends(self):
        return [c for c in self.lark.sent()]

    def test_thread_replies_end_with_at_first_message_does_not(self):
        rc1, _, _ = self.ask("--key", "PR #12")
        rc2, _, _ = self.ask("--key", "PR #12", text="再确认一下回滚方案")
        rc3, _, _ = self.ask("--key", "PR #12", kind="report", title="进度", text="已合入")
        self.assertEqual((rc1, rc2, rc3), (0, 0, 0))
        first, r1, r2 = self.sends()
        self.assertEqual(first[1], "+messages-send")
        self.assertNotIn("<at ", _opt(first, "--text"))                    # 起话题的第一条不加
        for c, head in ((r1, "🐕 需要你定 · 发版时间 [tp_alpha]"), (r2, "🐕 汇报 · 进度 [tp_alpha]")):
            self.assertIn("--reply-in-thread", c)
            text = _opt(c, "--text")
            lines = text.split("\n")
            self.assertEqual(lines[0], head)                               # 第一行不变
            self.assertEqual(lines[-1], AT)                                 # 末尾另起一行 @ 主人
            self.assertEqual(text.count("<at "), 1)
        self.assertTrue(re.search(HEADER_RX, _opt(r1, "--text")))

    def test_idem_key_and_ledger_text_exclude_the_at_line(self):
        self.ask("--key", "K1")
        self.ask("--key", "K1", text="补充")
        r = self.sends()[-1]
        body = outbound.compose(self.cfg, "ask", "tp_alpha", "发版时间", "补充")
        self.assertEqual(_opt(r, "--text"), body + "\n" + AT)
        batch = outbound.current_batch(self.store, "tp_alpha")
        self.assertEqual(_opt(r, "--idempotency-key"), outbound.idem_key("ask", "tp_alpha", "K1", batch, body))
        mid = self.store.conn.execute("SELECT message_id FROM sends ORDER BY sent_at DESC, rowid DESC").fetchone()[0]
        self.assertEqual(self.store.send_by_message(mid)["text"], body)
        self.assertEqual(self.store.get_bot_outbox(mid)["text"], body)
        # 同样的内容重试：已经发过，不再调飞书
        n = len(self.lark.calls)
        rc, out, _ = self.ask("--key", "K1", text="补充")
        self.assertEqual(rc, 0)
        self.assertIn("之前已经发过", out)
        self.assertEqual(len(self.lark.calls), n)

    def test_lost_then_retry_sends_one_message_with_same_text(self):
        self.ask("--key", "K2")
        self.lark.plan["+messages-reply"] = ["lost"]
        rc, _, _ = self.ask("--key", "K2", text="补充")
        self.assertEqual(rc, outbound.EXIT_SEND_FAILED)
        rc, _, _ = self.ask("--key", "K2", text="补充")
        self.assertEqual(rc, 0)
        a, b = [c for c in self.lark.calls if c[1:2] == ["+messages-reply"]][-2:]
        self.assertEqual((_opt(a, "--text"), _opt(a, "--idempotency-key")), (_opt(b, "--text"), _opt(b, "--idempotency-key")))
        root = self.store.thread_by_key("tp_alpha", "K2")["root_message_id"]
        self.assertEqual(len(self.lark.in_thread(root)), 1)

    def test_without_key_no_at(self):
        self.ask()
        self.ask(text="另一件")
        for c in self.sends():
            self.assertNotIn("<at ", _opt(c, "--text"))

    def test_test_group_owner_chat_id_same_rule(self):
        self.cfg.threads.owner_chat_id = TEST_GROUP
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8") + f'[threads]\nowner_chat_id = "{TEST_GROUP}"\n',
                                 encoding="utf-8")
        self.lark.chat_types[TEST_GROUP] = "group"
        self.ask("--key", "T1")
        self.ask("--key", "T1", text="群里补充")
        first, r = self.sends()
        self.assertEqual((_opt(first, "--chat-id"), first[1]), (TEST_GROUP, "+messages-send"))
        self.assertNotIn("<at ", _opt(first, "--text"))
        self.assertEqual(_opt(r, "--text").split("\n")[-1], AT)

    def test_pulled_back_thread_ask_is_still_bot_question_not_at_me(self):
        self.ask("--key", "PR #9")
        self.ask("--key", "PR #9", text="第二个问题")
        self.ask("--key", "PR #9", kind="report", title="进度", text="合入了")
        mids = [k for k, v in self.lark.msgs.items() if v["chat_id"] == ESC_CHAT]
        root, q2, rep = mids
        self.pull(root)
        self.deliver()
        raws = [rendered(self.lark, m) for m in (q2, rep)]
        self.assertIn("@主人", raws[0]["content"])
        from sheepdog.engine import Collector
        Collector(self.cfg, self.store, self.source, self.roster).ingest([parse_message(r) for r in raws])
        r2, rr = self.row(q2), self.row(rep)
        self.assertEqual((r2["route"], r2["reason"]), ("drop", "escalation_asked"))
        self.assertEqual((rr["route"], rr["reason"]), ("drop", "escalation_bot_message"))
        self.assertIsNotNone(self.store.get_escalation(q2))               # 照旧登记「需要你定」
        self.assertEqual(self.store.get_escalation(q2)["topic_id"], "tp_alpha")
        self.assertEqual(self.store.pending_dispatch(), [])                 # 没有当成 at_me 派给会话
        self.assertNotIn("第二个问题", self.deliver())


class ThreadAtOwnerWithIdentityTest(IdentityBase):
    def setUp(self):
        super().setUp()
        plain_mode(self)

    def test_bot_event_first_is_still_bot_question(self):
        self.assertTrue(self.cfg.identity.enabled)
        self.ask("--key", "PR #9")
        self.ask("--key", "PR #9", text="第二个问题")
        root, q2 = [k for k, v in self.lark.msgs.items() if v["chat_id"] == ESC_CHAT]
        self.pull(root)
        self.deliver()
        self.assertEqual(_opt(self.lark.sent()[-1], "--text").split("\n")[-1], AT)
        self.lark.msgs[q2]["content"] = self.lark.msgs[q2]["content"].replace(AT, "@主人")
        self.lark.msgs[q2]["mentions"] = [ME]
        self.bot_hears(self.lark.event(q2, mentions=[{"id": ME, "key": "@_user_1", "name": "主人"}]))
        r = self.row(q2)
        self.assertEqual((r["route"], r["reason"], r["received_by"]), ("drop", "escalation_asked", "bot"))
        self.assertIsNotNone(self.store.get_escalation(q2))
        self.assertEqual(self.store.pending_dispatch(), [])
        self.pull(q2)                                                      # 主人身份随后也搜到：只合并
        self.assertEqual((self.row(q2)["reason"], self.row(q2)["received_by"]), ("escalation_asked", "both"))
        self.assertEqual(self.store.pending_dispatch(), [])


class DocsTest(ThreadsBase):
    def test_readmes_and_reference_mention_at_owner_in_thread(self):
        from test_roster import REPO
        self.assertIn("@owner", (REPO / "README.md").read_text(encoding="utf-8"))
        self.assertIn("@ 主人", (REPO / "README.zh-CN.md").read_text(encoding="utf-8"))
        self.assertIn("オーナーを @", (REPO / "README.ja.md").read_text(encoding="utf-8"))
        self.assertIn('<at user_id=\\"ou_xxx\\"></at>'.replace("\\", ""), (REPO / "docs" / "reference.zh-CN.md").read_text(encoding="utf-8"))
