"""7.16 缺口 5、6 与配置热加载：prune 覆盖所有表和回执文件（未结 / 进行中的不删）；长消息按 max_message_lines
截断并提示 sheepdog show；run 每轮重读 config.toml，改关键词下一轮生效，改坏沿用旧值，state_dir / sink 变了只提示。"""

import io
import os
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.engine import BUS_TOPIC_ID, Collector, write_receipt
from sheepdog.roster import Roster

from test_roster import ME, Base, FakeSource, msg

OLD = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")


class PruneTest(Base):
    def test_prune_all_tables_and_receipts(self):
        s, c = self.store, self.store.conn
        d = self.disp(Roster())
        d.init()
        Collector(self.cfg, s, FakeSource([[msg(message_id="om_test_old", chat_type="p2p")]]), Roster()).poll_once()
        d.dispatch_once()
        done_batch = s.get_topic(BUS_TOPIC_ID)["pending_batch_id"]
        write_receipt(self.cfg, BUS_TOPIC_ID, done_batch, {"status": "handled"})
        d.dispatch_once()
        Collector(self.cfg, s, FakeSource([[msg(message_id="om_test_new", chat_type="p2p")]]), Roster()).poll_once()
        d.dispatch_once()
        live_batch = s.get_topic(BUS_TOPIC_ID)["pending_batch_id"]  # 还在等回执的批次
        write_receipt(self.cfg, BUS_TOPIC_ID, live_batch + "-x", {"status": "handled"})  # 一个无关的旧回执文件
        s.add_escalation("om_test_e_closed", "tp_bus", "oc_test_esc", "旧问题", OLD)
        s.expire_escalation("om_test_e_closed")
        s.add_escalation("om_test_e_open", "tp_bus", "oc_test_esc", "未结旧问题", OLD)
        s.add_bot_outbox("om_test_out", "tp_bus", False, "旧汇报", OLD)
        s.add_ack("om_test_a1", "oc_test_c", "p2p", "ou_test_a", "p2p", "rx1", None)
        s.mark_ack_removed("om_test_a1")
        s.add_ack("om_test_a2", "oc_test_c", "p2p", "ou_test_a", "p2p", "rx2", None)  # 未撤：进行中
        s.add_owner_reaction("om_test_r", "FX", ME, "rx3", True)
        w1 = s.add_watch("tp_bus", "ou_test_b", "", "旧等待")
        s.close_watch(w1, "done")
        w2 = s.add_watch("tp_bus", "ou_test_b", "", "还在等")
        a1 = s.add_action("init", {}, "test")
        s.finish_action(a1, result="ok")
        a2 = s.add_action("init", {}, "test")  # 排队中
        s.add_loop_event("oc_test_l", "群", 5, OLD)
        s.record_dispatch("b_test_unc", "tp_bus", ["om_test_x"], state="uncertain")
        # 全部改成 30 天前
        for sql in ("UPDATE messages SET first_seen=?", "UPDATE dispatches SET sent_at=?", "UPDATE escalations SET closed_at=? WHERE closed_at IS NOT NULL",
                    "UPDATE acks SET added_at=?", "UPDATE owner_reactions SET seen_at=?",
                    "UPDATE watches SET closed_at=? WHERE closed_at IS NOT NULL", "UPDATE actions SET done_at=? WHERE done_at IS NOT NULL"):
            c.execute(sql, (OLD,))
        c.commit()
        old_ts = time.time() - 30 * 86400
        for f in self.cfg.receipts_dir.glob("*/*.json"):
            os.utime(f, (old_ts, old_ts))
        out = s.prune(7, self.cfg.receipts_dir)
        self.assertIsNone(s.get_escalation("om_test_e_closed"))
        self.assertIsNotNone(s.get_escalation("om_test_e_open"))           # 未结不删
        self.assertIsNone(s.get_bot_outbox("om_test_out"))
        self.assertIsNone(s.get_ack("om_test_a1"))
        self.assertIsNotNone(s.get_ack("om_test_a2"))                      # 还没撤的不删
        self.assertIsNone(s.get_watch(w1))
        self.assertIsNotNone(s.get_watch(w2))                              # 还在等的不删
        ids = [a["id"] for a in s.list_actions(include_done=True)]
        self.assertEqual(ids, [a2])                                        # 排队中的不删
        self.assertEqual(s.list_loop_events(), [])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM owner_reactions").fetchone()[0], 0)
        self.assertIsNone(s.get_dispatch(done_batch))
        self.assertIsNotNone(s.get_dispatch(live_batch))                   # 还在等回执的不删
        self.assertIsNotNone(s.get_dispatch("b_test_unc"))                 # 不确定是否送达的不删
        self.assertIsNone(s.get_message("om_test_old"))
        self.assertIsNotNone(s.get_message("om_test_new"))                 # 还没回执的消息不删
        files = sorted(f.stem for f in self.cfg.receipts_dir.glob("*/*.json"))
        self.assertEqual(files, [])                                        # 旧回执文件被清（live 批次还没回执文件）
        self.assertGreaterEqual(out["receipt_files"], 2)
        # 正在等的批次如果已有回执文件，即使很旧也不删
        p = write_receipt(self.cfg, BUS_TOPIC_ID, live_batch, {"status": "handled"})
        os.utime(p, (old_ts, old_ts))
        s.prune(7, self.cfg.receipts_dir)
        self.assertTrue(p.exists())


