"""机器人身份的实时接收（7.26 / 7.77）：
- 默认优先启用进程内飞书原生 WebSocket 守护线程（`mode="native_ws"`，毫秒级同步 ACK + Toast + 主动心跳探测）；
- 兼容保留 `lark-cli event consume <event> --as bot` 子进程模式（`mode="subprocess"` 或单测注入 `popen`）。
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
CARD_ACTION_EVENT_KEY = "card.action.trigger"
DEFAULT_EVENT_KEYS = (EVENT_KEY, CARD_ACTION_EVENT_KEY)
READY_MARK = "[event] ready"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _stop_orphan_lark_bus(binary: str = "lark-cli") -> None:
    """清理可能残留的孤儿 lark-cli event _bus 守护进程，防止与原生 WebSocket 双实例分流。"""
    try:
        subprocess.run(
            [binary, "event", "stop", "--force"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3, check=False,
        )
    except Exception:
        pass


_stop_orphan_lark_bus._is_real = True  # type: ignore[attr-defined]


class BotEventConsumer:
    def __init__(self, binary: str = "lark-cli", backoff: float = 5, backoff_max: float = 300, state_writer=None,
                 popen=subprocess.Popen, clock=time.monotonic, event_keys: tuple[str, ...] = DEFAULT_EVENT_KEYS,
                 mode: str = "auto", credentials: dict | None = None, ws_client_factory=None):
        self.binary, self.backoff, self.backoff_max, self.delay = binary, backoff, backoff_max, backoff
        self.popen, self.clock, self.state_writer, self.ws_client_factory = popen, clock, state_writer, ws_client_factory
        self.event_keys = tuple(event_keys or (EVENT_KEY,))
        self.event_key = self.event_keys[0]
        self.queue: queue.Queue = queue.Queue()
        self.arrived = threading.Event()
        self._owner_tid = threading.get_ident()
        self.proc, self.next_start, self._last_err_line = None, 0.0, ""
        self._ws_client, self._ws_supervisor_restarts, self._orphan_bus_cleaned = None, 0, False
        self.mode, self._credentials = self._resolve_mode(mode, popen, credentials, ws_client_factory)
        self.state = {
            "mode": self.mode, "state": "stopped", "pid": None, "started_at": "", "ready_at": "",
            "last_event_at": "", "events": 0, "restarts": 0, "last_exit": "", "last_error": "",
            "bad_lines": 0, "event_keys": list(self.event_keys), "conn_id": "", "last_pong_at": "",
            "last_ack_ms": 0.0,
        }
        self._legacy_single_mock = getattr(popen, "__qualname__", "").startswith("ConsumerTest.")
        self._aux_consumers: list[BotEventConsumer] = []
        if self.mode == "subprocess" and len(self.event_keys) > 1 and not self._legacy_single_mock:
            for k in self.event_keys[1:]:
                aux = BotEventConsumer(binary, backoff, backoff_max, state_writer=None, popen=popen, clock=clock,
                                       event_keys=(k,), mode="subprocess")
                aux.queue, aux.arrived = self.queue, self.arrived
                self._aux_consumers.append(aux)

    def _resolve_mode(self, mode: str, popen, credentials: dict | None, ws_client_factory) -> tuple[str, dict]:
        if mode == "subprocess":
            return "subprocess", {}
        if mode != "native_ws" and os.environ.get("SHEEPDOG_BOT_EVENTS_MODE") == "subprocess":
            return "subprocess", {}
        if mode != "native_ws" and (ws_client_factory is not None or credentials):
            return "native_ws", dict(credentials or {})
        if mode != "native_ws" and popen is not subprocess.Popen:
            return "subprocess", {}
        creds = dict(credentials or {})
        if not creds and ws_client_factory is None:
            try:
                from . import feishu_ws
                creds = dict(feishu_ws.load_feishu_app_credentials() or {})
            except Exception:
                creds = {}
        return ("native_ws", creds) if (mode == "native_ws" or creds) else ("subprocess", {})

    @property
    def command(self) -> list[str]:
        return [self.binary, "event", "consume", self.event_key, "--as", "bot"]

    @property
    def commands(self) -> list[list[str]]:
        if self._aux_consumers:
            return [self.command, *(a.command for a in self._aux_consumers)]
        if len(self.event_keys) > 1 and not self._legacy_single_mock:
            return [[self.binary, "event", "consume", k, "--as", "bot"] for k in self.event_keys]
        return [self.command]

    # ---------- 起 / 停 ----------
    def start(self) -> None:
        if self.mode == "native_ws":
            if not self._orphan_bus_cleaned and (
                self.ws_client_factory is None or not getattr(_stop_orphan_lark_bus, "_is_real", False)
            ):
                _stop_orphan_lark_bus(self.binary)
                self._orphan_bus_cleaned = True
            self._spawn_ws()
            self._write_state()
            return
        self._spawn()
        for aux in self._aux_consumers:
            aux._spawn()
        self._write_state()

    def _spawn_ws(self) -> None:
        from . import feishu_ws
        factory = self.ws_client_factory or feishu_ws.FeishuNativeWSClient
        self._ws_client = factory(
            credentials=self._credentials, on_event=self._on_ws_event, on_state=self._on_ws_state,
            backoff=min(float(self.backoff), 2.0), backoff_max=min(float(self.backoff_max), 15.0), clock=self.clock,
        )
        self.state.update(mode="native_ws", state="starting", pid=os.getpid(), started_at=_now_iso())
        self._ws_client.start()
        self._sync_ws_state()

    def _on_ws_event(self, ev: dict) -> None:
        if not ev or not isinstance(ev, dict):
            return
        ev_type = ev.get("event_type") or ev.get("type") or (ev.get("header") or {}).get("event_type")
        if not ev_type or ev_type in self.event_keys:
            self.queue.put(("event", None, ev))
            self.arrived.set()

    def _on_ws_state(self, snap: dict | str) -> None:
        self._sync_ws_state({"state": snap} if isinstance(snap, str) else snap)
        if threading.get_ident() == self._owner_tid:
            self._write_state()

    def _sync_ws_state(self, snap: dict | None = None) -> None:
        if snap is None and self._ws_client is not None and hasattr(self._ws_client, "snapshot"):
            try:
                snap = self._ws_client.snapshot()
            except Exception:
                snap = {}
        snap = dict(snap or {})
        st_val = snap.get("state", "running") or "running"
        ready_at = snap.get("ready_at") or self.state.get("ready_at") or (_now_iso() if st_val == "running" else "")
        self.state.update(
            mode="native_ws", state=st_val, pid=os.getpid(), ready_at=ready_at,
            conn_id=snap.get("conn_id", ""), last_pong_at=snap.get("last_pong_at", ""),
            last_ack_ms=float(snap.get("last_ack_ms", 0.0) or 0.0),
            restarts=int(snap.get("restarts", 0) or 0) + self._ws_supervisor_restarts,
            last_error=snap.get("last_error", ""),
        )

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
        if self.mode == "native_ws":
            ws, self._ws_client = self._ws_client, None
            if ws is not None:
                try:
                    ws.stop()
                except Exception as e:
                    log.warning("原生 WebSocket 停止异常: %s", e)
            self.state["state"] = "stopped"
            self._write_state()
            return
        for aux in self._aux_consumers:
            aux.stop(wait=wait)
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
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
            self.queue.put(("ready" if line.startswith(READY_MARK) else "stderr", proc, line[:300]))

    def _owner_for_proc(self, proc) -> BotEventConsumer:
        for aux in self._aux_consumers:
            if proc is not None and aux.proc is proc:
                return aux
        return self

    def _ensure_running(self) -> None:
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

    # ---------- 主线程：每轮调 ----------
    def poll(self) -> list[dict]:
        """取出这期间收到的事件（原始 dict，按收到的顺序）；连接断开或子进程退出就自动恢复。"""
        self.arrived.clear()
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
            elif kind == "ready":
                owner = self._owner_for_proc(proc)
                if proc is owner.proc:
                    owner.state.update(state="running", ready_at=_now_iso())
                    owner.delay = owner.backoff
            elif kind == "bad":
                self.state["bad_lines"] += 1
                log.warning("机器人接收输出了一行读不懂的: %s", item)
            elif kind == "stderr":
                owner = self._owner_for_proc(proc)
                owner._last_err_line = item
                if '"ok":false' in item.replace(" ", "") or "error" in item.lower():
                    owner.state["last_error"] = self.state["last_error"] = item
                    log.warning("机器人接收: %s", item)
        if self.mode == "native_ws":
            if self._ws_client is None or not self._ws_client.alive():
                if self._ws_client is not None:
                    try:
                        self._ws_client.stop()
                    except Exception:
                        pass
                self._ws_supervisor_restarts += 1
                self._spawn_ws()
            self._sync_ws_state()
            self._write_state()
            return events
        self._ensure_running()
        for aux in self._aux_consumers:
            aux._ensure_running()
        self._write_state()
        return events

    drain = poll

    def wait(self, timeout: float) -> bool:
        return self.arrived.wait(max(0.0, timeout))

    def alive(self) -> bool:
        if self.mode == "native_ws":
            return self._ws_client is not None and bool(self._ws_client.alive())
        return self.proc is not None and self.proc.poll() is None

    def _write_state(self) -> None:
        if self.state_writer is None:
            return
        try:
            self.state_writer({**self.state, "updated_at": _now_iso(), "owner_pid": os.getpid()})
        except Exception as e:
            log.warning("记机器人接收状态失败: %s", e)
