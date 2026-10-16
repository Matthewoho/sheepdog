#!/usr/bin/env python3
"""上线测试闸门（规格 7.55）：不跑过测试就上不了线，不靠记得。

    python3.12 scripts/release_gate.py <提交号>            # 跑闸门：全绿才写闸门记录
    python3.12 scripts/release_gate.py --check <提交号>    # 上线脚本第一步：记录在、提交号对得上、3 次都绿才退出 0

跑闸门：
1. 用 git archive 把这个提交导出到临时目录（干净副本，不带工作树里没提交的改动）。
2. 在导出副本里把全量测试 `python3.12 -m unittest discover -s tests` 连跑 3 次（顺带抓抖动）。每次一份新副本、一个新的
   临时 HOME：HOME、XDG_*、SHEEPDOG_CONFIG、SHEEPDOG_STATE_DIR、Antigravity / Codex / Claude 的数据目录都指到临时目录，
   PYTHONPATH 只指导出副本，读不到也写不进本机正式的 ~/.config/sheepdog、~/.local/state/sheepdog 和正式代码。
3. 核 tests/REGRESSIONS.md 列的每个测试都还在、会被全量测试跑到、没被跳过（写「缺」的行不核）。
4. 3 次全绿、回归登记也齐，才写闸门记录 <记录目录>/<完整提交号>.json：提交号、3 次各自的测试数和用时、时间。
   有红就打印哪几个红，退出码 1，不写记录。

记录目录默认 ~/.local/state/sheepdog-release-gate/，可用环境变量 SHEEPDOG_RELEASE_GATE_DIR 改（测试用）。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

RUNS = 3
TEST_ARGS = ["-m", "unittest", "discover", "-s", "tests"]
REGRESSIONS = Path("tests") / "REGRESSIONS.md"
# 每次测试最长跑多久（秒）：卡死的测试也要有个头
RUN_TIMEOUT = 1800

_RAN = re.compile(r"^Ran (\d+) tests? in ([0-9.]+)s$", re.M)
_SEP = "=" * 70
_RED_HEAD = re.compile(r"^(FAIL|ERROR|UNEXPECTED SUCCESS): (\S+) \(([^)]+)\)")
_TEST_ID = re.compile(r"`([A-Za-z_][\w]*\.[A-Za-z_][\w]*\.[A-Za-z_][\w]*)`")


def gate_dir() -> Path:
    env = os.environ.get("SHEEPDOG_RELEASE_GATE_DIR")
    return Path(env).expanduser() if env else Path.home() / ".local" / "state" / "sheepdog-release-gate"


def record_path(sha: str) -> Path:
    return gate_dir() / f"{sha}.json"


def default_repo() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve(repo: Path, commit: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
                         capture_output=True, text=True)
    sha = out.stdout.strip()
    if out.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit(f"✗ 在 {repo} 里找不到提交 {commit!r}")
    return sha


def export(repo: Path, sha: str, dest: Path) -> Path:
    """git archive 导出这个提交（只有提交里的文件）。"""
    out = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha], capture_output=True)
    if out.returncode != 0:
        raise SystemExit(f"✗ git archive {sha} 失败：{out.stderr.decode(errors='replace')[:300]}")
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(out.stdout)) as tar:
        tar.extractall(dest, filter="data")
    return dest


def isolated_env(home: Path, src: Path) -> dict[str, str]:
    """测试进程的环境：家目录、配置、状态都指到临时目录；只认导出副本里的代码。"""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SHEEPDOG_", "XDG_", "ANTIGRAVITY_", "CODEX_", "CLAUDE_", "PYTHON"))}
    for d in ("config", "state", "cache", "data"):
        (home / f".{d}").mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".data"),
        "SHEEPDOG_CONFIG": str(home / ".config" / "sheepdog" / "config.toml"),
        "SHEEPDOG_STATE_DIR": str(home / ".state" / "sheepdog"),
        "ANTIGRAVITY_APP_DATA_DIR": str(home / ".gemini" / "antigravity"),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "PYTHONPATH": str(src),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def parse_run(stderr: str) -> dict:
    """unittest 的输出 → 测试数、它自己报的用时、红了哪几个。"""
    m = None
    for m in _RAN.finditer(stderr):
        pass
    tests = int(m.group(1)) if m else 0
    reported = float(m.group(2)) if m else None
    tail = stderr[m.end():] if m else ""
    ok_line = re.search(r"^OK(?: \((.*)\))?$", tail, re.M)
    red = []
    lines = stderr.splitlines()
    for i, ln in enumerate(lines[:-1]):
        if ln == _SEP and (h := _RED_HEAD.match(lines[i + 1])):
            red.append(f"{h.group(1)}: {h.group(3)}")
    return {"tests": tests, "reported_seconds": reported, "ok_line": bool(ok_line),
            "summary": (ok_line.group(0) if ok_line else (tail.strip().splitlines() or ["(没有结果行)"])[-1]),
            "red": red}


def run_once(src: Path, home: Path, python: str, timeout: float = RUN_TIMEOUT) -> dict:
    t0 = time.monotonic()
    try:
        proc = subprocess.run([python, *TEST_ARGS], cwd=src, env=isolated_env(home, src), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return {"tests": 0, "seconds": round(time.monotonic() - t0, 1), "ok": False, "returncode": None,
                "summary": f"超时（{timeout:g} 秒）", "red": ["整轮超时"],
                "stderr_tail": ((e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes)
                                else (e.stderr or ""))[-2000:]}
    secs = round(time.monotonic() - t0, 1)
    r = parse_run(proc.stderr)
    ok = proc.returncode == 0 and r["ok_line"] and r["tests"] > 0 and not r["red"]
    return {"tests": r["tests"], "seconds": secs, "reported_seconds": r["reported_seconds"], "ok": ok,
            "returncode": proc.returncode, "summary": r["summary"], "red": r["red"],
            "stderr_tail": "" if ok else proc.stderr[-4000:]}


# 在导出副本里跑：按全量测试同样的发现方式（discover -s tests）列出所有测试，核 REGRESSIONS.md 里列的每一个
_CHECK_CODE = r"""
import json, re, sys, unittest
ids = json.loads(sys.argv[1])
suite = unittest.defaultTestLoader.discover("tests")
found, broken = {}, []
def walk(s):
    for t in s:
        if isinstance(t, unittest.TestSuite):
            walk(t)
        elif type(t).__name__ == "_FailedTest":
            broken.append(t.id())
        else:
            found[t.id()] = t
