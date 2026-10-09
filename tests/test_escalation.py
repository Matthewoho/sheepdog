"""7.8 主人在 IM 上直接回复「需要你定」：登记提问、按引用 / 唯一未结投回会话、否则投总线附列表、
其他丢弃、不走安全闸、forward 带已核对标注、超时关闭、escalations 命令。提问开头用 fixtures 里的合成格式。"""

import io
import json
import os
import sqlite3
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.config import ConfigError, EscalationConfig, load_config
from sheepdog.engine import BUS_TOPIC_ID, Collector, forward_messages, open_escalations
from sheepdog.prompts import OWNER_REPLY_LABEL
from sheepdog.security import load_security
from sheepdog.store import Store

from test_roster import ME, Base, FakeSource, msg

FIX = Path(__file__).resolve().parent / "fixtures"
ESC_CHAT = "oc_test_esc"


def esc_config() -> EscalationConfig:
    with (FIX / "escalation.toml").open("rb") as f:
        return EscalationConfig(**tomllib.load(f)["escalation"])


def local(minutes_ago: float = 0) -> str:
    return (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def ask(mid: str, topic: str, question: str, minutes_ago: float = 5, **kw):
    return msg(message_id=mid, chat_id=ESC_CHAT, chat_type="p2p", sender_id="ou_test_bot", sender_name="bot",
               sender_type="app", content=f"FX-ASK[{topic}] {question}", create_time=local(minutes_ago), **kw)


def reply(mid: str, content: str, reply_to: str = "", minutes_ago: float = 1):
    return msg(message_id=mid, chat_id=ESC_CHAT, chat_type="p2p", sender_id=ME, sender_name="主人",
               sender_type="user", content=content, reply_to=reply_to, create_time=local(minutes_ago))


class EscalationTest(Base):
    def setUp(self):
        super().setUp()
        self.cfg.escalation = esc_config()
        # 找主人的聊天即使还留在 ignore 名单里，也由本节接管
        self.cfg.routing.ignore_chat_ids.append(ESC_CHAT)
        self.sec = load_security(FIX / "security.toml")
        self.d = self.disp()
        self.d.security = self.sec
        self.d.init()
        self.ack("tp_alpha")
        self.d.dispatch_once()

    def poll(self, msgs, muted=()):
        return Collector(self.cfg, self.store, FakeSource([msgs], muted), self.roster, self.sec).poll_once()

    def opens(self, topic=None):
        return [e["message_id"] for e in open_escalations(self.store, self.cfg.escalation.open_hours, topic)]

    def test_bot_question_registered_and_dropped(self):
        self.poll([ask("om_test_q1", "tp_alpha", "要不要延期？")])
        r = self.row("om_test_q1")
        self.assertEqual((r["route"], r["reason"]), ("drop", "escalation_asked"))
        e = self.store.get_escalation("om_test_q1")
        self.assertEqual((e["topic_id"], e["chat_id"], e["closed_at"]), ("tp_alpha", ESC_CHAT, None))
        self.assertEqual(self.opens(), ["om_test_q1"])
        # 总线自己找主人也登记
        self.poll([ask("om_test_qb", BUS_TOPIC_ID, "总线的问题")])
        self.assertIsNotNone(self.store.get_escalation("om_test_qb"))

    def test_unknown_topic_only_logged(self):
        with self.assertLogs("sheepdog", "WARNING") as logs:
            self.poll([ask("om_test_qx", "tp_nobody", "?"), ask("om_test_qk", "tp_ops", "known 不收")])
        self.assertIsNone(self.store.get_escalation("om_test_qx"))
        self.assertIsNone(self.store.get_escalation("om_test_qk"))
        self.assertEqual(self.row("om_test_qx")["reason"], "escalation_asked")
        self.assertTrue(any("tp_nobody" in line for line in logs.output))

    def test_fresh_ledger_first_poll(self):
        # 新账本：Collector 先于第一次名册同步运行，提问仍按名册登记
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "s.sqlite3")
            Collector(self.cfg, store, FakeSource([[ask("om_test_f1", "tp_alpha", "?"), reply("om_test_f2", "好")]]),
                      self.roster, self.sec).poll_once()
            self.assertIsNotNone(store.get_escalation("om_test_f1"))
            self.assertEqual(store.get_message("om_test_f2")["topic_id"], "tp_alpha")
            store.close()

    def test_quoted_reply_goes_to_that_topic(self):
        self.poll([ask("om_test_q1", "tp_alpha", "Alpha 的问题"), ask("om_test_q2", BUS_TOPIC_ID, "总线的问题")])
        self.poll([reply("om_test_r1", "延期一周", reply_to="om_test_q1")])
        r = self.row("om_test_r1")
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), ("dispatch", "matthew_reply", "tp_alpha"))
        self.assertIn("owner_verified", json.loads(r["tags_json"]))
        self.assertIn("Alpha 的问题", r["note"])
        self.assertIsNone(self.store.get_escalation("om_test_q1")["answered_at"])  # 投递后才标已答
        self.d.dispatch_once()
        text = self.sink.to("conv_test_alpha")[-1]
        self.assertLess(text.index(OWNER_REPLY_LABEL), text.index("> 延期一周"))
        self.assertIn("回复的问题（topic `tp_alpha`", text)
        e = self.store.get_escalation("om_test_q1")
        self.assertEqual((e["answer_message_id"], e["close_reason"]), ("om_test_r1", "answered"))
        self.assertEqual(self.opens(), ["om_test_q2"])

    def test_unquoted_single_open_goes_to_it(self):
        self.poll([ask("om_test_q1", "tp_alpha", "唯一的问题")])
        self.poll([reply("om_test_r2", "可以")])
        self.assertEqual(self.row("om_test_r2")["topic_id"], "tp_alpha")

    def test_unquoted_multiple_same_topic_goes_to_it(self):
        # 7.8 补丁：同一会话连问两条，主人没引用就回复 → 直接投给它，关闭最近一条；另一条仍未结
        self.poll([ask("om_test_s1", "tp_alpha", "第一问", minutes_ago=10), ask("om_test_s2", "tp_alpha", "第二问", minutes_ago=5)])
        self.poll([reply("om_test_rs", "都按你建议")])
        r = self.row("om_test_rs")
        self.assertEqual(r["topic_id"], "tp_alpha")
        self.assertIn("第二问", r["note"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE reason='escalation_list'").fetchone()[0], 0)
        self.d.dispatch_once()
        self.assertEqual(self.store.get_escalation("om_test_s2")["answer_message_id"], "om_test_rs")
        self.assertEqual(self.opens(), ["om_test_s1"])
        self.assertIn("om_test_rs", self.sink.to("conv_test_alpha")[-1])

    def test_same_batch_question_then_reply(self):
        # 同一批拉到提问和回复：提问先入账，回复才找得到它
        self.poll([reply("om_test_r3", "好", reply_to="om_test_q3", minutes_ago=0),
                   ask("om_test_q3", "tp_alpha", "同批的问题", minutes_ago=1)])
        self.assertEqual(self.row("om_test_r3")["topic_id"], "tp_alpha")

    def test_unquoted_multiple_or_zero_goes_to_bus_with_list(self):
        self.poll([ask("om_test_q1", "tp_alpha", "问题一"), ask("om_test_q2", BUS_TOPIC_ID, "问题二")])
        self.poll([reply("om_test_r4", "第一个同意")])
        self.assertEqual(self.row("om_test_r4")["topic_id"], BUS_TOPIC_ID)
        lists = self.store.conn.execute("SELECT * FROM messages WHERE reason='escalation_list'").fetchall()
        self.assertEqual(len(lists), 1)
        self.assertIn("om_test_q1", lists[0]["content"])
        self.assertIn("问题二", lists[0]["content"])
        self.assertEqual(len(self.opens()), 2)  # 没确定回答哪条，不关闭
        self.d.dispatch_once()
        bus = self.sink.to("conv_test_bus")[-1]
        self.assertIn(OWNER_REPLY_LABEL, bus)
        self.assertLess(bus.index("om_test_r4"), bus.index("问题一"))
        # 零条未结：同样投总线，列表为空
        self.store.answer_escalation("om_test_q1", "x")
        self.store.answer_escalation("om_test_q2", "x")
        self.poll([reply("om_test_r5", "随便说一句")])
        self.assertEqual(self.row("om_test_r5")["topic_id"], BUS_TOPIC_ID)
        last = self.store.conn.execute(
            "SELECT content FROM messages WHERE reason='escalation_list' ORDER BY first_seen DESC, rowid DESC").fetchone()
        self.assertIn("（无）", last["content"])

    def test_other_messages_dropped(self):
        self.poll([msg(message_id="om_test_o1", chat_id=ESC_CHAT, chat_type="p2p", sender_type="app", content="没带头"),
                   msg(message_id="om_test_o2", chat_id=ESC_CHAT, chat_type="p2p", sender_id="ou_test_someone",
                       content="FX-ASK[tp_alpha] 人冒充提问")])
        for mid in ("om_test_o1", "om_test_o2"):
            self.assertEqual((self.row(mid)["route"], self.row(mid)["reason"]), ("drop", "escalation_chat_other"))
        self.assertEqual(self.opens(), [])

    def test_owner_reply_skips_security_gate(self):
        self.poll([ask("om_test_q1", "tp_alpha", "问题")])
        self.poll([reply("om_test_r6", "就这么办 fx-holdme")])
        r = self.row("om_test_r6")
        self.assertEqual((r["topic_id"], r["security_action"]), ("tp_alpha", None))

    def test_owner_reply_edit_also_skips_security_gate(self):
        # 编辑会重新跑安全闸：主人本人的消息仍不受它影响，不会被拉回总线
        self.poll([ask("om_test_q1", "tp_alpha", "问题")])
        col = Collector(self.cfg, self.store, FakeSource([
            [reply("om_test_re", "先这样")],
            [msg(message_id="om_test_re", chat_id=ESC_CHAT, chat_type="p2p", sender_id=ME, sender_type="user",
                 content="改成 fx-holdme", updated=True)],
        ]), self.roster, self.sec)
        col.poll_once()
        self.d.dispatch_once()
        col.poll_once()
        r = self.row("om_test_re")
        self.assertEqual((r["reason"], r["topic_id"], r["security_action"]), ("edited", "tp_alpha", None))

    def test_forward_owner_message_verified_and_closes(self):
        self.poll([ask("om_test_q1", "tp_alpha", "Alpha 问题"), ask("om_test_q2", BUS_TOPIC_ID, "总线问题")])
        self.poll([reply("om_test_r7", "Alpha 那个按 B")])
        self.assertEqual(self.row("om_test_r7")["topic_id"], BUS_TOPIC_ID)
        forward_messages(self.store, "tp_alpha", ["om_test_r7"], self_open_id=ME,
                         escalation_open_hours=self.cfg.escalation.open_hours)  # 不需要 --quote
        r = self.row("om_test_r7")
        self.assertEqual(r["topic_id"], "tp_alpha")
        self.assertEqual(self.store.get_escalation("om_test_q1")["answer_message_id"], "om_test_r7")
        self.assertEqual(self.opens(), ["om_test_q2"])
        self.d.dispatch_once()
        text = self.sink.to("conv_test_alpha")[-1]
        self.assertIn(OWNER_REPLY_LABEL, text)
        self.assertIn("Alpha 问题", text)

    def test_timeout_auto_close(self):
        self.poll([ask("om_test_old", "tp_alpha", "很久以前的问题", minutes_ago=60 * 25)])
        self.assertIsNotNone(self.store.get_escalation("om_test_old"))
        self.assertEqual(self.opens(), [])  # 每轮检查之前就不算未结
        self.d.dispatch_once()
        e = self.store.get_escalation("om_test_old")
        self.assertEqual((e["close_reason"], e["answered_at"]), ("expired", None))
        # 超时后不引用的回复不会被投给它
        self.poll([reply("om_test_r8", "迟到的回复")])
        self.assertEqual(self.row("om_test_r8")["topic_id"], BUS_TOPIC_ID)

    def test_cli_escalations_and_sessions(self):
        self.poll([ask("om_test_q1", "tp_alpha", "Alpha 的问题"), ask("om_test_q2", BUS_TOPIC_ID, "总线的问题")])
        self.poll([reply("om_test_r9", "好", reply_to="om_test_q2")])
        self.d.dispatch_once()
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\n' + (FIX / "escalation.toml").read_text(encoding="utf-8"),
                            encoding="utf-8")
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}

        def run(*argv):
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), redirect_stdout(out):
                self.assertEqual(cli.main(list(argv)), 0)
            return out.getvalue()
        text = run("escalations")
        self.assertIn("[未结] topic=tp_alpha", text)
        self.assertNotIn("om_test_q2", text)
        text = run("escalations", "--all")
        self.assertIn("已答", text)
        self.assertIn("答复 om_test_r9", text)
        sessions = run("sessions")
        self.assertIn("未结「需要你定」: 1", sessions)


