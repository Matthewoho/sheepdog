from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from sheepdog import __version__, admin_web, card_callback, evolve, loops, parse_version_tuple
from sheepdog.config import Config, EscalationConfig, SourceConfig
from sheepdog.store import Store


class SheepdogLoopsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(
            self_open_id="ou_owner",
            state_dir=self.root / "state",
            config_path=self.root / "config.toml",
            roster_path=str(self.root / "roster.toml"),
            security_path=str(self.root / "security.toml"),
            loops_dir_path=str(self.root / "loops"),
            escalation=EscalationConfig(chat_ids=["oc_owner_p2p"], header_regex=r"\[(?P<topic>tp_[^\]]+)\]"),
            source=SourceConfig(enabled=True),
        )
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.root / "roster.toml").write_text(
            """
[[topic]]
id = "tp_release"
title = "生产环境常规发版"
duty = "负责生产发版"

[[topic]]
id = "tp_cost-ops"
title = "多云基建与成本"
duty = "负责云成本与算力"

[[topic]]
id = "tp_net-ops"
title = "VPS 专线运维"
duty = "负责 多台 VPS 与跳板机白名单"

[[topic]]
id = "tp_cert-ops"
title = "SSL 证书生命周期"
duty = "负责多云证书续期"

[[topic]]
id = "tp_migrate-ops"
title = "腾讯云迁移"
duty = "负责一批服务迁移"
""".strip()
            + "\n",
            encoding="utf-8",
        )
        self.store = Store(self.cfg.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_reg_089_loop_spec_five_slots_and_templates(self) -> None:
        self.assertGreaterEqual(parse_version_tuple(__version__), (1, 1, 0))
        skill_path = loops.ensure_loop_intake_skill(self.cfg)
        self.assertTrue(skill_path.is_file())
        self.assertIn("sheepdog-loop-intake", skill_path.read_text(encoding="utf-8"))

        recs = loops.recommend_loops(self.cfg, self.store)
        self.assertEqual(len(recs), 6)
        rec_ids = {r["id"] for r in recs}
        self.assertEqual(
            rec_ids,
            {
                "release-pilot-template",
                "spend-steward",
                "service-release-guardian",
                "vps-node-and-whitelist-steward",
                "ssl-cert-lifecycle-steward",
                "env-migration-daily-steward",
            },
        )

        for lid in rec_ids:
            spec = loops.init_loop(self.cfg, self.store, lid, template=lid)
            self.assertIsNotNone(spec.path)
            assert spec.path is not None and spec.memory_path is not None
            self.assertTrue(spec.path.is_file())
            self.assertTrue(spec.memory_path.is_file())
            reloaded = loops.load_loop_spec(spec.path)
            self.assertEqual(reloaded.id, lid)
            self.assertEqual(reloaded.autonomy, "shadow")
            self.assertGreaterEqual(len(reloaded.stages), 4)
            self.assertGreaterEqual(len(reloaded.plan), 3)
            self.assertIn("## 1.", reloaded.body)

    def test_reg_089_zero_token_preflight_dedup_and_memory_exempt(self) -> None:
        # 1. Empty load_cmd -> 0 token / status='empty'
        loops.init_loop(
            self.cfg,
            self.store,
            "spend-steward",
            template="spend-steward",
            load_cmd="printf '[]\\n'",
        )

        res_empty = loops.run_loop_tick(self.cfg, self.store, "spend-steward", force=True)
        self.assertTrue(res_empty["triggered"])
        self.assertEqual(res_empty["status"], "empty")
        self.assertEqual(res_empty["token_cost"], 0)
        self.assertEqual(res_empty["newly_added"], 0)

        # 2. Load outputs 2 cost anomalies -> promote to confirm -> dispatches to session
        payload = json.dumps(
            [
                {"key": "gcp-sql-idle-01", "title": "托管数据库 staging 实例 CPU < 3%", "summary": "连续 7 天低利用率"},
                {"key": "cvm-gpu-reserve-02", "title": "腾讯云 GPU 预留节点空转", "summary": "演练压测预留实例"},
            ],
            ensure_ascii=False,
        )
        loops.promote_loop(self.cfg, self.store, "spend-steward", "confirm")
        loops.init_loop(
            self.cfg,
            self.store,
            "spend-steward",
            template="spend-steward",
            autonomy="confirm",
            load_cmd=f"printf '%s\\n' '{payload}'",
            overwrite=True,
        )

        res_hit = loops.run_loop_tick(self.cfg, self.store, "spend-steward", force=True)
        self.assertTrue(res_hit["triggered"])
        self.assertEqual(res_hit["status"], "dispatched")
        self.assertEqual(res_hit["newly_added"], 2)
        self.assertEqual(res_hit["due_items_count"], 2)

        # 3. Exempt cvm-gpu-reserve-02 -> writes to MEMORY.md and marks exempted
        upd = loops.update_loop_item(
            self.cfg,
            self.store,
            "spend-steward",
            "cvm-gpu-reserve-02",
            status="exempted",
            note="演练压测常驻预留实例，老板特批不缩容",
        )
        self.assertEqual(upd["status"], "exempted")
        spec = loops.get_loop_spec(self.cfg, self.store, "spend-steward")
        assert spec is not None and spec.memory_path is not None
        mem_txt = spec.memory_path.read_text(encoding="utf-8")
        self.assertIn("cvm-gpu-reserve-02", mem_txt)
        self.assertIn("演练压测常驻预留实例", mem_txt)

        # Close gcp-sql-idle-01 as well
        loops.update_loop_item(
            self.cfg,
            self.store,
            "spend-steward",
            "gcp-sql-idle-01",
            stage="closed",
            status="closed",
            note="已完成降配并观察 24h 零报错",
        )

        # 4. Re-running with the exact same load output produces 0 new items and status='empty' (0 token)!
        res_dedup = loops.run_loop_tick(self.cfg, self.store, "spend-steward", force=True)
        self.assertEqual(res_dedup["status"], "empty")
        self.assertEqual(res_dedup["newly_added"], 0)
        self.assertEqual(res_dedup["active_items_count"], 0)

    def test_reg_089_multi_stage_item_lifecycle_and_adaptive_cadence(self) -> None:
        loops.init_loop(
            self.cfg,
            self.store,
            "service-release-guardian",
            template="service-release-guardian",
            autonomy="confirm",
            load_cmd="printf '[]\\n'",
        )

        # Initially 0 active items -> effective interval is 15m
        ov0 = loops.build_loops_overview(self.cfg, self.store)
        loop0 = next(x for x in ov0["loops"] if x["id"] == "service-release-guardian")
        self.assertEqual(loop0["effective_interval_minutes"], 15.0)

        # Inject 1 post-release regression item
        res1 = loops.run_loop_tick(
            self.cfg,
            self.store,
            "service-release-guardian",
            force=True,
            injected_items=[
                {
                    "key": "svc-alpha-v2.46-5xx",
                    "title": "svc-alpha-server 上线后 5xx 升至 2.1%",
                }
            ],
        )
        self.assertEqual(res1["status"], "dispatched")
        self.assertEqual(res1["newly_added"], 1)

        # Active item exists -> effective interval automatically speeds up to 5m (active_interval_minutes)!
        ov1 = loops.build_loops_overview(self.cfg, self.store)
        loop1 = next(x for x in ov1["loops"] if x["id"] == "service-release-guardian")
        self.assertEqual(loop1["active_items_count"], 1)
        self.assertEqual(loop1["effective_interval_minutes"], 5.0)

        # Advance item stage to testing -> awaiting_approval -> post_release_watch
        loops.update_loop_item(
            self.cfg,
            self.store,
            "service-release-guardian",
            "svc-alpha-v2.46-5xx",
            stage="testing_verified",
            note="已在本地定位空指针并部署至 acme-testing-env 验证通过",
        )
        # Even if load_cmd returns [] on the next tick, the loop STILL dispatches to follow up the in-flight item!
        res2 = loops.run_loop_tick(self.cfg, self.store, "service-release-guardian", force=True)
        self.assertEqual(res2["status"], "dispatched")
        self.assertEqual(res2["newly_added"], 0)
        self.assertEqual(res2["active_items_count"], 1)

    def test_reg_089_autonomy_promotion_card_callback_and_autopause_breaker(self) -> None:
        loops.init_loop(
            self.cfg,
            self.store,
            "vps-node-and-whitelist-steward",
            template="vps-node-and-whitelist-steward",
            autonomy="shadow",
            load_cmd='printf \'[{"key":"vps-hk-03-down","title":"庚城 03 节点 Xray 探针超时"}]\'',
        )

        fake_source = MagicMock()
        fake_source.send_to_chat.return_value = "om_loop_shadow_1"

        res_shadow = loops.run_loop_tick(
            self.cfg,
            self.store,
            "vps-node-and-whitelist-steward",
            source=fake_source,
            force=True,
        )
        self.assertEqual(res_shadow["status"], "shadow_preview")
        self.assertEqual(res_shadow["message_id"], "om_loop_shadow_1")
        fake_source.send_to_chat.assert_called_once()

        # Simulate owner clicking [🚀 升为拍板模式 (confirm)] on the Feishu shadow preview card
        event_payload = {
            "header": {"event_id": "evt_loop_promote_001", "event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_owner"},
                "context": {"open_message_id": "om_loop_shadow_1", "open_chat_id": "oc_owner_p2p"},
                "action": {
                    "tag": "button",
                    "value": {
                        "sd_cb": "1",
                        "sd_action": "loop_promote",
                        "loop_id": "vps-node-and-whitelist-steward",
                        "target_autonomy": "confirm",
                        "topic_id": "tp_net-ops",
                    },
                },
            },
        }
        cb_msg = card_callback.process_card_action_event(
            event_payload,
            cfg=self.cfg,
            store=self.store,
            source=fake_source,
        )
        self.assertIsNotNone(cb_msg)
        assert cb_msg is not None
        self.assertIn("升为 confirm", cb_msg.content)
        fake_source.patch_card.assert_called_once()

        # Verify LOOP.md and DB state are now confirm
        spec_after = loops.get_loop_spec(self.cfg, self.store, "vps-node-and-whitelist-steward")
        assert spec_after is not None
        self.assertEqual(spec_after.autonomy, "confirm")

        # Verify autopause circuit breaker after 3 consecutive load_cmd failures
        loops.init_loop(
            self.cfg,
            self.store,
            "vps-node-and-whitelist-steward",
            template="vps-node-and-whitelist-steward",
            autonomy="confirm",
            load_cmd="echo 'API timeout' >&2; exit 2",
            overwrite=True,
        )
        for i in range(3):
            r_err = loops.run_loop_tick(self.cfg, self.store, "vps-node-and-whitelist-steward", force=True)
            self.assertIn(r_err["status"], ("failed", "paused"))
            self.assertEqual(r_err["consecutive_failures"], i + 1)
        st_paused = loops.get_loop_state(self.store, "vps-node-and-whitelist-steward")
        self.assertTrue(st_paused["paused"])
        self.assertIn("连续失败 3 次自动熔断", st_paused["paused_reason"])

        # Verify evolve digest captures paused business loops (D7)
        digest = evolve.build_evolve_digest(self.cfg, self.store)
        self.assertIn("d7_business_loops", digest)
        self.assertEqual(len(digest["d7_business_loops"]["paused_loops"]), 1)
        self.assertEqual(digest["d7_business_loops"]["paused_loops"][0]["id"], "vps-node-and-whitelist-steward")

        # Verify admin_web API & action resume
        svc = admin_web.WorkbenchService(cfg=self.cfg)
        web_loops = svc.get_loops_state()
        self.assertEqual(web_loops["paused_loops"], 1)
        act_res = svc.execute_action({"action": "loop_resume", "loop_id": "vps-node-and-whitelist-steward"})
        self.assertTrue(act_res["ok"])
        st_resumed = loops.get_loop_state(self.store, "vps-node-and-whitelist-steward")
        self.assertFalse(st_resumed["paused"])
        self.assertEqual(st_resumed["consecutive_failures"], 0)

        # Verify dynamic live version resolution in admin_web (never stuck on stale in-memory constant)
        self.store.set_meta("daemon_runtime", json.dumps({"version": "1.1.99"}))
        ov = svc.get_overview()
        self.assertEqual(ov["version"], "1.1.99")

    def test_reg_091_transitional_workaround_report_guard_and_defer_item(self) -> None:
        from sheepdog import outbound

        self.store.upsert_topic(
            "tp_prod-topology",
            title="Acmeco 生产拓扑与跨云迁移",
            kind="adopted",
            conversation_id="aaaa0004-0000-4000-8000-0000000a0004",
        )
        # Initialize ssl-cert-lifecycle-steward in confirm mode with a probe.py script (testing auto-discovery)
        init_spec = loops.init_loop(
            self.cfg,
            self.store,
            "ssl-cert-lifecycle-steward",
            template="ssl-cert-lifecycle-steward",
            autonomy="confirm",
            load_cmd="",
        )
        assert init_spec.path is not None
        probe_py = init_spec.path.parent / "probe.py"
        probe_py.write_text("import sys\nprint('[]')\n", encoding="utf-8")
        spec = loops.get_loop_spec(self.cfg, self.store, "ssl-cert-lifecycle-steward")
        assert spec is not None
        self.assertIn("probe.py", spec.load_cmd)

        # Simulate Subagent SYSTEM_MESSAGE handoff in transcript containing the transitional A-record workaround
        subagent_transcript = (
            "[System Notification]\n"
            "Sender ID: aaaa0009-0000-4000-8000-0000000a0009\n"
            "4. **DNS 服务 (`site-a.example`)**：已添加 `app-edge.site-a.example` 解析记录（RecordId `1000000001`，"
            "当前指向 `203.0.113.172` 确保 HTTPS 立即 200 可通；待 CDN `app-edge.site-a.example.cdn-provider.example` "
            "从 `process` 变 `online` 后可一键切 CNAME）。"
        )
        sanitized_summary = (
            "已在甲城容器集群建好 svc-chat/bff/web-app 及 app-edge.site-a.example HTTPS 证书与解析，"
            "实测公网返回200，可发给瑞松联调。"
        )
        sanitized_body = (
            "已完成甲城生产 容器集群的三服务部署、Ingress TLS 证书与公网域名接入：\n"
            "1. 公网 HTTPS 端到端实测返回 HTTP/2 200。"
        )
        fake_source = MagicMock()
        fake_source.send_to_chat.return_value = {"message_id": "om_rep_091_1", "thread_id": ""}
        fake_source.send_to_user.return_value = {"message_id": "om_rep_091_1", "thread_id": ""}

        # 1) Calling report with sanitized first screen & no --defer-item MUST be blocked by REG-091!
        with self.assertRaises(outbound.OutboundError) as ctx1:
            outbound.send_to_owner(
                self.cfg,
                self.store,
                fake_source,
                outbound.REPORT,
                "tp_prod-topology",
                "app-edge.site-a.example与甲城容器集群三服务已就绪",
                sanitized_body,
                key="acmeco-prod-topology",
                summary=sanitized_summary,
                ball="done",
                transcript_text=subagent_transcript,
            )
        self.assertIn("REG-091", str(ctx1.exception))
        self.assertIn("临时过渡态防隐瞒与防漏收尾门禁", str(ctx1.exception))

        # 2) Disclosing temporary A record in summary BUT passing --ball done without --defer-item MUST ALSO be blocked!
        disclosed_summary = (
            "已在甲城容器集群建好三服务，因CDN证书还在部署中，目前先用临时 A 记录指向甲城负载均衡保HTTPS 200，"
            "待证书就绪后切回CNAME。"
        )
        with self.assertRaises(outbound.OutboundError) as ctx2:
            outbound.send_to_owner(
                self.cfg,
                self.store,
                fake_source,
                outbound.REPORT,
                "tp_prod-topology",
                "app-edge.site-a.example与甲城容器集群三服务已就绪（临时A记录过渡）",
                sanitized_body,
                key="acmeco-prod-topology",
                summary=disclosed_summary,
                ball="done",
                transcript_text=subagent_transcript,
            )
        self.assertIn("REG-091", str(ctx2.exception))
        self.assertIn("防假结案门禁", str(ctx2.exception))

        # 3) Passing --defer-item and --defer-check-cmd registers into loop_items and appends badge to first screen!
        flag_file = Path(self.tmp.name) / "eo_cert_ready.flag"
        check_cmd = f'test -f "{flag_file}" && echo "CDN cert deployed"'
        res_ok = outbound.send_to_owner(
            self.cfg,
            self.store,
            fake_source,
            outbound.REPORT,
            "tp_prod-topology",
            "app-edge.site-a.example与甲城容器集群三服务已就绪（临时A记录过渡）",
            sanitized_body,
            key="acmeco-prod-topology",
            summary=disclosed_summary,
            ball="doing",
            defer_item="cname-app-edge.site-a.example:待 CDN 证书变为 deployed 后把 app-edge.site-a.example 从临时 A 记录切回 CNAME",
            defer_check_cmd=check_cmd,
            transcript_text=subagent_transcript,
        )
        self.assertEqual(res_ok.message_id, "om_rep_091_1")
        self.assertIn("⏳ 在途收尾项（已挂入 Loop `ssl-cert-lifecycle-steward` 自动盯盘）", res_ok.text)
        active_items = loops.list_loop_items(self.store, "ssl-cert-lifecycle-steward", status="active")
        self.assertEqual(len(active_items), 1)
        self.assertEqual(active_items[0]["item_key"], "cname-app-edge.site-a.example")
        self.assertEqual(active_items[0]["stage"], "awaiting_async_ready")

        # 4a) Tick while flag_file does not exist (defer_check_cmd exits 1) -> 0 tokens spent!
        tick_waiting = loops.run_loop_tick(self.cfg, self.store, "ssl-cert-lifecycle-steward", force=True)
        self.assertEqual(tick_waiting["status"], "empty")
        items_still_waiting = loops.list_loop_items(self.store, "ssl-cert-lifecycle-steward", status="active")
        self.assertEqual(items_still_waiting[0]["stage"], "awaiting_async_ready")

        # 4b) Once CDN cert is deployed (flag_file created -> defer_check_cmd exits 0), tick wakes tp_prod-topology!
        flag_file.write_text("deployed", encoding="utf-8")
        tick_ready = loops.run_loop_tick(self.cfg, self.store, "ssl-cert-lifecycle-steward", force=True)
        self.assertEqual(tick_ready["status"], "dispatched")
        self.assertEqual(tick_ready["topic_id"], "tp_prod-topology")
        items_ready = loops.list_loop_items(self.store, "ssl-cert-lifecycle-steward", status="active")
        self.assertEqual(items_ready[0]["stage"], "async_ready_to_cutover")

        # 5) Reporting final CNAME cutover with --ball done passes cleanly!
        fake_source.send_to_chat.return_value = {"message_id": "om_rep_091_2", "thread_id": "omt_091"}
        fake_source.send_to_user.return_value = {"message_id": "om_rep_091_2", "thread_id": "omt_091"}
        fake_source.reply_to.return_value = {"message_id": "om_rep_091_2", "thread_id": "omt_091"}
        final_res = outbound.send_to_owner(
            self.cfg,
            self.store,
            fake_source,
            outbound.REPORT,
            "tp_prod-topology",
            "已将app-edge.site-a.example切换为CDN CNAME",
            "已将 DNS 服务 的 app-edge 正式切换为 CNAME app-edge.site-a.example.cdn-provider.example 并实测 HTTP/2 200。",
            key="acmeco-prod-topology",
            summary="现在证书已部署完成，已将DNS 服务的 app-edge 正式切换为 CNAME app-edge.site-a.example.cdn-provider.example 并实测CDN链路HTTP/2 200。",
            ball="done",
            transcript_text=subagent_transcript,
        )
        self.assertEqual(final_res.message_id, "om_rep_091_2")

    def test_reg_137_loop_waiting_stage_deferral_and_external_payload_delta_wake(self) -> None:
        from datetime import datetime, timedelta, timezone

        cst = timezone(timedelta(hours=8))
        t0 = datetime(2026, 3, 28, 5, 51, 0, tzinfo=cst)

        self.store.upsert_topic(
            "tp_prod-topology",
            title="Acmeco 生产拓扑与跨云迁移",
            kind="adopted",
            conversation_id="aaaa0004-0000-4000-8000-0000000a0004",
        )
        loops.init_loop(
            self.cfg,
            self.store,
            "launch-tracker-steward",
            title="DEMO-128 甲站上线验收巡检",
            topic_id="tp_prod-topology",
            autonomy="confirm",
            interval_minutes=15.0,
            active_interval_minutes=10.0,
            load_cmd="printf '[]\\n'",
        )

        # 1) Initial discovery at 04:00 dispatches the new issue
        base_item = {
            "issue_key": "DEMO-139",
            "key": "DEMO-139",
            "status": "Todo",
            "updatedAt": "2026-03-27T18:29:34.817Z",
            "assignee": "owner_test@example.com",
            "bucket": "bucket_c_in_progress",
            "stage": "claiming_and_diagnosing",
            "title": "[Step2·>14h停滞] DEMO-139 (Todo): 源站能绕过边缘节点",
        }
        r0 = loops.run_loop_tick(
            self.cfg,
            self.store,
            "launch-tracker-steward",
            force=False,
            injected_items=[base_item],
            now_dt=t0,
        )
        self.assertEqual(r0["status"], "dispatched")
        self.assertEqual(r0["newly_added"], 1)
        self.assertEqual(r0["due_items_count"], 1)

        # 2) Session transitions DEMO-139 to waiting_owner_or_review WITHOUT passing next_minutes -> auto-defers >= 60m
        u1 = loops.update_loop_item(
            self.cfg,
            self.store,
            "launch-tracker-steward",
            "DEMO-139",
            stage="waiting_owner_or_review",
            note="已发起飞书决策卡等主人拍板",
            now_dt=t0,
        )
        self.assertEqual(u1["stage"], "waiting_owner_or_review")
        self.assertEqual(u1["next_check_at"], "2026-03-28T06:51:00+08:00")

        # 3) Session sets --next-minutes 240 (08:00); a subsequent busy-deferral (+15m with keep_longer_next=True) must NOT shrink it
        u2 = loops.update_loop_item(
            self.cfg,
            self.store,
            "launch-tracker-steward",
            "DEMO-139",
            stage="waiting_owner_or_review",
            next_minutes=240.0,
            now_dt=t0,
        )
        self.assertEqual(u2["next_check_at"], "2026-03-28T09:51:00+08:00")

        u3 = loops.update_loop_item(
            self.cfg,
            self.store,
            "launch-tracker-steward",
            "DEMO-139",
            next_minutes=15.0,
            keep_longer_next=True,
            now_dt=t0 + timedelta(minutes=10),
        )
        self.assertEqual(u3["next_check_at"], "2026-03-28T09:51:00+08:00")

        # 4) At 04:30 (within 08:00 window), unchanged external status/updatedAt/assignee (even if title's relative hours changed) -> 0 Token empty!
        same_external_item = dict(
            base_item,
            title="[Step2·>15h停滞] DEMO-139 (Todo): 源站能绕过边缘节点",
        )
        r_idle = loops.run_loop_tick(
            self.cfg,
            self.store,
            "launch-tracker-steward",
            force=False,
            injected_items=[same_external_item],
            now_dt=t0 + timedelta(minutes=30),
        )
        self.assertEqual(r_idle["status"], "empty")
        self.assertEqual(r_idle["token_cost"], 0)
        items_after_idle = loops.list_loop_items(self.store, "launch-tracker-steward", status="active")
        self.assertEqual(items_after_idle[0]["next_check_at"], "2026-03-28T09:51:00+08:00")

        # 5) At 04:45 (still before 08:00), remote Linear updatedAt/status changes -> clears next_check_at and wakes immediately!
        changed_external_item = dict(
            base_item,
            status="In Review",
            updatedAt="2026-03-27T22:36:00.000Z",
        )
        r_wake = loops.run_loop_tick(
            self.cfg,
            self.store,
            "launch-tracker-steward",
            force=False,
            injected_items=[changed_external_item],
            now_dt=t0 + timedelta(minutes=45),
        )
        self.assertEqual(r_wake["status"], "dispatched")
        self.assertEqual(r_wake["due_items_count"], 1)

        # And because DEMO-139 is still in waiting_owner_or_review, dispatching it stamps last_run_id and bumps next_check_at (+60m -> 05:45)
        items_after_wake = loops.list_loop_items(self.store, "launch-tracker-steward", status="active")
        self.assertEqual(items_after_wake[0]["last_run_id"], r_wake["run_id"])
        self.assertEqual(items_after_wake[0]["next_check_at"], "2026-03-28T07:36:00+08:00")

        # 6) 10 minutes later at 04:55 with no further external change -> stays empty (0 Token), preventing 10m infinite spin!
        r_post_wake_idle = loops.run_loop_tick(
            self.cfg,
            self.store,
            "launch-tracker-steward",
            force=False,
            injected_items=[changed_external_item],
            now_dt=t0 + timedelta(minutes=55),
        )
        self.assertEqual(r_post_wake_idle["status"], "empty")
        self.assertEqual(r_post_wake_idle["token_cost"], 0)


if __name__ == "__main__":
    unittest.main()
