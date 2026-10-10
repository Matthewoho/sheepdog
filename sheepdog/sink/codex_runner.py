"""Codex 一轮的后台 runner（7.18）：自己起一个 `codex app-server`（JSON-RPC over stdio，每行一个 JSON），跑完一轮就退出。

由 CodexSink 以独立进程组启动：`python -m sheepdog.sink.codex_runner --codex <命令> --cwd <目录> --in <提示词文件> ...`。
标准输出写事件（每行一个 JSON，sheepdog 主循环收尾时读）：
  {"event": "thread", "id": ..., "source": ...}           拿到线程编号（新建时 thread/start 一返回就写）
  {"event": "server_request", "method": ...}             服务端发来的请求（审批等），一律拒绝
  {"event": "error", "message": ..., "will_retry": ...}  error 通知
  {"event": "result", "status": ..., "answer": ..., "thread_id": ..., "errors": [...], "declined": [...]}
调用序列：initialize → initialized →（thread/start{cwd, model, threadSource:"user"} + thread/name/set | thread/resume）
→ turn/start → 等 turn/completed。
thread/start、thread/resume、turn/start 都不带任何沙箱、审批相关的键：权限全部来自 Codex 自己的配置。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from .. import __version__

# 服务端请求 → 拒绝时回的 result（按 app-server 协议各自的格式）；不认识的回 JSON-RPC error
DECLINE = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "applyPatchApproval": {"decision": {"denied": {"rejection": "sheepdog 不替会话批准任何操作"}}},
    "execCommandApproval": {"decision": {"denied": {"rejection": "sheepdog 不替会话批准任何操作"}}},
    "item/permissions/requestApproval": {"permissions": {}},
    "item/tool/requestUserInput": {"answers": {}},
    "mcpServer/elicitation/request": {"action": "decline"},
}


def app_server_argv(codex: str, profile: str = "") -> list[str]:
    """起 app-server 的命令行。-p 是 codex 的全局参数，要放在子命令 app-server 前面；除此之外不传任何参数
    （沙箱、审批、网络都来自 Codex 自己的配置和配置档）。"""
    return [codex, *(["-p", profile] if profile else []), "app-server"]


def thread_start_params(cwd: str, model: str) -> dict:
    # threadSource = "user"：Codex 应用自己建的线程都带这个来源标记，有助于出现在应用侧边栏；它不是权限参数
    p: dict = {"cwd": cwd, "threadSource": "user"}
    if model:
        p["model"] = model
    return p


def thread_resume_params(thread_id: str, model: str) -> dict:
    p: dict = {"threadId": thread_id}
    if model:
        p["model"] = model
    return p


def turn_start_params(thread_id: str, text: str, model: str, effort: str) -> dict:
    p: dict = {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
    if model:
        p["model"] = model
    if effort:
        p["effort"] = effort
    return p


class RpcError(RuntimeError):
    pass


class Runner:
    def __init__(self, proc, emit):
        self.proc = proc
        self.emit = emit
        self.nid = 0
        self.answer = ""
        self.status = ""
        self.errors: list[str] = []
        self.declined: list[str] = []
        # 标了 willRetry 的 error 通知（宿主自己会重连）：这一轮最终失败、又没有别的错误时才拿来分类
        self.retried: list[str] = []
        self.thread_id = ""

    def send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _handle(self, m: dict) -> None:
        """处理一条服务端请求或通知。"""
        if "id" in m and "method" in m:
            method = m["method"]
            self.declined.append(method)
            self.emit({"event": "server_request", "method": method})
            if method in DECLINE:
                self.send({"id": m["id"], "result": DECLINE[method]})
            else:
                self.send({"id": m["id"], "error": {"code": -32601, "message": "sheepdog 不处理这个请求"}})
            return
        method, params = m.get("method"), m.get("params") or {}
        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                self.answer = item["text"]
        elif method == "error":
            err = params.get("error") or {}
            msg = " ".join(str(x) for x in (err.get("message"), err.get("codexErrorInfo"), err.get("additionalDetails"))
                           if x)
            self.emit({"event": "error", "message": msg, "will_retry": bool(params.get("willRetry"))})
            (self.retried if params.get("willRetry") else self.errors).append(msg)
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            self.status = turn.get("status") or "completed"
            if turn.get("error"):
                e = turn["error"]
                self.errors.append(" ".join(str(x) for x in (e.get("message"), e.get("codexErrorInfo")) if x))

    def _read(self) -> dict | None:
        line = self.proc.stdout.readline()
        if not line:
            return None
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            return {}
        return m if isinstance(m, dict) else {}

    def call(self, method: str, params: dict) -> dict:
        self.nid += 1
        rid = self.nid
        self.send({"id": rid, "method": method, "params": params})
        while True:
            m = self._read()
            if m is None:
                raise RpcError(f"{method}: app-server 提前退出")
            if m.get("id") == rid and "method" not in m:
                if "error" in m:
                    e = m["error"] or {}
                    raise RpcError(f"{method}: {e.get('message') or e}")
                return m.get("result") or {}
            if m:
                self._handle(m)

    def wait_turn(self) -> None:
        while not self.status:
            m = self._read()
            if m is None:
                self.errors.append("app-server 在这一轮结束前退出")
                self.status = "failed"
                return
            if m:
                self._handle(m)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sheepdog.sink.codex_runner")
    ap.add_argument("--codex", required=True)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--thread", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--effort", default="")
    ap.add_argument("--profile", default="", help="Codex 配置档名：codex -p <名字> app-server（权限全在那份配置档里）")
    a = ap.parse_args(argv)

    def emit(obj: dict) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    with open(a.inp, encoding="utf-8") as f:
        text = f.read()
    try:
        proc = subprocess.Popen(app_server_argv(a.codex, a.profile), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=None, text=True, bufsize=1, cwd=a.cwd)
    except OSError as e:
        emit({"event": "result", "status": "error", "errors": [f"codex app-server 起不来：{e}"]})
        return 1
    r = Runner(proc, emit)
    try:
        r.call("initialize", {"clientInfo": {"name": "sheepdog", "title": "Sheepdog", "version": __version__}})
        r.send({"method": "initialized"})
        if a.thread:
            r.thread_id = a.thread
            r.call("thread/resume", thread_resume_params(a.thread, a.model))
            emit({"event": "thread", "id": a.thread, "resumed": True})
        else:
            res = r.call("thread/start", thread_start_params(a.cwd, a.model))
            th = res.get("thread") or {}
            r.thread_id = th.get("id") or ""
            if not r.thread_id:
                raise RpcError(f"thread/start 没返回线程编号：{json.dumps(res, ensure_ascii=False)[:300]}")
            emit({"event": "thread", "id": r.thread_id, "source": th.get("source")})
            if a.title:
                try:
                    r.call("thread/name/set", {"threadId": r.thread_id, "name": a.title})
                except RpcError as e:  # 标题设不上不影响这一轮
                    emit({"event": "error", "message": str(e), "will_retry": False})
        r.call("turn/start", turn_start_params(r.thread_id, text, a.model, a.effort))
        r.wait_turn()
    except RpcError as e:
        r.errors.append(str(e))
        r.status = "error"
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if r.status != "completed" and not r.errors:
        r.errors = r.retried[-3:]
    emit({"event": "result", "status": r.status, "answer": r.answer, "thread_id": r.thread_id, "errors": r.errors,
          "declined": r.declined})
    return 0 if r.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
