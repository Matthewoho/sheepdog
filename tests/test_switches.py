"""7.40 开关：会增加负担的功能上线后能改一行配置关掉，不用重新发版。

- [escalation] followup：判「可能已处理」、核实提醒、快到期提醒、收掉会话的转总线提醒；关掉后都不做，resolve、来源记录、
  pending 清单照常；
- [escalation] owner_shortcut：「待办」快捷问法；关掉后照旧当普通回复；
- [session] owner_priority：主人回复插队；关掉后回到排队；
- [hooks] stop：Stop hook；关掉后什么都不输出。
常驻进程每轮重读配置（cli._reload_config），hook 每次调用现读配置：改完不用重启。这里各有一个「改配置文件 → 不重建对象就生效」的测试。
"""

import io
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sheepdog import cli, hook, pending as pend
from sheepdog.config import ConfigError, load_config
from sheepdog.engine import BUS_TOPIC_ID, PRIORITY_PREFIX, Collector

from sheepdog.source.lark import parse_message

from test_owner_priority import PriorityBase
from test_pending import PendingBase, at
from test_roster import ME
from test_stop_hook import HookBase, stop_input
from test_threads import ALICE, ESC_CHAT, _opt, sent_text


def reload_into(base, extra: str, col: Collector | None = None) -> None:
    """改配置文件，再走一遍常驻进程每轮的重读（cli._reload_config），不重建 Collector / Dispatcher。
    col 给了就是常驻进程手上那个 Collector（重读后它的 cfg 跟着换）；不给就临时建一个。"""
    base.cfg_file.write_text(base.cfg_base + extra, encoding="utf-8")
    col = col or Collector(base.cfg, base.store, base.source, base.roster)
    env = {"SHEEPDOG_CONFIG": str(base.cfg_file), "SHEEPDOG_STATE_DIR": base.tmp.name}
    with mock.patch.dict(os.environ, env):
        new = cli._reload_config(base.d.cfg, SimpleNamespace(dry_run=False), base.store, base.source, col, base.d)
    base.assertIs(base.d.cfg, new)
    new.playbook_dir = base.cfg.playbook_dir   # 测试 playbook（只换占位符文字，不影响开关）
    base.cfg = new


