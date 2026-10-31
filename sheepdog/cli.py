"""命令行入口：doctor / poll / run / inbox / init / sessions / forward / spawn / watch / security-log / escalations /
acks / new-session / close-session / push-rules / retire / actions / loops / redeliver / show / inject / reply-card /
receipt / session-reset / work / tell / board / notices / ask / report / resolve / pending / reply / thread / lamb / roster / hook。
"""
from __future__ import annotations

import argparse, fcntl, json, logging, os, re, shutil, subprocess, sys, time, tomllib, uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import (__version__, _git_run, actions, decision_hub, default_repo_dir, duty, guard, identity as ident, lastgood,
               observe as obs, outbound, parse_version_tuple, session as sm, version_info, version_watch)
from .ack import Acker, _clear_handled_owner_reactions, clear_handled_owner_reactions
from .card import build_reply_card, owner_title, plain_text, resolve_summary_and_body
from .config import ANTIGRAVITY, CLI_HOSTS, HOSTS, NORMAL, ConfigError, default_config_path, load_config
from .engine import (BUS_TOPIC_ID, DYNAMIC, HOST_LABEL, MANAGED_KINDS, NOTICE_STATE_LABEL, RETIRED, Collector,
                     Dispatcher, add_watch, attention_notice_key, attention_reason_label, auto_recovers,
                     bus_quote_verifier, cancel_lamb, close_session, complete_lamb, delivered_text, display_time,
                     duty_reset_key, escalation_summary, forward_messages, open_escalations, owner_quote_verifier,
                     register_lamb, touch_wakeup, unsent_notice_line, write_receipt)
from .models import Message, Mention
from .people_roster import add_roster_arguments, run_roster_cli
from .playbook import PLAYBOOK_FILES, Playbook
from .prompts import with_cli
from .roster import Roster, RosterError, check_hosts, load_roster
from .security import HOLD, TAG, SecurityConfig, SecurityError, load_security
from .sink import SinkError
from .sink.agentapi import AgentApiSink, DryRunSink, transcript_contains, transcript_path
from .sink.claude import ClaudeSink
from .sink.codex import CodexSink, codex_home
from .sink.codex_profile import ProfileError, profile_overrides
from .notify import FileNotifier, OwnerDM
from .source import NullSource
from .source.lark import LarkCliSource
from .source.bot_events import BotEventConsumer
from .store import Store, infer_topic_lifecycle, now_iso


def _setup_logging(verbose: bool) -> None:
    import warnings; warnings.filterwarnings("ignore", category=ResourceWarning)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


class _Abort(Exception):
    pass


SINK_CLASSES = {"codex": CodexSink, "claude": ClaudeSink}
# 这些宿主配置要重启才生效（sink 是启动时建的）；model / effort / timeout_minutes / max_concurrent 下一轮生效
HOST_RESTART_KEYS = ("enabled", "command", "workdir", "settings", "profile")


# 会话侧命令（receipt、ask、report…，每次都是新进程）读配置失败时可以用常驻进程存下的「最后好用的那份」（lastgood）。
# run / init / doctor / hook stop 不用：常驻进程有自己的「上一份」，启动时坏了要报出来；hook 出错一律不输出。main 里按命令设。
_FALLBACK = False
_FALLBACK_NOTED: set[str] = set()


def _fallback_note(what: str, err: Exception, saved: "lastgood.Saved", name: str) -> None:
    if name in _FALLBACK_NOTED:
        return
    _FALLBACK_NOTED.add(name)
    print(f"{what}无效（{err}），这次沿用 {saved.label(name)} 最后好用的那份（{saved.directory / name}）", file=sys.stderr)


def _load_cfg():
    """读配置；会话侧命令读失败、又有最后好用的那份时用它（stderr 写一行）。没有那份照旧抛 ConfigError。"""
    try:
        return load_config()
    except ConfigError as e:
        if not _FALLBACK:
            raise
        path = default_config_path()
        saved = lastgood.find(path)
        if saved is None:
            raise
        try:
            cfg = lastgood.load_saved_config(saved, path)
        except ConfigError:
            raise e
        _fallback_note("配置", e, saved, lastgood.CONFIG)
        return cfg


def _load_roster(cfg) -> Roster:
    try:
        return load_roster(cfg.roster_file, cfg.roster_required)
    except RosterError as e:
        saved = lastgood.find_for(cfg) if _FALLBACK else None
        p = saved.path(lastgood.ROSTER) if saved is not None else None
        if p is None:
            raise
        roster = load_roster(p, True)
        _fallback_note("名册", e, saved, lastgood.ROSTER)
        return roster


def _load_security(cfg) -> SecurityConfig:
    try:
        return load_security(cfg.security_file)
    except SecurityError as e:
        saved = lastgood.find_for(cfg) if _FALLBACK else None
        p = saved.path(lastgood.SECURITY) if saved is not None else None
        if p is None:
            raise
        sec = load_security(p)
        _fallback_note("安全规则", e, saved, lastgood.SECURITY)
        return sec


def _save_last_good(cfg, which=(lastgood.CONFIG, lastgood.ROSTER, lastgood.SECURITY)) -> None:
    """常驻进程加载成功后存「最后好用的那份」。存不了只记日志，不挡这一轮。"""
    try:
        if changed := lastgood.save(cfg, which):
            logging.info("最后好用的配置已更新：%s（%s）", "、".join(changed), cfg.state_dir / lastgood.DIR_NAME)
    except Exception as e:
        logging.warning("存最后好用的配置出错: %s", e)


def load_checked_roster(cfg) -> Roster:
    """读名册并检查 managed 会话引用的宿主都已启用（7.18）。"""
    roster = _load_roster(cfg)
    check_hosts(roster, cfg.host_enabled)
    return roster


def _roster(cfg) -> Roster:
    """名册校验失败直接报错退出，不静默降级。"""
    try:
        return load_checked_roster(cfg)
    except RosterError as e:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ERROR 名册无效: {e}", file=sys.stderr)
        raise _Abort() from e


def _security(cfg) -> SecurityConfig:
    """安全规则格式错误直接报错退出（同名册）；文件不存在 = 没有规则。"""
    try:
        return _load_security(cfg)
    except SecurityError as e:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ERROR 安全规则无效: {e}", file=sys.stderr)
        raise _Abort() from e


def _build(args):
    cfg = _load_cfg()
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


def _wire_images(dispatcher, source, args) -> None:
    """消息里的图片送进会话前先下载（7.54）：常驻进程和 poll 用 lark-cli 下；--dry-run 不下（正文后面写会话自己去取的命令）。
    [media] images = false 时也不下（每批现读，热加载）。"""
    if dispatcher is None:
        return
    if getattr(args, "dry_run", False):
        dispatcher.image_off_reason = "试运行（--dry-run）不下载图片"
        return
    dispatcher.image_binary = getattr(source, "binary", "") or "lark-cli"


def _group_source(source, args):
    """往话题群发东西用的飞书（7.51）：--dry-run、测试环境（[source] enabled = false）不给，常驻进程就不往群里发。"""
    if getattr(args, "dry_run", False) or isinstance(source, NullSource):
        return None
    return source


def _sinks(cfg, store: Store, dry: bool) -> dict:
    """按宿主建 sink（7.18）：sink = dryrun 只管 Antigravity；--dry-run 一律打印。没启用的宿主不建。"""
    sinks: dict = {ANTIGRAVITY: DryRunSink() if dry or cfg.sink == "dryrun" else AgentApiSink()}
    for h in CLI_HOSTS:
        if cfg.hosts[h].enabled:
            sinks[h] = DryRunSink(h) if dry else SINK_CLASSES[h](cfg.hosts[h], store, cfg.runs_dir)
    return sinks


