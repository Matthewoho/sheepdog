"""REG-033 回归测试：跨进程 TAT 文件锁缓存、_run_once 429 退避与 99991663 自动刷新、话题群瞬态错误免熔断与索引卡相对时间去抖（7.80）。"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from sheepdog import outbound, topicgroup as tg
from sheepdog.source import SourceError
from sheepdog.source.lark import LarkCliSource
from sheepdog.source.tat_cache import (
    get_bot_tat,
    inject_bot_tat_env,
    invalidate_tat_cache,
    is_token_invalid_error,
    is_transient_lark_error,
)
from test_topic_group import TGBase


class TatRateLimit780Test(TGBase):
    """守护 REG-033 的 4 条核心不变量。"""

    def test_reg_033_tat_cache_reuses_token_and_injects_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "tat_cache.json"
            fetch_count = 0

            def fake_creds() -> dict[str, str]:
                return {
                    "app_id": "cli_test_780",
                    "app_secret": "sec_test_780",
                    "domain": "https://open.larksuite.com",
                }

            def fake_fetcher(app_id: str, app_secret: str, domain: str) -> tuple[str, int]:
                nonlocal fetch_count
                fetch_count += 1
                self.assertEqual((app_id, app_secret, domain), ("cli_test_780", "sec_test_780", "https://open.larksuite.com"))
                return f"t-bot-token-{fetch_count}", 7200

            for _ in range(10):
                got = get_bot_tat(cache_path=cache_path, creds_loader=fake_creds, fetcher=fake_fetcher)
                self.assertEqual(got, ("t-bot-token-1", "cli_test_780", "lark"))
                env = inject_bot_tat_env({}, cache_path=cache_path, creds_loader=fake_creds, fetcher=fake_fetcher)
                self.assertIsNotNone(env)
                self.assertEqual(env["LARKSUITE_CLI_TENANT_ACCESS_TOKEN"], "t-bot-token-1")
                self.assertEqual(env["LARKSUITE_CLI_APP_ID"], "cli_test_780")
                self.assertEqual(env["LARKSUITE_CLI_BRAND"], "lark")

            self.assertEqual(fetch_count, 1, "10 consecutive TAT lookups MUST only invoke fetcher once!")

            invalidate_tat_cache(cache_path=cache_path)
            env2 = inject_bot_tat_env({}, cache_path=cache_path, creds_loader=fake_creds, fetcher=fake_fetcher)
            self.assertEqual(fetch_count, 2)
            self.assertEqual(env2["LARKSUITE_CLI_TENANT_ACCESS_TOKEN"], "t-bot-token-2")

    def test_reg_033_lark_run_once_retries_429_and_refreshes_invalid_tat(self) -> None:
        src = LarkCliSource(binary="lark-cli", retries=3, retry_wait=0.001)
        calls: list[tuple[list[str], dict[str, str] | None]] = []
        responses = [
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=json.dumps({
                    "ok": False,
                    "error": {
                        "type": "api",
                        "subtype": "rate_limit",
                        "code": 429,
                        "message": "TAT endpoint rate limited (HTTP 429)",
                    },
                }),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"ok": True, "data": {"message_id": "om_ok_after_429"}}),
                stderr="",
            ),
        ]

        def fake_run(cmd: list[str], capture_output: bool = True, text: bool = True, timeout: float | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
            calls.append((list(cmd), env))
            return responses.pop(0)

        fake_run._accepts_env = True  # type: ignore[attr-defined]
        with mock.patch("sheepdog.source.lark.subprocess.run", fake_run), mock.patch(
            "sheepdog.source.lark.inject_bot_tat_env",
            side_effect=lambda *a, **kw: {"LARKSUITE_CLI_TENANT_ACCESS_TOKEN": "t-fresh" if kw.get("force_refresh") else "t-initial"},
        ):
            res = src._run_once(["im", "+messages-send", "--user-id", "ou_test", "--text", "hello"], identity="bot", timeout=5)
            self.assertEqual(res["data"]["message_id"], "om_ok_after_429")
            self.assertEqual(len(calls), 2, "_run_once MUST retry transient 429 and succeed on 2nd attempt!")
            self.assertEqual(calls[0][1]["LARKSUITE_CLI_TENANT_ACCESS_TOKEN"], "t-initial")

        # 2) 99991663 invalid tenant_access_token triggers cache invalidation and immediate token refresh
        calls.clear()
        responses[:] = [
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=json.dumps({"ok": False, "code": 99991663, "msg": "invalid tenant_access_token"}),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"ok": True, "data": {"message_id": "om_ok_refreshed"}}),
                stderr="",
            ),
        ]
        self.assertTrue(is_token_invalid_error({"code": 99991663, "msg": "invalid tenant_access_token"}))
        with mock.patch("sheepdog.source.lark.subprocess.run", fake_run), mock.patch(
            "sheepdog.source.lark.invalidate_tat_cache"
        ) as mock_inv, mock.patch(
            "sheepdog.source.lark.inject_bot_tat_env",
            side_effect=lambda *a, **kw: {"LARKSUITE_CLI_TENANT_ACCESS_TOKEN": "t-refreshed" if kw.get("force_refresh") else "t-stale"},
        ):
            res2 = src._run_once(["im", "+messages-send", "--user-id", "ou_test", "--text", "hello"], identity="bot", timeout=5)
            self.assertEqual(res2["data"]["message_id"], "om_ok_refreshed")
            mock_inv.assert_called_once()
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][1]["LARKSUITE_CLI_TENANT_ACCESS_TOKEN"], "t-stale")
            self.assertEqual(calls[1][1]["LARKSUITE_CLI_TENANT_ACCESS_TOKEN"], "t-refreshed")

    def test_reg_033_transient_429_does_not_mark_topic_group_down(self) -> None:
        rc, _, err = self.ask("--key", "TAT-429-SAFE", "--track", title="限流防误熔断", text="建立事项")
        self.assertEqual(rc, 0, err)
        self.poll()
        it = self.item("TAT-429-SAFE")
        self.assertIsNotNone(it)
        self.assertFalse(tg.is_down(self.store))

        rate_limit_err = SourceError("lark-cli (api/rate_limit): TAT endpoint rate limited (HTTP 429)")
        self.assertTrue(is_transient_lark_error(rate_limit_err))

        # 1) outbound._send_to_group 遇到 429 不标 tg_down、不静默退回私聊，而是返回 EXIT_SEND_FAILED 让调用方重试
        with mock.patch.object(LarkCliSource, "reply_to", side_effect=rate_limit_err):
            rc_429, out_429, err_429 = self.ask("--key", "TAT-429-SAFE", kind="report", title="限流测试", text="受限流影响")
            self.assertEqual(rc_429, outbound.EXIT_SEND_FAILED)
            self.assertNotIn("已退回私聊", out_429 + err_429)
            self.assertFalse(tg.is_down(self.store), "Transient 429 MUST NOT mark topic group down!")

        # 2) topicgroup.dispatcher_round 遇到 429 同样不标 tg_down
        self.store.update_item(it["item_no"], handoff_to="tp_beta", handoff_at="2026-10-26T17:00:00+08:00")
        with mock.patch.object(LarkCliSource, "reply_to", side_effect=rate_limit_err):
            tg.dispatcher_round(self.d)
            self.assertFalse(tg.is_down(self.store), "dispatcher_round transient 429 MUST NOT mark topic group down!")

        # 3) 永久性错误（如机器人不在群里）照常触发 tg.mark_down 熔断并退回私聊
        perm_err = SourceError("lark-cli (api/sim): bot not in chat")
        self.assertFalse(is_transient_lark_error(perm_err))
        with mock.patch.object(LarkCliSource, "reply_to", side_effect=perm_err):
            rc_perm, out_perm, _ = self.ask("--key", "TAT-429-SAFE", kind="report", title="永久故障", text="机器人被移出群")
            self.assertEqual(rc_perm, 0)
            self.assertIn("已退回私聊", out_perm)
            self.assertTrue(tg.is_down(self.store))

    def test_reg_033_strip_time_ignores_relative_minute_ticks(self) -> None:
        idx_t1 = (
            "📋 跟踪事项索引 · 17:10 更新 · 跟踪 2 件 · 等你定 1\n\n"
            "等你定 · 1 件\n"
            "- T-1 · 证书续期确认\n"
            "  阿尔法 · 17:05 问 · 已等 5 分钟\n\n"
            "进行中 · 1 件\n"
            "- T-2 · 数据库巡检\n"
            "  阿尔法 · 最近动静 1 分钟前\n\n"
            "建议放进群 · 1 件\n"
            "- 架构方案讨论（拖了 2 小时）\n"
            "  阿尔法 · 钥匙 arch-1 · 最近动静 3 分钟前"
        )
        idx_t2 = (
            "📋 跟踪事项索引 · 17:11 更新 · 跟踪 2 件 · 等你定 1\n\n"
            "等你定 · 1 件\n"
            "- T-1 · 证书续期确认\n"
            "  阿尔法 · 17:05 问 · 已等 6 分钟\n\n"
            "进行中 · 1 件\n"
            "- T-2 · 数据库巡检\n"
            "  阿尔法 · 最近动静 9 分钟前\n\n"
            "建议放进群 · 1 件\n"
            "- 架构方案讨论（拖了 5 小时）\n"
            "  阿尔法 · 钥匙 arch-1 · 最近动静 18 分钟前"
        )
        self.assertEqual(tg._strip_time(idx_t1), tg._strip_time(idx_t2))
        idx_changed = idx_t2.replace("进行中 · 1 件", "最近办完 · 1 件")
        self.assertNotEqual(tg._strip_time(idx_t1), tg._strip_time(idx_changed))


if __name__ == "__main__":
    unittest.main()
