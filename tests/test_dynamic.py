"""7.10 总线临时新开会话：new-session 建会话与排队、key / 聊天冲突、配额、名册同步不碰 dynamic、
done 即收掉并释放聊天、close-session 只关 dynamic、{{roster}} 含 dynamic、--chat 归属。全部合成数据。"""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog import session as sm
from sheepdog.engine import BUS_TOPIC_ID, Collector, close_session
from sheepdog.models import Mention
from sheepdog.security import load_security

from test_roster import ME, Base, FakeSource, msg, parse_roster, roster_data

FIX = Path(__file__).resolve().parent / "fixtures"


class DynamicTest(Base):
    def setUp(self):
        super().setUp()
        self.d = self.disp()
        self.d.init()
        self.ack("tp_alpha")
        self.d.dispatch_once()

    def poll(self, msgs, muted=(), security=None):
        return Collector(self.cfg, self.store, FakeSource([msgs], muted), self.roster, security).poll_once()

    def new(self, key="deploy", chats=(), ids=(), note="", **kw):
        return self.d.new_session(key, kw.get("title", "发布排查"), kw.get("duty", "FX 职责：排查发布失败"),
                                  list(chats), list(ids), note)

    def test_new_session_creates_and_queues(self):
        self.poll([msg(message_id="om_test_n1", chat_type="p2p", chat_id="oc_test_stranger", content="发布挂了")])
        self.d.dispatch_once()  # 先到总线
        self.ack(BUS_TOPIC_ID)
        r = self.new(chats=[("oc_test_dep", True), ("oc_test_dep2", False)], ids=["om_test_n1"], note="FX 为什么开")
        cid = r["conversation_id"]
        title, boot = self.sink.created[-1]
        self.assertEqual(title, "[managed] 发布排查")
        # 开场：security → onboarding（授权用默认）→ common → 接口说明
        self.assertTrue(boot.startswith("FX-SECURITY title=发布排查 topic=tp_deploy"))
        self.assertIn("FX-ONBOARDING title=发布排查 duty=FX 职责：排查发布失败", boot)
        self.assertIn("auth=FX-AUTH-DEFAULT", boot)
        self.assertLess(boot.index("FX-ONBOARDING"), boot.index("FX-COMMON"))
        self.assertIn("## sheepdog 接口说明", boot)
        t = self.store.get_topic("tp_deploy")
        self.assertEqual((t["kind"], t["state"], t["conversation_id"], t["origin_note"]),
                         ("dynamic", sm.ACTIVE, cid, "FX 为什么开"))
        self.assertEqual(json.loads(t["anchors_json"]), ["oc_test_dep:all", "oc_test_dep2"])
        self.assertIsNotNone(t["onboarded_at"])
        self.assertEqual((self.row("om_test_n1")["topic_id"], self.row("om_test_n1")["dispatch_state"]),
                         ("tp_deploy", "pending"))
        self.d.dispatch_once()
        text = self.sink.to(cid)[-1]
        self.assertIn("om_test_n1", text)
        self.assertIn("总线备注（不是主人原话）：FX 为什么开", text)

    def test_conflicts_rejected(self):
        self.new(key="deploy", chats=[("oc_test_dep", False)])
        cases = [
            (dict(key="Bad"), "只允许"),
            (dict(key="bus"), "只允许"),
            (dict(key="alpha"), "名册"),
            (dict(key="deploy"), "已存在"),
            (dict(key="x1", chats=[("oc_test_alpha_p2p", False)]), "名册里的 tp_alpha"),
            (dict(key="x2", chats=[("oc_test_dep", True)]), "总线新开的 tp_deploy"),
            (dict(key="x3", ids=["om_test_missing"]), "没有消息"),
            (dict(key="x4", title=""), "必填"),
        ]
        n = len(self.sink.created)
        for kw, err in cases:
            with self.assertRaisesRegex(ValueError, err, msg=kw):
                self.new(**kw)
        self.assertEqual(len(self.sink.created), n)  # 校验失败不建会话
        # 被 hold 的消息不能随 new-session 转
        sec = load_security(FIX / "security.toml")
        self.poll([msg(message_id="om_test_h1", chat_type="p2p", chat_id="oc_test_stranger",
                       sender_tenant_key="tenant_test_own", content="fx-holdme")], security=sec)
        with self.assertRaisesRegex(ValueError, "安全规则"):
            self.new(key="x5", ids=["om_test_h1"])

    def test_quota(self):
        self.cfg.bus.max_new_sessions_per_day = 2
        self.new(key="a1")
        self.new(key="a2")
        close_session(self.store, "tp_a1")  # 收掉的也算今天开过的
        with self.assertRaisesRegex(ValueError, "上限，请找主人"):
            self.new(key="a3")
        self.cfg.bus.max_new_sessions_per_day = 0
        with self.assertRaisesRegex(ValueError, "禁止.*请找主人"):
            self.new(key="a4")

    def test_roster_sync_leaves_dynamic(self):
        self.new(key="deploy")
        self.d.sync_roster()
        self.d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_deploy")["state"], sm.ACTIVE)
        # 名册后来加了同名 key：跳过并报错，不把 dynamic 改成 adopted
        d = roster_data()
        d["session"].append({"key": "deploy", "mode": "managed", "conversation_id": "conv_test_x"})
        self.d.roster = parse_roster(d)
        with self.assertLogs("sheepdog", "ERROR"):
            self.d.sync_roster()
        t = self.store.get_topic("tp_deploy")
        self.assertEqual((t["kind"], t["state"]), ("dynamic", sm.ACTIVE))
        self.assertNotEqual(t["conversation_id"], "conv_test_x")

    def test_chat_ownership(self):
        self.new(key="deploy", chats=[("oc_test_dall", True), ("oc_test_dlvl", False)])
        self.poll([msg(message_id="om_test_c1", chat_id="oc_test_dall"),                       # 普通群消息，:all 也推
                   msg(message_id="om_test_c2", chat_id="oc_test_dlvl"),                       # 普通群消息，非 all 进 Inbox
                   msg(message_id="om_test_c3", chat_id="oc_test_dlvl", mentions=[Mention(ME)])])
        self.assertEqual(self.row("om_test_c1")["topic_id"], "tp_deploy")
        self.assertIn("owner:deploy", json.loads(self.row("om_test_c1")["tags_json"]))
        self.assertEqual((self.row("om_test_c2")["route"], self.row("om_test_c2")["topic_id"]), ("inbox", None))
        self.assertEqual(self.row("om_test_c3")["topic_id"], "tp_deploy")

    def test_done_closes_and_releases(self):
        r = self.new(key="deploy", chats=[("oc_test_dall", True)])
        self.poll([msg(message_id="om_test_d1", chat_type="p2p", chat_id="oc_test_dall")])
        self.d.dispatch_once()
        self.assertEqual(len(self.sink.to(r["conversation_id"])), 1)
        self.ack("tp_deploy", "done")
        self.d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_deploy")["state"], sm.CLOSED)
        self.poll([msg(message_id="om_test_d2", chat_type="p2p", chat_id="oc_test_dall")])
        self.assertEqual(self.row("om_test_d2")["topic_id"], BUS_TOPIC_ID)
        # 对比：adopted 的 done 不关
        self.poll([msg(message_id="om_test_d3", chat_type="p2p", chat_id="oc_test_alpha_p2p")])
        self.d.dispatch_once()
        self.ack("tp_alpha", "done")
        self.d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.ACTIVE)

    def test_close_session_only_dynamic(self):
        self.new(key="deploy", chats=[("oc_test_dall", True)])
        for tid, err in (("tp_alpha", "不是总线新开"), ("tp_ops", "不是总线新开"), (BUS_TOPIC_ID, "不是总线新开"),
                         ("tp_nobody", "不存在")):
            with self.assertRaisesRegex(ValueError, err):
                close_session(self.store, tid)
        self.poll([msg(message_id="om_test_p1", chat_type="p2p", chat_id="oc_test_dall")])  # 还没投就关
        close_session(self.store, "tp_deploy")
        with self.assertRaisesRegex(ValueError, "已经收掉"):
            close_session(self.store, "tp_deploy")
        self.d.dispatch_once()
        self.assertIn("om_test_p1", self.sink.to("conv_test_bus")[-1])  # 没投出去的退回总线

    def test_roster_placeholder_includes_dynamic(self):
        self.new(key="deploy", chats=[("oc_test_dall", True)])
        self.poll([msg(message_id="om_test_r1", chat_type="p2p", chat_id="oc_test_stranger")])
        self.d.dispatch_once()
        bus = self.sink.to("conv_test_bus")[-1]
        self.assertIn("名册更新", bus)
        self.assertIn("「发布排查」 topic `tp_deploy`", bus)
        self.assertIn("dynamic（总线新开", bus)
        self.assertIn("`oc_test_dall`（全部消息）", bus)
        # 新建的总线 bootstrap 里同样含 dynamic
        from sheepdog.engine import Dispatcher, open_dynamic_topics
        from sheepdog.prompts import bootstrap_prompt
        boot = bootstrap_prompt(self.d.playbook, "[managed] 总线", "总线", BUS_TOPIC_ID, self.roster, "", [15], 60,
                                open_dynamic_topics(self.store))
        self.assertIn("tp_deploy", boot)

    def test_receipt_cannot_claim_chats(self):
        r = self.new(key="deploy")
        self.poll([msg(message_id="om_test_rc", chat_type="p2p", chat_id="oc_test_stranger")])
        self.d.dispatch_once()
        self.ack(BUS_TOPIC_ID)
        from sheepdog.engine import forward_messages
        forward_messages(self.store, "tp_deploy", ["om_test_rc"])
        self.d.dispatch_once()
        t = self.store.get_topic("tp_deploy")
        from sheepdog.engine import write_receipt
        write_receipt(self.cfg, "tp_deploy", t["pending_batch_id"],
                      {"status": "handled", "anchors": ["oc_test_victim:all", "project:FX"]})
        self.d.dispatch_once()
        anchors = json.loads(self.store.get_topic("tp_deploy")["anchors_json"])
        self.assertEqual(anchors, ["project:FX"])
        self.poll([msg(message_id="om_test_v1", chat_id="oc_test_victim")])
        self.assertIsNone(self.row("om_test_v1")["topic_id"])

    def test_cli_dry_run_and_sessions(self):
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\nsink = "dryrun"\n', encoding="utf-8")
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}

        def run(*argv):
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), redirect_stdout(out):
                rc = cli.main(list(argv))
            return rc, out.getvalue()
        rc, text = run("new-session", "--key", "dry1", "--title", "T", "--duty", "D", "--chat", "oc_test_z:all", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIsNone(self.store.get_topic("tp_dry1"))  # dry-run 不写账本
        self.new(key="deploy", chats=[("oc_test_dall", True)], note="FX 创建原因")
        rc, text = run("sessions")
        self.assertIn("mode=dynamic（总线新开）", text)
        self.assertIn("创建原因: FX 创建原因", text)
        self.assertIn("oc_test_dall（全部）", text)
        rc, text = run("close-session", "--topic", "tp_alpha")
        self.assertEqual(rc, 2)
        rc, text = run("close-session", "--topic", "tp_deploy")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