def _notifier(cfg, source, args, store=None):
    """给主人的通知（在岗表中断 / 失联、attention 和恢复、危险命令报警、写锁提醒、登录失效）怎么送：

    - 配了 [notify] file：一律追加写进那个文件（带时间和通知类型），不发飞书（测试环境用；拉不拉 IM 都一样），不重试；
    - 否则经机器人私聊主人（OwnerDM，带防重编号）；先落给主人的通知账本再发，发不出去按退避重试；
      7.48：[notify] owner_card 开着发成卡片；设了 [threads] owner_chat_id（测试环境）发进那个测试群；
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
        where = f"发进测试群 {cfg.threads.owner_chat_id}" if cfg.threads.owner_chat_id else "发私聊"
        how = "卡片" if cfg.notify.owner_card else "纯文本"
        return lambda text: print(f"[dryrun] 给主人{where}（{how}）：\n{text}\n")
    return OwnerDM(source, cfg.self_open_id, cfg, store=store)


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


def _last_good_report(cfg) -> str:
    """doctor：会话侧命令读不了当前配置时能用的那份（常驻进程存的）。"""
    saved = lastgood.find_for(cfg)
    names = [(lastgood.CONFIG, "配置"), (lastgood.ROSTER, "名册"), (lastgood.SECURITY, "安全规则")]
    have = [f"{label} {saved.label(n)}" for n, label in names if saved is not None and saved.path(n) is not None]
    if not have:
        return f"最后好用的那份: 还没有（常驻进程加载成功后才存到 {cfg.state_dir / lastgood.DIR_NAME}）"
    return f"最后好用的那份: {saved.directory}（{'，'.join(have)}）"


def cmd_doctor(args) -> int:
    ok = True
    try:
        cfg = load_config()
        bad_cfg = ""
    except ConfigError as e:
        saved = lastgood.find(default_config_path())
        if saved is None:
            print(f"配置文件: ✗ 无效：{e}\n  没有最后好用的那份：会话命令（receipt / ask / report …）也会失败")
            return 2
        cfg = lastgood.load_saved_config(saved, default_config_path())
        bad_cfg = (f"配置文件: ✗ 当前无效：{e}\n  会话命令在用 {saved.label(lastgood.CONFIG)} 最后好用的那份"
                   f"（{saved.directory / lastgood.CONFIG}）；常驻进程沿用它上一份；下面按那份检查")
        ok = False
    print(get_version_status(Store(cfg.db_path))["summary_line"])
    if bad_cfg:
        print(bad_cfg)
    # 开头先说有没有给主人的通知没发出去（还在重试的、放弃了没人处理的），直到人工处理
    if warn := unsent_notice_line(Store(cfg.db_path), cfg.cli_command):
        print(warn)
    if not bad_cfg:
        print(f"配置文件: {cfg.config_path} {'✓' if cfg.config_path.exists() else '✗ 不存在（使用默认值）'}")
    print(f"状态目录: {cfg.state_dir}")
    print(_last_good_report(cfg))
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
        saved = lastgood.find_for(cfg)
        p_ = saved.path(lastgood.ROSTER) if saved is not None else None
        print(f"名册: ✗ {e}\n  " + (f"会话命令在用 {saved.label(lastgood.ROSTER)} 最后好用的那份（{p_}）" if p_
                                       else "没有最后好用的那份：会话命令也会失败"))
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
        saved = lastgood.find_for(cfg)
        p_ = saved.path(lastgood.SECURITY) if saved is not None else None
        print(f"安全规则: ✗ {e}\n  " + (f"会话命令在用 {saved.label(lastgood.SECURITY)} 最后好用的那份（{p_}）" if p_
                                         else "没有最后好用的那份：会话命令也会失败"))
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
    print(owner_chats_report(cfg))
    print(topic_group_report(cfg, store))
    for line in _test_group_warnings(cfg, store):   # 7.56：建了测试群忘了登记进 ignore，下一次 doctor 就看见
        print(line)
    print(media_report(cfg))
    for line in switches_report(cfg):
        print(line)
    for line in threads_report(cfg):
        print(line)
    for line in identity_report(cfg, store):
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


TEST_GROUP_MARK = "🧪"


def _test_group_warnings(cfg, store: Store) -> list[str]:
    """doctor（7.56）：账本里出现过、群名以「🧪」开头、却不在 [routing] ignore_chat_ids 里的群，逐个列出 chat_id 和群名。
    测试群和正式共用主人的飞书身份，正式牧羊犬照样拉得到；不在 ignore 里，里面的消息会被当真（当背景、当指令送总线）。
    找主人那一侧（找主人聊天、话题群）本来就不走普通路由，不报。群名按账本里最近的那条。只警告，不改退出码。"""
    latest: dict[str, str] = {}
    for r in store.conn.execute("SELECT chat_id, chat_name FROM messages WHERE chat_name LIKE ? "
                                "ORDER BY create_time", (f"%{TEST_GROUP_MARK}%",)):
        if r["chat_id"]:
            latest[r["chat_id"]] = r["chat_name"] or ""
    bad = [(cid, name.strip()) for cid, name in latest.items()
           if name.strip().startswith(TEST_GROUP_MARK) and cfg.chat_scope(cid) == NORMAL]
    if not bad:
        return []
    out = [f"测试群: ⚠ {len(bad)} 个「{TEST_GROUP_MARK}」开头的群不在 [routing] ignore_chat_ids 里，"
           "里面的消息会被正式牧羊犬当真（当背景、当指令送总线）："]
    out += [f"  ⚠ {cid}  {name}" for cid, name in sorted(bad, key=lambda x: x[1])]
    return out


def media_report(cfg) -> str:
    """doctor：消息里的图片送进会话前先下载（7.54）。"""
    m = cfg.media
    if not m.images:
        return "图片（7.54）: 关（[media] images = false），只在正文后面写会话自己去取的命令"
    return (f"图片（7.54）: 开，下到 {cfg.media_dir}；单张上限 {m.max_image_mb:g}MB、每批最多下 {m.max_images_per_batch} 张、"
            f"每张超时 {m.image_timeout_seconds:g} 秒；跟着 retention_days={cfg.retention_days} 清理")


def owner_chats_report(cfg) -> str:
    """doctor：「找主人」聊天按 Config.owner_chat_ids 算（[escalation] chat_ids 加测试用的 [threads] owner_chat_id）。"""
    chats, esc = cfg.owner_chat_ids, cfg.escalation
    if not chats:
        return "找主人聊天: （未配置：主人在 IM 上的回复不会送回会话）"
    extra = "，含测试群 [threads] owner_chat_id" if cfg.threads.owner_chat_id else ""
    return f"找主人聊天: ✓ {len(chats)} 个{extra}，未结保留 {esc.open_hours:g} 小时"


def topic_group_report(cfg, store: Store) -> str:
    """doctor：话题群（7.51）配没配、开没开、是不是退回私聊了、跟踪中几件。"""
    from . import topicgroup as tg
    g = cfg.topic_group
    if not g.chat_id:
        return "话题群: 没配（[topic_group] chat_id 为空，长期跟踪的事照旧在私聊）"
    if not g.enabled:
        return f"话题群: {g.chat_id} 关着（[topic_group] enabled = false：都走私聊，群里的消息不分派）"
    down = tg.down_info(store)
    n = len(store.list_items((tg.ACTIVE, tg.DONE)))
    if down is not None:
        return f"话题群: ⚠ {g.chat_id} 已退回私聊（{down.get('since', '')}：{down.get('why', '')}），跟踪中 {n} 件"
    idx = tg.index_info(store)
    return (f"话题群: ✓ {g.chat_id}，跟踪中 {n} 件，索引卡 {idx.get('message_id') or '还没发'}，"
            f"每 {g.index_refresh_minutes:g} 分钟对账")


def switches_report(cfg) -> list[str]:
    """doctor：真实飞书测试用的两个开关（7.22），设了就提示，正式环境不要设。"""
    out = []
    if cfg.source.only_chat_ids:
        out.append(f"⚠ 只拉这些聊天：{', '.join(cfg.source.only_chat_ids)}（测试用，[source] only_chat_ids；正式环境不要设）")
    if cfg.threads.owner_chat_id:
        out.append(f"⚠ 找主人改发到群 {cfg.threads.owner_chat_id}（测试用，[threads] owner_chat_id；正式环境不要设）")
    return out


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def _record_daemon_runtime(store: Store, repo_dir: Path | None = None) -> dict:
    return version_watch.record_daemon_runtime(store, repo_dir)


def get_version_status(store: Store | None = None, repo_dir: Path | None = None, *, check_remote: bool = False) -> dict:
    return version_watch.get_version_status(store, repo_dir, check_remote=check_remote)


def _check_version_update(
    cfg,
    store: Store,
    dispatcher: Dispatcher | None,
    *,
    runtime_info: dict | None = None,
    repo_dir: Path | None = None,
    now: datetime | None = None,
) -> list[str]:
    return version_watch.check_version_update(
        cfg, store, dispatcher, runtime_info=runtime_info, repo_dir=repo_dir, now=now
    )


def cmd_version(args) -> int:
    store = None
    try:
        cfg = _load_cfg()
        if cfg.db_path.exists():
            store = Store(cfg.db_path)
    except Exception:
        store = None
    try:
        check_remote = bool(getattr(args, "check_remote", False))
        if getattr(args, "json", False):
            st = get_version_status(store, check_remote=check_remote)
            if check_remote and not st.get("remote"):
                st["remote"] = version_watch.probe_remote_versions()
            print(json.dumps(st, ensure_ascii=False, indent=2))
        elif check_remote:
            print(version_watch.format_version_query_reply(store, check_remote=True))
        else:
            print(get_version_status(store)["summary_line"])
        return 0
    finally:
        if store is not None:
            store.close()


def cmd_upgrade(args, *, repo_dir: Path | None = None) -> int:
    cfg = _load_cfg()
    root = (repo_dir or default_repo_dir()).resolve()
    no_pull = bool(getattr(args, "no_pull", False))
    dry_run = bool(getattr(args, "dry_run", False))
    rollback = bool(getattr(args, "rollback", False))
    target_sha = str(getattr(args, "to", "") or "").strip()
    notes = str(getattr(args, "notes", "") or "").strip()
    channel = "rollback" if rollback else ("beta" if getattr(args, "beta", False) else "stable")
    disk_before = version_info(root, prefer_disk_version=True)
    if dry_run:
        store = Store(cfg.db_path) if cfg.db_path.exists() else None
        try:
            st = get_version_status(store, root)
        finally:
            if store is not None:
                store.close()
        if rollback:
            pull_plan = f"回滚至 {target_sha or '上一版本'}"
        else:
            pull_plan = "跳过 git pull" if (no_pull or disk_before["dirty"]) else f"git -C {root} pull --ff-only"
        print(f"[dry-run] upgrade (channel={channel}): {pull_plan}；{st['summary_line']}")
        return 0

    if not rollback and not no_pull and (root / ".git").exists():
        if not disk_before["dirty"]:
            pulled, _out, err_msg = version_watch.pull_repo_for_channel(root, channel=channel, timeout=15.0)
            if not pulled and err_msg != "git 命令执行失败":
                print(f"git pull --ff-only 失败：{err_msg}", file=sys.stderr)
                return 1
        else:
            print("提示：工作区有未提交改动（dirty），跳过 git pull，直接按本机当前代码触发重启。")

    store = Store(cfg.db_path)
    try:
        st_before = get_version_status(store, root)
        daemon_info = st_before.get("daemon") if isinstance(st_before.get("daemon"), dict) else {}
        from_sha = str(daemon_info.get("git_sha") or disk_before.get("git_sha") or "").strip()
        from_display = str(daemon_info.get("display") or disk_before.get("display") or "").strip()

        if rollback and not st_before["daemon_alive"]:
            rb_ref = target_sha
            if not rb_ref:
                saved = version_watch._safe_json(store.get_meta("version_rollback_target"))
                rb_ref = str(saved.get("git_sha") or "") if isinstance(saved, dict) else ""
            ok, _out, err_msg = version_watch.rollback_repo_to_target(root, rb_ref or "HEAD~1")
            if not ok:
                print(f"版本回滚失败：{err_msg}", file=sys.stderr)
                return 1

        st = get_version_status(store, root)
        if st["daemon_alive"]:
            existing = [a for a in store.pending_actions() if a["kind"] == "upgrade"]
            cmd_str = "sheepdog upgrade --rollback" if rollback else ("sheepdog upgrade --beta" if channel == "beta" else "sheepdog upgrade")
            payload: dict = {"no_pull": True, "channel": channel, "command": cmd_str}
            if rollback:
                payload["rollback"] = True
                if target_sha:
                    payload["target_sha"] = target_sha
            if from_sha:
                payload["from_sha"] = from_sha
            if from_display:
                payload["from_display"] = from_display
            if notes:
                payload["notes"] = notes
            if existing:
                aid = existing[0]["id"]
                with store.conn:
                    store.conn.execute("UPDATE actions SET args_json = ? WHERE id = ?", (json.dumps(payload, ensure_ascii=False), aid))
            else:
                aid = actions.enqueue(store, "upgrade", payload)
            print(f"已提交常驻进程原地热重启（动作 #{aid}，channel={channel}，目标版本 {st['cli']['display']}，当前常驻 {st['daemon_display']}）")
            return 0
        print(f"代码已就绪：{st['cli']['display']}（当前无运行中的常驻进程，将由 sidecar supervisor 自动拉起或运行 `sheepdog run`）")
        return 0
    finally:
        store.close()


def _acquire_daemon_lock(state_dir: Path):
    """常驻进程单实例内核级排他锁（7.69 REG-008）：
    对 state_dir / 'sheepdog.run.lock' 加非阻塞 LOCK_EX | LOCK_NB。
    若已被另一个常驻进程持有，返回 None；成功则返回打开的文件对象（随进程生命周期保持打开）。
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "sheepdog.run.lock"
    f = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        f.close()
        return None
    try:
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"pid": os.getpid(), "ppid": os.getppid(), "started_at": now_iso()}, ensure_ascii=False) + "\n")
        f.flush()
    except OSError:
        pass
    return f


def _release_daemon_lock(lock_file) -> None:
    if lock_file is None:
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        lock_file.close()
    except OSError:
        pass


def _exec_restart() -> None:
    """常驻进程原地热换像（7.69 REG-007）：保留原 PID、PPID（如 Antigravity sidecar 18900）与全部环境变量。"""
    logging.info("sheepdog 常驻进程原地重启（os.execv）: %s %s", sys.executable, sys.argv)
    os.execv(sys.executable, [sys.executable, *sys.argv])


def cmd_observe(args) -> int:
    try:
        cfg = _load_cfg()
    except Exception:
        cfg = None
    db_path = Path(args.db).expanduser() if getattr(args, "db", None) else None
    state_path = Path(args.state).expanduser() if getattr(args, "state", None) else None
    res = obs.run_observe(
        cfg,
        db_path=db_path,
        state_path=state_path,
        since_minutes=args.since_minutes,
        notify_lark=args.notify_lark,
        notify_chat_id=args.notify_chat_id,
        alert_cooldown_minutes=args.alert_cooldown_minutes,
        simulate_warn=args.simulate_warn,
        self_heal=args.self_heal,
    )
    print(obs.render_observe_output(res, as_json=args.json))
    return 1 if res["warns"] else 0


