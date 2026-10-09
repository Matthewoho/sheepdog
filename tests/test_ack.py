"""7.9 确认表情：送达才点、只点配置的原因、每条一次；主人回复后按私聊 / 群规则撤下；at_all 不撤；
失败不阻塞不重试；dry-run 不调接口；没有 [ack] 段完全不动；acks 命令。表情名来自 fixtures。"""

import io
import json
import os
import stat
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.ack import Acker
from sheepdog.config import AckConfig, load_config
from sheepdog.engine import BUS_TOPIC_ID, Collector
from sheepdog.models import Mention
from sheepdog.roster import Roster
from sheepdog.source.lark import LarkCliSource
from sheepdog.source import SourceError

from test_roster import ME, Base, FakeSource, msg

FIX = Path(__file__).resolve().parent / "fixtures"


def ack_config() -> AckConfig:
    with (FIX / "ack.toml").open("rb") as f:
        return AckConfig(enabled=True, **tomllib.load(f)["ack"])


def local(minutes: float = 0) -> str:
    """相对现在的本地时间；正数是将来。"""
    return (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


class FakeIM:
    def __init__(self):
        self.added: list[tuple[str, str]] = []
        self.identities: dict[str, str] = {}
        self.removed: list[tuple[str, str]] = []
        self.fail_add: set[str] = set()
        self.fail_remove: set[str] = set()

    def add_reaction(self, message_id, emoji_type, identity="user"):
        self.added.append((message_id, emoji_type))
        self.identities[message_id] = identity
        if message_id in self.fail_add:
            raise SourceError("fx 点失败")
        return f"rx_{message_id}"

    def remove_reaction(self, message_id, reaction_id):
        self.removed.append((message_id, reaction_id))
        if message_id in self.fail_remove:
            raise SourceError("fx 撤失败")


class AckTest(Base):
    def setUp(self):
        super().setUp()
        self.roster = Roster()  # 全部进总线，专注表情逻辑
        self.im = FakeIM()
        self.acker = Acker(ack_config(), self.store, self.im)
        self.d = self.disp(Roster())
        self.d.acker = self.acker

    def poll(self, msgs, muted=()):
        return Collector(self.cfg, self.store, FakeSource([msgs], muted), self.roster, None, self.acker).poll_once()

    def deliver(self):
        """投一批，再把总线回执掉，下一批可以继续投。"""
        res = self.d.dispatch_once()
        t = self.store.get_topic(BUS_TOPIC_ID)
        if t["pending_batch_id"]:
            self.ack(BUS_TOPIC_ID)
        return res

    def acked(self):
        return [m for m, _ in self.im.added]

    def test_only_configured_reasons_after_delivery(self):
        self.poll([msg(message_id="om_test_p1", chat_type="p2p", chat_id="oc_test_p1", create_time=local(-10)),
                   msg(message_id="om_test_g1", mentions=[Mention(ME)]),
                   msg(message_id="om_test_g2", mentions=[Mention("all", is_all=True)]),
                   msg(message_id="om_test_k1", content="出故障了")])
        self.assertEqual(self.im.added, [])  # 入账时不点
        self.deliver()
        self.assertEqual(sorted(self.acked()), ["om_test_g1", "om_test_g2", "om_test_p1"])
        self.assertEqual({e for _, e in self.im.added}, {"FX_EMOJI"})
        a = self.store.get_ack("om_test_p1")
        self.assertEqual((a["reaction_id"], a["reason"], a["chat_type"]), ("rx_om_test_p1", "p2p", "p2p"))

    def test_hold_not_acked_tag_acked(self):
        from sheepdog.security import load_security
        sec = load_security(FIX / "security.toml")
        Collector(self.cfg, self.store, FakeSource([[
            msg(message_id="om_test_hd", chat_type="p2p", chat_id="oc_test_p1", sender_tenant_key="tenant_test_own",
                content="fx-holdme"),
            msg(message_id="om_test_tg", chat_type="p2p", chat_id="oc_test_p2", sender_tenant_key="tenant_test_own",
                content="fx-tagme"),
        ]]), self.roster, sec, self.acker).poll_once()
        self.assertEqual(self.row("om_test_hd")["security_action"], "hold")
        res = self.deliver()
        self.assertEqual(res["sent"], 2)  # 两条都投到了总线
        self.assertEqual(self.acked(), ["om_test_tg"])
        self.assertIsNone(self.store.get_ack("om_test_hd"))

    def test_queued_not_acked_until_delivered(self):
        self.d.ensure_bus_topic()
        self.store.update_topic(BUS_TOPIC_ID, conversation_id="conv_test_bus")
        self.sink.human["conv_test_bus"] = datetime.now().astimezone() - timedelta(minutes=1)
        self.poll([msg(message_id="om_test_q1", chat_type="p2p", chat_id="oc_test_p1")])
        self.d.dispatch_once()
        self.assertEqual(self.im.added, [])
        self.sink.human["conv_test_bus"] = datetime.now().astimezone() - timedelta(minutes=30)
        self.d.dispatch_once()
        self.assertEqual(self.acked(), ["om_test_q1"])

    def test_once_per_message(self):
        col = Collector(self.cfg, self.store, FakeSource([
            [msg(message_id="om_test_o1", chat_type="p2p", chat_id="oc_test_p1", content="v1")],
            [msg(message_id="om_test_o1", chat_type="p2p", chat_id="oc_test_p1", content="v2", updated=True)],
        ]), self.roster, None, self.acker)
        col.poll_once()
        self.deliver()
        col.poll_once()  # 编辑后重新投递
        self.deliver()
        self.assertEqual(self.acked(), ["om_test_o1"])
        # 总线回执超时会原样重投（原因仍是 p2p）：也不再点第二次
        self.poll([msg(message_id="om_test_o2", chat_type="p2p", chat_id="oc_test_p1")])
        self.d.dispatch_once()
        old = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")
        self.store.update_topic(BUS_TOPIC_ID, dispatched_at=old)
        self.d.dispatch_once()
        self.assertEqual(self.row("om_test_o2")["dispatch_state"], "delivered")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM dispatches WHERE message_ids LIKE '%om_test_o2%'")
                         .fetchone()[0], 2)
        self.assertEqual(self.acked(), ["om_test_o1", "om_test_o2"])

    def test_p2p_own_message_removes_earlier_in_same_chat(self):
        self.poll([msg(message_id="om_test_a1", chat_type="p2p", chat_id="oc_test_p1"),
                   msg(message_id="om_test_a2", chat_type="p2p", chat_id="oc_test_p1"),
                   msg(message_id="om_test_b1", chat_type="p2p", chat_id="oc_test_p2")])
        self.deliver()
        # 自己在同一私聊里发的、但时间早于点表情的消息不撤（create_time 比点表情早一分钟以上）
        self.poll([msg(message_id="om_test_me0", chat_type="p2p", chat_id="oc_test_p1", sender_id=ME,
                       create_time=local(-5))])
        self.assertEqual(self.im.removed, [])
        self.poll([msg(message_id="om_test_me1", chat_type="p2p", chat_id="oc_test_p1", sender_id=ME,
                       create_time=local(0))])
        self.assertEqual(sorted(m for m, _ in self.im.removed), ["om_test_a1", "om_test_a2"])
        self.assertIsNotNone(self.store.get_ack("om_test_a1")["removed_at"])
        self.assertIsNone(self.store.get_ack("om_test_b1")["removed_at"])  # 别的聊天不撤

    def test_group_reply_or_mention_removes(self):
        self.poll([msg(message_id="om_test_g1", mentions=[Mention(ME)], sender_id="ou_test_alice"),
                   msg(message_id="om_test_g2", mentions=[Mention(ME)], sender_id="ou_test_bob"),
                   msg(message_id="om_test_g3", mentions=[Mention(ME)], sender_id="ou_test_carol")])
        self.deliver()
        self.poll([msg(message_id="om_test_s0", sender_id=ME, content="群里随便说一句")])
        self.assertEqual(self.im.removed, [])
        self.poll([msg(message_id="om_test_s1", sender_id=ME, reply_to="om_test_g1"),
                   msg(message_id="om_test_s2", sender_id=ME, mentions=[Mention("ou_test_bob")])])
        self.assertEqual(sorted(m for m, _ in self.im.removed), ["om_test_g1", "om_test_g2"])
        self.assertIsNone(self.store.get_ack("om_test_g3")["removed_at"])

    def test_at_all_not_removed(self):
        self.poll([msg(message_id="om_test_all", sender_id="ou_test_alice", mentions=[Mention("all", is_all=True)])])
        self.deliver()
        self.poll([msg(message_id="om_test_s3", sender_id=ME, reply_to="om_test_all",
                       mentions=[Mention("ou_test_alice")])])
        self.assertEqual(self.im.removed, [])
        self.assertEqual(self.acked(), ["om_test_all"])

    def test_failures_do_not_block_and_are_not_retried(self):
        self.im.fail_add.add("om_test_f1")
        self.poll([msg(message_id="om_test_f1", chat_type="p2p", chat_id="oc_test_p1"),
                   msg(message_id="om_test_f2", chat_type="p2p", chat_id="oc_test_p1")])
        res = self.deliver()
        self.assertEqual(res["sent"], 2)  # 投递照常
        self.assertEqual(self.row("om_test_f1")["dispatch_state"], "delivered")
        a = self.store.get_ack("om_test_f1")
        self.assertEqual((a["reaction_id"], a["error"]), (None, "fx 点失败"))
        # 撤失败：记 error，之后再回复也不重试
        self.im.fail_remove.add("om_test_f2")
        self.poll([msg(message_id="om_test_me2", chat_type="p2p", chat_id="oc_test_p1", sender_id=ME,
                       create_time=local(1))])
        self.assertIn("撤表情失败", self.store.get_ack("om_test_f2")["error"])
        self.poll([msg(message_id="om_test_me3", chat_type="p2p", chat_id="oc_test_p1", sender_id=ME,
                       create_time=local(2))])
        self.assertEqual([m for m, _ in self.im.removed], ["om_test_f2"])
        self.assertEqual(self.row("om_test_me3")["route"], "self")  # 入账照常

    def test_dry_run_does_not_call(self):
        self.d.acker = self.acker = Acker(ack_config(), self.store, self.im, dry_run=True)
        self.poll([msg(message_id="om_test_d1", chat_type="p2p", chat_id="oc_test_p1")])
        out = io.StringIO()
        with redirect_stdout(out):
            self.deliver()
        self.assertEqual((self.im.added, self.store.get_ack("om_test_d1")), ([], None))
        self.assertIn("[dryrun] 点表情 FX_EMOJI（user）-> om_test_d1", out.getvalue())

    def test_disabled_does_nothing(self):
        self.d.acker = self.acker = Acker(AckConfig(), self.store, self.im)
        self.poll([msg(message_id="om_test_n1", chat_type="p2p", chat_id="oc_test_p1")])
        self.deliver()
        self.poll([msg(message_id="om_test_n2", chat_type="p2p", chat_id="oc_test_p1", sender_id=ME)])
        self.assertEqual((self.im.added, self.im.removed), ([], []))
        self.assertEqual(self.store.list_acks(), [])

    def test_config_section_switch_and_cli(self):
        with tempfile.TemporaryDirectory() as d:
            off = Path(d) / "off.toml"
            off.write_text('self_open_id = "ou_test_me"\n', encoding="utf-8")
            self.assertFalse(load_config(off).ack.enabled)
            self.assertIsNone(cli._acker(load_config(off), None, None, None))
            on = Path(d) / "on.toml"
            on.write_text('self_open_id = "ou_test_me"\n' + (FIX / "ack.toml").read_text(encoding="utf-8"), encoding="utf-8")
            cfg = load_config(on)
            self.assertTrue(cfg.ack.enabled)
            self.assertEqual(cfg.ack.reasons, ["p2p", "at_me", "at_all"])
        # acks 命令
        self.im.fail_add.add("om_test_c2")
        self.poll([msg(message_id="om_test_c1", chat_type="p2p", chat_id="oc_test_p1", sender_name="Alice"),
                   msg(message_id="om_test_c2", chat_type="p2p", chat_id="oc_test_p2"),
                   msg(message_id="om_test_c3", mentions=[Mention(ME)], chat_name="测试群")])
        self.deliver()
        self.poll([msg(message_id="om_test_me4", chat_type="p2p", chat_id="oc_test_p1", sender_id=ME, create_time=local(1))])
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\n', encoding="utf-8")
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}

        def run(*argv):
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), redirect_stdout(out):
                self.assertEqual(cli.main(list(argv)), 0)
            return out.getvalue()
        text = run("acks")
        self.assertIn("[已撤 @", text)
        self.assertIn("[点失败：fx 点失败]", text)
        self.assertIn("群「测试群」", text)
        open_text = run("acks", "--open")
        self.assertIn("om_test_c3", open_text)
        self.assertNotIn("om_test_c1", open_text)
        self.assertNotIn("om_test_c2", open_text)


