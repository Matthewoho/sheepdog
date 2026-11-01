"""牧羊犬自身可观测与告警群（规格 7.81）：独立群自动建群、绑定持久化、通知路由与看门狗卡片发送。"""
from __future__ import annotations
import hashlib, inspect, json, logging, re, sqlite3, subprocess, sys
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("sheepdog")
ALERT_GROUP_NAME = "🐕 牧羊犬 · 自身可观测与告警"
META_ALERT_CHAT_ID, META_ALERT_PIN_MID = "alert_chat_id", "alert_pin_message_id"
SYSTEM_ALERT_KINDS = frozenset({
    "羊群看板通知", "attention 通知", "恢复通知", "投递不确定通知", "投递失败通知", "投递恢复通知",
    "回音账本·没送到", "回音账本·排队", "插话没回音提醒", "回音账本·没回音", "登录提醒", "写锁提醒",
    "危险命令报警", "话题群通知", "新版提醒", "升级完成",
})
ALERT_WELCOME_TEXT = (
    "🐕 汇报 · 自身可观测与告警群已就绪 [tp_bus]\n"
    "本群专用于承载牧羊犬自身运行态可观测通知（体检看门狗、会话熔断/恢复、投递异常、回音账本超时、版本升级与兜底回执），"
    "已与主业务群和机器人私聊 100% 物理隔离。"
)
NOTICE_BACKOFF_MINUTES = (1, 2, 4, 8)
NOTICE_BACKOFF_CAP, NOTICE_GIVE_UP_HOURS, NOTICE_RESEND_SAFE_MINUTES = 10, 2, 50

def _now() -> datetime:
    eng = sys.modules.get("sheepdog.engine")
    return eng._now() if eng is not None and hasattr(eng, "_now") else datetime.now().astimezone()

def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts and isinstance(ts, str) else None
    except ValueError:
        return None

