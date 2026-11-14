"""sheepdog：IM 信号 → AI Agent session 的路由与管理引擎。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

__version__ = "1.1.91"


def parse_version_tuple(ver: str) -> tuple[int, ...]:
    """把 '0.7.68' 或 'v0.7.68' 解析成可比较的整数元组 (0, 7, 68)；无法解析返回 ()。"""
    s = (ver or "").strip().lstrip("vV")
    parts = s.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return ()
    return tuple(int(p) for p in parts)


def get_build_info(pkg_dir: Path | None = None) -> dict | None:
    """读取只读发布快照里的 sheepdog/_build_info.py 元数据（不存在时返回 None）。"""
    target = (pkg_dir or Path(__file__).resolve().parent) / "_build_info.py"
    if not target.is_file():
        return None
    ns: dict[str, object] = {}
    try:
        exec(target.read_text(encoding="utf-8"), ns)
    except Exception:
        return None
    git_sha = str(ns.get("GIT_SHA") or "").strip()
    if not git_sha:
        return None
    return {
        "version": str(ns.get("VERSION") or __version__).strip(),
        "git_sha": git_sha,
        "tag": str(ns.get("TAG") or "").strip(),
        "snapshot_dir": str(ns.get("SNAPSHOT_DIR") or target.parent.parent).strip(),
        "repo_dir": str(ns.get("REPO_DIR") or "").strip(),
    }


def default_repo_dir() -> Path:
    env_repo = os.environ.get("SHEEPDOG_REPO_DIR", "").strip()
    if env_repo:
        return Path(env_repo).expanduser().resolve()
    bi = get_build_info()
    if bi and bi.get("repo_dir"):
        cand = Path(bi["repo_dir"]).expanduser()
        if (cand / ".git").exists():
            return cand.resolve()
    return Path(__file__).resolve().parent.parent


def read_disk_version(repo_dir: Path | None = None) -> str:
    """从磁盘 sheepdog/__init__.py 读取当前代码里的 __version__（供常驻进程检测磁盘代码是否已升版但未重启）。"""
    root = (repo_dir or default_repo_dir()).resolve()
    init_py = root / "sheepdog" / "__init__.py"
    try:
        for line in init_py.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("__version__") and "=" in line:
                rhs = line.split("=", 1)[1].strip().strip("\"'")
                if rhs:
                    return rhs
    except OSError:
        pass
    return __version__


def _git_run(args: list[str], cwd: Path | None = None, timeout: float = 3.0) -> subprocess.CompletedProcess | None:
    if type(subprocess.run).__name__ == "FakeLark" or not args:
        return None
    if args[0] == "git":
        cmd = ["git", "-C", str(cwd), *args[1:]] if (cwd is not None and "-C" not in args) else list(args)
    else:
        cmd = ["git", "-C", str(cwd), *args] if cwd is not None else ["git", *args]
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=5")
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except Exception:
        return None


def version_info(repo_dir: Path | None = None, *, prefer_disk_version: bool = False) -> dict:
    """返回版本与 Git 状态字典：
    {
        "version": str,       # 默认内存 __version__；prefer_disk_version=True 时读磁盘 __init__.py
        "git_sha": str,       # 40 位完整 SHA；无 .git 时为 ""
        "git_short": str,     # 7 位短 SHA；无 .git 时为 ""
        "dirty": bool,        # 工作区是否有改动（git status --porcelain 非空）
        "display": str,       # 如 "0.7.68 (c000007)" 或 "0.7.68 (c000007-dirty)"；无 git 时为 "0.7.68"
        "repo_dir": str,
        "snapshot_dir": str,  # 从只读快照运行时记录快照目录，否则为 ""
    }
    """
    root = (repo_dir or default_repo_dir()).resolve()
    bi = get_build_info() if (not prefer_disk_version and repo_dir is None) else None
    if bi is None and not (root / ".git").exists() and (root / "sheepdog" / "_build_info.py").is_file():
        bi = get_build_info(root / "sheepdog")
    if bi is not None:
        ver = read_disk_version(root) if prefer_disk_version else (bi.get("version") or __version__)
        git_sha = str(bi.get("git_sha") or "").strip()
        git_short = git_sha[:7]
        display = f"{ver} ({git_short})" if git_short else ver
        return {
            "version": ver,
            "git_sha": git_sha,
            "git_short": git_short,
            "dirty": False,
            "display": display,
            "repo_dir": str(root),
            "snapshot_dir": str(bi.get("snapshot_dir") or ""),
        }
    ver = read_disk_version(root) if prefer_disk_version else __version__
    git_sha = ""
    git_short = ""
    dirty = False
    if (root / ".git").exists():
        r = _git_run(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=2)
        if r is not None and r.returncode == 0:
            git_sha = (r.stdout or "").strip()
            git_short = git_sha[:7]
        r2 = _git_run(["git", "-C", str(root), "status", "--porcelain"], timeout=2)
        if r2 is not None and r2.returncode == 0:
            dirty = bool((r2.stdout or "").strip())
    if git_short:
        suffix = f"{git_short}-dirty" if dirty else git_short
        display = f"{ver} ({suffix})"
    else:
        display = ver
    return {
        "version": ver,
        "git_sha": git_sha,
        "git_short": git_short,
        "dirty": dirty,
        "display": display,
        "repo_dir": str(root),
        "snapshot_dir": "",
    }