class EscalationConfigTest(unittest.TestCase):
    def test_validation(self):
        esc_config().validate()
        for bad in (EscalationConfig(["oc_test_x"], "(unclosed"), EscalationConfig(["oc_test_x"], "no-group"),
                    EscalationConfig(["oc_test_x"], ""), EscalationConfig(["oc_test_x"], "(?P<topic>x)", 0)):
            with self.assertRaises(ConfigError):
                bad.validate()
        EscalationConfig().validate()  # 不配置 = 关闭

    def test_bad_regex_fails_startup(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_file = Path(d) / "config.toml"
            cfg_file.write_text('[escalation]\nchat_ids = ["oc_test_x"]\nheader_regex = \'(unclosed\'\n', encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "header_regex"):
                load_config(cfg_file)
            err = io.StringIO()
            env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": str(Path(d) / "state")}
            with mock.patch.dict(os.environ, env), mock.patch("sys.stderr", err):
                self.assertEqual(cli.main(["poll", "--dry-run"]), 2)
            self.assertIn("header_regex", err.getvalue())

    def test_existing_ledger_gets_table(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.sqlite3"
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            c.execute("INSERT INTO meta VALUES('watermark','x')")
            c.commit()
            c.close()
            st = Store(path)
            self.assertEqual(st.get_meta("watermark"), "x")
            self.assertEqual(st.open_escalations(), [])
            st.close()


if __name__ == "__main__":
    unittest.main()