def identity_report(cfg, store: Store) -> list[str]:
    """doctor：区分智能体身份和本人身份（7.26）。机器人接收在不在、最近一次收到事件的时间、机器人不在而代回退回本人的群。"""
    ic = cfg.identity
    if not ic.enabled:
        return ["身份区分: 关（[identity] enabled = false）：代回一律用主人本人身份、带代回标记"]
    oid = ic.bot_open_id or store.get_meta(ident.META_OPEN_ID)
    app = ic.bot_app_id or store.get_meta(ident.META_APP_ID)
    out = [f"身份区分: 开；机器人 open_id {oid or '✗ 不知道（sheepdog run 启动时查，或 [identity] bot_open_id 手填）'}"
           f"，app_id {app or '✗ 不知道'}"]
    st = ident.consumer_state(store)
    if not st:
        out.append("  机器人接收: ✗ 没记录（sheepdog run 还没起过）")
    else:
        owner = st.get("owner_pid")
        running = st.get("state") in ("running", "starting") and st.get("pid") and _pid_alive(st.get("pid")) \
            and owner and _pid_alive(owner)
        label = {"running": "✓ 在收", "starting": "起来了、还没连上", "down": "✗ 退出了，等重启",
                 "stopped": "✗ 停了"}.get(st.get("state", ""), st.get("state", ""))
        if st.get("state") in ("running", "starting") and not running:
            label = "✗ 不在（记录里的进程已经不在了：sheepdog run 没在跑？）"
        out.append(f"  机器人接收: {label}；pid {st.get('pid') or '-'}，最近一次收到事件 {st.get('last_event_at') or '（还没有）'}"
                   f"，共 {st.get('events', 0)} 个，重启 {st.get('restarts', 0)} 次；状态记于 {st.get('updated_at') or '-'}")
        if st.get("last_exit"):
            out.append(f"  最近一次退出: {st['last_exit']}")
        if st.get("last_error"):
            out.append(f"  最近一次报错: {st['last_error']}")
    chats, at = ident.cached_bot_chats(store)
    out.append(f"  机器人在的群: {len(chats)} 个（{at.isoformat(timespec='minutes') if at else '缓存已作废'}查的，"
               f"缓存 {ic.bot_chats_cache_minutes:g} 分钟）" if chats is not None else "  机器人在的群: 还没查过（第一次代回时查）")
    absent = store.bot_absent_chats()
    if absent:
        out.append(f"  ⚠ 机器人不在、代回退回本人的群 {len(absent)} 个（建议把机器人拉进去）：")
        out += [f"    - {r['chat_name'] or '-'}（{r['chat_id']}）退回 {r['fallbacks']} 次，最近 {r['last_at']}" for r in absent[:20]]
    since = (datetime.now().astimezone() - timedelta(hours=ic.bot_dm_reply_hours)).isoformat(timespec="seconds")
    n = len(store.list_bot_dm_replies(since))
    pb = Playbook.from_config(cfg)
    out.append(f"  别人私聊机器人: 不分派，自动回一句（{'playbook/bot_dm.md' if pb.read('bot_dm.md').strip() else '⚠ 没有 bot_dm.md，用代码里的默认话'}），"
               f"同一个人 {ic.bot_dm_reply_hours:g} 小时内只回一次；这段时间回了 {n} 次")
    return out


def threads_report(cfg) -> list[str]:
    """doctor：一事一串（7.22）。ask / report 加的开头一行要能被 [escalation] 的正则认出来，否则「需要你定」登记不上、
    主人的回复送不回去。"""
    tc, esc = cfg.threads, cfg.escalation
    out = [f"一事一串: 话题里除了开头那条满 {tc.complex_min_replies} 条算复杂，附最近 {tc.recent_replies} 条"]
    sample = "tp_doctor_check"
    ask = tc.header("ask", "示例", sample)
    report = tc.header("report", "示例", sample)
    if cfg.owner_chat_ids:
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
    out.append("  发给主人的消息（7.48）: " + ("卡片（[notify] owner_card = true）" if cfg.notify.owner_card
                                            else "纯文本（[notify] owner_card = false）"))
    if cfg.owner_chat_ids and cfg.notify.owner_card and esc.pattern is not None:
        # 卡片读回来是 <card title="开头一行">…</card>：按还原后的文字再认一遍，和纯文本的认法一致才算对
        back = plain_text(f'<card title="{owner_title(ask)}">\n示例正文\n</card>')
        hit = esc.pattern.search(back)
        if not (hit and hit.group("topic") == sample):
            out.append(f"  ✗ ask 发成卡片后读回「{back.splitlines()[0]}」认不出来：「需要你定」登记不上，"
                       "先把 [notify] owner_card 关掉（改回纯文本）再查")
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
                try: lines.append("  展开: " + " ".join(profile_overrides(hc.profile)))
                except ProfileError as e: lines.append(f"  ✗ 配置档有问题，这个宿主的每一轮都会直接失败：{e}")
            else:
                lines.append(f"  配置档: 没设（只用用户配置 {sb['file']}）")
            source = (f"来自配置档 {sb['file']}，sheepdog 展开成 -c 传给自己起的 app-server" if hc.profile
                      else "来自 Codex 用户配置，sheepdog 不传权限参数")
            lines.append(f"  沙箱: sandbox_mode = {mode}，网络 {net}，writable_roots = {sb['roots']}（{source}）")
            if mode == "read-only" or net == "关":
                lines.append("  ⚠ 这个宿主的会话能读、能分析，但发不了飞书")
    return lines


BOT_EVENT_SETTLE_SECONDS = 2


def _wakeup_mtime(cfg) -> int:
    try: return (cfg.state_dir / ".wakeup").stat().st_mtime_ns
    except OSError: return 0


def _mini_cycle(collector: Collector, dispatcher: Dispatcher | None, *, local_wake: bool = False) -> dict | None:
    """机器人事件或本地 CLI 写后（.wakeup）触发的小轮次：只入账 + 投递，不跑搜索。"""
    stats = collector.poll_bot_only()
    if not local_wake and (not stats or not stats.get("fetched")):
        return stats
    logging.info("机器人事件小轮次: %s", stats)
    if dispatcher:
        res = dispatcher.deliver_only(check_receipts=local_wake)
        logging.info("机器人事件小轮次投递: %s", res)
        if dispatcher.store.pending_actions():
            if n := actions.run_pending(dispatcher):
                logging.info("机器人事件小轮次执行动作 %d 条", n)
    return stats


def _wait_next(args, collector: Collector, dispatcher: Dispatcher | None, clock=None, sleep=None) -> int:
    """两轮完整轮次之间等待机器人事件或本地 CLI .wakeup 信号（7.45 / 7.86 REG-050）。"""
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    deadline = clock() + (args.interval or collector.cfg.poll_interval_seconds)
    consumer, n, w0 = collector.bot_events, 0, _wakeup_mtime(collector.cfg)
    while True:
        if getattr(dispatcher, "restart_requested", False) or (left := deadline - clock()) <= 0:
            return n
        if not (collector.cfg.run.wake_on_bot_event and consumer is not None and hasattr(consumer, "wait")):
            sleep(left); return n
        local_wake, ev_woke = False, False
        if isinstance(consumer, BotEventConsumer):
            while (left := deadline - clock()) > 0:
                if consumer.wait(min(left, 0.5)):
                    ev_woke = True; break
                if (w_now := _wakeup_mtime(collector.cfg)) != w0:
                    w0, local_wake = w_now, True; break
            if not ev_woke and not local_wake:
                return n
        elif not consumer.wait(left):
            return n
        else:
            ev_woke = True
        if ev_woke:
            sleep(max(0.0, min(BOT_EVENT_SETTLE_SECONDS, deadline - clock())))
        try:
            _mini_cycle(collector, dispatcher, local_wake=local_wake) if local_wake else _mini_cycle(collector, dispatcher)
        except Exception as e:
            logging.error("机器人事件小轮次失败: %s: %s", type(e).__name__, e)
        n += 1
        if getattr(dispatcher, "restart_requested", False):
            return n


def _cycle(collector: Collector, dispatcher: Dispatcher | None) -> None:
    stats = collector.poll_once()
    logging.info("poll: %s", stats)
    if dispatcher:
        res = dispatcher.dispatch_once()
        logging.info("dispatch: %s", res)
        if dispatcher.store.pending_actions():
            if n := actions.run_pending(dispatcher):
                logging.info("执行动作 %d 条", n)


def cmd_poll(args) -> int:
    cfg, store, source, sink, roster, security = _build(args)
    acker = _acker(cfg, store, source, args)
    collector = Collector(cfg, store, source, roster, security, acker)
    collector.dry_run = bool(getattr(args, "dry_run", False))
    dispatcher = None if args.no_dispatch else Dispatcher(cfg, store, sink, roster, security, acker,
                                                          _notifier(cfg, source, args, store), _group_source(source, args))
    _wire_images(dispatcher, source, args)
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
    """run 每轮重读 config.toml（7.16）：改坏了沿用上一份并 WARNING；state_dir / sink 变了只提示需要重启。
    改坏包括语法错误、读不了、结构不对（load_config 都包成 ConfigError，写明文件和行号）；万一漏网的别的异常也一样
    沿用上一份，不让这一轮的拉取和投递停下来（7.40）。"""
    try:
        new = load_config()
    except ConfigError as e:
        logging.warning("配置无效，沿用上一份: %s", e)
        return old
    except Exception as e:  # 兜底：读配置不该让整轮失败
        logging.warning("配置无效，沿用上一份（读配置出错 %s）: %s", type(e).__name__, e)
        return old
    if new.state_dir != old.state_dir:
        logging.warning("state_dir 改成了 %s，需要重启 sheepdog 才生效；本次沿用 %s", new.state_dir, old.state_dir)
        new.state_dir = old.state_dir
    if new.sink != old.sink:
        logging.warning("sink 改成了 %s，需要重启 sheepdog 才生效；本次沿用 %s", new.sink, old.sink)
        new.sink = old.sink
    if new.identity.enabled != old.identity.enabled:
        logging.warning("[identity] enabled 改成了 %s，需要重启 sheepdog run 才生效；本次沿用 %s",
                        new.identity.enabled, old.identity.enabled)
        new.identity.enabled = old.identity.enabled
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
    _save_last_good(new, (lastgood.CONFIG,))   # 重读成功：存一份给会话侧命令兜底（内容没变不重写）
    dispatcher.playbook = Playbook.from_config(new)  # 代回前缀、等待分钟数等占位符跟着新配置
    if hasattr(source, "max_pages"):
        source.max_pages = new.max_pages
    if hasattr(source, "tz"):
        source.tz = new.timezone_offset
    collector.acker = dispatcher.acker = _acker(new, store, source, args)
    # 主人通知的去向跟着配置走（[notify] file 改了下一轮生效）
    dispatcher.notifier = _notifier(new, source, args, store)
    return new


def cmd_run(args) -> int:
    # 本进程是常驻进程：只有它能跨项目调 agentapi，会话里发起的动作由它执行
    was_resident = actions.is_resident()
    cfg, store, source, sink, roster, security = _build(args)
    lock_file = None
    if not getattr(args, "dry_run", False):
        lock_file = _acquire_daemon_lock(cfg.state_dir)
        if lock_file is None:
            store.close()
            msg = f"已有 sheepdog run 常驻进程持有单实例锁（{cfg.state_dir / 'sheepdog.run.lock'}），本实例安全退出"
            logging.warning(msg)
            print(msg, file=sys.stderr)
            return 0
    actions.mark_resident()
    consumer = None
    dispatcher = None
    try:
        _save_last_good(cfg)   # 启动时三样都加载成功了：存一份给会话侧命令兜底
        acker = _acker(cfg, store, source, args)
        collector = Collector(cfg, store, source, roster, security, acker)
        collector.dry_run = bool(getattr(args, "dry_run", False))
        dispatcher = Dispatcher(cfg, store, sink, roster, security, acker, _notifier(cfg, source, args, store),
                                _group_source(source, args))
        _wire_images(dispatcher, source, args)
        logging.info("sheepdog 常驻运行，间隔 %ss，宿主 %s", args.interval or cfg.poll_interval_seconds,
                     "、".join(f"{h}={s.name}" for h, s in sink.items()))
        runtime_info = version_info() if getattr(args, "dry_run", False) else _record_daemon_runtime(store)
        dispatcher.runtime_info = runtime_info
        consumer = _start_bot_events(cfg, store, source, args)
        collector.bot_events = consumer
        return _run_loop(args, cfg, store, source, collector, dispatcher, runtime_info=runtime_info)
    finally:
        actions._resident = was_resident
        if consumer is not None:
            consumer.stop()
        _release_daemon_lock(lock_file)
        try:
            store.close()
        except Exception:
            pass
        if dispatcher is not None and getattr(dispatcher, "restart_requested", False) and not getattr(args, "dry_run", False):
            _exec_restart()


