"""REG-166: 30分钟窗口批量回查消息撤回与编辑改用 +messages-mget 与防误判夹具测试（T-9192）。

Matthew 指令要点：
1. 回查窗口 30 分钟：每 30 秒一次、每批最多 50 条；
2. 回查改用 lark-cli im +messages-mget --as user --no-reactions；
3. 与入账（+messages-search）走同一套渲染，格式 100% 对齐；
4. 简化 extract_mget_content，统一直接使用 content 字段；
5. 真实夹具测试（带 @ 文本、卡片、文件、post 富文本）断言回查防误判（content_changed == False）；
6. 真实编辑（如「这里」改成「infra群」）断言算编辑（content_changed == True）；
7. 撤回（deleted: true）断言算撤回（recalled == True）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
import unittest
from unittest import mock

from sheepdog.engine import Collector, RouteContext, _unwrap_edited_content
from sheepdog.models import Message
from sheepdog.router import DISPATCH, INBOX
from sheepdog.source.lark import extract_mget_content, parse_message
from test_threads import ThreadsBase, ALPHA_P2P, ESC_CHAT, ME, TZ8, _opt
from test_roster import msg


class RecallAuditTest(ThreadsBase):
    def test_unwrap_edited_content_variations(self):
        """测试 _unwrap_edited_content 正确剥离各类编辑与撤回包装（含嵌套与单层）。"""
        # 1. 干净纯文本
        self.assertEqual(_unwrap_edited_content("方案A"), ("方案A", "方案A"))
        self.assertEqual(_unwrap_edited_content(""), ("", ""))

        # 2. 单层（已编辑）旧：...\n新：...
        self.assertEqual(
            _unwrap_edited_content("（已编辑）旧：方案A\n新：方案B"),
            ("方案A", "方案B"),
        )

        # 3. 单层（编辑了消息）旧：...\n新：...
        self.assertEqual(
            _unwrap_edited_content("（编辑了消息）旧：方案A\n新：方案B"),
            ("方案A", "方案B"),
        )

        # 4. 嵌套多层编辑
        nested = "（已编辑）旧：（编辑了消息）旧：方案A\n新：方案B\n新：方案C"
        self.assertEqual(_unwrap_edited_content(nested), ("方案A", "方案C"))

        # 5. 单层（已撤回）原文：... 与（撤回了一条消息）原文：...
        self.assertEqual(_unwrap_edited_content("（已撤回）原文：机密文件"), ("机密文件", "机密文件"))
        self.assertEqual(_unwrap_edited_content("（撤回了一条消息）原文：机密文件"), ("机密文件", "机密文件"))

        # 6. 单纯的（已编辑）前缀（无 \n新：）
        self.assertEqual(_unwrap_edited_content("（已编辑）方案A"), ("方案A", "方案A"))
        self.assertEqual(_unwrap_edited_content("（已编辑）  带空格方案A"), ("带空格方案A", "带空格方案A"))

    def test_extract_mget_content(self):
        """测试 extract_mget_content 简化后统一直接提取 content 字段。"""
        # 1. 直接带 content 字段
        self.assertEqual(extract_mget_content({"content": "你好"}), "你好")
        # 2. 带 @ 文本
        self.assertEqual(extract_mget_content({"content": "@Dirk Ke 好像 #1001 是走这里发的"}), "@Dirk Ke 好像 #1001 是走这里发的")
        # 3. 卡片
        self.assertEqual(extract_mget_content({"content": '<card title="发版">\n正文\n</card>'}), '<card title="发版">\n正文\n</card>')
        # 4. 文件
        self.assertEqual(extract_mget_content({"content": '<file key="file_v3_0000" name="doc.pdf">'}), '<file key="file_v3_0000" name="doc.pdf">')
        # 5. body.content 兜底
        self.assertEqual(extract_mget_content({"body": {"content": "正文内容"}}), "正文内容")
        # 6. 空值或非 dict 保护
        self.assertEqual(extract_mget_content({}), "")
        self.assertEqual(extract_mget_content(None), "")

    def test_recent_human_message_ids_window_filter(self):
        """测试 30 分钟窗口过滤：真人未删除消息入选，过期/已删/机器人/系统消息严格排除。"""
        now = datetime.now().astimezone()

        # 1. 20分钟前的未删除真人消息 -> 应入选
        t20 = (now - timedelta(minutes=20)).isoformat(timespec="seconds")
        m_valid = msg(message_id="om_valid_1", sender_type="user", create_time=t20, content="有效消息")
        self.store.upsert_message(m_valid, route="inbox", reason="ambient", tags=[])

        # 2. 45分钟前的人类消息 -> 超出30分钟窗口，应排除
        t45 = (now - timedelta(minutes=45)).isoformat(timespec="seconds")
        m_expired = msg(message_id="om_expired_1", sender_type="user", create_time=t45, content="过期消息")
        self.store.upsert_message(m_expired, route="inbox", reason="ambient", tags=[])
        # 覆写 first_seen 为 45 分钟前
        with self.store.tx() as c:
            c.execute("UPDATE messages SET first_seen=? WHERE message_id='om_expired_1'", (t45,))

        # 3. 机器人消息 (app / bot) -> 应排除
        m_bot = msg(message_id="om_bot_1", sender_type="app", create_time=t20, content="机器人消息")
        self.store.upsert_message(m_bot, route="drop", reason="bot", tags=[])

        # 4. 系统合成消息 (sd_ 开头) -> 应排除
        m_sys = msg(message_id="sd_sys_1", sender_type="user", create_time=t20, content="系统消息")
        self.store.upsert_message(m_sys, route="drop", reason="sys", tags=[])

        # 5. 现场变更合成消息 (:change:) -> 应排除
        m_chg = msg(message_id="om_valid_1:change:12345", sender_type="user", create_time=t20, content="变更")
        self.store.upsert_message(m_chg, route="inbox", reason="ambient_change", tags=[])

        # 6. 已删除消息 (deleted=1) -> 应排除
        m_del = msg(message_id="om_del_1", sender_type="user", create_time=t20, deleted=True, content="已删消息")
        self.store.upsert_message(m_del, route="inbox", reason="ambient", tags=[])
        with self.store.tx() as c:
            c.execute("UPDATE messages SET deleted=1 WHERE message_id='om_del_1'")

        mids = self.store.recent_human_message_ids(minutes=30)
        self.assertIn("om_valid_1", mids)
        self.assertNotIn("om_expired_1", mids)
        self.assertNotIn("om_bot_1", mids)
        self.assertNotIn("sd_sys_1", mids)
        self.assertNotIn("om_valid_1:change:12345", mids)
        self.assertNotIn("om_del_1", mids)

    def test_audit_recalls_and_edits_120_messages_three_batches(self):
        """REG-166: 测试 30 分钟窗口内有 120 条真人消息时，默认 limit=200 全部查到并分 3 批调用 +messages-mget。"""
        now = datetime.now(TZ8)
        collector = Collector(self.cfg, self.store, self.source, self.roster)
        for i in range(120):
            mid = f"om_test_120_{i:03d}"
            t_iso = (now - timedelta(minutes=min(i // 5, 25))).isoformat(timespec="seconds")
            self.lark.msgs[mid] = {
                "chat_id": ALPHA_P2P, "content": f"消息内容 {i}", "deleted": False,
                "updated": False, "msg_type": "text", "sender": {"id": "ou_test_alice", "sender_type": "user"},
                "at": now, "update_time": "",
            }
            m = msg(message_id=mid, chat_id=ALPHA_P2P, sender_id="ou_test_alice",
                    content=f"消息内容 {i}", chat_type="p2p", create_time=t_iso)
            self.store.upsert_message(m, route="inbox", reason="ambient", tags=[])

        # 检查 recent_human_message_ids 默认 limit=200 查出全部 120 条
        mids = self.store.recent_human_message_ids(minutes=30)
        self.assertEqual(len(mids), 120)

        # 执行回查：调用 mget_messages，120 条应按 50, 50, 20 分成 3 批调用
        stats = collector.audit_recalls_and_edits(window_minutes=30, force=True)
        self.assertEqual(stats["audited"], 120)
        self.assertEqual(stats["changed"], 0)

        mget_calls = [c for c in self.lark.calls if len(c) >= 2 and c[:2] == ["im", "+messages-mget"]]
        self.assertEqual(len(mget_calls), 3)
        self.assertEqual(len(_opt(mget_calls[0], "--message-ids").split(",")), 50)
        self.assertEqual(len(_opt(mget_calls[1], "--message-ids").split(",")), 50)
        self.assertEqual(len(_opt(mget_calls[2], "--message-ids").split(",")), 20)

    def test_mget_messages_chunking_and_api_call(self):
        """测试 LarkCliSource.mget_messages 分批调用 +messages-mget（每批最多 50 条）与端点参数。"""
        # 构造 75 个消息 ID
        for i in range(75):
            mid = f"om_test_batch_{i:03d}"
            self.lark.msgs[mid] = {
                "chat_id": "oc_test_chunk", "content": f"msg {i}", "deleted": False,
                "updated": False, "msg_type": "text", "sender": {"id": "ou_user_1", "sender_type": "user"},
                "at": datetime.now(TZ8), "update_time": "",
            }

        mids = [f"om_test_batch_{i:03d}" for i in range(75)]
        items = self.source.mget_messages(mids, identity="user")
        self.assertEqual(len(items), 75)

        # 检查 FakeLark 收到的调用列表：必须为 2 次，第一批 50，第二批 25
        mget_calls = [c for c in self.lark.calls if len(c) >= 2 and c[:2] == ["im", "+messages-mget"]]
        self.assertEqual(len(mget_calls), 2)

        batch1_mids = _opt(mget_calls[0], "--message-ids").split(",")
        batch2_mids = _opt(mget_calls[1], "--message-ids").split(",")
        self.assertEqual(len(batch1_mids), 50)
        self.assertEqual(len(batch2_mids), 25)
        self.assertIn("--no-reactions", mget_calls[0])
        self.assertIn("--format", mget_calls[0])
        self.assertEqual(_opt(mget_calls[0], "--format"), "json")

    def test_audit_recalls_and_edits_recall_processing(self):
        """测试批量回查处理消息撤回（deleted: true）：标记撤回、保留原文、重置 pending、幂等防护。"""
        # 1. 投递并 ack 一条消息
        mid = "om_test_recall_1"
        self.lark.msgs[mid] = {
            "chat_id": ALPHA_P2P, "content": "这是重要指令", "deleted": False,
            "updated": False, "msg_type": "text", "sender": {"id": "ou_test_alice", "sender_type": "user"},
            "at": datetime.now(TZ8), "update_time": "",
        }
        collector = Collector(self.cfg, self.store, self.source, self.roster)
        collector.ingest([msg(message_id=mid, chat_id=ALPHA_P2P, sender_id="ou_test_alice",
                             content="这是重要指令", chat_type="p2p")])
        self.d.dispatch_once()
        self.ack("tp_alpha")
        self.d.dispatch_once()

        row_before = self.store.get_message(mid)
        self.assertEqual(row_before["dispatch_state"], "acked")
        self.assertEqual(row_before["deleted"], 0)

        # 2. 模拟用户在飞书端撤回该消息
        self.lark.msgs[mid]["deleted"] = True

        # 3. 执行批量回查
        stats = collector.audit_recalls_and_edits(window_minutes=30, force=True)
        self.assertEqual(stats["recalled"], 1)
        self.assertEqual(stats["changed"], 1)

        row_after = self.store.get_message(mid)
        self.assertEqual(row_after["deleted"], 1)
        self.assertEqual(row_after["reason"], "recalled")
        self.assertEqual(row_after["dispatch_state"], "pending")
        self.assertEqual(row_after["content"], "（已撤回）原文：这是重要指令")

        # 4. 再次回查：已记录为 deleted，changed 严格为 0（幂等防抖）
        stats_repeat = collector.audit_recalls_and_edits(window_minutes=30, force=True)
        self.assertEqual(stats_repeat["changed"], 0)
        self.assertEqual(stats_repeat["recalled"], 0)

    def test_audit_recalls_and_edits_true_edit_and_unwrap(self):
        """测试批量回查处理真编辑与拆包装比对：防止假编辑与套娃，真编辑保留单层差异。"""
        mid = "om_test_edit_1"
        self.lark.msgs[mid] = {
            "chat_id": ALPHA_P2P, "content": "原始方案A", "deleted": False,
            "updated": False, "msg_type": "text", "sender": {"id": "ou_test_alice", "sender_type": "user"},
            "at": datetime.now(TZ8), "update_time": "",
        }
        collector = Collector(self.cfg, self.store, self.source, self.roster)
        collector.ingest([msg(message_id=mid, chat_id=ALPHA_P2P, sender_id="ou_test_alice",
                             content="原始方案A", chat_type="p2p")])
        self.d.dispatch_once()
        self.ack("tp_alpha")
        self.d.dispatch_once()

        # 1. 远端发生真编辑：原始方案A -> 优化方案B
        self.lark.msgs[mid]["content"] = "优化方案B"
        self.lark.msgs[mid]["updated"] = True
        self.lark.msgs[mid]["update_time"] = "1772093170000"

        stats1 = collector.audit_recalls_and_edits(window_minutes=30, force=True)
        self.assertEqual(stats1["edited"], 1)
        self.assertEqual(stats1["changed"], 1)

        row1 = self.store.get_message(mid)
        self.assertEqual(row1["reason"], "edited")
        self.assertEqual(row1["dispatch_state"], "pending")
        self.assertEqual(row1["content"], "（已编辑）旧：原始方案A\n新：优化方案B")

        # 2. 模拟后续重扫：账本中已有包装，远端仍是「优化方案B」
        # _unwrap_edited_content 正确提取最新正文为「优化方案B」，与远端完全一致 -> changed 必须为 0
        stats2 = collector.audit_recalls_and_edits(window_minutes=30, force=True)
        self.assertEqual(stats2["changed"], 0)
        self.assertEqual(stats2["edited"], 0)
        # 账本内容保持单层，绝不套娃为『（已编辑）旧：（已编辑）旧：...』
        self.assertEqual(self.store.get_message(mid)["content"], "（已编辑）旧：原始方案A\n新：优化方案B")

        # 3. 投递并 ack 第一次编辑后，远端再次发生第二次真编辑：优化方案B -> 终极方案C
        self.d.dispatch_once()
        self.ack("tp_alpha")
        self.d.dispatch_once()
        self.assertEqual(self.store.get_message(mid)["dispatch_state"], "acked")

        self.lark.msgs[mid]["content"] = "终极方案C"
        stats3 = collector.audit_recalls_and_edits(window_minutes=30, force=True)
        self.assertEqual(stats3["edited"], 1)
        self.assertEqual(stats3["changed"], 1)
        # 正确更新为单层旧：优化方案B\n新：终极方案C
        self.assertEqual(self.store.get_message(mid)["content"], "（已编辑）旧：优化方案B\n新：终极方案C")

    def test_audit_recalls_and_edits_inbox_ambient_change_event(self):
        """测试普通 Inbox 消息撤回/编辑追加现场变更事件（ambient_change）。"""
        mid = "om_test_inbox_1"
        self.lark.msgs[mid] = {
            "chat_id": "oc_group_chatter", "content": "闲聊消息A", "deleted": False,
            "updated": False, "msg_type": "text", "sender": {"id": "ou_user_2", "sender_type": "user"},
            "at": datetime.now(TZ8), "update_time": "",
        }
        collector = Collector(self.cfg, self.store, self.source, self.roster)
        collector.ingest([msg(message_id=mid, chat_id="oc_group_chatter", sender_id="ou_user_2",
                             content="闲聊消息A", chat_type="group")])
        self.assertEqual(self.store.get_message(mid)["route"], INBOX)

        # 1. 编辑 Inbox 消息
        self.lark.msgs[mid]["content"] = "闲聊消息A已改"
        self.lark.msgs[mid]["updated"] = True
        stats_edit = collector.audit_recalls_and_edits(window_minutes=30, force=True)
        self.assertEqual(stats_edit["edited"], 1)

        # 验证生成了现场变更事件
        change_rows = self.store.conn.execute(
            "SELECT * FROM messages WHERE reason='ambient_change' AND message_id LIKE ?",
            (f"{mid}:change:%",)
        ).fetchall()
        self.assertEqual(len(change_rows), 1)
        self.assertIn("（编辑了消息）旧：闲聊消息A\n新：闲聊消息A已改", change_rows[0]["content"])

        # 2. 撤回 Inbox 消息
        self.lark.msgs[mid]["deleted"] = True
        stats_rec = collector.audit_recalls_and_edits(window_minutes=30, force=True)
        self.assertEqual(stats_rec["recalled"], 1)

        change_rows2 = self.store.conn.execute(
            "SELECT * FROM messages WHERE reason='ambient_change' AND message_id LIKE ?",
            (f"{mid}:change:%",)
        ).fetchall()
        self.assertEqual(len(change_rows2), 2)
        self.assertIn("（撤回了一条消息）原文：闲聊消息A已改", change_rows2[1]["content"])

    def test_audit_recalls_and_edits_throttle(self):
        """测试 30 秒回查节流：未加 force 时 30 秒内重复调用直接返回 throttled。"""
        collector = Collector(self.cfg, self.store, self.source, self.roster)
        # 第一次调用（无 force）
        res1 = collector.audit_recalls_and_edits(window_minutes=30, force=False)
        self.assertFalse(res1.get("throttled", False))

        # 紧接着第二次调用（无 force）-> 应当被节流拦截
        res2 = collector.audit_recalls_and_edits(window_minutes=30, force=False)
        self.assertTrue(res2.get("throttled", False))
        self.assertEqual(res2.get("changed", 0), 0)

        # 带 force=True -> 绕过节流立即执行
        res3 = collector.audit_recalls_and_edits(window_minutes=30, force=True)
        self.assertFalse(res3.get("throttled", False))

    def test_integration_poll_once_and_poll_bot_only_audit(self):
        """测试 poll_once 和 poll_bot_only 均集成调用 audit_recalls_and_edits 并回显 changed 统计。"""
        mid = "om_test_int_1"
        self.lark.msgs[mid] = {
            "chat_id": ALPHA_P2P, "content": "整体验收消息", "deleted": False,
            "updated": False, "msg_type": "text", "sender": {"id": "ou_test_alice", "sender_type": "user"},
            "at": datetime.now(TZ8), "update_time": "",
        }
        collector = Collector(self.cfg, self.store, self.source, self.roster)
        collector.ingest([msg(message_id=mid, chat_id=ALPHA_P2P, sender_id="ou_test_alice",
                             content="整体验收消息", chat_type="p2p")])
        self.d.dispatch_once()
        self.ack("tp_alpha")
        self.d.dispatch_once()

        # 远端编辑
        self.lark.msgs[mid]["content"] = "整体验收消息已改"
        self.lark.msgs[mid]["updated"] = True
        collector._last_recall_audit_ts = 0.0

        # poll_once 触发
        with mock.patch.object(self.source, "fetch_since", return_value=[]):
            stats = collector.poll_once()
            self.assertIn("audit", stats)
            self.assertEqual(stats["audit"]["edited"], 1)
            self.assertEqual(stats["changed"], 1)

    def test_audit_recalls_and_edits_real_fixtures_no_false_positive(self):
        """REG-166 核心夹具断言：
        1. 验证真实 +messages-mget 返回的 4 类复杂消息（@提及、卡片、文件、post富文本）与入账严格一致，回查不算编辑（content_changed == False）；
        2. 验证真编辑（如「这里」改成了「infra群」）精准算编辑（content_changed == True）；
        3. 验证撤回（deleted: true）精准算撤回（recalled == True）。
        """
        collector = Collector(self.cfg, self.store, self.source, self.roster)

        # 4 个真实复杂消息夹具
        fixture_at = "@Dirk Ke 好像 #1001 是走这里发的，麻烦看下"
        fixture_card = '<card title="🐕 汇报 · 生产发版结论">\n所有容器就绪\n</card>'
        fixture_file = '<file key="file_v3_0000_52bc114a-c973-4411-8d41-ec9edad70fhu" name="QA-report-1110.pdf">'
        fixture_post = "Matthew，预发环境已就绪！\n1. **请确认配置**\n2. 点击发布"

        fixtures = [
            ("om_test_fix_at", "text", fixture_at),
            ("om_test_fix_card", "interactive", fixture_card),
            ("om_test_fix_file", "file", fixture_file),
            ("om_test_fix_post", "post", fixture_post),
        ]

        now = datetime.now(TZ8)
        ingest_msgs = []
        for mid, mtype, content in fixtures:
            self.lark.msgs[mid] = {
                "chat_id": ALPHA_P2P, "content": content, "deleted": False,
                "updated": False, "msg_type": mtype,
                "sender": {"id": "ou_test_alice", "sender_type": "user"},
                "at": now, "update_time": "",
            }
            ingest_msgs.append(msg(
                message_id=mid, chat_id=ALPHA_P2P, sender_id="ou_test_alice",
                content=content, chat_type="p2p", msg_type=mtype,
            ))

        collector.ingest(ingest_msgs)
        self.d.dispatch_once()
        self.ack("tp_alpha")
        self.d.dispatch_once()

        # 确保全部入账并 acked
        for mid, _, content in fixtures:
            row = self.store.get_message(mid)
            self.assertIsNotNone(row)
            self.assertEqual(row["content"], content)
            self.assertEqual(row["deleted"], 0)

        # --- 步骤 1：批量回查真实夹具，断言不算编辑（防误判） ---
        stats = collector.audit_recalls_and_edits(window_minutes=30, force=True)
        self.assertEqual(stats["audited"], 4)
        self.assertEqual(stats["changed"], 0)
        self.assertEqual(stats["edited"], 0)
        self.assertEqual(stats["recalled"], 0)

        for mid, _, content in fixtures:
            row = self.store.get_message(mid)
            self.assertEqual(row["content"], content)
            self.assertEqual(row["deleted"], 0)

        # --- 步骤 2：构造一条真编辑（如「这里」改成了「infra群」），断言算编辑 ---
        mid_edit = "om_test_fix_edit"
        self.lark.msgs[mid_edit] = {
            "chat_id": ALPHA_P2P, "content": "请把日志发到这里", "deleted": False,
            "updated": False, "msg_type": "text",
            "sender": {"id": "ou_test_alice", "sender_type": "user"},
            "at": now, "update_time": "",
        }
        collector.ingest([msg(
            message_id=mid_edit, chat_id=ALPHA_P2P, sender_id="ou_test_alice",
            content="请把日志发到这里", chat_type="p2p",
        )])
        self.d.dispatch_once()
        self.ack("tp_alpha")
        self.d.dispatch_once()

        # 远端发生真编辑：把「这里」改成「infra群」
        self.lark.msgs[mid_edit]["content"] = "请把日志发到infra群"
        self.lark.msgs[mid_edit]["updated"] = True
        self.lark.msgs[mid_edit]["update_time"] = "1772093220000"

        stats_edit = collector.audit_recalls_and_edits(window_minutes=30, force=True)
        self.assertEqual(stats_edit["edited"], 1)
        self.assertEqual(stats_edit["changed"], 1)

        row_edit = self.store.get_message(mid_edit)
        self.assertEqual(row_edit["reason"], "edited")
        self.assertEqual(row_edit["dispatch_state"], "pending")
        self.assertEqual(row_edit["content"], "（已编辑）旧：请把日志发到这里\n新：请把日志发到infra群")

        # --- 步骤 3：构造一条撤回（deleted: true），断言算撤回 ---
        mid_recall = "om_test_fix_recall"
        self.lark.msgs[mid_recall] = {
            "chat_id": ALPHA_P2P, "content": "准备发版，请核验", "deleted": False,
            "updated": False, "msg_type": "text",
            "sender": {"id": "ou_test_alice", "sender_type": "user"},
            "at": now, "update_time": "",
        }
        collector.ingest([msg(
            message_id=mid_recall, chat_id=ALPHA_P2P, sender_id="ou_test_alice",
            content="准备发版，请核验", chat_type="p2p",
        )])
        self.d.dispatch_once()
        self.ack("tp_alpha")
        self.d.dispatch_once()

        # 远端发生撤回
        self.lark.msgs[mid_recall]["deleted"] = True

        stats_recall = collector.audit_recalls_and_edits(window_minutes=30, force=True)
        self.assertEqual(stats_recall["recalled"], 1)
        self.assertEqual(stats_recall["changed"], 1)

        row_recall = self.store.get_message(mid_recall)
        self.assertEqual(row_recall["deleted"], 1)
        self.assertEqual(row_recall["reason"], "recalled")
        self.assertEqual(row_recall["dispatch_state"], "pending")
        self.assertEqual(row_recall["content"], "（已撤回）原文：准备发版，请核验")


if __name__ == "__main__":
    unittest.main()