class FollowupSwitchTest(PendingBase):
    def scene(self):
        self.alice_batch()
        q = self.asked()
        self.ack("tp_alpha")
        self.owner_says("这事我办了")
        return q

    def test_on_marks_and_reminds(self):
        q = self.scene()
        text = self.deliver()
        self.assertTrue(self.store.get_escalation(q)["possibly_handled_at"])
        self.assertIn("🔎 你的「需要你定」可能已经处理了", text)
        self.assertIn("↳ 你 ", self.cli("pending")[1])

    def test_off_no_marks_no_reminders_but_sources_resolve_and_pending_stay(self):
        self.cfg.escalation.followup = False
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8").replace(
            "[owner_context]", "followup = false\n[owner_context]"), encoding="utf-8")
        q = self.scene()
        self.store.conn.execute("UPDATE escalations SET asked_at=? WHERE message_id=?",
                                ((datetime.now().astimezone() - timedelta(hours=23)).isoformat(timespec="seconds"), q))
        self.store.conn.commit()
        for _ in range(2):
            self.deliver()
        e = self.store.get_escalation(q)
        self.assertEqual((e["possibly_handled_at"], e["expiry_reminded_at"]), (None, None))
        self.assertEqual(self.system_msgs("escalation_check") + self.system_msgs("escalation_expiring"), [])
        self.assertEqual(len(self.store.escalation_sources(q)), 1)        # 来源照记
        rc, out, _ = self.cli("pending")
        self.assertEqual(rc, 0)
        self.assertIn("「上线时间」", out)                                 # 清单照常
        self.assertNotIn("↳", out)                                        # 不写「已提醒 / 会提醒」
        self.assertEqual(self.resolve("--message-id", q, "--why", "办完了")[0], 0)   # resolve 照常

    def test_off_closed_session_not_forwarded_to_bus(self):
        self.cfg.escalation.followup = False
        q = self.scene()
        self.store.update_topic("tp_alpha", state="closed")
        pend.followup_round(self.cfg, self.store, self.d.playbook)
        self.assertEqual(self.system_msgs("escalation_check", BUS_TOPIC_ID), [])
        self.assertIsNone(self.store.get_escalation(q)["possibly_handled_at"])

    def group_thread_reply(self) -> tuple[str, str]:
        """群里的来源消息（进账本时还没有话题），ask --about 它；主人后来在它下面开话题回复。返回 (来源, 主人那条)。"""
        src = self.lark.add(chat_id="oc_test_alpha_grp", sender_id=ALICE, content="群里：周五能上线吗？",
                            chat_type="group", at=at(-10))
        self.pull(src)
        self.asked("--about", src)
        m = self.lark.add(chat_id="oc_test_alpha_grp", sender_id=ME, content="话题里：下周一", thread_root=src, at=at(2))
        return src, m

    def mgets(self) -> list:
        return [c for c in self.lark.calls if c[1] == "+messages-mget"]

    def test_on_source_thread_learned_by_mget(self):
        src, m = self.group_thread_reply()
        self.pull(m)
        self.assertEqual([_opt(c, "--message-ids") for c in self.mgets()], [src])

    def test_off_no_mget_for_source_thread(self):
        # 跟进关掉：不判可能已处理，也就不为认「在来源消息的话题里说的」去 mget
        self.cfg.escalation.followup = False
        src, m = self.group_thread_reply()
        self.pull(m)
        self.assertEqual(self.mgets(), [])
        self.assertFalse(self.row(src)["thread_id"])

    def test_hot_reload_turns_it_off_and_on_without_restart(self):
        # 常驻进程每轮的重读：Dispatcher 不重建，改配置文件下一轮就生效
        base = self.cfg_file.read_text(encoding="utf-8")
        self.cfg_base = ""
        reload_into(self, base.replace("[owner_context]", "followup = false\n[owner_context]"))
        self.assertFalse(self.d.cfg.escalation.followup)
        q = self.scene()
        self.deliver()
        self.assertIsNone(self.store.get_escalation(q)["possibly_handled_at"])
        reload_into(self, base)
        self.assertTrue(self.d.cfg.escalation.followup)
        text = self.deliver()
        self.assertTrue(self.store.get_escalation(q)["possibly_handled_at"])
        self.assertIn("🔎 你的「需要你定」可能已经处理了", text)


class OwnerShortcutSwitchTest(PendingBase):
    def daiban(self) -> str:
        mid = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="待办", at=at(5))
        self.pull(mid)
        return mid

    def test_on_bot_replies_list(self):
        mid = self.daiban()
        self.assertEqual(self.row(mid)["reason"], "pending_query")
        self.assertTrue(sent_text(self.lark.sent("+messages-reply")[-1]).startswith("📋 待办\n"))

    def test_off_is_a_normal_owner_reply(self):
        self.cfg.escalation.owner_shortcut = False
        mid = self.daiban()
        r = self.row(mid)
        # 关掉快捷问法：「待办」就是一句普通的话（7.53 起没引用的话是指令，投总线）
        self.assertEqual((r["route"], r["reason"], r["topic_id"]), ("dispatch", "owner_instruction", BUS_TOPIC_ID))
        self.assertEqual(self.lark.sent("+messages-reply"), [])

    def test_hot_reload_reaches_the_resident_collector(self):
        # 常驻进程手上那个 Collector 不重建：重读配置后它的 cfg 跟着换，「待办」拦不拦下一轮就变
        col = Collector(self.cfg, self.store, self.source, self.roster)
        base = self.cfg_file.read_text(encoding="utf-8")
        self.cfg_base = ""

        def daiban(i: int) -> str:
            mid = self.lark.add(chat_id=ESC_CHAT, sender_id=ME, content="待办", at=at(i))
            col.ingest([parse_message(self.lark.raw(mid))])
            return mid

        reload_into(self, base.replace("[owner_context]", "owner_shortcut = false\n[owner_context]"), col)
        self.assertIs(col.cfg, self.d.cfg)
        self.assertEqual(self.row(daiban(5))["reason"], "owner_instruction")
        reload_into(self, base, col)
        self.assertEqual(self.row(daiban(6))["reason"], "pending_query")


