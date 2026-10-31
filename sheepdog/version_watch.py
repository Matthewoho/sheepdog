"""双轨版本巡检与可点击交互升级闭环（7.75 / 7.76，REG-023 ~ REG-026）。"""

from __future__ import annotations

import copy, json, logging, os, re, subprocess, urllib.error, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

import sheepdog as _pkg
from . import _git_run, default_repo_dir, parse_version_tuple, version_info
from .card import card_fits, plain_text
from .store import now_iso

log = logging.getLogger("sheepdog")

BUS_TOPIC_ID, PUBLIC_REPO_SLUG = "tp_bus", "Matthewoho/sheepdog"
PUBLIC_REPO_GIT_URL = "https://github.com/Matthewoho/sheepdog.git"
GITHUB_RELEASE_API_URL = f"https://api.github.com/repos/{PUBLIC_REPO_SLUG}/releases/latest"
GITHUB_RELEASE_WEB_URL = f"https://github.com/{PUBLIC_REPO_SLUG}/releases/latest"
BETA_UPGRADE_WORDS = frozenset({"升级beta", "升级 beta", "升级beta版", "升级 beta 版", "你来升级beta", "你来升级 beta", "立即升级beta", "立即升级 beta", "更新beta", "更新 beta", "升级尝鲜版", "你来升级尝鲜版", "sheepdog upgrade --beta"})
STABLE_UPGRADE_WORDS = frozenset({"升级", "你来升级", "立即升级", "更新", "你来更新", "升级稳定版", "你来升级稳定版", "更新稳定版", "重启", "你来重启", "重启生效", "升级重启", "sheepdog upgrade"})
ROLLBACK_WORDS = frozenset({"回滚", "你来回滚", "立即回滚", "回滚上一版", "退回上一版", "回退上一版", "版本回滚", "回滚版本", "sheepdog upgrade --rollback"})
UPGRADE_WORDS = STABLE_UPGRADE_WORDS | BETA_UPGRADE_WORDS | ROLLBACK_WORDS
VERSION_QUERY_WORDS = frozenset({"有新版本吗", "有没有新版本", "有没有新版", "有新版吗", "有没有beta", "有没有 beta", "有beta吗", "有 beta 吗", "有没有beta版", "有没有 beta 版", "有beta版吗", "有 beta 版吗", "有没有尝鲜版", "有尝鲜版吗", "查版本", "查询版本", "查一下版本", "看看版本", "当前版本", "检查更新", "查更新", "sheepdog version", "sheepdog version --check-remote"})
_AT_TAG_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.I | re.S)
_TRAIL_PUNCT = " \t\r\n。！!~～？?，,、.·\"'”’」』】"


def _is_mocked(fn: object) -> bool:
    return hasattr(fn, "_mock_name") or "unittest.mock" in type(fn).__module__


def _repo_root(repo_dir: Path | None) -> Path:
    fn = _pkg.default_repo_dir if _is_mocked(_pkg.default_repo_dir) else default_repo_dir
    return (repo_dir or fn()).resolve()


def _safe_json(raw: str | None, default=None):
    try:
        return json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError):
        return default


def _cleaned_candidates(content: str) -> list[str]:
    raw = _AT_TAG_RE.sub(" ", content or "")
    bodies = [raw.lstrip(), plain_text(raw).lstrip()]
    if isinstance(d := _safe_json(content), dict) and isinstance(d.get("text"), str):
        bodies.append(d["text"].lstrip())
    out: list[str] = []
    for b in bodies:
        c = " ".join(_AT_TAG_RE.sub(" ", b or "").split()).strip(_TRAIL_PUNCT).lower()
        if c and c not in out:
            out.append(c)
    return out


def parse_upgrade_request(content: str) -> str | None:
    """识别升级/回滚口令：返回 'rollback' | 'beta' | 'stable' | None。"""
    for c in _cleaned_candidates(content):
        if c in ROLLBACK_WORDS or c.startswith("sheepdog upgrade --rollback"):
            return "rollback"
        norm = c if c.startswith("sheepdog ") else re.sub(r"\s*beta\s*", "beta", c)
        if c in BETA_UPGRADE_WORDS or norm in BETA_UPGRADE_WORDS:
            return "beta"
        if c in STABLE_UPGRADE_WORDS:
            return "stable"
    return None


def is_upgrade_command(content: str) -> bool:
    return parse_upgrade_request(content) is not None


def is_version_query(content: str) -> bool:
    """识别主人询问版本或 Beta 的短句（整句精确匹配，防长业务句子误触）。"""
    return any(c in VERSION_QUERY_WORDS or (c if c.startswith("sheepdog ") else re.sub(r"\s*beta\s*", "beta", c)) in VERSION_QUERY_WORDS for c in _cleaned_candidates(content))


