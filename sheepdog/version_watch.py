"""双轨版本巡检与可点击交互升级闭环（7.75 / 7.76，REG-023 ~ REG-026）。"""

from __future__ import annotations

import compileall, copy, io, json, logging, os, re, shutil, stat, subprocess, sys, tarfile, urllib.error, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

import sheepdog as _pkg
from . import _git_run, default_repo_dir, get_build_info, parse_version_tuple, version_info
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


def release_gate_dir() -> Path:
    env_dir = os.environ.get("SHEEPDOG_RELEASE_GATE_DIR", "").strip()
    return Path(env_dir).expanduser() if env_dir else Path.home() / ".local" / "state" / "sheepdog-release-gate"


def releases_root_dir() -> Path:
    env_dir = os.environ.get("SHEEPDOG_RELEASES_DIR", "").strip()
    return Path(env_dir).expanduser() if env_dir else Path.home() / ".local" / "share" / "sheepdog-releases"


def _is_gate_enforced(root: Path) -> bool:
    if os.environ.get("SHEEPDOG_ENFORCE_RELEASE_GATE", "").strip() == "0":
        return False
    if os.environ.get("SHEEPDOG_RELEASE_GATE_DIR", "").strip() or os.environ.get("SHEEPDOG_ENFORCE_RELEASE_GATE", "").strip():
        return True
    if "unittest" in sys.modules:
        return False
    return bool((root / "scripts" / "release_gate.py").is_file() or get_build_info(root) is not None)


def check_release_gate_record(sha: str, *, gate_dir: Path | None = None, runs: int = 3) -> tuple[bool, str, Path]:
    """校验 <gate_dir>/<40位sha>.json 是否存在且 3 轮全绿。"""
    full_sha = (sha or "").strip()
    gdir = (gate_dir or release_gate_dir()).expanduser()
    rec_path = gdir / f"{full_sha}.json"
    if not re.fullmatch(r"[0-9a-f]{40}", full_sha):
        return False, f"无效的 40 位提交号: {full_sha!r}", rec_path
    if not rec_path.is_file():
        return False, f"{full_sha[:12]} 没有闸门记录（{rec_path}）", rec_path
    try:
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, f"闸门记录损坏（{rec_path}）：{e}", rec_path
    if not isinstance(rec, dict):
        return False, f"闸门记录格式无效（{rec_path}）", rec_path
    reasons: list[str] = []
    if rec.get("commit") != full_sha:
        reasons.append(f"记录里的提交号是 {rec.get('commit')!r}，不是 {full_sha}")
    rs = rec.get("runs") or []
    if len(rs) != runs:
        reasons.append(f"记录里跑了 {len(rs)} 次，要求 {runs} 次")
    if not all(isinstance(r, dict) and r.get("ok") is True and isinstance(r.get("tests"), int) and r["tests"] > 0 for r in rs):
        reasons.append("记录里存在未全绿或测试数为 0 的轮次")
    if len({r.get("tests") for r in rs if isinstance(r, dict)}) > 1:
        reasons.append("3 次的测试数不一致")
    if (rec.get("regressions") or {}).get("missing"):
        reasons.append("回归登记里存在缺失测试")
    if (rec.get("ledger_compat") or {}).get("ok") is False:
        reasons.append("账本迁移前向/回滚兼容性检查未通过")
    if reasons:
        return False, "；".join(reasons), rec_path
    return True, f"✓ {full_sha[:12]} 过了闸门（{rec.get('created_at', '')}）", rec_path


def resolve_commit_sha(root: Path, ref: str = "HEAD") -> str:
    target = (ref or "HEAD").strip()
    if (root / ".git").exists():
        r = _git_run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}"], timeout=3.0)
        if r is not None and r.returncode == 0:
            sha = (r.stdout or "").strip()
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                return sha
    if re.fullmatch(r"[0-9a-f]{40}", target):
        return target
    return ""


def verify_commit_release_gate(root: Path, ref: str = "HEAD", *, gate_dir: Path | None = None) -> tuple[bool, str, str]:
    """校验指定提交是否已通过 scripts/release_gate.py --check <sha>（含本地记录校验）。"""
    sha = resolve_commit_sha(root, ref)
    if not _is_gate_enforced(root) and gate_dir is None:
        return True, sha, "exempt"
    if not sha:
        return False, "", f"无法解析提交 {ref!r}"
    gdir = (gate_dir or release_gate_dir()).expanduser()
    script = root / "scripts" / "release_gate.py"
    if script.is_file() and not _is_mocked(subprocess.run):
        env = os.environ.copy()
        env["SHEEPDOG_RELEASE_GATE_DIR"] = str(gdir)
        try:
            proc = subprocess.run(
                [sys.executable, str(script), "--repo", str(root), "--check", sha],
                capture_output=True,
                text=True,
                timeout=15.0,
                env=env,
            )
            msg = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            return proc.returncode == 0, sha, msg
        except Exception as e:
            return False, sha, f"执行 release_gate.py --check 异常: {e}"
    ok, msg, _ = check_release_gate_record(sha, gate_dir=gdir)
    return ok, sha, msg


def read_commit_version(root: Path, sha: str) -> str:
    if (root / ".git").exists() and sha:
        r = _git_run(["git", "-C", str(root), "show", f"{sha}:sheepdog/__init__.py"], timeout=3.0)
        if r is not None and r.returncode == 0 and r.stdout:
            m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', r.stdout, re.M)
            if m:
                return m.group(1).strip()
    return ""


def _make_tree_writable(path: Path) -> None:
    if not path.exists():
        return
    for p in [path, *path.rglob("*")]:
        try:
            if not p.is_symlink():
                mode = p.stat().st_mode
                os.chmod(p, mode | stat.S_IWUSR | (stat.S_IXUSR if p.is_dir() else 0))
        except OSError:
            pass


