"""7.13 主人本人的发言作为背景送给负责的会话：名册归属、follow_hours 内最近处理过的会话、超时不送、
代回前缀 / 找主人聊天 / ignore 聊天不送、不点表情不要回执不改状态、Inbox 显示主人最近发言、关闭时不送。"""

import io
import os
from contextlib import redirect_stdout
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog import session as sm
from sheepdog.ack import Acker
from sheepdog.engine import BUS_TOPIC_ID, Collector
from sheepdog.models import Mention
from sheepdog.prompts import OWNER_CONTEXT_LABEL

from test_ack import FakeIM, ack_config
from test_escalation import ESC_CHAT, esc_config
from test_roster import ME, Base, FakeSource, msg

PREFIX = "FX-PREFIX "


def mine(mid, chat, content="我来回一下", chat_type="group", **kw):
    return msg(message_id=mid, chat_id=chat, chat_type=chat_type, sender_id=ME, sender_name="主人", content=content,
               create_time=datetime.now().astimezone().isoformat(timespec="seconds"), **kw)


class _OwnerBase(Base):
    def setUp(self):
        super().setUp()
        self.cfg.owner_context.enabled = True
        self.cfg.owner_context.skip_prefixes = [PREFIX]
        self.cfg.escalation = esc_config()
        self.cfg.routing.ignore_chat_ids.append("oc_test_ign")
        self.im = FakeIM()
        self.acker = Acker(ack_config(), self.store, self.im)
        self.d = self.disp()
        self.d.acker = self.acker
        self.d.init()
        self.ack("tp_alpha")
        self.d.dispatch_once()

    def poll(self, msgs, muted=()):
        return Collector(self.cfg, self.store, FakeSource([msgs], muted), self.roster, None, self.acker).poll_once()