def _start_bot_events(cfg, store: Store, source, args):
    """[identity] 开着（7.26）：查一次机器人自己的 id，起机器人身份的实时接收。不拉飞书、--dry-run 不起。"""
    if not cfg.identity.enabled or not cfg.source.enabled or getattr(args, "dry_run", False):
        return None
    ident.resolve_bot_ids(cfg, store, source)
    ic = cfg.identity
    consumer = BotEventConsumer(getattr(source, "binary", "lark-cli"), ic.restart_backoff_seconds,
                                ic.restart_backoff_max_seconds,
                                state_writer=lambda st: store.set_meta(ident.META_CONSUMER, json.dumps(st, ensure_ascii=False)))
    consumer.start()
    return consumer


def _prune(store: Store, cfg) -> dict[str, int]:
    """常驻进程每小时一次的旧数据清理：账本、回执、命令行宿主的运行文件、在岗表履历，加上送进会话的图片（7.54）。"""
    return store.prune(cfg.retention_days, cfg.receipts_dir, cfg.runs_dir, cfg.duty.history_days, cfg.media_dir)


def _run_loop(args, cfg, store: Store, source, collector: Collector, dispatcher: Dispatcher,
              *, runtime_info: dict | None = None) -> int:
    last_prune = 0.0
    while True:
        try:
            # 每轮重读 config.toml：轮询间隔、关键词、路由、ack、watch、loop_guard、owner_context 等下一轮生效
            cfg = _reload_config(cfg, args, store, source, collector, dispatcher)
            # 每轮重读名册：改名册不必重启；改坏了沿用上一份有效名册并报错，常驻进程不退出
            try:
                collector.roster = dispatcher.roster = load_checked_roster(cfg)
                _save_last_good(cfg, (lastgood.ROSTER,))
            except RosterError as e:
                logging.error("名册无效，沿用上一份: %s", e)
            # 安全规则同样每轮现读，改坏了沿用上一份有效规则
            try:
                collector.security = dispatcher.security = load_security(cfg.security_file)
                _save_last_good(cfg, (lastgood.SECURITY,))
            except SecurityError as e:
                logging.error("安全规则无效，沿用上一份: %s", e)
            # 先执行会话发起的动作（spawn / new-session / push-rules / init / retire / upgrade），再拉取和投递
            if n := actions.run_pending(dispatcher):
                logging.info("执行动作 %d 条", n)
            if getattr(dispatcher, "restart_requested", False):
                return 0
            _cycle(collector, dispatcher)
            if getattr(dispatcher, "restart_requested", False):
                return 0
            if cfg.version.enabled:
                _check_version_update(cfg, store, dispatcher, runtime_info=runtime_info)
            if time.time() - last_prune > 3600:
                pruned = _prune(store, cfg)
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
        _wait_next(args, collector, dispatcher)
        if getattr(dispatcher, "restart_requested", False):
            return 0


def cmd_inbox(args) -> int:
    cfg = _load_cfg()
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
    cfg = _load_cfg()
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
    cfg = _load_cfg()
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
            lc = t["lifecycle"] or infer_topic_lifecycle(t["title"])
            print(f"  创建于: {t['created_at']}  生命周期: {lc}  创建原因: {t['origin_note'] or '-'}")
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


def _reject_vip_needs_decision_without_floor_ask(
    cfg, store: Store, tid: str, bid: str, status: str, *, vips: dict | None = None
) -> str | None:
    if status != "needs_decision":
        return None
    tid = _topic_arg(tid)
    t = store.get_topic(tid)
    eff_bid = (bid or "").strip() or (t["pending_batch_id"] if t is not None and t["pending_batch_id"] else "")
    if not (vips_map := _ask_vips(cfg) if vips is None else vips):
        return None
    vip_srcs = outbound.vip_sources(cfg, store, tid, None, vips_map, batch=eff_bid)
    if not vip_srcs and t is not None and t["pending_batch_id"] and t["pending_batch_id"] != eff_bid:
        vip_srcs = outbound.vip_sources(cfg, store, tid, None, vips_map, batch=t["pending_batch_id"])
    if not vip_srcs:
        return None
    from .roster import primary_topic_id
    p_tid = primary_topic_id(tid)
    group_tids = list(dict.fromkeys([tid, p_tid, *(r["topic_id"] for r in store.list_topics() if primary_topic_id(r["topic_id"]) == p_tid)]))
    for gtid in group_tids:
        rows = store.list_sends(topic_id=gtid) if hasattr(store, "list_sends") else store.conn.execute("SELECT * FROM sends WHERE topic_id=?", (gtid,)).fetchall()
        for s in rows:
            sd = dict(s)
            if sd.get("kind") == "ask" and outbound._is_valid_vip_floor(sd.get("floor") or ""):
                esc = store.get_escalation(sd["message_id"]) if sd.get("message_id") else None
                if esc is None or not esc["closed_at"]:
                    return None
        for e in open_escalations(store, cfg.escalation.open_hours, gtid):
            s = store.send_by_message(e["message_id"])
            if s is not None and outbound._is_valid_vip_floor(s["floor"] or ""):
                return None
    return ('receipt 没收: 当前批次含 VIP 请求，未发起带合法 --floor-type 的 sheepdog ask，'
            '禁止提交 status="needs_decision" 挂起等主人；请按 VIP 规则合理就办并提交 status="handled"（长任务用 status="waiting"）')


def _check_monolithic_parent_turn(cfg, store: Store, tid: str, bid: str) -> str | None:
    tid = _topic_arg(tid)
    t = store.get_topic(tid)
    eff_bid = (bid or "").strip() or (t["pending_batch_id"] if t is not None and t["pending_batch_id"] else "")
    if t is None or not eff_bid or not (t["conversation_id"] or "").strip():
        return None
    d = store.get_dispatch(eff_bid)
    if d is None or not (sent_dt := guard._iso_dt(str(d["sent_at"] or ""))) or not (now_dt := guard._iso_dt(now_iso())):
        return None
    if (elapsed_sec := (now_dt - sent_dt).total_seconds()) < 90:
        return None
    for lb in store.list_lambs(parent_topic_id=tid, include_finished=True):
        st_dt = guard._iso_dt(str(lb.get("started_at") or ""))
        if lb.get("state") == "running" or (st_dt is not None and st_dt >= sent_dt):
            return None
    if (parent_steps := guard.count_parent_tool_steps_since(transcript_path(t["conversation_id"]), str(d["sent_at"]))) < 6:
        return None
    store.add_history(tid, "monolithic_turn", f"批次 {eff_bid} 耗时 {int(elapsed_sec)}s 母羊串行 {parent_steps} 步且 0 羔羊")
    return (f"⚠️ [牧羊犬并发守卫 REG-015] 本批母羊独自串行执行了 {parent_steps} 步业务工具（耗时 {int(elapsed_sec)}s）"
            "且派生了 0 只羔羊，导致会话长时间锁死在 running 态阻塞新消息！按规则：凡整批 >3 步工具调用或 >45s 的任务，"
            "必须在前 3 步内用 `sheepdog lamb prepare` + `invoke_subagent`（或 `sheepdog lamb spawn`）派生 1..N 只并行羔羊，"
            '并立即交 status="waiting" 回执卸锁！')


def cmd_receipt(args) -> int:
    cfg = _load_cfg()
    warn_msg: str | None = None
    try:
        receipt = json.loads(args.json)
        if isinstance(receipt, dict) and cfg.db_path.exists():
            store = Store(cfg.db_path)
            try:
                if not outbound.lamb_violation_message(store, "receipt", topic_id=args.topic):
                    if err := (_reject_vip_needs_decision_without_floor_ask(cfg, store, args.topic, args.batch, str(receipt.get("status") or ""))
                               or decision_hub.verify_receipt_no_dead_ball(cfg, store, args.topic, args.batch, receipt)
                               or decision_hub.verify_bus_at_me_no_silent_drop(cfg, store, args.topic, args.batch, receipt)):
                        print(err, file=sys.stderr); return 2
                    warn_msg = _check_monolithic_parent_turn(cfg, store, args.topic, args.batch)
            finally:
                store.close()
        p = write_receipt(cfg, args.topic, args.batch, receipt)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"回执无效: {e}", file=sys.stderr)
        return 2
    if warn_msg:
        print(warn_msg)
    print(f"回执已提交: {p}")
    return 0


def cmd_forward(args) -> int:
    cfg = _load_cfg()
    security, store = _security(cfg), Store(cfg.db_path)
    verify = bus_quote_verifier(store, security.quote_max_age_hours)
    try:
        ids = forward_messages(store, args.topic, (args.message_ids or "").split(","), args.note, args.quote, verify,
                               cfg.self_open_id, cfg.escalation.open_hours, cfg.duty.patterns, cfg.cli_command,
                               answers=getattr(args, "answers", "") or "")
    except ValueError as e:
        print(f"转交被拒绝: {e}", file=sys.stderr); return 2
    for mid, batch, sent_at in ids.skipped:
        print(f"`{mid}` {delivered_text(args.topic, batch, sent_at, cfg.timezone_offset)}，这次不重发")
    extra = "，附主人原话 / 总线备注" if args.quote or args.note else ""
    if ans := (getattr(args, "answers", "") or "").strip():
        print(f"已标记提问 `{args.answers}` 为已答（{args.topic}）")
        if cfg.source.enabled:
            esc, m0 = store.get_escalation(ans), (store.get_message((ids or [s[0] for s in ids.skipped])[0]) if (ids or ids.skipped) else None)
            raw_ans = " ".join(plain_text((m0["content"] if m0 is not None else "") or args.quote or args.note or "").split())[:60]
            try: decision_hub.sync_escalation_dual_cards(cfg, store, _source(cfg), ans, chosen_display=f"已在其他聊天答复：{raw_ans}" if raw_ans else "已在其他聊天答复", topic_id=_topic_arg(args.topic), orig_text=(esc["text"] or "") if esc is not None else "")
            except Exception: pass
    print(f"已转交 {len(ids)} 条消息{extra}到 {args.topic}，sheepdog 下一轮投递（对方被主人接管时排队）" if (ids or extra or not ids.skipped)
          else f"没有要转交的：{len(ids.skipped)} 条都已经送到 {args.topic}，不用再转")
    return 0


def cmd_watch(args) -> int:
    cfg = _load_cfg()
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
    cfg = _load_cfg()
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
    cfg = _load_cfg()
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
    cfg = _load_cfg()
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
    cfg = _load_cfg()
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
    cfg = _load_cfg()
    store = Store(cfg.db_path)
    tid = _topic_arg(args.topic) if getattr(args, "topic", "") else ""
    open_ids = {e["message_id"] for e in open_escalations(store, cfg.escalation.open_hours, tid or None)}
    rows = store.list_escalations(include_closed=args.all)
    if tid:
        rows = [e for e in rows if e["topic_id"] == tid]   # 7.38：只列这个会话名下的
    if not args.all:
        rows = [e for e in rows if e["message_id"] in open_ids]  # 超时还没被常驻进程关掉的也不算未结
    if not rows:
        who = f"{tid} 名下" if tid else ""
        print(f"{who}没有未结的「需要你定」" if not args.all else f"{who}没有「需要你定」记录")
        return 0
    for e in rows:
        if e["answered_at"]:
            state = f"已答 @ {e['answered_at']}（答复 {e['answer_message_id']}）"
        elif e["close_reason"] == "resolved":
            state = f"会话结案 @ {e['closed_at']}（{e['resolved_by']}：{e['resolve_note']}）"
        elif e["message_id"] in open_ids:
            state = "未结" + (f"，可能已处理（{e['possibly_handled_reason']}，{e['possibly_handled_message_id']}）"
                             if e["possibly_handled_at"] else "")
        else:
            state = f"已关闭（{e['close_reason'] or '超时'}）"
        print(f"- [{state}] topic={e['topic_id']} 提问 {e['asked_at']} message_id={e['message_id']}")
        print(f"  问题: {escalation_summary(e)}")
    return 0


