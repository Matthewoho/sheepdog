"""在岗表（7.20）/ attention 自动恢复（7.21）场景测试用的模拟环境。

真实的 Collector、Dispatcher、Store、playbook；宿主是 DryRunSink 的子类（记下投了什么、可以模拟发送失败，不打印、
不调 agentapi）；给主人的通知走 [notify] file 的 FileNotifier，写进临时目录里的文件，再读回来检查。
会话记录是临时目录里合成的 Antigravity transcript（格式照 sheepdog/guard.py 的解析：PLANNER_RESPONSE 带
tool_calls、GENERIC 且 source=MODEL 是工具输出、USER_INPUT 且 step_index 不为 0 是主人说话、ERROR_MESSAGE、
SYSTEM_MESSAGE、CHECKPOINT 以「# Resuming from a compaction」开头）。

时间：SimClock 替换 engine / store / duty / security / cli / notify 里的 datetime，datetime.now() 返回模拟时间；
会话记录每写一行就把文件 mtime 设成模拟时间（失联看的就是 mtime）。一轮 = 时间往前走 → 拉消息 → 投递，
和 sheepdog run 的主循环一样（先睡、再拉、再投）。

数据全是合成的：ou_test_* / oc_test_* / om_test_* / conv_test_*，没有真实的人名和编号。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sheepdog import duty
from sheepdog import session as sm
from sheepdog.config import Config, RoutingConfig
from sheepdog.engine import BUS_TOPIC_ID, Collector, Dispatcher, write_receipt
from sheepdog.models import Mention, Message
from sheepdog.notify import FileNotifier, read_notices
from sheepdog.roster import parse_roster
from sheepdog.sink import SinkError
from sheepdog.sink.agentapi import AgentApiSink, DryRunSink, transcript_contains, transcript_path
from sheepdog.source import NullSource
from sheepdog.store import Store

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "playbook"
OWNER = "ou_test_owner"
# 2026-10-12 09:00 北京时间开始（固定起点：批次号、时间戳每次都一样）
START = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)
BATCH_RE = re.compile(r"新信号批次 `([^`]+)`")
CLOCK_MODULES = ("sheepdog.engine", "sheepdog.store", "sheepdog.duty", "sheepdog.security", "sheepdog.cli",
                 "sheepdog.notify")


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SimClock:
    def __init__(self, start: datetime = START):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, minutes: float = 0, seconds: float = 0) -> None:
        self.t += timedelta(minutes=minutes, seconds=seconds)

    def datetime_class(self):
        clock = self

        class SimDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is not None:
                    return clock.t.astimezone(tz)
                return clock.t.astimezone().replace(tzinfo=None)  # 同 datetime.now()：本地时间、不带时区

        return SimDatetime


# ---------- 合成的 Antigravity 记录行 ----------
def planner(at: datetime, step: int, summary: str = "", cmd: str = "", tool: str = "") -> dict:
    """模型的回答：有 summary / cmd 就带一个工具调用（args 都是 JSON 编码过的字符串）。"""
    d = {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": utc(at),
         "thinking": "SIM-THINKING（在岗表不读）", "content": "SIM-ANSWER"}
    if summary or cmd or tool:
        name = tool or ("run_command" if cmd else "view_file")
        args = {}
        if summary:
            args["toolSummary"] = json.dumps(summary)
            args["toolAction"] = json.dumps(summary + "ing")
        if cmd:
            args["CommandLine"] = json.dumps(cmd)
            args["Cwd"] = json.dumps("/tmp/sim")
        d["tool_calls"] = [{"name": name, "args": args}]
    return d


def generic(at: datetime, step: int) -> dict:
    return {"step_index": step, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": utc(at),
            "content": "SIM-TOOL-OUTPUT"}


def system_message(at: datetime, step: int, text: str) -> dict:
    return {"step_index": step, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE", "created_at": utc(at),
            "content": text}


def error_message(at: datetime, step: int) -> dict:
    return {"step_index": step, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "DONE", "created_at": utc(at),
            "content": "SIM network error: connection reset"}


def user_input(at: datetime, step: int, text: str) -> dict:
    return {"step_index": step, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
            "created_at": utc(at), "content": text}


def checkpoint(at: datetime, step: int) -> dict:
    return {"step_index": step, "source": "SYSTEM", "type": "CHECKPOINT", "status": "DONE", "created_at": utc(at),
            "content": "# Resuming from a compaction\n\nSIM summary ..."}


class SimSession:
    """一个 Antigravity 会话的「真相」：记录文件、最后一次动作、主人什么时候说过话、收到过哪些批次。"""

    def __init__(self, world: "World", topic_id: str, cid: str):
        self.world, self.topic_id, self.cid = world, topic_id, cid
        self.path = transcript_path(cid)
        self.step = 0
        self.app_alive = True          # App 活着：投递会在记录里写一行 SYSTEM_MESSAGE（文件在动）
        self.net_down = False          # 断网：收到批次只写 SYSTEM_MESSAGE + ERROR_MESSAGE，不算动作
        self.last_action: datetime | None = None
        self.last_write: datetime | None = None
        self.owner_spoke: datetime | None = None
        self.batches: list[str] = []   # 收到过的批次（按收到顺序）
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.write(user_input(world.clock.t - timedelta(hours=3), 0, "SIM 开场提示词"))

    def write(self, *rows: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        now = self.world.clock.t
        os.utime(self.path, (now.timestamp(), now.timestamp()))
        self.last_write = now

    def next_step(self) -> int:
        self.step += 1
        return self.step

    # 会话自己的动作
    def act(self, summary: str = "Read the playbook", cmd: str = "", with_output: bool = True) -> None:
        now = self.world.clock.t
        rows = [planner(now, self.next_step(), summary, cmd)]
        if with_output:
            rows.append(generic(now, self.next_step()))
        self.write(*rows)
        self.last_action = now

    def answer_only(self) -> None:
        """只回答、不调工具：算动作，但不改「最近一次工具调用」。"""
        now = self.world.clock.t
        self.write(planner(now, self.next_step()))
        self.last_action = now

    def error(self) -> None:
        self.write(error_message(self.world.clock.t, self.next_step()))

    def owner_says(self, text: str = "我来看一下") -> None:
        now = self.world.clock.t
        self.write(user_input(now, self.next_step(), text))
        self.owner_spoke = now

    def compact(self) -> None:
        self.write(checkpoint(self.world.clock.t, self.next_step()))

    def receipt(self, batch: str | None = None, status: str = "handled", summary: str = "SIM 处理完了",
                **extra) -> str:
        """交回执：默认交 topics 里挂着的那一批。"""
        bid = batch or self.world.store.get_topic(self.topic_id)["pending_batch_id"]
        assert bid, f"{self.topic_id} 没有在等回执的批次"
        p = write_receipt(self.world.cfg, self.topic_id, bid, {"status": status, "summary": summary, **extra})
        now = self.world.clock.t.timestamp()
        os.utime(p, (now, now))  # 回执文件的 mtime 也按模拟时间（保留期清理按 mtime 删）
        return bid

    # sink 投递进来
    def on_delivered(self, batch: str | None, content: str) -> None:
        if batch:
            self.batches.append(batch)
        if not self.app_alive:
            return
        now = self.world.clock.t
        head = content.splitlines()[0] if content else ""
        rows = [system_message(now, self.next_step(), head)]
        if self.net_down:
            rows.append(error_message(now, self.next_step()))
        self.write(*rows)


class SimSink(DryRunSink):
    """DryRunSink：不调 agentapi。记下每次投递（批次号从正文认），按需要模拟发送失败；
    主人有没有在会话里说话：real_human_reader=True 时用 AgentApiSink 的真读法读合成的记录，否则用会话的真相（快）。"""

    def __init__(self, world: "World", real_human_reader: bool = True):
        super().__init__("antigravity")
        self.world = world
        self.real_human_reader = real_human_reader
        self.fail: set[str] = set()
        self.deliveries: list[dict] = []
        self.sends = 0
        self.streak: dict[str, int] = {}   # 每个会话连续发送失败几次（发成功清零）

    def send_message(self, conversation_id: str, content: str, **_ctx) -> None:
        self.sends += 1
        if conversation_id in self.fail:
            self.streak[conversation_id] = self.streak.get(conversation_id, 0) + 1
            raise SinkError("agentapi send-message 失败（模拟）")
        self.streak[conversation_id] = 0
        m = BATCH_RE.search(content)
        bid = m.group(1) if m else None
        self.deliveries.append({"at": self.world.clock.t, "cid": conversation_id, "batch": bid, "content": content})
        s = self.world.by_cid.get(conversation_id)
        if s is not None:
            s.on_delivered(bid, content)

    def new_conversation(self, title: str, prompt: str, model: str = "", effort: str = "", **_ctx) -> str:
        raise AssertionError("场景里的会话都是现成的，不该新建")

    def transcript_contains(self, conversation_id: str, text: str) -> bool:
        return transcript_contains(conversation_id, text)

    def last_human_activity(self, conversation_id: str):
        if self.real_human_reader:
            return AgentApiSink().last_human_activity(conversation_id)
        s = self.world.by_cid.get(conversation_id)
        return s.owner_spoke if s is not None else None

    def to(self, cid: str) -> list[dict]:
        return [d for d in self.deliveries if d["cid"] == cid]


def roster_for(keys: list[str]) -> dict:
    return {"session": [
        {"key": k, "mode": "managed", "conversation_id": f"conv_test_{k}", "title": f"会话{k}",
         "duty": f"负责 {k} 的事", "chats": [{"chat_id": f"oc_test_{k}_p2p", "name": f"{k} 私聊", "all_messages": True},
                                            {"chat_id": f"oc_test_{k}_grp", "name": f"{k} 群", "all_messages": False}]}
        for k in keys]}


class World:
    """一套隔离的环境：临时目录做 state_dir、Antigravity 数据目录、playbook、通知文件；真实的 Collector / Dispatcher。"""

    def __init__(self, keys: list[str] = ("alpha", "beta"), real_human_reader: bool = True,
                 start: datetime = START, fast_db: bool = False, cfg_hook=None):
        self.tmp = tempfile.TemporaryDirectory(prefix="sheepdog-sim-")
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.clock = SimClock(start)
        sim_dt = self.clock.datetime_class()
        for mod in CLOCK_MODULES:
            self.stack.enter_context(mock.patch(f"{mod}.datetime", sim_dt))
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "ANTIGRAVITY_APP_DATA_DIR": str(self.root / "app"),
            "SHEEPDOG_CONFIG": str(self.root / "config" / "config.toml"),
            "SHEEPDOG_STATE_DIR": str(self.root / "state")}))
        pdir = self.root / "config" / "playbook"
        shutil.copytree(FIXTURES, pdir)
        self.cfg = Config(self_open_id=OWNER, state_dir=self.root / "state")
        self.cfg.config_path = self.root / "config" / "config.toml"
        self.cfg.playbook_dir = str(pdir)
        self.cfg.routing = RoutingConfig(keywords=["故障"])
        self.cfg.source.enabled = False
        self.cfg.notify.file = str(self.root / "notices.log")
        self.cfg.receipts_dir.mkdir(parents=True, exist_ok=True)
        if cfg_hook is not None:
            cfg_hook(self.cfg)
        self.store = Store(self.cfg.db_path)
        if fast_db:
            self.store.conn.execute("PRAGMA synchronous=OFF")
        self.keys = list(keys)
        self.roster = parse_roster(roster_for(self.keys))
        self.sink = SimSink(self, real_human_reader)
        self.notifier = FileNotifier(self.cfg.notify_path, self.cfg.timezone_offset)
        self.d = Dispatcher(self.cfg, self.store, {"antigravity": self.sink}, self.roster, notifier=self.notifier)
        self.collector = Collector(self.cfg, self.store, NullSource(), self.roster)
        self.msg_n = 0
        # 名册同步、总线已有会话；各会话都登记过、规则是最新的（不打印开场）
        self.d.sync_roster()
        self.d.ensure_bus_topic()
        self.store.update_topic(BUS_TOPIC_ID, conversation_id="conv_test_bus")
        self.sessions: dict[str, SimSession] = {}
        for tid, cid in [(BUS_TOPIC_ID, "conv_test_bus")] + [(f"tp_{k}", f"conv_test_{k}") for k in self.keys]:
            t = self.store.get_topic(tid)
            self.store.update_topic(tid, onboarded_at=(self.clock.t - timedelta(hours=3)).isoformat(),
                                    rules_hash=self.d.rules_hash(t))
            self.sessions[tid] = SimSession(self, tid, cid)
        self.store.set_meta("roster_notify_bus", "0")
        self.by_cid = {s.cid: s for s in self.sessions.values()}
        self.round_no = 0
        self.last_result: dict = {}

    def close(self) -> None:
        self.store.close()
        self.stack.close()
        self.tmp.cleanup()

    def __getitem__(self, tid: str) -> SimSession:
        return self.sessions[tid if tid.startswith("tp_") else "tp_" + tid]

    @property
    def now(self) -> datetime:
        return self.clock.t

    # ---------- 外部事件 ----------
    def message(self, to: str, text: str = "", *, group: bool = False, at_me: bool = False,
                sender: str = "ou_test_colleague", mid: str = "") -> str:
        """一条 IM 消息进账本（同 sheepdog inject）：to 是会话 key（私聊 / 群）或 bus（陌生人私聊，路由给总线）。"""
        self.msg_n += 1
        mid = mid or f"om_test_sim{self.msg_n:05d}"
        if to in ("bus", BUS_TOPIC_ID):
            chat, ctype, name = f"oc_test_stranger{self.msg_n % 3}", "p2p", "陌生人私聊"
        else:
            key = to.removeprefix("tp_")
            chat, ctype, name = (f"oc_test_{key}_grp", "group", f"{key} 群") if group else \
                (f"oc_test_{key}_p2p", "p2p", f"{key} 私聊")
        mentions = [Mention(id=OWNER, name="主人")] if at_me else []
        m = Message(message_id=mid, chat_id=chat, chat_name=name, chat_type=ctype, sender_id=sender,
                    sender_name="同事", sender_type="user", content=text or f"SIM 消息 {self.msg_n}", msg_type="text",
                    create_time=self.now.astimezone().isoformat(timespec="seconds"), mentions=mentions)
        self.collector.ingest([m])
        return mid

    # ---------- 一轮 ----------
    def round(self, minutes: float = 1) -> dict:
        self.clock.advance(minutes=minutes)
        self.round_no += 1
        self.collector.poll_once()
        self.last_result = self.d.dispatch_once()
        return self.last_result

    def run(self, minutes: int, step: float = 1) -> None:
        """每 step 分钟一轮，跑 minutes 分钟。"""
        n = int(round(minutes / step))
        for _ in range(n):
            self.round(step)

    def restart(self) -> None:
        """sidecar 重启：同一份账本，新建 Store / Dispatcher / Collector（进程里的缓存全丢）。"""
        self.store.close()
        self.store = Store(self.cfg.db_path)
        self.d = Dispatcher(self.cfg, self.store, {"antigravity": self.sink}, self.roster, notifier=self.notifier)
        self.collector = Collector(self.cfg, self.store, NullSource(), self.roster)

    # ---------- 观察 ----------
    def card(self, tid: str):
        return self.store.get_card(tid if tid.startswith("tp_") else "tp_" + tid)

    def run_state(self, tid: str) -> str:
        c = self.card(tid)
        return c["run_state"] if c is not None else ""

    def topic(self, tid: str):
        return self.store.get_topic(tid if tid.startswith("tp_") else "tp_" + tid)

    def notices(self) -> list[dict]:
        return read_notices(self.cfg.notify_path)

    def delivered_ids(self, tid: str) -> list[str]:
        """投给这个会话的每一批里的消息（按投递顺序，含重投）。"""
        cid = self.sessions[tid].cid
        out: list[str] = []
        for d in self.sink.to(cid):
            if d["batch"]:
                out += json.loads(self.store.get_dispatch(d["batch"])["message_ids"] or "[]")
        return out

    def state(self, mid: str) -> str:
        return self.store.get_message(mid)["dispatch_state"]

    def cards(self) -> list[dict]:
        return duty.board_cards(self.store, self.cfg)


def is_dispatchable(state: str) -> bool:
    return state in sm.DISPATCHABLE or state == sm.FAILED
