"""P0.5 追加：前任退休与接手（7.2）、拿不准找主人与转达（7.3）、等别人回复（7.4）、需求归属项目（7.5）。
全部使用合成数据（*_test_*）。"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog import session as sm
from sheepdog.config import RoutingConfig
from sheepdog.engine import BUS_TOPIC_ID, add_watch, forward_messages, predecessor_topic_id, write_receipt
from sheepdog.playbook import Playbook
from sheepdog.prompts import adopted_bootstrap, bootstrap_prompt, onboarding_prompt
from sheepdog.roster import RosterError, load_roster, parse_roster
from sheepdog.router import DROP, RouteContext, route

from test_roster import DROP_PREFIX, FIXTURES, ME, PREFIX, Base, msg, roster_data

FAKE_APP_DIR = "/tmp/sheepdog_test_app"


def handover_data() -> dict:
    """roster_data() 再加一个待建、接手前任的 managed 条目 beta。"""
    d = roster_data()
    d["session"].append({
        "key": "beta", "mode": "managed", "conversation_id": "", "title": "Beta 接手",
        "duty": "接手 Beta 的需求", "predecessor_conversation_id": "conv_test_old", "retire_predecessor": True,
        "chats": [{"chat_id": "oc_test_beta_p2p", "name": "Beta 私聊", "all_messages": True}],
    })
    return d


def ago(minutes: float) -> str:
    return (datetime.now().astimezone() - timedelta(minutes=minutes)).isoformat(timespec="seconds")


class HandoverRosterTest(unittest.TestCase):
    def test_spawn_entry_needs_explicit_empty_id(self):
        r = parse_roster(handover_data())
        self.assertTrue(r.by_key("beta").to_spawn)
        self.assertFalse(r.by_key("alpha").to_spawn)

    def test_predecessor_rules(self):
        d = handover_data()
        d["session"][2]["conversation_id"] = "conv_test_x"
        with self.assertRaisesRegex(RosterError, "留空"):
            parse_roster(d)
        d = handover_data()
        d["session"][2]["predecessor_conversation_id"] = "conv_test_alpha"  # 前任仍在名册里
        with self.assertRaisesRegex(RosterError, "仍登记"):
            parse_roster(d)
        d = handover_data()
        del d["session"][2]["predecessor_conversation_id"]
        with self.assertRaisesRegex(RosterError, "retire_predecessor"):
            parse_roster(d)


@mock.patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": FAKE_APP_DIR})
class SpawnTest(Base):
    def setUp(self):
        super().setUp()
        # 名册写成文件，用来证明 spawn 不回写
        self.roster_file = Path(self.tmp.name) / "roster.toml"
        self.roster_file.write_text(_toml(handover_data()), encoding="utf-8")
        self.roster = load_roster(self.roster_file, required=True)

    def beta(self):
        return self.store.get_topic("tp_beta")

    def test_spawn_once_and_no_roster_writeback(self):
        before = self.roster_file.read_bytes()
        d = self.disp()
        rep = d.init()
        self.assertIn("已新建", rep["spawn"]["tp_beta"])
        self.assertNotIn("tp_beta", rep["onboarding"])  # bootstrap 已含职责，不再单独 onboarding
        cid = self.beta()["conversation_id"]
        self.assertTrue(cid.startswith("conv_test_new"))
        self.assertEqual([t for t, _ in self.sink.created if "Beta" in t], ["[managed] Beta 接手"])
        # 再 init / 再 spawn / 每轮同步都不会重建，也不会把账本里的 id 冲掉
        d.init()
        self.assertIn("已于", d.spawn("beta")["skipped"])
        d.dispatch_once()
        self.assertEqual(sum("Beta" in t for t, _ in self.sink.created), 1)
        self.assertEqual(self.beta()["conversation_id"], cid)
        self.assertEqual(self.roster_file.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "不是待建"):
            d.spawn("alpha")

    def test_retire_before_new_and_predecessor_closed(self):
        d = self.disp()
        d.sync_roster()
        d.spawn("beta")
        events = [e for e in self.sink.log if e == ("send", "conv_test_old") or e == ("new", "[managed] Beta 接手")]
        self.assertEqual(events, [("send", "conv_test_old"), ("new", "[managed] Beta 接手")])
        retire = self.sink.to("conv_test_old")[0]
        self.assertIn("退休通知", retire)
        bid = self.store.get_topic("tp_beta.prev")["pending_batch_id"]
        self.assertIn(f"FX-RETIRE successor=[managed] Beta 接手 batch={bid}", retire)
        self.assertIn(f"sheepdog receipt --topic {predecessor_topic_id('tp_beta')} --batch {bid}", retire)
        prev = self.store.get_topic("tp_beta.prev")
        self.assertEqual((prev["state"], prev["kind"]), (sm.CLOSED, "retired"))
        # 前任的聊天立即归接手会话；不再往前任投递
        self.poll([msg(message_id="om_test_bp1", chat_type="p2p", chat_id="oc_test_beta_p2p")])
        d.dispatch_once()
        self.assertEqual(self.row("om_test_bp1")["topic_id"], "tp_beta")
        self.assertEqual(len(self.sink.to("conv_test_old")), 1)

    def test_successor_reads_first_then_gets_signals_and_handover(self):
        d = self.disp()
        d.sync_roster()
        r = d.spawn("beta")
        cid = r["conversation_id"]
        boot = next(p for t, p in self.sink.created if "Beta" in t)
        # successor.md 被拼进接手 bootstrap，前任 id / transcript / 目录占位符被替换
        self.assertIn(f"FX-SUCCESSOR id=conv_test_old "
                      f"transcript={FAKE_APP_DIR}/brain/conv_test_old/.system_generated/logs/transcript.jsonl "
                      f"dir={FAKE_APP_DIR}/brain/conv_test_old", boot)
        self.assertIn("FX-ONBOARDING title=Beta 接手", boot)
        self.assertIn("FX-COMMON title=Beta 接手 topic=tp_beta", boot)
        # 读完回执的批次与 status 属于接口说明
        self.assertIn(f"--batch {r['read_batch']}", boot)
        self.assertIn('"status":"needs_decision"', boot)
        self.assertEqual(self.beta()["state"], sm.RUNNING)

        # 读完之前：信号排队不丢
        self.poll([msg(message_id="om_test_bq1", chat_type="p2p", chat_id="oc_test_beta_p2p")])
        d.dispatch_once()
        self.assertEqual(self.sink.to(cid), [])
        self.assertEqual(self.row("om_test_bq1")["dispatch_state"], "pending")

        # 前任交接回执 → 原样转给接手会话
        prev = self.store.get_topic("tp_beta.prev")
        write_receipt(self.cfg, "tp_beta.prev", prev["pending_batch_id"], {"status": "done", "summary": "交接要点：等 Bob 回复"})
        self.ack("tp_beta", "needs_decision")
        d.dispatch_once()
        sent = self.sink.to(cid)
        self.assertEqual(len(sent), 1)
        self.assertIn("om_test_bq1", sent[0])
        self.assertIn("前任交接回执", sent[0])
        self.assertIn("交接要点：等 Bob 回复", sent[0])
        self.assertIsNone(self.store.get_topic("tp_beta.prev")["pending_batch_id"])
        d.dispatch_once()
        self.assertEqual(sum("交接要点" in c for c in self.sink.to(cid)), 1)  # 只转一次

    def test_waiting_spawn_queues(self):
        d = self.disp()
        self.poll([msg(message_id="om_test_ws1", chat_type="p2p", chat_id="oc_test_beta_p2p")])
        res = d.dispatch_once()  # run 循环不自动 spawn
        self.assertTrue(res["topics"]["tp_beta"]["waiting_spawn"])
        self.assertEqual(self.sink.to("conv_test_old"), [])
        self.assertEqual(self.row("om_test_ws1")["dispatch_state"], "pending")

    def test_adopted_bootstrap_without_predecessor(self):
        s = parse_roster(handover_data()).by_key("beta")
        s.predecessor_conversation_id = ""
        pb = Playbook(FIXTURES)
        text = adopted_bootstrap(pb, s, "[managed] Beta", "", [15, 30], 60)
        self.assertNotIn("FX-SUCCESSOR", text)
        self.assertIn("FX-ONBOARDING", text)
        self.assertIn("已就绪", text)


class EscalationTest(Base):
    MARK = "FX-DROP·Alpha 需求 要不要延期"

    def test_self_escalation_dropped(self):
        ctx = RouteContext(ME)
        cfg = RoutingConfig(drop_bot_message_prefixes=[DROP_PREFIX])
        d = route(msg(chat_type="p2p", sender_type="app", content=self.MARK), ctx, cfg)
        self.assertEqual((d.route, d.reason), (DROP, "self_escalation"))
        d = route(msg(chat_type="p2p", sender_type="bot", content=json.dumps({"text": self.MARK}, ensure_ascii=False)), ctx, cfg)
        self.assertEqual(d.reason, "self_escalation")
        # 人转述这句话不受影响
        self.assertEqual(route(msg(chat_type="p2p", content=self.MARK), ctx, cfg).reason, "p2p")

    def test_self_escalation_not_overridden_by_all_messages(self):
        self.poll([msg(message_id="om_test_esc", chat_type="p2p", chat_id="oc_test_alpha_p2p",
                       sender_type="app", content=self.MARK)])
        r = self.row("om_test_esc")
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), (DROP, "self_escalation", None))

    def test_common_rules_in_all_prompts(self):
        """common.md（7.1、7.3–7.5 的业务规则所在）拼进总线、接管、新建三类开场；接口说明含 watch / 回执。"""
        s = self.roster.by_key("alpha")
        pb = Playbook(FIXTURES, {"reply_prefix": PREFIX})
        for text, title, tid in (
                (bootstrap_prompt(pb, "[managed] 总线", "Lark 信号·总线", BUS_TOPIC_ID, self.roster, "", [15, 30], 60),
                 "Lark 信号·总线", BUS_TOPIC_ID),
                (onboarding_prompt(pb, s, "o1", [15, 30], 60), "Alpha 需求", "tp_alpha"),
                (adopted_bootstrap(pb, s, "[managed] Alpha", "", [15, 30], 60), "Alpha 需求", "tp_alpha")):
            self.assertIn(f"FX-COMMON title={title} topic={tid} prefix={PREFIX}", text)
            self.assertIn(f"sheepdog watch --topic {tid}", text)
            self.assertIn(f"sheepdog receipt --topic {tid}", text)
            self.assertIn("project:", text)

    def test_forward_quote_only_and_labels(self):
        d = self.disp()
        d.init()
        self.ack("tp_alpha")
        d.dispatch_once()
        forward_messages(self.store, "tp_alpha", [], note="总线觉得可以先答应", quote="可以，按周五交付",
                         verify_quote=lambda q: q == "可以，按周五交付")
        d.dispatch_once()
        last = self.sink.to("conv_test_alpha")[-1]
        self.assertIn("✅ 主人原话（已核对：主人在总线里亲口说过）：\n> 可以，按周五交付", last)
        self.assertIn("总线备注（不是主人原话）：总线觉得可以先答应", last)
        note_line = next(ln for ln in last.splitlines() if ln.startswith("总线备注"))
        self.assertNotIn("周五", note_line)
        with self.assertRaisesRegex(ValueError, "至少"):
            forward_messages(self.store, "tp_alpha", [])
        with self.assertRaisesRegex(ValueError, "known"):
            forward_messages(self.store, "tp_ops", [], quote="x", verify_quote=lambda q: True)


class WatchTest(Base):
    def setUp(self):
        super().setUp()
        self.d = self.disp()
        self.d.init()

    def reasons(self, reason):
        return self.store.conn.execute("SELECT * FROM messages WHERE reason=? ORDER BY first_seen", (reason,)).fetchall()

    def test_reply_routed_to_watcher_until_confirmed(self):
        wid = add_watch(self.store, "tp_alpha", "ou_test_bob", note="等 Bob 确认排期")
        # Bob 在一个不相干的免打扰群里回复：本该丢弃，等待优先
        self.poll([msg(message_id="om_test_bob1", chat_id="oc_test_muted", sender_id="ou_test_bob")],
                  muted={"oc_test_muted"})
        r = self.row("om_test_bob1")
        self.assertEqual((r["route"], r["topic_id"]), ("dispatch", "tp_alpha"))
        self.assertIn(f"watch:{wid}", json.loads(r["tags_json"]))
        w = self.store.get_watch(wid)
        self.assertEqual((w["status"], w["closed_at"]), ("replied", None))  # 候选，等会话确认（7.16）
        self.store.close_watch(wid, "done")  # 会话确认等到了
        # 等待结束后，Bob 再说话按常规路由
        self.poll([msg(message_id="om_test_bob2", chat_id="oc_test_muted", sender_id="ou_test_bob")],
                  muted={"oc_test_muted"})
        self.assertEqual(self.row("om_test_bob2")["route"], DROP)

    def test_chat_scoped_and_newest_wins(self):
        old = add_watch(self.store, BUS_TOPIC_ID, "ou_test_bob", note="旧")
        self.store.update_watch(old, started_at=ago(5))
        new = add_watch(self.store, "tp_alpha", "ou_test_bob", note="新")
        scoped = add_watch(self.store, BUS_TOPIC_ID, "ou_test_carol", chat_id="oc_test_g9")
        self.poll([msg(message_id="om_test_c1", chat_id="oc_test_other", sender_id="ou_test_carol"),
                   msg(message_id="om_test_b1", chat_id="oc_test_other", sender_id="ou_test_bob")])
        self.assertEqual(self.row("om_test_b1")["topic_id"], "tp_alpha")
        self.assertEqual(self.store.get_watch(new)["status"], "replied")
        self.assertIsNone(self.store.get_watch(old)["closed_at"])
        self.assertIsNone(self.row("om_test_c1")["topic_id"])  # 不在指定聊天，不算
        self.assertIsNone(self.store.get_watch(scoped)["closed_at"])

    def test_nudges_once_each_then_expire(self):
        wid = add_watch(self.store, "tp_alpha", "ou_test_bob", note="等 Bob 确认排期")
        self.store.update_watch(wid, started_at=ago(16))
        self.d.dispatch_once()
        self.d.dispatch_once()
        nudges = self.reasons("watch_nudge")
        self.assertEqual(len(nudges), 1)
        self.assertIn(f"FX-REMIND note=等 Bob 确认排期 person=ou_test_bob minutes=15 prefix={PREFIX.rstrip()}",
                      nudges[0]["content"])
        self.assertEqual(nudges[0]["topic_id"], "tp_alpha")
        self.store.update_watch(wid, started_at=ago(31))
        self.d.dispatch_once()
        self.d.dispatch_once()
        self.assertEqual([("minutes=30" in n["content"]) for n in self.reasons("watch_nudge")], [False, True])
        self.store.update_watch(wid, started_at=ago(61))
        self.d.dispatch_once()
        self.d.dispatch_once()
        expired = self.reasons("watch_expired")
        self.assertEqual(len(expired), 1)
        self.assertIn("FX-EXPIRE note=等 Bob 确认排期 person=ou_test_bob minutes=60", expired[0]["content"])
        self.assertEqual(self.store.get_watch(wid)["close_reason"], "expired")
        self.assertEqual(len(self.reasons("watch_nudge")), 2)

    def test_configurable_minutes(self):
        self.cfg.watch.remind_minutes, self.cfg.watch.expire_minutes = [5], 10
        wid = add_watch(self.store, "tp_alpha", "ou_test_bob")
        self.store.update_watch(wid, started_at=ago(6))
        self.d.dispatch_once()
        self.assertIn("minutes=5", self.reasons("watch_nudge")[0]["content"])
        self.store.update_watch(wid, started_at=ago(11))
        self.d.dispatch_once()
        self.assertIn("minutes=10", self.reasons("watch_expired")[0]["content"])
        self.assertEqual(len(self.reasons("watch_nudge")), 1)

    def test_late_tick_sends_single_nudge(self):
        wid = add_watch(self.store, "tp_alpha", "ou_test_bob")
        self.store.update_watch(wid, started_at=ago(35))
        self.d.dispatch_once()
        self.assertEqual(len(self.reasons("watch_nudge")), 1)
        w = self.store.get_watch(wid)
        self.assertIsNotNone(w["nudged_15_at"])
        self.assertIsNotNone(w["nudged_30_at"])

    def test_watch_target_checks(self):
        with self.assertRaisesRegex(ValueError, "known"):
            add_watch(self.store, "tp_ops", "ou_test_bob")
        self.assertGreater(add_watch(self.store, BUS_TOPIC_ID, "ou_test_bob"), 0)


class ProjectAnchorTest(Base):
    def test_project_anchor_kept_and_shown(self):
        roster_file = Path(self.tmp.name) / "roster.toml"
        roster_file.write_text(_toml(roster_data()), encoding="utf-8")
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text('self_open_id = "ou_test_me"\nroster_path = "roster.toml"\n', encoding="utf-8")
        d = self.disp()
        d.init()
        t = self.store.get_topic("tp_alpha")
        write_receipt(self.cfg, "tp_alpha", t["pending_batch_id"],
                      {"status": "handled", "summary": "ok", "anchors": ["project:Alpha 平台", "issue:TEST-1"]})
        d.dispatch_once()
        d.sync_roster()  # 名册同步不能冲掉 project: 锚点
        anchors = json.loads(self.store.get_topic("tp_alpha")["anchors_json"])
        self.assertIn("project:Alpha 平台", anchors)
        self.assertIn("oc_test_alpha_p2p", anchors)
        out = io.StringIO()
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            self.assertEqual(cli.main(["sessions"]), 0)
        text = out.getvalue()
        self.assertIn("关联项目: Alpha 平台", text)
        self.assertIn("mode=managed", text)
        self.assertIn("Alpha 私聊（全部）", text)


def _toml(data: dict) -> str:
    """把测试用的名册 dict 写成 TOML（只覆盖本测试用到的类型）。"""
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        return json.dumps(v, ensure_ascii=False)
    out = []
    for s in data["session"]:
        out.append("[[session]]")
        out += [f"{k} = {val(v)}" for k, v in s.items() if k != "chats"]
        for c in s.get("chats", []):
            out.append("[[session.chats]]")
            out += [f"{k} = {val(v)}" for k, v in c.items()]
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    unittest.main()
