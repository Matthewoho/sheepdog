"""7.14 防止和 Agent 机器人来回循环：名字 / id / 机器人类型判定并加标注照常路由；会话代回次数熔断、
冷却期内非主人消息进 Inbox、主人消息不受影响、总线提示一次、冷却结束恢复、loops 与 --clear、配置缺省值。"""

import io
import os
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.config import LoopGuardConfig
from sheepdog.engine import BUS_TOPIC_ID, Collector
from sheepdog.models import Mention
from sheepdog.prompts import AGENT_SENDER_LABEL
from sheepdog.roster import Roster

from test_roster import ME, Base, FakeSource, msg

PREFIX = "FX-PREFIX "
LOOP = "oc_test_loop"


def now(minutes: float = 0) -> str:
    return (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


class LoopGuardTest(Base):
    def setUp(self):
        super().setUp()
        self.roster = Roster()
        self.cfg.owner_context.enabled = True
        self.cfg.owner_context.skip_prefixes = [PREFIX]
        self.cfg.loop_guard.agent_sender_names = ["fx-agentbot"]
        self.cfg.loop_guard.agent_sender_ids = ["ou_test_agentid"]
        self.d = self.disp(Roster())
        self.d.init()

    def poll(self, msgs):
        return Collector(self.cfg, self.store, FakeSource([msgs]), self.roster).poll_once()

    def agent(self, mid, **kw):
        base = dict(message_id=mid, chat_id=LOOP, chat_name="循环群", sender_name="FX-AgentBot 助手",
                    sender_id="ou_test_wiz", mentions=[Mention(ME)], create_time=now())
        base.update(kw)
        return msg(**base)

    def reply(self, mid, minutes=0):
        return msg(message_id=mid, chat_id=LOOP, chat_name="循环群", sender_id=ME, sender_name="主人",
                   content=PREFIX + "收到", create_time=now(minutes))

    def bus_notices(self):
        return self.store.conn.execute("SELECT * FROM messages WHERE reason='loop_guard' AND chat_type='sheepdog'").fetchall()

    def test_defaults(self):
        lg = LoopGuardConfig()
        self.assertEqual((lg.agent_sender_names, lg.agent_sender_ids, lg.treat_all_bots_as_agents, lg.max_agent_replies,
                          lg.window_minutes, lg.cooldown_minutes), ([], [], True, 4, 10, 30))

    def test_agent_detection_and_label(self):
        self.poll([self.agent("om_test_n1"),                                                       # 名字（不分大小写）
                   msg(message_id="om_test_i1", chat_type="p2p", chat_id="oc_test_p1", sender_id="ou_test_agentid",
                       sender_name="普通名字"),                                                    # id
                   msg(message_id="om_test_b1", chat_type="p2p", chat_id="oc_test_p2", sender_type="app",
                       sender_name="某机器人", content="出故障了"),                                 # 机器人类型
                   msg(message_id="om_test_h1", chat_type="p2p", chat_id="oc_test_p3", sender_id="ou_test_human")])
        for mid in ("om_test_n1", "om_test_i1", "om_test_b1"):
            r = self.row(mid)
            self.assertIn("agent_sender", r["tags_json"], mid)
            self.assertEqual(r["route"], "dispatch", mid)  # 照常路由
        self.assertNotIn("agent_sender", self.row("om_test_h1")["tags_json"])
        self.d.dispatch_once()
        text = self.sink.to("conv_test_bus")[-1]
        self.assertEqual(text.count(AGENT_SENDER_LABEL), 3)
        # 关掉「机器人一律算 Agent」：只按名字 / id
        self.cfg.loop_guard.treat_all_bots_as_agents = False
        self.poll([msg(message_id="om_test_b2", chat_type="p2p", chat_id="oc_test_p2", sender_type="app",
                       sender_name="某机器人", content="出故障了")])
        self.assertNotIn("agent_sender", self.row("om_test_b2")["tags_json"])

    def test_breaker(self):
        self.poll([self.agent("om_test_a0")])
        self.d.dispatch_once()  # 投到总线；之后主人的发言会作为背景跟过去
        self.ack(BUS_TOPIC_ID)
        # 窗口外的代回不算
        self.poll([self.reply("om_test_old1", minutes=-30), self.reply("om_test_old2", minutes=-25)])
        self.poll([self.reply(f"om_test_r{i}") for i in range(4)])
        self.assertEqual(self.store.open_loop_events(), [])  # 4 次不超过阈值
        self.poll([self.reply("om_test_r4")])
        events = self.store.open_loop_events()
        self.assertEqual((len(events), events[0]["replies"]), (1, 5))
        notices = self.bus_notices()
        self.assertEqual(len(notices), 1)
        self.assertIn("疑似循环：循环群", notices[0]["content"])
        # 冷却期：非主人消息进 Inbox，不投；主人本人的消息照常作为背景
        self.poll([self.agent("om_test_a1"), msg(message_id="om_test_hm", chat_id=LOOP, chat_name="循环群",
                                                   sender_id="ou_test_human", mentions=[Mention(ME)]),
                   msg(message_id="om_test_own", chat_id=LOOP, sender_id=ME, content="我自己说一句", create_time=now())])
        for mid in ("om_test_a1", "om_test_hm"):
            self.assertEqual((self.row(mid)["route"], self.row(mid)["reason"], self.row(mid)["topic_id"]),
                             ("inbox", "loop_guard", None))
        self.assertEqual((self.row("om_test_own")["reason"], self.row("om_test_own")["topic_id"]),
                         ("owner_context", BUS_TOPIC_ID))
        # 冷却中继续代回：不重复触发、不重复提示
        self.poll([self.reply("om_test_r5"), self.reply("om_test_r6")])
        self.assertEqual(len(self.store.open_loop_events()), 1)
        self.assertEqual(len(self.bus_notices()), 1)
        self.d.dispatch_once()
        self.assertIn("疑似循环", "\n".join(self.sink.to("conv_test_bus")))
        # 冷却结束：自动恢复
        self.store.conn.execute("UPDATE loop_events SET until=?", (now(-1),))
        self.store.conn.commit()
        self.poll([self.agent("om_test_a2")])
        self.assertEqual(self.row("om_test_a2")["route"], "dispatch")

    def test_loops_cli_and_clear(self):
        self.poll([self.agent("om_test_c_agent")])
        self.poll([self.reply(f"om_test_c{i}") for i in range(5)])
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\n', encoding="utf-8")
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}

        def run(*argv):
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), redirect_stdout(out):
                self.assertEqual(cli.main(list(argv)), 0)
            return out.getvalue()
        text = run("loops")
        self.assertIn("冷却中 1 个聊天", text)
        self.assertIn(f"循环群（{LOOP}）", text)
        self.assertIn("已解除", run("loops", "--clear", LOOP))
        self.assertIn("冷却中 0 个聊天", run("loops"))
        self.assertIn("[已手动解除]", run("loops"))
        self.poll([self.agent("om_test_after")])
        self.assertEqual(self.row("om_test_after")["route"], "dispatch")

    def human(self, mid, minutes=0):
        return msg(message_id=mid, chat_id=LOOP, chat_name="循环群", sender_name="真人同事", sender_id="ou_test_person",
                   mentions=[Mention(ME)], create_time=now(minutes))

    def test_human_conversation_not_tripped(self):
        self.poll([self.human("om_test_p1")])
        with self.assertLogs("sheepdog", "INFO") as logs:
            self.poll([self.reply(f"om_test_hr{i}") for i in range(6)])
        self.assertEqual(self.store.open_loop_events(), [])
        self.assertTrue(any("对方为真人，不熔断" in line for line in logs.output))
        self.poll([self.human("om_test_p2")])
        self.assertEqual(self.row("om_test_p2")["route"], "dispatch")  # 真人照常投递

    def test_mixed_chat_uses_latest_other_sender(self):
        # 先机器人、后真人：最近一条是真人 → 不熔断
        self.poll([self.agent("om_test_m1", create_time=now(-3)), self.human("om_test_m2", minutes=-2)])
        self.poll([self.reply(f"om_test_mr{i}") for i in range(5)])
        self.assertEqual(self.store.open_loop_events(), [])
        # 机器人又说话、成了最近一条 → 再代回就熔断
        self.poll([self.agent("om_test_m3", create_time=now(-1))])
        self.poll([self.reply("om_test_mr5")])
        self.assertEqual(len(self.store.open_loop_events()), 1)
        # 窗口外的机器人消息不算：只有窗口外的 Agent、没有窗口内的非主人消息 → 不熔断
        self.store.clear_loop(LOOP)
        self.store.conn.execute("DELETE FROM messages WHERE chat_id=? AND sender_id != ?", (LOOP, ME))
        self.store.conn.commit()
        self.poll([self.agent("om_test_m4", create_time=now(-60))])
        self.poll([self.reply("om_test_mr6")])
        self.assertEqual(self.store.open_loop_events(), [])

    def test_no_prefix_no_breaker(self):
        self.cfg.owner_context.skip_prefixes = []
        self.poll([self.reply(f"om_test_x{i}") for i in range(8)])
        self.assertEqual(self.store.open_loop_events(), [])
