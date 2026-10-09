"""命令行入口。

  signal-pilot doctor                 检查依赖与环境
  signal-pilot poll [--dry-run]       拉取一次并路由（--dry-run 不投递）
  signal-pilot run [--dry-run]        常驻循环：拉取 → 路由 → 投递
  signal-pilot inbox [--chat 群名]     查看 Inbox（按群分组）
  signal-pilot inbox-clear [--chat 群名 | --all]
  signal-pilot sessions               查看 session 注册表与状态
  signal-pilot receipt --topic T --batch B --json '{...}'   由 session 调用，提交回执
  signal-pilot session-reset --topic T                       人工把 attention/failed 复位
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
from .engine import Collector, Dispatcher, write_receipt
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


def _build(args):
    cfg = load_config()
    store = Store(cfg.db_path)
    source = LarkCliSource(tz=cfg.timezone_offset)
    sink = DryRunSink() if getattr(args, "dry_run", False) or cfg.sink == "dryrun" else AgentApiSink()
    return cfg, store, source, sink


def cmd_doctor(args) -> int:
    cfg = load_config()
    ok = True
    print(f"signal-pilot {__version__}")
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
    return 0 if ok else 1


def _cycle(collector: Collector, dispatcher: Dispatcher | None) -> None:
    stats = collector.poll_once()
    logging.info("poll: %s", stats)
    if dispatcher:
        res = dispatcher.dispatch_once()
        logging.info("dispatch: %s", res)


def cmd_poll(args) -> int:
    cfg, store, source, sink = _build(args)
    collector = Collector(cfg, store, source)
    dispatcher = None if args.no_dispatch else Dispatcher(cfg, store, sink)
    _cycle(collector, dispatcher)
    return 0


def cmd_run(args) -> int:
    cfg, store, source, sink = _build(args)
    collector = Collector(cfg, store, source)
    dispatcher = Dispatcher(cfg, store, sink)
    interval = args.interval or cfg.poll_interval_seconds
    logging.info("signal-pilot 常驻运行，间隔 %ss，sink=%s", interval, sink.name)
    last_prune = 0.0
    while True:
        try:
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


def cmd_sessions(args) -> int:
    cfg = load_config()
    store = Store(cfg.db_path)
    rows = store.list_topics()
    if not rows:
        print("暂无 session")
    for t in rows:
        print(f"- {t['title']}  [{t['state']}]  topic={t['topic_id']}  conversation={t['conversation_id'] or '-'}")
        print(f"  职责: {t['duty']}")
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
    p = argparse.ArgumentParser(prog="signal-pilot", description="IM 信号 → AI Agent session 路由引擎")
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

    sub.add_parser("sessions").set_defaults(func=cmd_sessions)

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
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
