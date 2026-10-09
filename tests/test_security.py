"""7.7 安全闸：规则解析、判定组合、tag 警示、hold 改投总线、hold 转交与主人原话核对、编辑重判、security-log。
规则只用 tests/fixtures/security.toml 里的合成标记词（fx-*），全部使用合成数据（*_test_*）。"""

import io
import json
import os
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.engine import (BUS_TOPIC_ID, SECURITY_HOLD_TAG, SECURITY_RELEASE_TAG, Collector, add_watch,
                             bus_quote_verifier, forward_messages)
from sheepdog.playbook import Playbook
from sheepdog.prompts import adopted_bootstrap, batch_prompt, bootstrap_prompt, onboarding_prompt
from sheepdog.roster import parse_roster
from sheepdog.security import (HOLD, NONE, TAG, SecurityError, load_security, owner_inputs, parse_security,
                               quote_verified)
from sheepdog.source.lark import parse_message

from test_roster import FIXTURES, ME, Base, msg, roster_data

SEC_FILE = Path(__file__).resolve().parent / "fixtures" / "security.toml"
OWN = "tenant_test_own"
OTHER = "tenant_test_other"


def sec_data() -> dict:
    with SEC_FILE.open("rb") as f:
        return tomllib.load(f)


def utc(minutes_ago: float) -> str:
    """transcript 的 created_at 形如 2026-10-09T08:13:28Z。"""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_transcript(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    return path


def transcript_lines() -> list[dict]:
    return [
        # 第 0 步：new-conversation 的开场 prompt，不算主人原话
        {"step_index": 0, "type": "USER_INPUT", "created_at": utc(5), "content": "FX 开场原话 只有 sheepdog 写过"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": utc(4), "content": "FX 模型说的话 不算"},
        {"step_index": 2, "type": "USER_INPUT", "created_at": utc(3),
         "content": "<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA><USER_REQUEST>\n可以，\n  按周五   交付给 Alpha\n</USER_REQUEST>"},
        {"step_index": 3, "type": "USER_INPUT", "created_at": utc(2), "content": "没有标签的 FX 全文原话"},
        {"step_index": 4, "type": "USER_INPUT", "created_at": utc(60 * 30), "content": "<USER_REQUEST>FX 很久以前说的</USER_REQUEST>"},
    ]


class RuleParseTest(unittest.TestCase):
    def test_fixture_loads(self):
        sec = load_security(SEC_FILE)
        self.assertTrue(sec.loaded)
        self.assertEqual([r.name for r in sec.rules], ["fx_tag", "fx_hold", "fx_external_hold", "fx_bot_only"])
        self.assertEqual(sec.counts(), {TAG: 2, HOLD: 2})
        self.assertEqual((sec.own_tenant_keys, sec.quote_max_age_hours), ([OWN], 24.0))

    def test_missing_file_means_no_rules(self):
        sec = load_security(Path(tempfile.gettempdir()) / "sheepdog_test_no_security.toml")
        self.assertFalse(sec.loaded)
        self.assertEqual(sec.evaluate(msg(content="fx-holdme")), ([], NONE))

    def test_errors(self):
        cases = [
            ("rule", lambda d: d["rule"].append(dict(d["rule"][0])), "重复"),
            ("action", lambda d: d["rule"][0].update(action="block"), "action"),
            ("regex", lambda d: d["rule"][0].update(patterns=["(unclosed"]), "正则"),
            ("unknown", lambda d: d["rule"][0].update(severity=1), "未知键"),
            ("empty", lambda d: d["rule"].append({"name": "x", "action": "tag"}), "至少要有"),
            ("top", lambda d: d.update(tenants=[]), "未知键"),
            ("age", lambda d: d.update(quote_max_age_hours=0), "quote_max_age_hours"),
        ]
        for label, mutate, err in cases:
            d = sec_data()
            mutate(d)
            with self.assertRaisesRegex(SecurityError, err, msg=label):
                parse_security(d)
        with tempfile.TemporaryDirectory() as t:
            bad = Path(t) / "security.toml"
            bad.write_text("[[rule]\nname=", encoding="utf-8")
            with self.assertRaisesRegex(SecurityError, "TOML"):
                load_security(bad)


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.sec = parse_security(sec_data())

    def ev(self, **kw):
        return self.sec.evaluate(msg(**kw))

    def test_patterns(self):
        self.assertEqual(self.ev(content="请 FX-TAGME 一下", sender_tenant_key=OWN), (["fx_tag"], TAG))
        self.assertEqual(self.ev(content="fx-holdme", sender_tenant_key=OWN), (["fx_hold"], HOLD))
        self.assertEqual(self.ev(content="fx-tagme fx-holdme", sender_tenant_key=OWN), (["fx_tag", "fx_hold"], HOLD))
        self.assertEqual(self.ev(content="普通消息", sender_tenant_key=OWN), ([], NONE))

    def test_external_sender_and_sender_types(self):
        # 同样的话：内部人不命中，外部人命中；没带租户按外部人
        self.assertEqual(self.ev(content="fx-outside", sender_tenant_key=OWN), ([], NONE))
        self.assertEqual(self.ev(content="fx-outside", sender_tenant_key=OTHER), (["fx_external_hold"], HOLD))
        self.assertEqual(self.ev(content="fx-outside", sender_tenant_key=""), (["fx_external_hold"], HOLD))
        # sender_types 限定 user：外部机器人说同样的话不命中这条，但命中只看条件的 fx_bot_only
        self.assertEqual(self.ev(content="fx-outside", sender_tenant_key=OTHER, sender_type="app"), (["fx_bot_only"], TAG))
        # 不写 patterns 的规则只看条件：内部机器人不命中
        self.assertEqual(self.ev(content="随便", sender_tenant_key=OWN, sender_type="bot"), ([], NONE))

    def test_no_own_tenant_keys_treats_everyone_external(self):
        d = sec_data()
        d["own_tenant_keys"] = []
        sec = parse_security(d)
        self.assertEqual(sec.evaluate(msg(content="fx-outside", sender_tenant_key=OWN))[1], HOLD)

    def test_lark_tenant_key_parsed(self):
        raw = {"message_id": "om_test_t", "chat_id": "oc_test_c", "chat_type": "p2p", "content": "x",
               "create_time": "2026-01-01 10:00", "sender": {"id": "ou_test_a", "sender_type": "user", "tenant_key": OTHER}}
        self.assertEqual(parse_message(raw).sender_tenant_key, OTHER)


class GateTest(Base):
    def setUp(self):
        super().setUp()
        self.sec = load_security(SEC_FILE)
        self.app_dir = Path(self.tmp.name) / "app"
        self.env = mock.patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(self.app_dir)})
        self.env.start()
        self.d = self.disp()
        self.d.security = self.sec
        self.d.init()
        self.ack("tp_alpha")
        self.d.dispatch_once()  # onboarding 回执消化掉，alpha 可投递

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def poll(self, msgs, muted=()):
        from test_roster import FakeSource
        return Collector(self.cfg, self.store, FakeSource([msgs], muted), self.roster, self.sec).poll_once()

    def alpha_msgs(self):
        return self.sink.to("conv_test_alpha")

    def test_tag_delivered_with_banner_before_body(self):
        self.poll([msg(message_id="om_test_tag1", chat_type="p2p", chat_id="oc_test_alpha_p2p",
                       sender_tenant_key=OWN, content="帮我 fx-tagme 看看")])
        r = self.row("om_test_tag1")
        self.assertEqual((r["topic_id"], r["security_action"], json.loads(r["security_tags"])), ("tp_alpha", TAG, ["fx_tag"]))
        self.d.dispatch_once()
        text = self.alpha_msgs()[-1]
        self.assertIn("FX-BANNER action=tag tags=fx_tag\nFX 标记规则说明", text)
        self.assertLess(text.index("FX-BANNER"), text.index("> 帮我 fx-tagme 看看"))
        self.assertLess(text.index("action=tag | rules=fx_tag"), text.index("> 帮我"))
        # 每批末尾：security_footer 在 batch_footer 之前
        self.assertLess(text.index("FX-SECURITY-FOOTER"), text.index("FX-FOOTER"))
        self.assertTrue(text.rstrip().splitlines()[-1].startswith("FX-FOOTER"))

    def test_hold_overrides_roster_and_all_messages(self):
        # alpha 私聊是 all_messages；免打扰也照样推，但 hold 改投总线
        self.poll([msg(message_id="om_test_h1", chat_id="oc_test_alpha_p2p",
                       sender_tenant_key=OWN, content="fx-holdme")], muted={"oc_test_alpha_p2p"})
        r = self.row("om_test_h1")
        self.assertEqual((r["topic_id"], r["security_action"]), (BUS_TOPIC_ID, HOLD))
        tags = json.loads(r["tags_json"])
        self.assertIn(SECURITY_HOLD_TAG, tags)
        self.assertIn("owner:alpha", tags)
        self.assertEqual(r["reason"], "muted_chat")  # reason 不变
        n_alpha = len(self.alpha_msgs())
        self.d.dispatch_once()
        self.assertEqual(len(self.alpha_msgs()), n_alpha)
        bus = self.sink.to("conv_test_bus")[-1]
        self.assertIn("om_test_h1", bus)
        self.assertIn("FX-BANNER action=hold tags=fx_hold", bus)

    def test_hold_overrides_watch_and_keeps_waiting(self):
        wid = add_watch(self.store, "tp_alpha", "ou_test_bob")
        self.poll([msg(message_id="om_test_hw", chat_id="oc_test_other", sender_id="ou_test_bob",
                       sender_tenant_key=OWN, content="fx-holdme")])
        r = self.row("om_test_hw")
        self.assertEqual(r["topic_id"], BUS_TOPIC_ID)
        self.assertFalse(any(t.startswith("watch:") for t in json.loads(r["tags_json"])))
        self.assertIsNone(self.store.get_watch(wid)["closed_at"])  # 回复没到等待的会话手里，等待不关

    def test_hold_safety_net_at_dispatch(self):
        self.poll([msg(message_id="om_test_sn", chat_type="p2p", chat_id="oc_test_alpha_p2p",
                       sender_tenant_key=OWN, content="fx-holdme")])
        self.store.conn.execute("UPDATE messages SET topic_id='tp_alpha' WHERE message_id='om_test_sn'")
        self.store.conn.commit()
        n_alpha = len(self.alpha_msgs())
        self.d.dispatch_once()
        self.assertEqual(len(self.alpha_msgs()), n_alpha)
        self.assertIn("om_test_sn", self.sink.to("conv_test_bus")[-1])

    def test_self_and_drop_not_evaluated(self):
        self.poll([msg(message_id="om_test_me1", chat_id="oc_test_alpha_p2p", sender_id=ME, content="fx-holdme"),
                   msg(message_id="om_test_ig1", chat_id="oc_test_ignored", content="fx-holdme")])
        self.assertIsNone(self.row("om_test_me1")["security_action"])
        self.assertIsNone(self.row("om_test_ig1")["security_action"])

    def test_edit_re_evaluates(self):
        from test_roster import FakeSource
        col = Collector(self.cfg, self.store, FakeSource([
            [msg(message_id="om_test_ed1", chat_type="p2p", chat_id="oc_test_alpha_p2p", sender_tenant_key=OWN,
                 content="正常内容")],
            [msg(message_id="om_test_ed1", chat_type="p2p", chat_id="oc_test_alpha_p2p", sender_tenant_key=OWN,
                 content="改成 fx-holdme", updated=True)],
        ]), self.roster, self.sec)
        col.poll_once()
        self.d.dispatch_once()
        self.assertEqual(self.row("om_test_ed1")["dispatch_state"], "delivered")
        col.poll_once()
        r = self.row("om_test_ed1")
        self.assertEqual((r["reason"], r["dispatch_state"], r["topic_id"], r["security_action"]),
                         ("edited", "pending", BUS_TOPIC_ID, HOLD))

    def write_bus_transcript(self):
        cid = self.store.get_topic(BUS_TOPIC_ID)["conversation_id"]
        return write_transcript(self.app_dir / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl",
                                transcript_lines())

    def test_hold_forward_requires_verified_quote(self):
        self.write_bus_transcript()
        verify = bus_quote_verifier(self.store, self.sec.quote_max_age_hours)
        self.poll([msg(message_id="om_test_hf", chat_type="p2p", chat_id="oc_test_stranger",
                       sender_tenant_key=OWN, content="fx-holdme")])
        with self.assertRaisesRegex(ValueError, "安全规则拦截"):
            forward_messages(self.store, "tp_alpha", ["om_test_hf"], note="总线觉得没问题", verify_quote=verify)
        with self.assertRaisesRegex(ValueError, "找不到主人的原文"):
            forward_messages(self.store, "tp_alpha", ["om_test_hf"], quote="FX 我编的一句话", verify_quote=verify)
        with self.assertRaisesRegex(ValueError, "找不到主人的原文"):  # 没有核对器一律拒绝
            forward_messages(self.store, "tp_alpha", ["om_test_hf"], quote="按周五 交付")
        self.assertEqual(self.row("om_test_hf")["topic_id"], BUS_TOPIC_ID)
        forward_messages(self.store, "tp_alpha", ["om_test_hf"], quote="按周五 交付", verify_quote=verify)
        r = self.row("om_test_hf")
        self.assertEqual(r["topic_id"], "tp_alpha")
        self.assertIn(SECURITY_RELEASE_TAG, json.loads(r["tags_json"]))
        self.d.dispatch_once()
        text = self.alpha_msgs()[-1]
        self.assertIn("om_test_hf", text)
        self.assertIn("FX-BANNER action=hold", text)  # 放行后警示仍在
        self.assertIn("✅ 主人原话（已核对：主人在总线里亲口说过）：\n> 按周五 交付", text)


class QuoteVerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = write_transcript(Path(self.tmp.name) / "t.jsonl", transcript_lines())

    def tearDown(self):
        self.tmp.cleanup()

    def ok(self, q, hours=24):
        return quote_verified(q, self.path, hours)

    def test_pass_with_whitespace_normalized(self):
        self.assertTrue(self.ok("可以， 按周五 交付给 Alpha"))
        self.assertTrue(self.ok("按周五\n交付"))
        self.assertTrue(self.ok("没有标签的 FX 全文原话"))  # 没有 <USER_REQUEST> 取全文

    def test_fail_cases(self):
        self.assertFalse(self.ok("FX 模型说的话"))             # 不是 USER_INPUT
        self.assertFalse(self.ok("FX 开场原话"))               # 第 0 步不算
        self.assertFalse(self.ok("ADDITIONAL_METADATA"))       # 只取 USER_REQUEST 里的正文
        self.assertFalse(self.ok("FX 很久以前说的"))           # 超过回看时长
        self.assertTrue(self.ok("FX 很久以前说的", hours=48))
        self.assertFalse(self.ok("   "))
        self.assertFalse(quote_verified("可以", Path(self.tmp.name) / "none.jsonl", 24))

    def test_owner_inputs_only_user_requests(self):
        said = owner_inputs(self.path, 24)
        self.assertEqual(said, ["可以， 按周五 交付给 Alpha", "没有标签的 FX 全文原话"])


