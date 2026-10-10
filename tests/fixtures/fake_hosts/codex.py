"""假的 `codex`（只用于单测）：模拟 `codex app-server` 的 JSON-RPC over stdio。

行为由环境变量控制（测试进程改 os.environ，子进程继承）：
  FAKE_CODEX_LOG     收到的每条消息追加一行 JSON（测试据此检查完整的请求参数）
  FAKE_CODEX_MODE    ok | approval | capacity | retrying | locked | logout | slow | nothread
  FAKE_CODEX_ANSWER  ok 时最后一条 agentMessage 的正文
"""
import json
import os
import sys
import time

MODE = os.environ.get("FAKE_CODEX_MODE", "ok")
LOG = os.environ.get("FAKE_CODEX_LOG", "")
ANSWER = os.environ.get("FAKE_CODEX_ANSWER", "完成")


def log(obj):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    m = json.loads(line)
    log(m)
    return m


def finish_turn(status, error=None):
    turn = {"id": "turn_test_1", "status": status}
    if error:
        turn["error"] = {"message": error}
    send({"method": "turn/completed", "params": {"threadId": THREAD, "turn": turn}})


if sys.argv[1:] != ["app-server"]:
    sys.stderr.write(f"fake codex: 只认 app-server，收到 {sys.argv[1:]}\n")
    sys.exit(2)

THREAD = ""
while True:
    m = read()
    method, rid, params = m.get("method"), m.get("id"), m.get("params") or {}
    if rid is None:
        continue  # 通知（initialized）或对服务端请求的回复
    if method == "initialize":
        send({"id": rid, "result": {"userAgent": "fake-codex/0.0"}})
    elif method == "thread/start":
        if MODE == "nothread":
            time.sleep(60)
            continue
        THREAD = "thr_test_" + os.urandom(4).hex()
        send({"id": rid, "result": {"thread": {"id": THREAD, "source": "vscode"}}})
    elif method == "thread/name/set":
        send({"id": rid, "result": {}})
    elif method == "thread/resume":
        THREAD = params.get("threadId", "")
        if MODE == "locked":
            # 和 15:54 实测的报错一模一样（Codex 应用自己的 app-server 持锁时）
            send({"id": rid, "error": {"code": -32600, "message": f"thread {THREAD} already has an active writer"}})
            continue
        send({"id": rid, "result": {"thread": {"id": THREAD}}})
    elif method == "turn/start":
        send({"id": rid, "result": {"turn": {"id": "turn_test_1", "status": "inProgress"}}})
        if MODE == "slow":
            time.sleep(120)
        elif MODE == "capacity":
            send({"method": "error", "params": {"error": {"message": "Selected model is at capacity. Please try a different model."},
                                                "willRetry": False}})
            finish_turn("failed", "Selected model is at capacity")
        elif MODE == "retrying":
            send({"method": "error", "params": {"error": {"message": "503 Service Unavailable"}, "willRetry": True}})
            send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": ANSWER}}})
            finish_turn("completed")
        elif MODE == "logout":
            finish_turn("failed", "Your access token could not be refreshed because you have since logged out.")
        else:
            if MODE == "approval":
                send({"id": 900, "method": "item/commandExecution/requestApproval",
                      "params": {"threadId": THREAD, "command": "rm -rf build"}})
                read()  # 等 sheepdog 的回复（记进日志）
                send({"id": 901, "method": "some/unknownRequest", "params": {}})
                read()
            send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "中间的话"}}})
            send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": ANSWER}}})
            finish_turn("completed")
    else:
        send({"id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})
