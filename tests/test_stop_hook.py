"""7.37 第 5 件：Stop hook 收尾查回执（SD-191）。

`sheepdog hook stop`：stdin 是 Antigravity 的 Stop 入参（样本照 hook 实验室 events.jsonl 里原样的字段，路径换成合成的）。
要拦（全部满足）：conversationId 是牧羊犬管的 Antigravity 会话；fullyIdle 为 true；会话 running、手上那批回执文件不在、账本
里也没收到；这一批拦过的次数 < 2；会话没被主人占着。只在要拦时输出 {"decision":"continue","reason":…}；其他一律不输出、
退出码 0，任何异常也一样。账本只读。
"""

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sheepdog import cli, duty, engine, hook
from sheepdog import session as sm
from sheepdog.config import Config
from sheepdog.engine import write_receipt
from sheepdog.playbook import Playbook
from sheepdog.store import Store

from test_roster import FIXTURES, ME, REPO

BUS_CID = "8fb0aeb2-0000-4000-8000-00000000bus0"
ALPHA_CID = "8fb0aeb2-0000-4000-8000-0000000alpha"
BATCH = "b20261014071200"
# 会话记录目录：HookBase.setUp 指到临时目录（入参里的 transcriptPath 指向这里）
BRAIN = [Path("/home/test/.gemini/antigravity/brain")]


def transcript(cid: str) -> Path:
    return BRAIN[0] / cid / ".system_generated" / "logs" / "transcript_full.jsonl"


def step(i: int, source: str, kind: str, content: str = "", at: str = "2026-10-13T23:12:00Z") -> dict:
    """会话记录的一行，字段同实验室里的真实记录（step_index / source / type / status / created_at / content）。"""
    return {"step_index": i, "source": source, "type": kind, "status": "DONE", "created_at": at, "content": content}


def delivered(batch: str, i: int = 8) -> dict:
    """agentapi send-message 送进去的批次：记成 SYSTEM_MESSAGE，正文里有批次开头那行。"""
    return step(i, "SYSTEM", "SYSTEM_MESSAGE",
                "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n<SYSTEM_MESSAGE>\n[Message] "
                f"sender=system priority=MESSAGE_PRIORITY_HIGH content=[sheepdog] 新信号批次 `{batch}`（topic `tp_bus`，共 1 条）"
                "\n</SYSTEM_MESSAGE>")


