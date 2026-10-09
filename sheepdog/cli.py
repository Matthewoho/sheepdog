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
  sheepdog watches [--all]  /  sheepdog unwatch --id N
  sheepdog security-log [--since 24h]                    列出被安全规则标记 / 拦截的消息
  sheepdog escalations [--all]                           列出未结 / 全部「需要你定」
  sheepdog acks [--open]                                 列出点过的确认表情及是否已撤
  sheepdog new-session --key K --title T --duty D [--chat oc_x[:all]]... [--message-ids a,b] [--note ...] [--dry-run]
                                                         总线临时新开一个专属会话（只在账本里）
  sheepdog close-session --topic T                       收掉总线新开的会话
  sheepdog push-rules [--topic T] [--dry-run]            立即给会话发一次完整现行规则
  sheepdog retire --topic <tp_x 或 conversation_id> [--successor-title ...]   给会话补发 / 手动发退休通知
  sheepdog actions [--all]                               排队 / 完成 / 失败的动作
  sheepdog loops [--clear <chat_id>]                     疑似和 Agent 循环而冷却中的聊天；手动解除
  sheepdog redeliver --batch B                           发送中断、不确定是否送达的批次：人工确认后重发
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
import sys
import time
from datetime import datetime, timedelta

from . import __version__
from . import actions
from .ack import Acker
from .config import ConfigError, load_config
from .engine import (DYNAMIC, RETIRED, Collector, Dispatcher, add_watch, bus_quote_verifier, close_session,
                     escalation_summary, forward_messages, open_escalations, write_receipt)
from .playbook import PLAYBOOK_FILES, Playbook
from .roster import Roster, RosterError, load_roster
from .security import HOLD, TAG, SecurityConfig, SecurityError, load_security
from .sink import SinkError
from .sink.agentapi import AgentApiSink, DryRunSink
from .source.lark import LarkCliSource
from .store import Store
from . import session as sm


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


class _Abort(Exception):
    pass


def _roster(cfg) -> Roster:
    """名册校验失败直接报错退出，不静默降级。"""
    try:
        return load_roster(cfg.roster_file, cfg.roster_required)
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
    source = LarkCliSource(tz=cfg.timezone_offset, max_pages=cfg.max_pages)
    sink = DryRunSink() if dry or cfg.sink == "dryrun" else AgentApiSink()
    return cfg, store, source, sink, roster, security


def _acker(cfg, store, source, args) -> Acker | None:
    """确认表情（7.9）：没有 [ack] 段就不建。dry-run（含 sink = dryrun）只打印，不调接口。"""
    if not cfg.ack.enabled:
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
    print(f"self_open_id: {'✓ 已配置' if cfg.self_open_id else '✗ 未配置（@我/回复我 判定会失效）'}")
    ok &= bool(cfg.self_open_id)
    lark = shutil.which("lark-cli")
    print(f"lark-cli: {lark or '✗ 未找到'}")
    ok &= bool(lark)
    avail, why = AgentApiSink().available()
    print(f"agentapi: {'✓' if avail else '✗ ' + why}")
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
          + ("" if cfg.owner_context.skip_prefixes else "（⚠ owner_context.skip_prefixes 为空，无法识别代回，熔断不生效）"))
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
    dispatcher = None if args.no_dispatch else Dispatcher(cfg, store, sink, roster, security, acker)
    _cycle(collector, dispatcher)
    return 0


def _queued(args, store: Store, kind: str, payload: dict) -> int | None:
    """不在常驻进程里、又不是 --dry-run：入队并返回退出码 0；否则返回 None 表示本地执行（7.12）。"""
    if getattr(args, "dry_run", False) or actions.is_resident():
        return None
    aid = actions.enqueue(store, kind, payload)
    print(f"动作 #{aid}（{kind}）{actions.QUEUED_HINT}")
    return 0


