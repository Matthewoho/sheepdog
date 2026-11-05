#!/usr/bin/env python3
"""提交前隐私与保密红线扫描：拦截真实 IM ID（含拆分拼接绕过）、内部云账号/资源 ID、真实 IP、私有路径、凭据与保密代号进入公开仓库。

用法：
  python3 scripts/check_private_data.py [文件...]   （无参数时扫描 git 暂存区）
  python3 scripts/check_private_data.py --all       （扫描 git 全量跟踪文件）

允许的合成 ID 形如 ou_test_* / oc_test_* / om_test_* / ou_0000* / 200000000001。
可在 ~/.config/sheepdog/private_terms.txt 中追加个人敏感词（每行一个，大小写不敏感，不进仓库）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "Lark open_id": re.compile(r"\bou_(?!test_|0000|1111|2222|aaaa|bbbb|cccc|dddd|eeee|ffff|123456|abcdef)[0-9a-f]{12,}\b"),
    "Lark chat_id": re.compile(r"\boc_(?!test_|0000|1111|2222|aaaa|bbbb|cccc|dddd|eeee|ffff|123456|abcdef)[0-9a-f]{12,}\b"),
    "Lark message_id": re.compile(
        r"\bom_(?!test_|x?0000|x?1111|x?2222|x?aaaa|x?bbbb|x?cccc|x?dddd|x?eeee|x?ffff|x?123456|x?abcdef)x?[0-9a-f]{12,}\b"
    ),
    "Lark app_id": re.compile(r"\bcli_(?!test_|0000)[0-9a-f]{12,}\b"),
    "拆分拼接 IM ID 辅助函数": re.compile(r"\bdef\s+_(?:oid|cid)\s*\("),
    "家目录绝对路径": re.compile(r"/Users/(?!<)[A-Za-z0-9._-]+/"),
    "私钥": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "疑似 Token": re.compile(r"(?i)(token|secret|password)\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"),
    "高危 API 密钥前缀": re.compile(
        r"\b(?:sk-(?!test|proj-test)[A-Za-z0-9_-]{16,}|AKID(?!test|0000)[A-Za-z0-9]{12,}|xox[baprs]-(?!test)[A-Za-z0-9-]{12,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,})\b"
    ),
    "真实子账号初始密码": re.compile(r"KaiStage2026"),
    "企业与真实项目服务账号邮箱": re.compile(
        r"[A-Za-z0-9._%+-]+@(?:tamaredge\.ai|tapnow\.ai|tap-testing-env\.iam\.gserviceaccount\.com|taplabs-453508\.iam\.gserviceaccount\.com|tpn-prod-506507\.iam\.gserviceaccount\.com)"
    ),
    "真实腾讯云 UIN": re.compile(r"\b(?:200053747\d{3}|200045686254|100048360698|100050321779)\b"),
    "真实云资源 ID": re.compile(r"\b(?:cls-gnui5plk|ins-mu1sqp1i|ins-cey0hf6s|zone-3wzk608w22ku|am-gs5l37e19m92e2wff)\b"),
    "真实专线/CLB 公网 IP": re.compile(r"\b(?:192\.220\.\d+\.\d+|103\.118\.244\.142|129\.211\.237\.172|8\.233\.219\.66)\b"),
    "私有知识库与保密代号": re.compile(r"(?i)\b(?:MatthewNote|meru-[a-z0-9_-]+)\b"),
}

SELF = Path(__file__).resolve()


def private_terms() -> list[str]:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    p = Path(base) / "sheepdog" / "private_terms.txt"
    if not p.exists():
        return []
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


def staged_files() -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"], capture_output=True, text=True)
    return [f for f in out.stdout.splitlines() if f]


def all_tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    return [f for f in out.stdout.splitlines() if f]


def scan_text(text: str, terms: list[str] | None = None) -> list[tuple[int, str, str]]:
    """返回 [(行号, 规则名, 摘要)]。"""
    active_terms = terms if terms is not None else private_terms()
    hits: list[tuple[int, str, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        for name, rx in PATTERNS.items():
            if rx.search(line):
                hits.append((i, name, line.strip()[:120]))
        low = line.lower()
        for t in active_terms:
            if t.lower() in low:
                hits.append((i, "个人敏感词", t))
    return hits


def scan_lines(lines: list[str] | str, terms: list[str] | None = None) -> list[tuple[int, str, str]]:
    text = "\n".join(lines) if isinstance(lines, (list, tuple)) else str(lines)
    return scan_text(text, terms)


def main(argv: list[str]) -> int:
    if "--all" in argv:
        files = [a for a in argv if a != "--all"] or all_tracked_files()
    else:
        files = argv or staged_files()
    terms = private_terms()
    bad = 0
    for f in files:
        p = Path(f)
        if not p.is_file() or p.resolve() == SELF:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, name, detail in scan_text(text, terms):
            print(f"✗ {f}:{lineno} [{name}] {detail}")
            bad += 1
    if bad:
        print(f"\n发现 {bad} 处疑似私有或保密数据，已阻止提交。合成数据请使用 *_test_* / 200000000001 等占位符。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
