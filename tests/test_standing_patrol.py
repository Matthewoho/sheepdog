"""7.50#6 + 7.57 + 7.58 回归测试：
- 7.58：dynamic 会话支持 standing / oneshot 生命周期（含标题前缀自动推断）；standing 会话回执 done 不收掉；
        已收掉的 dynamic 会话同 key 跑 new-session 原地重开、复用原 conversation_id、不占每日新建额度；
        总线名册表列出已收掉的 dynamic 会话供重开。
- 7.57：名册 managed 会话配置 patrol_every_minutes / patrol_prompt / patrol_hours，
        空闲且无待投信号时按间隔与时间窗入队定时巡检系统消息。
- 7.50#6：批次记了 sent 但超过 verify_transcript_minutes 在 Antigravity 会话记录里仍找不到且无动作时，
          改标 uncertain、放开 pending_batch_id 并通知主人；redeliver 支持 --topic 及未进会话记录的批次；
          forward 对未进会话记录的同会话消息允许重推，且当原批次全部消息被转走时当场释放原会话的 pending_batch_id。
全部使用 *_test_* 合成数据。"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog import session as sm
from sheepdog.engine import (
    BUS_TOPIC_ID,
    Collector,
    closed_dynamic_topics,
    close_session,
    forward_messages,
    open_dynamic_topics,
)
from sheepdog.prompts import bootstrap_prompt
from sheepdog.roster import RosterError, parse_patrol_hours, parse_roster
from sheepdog.store import ONESHOT, STANDING, is_standing_topic
from test_gaps_delivery import TranscriptSink
from test_roster import ME, Base, FakeSource, msg, roster_data


class StandingPatrolTest(Base):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(Path(self.tmp.name) / "app")})
        self.env.start()
        self.sink = TranscriptSink()
        self.d = self.disp()
        self.d.init()
        self.ack("tp_alpha")
        self.d.dispatch_once()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def poll(self, msgs, muted=()):
        return Collector(self.cfg, self.store, FakeSource([msgs], muted), self.roster).poll_once()

    def test_758_standing_dynamic_session_survives_done_receipt(self):
        r = self.d.new_session(
            "person_alice", "Alice 对接", "跟进 Alice 的所有长期需求",
            [("oc_test_alice_p2p", True)], [], "一人一会话", lifecycle="standing",
        )
        self.assertEqual(r["lifecycle"], STANDING)
        t = self.store.get_topic("tp_person_alice")
        self.assertEqual(t["lifecycle"], STANDING)
        self.assertTrue(is_standing_topic(t))

        self.poll([msg(message_id="om_test_st1", chat_type="p2p", chat_id="oc_test_alice_p2p", content="第一件事")])
        self.d.dispatch_once()
        self.ack("tp_person_alice", "done")
        self.d.dispatch_once()

        t = self.store.get_topic("tp_person_alice")
        self.assertEqual(t["state"], sm.ACTIVE)
        self.assertIsNone(t["pending_batch_id"])

        # 聊天归属保留，后续消息继续进 tp_person_alice 而不是回总线
        self.poll([msg(message_id="om_test_st2", chat_type="p2p", chat_id="oc_test_alice_p2p", content="第二件事")])
        self.assertEqual(self.row("om_test_st2")["topic_id"], "tp_person_alice")
        self.d.dispatch_once()

        # 晚到的 done 回执（回执超时标 missed 后补交 done）同样不关 standing 会话
        bid = self.store.get_topic("tp_person_alice")["pending_batch_id"]
        self.store.set_dispatch_state(bid, "missed", error="回执超时")
        self.store.update_topic("tp_person_alice", state=sm.ACTIVE, pending_batch_id=None)
        from sheepdog.engine import write_receipt
        write_receipt(self.cfg, "tp_person_alice", bid, {"status": "done", "summary": "晚到完成"})
        self.d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_person_alice")["state"], sm.ACTIVE)

        # 只有人工 close-session 才收掉
        close_session(self.store, "tp_person_alice")
        self.assertEqual(self.store.get_topic("tp_person_alice")["state"], sm.CLOSED)

    def test_758_person_prefix_infers_standing_lifecycle(self):
        for idx, title in enumerate(("人·张三", "职能·发版值守", "项目·Sheepdog")):
            key = f"p_{idx}"
            r = self.d.new_session(key, title, "按人或职能常驻", [], [], "")
            self.assertEqual(r["lifecycle"], STANDING, msg=title)
            t = self.store.get_topic(f"tp_{key}")
            self.assertEqual(t["lifecycle"], STANDING)
            self.assertTrue(is_standing_topic(t))

        self.cfg.bus.max_new_sessions_per_day = 20
        for idx, title in enumerate(("事件·故障排查", "临时·导数据", "事项·迁移", "普通排查")):
            key = f"o_{idx}"
            r = self.d.new_session(key, title, "一次性任务", [], [], "")
            self.assertEqual(r["lifecycle"], ONESHOT, msg=title)
            t = self.store.get_topic(f"tp_{key}")
            self.assertEqual(t["lifecycle"], ONESHOT)
            self.assertFalse(is_standing_topic(t))

        # 兼容老账本 lifecycle 为空但标题带「人·」前缀的会话
        with self.store.tx() as c:
            c.execute("UPDATE topics SET lifecycle='' WHERE topic_id='tp_p_0'")
        t_legacy = self.store.get_topic("tp_p_0")
        self.assertTrue(is_standing_topic(t_legacy))

    def test_758_oneshot_dynamic_session_closes_on_done_receipt(self):
        r = self.d.new_session(
            "inc_01", "事件·临时回滚", "处理一次回滚",
            [("oc_test_inc_chat", True)], [], "临时事件",
        )
        self.assertEqual(r["lifecycle"], ONESHOT)
        self.poll([msg(message_id="om_test_os1", chat_type="p2p", chat_id="oc_test_inc_chat", content="请回滚")])
        self.d.dispatch_once()
        self.ack("tp_inc_01", "done")
        self.d.dispatch_once()

        self.assertEqual(self.store.get_topic("tp_inc_01")["state"], sm.CLOSED)
        self.poll([msg(message_id="om_test_os2", chat_type="p2p", chat_id="oc_test_inc_chat", content="后续消息")])
        self.assertEqual(self.row("om_test_os2")["topic_id"], BUS_TOPIC_ID)

    def test_758_reopen_closed_dynamic_session_reuses_conversation_id_and_skips_quota(self):
        self.cfg.bus.max_new_sessions_per_day = 1
        r1 = self.d.new_session(
            " peer_bob ", "事件·Bob 初次咨询", "初次职责",
            [("oc_test_bob", True)], [], "第一次开",
        )
        orig_cid = r1["conversation_id"]
        created_count = len(self.sink.created)
        self.assertFalse(r1["reopened"])

        # 收掉该会话
        close_session(self.store, "tp_peer_bob")
        self.assertEqual(self.store.get_topic("tp_peer_bob")["state"], sm.CLOSED)

        # 配额已满（1/1），新 key 被拒
        with self.assertRaisesRegex(ValueError, "上限，请找主人"):
            self.d.new_session("other_key", "事件·别的", "别的职责", [], [], "")

        # 就算把每日新建配额设为 0，同 key 重开已收掉的会话依然放行、不调 sink 新建、复用原 conversation_id
        self.cfg.bus.max_new_sessions_per_day = 0
        self.poll([msg(message_id="om_test_re1", chat_type="p2p", chat_id="oc_test_stranger", content="Bob 又找来了")])
        self.d.dispatch_once()
        self.ack(BUS_TOPIC_ID)

        r2 = self.d.new_session(
            "peer_bob", "人·Bob", "长期对接 Bob",
            [("oc_test_bob", True)], ["om_test_re1"], "重开并改为常驻",
        )
        self.assertTrue(r2["reopened"])
        self.assertEqual(r2["conversation_id"], orig_cid)
        self.assertEqual(r2["lifecycle"], STANDING)
        self.assertEqual(len(self.sink.created), created_count)

        t = self.store.get_topic("tp_peer_bob")
        self.assertEqual((t["state"], t["conversation_id"], t["lifecycle"], t["duty"], t["origin_note"]),
                         (sm.ACTIVE, orig_cid, STANDING, "长期对接 Bob", "重开并改为常驻"))
        self.assertEqual(json.loads(t["anchors_json"]), ["oc_test_bob:all"])

        self.d.dispatch_once()
        self.assertIn("om_test_re1", self.sink.to(orig_cid)[-1])

    def test_758_bus_roster_table_lists_closed_dynamic_sessions_for_reopen(self):
        self.d.new_session("p_chen", "人·陈工", "对接陈工", [("oc_test_chen", True)], [], "")
        r_closed = self.d.new_session("inc_net", "事件·网络抖动", "排查网络抖动", [], [], "")
        close_session(self.store, "tp_inc_net")

        self.assertEqual([t["topic_id"] for t in open_dynamic_topics(self.store)], ["tp_p_chen"])
        self.assertEqual([t["topic_id"] for t in closed_dynamic_topics(self.store, self.roster)], ["tp_inc_net"])

        boot = bootstrap_prompt(
            self.d.playbook, "[managed] 总线", "总线", BUS_TOPIC_ID, self.roster, "", [15], 60,
            open_dynamic_topics(self.store), closed_dynamic=closed_dynamic_topics(self.store, self.roster),
        )
        self.assertIn("已收掉但可原地重开的动态会话", boot)
        self.assertIn(f"key `inc_net`（topic `tp_inc_net`） | conversation `{r_closed['conversation_id']}` | 已收掉（可重开）", boot)

        # 总线收到新消息时，「名册更新」里同样带出已收掉可重开的会话
        self.poll([msg(message_id="om_test_bus_r", chat_type="p2p", chat_id="oc_test_stranger", content="新消息")])
        self.d.dispatch_once()
        bus_last = self.sink.to("conv_test_bus")[-1]
        self.assertIn("已收掉但可原地重开的动态会话", bus_last)
        self.assertIn("key `inc_net`", bus_last)

        # 常驻类（人·/职能·/项目·）收掉后在 closed_dynamic_topics 中排在一次性会话前面，不被一次性会话挤掉
        close_session(self.store, "tp_p_chen")
        self.store.update_topic("tp_p_chen", last_active="2026-10-18T10:00:00+08:00")
        self.store.update_topic("tp_inc_net", last_active="2026-10-19T10:00:00+08:00")
        self.assertEqual([t["topic_id"] for t in closed_dynamic_topics(self.store, self.roster, limit=1)], ["tp_p_chen"])

    def test_757_patrol_enqueues_when_idle_and_respects_interval(self):
        d = roster_data()
        d["session"][0]["patrol_every_minutes"] = 30
        d["session"][0]["patrol_prompt"] = "请巡检看板与未结事项，没事回执 handled"
        d["session"][0]["patrol_hours"] = ""
        self.roster = parse_roster(d)
        self.d.roster = self.roster

        before_msgs = len(self.sink.to("conv_test_alpha"))
        self.d.dispatch_once()
        after_msgs = self.sink.to("conv_test_alpha")
        self.assertEqual(len(after_msgs), before_msgs + 1)
        self.assertIn("[例行巡检]", after_msgs[-1])
        self.assertIn("请巡检看板与未结事项，没事回执 handled", after_msgs[-1])
        self.assertIsNotNone(self.store.get_meta("patrol_last:tp_alpha"))

        # 回执后立刻再跑一轮：未到 30 分钟间隔，不再入队巡检
        self.ack("tp_alpha", "handled")
        self.d.dispatch_once()
        self.assertEqual(len(self.sink.to("conv_test_alpha")), before_msgs + 1)

        # 把上次巡检时间拨回 31 分钟前：下一轮再次入队并投递巡检
        past = (datetime.now().astimezone() - timedelta(minutes=31)).isoformat(timespec="seconds")
        self.store.set_meta("patrol_last:tp_alpha", past)
        self.d.dispatch_once()
        self.assertEqual(len(self.sink.to("conv_test_alpha")), before_msgs + 2)
        self.assertIn("[例行巡检]", self.sink.to("conv_test_alpha")[-1])

    def test_757_patrol_skips_when_busy_or_outside_hours_or_pending_signals(self):
        # 校验名册字段约束
        bad1 = roster_data()
        bad1["session"][0]["patrol_prompt"] = "巡检"
        with self.assertRaisesRegex(RosterError, "patrol_every_minutes > 0"):
            parse_roster(bad1)

        bad2 = roster_data()
        bad2["session"][0]["patrol_every_minutes"] = 15
        bad2["session"][0]["patrol_hours"] = "25-26"
        with self.assertRaisesRegex(RosterError, "patrol_hours"):
            parse_roster(bad2)

        self.assertEqual(parse_patrol_hours("0-2,9-11"), frozenset({0, 1, 2, 9, 10, 11}))
        self.assertEqual(parse_patrol_hours(""), frozenset(range(24)))

        now_hour = datetime.now().astimezone().hour
        other_hour = (now_hour + 2) % 24
        d = roster_data()
        d["session"][0]["patrol_every_minutes"] = 15
        d["session"][0]["patrol_prompt"] = "巡检任务"
        d["session"][0]["patrol_hours"] = str(other_hour)
        self.d.roster = parse_roster(d)

        # 1) 不在活跃时间段内：不入队
        self.assertEqual(self.d._enqueue_patrols(), 0)
        self.assertEqual(self.store.count_pending_signals("tp_alpha"), 0)

        # 2) 会话已有待投的真实信号：不叠加巡检
        d["session"][0]["patrol_hours"] = ""
        self.d.roster = parse_roster(d)
        self.poll([msg(message_id="om_test_pat_sig", chat_type="p2p", chat_id="oc_test_alpha_p2p", content="真人找")])
        self.assertEqual(self.d._enqueue_patrols(), 0)
        pending = [r for r in self.store.pending_dispatch() if r["topic_id"] == "tp_alpha"]
        self.assertEqual([m["message_id"] for m in pending], ["om_test_pat_sig"])

        # 3) 会话正在 running（手上有未回执批次）：不入队巡检
        self.d.dispatch_once()
        self.assertEqual(self.store.get_topic("tp_alpha")["state"], sm.RUNNING)
        self.store.set_meta("patrol_last:tp_alpha", (datetime.now().astimezone() - timedelta(minutes=30)).isoformat(timespec="seconds"))
        self.assertEqual(self.d._enqueue_patrols(), 0)
        self.assertEqual(self.store.count_pending_signals("tp_alpha"), 0)

    def test_750_6_unverified_transcript_marks_uncertain_and_unblocks_topic(self):
        self.cfg.session.verify_transcript_minutes = 5
        self.sink.write = False
        self.poll([msg(message_id="om_test_uv1", chat_type="p2p", chat_id="oc_test_alpha_p2p", content="可能会丢的批次")])
        self.d.dispatch_once()

        t = self.store.get_topic("tp_alpha")
        bid = t["pending_batch_id"]
        self.assertIsNotNone(bid)
        self.assertEqual(t["state"], sm.RUNNING)
        self.assertEqual(self.store.get_dispatch(bid)["state"], "sent")

        # 送出满 6 分钟（超过 verify_transcript_minutes=5），且 transcript 里始终没有该批次号也没有新动作
        six_m_ago = (datetime.now().astimezone() - timedelta(minutes=6)).isoformat(timespec="seconds")
        with self.store.tx() as c:
            c.execute("UPDATE dispatches SET sent_at=? WHERE batch_id=?", (six_m_ago, bid))
            c.execute("UPDATE topics SET dispatched_at=? WHERE topic_id='tp_alpha'", (six_m_ago,))

        self.d.dispatch_once()

        # 不必苦等 20 分钟回执超时：当场改标 uncertain，放开会话 pending_batch_id，并登记通知主人
        self.assertEqual(self.store.get_dispatch(bid)["state"], "uncertain")
        self.assertEqual(self.row("om_test_uv1")["dispatch_state"], "uncertain")
        t_after = self.store.get_topic("tp_alpha")
        self.assertEqual(t_after["state"], sm.ACTIVE)
        self.assertIsNone(t_after["pending_batch_id"])
        self.assertIsNotNone(self.store.get_meta(f"uncertain_notice:{bid}"))

        # 下一条新消息不再被幽灵批次堵住，立刻能送出去
        self.sink.write = True
        self.poll([msg(message_id="om_test_uv2", chat_type="p2p", chat_id="oc_test_alpha_p2p", content="后续正常消息")])
        self.d.dispatch_once()
        self.assertEqual(self.row("om_test_uv2")["dispatch_state"], "delivered")
        self.assertIn("om_test_uv2", self.sink.to("conv_test_alpha")[-1])

    def test_750_6_redeliver_and_forward_recover_unverified_sent_batch(self):
        # 1) 批次虽记了 sent，但没进 transcript：支持 sheepdog redeliver --topic tp_alpha 直接重投
        self.sink.write = False
        self.poll([msg(message_id="om_test_rf1", chat_type="p2p", chat_id="oc_test_alpha_p2p", content="未落盘消息1")])
        self.d.dispatch_once()
        bid1 = self.store.get_topic("tp_alpha")["pending_batch_id"]
        self.assertEqual(self.store.get_dispatch(bid1)["state"], "sent")

        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\n', encoding="utf-8")
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}

        def run_cli(*argv):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, env), redirect_stdout(out), mock.patch("sys.stderr", err):
                rc = cli.main(list(argv))
            return rc, out.getvalue() + err.getvalue()

        rc, out = run_cli("redeliver", "--topic", "tp_alpha")
        self.assertEqual(rc, 0, msg=out)
        self.assertIn(bid1, out)
        self.assertEqual(self.store.get_dispatch(bid1)["state"], "redelivered")
        self.assertEqual(self.row("om_test_rf1")["dispatch_state"], "pending")
        self.assertIsNone(self.store.get_topic("tp_alpha")["pending_batch_id"])

        # 恢复落盘后重发成功；此时再跑 redeliver 会拒绝（因为已在 transcript 里）
        self.sink.write = True
        self.d.dispatch_once()
        self.assertEqual(self.row("om_test_rf1")["dispatch_state"], "delivered")
        rc_again, out_again = run_cli("redeliver", "--topic", "tp_alpha")
        self.assertEqual(rc_again, 2)
        self.assertIn("没有可重投的批次", out_again)

        # 2) forward 同会话：已落盘的跳过，未落盘的允许重推并释放原 pending_batch_id
        res_skip = forward_messages(self.store, "tp_alpha", ["om_test_rf1"])
        self.assertEqual(list(res_skip), [])
        self.assertEqual(len(res_skip.skipped), 1)
        self.ack("tp_alpha", "handled")
        self.d.dispatch_once()

        self.sink.write = False
        self.poll([msg(message_id="om_test_rf2", chat_type="p2p", chat_id="oc_test_alpha_p2p", content="未落盘消息2")])
        self.d.dispatch_once()
        bid2 = self.store.get_topic("tp_alpha")["pending_batch_id"]
        res_resend = forward_messages(self.store, "tp_alpha", ["om_test_rf2"], note="补推")
        self.assertEqual(list(res_resend), ["om_test_rf2"])
        self.assertEqual(self.row("om_test_rf2")["dispatch_state"], "pending")
        self.assertEqual(self.store.get_dispatch(bid2)["state"], "redelivered")
        self.assertIsNone(self.store.get_topic("tp_alpha")["pending_batch_id"])

    def test_750_6_forward_releases_source_pending_batch_when_all_messages_moved(self):
        self.poll([msg(message_id="om_test_mv1", chat_type="p2p", chat_id="oc_test_stranger", content="先到总线再转走")])
        self.d.dispatch_once()
        bus_topic = self.store.get_topic(BUS_TOPIC_ID)
        bus_bid = bus_topic["pending_batch_id"]
        self.assertIsNotNone(bus_bid)
        self.assertEqual(bus_topic["state"], sm.RUNNING)

        # 总线还没交回执就把这批里唯一的消息 forward 给了 tp_alpha
        moved = forward_messages(self.store, "tp_alpha", ["om_test_mv1"], note="转给 alpha")
        self.assertEqual(list(moved), ["om_test_mv1"])

        # 原会话（总线）的 pending_batch_id 当场释放，不再卡在 running 等回执
        bus_after = self.store.get_topic(BUS_TOPIC_ID)
        self.assertEqual(bus_after["state"], sm.ACTIVE)
        self.assertIsNone(bus_after["pending_batch_id"])
        self.assertEqual(self.store.get_dispatch(bus_bid)["state"], "acked")


if __name__ == "__main__":
    unittest.main()