def cmd_run(args) -> int:
    # 本进程是常驻进程：只有它能跨项目调 agentapi，会话里发起的动作由它执行
    actions.mark_resident()
    cfg, store, source, sink, roster, security = _build(args)
    acker = _acker(cfg, store, source, args)
    collector = Collector(cfg, store, source, roster, security, acker)
    dispatcher = Dispatcher(cfg, store, sink, roster, security, acker)
    interval = args.interval or cfg.poll_interval_seconds
    logging.info("sheepdog 常驻运行，间隔 %ss，sink=%s", interval, sink.name)
    last_prune = 0.0
    while True:
        try:
            # 每轮重读名册：改名册不必重启；改坏了沿用上一份有效名册并报错，常驻进程不退出
            try:
                collector.roster = dispatcher.roster = load_roster(cfg.roster_file, cfg.roster_required)
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
                n = store.prune(cfg.retention_days)
                last_prune = time.time()
                if n:
                    logging.info("清理过期消息 %d 条", n)
        except KeyboardInterrupt:
            return 0
        except Exception:  # 单轮失败不退出，下一轮从水位线续跑
            logging.exception("本轮失败")
        time.sleep(interval)


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
    if (rc := _queued(args, store, "init", {})) is not None:
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
    if (rc := _queued(args, store, "spawn", {"key": args.key})) is not None:
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
        print(f"- {t['title']}  [{t['state']}]  mode={mode}  topic={t['topic_id']}  conversation={t['conversation_id'] or '-'}")
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
        for u in uncertain.get(t["topic_id"], []):
            print(f"  ⚠ 批次 {u['batch_id']} 发送中断、不确定是否送达（{u['sent_at']}），确认后: sheepdog redeliver --batch {u['batch_id']}")
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
          f"取消：sheepdog unwatch --id {wid}")
    return 0


def cmd_watches(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    rows = store.list_watches(include_closed=args.all)
    if not rows:
        print("没有等待中的回复" if not args.all else "没有等待记录")
    for w in rows:
        state = f"已结束 {w['close_reason']} @ {w['closed_at']}" if w["closed_at"] else "等待中"
        nudged = "/".join(m for m, k in (("15", "nudged_15_at"), ("30", "nudged_30_at")) if w[k]) or "-"
        print(f"#{w['id']} [{state}] topic={w['topic_id']} person={w['person_id']} chat={w['chat_id'] or '任意'} "
              f"开始 {w['started_at']} 已提醒 {nudged}")
        if w["note"]:
            print(f"  在等: {w['note']}")
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
            Dispatcher(cfg, store, sink, roster, security).new_session(
                args.key, args.title, args.duty, chats, ids, args.note, validate_only=True)
        except ValueError as e:
            print(f"new-session 被拒绝: {e}", file=sys.stderr)
            return 2
        return _queued(args, store, "new_session", {"key": args.key, "title": args.title, "duty": args.duty,
                                                    "chats": chats, "message_ids": ids, "note": args.note})
    if args.dry_run:
        print("[dry-run] 在账本内存副本上执行，不写真实账本、不调用 agentapi\n")
    d = Dispatcher(cfg, store, sink, roster, security)
    d.sync_roster()
    try:
        chats = [_parse_chat(c) for c in args.chat]
        r = d.new_session(args.key, args.title, args.duty, chats, (args.message_ids or "").split(","), args.note)
    except ValueError as e:
        print(f"new-session 被拒绝: {e}", file=sys.stderr)
        return 2
    except SinkError as e:
        print(f"new-session 失败（没有建会话）: {e}", file=sys.stderr)
        return 1
    print(f"已新开 {r['title']} -> {r['conversation_id']}（topic {r['topic_id']}，只记在账本，不写名册）")
    if r["queued"] or args.note:
        print(f"已排队 {r['queued']} 条消息{'和 note' if args.note else ''}，sheepdog 下一轮投递")
    return 0


def cmd_push_rules(args) -> int:
    cfg, store, _source, sink, roster, security = _build(args)
    if args.topic and not args.dry_run and not actions.is_resident() and store.get_topic(args.topic) is None:
        print(f"push-rules 被拒绝: topic {args.topic} 不存在", file=sys.stderr)
        return 2
    if (rc := _queued(args, store, "push_rules", {"topic": args.topic or ""})) is not None:
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
    if (rc := _queued(args, store, "retire", {"target": target, "successor_title": args.successor_title})) is not None:
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

    sp = sub.add_parser("close-session")
    sp.add_argument("--topic", required=True)
    sp.set_defaults(func=cmd_close_session)

    sp = sub.add_parser("acks")
    sp.add_argument("--open", action="store_true", help="只看还没撤的")
    sp.set_defaults(func=cmd_acks)

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
