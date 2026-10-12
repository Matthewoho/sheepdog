"""Claude 宿主（7.18）：每一轮是一次 `claude -p --output-format json`，提示词从标准输入读（提示词文件，读完即 EOF）。

- 新建：`--session-id <新 uuid>` 预先定编号，会话标题用 -n；续接：`--resume <编号>`。
- 模型、推理档位每次用 --model、--effort 指定；`settings` 配置了就原样传 --settings（只是文件名）。
- 输出是事件数组：init 带 session_id，result 带 result / is_error / permission_denials。
- 主人的动作：读 ~/.claude/projects/*/<编号>.jsonl 里 type=user、不是工具结果的条目；另外有交互式
  `claude --resume <编号>` 进程在跑（ps 匹配）也视同主人正在用：Claude 没有写锁，两边同时写会把历史搅乱。
- 桌面版里的会话外部进程驱动不了，这一期只接命令行。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from . import SinkError
from .cli_host import CliHostSink, _now, _text_of


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


_PRINT_FLAG = re.compile(r"(^|\s)(-p|--print)(\s|$)")


class ClaudeSink(CliHostSink):
    host = "claude"
    label = "Claude"

    def _session_exists(self, conversation_id: str) -> bool:
        """会话已经落盘（记录文件在），或者以前有一轮跑成功过。"""
        if self._record_files(conversation_id):
            return True
        return any(r["state"] == "done" for r in self.store.runs_for_conversation(conversation_id))

    def _argv(self, run, inp: Path) -> list[str]:
        argv = [self.hcfg.executable, "-p", "--output-format", "json"]
        # 新建那一轮没跑成（登录失效、出错）时会话并不存在：续接改成用同一个编号新建，免得 --resume 一个不存在的会话
        if run["kind"] == "new" or not self._session_exists(run["conversation_id"]):
            argv += ["--session-id", run["conversation_id"]]
            if run["title"]:
                argv += ["-n", run["title"]]
        else:
            argv += ["--resume", run["conversation_id"]]
        if run["model"]:
            argv += ["--model", run["model"]]
        if run["effort"]:
            argv += ["--effort", run["effort"]]
        if self.hcfg.settings:
            argv += ["--settings", self.hcfg.settings]
        return argv

    def _stdin(self, inp: Path):
        return inp.open("rb")

    def new_conversation(self, title: str, prompt: str, model: str = "", effort: str = "", *,
                         topic_id: str = "", batch_id: str = "") -> str:
        ok, why = self.available()
        if not ok:
            raise SinkError(why)
        cid = str(uuid.uuid4())
        self._start(batch_id or self._new_run_id("n", topic_id), topic_id, cid, "new", prompt, title, model, effort)
        return cid

    def _parse_output(self, text: str) -> dict:
        res = {"ok": False, "answer": "", "errors": [], "denials": [], "conversation_id": ""}
        try:
            data = json.loads(text) if text.strip() else []
        except json.JSONDecodeError:
            res["errors"] = [text.strip()[-1000:]] if text.strip() else []
            return res
        events = data if isinstance(data, list) else [data]
        for e in events:
            if not isinstance(e, dict):
                continue
            if e.get("type") == "system" and e.get("subtype") == "init" and e.get("session_id"):
                res["conversation_id"] = e["session_id"]
            elif e.get("type") == "result":
                answer = e.get("result") if isinstance(e.get("result"), str) else ""
                res["answer"] = answer
                res["conversation_id"] = e.get("session_id") or res["conversation_id"]
                res["denials"] = [d.get("tool_name") or "?" for d in e.get("permission_denials") or []
                                  if isinstance(d, dict)]
                if e.get("is_error"):
                    res["errors"] = [answer or json.dumps(e.get("errors") or e.get("subtype"), ensure_ascii=False)]
                else:
                    res["ok"] = True
        return res

    def _prepare_retry(self, run) -> None:
        # 新建那一轮失败了：会话记录已经落盘就改成续接，否则还用同一个编号新建
        if run["kind"] == "new" and self._record_files(run["conversation_id"]):
            self.store.update_run(run["batch_id"], kind="resume")

    # ---------- 记录文件 ----------
    def _record_files(self, conversation_id: str) -> list[Path]:
        if not conversation_id:
            return []
        root = claude_home() / "projects"
        return sorted(root.glob(f"*/{conversation_id}.jsonl")) if root.is_dir() else []

    def _turns(self, conversation_id: str) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for p in self._record_files(conversation_id):
            for d in self._jsonl(p):
                if d.get("isMeta") or d.get("isSidechain"):
                    continue
                kind = d.get("type")
                content = (d.get("message") or {}).get("content")
                if kind == "user":
                    if isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "tool_result"
                                                         for x in content):
                        continue  # 工具结果不是人说的话
                    out.append(("user", d.get("timestamp") or "", _text_of(content)))
                elif kind == "assistant":
                    text = _text_of(content)
                    if text:
                        out.append(("agent", d.get("timestamp") or "", text))
        return out

    def _user_entries(self, conversation_id: str) -> list[tuple[str, str]]:
        return [(ts, text) for role, ts, text in self._turns(conversation_id) if role == "user"]

    def history(self, conversation_id: str, limit: int = 6) -> list[tuple[str, str, str]]:
        return [("问" if role == "user" else "答", text, ts) for role, ts, text in self._turns(conversation_id)][-limit:]

    def resume_hint(self, conversation_id: str, title: str = "") -> str:
        return f"在 {self.hcfg.workdir} 里跑 `claude --resume {conversation_id}`"

    # ---------- 交互式进程 ----------
    def _ps_lines(self) -> list[tuple[int, str]]:
        try:
            out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return []
        lines = []
        for ln in out.stdout.splitlines():
            pid, _, cmd = ln.strip().partition(" ")
            if pid.isdigit():
                lines.append((int(pid), cmd.strip()))
        return lines

    def interactive_attached(self, conversation_id: str) -> bool:
        """有没有交互式 `claude --resume <编号>`（不是 -p）在跑：主人在终端里接着用这个会话。"""
        if not conversation_id:
            return False
        ours = {r["pid"] for r in self.store.active_runs_for(conversation_id) if r["pid"]}
        pat = re.compile(r"(--resume|-r)[ =]" + re.escape(conversation_id) + r"(\s|$)")
        return any(pid not in ours and pat.search(cmd) and not _PRINT_FLAG.search(cmd)
                   for pid, cmd in self._ps_lines())

    def occupied_at(self, conversation_id: str) -> datetime | None:
        """有交互式 claude --resume 在跑也视同主人正在用（Claude 没有写锁，两边同时写会把历史搅乱）。"""
        if self.interactive_attached(conversation_id):
            return _now()
        return super().occupied_at(conversation_id)
