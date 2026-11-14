#!/usr/bin/env python3
"""单点发版合入与闸门晋级脚本（REG-168）：

1. 分支开发期间不改 `sheepdog/__init__.py` 的 `__version__`，`tests/REGRESSIONS.md` 新增行统一写 `| NEXT | ... (REG-NEXT) |`；
2. 本脚本在独立集成工作树 `~/.local/state/sheepdog-release-gate/integration-wt` 中执行分支合入、
   自动消解多个分支在 `tests/REGRESSIONS.md` 末尾追加 `NEXT` 行的尾行冲突、
   单点分配 `<next_ver>`（`main` 最新 tag + 1）与 `REG-(N+1)` 并提交候选 commit `<cand_sha>`；
3. 在独立集成工作树中运行 `scripts/release_gate.py <cand_sha>`（3 轮全量单测 + 回归核验 + 隐私扫描 + 账本迁移兼容性检查）；
   若闸门失败，主工作树 `~/Code/sheepdog` 从未被触碰，零污染退出、无需 `reset --hard`；
4. 闸门 3 轮全绿后，主工作树执行 `git merge --ff-only <cand_sha>`、打 tag `v<next_ver>`、
   导出只读发布快照 `~/.local/share/sheepdog-releases/v<next_ver>-<sha7>` 并原子切换 `current` 软链，
   最后推送远端并触发 `sheepdog upgrade --no-pull`。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parent.parent
_VER_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)
_REG_NO_RE = re.compile(r"\bREG-(\d+)\b")
_NEXT_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|(.*)$")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=sheepdog-release", "-c", "user.email=release@sheepdog.local", *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def parse_version_tuple(ver: str) -> tuple[int, ...]:
    s = (ver or "").strip().lstrip("vV")
    parts = s.split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    return ()


def compute_next_version(repo: Path) -> str:
    """从 main 分支最新 v* tag 与 sheepdog/__init__.py 计算下一个语义版本号（patch + 1）。"""
    best = (0, 0, 0)
    r_tags = _git(repo, "tag", "-l", "v*", check=False)
    for ln in (r_tags.stdout or "").splitlines():
        t = parse_version_tuple(ln.strip())
        if t and t > best:
            best = t
    r_init = _git(repo, "show", "main:sheepdog/__init__.py", check=False)
    if r_init.returncode == 0 and r_init.stdout:
        m = _VER_RE.search(r_init.stdout)
        if m:
            t = parse_version_tuple(m.group(1))
            if t and t > best:
                best = t
    if best == (0, 0, 0):
        return "1.0.0"
    return f"{best[0]}.{best[1]}.{best[2] + 1}"


def compute_max_reg_number(reg_text: str) -> int:
    nums = [int(m.group(1)) for m in _REG_NO_RE.finditer(reg_text or "")]
    return max(nums) if nums else 0


def find_worktree_for_commit_or_branch(repo: Path, branch: str) -> Path | None:
    r = _git(repo, "worktree", "list", "--porcelain", check=False)
    if r.returncode != 0:
        return None
    cur_wt: Path | None = None
    want_ref = f"refs/heads/{branch}"
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("worktree "):
            cur_wt = Path(ln.split(" ", 1)[1].strip())
        elif ln.startswith("branch ") and cur_wt is not None:
            if ln.split(" ", 1)[1].strip() == want_ref:
                return cur_wt
        elif not ln.strip():
            cur_wt = None
    return None


def validate_and_extract_branch_changes(
    repo: Path,
    main_sha: str,
    branch_sha: str,
    *,
    allow_no_regression_row: bool = False,
) -> list[str]:
    """校验分支未自行修改 __version__ 或硬编码 REG-XXX，并提取分支新增的 | NEXT | ... | 回归行。"""
    mb_proc = _git(repo, "merge-base", main_sha, branch_sha)
    merge_base = mb_proc.stdout.strip()

    # 1) 禁止分支修改 sheepdog/__init__.py 中的 __version__
    diff_init = _git(repo, "diff", f"{merge_base}..{branch_sha}", "--", "sheepdog/__init__.py")
    for ln in (diff_init.stdout or "").splitlines():
        if (ln.startswith("+") and not ln.startswith("+++")) or (ln.startswith("-") and not ln.startswith("---")):
            if _VER_RE.match(ln[1:].strip()):
                raise RuntimeError(
                    "拒绝合入：分支不得直接修改 sheepdog/__init__.py 的 __version__，版本号由 release_to_main.py 单点分配"
                )

    # 2) 解析分支对 tests/REGRESSIONS.md 的改动
    diff_reg = _git(repo, "diff", "--unified=0", f"{merge_base}..{branch_sha}", "--", "tests/REGRESSIONS.md")
    added_rows: list[str] = []
    for ln in (diff_reg.stdout or "").splitlines():
        if ln.startswith("---") or ln.startswith("+++") or ln.startswith("@@"):
            continue
        if ln.startswith("-"):
            removed = ln[1:].strip()
            if removed.startswith("|"):
                raise RuntimeError(
                    f"拒绝合入：分支不得删除或篡改 tests/REGRESSIONS.md 的存量行：{removed[:80]}"
                )
        elif ln.startswith("+"):
            added = ln[1:].strip()
            if not added:
                continue
            if not added.startswith("|"):
                raise RuntimeError(
                    f"拒绝合入：tests/REGRESSIONS.md 仅允许在末尾追加 | NEXT | ... | 表格行，发现非表格新增行：{added[:80]}"
                )
            m = _NEXT_ROW_RE.match(added)
            col1 = m.group(1).strip() if m else ""
            rest = m.group(2) if m else ""
            if col1 != "NEXT":
                raise RuntimeError(
                    f"拒绝合入：分支在 tests/REGRESSIONS.md 新增行的第一列必须写 NEXT（实际为 {col1!r}）"
                )
            if _REG_NO_RE.search(rest):
                raise RuntimeError(
                    f"拒绝合入：分支在 tests/REGRESSIONS.md 新增行不得硬编码具体 REG-数字，请统一写 REG-NEXT：{added[:100]}"
                )
            added_rows.append(added)

    if not added_rows and not allow_no_regression_row:
        raise RuntimeError(
            "拒绝合入：分支未在 tests/REGRESSIONS.md 末尾登记 | NEXT | ... (REG-NEXT) | 回归测试行"
        )
    return added_rows


def assign_version_and_reg_rows(
    main_reg_text: str,
    added_next_rows: list[str],
    next_ver: str,
) -> tuple[str, list[str]]:
    """将分支新增的 | NEXT | ... (REG-NEXT) | 行替换为正式版本号与递增 REG-XXX 编号，并追加到 main 的 REGRESSIONS.md 末尾。"""
    max_reg = compute_max_reg_number(main_reg_text)
    assigned_regs: list[str] = []
    rendered_rows: list[str] = []
    for raw_row in added_next_rows:
        m = _NEXT_ROW_RE.match(raw_row.strip())
        if not m:
            continue
        rest = m.group(2)
        if "REG-NEXT" in rest:
            def _repl(_m: re.Match) -> str:
                nonlocal max_reg
                max_reg += 1
                reg_id = f"REG-{max_reg:03d}"
                assigned_regs.append(reg_id)
                return reg_id

            rest = re.sub(r"\bREG-NEXT\b", _repl, rest)
        else:
            max_reg += 1
            reg_id = f"REG-{max_reg:03d}"
            assigned_regs.append(reg_id)
            parts = rest.split("|")
            if len(parts) >= 2:
                parts[0] = f" {parts[0].strip()}（{reg_id}） "
                rest = "|".join(parts)
        rendered_rows.append(f"| {next_ver} |{rest}")

    base_text = main_reg_text.rstrip("\n") + "\n"
    if rendered_rows:
        base_text += "\n".join(rendered_rows) + "\n"
    return base_text, assigned_regs


def release_branch_to_main(
    repo: Path,
    branch: str,
    *,
    python: str = sys.executable,
    gate_dir: Path | None = None,
    releases_dir: Path | None = None,
    no_push: bool = False,
    no_upgrade: bool = False,
    allow_no_regression_row: bool = False,
    runs: int = 3,
) -> dict:
    repo = repo.resolve()
    gdir = (
        gate_dir
        or (Path(os.environ["SHEEPDOG_RELEASE_GATE_DIR"]).expanduser() if os.environ.get("SHEEPDOG_RELEASE_GATE_DIR") else Path.home() / ".local" / "state" / "sheepdog-release-gate")
    ).resolve()
    rdir = (
        releases_dir
        or (Path(os.environ["SHEEPDOG_RELEASES_DIR"]).expanduser() if os.environ.get("SHEEPDOG_RELEASES_DIR") else Path.home() / ".local" / "share" / "sheepdog-releases")
    ).resolve()

    # 1) 校验主仓库处于 main 且工作树干净
    cur_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if cur_branch != "main":
        raise RuntimeError(f"主工作树 {repo} 当前分支为 {cur_branch!r}（必须处于 'main'）")
    dirty_main = _git(repo, "status", "--porcelain").stdout.strip()
    if dirty_main:
        raise RuntimeError(f"主工作树 {repo} 存在未提交改动，拒绝发版：\n{dirty_main}")

    branch_wt = find_worktree_for_commit_or_branch(repo, branch)
    if branch_wt is not None and branch_wt.resolve() != repo:
        dirty_br = _git(branch_wt, "status", "--porcelain").stdout.strip()
        if dirty_br:
            raise RuntimeError(f"待发版分支工作树 {branch_wt} 存在未提交改动，请先提交：\n{dirty_br}")

    main_sha = _git(repo, "rev-parse", "--verify", "main^{commit}").stdout.strip()
    branch_sha = _git(repo, "rev-parse", "--verify", f"{branch}^{{commit}}").stdout.strip()
    if main_sha == branch_sha:
        raise RuntimeError(f"分支 {branch} ({branch_sha[:7]}) 与 main 完全相同，无新提交可发布")

    added_next_rows = validate_and_extract_branch_changes(
        repo,
        main_sha,
        branch_sha,
        allow_no_regression_row=allow_no_regression_row,
    )
    next_ver = compute_next_version(repo)
    main_reg_text = _git(repo, "show", f"{main_sha}:tests/REGRESSIONS.md").stdout

    # 2) 在独立集成工作树 integration-wt 中执行合入、版本号与 REG 编号单点写入
    gdir.mkdir(parents=True, exist_ok=True)
    integration_wt = gdir / "integration-wt"
    if integration_wt.exists():
        _git(repo, "worktree", "remove", "--force", str(integration_wt), check=False)
        shutil.rmtree(integration_wt, ignore_errors=True)
    _git(repo, "worktree", "prune", check=False)
    _git(repo, "worktree", "add", "--force", "--detach", str(integration_wt), main_sha)

    try:
        m_proc = _git(integration_wt, "merge", "--no-commit", "--no-ff", branch_sha, check=False)
        if m_proc.returncode != 0:
            # 检查是否仅 tests/REGRESSIONS.md 发生尾行追加冲突
            unmerged = [
                ln.strip()
                for ln in _git(integration_wt, "diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines()
                if ln.strip()
            ]
            if unmerged == ["tests/REGRESSIONS.md"]:
                _git(integration_wt, "checkout", "--ours", "--", "tests/REGRESSIONS.md")
                _git(integration_wt, "add", "tests/REGRESSIONS.md")
            else:
                raise RuntimeError(
                    f"分支 {branch} 合入 main 存在代码冲突（文件：{unmerged}）：\n{(m_proc.stdout or '') + (m_proc.stderr or '')}"
                )

        # 单点更新 tests/REGRESSIONS.md 与 sheepdog/__init__.py
        new_reg_text, assigned_regs = assign_version_and_reg_rows(main_reg_text, added_next_rows, next_ver)
        (integration_wt / "tests" / "REGRESSIONS.md").write_text(new_reg_text, encoding="utf-8")

        init_path = integration_wt / "sheepdog" / "__init__.py"
        init_text = init_path.read_text(encoding="utf-8")
        new_init_text, n_sub = _VER_RE.subn(f'__version__ = "{next_ver}"', init_text, count=1)
        if n_sub == 0:
            raise RuntimeError("sheepdog/__init__.py 中未找到 __version__ 定义")
        init_path.write_text(new_init_text, encoding="utf-8")

        _git(integration_wt, "add", "sheepdog/__init__.py", "tests/REGRESSIONS.md")
        branch_subj = _git(repo, "log", "-n", "1", "--format=%s", branch_sha).stdout.strip()
        branch_body = _git(repo, "log", "-n", "1", "--format=%b", branch_sha).stdout.strip()
        reg_label = " ".join(assigned_regs) if assigned_regs else ""
        commit_title = f"release: v{next_ver}{(' ' + reg_label) if reg_label else ''} — {branch_subj}"
        commit_msg = f"{commit_title}\n\n{branch_body}".strip() if branch_body else commit_title
        _git(integration_wt, "commit", "-m", commit_msg)
        cand_sha = _git(integration_wt, "rev-parse", "HEAD").stdout.strip()

        # 3) 在独立集成工作树内运行 release_gate.py（此期间主目录 repo 停留在旧版，零未过闸窗口）
        sys.path.insert(0, str(integration_wt))
        import importlib.util
        gate_script = integration_wt / "scripts" / "release_gate.py"
        spec = importlib.util.spec_from_file_location("release_gate_mod", gate_script)
        gate_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate_mod)

        env_patch = os.environ.copy()
        env_patch["SHEEPDOG_RELEASE_GATE_DIR"] = str(gdir)
        old_gate_env = os.environ.get("SHEEPDOG_RELEASE_GATE_DIR")
        os.environ["SHEEPDOG_RELEASE_GATE_DIR"] = str(gdir)
        try:
            rc = gate_mod.run_gate(integration_wt, cand_sha, python, runs=runs)
            if rc != 0:
                raise RuntimeError(f"候选提交 {cand_sha[:12]} (v{next_ver}) 未通过 release_gate（退出码 {rc}），主工作树保持 {main_sha[:7]} 未改动")
            if added_next_rows:
                chk_rc = gate_mod.check(integration_wt, cand_sha, runs=runs, specs=[next_ver], python=python)
                if chk_rc != 0:
                    raise RuntimeError(f"候选提交 {cand_sha[:12]} 的规格 {next_ver} 回归核验未通过")
        finally:
            if old_gate_env is None:
                os.environ.pop("SHEEPDOG_RELEASE_GATE_DIR", None)
            else:
                os.environ["SHEEPDOG_RELEASE_GATE_DIR"] = old_gate_env
            if str(integration_wt) in sys.path:
                sys.path.remove(str(integration_wt))

        # 4) 闸门全绿后，主工作树执行毫秒级 merge --ff-only、打 tag、导出只读快照并切换 current 软链
        cur_main_sha = _git(repo, "rev-parse", "--verify", "main^{commit}").stdout.strip()
        if cur_main_sha != main_sha:
            raise RuntimeError(f"跑闸门期间 main 分支已发生变化（{main_sha[:7]} -> {cur_main_sha[:7]}），请重跑 release_to_main.py")
        if _git(repo, "status", "--porcelain").stdout.strip():
            raise RuntimeError(f"跑闸门期间主工作树 {repo} 出现未提交改动，已中止 fast-forward")

        _git(repo, "merge", "--ff-only", cand_sha)
        tag_name = f"v{next_ver}"
        _git(repo, "tag", "-f", tag_name, cand_sha)

        # 导出只读快照并原子更新 current 软链
        added_default_path = False
        if str(REPO_DEFAULT) not in sys.path:
            sys.path.insert(0, str(REPO_DEFAULT))
            added_default_path = True
        try:
            from sheepdog import version_watch as vw
            snap = vw.install_gated_release_snapshot(
                repo,
                cand_sha,
                releases_dir=rdir,
                gate_dir=gdir,
                verify_gate=True,
            )
        finally:
            if added_default_path and str(REPO_DEFAULT) in sys.path:
                sys.path.remove(str(REPO_DEFAULT))
    finally:
        _git(repo, "worktree", "remove", "--force", str(integration_wt), check=False)
        shutil.rmtree(integration_wt, ignore_errors=True)
        _git(repo, "worktree", "prune", check=False)

    pushed = False
    if not no_push:
        p_res = _git(repo, "push", "origin", "main", "--follow-tags", check=False)
        if p_res.returncode != 0:
            raise RuntimeError(f"推送 origin main --follow-tags 失败：\n{(p_res.stdout or '') + (p_res.stderr or '')}")
        pushed = True

    upgraded = False
    if not no_upgrade:
        u_env = os.environ.copy()
        u_env["SHEEPDOG_RELEASE_GATE_DIR"] = str(gdir)
        u_env["SHEEPDOG_RELEASES_DIR"] = str(rdir)
        u_proc = subprocess.run(
            [python, "-m", "sheepdog.cli", "upgrade", "--no-pull"],
            cwd=str(repo),
            env=u_env,
            capture_output=True,
            text=True,
        )
        if u_proc.returncode != 0:
            raise RuntimeError(f"触发 sheepdog upgrade --no-pull 失败：\n{(u_proc.stdout or '') + (u_proc.stderr or '')}")
        upgraded = True

    return {
        "version": next_ver,
        "tag": tag_name,
        "commit": cand_sha,
        "assigned_regs": assigned_regs,
        "snapshot": snap,
        "gate_record": str(gdir / f"{cand_sha}.json"),
        "pushed": pushed,
        "upgraded": upgraded,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="sheepdog 单点合入、自动编号与过闸发布脚本")
    ap.add_argument("--branch", required=True, help="待合入 main 的分支名或提交引用（如 evolve/tp-sheepdog）")
    ap.add_argument("--repo", type=Path, default=REPO_DEFAULT, help="主工作树路径（默认 ~/Code/sheepdog）")
    ap.add_argument("--python", default=sys.executable, help="运行测试与 CLI 的 Python 解释器")
    ap.add_argument("--no-push", action="store_true", help="仅在本地完成合入、打 tag 与快照切换，不 git push")
    ap.add_argument("--no-upgrade", action="store_true", help="不自动调用 sheepdog upgrade --no-pull")
    ap.add_argument("--allow-no-regression-row", action="store_true", help="允许本次变更不新增 REGRESSIONS.md 行")
    args = ap.parse_args(argv)
    try:
        res = release_branch_to_main(
            args.repo,
            args.branch,
            python=args.python,
            no_push=args.no_push,
            no_upgrade=args.no_upgrade,
            allow_no_regression_row=args.allow_no_regression_row,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(f"✗ 发版中止：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
