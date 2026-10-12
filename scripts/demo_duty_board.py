#!/usr/bin/env python3
"""在岗表（7.20）演示：在临时目录里搭一套隔离的配置和账本，用合成数据跑一轮投递（宿主一律 dryrun），
打印三样东西：`sheepdog board` 的输出、总线收到的那一批、卡住的会话被判成什么和给主人的通知。

用法：python3 scripts/demo_duty_board.py [--keep]
  --keep  跑完不删临时目录（里面有 config.toml、roster.toml、账本、合成的会话记录），方便接着用
          SHEEPDOG_CONFIG / SHEEPDOG_STATE_DIR 指过去手动敲 sheepdog board / work / tell

不碰 ~/.config/sheepdog、~/.local/state/sheepdog、~/.gemini：配置、账本、会话记录全在临时目录里；
不调 agentapi、lark-cli、codex：投递用 DryRunSink（只打印），给主人的私聊只打印。
数据全是合成的（*_test_* 编号，没有真实的人名和 ID）。

场景：
- 总线 tp_bus；「发版」tp_release 正在做审批单 202610100035：8 分钟前牧羊犬把提到这个单号的消息投给了它（路由事实，
  不靠它登记），它自己也申报了 work（会话自报），最近一次工具调用「Trigger agent-worker build」在 5 分钟前；
  「授权」tp_auth 在等同事回复（登记了 watch，牧羊犬自动补活 → 自动·等人回复）；
  「巡检」tp_patrol 卡住了：批次送出 20 分钟，会话记录里再没有新动作 → 应判为「中断」，信号排队、私聊主人。
- 一条飞书私聊提到 202610100035，路由给总线 → 带撞车提醒（以路由事实为主，自报的活注明「会话自报」）；
- release 用 `sheepdog tell --to tp_bus` 给总线留一句话。
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OWNER = "ou_test_owner"
COLLEAGUE = "ou_test_colleague"
REQUESTER = "ou_test_requester"
APPROVAL = "202610100035"

CONFIG = f"""
self_open_id = "{OWNER}"
sink = "dryrun"
cli_name = "sheepdog"
roster_path = "roster.toml"
playbook_dir = "playbook"

[source]
enabled = false
"""

ROSTER = """
[[session]]
key = "release"
mode = "managed"
conversation_id = "conv_test_release"
title = "发版"
duty = "按发版技能处理发版审批单：触发构建、盯 CI、回报结果。不负责改代码。"
[[session.chats]]
chat_id = "oc_test_release_grp"
name = "发版群"
all_messages = false

[[session]]
key = "auth"
mode = "managed"
conversation_id = "conv_test_auth"
title = "授权"
duty = "处理权限申请：核对范围、找人确认。不直接开生产权限。"
[[session.chats]]
chat_id = "oc_test_auth_p2p"
name = "授权申请私聊"
all_messages = true