walk(suite)
out = {}
for i in ids:
    t = found.get(i)
    if t is None:
        out[i] = "不在全量测试里（改名、删了、或模块导入失败）"
        continue
    meth = getattr(t, t._testMethodName, None)
    if getattr(type(t), "__unittest_skip__", False) or getattr(meth, "__unittest_skip__", False):
        out[i] = "被跳过"
print(json.dumps({"missing": out, "broken_modules": broken, "total": len(found)}))
"""


def parse_regressions(text: str) -> list[dict]:
    """REGRESSIONS.md 的表格行 → [{spec, symptom, tests: [...], missing_guard: bool}]。"""
    rows = []
    for ln in text.splitlines():
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("规格", "") or set(cells[0]) <= set("-: "):
            continue
        guard = "|".join(cells[2:])
        tests = _TEST_ID.findall(guard)
        rows.append({"spec": cells[0], "symptom": cells[1], "tests": tests,
                     "missing_guard": not tests and guard.startswith("缺")})
    return rows


def check_regressions(src: Path, home: Path, python: str) -> dict:
    p = src / REGRESSIONS
    if not p.is_file():
        return {"ok": False, "error": f"{REGRESSIONS} 不在这个提交里", "rows": 0, "tests": 0, "missing": {}}
    rows = parse_regressions(p.read_text(encoding="utf-8"))
    bad_rows = [r for r in rows if not r["tests"] and not r["missing_guard"]]
    ids = sorted({t for r in rows for t in r["tests"]})
    proc = subprocess.run([python, "-c", _CHECK_CODE, json.dumps(ids)], cwd=src, env=isolated_env(home, src),
                          capture_output=True, text=True, timeout=600)
    try:
        res = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": f"核回归登记出错：{proc.stderr[-1000:]}", "rows": len(rows), "tests": len(ids),
                "missing": {}}
    missing = res["missing"]
    errs = []
    if not rows:
        errs.append("REGRESSIONS.md 里一行都没认出来")
    if bad_rows:
        errs.append("这些行既没列测试也没写「缺」：" + "；".join(f"{r['spec']} {r['symptom']}" for r in bad_rows))
    return {"ok": not missing and not errs, "error": "；".join(errs), "rows": len(rows), "tests": len(ids),
            "missing": missing, "no_guard": [f"{r['spec']}：{r['symptom']}" for r in rows if r["missing_guard"]]}


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_gate(repo: Path, commit: str, python: str, runs: int = RUNS, keep: bool = False) -> int:
    sha = resolve(repo, commit)
    print(f"闸门：{sha}（{repo}）")
    work = Path(tempfile.mkdtemp(prefix="sheepdog-gate-"))
    results: list[dict] = []
    try:
        for i in range(1, runs + 1):
            src = export(repo, sha, work / f"src{i}")
            r = run_once(src, work / f"home{i}", python)
            results.append(r)
            mark = "✓" if r["ok"] else "✗"
            print(f"  第 {i} 次 {mark} {r['tests']} 个测试，{r['seconds']} 秒（{r['summary']}）")
            for x in r["red"]:
                print(f"      红：{x}")
        reg = check_regressions(work / "src1", work / "home-reg", python)
        if reg["ok"]:
            print(f"  回归登记 ✓ {reg['rows']} 行、{reg['tests']} 个测试都在")
        else:
            print(f"  回归登记 ✗ {reg.get('error') or ''}")
            for t, why in sorted(reg["missing"].items()):
                print(f"      缺测试：{t}（{why}）")
        for g in reg.get("no_guard", []):
            print(f"  （登记里写「缺」、没有守卫的：{g}）")
        counts = {r["tests"] for r in results}
        same = len(counts) == 1
        if not same:
            print(f"  ✗ 3 次的测试数不一样：{[r['tests'] for r in results]}")
        green = all(r["ok"] for r in results) and len(results) == runs and same and reg["ok"]
        if not green:
            for i, r in enumerate(results, 1):
                if not r["ok"] and r.get("stderr_tail") and not r["red"]:
                    print(f"  第 {i} 次的输出末尾：\n{r['stderr_tail'][-1500:]}")
            print(f"✗ 闸门没过，不写记录（{sha[:12]}）")
            return 1
        record = {
            "commit": sha,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "command": " ".join([Path(python).name, *TEST_ARGS]),
            "python": subprocess.run([python, "-c", "import platform; print(platform.python_version())"],
                                     capture_output=True, text=True).stdout.strip(),
            "host": platform.node(),
            "runs": [{"tests": r["tests"], "seconds": r["seconds"], "reported_seconds": r["reported_seconds"],
                      "ok": r["ok"]} for r in results],
            "regressions": {"rows": reg["rows"], "tests": reg["tests"], "missing": reg["missing"],
                            "no_guard": reg.get("no_guard", [])},
            "gate_script_sha256": sha256(Path(__file__).resolve()),
        }
        dest = record_path(sha)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, dest)
        print(f"✓ 闸门通过，记录写到 {dest}")
        return 0
    finally:
        if keep:
            print(f"  （临时目录留着：{work}）")
        else:
            shutil.rmtree(work, ignore_errors=True)


def check(repo: Path, commit: str, runs: int = RUNS) -> int:
    """上线脚本第一步：这个提交的闸门记录在、对得上、3 次都绿，才退出 0。"""
    try:
        sha = resolve(repo, commit)
    except SystemExit as e:
        print(e)
        return 1
    p = record_path(sha)
    if not p.is_file():
        print(f"✗ {sha[:12]} 没有闸门记录（{p}）：先跑 scripts/release_gate.py {sha}")
        return 1
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"✗ 闸门记录读不了：{p}：{e}")
        return 1
    why = []
    if rec.get("commit") != sha:
        why.append(f"记录里的提交号是 {rec.get('commit')!r}，不是 {sha}")
    rs = rec.get("runs") or []
    if len(rs) != runs:
        why.append(f"记录里跑了 {len(rs)} 次，要 {runs} 次")
    if not all(r.get("ok") is True and isinstance(r.get("tests"), int) and r["tests"] > 0 for r in rs):
        why.append("记录里有没全绿的一次")
    if len({r.get("tests") for r in rs}) > 1:
        why.append("3 次的测试数不一样")
    if (rec.get("regressions") or {}).get("missing"):
        why.append("回归登记里有测试不在")
    if why:
        print(f"✗ {sha[:12]} 的闸门记录对不上：" + "；".join(why))
        return 1
    print(f"✓ {sha[:12]} 过了闸门：{rec.get('created_at')}，"
          + "、".join(f"{r['tests']} 个 {r['seconds']} 秒" for r in rs))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="sheepdog 上线测试闸门（7.55）")
    ap.add_argument("commit", nargs="?", help="要上线的提交号（跑闸门）")
    ap.add_argument("--check", metavar="COMMIT", help="只核闸门记录（上线脚本第一步）")
    ap.add_argument("--repo", type=Path, default=None, help="仓库目录（默认本脚本所在的仓库）")
    ap.add_argument("--python", default="python3.12", help="跑测试用的 Python（默认 python3.12）")
    ap.add_argument("--keep", action="store_true", help="留着临时目录（排查用）")
    a = ap.parse_args(argv)
    repo = (a.repo or default_repo()).resolve()
    if a.check:
        return check(repo, a.check)
    if not a.commit:
        ap.error("要给一个提交号，或者 --check <提交号>")
    if shutil.which(a.python) is None and not Path(a.python).is_file():
        print(f"✗ 找不到 {a.python}")
        return 1
    return run_gate(repo, a.commit, a.python, keep=a.keep)


if __name__ == "__main__":
    sys.exit(main())
