"""规格 7.71（SD-771-LAOHU-VIP-UNBLOCK）回归测试：
1. REG-010：可回退的 Cloud Run 环境变量更新/切流与 pre-tool hook 借口伪造 --floor-type destructive 必须拦截；真不可逆删除放行；
2. REG-011：含 VIP 消息的批次在未成功发起带底线 sheepdog ask 时，禁止交 status=needs_decision 回执把会话锁进 waiting_human；
3. REG-012：母羊 transcript 中 invoke_subagent 紧随 GENERIC pre-tool hook 拒绝报错时，预建羔羊当场标 failed；
4. REG-013：羊圈成员处于 running 态时同发送人同私聊后续无引用消息 100% 吸附原成员不脑裂，且同羊圈兄弟槽位可按 --key 协同 resolve。
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sheepdog import cli, outbound
from sheepdog import session as sm
from sheepdog.config import Config, EscalationConfig
from sheepdog.engine import Dispatcher
from sheepdog.roster import parse_roster
from sheepdog.router import route_within_group
from sheepdog.security import SecurityConfig
from sheepdog.sink.agentapi import DryRunSink, transcript_path
from sheepdog.store import Store
from test_roster import msg

VIP_LAOHU = "ou_test_vip_laohu"
LAOHU_P2P = "oc_test_laohu_p2p"
OWNER_DM = "oc_test_owner_dm"
ME = "ou_test_owner"


def _roster_toml() -> str:
    return f"""
[[session]]
key = "laohu"
mode = "managed"
conversation_id = "conv_test_laohu_1"
title = "人·Lao Hu·需求对接"
duty = "负责老胡需求对接"
max_sessions = 2
chats = [{{ chat_id = "{LAOHU_P2P}", name = "老胡私聊", all_messages = true }}]