class SecurityPromptTest(unittest.TestCase):
    def setUp(self):
        self.pb = Playbook(FIXTURES)
        self.roster = parse_roster(roster_data())
        self.s = self.roster.by_key("alpha")

    def test_security_md_first_in_all_openings(self):
        openings = {
            "bus": bootstrap_prompt(self.pb, "[managed] 总线", "Lark 信号·总线", BUS_TOPIC_ID, self.roster, "", [15, 30], 60),
            "onboarding": onboarding_prompt(self.pb, self.s, "o1", [15, 30], 60),
            "spawned": adopted_bootstrap(self.pb, self.s, "[managed] Alpha", "", [15, 30], 60),
        }
        handover = parse_roster(roster_data()).by_key("alpha")
        handover.predecessor_conversation_id = "conv_test_old"
        openings["successor"] = adopted_bootstrap(self.pb, handover, "[managed] Alpha", "h1", [15, 30], 60,
                                                  "/tmp/sheepdog_test_t.jsonl", "/tmp/sheepdog_test_dir")
        for name, text in openings.items():
            self.assertTrue(text.startswith("FX-SECURITY title="), name)
            self.assertLess(text.index("FX-SECURITY"), text.index("FX-COMMON"), name)

    def test_missing_security_files_leave_mechanism(self):
        with tempfile.TemporaryDirectory() as d:
            pb = Playbook(Path(d))
            text = onboarding_prompt(pb, self.s, "o1", [15, 30], 60)
            self.assertTrue(text.startswith("[sheepdog] 登记通知"))
            self.assertTrue(batch_prompt(pb, "tp_alpha", "b1", [], [], "A").rstrip().splitlines()[-1]
                            .startswith("处理完成后提交回执"))