def _accepts(fn, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False

def _notice_backoff(attempts: int) -> timedelta:
    return timedelta(minutes=NOTICE_BACKOFF_MINUTES[attempts - 1] if 1 <= attempts <= len(NOTICE_BACKOFF_MINUTES) else NOTICE_BACKOFF_CAP)

def _display_time(dt: datetime | None, offset: str = "+08:00") -> str:
    eng = sys.modules.get("sheepdog.engine")
    return eng.display_time(dt, offset) if eng is not None and hasattr(eng, "display_time") else (dt.strftime("%m-%d %H:%M") if dt else "")

def _extract_chat_id(data) -> str:
    if isinstance(data, str):
        return data.strip() if data.strip().startswith("oc_") else ""
    cur = data
    for _ in range(3):
        if not isinstance(cur, dict):
            break
        if isinstance(cid := cur.get("chat_id"), str) and cid.strip().startswith("oc_"):
            return cid.strip()
        cur = cur.get("data")
    return ""

def _persist_alert_chat_to_config(config_path: Path | None, chat_id: str) -> None:
    if not config_path or not chat_id or not Path(config_path).exists():
        return
    p, line = Path(config_path), f'alert_chat_id = "{chat_id}"'
    try:
        text = p.read_text(encoding="utf-8")
        if re.search(r"(?m)^\s*alert_chat_id\s*=", text):
            new_text = re.sub(r"(?m)^\s*alert_chat_id\s*=.*$", line, text, count=1)
        elif re.search(r"(?m)^\[notify\]\s*$", text):
            new_text = re.sub(r"(?m)^(\[notify\]\s*)$", rf"\1\n{line}", text, count=1)
        else:
            new_text = text.rstrip("\n") + f"\n\n[notify]\n{line}\n"
        if new_text != text:
            p.write_text(new_text, encoding="utf-8")
    except OSError:
        pass

def reply_belongs_to_chat(store, reply_to: str, target_chat: str) -> bool:
    if not reply_to or not target_chat:
        return bool(reply_to and not target_chat)
    try:
        m = store.get_message(reply_to) if store is not None and hasattr(store, "get_message") else None
        return str(m["chat_id"] or "").strip() == target_chat if m is not None else False
    except Exception:
        return False

def resolve_alert_chat_id(cfg=None, store=None, db_path=None) -> str:
    if cfg is not None and not hasattr(cfg, "notify") and hasattr(cfg, "get_meta") and store is None:
        cfg, store = None, cfg
    if (cfg_chat := str(getattr(getattr(cfg, "notify", None), "alert_chat_id", "") or "").strip()).startswith("oc_"):
        try:
            if store is not None and hasattr(store, "get_meta") and hasattr(store, "set_meta") and store.get_meta(META_ALERT_CHAT_ID) != cfg_chat:
                store.set_meta(META_ALERT_CHAT_ID, cfg_chat)
        except Exception:
            pass
        return cfg_chat
    try:
        if store is not None and hasattr(store, "get_meta") and (st_chat := str(store.get_meta(META_ALERT_CHAT_ID) or "").strip()).startswith("oc_"):
            if getattr(cfg, "notify", None) is not None:
                cfg.notify.alert_chat_id = st_chat
            return st_chat
    except Exception:
        pass
    p = Path(db_path) if db_path else (getattr(cfg, "db_path", None) if cfg is not None else None)
    if p is not None and Path(p).exists():
        try:
            with sqlite3.connect(f"file:{p}?mode=ro", uri=True) as conn:
                if (row := conn.execute("SELECT value FROM meta WHERE key = ?", (META_ALERT_CHAT_ID,)).fetchone()) and row[0] and (db_chat := str(row[0]).strip()).startswith("oc_"):
                    if getattr(cfg, "notify", None) is not None:
                        cfg.notify.alert_chat_id = db_chat
                    return db_chat
        except Exception:
            pass
    return ""

def ensure_alert_group(cfg, store=None, source=None) -> str:
    if chat_id := resolve_alert_chat_id(cfg, store):
        return chat_id
    if cfg is None or source is None:
        return ""
    if owner_chat := str(getattr(getattr(cfg, "threads", None), "owner_chat_id", "") or "").strip():
        return owner_chat
    if not getattr(getattr(cfg, "source", None), "enabled", False) or not (me := getattr(cfg, "self_open_id", "")):
        return ""
    if ("create_alert_chat" in getattr(source, "__dict__", {}) or hasattr(type(source), "create_alert_chat")) and callable(getattr(source, "create_alert_chat", None)):
        try:
            chat_id = _extract_chat_id(source.create_alert_chat(name=ALERT_GROUP_NAME, owner_id=me))
        except TypeError:
            chat_id = _extract_chat_id(source.create_alert_chat(ALERT_GROUP_NAME, me))
        except Exception as e:
            log.warning("自动建告警群失败（create_alert_chat）: %s", e)
    if not chat_id and callable(getattr(source, "_run_once", None)):
        try:
            cmd = ["im", "+chat-create", "--type", "private", "--name", ALERT_GROUP_NAME, "--owner", me, "--users", me, "--set-bot-manager", "--format", "json"]
            chat_id = _extract_chat_id(source._run_once(cmd, "bot"))
        except Exception as e:
            log.warning("自动建告警群失败（+chat-create）: %s", e)
            return ""
    if not chat_id:
        return ""
    if callable(getattr(source, "_run_once", None)):
        perm = {"add_member_permission": "only_owner", "share_card_permission": "not_allowed"}
        try:
            source._run_once(["im", "chats", "update", "--params", json.dumps({"chat_id": chat_id}), "--data", json.dumps(perm), "--yes"], "bot")
        except Exception:
            pass
    try:
        if store is not None and hasattr(store, "set_meta"):
            store.set_meta(META_ALERT_CHAT_ID, chat_id)
    except Exception:
        pass
    if getattr(cfg, "notify", None) is not None:
        cfg.notify.alert_chat_id = chat_id
    _persist_alert_chat_to_config(getattr(cfg, "config_path", None), chat_id)
    if callable(getattr(source, "send_to_chat", None)):
        try:
            from .outbound import owner_content, send_owner
            plain, card = owner_content(cfg, ALERT_WELCOME_TEXT)
            ik = f"sd-alert-init-{chat_id[-16:]}"[:50]
            res = send_owner(lambda t, c: source.send_to_chat(chat_id, t, ik, "bot", **({"card": c} if c else {})), plain, card)
            if mid := (str((res or {}).get("message_id") or "") if isinstance(res, dict) else ""):
                if store is not None and hasattr(store, "set_meta"):
                    store.set_meta(META_ALERT_PIN_MID, mid)
                if callable(getattr(source, "pin", None)):
                    source.pin(mid)
        except Exception:
            pass
    return chat_id

def send_owner_notice(disp, row) -> bool:
    nid, what, body = row["id"], row["kind"], bytes(row["body"]).decode("utf-8")
    if disp.notifier is not None and getattr(disp.notifier, "store", None) is None:
        try:
            setattr(disp.notifier, "store", disp.store)
        except Exception:
            pass
    src = getattr(disp, "source", None) or getattr(disp.notifier, "source", None)
    alert_chat = resolve_alert_chat_id(disp.cfg, disp.store) or ensure_alert_group(disp.cfg, disp.store, src)
    anchor, in_thread = (row["reply_to"] if "reply_to" in row.keys() else "") or "", bool(row["in_thread"]) if "in_thread" in row.keys() else False
    if alert_chat and what in SYSTEM_ALERT_KINDS and anchor and not reply_belongs_to_chat(disp.store, anchor, alert_chat):
        anchor, in_thread = "", False
    disp.store.owner_notice_start(nid)
    try:
        send = getattr(disp.notifier, "send", None)
        if callable(send) and anchor and _accepts(send, "reply_to"):
            send(body, row["idem_key"], reply_to=anchor, in_thread=in_thread)
        elif callable(send):
            send(body, row["idem_key"])
        else:
            disp.notifier(body)
    except Exception as e:
        row = disp.store.get_owner_notice(nid)
        n, err = row["attempts"] or 1, (" ".join(str(e).split())[-300:] or type(e).__name__)
        if (w := _parse(row["window_at"] or "")) is not None and _now() - w >= timedelta(hours=NOTICE_GIVE_UP_HOURS):
            abandon_owner_notice(disp, row, err)
            return False
        delay = _notice_backoff(n)
        disp.store.owner_notice_failed(nid, err, (_now() + delay).isoformat(timespec="seconds"))
        log.warning("给主人发%s失败（通知 #%d，第 %d 次）：%s；%d 分钟后重试", what, nid, n, err, int(delay.total_seconds() // 60))
        return False
    disp.store.owner_notice_sent(nid)
    if (row["attempts"] or 0) >= 1 or row["inflight_at"]:
        log.info("给主人的%s（通知 #%d）重试后发出去了", what, nid)
    return True

def abandon_owner_notice(disp, row, err: str) -> None:
    disp.store.owner_notice_abandon(row["id"], err)
    log.error("给主人的%s（通知 #%d%s）没发出去，放弃：%s。`%s notices` 查看，确认没收到用 `%s notices --retry %d` 重发，处理过了用 `%s notices --ack %d`",
              row["kind"], row["id"], f"，topic {row['topic_id']}" if row["topic_id"] else "", err, disp.cfg.cli_command, disp.cfg.cli_command, row["id"], disp.cfg.cli_command, row["id"])

def retry_owner_notices(disp) -> None:
    now = _now()
    for row in disp.store.pending_owner_notices():
        window, inflight = _parse(row["window_at"] or ""), _parse(row["inflight_at"] or "")
        if inflight is not None:
            if now - inflight >= timedelta(minutes=NOTICE_RESEND_SAFE_MINUTES):
                abandon_owner_notice(disp, row, f"{_display_time(inflight, disp.cfg.timezone_offset)}那次发送中断（进程在发送途中没了），不知道发出去没有；已超过飞书防重窗口，不自动补发")
                continue
        elif window is not None and now - window >= timedelta(hours=NOTICE_GIVE_UP_HOURS):
            abandon_owner_notice(disp, row, row["last_error"] or "2 小时内没发出去")
            continue
        elif (nxt := _parse(row["next_at"] or "")) is not None and now < nxt:
            continue
        if disp.notifier is not None and getattr(disp.notifier, "ledger", True):
            send_owner_notice(disp, row)

def send_watchdog_lark_card(chat_id: str, title: str, template: str, markdown_body: str) -> bool:
    if not chat_id:
        return False
    lark_bin = "/opt/homebrew/bin/lark-cli" if Path("/opt/homebrew/bin/lark-cli").exists() else "lark-cli"
    card = {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": " ".join(title.split()).replace('"', "＂")}, "template": template}, "body": {"elements": [{"tag": "markdown", "content": markdown_body}]}}
    base, errs = [lark_bin, "im", "+messages-send", "--as", "bot", "--chat-id", chat_id], []
    for extra in (["--msg-type", "interactive", "--content", json.dumps(card, ensure_ascii=False)], ["--text", f"{title}\n{markdown_body}"]):
        try:
            r = subprocess.run([*base, *extra], capture_output=True, text=True, timeout=20, check=False)
            if r.returncode == 0:
                return True
            errs.append((r.stderr or r.stdout or f"rc={r.returncode}").strip())
        except Exception as e:
            errs.append(str(e))
    print(f"看门狗飞书发送失败（card: {errs[0]}; text: {errs[-1]}）", file=sys.stderr)
    return False

_ORIG_WATCHDOG_SEND = send_watchdog_lark_card

def notify_watchdog_result(
    cfg, db, state: dict, state_path: Path | None, warns: list[str], prev_warns: list[str] | None, ref_now: datetime,
    *, notify_lark: bool, notify_chat_id: str = "", alert_cooldown_minutes: int | float = 60,
    infos: list[str] | None = None, bus_topic_id: str = "tp_bus",
) -> bool:
    prev_fp, prev_at = state.get("active_alert_fp", ""), state.get("last_alert_at", "")
    prev_w = list(prev_warns if prev_warns is not None else state.get("active_alert_warns", []))
    active_fp, last_at, active_warns, notify_line, sent = prev_fp, prev_at, prev_w, "", False
    obs = sys.modules.get("sheepdog.observe")
    fp_fn, obs_send = getattr(obs, "_fingerprint_warns", None), getattr(obs, "_send_lark_card", None)
    fp = fp_fn(warns) if callable(fp_fn) else (hashlib.sha256("\n".join(sorted(warns)).encode()).hexdigest()[:16] if warns else "")
    send_fn = obs_send if (callable(obs_send) and obs_send is not _ORIG_WATCHDOG_SEND) else send_watchdog_lark_card
    if notify_lark:
        chat_id = (notify_chat_id or "").strip() or resolve_alert_chat_id(cfg, db_path=db) or (str(getattr(getattr(cfg, "threads", None), "owner_chat_id", "") or "").strip() if cfg is not None else "")
        prev_dt = _parse(prev_at)
        elapsed_m = ((ref_now - prev_dt.replace(tzinfo=ref_now.tzinfo if prev_dt.tzinfo is None else prev_dt.tzinfo)).total_seconds() / 60.0) if prev_dt else 999999.0
        if fp != "":
            if (fp != prev_fp) or (prev_at == "") or (elapsed_m >= alert_cooldown_minutes):
                title = f"🐕 看门狗告警 · 牧羊犬体检发现 {len(warns)} 个问题 [{bus_topic_id}]"
                info_lines = "\n".join(f"· {i}" for i in (infos or []) if i)
                parts = [f"**时间**：{ref_now.strftime('%Y-%m-%d %H:%M:%S')}", "\n".join(f"⚠ {w}" for w in warns), *([f"**运行摘要**：\n{info_lines}"] if info_lines else []), f"（同指纹告警冷却 {int(alert_cooldown_minutes)} 分钟；恢复正常后将自动通知）"]
                if chat_id and send_fn(chat_id, title, "orange", "\n\n".join(parts)):
                    active_fp, last_at, active_warns, sent, notify_line = fp, ref_now.isoformat(timespec="seconds"), list(warns), True, f"已发送飞书看门狗告警（fp={fp}）"
            else:
                active_fp, last_at, active_warns, notify_line = prev_fp, prev_at, list(warns), f"看门狗告警冷却中（上次发送于 {prev_at[11:16]}，fp={fp}）"
        elif prev_fp != "":
            title = f"🐕 看门狗恢复 · 牧羊犬体检已恢复正常 [{bus_topic_id}]"
            prev_lines, info_lines = "\n".join(f"- {w}" for w in (prev_w or ["（上一轮告警）"])), ("\n".join(f"- · {i}" for i in (infos or []) if i) or "- · 正常")
            if chat_id and send_fn(chat_id, title, "green", f"**已解除的告警**：\n{prev_lines}\n\n**当前状态**：\n{info_lines}"):
                active_fp, last_at, active_warns, sent, notify_line = "", "", [], True, "已发送飞书看门狗恢复通知"
    if notify_line and infos is not None:
        infos.append(notify_line)
    state.update({"active_alert_fp": active_fp, "last_alert_at": last_at, "active_alert_warns": active_warns})
    if state_path is not None:
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(state_path).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return sent