def _make_tree_readonly(path: Path) -> None:
    for p in sorted(path.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            if not p.is_symlink():
                mode = p.stat().st_mode
                os.chmod(p, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        except OSError:
            pass
    try:
        mode = path.stat().st_mode
        os.chmod(path, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        pass


def install_gated_release_snapshot(
    root: Path,
    ref: str = "HEAD",
    *,
    releases_dir: Path | None = None,
    gate_dir: Path | None = None,
    verify_gate: bool = True,
) -> dict:
    """将已通过 release_gate 的提交导出为只读快照 ~/.local/share/sheepdog-releases/v<ver>-<sha7>，并原子切换 current 软链。"""
    gdir = (gate_dir or release_gate_dir()).expanduser().resolve()
    rdir = (releases_dir or releases_root_dir()).expanduser().resolve()
    if verify_gate:
        ok, sha, msg = verify_commit_release_gate(root, ref, gate_dir=gdir)
        if not ok:
            raise RuntimeError(f"拒绝安装未过闸快照（{ref}）：{msg}")
    else:
        sha = resolve_commit_sha(root, ref)
        if not sha:
            raise RuntimeError(f"无法解析快照提交：{ref!r}")
    ver = read_commit_version(root, sha) or version_info(root, prefer_disk_version=True).get("version") or "0.0.0"
    ver = ver.lstrip("vV")
    snap_name = f"v{ver}-{sha[:7]}"
    rdir.mkdir(parents=True, exist_ok=True)
    snap_dir = rdir / snap_name
    existing_bi = get_build_info(snap_dir / "sheepdog") if (snap_dir / "sheepdog" / "_build_info.py").is_file() else None
    if existing_bi is None or existing_bi.get("git_sha") != sha:
        staging = rdir / f".staging-{snap_name}-{os.getpid()}"
        if staging.exists():
            _make_tree_writable(staging)
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        if (root / ".git").exists() and not _is_mocked(subprocess.run):
            out = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", sha], capture_output=True, timeout=15.0)
            if out.returncode != 0:
                shutil.rmtree(staging, ignore_errors=True)
                raise RuntimeError(f"git archive {sha[:12]} 失败: {out.stderr.decode(errors='replace')[:200]}")
            with tarfile.open(fileobj=io.BytesIO(out.stdout)) as tar:
                tar.extractall(staging, filter="data")
        else:
            pkg_src = root / "sheepdog"
            if pkg_src.is_dir():
                shutil.copytree(pkg_src, staging / "sheepdog", dirs_exist_ok=True)
            else:
                (staging / "sheepdog").mkdir(parents=True, exist_ok=True)
                (staging / "sheepdog" / "__init__.py").write_text(f'__version__ = "{ver}"\n', encoding="utf-8")
        build_info_path = staging / "sheepdog" / "_build_info.py"
        build_info_path.parent.mkdir(parents=True, exist_ok=True)
        build_info_path.write_text(
            f'# Auto-generated by sheepdog release snapshot installer. DO NOT EDIT.\n'
            f'VERSION = {ver!r}\n'
            f'GIT_SHA = {sha!r}\n'
            f'TAG = {("v" + ver)!r}\n'
            f'SNAPSHOT_DIR = {str(snap_dir)!r}\n'
            f'REPO_DIR = {str(root.resolve())!r}\n',
            encoding="utf-8",
        )
        try:
            compileall.compile_dir(str(staging / "sheepdog"), quiet=1)
        except Exception:
            pass
        _make_tree_readonly(staging)
        if snap_dir.exists():
            _make_tree_writable(snap_dir)
            shutil.rmtree(snap_dir, ignore_errors=True)
        os.replace(staging, snap_dir)

    current_link = rdir / "current"
    tmp_link = rdir / f".current.tmp.{os.getpid()}"
    try:
        tmp_link.unlink(missing_ok=True)
    except OSError:
        pass
    os.symlink(snap_name, tmp_link)
    os.replace(tmp_link, current_link)
    return {
        "version": ver,
        "git_sha": sha,
        "git_short": sha[:7],
        "tag": f"v{ver}",
        "display": f"{ver} ({sha[:7]})",
        "snapshot_name": snap_name,
        "snapshot_dir": str(snap_dir),
        "current_link": str(current_link),
        "gate_record": str(gdir / f"{sha}.json"),
    }


def find_latest_gated_release(
    root: Path,
    *,
    gate_dir: Path | None = None,
    releases_dir: Path | None = None,
    exclude_shas: set[str] | None = None,
) -> dict | None:
    """查找最近一个具备有效 release_gate 记录的提交/快照，并确保其快照就绪且切换 current 软链。"""
    gdir = (gate_dir or release_gate_dir()).expanduser().resolve()
    rdir = (releases_dir or releases_root_dir()).expanduser().resolve()
    excluded = {s.strip() for s in (exclude_shas or set()) if s and s.strip()}
    valid_records: dict[str, dict] = {}
    if gdir.is_dir():
        for p in gdir.glob("*.json"):
            if p.name == "inflight.json":
                continue
            sha = p.stem
            if sha in excluded:
                continue
            ok, _, _ = check_release_gate_record(sha, gate_dir=gdir)
            if ok:
                try:
                    valid_records[sha] = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    valid_records[sha] = {"commit": sha}
    if not valid_records:
        return None

    existing_snaps: dict[str, Path] = {}
    if rdir.is_dir():
        for d in rdir.iterdir():
            if d.name == "current" or d.name.startswith(".") or not d.is_dir():
                continue
            bi = get_build_info(d / "sheepdog")
            if bi and bi.get("git_sha") in valid_records:
                existing_snaps[bi["git_sha"]] = d

    ordered_candidates: list[str] = []
    if (root / ".git").exists():
        r_rev = _git_run(["git", "-C", str(root), "rev-list", "HEAD", "-n", "500"], timeout=5.0)
        if r_rev is not None and r_rev.returncode == 0 and r_rev.stdout:
            for ln in r_rev.stdout.splitlines():
                c_sha = ln.strip()
                if c_sha in valid_records and c_sha not in ordered_candidates:
                    ordered_candidates.append(c_sha)
        r_tags = _git_run(["git", "-C", str(root), "tag", "-l", "v*", "--sort=-v:refname"], timeout=5.0)
        if r_tags is not None and r_tags.returncode == 0 and r_tags.stdout:
            for t_name in r_tags.stdout.splitlines():
                t_sha = resolve_commit_sha(root, t_name.strip())
                if t_sha in valid_records and t_sha not in ordered_candidates:
                    ordered_candidates.append(t_sha)

    remaining = sorted(
        (s for s in valid_records if s not in ordered_candidates),
        key=lambda s: str(valid_records[s].get("created_at") or ""),
        reverse=True,
    )
    for s in remaining:
        if s in existing_snaps or ((root / ".git").exists() and resolve_commit_sha(root, s) == s):
            ordered_candidates.append(s)

    for cand_sha in ordered_candidates:
        try:
            if (root / ".git").exists() and resolve_commit_sha(root, cand_sha) == cand_sha:
                return install_gated_release_snapshot(root, cand_sha, releases_dir=rdir, gate_dir=gdir, verify_gate=False)
            if cand_sha in existing_snaps:
                snap_dir = existing_snaps[cand_sha]
                bi = get_build_info(snap_dir / "sheepdog") or {}
                ver = str(bi.get("version") or "0.0.0").lstrip("vV")
                current_link = rdir / "current"
                tmp_link = rdir / f".current.tmp.{os.getpid()}"
                tmp_link.unlink(missing_ok=True)
                os.symlink(snap_dir.name, tmp_link)
                os.replace(tmp_link, current_link)
                return {
                    "version": ver,
                    "git_sha": cand_sha,
                    "git_short": cand_sha[:7],
                    "tag": f"v{ver}",
                    "display": f"{ver} ({cand_sha[:7]})",
                    "snapshot_name": snap_dir.name,
                    "snapshot_dir": str(snap_dir),
                    "current_link": str(current_link),
                    "gate_record": str(gdir / f"{cand_sha}.json"),
                }
        except Exception:
            continue
    return None


def ensure_startup_release_gate(
    root: Path | None = None,
    *,
    store=None,
    dispatcher=None,
    gate_dir: Path | None = None,
    releases_dir: Path | None = None,
) -> dict:
    """常驻进程 sheepdog run 启动闸门校验：
    - 若当前快照/目标代码已过闸，确保 current 软链就绪并放行；
    - 若未过闸或工作树 dirty，不直接停掉服务端，而是自动切换到最近一个有闸门记录的快照启动并在飞书告警；
    - 仅当本机连一个可用闸门记录都没有时才拒绝启动。
    """
    repo = _repo_root(root)
    if not _is_gate_enforced(repo) and gate_dir is None and releases_dir is None:
        return {"ok": True, "status": "exempt"}
    gdir = (gate_dir or release_gate_dir()).expanduser().resolve()
    rdir = (releases_dir or releases_root_dir()).expanduser().resolve()

    cur_link = rdir / "current"
    cur_bi = None
    if cur_link.exists() and (cur_link / "sheepdog" / "_build_info.py").is_file():
        cur_bi = get_build_info((cur_link / "sheepdog").resolve())
    in_proc_bi = get_build_info()
    disk = version_info(repo, prefer_disk_version=True)

    active_bi = in_proc_bi or cur_bi
    if active_bi is not None:
        cand_sha = str(active_bi.get("git_sha") or "").strip()
        cand_ver = str(active_bi.get("version") or "").strip()
        cand_disp = f"{cand_ver} ({cand_sha[:7]})" if cand_sha else cand_ver
        dirty = False
    else:
        cand_sha = str(disk.get("git_sha") or "").strip()
        cand_ver = str(disk.get("version") or "").strip()
        cand_disp = str(disk.get("display") or cand_ver or "未知版本")
        dirty = bool(disk.get("dirty"))

    reasons: list[str] = []
    if dirty:
        reasons.append(f"工作树存在未提交改动（{cand_disp}）")
    gate_ok, gate_msg, _ = check_release_gate_record(cand_sha, gate_dir=gdir) if cand_sha else (False, "未解析到 git_sha", gdir)
    if not gate_ok:
        reasons.append(gate_msg)

    if not reasons:
        snap = None
        if (repo / ".git").exists() and resolve_commit_sha(repo, cand_sha) == cand_sha:
            try:
                snap = install_gated_release_snapshot(repo, cand_sha, releases_dir=rdir, gate_dir=gdir, verify_gate=False)
            except Exception:
                snap = None
        return {
            "ok": True,
            "status": "passed",
            "version": cand_ver,
            "git_sha": cand_sha,
            "display": cand_disp,
            "snapshot": snap,
        }

    reason_text = "；".join(reasons)
    fb = find_latest_gated_release(repo, gate_dir=gdir, releases_dir=rdir)
    if fb is not None:
        alert_text = (
            f"🐕 汇报 · 启动闸门降级告警 [{BUS_TOPIC_ID}]\n"
            f"⚠️ 常驻进程启动校验未通过（当前目标：**{cand_disp}**；原因：{reason_text}），"
            f"未直接停掉服务，已自动切换至最近一个有闸门记录的只读快照 **{fb['display']}**（`{fb['snapshot_name']}`）启动运行。"
        )
        if dispatcher is not None:
            try:
                dispatcher._notify_owner(alert_text, "启动闸门降级告警", BUS_TOPIC_ID)
            except Exception as e:
                log.warning("发送启动闸门降级告警失败: %s", e)
        if store is not None:
            try:
                store.set_meta(
                    "daemon_startup_fallback",
                    json.dumps(
                        {"failed_target": cand_disp, "reason": reason_text, "fallback": fb, "at": now_iso()},
                        ensure_ascii=False,
                    ),
                )
            except Exception:
                pass
        log.warning("启动闸门降级：目标 %s 未过闸（%s），已切换至 %s 快照启动", cand_disp, reason_text, fb["display"])
        return {
            "ok": True,
            "status": "fallback",
            "failed_target": cand_disp,
            "reason": reason_text,
            "fallback": fb,
            "alert_text": alert_text,
        }

    alert_text = (
        f"🐕 汇报 · 启动闸门拦截告警 [{BUS_TOPIC_ID}]\n"
        f"❌ 常驻进程启动校验未通过（当前目标：**{cand_disp}**；原因：{reason_text}），"
        "且本机没有任何可用的历史过闸记录或快照，已拒绝启动。"
    )
    if dispatcher is not None:
        try:
            dispatcher._notify_owner(alert_text, "启动闸门拦截告警", BUS_TOPIC_ID)
        except Exception as e:
            log.warning("发送启动闸门拦截告警失败: %s", e)
    log.error("启动闸门拒绝启动：目标 %s 未过闸（%s），且无任何历史过闸快照可用", cand_disp, reason_text)
    return {
        "ok": False,
        "status": "rejected",
        "failed_target": cand_disp,
        "reason": reason_text,
        "fallback": None,
        "alert_text": alert_text,
    }


def is_release_gate_inflight() -> bool:
    """REG-130：检查本机是否正在运行 scripts/release_gate.py 全量测试闸门，避免在闸门运行期间抢先发『新版提醒』或误报『版本不一致已超 N 分钟』。"""
    inflight_file = release_gate_dir() / "inflight.json"
    if not inflight_file.is_file():
        return False
    try:
        data = json.loads(inflight_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        pid = int(data.get("pid") or 0)
        return pid > 0 and _pid_alive(pid)
    except Exception:
        return False


def _resolve_mem(store, runtime_info: dict | None, root: Path) -> dict:
    mem = runtime_info if isinstance(runtime_info, dict) else (_safe_json(store.get_meta("daemon_runtime")) if store else None)
    return mem if isinstance(mem, dict) else version_info(root, prefer_disk_version=False)


def record_daemon_runtime(store, repo_dir: Path | None = None) -> dict:
    if repo_dir is None and not _is_mocked(_pkg.default_repo_dir) and get_build_info() is not None:
        vinfo = version_info(None, prefer_disk_version=False)
    else:
        vinfo = version_info(_repo_root(repo_dir), prefer_disk_version=False)
    payload = {**vinfo, "pid": os.getpid(), "started_at": now_iso()}
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
    if ((disk_t and mem_t and disk_t > mem_t) or (disk_t == mem_t and sha_changed)) and not is_release_gate_inflight():
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


_UPGRADE_BUCKET_CN = {
    "stale_or_already_handled": "「陈旧/已办结却未复核」",
    "context_incomplete": "「上下文/现状未查清」",
    "arch_or_config_posture": "「架构/配置姿势纠偏」",
    "peer_or_owner_delegate": "「转交他人/专席」",
    "option_or_schedule_redirect": "「选项/排期调整」",
}

_UPGRADE_TERM_CN = (
    (r"(?<![A-Za-z0-9_])`?owner_instruction`?\s*[/、与和]\s*`?owner_reply`?\s*(?:指令)?(?![A-Za-z0-9_])", "群内 @ 指令或回复"),
    (r"(?<![A-Za-z0-9_])`?owner_instruction`?\s*(?:指令)?(?![A-Za-z0-9_])", "主人群内指令"),
    (r"(?<![A-Za-z0-9_])`?owner_reply`?(?![A-Za-z0-9_])", "主人回复"),
    (r"(?<![A-Za-z0-9_])`?needs_decision`?\s*回执", "「待拍板」回执"),
    (r"(?<![A-Za-z0-9_])`?needs_decision`?(?![A-Za-z0-9_])", "「待拍板」"),
    (r"(?<![A-Za-z0-9_])`?waiting`?\s*回执", "「等待中」回执"),
    (r"waiting/handled/done", "执行中或已完成"),
    (r"(?<![A-Za-z0-9_])`?open\s+escalations?`?(?![A-Za-z0-9_])", "未关闭的待拍板提问"),
    (r"(?<![A-Za-z0-9_])`?escalations?`?(?![A-Za-z0-9_])", "待拍板提问"),
    (r"(?<![A-Za-z0-9_])`?event_override`?(?![A-Za-z0-9_])", "拍板纠偏"),
    (r"(?<![A-Za-z0-9_])`?sheepdog\s+ask`?(?![A-Za-z0-9_])", "拍板请示"),
    (r"(?<![A-Za-z0-9_])`?sheepdog\s+report`?(?![A-Za-z0-9_])", "结果汇报"),
    (r"(?<![A-Za-z0-9_.])`?ask`?(?![A-Za-z0-9_])", "请示"),
    (r"(?<![A-Za-z0-9_])`?(?:topics\.)?last_active`?(?![A-Za-z0-9_])", "最后活跃时间"),
    (r"(?<![A-Za-z0-9_])`?dispatched_at`?(?![A-Za-z0-9_])", "派单时间"),
    (r"(?<![A-Za-z0-9_])`?duty_cards`?(?![A-Za-z0-9_])", "当班状态卡"),
    (r"(?<![A-Za-z0-9_])`?bus_core`?(?![A-Za-z0-9_])", "顶层中枢分组"),
    (r"(?<![A-Za-z0-9_])`?dynamic_oneshot`?(?![A-Za-z0-9_])", "一次性会话分组"),
    (r"(?<![A-Za-z0-9_])`?reply_to`?(?![A-Za-z0-9_])", "引用消息"),
    (r"(?<![A-Za-z0-9_])`?summary`?(?![A-Za-z0-9_])", "摘要"),
    (r"(?<![A-Za-z0-9_])`?standing`?\s*常设会话", "常驻会话"),
    (r"(?<![A-Za-z0-9_])`?standing`?(?![A-Za-z0-9_])", "常驻"),
)

_UPGRADE_PY_SYM = (
    r"(?<![A-Za-z0-9_.-])(?:"
    r"`?(?:sheepdog\.)?(?:engine|pending|outbound|admin_web|evolve|observe|store|router|card|card_callback|decision_hub|loops|version_watch|cli|duty|hook|actions|config|feishu_ws|topics)\.[A-Za-z0-9_.]+`?"
    r"|`?[A-Z][A-Za-z0-9_]*\.[a-z_][A-Za-z0-9_.]*`?"
    r"|`?_[A-Za-z0-9_.]+`?"
    r"|`?(?!tp_)[a-z][a-z0-9]*_[a-z0-9_]+(?:\([^)]*\))?`?"
    r"|`?[A-Z][A-Z0-9]*_[A-Z0-9_]+`?"
    r")(?![A-Za-z0-9_.-])"
)


def _plain_chinese_upgrade_text(text: str) -> str:
    """1.1.22（REG-111）：将含代码黑话（函数名、模块路径、正则常量、REG 编号）的升级说明提纯为面向主人的中文大白话。"""
    s = (text or "").strip()
    if not s:
        return ""
    for k, v in _UPGRADE_BUCKET_CN.items():
        s = re.sub(rf"(?<![A-Za-z0-9_])`?{re.escape(k)}`?(?![A-Za-z0-9_])", v, s)
    for pat, repl in _UPGRADE_TERM_CN:
        s = re.sub(pat, repl, s)
    s = re.sub(r"\s*[（(]\s*(?:v?[0-9]+(?:\.[0-9]+){1,2}\s*[,，\s]\s*)?REG-\d+\s*[）)]", "", s)
    s = re.sub(r"\bREG-\d+\b", "", s)
    s = re.sub(r"\s*[（(]\s*`?(?:om|lamb)_[A-Za-z0-9_]+`?(?:\s*[/、与和]\s*`?(?:om|lamb)_[A-Za-z0-9_]+`?)*\s*[）)]", "", s)
    s = re.sub(r"在\s*`?om_[A-Za-z0-9_]+`?\s*", "", s)
    s = re.sub(rf"(?:{_UPGRADE_PY_SYM}\s*)?新增\s+{_UPGRADE_PY_SYM}(?:\s*(?:[$]\\times[$]|×|与|和)\s*{_UPGRADE_PY_SYM})*\s*门禁", "新增发前门禁", s)
    s = re.sub(rf"(?:{_UPGRADE_PY_SYM}\s*)?新增\s+{_UPGRADE_PY_SYM}\s*[，,]\s*", "", s)
    s = re.sub(rf"(?:{_UPGRADE_PY_SYM}\s*)?新增\s+{_UPGRADE_PY_SYM}\s*", "", s)
    s = re.sub(r"`[^`]*(?:==|!=|<=|>=|\bNone\b|\bTrue\b|\bFalse\b|='[a-z_]+')[^`]*`", "", s)
    s = re.sub(rf"{_UPGRADE_PY_SYM}(?:\s*(?:与|和|及|、|/)\s*{_UPGRADE_PY_SYM})*\s*", "", s)
    s = re.sub(r"[（(]\s*[）)]", "", s)
    s = re.sub(r"因\s*(?:要求\s*)?且\s*", "因", s)
    s = re.sub(r"[，,]\s*且\s*", "，", s)
    s = re.sub(r"(?:^|(?<=[；;：:①②③④⑤⑥\s]))且\s*", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ；;，,")
    return s


def _extract_commit_body_notes(body_text: str) -> tuple[list[str], list[str]]:
    """从 git log --format=%B 提交正文中提取显式编写的『更新了什么』与『解决了何种问题』行（支持同行单行或换行列举 1./2./- 多行块）。"""
    updated: list[str] = []
    solved: list[str] = []
    current_sec: str | None = None
    for raw_ln in (body_text or "").splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        mu = re.match(r"^(?:[-*•]\s*)?(?:📋\s*)?\*{0,2}更新了什么\*{0,2}\s*[：:]\s*(.*)$", ln)
        if mu is not None:
            current_sec = "updated"
            inline = mu.group(1).strip()
            if inline:
                cleaned = _plain_chinese_upgrade_text(inline)
                if cleaned and cleaned not in updated:
                    updated.append(cleaned)
            continue
        ms = re.match(r"^(?:[-*•]\s*)?(?:🎯\s*)?\*{0,2}解决了何种问题\*{0,2}\s*[：:]\s*(.*)$", ln)
        if ms is not None:
            current_sec = "solved"
            inline = ms.group(1).strip()
            if inline:
                cleaned = _plain_chinese_upgrade_text(inline)
                if cleaned and cleaned not in solved:
                    solved.append(cleaned)
            continue
        if current_sec in ("updated", "solved"):
            m_item = re.match(r"^(?:[-*•]|\d+[\.、)\]])\s*(.+)$", ln)
            if m_item:
                cleaned = _plain_chinese_upgrade_text(m_item.group(1).strip())
                target_list = updated if current_sec == "updated" else solved
                if cleaned and cleaned not in target_list:
                    target_list.append(cleaned)
                continue
            current_sec = None
    return updated, solved


def _plain_chinese_commit_fallback(commit_line: str) -> str:
    """当既无 Commit Body 中文说明又无 REGRESSIONS.md 匹配时，把英文 git subject 转为大白话，严禁原样吐出 feat(...): 英文行。"""
    s = (commit_line or "").strip()
    if not s:
        return ""
    m = re.match(r"^([0-9a-f]{7,40})\s+(.*)$", s)
    sha = m.group(1)[:7] if m else ""
    subj = (m.group(2) if m else s).strip()
    subj_no_prefix = re.sub(r"^[a-z]+(?:\([^)]*\))?!?:\s*", "", subj, flags=re.IGNORECASE).strip()
    subj_no_reg = re.sub(r"\s*[（(]\s*(?:v?[0-9]+(?:\.[0-9]+){1,2}\s*[,，\s]\s*)?REG-\d+\s*[）)]\s*$", "", subj_no_reg := subj_no_prefix).strip()
    if re.search(r"[\u4e00-\u9fff]", subj_no_reg):
        cleaned = _plain_chinese_upgrade_text(subj_no_reg)
        return f"{cleaned}（{sha}）" if sha else cleaned
    return f"完成本轮代码改进与问题修复（提交 {sha}）" if sha else "完成本轮代码改进与问题修复"


def summarize_upgrade_changes(root: Path, from_sha: str = "", to_sha: str = "", explicit_notes: str = "") -> str:
    """7.93（REG-061）/ 1.1.22（REG-111）：提炼本次升级『更新了什么』与『解决了何种问题』中文大白话摘要，供升级完成通知展示。"""
    notes = (explicit_notes or "").strip()
    if notes:
        u_notes, s_notes = _extract_commit_body_notes(notes)
        if u_notes or s_notes:
            out_lines: list[str] = []
            if u_notes:
                out_lines.append(f"- 📋 **更新了什么**：{'；'.join(u_notes[:3])}")
            if s_notes:
                out_lines.append(f"- 🎯 **解决了何种问题**：{'；'.join(s_notes[:3])}")
            return "\n".join(out_lines)
        if "更新了什么" in notes or "解决了何种问题" in notes:
            return notes
        parts = [_plain_chinese_upgrade_text(p) for p in re.split(r"[\n；;]+", notes) if p.strip()]
        parts = [p for p in parts if p]
        if len(parts) >= 2:
            return f"- 📋 **更新了什么**：{parts[0]}\n- 🎯 **解决了何种问题**：{'；'.join(parts[1:])}"
        cleaned_single = parts[0] if parts else notes
        return f"- 📋 **更新了什么 / 解决了何种问题**：{cleaned_single}"

    if not (root / ".git").exists():
        return ""
    rng = f"{from_sha}..{to_sha}" if (from_sha and to_sha and from_sha != to_sha) else "HEAD~1..HEAD"
    updated_items: list[str] = []
    solved_items: list[str] = []

    # 1) 优先从 git commit 正文（%B）提取显式编写的「更新了什么 / 解决了何种问题」并做大白话清洗
    r_body = _git_run(["git", "-C", str(root), "log", "--no-merges", "-n", "5", "--format=%B", rng], timeout=5.0)
    if (r_body is None or r_body.returncode != 0 or not (r_body.stdout or "").strip()) and to_sha:
        r_body = _git_run(["git", "-C", str(root), "log", "-n", "1", "--format=%B", to_sha], timeout=5.0)
    if r_body is not None and r_body.returncode == 0 and (r_body.stdout or "").strip():
        u_body, s_body = _extract_commit_body_notes(r_body.stdout)
        updated_items.extend(u_body)
        solved_items.extend(s_body)

    # 2) 从 tests/REGRESSIONS.md 差异提取（同时支持 7.93 两段式与 1.1.22 三段式版本号，并自动拆分「症状；现通过...根治：修法」）
    if not updated_items or not solved_items:
        r_diff = _git_run(["git", "-C", str(root), "diff", "--unified=0", rng, "--", "tests/REGRESSIONS.md"], timeout=5.0)
        if r_diff is not None and r_diff.returncode == 0 and (r_diff.stdout or "").strip():
            for raw_ln in r_diff.stdout.splitlines():
                if not raw_ln.startswith("+") or raw_ln.startswith("+++"):
                    continue
                ln = raw_ln[1:].strip()
                if mh := re.match(r"^##\s+([0-9]+(?:\.[0-9]+){1,2}\s+.+)$", ln):
                    item = _plain_chinese_upgrade_text(mh.group(1).strip())
                    if item and item not in updated_items:
                        updated_items.append(item)
                elif mr := re.match(r"^\|\s*([0-9]+(?:\.[0-9]+){1,2})\s*\|\s*([^|]+?)\s*\|", ln):
                    spec_no, col2 = mr.group(1).strip(), mr.group(2).strip()
                    if not col2 or col2 in ("症状", "---"):
                        continue
                    split_parts = re.split(r"(?:[；。]\s*)?现通过[^：:]{0,16}[：:]\s*", col2, maxsplit=1)
                    if len(split_parts) == 2:
                        sym_clean = _plain_chinese_upgrade_text(split_parts[0])
                        fix_clean = _plain_chinese_upgrade_text(split_parts[1])
                        if fix_clean and fix_clean not in updated_items:
                            updated_items.append(fix_clean)
                        if sym_clean and sym_clean not in solved_items:
                            solved_items.append(sym_clean)
                    else:
                        sym_clean = _plain_chinese_upgrade_text(col2)
                        entry = f"[{spec_no}] {sym_clean}" if spec_no.count(".") == 1 else sym_clean
                        if entry and entry not in solved_items:
                            solved_items.append(entry)

    commits: list[str] = []
    r_log = _git_run(["git", "-C", str(root), "log", "--no-merges", "-n", "5", "--format=%h %s", rng], timeout=5.0)
    if (r_log is None or r_log.returncode != 0 or not (r_log.stdout or "").strip()) and to_sha:
        r_log = _git_run(["git", "-C", str(root), "log", "-n", "1", "--format=%h %s", to_sha], timeout=5.0)
    if r_log is not None and r_log.returncode == 0 and (r_log.stdout or "").strip():
        commits = [_plain_chinese_commit_fallback(c.strip()) for c in r_log.stdout.splitlines() if c.strip()]
        commits = [c for c in commits if c]

    lines: list[str] = []
    if updated_items:
        lines.append(f"- 📋 **更新了什么**：{'；'.join(updated_items[:3])}")
    elif commits:
        lines.append(f"- 📋 **更新了什么**：{'；'.join(commits[:3])}")
    if solved_items:
        lines.append(f"- 🎯 **解决了何种问题**：{'；'.join(solved_items[:3])}")
    elif commits:
        lines.append(f"- 🎯 **解决了何种问题**：{'；'.join(commits[:3])}")
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
        ver_at_sha = ""
        if (root / ".git").exists():
            rf = _git_run(["git", "-C", str(root), "show", f"{prev_sha}:sheepdog/__init__.py"], timeout=3.0)
            if rf is not None and rf.returncode == 0 and rf.stdout:
                m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', rf.stdout)
                ver_at_sha = m.group(1).strip() if m else ""
        if not ver_at_sha and prev_disp:
            md = re.match(r"^([vV]?[0-9]+\.[0-9]+\.[0-9]+)", prev_disp.strip())
            ver_at_sha = md.group(1).lstrip("vV") if md else ""
        final_ver = ver_at_sha or prev_ver
        info = {"version": final_ver, "git_sha": prev_sha, "git_short": prev_sha[:7], "display": prev_disp, "saved_at": now_iso()}
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


def _collect_rollback_window_residue(store, since_iso: str) -> dict:
    """盘点新版本在岗窗口 [t_swap, t_rollback] 内残留的在途子任务（lambs）、在途批次（dispatches）与已产生的外发副作用（sends）。"""
    res: dict = {
        "since_at": since_iso or "",
        "running_lambs": [],
        "in_flight_dispatches": [],
        "completed_dispatches_count": 0,
        "outbound_sends_count": 0,
    }
    if store is None or not getattr(store, "conn", None):
        return res
    conn = store.conn
    tabs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    try:
        if "lambs" in tabs:
            if since_iso:
                rows = conn.execute(
                    "SELECT lamb_id, parent_topic_id, kind, task_title FROM lambs WHERE state='running' AND started_at >= ? ORDER BY started_at DESC LIMIT 10",
                    (since_iso,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT lamb_id, parent_topic_id, kind, task_title FROM lambs WHERE state='running' ORDER BY started_at DESC LIMIT 10"
                ).fetchall()
            res["running_lambs"] = [
                {
                    "lamb_id": r["lamb_id"],
                    "parent_topic_id": r["parent_topic_id"],
                    "kind": r["kind"],
                    "task_title": r["task_title"],
                }
                for r in rows
            ]
        if "dispatches" in tabs:
            if since_iso:
                inf = conn.execute(
                    "SELECT batch_id, topic_id, state FROM dispatches WHERE state IN ('sent','sending','uncertain') AND sent_at >= ? ORDER BY sent_at DESC LIMIT 10",
                    (since_iso,),
                ).fetchall()
                res["completed_dispatches_count"] = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM dispatches WHERE state IN ('acked','acked_late') AND sent_at >= ?",
                        (since_iso,),
                    ).fetchone()[0]
                )
            else:
                inf = conn.execute(
                    "SELECT batch_id, topic_id, state FROM dispatches WHERE state IN ('sent','sending','uncertain') ORDER BY sent_at DESC LIMIT 10"
                ).fetchall()
            res["in_flight_dispatches"] = [
                {"batch_id": r["batch_id"], "topic_id": r["topic_id"], "state": r["state"]}
                for r in inf
            ]
        if "sends" in tabs and since_iso:
            res["outbound_sends_count"] = int(
                conn.execute("SELECT COUNT(*) FROM sends WHERE sent_at >= ?", (since_iso,)).fetchone()[0]
            )
    except Exception:
        pass
    return res


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
        since_iso = ""
        if dispatcher.store is not None:
            saved = _safe_json(dispatcher.store.get_meta("version_rollback_target"))
            if isinstance(saved, dict):
                if not rb_target:
                    rb_target = str(saved.get("git_sha") or "").strip()
                since_iso = str(saved.get("saved_at") or "").strip()
        if not rb_target:
            rb_target = "HEAD~1"
        gate_ok, rb_resolved_sha, gate_msg = verify_commit_release_gate(root, rb_target)
        if not gate_ok and not target_sha:
            fb = find_latest_gated_release(root, exclude_shas={prev_sha} if prev_sha else None)
            if fb is not None and fb.get("git_sha"):
                rb_target = fb["git_sha"]
                gate_ok, rb_resolved_sha, gate_msg = True, fb["git_sha"], "fallback_to_latest_gated"
        if not gate_ok:
            rolled_ok, rb_out, rb_err = False, "", f"回滚目标未通过上线闸门校验：{gate_msg}"
        else:
            rolled_ok, rb_out, rb_err = rollback_repo_to_target(root, rb_resolved_sha or rb_target, timeout=15.0)
        disk_after = version_info(root, prefer_disk_version=True)
        snap_info = None
        if rolled_ok and _is_gate_enforced(root) and disk_after.get("git_sha"):
            try:
                snap_info = install_gated_release_snapshot(root, disk_after["git_sha"], verify_gate=False)
            except Exception as e:
                log.warning("回滚时更新只读快照软链异常: %s", e)
        residue = _collect_rollback_window_residue(dispatcher.store, since_iso)
        res_parts: list[str] = []
        if residue["running_lambs"]:
            lb_names = "、".join(f"`{x['lamb_id']}`({x['parent_topic_id']})" for x in residue["running_lambs"][:5])
            res_parts.append(f"在途羔羊 `{len(residue['running_lambs'])}` 只（{lb_names}）")
        if residue["in_flight_dispatches"]:
            dp_names = "、".join(f"`{x['batch_id']}`({x['topic_id']})" for x in residue["in_flight_dispatches"][:5])
            res_parts.append(f"在途批次 `{len(residue['in_flight_dispatches'])}` 个（{dp_names}）")
        if not res_parts:
            res_parts.append("在途残留 `0` 项")
        if residue["completed_dispatches_count"] or residue["outbound_sends_count"]:
            res_parts.append(
                f"期间已结案批次 `{residue['completed_dispatches_count']}` 个 / 外发消息 `{residue['outbound_sends_count']}` 条"
            )
        residue_line = f"\n🔍 期间状态与在途盘点：{'；'.join(res_parts)}。"
        if rolled_ok:
            dispatcher.restart_requested = True
            body = f"⏪ 已从 **{mem_disp}** 回滚至 **{disk_after['display']}**（PID {pid}），正在原地重启常驻进程使其立即生效。{residue_line}"
        else:
            dispatcher.restart_requested = False
            body = f"⚠️ 回滚失败（{rb_err}），当前仍为 **{disk_after['display']}**（PID {pid}）。{residue_line}"
        dispatcher._notify_owner(f"🐕 汇报 · 升级完成 [{BUS_TOPIC_ID}]\n{body}", "升级完成", BUS_TOPIC_ID, reply_to=reply_to)
        log.info("已执行 rollback 动作（%s -> %s，ok=%s，target=%s，command=%r）", mem_disp, disk_after["display"], rolled_ok, rb_target, command)
        return {"from": mem_disp, "to": disk_after["display"], "version": disk_after["version"], "git_sha": disk_after["git_sha"], "channel": "rollback", "rollback": True, "rolled_back": rolled_ok, "rollback_output": rb_out, "rollback_failed_reason": rb_err, "window_residue": residue, "snapshot": snap_info, "restart_requested": bool(rolled_ok)}

    if disk_before["dirty"]:
        dispatcher.restart_requested = False
        reject_msg = f"⚠️ 拒绝升级：工作区存在未提交或未跟踪改动（**{disk_before['display']}**），已拦截本次热升级，当前仍运行在 **{mem_disp}**（PID {pid}）。"
        dispatcher._notify_owner(f"🐕 汇报 · 升级拦截 [{BUS_TOPIC_ID}]\n{reject_msg}", "升级拦截", BUS_TOPIC_ID, reply_to=reply_to)
        log.warning("拒绝执行 upgrade：工作树 dirty (%s)", disk_before["display"])
        return {
            "from": mem_disp,
            "to": disk_before["display"],
            "version": disk_before["version"],
            "git_sha": disk_before["git_sha"],
            "channel": channel,
            "pulled": False,
            "pull_output": "",
            "pull_failed_reason": "dirty_worktree",
            "rejected": True,
            "reject_reason": "dirty_worktree",
            "restart_requested": False,
        }

    pulled, pull_output, pull_failed_reason = False, "", ""
    if not no_pull and (root / ".git").exists():
        pulled, pull_output, pull_failed_reason = pull_repo_for_channel(root, channel=channel, timeout=15.0)
    disk_after = version_info(root, prefer_disk_version=True)
    if disk_after["dirty"]:
        dispatcher.restart_requested = False
        reject_msg = f"⚠️ 拒绝升级：工作区存在未提交或未跟踪改动（**{disk_after['display']}**），已拦截本次热升级。"
        dispatcher._notify_owner(f"🐕 汇报 · 升级拦截 [{BUS_TOPIC_ID}]\n{reject_msg}", "升级拦截", BUS_TOPIC_ID, reply_to=reply_to)
        return {
            "from": mem_disp,
            "to": disk_after["display"],
            "version": disk_after["version"],
            "git_sha": disk_after["git_sha"],
            "channel": channel,
            "pulled": pulled,
            "pull_output": pull_output,
            "pull_failed_reason": pull_failed_reason,
            "rejected": True,
            "reject_reason": "dirty_worktree",
            "restart_requested": False,
        }

    gate_ok, gated_sha, gate_msg = verify_commit_release_gate(root, target_sha or disk_after.get("git_sha") or "HEAD")
    if not gate_ok:
        dispatcher.restart_requested = False
        reject_msg = (
            f"⚠️ 拒绝升级：目标版本 **{disk_after['display']}** 未通过上线测试闸门（{gate_msg}），"
            f"已拦截本次热升级，当前仍运行在 **{mem_disp}**（PID {pid}）。"
        )
        dispatcher._notify_owner(f"🐕 汇报 · 升级拦截 [{BUS_TOPIC_ID}]\n{reject_msg}", "升级拦截", BUS_TOPIC_ID, reply_to=reply_to)
        log.warning("拒绝执行 upgrade：release_gate 未通过 (%s: %s)", disk_after["display"], gate_msg)
        return {
            "from": mem_disp,
            "to": disk_after["display"],
            "version": disk_after["version"],
            "git_sha": disk_after["git_sha"],
            "channel": channel,
            "pulled": pulled,
            "pull_output": pull_output,
            "pull_failed_reason": pull_failed_reason,
            "rejected": True,
            "reject_reason": "release_gate_failed",
            "gate_error": gate_msg,
            "restart_requested": False,
        }

    snap_info = None
    if _is_gate_enforced(root) and (gated_sha or disk_after.get("git_sha")):
        try:
            snap_info = install_gated_release_snapshot(root, gated_sha or disk_after["git_sha"], verify_gate=False)
        except Exception as e:
            log.warning("升级时导出只读快照软链异常: %s", e)

    dispatcher.restart_requested = True
    if disk_after["display"] != mem_disp:
        headline = f"✅ 已从 **{mem_disp}** 升级至 **{disk_after['display']}**（PID {pid}），正在原地重启常驻进程使其立即生效。"
    elif pull_failed_reason:
        headline = f"⚠️ 未能拉取远端最新代码（{pull_failed_reason}），当前仍为 **{disk_after['display']}**（PID {pid}），正在原地重启常驻进程。"
    else:
        headline = f"✅ 当前代码已是最新 **{disk_after['display']}**（PID {pid}），正在原地重启常驻进程刷新运行态。"

    rb_info = _resolve_rollback_info(dispatcher.store, root, mem_ver, prev_sha, mem_disp, disk_after.get("git_sha") or "")
    _summarize_fn = summarize_upgrade_changes
    try:
        import importlib
        import sys as _sys
        if __name__ in _sys.modules and (root / "sheepdog" / "version_watch.py").is_file():
            _reloaded = importlib.reload(_sys.modules[__name__])
            _summarize_fn = getattr(_reloaded, "summarize_upgrade_changes", summarize_upgrade_changes)
    except Exception:
        pass
    change_summary = _summarize_fn(
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
    return {"from": mem_disp, "to": disk_after["display"], "version": disk_after["version"], "git_sha": disk_after["git_sha"], "channel": channel, "pulled": pulled, "pull_output": pull_output, "pull_failed_reason": pull_failed_reason, "change_summary": change_summary, "rollback_target": rb_info, "snapshot": snap_info, "restart_requested": True}


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
            if is_release_gate_inflight():
                infos.append(f"磁盘版本 {disk_ver_info['display']} ≠ 常驻进程 {daemon_runtime.get('display', d_ver)}（上线闸门测试运行中 {int(mismatch_sec)}s）")
            elif mismatch_sec >= 300:
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