class OwnerContextTest(_OwnerBase):
    def test_roster_owned_chat_goes_to_owner_as_context(self):
        self.poll([msg(message_id="om_test_a1", chat_type="p2p", chat_id="oc_test_alpha_p2p", sender_id="ou_test_alice",
                       content="能下周给吗")])
        self.d.dispatch_once()
        self.ack("tp_alpha")
        self.d.dispatch_once()
        n_added = len(self.im.added)
        self.poll([mine("om_test_m1", "oc_test_alpha_p2p", "可以，下周三给你", chat_type="p2p", reply_to="om_test_a1")])
        r = self.row("om_test_m1")
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), ("dispatch", "owner_context", "tp_alpha"))
        self.assertIn("回复的是 Alice（ou_test_alice）：能下周给吗", r["note"])
        state_before = self.store.get_topic("tp_alpha")["state"]
        res = self.d.dispatch_once()
        text = self.sink.to("conv_test_alpha")[-1]
        self.assertIn(OWNER_CONTEXT_LABEL, text)
        self.assertIn("> 可以，下周三给你", text)
        self.assertIn("不需要回执", text)
        self.assertNotIn("处理完成后提交回执", text)
        t = self.store.get_topic("tp_alpha")
        self.assertEqual((t["state"], t["pending_batch_id"]), (state_before, None))  # 不改状态、不等回执
        self.assertEqual(self.row("om_test_m1")["dispatch_state"], "context")
        self.assertEqual(res["topics"]["tp_alpha"].get("sent", 0), 0)  # 不算信号
        self.assertEqual(len(self.im.added), n_added)  # 不点表情

    def test_follow_hours_recent_topic_or_nothing(self):
        self.poll([msg(message_id="om_test_g1", chat_id="oc_test_g9", mentions=[Mention(ME)])])
        self.d.dispatch_once()  # 投到总线
        self.poll([mine("om_test_m2", "oc_test_g9")])
        self.assertEqual(self.row("om_test_m2")["topic_id"], BUS_TOPIC_ID)
        # 超过 follow_hours：不送
        old = (datetime.now().astimezone() - timedelta(hours=30)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE dispatches SET sent_at=?", (old,))
        self.store.conn.commit()
        self.poll([mine("om_test_m3", "oc_test_g9")])
        self.assertEqual((self.row("om_test_m3")["route"], self.row("om_test_m3")["topic_id"]), ("self", None))
        # 从没投递过的聊天：不送
        self.poll([mine("om_test_m4", "oc_test_never")])
        self.assertEqual(self.row("om_test_m4")["route"], "self")

    def test_skipped_cases(self):
        self.poll([mine("om_test_s1", "oc_test_alpha_p2p", PREFIX + "收到", chat_type="p2p"),  # 会话代回
                   mine("om_test_s2", ESC_CHAT, "好", chat_type="p2p"),                       # 找主人聊天
                   mine("om_test_s3", "oc_test_ign", "嗯")])                                   # ignore 聊天
        self.assertEqual(self.row("om_test_s1")["route"], "self")
        self.assertNotEqual(self.row("om_test_s2")["reason"], "owner_context")
        self.assertEqual(self.row("om_test_s3")["route"], "self")
        # 名册负责、但在 ignore 名单里的聊天：同样不送
        self.cfg.routing.ignore_chat_ids.append("oc_test_alpha_p2p")
        self.poll([mine("om_test_s5", "oc_test_alpha_p2p", "被忽略的聊天", chat_type="p2p")])
        self.assertEqual(self.row("om_test_s5")["route"], "self")
        self.cfg.routing.ignore_chat_ids.remove("oc_test_alpha_p2p")
        self.cfg.owner_context.enabled = False
        self.poll([mine("om_test_s4", "oc_test_alpha_p2p", "关了就不送", chat_type="p2p")])
        self.assertEqual(self.row("om_test_s4")["route"], "self")

    def test_mixed_batch_and_no_requeue(self):
        self.poll([msg(message_id="om_test_x1", chat_id="oc_test_g8", mentions=[Mention(ME)])])
        self.d.dispatch_once()
        self.ack(BUS_TOPIC_ID)
        self.poll([msg(message_id="om_test_x2", chat_id="oc_test_g8", mentions=[Mention(ME)]),
                   mine("om_test_m5", "oc_test_g8", "我先看看")])
        res = self.d.dispatch_once()
        self.assertEqual(res["topics"][BUS_TOPIC_ID]["sent"], 1)
        self.assertEqual(res["topics"][BUS_TOPIC_ID]["context"], 1)
        self.assertEqual(self.store.get_topic(BUS_TOPIC_ID)["state"], sm.RUNNING)
        # 总线回执超时会重投信号，但背景不跟着重投
        old = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")
        self.store.update_topic(BUS_TOPIC_ID, dispatched_at=old)
        self.d.dispatch_once()
        self.assertEqual(self.row("om_test_m5")["dispatch_state"], "context")
        self.assertNotIn("om_test_m5", self.sink.to("conv_test_bus")[-1])
        self.assertIn("om_test_x2", self.sink.to("conv_test_bus")[-1])

    def test_queued_while_human_attached(self):
        self.sink.human["conv_test_alpha"] = datetime.now().astimezone()
        self.poll([mine("om_test_h1", "oc_test_alpha_p2p", "等等", chat_type="p2p")])
        self.d.dispatch_once()
        self.assertEqual(self.row("om_test_h1")["dispatch_state"], "pending")

    def test_inbox_shows_owner_last(self):
        self.poll([msg(message_id="om_test_i1", chat_id="oc_test_g5", chat_name="闲聊群"),
                   mine("om_test_i2", "oc_test_g5", "大家好")])
        rows = self.store.inbox_summary(ME)
        self.assertTrue(rows[0]["owner_last"])
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\n', encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}), \
                redirect_stdout(out):
            cli.main(["inbox"])
        self.assertIn("「闲聊群」", out.getvalue())
        self.assertIn("主人最近发言", out.getvalue())
        # 总线的 Inbox 摘要同样显示
        self.poll([msg(message_id="om_test_i3", chat_id="oc_test_g6", mentions=[Mention(ME)])])
        self.d.dispatch_once()
        self.assertIn("主人最近发言", self.sink.to("conv_test_bus")[-1])


class ReactSource(FakeSource):
    """FakeSource 加上 reactions_of：返回预设的表情，并记下查了哪些消息。"""

    def __init__(self, batches, reactions):
        super().__init__(batches)
        self.reactions = reactions
        self.queried: list[list[str]] = []

    def reactions_of(self, ids):
        self.queried.append(sorted(ids))
        return {i: self.reactions.get(i, []) for i in ids}


class OwnerActionsTest(_OwnerBase):
    """7.13 补充：主人点的表情、主人编辑 / 撤回自己的发言。"""

    def deliver_alpha(self, mid):
        self.poll([msg(message_id=mid, chat_type="p2p", chat_id="oc_test_alpha_p2p", sender_name="Alice",
                       sender_id="ou_test_alice", content="FX 要交付的东西")])
        self.d.dispatch_once()
        self.ack("tp_alpha")

    def check(self, reactions, force=True):
        if force:
            old = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")
            if self.store.get_meta("owner_reactions_at"):
                self.store.set_meta("owner_reactions_at", old)
        src = ReactSource([[]], reactions)
        Collector(self.cfg, self.store, src, self.roster, None, self.acker).poll_once()
        return src

    def contexts(self):
        return self.store.conn.execute(
            "SELECT * FROM messages WHERE reason='owner_context' AND chat_type='sheepdog' ORDER BY first_seen, rowid").fetchall()

    def test_owner_reaction_reported_once_excluding_acks_and_others(self):
        self.deliver_alpha("om_test_r1")
        ack_rid = self.store.get_ack("om_test_r1")["reaction_id"]  # sheepdog 以主人身份点的确认表情
        self.check({})  # 第一次检查：只记基线
        reactions = {"om_test_r1": [
            {"emoji_type": "FX_EMOJI", "operator_id": ME, "reaction_id": ack_rid},   # 自己的 ack：不报
            {"emoji_type": "FX_THUMBS", "operator_id": "ou_test_bob", "reaction_id": "rx_b"},  # 别人的：不报
            {"emoji_type": "FX_THUMBS", "operator_id": ME, "reaction_id": "rx_me"},  # 主人新加：报
        ]}
        src = self.check(reactions)
        self.assertEqual(src.queried, [["om_test_r1"]])
        ctx = self.contexts()
        self.assertEqual(len(ctx), 1)
        self.assertEqual(ctx[0]["topic_id"], "tp_alpha")
        self.assertIn("主人对这条消息点了 FX_THUMBS", ctx[0]["content"])
        self.assertIn("Alice（ou_test_alice）：FX 要交付的东西", ctx[0]["content"])
        self.check(reactions)  # 再查：不重复报
        self.assertEqual(len(self.contexts()), 1)
        # 送达时是背景：不要回执、不改状态
        self.d.dispatch_once()
        self.assertIn("主人对这条消息点了 FX_THUMBS", self.sink.to("conv_test_alpha")[-1])
        self.assertIsNone(self.store.get_topic("tp_alpha")["pending_batch_id"])

    def test_baseline_not_reported(self):
        self.deliver_alpha("om_test_r2")
        self.check({"om_test_r2": [{"emoji_type": "FX_OLD", "operator_id": ME, "reaction_id": "rx_old"}]})
        self.assertEqual(self.contexts(), [])
        self.check({"om_test_r2": [{"emoji_type": "FX_OLD", "operator_id": ME, "reaction_id": "rx_old"}]})
        self.assertEqual(self.contexts(), [])

    def test_lookback_and_interval_and_off(self):
        self.deliver_alpha("om_test_r3")
        old = (datetime.now().astimezone() - timedelta(hours=30)).isoformat(timespec="seconds")
        self.store.conn.execute("UPDATE dispatches SET sent_at=?", (old,))
        self.store.conn.commit()
        src = self.check({})
        self.assertEqual(src.queried, [])  # 超出回看窗口的不查
        self.deliver_alpha("om_test_r4")
        src = self.check({}, force=False)
        self.assertEqual(src.queried, [])  # 没到检查间隔
        self.cfg.owner_context.reaction_check_minutes = 0
        src = self.check({})
        self.assertEqual(src.queried, [])  # 0 = 不查

    def test_owner_edit_and_recall_sent_as_context(self):
        col = Collector(self.cfg, self.store, FakeSource([
            [mine("om_test_e1", "oc_test_alpha_p2p", "下周三给", chat_type="p2p"),
             mine("om_test_e2", "oc_test_alpha_p2p", PREFIX + "代回的话", chat_type="p2p"),
             mine("om_test_e3", "oc_test_alpha_p2p", PREFIX + "代回的另一句", chat_type="p2p")],
            [mine("om_test_e1", "oc_test_alpha_p2p", "下周四给", chat_type="p2p", updated=True),
             mine("om_test_e2", "oc_test_alpha_p2p", PREFIX + "代回改了", chat_type="p2p", updated=True),
             mine("om_test_e3", "oc_test_alpha_p2p", "代回改成没前缀", chat_type="p2p", updated=True)],
            [mine("om_test_e1", "oc_test_alpha_p2p", "", chat_type="p2p", deleted=True)],
        ]), self.roster, None, self.acker)
        col.poll_once()
        col.poll_once()
        ctx = self.contexts()
        self.assertEqual(len(ctx), 1)  # 代回前缀的编辑不送
        self.assertIn("主人编辑了他在私聊的发言", ctx[0]["content"])
        self.assertIn("旧：下周三给\n新：下周四给", ctx[0]["content"])
        col.poll_once()
        ctx = self.contexts()
        self.assertEqual(len(ctx), 2)
        self.assertIn("主人撤回了他在私聊的发言", ctx[1]["content"])
        self.assertIn("> 下周四给", ctx[1]["content"])
        self.assertEqual({c["topic_id"] for c in ctx}, {"tp_alpha"})


class LarkReactionsOfTest(unittest.TestCase):
    def test_parse_batch_query(self):
        import json as _json
        import stat
        import tempfile
        from sheepdog.source.lark import LarkCliSource
        with tempfile.TemporaryDirectory() as t:
            out = {"ok": True, "data": {"success_msg_reaction_details": [
                {"message_id": "om_test_1", "message_reaction_items": [
                    {"emoji_type": "FX_A", "operator": {"operator_id": ME, "operator_type": "user"}, "reaction_id": "rx_1"}]}]}}
            log = Path(t) / "args.json"
            script = Path(t) / "lark-cli"
            script.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                              f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                              f"print({_json.dumps(out)!r})\n", encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            got = LarkCliSource(binary=str(script)).reactions_of(["om_test_1"])
            self.assertEqual(got, {"om_test_1": [{"emoji_type": "FX_A", "operator_id": ME, "reaction_id": "rx_1"}]})
            a = _json.loads(log.read_text())
            self.assertEqual(a[:3], ["im", "reactions", "batch_query"])
            self.assertEqual(a[-2:], ["--as", "user"])
            self.assertEqual(_json.loads(a[a.index("--data") + 1])["queries"], [{"message_id": "om_test_1"}])
