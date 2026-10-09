#!/usr/bin/env python3
"""提交前隐私扫描：拦截真实 IM ID、私有路径与凭据进入仓库。

用法：python3 scripts/check_private_data.py [文件...]   （无参数时扫描 git 暂存区）
允许的合成 ID 形如 ou_test_* / oc_test_* / om_test_*。
可在 ~/.config/signal-pilot/private_terms.txt 中追加个人敏感词（每行一个，不进仓库）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "Lark open_id": re.compile(r"\bou_(?!test_)[0-9a-f]{20,}\b"),
    "Lark chat_id": re.compile(r"\boc_(?!test_)[0-9a-f]{20,}\b"),
    "Lark message_id": re.compile(r"\bom_(?!test_)x?[0-9a-f]{20,}\b"),
    "Lark app_id": re.compile(r"\bcli_[0-9a-f]{12,}\b"),
    "家目录绝对路径": re.compile(r"/Users/(?!<)[A-Za-z0-9._-]+/"),
    "私钥": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "疑似 Token": re.compile(r"(?i)(token|secret|password)\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"),
}

SELF = Path(__file__).resolve()


def private_terms() -> list[str]:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    p = Path(base) / "signal-pilot" / "private_terms.txt"
    if not p.exists():
        return []
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


def staged_files() -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"], capture_output=True, text=True)
    return [f for f in out.stdout.splitlines() if f]


def main(argv: list[str]) -> int:
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
        for i, line in enumerate(text.splitlines(), 1):
            for name, rx in PATTERNS.items():
                if rx.search(line):
                    print(f"✗ {f}:{i} [{name}] {line.strip()[:120]}")
                    bad += 1
            for t in terms:
                if t in line:
                    print(f"✗ {f}:{i} [个人敏感词] {t}")
                    bad += 1
    if bad:
        print(f"\n发现 {bad} 处疑似私有数据，已阻止提交。合成数据请使用 *_test_* 前缀。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