def cmd_acks(args) -> int:
    cfg = _load_cfg()
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
                host=args.host, model=args.model, effort=args.effort, quote=args.quote, lifecycle=args.lifecycle)
        except ValueError as e:
            print(f"new-session 被拒绝: {e}", file=sys.stderr)
            return 2
        return _queued(args, store, cfg, "new_session", {"key": args.key, "title": args.title, "duty": args.duty,
                                                    "chats": chats, "message_ids": ids, "note": args.note,
                                                    "host": args.host, "model": args.model, "effort": args.effort,
                                                    "quote": args.quote, "lifecycle": args.lifecycle})
    if args.dry_run:
        print("[dry-run] 在账本内存副本上执行，不写真实账本、不调用 agentapi\n")
    d = Dispatcher(cfg, store, sink, roster, security)
    d.sync_roster()
    try:
        chats = [_parse_chat(c) for c in args.chat]
        r = d.new_session(args.key, args.title, args.duty, chats, (args.message_ids or "").split(","), args.note,
                          host=args.host, model=args.model, effort=args.effort, quote=args.quote,
                          lifecycle=args.lifecycle)
    except ValueError as e:
        print(f"new-session 被拒绝: {e}", file=sys.stderr)
        return 2
    except SinkError as e:
        print(f"new-session 失败（没有建会话）: {e}", file=sys.stderr)
        return 1
    if r.get("reopened"):
        print(f"已重开 {r['title']} -> {r['conversation_id']}（{HOST_LABEL.get(r['host'], r['host'])}，topic {r['topic_id']}，"
              f"lifecycle={r.get('lifecycle')}，复用原会话，不消耗新建额度）")
    else:
        print(f"已新开 {r['title']} -> {r['conversation_id']}（{HOST_LABEL.get(r['host'], r['host'])}，topic {r['topic_id']}，"
              f"lifecycle={r.get('lifecycle')}，只记在账本，不写名册）")
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
    cfg = _load_cfg()
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
    cfg = _load_cfg()
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


def _can_redeliver_dispatch(store: Store, d) -> tuple[bool, str]:
    bid = d["batch_id"]
    t = store.get_topic(d["topic_id"])
    cid = (t["conversation_id"] or "") if t is not None else ""
    in_rec = bool(cid and Dispatcher.topic_host(t) == ANTIGRAVITY and transcript_contains(cid, bid))
    if in_rec:
        return False, f"批次 {bid} 在会话记录里已存在，无需重投"
    if d["state"] == "uncertain":
        return True, ""
    if d["state"] in ("sent", "missed", "failed") and cid and Dispatcher.topic_host(t) == ANTIGRAVITY and not in_rec:
        return True, ""
    return False, f"批次 {bid} 状态是 {d['state']}，只有 uncertain 或会话记录里缺失的批次才需要 redeliver"


def cmd_redeliver(args) -> int:
    cfg = _load_cfg()
    store = Store(cfg.db_path)
    batch_arg = (getattr(args, "batch", "") or "").strip()
    topic_arg = (getattr(args, "topic", "") or "").strip()
    if not batch_arg and not topic_arg:
        print("请指定 --batch <批次号> 或 --topic <会话>", file=sys.stderr)
        return 2
    if batch_arg:
        d = store.get_dispatch(batch_arg)
        if d is None:
            print(f"批次 {batch_arg} 不存在", file=sys.stderr)
            return 2
        ok, why = _can_redeliver_dispatch(store, d)
        if not ok:
            print(why, file=sys.stderr)
            return 2
    else:
        tid = _topic_arg(topic_arg)
        rows = store.conn.execute(
            "SELECT * FROM dispatches WHERE topic_id=? ORDER BY sent_at DESC, rowid DESC", (tid,)
        ).fetchall()
        d = next((r for r in rows if _can_redeliver_dispatch(store, r)[0]), None)
        if d is None:
            print(f"{tid} 没有可重投的批次（uncertain 或未进会话记录的批次）", file=sys.stderr)
            return 2
    bid = d["batch_id"]
    ids = json.loads(d["message_ids"] or "[]")
    store.requeue_messages(ids)
    with store.tx() as c:
        c.execute("UPDATE messages SET dispatch_state='pending', batch_id=NULL WHERE batch_id=?", (bid,))
    store.set_dispatch_state(bid, "redelivered", error="manual redeliver")
    t = store.get_topic(d["topic_id"])
    if t is not None and (t["pending_batch_id"] == bid or t["state"] in (sm.RUNNING, sm.ATTENTION, sm.FAILED)):
        store.update_topic(t["topic_id"], state=sm.ACTIVE, pending_batch_id=None, retries=0,
                           attention_at=None, attention_reason=None)
        store.set_meta(duty_reset_key(t["topic_id"]), now_iso())
    print(f"已把批次 {bid} 的 {len(ids)} 条消息放回待推，sheepdog 下一轮重发")
    return 0


def cmd_reply_card(args) -> int:
    """打印代回卡片（Card 2.0）JSON（7.17 / 7.62），用法：
    lark-cli im +messages-send --as user --chat-id oc_x --msg-type interactive --content "$(sheepdog reply-card --summary '...' --text '...')"
    """
    cfg = _load_cfg()
    store = Store(cfg.db_path)
    if viol := outbound.lamb_violation_message(store, "reply-card", topic_id=getattr(args, "topic", "") or ""):
        print(f"reply-card 被拒绝: {viol}", file=sys.stderr)
        return 2
    try:
        text = _body_arg(args.text, args.text_file)
        summary = _body_arg(getattr(args, "summary", None), getattr(args, "summary_file", None))
    except OSError as e:
        print(f"reply-card 被拒绝: 读不了文件: {e}", file=sys.stderr)
        return 2
    try:
        _, _, combined = resolve_summary_and_body(text, summary, require_summary=cfg.notify.require_summary)
        card = build_reply_card(combined, cfg.session.reply_suffix, cfg.session.reply_card_width)
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
    cfg = _load_cfg()
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
    cfg = _load_cfg()
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
    cfg = _load_cfg()
    store = Store(cfg.db_path)
    try:
        close_session(store, args.topic)
    except ValueError as e:
        print(f"close-session 被拒绝: {e}", file=sys.stderr)
        return 2
    print(f"已收掉 {args.topic}，它负责的聊天之后回总线")
    return 0


def cmd_session_reset(args) -> int:
    from . import hook
    cfg = _load_cfg()
    store = Store(cfg.db_path)
    t = store.get_topic(args.topic)
    if not t:
        print("topic 不存在")
        return 2
    # 在岗表（7.20）：在这之前送出、会话没碰过的批次不再算中断 / 失联，排队的信号下一轮照常投
    store.set_meta(duty_reset_key(args.topic), now_iso())
    has_stop_state = hook.count_path(cfg, args.topic).exists()
    hook.clear_stop_state(cfg.state_dir, args.topic)
    card = store.get_card(args.topic)
    was_stuck = card is not None and card["run_state"] in duty.STUCK
    if was_stuck:
        store.upsert_card(args.topic, run_state=None, detail="session-reset 清掉了中断 / 失联判定，下一轮重算",
                          stuck_since=None)
        print("已清掉羊群看板的中断 / 失联判定")
    if not sm.can(t["state"], "reset") or (t["state"] == sm.RUNNING and was_stuck and not has_stop_state):
        print(f"当前状态 {t['state']} 无需复位")
        return 0
    if t["state"] == sm.RUNNING and t["pending_batch_id"]:
        old_bid = t["pending_batch_id"]
        old_d = store.get_dispatch(old_bid)
        if old_d is not None and old_d["state"] == "sent":
            store.set_dispatch_state(old_bid, "missed", error="session-reset 手动复位")
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
    cfg = _load_cfg(); store, tid = Store(cfg.db_path), _topic_arg(args.topic)
    if not _duty_target_ok(store)(tid):
        print(f"work 被拒绝: {tid} 不是在投递的会话（总线、名册 managed、总线新开的）", file=sys.stderr); return 2
    try:
        if args.work_cmd == "set":
            if decision_hub.hub_active(cfg, store):
                setattr(store, "enforce_waiting_owner_ask", True)
            wid, others = duty.set_work(store, tid, args.key, title=args.title, state=args.state,
                                        waiting_on=args.waiting_on, until=args.until, expect=args.expect,
                                        to=_topic_arg(args.to), result=args.result)
        else:
            wid, others = duty.done_work(store, tid, args.key, args.result), []
    except duty.DutyError as e:
        print(f"work 被拒绝: {e}", file=sys.stderr); return 2
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
    cfg = _load_cfg()
    security, store = _security(cfg), Store(cfg.db_path)
    verify = owner_quote_verifier(store, security.quote_max_age_hours, cfg.self_open_id) if args.quote else None
    try:
        mail_id = duty.send_mail(store, cfg, _topic_arg(args.topic), _topic_arg(args.to), args.text, args.key,
                                 args.quote, verify, Playbook.from_config(cfg), _duty_target_ok(store))
    except duty.DutyError as e:
        print(f"tell 被拒绝: {e}", file=sys.stderr); return 2
    touch_wakeup(cfg)
    print(f"留言 #{mail_id} 已存进信箱，{_topic_arg(args.to)} 下一批收到（对方排队、离线都不丢）")
    return 0


def cmd_board(args) -> int:
    cfg = _load_cfg()
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
    cfg = _load_cfg()
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


def _ask_vips(cfg) -> dict[str, str]:
    """ask 用的名册 VIP（7.61）：open_id → 名字，排除主人自己。名册读不了（也没有最后好用的那份）时当作没有 VIP，
    stderr 写一行：拦不了就照旧让它问主人，不能因为名册坏了把找主人这条路也堵上。"""
    try:
        return _load_roster(cfg).vips(exclude=cfg.self_open_id or "")
    except RosterError as e:
        print(f"⚠ 名册读不了（{e}），这次不核 VIP 来源", file=sys.stderr)
        return {}


def cmd_ask(args) -> int:
    """sheepdog ask / report：经机器人找主人。带 --key：同一把钥匙第一次起新话题，之后回到这个话题里。
    ask 关联的来源里有名册 VIP 本人发的消息时必须带 --floor（7.61），否则不发、不写账本，退出码 2。
    7.62：长消息必须带四刀法白话总结（--summary / --summary-file 或正文内 ---more---），原文自动收进 more 默认折叠区。"""
    cfg = _load_cfg()
    store = Store(cfg.db_path)
    kind = args.cmd
    tid = _topic_arg(args.topic)
    try:
        outbound.check_not_lamb(store, kind, topic_id=tid)
    except outbound.OutboundError as e:
        print(f"{kind} 没发: {e}", file=sys.stderr)
        return e.code
    try:
        body = _body_arg(args.text, args.file)
        summary = _body_arg(getattr(args, "summary", None), getattr(args, "summary_file", None))
    except OSError as e:
        print(f"{kind} 没发: 读不了正文文件: {e}", file=sys.stderr)
        return outbound.EXIT_INVALID
    floor = getattr(args, "floor", "") or ""
    floor_type = getattr(args, "floor_type", None)
    ball, fyi = getattr(args, "ball", "") or "", bool(getattr(args, "fyi", False))
    vips = _ask_vips(cfg) if kind == outbound.ASK else {}
    if not cfg.source.enabled:
        if not outbound.target_ok(store, tid):
            print(f"{kind} 没发: {tid} 不是在投递的会话", file=sys.stderr)
            return outbound.EXIT_INVALID
        try:
            if kind == outbound.ASK:
                if getattr(args, "about", None):
                    outbound.ask_sources(cfg, store, tid, args.about)
                outbound.check_vip_floor(cfg, store, tid, getattr(args, "about", None), vips, floor, args.batch, floor_type=floor_type)
            else:
                from . import decision_hub
                decision_hub.enforce_report_contract(cfg, store, tid, args.title, body, key=args.key, ball=ball, fyi=fyi)
        except outbound.OutboundError as e:
            print(f"{kind} 没发: {e}", file=sys.stderr)
            return e.code
        try: _, _, resolved_body = resolve_summary_and_body(body, summary, require_summary=cfg.notify.require_summary, hoist_at=False)
        except ValueError as e:
            print(f"{kind} 没发: {e}", file=sys.stderr)
            return outbound.EXIT_INVALID
        if cfg.notify_path is not None: clear_handled_owner_reactions(store, None, tid)
        return _test_env_write(cfg, "需要你定" if kind == outbound.ASK else "汇报",
                               outbound.compose(cfg, kind, tid, args.title, resolved_body))
    try:
        r = outbound.send_to_owner(cfg, store, _source(cfg), kind, tid, args.title, body, args.key, args.batch,
                                   about=getattr(args, "about", None) or None, track=getattr(args, "track", False),
                                   floor=floor, vips=vips, summary=summary, floor_type=floor_type,
                                   options=getattr(args, "options", "") or "", ball=ball, fyi=fyi)
    except outbound.OutboundError as e:
        print(f"{kind} 没发: {e}", file=sys.stderr)
        return e.code
    for line in outbound.duplicate_text(store, cfg, r.duplicates):
        print(line)
    if hint := outbound.source_hint(r):
        print(hint)
    if r.note:
        print(f"⚠ {r.note}")
    if r.already:
        print(f"之前已经发过同样的内容（防重编号相同，这次没再发）：message_id {r.message_id}")
    elif r.item:
        what = "新开了事项" if r.new_item else "重开了事项" if r.reopened else "发到了话题群里的事项"
        print(f"{what} {r.item}（话题群，钥匙 {args.key}）：message_id {r.message_id}。之后用 --key {args.key} 或 --key {r.item} "
              "的消息都发进群里这个话题；办完用 `sheepdog track done`")
    elif r.new_thread:
        print(f"已发给主人，新起了一个话题（钥匙 {args.key}）：message_id {r.message_id}。之后同一把钥匙的消息都回到这个话题里")
    elif r.root_message_id:
        print(f"已发到钥匙 {args.key} 的话题里：message_id {r.message_id}（话题开头 {r.root_message_id}）")
    else:
        print(f"已发给主人：message_id {r.message_id}")
    return outbound.EXIT_OK