class LongMessageTest(Base):
    def setUp(self):
        super().setUp()
        self.d = self.disp(Roster())
        self.d.init()

    def deliver(self, mid, n):
        body = "\n".join(f"第{i}行" for i in range(1, n + 1))
        Collector(self.cfg, self.store, FakeSource([[msg(message_id=mid, chat_type="p2p", chat_id="oc_test_" + mid,
                                                         content=body)]]), Roster()).poll_once()
        self.d.dispatch_once()
        text = self.sink.to("conv_test_bus")[-1]
        self.ack(BUS_TOPIC_ID)
        return text

    def test_41st_line_now_delivered(self):
        text = self.deliver("om_test_l41", 41)
        self.assertIn("> 第41行", text)  # 旧代码固定只送前 40 行
        self.assertNotIn("后面还有", text)

    def test_truncated_with_hint_and_show(self):
        text = self.deliver("om_test_l250", 250)
        self.assertIn("> 第200行", text)
        self.assertNotIn("> 第201行", text)
        self.assertIn("…（后面还有 50 行未显示，用 `sheepdog show om_test_l250` 看全文）", text)
        self.cfg.session.max_message_lines = 10
        text = self.deliver("om_test_l12", 12)
        self.assertIn("后面还有 2 行", text)
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\n', encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}), \
                redirect_stdout(out):
            self.assertEqual(cli.main(["show", "om_test_l250"]), 0)
        self.assertIn("第250行", out.getvalue())


class HotReloadTest(Base):
    def test_run_reloads_config_each_round(self):
        cfg_file = Path(self.tmp.name) / "config.toml"
        base = f'self_open_id = "{ME}"\nsink = "dryrun"\n'
        cfg_file.write_text(base + '[routing]\nkeywords = ["fxA"]\n', encoding="utf-8")
        seen = []

        def fake_cycle(collector, dispatcher):
            seen.append((list(collector.cfg.routing.keywords), dispatcher.cfg.sink, collector.cfg is dispatcher.cfg))

        writes = iter([
            base + '[routing]\nkeywords = ["fxB"]\n',                                   # 第 2 轮：改关键词
            base + '[watch]\nremind_minutes = [30, 15]\n',                               # 第 3 轮：改坏
            f'self_open_id = "{ME}"\nsink = "agentapi"\n[routing]\nkeywords = ["fxC"]\n',  # 第 4 轮：sink 变了
        ])

        def fake_sleep(_):
            try:
                cfg_file.write_text(next(writes), encoding="utf-8")
            except StopIteration:
                raise KeyboardInterrupt

        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        with mock.patch.dict(os.environ, env), mock.patch.object(cli, "_cycle", fake_cycle), \
                mock.patch.object(cli.time, "sleep", side_effect=fake_sleep), \
                self.assertLogs("root", "WARNING") as logs:
            with self.assertRaises(KeyboardInterrupt):
                cli.main(["run"])
        self.assertEqual([k for k, _, _ in seen], [["fxA"], ["fxB"], ["fxB"], ["fxC"]])
        self.assertEqual([s for _, s, _ in seen], ["dryrun"] * 4)  # sink 变更需要重启，本次沿用
        self.assertTrue(all(same for _, _, same in seen))
        self.assertTrue(any("配置无效，沿用上一份" in line for line in logs.output))
        self.assertTrue(any("需要重启" in line for line in logs.output))