class SecurityCliTest(GateTest):
    """security-log 输出与 doctor 规则统计（复用 GateTest 的环境，不重复跑它的用例）。"""

    def run_cli(self, *argv, security_line=True):
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\nsecurity_path = "{SEC_FILE}"\n' if security_line
                            else f'self_open_id = "{ME}"\n', encoding="utf-8")
        out = io.StringIO()
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            cli.main(list(argv))
        return out.getvalue()

    def test_security_log(self):
        self.poll([msg(message_id="om_test_l1", chat_type="p2p", chat_id="oc_test_alpha_p2p",
                       sender_name="Mallory", sender_id="ou_test_mallory", sender_tenant_key=OTHER, content="fx-outside"),
                   msg(message_id="om_test_l2", chat_type="p2p", chat_id="oc_test_alpha_p2p",
                       sender_tenant_key=OWN, content="fx-tagme"),
                   msg(message_id="om_test_l3", chat_type="p2p", chat_id="oc_test_alpha_p2p",
                       sender_tenant_key=OWN, content="普通")])
        text = self.run_cli("security-log", "--since", "1h")
        self.assertIn("被标记 / 拦截 2 条", text)
        self.assertIn("Mallory (ou_test_mallory) | 规则: fx_external_hold | 动作: hold | 去向: tp_bus", text)
        self.assertIn("规则: fx_tag | 动作: tag | 去向: tp_alpha", text)
        self.assertNotIn("om_test_l3", text)

    def test_doctor_shows_rules(self):
        text = self.run_cli("doctor")
        self.assertIn("✓ 4 条（tag 2 / hold 2）", text)
        self.assertIn("own_tenant_keys: ✓ 1 个", text)
        self.assertIn("不存在，没有任何规则", self.run_cli("doctor", security_line=False))

    # 父类用例在 GateTest 里已经跑过
    test_tag_delivered_with_banner_before_body = None
    test_hold_overrides_roster_and_all_messages = None
    test_hold_overrides_watch_and_keeps_waiting = None
    test_hold_safety_net_at_dispatch = None
    test_self_and_drop_not_evaluated = None
    test_edit_re_evaluates = None
    test_hold_forward_requires_verified_quote = None


if __name__ == "__main__":
    unittest.main()
