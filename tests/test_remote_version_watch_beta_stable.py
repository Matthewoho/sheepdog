"""规格 7.75（SD-775-REMOTE-VERSION-WATCH）回归测试（REG-023 ~ REG-025）：
1. REG-023：_git_run 强制注入 GIT_TERMINAL_PROMPT=0 与 GIT_SSH_COMMAND BatchMode=yes；origin 失败自动 fallback 到公开 HTTPS 源；附注标签 ^{} commit SHA 解引用；本机版本高于远端时零倒挂误报。
2. REG-024：双轨版本分发（Release=Stable 稳定版 vs Tag=Beta 尝鲜版），后台周期巡检仅对新 GitHub Release 发送单次非强制更新建议，对 Beta Tag（tag > release）保持静默并写入 meta["version_remote_latest"]。
3. REG-025：主人在飞书找主人私聊问「有没有 beta / 有新版本吗」被拦截为 version_query 并返回 Stable + Beta 状态及双轨非强制升级口令；「升级稳定版」与「升级 beta」双轨路由；dirty 或 git pull 失败时诚实回执（严禁谎报已是最新）。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import sheepdog
from sheepdog import actions, observe, version_watch
from sheepdog.config import Config, EscalationConfig
from sheepdog.engine import Collector, Dispatcher
from sheepdog.models import Message
from sheepdog.roster import Roster
from sheepdog.security import SecurityConfig
from sheepdog.sink.agentapi import DryRunSink
from sheepdog.store import Store


def _init_temp_git_repo(repo: Path, ver: str) -> str:
    (repo / "sheepdog").mkdir(parents=True, exist_ok=True)
    for args in (("init", "-q"), ("config", "user.name", "test"), ("config", "user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    (repo / "sheepdog" / "__init__.py").write_text(f'__version__ = "{ver}"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"v{ver}"], capture_output=True, text=True, check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _make_owner_cfg(state_dir: Path) -> Config:
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(self_open_id="ou_test_owner", state_dir=state_dir)
    cfg.escalation = EscalationConfig(
        chat_ids=["oc_owner_dm"],
        header_regex=r"^🐕 需要你定 · [^\n]*\[(?P<topic>tp_[a-z0-9_.-]+)\]",
        attribution_regex=r"\[(?P<topic>tp_[a-z0-9_.-]+)\]",
        owner_shortcut=True,
    )
    return cfg


def _owner_dm_msg(mid: str, content: str, create_time: str) -> Message:
    return Message(
        message_id=mid, chat_id="oc_owner_dm", chat_name="私聊", chat_type="p2p",
        sender_id="ou_test_owner", sender_name="Matthew", sender_type="user",
        content=content, msg_type="text", create_time=create_time,
    )


def _fake_tag_runner(ls_out: str):
    real_run = subprocess.run

    def _runner(args, *a, **kw):
        cmd = list(args)
        if "ls-remote" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=ls_out, stderr="")
        return real_run(args, *a, **kw)
    return _runner


class RemoteVersionWatchBetaStableTest(unittest.TestCase):
    def tearDown(self):
        actions._resident = False

    def test_reg_023_non_interactive_git_fallback_and_peeled_tag_and_no_downgrade_alert(self):
        # 1. 验证 _git_run 自动注入 GIT_TERMINAL_PROMPT=0 与 GIT_SSH_COMMAND (BatchMode=yes)
        captured_envs: list[dict] = []

        def fake_subproc_env(cmd, *a, **kw):
            captured_envs.append(dict(kw.get("env") or {}))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok\n", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_subproc_env):
            res = sheepdog._git_run(["git", "status"], timeout=2)
        self.assertIsNotNone(res)
        self.assertEqual(captured_envs[0].get("GIT_TERMINAL_PROMPT"), "0")
        self.assertIn("BatchMode=yes", captured_envs[0].get("GIT_SSH_COMMAND", ""))

        # 2 & 3. 验证 origin 失败（rc=128）自动 fallback 到公开 HTTPS 地址，且附注标签 ^{} 解引用 SHA 优先于 tag object SHA
        tag_obj_sha, peeled_commit_sha, old_sha = "a" * 40, "b" * 40, "c" * 40
        ls_remote_lines = (
            f"{old_sha}\trefs/tags/v0.7.72\n"
            f"{tag_obj_sha}\trefs/tags/v0.7.75\n"
            f"{peeled_commit_sha}\trefs/tags/v0.7.75^{{}}\n"
        )
        seen_remotes: list[str] = []

        def fake_ls_remote(args, *a, **kw):
            cmd = list(args)
            if "ls-remote" in cmd:
                idx = cmd.index("ls-remote")
                target_remote = cmd[idx + 2] if cmd[idx + 1] == "--tags" else cmd[idx + 1]
                seen_remotes.append(target_remote)
                if target_remote == "origin":
                    return subprocess.CompletedProcess(args=cmd, returncode=128, stdout="", stderr="Permission denied")
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=ls_remote_lines, stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            sha_775 = _init_temp_git_repo(repo, "0.7.75")

            with mock.patch("subprocess.run", side_effect=fake_ls_remote):
                tag_probe = version_watch.probe_remote_tags(repo)
            self.assertTrue(tag_probe["ok"])
            self.assertEqual(seen_remotes, ["origin", version_watch.PUBLIC_REPO_GIT_URL])
            self.assertEqual(tag_probe["latest_tag"], "v0.7.75")
            self.assertEqual(tag_probe["latest_sha"], peeled_commit_sha)
            self.assertEqual(tag_probe["tags"]["v0.7.75"], peeled_commit_sha)

            # 4. 防倒挂误报：当本机版本为 0.7.75 而远端最高只有 v0.7.72 时，check_version_update 返回 []
            cfg = _make_owner_cfg(tmp_path / "state")
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            disp = Dispatcher(cfg, store, DryRunSink(), Roster(), SecurityConfig())
            rt = {"version": "0.7.75", "git_sha": sha_775, "display": f"0.7.75 ({sha_775[:7]})", "pid": 999}
            now_dt = datetime(2026, 10, 23, 2, 0, 0, tzinfo=observe.TZ)

            with mock.patch("subprocess.run", side_effect=_fake_tag_runner(f"{old_sha}\trefs/tags/v0.7.72\n")), \
                    mock.patch("sheepdog.version_watch.fetch_latest_github_release", return_value={"tag": "v0.7.72", "version": "0.7.72", "url": ""}):
                alerts = version_watch.check_version_update(cfg, store, disp, runtime_info=rt, repo_dir=repo, now=now_dt)
            self.assertEqual(alerts, [])

    def test_reg_024_stable_release_advisory_only_and_beta_silence_in_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            sha_772 = _init_temp_git_repo(repo, "0.7.72")
            cfg = _make_owner_cfg(tmp_path / "state")
            store = Store(cfg.db_path)
            self.addCleanup(store.close)

            notified_cards: list[tuple[str, str, str]] = []
            disp = Dispatcher(cfg, store, DryRunSink(), Roster(), SecurityConfig())
            disp._notify_owner = lambda text, what, topic_id="", **kw: (notified_cards.append((text, what, topic_id)) or True)
            rt = {"version": "0.7.72", "git_sha": sha_772, "display": f"0.7.72 ({sha_772[:7]})", "pid": 1001}
            now_dt = datetime(2026, 10, 23, 2, 10, 0, tzinfo=observe.TZ)
            ls_out = f"{sha_772}\trefs/tags/v0.7.72\n{'d' * 40}\trefs/tags/v0.7.75^{{}}\n"

            # 1 & 2. 远端 Stable Release 为 v0.7.72，但已有 Beta Tag v0.7.75（tag > release）：后台巡检保持静默
            with mock.patch("subprocess.run", side_effect=_fake_tag_runner(ls_out)), \
                    mock.patch("sheepdog.version_watch.fetch_latest_github_release", return_value={"tag": "v0.7.72", "version": "0.7.72", "url": ""}):
                sent_beta_only = version_watch.check_version_update(cfg, store, disp, runtime_info=rt, repo_dir=repo, now=now_dt)
            self.assertEqual(sent_beta_only, [])
            self.assertEqual(notified_cards, [])
            latest_meta = json.loads(store.get_meta("version_remote_latest") or "{}")
            self.assertEqual(latest_meta.get("stable_tag"), "v0.7.72")
            self.assertEqual(latest_meta.get("beta_tag"), "v0.7.75")
            self.assertTrue(latest_meta.get("has_beta"))

            # 3. 远端正式发布新 GitHub Release v0.7.75（Stable > 本机 0.7.72）：触发 1 次非强制稳定版更新建议，再次调用不重复
            with mock.patch("subprocess.run", side_effect=_fake_tag_runner(ls_out)), \
                    mock.patch("sheepdog.version_watch.fetch_latest_github_release", return_value={"tag": "v0.7.75", "version": "0.7.75", "url": ""}):
                sent_1 = version_watch.check_version_update(cfg, store, disp, runtime_info=rt, repo_dir=repo, now=now_dt + timedelta(minutes=35))
                sent_2 = version_watch.check_version_update(cfg, store, disp, runtime_info=rt, repo_dir=repo, now=now_dt + timedelta(minutes=70))
            self.assertEqual(len(sent_1), 1)
            self.assertEqual(sent_2, [])
            self.assertEqual(len(notified_cards), 1)
            for expected in ("v0.7.75", "你来升级", "非强制"):
                self.assertIn(expected, sent_1[0])

    def test_reg_025_owner_ask_beta_query_and_dual_channel_honest_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            sha_772 = _init_temp_git_repo(repo, "0.7.72")
            cfg = _make_owner_cfg(tmp_path / "state")
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            rt = {"version": "0.7.72", "git_sha": sha_772, "display": f"0.7.72 ({sha_772[:7]})", "pid": 2002}
            store.set_meta("daemon_runtime", json.dumps(rt, ensure_ascii=False))
            ls_out = f"{sha_772}\trefs/tags/v0.7.72\n{'e' * 40}\trefs/tags/v0.7.75^{{}}\n"

            # 1. 主人在找主人私聊发「有没有 beta」/「有新版本吗」：拦截为 ("drop", "version_query") 并返回 Stable + Beta 卡片
            fake_source = mock.MagicMock()
            fake_source.reply_to.return_value = {"message_id": "om_bot_ver_reply_1"}
            col = Collector(cfg, store, fake_source, Roster(), SecurityConfig())
            now_dt = datetime(2026, 10, 23, 2, 20, 0, tzinfo=observe.TZ)

            with mock.patch("subprocess.run", side_effect=_fake_tag_runner(ls_out)), \
                    mock.patch("sheepdog.default_repo_dir", return_value=repo), \
                    mock.patch("sheepdog.version_watch.fetch_latest_github_release", return_value={"tag": "v0.7.72", "version": "0.7.72", "url": ""}):
                for idx, q_text in enumerate(("有没有 beta", "有新版本吗"), start=1):
                    d_q = col._escalation_message(_owner_dm_msg(f"om_owner_q_{idx}", q_text, observe.iso(now_dt + timedelta(seconds=idx))))
                    self.assertEqual((d_q.route, d_q.reason), ("drop", "version_query"))
                reply_text = version_watch.format_version_query_reply(store, repo_dir=repo, check_remote=True, now=now_dt)
            for expected in ("v0.7.72", "🧪 Beta 尝鲜版（tag > release）：**v0.7.75**", "升级稳定版", "升级 beta"):
                self.assertIn(expected, reply_text)

            # 2. 主人回复「升级 beta」入队 channel="beta"；回复「升级稳定版」入队 channel="stable"
            d_b = col._escalation_message(_owner_dm_msg("om_upg_beta", "升级 beta", observe.iso(now_dt + timedelta(seconds=10))))
            self.assertEqual((d_b.route, d_b.reason), ("drop", "upgrade_command"))
            acts = store.pending_actions()
            self.assertEqual(json.loads(acts[0]["args_json"]).get("channel"), "beta")
            store.finish_action(acts[0]["id"], result="{}")

            d_s = col._escalation_message(_owner_dm_msg("om_upg_stable", "升级稳定版", observe.iso(now_dt + timedelta(seconds=15))))
            self.assertEqual((d_s.route, d_s.reason), ("drop", "upgrade_command"))
            acts2 = store.pending_actions()
            self.assertEqual(json.loads(acts2[0]["args_json"]).get("channel"), "stable")

            # 3. 诚实升级回执：当工作区 dirty 或 git pull 返回非 0 时，绝不谎报「✅ 当前代码已是最新」
            sent_cards: list[str] = []
            disp = Dispatcher(cfg, store, DryRunSink(), Roster(), SecurityConfig())
            disp._notify_owner = lambda text, what, topic_id="", **kw: (sent_cards.append(text) or True)
            (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
            disp.runtime_info = {**rt, "dirty": True, "display": f"0.7.72 ({sha_772[:7]}-dirty)"}
            disp.upgrade(reply_to="om_upg_stable", command="升级稳定版", channel="stable", repo_dir=repo)
            self.assertIn("⚠️ 未能拉取远端最新代码", sent_cards[0])
            self.assertNotIn("✅ 当前代码已是最新", sent_cards[0])

            (repo / "dirty.txt").unlink()
            sent_cards.clear()
            disp.runtime_info = rt
            real_run = subprocess.run
            fail_pull = lambda args, *a, **kw: (
                subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="fatal: network error")
                if "pull" in args else real_run(args, *a, **kw)
            )
            with mock.patch("subprocess.run", side_effect=fail_pull):
                disp.upgrade(reply_to="om_upg_beta", command="升级 beta", channel="beta", repo_dir=repo)
            self.assertIn("⚠️ 未能拉取远端最新代码", sent_cards[0])
            self.assertNotIn("✅ 当前代码已是最新", sent_cards[0])

    def test_reg_026_interactive_version_notice_and_beta_query_card_callbacks(self):
        from sheepdog import card_callback, outbound
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            sha_772 = _init_temp_git_repo(repo, "0.7.72")
            cfg = _make_owner_cfg(tmp_path / "state")
            store = Store(cfg.db_path)
            self.addCleanup(store.close)
            rt = {"version": "0.7.72", "git_sha": sha_772, "display": f"0.7.72 ({sha_772[:7]})", "pid": 3003}
            store.set_meta("daemon_runtime", json.dumps(rt, ensure_ascii=False))
            now_dt = datetime(2026, 10, 23, 3, 0, 0, tzinfo=observe.TZ)
            ls_out = f"{sha_772}\trefs/tags/v0.7.72\n{'a' * 40}\trefs/tags/v0.7.75^{{}}\n{'b' * 40}\trefs/tags/v0.7.76^{{}}\n"
            # 1. 生成 Scenario B（本地待重启）、Scenario A（远端稳定版）、Scenario C（版本与 Beta 查询）三张交互卡片
            (repo / "sheepdog" / "__init__.py").write_text('__version__ = "0.7.73"\n', encoding="utf-8")
            with mock.patch("subprocess.run", side_effect=_fake_tag_runner(ls_out)), \
                    mock.patch("sheepdog.version_watch.fetch_latest_github_release", return_value={"tag": "v0.7.75", "version": "0.7.75", "url": ""}):
                notices = version_watch.check_version_update(cfg, store, None, runtime_info=rt, repo_dir=repo, now=now_dt)
                query_text = version_watch.format_version_query_reply(store, repo_dir=repo, check_remote=True, now=now_dt)
            local_text, remote_text = notices[0], notices[1]
            _, card_b = outbound.owner_content(cfg, local_text)
            _, card_a = outbound.owner_content(cfg, remote_text)
            _, card_c = outbound.owner_content(cfg, query_text)
            for c in (card_a, card_b, card_c):
                cs = c["body"]["elements"][-1]
                self.assertEqual((cs["tag"], cs["flex_mode"], cs["horizontal_spacing"]), ("column_set", "flow", "8px"))
                self.assertNotIn("vertical_spacing", cs)
                self.assertNotIn('"tag": "form"', json.dumps(c, ensure_ascii=False))
            btns_a = [col["elements"][0] for col in card_a["body"]["elements"][-1]["columns"]]
            btns_b = [col["elements"][0] for col in card_b["body"]["elements"][-1]["columns"]]
            btns_c = [col["elements"][0] for col in card_c["body"]["elements"][-1]["columns"]]
            self.assertEqual([b["text"]["content"] for b in btns_a], ["🚀 升级稳定版 v0.7.75", "🔍 查有没有 Beta 版", "⏸ 暂不升级"])
            self.assertEqual([b["text"]["content"] for b in btns_b], ["🔄 立即重启生效 0.7.73", "⏸ 稍后手动重启"])
            self.assertEqual([b["text"]["content"] for b in btns_c], ["🧪 升级 Beta 尝鲜版 v0.7.76", "🚀 升级稳定版 v0.7.75", "⏸ 暂不升级"])
            for mid, txt in (("om_card_a", remote_text), ("om_card_b", local_text), ("om_card_c", query_text), ("om_card_d", remote_text)):
                store.add_bot_outbox(mid, "tp_bus", False, txt, observe.iso(now_dt))
            fake_source = mock.MagicMock()
            fake_source.reply_to.return_value = {"message_id": "om_query_reply_card"}
            col = Collector(cfg, store, fake_source, Roster(), SecurityConfig())
            mk_ev = lambda eid, mid, val, op="ou_test_owner": {
                "type": "card.action.trigger", "event_id": eid, "message_id": mid, "chat_id": "oc_owner_dm",
                "operator_id": op, "action_tag": "button", "action_value": val, "timestamp": "2026-10-23T03:05:00+08:00",
            }
            # 2. 非主人点击升级按钮：fail-closed 拦截为 card_callback_non_owner，不入队、不改卡
            self.assertIsNone(card_callback.process_card_action_event(mk_ev("ev_stranger", "om_card_a", btns_a[0]["value"], op="ou_stranger"), cfg=cfg, store=store, source=fake_source))
            self.assertEqual(store.get_message("sd_card_cb_ev_stranger")["reason"], "card_callback_non_owner")
            self.assertEqual(store.pending_actions(), [])
            fake_source.patch_card.assert_not_called()
            # 3. 点击 [🔍 查有没有 Beta 版]：路由为 version_query 并触发回复带胶囊按钮的交互状态卡
            with mock.patch("subprocess.run", side_effect=_fake_tag_runner(ls_out)), \
                    mock.patch("sheepdog.default_repo_dir", return_value=repo), \
                    mock.patch("sheepdog.version_watch.fetch_latest_github_release", return_value={"tag": "v0.7.75", "version": "0.7.75", "url": ""}):
                m_q = card_callback.process_card_action_event(mk_ev("ev_q1", "om_card_a", btns_a[1]["value"]), cfg=cfg, store=store, source=fake_source)
                col.ingest([m_q], received_by="bot")
            self.assertEqual(store.get_message("sd_card_cb_ev_q1")["reason"], "version_query")
            self.assertEqual(fake_source.reply_to.call_count, 1)
            self.assertEqual(fake_source.reply_to.call_args.kwargs["card"]["body"]["elements"][-1]["tag"], "column_set")
            # 4. 点击 [🚀 升级稳定版 v0.7.75] + 双重防重（同卡二次点击 & 文字口令防重）
            m_s = card_callback.process_card_action_event(mk_ev("ev_upg_s1", "om_card_a", btns_a[0]["value"]), cfg=cfg, store=store, source=fake_source)
            col.ingest([m_s], received_by="bot")
            self.assertEqual(fake_source.patch_card.call_count, 1)
            patched_a = fake_source.patch_card.call_args.args[1]
            self.assertEqual(patched_a["header"]["template"], "green")
            self.assertNotIn('"tag": "button"', json.dumps(patched_a, ensure_ascii=False))
            self.assertIn("✅ **已拍板：🚀 升级稳定版 v0.7.75（已提交后台执行）**", json.dumps(patched_a, ensure_ascii=False))
            self.assertIsNone(card_callback.process_card_action_event(mk_ev("ev_upg_s2_dup", "om_card_a", btns_a[0]["value"]), cfg=cfg, store=store, source=fake_source))
            self.assertEqual(store.get_message("sd_card_cb_ev_upg_s2_dup")["reason"], "card_callback_duplicate")
            col._escalation_message(_owner_dm_msg("om_text_dup", "升级", observe.iso(now_dt + timedelta(seconds=20))))
            acts = store.pending_actions()
            self.assertEqual(len(acts), 1)
            self.assertEqual(json.loads(acts[0]["args_json"]), {"reply_to": "om_card_a", "command": "升级稳定版", "channel": "stable", "no_pull": False})
            store.finish_action(acts[0]["id"], result="{}")
            # 5. 点击 [🧪 升级 Beta 尝鲜版 v0.7.76]、[🔄 立即重启生效 0.7.73] 与 [⏸ 暂不升级]
            m_b = card_callback.process_card_action_event(mk_ev("ev_upg_b1", "om_card_c", btns_c[0]["value"]), cfg=cfg, store=store, source=fake_source)
            col.ingest([m_b], received_by="bot")
            acts_b = store.pending_actions()
            self.assertEqual(json.loads(acts_b[0]["args_json"]), {"reply_to": "om_card_c", "command": "升级 beta", "channel": "beta", "no_pull": False})
            store.finish_action(acts_b[0]["id"], result="{}")
            m_r = card_callback.process_card_action_event(mk_ev("ev_upg_r1", "om_card_b", btns_b[0]["value"]), cfg=cfg, store=store, source=fake_source)
            col.ingest([m_r], received_by="bot")
            acts_r = store.pending_actions()
            self.assertEqual(json.loads(acts_r[0]["args_json"]), {"reply_to": "om_card_b", "command": "重启生效", "channel": "stable", "no_pull": True})
            store.finish_action(acts_r[0]["id"], result="{}")
            m_d = card_callback.process_card_action_event(mk_ev("ev_dis_1", "om_card_d", btns_a[2]["value"]), cfg=cfg, store=store, source=fake_source)
            col.ingest([m_d], received_by="bot")
            self.assertEqual((store.get_message("sd_card_cb_ev_dis_1")["route"], store.get_message("sd_card_cb_ev_dis_1")["reason"]), ("drop", "version_dismiss"))
            self.assertEqual(store.pending_actions(), [])
            self.assertEqual(store.pending_dispatch(), [])


if __name__ == "__main__":
    unittest.main()
