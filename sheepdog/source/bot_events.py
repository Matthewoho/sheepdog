"""机器人身份的实时接收（7.26）：sheepdog run 起一个常驻子进程

    lark-cli event consume im.message.receive_v1 --as bot

逐行读它的标准输出（每行一个事件，NDJSON），交给主循环写进账本。

- 读输出在后台线程里做，只往队列里放；写账本只在主线程（Collector 每轮调 poll()）。
- 子进程的标准输入一直开着不写：lark-cli 把标准输入关闭当成「正常退出」的信号（不带 --timeout / --max-events 时）。
- 子进程退出（不管为什么）就按退避重启：从 [identity] restart_backoff_seconds 起每次翻倍，最多
  restart_backoff_max_seconds；连上（标准错误里出现 ready 那一行）以后退避回到起点。
- 状态（在不在、pid、最近一次收到事件的时间、重启次数、最近一次退出的原因）每轮写进账本的 meta，sheepdog doctor 读它。
- 停：关标准输入（lark-cli 正常退出、注销订阅），等一会儿再 SIGTERM；不用 kill -9（会漏掉服务端的订阅清理）。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from datetime import datetime

log = logging.getLogger("sheepdog")

EVENT_KEY = "im.message.receive_v1"
READY_MARK = "[event] ready"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class BotEventConsumer:
    def __init__(self, binary: str = "lark-cli", backoff: float = 5, backoff_max: float = 300, state_writer=None,
                 popen=subprocess.Popen, clock=time.monotonic):
        self.binary = binary
        self.backoff = backoff
        self.backoff_max = backoff_max
        self.delay = backoff
        self.popen = popen
        self.clock = clock
        # 每轮把状态写到哪（state_writer(dict)）：sheepdog run 传进来写 meta；测试里可以不传
        self.state_writer = state_writer
        self.queue: queue.Queue = queue.Queue()
        # 7.45 第三节：收到事件就置位，常驻循环用 wait() 等它（等事件或到点，先到先处理）；poll() 取走之前清掉
        self.arrived = threading.Event()
        self.proc = None
        self.next_start = 0.0
        self.state = {"state": "stopped", "pid": None, "started_at": "", "ready_at": "", "last_event_at": "",
                      "events": 0, "restarts": 0, "last_exit": "", "last_error": "", "bad_lines": 0}
        self._last_err_line = ""

    @property
    def command(self) -> list[str]:
        return [self.binary, "event", "consume", EVENT_KEY, "--as", "bot"]

    # ---------- 起 / 停 ----------
    def start(self) -> None:
        self._spawn()
        self._write_state()

    def _spawn(self) -> None:
        try:
            proc = self.popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, bufsize=1)
        except OSError as e:
            self.proc = None
            self.state.update(state="down", pid=None, last_error=f"起不来: {e}"[:300])
            self._schedule_restart()
            log.warning("机器人接收起不来，%.0f 秒后重试: %s", self.next_start - self.clock(), e)
            return
        self.proc = proc
        self.state.update(state="starting", pid=getattr(proc, "pid", None), started_at=_now_iso(), ready_at="")
        for target, stream in ((self._read_stdout, proc.stdout), (self._read_stderr, proc.stderr)):
            if stream is not None:
                threading.Thread(target=target, args=(proc, stream), daemon=True).start()
        log.info("机器人接收已起（pid %s）：%s", self.state["pid"], " ".join(self.command))

    def _schedule_restart(self) -> None:
        self.next_start = self.clock() + self.delay
        self.delay = min(self.delay * 2, self.backoff_max)

    def stop(self, wait: float = 10) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()  # lark-cli：标准输入关闭 = 正常退出（注销订阅）
            proc.wait(timeout=wait)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=wait)
            except Exception as e:
                log.warning("机器人接收停不下来（pid %s）: %s", getattr(proc, "pid", None), e)
        self.state.update(state="stopped", pid=None)
        self._write_state()

    # ---------- 后台线程：只往队列里放 ----------
    def _read_stdout(self, proc, stream) -> None:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                self.queue.put(("bad", proc, line[:200]))
                continue
            if isinstance(raw, dict):
                self.queue.put(("event", proc, raw))
                self.arrived.set()

    def _read_stderr(self, proc, stream) -> None:
        for line in stream:
            line = line.rstrip()
            if not line:
                continue
            if line.startswith(READY_MARK):
                self.queue.put(("ready", proc, line))
            else:
                self.queue.put(("stderr", proc, line[:300]))

    # ---------- 主线程：每轮调 ----------
    def poll(self) -> list[dict]:
        """取出这期间收到的事件（原始 dict，按收到的顺序）；子进程退出了就按退避重启。"""
        self.arrived.clear()   # 先清再取：清了以后才到的事件会再置位，下一次 wait() 马上返回，不会漏
        events: list[dict] = []
        while True:
            try:
                kind, proc, item = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind == "event":
                events.append(item)
                self.state["events"] += 1
                self.state["last_event_at"] = _now_iso()
            elif kind == "ready" and proc is self.proc:
                self.state.update(state="running", ready_at=_now_iso())
                self.delay = self.backoff  # 连上了：退避回到起点
            elif kind == "bad":
                self.state["bad_lines"] += 1
                log.warning("机器人接收输出了一行读不懂的: %s", item)
            elif kind == "stderr":
                self._last_err_line = item
                if '"ok":false' in item.replace(" ", "") or "error" in item.lower():
                    self.state["last_error"] = item
                    log.warning("机器人接收: %s", item)
        proc = self.proc
        if proc is not None and proc.poll() is not None:
            rc = proc.returncode
            self.state.update(state="down", pid=None,
                              last_exit=f"{_now_iso()} 退出码 {rc}" + (f"：{self._last_err_line}" if self._last_err_line else ""))
            self.proc = None
            self._schedule_restart()
            log.warning("机器人接收退出了（退出码 %s），%.0f 秒后重启", rc, self.next_start - self.clock())
        if self.proc is None and self.clock() >= self.next_start:
            self.state["restarts"] += 1
            self._spawn()
        self._write_state()
        return events

    def wait(self, timeout: float) -> bool:
        """等到收到事件（返回 True）或超时（False）。只等，不取：事件仍由 poll() 取走。"""
        return self.arrived.wait(max(0.0, timeout))

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _write_state(self) -> None:
        if self.state_writer is None:
            return
        try:
            self.state_writer({**self.state, "updated_at": _now_iso(), "owner_pid": os.getpid()})
        except Exception as e:
            log.warning("记机器人接收状态失败: %s", e)