def record(cid: str, *rows: dict) -> Path:
    p = transcript(cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def stop_input(cid: str = BUS_CID, fully_idle=True, execution_num: int = 0) -> str:
    """Stop 入参：字段、顺序、取值同实验室里 Antigravity 原样给的（10-14 07:08–07:17，E0 / E3 / E6），路径换成合成的。"""
    brain = f"{BRAIN[0]}/{cid}"
    return json.dumps({"artifactDirectoryPath": brain, "conversationId": cid, "error": "",
                       "executionNum": execution_num, "fullyIdle": fully_idle, "modelName": "gemini-3.8-flash-tiered",
                       "terminationReason": "NO_TOOL_CALL",
                       "transcriptPath": str(transcript(cid)),
                       "workspacePaths": ["/home/test/Code/sheepdog-hooklab"]})


def file_digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


class HookBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = Config(self_open_id=ME, state_dir=self.dir / "state")
        self.cfg.playbook_dir = str(FIXTURES)
        self.cfg.state_dir.mkdir()
        store = Store(self.cfg.db_path)
        try:
            store.create_topic("tp_bus", "[managed] 总线", "bus", "见 playbook/bus.md", sm.RUNNING)
            store.update_topic("tp_bus", conversation_id=BUS_CID, pending_batch_id=BATCH,
                               dispatched_at="2026-10-14T07:12:00+08:00")
            store.record_dispatch(BATCH, "tp_bus", ["om_test_1"])
            store.create_topic("tp_alpha", "Alpha", "adopted", "跟进 Alpha", sm.ACTIVE)
            store.update_topic("tp_alpha", conversation_id=ALPHA_CID)
        finally:
            store.close()
        self.now = datetime(2026, 10, 14, 7, 20).astimezone()
        BRAIN[0] = self.dir / "brain"
        # 总线的会话记录：第 0 步开场（USER_EXPLICIT），之后 sheepdog 送进去这一批，会话干了几步
        record(BUS_CID, step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>[sheepdog] 总线开场</USER_REQUEST>"),
               step(7, "MODEL", "PLANNER_RESPONSE", "好的"), delivered(BATCH),
               step(9, "MODEL", "GENERIC", "ran sheepdog inbox"), step(10, "MODEL", "PLANNER_RESPONSE", "处理完了"))

    def edit(self, sql: str, *args) -> None:
        """改测试账本（相当于 sheepdog 常驻进程那边写的）。"""
        conn = sqlite3.connect(self.cfg.db_path)
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def check(self, raw: str | None = None, cfg: Config | None = None):
        return hook.stop_check(stop_input() if raw is None else raw, cfg or self.cfg, self.now)

    def run_main(self, raw: str | bytes | None = None, cfg: Config | None = None) -> tuple[int, str]:
        raw = stop_input() if raw is None else raw
        stdin = io.BytesIO(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
        out = io.StringIO()
        rc = hook.stop_main(stdin, out, cfg or self.cfg, self.now)
        return rc, out.getvalue()


class BlockTest(HookBase):
    def test_blocks_with_batch_and_full_receipt_command(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(set(d), {"decision", "reason"})
        self.assertEqual(d["decision"], "continue")
        receipt = (f"sheepdog receipt --topic tp_bus --batch {BATCH} "
                   "--json '{\"status\": \"handled\", \"summary\": \"一句话：做了什么\"}'")
        waiting = (f"sheepdog receipt --topic tp_bus --batch {BATCH} "
                   "--json '{\"status\": \"waiting_external\", \"summary\": \"等谁、等什么\"}'")
        self.assertEqual(d["reason"], f"FX-STOP batch={BATCH} n=1 receipt=[{receipt}] waiting=[{waiting}] topic=tp_bus")
        # 回执命令照 reason 原样敲下去能交上（status 合法、JSON 合法）
        p = write_receipt(self.cfg, "tp_bus", BATCH, json.loads(receipt.split("--json ")[1].strip("'")))
        self.assertTrue(p.exists())
        self.assertIsNone(self.check())                                # 交了：不再拦

    def test_managed_and_dynamic_sessions_block_too(self):
        for kind in ("adopted", "dynamic"):
            with self.subTest(kind=kind):
                b = f"b20261014071500-alpha-{kind}"
                self.edit("UPDATE topics SET kind=?, state='running', pending_batch_id=? WHERE topic_id='tp_alpha'", kind, b)
                self.edit("INSERT INTO dispatches(batch_id,topic_id,message_ids,sent_at,state) VALUES(?,?,?,?,?)",
                          b, "tp_alpha", "[]", "2026-10-14T07:15:00+08:00", "sent")
                record(ALPHA_CID, delivered(b))
                d = self.check(stop_input(ALPHA_CID))
                self.assertIn(f"batch={b}", d["reason"])
                self.assertIn("--topic tp_alpha", d["reason"])

    def test_cli_name_from_config(self):
        self.cfg.cli_name = "/opt/sd test/sheepdog"
        self.assertIn("'/opt/sd test/sheepdog' receipt --topic tp_bus", self.check()["reason"])

    def test_example_playbook_and_default_reason(self):
        self.cfg.playbook_dir = str(REPO / "examples" / "playbook")
        r = self.check()["reason"]
        self.assertIn(f"批次 `{BATCH}` 还没交回执", r)
        self.assertIn(f"sheepdog receipt --topic tp_bus --batch {BATCH}", r)
        self.assertIn("waiting_external", r)
        self.assertNotIn("<!--", r)
        self.assertNotIn("{{", r)
        os.unlink(hook.count_path(self.cfg, "tp_bus"))
        self.cfg.playbook_dir = str(self.dir / "no_playbook")
        r = self.check()["reason"]
        self.assertTrue(r.startswith(f"[sheepdog] 先别结束：批次 `{BATCH}` 还没交回执。"), r)
        self.assertNotIn("{{", r)

    def test_at_most_twice_per_batch_then_new_batch_counts_again(self):
        self.assertEqual(json.loads(self.run_main()[1])["decision"], "continue")
        self.assertIn("n=2", json.loads(self.run_main(stop_input(execution_num=1))[1])["reason"])
        self.assertEqual(self.run_main(stop_input(execution_num=2)), (0, ""))   # 第三次放行
        self.assertEqual(self.run_main(), (0, ""))
        nb = "b20261014073000"
        self.edit("UPDATE topics SET pending_batch_id=? WHERE topic_id='tp_bus'", nb)
        self.edit("INSERT INTO dispatches(batch_id,topic_id,message_ids,sent_at,state) VALUES(?,?,?,?,?)",
                  nb, "tp_bus", "[]", "2026-10-14T07:30:00+08:00", "sent")
        record(BUS_CID, delivered(nb, 20))
        self.assertIn("n=1", self.check()["reason"])                   # 换了批次从头算
        self.assertEqual(json.loads(hook.count_path(self.cfg, "tp_bus").read_text()), {"batch": nb, "count": 1})

    def test_ledger_is_read_only(self):
        # 账本一个字节不改：只读打开，不建表、不迁移；拦的次数记在状态目录下的小文件里
        # （WAL 账本的读者会建 -wal / -shm 两个辅助文件，正式环境常驻进程一直开着它们；-wal 里不会有这边写的东西）
        before = file_digest(self.cfg.db_path)
        for _ in range(3):
            self.run_main()
        self.assertEqual(file_digest(self.cfg.db_path), before)
        wal = self.cfg.state_dir / "sheepdog.sqlite3-wal"
        self.assertEqual(wal.stat().st_size if wal.exists() else 0, 0)
        self.assertEqual(sorted(p.name for p in (self.cfg.state_dir / "hooks" / "stop").iterdir()), ["tp_bus.json"])

    def test_old_ledger_missing_tables_not_migrated(self):
        self.edit("DROP TABLE duty_cards")
        self.assertEqual(self.run_main(), (0, ""))
        conn = sqlite3.connect(self.cfg.db_path)
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertNotIn("duty_cards", have)


class PassTest(HookBase):
    """不拦的每个条件：什么都不输出、退出码 0，也不记次数。"""

    def assert_pass(self, raw: str | None = None):
        self.assertEqual(self.run_main(raw), (0, ""))
        self.assertFalse(hook.count_path(self.cfg, "tp_bus").exists())

    def test_not_fully_idle(self):
        for v in (False, None, "true", 1):
            with self.subTest(fullyIdle=v):
                self.assert_pass(stop_input(fully_idle=v))
        raw = json.loads(stop_input())
        del raw["fullyIdle"]
        self.assert_pass(json.dumps(raw))

    def test_unknown_or_unmanaged_conversation(self):
        self.assert_pass(stop_input("4c1af98c-0000-4000-8000-owner-own-chat"))   # 主人自己的对话
        for cid in ("", "   ", None, 42):
            with self.subTest(cid=cid):
                raw = json.loads(stop_input())
                raw["conversationId"] = cid
                self.assert_pass(json.dumps(raw))
        for kind in ("known", "retired"):
            with self.subTest(kind=kind):
                self.edit("UPDATE topics SET kind=?, state='running', pending_batch_id=? WHERE topic_id='tp_alpha'",
                          kind, BATCH)
                self.assertEqual(self.run_main(stop_input(ALPHA_CID)), (0, ""))

    def test_closed_session(self):
        self.edit("UPDATE topics SET kind='dynamic' WHERE topic_id='tp_bus'")
        self.edit("UPDATE topics SET state='closed' WHERE topic_id='tp_bus'")
        self.assert_pass()

    def test_cli_host_session(self):
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                self.edit("UPDATE topics SET host=? WHERE topic_id='tp_bus'", host)
                self.assert_pass()

    def test_same_conversation_on_two_topics_is_ambiguous(self):
        self.edit("UPDATE topics SET conversation_id=? WHERE topic_id='tp_alpha'", BUS_CID)
        self.assert_pass()

    def test_not_running(self):
        for state in (sm.ACTIVE, sm.IDLE, sm.WAITING_HUMAN, sm.BLOCKED, sm.HUMAN_ATTACHED, sm.FAILED, sm.ATTENTION):
            with self.subTest(state=state):
                self.edit("UPDATE topics SET state=? WHERE topic_id='tp_bus'", state)
                self.assert_pass()

    def test_running_without_pending_batch(self):
        self.edit("UPDATE topics SET pending_batch_id=NULL WHERE topic_id='tp_bus'")
        self.assert_pass()

    def test_receipt_file_already_there(self):
        write_receipt(self.cfg, "tp_bus", BATCH, {"status": "handled", "summary": "ok"})
        self.assert_pass()

    def test_ledger_already_got_it(self):
        for state in ("acked", "acked_late", "missed", "failed", "sending", "uncertain", "context", "priority"):
            with self.subTest(state=state):
                self.edit("UPDATE dispatches SET state=? WHERE batch_id=?", state, BATCH)
                self.assert_pass()
        self.edit("DELETE FROM dispatches WHERE batch_id=?", BATCH)
        self.assert_pass()

    def test_owner_occupies_session(self):
        store = Store(self.cfg.db_path)
        try:
            store.upsert_card("tp_bus", run_state=duty.OCCUPIED, detail="主人 07:18 在会话里说过话")
        finally:
            store.close()
        self.assert_pass()
        # 卡片还没重算、但主人刚在会话里说过话：同样算占着
        self.edit("UPDATE duty_cards SET run_state='working', human_at=?", (self.now - timedelta(minutes=3)).isoformat())
        self.assert_pass()
        # 说话已经是 human_attach_minutes 之前的事：拦
        old = self.now - timedelta(minutes=self.cfg.session.human_attach_minutes + 1)
        self.edit("UPDATE duty_cards SET human_at=?", old.isoformat())
        self.assertEqual(json.loads(self.run_main()[1])["decision"], "continue")

    def test_human_attach_minutes_zero_never_occupied_by_typing(self):
        self.cfg.session.human_attach_minutes = 0                      # 正式配置就是 0
        self.edit("INSERT INTO duty_cards(topic_id, run_state, human_at) VALUES('tp_bus', 'working', ?)",
                  self.now.isoformat())
        self.assertEqual(json.loads(self.run_main()[1])["decision"], "continue")


class TranscriptTest(HookBase):
    """7.38 第 6 条：读入参的 transcriptPath。这一批送达以后主人亲自在会话里打过字（USER_INPUT、USER_EXPLICIT）不拦；
    末尾那一段里找不到这一批也不拦。"""

    def blocks(self) -> bool:
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        return bool(out) and json.loads(out)["decision"] == "continue"

    def test_owner_typed_after_batch(self):
        record(BUS_CID, step(11, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>先停一下，我看看</USER_REQUEST>"),
               step(12, "MODEL", "PLANNER_RESPONSE", "好的"))
        self.assertFalse(self.blocks())
        self.assertFalse(hook.count_path(self.cfg, "tp_bus").exists())

    def test_opening_step0_is_before_batch_and_does_not_count(self):
        self.assertTrue(self.blocks())                                  # setUp 里第 0 步是 USER_EXPLICIT，在批次之前

    def test_owner_typed_only_before_batch(self):
        p = transcript(BUS_CID)
        p.unlink()
        record(BUS_CID, step(0, "USER_EXPLICIT", "USER_INPUT", "开场"), step(3, "USER_EXPLICIT", "USER_INPUT", "早先的话"),
               delivered(BATCH), step(9, "MODEL", "PLANNER_RESPONSE", "完"))
        self.assertTrue(self.blocks())

    def test_sdk_and_system_inputs_do_not_count(self):
        # hook 插的 userMessage 记成 SYSTEM_SDK 来源的 USER_INPUT（E2）；agentapi 送的是 SYSTEM_MESSAGE；都不是主人亲自打字
        record(BUS_CID, step(11, "SYSTEM_SDK", "USER_INPUT", "<USER_REQUEST>插话：source 是 USER_EXPLICIT 才算</USER_REQUEST>"),
               step(12, "SYSTEM", "SYSTEM_MESSAGE", "Stop hook blocked termination: 先交回执"),
               step(13, "MODEL", "PLANNER_RESPONSE", "USER_EXPLICIT 这个词出现在回答里不算"))
        self.assertTrue(self.blocks())

    def test_batch_not_in_transcript(self):
        p = transcript(BUS_CID)
        p.unlink()
        record(BUS_CID, step(0, "USER_EXPLICIT", "USER_INPUT", "开场"),
               delivered(BATCH + "-2"),                                 # 别的批次号以它开头：不算
               step(9, "MODEL", "PLANNER_RESPONSE", f"我在跑 sheepdog receipt --batch {BATCH}"))   # 不是送达那一行
        self.assertFalse(self.blocks())

    def test_transcript_missing_or_path_bad(self):
        transcript(BUS_CID).unlink()
        self.assertFalse(self.blocks())
        for v in (None, "", 42, ["x"]):
            with self.subTest(transcriptPath=v):
                record(BUS_CID, delivered(BATCH))
                raw = json.loads(stop_input())
                raw["transcriptPath"] = v
                self.assertEqual(self.run_main(json.dumps(raw)), (0, ""))
        raw = json.loads(stop_input())
        raw["transcriptPath"] = str(self.dir)                           # 是个目录
        self.assertEqual(self.run_main(json.dumps(raw)), (0, ""))

    def test_only_reads_the_tail(self):
        # 送达那一行被挤出末尾那一段：找不到 → 不拦；主人打字在末尾那一段里 → 照样认得出
        filler = [step(100 + i, "MODEL", "GENERIC", "x" * 200) for i in range(40)]
        record(BUS_CID, *filler)
        with mock.patch.object(hook, "TRANSCRIPT_TAIL_BYTES", 4096):
            self.assertFalse(self.blocks())
            record(BUS_CID, delivered(BATCH, 200), *filler[:5])
            self.assertTrue(self.blocks())
            record(BUS_CID, step(300, "USER_EXPLICIT", "USER_INPUT", "我来"))
            self.assertFalse(self.blocks())

    def test_partial_last_line_and_garbage_lines(self):
        p = transcript(BUS_CID)
        with p.open("a", encoding="utf-8") as f:
            f.write("not json\n{\"type\": \"USER_INPUT\", \"source\": \"USER_EXPLICIT\"")   # 最后一行还没写完
        self.assertTrue(self.blocks())

    def test_real_lab_shape(self):
        """实验室真实记录的样子（4031437e 那个会话）：第 0 步 USER_EXPLICIT 开场、第 3 步 SYSTEM_SDK 插话，没有主人亲自打字。"""
        p = transcript(BUS_CID)
        p.unlink()
        record(BUS_CID, step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>依次运行 echo</USER_REQUEST>"),
               {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                "created_at": "2026-10-13T23:11:44Z", "tool_calls": [{"name": "run_command"}]},
               delivered(BATCH, 2),
               step(3, "SYSTEM_SDK", "USER_INPUT", "<USER_REQUEST>（实验插话）主人问：现在几点？</USER_REQUEST>"),
               step(8, "MODEL", "PLANNER_RESPONSE", "现在是 07:11"))
        self.assertTrue(self.blocks())


class ErrorPathTest(HookBase):
    """异常路径：一律什么都不输出、退出码 0。"""

    def assert_silent(self, raw=None, cfg=None):
        self.assertEqual(self.run_main(raw, cfg), (0, ""))

    def test_stdin_not_json(self):
        for raw in ("", "not json", "{", "[1, 2]", "null", "\"Stop\"", b"\xff\xfe\x00garbage"):
            with self.subTest(raw=raw):
                self.assert_silent(raw)

    def test_config_broken(self):
        bad = self.dir / "config.toml"
        bad.write_text("this is = = not toml", encoding="utf-8")
        # 状态目录也指到临时目录：配置读不了时调用日志（7.45 三之二）退回默认状态目录，不能写到本机正式的那个
        with mock.patch.dict(os.environ, {"SHEEPDOG_CONFIG": str(bad), "SHEEPDOG_STATE_DIR": str(self.dir / "fallback")}):
            self.assertEqual(hook.stop_main(io.BytesIO(stop_input().encode()), out := io.StringIO()), 0)
        self.assertEqual(out.getvalue(), "")

    def test_config_invalid_value(self):
        bad = self.dir / "config.toml"
        bad.write_text("[hosts.gemini]\nenabled = true\n", encoding="utf-8")         # ConfigError
        # 状态目录也指到临时目录：配置读不了时调用日志（7.45 三之二）退回默认状态目录，不能写到本机正式的那个
        with mock.patch.dict(os.environ, {"SHEEPDOG_CONFIG": str(bad), "SHEEPDOG_STATE_DIR": str(self.dir / "fallback")}):
            self.assertEqual(hook.stop_main(io.BytesIO(stop_input().encode()), out := io.StringIO()), 0)
        self.assertEqual(out.getvalue(), "")

    def test_ledger_missing(self):
        cfg = Config(self_open_id=ME, state_dir=self.dir / "nowhere")
        self.assert_silent(cfg=cfg)
        self.assertFalse((self.dir / "nowhere").exists())              # 也不建目录、不建账本

    def test_ledger_corrupt(self):
        for name in ("sheepdog.sqlite3-wal", "sheepdog.sqlite3-shm"):
            (self.cfg.state_dir / name).unlink(missing_ok=True)
        self.cfg.db_path.write_bytes(b"not a sqlite file at all" * 100)
        self.assert_silent()

    def test_ledger_is_a_directory(self):
        self.cfg.db_path.unlink()
        self.cfg.db_path.mkdir()
        self.assert_silent()

    def test_counter_not_writable(self):
        (self.cfg.state_dir / "hooks").write_text("挡路的文件", encoding="utf-8")     # hooks/stop 建不出来
        self.assert_silent()

    def test_counter_file_garbage_counts_as_zero(self):
        p = hook.count_path(self.cfg, "tp_bus")
        p.parent.mkdir(parents=True)
        p.write_text("{oops", encoding="utf-8")
        self.assertIn("n=1", json.loads(self.run_main()[1])["reason"])

    def test_playbook_unreadable(self):
        self.cfg.playbook_dir = str(self.dir / "pb")
        (self.dir / "pb").mkdir()
        (self.dir / "pb" / hook.STOP_TEMPLATE).mkdir()                 # 读的时候 IsADirectoryError
        self.assert_silent()

    def test_stdout_broken(self):
        class Broken(io.StringIO):
            def write(self, s):
                raise BrokenPipeError()

        self.assertEqual(hook.stop_main(io.BytesIO(stop_input().encode()), Broken(), self.cfg), 0)


class CliTest(HookBase):
    """经 `sheepdog hook stop` 走一遍：配置和账本全部指向临时目录（三个环境变量）。"""

    def env(self) -> dict:
        cfg_file = self.dir / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\nstate_dir = "{self.cfg.state_dir}"\n'
                            f'playbook_dir = "{FIXTURES}"\n', encoding="utf-8")
        return {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": str(self.cfg.state_dir),
                "XDG_CONFIG_HOME": str(self.dir / "xdg")}

    def cli(self, raw: str | bytes) -> tuple[int, str, str]:
        data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        stdin = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env()), mock.patch("sys.stdin", stdin), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = cli.main(["hook", "stop"])
        return rc, out.getvalue(), err.getvalue()

    def test_block_and_pass_through_cli(self):
        rc, out, err = self.cli(stop_input())
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out)["decision"], "continue")
        self.assertEqual(self.cli(stop_input(fully_idle=False)), (0, "", ""))
        self.assertEqual(self.cli("garbage"), (0, "", ""))

    def test_real_process_cold_start(self):
        """真起一个进程：出错（入参不是 JSON）不输出、退出码 0；要拦时输出一行 JSON；不导入 engine。"""
        env = {**os.environ, **self.env(), "PYTHONPATH": str(REPO)}
        cmd = [sys.executable, "-m", "sheepdog", "hook", "stop"]
        r = subprocess.run(cmd, input=b"not json", capture_output=True, env=env, timeout=30)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        r = subprocess.run(cmd, input=stop_input().encode(), capture_output=True, env=env, timeout=30)
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        self.assertEqual(json.loads(r.stdout)["decision"], "continue")
        probe = "import sys, sheepdog.hook; print('sheepdog.engine' in sys.modules)"
        r = subprocess.run([sys.executable, "-c", probe], capture_output=True, env=env, timeout=30)
        self.assertEqual(r.stdout.strip(), b"False")