[[person]]
open_id = "{VIP_LAOHU}"
name = "老胡"
tier = "vip"
authority = "2026-10-21「其实vip想要动生产也是可以的 合理就办」"
""".strip() + "\n"


class Vip771UnblockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(self.root / "app")})
        self.env.start()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.roster_path = self.root / "roster.toml"
        self.roster_path.write_text(_roster_toml(), encoding="utf-8")
        self.roster = parse_roster(tomllib.loads(_roster_toml()))
        self.cfg = Config(
            self_open_id=ME,
            state_dir=self.state_dir,
            roster_path=str(self.roster_path),
            config_path=self.root / "config.toml",
        )
        self.cfg.source.enabled = False
        self.cfg.notify.file = "notices.md"
        self.cfg.escalation = EscalationConfig(
            chat_ids=[OWNER_DM],
            header_regex=r"^🐕 需要你定 · [^\n]*\[(?P<topic>tp_[a-z0-9_#.-]+)\]",
            attribution_regex=r"\[(?P<topic>tp_[a-z0-9_#.-]+)\]",
        )
        self.store = Store(self.cfg.db_path)
        self.sink = DryRunSink()
        self.eng = Dispatcher(self.cfg, self.store, {"antigravity": self.sink}, self.roster, SecurityConfig())
        self.eng.sync_roster()

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_reg_010_reversible_prod_env_update_and_hook_excuse_rejected_as_destructive(self):
        excuse = (
            "生产环境 taplabs-453508 核心服务 app-tapnow-bff 环境变量覆盖更新与全量流量切换，"
            "触及全局生产逐项指名授权红线与宿主安全钩子拦截"
        )
        self.assertFalse(outbound._is_valid_vip_floor(excuse, "destructive"))

        mid = "om_test_vip_771_10"
        self.store.upsert_message(
            msg(message_id=mid, chat_id=LAOHU_P2P, chat_type="p2p", sender_id=VIP_LAOHU, content="请更新生产 BFF 环境变量"),
            "dispatch",
            "p2p",
            [],
            "tp_laohu",
            "delivered",
        )
        with self.assertRaises(outbound.OutboundError) as cm:
            outbound.check_vip_floor(
                self.cfg,
                self.store,
                "tp_laohu",
                [mid],
                {VIP_LAOHU: "老胡"},
                excuse,
                floor_type="destructive",
            )
        self.assertEqual(cm.exception.code, outbound.EXIT_INVALID)

        real_delete = "老胡要求执行 gcloud run services delete 删除生产服务"
        self.assertTrue(outbound._is_valid_vip_floor(real_delete, "destructive"))

    def test_reg_011_vip_batch_needs_decision_receipt_without_floor_ask_rejected(self):
        mid = "om_test_vip_771_11"
        bid = "b20261022193000-laohu"
        self.store.upsert_message(
            msg(message_id=mid, chat_id=LAOHU_P2P, chat_type="p2p", sender_id=VIP_LAOHU, content="帮我把生产 BFF 加上新配置"),
            "dispatch",
            "p2p",
            [],
            "tp_laohu",
        )
        self.store.record_dispatch(bid, "tp_laohu", [mid])
        self.store.mark_batch([mid], bid, "tp_laohu", "delivered")
        self.store.update_topic("tp_laohu", state=sm.RUNNING, pending_batch_id=bid)

        # 会话只调了 sheepdog report（或历史残留了伪底线 ask），严禁交 status=needs_decision 把会话挂成 waiting_human
        self.store.add_send("sda-legacy-fake", "ask", "tp_laohu", "om_test_legacy_fake_ask", "🐕 需要你定")
        self.store.set_send_floor("om_test_legacy_fake_ask", "生产环境环境变量覆盖更新与全量流量切换，触及宿主安全钩子拦截")
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with mock.patch("sheepdog.cli._load_cfg", return_value=self.cfg), \
                redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc_rep = cli.main([
                "report",
                "--topic", "tp_laohu",
                "--title", "生产配置报备",
                "--text", "正在核查生产配置",
            ])
            self.assertEqual(rc_rep, 0, (out_buf.getvalue(), err_buf.getvalue()))
            rc = cli.main([
                "receipt",
                "--topic", "tp_laohu",
                "--batch", bid,
                "--json", '{"status":"needs_decision","summary":"等主人拍板"}',
            ])
        self.assertEqual(rc, 2, (out_buf.getvalue(), err_buf.getvalue()))
        self.assertNotEqual(self.store.get_topic("tp_laohu")["state"], sm.WAITING_HUMAN)
        self.assertEqual(self.store.get_topic("tp_laohu")["state"], sm.RUNNING)
        self.assertFalse((self.cfg.receipts_dir / f"{bid}.json").exists())

    def test_reg_012_pre_tool_hook_denied_subagent_immediately_marked_failed(self):
        tp = transcript_path("conv_test_laohu_1")
        tp.parent.mkdir(parents=True, exist_ok=True)
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        steps = [
            {
                "step_index": 10,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": now_str,
                "tool_calls": [
                    {
                        "name": "invoke_subagent",
                        "args": {
                            "TaskTitle": json.dumps("更新生产 BFF 环境变量"),
                            "Task": json.dumps("执行 gcloud run services update"),
                        },
                    }
                ],
            },
            {
                "step_index": 11,
                "source": "SYSTEM",
                "type": "GENERIC",
                "status": "ERROR",
                "created_at": now_str,
                "content": (
                    "Encountered error in step execution: tool call denied by pre-tool hook: "
                    "Command blocked by Conseca policy"
                ),
            },
        ]
        tp.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in steps) + "\n", encoding="utf-8")

        self.eng._scan_lambs()
        all_lambs = self.store.list_lambs("tp_laohu")
        self.assertEqual(len(all_lambs), 1)
        self.assertEqual(all_lambs[0]["state"], "failed")
        self.assertEqual(self.store.get_active_lambs("tp_laohu"), [])

    def test_reg_013_running_affinity_prevents_p2p_split_and_sibling_resolve_works(self):
        # 1. tp_laohu 正在 running 处理老胡私聊消息，且 tp_laohu#2 空闲存在
        self.store.create_topic(
            "tp_laohu#2", "人·Lao Hu·需求对接 #2", "adopted", "负责老胡需求对接", sm.ACTIVE, group_id="laohu"
        )
        m1 = msg(
            message_id="om_test_laohu_m1",
            chat_id=LAOHU_P2P,
            chat_type="p2p",
            sender_id=VIP_LAOHU,
            content="生产环境也需要加上",
        )
        self.store.upsert_message(m1, "dispatch", "p2p", [], "tp_laohu", "delivered")
        self.store.record_dispatch("b20261022181000-laohu", "tp_laohu", ["om_test_laohu_m1"])
        self.store.update_topic("tp_laohu", state=sm.RUNNING, pending_batch_id="b20261022181000-laohu")

        for follow_text in ("补充配置好了喊我", "转人工 加急"):
            with self.subTest(follow_text=follow_text):
                aff = self.store.find_recent_thread_topic_in_group(
                    "laohu", chat_id=LAOHU_P2P, sender_id=VIP_LAOHU
                )
                self.assertEqual(aff, "tp_laohu")
                routed = route_within_group(
                    self.store,
                    self.roster,
                    "tp_laohu",
                    chat_id=LAOHU_P2P,
                    sender_id=VIP_LAOHU,
                )
                self.assertEqual(routed, "tp_laohu")

        # 2. tp_laohu 发起的 --key 提问，同羊圈兄弟槽位 tp_laohu#2 调用 outbound.resolve 可成功协同结案
        key = "prod-bff-cinemap-external-apps"
        ask_mid = "om_test_laohu_esc_1"
        nk = outbound.duty.norm_key(key)
        self.store.add_thread(ask_mid, "tp_laohu", key, nk, "生产 BFF 变更", outbound.ASK, "omt_test_laohu_1")
        self.store.add_send(
            outbound.idem_key(outbound.ASK, "tp_laohu", key),
            outbound.ASK,
            "tp_laohu",
            ask_mid,
            "🐕 需要你定 · 生产 BFF 变更 [tp_laohu]\n请确认",
            root_message_id=ask_mid,
            key=key,
            norm_key=nk,
        )
        self.store.add_escalation(
            ask_mid,
            "tp_laohu",
            OWNER_DM,
            "🐕 需要你定 · 生产 BFF 变更 [tp_laohu]\n请确认",
            datetime.now(timezone.utc).isoformat(),
        )
        self.store.add_thread("om_test_laohu2_rep_1", "tp_laohu#2", key, nk, "进度汇报", outbound.REPORT, "omt_test_laohu_2")

        res = outbound.resolve(
            self.cfg,
            self.store,
            None,
            "tp_laohu#2",
            "已办妥",
            key=key,
        )
        self.assertEqual(res.closed, [ask_mid])
        esc_after = self.store.get_escalation(ask_mid)
        self.assertEqual(esc_after["close_reason"], "resolved")
        self.assertEqual(esc_after["resolved_by"], "tp_laohu#2")


if __name__ == "__main__":
    unittest.main()