class OwnerPrioritySwitchTest(PriorityBase):
    def setUp(self):
        super().setUp()
        self.cfg_base = self.cfg_file.read_text(encoding="utf-8")

    def priority_batches(self) -> list[str]:
        return [d["batch_id"] for d in self.store.recent_dispatches("tp_alpha") if d["batch_id"].startswith(PRIORITY_PREFIX)]

    def test_on_owner_reply_jumps_the_queue(self):
        self.owner_reply()
        self.d.dispatch_once()
        self.assertEqual(len(self.priority_batches()), 1)

    def test_off_owner_reply_waits_for_the_receipt(self):
        self.cfg.session.owner_priority = False
        mid = self.owner_reply()
        n = len(self.alpha_texts())
        self.d.dispatch_once()
        self.assertEqual((self.priority_batches(), len(self.alpha_texts())), ([], n))
        self.assertEqual(self.row(mid)["dispatch_state"], "pending")      # 照旧排队
        self.ack("tp_alpha")
        self.d.dispatch_once()                                             # 交了回执，随下一批送到
        self.assertIn("周五发", self.alpha_texts()[-1])
        self.assertEqual(self.priority_batches(), [])

    def test_hot_reload_turns_it_off_and_on_without_restart(self):
        # 常驻进程每轮的重读：改配置文件就生效，Collector / Dispatcher 不重建
        reload_into(self, "[session]\nowner_priority = false\n")
        self.assertFalse(self.d.cfg.session.owner_priority)
        self.owner_reply()
        self.d.dispatch_once()
        self.assertEqual(self.priority_batches(), [])
        reload_into(self, "[session]\nowner_priority = true\n")
        self.d.dispatch_once()
        self.assertEqual(len(self.priority_batches()), 1)


class StopHookSwitchTest(HookBase):
    def test_on_blocks(self):
        self.assertEqual(json.loads(self.run_main()[1])["decision"], "continue")

    def test_off_outputs_nothing(self):
        self.cfg.hooks.stop = False
        self.assertEqual(self.run_main(), (0, ""))

    def test_each_call_rereads_the_config_file(self):
        # hook 每次调用都现读配置：同一个进程里改配置文件，下一次调用就生效
        cfg_file = self.dir / "config.toml"
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": str(self.cfg.state_dir),
               "XDG_CONFIG_HOME": str(self.dir / "xdg")}

        def call(stop: str, text: str = "") -> str:
            cfg_file.write_text(text or f'self_open_id = "{ME}"\n[hooks]\nstop = {stop}\n', encoding="utf-8")
            out = io.StringIO()
            with mock.patch.dict(os.environ, env):
                hook.stop_main(io.BytesIO(stop_input().encode("utf-8")), out, None, self.now)
            return out.getvalue()

        self.assertEqual(call("false"), "")
        self.assertEqual(json.loads(call("true"))["decision"], "continue")
        self.assertEqual(call("false"), "")
        self.assertEqual(call('"no"'), "")          # 写坏了（不是 true/false）：读配置报错，兜成不输出
        self.assertEqual(call("", f'self_open_id = "{ME}"\n[hooks\nstop = true\n'), "")   # 语法错：同样不输出
        self.assertEqual(json.loads(call("true"))["decision"], "continue")             # 改好了下一次照常

    def test_bad_value_rejected_by_config(self):
        p = self.dir / "bad.toml"
        for section, key in (("hooks", "stop"), ("session", "owner_priority"), ("escalation", "followup"),
                             ("escalation", "owner_shortcut")):
            p.write_text(f"[{section}]\n{key} = \"yes\"\n", encoding="utf-8")
            with self.assertRaises(ConfigError, msg=key):
                load_config(p)