class BotAckTest(Base):
    """[ack] bot_reasons：主人在「找主人」聊天里的回复送达会话后以 bot 身份点表情，永不撤。"""

    def setUp(self):
        super().setUp()
        from test_escalation import ESC_CHAT, ask, esc_config, reply
        self.ask, self.reply = ask, reply
        self.cfg.escalation = esc_config()
        self.im = FakeIM()
        cfg = ack_config()
        cfg.bot_reasons = ["owner_reply", "p2p"]  # p2p 同时在 reasons 里：以 bot_reasons 为准
        self.acker = Acker(cfg, self.store, self.im)
        self.d = self.disp()
        self.d.acker = self.acker
        self.d.init()
        self.ack("tp_alpha")
        self.d.dispatch_once()

    def poll(self, msgs, muted=()):
        return Collector(self.cfg, self.store, FakeSource([msgs], muted), self.roster, None, self.acker).poll_once()

    def test_owner_reply_acked_as_bot_once_never_removed(self):
        self.poll([self.ask("om_test_q1", "tp_alpha", "问题")])
        self.poll([self.reply("om_test_r1", "好的")])
        self.assertEqual(self.im.added, [])  # 入账时不点
        self.d.dispatch_once()
        self.assertEqual(self.im.identities, {"om_test_r1": "bot"})
        a = self.store.get_ack("om_test_r1")
        self.assertEqual((a["identity"], a["reason"]), ("bot", "owner_reply"))
        # 主人在同一私聊再发消息：bot 点的表情不撤
        self.poll([self.reply("om_test_r2", "补充一句", minutes_ago=-1)])
        self.assertEqual(self.im.removed, [])
        self.assertIsNone(self.store.get_ack("om_test_r1")["removed_at"])
        # 同一条再投一次也不重复点
        self.store.conn.execute("UPDATE messages SET dispatch_state='pending' WHERE message_id='om_test_r1'")
        self.store.conn.commit()
        self.ack("tp_alpha")
        self.d.dispatch_once()
        self.assertEqual([m for m, _ in self.im.added].count("om_test_r1"), 1)

    def test_forwarded_owner_reply_acked_on_delivery(self):
        from sheepdog.engine import forward_messages
        self.poll([self.ask("om_test_q1", "tp_alpha", "一"), self.ask("om_test_q2", BUS_TOPIC_ID, "二")])
        self.poll([self.reply("om_test_r3", "第一个按 A")])  # 分属两个会话 → 投总线
        self.d.dispatch_once()
        self.assertEqual(self.im.identities.get("om_test_r3"), "bot")  # 送达总线就点了
        self.im.added.clear()
        forward_messages(self.store, "tp_alpha", ["om_test_r3"], self_open_id=ME)
        self.ack(BUS_TOPIC_ID)
        self.d.dispatch_once()
        self.assertIn("om_test_r3", self.sink.to("conv_test_alpha")[-1])
        self.assertEqual(self.im.added, [])  # 已点过，转交后不再点

    def test_forwarded_owner_reply_first_delivery_is_the_forward(self):
        from sheepdog.engine import forward_messages
        self.poll([self.ask("om_test_q1", "tp_alpha", "一"), self.ask("om_test_q2", BUS_TOPIC_ID, "二")])
        self.sink.human["conv_test_bus"] = datetime.now().astimezone()  # 总线被主人接管，回复卡在队列
        self.poll([self.reply("om_test_r4", "第一个按 A")])
        self.d.dispatch_once()
        self.assertEqual(self.im.added, [])
        forward_messages(self.store, "tp_alpha", ["om_test_r4"], self_open_id=ME)
        self.d.dispatch_once()
        self.assertEqual(self.im.identities, {"om_test_r4": "bot"})

    def test_owner_verified_rows_count_as_owner_reply(self):
        # 已上线账本里改名前的旧 reason：靠 owner_verified 标记认作主人回复，照样以 bot 点
        self.poll([self.ask("om_test_q1", "tp_alpha", "问题")])
        self.poll([self.reply("om_test_lg", "好")])
        self.store.conn.execute("UPDATE messages SET reason='legacy_owner_reply_name' WHERE message_id='om_test_lg'")
        self.store.conn.commit()
        self.d.dispatch_once()
        self.assertEqual(self.im.identities, {"om_test_lg": "bot"})

    def test_bot_reasons_win_over_reasons(self):
        self.poll([msg(message_id="om_test_p1", chat_type="p2p", chat_id="oc_test_alpha_p2p")])
        self.d.dispatch_once()
        self.assertEqual(self.im.identities["om_test_p1"], "bot")
        self.poll([msg(message_id="om_test_me", chat_type="p2p", chat_id="oc_test_alpha_p2p", sender_id=ME,
                       create_time=local(1))])
        self.assertEqual(self.im.removed, [])  # bot 的不撤

    def test_default_empty_bot_reasons(self):
        self.acker.cfg.bot_reasons = []
        self.poll([self.ask("om_test_q1", "tp_alpha", "问题")])
        self.poll([self.reply("om_test_r5", "好")])
        self.d.dispatch_once()
        self.assertEqual(self.im.added, [])
        self.assertEqual(load_config_default_bot_reasons(), [])


