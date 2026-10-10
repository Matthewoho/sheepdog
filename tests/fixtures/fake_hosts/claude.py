"""假的 `claude`（只用于单测）：模拟 `claude -p --output-format json` 的事件数组和 `claude auth status`。

  FAKE_CLAUDE_LOG     每次调用追加一行 JSON：{"argv": [...], "stdin": "..."}
  FAKE_CLAUDE_MODE    ok | notlogged | denied | capacity
  FAKE_CLAUDE_ANSWER  ok 时 result 的正文
"""
import json
import os
import sys

MODE = os.environ.get("FAKE_CLAUDE_MODE", "ok")
argv = sys.argv[1:]
if argv[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": MODE != "notlogged", "authMethod": "none" if MODE == "notlogged" else "oauth"}))
    sys.exit(0)
stdin = sys.stdin.read()
if os.environ.get("FAKE_CLAUDE_LOG"):
    with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "stdin": stdin}, ensure_ascii=False) + "\n")


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else ""


sid = opt("--session-id") or opt("--resume")
init = {"type": "system", "subtype": "init", "session_id": sid, "model": opt("--model") or "default",
        "permissionMode": "default"}
result = {"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
          "result": os.environ.get("FAKE_CLAUDE_ANSWER", "完成"), "permission_denials": []}
if MODE == "notlogged":
    result.update(is_error=True, result="Not logged in · Please run /login")
elif MODE == "denied":
    result["permission_denials"] = [{"tool_name": "Bash", "tool_use_id": "toolu_test_1", "tool_input": {"command": "lark-cli"}}]
elif MODE == "capacity":
    result.update(is_error=True, subtype="error_during_execution", result="API Error: 503 Service Unavailable")
print(json.dumps([init, {"type": "assistant", "session_id": sid}, result], ensure_ascii=False))
sys.exit(1 if result["is_error"] else 0)
