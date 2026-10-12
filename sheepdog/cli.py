"""命令行入口。

  sheepdog doctor                 检查依赖与环境
  sheepdog poll [--dry-run]       拉取一次并路由（--dry-run 不投递）
  sheepdog run [--dry-run]        常驻循环：拉取 → 路由 → 投递
  sheepdog inbox [--chat 群名]     查看 Inbox（按群分组）
  sheepdog inbox-clear [--chat 群名 | --all]
  sheepdog init [--dry-run]       同步名册、没有总线就建、给未 onboarding 的 managed 会话发 onboarding
  sheepdog sessions               查看 session 注册表与状态（含名册 mode、职责、负责的聊天）
  sheepdog forward --topic T [--message-ids a,b] [--quote 主人原话] [--note 总线备注]   转交消息 / 转达主人原话
  sheepdog spawn --key K [--dry-run]                     为名册里 conversation_id 留空的条目新建会话（可接手前任）
  sheepdog watch --topic T --person ou_x [--chat oc_x] [--note ...]   登记「等别人回复」
  sheepdog watches [--all]  /  sheepdog unwatch --id N  /  sheepdog watch-done --id N（确认真等到了）
  sheepdog security-log [--since 24h]                    列出被安全规则标记 / 拦截的消息
  sheepdog escalations [--all]                           列出未结 / 全部「需要你定」
  sheepdog acks [--open]                                 列出点过的确认表情及是否已撤
  sheepdog new-session --key K --title T --duty D [--chat oc_x[:all]]... [--message-ids a,b] [--note ...]
                       [--host antigravity|codex|claude] [--model M] [--effort E] [--quote 主人原话] [--dry-run]
                                                         总线临时新开一个专属会话（只在账本里）
  sheepdog close-session --topic T                       收掉总线新开的会话
  sheepdog push-rules [--topic T] [--dry-run]            立即给会话发一次完整现行规则
  sheepdog retire --topic <tp_x 或 conversation_id> [--successor-title ...]   给会话补发 / 手动发退休通知
  sheepdog actions [--all]                               排队 / 完成 / 失败的动作
  sheepdog loops [--clear <chat_id>]                     疑似和 Agent 循环而冷却中的聊天；手动解除
  sheepdog redeliver --batch B                           发送中断、不确定是否送达的批次：人工确认后重发
  sheepdog show <message_id>                             打印账本里这条消息的完整正文
  sheepdog show --topic T                                打印这个会话最近几轮的问答，告诉你去哪里接着看
  sheepdog inject --text 正文 [--chat oc_x] [--sender ou_x] ...  （只在 [source] enabled = false 时）放一条合成消息进账本
  sheepdog reply-card (--text 正文 | --text-file 文件或 -)  打印代回卡片 JSON（正文 + 右下角标记）
  sheepdog receipt --topic T --batch B --json '{...}'   由 session 调用，提交回执
  sheepdog session-reset --topic T                       人工把 attention/failed 复位（同时清掉羊群看板的中断 / 失联判定）
  sheepdog work set --topic T --key K --title ... --state S [--waiting-on 谁] [--until 几点] [--expect ...] [--to 会话]
  sheepdog work done --topic T --key K --result ...      羊群看板：登记 / 收掉会话名下的活（立即写入）
  sheepdog tell --topic T --to 会话 "话" [--key K] [--quote 主人原话]   给别的会话留言（下一批送到）
  sheepdog board [--topic T] [--json]                    羊群看板：各会话在干什么
  sheepdog notices [--all] [--show N] [--retry N|all] [--ack N|all]   给主人的通知：没发出去的；人工重发 / 确认已处理
  sheepdog ask --topic T --title 短标题 (--text 正文 | --file F) [--key K] [--batch B（默认当前 / 最近一批）]
                                                         经机器人找主人「需要你定」（sheepdog 加开头一行）；带 --key：
                                                         同一把钥匙第一次在主聊天里起一条，之后回到它的话题里（一事一串）
  sheepdog report ...（参数同 ask）                       经机器人向主人汇报
  sheepdog reply --topic T --to om_x (--card-text 正文 | --card-file F | --text 正文) [--even-if-owner-replied]
                                                         代回：发前只读检查主人有没有新发言；说过就不发（退出码 3）
  sheepdog thread --topic T (--key K | --thread omt_x)    只读：列出一个话题的全部消息

ask / report / reply 的退出码：0 发出（或同样的内容已经发过）/ 1 发送失败或超时（原样重试，防重编号保证只发一条）/
2 参数不对 / 3 主人已经回过、没发 / 4 发前检查失败、没发。代回内容一样才算重复：同一条先回「稍等」再回结论两条都发

--dry-run 一律在账本的内存副本上执行，不写真实账本（否则假的 conversation_id、onboarded_at 会留下来）。

spawn / new-session / push-rules / init / retire 要调 agentapi：只有常驻进程（sheepdog run）能跨项目投递，
其他进程里执行时只入队，由 run 下一轮执行（--dry-run 一律本地执行）。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from . import actions
from .ack import Acker
from .card import build_reply_card
from .config import ANTIGRAVITY, CLI_HOSTS, HOSTS, ConfigError, load_config
from . import duty
from .engine import (BUS_TOPIC_ID, DYNAMIC, HOST_LABEL, MANAGED_KINDS, NOTICE_STATE_LABEL, RETIRED, Collector,
                     Dispatcher, add_watch, attention_notice_key, attention_reason_label, auto_recovers,
                     bus_quote_verifier, close_session, display_time, duty_reset_key, escalation_summary,
                     forward_messages, open_escalations, owner_quote_verifier, unsent_notice_line, write_receipt)
from .models import Message, Mention
from .playbook import PLAYBOOK_FILES, Playbook
from .prompts import with_cli
from .roster import Roster, RosterError, check_hosts, load_roster
from .security import HOLD, TAG, SecurityConfig, SecurityError, load_security
from .sink import SinkError
from .sink.agentapi import AgentApiSink, DryRunSink
from .sink.claude import ClaudeSink
from .sink.codex import CodexSink, codex_home
from .sink.codex_profile import ProfileError, profile_overrides
from .notify import FileNotifier, OwnerDM
from . import outbound
from .source import NullSource
from .source.lark import LarkCliSource
from .store import Store, now_iso
from . import session as sm


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


class _Abort(Exception):
    pass


SINK_CLASSES = {"codex": CodexSink, "claude": ClaudeSink}
# 这些宿主配置要重启才生效（sink 是启动时建的）；model / effort / timeout_minutes / max_concurrent 下一轮生效
HOST_RESTART_KEYS = ("enabled", "command", "workdir", "settings", "profile")


def load_checked_roster(cfg) -> Roster:
    """读名册并检查 managed 会话引用的宿主都已启用（7.18）。"""
    roster = load_roster(cfg.roster_file, cfg.roster_required)
    check_hosts(roster, cfg.host_enabled)
    return roster


def _roster(cfg) -> Roster:
    """名册校验失败直接报错退出，不静默降级。"""
    try:
        return load_checked_roster(cfg)
    except RosterError as e:
        print(f"名册无效: {e}", file=sys.stderr)
        raise _Abort() from e


def _security(cfg) -> SecurityConfig:
    """安全规则格式错误直接报错退出（同名册）；文件不存在 = 没有规则。"""
    try:
        return load_security(cfg.security_file)
    except SecurityError as e:
        print(f"安全规则无效: {e}", file=sys.stderr)
        raise _Abort() from e


def _build(args):
    cfg = load_config()
    roster = _roster(cfg)
    security = _security(cfg)
    dry = getattr(args, "dry_run", False)
    store = Store.snapshot(cfg.db_path) if dry else Store(cfg.db_path)
    source = _source(cfg)
    return cfg, store, source, _sinks(cfg, store, dry), roster, security


def _source(cfg):
    """[source] enabled = false（测试环境）时不拉 IM（7.18）。"""
    if not cfg.source.enabled:
        return NullSource()
    return LarkCliSource(tz=cfg.timezone_offset, max_pages=cfg.max_pages)


def _sinks(cfg, store: Store, dry: bool) -> dict:
    """按宿主建 sink（7.18）：sink = dryrun 只管 Antigravity；--dry-run 一律打印。没启用的宿主不建。"""
    sinks: dict = {ANTIGRAVITY: DryRunSink() if dry or cfg.sink == "dryrun" else AgentApiSink()}
    for h in CLI_HOSTS:
        if cfg.hosts[h].enabled:
            sinks[h] = DryRunSink(h) if dry else SINK_CLASSES[h](cfg.hosts[h], store, cfg.runs_dir)
    return sinks


def _notifier(cfg, source, args):
    """给主人的通知（在岗表中断 / 失联、attention 和恢复、危险命令报警、写锁提醒、登录失效）怎么送：

    - 配了 [notify] file：一律追加写进那个文件（带时间和通知类型），不发飞书（测试环境用；拉不拉 IM 都一样），不重试；
    - 否则经机器人私聊主人（OwnerDM，带防重编号）；先落给主人的通知账本再发，发不出去按退避重试；
      不拉 IM、没有 self_open_id 时没有渠道（只记日志）。
    dry-run 一律只打印，不写文件、不发飞书。"""
    path = cfg.notify_path
    dry = getattr(args, "dry_run", False)
    if path is not None:
        if dry:
            return lambda text: print(f"[dryrun] 给主人的通知（正式运行写进 {path}，不发飞书）：\n{text}\n")
        return FileNotifier(path, cfg.timezone_offset)
    if not cfg.self_open_id or not hasattr(source, "send_text"):
        return None
    if dry:
        return lambda text: print(f"[dryrun] 给主人发私聊：\n{text}\n")
    return OwnerDM(source, cfg.self_open_id)


def notify_report(cfg) -> str:
    """doctor：给主人的通知送到哪。设了 [notify] file 又在拉飞书，多半是正式配置误设了，提示一句。"""
    path = cfg.notify_path
    if path is not None:
        line = f"主人通知: 写到文件 {path}（[notify] file），不发飞书"
        if cfg.source.enabled:
            line += "\n  ⚠ 主人通知写到文件，不发飞书：现在在拉飞书（[source] enabled = true），正式配置不要设 [notify] file"
        return line
    if not cfg.source.enabled:
        return "主人通知: ⚠ 不拉飞书又没设 [notify] file，给主人的通知只记日志（测试环境可设 [notify] file 写到文件）"
    if not cfg.self_open_id:
        return "主人通知: ⚠ 没配 self_open_id，给主人的通知只记日志"
    return "主人通知: 经机器人私聊"


def _acker(cfg, store, source, args) -> Acker | None:
    """确认表情（7.9）：没有 [ack] 段就不建。dry-run（含 sink = dryrun）只打印，不调接口。"""
    if not cfg.ack.enabled or not cfg.source.enabled:
        return None
    dry = getattr(args, "dry_run", False) or cfg.sink == "dryrun"
    return Acker(cfg.ack, store, source, dry_run=dry)


def _collect_warning(store: Store) -> str:
    """连续 3 轮以上没拉完时的提示（7.16），否则空串。"""
    streak = int(store.get_meta("partial_streak", "0") or 0)
    if streak >= 3:
        return f"⚠ 采集不完整：已连续 {streak} 轮没拉完（从 {store.get_meta('partial_start')} 起），可调大 max_pages"
    return ""


def cmd_doctor(args) -> int:
    cfg = load_config()
    ok = True
    print(f"sheepdog {__version__}")
    # 开头先说有没有给主人的通知没发出去（还在重试的、放弃了没人处理的），直到人工处理
    if warn := unsent_notice_line(Store(cfg.db_path), cfg.cli_command):
        print(warn)
    print(f"配置文件: {cfg.config_path} {'✓' if cfg.config_path.exists() else '✗ 不存在（使用默认值）'}")
    print(f"状态目录: {cfg.state_dir}")
    print(cli_name_report(cfg))
    print(notify_report(cfg))
    print(f"self_open_id: {'✓ 已配置' if cfg.self_open_id else '✗ 未配置（@我/回复我 判定会失效）'}")
    ok &= bool(cfg.self_open_id)
    if cfg.source.enabled:
        lark = shutil.which("lark-cli")
        print(f"lark-cli: {lark or '✗ 未找到'}")
        ok &= bool(lark)
    else:
        print("消息源: [source] enabled = false，不拉 IM（测试环境，用 sheepdog inject 放消息）")
    avail, why = AgentApiSink().available()
    print(f"agentapi: {'✓' if avail else '✗ ' + why}")
    store = Store(cfg.db_path)
    for line in host_report(cfg, store):
        print(line)
    overlay = cfg.prompt_overlay()
    print(f"prompt overlay: {'✓ ' + str(len(overlay)) + ' 字' if overlay else '（无）'}")
    try:
        r = load_roster(cfg.roster_file, cfg.roster_required)
        exists = cfg.roster_file.exists()
        print(f"名册: {cfg.roster_file} " + (f"✓ {len(r.managed)} managed / {len(r.sessions) - len(r.managed)} known"
                                            if exists else "（不存在，所有信号进总线）"))
    except RosterError as e:
        print(f"名册: ✗ {e}")
        ok = False
    try:
        sec = load_security(cfg.security_file)
        if not sec.loaded:
            print(f"安全规则: ⚠ {cfg.security_file} 不存在，没有任何规则")
        else:
            c = sec.counts()
            print(f"安全规则: {cfg.security_file} ✓ {len(sec.rules)} 条（tag {c[TAG]} / hold {c[HOLD]}）")
        print(f"own_tenant_keys: {'✓ ' + str(len(sec.own_tenant_keys)) + ' 个' if sec.own_tenant_keys else '⚠ 未配置（所有发送方都按外部人处理）'}"
              f"  quote 回看 {sec.quote_max_age_hours:g} 小时")
    except SecurityError as e:
        print(f"安全规则: ✗ {e}")
        ok = False
    if warn := _collect_warning(Store(cfg.db_path)):
        print(warn)
    a = cfg.ack
    print(f"确认表情: {'✓ ' + a.emoji_type + '，点: ' + ','.join(a.reasons) + '；回复后撤: ' + ','.join(a.remove_on_reply_reasons) + '；bot 身份点: ' + (','.join(a.bot_reasons) or '-') if a.enabled else '（未配置 [ack]，不点表情）'}")
    lg = cfg.loop_guard
    print(f"防循环: Agent 名字 {len(lg.agent_sender_names)} 个 / id {len(lg.agent_sender_ids)} 个 / "
          f"机器人一律算 Agent {'是' if lg.treat_all_bots_as_agents else '否'}；"
          f"{lg.window_minutes:g} 分钟内代回超过 {lg.max_agent_replies} 次冷却 {lg.cooldown_minutes:g} 分钟"
          + ("" if cfg.owner_context.skip_prefixes or cfg.owner_context.agent_markers
             else "（⚠ owner_context 的 skip_prefixes 和 agent_markers 都为空，无法识别代回，熔断不生效）"))
    esc = cfg.escalation
    print(f"找主人聊天: {'✓ ' + str(len(esc.chat_ids)) + ' 个，未结保留 ' + format(esc.open_hours, 'g') + ' 小时' if esc.chat_ids else '（未配置：主人在 IM 上的回复不会送回会话）'}")
    for line in threads_report(cfg):
        print(line)
    # playbook 缺文件只告警不判失败：缺的那一段在 prompt 里为空
    pb = Playbook.from_config(cfg)
    missing = pb.missing()
    print(f"playbook: {pb.directory} " + (f"✓ {len(PLAYBOOK_FILES)} 个文件齐全" if not missing
                                          else f"缺 {len(missing)} 个（对应段落为空）"))
    for name in missing:
        print(f"  ✗ 缺 {name}：{PLAYBOOK_FILES[name]}")
    for line in guard_report(cfg, Store(cfg.db_path), pb):
        print(line)
    for line in attention_report(cfg, store):
        print(line)
    print(duty_report(cfg, store))
    return 0 if ok else 1


def threads_report(cfg) -> list[str]:
    """doctor：一事一串（7.22）。ask / report 加的开头一行要能被 [escalation] 的正则认出来，否则「需要你定」登记不上、
    主人的回复送不回去。"""
    tc, esc = cfg.threads, cfg.escalation
    out = [f"一事一串: 话题里除了开头那条满 {tc.complex_min_replies} 条算复杂，附最近 {tc.recent_replies} 条"]
    sample = "tp_doctor_check"
    ask = tc.header("ask", "示例", sample)
    report = tc.header("report", "示例", sample)
    if esc.chat_ids:
        rx = esc.pattern
        hit = rx.search(ask) if rx is not None else None
        out.append(f"  ask 开头一行「{ask}」" + ("✓ 能被 header_regex 认出来" if hit and hit.group("topic") == sample
                                                 else "✗ 认不出来：ask 发的「需要你定」登记不上，改 [threads] ask_header 或 header_regex"))
        if rx is not None and rx.search(report):
            out.append(f"  ⚠ report 开头一行「{report}」也被 header_regex 认成「需要你定」了")
        ax = esc.attribution
        if ax is not None:
            for name, line in (("ask", ask), ("report", report)):
                m = ax.search(line)
                if not (m and m.group("topic") == sample):
                    out.append(f"  ⚠ {name} 开头一行认不出 topic（attribution_regex），主人引用回复它时靠发送时记的来源")
    return out


def duty_report(cfg, store: Store) -> str:
    """doctor：在岗表（7.20）的阈值和现在卡住的会话。"""
    dc = cfg.duty
    stuck = [c["topic_id"] for c in store.list_cards() if c["run_state"] in duty.STUCK]
    return (f"羊群看板: 中断 {dc.stall_minutes:g} 分钟 / 失联 {dc.lost_minutes:g} 分钟 / 空转 {dc.spin_repeat} 次；"
            f"编号规则 {len(dc.key_patterns)} 条；留言同一对 {dc.mail_window_minutes:g} 分钟内超过 {dc.mail_pair_limit} 条暂停；"
            f"路由事实算 {dc.routed_active_hours:g} 小时；自报的活 {dc.stale_hours:g} 小时没更新标可能过期；"
            f"履历保留 {dc.history_days} 天"
            + (f"；⚠ 中断 / 失联：{', '.join(stuck)}" if stuck else ""))


def guard_report(cfg, store: Store, pb: Playbook) -> list[str]:
    """doctor 的护栏一节（7.19）：哪些会话有每批红线（K1），命令审计的配置（K3）。"""
    keys = pb.reminder_keys()
    live = {"bus"} | {t["topic_id"].removeprefix("tp_") for t in store.list_topics()
                      if t["kind"] in ("adopted", "dynamic") and t["state"] != sm.CLOSED}
    try:
        live |= {s.key for s in load_checked_roster(cfg).managed}
    except RosterError:
        pass
    lines = [f"每批红线: {pb.directory / 'reminders'} " + ("（没有，批次末尾不附红线）" if not keys
                                                          else f"✓ {len(keys)} 个会话：{', '.join(keys)}")]
    if stale := [k for k in keys if k not in live]:
        lines.append(f"  ⚠ 这些红线文件没有对应的会话（key 写错了？）：{', '.join(stale)}")
    a = cfg.audit
    if a.danger_patterns and a.topics:
        lines.append(f"命令审计: 审 {', '.join(a.topics)}，{len(a.danger_patterns)} 条危险命令、{len(a.ignore_patterns)} 条忽略规则；"
                     f"命中后私聊主人、提示犯规会话"
                     + (f"、提示 {', '.join(a.also_notify)}" if a.also_notify else ""))
    else:
        lines.append("命令审计: 关（[audit] topics 或 danger_patterns 为空）")
    return lines


def cli_name_report(cfg) -> str:
    """doctor：给会话看的命令名（cli_name）；不是默认的 sheepdog 时再看它解析到的路径在不在。"""
    line = f"cli_name: {cfg.cli_name}"
    if cfg.cli_name == "sheepdog":
        return line + "（默认）"
    name = cfg.cli_name
    path = Path(name).expanduser() if ("/" in name or name.startswith("~")) else None
    found = (str(path) if path.is_file() else "") if path is not None else (shutil.which(name) or "")
    return line + (f" -> {found} ✓" if found else f" ✗ 找不到（会话敲这条命令会失败）：{path or name}")


def _codex_sandbox(profile: str = "") -> dict:
    """Codex 生效的沙箱设置（只读）：设了 profile 读 ~/.codex/<profile>.config.toml，没设读 ~/.codex/config.toml
    （CODEX_HOME 可改根目录）。返回 {file, mode, net, roots}；文件读不到时各项都是「读不到」，文件里没写的项是「没写」。"""
    p = codex_home() / (f"{profile}.config.toml" if profile else "config.toml")
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {"file": str(p), "mode": "读不到", "net": "读不到", "roots": "读不到"}
    missing = "没写（沿用用户配置）" if profile else "没写"
    ws = data.get("sandbox_workspace_write") or {}
    mode = data.get("sandbox_mode")
    net = ws.get("network_access")
    roots = ws.get("writable_roots")
    return {"file": str(p), "mode": str(mode) if mode else missing,
            "net": missing if net is None else ("开" if net else "关"),
            "roots": missing if roots is None else (", ".join(map(str, roots)) if roots else "（空）")}


def _claude_login(exe: str) -> str:
    try:
        out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return f"读不到（{e}）"
    text = (out.stdout or out.stderr).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return f"读不到（{text[:80]}）"
    return "✓ 已登录" if data.get("loggedIn") else "✗ 没登录：在终端跑 `claude auth login`"


def host_report(cfg, store: Store) -> list[str]:
    """sheepdog doctor 的宿主一节（7.18 第 3 条）：命令在不在、登录状态、Codex 的沙箱和网络。"""
    lines = []
    for h in CLI_HOSTS:
        hc = cfg.hosts[h]
        label = HOST_LABEL[h]
        if not hc.enabled:
            lines.append(f"{label}: 未启用（[hosts.{h}] enabled = false）")
            continue
        exe = shutil.which(hc.executable) or (hc.executable if Path(hc.executable).expanduser().is_file() else "")
        lines.append(f"{label}: 命令 {exe or '✗ 找不到 ' + hc.executable}；workdir {hc.workdir}"
                     f"{'' if hc.workdir_path.is_dir() else ' ✗ 不存在'}；默认模型 {hc.model or '（宿主默认）'} / "
                     f"档位 {hc.effort or '（宿主默认）'}；超时 {hc.timeout_minutes:g} 分钟，并发 {hc.max_concurrent}；"
                     f"新开先问主人 {'开' if hc.ask_owner_before_new else '关'}")
        u = store.get_meta(f"host_unavailable:{h}")
        if u:
            lines.append(f"  ⚠ 标为不可用：{json.loads(u).get('reason', '')}")
        if h == "claude":
            lines.append(f"  登录: {_claude_login(hc.executable) if exe else '读不到'}")
            if hc.settings:
                lines.append(f"  权限: 来自 --settings {hc.settings}（sheepdog 不写它的内容）")
            else:
                lines.append("  权限: 来自 Claude 自己的用户设置和 workdir 下的设置")
        else:
            last = store.conn.execute("SELECT * FROM runs WHERE host='codex' AND ended_at IS NOT NULL "
                                      "ORDER BY ended_at DESC LIMIT 1").fetchone()
            if last is None:
                lines.append("  登录: 还没跑过，看不出来")
            elif last["error_kind"] == "auth":
                lines.append(f"  登录: ✗ 最近一次运行报登录失效（{last['ended_at']}）：在终端跑 `codex login`")
            else:
                lines.append(f"  登录: ✓ 最近一次运行（{last['ended_at']}）没有登录类报错")
            lines.append("  提示: Codex 应用侧边栏只列登记过的项目下的线程；新建的线程要重开应用才看得到")
            sb = _codex_sandbox(hc.profile)
            mode, net = sb["mode"], sb["net"]
            if hc.profile:
                lines.append(f"  配置档: profile = {hc.profile}，读的是 {sb['file']}；sheepdog 把它展开成 -c 参数传给自己起的 "
                             "app-server（主人在 Codex 应用里的会话不受影响）")
                lines.append("  提示: 配置档只对之后新开的 Codex 会话生效；改了配置档，老会话要退休换新才会跟着变")
                try:
                    lines.append("  展开: " + " ".join(profile_overrides(hc.profile)))
                except ProfileError as e:
                    lines.append(f"  ✗ 配置档有问题，这个宿主的每一轮都会直接失败：{e}")
            else:
                lines.append(f"  配置档: 没设（只用用户配置 {sb['file']}）")
            source = (f"来自配置档 {sb['file']}，sheepdog 展开成 -c 传给自己起的 app-server" if hc.profile
                      else "来自 Codex 用户配置，sheepdog 不传权限参数")
            lines.append(f"  沙箱: sandbox_mode = {mode}，网络 {net}，writable_roots = {sb['roots']}（{source}）")
            if mode == "read-only" or net == "关":
                lines.append("  ⚠ 这个宿主的会话能读、能分析，但发不了飞书")
    return lines


def _cycle(collector: Collector, dispatcher: Dispatcher | None) -> None:
    stats = collector.poll_once()
    logging.info("poll: %s", stats)
    if dispatcher:
        res = dispatcher.dispatch_once()
        logging.info("dispatch: %s", res)


def cmd_poll(args) -> int:
    cfg, store, source, sink, roster, security = _build(args)
    acker = _acker(cfg, store, source, args)
    collector = Collector(cfg, store, source, roster, security, acker)
    dispatcher = None if args.no_dispatch else Dispatcher(cfg, store, sink, roster, security, acker,
                                                          _notifier(cfg, source, args))
    _cycle(collector, dispatcher)
    return 0


def _queued(args, store: Store, cfg, kind: str, payload: dict) -> int | None:
    """不在常驻进程里、又不是 --dry-run：入队并返回退出码 0；否则返回 None 表示本地执行（7.12）。"""
    if getattr(args, "dry_run", False) or actions.is_resident():
        return None
    aid = actions.enqueue(store, kind, payload)
    print(f"动作 #{aid}（{kind}）{with_cli(actions.QUEUED_HINT, cfg.cli_command)}")
    return 0


def _reload_config(old, args, store: Store, source, collector: Collector, dispatcher: Dispatcher):
    """run 每轮重读 config.toml（7.16）：改坏了沿用上一份并 WARNING；state_dir / sink 变了只提示需要重启。"""
    try:
        new = load_config()
    except ConfigError as e:
        logging.warning("配置无效，沿用上一份: %s", e)
        return old
    if new.state_dir != old.state_dir:
        logging.warning("state_dir 改成了 %s，需要重启 sheepdog 才生效；本次沿用 %s", new.state_dir, old.state_dir)
        new.state_dir = old.state_dir
    if new.sink != old.sink:
        logging.warning("sink 改成了 %s，需要重启 sheepdog 才生效；本次沿用 %s", new.sink, old.sink)
        new.sink = old.sink
    if new.source.enabled != old.source.enabled:
        logging.warning("[source] enabled 改成了 %s，需要重启 sheepdog 才生效；本次沿用 %s",
                        new.source.enabled, old.source.enabled)
        new.source.enabled = old.source.enabled
    for h in CLI_HOSTS:
        # 宿主的 sink 是启动时建的：开关、命令、工作目录、settings 要重启；模型、档位、超时、并发下一轮生效
        for k in HOST_RESTART_KEYS:
            if getattr(new.hosts[h], k) != getattr(old.hosts[h], k):
                logging.warning("[hosts.%s] %s 改了，需要重启 sheepdog 才生效；本次沿用 %r", h, k, getattr(old.hosts[h], k))
                setattr(new.hosts[h], k, getattr(old.hosts[h], k))
        sink = dispatcher.sinks.get(h)
        if sink is not None and hasattr(sink, "update_config"):
            sink.update_config(new.hosts[h])
    collector.cfg = dispatcher.cfg = new
    dispatcher.playbook = Playbook.from_config(new)  # 代回前缀、等待分钟数等占位符跟着新配置
    if hasattr(source, "max_pages"):
        source.max_pages = new.max_pages
    if hasattr(source, "tz"):
        source.tz = new.timezone_offset
    collector.acker = dispatcher.acker = _acker(new, store, source, args)
    # 主人通知的去向跟着配置走（[notify] file 改了下一轮生效）
    dispatcher.notifier = _notifier(new, source, args)
    return new


def cmd_run(args) -> int:
    # 本进程是常驻进程：只有它能跨项目调 agentapi，会话里发起的动作由它执行
    actions.mark_resident()
    cfg, store, source, sink, roster, security = _build(args)
    acker = _acker(cfg, store, source, args)
    collector = Collector(cfg, store, source, roster, security, acker)
    dispatcher = Dispatcher(cfg, store, sink, roster, security, acker, _notifier(cfg, source, args))
    logging.info("sheepdog 常驻运行，间隔 %ss，宿主 %s", args.interval or cfg.poll_interval_seconds,
                 "、".join(f"{h}={s.name}" for h, s in sink.items()))
    last_prune = 0.0
    while True:
        try:
            # 每轮重读 config.toml：轮询间隔、关键词、路由、ack、watch、loop_guard、owner_context 等下一轮生效
            cfg = _reload_config(cfg, args, store, source, collector, dispatcher)
            # 每轮重读名册：改名册不必重启；改坏了沿用上一份有效名册并报错，常驻进程不退出
            try:
                collector.roster = dispatcher.roster = load_checked_roster(cfg)
            except RosterError as e:
                logging.error("名册无效，沿用上一份: %s", e)
            # 安全规则同样每轮现读，改坏了沿用上一份有效规则
            try:
                collector.security = dispatcher.security = load_security(cfg.security_file)
            except SecurityError as e:
                logging.error("安全规则无效，沿用上一份: %s", e)
            # 先执行会话发起的动作（spawn / new-session / push-rules / init / retire），再拉取和投递
            if n := actions.run_pending(dispatcher):
                logging.info("执行动作 %d 条", n)
            _cycle(collector, dispatcher)
            if time.time() - last_prune > 3600:
                pruned = store.prune(cfg.retention_days, cfg.receipts_dir, cfg.runs_dir, cfg.duty.history_days)
                last_prune = time.time()
                if any(pruned.values()):
                    logging.info("清理超过 %d 天的数据: %s", cfg.retention_days,
                                 {k: v for k, v in pruned.items() if v})
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # 单轮失败不退出，下一轮从水位线续跑
            if getattr(args, "verbose", False):
                logging.exception("本轮失败")
            else:
                logging.error("本轮失败（下一轮从水位线续跑）: %s: %s", type(e).__name__, e)
        time.sleep(args.interval or cfg.poll_interval_seconds)


def cmd_inbox(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    if args.chat:
        rows = store.inbox_messages(args.chat, args.limit)
        if not rows:
            print("没有匹配的 Inbox 消息")
            return 0
        for r in reversed(rows):
            read = "✓" if r["is_read"] == 1 else "•"
            print(f"{read} [{r['create_time']}] 「{r['chat_name']}」{r['sender_name']}: {(r['content'] or '').strip()[:300]}")
        return 0
    rows = store.inbox_summary(cfg.self_open_id)
    total = sum(r["unread"] or 0 for r in rows)
    print(f"📥 Inbox：{total} 条未读，{len(rows)} 个会话")
    for r in rows:
        tag = "（Bot 私聊）" if r["reason"] == "bot_p2p" else ""
        owner = f"，主人最近发言 {r['owner_last']}" if r["owner_last"] else ""
        print(f"  「{r['chat_name'] or r['chat_id']}」{tag} 未读 {r['unread'] or 0} / 共 {r['total']}，最近 {r['last_time']}{owner}")
    return 0


def cmd_inbox_clear(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    if not args.all and not args.chat:
        print("请指定 --chat <群名> 或 --all")
        return 2
    n = store.inbox_clear(None if args.all else args.chat)
    print(f"已标记处理 {n} 条（不影响 IM 里的已读状态）")
    return 0


def cmd_init(args) -> int:
    cfg, store, _source, sink, roster, security = _build(args)
    if (rc := _queued(args, store, cfg, "init", {})) is not None:
        return rc
    if args.dry_run:
        print("[dry-run] 在账本内存副本上执行，不写真实账本、不调用 agentapi\n")
    print(f"名册: {cfg.roster_file}（{len(roster.managed)} managed / {len(roster.sessions) - len(roster.managed)} known）")
    rep = Dispatcher(cfg, store, sink, roster, security).init()
    for k, label in (("created", "新登记"), ("updated", "已更新"), ("reopened", "重新启用"), ("closed", "已移除→closed")):
        if rep["roster"][k]:
            print(f"  {label}: {', '.join(rep['roster'][k])}")
    print(f"总线: {rep['bus']}")
    if not rep["onboarding"]:
        print("onboarding: 名册里没有 managed 会话")
    for tid, what in rep["spawn"].items():
        print(f"spawn {tid}: {what}")
    for tid, what in rep["onboarding"].items():
        print(f"onboarding {tid}: {what}")
    return 0


def cmd_spawn(args) -> int:
    cfg, store, _source, sink, roster, security = _build(args)
    if not args.dry_run and not actions.is_resident():
        s = roster.by_key(args.key)
        if s is None or not s.to_spawn:
            print(f"spawn 被拒绝: {args.key} 不是名册里 conversation_id 留空的 managed 条目", file=sys.stderr)
            return 2
    if (rc := _queued(args, store, cfg, "spawn", {"key": args.key})) is not None:
        return rc
    if args.dry_run:
        print("[dry-run] 在账本内存副本上执行，不写真实账本、不调用 agentapi\n")
    d = Dispatcher(cfg, store, sink, roster, security)
    d.sync_roster()
    try:
        r = d.spawn(args.key)
    except ValueError as e:
        print(f"spawn 被拒绝: {e}", file=sys.stderr)
        return 2
    except SinkError as e:
        print(f"spawn 失败: {e}", file=sys.stderr)
        return 1
    if r.get("skipped"):
        print(f"跳过：{r['skipped']}")
        return 0
    if r.get("retire"):
        print(f"前任退休通知：{r['retire']}")
    print(f"已新建 {r['title']} -> {r['conversation_id']}（只记在账本，不回写名册）")
    if r.get("read_batch"):
        print(f"等待接手会话读完前任上下文并回执（批次 {r['read_batch']}），期间信号排队")
    return 0


_MODE = {"adopted": "managed", "known": "known", "bus": "bus", "dynamic": "dynamic（总线新开）"}


def cmd_sessions(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    try:
        roster = load_roster(cfg.roster_file, cfg.roster_required)
    except RosterError as e:  # 只是展示用，名册坏了照样列出账本里的记录
        print(f"（名册无效，聊天名称显示为 ID: {e}）")
        roster = Roster()
    rows = store.list_topics()
    if warn := _collect_warning(store):
        print(warn)
    if not rows:
        print("暂无 session")
    # 只用来算规则指纹，不投递
    rules = Dispatcher(cfg, store, DryRunSink(), roster)
    uncertain: dict[str, list] = {}
    for u in store.dispatches_in_state("uncertain"):
        uncertain.setdefault(u["topic_id"], []).append(u)
    for t in rows:
        mode = _MODE.get(t["kind"], t["kind"] or "-")
        host = rules.topic_host(t)
        model, effort = rules.model_effort(t)
        cols = (f"  宿主={host}  模型={(model or '（宿主默认）') + ('' if t['model'] or not model else '（默认）')}"
                + (f"  档位={(effort or '（宿主默认）') + ('' if t['effort'] or not effort else '（默认）')}"
                   if host != ANTIGRAVITY else ""))
        print(f"- {t['title']}  [{t['state']}]  mode={mode}  topic={t['topic_id']}  conversation={t['conversation_id'] or '-'}"
              + cols)
        print(f"  职责: {t['duty']}")
        anchors = json.loads(t["anchors_json"] or "[]")
        projects = [a.removeprefix("project:") for a in anchors if a.startswith("project:")]
        if projects:
            print(f"  关联项目: {'、'.join(projects)}")
        if t["kind"] in ("adopted", "known"):
            s = roster.by_topic(t["topic_id"])
            names = {c.chat_id: f"{c.name or c.chat_id}{'（全部）' if c.all_messages else ''}" for c in (s.chats if s else [])}
            chats = [names.get(c, c) for c in anchors if c.startswith("oc_")]
            print(f"  负责的聊天: {'、'.join(chats) or '-'}")
            if s and (s.conversation_id or None) != t["conversation_id"]:
                # 名册是人写的，运行态以账本为准；两者不一致时显示出来
                rc = s.conversation_id or "（留空：由 sheepdog 新建）"
                print(f"  名册 conversation: {rc}  账本: {t['conversation_id'] or '（尚未新建）'}"
                      + (f"  spawn 于 {t['spawned_at']}" if t["spawned_at"] else ""))
            if s and s.predecessor_conversation_id:
                print(f"  前任: {s.predecessor_conversation_id}")
            if t["kind"] == "adopted":
                print(f"  onboarded: {t['onboarded_at'] or '否'}  未回执: {t['receipt_missed'] or 0}")
        if t["kind"] in ("adopted", "bus", "dynamic"):
            print(f"  未结「需要你定」: {len(open_escalations(store, cfg.escalation.open_hours, t['topic_id']))}")
        if t["kind"] == DYNAMIC:
            chats = []
            for a in anchors:
                if a.startswith("oc_"):
                    cid, _, flag = a.partition(":")
                    chats.append(cid + ("（全部）" if flag == "all" else ""))
            print(f"  负责的聊天: {'、'.join(chats) or '-'}")
            print(f"  创建于: {t['created_at']}  创建原因: {t['origin_note'] or '-'}")
        if t["kind"] == RETIRED:
            print(f"  交接回执: {'已转给接手会话' if not t['pending_batch_id'] else '等待中（批次 ' + t['pending_batch_id'] + '）'}")
        if t["summary"]:
            print(f"  进展: {t['summary']}")
        if t["state"] != sm.CLOSED and (status := rules.rules_status(t)):
            print(f"  规则: {status}")
        if host != ANTIGRAVITY:
            run = store.latest_run_for_topic(t["topic_id"])
            if run is not None:
                extra = f"，下次重试 {run['retry_at']}" if run["state"] == "retry_wait" else ""
                err = f"（{run['error_kind']}：{(run['error'] or '')[:120]}）" if run["error_kind"] else ""
                print(f"  最近一轮: {run['batch_id']} [{run['state']}] 第 {run['attempts'] or 0} 次，"
                      f"开始 {run['started_at']}{extra}{err}")
                if run["denials"]:
                    print(f"  被拒的工具: {', '.join(json.loads(run['denials']))}")
        for u in uncertain.get(t["topic_id"], []):
            print(f"  ⚠ 批次 {u['batch_id']} 发送中断、不确定是否送达（{u['sent_at']}），"
                  + with_cli(f"确认后: `sheepdog redeliver --batch {u['batch_id']}`", cfg.cli_command))
        if t["state"] == sm.ATTENTION:
            print("  " + attention_line(t, cfg.cli_command))
        print(f"  最近活动: {t['last_active']}  待回执批次: {t['pending_batch_id'] or '-'}  重试: {t['retries']}")
    return 0


def attention_line(t, cli_command: str = "sheepdog") -> str:
    """一个卡在 attention 的会话：从几点起、为什么、会不会自动恢复（7.21）。"""
    reason = t["attention_reason"]
    how = ("会自动恢复（晚到的回执或会话记录里的新动作）" if auto_recovers(reason)
           else with_cli(f"不会自动恢复，处理好以后 `sheepdog session-reset --topic {t['topic_id']}`", cli_command))
    return (f"attention: 自 {t['attention_at'] or '（没记录）'}，原因 {attention_reason_label(reason)}"
            f"（{reason or '-'}），{how}")


def attention_report(cfg, store: Store) -> list[str]:
    """doctor：卡在 attention 的会话（7.21）。"""
    stuck = [t for t in store.list_topics() if t["state"] == sm.ATTENTION]
    if not stuck:
        return ["attention: 没有卡住的会话"]
    lines = [f"attention: ⚠ {len(stuck)} 个会话卡住，信号在排队"]
    for t in stuck:
        lines.append(f"  - {t['title']}（{t['topic_id']}）{attention_line(t, cfg.cli_command).removeprefix('attention: ')}")
    return lines


def cmd_receipt(args) -> int:
    cfg = load_config()
    try:
        receipt = json.loads(args.json)
        p = write_receipt(cfg, args.topic, args.batch, receipt)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"回执无效: {e}", file=sys.stderr)
        return 2
    print(f"回执已提交: {p}")
    return 0


def cmd_forward(args) -> int:
    cfg = load_config()
    security = _security(cfg)
    store = Store(cfg.db_path)
    verify = bus_quote_verifier(store, security.quote_max_age_hours)
    try:
        ids = forward_messages(store, args.topic, (args.message_ids or "").split(","), args.note, args.quote, verify,
                               cfg.self_open_id, cfg.escalation.open_hours, cfg.duty.patterns)
    except ValueError as e:
        print(f"转交被拒绝: {e}", file=sys.stderr)
        return 2
    extra = "，附主人原话 / 总线备注" if args.quote or args.note else ""
    print(f"已转交 {len(ids)} 条消息{extra}到 {args.topic}，sheepdog 下一轮投递（对方被主人接管时排队）")
    return 0


def cmd_watch(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    try:
        wid = add_watch(store, args.topic, args.person, args.chat, args.note)
    except ValueError as e:
        print(f"登记被拒绝: {e}", file=sys.stderr)
        return 2
    w = cfg.watch
    remind = "/".join(map(str, w.remind_minutes)) or "-"
    print(f"已登记等待 #{wid}：对方回复会直接推给 {args.topic}；{remind} 分钟提醒，{w.expire_minutes} 分钟到期。"
          + with_cli(f"取消：`sheepdog unwatch --id {wid}`", cfg.cli_command))
    return 0


def cmd_watches(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    rows = store.list_watches(include_closed=args.all)
    if not rows:
        print("没有等待中的回复" if not args.all else "没有等待记录")
    for w in rows:
        if w["closed_at"]:
            state = f"已结束 {w['close_reason']} @ {w['closed_at']}"
        elif w["status"] == "replied":
            state = f"对方已回复、等会话确认（{w['last_reply_at']}，消息 {w['last_reply_message_id']}）"
        else:
            state = "等待中" + (f"（从 {w['last_reply_at']} 的回复重新计时）" if w["last_reply_at"] else "")
        nudged = "/".join(m for m, k in (("15", "nudged_15_at"), ("30", "nudged_30_at")) if w[k]) or "-"
        print(f"#{w['id']} [{state}] topic={w['topic_id']} person={w['person_id']} chat={w['chat_id'] or '任意'} "
              f"开始 {w['started_at']} 已提醒 {nudged}")
        if w["note"]:
            print(f"  在等: {w['note']}")
    return 0


def cmd_watch_done(args) -> int:
    """会话确认真正等到了对方的回复（7.16）。"""
    cfg = load_config()
    store = Store(cfg.db_path)
    w = store.get_watch(args.id)
    if not w:
        print("没有这条等待", file=sys.stderr)
        return 2
    if w["closed_at"]:
        print(f"#{args.id} 已结束（{w['close_reason']}）")
        return 0
    store.close_watch(args.id, "done")
    print(f"等待 #{args.id} 已确认等到，关闭")
    return 0


def cmd_unwatch(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    w = store.get_watch(args.id)
    if not w:
        print("没有这条等待", file=sys.stderr)
        return 2
    if w["closed_at"]:
        print(f"#{args.id} 已结束（{w['close_reason']}）")
        return 0
    store.close_watch(args.id, "cancelled")
    print(f"已取消等待 #{args.id}")
    return 0


def _parse_since(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", text or "")
    if not m:
        raise ValueError(f"--since 格式应为 30m / 24h / 7d，收到 {text!r}")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(minutes=n) if unit == "m" else timedelta(hours=n) if unit == "h" else timedelta(days=n)


def cmd_security_log(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    try:
        since = datetime.now().astimezone() - _parse_since(args.since)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    rows = store.security_log(since.isoformat(timespec="seconds"))
    if not rows:
        print(f"最近 {args.since} 没有被标记或拦截的消息")
        return 0
    print(f"最近 {args.since} 被标记 / 拦截 {len(rows)} 条：")
    for r in rows:
        where = "私聊" if r["chat_type"] == "p2p" else f"群「{r['chat_name'] or r['chat_id']}」"
        rules = ", ".join(json.loads(r["security_tags"] or "[]"))
        print(f"- [{r['create_time']}] {where} | {r['sender_name']} ({r['sender_id']}) | 规则: {rules} | "
              f"动作: {r['security_action']} | 去向: {r['topic_id'] or '-'}（{r['dispatch_state'] or '-'}）")
        print(f"  message_id: {r['message_id']}")
    return 0


def cmd_escalations(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    open_ids = {e["message_id"] for e in open_escalations(store, cfg.escalation.open_hours)}
    rows = store.list_escalations(include_closed=args.all)
    if not args.all:
        rows = [e for e in rows if e["message_id"] in open_ids]  # 超时还没被常驻进程关掉的也不算未结
    if not rows:
        print("没有未结的「需要你定」" if not args.all else "没有「需要你定」记录")
        return 0
    for e in rows:
        if e["answered_at"]:
            state = f"已答 @ {e['answered_at']}（答复 {e['answer_message_id']}）"
        elif e["message_id"] in open_ids:
            state = "未结"
        else:
            state = f"已关闭（{e['close_reason'] or '超时'}）"
        print(f"- [{state}] topic={e['topic_id']} 提问 {e['asked_at']} message_id={e['message_id']}")
        print(f"  问题: {escalation_summary(e)}")
    return 0


def cmd_acks(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    rows = store.list_acks(open_only=args.open)
    if not rows:
        print("没有未撤的确认表情" if args.open else "没有确认表情记录")
        return 0
    for a in rows:
        if not a["reaction_id"]:
            state = f"点失败：{a['error']}"
        elif a["removed_at"]:
            state = f"已撤 @ {a['removed_at']}"
        elif a["error"]:
            state = f"未撤（{a['error']}）"
        else:
            state = "未撤"
        where = "私聊" if a["chat_type"] == "p2p" else f"群「{a['chat_name'] or a['chat_id']}」"
        print(f"- [{state}] {a['added_at']} {where} | {a['sender_name'] or a['sender_id']} | 原因 {a['reason']} | "
              f"身份 {a['identity'] or 'user'} | message_id={a['message_id']}")
    return 0


def _parse_chat(text: str) -> tuple[str, bool]:
    cid, _, flag = text.strip().partition(":")
    if flag not in ("", "all"):
        raise ValueError(f"--chat {text!r} 格式应为 oc_xxx 或 oc_xxx:all")
    return cid, flag == "all"


def cmd_new_session(args) -> int:
    cfg, store, _source, sink, roster, security = _build(args)
    if not args.dry_run and not actions.is_resident():
        # 入队前先校验，key / 聊天冲突、配额等当场就能告诉调用方
        try:
            chats = [_parse_chat(c) for c in args.chat]
            ids = [i for i in (args.message_ids or "").split(",") if i.strip()]
            # 新开 Codex / Claude 会话的主人原话也在入队前核对（7.18）：被拒就不入队
            Dispatcher(cfg, store, sink, roster, security).new_session(
                args.key, args.title, args.duty, chats, ids, args.note, validate_only=True,
                host=args.host, model=args.model, effort=args.effort, quote=args.quote)
        except ValueError as e:
            print(f"new-session 被拒绝: {e}", file=sys.stderr)
            return 2
        return _queued(args, store, cfg, "new_session", {"key": args.key, "title": args.title, "duty": args.duty,
                                                    "chats": chats, "message_ids": ids, "note": args.note,
                                                    "host": args.host, "model": args.model, "effort": args.effort,
                                                    "quote": args.quote})
    if args.dry_run:
        print("[dry-run] 在账本内存副本上执行，不写真实账本、不调用 agentapi\n")
    d = Dispatcher(cfg, store, sink, roster, security)
    d.sync_roster()
    try:
        chats = [_parse_chat(c) for c in args.chat]
        r = d.new_session(args.key, args.title, args.duty, chats, (args.message_ids or "").split(","), args.note,
                          host=args.host, model=args.model, effort=args.effort, quote=args.quote)
    except ValueError as e:
        print(f"new-session 被拒绝: {e}", file=sys.stderr)
        return 2
    except SinkError as e:
        print(f"new-session 失败（没有建会话）: {e}", file=sys.stderr)
        return 1
    print(f"已新开 {r['title']} -> {r['conversation_id']}（{HOST_LABEL.get(r['host'], r['host'])}，topic {r['topic_id']}，"
          "只记在账本，不写名册）")
    if r["queued"] or args.note:
        print(f"已排队 {r['queued']} 条消息{'和 note' if args.note else ''}，sheepdog 下一轮投递")
    return 0


def cmd_push_rules(args) -> int:
    cfg, store, _source, sink, roster, security = _build(args)
    if args.topic and not args.dry_run and not actions.is_resident() and store.get_topic(args.topic) is None:
        print(f"push-rules 被拒绝: topic {args.topic} 不存在", file=sys.stderr)
        return 2
    if (rc := _queued(args, store, cfg, "push_rules", {"topic": args.topic or ""})) is not None:
        return rc
    if args.dry_run:
        print("[dry-run] 在账本内存副本上执行，不写真实账本、不调用 agentapi\n")
    d = Dispatcher(cfg, store, sink, roster, security)
    d.sync_roster()
    try:
        report = d.push_rules(args.topic or None)
    except ValueError as e:
        print(f"push-rules 被拒绝: {e}", file=sys.stderr)
        return 2
    if not report:
        print("没有可发的会话")
    for tid, what in report.items():
        print(f"{tid}: {what}")
    return 0


def cmd_retire(args) -> int:
    cfg, store, _source, sink, roster, security = _build(args)
    target = (args.topic or "").strip()
    if target.startswith("tp_") and store.get_topic(target) is None:
        print(f"retire 被拒绝: topic {target} 不存在", file=sys.stderr)
        return 2
    if target == "tp_bus":
        print("retire 被拒绝: 总线不能退休", file=sys.stderr)
        return 2
    if (rc := _queued(args, store, cfg, "retire", {"target": target, "successor_title": args.successor_title})) is not None:
        return rc
    if args.dry_run:
        print("[dry-run] 在账本内存副本上执行，不写真实账本、不调用 agentapi\n")
    d = Dispatcher(cfg, store, sink, roster, security)
    d.sync_roster()
    try:
        r = d.retire(target, args.successor_title)
    except ValueError as e:
        print(f"retire 被拒绝: {e}", file=sys.stderr)
        return 2
    except SinkError as e:
        print(f"retire 失败: {e}", file=sys.stderr)
        return 1
    print(f"已发退休通知 -> {r['conversation_id']}（批次 {r['batch']}，记录 {r['record_topic']}），"
          f"交接回执会转给 {r['successor']}")
    if r.get("closed"):
        print(f"已收掉 {r['closed']}")
    if r.get("note"):
        print(r["note"])
    return 0


def cmd_actions(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    rows = store.list_actions(include_done=args.all)
    if not rows:
        print("没有动作记录")
        return 0
    for a in rows:
        if not a["done_at"]:
            state = "排队中"
        elif a["error"]:
            state = f"失败 @ {a['done_at']}"
        else:
            state = f"完成 @ {a['done_at']}"
        print(f"#{a['id']} [{state}] {a['kind']} {a['args_json']}  提交于 {a['requested_at']}（{a['requested_by'] or '-'}）")
        if a["error"]:
            print(f"  错误: {a['error']}")
        elif a["result"]:
            print(f"  结果: {a['result'][:500]}")
    return 0


def cmd_loops(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    if args.clear:
        n = store.clear_loop(args.clear)
        print(f"已解除 {args.clear} 的冷却（{n} 条记录）" if n else f"{args.clear} 没有冷却记录")
        return 0
    now = datetime.now().astimezone()
    active = []
    for e in store.open_loop_events():
        until = datetime.fromisoformat(e["until"])
        if until > now and e["chat_id"] not in {a["chat_id"] for a in active}:
            active.append(e)
    print(f"冷却中 {len(active)} 个聊天" + ("：" if active else ""))
    for e in active:
        print(f"- {e['chat_name'] or e['chat_id']}（{e['chat_id']}）到 {e['until']}，触发时代回 {e['replies']} 次")
    recent = store.list_loop_events(10)
    if recent:
        print("最近触发记录：")
    for e in recent:
        state = "已手动解除" if e["cleared_at"] else ("冷却中" if datetime.fromisoformat(e["until"]) > now else "已结束")
        print(f"- [{state}] {e['triggered_at']} {e['chat_name'] or e['chat_id']}（{e['chat_id']}）代回 {e['replies']} 次，"
              f"冷却到 {e['until']}")
    return 0


def cmd_redeliver(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    d = store.get_dispatch(args.batch)
    if d is None:
        print(f"批次 {args.batch} 不存在", file=sys.stderr)
        return 2
    if d["state"] != "uncertain":
        print(f"批次 {args.batch} 状态是 {d['state']}，只有 uncertain 的才需要 redeliver", file=sys.stderr)
        return 2
    ids = json.loads(d["message_ids"] or "[]")
    store.requeue_messages(ids)
    store.set_dispatch_state(args.batch, "redelivered")
    print(f"已把批次 {args.batch} 的 {len(ids)} 条消息放回待推，sheepdog 下一轮重发")
    return 0


def cmd_reply_card(args) -> int:
    """打印代回卡片（Card 2.0）JSON（7.17），用法：
    lark-cli im +messages-send --as user --chat-id oc_x --msg-type interactive --content "$(sheepdog reply-card --text ...)"
    """
    cfg = load_config()
    if args.text_file:
        text = sys.stdin.read() if args.text_file == "-" else Path(args.text_file).expanduser().read_text(encoding="utf-8")
    else:
        text = args.text or ""
    try:
        card = build_reply_card(text, cfg.session.reply_suffix, cfg.session.reply_card_width)
    except ValueError as e:
        print(f"reply-card 被拒绝: {e}", file=sys.stderr)
        return 2
    print(json.dumps(card, ensure_ascii=False))
    return 0


def _show_topic(cfg, store: Store, topic_id: str, limit: int) -> int:
    """sheepdog show --topic（7.18 第 9 条）：这个会话最近几轮的问答（读对应宿主的记录），最后一行告诉人去哪里接着看。"""
    t = store.get_topic(topic_id)
    if t is None:
        print(f"topic {topic_id} 不存在", file=sys.stderr)
        return 2
    if not t["conversation_id"]:
        print(f"{topic_id} 还没有会话")
        return 0
    host = Dispatcher.topic_host(t)
    if host == ANTIGRAVITY:
        sink = AgentApiSink()
    else:
        sink = SINK_CLASSES[host](cfg.hosts[host], store, cfg.runs_dir)
    print(f"{t['title']}（{HOST_LABEL.get(host, host)}，topic {topic_id}，会话 {t['conversation_id']}）")
    items = sink.history(t["conversation_id"], limit)
    if not items:
        print("（记录里没有问答，可能会话记录不在本机，或者还没跑过）")
    for role, text, ts in items:
        text = (text or "").strip()
        lines = text.splitlines() or [""]
        more = f"\n  …（还有 {len(lines) - 20} 行）" if len(lines) > 20 else ""
        print(f"[{role}] {ts}\n  " + "\n  ".join(lines[:20]) + more)
    full_title = f"{cfg.session.title_prefix} {t['title']}".strip()  # 新建时给会话起的标题带前缀
    print(f"接着看：{sink.resume_hint(t['conversation_id'], full_title)}")
    return 0


def cmd_show(args) -> int:
    """打印账本里一条消息的完整正文（送进会话的长消息会截断，7.16）；--topic 打印会话最近几轮问答（7.18）。"""
    cfg = load_config()
    store = Store(cfg.db_path)
    if args.topic:
        return _show_topic(cfg, store, args.topic, args.limit)
    if not args.message_id:
        print("要给 message_id，或者 --topic <topic>", file=sys.stderr)
        return 2
    r = store.get_message(args.message_id)
    if r is None:
        print(f"账本里没有 {args.message_id}（可能已超过保留期被清理）", file=sys.stderr)
        return 2
    where = "私聊" if r["chat_type"] == "p2p" else f"群「{r['chat_name'] or r['chat_id']}」"
    print(f"{where} | {r['sender_name'] or '-'}（{r['sender_id'] or '-'}）| {r['create_time']} | {r['route']}/{r['reason']}"
          + (f" | 回复 {r['reply_to']}" if r["reply_to"] else ""))
    print(r["content"] or "")
    return 0


INJECT_ID_PREFIX = "om_test_inject_"


def cmd_inject(args) -> int:
    """测试环境（7.18）：不拉 IM 时，把一条合成消息当作刚收到的 IM 消息入账，走正常的路由和归属。"""
    cfg = load_config()
    if cfg.source.enabled:
        print("inject 被拒绝: 只在 [source] enabled = false（测试环境，不拉 IM）时可用", file=sys.stderr)
        return 2
    roster = _roster(cfg)
    security = _security(cfg)
    store = Store(cfg.db_path)
    mid = INJECT_ID_PREFIX + uuid.uuid4().hex[:12]
    mentions = [Mention(id=cfg.self_open_id, name="主人")] if args.at_me and cfg.self_open_id else []
    m = Message(message_id=mid, chat_id=args.chat, chat_name=args.chat_name, chat_type=args.chat_type,
                sender_id=args.sender, sender_name=args.sender_name, sender_type=args.sender_type, content=args.text,
                msg_type="text", create_time=now_iso(), mentions=mentions, reply_to=args.reply_to)
    Collector(cfg, store, NullSource(), roster, security).ingest([m])
    r = store.get_message(mid)
    print(f"已放进账本 {mid}：{r['route']}/{r['reason']} -> {r['topic_id'] or '-'}（sheepdog 下一轮投递）")
    return 0


def cmd_close_session(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    try:
        close_session(store, args.topic)
    except ValueError as e:
        print(f"close-session 被拒绝: {e}", file=sys.stderr)
        return 2
    print(f"已收掉 {args.topic}，它负责的聊天之后回总线")
    return 0


def cmd_session_reset(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    t = store.get_topic(args.topic)
    if not t:
        print("topic 不存在")
        return 2
    # 在岗表（7.20）：在这之前送出、会话没碰过的批次不再算中断 / 失联，排队的信号下一轮照常投
    store.set_meta(duty_reset_key(args.topic), now_iso())
    card = store.get_card(args.topic)
    if card is not None and card["run_state"] in duty.STUCK:
        store.upsert_card(args.topic, run_state=None, detail="session-reset 清掉了中断 / 失联判定，下一轮重算",
                          stuck_since=None)
        print("已清掉羊群看板的中断 / 失联判定")
    if not sm.can(t["state"], "reset"):
        print(f"当前状态 {t['state']} 无需复位")
        return 0
    store.update_topic(args.topic, state=sm.transition(t["state"], "reset"), retries=0, pending_batch_id=None,
                       attention_at=None, attention_reason=None)
    store.set_meta(attention_notice_key(args.topic), "")
    print("已复位为 active")
    return 0


# ======================= 在岗表（7.20） =======================
def _duty_target_ok(store: Store):
    """留言和登记活的会话：总线、名册 managed、总线新开的（没收掉的）。"""
    def ok(tid: str) -> bool:
        t = store.get_topic(tid)
        return t is not None and t["state"] != sm.CLOSED and (tid == BUS_TOPIC_ID or t["kind"] in MANAGED_KINDS)
    return ok


def _topic_arg(x: str) -> str:
    x = (x or "").strip()
    return x if not x or x.startswith("tp_") else "tp_" + x


def cmd_work(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    tid = _topic_arg(args.topic)
    if not _duty_target_ok(store)(tid):
        print(f"work 被拒绝: {tid} 不是在投递的会话（总线、名册 managed、总线新开的）", file=sys.stderr)
        return 2
    try:
        if args.work_cmd == "set":
            wid, others = duty.set_work(store, tid, args.key, title=args.title, state=args.state,
                                        waiting_on=args.waiting_on, until=args.until, expect=args.expect,
                                        to=_topic_arg(args.to), result=args.result)
        else:
            wid, others = duty.done_work(store, tid, args.key, args.result), []
    except duty.DutyError as e:
        print(f"work 被拒绝: {e}", file=sys.stderr)
        return 2
    w = store.conn.execute("SELECT * FROM work_items WHERE id=?", (wid,)).fetchone()
    print(f"已登记（立即写入羊群看板）：{tid} · {duty.work_line(w)}")
    for o in others:
        card = store.get_card(o["topic_id"])
        run = duty.RUN_LABEL.get(card["run_state"], "") if card is not None and card["run_state"] else ""
        act = "，" + phrase if (phrase := duty.last_action_phrase(card)) else ""
        print(f"⚠ 这把钥匙{duty.topic_label(store, o['topic_id'])}也在做：{duty.WORK_LABEL.get(o['state'], o['state'])}"
              + (f"，会话{run}" if run else "") + act + "。仍已登记；需要它配合就用 tell 跟它说，不要动手做它的活")
    return 0


def cmd_tell(args) -> int:
    cfg = load_config()
    security = _security(cfg)
    store = Store(cfg.db_path)
    verify = owner_quote_verifier(store, security.quote_max_age_hours, cfg.self_open_id) if args.quote else None
    try:
        mail_id = duty.send_mail(store, cfg, _topic_arg(args.topic), _topic_arg(args.to), args.text, args.key,
                                 args.quote, verify, Playbook.from_config(cfg), _duty_target_ok(store))
    except duty.DutyError as e:
        print(f"tell 被拒绝: {e}", file=sys.stderr)
        return 2
    print(f"留言 #{mail_id} 已存进信箱，{_topic_arg(args.to)} 下一批收到（对方排队、离线都不丢）")
    return 0


def cmd_board(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    cards = duty.board_cards(store, cfg, topic=args.topic, history=bool(args.topic))
    if args.topic and not cards:
        print(f"topic {_topic_arg(args.topic)} 不存在", file=sys.stderr)
        return 2
    warn = unsent_notice_line(store, cfg.cli_command)
    if args.json:
        if warn:
            print(warn, file=sys.stderr)  # stdout 只放 JSON
        print(json.dumps(cards, ensure_ascii=False, indent=2))
        return 0
    if warn:
        print(warn)
    now = datetime.now().astimezone()
    print(duty.render_board(cards, display_time(now, cfg.timezone_offset)))
    return 0


def _notice_ids(store: Store, spec: str) -> list[int]:
    """--retry / --ack 的参数：all = 所有没发出去、没人处理的；否则逗号分隔的编号。"""
    if spec.strip().lower() == "all":
        return [r["id"] for r in store.unsent_owner_notices()]
    out = []
    for x in spec.split(","):
        x = x.strip().lstrip("#")
        if not x.isdigit():
            raise ValueError(f"通知编号要是数字或 all：{x!r}")
        out.append(int(x))
    return out


def _notice_line(cfg, r) -> str:
    def t(ts):
        dt = datetime.fromisoformat(ts) if ts else None
        return display_time(dt, cfg.timezone_offset) if dt is not None and dt.tzinfo is not None else (ts or "")
    state = NOTICE_STATE_LABEL.get(r["state"], r["state"]) + ("（已确认）" if r["acked_at"] else "")
    head = (bytes(r["body"]).decode("utf-8", "replace").splitlines() or [""])[0]
    parts = [f"#{r['id']} {state}", r["kind"], r["topic_id"] or "-", f"落账本 {t(r['created_at'])}",
             f"试了 {r['attempts'] or 0} 次"]
    if r["state"] == "sent":
        parts.append(f"发出 {t(r['sent_at'])}")
    elif r["state"] == "pending" and r["next_at"]:
        parts.append(f"下次 {t(r['next_at'])}")
    line = " · ".join(parts) + f"\n    {head}"
    if r["last_error"] and r["state"] != "sent":
        line += f"\n    最近一次错误：{r['last_error']}"
    return line


def cmd_notices(args) -> int:
    """给主人的通知账本：默认列没发出去、没人处理的（还在重试的、放弃了的）。--retry 让它们回到待发、2 小时重新算
    （常驻进程下一轮重发，防重编号不变）；--ack 确认已处理（不再提示、不再重发）；--show 打印正文原样。"""
    cfg = load_config()
    store = Store(cfg.db_path)
    if args.show is not None:
        r = store.get_owner_notice(args.show)
        if r is None:
            print(f"没有通知 #{args.show}", file=sys.stderr)
            return 2
        print(_notice_line(cfg, r))
        print(f"防重编号: {r['idem_key']}")
        print("正文:")
        print(bytes(r["body"]).decode("utf-8", "replace"))
        return 0
    for spec, fn, verb in ((args.retry, store.owner_notice_retry, "回到待发，常驻进程下一轮重发"),
                           (args.ack, store.owner_notice_ack, "已确认，不再提示、不再重发")):
        if spec is None:
            continue
        try:
            ids = _notice_ids(store, spec)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if not ids:
            print("没有没发出去的通知")
            return 0
        changed = [i for i in ids if fn(i)]
        skipped = [i for i in ids if i not in changed]
        if changed:
            print(f"{'、'.join('#' + str(i) for i in changed)} {verb}")
        if skipped:
            print(f"{'、'.join('#' + str(i) for i in skipped)} 没改：不存在或已经发出去了", file=sys.stderr)
        return 0 if changed else 2
    rows = store.list_owner_notices(args.limit) if args.all else store.unsent_owner_notices()
    if not rows:
        print("给主人的通知都发出去了" if not args.all else "账本里还没有给主人的通知")
        return 0
    if not args.all and (warn := unsent_notice_line(store, cfg.cli_command)):
        print(warn)
    for r in rows:
        print(_notice_line(cfg, r))
    return 0

# ======================= 一事一串、发前再看一眼（7.22） =======================
def _body_arg(text: str | None, path: str | None) -> str:
    if path:
        return sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(encoding="utf-8")
    return text or ""


def _test_env_write(cfg, what: str, text: str) -> int:
    """测试环境（[source] enabled = false）不发飞书：设了 [notify] file 就写进去，否则拒绝。不写账本。"""
    path = cfg.notify_path
    if path is None:
        print(f"{what}没发: 测试环境（[source] enabled = false）不发飞书，也没设 [notify] file", file=sys.stderr)
        return outbound.EXIT_INVALID
    FileNotifier(path, cfg.timezone_offset).notify(text, what)
    print(f"测试环境（[source] enabled = false）：没发飞书，写进了 {path}")
    return outbound.EXIT_OK


def cmd_ask(args) -> int:
    """sheepdog ask / report：经机器人找主人。带 --key：同一把钥匙第一次起新话题，之后回到这个话题里。"""
    cfg = load_config()
    store = Store(cfg.db_path)
    kind = args.cmd
    tid = _topic_arg(args.topic)
    try:
        body = _body_arg(args.text, args.file)
    except OSError as e:
        print(f"{kind} 没发: 读不了正文文件: {e}", file=sys.stderr)
        return outbound.EXIT_INVALID
    if not cfg.source.enabled:
        if not outbound.target_ok(store, tid):
            print(f"{kind} 没发: {tid} 不是在投递的会话", file=sys.stderr)
            return outbound.EXIT_INVALID
        return _test_env_write(cfg, "需要你定" if kind == outbound.ASK else "汇报",
                               outbound.compose(cfg, kind, tid, args.title, body))
    try:
        r = outbound.send_to_owner(cfg, store, _source(cfg), kind, tid, args.title, body, args.key, args.batch)
    except outbound.OutboundError as e:
        print(f"{kind} 没发: {e}", file=sys.stderr)
        return e.code
    if r.already:
        print(f"之前已经发过同样的内容（防重编号相同，这次没再发）：message_id {r.message_id}")
    elif r.new_thread:
        print(f"已发给主人，新起了一个话题（钥匙 {args.key}）：message_id {r.message_id}。之后同一把钥匙的消息都回到这个话题里")
    elif r.root_message_id:
        print(f"已发到钥匙 {args.key} 的话题里：message_id {r.message_id}（话题开头 {r.root_message_id}）")
    else:
        print(f"已发给主人：message_id {r.message_id}")
    return outbound.EXIT_OK


def cmd_reply(args) -> int:
    """sheepdog reply：代回。发前只读检查主人有没有新发言；说过就不发（退出码 3），把他的话打印出来。"""
    cfg = load_config()
    store = Store(cfg.db_path)
    tid = _topic_arg(args.topic)
    try:
        card_text = _body_arg(args.card_text, args.card_file) if args.card_text is not None or args.card_file else ""
    except OSError as e:
        print(f"代回没发: 读不了正文文件: {e}", file=sys.stderr)
        return outbound.EXIT_INVALID
    if not cfg.source.enabled:
        if not outbound.target_ok(store, tid):
            print(f"代回没发: {tid} 不是在投递的会话", file=sys.stderr)
            return outbound.EXIT_INVALID
        try:
            shown, _ = outbound.reply_body(cfg, args.text or "", card_text)
        except ValueError as e:
            print(f"代回没发: {e}", file=sys.stderr)
            return outbound.EXIT_INVALID
        return _test_env_write(cfg, "代回", f"回复 {args.to}：\n{shown}")
    try:
        r = outbound.reply(cfg, store, _source(cfg), tid, args.to, text=args.text or "", card_text=card_text,
                           even_if_owner_replied=args.even_if_owner_replied)
    except outbound.OutboundError as e:
        print(f"代回没发: {e}", file=sys.stderr)
        return e.code
    if r.spoke is not None:
        print(outbound.spoke_text(cfg, r.spoke))
        return outbound.EXIT_OWNER_REPLIED
    if r.already:
        print(f"之前已经发过同样的代回（防重编号相同，这次没再发）：message_id {r.message_id}")
    else:
        print(f"已代回：message_id {r.message_id}")
    return outbound.EXIT_OK


def cmd_thread(args) -> int:
    """sheepdog thread：只读列出一个话题的全部消息（开头那条从账本 / sends / mget 取，回复从账本和话题列表取）。"""
    cfg = load_config()
    store = Store(cfg.db_path)
    source = _source(cfg)
    try:
        v = outbound.view_thread(cfg, store, source, _topic_arg(args.topic), args.key or "", args.thread or "")
    except outbound.OutboundError as e:
        print(f"thread: {e}", file=sys.stderr)
        return e.code
    print(outbound.render_thread(cfg, v))
    return outbound.EXIT_OK


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sheepdog", description="工作助理：把 IM 上的工作分给负责的 Agent session")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    sp = sub.add_parser("poll")
    sp.add_argument("--dry-run", action="store_true", help="不真正投递，只打印")
    sp.add_argument("--no-dispatch", action="store_true", help="只拉取与路由，不投递")
    sp.set_defaults(func=cmd_poll)

    sp = sub.add_parser("run")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--interval", type=int, default=0)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("inbox")
    sp.add_argument("--chat", default="")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_inbox)

    sp = sub.add_parser("inbox-clear")
    sp.add_argument("--chat", default="")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(func=cmd_inbox_clear)

    sp = sub.add_parser("init")
    sp.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容，不写账本")
    sp.set_defaults(func=cmd_init)

    sub.add_parser("sessions").set_defaults(func=cmd_sessions)

    sp = sub.add_parser("forward")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--message-ids", default="", help="逗号分隔；可以不带，只转达 --quote / --note")
    sp.add_argument("--quote", default="", help="主人原话，投递时标成「主人原话（经总线转达）」")
    sp.add_argument("--note", default="", help="总线备注，投递时标成「总线备注」")
    sp.set_defaults(func=cmd_forward)

    sp = sub.add_parser("spawn")
    sp.add_argument("--key", required=True)
    sp.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容，不写账本")
    sp.set_defaults(func=cmd_spawn)

    sp = sub.add_parser("watch")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--person", required=True, help="对方 open_id")
    sp.add_argument("--chat", default="", help="只认这个聊天里的回复；不填 = 任意聊天")
    sp.add_argument("--note", default="", help="在等什么")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("watches")
    sp.add_argument("--all", action="store_true", help="包括已结束的")
    sp.set_defaults(func=cmd_watches)

    sp = sub.add_parser("security-log")
    sp.add_argument("--since", default="24h", help="回看时长：30m / 24h / 7d")
    sp.set_defaults(func=cmd_security_log)

    sp = sub.add_parser("escalations")
    sp.add_argument("--all", action="store_true", help="包括已答和已关闭的")
    sp.set_defaults(func=cmd_escalations)

    sp = sub.add_parser("new-session")
    sp.add_argument("--key", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--duty", required=True)
    sp.add_argument("--chat", action="append", default=[], help="oc_xxx 或 oc_xxx:all，可重复")
    sp.add_argument("--message-ids", default="")
    sp.add_argument("--note", default="", help="为什么开（会作为创建原因记下，并排进它的队列）")
    sp.add_argument("--host", default=ANTIGRAVITY, choices=HOSTS, help="会话宿主，默认 antigravity；建好后不能换")
    sp.add_argument("--model", default="", help="模型；留空 = 这个宿主的默认值")
    sp.add_argument("--effort", default="", help="推理档位（只对 codex / claude）；留空 = 这个宿主的默认值")
    sp.add_argument("--quote", default="", help="主人同意开这个会话的原话（codex / claude 且 ask_owner_before_new 时必填，"
                                                "核对同 forward）")
    sp.add_argument("--dry-run", action="store_true", help="只打印，不写账本、不建会话")
    sp.set_defaults(func=cmd_new_session)

    sp = sub.add_parser("push-rules")
    sp.add_argument("--topic", default="", help="不填 = 全部会话（含总线）")
    sp.add_argument("--dry-run", action="store_true", help="只打印，不发、不写账本")
    sp.set_defaults(func=cmd_push_rules)

    sp = sub.add_parser("retire")
    sp.add_argument("--topic", required=True, help="tp_x 或裸 conversation_id")
    sp.add_argument("--successor-title", default="", help="接手者的称呼，填进 retire.md 的 {{successor_title}}")
    sp.add_argument("--dry-run", action="store_true", help="只打印，不发、不写账本")
    sp.set_defaults(func=cmd_retire)

    sp = sub.add_parser("actions")
    sp.add_argument("--all", action="store_true", help="全部（默认只看排队中和最近 10 条已完成）")
    sp.set_defaults(func=cmd_actions)

    sp = sub.add_parser("loops")
    sp.add_argument("--clear", default="", help="手动解除这个聊天的冷却")
    sp.set_defaults(func=cmd_loops)

    sp = sub.add_parser("redeliver")
    sp.add_argument("--batch", required=True)
    sp.set_defaults(func=cmd_redeliver)

    sp = sub.add_parser("reply-card")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="正文")
    g.add_argument("--text-file", help="正文文件；- 表示标准输入")
    sp.set_defaults(func=cmd_reply_card)

    sp = sub.add_parser("show")
    sp.add_argument("message_id", nargs="?", default="")
    sp.add_argument("--topic", default="", help="打印这个会话最近几轮的问答")
    sp.add_argument("--limit", type=int, default=6, help="--topic 时最多打印几条")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("inject")
    sp.add_argument("--text", required=True)
    sp.add_argument("--chat", default="oc_test_inject", help="聊天 ID（默认 oc_test_inject）")
    sp.add_argument("--chat-name", default="测试聊天")
    sp.add_argument("--chat-type", default="p2p", choices=("p2p", "group"))
    sp.add_argument("--sender", default="ou_test_inject", help="发送人 open_id")
    sp.add_argument("--sender-name", default="测试同事")
    sp.add_argument("--sender-type", default="user", choices=("user", "app", "bot"))
    sp.add_argument("--at-me", action="store_true", help="群消息里 @主人")
    sp.add_argument("--reply-to", default="")
    sp.set_defaults(func=cmd_inject)

    sp = sub.add_parser("close-session")
    sp.add_argument("--topic", required=True)
    sp.set_defaults(func=cmd_close_session)

    sp = sub.add_parser("acks")
    sp.add_argument("--open", action="store_true", help="只看还没撤的")
    sp.set_defaults(func=cmd_acks)

    sp = sub.add_parser("watch-done")
    sp.add_argument("--id", type=int, required=True)
    sp.set_defaults(func=cmd_watch_done)

    sp = sub.add_parser("unwatch")
    sp.add_argument("--id", type=int, required=True)
    sp.set_defaults(func=cmd_unwatch)

    sp = sub.add_parser("receipt")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--batch", required=True)
    sp.add_argument("--json", required=True)
    sp.set_defaults(func=cmd_receipt)

    sp = sub.add_parser("session-reset")
    sp.add_argument("--topic", required=True)
    sp.set_defaults(func=cmd_session_reset)

    sp = sub.add_parser("work", help="羊群看板：登记 / 收掉会话名下的活")
    wsub = sp.add_subparsers(dest="work_cmd", required=True)
    w = wsub.add_parser("set")
    w.add_argument("--topic", required=True, help="你自己的会话（tp_x）")
    w.add_argument("--key", required=True, help="钥匙：外部编号（审批单号、PR、Linear 单号）或短名")
    w.add_argument("--title", default="", help="一句话：这件活是什么")
    w.add_argument("--state", default=duty.DOING,
                   help="doing | waiting_other | waiting_owner | done | handed_off（也认中文：进行中 / 等别人 / 等主人 / 已完成 / 已转交）")
    w.add_argument("--waiting-on", default="", help="等谁")
    w.add_argument("--until", default="", help="到几点")
    w.add_argument("--expect", default="", help="答应交出什么（一句话）")
    w.add_argument("--to", default="", help="转交给哪个会话（handed_off 必填）")
    w.add_argument("--result", default="", help="结果（一句话）")
    w.set_defaults(func=cmd_work)
    w = wsub.add_parser("done")
    w.add_argument("--topic", required=True)
    w.add_argument("--key", required=True)
    w.add_argument("--result", required=True, help="做成了什么（一句话）")
    w.set_defaults(func=cmd_work)

    sp = sub.add_parser("tell", help="羊群看板：给别的会话留言")
    sp.add_argument("--topic", "--from", dest="topic", required=True, help="你自己的会话（tp_x）")
    sp.add_argument("--to", required=True, help="收件会话（tp_x）")
    sp.add_argument("text", help="留言正文")
    sp.add_argument("--key", default="", help="说的是哪件活")
    sp.add_argument("--quote", default="", help="要转达的主人原话（核对同 forward）")
    sp.set_defaults(func=cmd_tell)

    sp = sub.add_parser("notices", help="给主人的通知：没发出去的；人工重发 / 确认已处理")
    sp.add_argument("--all", action="store_true", help="列最近的全部通知（含已发的）")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--show", type=int, help="打印这条通知的正文原样")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--retry", metavar="N|all", help="没发出去的回到待发（2 小时重新算），常驻进程下一轮重发")
    g.add_argument("--ack", metavar="N|all", help="确认已处理：不再提示、不再重发")
    sp.set_defaults(func=cmd_notices)

    sp = sub.add_parser("board", help="羊群看板：各会话在干什么")
    sp.add_argument("--topic", default="", help="只看这个会话（附最近回执和动作）")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_board)

    for name, what in (("ask", "需要主人定的事"), ("report", "向主人汇报")):
        sp = sub.add_parser(name, help=f"经机器人私聊主人：{what}（sheepdog 加开头一行；带 --key 的同一件事归到一个话题里）")
        sp.add_argument("--topic", required=True, help="你自己的会话（tp_x）")
        sp.add_argument("--title", required=True, help="短标题（一行）")
        g = sp.add_mutually_exclusive_group(required=True)
        g.add_argument("--text", help="正文")
        g.add_argument("--file", help="正文文件；- 表示标准输入")
        sp.add_argument("--key", default="", help="钥匙（和在岗表的活同一套）：同一把钥匙第一次在主聊天里起一条，之后回到它的话题里")
        sp.add_argument("--batch", default="",
                        help="批次号（可选，一般不用传）：参与防重编号。不传时取这个会话当前在跑的批次，没有就取最近一批")
        sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("reply", help="代回：以主人身份回复一条消息（卡片带代回标记）；发前先查主人有没有新发言")
    sp.add_argument("--topic", required=True, help="你自己的会话（tp_x）")
    sp.add_argument("--to", required=True, help="要回复的那条消息（om_）")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--card-text", help="卡片正文（同 reply-card --text）")
    g.add_argument("--card-file", help="卡片正文文件，- 表示标准输入（同 reply-card --text-file）")
    g.add_argument("--text", help="纯文本代回（末尾自动附代回标记）")
    sp.add_argument("--even-if-owner-replied", action="store_true",
                    help="主人已经回过也照样发（跳过发前检查）；默认主人回过就不发，退出码 3")
    sp.set_defaults(func=cmd_reply)

    sp = sub.add_parser("thread", help="只读：列出一个话题的全部消息")
    sp.add_argument("--topic", required=True, help="你自己的会话（tp_x）")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--key", help="钥匙（ask / report 带这把 --key 发过）")
    g.add_argument("--thread", help="话题编号 omt_…（或开头那条的 om_）")
    sp.set_defaults(func=cmd_thread)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except _Abort:
        return 2
    except ConfigError as e:
        print(f"配置无效: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
