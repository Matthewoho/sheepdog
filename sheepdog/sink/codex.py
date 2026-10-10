"""Codex 宿主（7.18）：每一轮起一个后台 runner（sheepdog.sink.codex_runner），由它和 `codex app-server` 对话。

- 新建：thread/start 返回线程编号（runner 一拿到就写进 .out），sheepdog 最多等 30 秒，等不到算新建失败。
  会话标题用 thread/name/set 设上（app-server 建的线程来源和 Codex 应用里建的一样，应用里能看到）。
- 续接：thread/resume，再 turn/start。模型和推理档位每轮都传（只是默认值，可以逐个会话覆盖）。
- 主人的动作：读线程的 rollout 文件 ~/.codex/sessions/**/rollout-*-<编号>.jsonl（CODEX_HOME 可改根目录）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from ..store import now_iso
from . import SinkError
from .cli_host import CliHostSink, _text_of


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


class CodexSink(CliHostSink):
    host = "codex"
    label = "Codex"

    def __init__(self, hcfg, store, runs_dir):
        super().__init__(hcfg, store, runs_dir)
        self._rollouts: dict[str, Path] = {}

    # ---------- 命令行 ----------
    def _argv(self, run, inp: Path) -> list[str]:
        argv = [sys.executable, "-m", "sheepdog.sink.codex_runner", "--codex", self.hcfg.executable,
                "--cwd", str(self.hcfg.workdir_path), "--in", str(inp)]
        if run["kind"] == "resume":
            argv += ["--thread", run["conversation_id"]]
        elif run["title"]:
            argv += ["--title", run["title"]]
        if run["model"]:
            argv += ["--model", run["model"]]
        if run["effort"]:
            argv += ["--effort", run["effort"]]
        return argv

    def new_conversation(self, title: str, prompt: str, model: str = "", effort: str = "", *,
                         topic_id: str = "", batch_id: str = "") -> str:
        """batch_id：这一轮属于哪个批次（接手前任时的「读完前任上下文」批次），没有就自己生成。"""
        ok, why = self.available()
        if not ok:
            raise SinkError(why)
        run_id = batch_id or self._new_run_id("n", topic_id)
        self._start(run_id, topic_id, "", "new", prompt, title, model, effort)
        _, out, _ = self._paths(run_id)
        deadline = time.time() + self.NEW_WAIT_SECONDS
        while time.time() < deadline:
            tid = self._thread_id(self._read(out))
            if tid:
                self.store.update_run(run_id, conversation_id=tid)
                return tid
            run = self.store.get_run(run_id)
            if not self._alive(run):
                self._procs.pop(run_id, None)
                res = self._finish(run)
                tid = self._thread_id(self._read(out))
                if tid:
                    return tid
                if res.state == "retry_wait":
                    # 线程都没建出来，调用方会当新建失败处理：不能再按计划重试，否则会多出一个没人管的线程
                    self.store.update_run(run_id, state="failed", retry_at=None)
                raise SinkError(f"Codex 新建会话失败（{res.error_kind}）：{res.error[-300:]}")
            time.sleep(0.1)
        run = self.store.get_run(run_id)
        self._kill(run)
        msg = f"{self.NEW_WAIT_SECONDS} 秒内没拿到线程编号"
        self.store.update_run(run_id, state="failed", ended_at=now_iso(), error_kind="other", error=msg)
        raise SinkError(f"Codex 新建会话失败：{msg}")

    @staticmethod
    def _events(text: str):
        for line in (text or "").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                yield d

    def _thread_id(self, text: str) -> str:
        return next((e.get("id") or "" for e in self._events(text) if e.get("event") == "thread" and e.get("id")), "")

    def _parse_output(self, text: str) -> dict:
        res = {"ok": False, "answer": "", "errors": [], "denials": [], "conversation_id": self._thread_id(text)}
        seen_errors = []
        for e in self._events(text):
            if e.get("event") == "error" and not e.get("will_retry"):
                seen_errors.append(e.get("message") or "")
            elif e.get("event") == "result":
                res.update(ok=e.get("status") == "completed", answer=e.get("answer") or "",
                           errors=[x for x in e.get("errors") or [] if x], denials=list(e.get("declined") or []))
                res["conversation_id"] = e.get("thread_id") or res["conversation_id"]
                return res
        res["errors"] = [x for x in seen_errors if x]
        return res

    def _prepare_retry(self, run) -> None:
        if run["kind"] == "new" and run["conversation_id"]:
            self.store.update_run(run["batch_id"], kind="resume")

    # ---------- 记录文件 ----------
    def _record_files(self, conversation_id: str) -> list[Path]:
        if not conversation_id:
            return []
        p = self._rollouts.get(conversation_id)
        if p is not None and p.exists():
            return [p]
        root = codex_home() / "sessions"
        if not root.is_dir():
            return []
        found = sorted(root.rglob(f"rollout-*-{conversation_id}.jsonl"))
        if found:
            self._rollouts[conversation_id] = found[-1]
        return found[-1:]

    def _turns(self, conversation_id: str) -> list[tuple[str, str, str]]:
        """rollout 里的一问一答：(角色 user/agent, 时间, 正文)。

        以 event_msg 的 item_completed 为准：item.type 是 UserMessage / AgentMessage，正文是 content 里各段 text
        拼起来（UserMessage 的段 type 是 "text"，AgentMessage 的是 "Text"，键都是 text）。
        老格式 event_msg 的 user_message / agent_message（exec 建的线程）只在文件里没有 item_completed 时才用，免得重复。
        response_item 里 role=user 的条目混着系统注入的内容（插件推荐、AGENTS.md、环境信息），一律不读。
        """
        items: list[tuple[str, str, str]] = []
        legacy: list[tuple[str, str, str]] = []
        for p in self._record_files(conversation_id):
            for d in self._jsonl(p):
                if d.get("type") != "event_msg":
                    continue
                pl = d.get("payload") or {}
                ts = d.get("timestamp") or ""
                if pl.get("type") == "item_completed":
                    item = pl.get("item") or {}
                    role = {"UserMessage": "user", "AgentMessage": "agent"}.get(item.get("type"))
                    if role:
                        items.append((role, ts, _text_of(item.get("content"), "")))  # 各段直接拼起来
                elif pl.get("type") == "user_message" and isinstance(pl.get("message"), str):
                    legacy.append(("user", ts, pl["message"]))
                elif pl.get("type") == "agent_message" and isinstance(pl.get("message"), str):
                    legacy.append(("agent", ts, pl["message"]))
        return items or legacy

    def transcript_contains(self, conversation_id: str, text: str) -> bool:
        """发送中断恢复：runs 里有、且起过进程就算；否则在 rollout 的用户消息（同 _turns 的口径）里找批次号。"""
        if not text:
            return False
        r = self.store.get_run(text)
        if r is not None and r["pid"]:
            return True
        return any(text in body for role, _, body in self._turns(conversation_id) if role == "user")

    def _user_entries(self, conversation_id: str) -> list[tuple[str, str]]:
        return [(ts, text) for role, ts, text in self._turns(conversation_id) if role == "user"]

    def history(self, conversation_id: str, limit: int = 6) -> list[tuple[str, str, str]]:
        return [("问" if role == "user" else "答", text, ts) for role, ts, text in self._turns(conversation_id)][-limit:]

    def resume_hint(self, conversation_id: str, title: str = "") -> str:
        name = f"「{title}」" if title else ""
        return (f"到 Codex 应用里找同名线程{name}（线程编号 {conversation_id}）；"
                f"应用侧边栏只列登记过的项目下的线程，新建的线程要重开应用才看得到")