def cmd_resolve(args) -> int:
    """sheepdog resolve（7.36）：会话结案自己名下还开着的「需要你定」，机器人在那条提问的话题里补一句「已结案」。"""
    cfg = _load_cfg()
    store = Store(cfg.db_path)
    tid = _topic_arg(args.topic)
    source = _source(cfg) if cfg.source.enabled else None
    try:
        r = outbound.resolve(cfg, store, source, tid, args.why or "", message_id=args.message_id or "", key=args.key or "",
                             playbook=Playbook.from_config(cfg), quote=getattr(args, "quote", "") or "",
                             superseded_by=getattr(args, "superseded_by", "") or "", vips=_ask_vips(cfg))
    except outbound.OutboundError as e:
        print(f"resolve 没做: {e}", file=sys.stderr)
        return e.code
    if r.already:
        print(f"之前已经结过案，「已结案」那句话也发过了（message_id {r.notice_message_id}），这次什么都没动")
        return outbound.EXIT_OK
    print(f"已结案 {len(r.closed)} 条：{', '.join(r.closed)}")
    if r.notice_message_id:
        print(f"已在话题里补了一句（message_id {r.notice_message_id}）：{r.text}")
    elif r.notice_error:
        if cfg.notify_path is not None:
            FileNotifier(cfg.notify_path, cfg.timezone_offset).notify(r.text, "已结案")
            print(f"{r.notice_error}，那句话写进了 {cfg.notify_path}")
        else:
            print(f"{r.notice_error}：{r.text}")
    return outbound.EXIT_OK


def cmd_pending(args) -> int:
    """sheepdog pending（7.36）：只读列出要主人定的、可能已处理的、各会话在等主人的。不写账本。"""
    from . import pending as pend
    cfg = _load_cfg()
    store = Store(cfg.db_path)
    data = pend.build(cfg, store)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(pend.render(data))
    return 0


def cmd_reply(args) -> int:
    """sheepdog reply：代回。发前只读检查主人有没有新发言；说过就不发（退出码 3），把他的话打印出来。"""
    cfg = _load_cfg()
    store = Store(cfg.db_path)
    tid = _topic_arg(args.topic)
    try:
        outbound.check_not_lamb(store, "reply", topic_id=tid)
    except outbound.OutboundError as e:
        print(f"代回没发: {e}", file=sys.stderr)
        return e.code
    try:
        card_text = _body_arg(args.card_text, args.card_file) if args.card_text is not None or args.card_file else ""
        summary = _body_arg(getattr(args, "summary", None), getattr(args, "summary_file", None))
    except OSError as e:
        print(f"代回没发: 读不了正文文件: {e}", file=sys.stderr)
        return outbound.EXIT_INVALID
    if not cfg.source.enabled:
        if not outbound.target_ok(store, tid):
            print(f"代回没发: {tid} 不是在投递的会话", file=sys.stderr)
            return outbound.EXIT_INVALID
        target = store.get_message((args.to or "").strip())
        try:
            outbound.check_bus_not_forwarded_reply(store, tid, (args.to or "").strip())
            if target is not None:
                outbound.reject_owner_chat(cfg, tid, target["chat_id"])
        except outbound.OutboundError as e:
            print(f"代回没发: {e}", file=sys.stderr)
            return e.code
        try: shown, _ = outbound.reply_body(cfg, args.text or "", card_text, summary=summary)
        except ValueError as e:
            print(f"代回没发: {e}", file=sys.stderr)
            return outbound.EXIT_INVALID
        if cfg.notify_path is not None: clear_handled_owner_reactions(store, None, tid)
        return _test_env_write(cfg, "代回", f"回复 {args.to}：\n{shown}")
    try:
        r = outbound.reply(cfg, store, _source(cfg), tid, args.to, text=args.text or "", card_text=card_text,
                           summary=summary, even_if_owner_replied=args.even_if_owner_replied)
    except outbound.OutboundError as e:
        print(f"代回没发: {e}", file=sys.stderr)
        return e.code
    if r.spoke is not None:
        print(outbound.spoke_text(cfg, r.spoke))
        return outbound.EXIT_OWNER_REPLIED
    who = ""
    if cfg.identity.enabled:
        who = ("（机器人身份，带代回标记）" if r.identity == ident.BOT
               else f"（主人本人身份，带代回标记：{ident.WHY_LABEL.get(r.why, r.why)}）" if r.why else "")
    if r.already:
        print(f"之前已经发过同样的代回（防重编号相同，这次没再发）：message_id {r.message_id}")
    else:
        print(f"已代回{who}：message_id {r.message_id}")
    return outbound.EXIT_OK


def cmd_thread(args) -> int:
    """sheepdog thread：只读列出一个话题的全部消息（开头那条从账本 / sends / mget 取，回复从账本和话题列表取）。"""
    cfg = _load_cfg()
    store = Store(cfg.db_path)
    source = _source(cfg)
    try:
        v = outbound.view_thread(cfg, store, source, _topic_arg(args.topic), args.key or "", args.thread or "")
    except outbound.OutboundError as e:
        print(f"thread: {e}", file=sys.stderr)
        return e.code
    print(outbound.render_thread(cfg, v))
    return outbound.EXIT_OK


def cmd_track(args) -> int:
    """sheepdog track（7.51）：会话结束一件话题群里的事项（done）、给事项登记别名（alias）、只读列出（list）。"""
    from . import topicgroup as tg
    cfg = _load_cfg()
    store = Store(cfg.db_path)
    if args.track == "list":
        lines = tg.list_lines(cfg, store, include_all=args.all)
        print("\n".join(lines) if lines else "（话题群里没有跟踪中的事项）")
        return 0
    tid = _topic_arg(args.topic)
    if not outbound.target_ok(store, tid):
        print(f"track {args.track} 没做: {tid} 不是在投递的会话", file=sys.stderr)
        return outbound.EXIT_INVALID
    key = " ".join((args.key if args.track == "done" else args.item).split())
    item = tg.find_by_key(store, key)
    if item is None or item["state"] == tg.REMOVED:
        print(f"track {args.track} 没做: 话题群里没有钥匙 {key!r} 的事项（`sheepdog track list` 看）", file=sys.stderr)
        return outbound.EXIT_INVALID
    no = tg.label(item["item_no"])
    if args.track == "done":
        why = " ".join((args.why or "").split())
        if not why:
            print("track done 没做: --why 不能为空：一句话写依据", file=sys.stderr)
            return outbound.EXIT_INVALID
        if item["state"] != tg.ACTIVE:
            print(f"{no} 已经{tg.STATUS_LABEL.get(item['state'], item['state'])}，这次没动")
            return outbound.EXIT_OK
        tg.close(store, item, tid, why)
        print(f"已把 {no}「{item['title']}」标成办完了；牧羊犬下一轮在话题里补一行「✅ 办完了」并更新索引卡。"
              f"同一把钥匙再发 ask / report 会把它重开")
        return outbound.EXIT_OK
    alias = " ".join((args.key or "").split())
    other = tg.find_by_key(store, alias)
    if other is not None and other["item_no"] != item["item_no"] and other["state"] in (tg.ACTIVE, tg.DONE):
        print(f"track alias 没做: 钥匙 {alias!r} 已经是 {tg.label(other['item_no'])} 的钥匙", file=sys.stderr)
        return outbound.EXIT_INVALID
    store.add_item_key(item["item_no"], alias, tg.track_norm(alias), "alias", tid)
    print(f"已把 {alias} 登记成 {no} 的别名：之后 --key {alias} 的 ask / report 发进群里 {no} 的话题")
    return outbound.EXIT_OK


def cmd_topic_group(args) -> int:
    """sheepdog topic-group（7.51 / 7.74）：建群（create）、看状态（status）、从私聊迁入（move）。"""
    import shlex as _sh
    cfg = _load_cfg()
    if args.tg == "status":
        print(topic_group_report(cfg, Store(cfg.db_path)))
        return 0
    if args.tg == "move":
        from . import topicgroup as tg
        store, source = Store(cfg.db_path), _source(cfg)
        tid = _topic_arg(args.topic) if getattr(args, "topic", "") else ""
        try:
            it = tg.move_from_dm(cfg, store, source, args.key, topic_id=tid, title=getattr(args, "title", "") or "")
        except Exception as e:
            print(f"topic-group move 失败: {e}", file=sys.stderr)
            return 2
        print(f"已迁入话题群：{tg.label(it['item_no'])}「{it['title']}」（root={it['root_message_id'] or '-'}）")
        return 0
    if not (me := cfg.self_open_id):
        print("topic-group create 没做: 配置里没有 self_open_id，不知道群主是谁", file=sys.stderr)
        return 2
    create = ["im", "+chat-create", "--chat-mode", "topic", "--type", "private", "--name", args.name.strip(), "--owner", me,
              "--users", me, "--set-bot-manager", "--format", "json"]
    perm = {"add_member_permission": "only_owner", "share_card_permission": "not_allowed"}
    update = ["im", "chats", "update", "--params", '{"chat_id":"<CHAT_ID>"}', "--data", json.dumps(perm), "--yes"]
    if args.dry_run:
        print("# 1. 建话题群（机器人身份；群主 = 主人，机器人当管理员）\n" + " ".join(_sh.quote(x) for x in ["lark-cli", *create, "--as", "bot"]))
        print("# 2. 只有群主和管理员能拉人\n" + " ".join(_sh.quote(x) for x in ["lark-cli", *update, "--as", "bot"]))
        print('# 3. 把第 1 步返回的 chat_id 写进配置：\n[topic_group]\nenabled = true\nchat_id = "<CHAT_ID>"')
        return 0
    src = LarkCliSource(tz=cfg.timezone_offset)
    try:
        data = src._run_once(create, "bot")
    except Exception as e:
        print(f"建群失败: {e}", file=sys.stderr)
        return 1
    if not (chat := src._find(data, "chat_id") or ""):
        print(f"建群没返回 chat_id: {json.dumps(data, ensure_ascii=False)[:300]}", file=sys.stderr)
        return 1
    print(f"已建话题群：{chat}")
    try:
        src._run_once(["im", "chats", "update", "--params", json.dumps({"chat_id": chat}), "--data", json.dumps(perm), "--yes"], "bot")
        print("已设成只有群主和管理员能拉人")
    except Exception as e:
        print(f"⚠ 设「只有群主和管理员能拉人」失败（话题群可能不支持 im chats update，在群设置里手动改）：{e}")
    print(f'把这两行写进配置 [topic_group]：\nenabled = true\nchat_id = "{chat}"')
    return 0


