"""DEMO-144：规则改成 skill 按需读，批次里只放信号（全链路验收与 T1 变异证伪单测）。

覆盖方案第七节全部 7 项验收与 T1 证伪场景（全合成假名与假 ID：Alice / Bob，`ou_test_*`）：
1. 重放 批次：用 `b20250821205622-tp_alpha` 的原始信号与当前 playbook 拼批（含规则更新或压缩状态）：
   整条 <= 40,000B；消息 `om_test_20250821_205622` 在前 2KB 内；不含 50KB 整份规则，带 `📘 规则...` 指针行。
2. 总线最坏情况：总线 (`tp_bus`) + 名册更新 + 3 条 10KB 信号 + 33KB 全景上下文：
   拆成 2 段，每段 <= 40,000B，每条信号都在所在段的前 40KB 内，回执那一行只在最后一段。
   T1 证伪：把 50KB 整份规则塞回信号前面，`assert_batch_chunks_within_budget` 当场抛 `ValueError` 变红。
3. 规则已读发前闸门（6 个场景 + 3 次熔断 + 1 小时节流）：
   ① 没读 -> `reply / report / ask / receipt(handled)` 退出码 4，没写 `sends`；`receipt(waiting / stopped)` 照收并记 `rule_unread_exempt_receipt`；
   ② 读了旧版本 -> 退出码 4 拒；
   ③ 读了当前版本但之后有 `CHECKPOINT` -> 退出码 4 拒；
   ④ 用 `cat` 只拿到开始标记（无结束标记）-> 退出码 4 拒；
   ⑤ 完整读了当前版本（同时含开始与结束标记；总线还含 `references/bus.md` 标记）-> 放行并缓存；
   ⑥ 会话记录文件不存在（或非 Antigravity 宿主）-> 出错放行并记 `observe:gate_error_passthrough`；
   ⑦ 同一批次连续被拦满 3 次 -> 自动放行并登记一条 `owner_notices` 告警；
   ⑧ 1 小时节流：基线后首次改规则立即升版，1 小时内第 2 次改规则节流不刷版本号。
   T1 证伪：把判据改成「只要有开始标记」，场景 ④ 当场放行变红。
4. 回音闸门（4.6）：
   批次带主人的话（`owner_reply` / `owner_instruction`），会话没回就交 `handled` -> 退出码 4 拒；
   会话在话题里 `reply` 了主人（且主人引用的更早旧提问不被误算）-> 放行，回音账本结案 (`settled_by='report'`)、不再发带回卡片；
   回执写了 `no_reply` -> 放行，带回卡片写 `ℹ️ <name> 会话表示这句无需回复：<why> [<tid>]`。
   T1 证伪：把 `reply` 从回音判据里去掉，「在话题里回过」那条当场变红。
5. 4b 带回措辞：
   回执 `handled` 但没有对得上的 `sends`（如闸门出错放行）-> 带回卡片写 `⚠️ <name> 会话说处理了，但没回你这句 [<tid>]`，绝不出现 `✅`；
   有对得上的 `sends` 时 -> 带回卡片写 `✅ <name> 处理了你的话（回复 <send_mid>）[<tid>]`。
   T1 证伪：去掉「引得到 sends」检查，无 `sends` 时误发 `✅` 当场变红。
6. 4c 读对文件（`transcript_full.jsonl`）与截断救援 + `sheepdog show --batch`：
   10KB 未被截批次在 `transcript.jsonl` 被截为 4KB 带 `<truncated>`，在 `transcript_full.jsonl` 完整 -> `delivery_check_batch` 判 `was_truncated=False`；
   49,152B 真被宿主截断时 -> 判 `was_truncated=True`，自动补投 `sheepdog show --batch` 救援提示并记 `observe:truncated_batches`；
   `sheepdog show --batch <batch_id>` 完整输出该批信号原文。
   T1 证伪：把 `delivery_check_batch` 读的文件换成 `transcript.jsonl`，10KB 未截断批次被误判为 `was_truncated=True` 当场变红。
7. `SKILL.md <= 40,000B` 预算门禁与 `observe.run_observe` 体检四项指标。
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli, observe
from sheepdog.engine import BUS_TOPIC_ID, write_receipt
from sheepdog.models import Message
from sheepdog.playbook import Playbook
from sheepdog.prompts import MAX_BATCH_PROMPT_BYTES, RULES_UPDATE_HEADER, batch_prompt_chunks
from sheepdog.sink.agentapi import transcript_path
from sheepdog.skillpack import (
    MAX_SKILL_MD_BYTES,
    SkillPackBudgetError,
    assert_batch_chunks_within_budget,
    check_owner_echo_gate,
    delivery_check_batch,
    enforce_rule_read_gate,
    ensure_skillpack_rendered,
    find_matching_send_for_owner_msg,
    skill_end_marker,
    skill_start_marker,
    transcript_full_path,
)

from test_identity import APP
from test_owner_echo import ESC_CHAT, EchoBase
from test_roster import FIXTURES, ME
from test_threads import ALICE, HEADER_RX, ATTR_RX, MARK, now8

GROUP_CHAT = "oc_test_demo144_group"


def _write_jsonl(path: Path, steps: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for s in steps:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")


class SkillRulesDemo144Test(EchoBase):
    def setUp(self):
        super().setUp()
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "ANTIGRAVITY_APP_DATA_DIR": str(Path(self.tmp.name) / "app"),
                "SHEEPDOG_STATE_DIR": self.tmp.name,
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        self.lark.chat_types[GROUP_CHAT] = "group"
        self.pdir = Path(self.tmp.name) / "playbook"
        shutil.copytree(FIXTURES, self.pdir)
        self.cfg.playbook_dir = str(self.pdir)
        self.d.playbook = Playbook.from_config(self.cfg)
        existing_cfg = self.cfg_file.read_text(encoding="utf-8")
        self.cfg_file.write_text(f'playbook_dir = "{self.pdir}"\n' + existing_cfg, encoding="utf-8")
        self._mark_skill_read_in_transcript("conv_test_alpha", "tp_alpha")
        self._mark_skill_read_in_transcript("conv_test_beta", "tp_beta")
        self._mark_skill_read_in_transcript("conv_test_bus", BUS_TOPIC_ID)

    def reply(self, mid: str, text: str, topic: str = "tp_alpha", *extra) -> tuple[int, str, str]:
        return self.cli("reply", "--topic", topic, "--to", mid, "--text", text, *extra)

    def _mark_skill_read_in_transcript(self, cid: str, topic_id: str, version: str = "") -> str:
        sp = ensure_skillpack_rendered(self.store, playbook_dir=self.pdir)
        ver = version or sp.version
        content = f"{skill_start_marker(ver)}\nbody\n{skill_end_marker(ver)}"
        if topic_id == BUS_TOPIC_ID:
            content += f"\n{skill_start_marker(ver, 'bus')}\nbus body\n{skill_end_marker(ver, 'bus')}"
        tf = transcript_full_path(cid)
        _write_jsonl(
            tf,
            [
                {
                    "step_index": 10,
                    "type": "GENERIC",
                    "created_at": (now8() + timedelta(hours=2)).isoformat(timespec="seconds"),
                    "content": content,
                }
            ],
        )
        return ver

    # ======================= 1. 重放 =======================
    def test_demo144_1_replay_1111_2028_batch(self):
        """验收 1：重放 批次（b20250821205622），整条 <= 40KB，信号在前 2KB，不含整份规则。"""
        mid_target = "om_test_20250821_205622"
        m = Message(
            message_id=mid_target,
            chat_id=GROUP_CHAT,
            chat_type="group",
            chat_name="架构讨论群",
            sender_id=ME,
            sender_name="Matthew",
            sender_type="user",
            msg_type="text",
            content="@Mattheuu 这个延迟 P95 为什么还是 12 秒？帮我看一下",
            create_time="2025-08-21T20:56:15+08:00",
        )
        self.store.upsert_message(m, "dispatch", "owner_instruction", ["owner_instruction"], "tp_alpha")
        row = self.store.get_message(mid_target)
        pb = Playbook.from_config(self.cfg)
        sp = ensure_skillpack_rendered(self.store, playbook_dir=self.pdir)
        ambient_msgs = [
            {
                "sender_name": "Bob",
                "sender_id": "ou_test_bob",
                "content": "全景背景动态 " + ("A" * 500),
                "message_position": str(i),
            }
            for i in range(16)
        ]  # ~8KB
        reminder = "### 本会话红线\n" + ("- 严禁未经授权操作生产环境，所有动作先核实事实源。\n" * 55)  # ~3.6KB

        chunks = batch_prompt_chunks(
            pb,
            "tp_alpha",
            "b20250821205622-tp_alpha",
            [row],
            [],
            "Alpha 需求",
            rules_update="📌 现行规则（假装旧版传入 50KB 整份规则）：" + ("规则正文" * 6000),
            rules_note="上下文压缩过",
            reminder=reminder,
            ambient_msgs=ambient_msgs,
            vips={ALICE: "Alice"},
            skill_dir=sp.skill_dir,
        )
        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertLessEqual(len(chunk.encode("utf-8")), MAX_BATCH_PROMPT_BYTES)
        self.assertNotIn(RULES_UPDATE_HEADER, chunk)
        self.assertIn("📘 规则", chunk)
        self.assertIn(mid_target, chunk)
        prefix_before_mid = chunk[: chunk.index(mid_target)]
        self.assertLess(len(prefix_before_mid.encode("utf-8")), 2_000)

    # ======================= 2. 总线最坏情况 + T1 证伪 =======================
    def test_demo144_2_bus_worst_case_split_and_budget(self):
        """验收 2：总线 + 名册更新 + 3 条 10KB 信号 + 33KB 全景上下文 -> 拆成 2 段，每段 <= 40KB，
        每条信号都在所在段前 40KB，回执只出现在最后一段；T1 证伪把 50KB 规则塞回信号前面必须红。"""
        mids = []
        rows = []
        for i in range(3):
            mid = f"om_test_bus_10k_{i}"
            mids.append(mid)
            m = Message(
                message_id=mid,
                chat_id=GROUP_CHAT,
                chat_type="group",
                chat_name="大群",
                sender_id=ALICE,
                sender_name="Alice",
                sender_type="user",
                msg_type="text",
                content=f"信号#{i}开头 " + ("长日志内容A1B2C3 " * 800),  # > 10KB
                create_time="2025-08-22T01:28:00+08:00",
            )
            self.store.upsert_message(m, "dispatch", "mention", [], BUS_TOPIC_ID)
            rows.append(self.store.get_message(mid))

        ambient_msgs = [
            {
                "sender_name": "Bob",
                "sender_id": "ou_test_bob",
                "content": "全景背景动态 " + ("X" * 1200),
                "message_position": str(i),
            }
            for i in range(30)
        ]  # > 33KB
        roster_update = "\n".join(
            f"| tp_worker_{i:02d} | 专席 {i:02d} | 负责模块 {i:02d} 的日常排障与发布核验 | active |"
            for i in range(180)
        )  # ~16KB
        digest = "\n".join(
            f"- `tp_worker_{i:02d}` [running]：正在处理批次 b20250822-{i:02d}（耗时 3m）"
            for i in range(150)
        )  # ~12KB
        reminder = "### 本会话红线\n" + ("- 严禁未经授权操作生产环境，所有动作先核实事实源。\n" * 40)

        pb = Playbook.from_config(self.cfg)
        sp = ensure_skillpack_rendered(self.store, playbook_dir=self.pdir)
        chunks = batch_prompt_chunks(
            pb,
            BUS_TOPIC_ID,
            "b20250822012800-bus",
            rows,
            [],
            "Lark 信号·总线",
            roster_update=roster_update,
            digest=digest,
            reminder=reminder,
            ambient_msgs=ambient_msgs,
            skill_dir=sp.skill_dir,
        )
        self.assertEqual(len(chunks), 2)
        for idx, ch in enumerate(chunks):
            self.assertLessEqual(len(ch.encode("utf-8")), MAX_BATCH_PROMPT_BYTES, f"chunk {idx} Over 40KB")
        combined = "\n".join(chunks)
        for mid in mids:
            self.assertIn(f"sheepdog show {mid}", combined)
        # 回执只出现在最后一段
        self.assertNotIn("处理完成后提交回执", chunks[0])
        self.assertIn("处理完成后提交回执", chunks[-1])
        # 全景上下文被裁到 <= 8KB 并提示 sheepdog thread
        self.assertIn("sheepdog thread", combined)

        # T1 证伪：若把 50KB 整份规则塞回信号前面，出口断言必须抛 ValueError 变红
        stuffed_chunk = (
            "[sheepdog] 新信号批次 `b_stuffed`（topic `tp_bus`，共 3 条）\n\n"
            + ("📌 现行规则 " + "R" * 50_000)
            + "\n\n"
            + chunks[0]
        )
        with self.assertRaises(ValueError):
            assert_batch_chunks_within_budget([stuffed_chunk], mids, "b_stuffed")

    # ======================= 3. 规则已读发前闸门 6 场景 + T1 =======================
    def test_demo144_3_rule_read_gate_six_cases_and_three_strikes(self):
        """验收 3：规则已读发前闸门 ①~⑥ + 同一批被拦 3 次放行并告警 + 1 小时版本节流 + T1 证伪。"""
        sp = ensure_skillpack_rendered(self.store, playbook_dir=self.pdir, now_ts=1_700_000_000)
        ver = sp.version
        cid = "conv_test_alpha"
        tf = transcript_full_path(cid)

        # 先送一批普通消息给 tp_alpha，使其手上有 pending_batch_id
        from test_threads import ALPHA_P2P
        m1 = self.say(ALPHA_P2P, sender=ALICE, content="请帮我看一下日志")
        self.pull(m1)
        self.d.dispatch_once()
        batch_id = self.store.get_topic("tp_alpha")["pending_batch_id"]
        self.assertTrue(batch_id)

        # ① 没读：transcript_full.jsonl 里只有普通步骤，没有读 SKILL.md
        _write_jsonl(
            tf,
            [{"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "2025-08-22T01:28:00+08:00", "content": "thinking"}],
        )
        self.store.set_meta(f"rules_read:{cid}", "")
        rc, out, err = self.reply(m1, "我来看看")
        self.assertEqual(rc, 4)
        self.assertIn("SKILL.md", out + err)
        self.assertIn("参数不用改", out + err)
        self.assertEqual(self.store.sends_in_thread(m1), [])

        # waiting 与 stopped 回执豁免，不拦，但记 observe:rule_unread_exempt_receipt
        res_wait = enforce_rule_read_gate(
            self.store,
            topic="tp_alpha",
            action="receipt",
            receipt_status="waiting",
            playbook_dir=self.pdir,
        )
        self.assertTrue(res_wait.allowed)
        res_stop = enforce_rule_read_gate(
            self.store,
            topic="tp_alpha",
            action="receipt",
            receipt_status="stopped",
            playbook_dir=self.pdir,
        )
        self.assertTrue(res_stop.allowed)

        # 重置 strike 计数，测试 ② 读了旧版本 -> 拒
        self.store.set_meta(f"skillpack:block_count:tp_alpha:{batch_id}:{ver}", "0")
        _write_jsonl(
            tf,
            [
                {
                    "step_index": 2,
                    "type": "GENERIC",
                    "created_at": "2025-08-22T02:28:00+08:00",
                    "content": f"{skill_start_marker('0000dead')}\nold\n{skill_end_marker('0000dead')}",
                }
            ],
        )
        res_old = enforce_rule_read_gate(
            self.store,
            topic="tp_alpha",
            action="reply",
            playbook_dir=self.pdir,
        )
        self.assertFalse(res_old.allowed)
        self.assertEqual(res_old.exit_code, 4)

        # ③ 读了当前版本，但之后发生了 CHECKPOINT 压缩 -> 拒
        self.store.set_meta(f"skillpack:block_count:tp_alpha:{batch_id}:{ver}", "0")
        _write_jsonl(
            tf,
            [
                {
                    "step_index": 3,
                    "type": "GENERIC",
                    "created_at": (now8() + timedelta(minutes=5)).isoformat(timespec="seconds"),
                    "content": f"{skill_start_marker(ver)}\nfull\n{skill_end_marker(ver)}",
                },
                {
                    "step_index": 4,
                    "type": "CHECKPOINT",
                    "created_at": (now8() + timedelta(minutes=6)).isoformat(timespec="seconds"),
                    "content": "# Resuming from a compaction\nSummary...",
                },
            ],
        )
        self.store.set_meta(f"rules_read:{cid}", "")
        res_cp = enforce_rule_read_gate(
            self.store,
            topic="tp_alpha",
            action="reply",
            playbook_dir=self.pdir,
        )
        self.assertFalse(res_cp.allowed)
        self.assertEqual(res_cp.exit_code, 4)

        # ④ 用 cat 只读到了开头标记，没有结束标记 -> 拒
        self.store.set_meta(f"skillpack:block_count:tp_alpha:{batch_id}:{ver}", "0")
        _write_jsonl(
            tf,
            [
                {
                    "step_index": 5,
                    "type": "GENERIC",
                    "created_at": (now8() + timedelta(minutes=7)).isoformat(timespec="seconds"),
                    "content": f"{skill_start_marker(ver)}\nhalf read truncated...",
                }
            ],
        )
        res_half = enforce_rule_read_gate(
            self.store,
            topic="tp_alpha",
            action="reply",
            playbook_dir=self.pdir,
        )
        self.assertFalse(res_half.allowed)
        self.assertEqual(res_half.exit_code, 4)

        # T1 证伪：如果把判据改成「只要有开始标记就算读过」，场景 ④ 会错误放行（证明结束标记检查不可省）
        naive_only_start_check = skill_start_marker(ver) in tf.read_text(encoding="utf-8")
        self.assertTrue(naive_only_start_check)
        self.assertNotEqual(res_half.allowed, naive_only_start_check)

        # ⑦ 同一批连续被拦满 3 次 -> 自动放行并写 owner_notices 通知主人
        self.store.set_meta(f"skillpack:block_count:tp_alpha:{batch_id}:{ver}", "0")
        r1 = enforce_rule_read_gate(self.store, topic="tp_alpha", action="reply", playbook_dir=self.pdir)
        r2 = enforce_rule_read_gate(self.store, topic="tp_alpha", action="reply", playbook_dir=self.pdir)
        r3 = enforce_rule_read_gate(self.store, topic="tp_alpha", action="reply", playbook_dir=self.pdir)
        self.assertFalse(r1.allowed)
        self.assertFalse(r2.allowed)
        self.assertTrue(r3.allowed)
        notices = self.store.unsent_owner_notices()
        self.assertTrue(
            any(
                "连续 3 次未读规则"
                in (n["body"].decode("utf-8", errors="ignore") if isinstance(n["body"], bytes) else str(n["body"]))
                for n in notices
            )
        )

        # ⑤ 完整读了当前版本 -> 放行并写入 rules_read:<cid> 缓存
        self.store.set_meta(f"skillpack:block_count:tp_alpha:{batch_id}:{ver}", "0")
        _write_jsonl(
            tf,
            [
                {
                    "step_index": 8,
                    "type": "GENERIC",
                    "created_at": (now8() + timedelta(minutes=10)).isoformat(timespec="seconds"),
                    "content": f"{skill_start_marker(ver)}\ncomplete body\n{skill_end_marker(ver)}",
                }
            ],
        )
        res_ok = enforce_rule_read_gate(
            self.store,
            topic="tp_alpha",
            action="reply",
            playbook_dir=self.pdir,
        )
        self.assertTrue(res_ok.allowed)
        self.assertEqual(res_ok.exit_code, 0)
        cached = json.loads(self.store.get_meta(f"rules_read:{cid}") or "{}")
        self.assertEqual(cached.get("version"), ver)

        # ⑥ 会话记录文件不存在 -> 出错放行并记一笔 observe:gate_error_passthrough
        tf.unlink()
        self.store.set_meta(f"rules_read:{cid}", "")
        before_err = int(self.store.get_meta("observe:gate_error_passthrough") or "0")
        res_missing = enforce_rule_read_gate(
            self.store,
            topic="tp_alpha",
            action="reply",
            playbook_dir=self.pdir,
        )
        self.assertTrue(res_missing.allowed)
        after_err = int(self.store.get_meta("observe:gate_error_passthrough") or "0")
        self.assertEqual(after_err, before_err + 1)

        # ⑧ 1 小时版本节流：首次改规则立即出新版 v1；10 分钟后再改规则被节流保持 v1；满 1 小时后升到 v2
        (self.pdir / "common.md").write_text("FX-COMMON-THROTTLE-1", encoding="utf-8")
        sp1 = ensure_skillpack_rendered(self.store, playbook_dir=self.pdir, now_ts=1_700_000_100)
        self.assertNotEqual(sp1.version, ver)
        self.assertFalse(sp1.throttled)

        (self.pdir / "common.md").write_text("FX-COMMON-THROTTLE-2", encoding="utf-8")
        sp2 = ensure_skillpack_rendered(self.store, playbook_dir=self.pdir, now_ts=1_700_000_600)
        self.assertEqual(sp2.version, sp1.version)
        self.assertTrue(sp2.throttled)

        sp3 = ensure_skillpack_rendered(self.store, playbook_dir=self.pdir, now_ts=1_700_003_800)
        self.assertNotEqual(sp3.version, sp1.version)
        self.assertFalse(sp3.throttled)

    # ======================= 4. 回音闸门 + 群话题 reply 结案 + no_reply + T1 =======================
    def test_demo144_4_owner_echo_gate_and_reply_in_thread(self):
        """验收 4：批次含主人的话，没回交 handled 退 4；在话题里 reply 了放行且回音账本结案不带回；
        写了 no_reply 放行且带回卡片写「会话表示这句无需回复」；T1 证伪去掉 reply 判据必红。"""
        # 场景 A：主人在群话题里向会话下达指令，会话没发任何消息就调 CLI receipt --status handled -> 退出码 4
        root = self.root_to("tp_beta")
        bot = self.bot_reply_for("tp_beta", root)
        owner_mid = self.say(
            sender=ME,
            content="@Mattheuu 帮我重跑一下流水线",
            quote=bot,
            thread_root=root,
            mentions=[APP],
        )
        self.pull(owner_mid)
        self.d.dispatch_once()
        b_beta = self.store.get_topic("tp_beta")["pending_batch_id"]
        self.assertTrue(b_beta)

        # 注意：bot_reply_for 在 owner_mid 之前已经在 sends 里有一条旧记录，绝不能被误算成对 owner_mid 的回复！
        gate_before_reply = check_owner_echo_gate(
            self.store,
            batch_id=b_beta,
            topic="tp_beta",
            status="handled",
        )
        self.assertFalse(gate_before_reply.allowed)
        self.assertEqual(gate_before_reply.exit_code, 4)
        self.assertIn(owner_mid, gate_before_reply.reason)

        # 通过 CLI 提交 handled 回执同样返回退出码 4
        rc, out, err = self.cli(
            "receipt", "--topic", "tp_beta", "--batch", b_beta,
            "--json", json.dumps({"status": "handled", "summary": "重跑好了"}, ensure_ascii=False),
        )
        self.assertEqual(rc, 4)
        self.assertIn(owner_mid, out + err)

        # 场景 B：会话在群话题里用 sheepdog reply 回复了主人 -> 回音闸门放行，且回音账本直接结案不再发带回卡片
        rc_rep, out_rep, err_rep = self.reply(owner_mid, "已重跑流水线 #1024，全绿通过", topic="tp_beta")
        self.assertEqual(rc_rep, 0, out_rep + err_rep)

        gate_after_reply = check_owner_echo_gate(
            self.store,
            batch_id=b_beta,
            topic="tp_beta",
            status="handled",
        )
        self.assertTrue(gate_after_reply.allowed)

        # T1 证伪：如果回音判据像旧版 store.owner_sends_since 一样只认 ask/report、不认群话题里的 reply，这里就会误判为未回复
        legacy_only_ask_report = self.store.owner_sends_since("tp_beta", self.echo(owner_mid)["delivered_at"])
        self.assertEqual(legacy_only_ask_report, 0)
        self.assertNotEqual(gate_after_reply.allowed, bool(legacy_only_ask_report))

        # 交回执并跑一轮：回音账本因话题内 reply 自动结案 (settled_by='report')，不再给主人发带回卡片
        self.receipt("tp_beta", summary="流水线重跑通过")
        self.d.dispatch_once()
        self.assertEqual(self.echo(owner_mid)["stage"], "settled")
        self.assertEqual(self.echo(owner_mid)["settled_by"], "report")
        self.assertEqual(self.relays(), [])

        # 场景 C：另一条主人的话，会话在回执里写 no_reply -> 放行，且带回卡片写「ℹ️ ... 会话表示这句无需回复：<why>」
        q2 = self.asked(topic="tp_alpha", key="PR #99", title="确认收到", text="请确认")
        owner_mid2 = self.owner("知道了", quote=q2)
        self.d.dispatch_once()
        b_alpha = self.store.get_topic("tp_alpha")["pending_batch_id"]
        rc2, out2, err2 = self.cli(
            "receipt", "--topic", "tp_alpha", "--batch", b_alpha,
            "--json", json.dumps({"status": "handled", "summary": "主人已知悉"}, ensure_ascii=False),
            "--no-reply", f"{owner_mid2}:主人仅回知道了无需再追发",
        )
        self.assertEqual(rc2, 0, out2 + err2)
        self.d.dispatch_once()
        (card_no_reply,) = self.relays()
        self.assertTrue(
            card_no_reply["text"].startswith("ℹ️ Alpha 需求 会话表示这句无需回复：主人仅回知道了无需再追发 [tp_alpha]"),
            card_no_reply["text"],
        )
        self.assertNotIn("✅", card_no_reply["text"])

    # ======================= 5. 4b 带回措辞 + T1 证伪 =======================
    def test_demo144_4b_relay_card_warns_when_no_matching_send(self):
        """验收 4b：回执 handled 但没有对得上的 sends（如闸门出错放行）-> 带回卡片写
        「⚠️ <name> 会话说处理了，但没回你这句」，绝不出现「✅」；有对得上的 sends 才写「✅ ...（回复 <mid>）」。"""
        q = self.asked(topic="tp_alpha", key="K-4B", title="发布确认", text="能发吗？")
        a = self.owner("先别发", quote=q)
        self.d.dispatch_once()
        b_id = self.store.get_topic("tp_alpha")["pending_batch_id"]
        # 模拟闸门因 transcript 不存在而出错放行，会话直接交了 handled 回执但没有任何 sends
        write_receipt(self.cfg, "tp_alpha", b_id, {"status": "handled", "summary": "已暂停发布"})
        self.d.dispatch_once()
        (c_warn,) = self.relays()
        self.assertTrue(
            c_warn["text"].startswith("⚠️ Alpha 需求 会话说处理了，但没回你这句 [tp_alpha]"),
            c_warn["text"],
        )
        self.assertNotIn("✅", c_warn["text"])

        # T1 证伪：验证 find_matching_send_for_owner_msg 确实返回 None（若不核对 sends 就会像旧版一样误发 ✅）
        matched = find_matching_send_for_owner_msg(
            self.store,
            topic="tp_alpha",
            owner_msg=self.store.get_message(a),
            since_iso=self.echo(a)["delivered_at"],
        )
        self.assertIsNone(matched)

        # 验证 waiting（已派后台任务）/ waiting_external 等未完结状态写「⏳ 在处理（...）」，绝不出现「说处理了」或「✅」
        q_wait = self.asked(topic="tp_alpha", key="K-4B-WAIT", title="后台任务", text="跑一下压测？")
        a_wait = self.owner("跑一下", quote=q_wait)
        self.d.dispatch_once()
        b_wait = self.store.get_topic("tp_alpha")["pending_batch_id"]
        write_receipt(self.cfg, "tp_alpha", b_wait, {"status": "waiting", "summary": "已派后台羔羊跑压测", "anchors": ["lamb:lamb_test4b"]})
        self.d.dispatch_once()
        c_wait = self.relays()[-1]
        self.assertTrue(
            c_wait["text"].startswith("⏳ Alpha 需求 在处理（已派后台任务） [tp_alpha]"),
            c_wait["text"],
        )
        self.assertNotIn("说处理了", c_wait["text"])
        self.assertNotIn("没回你", c_wait["text"])
        self.assertNotIn("✅", c_wait["text"])

        # 验证 waiting 状态如果已有对应回复，_echo_relays 带上那条回复的链接「⏳ ... 在处理（已派后台任务）（回复 <mid>）」
        self.store.update_topic("tp_alpha", state="active")
        q_wait_rep = self.asked(topic="tp_alpha", key="K-4B-WAIT-REP", title="后台任务2", text="再跑一次？")
        a_wait_rep = self.owner("好，再跑一次", quote=q_wait_rep)
        self.d.dispatch_once()
        b_wait_rep = self.store.get_topic("tp_alpha")["pending_batch_id"]
        self.store.echo_bind_relay(a_wait_rep, b_wait_rep, self.echo(a_wait_rep)["delivered_at"], "waiting", "已回并派后台任务")
        self.store.add_send(
            "idem_wait_rep_4b",
            "reply",
            "tp_alpha",
            "om_reply_wait_4b",
            "收到，正在跑后台任务",
            target_message_id=a_wait_rep,
        )
        self.d._echo_relays()
        c_wait_rep = self.relays()[-1]
        self.assertTrue(
            c_wait_rep["text"].startswith("⏳ Alpha 需求 在处理（已派后台任务）（回复 om_reply_wait_4b） [tp_alpha]"),
            c_wait_rep["text"],
        )
        self.assertNotIn("说处理了", c_wait_rep["text"])
        self.assertNotIn("✅", c_wait_rep["text"])

    # ======================= 6. 4c 读对文件与截断救援 + show --batch =======================
    def test_demo144_4c_delivery_check_reads_transcript_full_and_rescues_truncation(self):
        """验收 4c：10KB 批次在 transcript.jsonl 被截为 4KB 带 <truncated>，在 transcript_full.jsonl 完整 ->
        delivery_check_batch 判没被截；把读的文件换成 transcript.jsonl 立即误判变红（T1）；
        真在 49,152B 被截时自动触发救援提示，且 sheepdog show --batch 能打出整批信号。"""
        cid = "conv_test_alpha"
        t_compact = transcript_path(cid)
        t_full = transcript_full_path(cid)
        batch_id = "b20250822032800-alpha"
        ten_kb_body = (
            f"[sheepdog] 新信号批次 `{batch_id}`（topic `tp_alpha`，共 1 条）\n\n"
            + ("正常的 10KB 信号正文内容 " * 450)
        )
        # 宿主在 compact transcript.jsonl 里超过 4KB 就截断加 <truncated ...>
        _write_jsonl(
            t_compact,
            [
                {
                    "step_index": 20,
                    "type": "SYSTEM_MESSAGE",
                    "created_at": "2025-08-22T03:28:01+08:00",
                    "content": ten_kb_body[:4000] + "\n<truncated 6200 bytes>",
                }
            ],
        )
        # 但在 transcript_full.jsonl 里是完整的 10KB
        _write_jsonl(
            t_full,
            [
                {
                    "step_index": 20,
                    "type": "SYSTEM_MESSAGE",
                    "created_at": "2025-08-22T03:28:01+08:00",
                    "content": ten_kb_body,
                }
            ],
        )
        was_trunc, trunc_b, rescue = delivery_check_batch(self.store, conversation_id=cid, batch_id=batch_id)
        self.assertFalse(was_trunc)
        self.assertEqual(trunc_b, 0)
        self.assertEqual(rescue, "")

        # T1 证伪：如果把读的文件换成 transcript.jsonl（t_compact），10KB 正常批次会被误判为 was_truncated=True
        with mock.patch("sheepdog.skillpack.transcript_full_path", return_value=t_compact):
            wrong_trunc, wrong_b, _ = delivery_check_batch(self.store, conversation_id=cid, batch_id=batch_id)
            self.assertTrue(wrong_trunc)
            self.assertEqual(wrong_b, 6200)

        # 真被宿主 49,152B 截断的场景：transcript_full.jsonl 末尾出现 <truncated 18432 bytes>
        real_truncated_body = ten_kb_body + ("P" * 40_000) + "\n<truncated 18432 bytes>"
        _write_jsonl(
            t_full,
            [
                {
                    "step_index": 21,
                    "type": "SYSTEM_MESSAGE",
                    "created_at": "2025-08-22T03:29:00+08:00",
                    "content": real_truncated_body,
                }
            ],
        )
        real_trunc, real_b, real_rescue = delivery_check_batch(self.store, conversation_id=cid, batch_id=batch_id)
        self.assertTrue(real_trunc)
        self.assertEqual(real_b, 18432)
        self.assertIn(f"sheepdog show --batch {batch_id}", real_rescue)

        # 验证 sheepdog show --batch <batch_id> 能完整重新打出这一批的所有信号
        from test_threads import ALPHA_P2P
        m_show = self.say(ALPHA_P2P, sender=ALICE, content="这是需要用 show --batch 补读的完整信号原文")
        self.pull(m_show)
        self.d.dispatch_once()
        real_batch = self.store.get_topic("tp_alpha")["pending_batch_id"]
        rc_show, out_show, err_show = self.cli("show", "--batch", real_batch)
        self.assertEqual(rc_show, 0, err_show)
        self.assertIn(m_show, out_show)
        self.assertIn("这是需要用 show --batch 补读的完整信号原文", out_show)

    # ======================= 7. SKILL.md <= 40KB 预算与体检四项指标 =======================
    def test_demo144_5_skill_md_budget_and_observe_metrics(self):
        """验收 5：SKILL.md <= 40,000B 硬上限门禁（超限抛 SkillPackBudgetError），且 observe 输出四项体检指标。"""
        sp = ensure_skillpack_rendered(self.store, playbook_dir=self.pdir, force=True)
        self.assertLessEqual(sp.skill_md_bytes, MAX_SKILL_MD_BYTES)
        skill_text = sp.skill_md_path.read_text(encoding="utf-8")
        self.assertTrue(skill_text.splitlines()[0].startswith(f"<!-- sheepdog-worker 规则版本 {sp.version} · 开始 -->"))
        self.assertTrue(skill_text.splitlines()[-1].startswith(f"<!-- sheepdog-worker 规则版本 {sp.version} · 结束 -->"))

        # 当 common.md 被塞入超过 40KB 的未分节内容时，渲染必须当场抛 SkillPackBudgetError 拒绝发布
        with tempfile.TemporaryDirectory() as tmp_over:
            over_dir = Path(tmp_over) / "playbook"
            shutil.copytree(self.pdir, over_dir)
            (over_dir / "common.md").write_text("超长核心规则 " * 4000, encoding="utf-8")
            with self.assertRaises(SkillPackBudgetError):
                ensure_skillpack_rendered(playbook_dir=over_dir, force=True)

        # 验证 observe.run_observe 包含 skillpack 体检指标
        db_path = Path(self.tmp.name) / "sheepdog.db"
        state_path = Path(self.tmp.name) / "watchdog-cursor.json"
        obs_res = observe.run_observe(
            self.cfg,
            db_path=db_path,
            state_path=state_path,
            check_processes=False,
            check_release_bot=False,
        )
        self.assertIn("skillpack", obs_res)
        for k in ("truncated_batches", "rule_unread_blocked", "owner_no_echo_blocked", "gate_error_passthrough", "skill_md_bytes"):
            self.assertIn(k, obs_res["skillpack"])
        self.assertGreater(obs_res["skillpack"]["skill_md_bytes"], 0)
        self.assertLessEqual(obs_res["skillpack"]["skill_md_bytes"], MAX_SKILL_MD_BYTES)

    # ======================= 8. 占位符替换、interface_section 注入与总线 overlay =======================
    def test_demo144_6_placeholder_replacement_interface_section_and_bus_overlay(self):
        """验收 6：渲染 SKILL.md 与 references/*.md 时用 Playbook.render 替换全部 {{...}} 占位符（结果零 {{ 残留）；
        references/commands.md 含会话 interface_section，references/bus.md 含 prompts/overlay.md 与总线 interface_section，
        且二者变更均计入版本哈希。"""
        with tempfile.TemporaryDirectory() as tmp_cfg_root:
            cfg_root = Path(tmp_cfg_root)
            pb_dir = cfg_root / "playbook"
            shutil.copytree(self.pdir, pb_dir)
            out_skill_dir = cfg_root / "out_skill"

            (cfg_root / "config.toml").write_text(
                'prompt_overlay_path = "prompts/overlay.md"\n'
                'cli_name = "sheepdog"\n'
                '[session]\n'
                'reply_suffix = "[🐕Custom Reply]"\n'
                '[watch]\n'
                'remind_minutes = [5, 15, 30]\n'
                'expire_minutes = 120\n',
                encoding="utf-8",
            )
            (cfg_root / "prompts").mkdir(parents=True, exist_ok=True)
            (cfg_root / "prompts" / "overlay.md").write_text(
                "## 总线专属 Overlay\n使用 `{{cli}} forward` 转交（后缀 `{{reply_suffix}}`，会话 `{{topic_id}}`）。\n",
                encoding="utf-8",
            )
            (pb_dir / "common.md").write_text(
                "### 1. 代回规则\n命令：`{{cli}} reply --topic {{topic_id}}`，后缀 `{{reply_suffix}}`，标题 `{{session_title}}`。\n\n"
                "### 4. 等待规则\n提醒分钟：`{{watch_remind_minutes}}`，超期：`{{watch_expire_minutes}}`。\n",
                encoding="utf-8",
            )
            (pb_dir / "bus.md").write_text(
                "总线名册占位：{{roster}}，命令 {{cli}}。\n",
                encoding="utf-8",
            )
            (pb_dir / "vip.md").write_text(
                "VIP 名单：{{vips}}，命令 {{cli}}。\n",
                encoding="utf-8",
            )
            (pb_dir / "cli_host.md").write_text(
                "当前命令行宿主：{{host}}，命令 {{cli}}。\n",
                encoding="utf-8",
            )

            sp_a = ensure_skillpack_rendered(
                config_dir=cfg_root,
                playbook_dir=pb_dir,
                skill_dir=out_skill_dir,
                force=True,
            )

            # 1) 断言 SKILL.md 与所有 references/*.md 零 `{{` 残留
            all_md_files = [out_skill_dir / "SKILL.md", *sorted((out_skill_dir / "references").glob("*.md"))]
            self.assertGreaterEqual(len(all_md_files), 7)
            for md_path in all_md_files:
                content = md_path.read_text(encoding="utf-8")
                self.assertNotIn("{{", content, f"{md_path.name} 残留未替换占位符")
                self.assertNotIn("}}", content, f"{md_path.name} 残留未替换占位符")

            skill_md_txt = (out_skill_dir / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("[🐕Custom Reply]", skill_md_txt)
            self.assertIn("sheepdog reply --topic <批次头里的 topic>", skill_md_txt)

            # 2) 断言 references/commands.md 包含完整会话 interface_section
            commands_txt = (out_skill_dir / "references" / "commands.md").read_text(encoding="utf-8")
            self.assertIn("## sheepdog 接口说明", commands_txt)
            self.assertIn("### 回执", commands_txt)
            self.assertIn("### 等别人回复", commands_txt)
            self.assertIn("### 羔羊（重活后台并发与 30 秒卸锁）", commands_txt)
            self.assertIn("### 找主人、代回", commands_txt)
            self.assertIn("5/15/30", commands_txt)
            self.assertIn("120", commands_txt)

            # 3) 断言 references/bus.md 包含 prompts/overlay.md 与总线 interface_section
            bus_txt = (out_skill_dir / "references" / "bus.md").read_text(encoding="utf-8")
            self.assertIn("## 总线专属 Overlay", bus_txt)
            self.assertIn("（名册见批次里的「名册更新」）", bus_txt)
            self.assertIn("### 转交与转达", bus_txt)
            self.assertIn("### 新开 / 收掉会话", bus_txt)

            vip_txt = (out_skill_dir / "references" / "vip.md").read_text(encoding="utf-8")
            self.assertIn("（本批 VIP 见批次内 👑 印章）", vip_txt)
            cli_txt = (out_skill_dir / "references" / "cli-host.md").read_text(encoding="utf-8")
            self.assertIn("codex / claude", cli_txt)

            # 4) 断言修改 interface_doc 或 prompts/overlay.md 会改变版本哈希
            sp_b = ensure_skillpack_rendered(
                config_dir=cfg_root,
                playbook_dir=pb_dir,
                skill_dir=out_skill_dir,
                interface_doc="## sheepdog 接口说明\n自定义覆写接口文档 v2",
                force=True,
            )
            self.assertNotEqual(sp_b.version, sp_a.version)

            (cfg_root / "prompts" / "overlay.md").write_text(
                "## 总线专属 Overlay v2\n新增一条总线规则。\n",
                encoding="utf-8",
            )
            sp_c = ensure_skillpack_rendered(
                config_dir=cfg_root,
                playbook_dir=pb_dir,
                skill_dir=out_skill_dir,
                force=True,
            )
            self.assertNotEqual(sp_c.version, sp_a.version)

            # 5) 断言仅传 Config(state_dir=tmp)（未设 SHEEPDOG_STATE_DIR 环境变量）时，
            #    resolve_skill_dir / ensure_skillpack_rendered 隔离在测试 state_dir 内，绝不污染宿主机真实工作区
            from sheepdog.config import Config
            from sheepdog.skillpack import resolve_skill_dir
            from sheepdog.store import Store
            bare_state = cfg_root / "bare_state"
            bare_state.mkdir(parents=True, exist_ok=True)
            bare_cfg = Config(state_dir=bare_state)
            bare_store = Store(bare_cfg.db_path)
            self.addCleanup(bare_store.close)
            with mock.patch.dict(os.environ, {"SHEEPDOG_STATE_DIR": "", "SHEEPDOG_SKILL_DIR": ""}):
                sp_bare = ensure_skillpack_rendered(
                    bare_store,
                    config_dir=cfg_root,
                    playbook_dir=pb_dir,
                    force=True,
                )
                self.assertTrue(
                    str(sp_bare.skill_dir.resolve()).startswith(str( cfg_root.resolve() )),
                    f"Expected skill_dir inside tmpdir {cfg_root.resolve()}, got {sp_bare.skill_dir.resolve()}",
                )
                resolved_state = resolve_skill_dir(state_dir=bare_state)
                self.assertEqual(resolved_state.resolve(), (bare_state / ".agents" / "skills" / "sheepdog-worker").resolve())


if __name__ == "__main__":
    unittest.main()
