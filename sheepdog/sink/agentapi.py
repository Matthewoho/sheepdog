"""Antigravity App 投递：通过 App 内置的 agentapi 新建 / 续投 session。

依赖 App 注入的环境变量（App 托管的 sidecar 与 App 内终端都会自动带上）：
  ANTIGRAVITY_AGENTAPI_EXE、ANTIGRAVITY_LS_ADDRESS、ANTIGRAVITY_CSRF_TOKEN

agentapi 命令：
  agentapi new-conversation [--model=...] [--title=...] -- <prompt>
  agentapi send-message <conversation_id> <content>      （异步投递，不返回回答）
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
from pathlib import Path

from . import SinkError, SinkTimeout

# agentapi 一次调用最多等多少秒
AGENTAPI_TIMEOUT = 60

_REQUIRED_ENV = ("ANTIGRAVITY_AGENTAPI_EXE", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN")
_ALWAYS_STRIP_ENV = (
    "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_TRAJECTORY_ID",
)
_CALLER_SCOPED_ENV = (
    "ANTIGRAVITY_PROJECT_ID",
    "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_TRAJECTORY_ID",
    "ANTIGRAVITY_SOURCE_METADATA",
)
ENV_CACHE_FILENAME = "antigravity_env.json"


def _encode_varint(val: int) -> bytes:
    out = bytearray()
    while val > 0x7F:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)


def _decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    res = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, pos
        shift += 7
    raise ValueError("truncated protobuf varint")


def _iter_proto_fields(buf: bytes) -> list[tuple[int, int, bytes, bytes]]:
    """解析 protobuf 顶层字段，返回 (field_no, wire_type, raw_field_bytes, payload_bytes)。"""
    pos = 0
    out: list[tuple[int, int, bytes, bytes]] = []
    while pos < len(buf):
        start = pos
        tag, pos = _decode_varint(buf, pos)
        field_no = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            _, pos = _decode_varint(buf, pos)
            payload = buf[start:pos]
        elif wire_type == 1:
            pos += 8
            payload = buf[start:pos]
        elif wire_type == 2:
            length, val_start = _decode_varint(buf, pos)
            pos = val_start + length
            payload = buf[val_start:pos]
        elif wire_type == 5:
            pos += 4
            payload = buf[start:pos]
        else:
            raise ValueError(f"unsupported protobuf wire_type={wire_type}")
        if pos > len(buf):
            raise ValueError("truncated protobuf field")
        out.append((field_no, wire_type, buf[start:pos], payload))
    return out


def _len_delimited_field(field_no: int, payload: bytes) -> bytes:
    tag = (field_no << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(payload)) + payload


def _patch_trajectory_metadata_blob(blob: bytes, ref_meta_fields: dict[int, bytes]) -> tuple[bytes, bool]:
    """向 TrajectoryMetadata protobuf 补齐缺失的字段 1 (workspace)、7 (sidecar)、12、18 (project_id)。"""
    if not blob or not ref_meta_fields:
        return blob, False
    try:
        fields = _iter_proto_fields(blob)
    except ValueError:
        return blob, False
    existing_nums = {f[0] for f in fields}
    missing = [k for k in (1, 7, 12, 18) if k in ref_meta_fields and k not in existing_nums]
    if not missing:
        return blob, False
    out = bytearray()
    if 1 in missing:
        out.extend(ref_meta_fields[1])
    for _, _, raw_f, _ in fields:
        out.extend(raw_f)
    for k in (7, 12, 18):
        if k in missing:
            out.extend(ref_meta_fields[k])
    return bytes(out), True


def _patch_raw_summary_blob(
    raw_summary: bytes,
    ref_field9_raw: bytes,
    ref_meta_fields: dict[int, bytes],
) -> tuple[bytes, bool]:
    """向 conversation_summaries.raw_summary 补齐顶层字段 9 (workspace_uris) 与字段 17 内嵌元数据。"""
    if not raw_summary:
        return raw_summary, False
    try:
        fields = _iter_proto_fields(raw_summary)
    except ValueError:
        return raw_summary, False
    has_field9 = any(f[0] == 9 for f in fields)
    changed = False
    out = bytearray()
    inserted_field9 = has_field9 or not ref_field9_raw
    for fno, wtype, raw_f, payload in fields:
        if not inserted_field9 and fno > 9:
            out.extend(ref_field9_raw)
            inserted_field9 = True
            changed = True
        if fno == 17 and wtype == 2:
            patched_meta, meta_changed = _patch_trajectory_metadata_blob(payload, ref_meta_fields)
            if meta_changed:
                out.extend(_len_delimited_field(17, patched_meta))
                changed = True
            else:
                out.extend(raw_f)
        else:
            out.extend(raw_f)
    if not inserted_field9:
        out.extend(ref_field9_raw)
        changed = True
    return (bytes(out) if changed else raw_summary), changed


def reconcile_orphan_managed_conversations(
    app_data_dir: Path | None = None,
    *,
    target_cid: str | None = None,
) -> dict[str, object]:
    """自愈回填孤儿 [managed] 会话的 project_id、workspace_uris、raw_summary、FTS 与 trajectory_metadata_blob（REG-157）。"""
    base_dir = Path(app_data_dir) if app_data_dir is not None else _app_data_dir()
    summaries_db = base_dir / "conversation_summaries.db"
    convs_dir = base_dir / "conversations"
    if not summaries_db.exists():
        return {"ok": False, "reason": "summaries_db_missing", "healed_ids": [], "healed_count": 0}
    try:
        conn = sqlite3.connect(str(summaries_db), timeout=5.0)
    except sqlite3.Error as e:
        return {"ok": False, "reason": str(e), "healed_ids": [], "healed_count": 0}
    healed_ids: list[str] = []
    try:
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()
        }
        id_col = "conversation_id" if "conversation_id" in cols else "id"
        ref_row = conn.execute(
            f"""
            SELECT {id_col}, project_id, workspace_uris, raw_summary
            FROM conversation_summaries
            WHERE title LIKE '[managed]%'
              AND project_id IS NOT NULL AND project_id != ''
              AND workspace_uris IS NOT NULL AND workspace_uris != ''
              AND raw_summary IS NOT NULL
            ORDER BY CASE WHEN title = '[managed] Lark 信号·总线' THEN 0 ELSE 1 END,
                     last_modified_time DESC
            LIMIT 1
            """
        ).fetchone()
        if not ref_row:
            return {"ok": False, "reason": "no_reference_managed_conversation", "healed_ids": [], "healed_count": 0}
        _, ref_project_id, ref_workspace_uris, ref_raw_summary = ref_row
        ref_field9_chunks: list[bytes] = []
        ref_meta_fields: dict[int, bytes] = {}
        try:
            for fno, wtype, raw_f, payload in _iter_proto_fields(bytes(ref_raw_summary)):
                if fno == 9:
                    ref_field9_chunks.append(raw_f)
                elif fno == 17 and wtype == 2:
                    for mfno, _, mraw_f, _ in _iter_proto_fields(payload):
                        if mfno in (1, 7, 12, 18) and mfno not in ref_meta_fields:
                            ref_meta_fields[mfno] = mraw_f
        except ValueError:
            pass
        ref_field9_raw = b"".join(ref_field9_chunks)

        has_fts = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations_fts'"
            ).fetchone()
        )
        fts_id_col = "conversation_id"
        if has_fts:
            fts_cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(conversations_fts)").fetchall()
            }
            if "conversation_id" not in fts_cols and "id" in fts_cols:
                fts_id_col = "id"
        if target_cid:
            rows = conn.execute(
                f"""
                SELECT {id_col}, project_id, workspace_uris, raw_summary
                FROM conversation_summaries
                WHERE {id_col} = ? AND title LIKE '[managed]%'
                """,
                (target_cid,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT {id_col}, project_id, workspace_uris, raw_summary
                FROM conversation_summaries
                WHERE title LIKE '[managed]%'
                """
            ).fetchall()

        for cid, proj_id, ws_uris, raw_sum in rows:
            needs_summary_col = (proj_id != ref_project_id) or (ws_uris != ref_workspace_uris)
            patched_raw, raw_changed = _patch_raw_summary_blob(
                bytes(raw_sum) if raw_sum else b"",
                ref_field9_raw,
                ref_meta_fields,
            )
            row_touched = False
            if needs_summary_col or raw_changed:
                conn.execute(
                    f"""
                    UPDATE conversation_summaries
                    SET project_id = ?, workspace_uris = ?, raw_summary = ?
                    WHERE {id_col} = ?
                    """,
                    (ref_project_id, ref_workspace_uris, patched_raw if raw_sum is not None else raw_sum, cid),
                )
                if has_fts:
                    conn.execute(
                        f"UPDATE conversations_fts SET project = ?, workspace = ? WHERE {fts_id_col} = ?",
                        (ref_project_id, ref_workspace_uris, cid),
                    )
                row_touched = True

            conv_db = convs_dir / f"{cid}.db"
            if conv_db.exists() and ref_meta_fields:
                try:
                    c_conn = sqlite3.connect(str(conv_db), timeout=5.0)
                    try:
                        mrow = c_conn.execute(
                            "SELECT data FROM trajectory_metadata_blob WHERE id = 'main'"
                        ).fetchone()
                        if mrow and mrow[0]:
                            patched_meta, meta_changed = _patch_trajectory_metadata_blob(
                                bytes(mrow[0]), ref_meta_fields
                            )
                            if meta_changed:
                                c_conn.execute(
                                    "UPDATE trajectory_metadata_blob SET data = ? WHERE id = 'main'",
                                    (patched_meta,),
                                )
                                c_conn.commit()
                                row_touched = True
                    finally:
                        c_conn.close()
                except sqlite3.Error:
                    pass

            if row_touched:
                healed_ids.append(cid)

        esc_proj = str(ref_project_id).replace("'", "''")
        esc_ws = str(ref_workspace_uris).replace("'", "''")
        fts_stmt = (
            f"UPDATE conversations_fts SET project = '{esc_proj}', workspace = '{esc_ws}' WHERE {fts_id_col} = NEW.{id_col};"
            if has_fts
            else ""
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_sheepdog_managed_project_insert
            AFTER INSERT ON conversation_summaries
            WHEN NEW.title LIKE '[managed]%'
             AND (NEW.project_id IS NULL OR NEW.project_id = '' OR NEW.workspace_uris IS NULL OR NEW.workspace_uris = '')
            BEGIN
                UPDATE conversation_summaries
                SET project_id = '{esc_proj}', workspace_uris = '{esc_ws}'
                WHERE {id_col} = NEW.{id_col};
                {fts_stmt}
            END;
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_sheepdog_managed_project_update
            AFTER UPDATE ON conversation_summaries
            WHEN NEW.title LIKE '[managed]%'
             AND (NEW.project_id IS NULL OR NEW.project_id = '' OR NEW.workspace_uris IS NULL OR NEW.workspace_uris = '')
            BEGIN
                UPDATE conversation_summaries
                SET project_id = '{esc_proj}', workspace_uris = '{esc_ws}'
                WHERE {id_col} = NEW.{id_col};
                {fts_stmt}
            END;
            """
        )
        conn.commit()
        return {
            "ok": True,
            "project_id": ref_project_id,
            "workspace_uris": ref_workspace_uris,
            "healed_ids": healed_ids,
            "healed_count": len(healed_ids),
        }
    except sqlite3.Error as e:
        return {"ok": False, "reason": str(e), "healed_ids": healed_ids, "healed_count": len(healed_ids)}
    finally:
        conn.close()


def is_ls_address_reachable(ls_address: str, timeout: float = 0.5) -> bool:
    """探测 ANTIGRAVITY_LS_ADDRESS（如 localhost:52430）TCP 端口是否存活可达（REG-088）。"""
    addr = (ls_address or "").strip()
    if not addr or ":" not in addr:
        return False
    host, _, port_str = addr.rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    try:
        port = int(port_str)
    except ValueError:
        return False
    if port <= 0 or port > 65535:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def save_live_agentapi_env(state_dir: Path | None) -> bool:
    """当当前进程携带存活可达的 ANTIGRAVITY_* 凭据时，原子写入 state_dir/antigravity_env.json（REG-088 / REG-157）。"""
    if state_dir is None:
        return False
    if any(not os.environ.get(k) for k in _REQUIRED_ENV):
        return False
    ls_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "")
    if not is_ls_address_reachable(ls_addr):
        return False
    try:
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        target = state_dir / ENV_CACHE_FILENAME
        existing: dict[str, object] = {}
        if target.exists():
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                existing = {}
        env_meta = os.environ.get("ANTIGRAVITY_SOURCE_METADATA", "")
        is_sidecar = '"sidecar"' in env_meta
        proj_id = (
            os.environ.get("ANTIGRAVITY_PROJECT_ID", "")
            if is_sidecar or not existing.get("ANTIGRAVITY_PROJECT_ID")
            else str(existing.get("ANTIGRAVITY_PROJECT_ID") or "")
        )
        sidecar_meta = (
            env_meta
            if is_sidecar
            else str(existing.get("ANTIGRAVITY_SOURCE_METADATA") or "")
        )
        tmp = state_dir / f".{ENV_CACHE_FILENAME}.tmp.{os.getpid()}"
        payload = {
            "ANTIGRAVITY_AGENTAPI_EXE": os.environ["ANTIGRAVITY_AGENTAPI_EXE"],
            "ANTIGRAVITY_LS_ADDRESS": ls_addr,
            "ANTIGRAVITY_CSRF_TOKEN": os.environ["ANTIGRAVITY_CSRF_TOKEN"],
            "ANTIGRAVITY_APP_DATA_DIR": os.environ.get("ANTIGRAVITY_APP_DATA_DIR", ""),
            "ANTIGRAVITY_PROJECT_ID": proj_id,
            "ANTIGRAVITY_SOURCE_METADATA": sidecar_meta,
            "pid": os.getpid(),
            "ppid": os.getppid(),
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


def load_live_agentapi_env(state_dir: Path | None, *, include_project: bool = False) -> dict[str, str] | None:
    """从 state_dir/antigravity_env.json 读取且经 TCP 存活验证的最新宿主凭据（REG-088 / REG-157）。"""
    if state_dir is None:
        return None
    target = Path(state_dir) / ENV_CACHE_FILENAME
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or any(not data.get(k) for k in _REQUIRED_ENV):
        return None
    ls_addr = str(data.get("ANTIGRAVITY_LS_ADDRESS") or "")
    if not is_ls_address_reachable(ls_addr):
        return None
    out = {k: str(data[k]) for k in _REQUIRED_ENV}
    if data.get("ANTIGRAVITY_APP_DATA_DIR"):
        out["ANTIGRAVITY_APP_DATA_DIR"] = str(data["ANTIGRAVITY_APP_DATA_DIR"])
    if include_project:
        if data.get("ANTIGRAVITY_PROJECT_ID"):
            out["ANTIGRAVITY_PROJECT_ID"] = str(data["ANTIGRAVITY_PROJECT_ID"])
        if data.get("ANTIGRAVITY_SOURCE_METADATA") and '"sidecar"' in str(data["ANTIGRAVITY_SOURCE_METADATA"]):
            out["ANTIGRAVITY_SOURCE_METADATA"] = str(data["ANTIGRAVITY_SOURCE_METADATA"])
    return out


def _app_data_dir() -> Path:
    return Path(os.environ.get("ANTIGRAVITY_APP_DATA_DIR") or Path.home() / ".gemini" / "antigravity")


def conversation_dir(conversation_id: str) -> Path:
    """会话目录：transcript 和会话产出的文档都在这里。"""
    return _app_data_dir() / "brain" / conversation_id


def transcript_path(conversation_id: str) -> Path:
    return conversation_dir(conversation_id) / ".system_generated" / "logs" / "transcript.jsonl"


def transcript_contains(conversation_id: str, text: str) -> bool:
    """会话 transcript 里有没有出现过 text（用于发送中断后核对批次是否已送达，7.16）。"""
    p = transcript_path(conversation_id)
    if not text or not p.exists():
        return False
    with p.open(encoding="utf-8", errors="replace") as f:
        return any(text in line for line in f)


class AgentApiSink:
    name = "agentapi"
    host = "antigravity"

    def __init__(self, state_dir: Path | None = None):
        self.state_dir = Path(state_dir) if state_dir is not None else None

    def _reload_saved_env(self, *, force: bool = False) -> bool:
        saved = load_live_agentapi_env(self.state_dir)
        if saved is None:
            return False
        if not force and all(os.environ.get(k) for k in _REQUIRED_ENV):
            return False
        if (
            force
            and saved.get("ANTIGRAVITY_LS_ADDRESS") == os.environ.get("ANTIGRAVITY_LS_ADDRESS")
            and saved.get("ANTIGRAVITY_CSRF_TOKEN") == os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
        ):
            return False
        os.environ.update(saved)
        return True

    def available(self) -> tuple[bool, str]:
        missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
        if missing and self._reload_saved_env():
            missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            return False, f"缺少环境变量 {missing}：需在 Antigravity App 托管的 sidecar 或 App 内终端中运行"
        return True, "ok"

    def _build_cmd_env(self, args: list[str]) -> dict[str, str]:
        """按动词区分环境变量（REG-098 / REG-157）：
        - send-message：剥离 ANTIGRAVITY_PROJECT_ID 与 ANTIGRAVITY_SOURCE_METADATA，防止跨项目投递报 PermissionDenied；
        - new-conversation：保留/恢复母羊 sidecar 的 ANTIGRAVITY_PROJECT_ID 与 sidecar ANTIGRAVITY_SOURCE_METADATA，确保新会话归属 sheepdog 项目与工作区。
        """
        verb = args[0] if args else ""
        if verb != "new-conversation":
            return {k: v for k, v in os.environ.items() if k not in _CALLER_SCOPED_ENV}
        cmd_env = {k: v for k, v in os.environ.items() if k not in _ALWAYS_STRIP_ENV}
        cur_meta = cmd_env.get("ANTIGRAVITY_SOURCE_METADATA", "")
        if cur_meta and '"sidecar"' not in cur_meta:
            cmd_env.pop("ANTIGRAVITY_SOURCE_METADATA", None)
        if self.state_dir is not None:
            target = Path(self.state_dir) / ENV_CACHE_FILENAME
            if target.exists():
                try:
                    saved = json.loads(target.read_text(encoding="utf-8"))
                    if isinstance(saved, dict):
                        if saved.get("ANTIGRAVITY_PROJECT_ID"):
                            cmd_env["ANTIGRAVITY_PROJECT_ID"] = str(saved["ANTIGRAVITY_PROJECT_ID"])
                        if saved.get("ANTIGRAVITY_SOURCE_METADATA") and '"sidecar"' in str(saved["ANTIGRAVITY_SOURCE_METADATA"]):
                            cmd_env["ANTIGRAVITY_SOURCE_METADATA"] = str(saved["ANTIGRAVITY_SOURCE_METADATA"])
                except (OSError, json.JSONDecodeError):
                    pass
        return cmd_env

    def _call(self, args: list[str]) -> dict:
        ok, why = self.available()
        if not ok:
            raise SinkError(why)
        exe = os.environ["ANTIGRAVITY_AGENTAPI_EXE"]
        cmd_env = self._build_cmd_env(args)
        try:
            proc = subprocess.run([exe, "agentapi", *args], env=cmd_env, capture_output=True, text=True, timeout=AGENTAPI_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            # 7.47 第 7 条：不再冲出 dispatch_once 中断整轮。send-message 超时可能其实送到了，由调用方按 SinkTimeout 处理
            raise SinkTimeout(f"agentapi {args[0] if args else ''} 超过 {AGENTAPI_TIMEOUT} 秒没返回（可能已经送到）") from e
        out = proc.stdout.strip()
        start = out.find("{")
        if proc.returncode != 0 or start < 0:
            err_tail = (proc.stderr or out)[-400:]
            err_low = err_tail.lower()
            if (
                ("connection refused" in err_low or "code = unavailable" in err_low or "csrf" in err_low)
                and self._reload_saved_env(force=True)
            ):
                exe = os.environ["ANTIGRAVITY_AGENTAPI_EXE"]
                cmd_env = self._build_cmd_env(args)
                try:
                    proc = subprocess.run([exe, "agentapi", *args], env=cmd_env, capture_output=True, text=True, timeout=AGENTAPI_TIMEOUT)
                except subprocess.TimeoutExpired as e:
                    raise SinkTimeout(f"agentapi {args[0] if args else ''} 超过 {AGENTAPI_TIMEOUT} 秒没返回（可能已经送到）") from e
                out = proc.stdout.strip()
                start = out.find("{")
        # language_server 会往 stderr 打启动日志，只解析 stdout 里的 JSON
        if proc.returncode != 0 or start < 0:
            raise SinkError(f"agentapi 失败 rc={proc.returncode}: {(proc.stderr or out)[-400:]}")
        try:
            return json.loads(out[start:])
        except json.JSONDecodeError as e:
            raise SinkError(f"agentapi 输出无法解析: {out[:300]}") from e

    def new_conversation(self, title: str, prompt: str, model: str = "", effort: str = "", **_ctx) -> str:
        # agentapi 没有推理档位参数，effort 忽略
        args = ["new-conversation"]
        if model:
            args.append(f"--model={model}")
        args += [f"--title={title}", "--", prompt]
        data = self._call(args)
        cid = ((data.get("response") or {}).get("newConversation") or {}).get("conversationId")
        if not cid:
            raise SinkError(f"agentapi 未返回 conversationId: {data}")
        if title.startswith("[managed]"):
            try:
                reconcile_orphan_managed_conversations(target_cid=cid)
            except Exception:
                pass
        return cid

    def send_message(self, conversation_id: str, content: str, **_ctx) -> None:
        data = self._call(["send-message", conversation_id, content])
        if not (data.get("response") or {}).get("sendMessage"):
            raise SinkError(f"agentapi send-message 异常: {data}")
        try:
            reconcile_orphan_managed_conversations(target_cid=conversation_id)
        except Exception:
            pass

    def transcript_contains(self, conversation_id: str, text: str) -> bool:
        return transcript_contains(conversation_id, text)

    def history(self, conversation_id: str, limit: int = 6) -> list[tuple[str, str, str]]:
        """transcript 里最近几条 USER_INPUT / PLANNER_RESPONSE：(角色, 正文, 时间)。"""
        p = transcript_path(conversation_id)
        if not p.exists():
            return []
        out: list[tuple[str, str, str]] = []
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = d.get("type") if isinstance(d, dict) else None
                if kind not in ("USER_INPUT", "PLANNER_RESPONSE"):
                    continue
                text = d.get("content")
                if not isinstance(text, str):
                    text = json.dumps(text, ensure_ascii=False)
                out.append(("问" if kind == "USER_INPUT" else "答", text, d.get("created_at") or ""))
        return out[-limit:]

    def resume_hint(self, conversation_id: str, title: str = "") -> str:
        return f"在 Antigravity App 里打开会话 {conversation_id}"


class DryRunSink:
    """不真正投递，只打印，用于调试规则。每个宿主一个，打印时带上宿主名（7.18）。"""

    name = "dryrun"

    def __init__(self, host: str = "antigravity"):
        self.host = host
        self._n = 0

    @property
    def _tag(self) -> str:
        return "[dryrun]" if self.host == "antigravity" else f"[dryrun:{self.host}]"

    def available(self) -> tuple[bool, str]:
        return True, "dry-run"

    def new_conversation(self, title: str, prompt: str, model: str = "", effort: str = "", **_ctx) -> str:
        self._n += 1
        cid = f"dryrun-{self._n}" if self.host == "antigravity" else f"dryrun-{self.host}-{self._n}"
        extra = "".join(f" {k}={v}" for k, v in (("model", model), ("effort", effort)) if v)
        print(f"{self._tag} new-conversation title={title!r}{extra} -> {cid}\n{prompt}\n")
        return cid

    def send_message(self, conversation_id: str, content: str, **_ctx) -> None:
        extra = "".join(f" {k}={_ctx[k]}" for k in ("model", "effort") if _ctx.get(k))
        print(f"{self._tag} send-message -> {conversation_id}{extra}\n{content}\n")

    def transcript_contains(self, conversation_id: str, text: str) -> bool:
        return False
