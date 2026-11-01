"""命令行宿主（Codex / Claude）的公共部分（7.18）：后台运行、收尾、超时、重试、错误分类、重启恢复。

一轮就是一个后台子进程（start_new_session=True，自成进程组），标准输入不接终端；
输入、输出、错误分别在 state_dir/runs/<batch_id>.in / .out / .err，运行记录在账本 runs 表。
主循环每轮调 collect_finished()：收已经结束的运行，按输出内容分类（不靠退出码）：
- 容量满 / 5xx：同一批次 5、10、20 分钟后重试，最多 3 次；还不行记 failed。不换模型。
- 写锁冲突：记 locked，engine 视同主人正在用（occupied_at），批次退回排队。
- 登录失效：整个宿主标为不可用（available() 返回原因），过一阵再放一轮试。
- 其他：failed。
超过 timeout_minutes 的杀掉整个进程组，记 timeout。
牧羊犬重启后，running 的记录：pid 还活着、命令行也对得上就继续等；已经退出的按输出文件收尾；一律不重发。

权限不归牧羊犬管：子类生成的命令行和请求里没有任何沙箱、审批、权限参数（有单测守着）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from ..prompts import RECEIPT_FENCE, is_sheepdog_prompt
from ..store import Store, now_iso
from . import RunResult, SinkError

log = logging.getLogger("sheepdog")

# 容量满后的重试间隔（分钟）：第 1、2、3 次重试
RETRY_MINUTES = (5, 10, 20)
# 登录失效后多久放一轮试试（主人可能已经重新登录）
AUTH_RETRY_MINUTES = 30
# 后台进程没了、既没结果也没报错（在岗表据此判「失联」，7.20）
NO_RESULT = "进程退出，没有结果"
_RECEIPT_RE = re.compile(r"```" + RECEIPT_FENCE + r"[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)

# 错误分类（7.18 第 6 条）：按输出内容认
_AUTH_RE = re.compile(r"logged out|could not be refreshed|not logged in|please run /login", re.I)
_LOCK_RE = re.compile(r"already has an active writer", re.I)
# 5xx 只认常见的几个状态码，避免把「第 512 行」这类数字当成容量问题
_CAPACITY_RE = re.compile(r"at capacity|service unavailable|server ?overloaded|internal ?server ?error|"
                          r"\b(?:500|502|503|504|529)\b", re.I)


def classify_error(text: str) -> str:
    if _AUTH_RE.search(text or ""):
        return "auth"
    if _LOCK_RE.search(text or ""):
        return "locked"
    if _CAPACITY_RE.search(text or ""):
        return "capacity"
    return "other"


def extract_receipt(answer: str) -> tuple[dict | None, str]:
    """从回答里取最后一个 ```sheepdog-receipt 回执块。返回 (回执, 不合格的原因)；没有块时 (None, "")。"""
    blocks = _RECEIPT_RE.findall(answer or "")
    if not blocks:
        return None, ""
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError as e:
        return None, f"回执块不是合法 JSON：{e}"
    if not isinstance(data, dict):
        return None, "回执块必须是 JSON 对象"
    return data, ""


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


def _text_of(content, sep: str = "\n") -> str:
    """消息内容：字符串，或 [{type, text}] 列表（各段 text 用 sep 连起来）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return sep.join(x.get("text", "") for x in content if isinstance(x, dict) and isinstance(x.get("text"), str))
    return ""


def human_messages(entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(时间, 正文) 的用户消息里去掉不算主人的：第一条初始提示词、带牧羊犬批次头的。"""
    return [(ts, text) for i, (ts, text) in enumerate(entries) if i > 0 and not is_sheepdog_prompt(text)]


class CliHostSink:
    """Codex / Claude 公共部分；子类实现 _argv / _stdin / _parse_output / _user_entries / _record_files / history。"""

    host = ""
    label = ""
    # 新建会话时最多等多久拿到会话编号（只有 Codex 需要等）
    NEW_WAIT_SECONDS = 30

    def __init__(self, hcfg, store: Store, runs_dir: Path):
        self.hcfg = hcfg
        self.store = store
        self.runs_dir = runs_dir
        self.name = self.host
        self._procs: dict[str, subprocess.Popen] = {}

    # ---------- 配置 ----------
    def update_config(self, hcfg) -> None:
        """热加载（7.18 第 2 条）：model / effort / timeout_minutes / max_concurrent 下一轮生效。"""
        self.hcfg = hcfg

    # ---------- 可用性 ----------
    def _unavailable(self) -> dict | None:
        raw = self.store.get_meta(f"host_unavailable:{self.host}")
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return None

    def mark_unavailable(self, reason: str) -> None:
        self.store.set_meta(f"host_unavailable:{self.host}",
                            json.dumps({"reason": reason, "since": now_iso()}, ensure_ascii=False))
        log.warning("宿主 %s 标为不可用：%s", self.host, reason)

    def clear_unavailable(self) -> None:
        if self.store.get_meta(f"host_unavailable:{self.host}"):
            self.store.set_meta(f"host_unavailable:{self.host}", "")
            log.info("宿主 %s 恢复可用", self.host)

    def unavailable_reason(self) -> str:
        """登录失效时记下的原因；可用时为空串。engine 据此给主人发「需要你重新登录」（24 小时一次）。"""
        u = self._unavailable()
        return (u.get("reason") or "登录失效") if u else ""

    def available(self) -> tuple[bool, str]:
        exe = self.hcfg.executable
        if not (shutil.which(exe) or Path(exe).expanduser().is_file()):
            return False, f"找不到 {self.label} 命令 {exe!r}"
        if not self.hcfg.workdir_path.is_dir():
            return False, f"[hosts.{self.host}] workdir 不存在：{self.hcfg.workdir}"
        u = self._unavailable()
        if u:
            since = _parse(u.get("since"))
            if since is None or _now() - since < timedelta(minutes=AUTH_RETRY_MINUTES):
                return False, f"{self.label} 登录失效：{u.get('reason', '')}（{AUTH_RETRY_MINUTES} 分钟后再试一轮）"
        return True, "ok"

    def busy(self, conversation_id: str) -> str:
        """能不能现在开一轮：空串 = 可以，否则是排队的原因。"""
        ok, why = self.available()
        if not ok:
            return why
        if conversation_id and self.store.active_runs_for(conversation_id):
            return "这个会话还在跑上一轮"
        running = len(self.store.runs_in_state(self.host, "running"))
        if running >= self.hcfg.max_concurrent:
            return f"{self.label} 同时在跑 {running} 轮，已到上限 max_concurrent = {self.hcfg.max_concurrent}"
        return ""

    def run_active(self, batch_id: str) -> bool:
        r = self.store.get_run(batch_id)
        return bool(r and r["state"] in Store.RUN_ACTIVE)

    # ---------- 投递 ----------
    def _new_run_id(self, prefix: str, topic_id: str) -> str:
        return f"{prefix}{_now().strftime('%Y%m%d%H%M%S')}-{(topic_id or 'x').removeprefix('tp_')}-{uuid.uuid4().hex[:6]}"

    def _paths(self, run_id: str) -> tuple[Path, Path, Path]:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        return (self.runs_dir / f"{run_id}.in", self.runs_dir / f"{run_id}.out", self.runs_dir / f"{run_id}.err")

    def _start(self, run_id: str, topic_id: str, conversation_id: str, kind: str, prompt: str, title: str,
               model: str, effort: str) -> None:
        inp, _, _ = self._paths(run_id)
        inp.write_text(prompt, encoding="utf-8")
        self.store.add_run(run_id, topic_id, self.host, conversation_id, kind, title, model, effort)
        self._launch(self.store.get_run(run_id))

    def _launch(self, run) -> None:
        inp, out, err = self._paths(run["batch_id"])
        argv = self._argv(run, inp)
        repo_dir = Path(__file__).resolve().parent.parent.parent
        env = os.environ.copy()
        existing_pp = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{repo_dir}{os.pathsep}{existing_pp}" if existing_pp else str(repo_dir)
        try:
            with out.open("wb") as fo, err.open("wb") as fe, self._stdin(inp) as fi:
                p = subprocess.Popen(argv, cwd=str(self.hcfg.workdir_path), stdin=fi, stdout=fo, stderr=fe,
                                     start_new_session=True, env=env)
        except OSError as e:
            self.store.update_run(run["batch_id"], state="failed", ended_at=now_iso(), error_kind="other",
                                  error=f"起不来：{e}")
            raise SinkError(f"{self.label} 起不来：{e}") from e
        self._procs[run["batch_id"]] = p
        self.store.update_run(run["batch_id"], pid=p.pid, cmd_fingerprint=" ".join(argv[1:]), state="running",
                              started_at=now_iso(), attempts=(run["attempts"] or 0) + 1, retry_at=None)
        log.info("%s 开始一轮 %s（%s %s，pid %s）", self.label, run["batch_id"], run["kind"],
                 run["conversation_id"] or "新会话", p.pid)

    def _stdin(self, inp: Path):
        return open(os.devnull, "rb")

    def send_message(self, conversation_id: str, content: str, *, batch_id: str = "", topic_id: str = "",
                     model: str = "", effort: str = "") -> None:
        why = self.busy(conversation_id)
        if why:
            raise SinkError(why)
        self._start(batch_id or self._new_run_id("x", topic_id), topic_id, conversation_id, "resume", content, "",
                    model, effort)

    # ---------- 进程 ----------
    def _ps_command(self, pid: int) -> str:
        try:
            out = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=10)
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip()

    def _alive(self, run) -> bool:
        p = self._procs.get(run["batch_id"])
        if p is not None:
            return p.poll() is None
        pid = run["pid"]
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        # 重启后不是自己的子进程：pid 可能被别的进程复用，命令行对得上才算
        fp = run["cmd_fingerprint"] or ""
        return bool(fp) and fp in self._ps_command(pid)

    def _kill(self, run) -> None:
        """杀掉整个进程组：先 SIGTERM，3 秒没退再 SIGKILL。"""
        pid = run["pid"]
        if pid:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                deadline = time.time() + 3
                while time.time() < deadline and self._alive(run):
                    time.sleep(0.1)
                if not self._alive(run):
                    break
        p = self._procs.pop(run["batch_id"], None)
        if p is not None:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    # ---------- 收尾 ----------
    def collect_finished(self) -> list[RunResult]:
        out: list[RunResult] = []
        now = _now()
        for r in self.store.runs_in_state(self.host, "running"):
            if self._alive(r):
                started = _parse(r["started_at"])
                if started and now - started > timedelta(minutes=self.hcfg.timeout_minutes):
                    self._kill(r)
                    msg = f"超过 {self.hcfg.timeout_minutes:g} 分钟没跑完，已杀掉整个进程组"
                    self.store.update_run(r["batch_id"], state="timeout", ended_at=now_iso(), error_kind="timeout",
                                          error=msg)
                    log.warning("%s 一轮 %s 超时：%s", self.label, r["batch_id"], msg)
                    out.append(self._result(r, "timeout", "timeout", msg))
                continue
            self._procs.pop(r["batch_id"], None)
            out.append(self._finish(r))
        for r in self.store.runs_in_state(self.host, "retry_wait"):
            due = _parse(r["retry_at"])
            if due and due > now:
                continue
            why = self.busy("")  # 只看宿主可用和并发上限；这一轮本身就是这个会话唯一的一轮
            if why:
                log.info("%s 一轮 %s 到了重试时间，但 %s，继续等", self.label, r["batch_id"], why)
                continue
            try:
                self._prepare_retry(r)
                self._launch(self.store.get_run(r["batch_id"]))
            except SinkError as e:
                out.append(self._result(self.store.get_run(r["batch_id"]), "failed", "other", str(e)))
        return out

    def _prepare_retry(self, run) -> None:
        """重试前的准备：子类可以把 new 改成 resume（会话已经建好了）。"""

    def _result(self, run, state: str, kind: str = "", error: str = "", answer: str = "",
                denials: list[str] | None = None) -> RunResult:
        return RunResult(batch_id=run["batch_id"], topic_id=run["topic_id"], host=self.host,
                         conversation_id=run["conversation_id"] or "", state=state, error_kind=kind, error=error,
                         answer=answer, denials=list(denials or []))

    def _read(self, p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def _finish(self, run) -> RunResult:
        _, out, err = self._paths(run["batch_id"])
        parsed = self._parse_output(self._read(out))
        err_text = self._read(err)[-4000:]
        cid = parsed.get("conversation_id") or run["conversation_id"] or ""
        denials = parsed.get("denials") or []
        fields = dict(ended_at=now_iso(), conversation_id=cid or None,
                      denials=json.dumps(denials, ensure_ascii=False) if denials else None,
                      answer=parsed.get("answer") or None)
        run = {**dict(run), "conversation_id": cid}
        if denials:
            log.warning("%s 一轮 %s 有被拒的工具 / 请求：%s", self.label, run["batch_id"], ", ".join(denials))
        if parsed.get("ok"):
            self.store.update_run(run["batch_id"], state="done", error=None, error_kind=None, **fields)
            self.clear_unavailable()
            return self._result(run, "done", answer=parsed.get("answer") or "", denials=denials)
        errors = "\n".join(parsed.get("errors") or []) or err_text.strip() or NO_RESULT
        kind = parsed.get("error_kind") or classify_error(errors + "\n" + err_text)
        attempts = run["attempts"] or 1
        if kind == "capacity" and attempts <= len(RETRY_MINUTES):
            wait = RETRY_MINUTES[attempts - 1]
            retry_at = (_now() + timedelta(minutes=wait)).isoformat(timespec="seconds")
            self.store.update_run(run["batch_id"], state="retry_wait", retry_at=retry_at, error_kind=kind,
                                  error=errors[-1000:], **fields)
            log.warning("%s 一轮 %s 容量满（第 %d 次），%d 分钟后重试：%s", self.label, run["batch_id"], attempts, wait,
                        errors[-200:])
            return self._result(run, "retry_wait", kind, errors[-1000:], denials=denials)
        state = "locked" if kind == "locked" else "failed"
        if kind == "auth":
            self.mark_unavailable(errors.strip().splitlines()[-1][:200] if errors.strip() else "登录失效")
        self.store.update_run(run["batch_id"], state=state, error_kind=kind, error=errors[-1000:], **fields)
        log.warning("%s 一轮 %s %s（%s）：%s", self.label, run["batch_id"], state, kind, errors[-300:])
        return self._result(run, state, kind, errors[-1000:], parsed.get("answer") or "", denials)

    # ---------- 主人的动作（7.18 第 7 条） ----------
    def lock_streak_since(self, conversation_id: str) -> datetime | None:
        """这个会话最近连续写锁冲突的起点；最近一轮不是 locked 时为 None。"""
        since = None
        for r in self.store.runs_for_conversation(conversation_id):
            if r["state"] != "locked":
                break
            since = _parse(r["started_at"]) or since
        return since

    def occupied_at(self, conversation_id: str) -> datetime | None:
        """会话被别处占着的最近时间：最近一轮撞上写锁（应用开着它），视同主人正在用；不是就 None。
        主人在会话里说话不在这里算（engine 看在岗表卡片上的 human_at，由 human_message_at 写进去）。"""
        for r in self.store.runs_for_conversation(conversation_id, limit=1):
            if r["state"] == "locked":
                return _parse(r["ended_at"]) or _parse(r["started_at"])
        return None

    def human_message_at(self, conversation_id: str) -> datetime | None:
        """主人最后一次在这个会话里说话的时间（7.18 第 7 条：去掉第一条初始提示词和带牧羊犬批次头的）。
        在岗表扫到记录里来了新的用户消息时调它，结果写进卡片的 human_at；投递要不要暂停看的就是那个值。"""
        times = [_parse(ts) for ts, _ in human_messages(self._user_entries(conversation_id))]
        times = [t for t in times if t is not None]
        return max(times) if times else None

    def transcript_contains(self, conversation_id: str, text: str) -> bool:
        """发送中断恢复（7.16 第 2 条）：这个批次是不是已经交给宿主了。runs 里有、且起过进程就算；否则查宿主的记录文件。"""
        if not text:
            return False
        r = self.store.get_run(text)
        if r is not None and r["pid"]:
            return True
        for p in self._record_files(conversation_id):
            try:
                with p.open(encoding="utf-8", errors="replace") as f:
                    if any(text in line for line in f):
                        return True
            except OSError:
                continue
        return False

    # ---------- 子类 ----------
    def _argv(self, run, inp: Path) -> list[str]:
        raise NotImplementedError

    def _parse_output(self, text: str) -> dict:
        """返回 {ok, answer, errors, denials, conversation_id}。"""
        raise NotImplementedError

    def _record_files(self, conversation_id: str) -> list[Path]:
        raise NotImplementedError

    def record_files(self, conversation_id: str) -> list[Path]:
        """这个会话在宿主那边的记录文件（接手会话读前任上下文用）。"""
        return self._record_files(conversation_id)

    def _user_entries(self, conversation_id: str) -> list[tuple[str, str]]:
        raise NotImplementedError

    @staticmethod
    def _jsonl(p: Path):
        try:
            with p.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(d, dict):
                        yield d
        except OSError:
            return
