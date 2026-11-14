"""单测套件：阅读游标与全景上下文感知优化验收测试。

覆盖要求：
1. 话题群卡片回调隔离测试：无 thread_id 的卡片回调在话题群内 resolve_ambient_scope 返回 None，绝不退化为整群现场；
2. 内部信箱隔离测试：chat_id == 'sheepdog' 或 chat_type == 'sheepdog' 返回 None，彻底不进现场；
3. 判空推进测试：候选消息全部属于当前批次触发消息（unseen 过滤后为空），游标正常前移至 max(candidates)；
4. 溢出截取测试：单 scope 存在 25 条未读消息，截取最新 20 条，游标推进到第 25 条 rowid，返回 overflow 标记与拉取提示；
5. 提示词渲染测试：身份分层、安全标记展示、纯背景批次文案更新。
"""

import json
import tempfile
import unittest
from pathlib import Path

from sheepdog.engine import resolve_ambient_scope
from sheepdog.models import Message
from sheepdog.prompts import (
    batch_prompt,
    render_ambient_surrounding_context,
)
from sheepdog.playbook import Playbook
from sheepdog.store import Store


class AmbientCursorTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_ambient.db"
        self.store = Store(self.db_path)
        self.topic_id = "tp_test_ambient"

    def tearDown(self):
        self.store.close()
        self.tmp_dir.cleanup()

    def _insert_msg(
        self,
        message_id: str,
        chat_id: str,
        content: str,
        sender_id: str = "ou_user1",
        sender_name: str = "User1",
        sender_type: str = "user",
        msg_type: str = "text",
        chat_type: str = "group",
        thread_id: str = "",
        security_action: str = "none",
        security_tags: str = "[]",
        route: str = "dispatch",
        reason: str = "at_me",
        tags_json: str = "[]",
        create_time: str = "2025-11-17T11:45:07+08:00",
        thread_position: str = "",
        message_position: str = "",
    ):
        m = Message(
            message_id=message_id,
            chat_id=chat_id,
            chat_name="Test Chat",
            chat_type=chat_type,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_type=sender_type,
            content=content,
            msg_type=msg_type,
            thread_id=thread_id,
            create_time=create_time,
            thread_position=thread_position,
            message_position=message_position,
        )
        self.store.upsert_message(
            m,
            route=route,
            reason=reason,
            tags=json.loads(tags_json) if isinstance(tags_json, str) else tags_json,
            topic_id=self.topic_id,
        )
        with self.store.tx() as c:
            c.execute(
                """UPDATE messages
                   SET security_action = ?, security_tags = ?, thread_id = ?,
                       thread_position = ?, message_position = ?
                   WHERE message_id = ?""",
                (security_action, security_tags, thread_id, thread_position, message_position, message_id),
            )
        return self.store.conn.execute("SELECT rowid, * FROM messages WHERE message_id = ?", (message_id,)).fetchone()

    # 1. 话题群卡片回调隔离测试
    def test_topic_group_card_callback_isolation(self):
        """话题群内有一条无 thread_id 的卡片回调消息，验证其调用 resolve_ambient_scope 返回 None，
        不拉取任何现场消息，也绝不退化为整群现场。"""
        card_callback_msg = {
            "message_id": "om_card_cb_1",
            "chat_id": "oc_topic_group_001",
            "chat_type": "group",
            "thread_id": "",
            "content": "[卡片回调] 确认执行",
            "sender_id": "ou_owner",
        }
        chat_meta = {"chat_mode": "topic"}

        # 1) resolve_ambient_scope 必须返回 None
        scope = resolve_ambient_scope(card_callback_msg, chat_meta)
        self.assertIsNone(scope, "话题群内缺少 thread_id 的消息必须返回 None，不能退化为整群现场")

        # 2) 即使通过字典 metadata 嵌套传入话题群属性，也应判定为话题群并返回 None
        chat_meta_nested = {"metadata": {"chat_mode": "topic"}}
        scope_nested = resolve_ambient_scope(card_callback_msg, chat_meta_nested)
        self.assertIsNone(scope_nested)

        # 3) 话题群内带有 thread_id 的普通业务消息正常解析为 ('thread', thread_id)
        normal_thread_msg = {
            "message_id": "om_normal_1",
            "chat_id": "oc_topic_group_001",
            "chat_type": "group",
            "thread_id": "th_topic_999",
            "content": "业务讨论",
        }
        scope_th = resolve_ambient_scope(normal_thread_msg, chat_meta)
        self.assertEqual(scope_th, ("thread", "th_topic_999"))

    # 2. 内部信箱隔离测试
    def test_internal_mailbox_isolation(self):
        """chat_id == 'sheepdog' 或 chat_type == 'sheepdog' 验证返回 None，彻底不进现场。"""
        # chat_id == 'sheepdog'
        msg_internal_chat_id = {
            "message_id": "sd_report_101",
            "chat_id": "sheepdog",
            "chat_type": "p2p",
            "content": "巡检汇报",
        }
        self.assertIsNone(resolve_ambient_scope(msg_internal_chat_id))

        # chat_type == 'sheepdog'
        msg_internal_chat_type = {
            "message_id": "sd_report_102",
            "chat_id": "oc_some_group",
            "chat_type": "sheepdog",
            "content": "系统广播",
        }
        self.assertIsNone(resolve_ambient_scope(msg_internal_chat_type))

    # 3. 判空推进测试
    def test_cursor_advances_when_unseen_is_empty(self):
        """候选消息全部属于当前批次的触发消息（unseen 过滤后为空），验证游标依然正常前移至 max(candidates)。"""
        chat_id = "oc_chat_empty_test"

        # 插入基线消息以初始化冷启动游标
        self._insert_msg("om_base_1", chat_id, "基线消息 1")
        msgs, cur_rowid, overflow = self.store.get_ambient_since_cursor(
            self.topic_id, "chat", chat_id, exclude_msg_ids=set()
        )
        self.assertEqual(msgs, [])
        self.assertFalse(overflow)
        base_cursor = cur_rowid
        self.assertGreater(base_cursor, 0)

        # 插入两条新消息，作为本次批次的触发消息
        self._insert_msg("om_trigger_1", chat_id, "触发消息 1")
        m2 = self._insert_msg("om_trigger_2", chat_id, "触发消息 2")

        # 将 trigger 消息传入 exclude_msg_ids
        trigger_ids = {"om_trigger_1", "om_trigger_2"}
        unseen, target_rowid, overflow = self.store.get_ambient_since_cursor(
            self.topic_id, "chat", chat_id, exclude_msg_ids=trigger_ids
        )

        # unseen 过滤后必须为空，但 target_rowid 必须推进到最新消息的 rowid
        self.assertEqual(unseen, [])
        self.assertFalse(overflow)
        self.assertGreater(target_rowid, base_cursor)
        self.assertEqual(target_rowid, m2["rowid"])

        # 推进游标并持久化
        self.store.advance_ambient_cursor(self.topic_id, "chat", chat_id, target_rowid)

        # 验证游标已成功推进：再次查询同一 scope，不应再重复拉取 om_trigger_1 / om_trigger_2
        unseen_next, target_rowid_next, _ = self.store.get_ambient_since_cursor(
            self.topic_id, "chat", chat_id, exclude_msg_ids=set()
        )
        self.assertEqual(unseen_next, [])
        self.assertEqual(target_rowid_next, target_rowid)

    # 4. 溢出截取测试
    def test_cursor_overflow_truncation_and_prompt(self):
        """单 scope 存在 25 条未读消息，验证截取最新 20 条（保留最新的 5 条～25 条），
        游标推进到第 25 条的 rowid，且返回 overflow 标记与拉取提示。"""
        chat_id = "oc_chat_overflow_test"

        # 初始化游标
        self._insert_msg("om_init_0", chat_id, "初始消息 0")
        self.store.get_ambient_since_cursor(self.topic_id, "chat", chat_id)

        # 连续写入 25 条未读环境消息
        created_rows = []
        for i in range(1, 26):
            r = self._insert_msg(
                f"om_ambient_{i:02d}",
                chat_id,
                f"现场讨论第 {i} 条",
                sender_name=f"同事_{i}",
                thread_position=str(i),
            )
            created_rows.append(r)

        # 查询未读环境动态，limit 保持默认 20
        unseen, target_rowid, overflow = self.store.get_ambient_since_cursor(
            self.topic_id, "chat", chat_id, exclude_msg_ids=set(), limit=20
        )

        # 验证溢出标记为 True
        self.assertTrue(overflow, "超过 20 条时 overflow 必须为 True")
        # 验证截取后保留 20 条
        self.assertEqual(len(unseen), 20)
        # 验证保留的是最新的 20 条（即第 6 条到第 25 条）
        self.assertEqual(unseen[0]["message_id"], "om_ambient_06")
        self.assertEqual(unseen[-1]["message_id"], "om_ambient_25")
        # 验证目标游标水位推进到了第 25 条的 rowid
        self.assertEqual(target_rowid, created_rows[-1]["rowid"])

        # 验证提示词渲染与拉取引导提示
        rendered_block = render_ambient_surrounding_context(unseen, overflow_scopes=[chat_id])
        self.assertIn("🌐 全景上下文（自上次处理以来的环境动态，按发生顺序排列）：", rendered_block)
        expected_pull_hint = f"（本话题最新消息积压超过 20 条，已截取最新 20 条。如需查阅更早未读历史，可使用 sheepdog inbox --chat {chat_id}）"
        self.assertIn(expected_pull_hint, rendered_block)

    # 5. 提示词渲染与安全标记、纯背景文案测试
    def test_prompt_rendering_and_security_tag(self):
        """测试按身份与序号分层渲染、security_action='tag' 安全标记输出，以及纯背景批次提示词。"""
        chat_id = "oc_chat_render_test"

        # 构造一条带安全标记的普通同事消息
        m1 = self._insert_msg(
            "om_ambient_tag",
            chat_id,
            "这是一条带敏感标签的第三方发言",
            sender_name="Ivor",
            security_action="tag",
            security_tags=json.dumps(["permissions", "auth_token"]),
            thread_position="10",
        )
        # 构造一条主人发言消息
        m2 = self._insert_msg(
            "om_ambient_owner",
            chat_id,
            "这是主人的现场发言",
            sender_name="主人",
            reason="owner_context",
            thread_position="11",
        )

        rendered = render_ambient_surrounding_context([m1, m2])
        # 验证同事消息渲染格式
        self.assertIn("[pos: 10] 👥 Ivor: 这是一条带敏感标签的第三方发言", rendered)
        # 验证安全标记显式展示
        self.assertIn("⚠️ 安全标记: permissions, auth_token", rendered)
        # 验证主人消息身份渲染格式
        self.assertIn("[pos: 11] 📝 主人: 这是主人的现场发言", rendered)

        # 验证纯背景批次（context_only=True）提示词更新
        pb = Playbook(Path(__file__).parent / "fixtures" / "playbook")
        prompt_context_only = batch_prompt(
            pb,
            self.topic_id,
            "b_test_ctx_01",
            [m2],
            inbox=[],
            session_title="测试专席",
            context_only=True,
            ambient_msgs=[m1],
        )
        expected_ctx_text = "这是主人的补充发言；如果改变了你手头上正在推进的事，请严格按主人的补充调整；本批不用交回执，确认无冲突继续执行手上的事。"
        self.assertIn(expected_ctx_text, prompt_context_only)
        self.assertIn("🌐 全景上下文（自上次处理以来的环境动态，按发生顺序排列）：", prompt_context_only)

    # 6. Dispatcher 组装与送达闭环推进测试
    def test_dispatcher_ambient_context_assembly_and_cursor_advance(self):
        """测试 Dispatcher 在 _dispatch_topic 时组装全景上下文，并在 _complete_delivery（包含纯背景批）与 _complete_priority 时闭环推进游标。"""
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Dispatcher

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        cfg.playbook_dir = str(Path(__file__).parent / "fixtures" / "playbook")
        roster_mock = MagicMock()
        roster_mock.by_topic.return_value = None
        roster_mock.vips.return_value = {}
        roster_mock.owner_of.return_value = None
        fake_sink = MagicMock(busy=lambda cid: "")
        disp = Dispatcher(cfg, self.store, {"antigravity": fake_sink}, roster_mock, None)

        sent_prompts = []
        disp._send = lambda topic, prompt, batch_id=None: sent_prompts.append((batch_id, prompt))

        # 1) 创建会话并初始化游标
        chat_id = "oc_chat_e2e"
        self.store.create_topic("tp_e2e", "E2E 会话", "adopted", "测试职责", "idle")
        self.store.update_topic("tp_e2e", conversation_id="cid_e2e", onboarded_at="2025-11-17T11:45:07+08:00")

        # 写入历史基线消息
        self._insert_msg("om_e2e_base", chat_id, "历史消息 0")
        self.store.get_ambient_since_cursor("tp_e2e", "chat", chat_id)

        # 写入两条同事的环境讨论
        self._insert_msg("om_e2e_amb_1", chat_id, "同事讨论 A", sender_name="Bob")
        self._insert_msg("om_e2e_amb_2", chat_id, "同事讨论 B", sender_name="Charlie")

        # 写入本次批次的触发消息
        trig = self._insert_msg("om_e2e_trig", chat_id, "@羊 帮我查一下指标", route="dispatch", reason="at_me")

        # 执行 _dispatch_topic
        t = self.store.get_topic("tp_e2e")
        res = disp._dispatch_topic(t, [trig])
        self.assertEqual(len(sent_prompts), 1)
        bid, prompt_text = sent_prompts[0]
        self.assertIn("🌐 全景上下文（自上次处理以来的环境动态，按发生顺序排列）：", prompt_text)
        self.assertIn("同事讨论 A", prompt_text)
        self.assertIn("同事讨论 B", prompt_text)
        self.assertIn("@羊 帮我查一下指标", prompt_text)

        # 游标暂存在 _pending_ambient_cursors 且 dispatches.cursors_json 已持久化
        d_rec = self.store.get_dispatch(bid)
        self.assertIsNotNone(d_rec)
        self.assertTrue("cursors_json" in d_rec.keys() and bool(d_rec["cursors_json"]))

        # 验证送达后 _complete_delivery 已自动推进游标至触发消息水位
        cur_after = self.store.get_ambient_cursor("tp_e2e", "chat", chat_id)["last_rowid"]
        self.assertEqual(cur_after, trig["rowid"])

        # 2) 测试纯背景批次（context_only，无 signals）也能正常推进游标
        sent_prompts.clear()
        self._insert_msg("om_e2e_amb_3", chat_id, "同事讨论 C", sender_name="Dave")
        owner_ctx = self._insert_msg("om_e2e_owner", chat_id, "这是主人的补充背景", sender_name="主人", reason="owner_context")

        self.store.update_topic("tp_e2e", state="idle")
        t = self.store.get_topic("tp_e2e")
        res_ctx = disp._dispatch_topic(t, [owner_ctx])
        self.assertEqual(len(sent_prompts), 1)
        bid_ctx, prompt_ctx_text = sent_prompts[0]
        self.assertIn("这是主人的补充发言；如果改变了你手头上正在推进的事", prompt_ctx_text)

        disp._complete_delivery(t, bid_ctx, [], [owner_ctx])
        cur_ctx_after = self.store.get_ambient_cursor("tp_e2e", "chat", chat_id)["last_rowid"]
        self.assertEqual(cur_ctx_after, owner_ctx["rowid"])

    # 7. 真实调度路径：未配置话题群卡片回调隔离测试（案例 1）
    def test_dispatch_unconfigured_topic_group_card_callback_isolation(self):
        """案例 1：构造一个不在配置里的话题群 oc_unconfigured_topic_group，写入一条 chat_type='topic' 消息
        让系统记录群模式；再写入不同话题的消息，以及一条 chat_type='group' 且无 thread_id 的卡片回调；
        触发真实调度，验证卡片回调消息的现场段绝不包含该话题群其他话题的消息。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Dispatcher

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        cfg.playbook_dir = str(Path(__file__).parent / "fixtures" / "playbook")
        roster_mock = MagicMock()
        roster_mock.by_topic.return_value = None
        roster_mock.vips.return_value = {}
        roster_mock.owner_of.return_value = None
        fake_sink = MagicMock(busy=lambda cid: "")
        disp = Dispatcher(cfg, self.store, {"antigravity": fake_sink}, roster_mock, None)

        sent_prompts = []
        disp._send = lambda topic, prompt, batch_id=None: sent_prompts.append((batch_id, prompt))

        # 确保配置中没有 oc_unconfigured_topic_group
        topic_group_chat_id = getattr(cfg.topic_group, "chat_id", None) if getattr(cfg, "topic_group", None) else None
        chat_id = "oc_unconfigured_topic_group"
        self.assertNotEqual(topic_group_chat_id, chat_id)

        # 1) 创建会话并初始化
        topic_id = "tp_unconf"
        self.store.create_topic(topic_id, "未配置话题群会话", "adopted", "测试职责", "idle")
        self.store.update_topic(topic_id, conversation_id="cid_unconf", onboarded_at="2025-11-17T11:45:07+08:00")

        # 2) 写入一条 chat_type='topic' 消息让系统记录群模式
        self._insert_msg(
            "om_unconf_init",
            chat_id=chat_id,
            content="话题群初始建立消息",
            chat_type="topic",
            thread_id="th_init",
        )
        self.assertEqual(self.store.get_meta(f"chat_mode:{chat_id}"), "topic")

        # 3) 写入不同话题的消息
        self._insert_msg(
            "om_unconf_th1",
            chat_id=chat_id,
            content="话题1业务讨论详情",
            chat_type="topic",
            thread_id="th_topic_1",
            sender_name="Alice",
            route="inbox",
        )
        self._insert_msg(
            "om_unconf_th2",
            chat_id=chat_id,
            content="话题2排查日志详情",
            chat_type="topic",
            thread_id="th_topic_2",
            sender_name="Bob",
            route="inbox",
        )

        # 4) 写入一条 chat_type='group' 且无 thread_id 的卡片回调消息
        card_cb = self._insert_msg(
            "om_unconf_card_cb",
            chat_id=chat_id,
            content="[卡片回调] 确认执行生产变更",
            chat_type="group",
            thread_id="",
            sender_id="ou_me",
            sender_name="Matthew Hong",
            route="dispatch",
            reason="owner_instruction",
        )

        # 5) 触发真实调度
        t = self.store.get_topic(topic_id)
        res = disp._dispatch_topic(t, [card_cb])
        self.assertEqual(len(sent_prompts), 1)
        bid, prompt_text = sent_prompts[0]

        # 6) 验证卡片回调消息的现场段绝不包含该话题群其他话题的消息
        self.assertNotIn("话题1业务讨论详情", prompt_text)
        self.assertNotIn("话题2排查日志详情", prompt_text)
        self.assertNotIn("🌐 全景上下文", prompt_text)

        # 验证 dispatches 表记录
        d_rec = self.store.get_dispatch(bid)
        self.assertIsNotNone(d_rec)
        self.assertEqual(d_rec["batch_id"], bid)

    # 8. 真实调度路径：私聊 route='self' 过滤与真人主人判定测试（案例 2）
    def test_dispatch_private_chat_self_filtered_and_owner_rendered(self):
        """案例 2：构造私聊，包含正常同事消息、route='self' 代回消息、真人主人（sender_id=self_open_id, route='dispatch'）消息；
        触发调度，验证 route='self' 彻底不进现场，真人主人消息被渲染为 📝 主人:（而不是 👥 Matthew Hong:）。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Dispatcher

        owner_open_id = "ou_owner_matthew"
        cfg = Config(
            self_open_id=owner_open_id,
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        cfg.playbook_dir = str(Path(__file__).parent / "fixtures" / "playbook")
        roster_mock = MagicMock()
        roster_mock.by_topic.return_value = None
        roster_mock.vips.return_value = {}
        roster_mock.owner_of.return_value = None
        fake_sink = MagicMock(busy=lambda cid: "")
        disp = Dispatcher(cfg, self.store, {"antigravity": fake_sink}, roster_mock, None)

        sent_prompts = []
        disp._send = lambda topic, prompt, batch_id=None: sent_prompts.append((batch_id, prompt))

        # 1) 创建会话并初始化游标
        topic_id = "tp_p2p"
        self.store.create_topic(topic_id, "私聊专席会话", "adopted", "私聊职责", "idle")
        self.store.update_topic(topic_id, conversation_id="cid_p2p", onboarded_at="2025-11-17T11:45:07+08:00")

        chat_id = "oc_p2p_chat"
        # 写入历史基线并初始化游标
        self._insert_msg("om_p2p_base", chat_id=chat_id, content="私聊历史基线 0", chat_type="p2p")
        self.store.get_ambient_since_cursor(topic_id, "chat", chat_id)

        # 2) 构造私聊中的各类消息
        # 正常同事消息（route='inbox'）
        self._insert_msg(
            "om_p2p_colleague",
            chat_id=chat_id,
            content="同事正常发言：接口调用耗时偏高",
            sender_id="ou_colleague_alice",
            sender_name="Alice",
            chat_type="p2p",
            route="inbox",
            reason="inbox",
            thread_position="1",
        )
        # sheepdog 代回消息（route='self'）
        self._insert_msg(
            "om_p2p_self",
            chat_id=chat_id,
            content="sheepdog代回：已收到工单，稍后排查",
            sender_id=owner_open_id,
            sender_name="Matthew Hong",
            chat_type="p2p",
            route="self",
            reason="agent_reply_in_owner_chat",
            thread_position="2",
        )
        # 真人主人消息（sender_id=self_open_id, route='dispatch', sender_name='Matthew Hong'）
        self._insert_msg(
            "om_p2p_owner",
            chat_id=chat_id,
            content="主人的真实指令：优先发布新版本并观察指标",
            sender_id=owner_open_id,
            sender_name="Matthew Hong",
            chat_type="p2p",
            route="dispatch",
            reason="owner_instruction",
            thread_position="3",
        )

        # 触发调度的消息
        trig = self._insert_msg(
            "om_p2p_trig",
            chat_id=chat_id,
            content="@sheepdog 请汇报当前进展",
            sender_id="ou_colleague_alice",
            sender_name="Alice",
            chat_type="p2p",
            route="dispatch",
            reason="at_me",
            thread_position="4",
        )

        # 3) 触发真实调度
        t = self.store.get_topic(topic_id)
        res = disp._dispatch_topic(t, [trig])
        self.assertEqual(len(sent_prompts), 1)
        bid, prompt_text = sent_prompts[0]

        # 4) 验证：
        # - route='self' 代回消息彻底不进现场
        self.assertNotIn("sheepdog代回：已收到工单，稍后排查", prompt_text)
        # - 同事消息正常进入全景现场
        self.assertIn("🌐 全景上下文（自上次处理以来的环境动态，按发生顺序排列）：", prompt_text)
        self.assertIn("👥 Alice: 同事正常发言：接口调用耗时偏高", prompt_text)
        # - 真人主人消息被渲染为 📝 主人:，绝不能出现 👥 Matthew Hong:
        self.assertIn("📝 主人: 主人的真实指令：优先发布新版本并观察指标", prompt_text)
        self.assertNotIn("👥 Matthew Hong", prompt_text)

        # 5) 验证 dispatches 表记录完整性
        d_rec = self.store.get_dispatch(bid)
        self.assertIsNotNone(d_rec)
        self.assertEqual(d_rec["batch_id"], bid)

    def test_migrate_topic_group_modes_once(self):
        """测试 store._migrate 一次性扫描历史账本中所有 chat_type='topic' 的群写入 meta 表并标记 migrated_topic_group_modes。"""
        import sqlite3
        from sheepdog.store import SCHEMA

        custom_db_path = Path(self.tmp_dir.name) / "test_migration.db"
        # 1) 手工建立基础库并写入历史消息，故意不写入 meta 表和 migrated_topic_group_modes
        conn = sqlite3.connect(str(custom_db_path))
        conn.executescript(SCHEMA)
        # 写入两个话题群消息和一个普通群消息
        conn.execute(
            """INSERT INTO messages (message_id, chat_id, chat_type, content, create_time, first_seen, last_seen, route, reason)
               VALUES ('m_t1', 'oc_topic_alpha', 'topic', '话题群消息 1', '2025-11-17T09:44:06+08:00', '2025-11-17T09:44:06+08:00', '2025-11-17T09:44:06+08:00', 'inbox', 'inbox')"""
        )
        conn.execute(
            """INSERT INTO messages (message_id, chat_id, chat_type, content, create_time, first_seen, last_seen, route, reason)
               VALUES ('m_t2', 'oc_topic_beta', 'topic', '话题群消息 2', '2025-11-17T09:47:30+08:00', '2025-11-17T09:47:30+08:00', '2025-11-17T09:47:30+08:00', 'inbox', 'inbox')"""
        )
        conn.execute(
            """INSERT INTO messages (message_id, chat_id, chat_type, content, create_time, first_seen, last_seen, route, reason)
               VALUES ('m_g1', 'oc_normal_gamma', 'group', '普通群消息', '2025-11-17T09:49:58+08:00', '2025-11-17T09:49:58+08:00', '2025-11-17T09:49:58+08:00', 'inbox', 'inbox')"""
        )
        conn.commit()
        conn.close()

        # 2) 使用 Store 打开该数据库，触发 _migrate
        migrated_store = Store(custom_db_path)
        try:
            # 验证 meta 表已写入标记和两个话题群
            self.assertEqual(migrated_store.get_meta("migrated_topic_group_modes"), "1")
            self.assertEqual(migrated_store.get_meta("chat_mode:oc_topic_alpha"), "topic")
            self.assertEqual(migrated_store.get_meta("chat_mode:oc_topic_beta"), "topic")
            self.assertEqual(migrated_store.get_meta("chat_mode:oc_normal_gamma"), "")

            # 3) 再次执行 _migrate，验证幂等性
            migrated_store._migrate()
            self.assertEqual(migrated_store.get_meta("migrated_topic_group_modes"), "1")
            self.assertEqual(migrated_store.get_meta("chat_mode:oc_topic_alpha"), "topic")
        finally:
            migrated_store.close()

    # 10. 插话批次（Priority Dispatch）现场段渲染与游标绑定推进测试
    def test_priority_dispatch_ambient_context_and_cursor_advance(self):
        """测试插话批次（running 状态下主人插话）：
        1) 期间有第三方同事发言时，提示词包含 Alice 发言现场，且批次完成后游标推进；
        2) 无现场段时，提示词不含环境段，且阅读位置严格不前移。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Dispatcher

        owner_open_id = "ou_owner_matthew"
        cfg = Config(
            self_open_id=owner_open_id,
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        cfg.playbook_dir = str(Path(__file__).parent / "fixtures" / "playbook")
        cfg.session.owner_priority = True

        roster_mock = MagicMock()
        roster_mock.by_topic.return_value = None
        roster_mock.vips.return_value = {}
        roster_mock.owner_of.return_value = None
        fake_sink = MagicMock(busy=lambda cid: "")
        disp = Dispatcher(cfg, self.store, {"antigravity": fake_sink}, roster_mock, None)

        sent_prompts = []
        disp._send = lambda topic, prompt, batch_id=None: sent_prompts.append((batch_id, prompt))

        # 场景 1：期间有第三方同事（Alice）发言
        topic_id = "tp_prio"
        chat_id = "oc_prio_chat"
        self.store.create_topic(topic_id, "插话全景测试会话", "adopted", "插话职责", "running")
        self.store.update_topic(
            topic_id,
            conversation_id="cid_prio",
            pending_batch_id="b_running_01",
            onboarded_at="2025-11-17T11:45:07+08:00",
        )

        # 写入历史基线并初始化游标
        self._insert_msg("om_prio_base", chat_id=chat_id, content="历史基线 0")
        self.store.get_ambient_since_cursor(topic_id, "chat", chat_id)
        base_cur = self.store.get_ambient_cursor(topic_id, "chat", chat_id)["last_rowid"]

        # 会话运行期间进来的第三方同事发言
        m_alice = self._insert_msg(
            "om_prio_alice",
            chat_id=chat_id,
            content="Alice: 生产环境接口耗时突增",
            sender_id="ou_alice",
            sender_name="Alice",
            route="inbox",
            reason="inbox",
        )

        # 随后主人插话
        m_owner = self._insert_msg(
            "om_prio_owner",
            chat_id=chat_id,
            content="主人: 立即执行流量旁路与降级",
            sender_id=owner_open_id,
            sender_name="Matthew Hong",
            route="dispatch",
            reason="owner_instruction",
        )

        # 触发调度
        t = self.store.get_topic(topic_id)
        res = disp._dispatch_topic(t, [m_owner])
        self.assertEqual(len(sent_prompts), 1)
        bid, prompt_text = sent_prompts[0]
        self.assertTrue(bid.startswith("p"))

        # 断言插话批次提示词包含 Alice 的现场发言
        self.assertIn("🌐 期间环境里动态（自上次处理以来的讨论）：", prompt_text)
        self.assertIn("Alice: 生产环境接口耗时突增", prompt_text)
        self.assertIn("主人: 立即执行流量旁路与降级", prompt_text)

        # 断言游标已正确推进
        cur_after = self.store.get_ambient_cursor(topic_id, "chat", chat_id)["last_rowid"]
        self.assertGreaterEqual(cur_after, m_alice["rowid"])
        self.assertEqual(cur_after, m_owner["rowid"])

        # 断言 dispatches 记录中记录了 cursors_json
        d_rec = self.store.get_dispatch(bid)
        self.assertIsNotNone(d_rec)
        self.assertTrue(bool(d_rec["cursors_json"]))

        # 场景 2：无现场段（期间无任何第三方消息），断言阅读位置严格不前移
        sent_prompts.clear()
        topic_id_2 = "tp_prio_no_ambient"
        chat_id_2 = "oc_prio_no_amb"
        self.store.create_topic(topic_id_2, "无现场插话会话", "adopted", "插话职责", "running")
        self.store.update_topic(
            topic_id_2,
            conversation_id="cid_prio_2",
            pending_batch_id="b_running_02",
            onboarded_at="2025-11-17T11:45:07+08:00",
        )

        # 写入历史基线并初始化游标
        m_base2 = self._insert_msg("om_prio_base2", chat_id=chat_id_2, content="历史基线 2")
        self.store.get_ambient_since_cursor(topic_id_2, "chat", chat_id_2)
        cur_before2 = self.store.get_ambient_cursor(topic_id_2, "chat", chat_id_2)["last_rowid"]
        self.assertEqual(cur_before2, m_base2["rowid"])

        # 期间无第三方发言，仅主人插话
        m_owner2 = self._insert_msg(
            "om_prio_owner2",
            chat_id=chat_id_2,
            content="主人: 继续保持观察",
            sender_id=owner_open_id,
            sender_name="Matthew Hong",
            route="dispatch",
            reason="owner_instruction",
        )

        t2 = self.store.get_topic(topic_id_2)
        res2 = disp._dispatch_topic(t2, [m_owner2])
        self.assertEqual(len(sent_prompts), 1)
        bid2, prompt_text2 = sent_prompts[0]
        self.assertTrue(bid2.startswith("p"))

        # 断言无现场段
        self.assertNotIn("🌐 期间环境里动态", prompt_text2)
        self.assertNotIn("🌐 全景上下文", prompt_text2)

        # 断言阅读位置严格不前移
        cur_after2 = self.store.get_ambient_cursor(topic_id_2, "chat", chat_id_2)["last_rowid"]
        self.assertEqual(cur_after2, cur_before2)

        # 断言 dispatches 记录中 cursors_json 为空
        d_rec2 = self.store.get_dispatch(bid2)
        self.assertIsNotNone(d_rec2)
        self.assertIsNone(d_rec2["cursors_json"])

    # 11. 新话题群模式从飞书群信息自动探测测试
    def test_topic_group_mode_auto_detected_from_source(self):
        """测试新话题群模式自动探测：
        构造一个新群，第一条消息 chat_type='group' 但带有 thread_id；
        mock source.chat_mode 返回 'topic'；
        验证 _resolve_ambient_scope 成功探测并持久化记录 chat_mode='topic'，
        且该消息被正确约束在 ('thread', thread_id)，绝不退化为整群范围。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Dispatcher

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        fake_sink = MagicMock(busy=lambda cid: "")
        roster_mock = MagicMock()
        roster_mock.by_topic.return_value = None
        roster_mock.vips.return_value = {}
        roster_mock.owner_of.return_value = None

        chat_id = "oc_brand_new_topic_chat"
        # 初始状态下该群无 meta 记录
        self.assertEqual(self.store.get_meta(f"chat_mode:{chat_id}"), "")

        mock_source = MagicMock()
        mock_source.chat_mode.return_value = "topic"

        disp = Dispatcher(cfg, self.store, {"antigravity": fake_sink}, roster_mock, None, source=mock_source)

        # 构造一条新群消息：chat_type='group'，但带有 thread_id
        msg = {
            "message_id": "om_new_topic_msg_1",
            "chat_id": chat_id,
            "chat_type": "group",
            "thread_id": "th_brand_new_001",
            "content": "新话题第一条消息",
        }

        # 第一次解析：触发探测
        scope = disp._resolve_ambient_scope(msg)

        # 验证调用了 source.chat_mode
        mock_source.chat_mode.assert_called_once_with(chat_id)

        # 验证已持久化记录到 meta
        self.assertEqual(self.store.get_meta(f"chat_mode:{chat_id}"), "topic")

        # 验证被正确约束在 ('thread', thread_id)，绝不退化为整群范围
        self.assertEqual(scope, ("thread", "th_brand_new_001"))

        # 第二次解析：应直接读取 meta 缓存，不应再重复调用 source.chat_mode
        scope2 = disp._resolve_ambient_scope(msg)
        self.assertEqual(mock_source.chat_mode.call_count, 1)
        self.assertEqual(scope2, ("thread", "th_brand_new_001"))

    # 12. 第三方消息读后编辑：追加现场变更记录并自然拉取
    def test_ambient_change_event_on_edit(self):
        """第三方消息读后编辑：
        1. 插入第三方（Alice）消息与触发消息，调度第 1 批，会话读取该消息，游标推进至触发消息 rowid；
        2. Alice 对该消息进行编辑，调用 _handle_change；
        3. 断言 messages 中生成了 reason='ambient_change' 且拥有更大 rowid 的记录；
        4. 触发第 2 批调度，断言全景现场段实际包含 Alice 的编辑变更记录。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Collector, Dispatcher
        from sheepdog.router import RouteContext

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        cfg.playbook_dir = str(Path(__file__).parent / "fixtures" / "playbook")
        roster_mock = MagicMock()
        roster_mock.by_topic.return_value = None
        roster_mock.vips.return_value = {}
        roster_mock.owner_of.return_value = None
        fake_sink = MagicMock(busy=lambda cid: "")
        disp = Dispatcher(cfg, self.store, {"antigravity": fake_sink}, roster_mock, None)
        coll = Collector(cfg, self.store, MagicMock(), roster_mock)

        sent_prompts = []
        disp._send = lambda topic, prompt, batch_id=None: sent_prompts.append((batch_id, prompt))

        chat_id = "oc_chat_edit_e2e"
        topic_id = "tp_test_edit"
        self.store.create_topic(topic_id, "编辑感知会话", "adopted", "测试职责", "idle")
        self.store.update_topic(topic_id, conversation_id="cid_edit", onboarded_at="2025-11-17T11:45:07+08:00")

        # 1) 初始化基线
        self._insert_msg("om_edit_base", chat_id, "历史基线")
        self.store.get_ambient_since_cursor(topic_id, "chat", chat_id)

        # 2) 插入 Alice 原始消息与触发消息
        alice_msg = self._insert_msg(
            "om_alice_001", chat_id, "原始方案：方案A",
            sender_id="ou_alice", sender_name="Alice", route="inbox", reason="at_me"
        )
        trig1 = self._insert_msg(
            "om_trig_edit1", chat_id, "@羊 第一轮任务",
            sender_id="ou_bob", sender_name="Bob", route="dispatch", reason="at_me"
        )

        # 执行第 1 批调度
        t = self.store.get_topic(topic_id)
        disp._dispatch_topic(t, [trig1])
        self.assertEqual(len(sent_prompts), 1)
        _, prompt1 = sent_prompts[0]
        self.assertIn("原始方案：方案A", prompt1)

        # 游标已前进到 trig1 的 rowid（严格大于 alice_msg rowid）
        cur1 = self.store.get_ambient_cursor(topic_id, "chat", chat_id)["last_rowid"]
        self.assertGreaterEqual(cur1, trig1["rowid"])
        self.assertGreater(cur1, alice_msg["rowid"])

        # 3) Alice 在读后编辑了消息
        alice_edit = Message(
            message_id="om_alice_001",
            chat_id=chat_id,
            chat_name="Test Chat",
            chat_type="group",
            sender_id="ou_alice",
            sender_name="Alice",
            sender_type="user",
            content="原始方案：修改为方案B",
            msg_type="text",
            create_time="2025-11-17T11:52:06+08:00",
        )
        ctx = RouteContext(cfg.self_open_id, set(), lambda mid: False, coll.bot_ids)
        changed = coll._handle_change(self.store.get_message("om_alice_001"), alice_edit, ctx)
        self.assertTrue(changed)

        # 断言 store 中已生成 ambient_change 追加事件
        change_row = self.store.conn.execute(
            "SELECT rowid, * FROM messages WHERE reason='ambient_change' AND sender_id='ou_alice'"
        ).fetchone()
        self.assertIsNotNone(change_row)
        self.assertGreater(change_row["rowid"], cur1, "追加事件的物理 rowid 必须严格大于当前阅读位置")
        self.assertIn("（编辑了消息）旧：原始方案：方案A\n新：原始方案：修改为方案B", change_row["content"])

        # 4) 触发第 2 批调度，断言全景现场段自然包含 Alice 的编辑变更记录
        sent_prompts.clear()
        self.store.update_topic(topic_id, state="idle")
        t = self.store.get_topic(topic_id)
        trig2 = self._insert_msg(
            "om_trig_edit2", chat_id, "@羊 第二轮任务",
            sender_id="ou_bob", sender_name="Bob", route="dispatch", reason="at_me"
        )
        disp._dispatch_topic(t, [trig2])
        self.assertEqual(len(sent_prompts), 1)
        _, prompt2 = sent_prompts[0]
        self.assertIn("🌐 全景上下文（自上次处理以来的环境动态，按发生顺序排列）：", prompt2)
        self.assertIn("Alice: （编辑了消息）旧：原始方案：方案A\n新：原始方案：修改为方案B", prompt2)

    # 13. 第三方消息读后撤回：追加现场变更记录并自然拉取
    def test_ambient_change_event_on_recall(self):
        """第三方消息读后撤回：
        1. 插入第三方（Alice）消息并推进游标；
        2. Alice 随后撤回该消息（deleted=1 并调用 _handle_change）；
        3. 断言 messages 中生成 reason='ambient_change' 且 content 为撤回说明的记录；
        4. 触发下一批调度，断言全景现场段实际包含撤回记录。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Collector, Dispatcher
        from sheepdog.router import RouteContext

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        cfg.playbook_dir = str(Path(__file__).parent / "fixtures" / "playbook")
        roster_mock = MagicMock()
        roster_mock.by_topic.return_value = None
        roster_mock.vips.return_value = {}
        roster_mock.owner_of.return_value = None
        fake_sink = MagicMock(busy=lambda cid: "")
        disp = Dispatcher(cfg, self.store, {"antigravity": fake_sink}, roster_mock, None)
        coll = Collector(cfg, self.store, MagicMock(), roster_mock)

        sent_prompts = []
        disp._send = lambda topic, prompt, batch_id=None: sent_prompts.append((batch_id, prompt))

        chat_id = "oc_chat_recall_e2e"
        topic_id = "tp_test_recall"
        self.store.create_topic(topic_id, "撤回感知会话", "adopted", "测试职责", "idle")
        self.store.update_topic(topic_id, conversation_id="cid_recall", onboarded_at="2025-11-17T11:45:07+08:00")

        # 1) 初始化基线并推进游标
        self._insert_msg("om_rec_base", chat_id, "历史基线")
        self.store.get_ambient_since_cursor(topic_id, "chat", chat_id)

        alice_msg = self._insert_msg(
            "om_alice_rec_001", chat_id, "这有一份机密财务报告",
            sender_id="ou_alice", sender_name="Alice", route="inbox", reason="at_me"
        )
        trig1 = self._insert_msg(
            "om_trig_rec1", chat_id, "@羊 第一轮任务",
            sender_id="ou_bob", sender_name="Bob", route="dispatch", reason="at_me"
        )

        t = self.store.get_topic(topic_id)
        disp._dispatch_topic(t, [trig1])
        cur1 = self.store.get_ambient_cursor(topic_id, "chat", chat_id)["last_rowid"]
        self.assertGreaterEqual(cur1, trig1["rowid"])

        # 2) Alice 撤回消息
        alice_recall = Message(
            message_id="om_alice_rec_001",
            chat_id=chat_id,
            chat_name="Test Chat",
            chat_type="group",
            sender_id="ou_alice",
            sender_name="Alice",
            sender_type="user",
            content="",
            msg_type="text",
            deleted=True,
            create_time="2025-11-17T11:52:06+08:00",
        )
        ctx = RouteContext(cfg.self_open_id, set(), lambda mid: False, coll.bot_ids)
        changed = coll._handle_change(self.store.get_message("om_alice_rec_001"), alice_recall, ctx)
        self.assertTrue(changed)

        # 断言原记录原地更新为 deleted=1
        orig_row = self.store.get_message("om_alice_rec_001")
        self.assertEqual(orig_row["deleted"], 1)

        # 断言追加了带有更大 rowid 的撤回变更事件
        change_row = self.store.conn.execute(
            "SELECT rowid, * FROM messages WHERE reason='ambient_change' AND sender_id='ou_alice'"
        ).fetchone()
        self.assertIsNotNone(change_row)
        self.assertGreater(change_row["rowid"], cur1)
        self.assertIn("（撤回了一条消息）原文：这有一份机密财务报告", change_row["content"])

        # 3) 触发下一批调度，断言全景现场段实际包含撤回记录
        sent_prompts.clear()
        self.store.update_topic(topic_id, state="idle")
        t = self.store.get_topic(topic_id)
        trig2 = self._insert_msg(
            "om_trig_rec2", chat_id, "@羊 第二轮任务",
            sender_id="ou_bob", sender_name="Bob", route="dispatch", reason="at_me"
        )
        disp._dispatch_topic(t, [trig2])
        self.assertEqual(len(sent_prompts), 1)
        _, prompt2 = sent_prompts[0]
        self.assertIn("🌐 全景上下文（自上次处理以来的环境动态，按发生顺序排列）：", prompt2)
        self.assertIn("Alice: （撤回了一条消息）原文：这有一份机密财务报告", prompt2)

    # 14. 机器人卡片刷新防刷屏：原地更新，绝不追加现场变更记录
    def test_bot_card_refresh_no_ambient_change(self):
        """机器人卡片刷新防刷屏测试：
        机器人发送 interactive 卡片并频繁更新内容，断言未产生任何 ambient_change 追加记录。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Collector
        from sheepdog.router import RouteContext

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        coll = Collector(cfg, self.store, MagicMock())
        coll.bot_ids = frozenset(["ou_bot_app_01"])

        chat_id = "oc_chat_card_refresh"
        # 1) 机器人入账一条 interactive 卡片消息
        self._insert_msg(
            "om_card_msg_01",
            chat_id,
            content="[卡片] 执行进度 10%",
            sender_id="ou_bot_app_01",
            sender_name="Sheepdog Bot",
            sender_type="app",
            msg_type="interactive",
            route="inbox",
        )

        ctx = RouteContext(cfg.self_open_id, set(), lambda mid: False, coll.bot_ids)

        # 2) 机器人频繁刷新进度条
        for progress in ("30%", "70%", "100%"):
            card_upd = Message(
                message_id="om_card_msg_01",
                chat_id=chat_id,
                chat_name="Test Chat",
                chat_type="group",
                sender_id="ou_bot_app_01",
                sender_name="Sheepdog Bot",
                sender_type="app",
                content=f"[卡片] 执行进度 {progress}",
                msg_type="interactive",
                create_time="2025-11-17T11:56:03+08:00",
            )
            coll._handle_change(self.store.get_message("om_card_msg_01"), card_upd, ctx)

        # 断言 messages 中 ambient_change 记录数严格为 0
        cnt = self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE reason='ambient_change'").fetchone()[0]
        self.assertEqual(cnt, 0, "机器人/卡片刷新绝不应追加 ambient_change 变更记录")

        # 断言原行已被原地更新至最新内容
        final_row = self.store.get_message("om_card_msg_01")
        self.assertEqual(final_row["content"], "[卡片] 执行进度 100%")

    # 15. 私聊绝对不调用 source.chat_mode 探测
    def test_p2p_chat_never_calls_source_chat_mode(self):
        """私聊（chat_type == 'p2p' 或私聊特征）绝对不要调用 source.chat_mode 查，
        私聊的范围本来就是全部 ('chat', cid)。"""
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Dispatcher

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        mock_source = MagicMock()
        disp = Dispatcher(cfg, self.store, {"antigravity": MagicMock()}, None, None, source=mock_source)

        # 1) chat_type == 'p2p'
        p2p_msg1 = {
            "message_id": "om_p2p_1",
            "chat_id": "oc_p2p_chat_001",
            "chat_type": "p2p",
            "content": "私聊第一条",
        }
        scope1 = disp._resolve_ambient_scope(p2p_msg1)
        self.assertEqual(scope1, ("chat", "oc_p2p_chat_001"))
        self.assertEqual(mock_source.chat_mode.call_count, 0, "私聊绝对不得调用 source.chat_mode")

        # 2) cid 以 ou_ 开头的私聊特征
        p2p_msg2 = {
            "message_id": "om_p2p_2",
            "chat_id": "ou_user_direct_999",
            "chat_type": "",
            "content": "以 ou_ 开头的用户直连",
        }
        scope2 = disp._resolve_ambient_scope(p2p_msg2)
        self.assertEqual(scope2, ("chat", "ou_user_direct_999"))
        self.assertEqual(mock_source.chat_mode.call_count, 0, "ou_ 直连私聊绝对不得调用 source.chat_mode")

    # 16. 群模式查询失败时单群负缓存，连续多条消息只查一次
    def test_chat_mode_query_failure_negative_caching(self):
        """测试查询失败时（如 429 或网络异常），同一群里接连来了好几条消息，
        断言 source.chat_mode 只被调用一次，后续消息直接命中负缓存记录。"""
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Dispatcher

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        mock_source = MagicMock()
        mock_source.chat_mode.side_effect = Exception("429 Too Many Requests: TAT endpoint rate limited")

        disp = Dispatcher(cfg, self.store, {"antigravity": MagicMock()}, None, None, source=mock_source)

        chat_id = "oc_rate_limited_group_001"
        self.assertEqual(self.store.get_meta(f"chat_mode:{chat_id}"), "")

        # 连续来 3 条该群消息
        for i in range(1, 4):
            msg = {
                "message_id": f"om_fail_cached_{i}",
                "chat_id": chat_id,
                "chat_type": "group",
                "content": f"第 {i} 条群消息",
            }
            scope = disp._resolve_ambient_scope(msg)
            self.assertEqual(scope, ("chat", chat_id))

        # 断言 source.chat_mode 只被调用了一次
        self.assertEqual(mock_source.chat_mode.call_count, 1, "查询失败时必须写入负缓存，绝不每条消息重复调用")
        # 断言负缓存已记入 meta
        self.assertEqual(self.store.get_meta(f"chat_mode:{chat_id}"), "group")

    # 17. 测试 2：已送达 dispatch 不追加现场变更事件
    def test_delivered_dispatch_edit_no_ambient_change(self):
        """测试 2（已送达 dispatch 不追加）：
        原消息为 route=dispatch 且 dispatch_state=delivered，
        编辑后断言未生成任何 ambient_change 记录。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Collector
        from sheepdog.router import RouteContext

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        coll = Collector(cfg, self.store, MagicMock())

        chat_id = "oc_chat_dispatch_test"
        # 插入原 dispatch 消息并标为已送达
        self._insert_msg(
            "om_disp_001",
            chat_id,
            content="原始直推任务",
            sender_id="ou_alice",
            sender_name="Alice",
            route="dispatch",
            reason="at_me",
        )
        with self.store.tx() as c:
            c.execute("UPDATE messages SET dispatch_state='delivered' WHERE message_id='om_disp_001'")

        ctx = RouteContext(cfg.self_open_id, set(), lambda mid: False, coll.bot_ids)
        edit_m = Message(
            message_id="om_disp_001",
            chat_id=chat_id,
            chat_name="Test Chat",
            chat_type="group",
            sender_id="ou_alice",
            sender_name="Alice",
            sender_type="user",
            content="原始直推任务（已修改）",
            msg_type="text",
            create_time="2025-11-17T11:56:03+08:00",
        )
        changed = coll._handle_change(self.store.get_message("om_disp_001"), edit_m, ctx)
        self.assertTrue(changed)

        # 断言未生成任何 ambient_change 记录
        cnt = self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE reason='ambient_change'").fetchone()[0]
        self.assertEqual(cnt, 0, "已送达 dispatch 消息编辑后绝不应追加 ambient_change 变更记录")

        # 原消息按已送达 dispatch 编辑逻辑标为 pending/edited 待重推
        row = self.store.get_message("om_disp_001")
        self.assertEqual(row["reason"], "edited")
        self.assertEqual(row["dispatch_state"], "pending")

    # 18. 测试 3：主人发言 self 不追加现场变更事件
    def test_owner_self_edit_no_ambient_change(self):
        """测试 3（主人发言 self 不追加）：
        sender_id == owner_open_id 的发言编辑后，
        断言未生成任何 ambient_change 记录。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Collector
        from sheepdog.router import RouteContext

        owner_open_id = "ou_owner_matthew"
        cfg = Config(
            self_open_id=owner_open_id,
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        coll = Collector(cfg, self.store, MagicMock())

        chat_id = "oc_chat_owner_group"
        self._insert_msg(
            "om_owner_001",
            chat_id,
            content="主人第一版方案",
            sender_id=owner_open_id,
            sender_name="Matthew Hong",
            route="inbox",
            reason="group",
        )

        ctx = RouteContext(owner_open_id, set(), lambda mid: False, coll.bot_ids)
        edit_m = Message(
            message_id="om_owner_001",
            chat_id=chat_id,
            chat_name="Test Chat",
            chat_type="group",
            sender_id=owner_open_id,
            sender_name="Matthew Hong",
            sender_type="user",
            content="主人第二版方案",
            msg_type="text",
            create_time="2025-11-17T11:56:03+08:00",
        )
        changed = coll._handle_change(self.store.get_message("om_owner_001"), edit_m, ctx)
        self.assertTrue(changed)

        # 断言未生成任何 ambient_change 记录
        cnt = self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE reason='ambient_change'").fetchone()[0]
        self.assertEqual(cnt, 0, "主人发言编辑后绝不应追加 ambient_change 变更记录")

        # 断言原行已被更新内容
        row = self.store.get_message("om_owner_001")
        self.assertEqual(row["content"], "主人第二版方案")

    # 19. 测试 4：drop 消息不追加现场变更事件
    def test_drop_message_edit_no_ambient_change(self):
        """测试 4（drop 消息不追加）：
        静音/忽略群的 drop 消息编辑后，
        断言未生成任何 ambient_change 记录。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Collector
        from sheepdog.router import RouteContext

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        chat_id = "oc_muted_chat_001"
        cfg.routing.ignore_chat_ids.append(chat_id)

        coll = Collector(cfg, self.store, MagicMock())

        self._insert_msg(
            "om_drop_001",
            chat_id,
            content="静音群闲聊消息",
            sender_id="ou_charlie",
            sender_name="Charlie",
            route="drop",
            reason="ignored_chat",
        )

        ctx = RouteContext(cfg.self_open_id, {chat_id}, lambda mid: False, coll.bot_ids)
        edit_m = Message(
            message_id="om_drop_001",
            chat_id=chat_id,
            chat_name="Muted Group",
            chat_type="group",
            sender_id="ou_charlie",
            sender_name="Charlie",
            sender_type="user",
            content="静音群修改后的闲聊消息",
            msg_type="text",
            create_time="2025-11-17T11:56:03+08:00",
        )
        changed = coll._handle_change(self.store.get_message("om_drop_001"), edit_m, ctx)
        self.assertTrue(changed)

        # 断言未生成任何 ambient_change 记录
        cnt = self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE reason='ambient_change'").fetchone()[0]
        self.assertEqual(cnt, 0, "静音群/忽略群 drop 消息编辑后绝不应追加 ambient_change 变更记录")

        row = self.store.get_message("om_drop_001")
        self.assertEqual(row["content"], "静音群修改后的闲聊消息")

    # 20. 测试 5：hold 扣下消息不追加现场变更事件
    def test_security_hold_message_edit_no_ambient_change(self):
        """测试 5（hold 扣下消息不追加）：
        带有 security_action='hold' 的敏感消息编辑后，
        断言未生成任何 ambient_change 记录。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Collector
        from sheepdog.router import RouteContext

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        coll = Collector(cfg, self.store, MagicMock())

        chat_id = "oc_chat_security_test"
        # 1) 原消息被扣下 (old['security_action'] == 'hold')
        self._insert_msg(
            "om_hold_001",
            chat_id,
            content="带有敏感凭据的内容 token=abc",
            sender_id="ou_eve",
            sender_name="Eve",
            route="inbox",
            reason="group",
            security_action="hold",
        )

        ctx = RouteContext(cfg.self_open_id, set(), lambda mid: False, coll.bot_ids)
        edit_m = Message(
            message_id="om_hold_001",
            chat_id=chat_id,
            chat_name="Test Chat",
            chat_type="group",
            sender_id="ou_eve",
            sender_name="Eve",
            sender_type="user",
            content="修改后的凭据内容 token=def",
            msg_type="text",
            create_time="2025-11-17T11:56:03+08:00",
        )
        changed = coll._handle_change(self.store.get_message("om_hold_001"), edit_m, ctx)
        self.assertTrue(changed)

        cnt = self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE reason='ambient_change'").fetchone()[0]
        self.assertEqual(cnt, 0, "原消息带有 security_action='hold' 时编辑后绝不应追加 ambient_change")

        # 2) 新编辑消息被判定或带有 security_action='hold'
        self._insert_msg(
            "om_hold_002",
            chat_id,
            content="普通无害消息",
            sender_id="ou_eve",
            sender_name="Eve",
            route="inbox",
            reason="group",
        )
        edit_m2 = Message(
            message_id="om_hold_002",
            chat_id=chat_id,
            chat_name="Test Chat",
            chat_type="group",
            sender_id="ou_eve",
            sender_name="Eve",
            sender_type="user",
            content="改为了带有凭据的危险消息",
            msg_type="text",
            create_time="2025-11-17T11:56:03+08:00",
        )
        edit_m2.security_action = "hold"
        changed2 = coll._handle_change(self.store.get_message("om_hold_002"), edit_m2, ctx)
        self.assertTrue(changed2)

        cnt2 = self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE reason='ambient_change'").fetchone()[0]
        self.assertEqual(cnt2, 0, "新消息带有 security_action='hold' 时绝不应追加 ambient_change")

    # 21. 测试 6：drop 重新判定为 inbox 不追加现场变更事件（精确守卫 old_route == INBOX）
    def test_drop_to_inbox_edit_no_ambient_change(self):
        """测试 6（守卫 old_route == INBOX）：
        原消息是 drop（如原为忽略/静音群闲聊），
        编辑后重新判定为 inbox（普通群闲聊）：
        断言严格不追加 ambient_change。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Collector
        from sheepdog.router import RouteContext

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        coll = Collector(cfg, self.store, MagicMock())

        chat_id = "oc_chat_drop_to_inbox"
        self._insert_msg(
            "om_drop_to_inbox_001",
            chat_id,
            content="原消息：静音闲聊",
            sender_id="ou_charlie",
            sender_name="Charlie",
            route="drop",
            reason="ignored_chat",
        )

        ctx = RouteContext(cfg.self_open_id, set(), lambda mid: False, coll.bot_ids)
        edit_m = Message(
            message_id="om_drop_to_inbox_001",
            chat_id=chat_id,
            chat_name="Normal Chat",
            chat_type="group",
            sender_id="ou_charlie",
            sender_name="Charlie",
            sender_type="user",
            content="编辑后内容：仍为普通群闲聊（重新判定为 inbox）",
            msg_type="text",
            create_time="2025-11-17T11:56:03+08:00",
        )
        changed = coll._handle_change(self.store.get_message("om_drop_to_inbox_001"), edit_m, ctx)
        self.assertTrue(changed)

        # 断言未生成任何 ambient_change 记录
        cnt = self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE reason='ambient_change'").fetchone()[0]
        self.assertEqual(cnt, 0, "原消息为 drop 重新判定为 inbox 时绝不应追加 ambient_change")

        row = self.store.get_message("om_drop_to_inbox_001")
        self.assertEqual(row["content"], "编辑后内容：仍为普通群闲聊（重新判定为 inbox）")

    # 22. 测试 7：inbox 重新判定为 dispatch 不追加现场变更事件（精确守卫 d.route == INBOX）
    def test_inbox_to_dispatch_edit_no_ambient_change(self):
        """测试 7（守卫 d.route == INBOX）：
        原消息是 inbox（普通群讨论），
        编辑后加入了 @主人，被重新判定成 dispatch：
        断言严格不追加 ambient_change。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Collector
        from sheepdog.models import Mention
        from sheepdog.router import RouteContext

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        coll = Collector(cfg, self.store, MagicMock())

        chat_id = "oc_chat_inbox_to_disp"
        self._insert_msg(
            "om_inbox_to_disp_001",
            chat_id,
            content="原消息：普通群讨论",
            sender_id="ou_bob",
            sender_name="Bob",
            route="inbox",
            reason="group",
        )

        ctx = RouteContext(cfg.self_open_id, set(), lambda mid: False, coll.bot_ids)
        edit_m = Message(
            message_id="om_inbox_to_disp_001",
            chat_id=chat_id,
            chat_name="Test Chat",
            chat_type="group",
            sender_id="ou_bob",
            sender_name="Bob",
            sender_type="user",
            content="@主人 请看一下线上这个报警",
            msg_type="text",
            create_time="2025-11-17T11:56:03+08:00",
            mentions=[Mention("ou_me", "主人")],
        )
        changed = coll._handle_change(self.store.get_message("om_inbox_to_disp_001"), edit_m, ctx)
        self.assertTrue(changed)

        # 断言未生成任何 ambient_change 记录
        cnt = self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE reason='ambient_change'").fetchone()[0]
        self.assertEqual(cnt, 0, "原消息 inbox 编辑后升级为 dispatch 时绝不应追加 ambient_change")

        # 原消息升级为 dispatch pending
        row = self.store.get_message("om_inbox_to_disp_001")
        self.assertEqual(row["route"], "dispatch")
        self.assertEqual(row["reason"], "at_me")
        self.assertEqual(row["dispatch_state"], "pending")

    # 23. 测试 8：机器人/应用卡片消息编辑不追加现场变更事件（精确守卫 not is_bot_or_card_refresh）
    def test_bot_app_message_edit_no_ambient_change(self):
        """测试 8（守卫 not is_bot_or_card_refresh）：
        原消息是 inbox，发送人是机器人（sender_type="app" 或 sender_id 在 bot_ids 中），
        内容改了（普通编辑）：断言严格不追加 ambient_change。
        """
        from unittest.mock import MagicMock
        from sheepdog.config import Config
        from sheepdog.engine import Collector
        from sheepdog.router import RouteContext

        cfg = Config(
            self_open_id="ou_me",
            state_dir=Path(self.tmp_dir.name),
            config_path=Path(self.tmp_dir.name) / "config.toml",
        )
        coll = Collector(cfg, self.store, MagicMock())

        chat_id = "oc_chat_bot_edit"
        self._insert_msg(
            "om_bot_app_001",
            chat_id,
            content="机器人原始卡片消息",
            sender_id="cli_a1b2c3d4",
            sender_name="CI Bot",
            sender_type="app",
            route="inbox",
            reason="group",
        )

        ctx = RouteContext(cfg.self_open_id, set(), lambda mid: False, coll.bot_ids)
        edit_m = Message(
            message_id="om_bot_app_001",
            chat_id=chat_id,
            chat_name="Dev Group",
            chat_type="group",
            sender_id="cli_a1b2c3d4",
            sender_name="CI Bot",
            sender_type="app",
            content="机器人编辑后的卡片内容",
            msg_type="text",
            create_time="2025-11-17T11:56:03+08:00",
        )
        changed = coll._handle_change(self.store.get_message("om_bot_app_001"), edit_m, ctx)
        self.assertTrue(changed)

        cnt = self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE reason='ambient_change'").fetchone()[0]
        self.assertEqual(cnt, 0, "sender_type='app' 机器人消息编辑绝不应追加 ambient_change")

        # 验证 sender_type='bot' 也绝不追加
        self._insert_msg(
            "om_bot_type_002",
            chat_id,
            content="sender_type 为 bot 的原始消息",
            sender_id="ou_bot_user_02",
            sender_name="Bot User",
            sender_type="bot",
            route="inbox",
            reason="group",
        )
        edit_m2 = Message(
            message_id="om_bot_type_002",
            chat_id=chat_id,
            chat_name="Dev Group",
            chat_type="group",
            sender_id="ou_bot_user_02",
            sender_name="Bot User",
            sender_type="bot",
            content="sender_type 为 bot 的编辑后消息",
            msg_type="text",
            create_time="2025-11-17T11:56:03+08:00",
        )
        changed2 = coll._handle_change(self.store.get_message("om_bot_type_002"), edit_m2, ctx)
        self.assertTrue(changed2)

        cnt2 = self.store.conn.execute("SELECT COUNT(*) FROM messages WHERE message_id LIKE 'om_bot_type_002:change:%'").fetchone()[0]
        self.assertEqual(cnt2, 0, "sender_type='bot' 机器人消息编辑绝不应追加 ambient_change")

    # 6. 全景现场段按真实发生先后排序验收测试（REG-163）
    def test_ambient_context_sorted_by_occurrence_time_not_arrival(self):
        """现场段标题写「按发生顺序」，必须按真实发生先后排序，严禁按入账顺序（rowid）。
        故意以相反的入账顺序插入两条消息：
        - 先插入发生时间较晚、pos 59 的消息（rowid=1，Darcy: 收到）
        - 后插入发生时间较早、pos 58 的消息（rowid=2，示例小助手: 请 Darcy 确认）
        验证：
        1. store.get_ambient_since_cursor 返回的 unseen 中，pos 58 严格排在 pos 59 前面；
        2. render_ambient_surrounding_context 渲染出的现场段中，[pos: 58] 严格排在 [pos: 59] 前面；
        3. 同时间戳下优先按 thread_position 升序排；无 thread_position 时按 message_position 升序排。
        """
        chat_id = "oc_chat_sort_test"
        # 基线消息初始化游标
        self._insert_msg("om_base_sort", chat_id, "基线消息", create_time="2025-11-18T07:04:12+08:00")
        self.store.get_ambient_since_cursor(self.topic_id, "chat", chat_id)

        # 1) 先插入发生时间较晚的消息（如 07:10:42，pos 59，Darcy: 收到），其 rowid 较小
        m_late = self._insert_msg(
            "om_late_59",
            chat_id,
            "收到",
            sender_name="Darcy",
            create_time="2025-11-18T07:10:42+08:00",
            thread_position="59",
        )

        # 2) 后插入发生时间较早的消息（如 07:10:15，pos 58，示例小助手: 请 Darcy 确认），其 rowid 较大
        m_early = self._insert_msg(
            "om_early_58",
            chat_id,
            "请 Darcy 确认",
            sender_name="示例小助手",
            create_time="2025-11-18T07:10:15+08:00",
            thread_position="58",
        )

        # 验证入账顺序（rowid）确实是相反的：m_late 的 rowid 小于 m_early
        self.assertLess(m_late["rowid"], m_early["rowid"], "前置断言：入账顺序上 m_late (pos 59) 必须先于 m_early (pos 58)")

        # 3) 拉取未读现场消息
        unseen, target_rowid, overflow = self.store.get_ambient_since_cursor(
            self.topic_id, "chat", chat_id, exclude_msg_ids=set()
        )
        self.assertEqual(len(unseen), 2)
        # 断言列表第一项必须为发生较早的消息（pos 58）
        self.assertEqual(unseen[0]["message_id"], "om_early_58")
        self.assertEqual(unseen[1]["message_id"], "om_late_59")

        # 4) 渲染全景上下文并断言文本顺序
        rendered = render_ambient_surrounding_context(unseen)
        self.assertIn("[pos: 58] 👥 示例小助手: 请 Darcy 确认", rendered)
        self.assertIn("[pos: 59] 👥 Darcy: 收到", rendered)
        idx_58 = rendered.index("[pos: 58]")
        idx_59 = rendered.index("[pos: 59]")
        self.assertLess(idx_58, idx_59, "现场段中发生较早的 pos 58 必须严格排在发生较晚的 pos 59 前面！")

        # 5) 次排序键测试：相同 create_time 下按 thread_position 排序，再按 message_position 排序
        m_same_t2 = {
            "message_id": "om_mpos_10",
            "create_time": "2025-11-18T07:10:15+08:00",
            "message_position": "10",
            "rowid": 100,
        }
        m_same_t1 = {
            "message_id": "om_tpos_5",
            "create_time": "2025-11-18T07:10:15+08:00",
            "thread_position": "5",
            "rowid": 200,
        }
        from sheepdog.store import ambient_event_sort_key
        sorted_sub = sorted([m_same_t2, m_same_t1], key=ambient_event_sort_key)
        self.assertEqual(sorted_sub[0]["message_id"], "om_tpos_5")



if __name__ == "__main__":
    unittest.main()