[[session]]
key = "patrol"
mode = "managed"
conversation_id = "conv_test_patrol"
title = "巡检"
duty = "每天巡检服务看板，异常汇报主人。"
[[session.chats]]
chat_id = "oc_test_patrol_grp"
name = "巡检群"
all_messages = true
"""


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tool_call(at: datetime, step: int, summary: str, action: str, cmd: str = "") -> dict:
    """Antigravity 的 PLANNER_RESPONSE：工具调用的 args 都是 JSON 编码过的字符串。"""
    name = "run_command" if cmd else "view_file"
    args = {"toolSummary": json.dumps(summary), "toolAction": json.dumps(action)}
    if cmd:
        args.update(CommandLine=json.dumps(cmd), Cwd=json.dumps("/tmp/demo"))
    return {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
            "created_at": utc(at), "thinking": "（演示：模型的思考，在岗表不读）", "tool_calls": [{"name": name, "args": args}]}


def generic(at: datetime, step: int) -> dict:
    return {"step_index": step, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": utc(at),
            "content": "（演示：工具输出）"}


def system_msg(at: datetime, step: int, text: str) -> dict:
    return {"step_index": step, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE", "created_at": utc(at),
            "content": text}


def write_record(app: Path, cid: str, rows: list[dict], mtime: datetime) -> None:
    p = app / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    first = {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
             "created_at": utc(mtime - timedelta(hours=2)), "content": "（演示：开场提示词）"}
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [first, *rows]), encoding="utf-8")
    os.utime(p, (mtime.timestamp(), mtime.timestamp()))


def run_cli(*argv: str) -> str:
    from sheepdog import cli
    buf, err = io.StringIO(), io.StringIO()
    old_err = sys.stderr
    sys.stderr = err
    try:
        with redirect_stdout(buf):
            rc = cli.main(list(argv))
    finally:
        sys.stderr = old_err
    out = buf.getvalue() + err.getvalue()
    if rc != 0:
        raise SystemExit(f"sheepdog {' '.join(argv)} 失败（{rc}）：\n{out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", action="store_true", help="跑完不删临时目录")
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="sheepdog-duty-demo-"))
    conf_dir, state, app = root / "config", root / "state", root / "app"
    conf_dir.mkdir()
    (conf_dir / "config.toml").write_text(CONFIG, encoding="utf-8")
    (conf_dir / "roster.toml").write_text(ROSTER, encoding="utf-8")
    shutil.copytree(REPO / "examples" / "playbook", conf_dir / "playbook")
    os.environ.update(SHEEPDOG_CONFIG=str(conf_dir / "config.toml"), SHEEPDOG_STATE_DIR=str(state),
                      ANTIGRAVITY_APP_DATA_DIR=str(app))
    logging.basicConfig(filename=str(root / "demo.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", force=True)

    from sheepdog import session as sm
    from sheepdog.config import load_config
    from sheepdog.engine import BUS_TOPIC_ID, Collector, Dispatcher, add_watch
    from sheepdog.models import Message
    from sheepdog.roster import load_roster
    from sheepdog.security import load_security
    from sheepdog.sink.agentapi import DryRunSink
    from sheepdog.source import NullSource
    from sheepdog.store import Store

    cfg = load_config()
    roster = load_roster(cfg.roster_file, True)
    security = load_security(cfg.security_file)
    store = Store(cfg.db_path)
    now = datetime.now().astimezone()

    def ago(minutes: float) -> datetime:
        return now - timedelta(minutes=minutes)

    def iso(dt: datetime) -> str:
        return dt.isoformat(timespec="seconds")

    notices: list[str] = []
    d = Dispatcher(cfg, store, {"antigravity": DryRunSink()}, roster, security, notifier=notices.append)

    # ---- 1. 名册同步；各会话都已登记过（不打印开场、规则更新） ----
    d.sync_roster()
    d.ensure_bus_topic()
    store.update_topic(BUS_TOPIC_ID, conversation_id="conv_test_bus")
    for tid in (BUS_TOPIC_ID, "tp_release", "tp_auth", "tp_patrol"):
        t = store.get_topic(tid)
        store.update_topic(tid, onboarded_at=iso(ago(180)), rules_hash=d.rules_hash(t))
    store.set_meta("roster_notify_bus", "0")
    # 总线也有会话记录（只有开场）；没有记录的会话，卡片的动作会写「（找不到会话记录）」
    write_record(app, "conv_test_bus", [], mtime=ago(60))

    # ---- 2. 发版：批次 8 分钟前送出，在做审批单；最近一次工具调用 5 分钟前 ----
    store.record_dispatch("b_test_release", "tp_release", [])
    store.conn.execute("UPDATE dispatches SET sent_at=? WHERE batch_id='b_test_release'", (iso(ago(8)),))
    store.conn.commit()
    store.update_topic("tp_release", state=sm.RUNNING, pending_batch_id="b_test_release", dispatched_at=iso(ago(8)))
    # 那一批里有发版群的一条消息提到审批单号：投递时牧羊犬记下路由事实（同 _complete_delivery 的做法）
    release_ask = Message(message_id="om_test_demo_release", chat_id="oc_test_release_grp", chat_name="发版群",
                          chat_type="group", sender_id=REQUESTER, sender_name="需求方乙", sender_type="user",
                          content=f"@发版 审批单 {APPROVAL} 已经批了，麻烦发一下 agent-worker", msg_type="text",
                          create_time=iso(ago(9)))
    store.upsert_message(release_ask, "dispatch", "at_me", [], "tp_release")
    store.mark_batch([release_ask.message_id], "b_test_release", "tp_release", "delivered")
    store.conn.execute("UPDATE dispatches SET message_ids=? WHERE batch_id='b_test_release'",
                       (json.dumps([release_ask.message_id]),))
    store.conn.commit()
    from sheepdog import duty as duty_mod
    duty_mod.record_routes(store, "tp_release", [store.get_message(release_ask.message_id)], cfg.duty.patterns,
                           iso(ago(8)))
    write_record(app, "conv_test_release", [
        system_msg(ago(8), 101, "[sheepdog] 新信号批次 `b_test_release`（演示）"),
        tool_call(ago(7), 102, "Read the release skill", "Reading release skill"),
        tool_call(ago(5), 103, "Trigger agent-worker build", "Triggering agent-worker build",
                  cmd="gcloud builds triggers run demo-agent-worker --region=demo-region"),
        generic(ago(5), 104),
    ], mtime=ago(5))
    run_cli("work", "set", "--topic", "tp_release", "--key", APPROVAL, "--title", f"审批单 {APPROVAL}：agent-worker 发版",
            "--state", "doing", "--expect", "构建通过后回报发版结果")

    # ---- 3. 授权：在等同事回复（watch → 牧羊犬自动补活：等别人） ----
    store.upsert_message(Message(message_id="om_test_demo_colleague", chat_id="oc_test_some_grp", chat_name="某个群",
                                 chat_type="group", sender_id=COLLEAGUE, sender_name="同事甲", sender_type="user",
                                 content="（演示：同事在别的群里说过话，用来查名字）", msg_type="text",
                                 create_time=iso(ago(300))), "inbox", "group", [])
    write_record(app, "conv_test_auth", [
        tool_call(ago(14), 51, "Ask colleague to confirm token scope", "Sending confirmation request",
                  cmd="lark-cli im +messages-send --as user --chat-id oc_test_auth_p2p --text '...'"),
        generic(ago(14), 52),
    ], mtime=ago(14))
    add_watch(store, "tp_auth", COLLEAGUE, "", "等同事确认 AUTH-42 的权限范围")
    store.conn.execute("UPDATE watches SET started_at=? WHERE topic_id='tp_auth'", (iso(ago(14)),))
    store.conn.commit()

    # ---- 4. 巡检：批次 20 分钟前送出，之后会话记录没有新动作（最后一次动作 26 分钟前） ----
    store.record_dispatch("b_test_patrol", "tp_patrol", [])
    store.conn.execute("UPDATE dispatches SET sent_at=? WHERE batch_id='b_test_patrol'", (iso(ago(20)),))
    store.conn.commit()
    store.update_topic("tp_patrol", state=sm.RUNNING, pending_batch_id="b_test_patrol", dispatched_at=iso(ago(20)))
    write_record(app, "conv_test_patrol", [
        tool_call(ago(26), 301, "Check service dashboards", "Checking dashboards"),
        generic(ago(26), 302),
        system_msg(ago(20), 303, "[sheepdog] 新信号批次 `b_test_patrol`（演示）"),
    ], mtime=ago(20))

    # ---- 5. 飞书消息：提到 202610100035，路由给总线；另一条进巡检群（巡检卡住，应排队） ----
    col = Collector(cfg, store, NullSource(), roster, security)
    col.ingest([
        Message(message_id="om_test_demo_ask", chat_id="oc_test_requester_p2p", chat_name="需求方乙", chat_type="p2p",
                sender_id=REQUESTER, sender_name="需求方乙", sender_type="user",
                content=f"你好，审批单 {APPROVAL} 现在到哪一步了？今天能发出去吗？", msg_type="text", create_time=iso(ago(1))),
        Message(message_id="om_test_demo_patrol", chat_id="oc_test_patrol_grp", chat_name="巡检群", chat_type="group",
                sender_id="ou_test_oncall", sender_name="值班丙", sender_type="user",
                content="看板上 demo-api 的错误率有点高，帮忙看一眼", msg_type="text", create_time=iso(ago(1))),
    ])

    # ---- 6. release 给总线留言 ----
    tell_out = run_cli("tell", "--topic", "tp_release", "--to", "tp_bus", "--key", APPROVAL,
                       f"{APPROVAL} 的构建已经触发，CI 跑完我来回报发版结果；有人问进度，请让对方等 20 分钟左右，别重复触发。")

    # ---- 跑一轮投递（宿主 dryrun：只打印，不调 agentapi） ----
    buf = io.StringIO()
    with redirect_stdout(buf):
        d.dispatch_once()
    sent = buf.getvalue()

    board = run_cli("board")
    bus_batch = ""
    for chunk in sent.split("[dryrun] send-message -> ")[1:]:
        if chunk.startswith("conv_test_bus"):
            bus_batch = chunk.split("\n", 1)[1].rstrip()
    patrol = store.get_card("tp_patrol")
    patrol_topic = store.get_topic("tp_patrol")
    queued = store.count_pending_signals("tp_patrol")

    print("在岗表演示（临时目录：" + str(root) + "）")
    print(f"\n准备：{tell_out.strip()}")
    print("\n" + "=" * 20 + " 1. sheepdog board " + "=" * 20)
    print(board.rstrip())
    print("\n" + "=" * 20 + " 2. 总线收到的那一批 " + "=" * 20)
    print(bus_batch or "（总线这一轮没有收到批次）")
    print("\n" + "=" * 20 + " 3. 卡住的会话（tp_patrol） " + "=" * 20)
    print(f"运行状态：{patrol['run_state']}（{__import__('sheepdog.duty', fromlist=['RUN_LABEL']).RUN_LABEL[patrol['run_state']]}）")
    print(f"判定依据：{patrol['detail']}")
    print(f"会话状态机：{patrol_topic['state']}；排队信号 {queued} 条（这一轮没有投给它）")
    print("给主人的通知：")
    for n in notices:
        print(n)
    if not notices:
        print("（没有）")
    ok = (patrol["run_state"] == "stalled" and notices and "的消息最近送到了「发版」" in bus_batch
          and "会话自报" in bus_batch and "会话留言" in bus_batch and "在岗表" in bus_batch and queued >= 1
          and "自动·路由" in board and "自动·等人回复" in board)
    print("\n检查：" + ("✓ 中断判定、排队、通知、撞车提醒（路由事实 + 会话自报）、留言、摘要、手上的事的来源都在" if ok
                       else "✗ 有东西没出来，看上面"))
    if args.keep:
        print(f"\n临时目录保留在 {root}；接着手动试：\n  export SHEEPDOG_CONFIG={conf_dir / 'config.toml'} "
              f"SHEEPDOG_STATE_DIR={state} ANTIGRAVITY_APP_DATA_DIR={app}\n  sheepdog board --topic tp_release")
    else:
        store.close()
        shutil.rmtree(root, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