def load_config_default_bot_reasons():
    with tempfile.TemporaryDirectory() as d:
        on = Path(d) / "on.toml"
        on.write_text((FIX / "ack.toml").read_text(encoding="utf-8"), encoding="utf-8")
        return load_config(on).ack.bot_reasons


class LarkReactionTest(unittest.TestCase):
    """真实 LarkCliSource 的加 / 撤表情：用假 lark-cli 校验参数与输出解析，不碰真实飞书。"""

    def fake_cli(self, d: Path, stdout: str, rc: int = 0) -> LarkCliSource:
        log = d / "args.json"
        script = d / "lark-cli"
        script.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                          f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                          f"print({stdout!r})\nsys.exit({rc})\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.log = log
        return LarkCliSource(binary=str(script))

    def args(self):
        return json.loads(self.log.read_text())

    def test_create_parses_both_envelopes(self):
        with tempfile.TemporaryDirectory() as t:
            for out in ('{"ok": true, "data": {"reaction_id": "rx_1"}}', '{"code": 0, "data": {"reaction_id": "rx_1"}}',
                        '{"reaction_id": "rx_1"}'):
                src = self.fake_cli(Path(t), out)
                self.assertEqual(src.add_reaction("om_test_1", "FX_EMOJI"), "rx_1")
            a = self.args()
            self.assertEqual(a[:3], ["im", "reactions", "create"])
            self.assertEqual(json.loads(a[a.index("--params") + 1]), {"message_id": "om_test_1"})
            self.assertEqual(json.loads(a[a.index("--data") + 1]), {"reaction_type": {"emoji_type": "FX_EMOJI"}})
            self.assertEqual(a[-2:], ["--as", "user"])
            src = self.fake_cli(Path(t), '{"ok": true, "data": {"reaction_id": "rx_2"}}')
            self.assertEqual(src.add_reaction("om_test_2", "FX_EMOJI", "bot"), "rx_2")
            self.assertEqual(self.args()[-2:], ["--as", "bot"])
            with self.assertRaises(SourceError):
                src.add_reaction("om_test_2", "FX_EMOJI", "admin")

    def test_delete_and_errors(self):
        with tempfile.TemporaryDirectory() as t:
            src = self.fake_cli(Path(t), '{"ok": true, "data": {}}')
            src.remove_reaction("om_test_1", "rx_1")
            a = self.args()
            self.assertEqual(a[:3], ["im", "reactions", "delete"])
            self.assertEqual(json.loads(a[a.index("--params") + 1]), {"message_id": "om_test_1", "reaction_id": "rx_1"})
            for out, rc in (('{"ok": false, "error": {"message": "x"}}', 0), ('{"code": 230002, "msg": "x"}', 0),
                            ('not json', 0), ('{"ok": true}', 1)):
                src = self.fake_cli(Path(t), out, rc)
                with self.assertRaises(SourceError):
                    src.remove_reaction("om_test_1", "rx_1")
            src = self.fake_cli(Path(t), '{"ok": true, "data": {}}')
            with self.assertRaisesRegex(SourceError, "reaction_id"):
                src.add_reaction("om_test_1", "FX_EMOJI")


if __name__ == "__main__":
    unittest.main()
