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
  sheepdog session-reset --topic T                       人工把 attention/failed 复位

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
from .engine import (DYNAMIC, HOST_LABEL, RETIRED, Collector, Dispatcher, add_watch, bus_quote_verifier,
                     close_session, escalation_summary, forward_messages, open_escalations, write_receipt)
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
    """经机器人给主人发私聊（登录失效提醒）。不拉 IM、没有 self_open_id 时没有；dry-run 只打印。"""
    if not cfg.self_open_id or not hasattr(source, "send_text"):
        return None
    if getattr(args, "dry_run", False):
        return lambda text: print(f"[dryrun] 给主人发私聊：\n{text}\n")
    return lambda text: source.send_text(cfg.self_open_id, text, "bot")


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
    print(f"配置文件: {cfg.config_path} {'✓' if cfg.config_path.exists() else '✗ 不存在（使用默认值）'}")
    print(f"状态目录: {cfg.state_dir}")
    print(cli_name_report(cfg))
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
    # playbook 缺文件只告警不判失败：缺的那一段在 prompt 里为空
    pb = Playbook.from_config(cfg)
    missing = pb.missing()
    print(f"playbook: {pb.directory} " + (f"✓ {len(PLAYBOOK_FILES)} 个文件齐全" if not missing
                                          else f"缺 {len(missing)} 个（对应段落为空）"))
    for name in missing:
        print(f"  ✗ 缺 {name}：{PLAYBOOK_FILES[name]}")
    return 0 if ok else 1


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
                pruned = store.prune(cfg.retention_days, cfg.receipts_dir, cfg.runs_dir)
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
        print(f"  最近活动: {t['last_active']}  待回执批次: {t['pending_batch_id'] or '-'}  重试: {t['retries']}")
    return 0


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
                               cfg.self_open_id, cfg.escalation.open_hours)
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
    if not sm.can(t["state"], "reset"):
        print(f"当前状态 {t['state']} 无需复位")
        return 0
    store.update_topic(args.topic, state=sm.transition(t["state"], "reset"), retries=0, pending_batch_id=None)
    print("已复位为 active")
    return 0


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
