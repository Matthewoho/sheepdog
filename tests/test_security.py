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
from sheepdog.engine import (BUS_TOPIC_ID, OWNER_INSTRUCTION, OWNER_VERIFIED_TAG, SECURITY_HOLD_TAG,
                             SECURITY_RELEASE_TAG, Collector, add_watch, bus_quote_verifier, forward_messages,
                             owner_quote_verifier)
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

    def test_reg_073_forward_owner_im_quote_and_observe_recovered_net_jitter(self):
        from sheepdog import observe
        now_str = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        # 1. 主人在飞书私聊发送指令（带图片与 owner_verified + owner_instruction）
        m_priv = msg(
            message_id="om_test_7105_priv",
            chat_type="p2p",
            chat_id="oc_test_owner_dm",
            sender_id=ME,
            sender_name="Matthew Hong",
            sender_tenant_key=OWN,
            content="帮我看一下哪一个vps的ip段好一点\n![Image](img_v3_test_vps)",
            create_time=now_str,
        )
        self.store.upsert_message(m_priv, "dispatch", OWNER_INSTRUCTION, [OWNER_VERIFIED_TAG, OWNER_INSTRUCTION, "owner_at:private"])
        # 2. 主人在群内 @ 机器人发送指令（仅带 owner_instruction 标签）
        m_grp = msg(
            message_id="om_test_7105_grp",
            chat_type="group",
            chat_id="oc_test_grp",
            sender_id=ME,
            sender_name="Matthew Hong",
            sender_tenant_key=OWN,
            content="@Sheepdog 帮我核一下东京出口带宽",
            create_time=now_str,
        )
        self.store.upsert_message(m_grp, "dispatch", OWNER_INSTRUCTION, [OWNER_INSTRUCTION, "owner_at:bus"])
        # 3. 主人账号下的 AI 助理代发消息不能冒充主人原话
        m_proxy = msg(
            message_id="om_test_7105_proxy",
            chat_type="group",
            chat_id="oc_test_grp",
            sender_id=ME,
            sender_name="Matthew Hong",
            sender_tenant_key=OWN,
            content="[✦ Matthew 的 AI 助理] 自动代回同意放行",
            create_time=now_str,
        )
        self.store.upsert_message(m_proxy, "dispatch", OWNER_INSTRUCTION, [OWNER_VERIFIED_TAG, "agent_sender"])

        verify = owner_quote_verifier(self.store, self.sec.quote_max_age_hours, ME)
        self.assertEqual(verify("帮我看一下哪一个vps的ip段好一点"), "✅ 主人原话（已核对：主人本人账号在飞书上的回复）")
        self.assertEqual(verify("帮我核一下东京出口带宽"), "✅ 主人原话（已核对：主人本人账号在飞书上的回复）")
        self.assertEqual(verify("自动代回同意放行"), "")

        # 通过 CLI cmd_forward 带 --quote 转交飞书主人原话应成功并标注 QUOTE_LABEL_IM
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\nsecurity_path = "{SEC_FILE}"\n', encoding="utf-8")
        out = io.StringIO()
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            rc = cli.main([
                "forward", "--topic", "tp_alpha",
                "--message-ids", "om_test_7105_priv",
                "--quote", "帮我看一下哪一个vps的ip段好一点",
                "--note", "评估 9929 vs BGP",
            ])
        self.assertEqual(rc, 0)
        self.d.dispatch_once()
        delivered_text = self.alpha_msgs()[-1]
        self.assertIn("✅ 主人原话（已核对：主人本人账号在飞书上的回复）：\n> 帮我看一下哪一个vps的ip段好一点", delivered_text)
        self.assertIn("总线备注（不是主人原话）：评估 9929 vs BGP", delivered_text)

        # 4. observe 对已从水位线恢复的瞬时 SourceError 网络抖动归入 infos，未恢复或非网络错误仍归入 warns
        ref_now = datetime(2026, 11, 2, 16, 44, 9, tzinfo=timezone(timedelta(hours=8)))
        log_recovered = Path(self.tmp.name) / "sidecar_recovered.log"
        log_recovered.write_text(
            "2026-11-02 15:51:37 ERROR 本轮失败（下一轮从水位线续跑）: SourceError: lark-cli 调用失败 ['im', '+messages-search']: network/transport: API call failed: dial tcp: lookup open.larksuite.com: no such host\n"
            "2026-11-02 16:43:56 INFO dispatch: {'pending': 0, 'sent': 0}\n",
            encoding="utf-8",
        )
        res_ok = observe.run_observe(
            db_path=Path(self.tmp.name) / "nonexistent.sqlite3",
            state_path=Path(self.tmp.name) / "cursor_rec.json",
            log_path=log_recovered,
            now_dt=ref_now,
            since_minutes=65.0,
            check_processes=False,
            check_log=True,
        )
        self.assertFalse(any("ERROR/Traceback" in w for w in res_ok["warns"]), res_ok["warns"])
        self.assertTrue(any("瞬时网络抖动已自动从水位线恢复" in i for i in res_ok["infos"]), res_ok["infos"])

        log_unrecovered = Path(self.tmp.name) / "sidecar_unrecovered.log"
        log_unrecovered.write_text(
            "2026-11-02 16:40:00 INFO dispatch: {'pending': 0, 'sent': 0}\n"
            "2026-11-02 16:43:56 ERROR 本轮失败（下一轮从水位线续跑）: SourceError: lark-cli 调用失败 ['im', '+messages-search']: network/transport: API call failed\n",
            encoding="utf-8",
        )
        res_warn = observe.run_observe(
            db_path=Path(self.tmp.name) / "nonexistent.sqlite3",
            state_path=Path(self.tmp.name) / "cursor_unrec.json",
            log_path=log_unrecovered,
            now_dt=ref_now,
            since_minutes=65.0,
            check_processes=False,
            check_log=True,
        )
        self.assertTrue(any("ERROR/Traceback" in w for w in res_warn["warns"]), res_warn["warns"])

    def test_reg_074_subagent_return_markdown_summary_and_cli_lamb_grep_noise(self):
        """7.106（REG-074）：subagent_returns_in 自动剥离 [lamb:...] 路由标签、Markdown 标题与导语寒暄并自愈存量占位摘要；_summarize_cli_lamb_log 过滤尾部 grep 行噪音。"""
        from sheepdog.guard import subagent_returns_in
        from sheepdog.engine import _summarize_cli_lamb_log

        # 1. 子代理回传以 [lamb:lamb_88b5b1a8] + Markdown 标题开头时，提取真正的一句话结论
        cid1 = "86cb80c4-43fb-44f3-9048-f4115f2571fd"
        sys_msg_1 = {
            "type": "SYSTEM_MESSAGE",
            "created_at": "2026-11-02T08:44:17Z",
            "content": (
                "<SYSTEM_MESSAGE>\n"
                f"[Message] timestamp=2026-11-02T08:44:17Z sender={cid1} priority=MESSAGE_PRIORITY_HIGH content=[lamb:lamb_88b5b1a8]\n\n"
                "# 生产环境东京业务服务端访问国内网络链路与带宽现状调研报告\n\n"
                "## 一句话总览\n"
                "**目前东京业务服务端访问国内没有单点物理带宽瓶颈，整体处于“大容量低利用率”状态：**\n"
                "1. **链路双轨分流**：核心模型服务访问国内公网模型走骨干公网直出。\n"
                "</SYSTEM_MESSAGE>"
            ),
        }
        rets1 = subagent_returns_in("antigravity", sys_msg_1)
        self.assertEqual(len(rets1), 1)
        self.assertNotIn("[lamb:", rets1[0].summary)
        self.assertFalse(rets1[0].summary.startswith("#"))
        self.assertIn("目前东京业务服务端访问国内没有单点物理带宽瓶颈", rets1[0].summary)

        # 2. 子代理回传以 # 标题 + [lamb:...] + 短编号小节头（1. **核心直接因果链**：）开头时，自动拼接实质内容
        cid2 = "c3cf7d96-6d04-40bd-addb-3361f5c08527"
        sys_msg_2 = {
            "type": "SYSTEM_MESSAGE",
            "created_at": "2026-11-02T07:31:47Z",
            "content": (
                "<SYSTEM_MESSAGE>\n"
                f"[Message] timestamp=2026-11-02T07:31:47Z sender={cid2} priority=MESSAGE_PRIORITY_HIGH content=# 任务 5f9b3a44 排查报告\n\n"
                "[lamb:lamb_36aae5ec]\n\n"
                "## 一、排查结论总览（Executive Summary）\n\n"
                "1. **核心直接因果链**：\n"
                "   - 任务在传入外部供应商时的 4 张参考图均为内部动态路由，gRPC 超时导致 400。\n"
                "</SYSTEM_MESSAGE>"
            ),
        }
        rets2 = subagent_returns_in("antigravity", sys_msg_2)
        self.assertEqual(len(rets2), 1)
        self.assertIn("核心直接因果链：任务在传入外部供应商时的 4 张参考图均为内部动态路由", rets2[0].summary)

        # 3. _handle_lamb_line 与 _scan_lambs 对存量已 done 但 result_summary 仍为 '[lamb:...]' 的子代理自动回填且重扫不产生克隆羊
        self.store.create_lamb(
            "lamb_88b5b1a8", "tp_alpha", kind="subagent", task_title="调研东京带宽",
            conversation_id=cid1, state="done", result_summary="[lamb:lamb_88b5b1a8]",
        )
        inv_line = {
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-11-02T08:35:00Z",
            "tool_calls": [{"name": "invoke_subagent", "args": {"Role": "调研东京带宽", "Prompt": "[lamb:lamb_88b5b1a8] 调研"}}],
        }
        gen_line = {
            "type": "GENERIC",
            "created_at": "2026-11-02T08:35:01Z",
            "content": f'Created the following subagent:\n{{"conversationId": "{cid1}"}}',
        }
        self.d._handle_lamb_line("tp_alpha", "antigravity", 1, inv_line, [])
        self.d._handle_lamb_line("tp_alpha", "antigravity", 2, gen_line, [])
        self.d._handle_lamb_line("tp_alpha", "antigravity", 10, sys_msg_1, [])
        all_alpha_lambs = self.store.list_lambs("tp_alpha", include_finished=True)
        self.assertEqual(len(all_alpha_lambs), 1)
        repaired = self.store.get_lamb("lamb_88b5b1a8")
        self.assertIn("目前东京业务服务端访问国内没有单点物理带宽瓶颈", repaired["result_summary"])

        # 验证游标已在文件末尾（offset == size）时调用 _scan_lambs() 仍可通过 scan_jsonl 自愈存量占位摘要
        tp_alpha_file = write_transcript(Path(self.tmp.name) / "alpha_lamb.jsonl", [inv_line, gen_line, sys_msg_1])
        self.store.update_lamb("lamb_88b5b1a8", result_summary="[lamb:lamb_88b5b1a8]")
        alpha_cid = self.store.get_topic("tp_alpha")["conversation_id"]
        self.store.set_cursor("tp_alpha", "lamb_discovery", alpha_cid, str(tp_alpha_file), tp_alpha_file.stat().st_size)
        with mock.patch.object(self.d, "_record_file", side_effect=lambda t: tp_alpha_file if t["topic_id"] == "tp_alpha" else None):
            self.d._scan_lambs()
        repaired_by_scan = self.store.get_lamb("lamb_88b5b1a8")
        self.assertIn("目前东京业务服务端访问国内没有单点物理带宽瓶颈", repaired_by_scan["result_summary"])

        # 4. _summarize_cli_lamb_log 过滤尾部 rg/grep 归档检索行（含带空格路径），保留前面的实质执行与回读结论
        cli_log = (
            "=== 4. Apply RoleBinding qa-runtime-ben-admin in namespace qa-runtime ===\n"
            "rolebinding.rbac.authorization.k8s.io/qa-runtime-ben-admin created\n"
            "After -> can-i patch statefulsets -n qa-runtime: yes\n"
            "After -> can-i patch statefulsets -n default (isolation check): no\n"
            "=== 6. Lookup P2P chat_id in vault ===\n"
            "/tmp/sheepdog_test_vault/50_People/Ben Zhang.md:76:- 无 active checkbox\n"
            "/tmp/sheepdog_test_vault/90_Archive/communication_sources/2026-05-30/normalized/lark_events.jsonl:318:{\"text\": \"ok\"}\n"
            "/tmp/sheepdog_test_vault/90_Archive/communication_sources/2026-05-30/normalized/lark_events.jsonl:337:{\"text\": \"test\"}\n"
        )
        summary_txt, has_err = _summarize_cli_lamb_log(cli_log)
        self.assertFalse(has_err)
        self.assertIn("After -> can-i patch statefulsets -n qa-runtime: yes", summary_txt)
        self.assertNotIn("Ben Zhang.md:76:", summary_txt)
        self.assertNotIn("lark_events.jsonl:318:", summary_txt)


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
            if name in ("onboarding", "successor"):
                # 开启批次的开场：第一行是批次头（7.47 第 4 条返工：会话记录截断后 Stop hook 还认得出），紧接着就是 security.md
                head, _, text = text.partition("\n\n")
                self.assertTrue(head.startswith("[sheepdog] "), name)
                self.assertNotIn("\n", head, name)
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
    test_reg_073_forward_owner_im_quote_and_observe_recovered_net_jitter = None
    test_reg_074_subagent_return_markdown_summary_and_cli_lamb_grep_noise = None


if __name__ == "__main__":
    unittest.main()