class ConstantsTest(unittest.TestCase):
    def test_hook_constants_match_engine(self):
        self.assertEqual(hook.BUS_TOPIC_ID, engine.BUS_TOPIC_ID)
        self.assertEqual(tuple(hook.MANAGED_KINDS), tuple(engine.MANAGED_KINDS))
        self.assertEqual(hook.OCCUPIED, duty.OCCUPIED)

    def test_example_hooks_json(self):
        p = REPO / "examples" / "antigravity-hooks.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        (name, events), = data.items()
        self.assertEqual(name, "sheepdog")
        self.assertEqual(list(events), ["Stop"])                       # 只挂 Stop
        (h,) = events["Stop"]
        self.assertEqual((h["type"], h["timeout"]), ("command", 10))
        self.assertTrue(h["command"].endswith(" hook stop"))
        self.assertNotIn("~", h["command"])                            # 示例写的是绝对路径（占位）

    def test_playbook_files_registered(self):
        from sheepdog.playbook import OPTIONAL_PLAYBOOK_FILES
        for name in (hook.STOP_TEMPLATE, "owner_priority.md"):
            self.assertIn(name, OPTIONAL_PLAYBOOK_FILES)
            self.assertTrue((REPO / "examples" / "playbook" / name).read_text(encoding="utf-8").strip())
        self.assertEqual(Playbook(REPO / "tests" / "no_such_dir").render_or("x.md", "默认 {{a}}", a="1"), "默认 1")
