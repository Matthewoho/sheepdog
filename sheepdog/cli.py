"""命令行入口。

  sheepdog doctor                 检查依赖与环境
  sheepdog poll [--dry-run]       拉取一次并路由（--dry-run 不投递）
  sheepdog run [--dry-run]        常驻循环：拉取 → 路由 → 投递
  sheepdog inbox [--chat 群名]     查看 Inbox（按群分组）
  sheepdog inbox-clear [--chat 群名 | --all]
  sheepdog init [--dry-run]       同步名册、没有总线就建、给未 onboarding 的 managed 会话发 onboarding
  sheepdog sessions               查看 session 注册表与状态（含名册 mode、职责、负责的聊天）
  sheepdog forward --topic T --message-ids a,b [--note ...]   由总线调用，把消息转交给 managed 会话
  sheepdog receipt --topic T --batch B --json '{...}'   由 session 调用，提交回执
  sheepdog session-reset --topic T                       人工把 attention/failed 复位

--dry-run 一律在账本的内存副本上执行，不写真实账本（否则假的 conversation_id、onboarded_at 会留下来）。
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time

from . import __version__
from .config import load_config
from .engine import Collector, Dispatcher, forward_messages, write_receipt
from .roster import Roster, RosterError, load_roster
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


def _build(args):
    cfg = load_config()
    roster = _roster(cfg)
    dry = getattr(args, "dry_run", False)
    store = Store.snapshot(cfg.db_path) if dry else Store(cfg.db_path)
    source = LarkCliSource(tz=cfg.timezone_offset)
    sink = DryRunSink() if dry or cfg.sink == "dryrun" else AgentApiSink()
    return cfg, store, source, sink, roster


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
    return 0 if ok else 1


def _cycle(collector: Collector, dispatcher: Dispatcher | None) -> None:
    stats = collector.poll_once()
    logging.info("poll: %s", stats)
    if dispatcher:
        res = dispatcher.dispatch_once()
        logging.info("dispatch: %s", res)


def cmd_poll(args) -> int:
    cfg, store, source, sink, roster = _build(args)
    collector = Collector(cfg, store, source, roster)
    dispatcher = None if args.no_dispatch else Dispatcher(cfg, store, sink, roster)
    _cycle(collector, dispatcher)
    return 0


def cmd_run(args) -> int:
    cfg, store, source, sink, roster = _build(args)
    collector = Collector(cfg, store, source, roster)
    dispatcher = Dispatcher(cfg, store, sink, roster)
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
    rows = store.inbox_summary()
    total = sum(r["unread"] or 0 for r in rows)
    print(f"📥 Inbox：{total} 条未读，{len(rows)} 个会话")
    for r in rows:
        tag = "（Bot 私聊）" if r["reason"] == "bot_p2p" else ""
        print(f"  「{r['chat_name'] or r['chat_id']}」{tag} 未读 {r['unread'] or 0} / 共 {r['total']}，最近 {r['last_time']}")
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
    cfg, store, _source, sink, roster = _build(args)
    if args.dry_run:
        print("[dry-run] 在账本内存副本上执行，不写真实账本、不调用 agentapi\n")
    print(f"名册: {cfg.roster_file}（{len(roster.managed)} managed / {len(roster.sessions) - len(roster.managed)} known）")
    rep = Dispatcher(cfg, store, sink, roster).init()
    for k, label in (("created", "新登记"), ("updated", "已更新"), ("reopened", "重新启用"), ("closed", "已移除→closed")):
        if rep["roster"][k]:
            print(f"  {label}: {', '.join(rep['roster'][k])}")
    print(f"总线: {rep['bus']}")
    if not rep["onboarding"]:
        print("onboarding: 名册里没有 managed 会话")
    for tid, what in rep["onboarding"].items():
        print(f"onboarding {tid}: {what}")
    return 0


_MODE = {"adopted": "managed", "known": "known", "bus": "bus"}


def cmd_sessions(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    try:
        roster = load_roster(cfg.roster_file, cfg.roster_required)
    except RosterError as e:  # 只是展示用，名册坏了照样列出账本里的记录
        print(f"（名册无效，聊天名称显示为 ID: {e}）")
        roster = Roster()
    rows = store.list_topics()
    if not rows:
        print("暂无 session")
    for t in rows:
        mode = _MODE.get(t["kind"], t["kind"] or "-")
        print(f"- {t['title']}  [{t['state']}]  mode={mode}  topic={t['topic_id']}  conversation={t['conversation_id'] or '-'}")
        print(f"  职责: {t['duty']}")
        if t["kind"] in ("adopted", "known"):
            s = roster.by_topic(t["topic_id"])
            names = {c.chat_id: f"{c.name or c.chat_id}{'（全部）' if c.all_messages else ''}" for c in (s.chats if s else [])}
            chats = [names.get(c, c) for c in json.loads(t["anchors_json"] or "[]") if c.startswith("oc_")]
            print(f"  负责的聊天: {'、'.join(chats) or '-'}")
            if t["kind"] == "adopted":
                print(f"  onboarded: {t['onboarded_at'] or '否'}  未回执: {t['receipt_missed'] or 0}")
        if t["summary"]:
            print(f"  进展: {t['summary']}")
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
    store = Store(cfg.db_path)
    try:
        ids = forward_messages(store, args.topic, args.message_ids.split(","), args.note)
    except ValueError as e:
        print(f"转交被拒绝: {e}", file=sys.stderr)
        return 2
    print(f"已转交 {len(ids)} 条到 {args.topic}，sheepdog 下一轮投递（对方被主人接管时排队）")
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
    sp.add_argument("--message-ids", required=True, help="逗号分隔")
    sp.add_argument("--note", default="", help="为什么转给它")
    sp.set_defaults(func=cmd_forward)

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


if __name__ == "__main__":
    sys.exit(main())