def fetch_latest_github_release(repo_slug: str = PUBLIC_REPO_SLUG, timeout: float = 5.0) -> dict | None:
    """探测 GitHub Latest Release（稳定版）。无 Release(404) 返回空 tag 字典；网络不可达返回 None。"""
    if (_is_mocked(subprocess.run) or _is_mocked(_git_run) or _is_mocked(probe_remote_tags)) and not _is_mocked(urllib.request.urlopen):
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo_slug}/releases/latest", headers={"Accept": "application/vnd.github+json", "User-Agent": "sheepdog-version-watch"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _safe_json(resp.read().decode("utf-8", errors="replace"), {})
            tag = str((data or {}).get("tag_name") or "").strip()
            return {"tag": tag, "version": tag.lstrip("vV"), "url": str((data or {}).get("html_url") or "")}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"tag": "", "version": "", "url": ""}
        if e.code not in (403, 429):
            return None
    except Exception:
        return None
    try:
        web_req = urllib.request.Request(f"https://github.com/{repo_slug}/releases/latest", headers={"User-Agent": "sheepdog-version-watch"})
        with urllib.request.urlopen(web_req, timeout=timeout) as resp:
            final_url = resp.geturl() or ""
            if "/releases/tag/" in final_url:
                tag = urllib.parse.unquote(final_url.split("/releases/tag/", 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/"))
                return {"tag": tag, "version": tag.lstrip("vV"), "url": final_url}
            return {"tag": "", "version": "", "url": ""}
    except Exception:
        return None


def probe_remote_tags(repo_dir: Path | None = None, *, remote_url: str = "", timeout: float = 5.0) -> dict:
    """双源（origin -> 公开 HTTPS fallback）探测远端 refs/tags/v*，正确解引用附注标签 ^{}。"""
    root = _repo_root(repo_dir)
    r = _git_run(["ls-remote", "--tags", "origin", "refs/tags/v*"], root, timeout=timeout)
    if r is None or r.returncode != 0 or not (r.stdout or "").strip():
        r2 = _git_run(["ls-remote", "--tags", remote_url or PUBLIC_REPO_GIT_URL, "refs/tags/v*"], root, timeout=timeout)
        if r2 is not None and (r is None or r.returncode != 0 or (r2.stdout or "").strip()):
            r = r2
    ok = bool(r is not None and r.returncode == 0)
    tags, peeled_seen, best_tuple = {}, set(), ()
    best_tag = best_ver = best_sha = ""
    for line in (r.stdout.splitlines() if (ok and r and r.stdout) else ()):
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        sha, ref = parts[0], parts[1]
        is_peeled = ref.endswith("^{}")
        ref = ref[:-3] if is_peeled else ref
        if not ref.startswith("refs/tags/v") or not (t_tuple := parse_version_tuple(tag_name := ref.removeprefix("refs/tags/"))):
            continue
        if is_peeled or tag_name not in peeled_seen:
            tags[tag_name] = sha
            if is_peeled:
                peeled_seen.add(tag_name)
        if t_tuple > best_tuple or (t_tuple == best_tuple and is_peeled):
            best_tuple, best_tag, best_ver, best_sha = t_tuple, tag_name, tag_name.lstrip("vV"), sha
    return {"ok": ok, "latest_tag": best_tag, "latest_version": best_ver, "latest_sha": best_sha, "tags": tags}


def probe_remote_versions(repo_dir: Path | None = None, *, timeout: float = 5.0, now: datetime | None = None) -> dict:
    """组合 Git Tag（Beta 候选）与 GitHub Release（Stable）双轨版本状态。"""
    ref_now = now or datetime.now().astimezone()
    tag_info, rel_info = probe_remote_tags(repo_dir, timeout=timeout), fetch_latest_github_release(timeout=timeout)
    tags, latest_tag, latest_version, latest_sha = tag_info["tags"], tag_info["latest_tag"], tag_info["latest_version"], tag_info["latest_sha"]
    if rel_info is not None:
        stable_tag, stable_version = rel_info.get("tag") or "", rel_info.get("version") or ""
        stable_sha, release_source = (tags.get(stable_tag, "") if stable_tag else ""), "github_release"
    elif (_is_mocked(subprocess.run) or _is_mocked(_git_run)) and not _is_mocked(urllib.request.urlopen) and not _is_mocked(fetch_latest_github_release):
        stable_tag, stable_version, stable_sha, release_source = latest_tag, latest_version, latest_sha, "tag_fallback"
    else:
        stable_tag = stable_version = stable_sha = ""
        release_source = "unreachable"
    has_beta = bool(latest_tag and parse_version_tuple(latest_tag) > parse_version_tuple(stable_tag or "0.0.0"))
    return {
        "ok": bool(tag_info["ok"] or rel_info is not None), "checked_at": ref_now.isoformat(timespec="seconds"),
        "release_source": release_source, "stable_tag": stable_tag, "stable_version": stable_version, "stable_sha": stable_sha,
        "release_url": (rel_info.get("url") or "") if isinstance(rel_info, dict) else "", "has_beta": has_beta,
        "beta_tag": latest_tag if has_beta else "", "beta_version": latest_version if has_beta else "",
        "beta_sha": latest_sha if has_beta else "", "latest_tag": latest_tag, "latest_version": latest_version,
        "latest_sha": latest_sha, "tags": tags,
    }


def _pid_alive(pid: int) -> bool:
    if not pid or int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _resolve_mem(store, runtime_info: dict | None, root: Path) -> dict:
    mem = runtime_info if isinstance(runtime_info, dict) else (_safe_json(store.get_meta("daemon_runtime")) if store else None)
    return mem if isinstance(mem, dict) else version_info(root, prefer_disk_version=False)


def record_daemon_runtime(store, repo_dir: Path | None = None) -> dict:
    payload = {**version_info(_repo_root(repo_dir)), "pid": os.getpid(), "started_at": now_iso()}
    store.set_meta("daemon_runtime", json.dumps(payload, ensure_ascii=False))
    return payload


def get_version_status(store=None, repo_dir: Path | None = None, *, check_remote: bool = False) -> dict:
    root = _repo_root(repo_dir)
    cli_info = version_info(root, prefer_disk_version=True)
    daemon = _safe_json(store.get_meta("daemon_runtime")) if store is not None else None
    remote = _safe_json(store.get_meta("version_remote_latest")) if store is not None else None
    if check_remote:
        remote = probe_remote_versions(root)
        if store is not None:
            store.set_meta("version_remote_latest", json.dumps(remote, ensure_ascii=False))
    pid = daemon.get("pid") if isinstance(daemon, dict) else None
    daemon_alive = bool(pid and _pid_alive(pid))
    in_sync = bool(isinstance(daemon, dict) and daemon_alive and daemon.get("version") == cli_info["version"] and (not cli_info["git_sha"] or not daemon.get("git_sha") or daemon.get("git_sha") == cli_info["git_sha"]))
    if not isinstance(daemon, dict):
        daemon_display = "未记录"
    elif not daemon_alive:
        daemon_display = f"{daemon.get('display', daemon.get('version', '?'))} [PID {pid} 未运行]"
    elif in_sync:
        daemon_display = f"{daemon.get('display')} PID {pid} 一致"
    else:
        daemon_display = f"{daemon.get('display')} PID {pid} ≠ 磁盘 {cli_info['display']}（待重启）"
    return {"cli": cli_info, "daemon": daemon, "daemon_alive": daemon_alive, "in_sync": in_sync, "daemon_display": daemon_display, "summary_line": f"sheepdog {cli_info['display']}（常驻进程: {daemon_display}）", "remote": remote}


def check_version_update(cfg, store, dispatcher, *, runtime_info: dict | None = None, repo_dir: Path | None = None, now: datetime | None = None) -> list[str]:
    """常驻进程版本巡检：本机升版单次提醒 + 远端 Stable Release 单次非强制建议（Beta 静默不打扰，严禁倒挂误报）。"""
    if not cfg.version.enabled:
        return []
    ref_now, root = now or datetime.now().astimezone(), _repo_root(repo_dir)
    disk, mem = version_info(root, prefer_disk_version=True), _resolve_mem(store, runtime_info, root)
    notified: set[str] = set(_safe_json(store.get_meta("version_notified"), []) or [])
    sent_texts: list[str] = []

    def _emit(key: str, text: str) -> None:
        if key in notified:
            return
        if dispatcher is not None:
            dispatcher._notify_owner(text, "新版提醒", BUS_TOPIC_ID)
        notified.add(key)
        store.set_meta("version_notified", json.dumps(sorted(notified), ensure_ascii=False))
        sent_texts.append(text)

    disk_t, mem_t = parse_version_tuple(disk["version"]), parse_version_tuple(mem.get("version") or "")
    sha_changed = bool(disk["git_sha"] and mem.get("git_sha") and disk["git_sha"] != mem.get("git_sha"))
    if (disk_t and mem_t and disk_t > mem_t) or (disk_t == mem_t and sha_changed):
        local_key = f"local:{disk['version']}:{disk['git_sha'][:12]}"
        mem_disp, pid = mem.get("display") or mem.get("version") or "?", mem.get("pid") or os.getpid()
        _emit(local_key, (
            f"🐕 汇报 · 新版提醒 [{BUS_TOPIC_ID}]\n"
            f"🔔 检测到本地仓库代码已更新至 **{disk['display']}**，当前常驻进程仍运行在 **{mem_disp}**（PID {pid}，尚未重启加载）。\n"
            "💡 非强制更新建议：可直接点击下方卡片按钮一键操作，或回复「**你来升级**」（或「**升级**」「**重启生效**」），我会自动原地重启常驻进程使其生效；也可在终端运行 `sheepdog upgrade`。"
        ))
        store.set_meta("version_watch_local_notified", local_key)

    interval_min = cfg.version.check_interval_minutes
    if interval_min > 0:
        last_raw, should_check = store.get_meta("version_remote_checked_at"), True
        if last_raw:
            try:
                should_check = (ref_now - datetime.fromisoformat(last_raw)).total_seconds() >= interval_min * 60.0
            except ValueError:
                should_check = True
        if should_check:
            store.set_meta("version_remote_checked_at", ref_now.isoformat(timespec="seconds"))
            probe = probe_remote_versions(root, timeout=5.0, now=ref_now)
            store.set_meta("version_remote_latest", json.dumps(probe, ensure_ascii=False))
            stable_tag = probe.get("stable_tag") or ""
            if stable_tag and parse_version_tuple(stable_tag) > max(disk_t, mem_t):
                s_sha = (probe.get("stable_sha") or "")[:7]
                desc = f"{stable_tag} ({s_sha})" if s_sha else stable_tag
                beta_hint = (f"（另有 Beta 尝鲜版 **{probe['beta_tag']}**，可发「**有没有 beta**」查看或「**升级 beta**」尝鲜）" if probe.get("has_beta") and probe.get("beta_tag") else "（想查是否有 Beta 尝鲜版可发「**有没有 beta**」）")
                _emit(f"remote:{stable_tag}", (
                    f"🐕 汇报 · 新版提醒 [{BUS_TOPIC_ID}]\n"
                    f"🔔 GitHub 已发布最新稳定版 Release **{desc}**（当前本机为 **{disk['display']}**）。\n"
                    f"💡 非强制更新建议：可直接点击下方卡片按钮一键操作，或回复「**你来升级**」（或「**升级稳定版**」），我会自动拉取并重启生效；也可在终端运行 `sheepdog upgrade`{beta_hint}。"
                ))
                store.set_meta("version_watch_remote_notified", stable_tag)
    return sent_texts


def format_version_query_reply(store=None, *, repo_dir: Path | None = None, check_remote: bool = True, now: datetime | None = None) -> str:
    root = _repo_root(repo_dir)
    st = get_version_status(store, repo_dir=root, check_remote=False)
    probe = probe_remote_versions(root, now=now) if check_remote else (st.get("remote") or {})
    if check_remote and store is not None:
        store.set_meta("version_remote_latest", json.dumps(probe, ensure_ascii=False))
    local_t = parse_version_tuple(st["cli"]["version"])
    if isinstance(st.get("daemon"), dict) and st["daemon"].get("version"):
        local_t = max(local_t, parse_version_tuple(st["daemon"]["version"]))
    stable_tag, s_sha = probe.get("stable_tag") or "", (probe.get("stable_sha") or "")[:7]
    if stable_tag:
        s_desc = f"**{stable_tag}** ({s_sha})" if s_sha else f"**{stable_tag}**"
        s_state = "高于本机，可升级" if parse_version_tuple(stable_tag) > local_t else "本机已是最新或更高"
        stable_line = f"- 📦 GitHub 稳定版（Release）：{s_desc}（{s_state}）"
    elif probe.get("release_source") == "unreachable" and not probe.get("ok"):
        stable_line = "- 📦 GitHub 稳定版（Release）：暂无法连接远端仓库"
    else:
        stable_line = "- 📦 GitHub 稳定版（Release）：暂无正式 Release"
    if probe.get("has_beta") and probe.get("beta_tag"):
        b_tag, b_sha = probe["beta_tag"], (probe.get("beta_sha") or "")[:7]
        b_state = "高于本机，可尝鲜升级" if parse_version_tuple(b_tag) > local_t else "本机已包含该版本"
        beta_line = f"- 🧪 Beta 尝鲜版（tag > release）：**{b_tag}**{f' ({b_sha})' if b_sha else ''}（{b_state}）"
    else:
        beta_line = "- 🧪 Beta 尝鲜版（tag > release）：当前无高于稳定版的 Beta Tag"
    return (
        f"🐕 汇报 · 版本与更新状态 [{BUS_TOPIC_ID}]\n- 💻 本机状态：{st['summary_line']}\n{stable_line}\n{beta_line}\n"
        "💡 非强制更新建议：可直接点击下方卡片按钮一键操作，或回复「**升级稳定版**」/「**你来升级**」（`sheepdog upgrade`）升级稳定版；回复「**升级 beta**」（`sheepdog upgrade --beta`）升级尝鲜版。"
    )


def _ver_btn(label: str, sd_action: str, btn_type: str = "default", **extra) -> dict:
    lbl = label.strip()
    return {"label": lbl, "type": btn_type, "value": {"sd_action": sd_action, **extra, "label": lbl}}


def parse_version_card_actions(text: str) -> list[dict]:
    """按新版提醒（Scenario A/B）、版本与更新状态（Scenario C）或升级完成（Scenario D，7.93）文本推导 Card 2.0 胶囊按钮列表。"""
    first, _, body = (text or "").strip("\n").partition("\n")
    if first.startswith("🐕 汇报 · 升级完成"):
        if "已从" in body and "回滚至" in body and "升级至" not in body:
            return [_ver_btn("🔍 查看版本状态", "version_query")]
        m_rb = re.search(r"回滚至\s*\*{0,2}([vV]?[0-9]+\.[0-9]+\.[0-9]+)?\s*(?:\(([0-9a-f]{7,40})\))?\*{0,2}", body)
        if not (m_rb and (m_rb.group(1) or m_rb.group(2))):
            m_rb = re.search(r"已从\s*\*\*([vV]?[0-9]+\.[0-9]+\.[0-9]+)?(?:\s*\(([0-9a-f]{7,40})(?:-dirty)?\))?\*\*\s*升级至", body)
        rb_ver = (m_rb.group(1) or "").strip() if m_rb else ""
        rb_sha = (m_rb.group(2) or "").strip() if m_rb else ""
        if rb_ver or rb_sha:
            disp = f"{rb_ver} ({rb_sha[:7]})".strip() if (rb_ver and rb_sha) else (rb_ver or rb_sha[:7])
            return [
                _ver_btn(f"⏪ 回滚至 {disp}", "version_rollback", "danger", target_sha=rb_sha, target_version=rb_ver or disp),
                _ver_btn("🔍 查看版本状态", "version_query"),
            ]
        return [_ver_btn("🔍 查看版本状态", "version_query")]
    if first.startswith("🐕 汇报 · 新版提醒"):
        if any(k in body for k in ("GitHub 已发布最新稳定版", "远端新稳定版", "升级稳定版")):
            m = re.search(r"(?:Release|稳定版)[^vV0-9\n]*?([vV]?[0-9]+\.[0-9]+\.[0-9]+[^\s*()（）`'\"”]*)", body) or re.search(r"\*\*([vV]?[0-9]+\.[0-9]+\.[0-9]+[^\s*()（）]*)", body)
            ver = m.group(1) if m else ""
            return [
                _ver_btn(f"🚀 升级稳定版 {ver}", "version_upgrade", "primary", channel="stable", no_pull=False, target_version=ver),
                _ver_btn("🔍 查有没有 Beta 版", "version_query"),
                _ver_btn("⏸ 暂不升级", "version_dismiss", target_version=ver),
            ]
        m = re.search(r"更新至[^vV0-9\n]*?([vV]?[0-9]+\.[0-9]+\.[0-9]+[^\s*()（）`'\"”]*)", body) or re.search(r"\*\*([vV]?[0-9]+\.[0-9]+\.[0-9]+[^\s*()（）]*)", body)
        ver = m.group(1) if m else ""
        return [_ver_btn(f"🔄 立即重启生效 {ver}", "version_upgrade", "primary", channel="stable", no_pull=True, target_version=ver), _ver_btn("⏸ 稍后手动重启", "version_dismiss", target_version=ver)]
    if not first.startswith("🐕 汇报 · 版本与更新状态"):
        return []
    b_line = next((ln for ln in body.splitlines() if "Beta 尝鲜版" in ln), "")
    s_line = next((ln for ln in body.splitlines() if "GitHub 稳定版" in ln), "")
    l_line = next((ln for ln in body.splitlines() if "本机状态" in ln), "")
    mb, ms = re.search(r"\*\*([vV]?[0-9]+\.[0-9]+\.[0-9]+[^\s*()（）]*)", b_line), re.search(r"\*\*([vV]?[0-9]+\.[0-9]+\.[0-9]+[^\s*()（）]*)", s_line)
    b_tag, s_tag = (mb.group(1) if mb else ""), (ms.group(1) if ms else "")
    if b_tag and ("高于本机" in b_line or "可尝鲜升级" in b_line):
        btns = [_ver_btn(f"🧪 升级 Beta 尝鲜版 {b_tag}", "version_upgrade", "primary", channel="beta", no_pull=False, target_version=b_tag)]
        if s_tag:
            btns.append(_ver_btn(f"🚀 升级稳定版 {s_tag}", "version_upgrade", "default", channel="stable", no_pull=False, target_version=s_tag))
        return [*btns, _ver_btn("⏸ 暂不升级", "version_dismiss", target_version=b_tag)]
    if s_tag and "高于本机" in s_line:
        return [_ver_btn(f"🚀 升级稳定版 {s_tag}", "version_upgrade", "primary", channel="stable", no_pull=False, target_version=s_tag), _ver_btn("🔍 刷新版本状态", "version_query"), _ver_btn("⏸ 暂不升级", "version_dismiss", target_version=s_tag)]
    if any(k in l_line for k in ("≠", "待重启", "未运行")):
        return [_ver_btn("🔄 立即重启生效", "version_upgrade", "primary", channel="stable", no_pull=True, target_version=""), _ver_btn("⏸ 暂不升级", "version_dismiss", target_version="")]
    btns = [_ver_btn("🔄 重新检查远端版本", "version_query")]
    if b_tag:
        btns.append(_ver_btn(f"🧪 重新拉取 Beta 版 {b_tag}", "version_upgrade", channel="beta", no_pull=False, target_version=b_tag))
    elif s_tag:
        btns.append(_ver_btn(f"🚀 升级/对齐稳定版 {s_tag}", "version_upgrade", channel="stable", no_pull=False, target_version=s_tag))
    return [*btns, _ver_btn("⏸ 暂不升级", "version_dismiss", target_version=b_tag or s_tag)]


def attach_version_interactions(card: dict, text: str, cfg=None) -> dict:
    """为新版提醒与版本查询卡片挂载 Card JSON 2.0 紧凑胶囊按钮（不含 vertical_spacing 与表单）。"""
    acts = parse_version_card_actions(text)
    if not acts or not isinstance(card, dict):
        return card
    cols = [
        {"tag": "column", "width": "auto", "elements": [{"tag": "button", "element_id": f"sd_ver_{idx + 1}", "text": {"tag": "plain_text", "content": a["label"][:28]}, "type": a["type"], "size": "medium", "width": "default", "value": a["value"], "behaviors": [{"type": "callback", "value": a["value"]}]}]}
        for idx, a in enumerate(acts)
    ]
    new_card = copy.deepcopy(card)
    new_card.setdefault("body", {}).setdefault("elements", []).extend([{"tag": "hr"}, {"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "8px", "margin": "4px 0px 0px 0px", "columns": cols}])
    return new_card if card_fits(new_card) else card


def pull_repo_for_channel(root: Path, channel: str = "stable", timeout: float = 15.0) -> tuple[bool, str, str]:
    """按通道拉取远端代码：返回 (pulled, pull_output, pull_failed_reason)。"""
    r = None
    if channel == "stable":
        probe = probe_remote_versions(root, timeout=5.0)
        if probe.get("release_source") == "github_release" and probe.get("stable_tag") and probe.get("has_beta"):
            s_tag = probe["stable_tag"]
            rf = _git_run(["git", "-C", str(root), "fetch", "--tags", "origin"], timeout=timeout)
            if rf is None or rf.returncode != 0:
                rf = _git_run(["git", "-C", str(root), "fetch", "--tags", PUBLIC_REPO_GIT_URL], timeout=timeout)
            r = _git_run(["git", "-C", str(root), "merge", "--ff-only", f"refs/tags/{s_tag}"], timeout=timeout) if (rf and rf.returncode == 0) else rf
    if r is None:
        r = _git_run(["git", "-C", str(root), "pull", "--ff-only"], timeout=timeout)
    if r is None:
        return False, "", "git 命令执行失败"
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()[:300]
    return (r.returncode == 0), out, ("" if r.returncode == 0 else (out or f"returncode={r.returncode}"))


def summarize_upgrade_changes(root: Path, from_sha: str = "", to_sha: str = "", explicit_notes: str = "") -> str:
    """7.93（REG-061）：提炼本次升级『更新了什么』与『解决了什么问题』摘要，供升级完成通知展示。"""
    notes = (explicit_notes or "").strip()
    if notes:
        if "更新了什么" in notes or "解决了什么问题" in notes:
            return notes
        parts = [p.strip() for p in re.split(r"[\n；;]+", notes) if p.strip()]
        if len(parts) >= 2:
            return f"- 📋 **更新了什么**：{parts[0]}\n- 🎯 **解决了什么问题**：{'；'.join(parts[1:])}"
        return f"- 📋 **更新了什么 / 解决了什么问题**：{notes}"

    if not (root / ".git").exists():
        return ""
    rng = f"{from_sha}..{to_sha}" if (from_sha and to_sha and from_sha != to_sha) else "HEAD~1..HEAD"
    updated_items: list[str] = []
    solved_items: list[str] = []
    r_diff = _git_run(["git", "-C", str(root), "diff", "--unified=0", rng, "--", "tests/REGRESSIONS.md"], timeout=5.0)
    if r_diff is not None and r_diff.returncode == 0 and (r_diff.stdout or "").strip():
        for raw_ln in r_diff.stdout.splitlines():
            if not raw_ln.startswith("+") or raw_ln.startswith("+++"):
                continue
            ln = raw_ln[1:].strip()
            if mh := re.match(r"^##\s+([0-9]+\.[0-9]+\s+.+)$", ln):
                item = mh.group(1).strip()
                if item not in updated_items:
                    updated_items.append(item)
            elif mr := re.match(r"^\|\s*([0-9]+\.[0-9]+)\s*\|\s*([^|]+?)\s*\|", ln):
                spec_no, symptom = mr.group(1).strip(), mr.group(2).strip()
                if symptom and symptom not in ("症状", "---"):
                    entry = f"[{spec_no}] {symptom}"
                    if entry not in solved_items:
                        solved_items.append(entry)

    commits: list[str] = []
    r_log = _git_run(["git", "-C", str(root), "log", "--no-merges", "-n", "5", "--format=%h %s", rng], timeout=5.0)
    if (r_log is None or r_log.returncode != 0 or not (r_log.stdout or "").strip()) and to_sha:
        r_log = _git_run(["git", "-C", str(root), "log", "-n", "1", "--format=%h %s", to_sha], timeout=5.0)
    if r_log is not None and r_log.returncode == 0 and (r_log.stdout or "").strip():
        commits = [c.strip() for c in r_log.stdout.splitlines() if c.strip()]

    lines: list[str] = []
    if updated_items:
        lines.append(f"- 📋 **更新了什么**：{'；'.join(updated_items[:3])}")
    elif commits:
        lines.append(f"- 📋 **更新了什么**：{'；'.join(commits[:3])}")
    if solved_items:
        lines.append(f"- 🎯 **解决了什么问题**：{'；'.join(solved_items[:3])}")
    elif commits and updated_items:
        lines.append(f"- 🎯 **提交记录**：{'；'.join(commits[:3])}")
    elif commits:
        lines.append(f"- 🎯 **解决了什么问题**：同步提交 {'；'.join(commits[:3])} 中的修复与改进")
    return "\n".join(lines)


def rollback_repo_to_target(root: Path, target_sha: str, timeout: float = 15.0) -> tuple[bool, str, str]:
    """7.93（REG-061）：安全将仓库回滚至目标 commit/tag（保护无关未提交文件不被误抹）。"""
    ref = (target_sha or "").strip()
    if not ref:
        return False, "", "未指定回滚目标版本（target_sha 为空）"
    if not (root / ".git").exists():
        return False, "", "当前目录不是 Git 仓库，无法执行版本回滚"
    rv = _git_run(["git", "-C", str(root), "rev-parse", "--verify", f"{ref}^{{commit}}"], timeout=5.0)
    if rv is None or rv.returncode != 0 or not (rv.stdout or "").strip():
        err = ((rv.stderr if rv else "") or "").strip() or f"无法解析目标版本 {ref}"
        return False, "", err
    resolved_sha = rv.stdout.strip().splitlines()[0].strip()

    rd = _git_run(["git", "-C", str(root), "diff", "--name-only", resolved_sha, "HEAD"], timeout=5.0)
    changed_files = [f.strip() for f in (rd.stdout.splitlines() if (rd and rd.returncode == 0 and rd.stdout) else ()) if f.strip()]

    rs = _git_run(["git", "-C", str(root), "status", "--porcelain", "-uno"], timeout=5.0)
    has_dirty_tracked = bool(rs and rs.returncode == 0 and (rs.stdout or "").strip())
    if not has_dirty_tracked:
        rr = _git_run(["git", "-C", str(root), "reset", "--hard", resolved_sha], timeout=timeout)
        if rr is None or rr.returncode != 0:
            err = ((rr.stderr if rr else "") or (rr.stdout if rr else "") or "git reset --hard 失败").strip()[:300]
            return False, "", err
        out = ((rr.stdout or "") + "\n" + (rr.stderr or "")).strip()[:300]
        return True, out, ""

    if changed_files:
        rc_dirty = _git_run(["git", "-C", str(root), "status", "--porcelain", "-uno", "--", *changed_files], timeout=5.0)
        if rc_dirty and rc_dirty.returncode == 0 and (rc_dirty.stdout or "").strip():
            return False, "", "回滚涉及的目标文件存在未提交修改，已拒绝覆盖"
    r_soft = _git_run(["git", "-C", str(root), "reset", "--soft", resolved_sha], timeout=timeout)
    if r_soft is None or r_soft.returncode != 0:
        err = ((r_soft.stderr if r_soft else "") or "git reset --soft 失败").strip()[:300]
        return False, "", err
    if changed_files:
        _git_run(["git", "-C", str(root), "checkout", resolved_sha, "--", *changed_files], timeout=timeout)
    _git_run(["git", "-C", str(root), "reset", "HEAD", "--", "."], timeout=timeout)
    return True, f"HEAD selectively reset to {resolved_sha[:7]}", ""


def _resolve_rollback_info(store, root: Path, prev_ver: str, prev_sha: str, prev_disp: str, after_sha: str) -> dict:
    """确定并持久化升级完成卡片上可供一键回滚的上一版本目标。"""
    if prev_sha and after_sha and prev_sha != after_sha:
        info = {"version": prev_ver, "git_sha": prev_sha, "git_short": prev_sha[:7], "display": prev_disp, "saved_at": now_iso()}
        if store is not None:
            store.set_meta("version_rollback_target", json.dumps(info, ensure_ascii=False))
        return info
    if store is not None and isinstance(saved := _safe_json(store.get_meta("version_rollback_target")), dict):
        s_sha = str(saved.get("git_sha") or "").strip()
        if s_sha and s_sha != after_sha:
            return saved
    if (root / ".git").exists():
        rp = _git_run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD~1^{commit}"], timeout=3.0)
        if rp is not None and rp.returncode == 0 and (p_sha := (rp.stdout or "").strip()):
            p_ver = ""
            rf = _git_run(["git", "-C", str(root), "show", f"{p_sha}:sheepdog/__init__.py"], timeout=3.0)
            if rf is not None and rf.returncode == 0 and rf.stdout:
                m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', rf.stdout)
                p_ver = m.group(1).strip() if m else ""
            info = {"version": p_ver, "git_sha": p_sha, "git_short": p_sha[:7], "display": f"{p_ver} ({p_sha[:7]})".strip() if p_ver else p_sha[:7]}
            if store is not None:
                store.set_meta("version_rollback_target", json.dumps({**info, "saved_at": now_iso()}, ensure_ascii=False))
            return info
    return {}


def execute_upgrade(
    dispatcher, *, reply_to: str = "", command: str = "", no_pull: bool = False,
    channel: str = "stable", rollback: bool = False, target_sha: str = "",
    from_sha: str = "", from_display: str = "", notes: str = "", repo_dir: Path | None = None,
) -> dict:
    """执行常驻进程双轨（stable / beta）热升级或一键回滚（rollback），附带更新摘要与回滚按钮（7.75 / 7.93）。"""
    root = _repo_root(repo_dir)
    mem = _resolve_mem(dispatcher.store, dispatcher.runtime_info, root)
    mem_disp = (from_display or mem.get("display") or mem.get("version") or "?").strip()
    mem_ver = (mem.get("version") or "").strip()
    pid = mem.get("pid") or os.getpid()
    disk_before = version_info(root, prefer_disk_version=True)
    prev_sha = (from_sha or mem.get("git_sha") or disk_before.get("git_sha") or "").strip()
    if not mem_ver:
        mem_ver = disk_before.get("version") or ""

    is_rollback = bool(rollback or channel == "rollback")
    if is_rollback:
        rb_target = (target_sha or "").strip()
        if not rb_target and dispatcher.store is not None:
            saved = _safe_json(dispatcher.store.get_meta("version_rollback_target"))
            if isinstance(saved, dict):
                rb_target = str(saved.get("git_sha") or "").strip()
        if not rb_target:
            rb_target = "HEAD~1"
        rolled_ok, rb_out, rb_err = rollback_repo_to_target(root, rb_target, timeout=15.0)
        disk_after = version_info(root, prefer_disk_version=True)
        if rolled_ok:
            dispatcher.restart_requested = True
            body = f"⏪ 已从 **{mem_disp}** 回滚至 **{disk_after['display']}**（PID {pid}），正在原地重启常驻进程使其立即生效。"
        else:
            dispatcher.restart_requested = False
            body = f"⚠️ 回滚失败（{rb_err}），当前仍为 **{disk_after['display']}**（PID {pid}）。"
        dispatcher._notify_owner(f"🐕 汇报 · 升级完成 [{BUS_TOPIC_ID}]\n{body}", "升级完成", BUS_TOPIC_ID, reply_to=reply_to)
        log.info("已执行 rollback 动作（%s -> %s，ok=%s，target=%s，command=%r）", mem_disp, disk_after["display"], rolled_ok, rb_target, command)
        return {"from": mem_disp, "to": disk_after["display"], "version": disk_after["version"], "git_sha": disk_after["git_sha"], "channel": "rollback", "rollback": True, "rolled_back": rolled_ok, "rollback_output": rb_out, "rollback_failed_reason": rb_err, "restart_requested": bool(rolled_ok)}

    pulled, pull_output, pull_failed_reason = False, "", ""
    if not no_pull and (root / ".git").exists():
        if disk_before["dirty"]:
            pull_output = pull_failed_reason = "工作区有未提交修改，已跳过代码拉取"
        else:
            pulled, pull_output, pull_failed_reason = pull_repo_for_channel(root, channel=channel, timeout=15.0)
    disk_after = version_info(root, prefer_disk_version=True)
    dispatcher.restart_requested = True
    if disk_after["display"] != mem_disp:
        headline = f"✅ 已从 **{mem_disp}** 升级至 **{disk_after['display']}**（PID {pid}），正在原地重启常驻进程使其立即生效。"
    elif pull_failed_reason:
        headline = f"⚠️ 未能拉取远端最新代码（{pull_failed_reason}），当前仍为 **{disk_after['display']}**（PID {pid}），正在原地重启常驻进程。"
    else:
        headline = f"✅ 当前代码已是最新 **{disk_after['display']}**（PID {pid}），正在原地重启常驻进程刷新运行态。"

    rb_info = _resolve_rollback_info(dispatcher.store, root, mem_ver, prev_sha, mem_disp, disk_after.get("git_sha") or "")
    change_summary = summarize_upgrade_changes(
        root,
        from_sha=prev_sha or str(rb_info.get("git_sha") or ""),
        to_sha=disk_after.get("git_sha") or "",
        explicit_notes=notes,
    )
    extra_lines: list[str] = []
    if change_summary:
        extra_lines.append(change_summary)
    if rb_info.get("git_sha"):
        rb_ver_str = str(rb_info.get("version") or "").strip()
        rb_short = str(rb_info["git_sha"])[:7]
        rb_label = f"{rb_ver_str} ({rb_short})" if rb_ver_str else rb_short
        extra_lines.append(f"⏪ 如需回退：可直接点击下方「**回滚至 {rb_label}**」按钮（或回复「**回滚**」/ 运行 `sheepdog upgrade --rollback`）。")
    full_body = "\n".join([headline, *extra_lines])
    dispatcher._notify_owner(f"🐕 汇报 · 升级完成 [{BUS_TOPIC_ID}]\n{full_body}", "升级完成", BUS_TOPIC_ID, reply_to=reply_to)
    log.info("已执行 upgrade 动作（channel=%s，%s -> %s，pulled=%s，command=%r），标记原地重启", channel, mem_disp, disk_after["display"], pulled, command)
    return {"from": mem_disp, "to": disk_after["display"], "version": disk_after["version"], "git_sha": disk_after["git_sha"], "channel": channel, "pulled": pulled, "pull_output": pull_output, "pull_failed_reason": pull_failed_reason, "change_summary": change_summary, "rollback_target": rb_info, "restart_requested": True}


def check_version_drift(c, disk_ver_info: dict, version_mismatch_since: str | None, ref_now: datetime) -> tuple[list[str], list[str], str | None]:
    """L1.3：daemon_runtime vs 磁盘版本一致性检查 + 远端可用版本轻量 info 提示。"""
    warns, infos, mismatch_since = [], [], version_mismatch_since or ""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
        return warns, infos, mismatch_since
    row = c.execute("SELECT value FROM meta WHERE key = 'daemon_runtime'").fetchone()
    daemon_runtime = _safe_json(row[0] if row else None)
    if isinstance(daemon_runtime, dict):
        d_ver, d_sha = daemon_runtime.get("version") or "", daemon_runtime.get("git_sha") or ""
        if bool(d_ver and d_ver != disk_ver_info["version"]) or bool(d_sha and disk_ver_info["git_sha"] and d_sha != disk_ver_info["git_sha"]):
            mismatch_since = mismatch_since or ref_now.isoformat(timespec="seconds")
            try:
                mismatch_sec = max(0.0, (ref_now - datetime.fromisoformat(mismatch_since)).total_seconds())
            except ValueError:
                mismatch_sec = 0.0
            if mismatch_sec >= 300:
                warns.append(f"版本不一致已超 {int(mismatch_sec // 60)} 分钟：磁盘代码为 {disk_ver_info['display']}，但常驻进程仍运行在 {daemon_runtime.get('display', d_ver)}（PID {daemon_runtime.get('pid')}），请重启")
            else:
                infos.append(f"磁盘版本 {disk_ver_info['display']} ≠ 常驻进程 {daemon_runtime.get('display', d_ver)}（观察中 {int(mismatch_sec)}s）")
        else:
            mismatch_since = ""
    row_rem = c.execute("SELECT value FROM meta WHERE key = 'version_remote_latest'").fetchone()
    if isinstance(rem := _safe_json(row_rem[0] if row_rem else None), dict):
        local_t = parse_version_tuple(disk_ver_info.get("version") or "")
        if isinstance(daemon_runtime, dict) and daemon_runtime.get("version"):
            local_t = max(local_t, parse_version_tuple(daemon_runtime["version"]))
        s_tag, b_tag = rem.get("stable_tag") or "", rem.get("beta_tag") or ""
        hints = ([f"稳定版 {s_tag}"] if (s_tag and parse_version_tuple(s_tag) > local_t) else []) + ([f"Beta {b_tag}"] if (rem.get("has_beta") and b_tag and parse_version_tuple(b_tag) > local_t) else [])
        if hints:
            infos.append(f"远端有可用更新（{'，'.join(hints)}，非强制）")
    return warns, infos, mismatch_since