def _lamb_prepare(cfg, store: Store, args) -> int:
    tid, title = _topic_arg(args.topic), (args.title or "").strip()
    if store.get_topic(tid) is None or not title:
        print(f"lamb prepare 被拒绝: {'母羊会话 ' + tid + ' 不存在' if store.get_topic(tid) is None else '--title 不能为空'}", file=sys.stderr)
        return 2
    try:
        spec_body = (args.spec if getattr(args, "spec", None) else
                     (sys.stdin.read() if args.spec_file == "-" else Path(args.spec_file).read_text(encoding="utf-8")) if getattr(args, "spec_file", None) else
                     sys.stdin.read() if getattr(args, "spec_stdin", False) else title)
        lid = register_lamb(store, tid, title, kind=args.kind or "subagent", lamb_id=args.lamb_id or None)
    except (OSError, ValueError) as e:
        print(f"lamb prepare 被拒绝: {e}", file=sys.stderr)
        return 2
    tasks_dir = cfg.state_dir / "lamb_tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task_file = tasks_dir / f"{lid}.md"
    task_file.write_text(
        f"# 羔羊任务单 {lid}（母羊：{tid}）\n- 任务标题：{title}\n"
        f"- 铁律：你是母羊 {tid} 派出的后台执行/调研子代理（羔羊 {lid}）。严禁调用 sheepdog reply / ask / report / receipt；"
        f"完成任务后直接将结论与证据回报母羊，并在回报开头或末尾原样带上标签 [lamb:{lid}]。\n\n"
        f"## 任务详情与执行步骤\n{spec_body.strip()}\n", encoding="utf-8")
    store.update_lamb(lid, log_path=str(task_file))
    subagent_prompt = (f"请读取本机任务单文件 {task_file} ，严格按其中步骤完成调研或执行；"
                       f"禁止调用 sheepdog 外发/回执命令，完成后将结论与证据回报母羊并附上 [lamb:{lid}]。")
    if getattr(args, "json", False):
        print(json.dumps({"lamb_id": lid, "topic_id": tid, "task_file": str(task_file),
                          "subagent_prompt": subagent_prompt, "waiting_anchor": f"lamb:{lid}"}, ensure_ascii=False))
    else:
        print(f"已准备羔羊任务单 {lid}（母羊 {tid}）\n- task_file: {task_file}\n- subagent_prompt: {subagent_prompt}\n"
              f"- waiting 回执示例: sheepdog receipt --topic {tid} --batch <BATCH_ID> "
              f"--json '{{\"status\":\"waiting\",\"summary\":\"已派羔羊 {lid}\",\"anchors\":[\"lamb:{lid}\"]}}'")
    return 0


def cmd_lamb(args) -> int:
    """sheepdog lamb（7.63 / 7.72）：母羊准备/登记/派生/收尾/列出/取消羔羊（Subagent 或后台 CLI 子进程）。"""
    cfg, store, sub_cmd = _load_cfg(), Store(_load_cfg().db_path), args.lamb_cmd
    if sub_cmd == "prepare":
        return _lamb_prepare(cfg, store, args)
    if sub_cmd == "register":
        tid = _topic_arg(args.topic)
        try:
            lid = register_lamb(store, tid, args.title, kind=args.kind, conversation_id=args.cid or None, lamb_id=args.lamb_id or None)
        except ValueError as e:
            print(f"lamb register 被拒绝: {e}", file=sys.stderr)
            return 2
        lb = store.get_lamb(lid)
        cid_text = f"，cid={lb['conversation_id']}" if lb and lb["conversation_id"] else ""
        print(f"已登记羔羊 {lid}（母羊 {tid}，[{args.kind}] {args.title}{cid_text}）")
        return 0
    if sub_cmd == "spawn":
        tid, cmd_str = _topic_arg(args.topic), (args.command or "").strip()
        if store.get_topic(tid) is None or not cmd_str:
            print(f"lamb spawn 被拒绝: {'母羊会话 ' + tid + ' 不存在' if store.get_topic(tid) is None else '--cmd 不能为空'}", file=sys.stderr)
            return 2
        raw_lid = (args.lamb_id or "").strip() or f"lamb_{uuid.uuid4().hex[:8]}"
        lid = raw_lid if raw_lid.startswith("lamb_") else f"lamb_{raw_lid}"
        cfg.lambs_dir.mkdir(parents=True, exist_ok=True)
        log_path = cfg.lambs_dir / f"{lid}.log"
        env = {**os.environ, "SHEEPDOG_LAMB_ID": lid}
        try:
            with log_path.open("ab") as lf:
                proc = subprocess.Popen(cmd_str, shell=True, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            try:
                proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            register_lamb(store, tid, args.title, kind="cli", lamb_id=lid, pid=proc.pid, log_path=str(log_path))
        except (OSError, ValueError) as e:
            print(f"lamb spawn 失败: {e}", file=sys.stderr)
            return 2
        print(f"已启动羔羊 {lid}（母羊 {tid}，[cli] {args.title}，pid={proc.pid}，日志 {log_path}）")
        return 0
    if sub_cmd in ("done", "cancel"):
        try:
            r = complete_lamb(store, args.lamb_id, summary=args.summary, state=args.status) if sub_cmd == "done" else cancel_lamb(store, args.lamb_id, reason=args.reason)
        except ValueError as e:
            print(f"lamb {sub_cmd} 被拒绝: {e}", file=sys.stderr)
            return 2
        print(f"羔羊 {r['lamb_id']} 已标为 {r['state']}（母羊 {r['parent_topic_id']}）：{r['summary']}" if sub_cmd == "done"
              else f"已取消羔羊 {r['lamb_id']}（母羊 {r['parent_topic_id']}）：{r['summary']}")
        return 0
    if sub_cmd == "list":
        tid = _topic_arg(args.topic) if getattr(args, "topic", "") else None
        rows = [dict(r) for r in store.list_lambs(parent_topic_id=tid, include_finished=bool(args.all))]
        if getattr(args, "json", False):
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("没有运行中的羔羊" if not args.all else "没有羔羊记录")
            return 0
        for lb in rows:
            extra = [f"{k}={lb[col]}" for k, col in (("cid", "conversation_id"), ("pid", "pid"), ("log", "log_path"), ("结果", "result_summary")) if lb.get(col)]
            tail = (" · " + " · ".join(extra)) if extra else ""
            print(f"- {lb['lamb_id']} [{lb['kind']}] [{lb['state']}] topic={lb['parent_topic_id']} title={lb['task_title']}{tail}")
        return 0
    return 2


def cmd_hook_stop(args) -> int:
    from . import hook  # 只读账本、不导入别的；任何异常都不输出、退出码 0
    return hook.stop_main(sys.stdin.buffer, sys.stdout)


def cmd_roster(args) -> int:
    try:
        cfg = _load_cfg()
    except Exception:
        cfg = None
    return run_roster_cli(args, cfg)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sheepdog", description="工作助理：把 IM 上的工作分给负责的 Agent session")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-V", "--version", action="store_true", help="显示版本信息（含常驻进程内存版本对比）")
    sub = p.add_subparsers(dest="cmd", required=False)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    sp = sub.add_parser("version", help="显示 CLI 代码版本与常驻进程内存版本")
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.add_argument("--check-remote", action="store_true", help="实时探测 GitHub Release 与 Beta Tag")
    sp.set_defaults(func=cmd_version)
    sp = sub.add_parser("upgrade", help="拉取最新代码并通知常驻进程原地热重启（os.execv，保留 sidecar 环境）")
    sp.add_argument("--no-pull", action="store_true", help="不执行 git pull，仅通知常驻进程原地重启")
    sp.add_argument("--dry-run", action="store_true", help="只打印将要执行的升级动作，不拉代码、不入队")
    sp.add_argument("--beta", action="store_true", help="升级至最新 Beta Tag（tag > release）")
    sp.add_argument("--stable", action="store_true", help="升级至最新 GitHub Release 稳定版（默认）")
    sp.add_argument("--rollback", action="store_true", help="回滚至升级前的上一版本并通知常驻进程原地热重启")
    sp.add_argument("--to", default="", help="回滚目标 commit SHA 或 tag（配合 --rollback 使用，默认取上一版本）")
    sp.add_argument("--notes", default="", help="本次更新说明（展示在飞书升级完成通知卡片中）")
    sp.set_defaults(func=cmd_upgrade)
    sp = sub.add_parser("observe", help="深度水位巡检（Watchdog 2.0）")
    obs.add_observe_arguments(sp)
    sp.set_defaults(func=cmd_observe)
    sp = sub.add_parser("poll")
    sp.add_argument("--dry-run", action="store_true", help="不真正投递，只打印")
    sp.add_argument("--no-dispatch", action="store_true", help="只拉取与路由，不投递")
    sp.set_defaults(func=cmd_poll)
    sp = sub.add_parser("run")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--interval", type=int, default=0)
    sp.set_defaults(func=cmd_run)
    sp = sub.add_parser("inbox")
    sp.add_argument("--chat", default=""); sp.add_argument("--limit", type=int, default=50); sp.set_defaults(func=cmd_inbox)
    sp = sub.add_parser("inbox-clear")
    sp.add_argument("--chat", default=""); sp.add_argument("--all", action="store_true"); sp.set_defaults(func=cmd_inbox_clear)
    sp = sub.add_parser("init")
    sp.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容，不写账本"); sp.set_defaults(func=cmd_init)
    sub.add_parser("sessions").set_defaults(func=cmd_sessions)
    sp = sub.add_parser("forward")
    sp.add_argument("--topic", required=True)
    sp.add_argument("--message-ids", default="", help="逗号分隔；可以不带，只转达 --quote / --note")
    sp.add_argument("--quote", default="", help="主人原话，投递时标成「主人原话（经总线转达）」")
    sp.add_argument("--note", default="", help="总线备注，投递时标成「总线备注」")
    sp.add_argument("--answers", default="", help="7.53：主人这句是在回答哪条「需要你定」（提问的 om_，只能一个）：给了才关那一条；必须是 --topic 那个会话名下、还开着的")
    sp.set_defaults(func=cmd_forward)
    sp = sub.add_parser("spawn")
    sp.add_argument("--key", required=True); sp.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容，不写账本"); sp.set_defaults(func=cmd_spawn)
    sp = sub.add_parser("watch")
    sp.add_argument("--topic", required=True); sp.add_argument("--person", required=True, help="对方 open_id")
    sp.add_argument("--chat", default="", help="只认这个聊天里的回复；不填 = 任意聊天"); sp.add_argument("--note", default="", help="在等什么")
    sp.set_defaults(func=cmd_watch)
    sp = sub.add_parser("watches")
    sp.add_argument("--all", action="store_true", help="包括已结束的"); sp.set_defaults(func=cmd_watches)
    sp = sub.add_parser("security-log")
    sp.add_argument("--since", default="24h", help="回看时长：30m / 24h / 7d"); sp.set_defaults(func=cmd_security_log)
    sp = sub.add_parser("escalations")
    sp.add_argument("--all", action="store_true", help="包括已答和已关闭的"); sp.add_argument("--topic", default="", help="只列这个会话（tp_x）名下的")
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
    sp.add_argument("--quote", default="", help="主人同意开这个会话的原话（codex / claude 且 ask_owner_before_new 时必填，核对同 forward）")
    sp.add_argument("--lifecycle", choices=("standing", "oneshot"), default=None, help="生命周期（7.58）：standing（回执 done 不关）| oneshot（回执 done 自动收掉）；不传按标题前缀推断")
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
    sp.add_argument("--batch", default="", help="按批次号重投")
    sp.add_argument("--topic", default="", help="按会话重投最近一批 uncertain 或未进会话记录的批次")
    sp.set_defaults(func=cmd_redeliver)
    sp = sub.add_parser("reply-card")
    sp.add_argument("--topic", default="", help="可选：调用方会话（tp_x）")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="原有完整回复（长消息自动收进 more 默认折叠区）")
    g.add_argument("--text-file", help="正文文件；- 表示标准输入")
    sg = sp.add_mutually_exclusive_group()
    sg.add_argument("--summary", default="", help="当面日常大白话回话（7.70：展示在卡片首屏，≤3 行、≤100 字，只答问题、不括技术亮点、不露修订号/SHA/路径/分点列表）")
    sg.add_argument("--summary-file", default="", help="白话总结文件")
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
    w.add_argument("--state", default=duty.DOING, help="doing | waiting_other | waiting_owner | done | handed_off（也认中文：进行中 / 等别人 / 等主人 / 已完成 / 已转交）")
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
        g.add_argument("--text", help="正文（长消息原文不需要改，配合 --summary 自动收进 more 折叠区）")
        g.add_argument("--file", help="正文文件；- 表示标准输入")
        sg = sp.add_mutually_exclusive_group()
        sg.add_argument("--summary", default="", help="卡片首屏白话总结（7.62/7.70：ask/report ≤6 行、≤220 字、不含代码块；原文自动收进 more 折叠区）")
        sg.add_argument("--summary-file", default="", help="白话总结文件")
        sp.add_argument("--key", default="", help="钥匙（和羊群看板的活同一套）：同一把钥匙第一次在主聊天里起一条，之后回到它的话题里")
        sp.add_argument("--batch", default="", help="批次号（可选，一般不用传）：参与防重编号。不传时取这个会话当前在跑的批次，没有就取最近一批")
        sp.add_argument("--track", action="store_true", help="长期跟踪（7.51）：第一次用这把 --key 时带上，这件事放进话题群（一件事一个话题）；之后同一把钥匙不用再带，自动发进群里那个话题")
        if name == "ask":
            sp.add_argument("--about", action="append", default=[], help="为哪条消息问的（om_，可多次或逗号分隔）；不给 = 这个会话最近一批送到的全部消息。主人之后在那个聊天里说过话，sheepdog 会提醒你核实")
            sp.add_argument("--floor-type", default=None, help="碰到哪类底线（7.65，必填枚举：credentials | destructive | vip_conflict）：为名册 VIP 本人发的消息找主人（或使用 --floor）时必须与 --floor 同时写")
            sp.add_argument("--floor", default="", help="碰到哪条底线（7.61 / 7.65）：为名册 VIP 本人发的消息找主人时必须与 --floor-type 一起写，不写就不发（退出码 2）。VIP 请求默认不等主人批：合理就办，不合理给方案，坚持就 report 后配合")
            sp.add_argument("--options", default="", help="显式指定卡片按钮选项（如 'A:同意发布|B:暂缓'、'auto' 或 'none'）")
        else:
            sp.add_argument("--ball", default="", help="球权归属（done=已办结 | doing=继续推进 | other=等第三方；球在主人脚下必须用 ask）")
            sp.add_argument("--fyi", action="store_true", help="纯知会备案（含选项示例但无需主人拍板时显式声明）")
        sp.set_defaults(func=cmd_ask)
    sp = sub.add_parser("resolve", help="结案自己名下还开着的「需要你定」（写依据）；机器人在话题里补一句「已结案」")
    sp.add_argument("--topic", required=True, help="你自己的会话（tp_x）")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--message-id", help="那条提问的消息号（om_）")
    g.add_argument("--key", help="钥匙：结掉这个话题里所有还开着的提问")
    sp.add_argument("--why", required=True, help="依据，一句话（主人在哪说了什么 / 你核实到什么）")
    sp.add_argument("--quote", default="", help="主人在别处拍板的原话或消息号（om_...）")
    sp.add_argument("--superseded-by", default="", help="替代本提问的新 ask 消息号（om_...）")
    sp.set_defaults(func=cmd_resolve)
    sp = sub.add_parser("pending", help="只读：待办清单（要主人定的、可能已处理的、各会话在等主人的）")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_pending)
    sp = sub.add_parser("reply", help="代回：以主人身份回复一条消息（卡片带代回标记）；发前先查主人有没有新发言")
    sp.add_argument("--topic", required=True, help="你自己的会话（tp_x）")
    sp.add_argument("--to", required=True, help="要回复的那条消息（om_）")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--card-text", help="卡片正文（长消息原文不需要改，配合 --summary 自动收进 more 折叠区）")
    g.add_argument("--card-file", help="卡片正文文件，- 表示标准输入（同 reply-card --text-file）")
    g.add_argument("--text", help="代回正文（同 --card-text，一律发成卡片）")
    sg = sp.add_mutually_exclusive_group()
    sg.add_argument("--summary", default="", help="当面日常大白话回话（7.70：展示在卡片首屏，≤3 行、≤100 字，只答问题、不括技术亮点、不露修订号/SHA/路径/分点列表）")
    sg.add_argument("--summary-file", default="", help="白话总结文件")
    sp.add_argument("--even-if-owner-replied", action="store_true", help="主人已经回过也照样发（跳过发前检查）；默认主人回过就不发，退出码 3")
    sp.set_defaults(func=cmd_reply)
    sp = sub.add_parser("thread", help="只读：列出一个话题的全部消息")
    sp.add_argument("--topic", required=True, help="你自己的会话（tp_x）")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--key", help="钥匙（ask / report 带这把 --key 发过）")
    g.add_argument("--thread", help="话题编号 omt_…（或开头那条的 om_）")
    sp.set_defaults(func=cmd_thread)
    sp = sub.add_parser("track", help="话题群里的事项（7.51）：办完、登记别名、列出")
    tsub = sp.add_subparsers(dest="track", required=True)
    t = tsub.add_parser("done", help="这件事办完了（只认明确的命令）；牧羊犬在话题里补一行并更新索引卡")
    t.add_argument("--topic", required=True, help="你自己的会话（tp_x）")
    t.add_argument("--key", required=True, help="钥匙或事项编号（T-12）")
    t.add_argument("--why", required=True, help="依据，一句话")
    t = tsub.add_parser("alias", help="给事项登记别名：之后这把钥匙的 ask / report 也发进这件事的话题")
    t.add_argument("--topic", required=True, help="你自己的会话（tp_x）")
    t.add_argument("--item", required=True, help="事项编号（T-12）或它已有的钥匙")
    t.add_argument("--key", required=True, help="要登记的钥匙")
    t = tsub.add_parser("list", help="只读：话题群里跟踪中的事项和状态")
    t.add_argument("--all", action="store_true", help="含已归档、已移出的")
    sp.set_defaults(func=cmd_track)
    sp = sub.add_parser("topic-group", help="话题群（7.51 / 7.74）：建群（create）、看状态（status）、从私聊迁入（move）")
    gsub = sp.add_subparsers(dest="tg", required=True)
    g = gsub.add_parser("create", help="建话题群：群主是主人、机器人当管理员、只有群主和管理员能拉人")
    g.add_argument("--name", default="🐕 牧羊犬 · 长期跟踪", help="群名")
    g.add_argument("--dry-run", action="store_true", help="只打印要调的 lark-cli 命令，不建群")
    gsub.add_parser("status", help="只读：配没配、开没开、是不是退回私聊了")
    gm = gsub.add_parser("move", help="把私聊长线事项一键迁移到话题群（7.74 REG-022）")
    gm.add_argument("--key", required=True, help="要迁移的私聊话题钥匙")
    gm.add_argument("--topic", default="", help="可选：负责会话（tp_x）")
    gm.add_argument("--title", default="", help="可选：指定事项标题")
    sp.set_defaults(func=cmd_topic_group)
    sp = sub.add_parser("lamb", help="羔羊管理（7.63 / 7.72）：准备任务单、登记 subagent、派生后台 CLI 子进程、标记完工、查看、取消")
    lsub = sp.add_subparsers(dest="lamb_cmd", required=True)
    lp = lsub.add_parser("prepare", help="零敏感文件指针派单（7.72 REG-014）：落盘任务单并预登记羔羊")
    lp.add_argument("--topic", required=True, help="母羊会话（tp_x）")
    lp.add_argument("--title", required=True, help="一句话任务标题")
    lp.add_argument("--spec", default="", help="详细任务与命令正文")
    lp.add_argument("--spec-file", default="", help="详细任务文件路径（- 表示 stdin）")
    lp.add_argument("--spec-stdin", action="store_true", help="从标准输入读取详细任务")
    lp.add_argument("--kind", choices=("subagent", "cli"), default="subagent", help="羔羊类型（默认 subagent）")
    lp.add_argument("--lamb-id", "--id", dest="lamb_id", default="", help="指定 lamb_id（不填自动生成）")
    lp.add_argument("--json", action="store_true", help="输出 JSON")
    lr = lsub.add_parser("register", help="登记一只 subagent 羔羊")
    lr.add_argument("--topic", required=True, help="母羊会话（tp_x）")
    lr.add_argument("--title", required=True, help="羔羊任务标题")
    lr.add_argument("--cid", "--conversation-id", dest="cid", default="", help="subagent 的 conversation_id")
    lr.add_argument("--lamb-id", "--id", dest="lamb_id", default="", help="指定 lamb_id（不填自动生成）")
    lr.add_argument("--kind", choices=("subagent", "cli"), default="subagent", help="羔羊类型（默认 subagent）")
    ls = lsub.add_parser("spawn", help="后台起 CLI 羔羊子进程")
    ls.add_argument("--topic", required=True, help="母羊会话（tp_x）")
    ls.add_argument("--title", required=True, help="羔羊任务标题")
    ls.add_argument("--cmd", "--command", dest="command", required=True, help="后台执行的 shell 命令")
    ls.add_argument("--lamb-id", "--id", dest="lamb_id", default="", help="指定 lamb_id（不填自动生成）")
    ld = lsub.add_parser("done", help="标记羔羊完工或失败")
    ld.add_argument("--lamb-id", "--id", dest="lamb_id", required=True, help="羔羊 ID")
    ld.add_argument("--summary", "--result", dest="summary", required=True, help="结论摘要")
    ld.add_argument("--status", "--state", dest="status", choices=("done", "failed", "timeout"), default="done")
    ld.add_argument("--topic", default="", help="可选：母羊会话（tp_x）")
    ll = lsub.add_parser("list", help="列出运行中或全部羔羊")
    ll.add_argument("--topic", default="", help="按母羊会话（tp_x）过滤")
    ll.add_argument("--all", action="store_true", help="包含已结束的羔羊")
    ll.add_argument("--json", action="store_true", help="输出 JSON")
    lc = lsub.add_parser("cancel", help="取消并终止运行中的羔羊")
    lc.add_argument("--lamb-id", "--id", dest="lamb_id", required=True, help="羔羊 ID")
    lc.add_argument("--reason", "--summary", dest="reason", default="", help="取消原因")
    lc.add_argument("--topic", default="", help="可选：母羊会话（tp_x）")
    sp.set_defaults(func=cmd_lamb)
    sp = sub.add_parser("hook", help="Antigravity 生命周期 hook（由 hooks.json 调用，不是给人敲的）")
    hsub = sp.add_subparsers(dest="hook", required=True)
    hsub.add_parser("stop", help="Stop：这一轮要结束时查这批交没交回执").set_defaults(func=cmd_hook_stop)
    add_roster_arguments(sub)
    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    if getattr(args, "version", False):
        return cmd_version(args)
    if not getattr(args, "cmd", None):
        p.error("the following arguments are required: cmd")
    if args.cmd == "roster":
        return cmd_roster(args)
    global _FALLBACK
    _FALLBACK = args.func not in (cmd_run, cmd_init, cmd_doctor, cmd_hook_stop, cmd_observe)
    _FALLBACK_NOTED.clear()
    try:
        return args.func(args)
    except _Abort:
        return 2
    except ConfigError as e:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} ERROR 配置无效: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
