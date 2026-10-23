"""飞书原生常驻 WebSocket 客户端（规格 7.77，REG-027 ~ REG-029）。"""
from __future__ import annotations

import base64, ctypes, ctypes.util, json, logging, os, socket, ssl, struct, subprocess, threading, time
import urllib.parse, urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger("sheepdog")
DEFAULT_TOAST_TEXT = "🐕 已收到拍板，正在处理..."

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

def _load_libcrypto():
    for path in ("/opt/homebrew/opt/openssl@3/lib/libcrypto.dylib", "/usr/local/opt/openssl@3/lib/libcrypto.dylib",
                 ctypes.util.find_library("crypto") or "", "/usr/lib/libcrypto.dylib"):
        if not path: continue
        try:
            lib = ctypes.CDLL(path)
            vp, ci, cp, pi = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)
            for fn, args, res in (
                ("EVP_aes_256_gcm", [], vp), ("EVP_CIPHER_CTX_new", [], vp), ("EVP_CIPHER_CTX_free", [vp], None),
                ("EVP_DecryptInit_ex", [vp, vp, vp, cp, cp], ci), ("EVP_CIPHER_CTX_ctrl", [vp, ci, ci, vp], ci),
                ("EVP_DecryptUpdate", [vp, cp, pi, cp, ci], ci), ("EVP_DecryptFinal_ex", [vp, cp, pi], ci),
            ):
                getattr(lib, fn).argtypes, getattr(lib, fn).restype = args, res
            return lib
        except Exception: continue
    return None

def _decrypt_aes_256_gcm(key: bytes, blob: bytes) -> str | None:
    if len(key) != 32 or len(blob) <= 28 or (lib := _load_libcrypto()) is None: return None
    nonce, ct, tag = blob[:12], blob[12:-16], blob[-16:]
    if not (ctx := lib.EVP_CIPHER_CTX_new()): return None
    try:
        out, out_len, fin_len = ctypes.create_string_buffer(len(ct) + 16), ctypes.c_int(0), ctypes.c_int(0)
        tag_buf = ctypes.create_string_buffer(tag, len(tag))
        ok = (lib.EVP_DecryptInit_ex(ctx, lib.EVP_aes_256_gcm(), None, None, None) == 1
              and lib.EVP_CIPHER_CTX_ctrl(ctx, 0x9, len(nonce), None) == 1
              and lib.EVP_DecryptInit_ex(ctx, None, None, key, nonce) == 1
              and lib.EVP_DecryptUpdate(ctx, out, ctypes.byref(out_len), ct, len(ct)) == 1
              and lib.EVP_CIPHER_CTX_ctrl(ctx, 0x11, len(tag), ctypes.cast(tag_buf, ctypes.c_void_p)) == 1
              and lib.EVP_DecryptFinal_ex(ctx, ctypes.cast(ctypes.addressof(out) + out_len.value, ctypes.c_char_p), ctypes.byref(fin_len)) == 1)
        return out.raw[: out_len.value + fin_len.value].decode("utf-8").strip() if ok else None
    except Exception: return None
    finally: lib.EVP_CIPHER_CTX_free(ctx)

def _resolve_encrypted_secret(app_id: str, sec_id: str, app_support_dir: str | None) -> str | None:
    sup = Path(app_support_dir).expanduser() if app_support_dir else (Path.home() / "Library/Application Support/lark-cli")
    if (key_file := sup / "master.key.file").is_file():
        for enc_file in (sup / f"{sec_id.replace(':', '_')}.enc", sup / f"appsecret_{app_id}.enc"):
            if enc_file.is_file():
                try:
                    if plain := _decrypt_aes_256_gcm(key_file.read_bytes(), enc_file.read_bytes()): return plain
                except Exception: pass
    try:
        res = subprocess.run(["security", "find-generic-password", "-s", "lark-cli", "-a", sec_id, "-w"],
                             capture_output=True, text=True, timeout=3.0, check=False)
        return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None
    except Exception: return None

def load_feishu_app_credentials(lark_cli_dir: str | None = None, app_support_dir: str | None = None) -> dict | None:
    env_id, env_sec = os.environ.get("SHEEPDOG_FEISHU_APP_ID", "").strip(), os.environ.get("SHEEPDOG_FEISHU_APP_SECRET", "").strip()
    env_dom = os.environ.get("SHEEPDOG_FEISHU_DOMAIN", "").strip().rstrip("/")
    if env_id and env_sec:
        return {"app_id": env_id, "app_secret": env_sec, "domain": env_dom or "https://open.larksuite.com"}
    try:
        cfg_dir = Path(lark_cli_dir).expanduser() if lark_cli_dir else (Path.home() / ".lark-cli")
        cfg = json.loads((cfg_dir / "config.json").read_text(encoding="utf-8"))
        app = (cfg.get("apps") or [None])[0] or {}
        if not (app_id := str(app.get("appId") or "").strip()): return None
        brand = str(app.get("brand") or "lark").strip().lower()
        domain = env_dom or ("https://open.feishu.cn" if brand == "feishu" else "https://open.larksuite.com")
        raw_sec = app.get("appSecret")
        if isinstance(raw_sec, str) and raw_sec.strip():
            return {"app_id": app_id, "app_secret": raw_sec.strip(), "domain": domain, "brand": brand}
        if isinstance(raw_sec, dict) and (secret := _resolve_encrypted_secret(app_id, str(raw_sec.get("id") or f"appsecret:{app_id}").strip(), app_support_dir)):
            return {"app_id": app_id, "app_secret": secret, "domain": domain, "brand": brand}
    except Exception: return None
    return None

@dataclass
class PbFrame:
    seq_id: int = 0
    log_id: int = 0
    service: int = 0
    method: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    payload_encoding: str = ""
    payload_type: str = ""
    payload: bytes = b""
    log_id_new: str = ""

def _encode_varint(val: int) -> bytes:
    v, out = int(val) & ((1 << 64) - 1), bytearray()
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)

def _decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    res, shift, n = 0, 0, len(buf)
    while pos < n:
        b, pos = buf[pos], pos + 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80): return res, pos
        shift += 7
        if shift >= 64: raise ValueError("varint overflow")
    raise ValueError("truncated varint")

def _encode_len_field(field_no: int, data: bytes) -> bytes:
    return _encode_varint((field_no << 3) | 2) + _encode_varint(len(data)) + data

def encode_pb_frame(frame: PbFrame) -> bytes:
    out = bytearray()
    for fno, val in ((1, frame.seq_id), (2, frame.log_id), (3, frame.service), (4, frame.method)):
        out.extend(_encode_varint((fno << 3) | 0) + _encode_varint(val))
    for k, v in (frame.headers or {}).items():
        out.extend(_encode_len_field(5, _encode_len_field(1, str(k).encode("utf-8")) + _encode_len_field(2, str(v).encode("utf-8"))))
    for fno, s in ((6, frame.payload_encoding), (7, frame.payload_type), (9, frame.log_id_new)):
        if s: out.extend(_encode_len_field(fno, s.encode("utf-8")))
    if frame.payload: out.extend(_encode_len_field(8, bytes(frame.payload)))
    return bytes(out)

def decode_pb_frame(buf: bytes) -> PbFrame:
    fr, pos, n = PbFrame(), 0, len(buf)
    while pos < n:
        tag, pos = _decode_varint(buf, pos)
        fno, wtype = tag >> 3, tag & 0x7
        if wtype == 0:
            val, pos = _decode_varint(buf, pos)
            if fno in (1, 2, 3, 4): setattr(fr, ("seq_id", "log_id", "service", "method")[fno - 1], val)
        elif wtype == 2:
            ln, pos = _decode_varint(buf, pos)
            chunk, pos = buf[pos : pos + ln], pos + ln
            if fno == 5:
                hk, hv, sp = "", "", 0
                while sp < len(chunk):
                    stag, sp = _decode_varint(chunk, sp)
                    sfno, swt = stag >> 3, stag & 0x7
                    if swt == 2:
                        sln, sp = _decode_varint(chunk, sp)
                        sval, sp = chunk[sp : sp + sln].decode("utf-8", errors="replace"), sp + sln
                        hk, hv = (sval, hv) if sfno == 1 else (hk, sval if sfno == 2 else hv)
                    elif swt == 0: _, sp = _decode_varint(chunk, sp)
                    else: break
                if hk: fr.headers[hk] = hv
            elif fno == 8: fr.payload = bytes(chunk)
            elif fno in (6, 7, 9): setattr(fr, {6: "payload_encoding", 7: "payload_type", 9: "log_id_new"}[fno], chunk.decode("utf-8", errors="replace"))
        elif wtype in (1, 5): pos += 8 if wtype == 1 else 4
        else: raise ValueError(f"unsupported wire type {wtype}")
    return fr

def build_ping_frame(service_id: int, seq_id: int = 1) -> PbFrame:
    return PbFrame(seq_id=seq_id, log_id=0, service=int(service_id), method=0, headers={"type": "ping"})

def build_ack_frame(req: PbFrame, code: int = 200, toast_text: str | None = None, biz_rt_ms: int = 1) -> PbFrame:
    headers = {**(req.headers or {}), "biz_rt": str(max(1, int(biz_rt_ms)))}
    inner = json.dumps({"toast": {"type": "info", "content": toast_text}}, ensure_ascii=False).encode("utf-8") if toast_text else b""
    payload = json.dumps({"code": int(code), "headers": {}, "data": base64.b64encode(inner).decode("ascii") if inner else ""}, ensure_ascii=False).encode("utf-8")
    return PbFrame(seq_id=req.seq_id, log_id=req.log_id, service=req.service, method=1, headers=headers, payload=payload, log_id_new=req.log_id_new)

def normalize_ws_event_payload(raw_obj: dict, frame_headers: dict[str, str] | None = None) -> dict | None:
    if not isinstance(raw_obj, dict): return None
    fh, hdr = frame_headers or {}, raw_obj.get("header") if isinstance(raw_obj.get("header"), dict) else {}
    ev = raw_obj.get("event") if isinstance(raw_obj.get("event"), dict) else raw_obj
    ev_type = str(hdr.get("event_type") or raw_obj.get("event_type") or raw_obj.get("type") or fh.get("type") or "").strip()
    if ev_type == "card.action.trigger" or fh.get("type") == "card" or "action" in ev:
        act, op, ctx = (ev.get(k) if isinstance(ev.get(k), dict) else {} for k in ("action", "operator", "context"))
        return {
            **raw_obj, "schema": raw_obj.get("schema", "2.0"), "header": hdr, "event": ev, "type": "card.action.trigger", "event_type": "card.action.trigger",
            "event_id": hdr.get("event_id") or raw_obj.get("event_id") or fh.get("message_id") or "",
            "timestamp": hdr.get("create_time") or raw_obj.get("timestamp") or raw_obj.get("create_time") or "",
            "token": ev.get("token") or hdr.get("token") or raw_obj.get("token") or "",
            "operator_id": op.get("open_id") or op.get("user_id") or ev.get("operator_id") or raw_obj.get("operator_id") or "",
            "action_tag": act.get("tag") or ev.get("action_tag") or raw_obj.get("action_tag") or "button",
            "action_name": act.get("name") or ev.get("action_name") or raw_obj.get("action_name") or "",
            "action_value": act.get("value") if "value" in act else ev.get("action_value", raw_obj.get("action_value")),
            "form_value": act.get("form_value") if "form_value" in act else ev.get("form_value", raw_obj.get("form_value")),
            "input_value": act.get("input_value") if "input_value" in act else ev.get("input_value", raw_obj.get("input_value")),
            "option": act.get("option") if "option" in act else ev.get("option", raw_obj.get("option")),
            "message_id": ctx.get("open_message_id") or ev.get("open_message_id") or ev.get("message_id") or raw_obj.get("message_id") or "",
            "chat_id": ctx.get("open_chat_id") or ev.get("open_chat_id") or ev.get("chat_id") or raw_obj.get("chat_id") or "",
        }
    if ev_type == "im.message.receive_v1":
        if "message_id" in raw_obj and not isinstance(ev.get("message"), dict):
            return {**raw_obj, "type": "im.message.receive_v1", "event_type": "im.message.receive_v1"}
        msg, snd = (ev.get(k) if isinstance(ev.get(k), dict) else {} for k in ("message", "sender"))
        msg_type, raw_c = str(msg.get("message_type") or msg.get("msg_type") or ""), msg.get("content") or ""
        mentions = [{"id": (m.get("id") or {}).get("open_id") if isinstance(m.get("id"), dict) else (m.get("id") or ""),
                     "key": str(m.get("key") or ""), "name": str(m.get("name") or "")} for m in (msg.get("mentions") or []) if isinstance(m, dict)]
        content_str = raw_c if isinstance(raw_c, str) else json.dumps(raw_c, ensure_ascii=False)
        if msg_type == "text" and isinstance(raw_c, str):
            try:
                if isinstance(parsed_c := json.loads(raw_c), dict) and isinstance(parsed_c.get("text"), str):
                    content_str = parsed_c["text"]
                    for m in mentions:
                        if m["key"] and m["name"]: content_str = content_str.replace(m["key"], f"@{m['name']}")
            except Exception: pass
        snd_type, snd_id_obj = str(snd.get("sender_type") or ""), snd.get("sender_id")
        sender_id = (snd_id_obj.get("open_id") or snd_id_obj.get("user_id") or "") if isinstance(snd_id_obj, dict) else str(snd_id_obj or "")
        return {
            "type": "im.message.receive_v1", "event_id": hdr.get("event_id") or raw_obj.get("event_id") or "", "event_type": "im.message.receive_v1",
            "id": msg.get("message_id") or "", "message_id": msg.get("message_id") or "", "chat_id": msg.get("chat_id") or "", "chat_type": msg.get("chat_type") or "",
            "message_type": msg_type, "msg_type": msg_type, "content": content_str, "sender_id": sender_id, "sender_type": "bot" if snd_type == "app" else snd_type,
            "create_time": msg.get("create_time") or hdr.get("create_time") or "", "reply_to": msg.get("parent_id") or "",
            "root_id": msg.get("root_id") or "", "thread_id": msg.get("thread_id") or "", "mentions": mentions,
        }
    return None

def fetch_ws_endpoint(app_id: str, app_secret: str, domain: str = "https://open.larksuite.com",
                      timeout: float = 10.0, opener=urllib.request.urlopen) -> dict:
    url, body = f"{domain.rstrip('/')}/callback/ws/endpoint", json.dumps({"AppID": app_id, "AppSecret": app_secret}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "locale": "zh"}, method="POST")
    with opener(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if payload.get("code", 0) not in (0, None) or not (ws_url := data.get("URL") or payload.get("URL") or ""):
        raise RuntimeError(f"ws endpoint rejected: code={payload.get('code')} msg={payload.get('msg')}")
    return {"url": str(ws_url), "client_config": data.get("ClientConfig") or payload.get("ClientConfig") or {}}

class _RawWebSocket:
    def __init__(self, ws_url: str, connect_timeout: float = 8.0, read_timeout: float = 0.5):
        parsed = urllib.parse.urlsplit(ws_url)
        scheme, host = (parsed.scheme or "wss").lower(), parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if scheme == "wss" else 80)
        path, qs = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else ""), urllib.parse.parse_qs(parsed.query)
        self.service_id = int((qs.get("service_id") or ["0"])[0] or 0)
        self.conn_id = (qs.get("device_id") or qs.get("conn_id") or [f"svc_{self.service_id}"])[0]
        raw_sock = socket.create_connection((host, port), timeout=connect_timeout)
        try: raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception: pass
        self.sock = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=host) if scheme == "wss" else raw_sock
        self._send_lock, self._rx_buf = threading.Lock(), bytearray()
        self._handshake(host, port, path)
        self.sock.settimeout(read_timeout)

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode("ascii"))
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            if not (chunk := self.sock.recv(4096)): raise ConnectionResetError("ws handshake eof")
            buf.extend(chunk)
        idx = buf.index(b"\r\n\r\n") + 4
        hdr_text = bytes(buf[:idx]).decode("latin-1", errors="replace")
        self._rx_buf.extend(buf[idx:])
        if " 101 " not in (hdr_text.splitlines()[0] if hdr_text else ""):
            raise ConnectionError(f"ws handshake failed: {hdr_text.splitlines()[:1]}")

    def _read_exact(self, n: int) -> bytes:
        while len(self._rx_buf) < n:
            if not (chunk := self.sock.recv(max(4096, n - len(self._rx_buf)))): raise ConnectionResetError("ws socket closed")
            self._rx_buf.extend(chunk)
        out = bytes(self._rx_buf[:n])
        del self._rx_buf[:n]
        return out

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        data, n, mask = bytes(payload), len(payload), os.urandom(4)
        hdr = bytearray([0x80 | (opcode & 0x0F)])
        hdr.extend([0x80 | n] if n < 126 else (b"\xfe" + struct.pack("!H", n) if n < 65536 else b"\xff" + struct.pack("!Q", n)))
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        with self._send_lock:
            self.sock.sendall(bytes(hdr) + mask + masked)

    def recv_frame(self) -> tuple[int, bytes]:
        frag_op, frag_buf = 0, bytearray()
        while True:
            b0, b1 = self._read_exact(2)
            fin, op, masked, ln = (b0 >> 7) & 1, b0 & 0x0F, (b1 >> 7) & 1, b1 & 0x7F
            if ln in (126, 127): ln = struct.unpack("!H" if ln == 126 else "!Q", self._read_exact(2 if ln == 126 else 8))[0]
            mask = self._read_exact(4) if masked else b""
            data = self._read_exact(ln) if ln else b""
            if masked: data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            if op == 0x8:
                try: self.send_frame(0x8, b"")
                except Exception: pass
                raise ConnectionResetError("ws closed by server")
            if op in (0x9, 0xA):
                if op == 0x9: self.send_frame(0xA, data)
                return op, data
            if op == 0x0:
                frag_buf.extend(data)
                if fin: return frag_op, bytes(frag_buf)
            elif fin: return op, data
            else: frag_op, frag_buf = op, bytearray(data)

    def close(self) -> None:
        try: self.sock.close()
        except Exception: pass

class FeishuNativeWSClient:
    def __init__(self, credentials: dict, on_event: Callable[[dict], None], on_state: Callable[[dict], None] | None = None,
                 ping_interval: float = 15.0, pong_timeout: float = 8.0, backoff: float = 1.0, backoff_max: float = 15.0,
                 toast_text: str = DEFAULT_TOAST_TEXT, endpoint_fetcher=fetch_ws_endpoint, clock=time.monotonic):
        self.credentials, self.on_event, self.on_state = dict(credentials or {}), on_event, on_state
        self.ping_interval, self.pong_timeout, self.backoff, self.backoff_max = float(ping_interval), float(pong_timeout), float(backoff), float(backoff_max)
        self.toast_text, self.endpoint_fetcher, self.clock = toast_text, endpoint_fetcher, clock
        self._stop_evt, self._reconnect_evt, self._thread, self._ws = threading.Event(), threading.Event(), None, None
        self._seq, self._connected_in_round, self._multipart = 0, False, {}
        self._state = {
            "mode": "native_ws", "state": "stopped", "pid": os.getpid(), "started_at": "", "ready_at": "",
            "last_event_at": "", "last_ping_at": "", "last_pong_at": "", "last_pong_mono": 0.0,
            "last_ack_ms": 0.0, "events": 0, "restarts": 0, "zombie_restarts": 0, "last_exit": "", "last_error": "", "conn_id": "", "ws_url": "",
        }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive(): return
        self._stop_evt.clear(); self._reconnect_evt.clear()
        self._state.update(state="starting", pid=os.getpid(), started_at=_now_iso())
        self._thread = threading.Thread(target=self._run_loop, name="sheepdog-feishu-ws", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_evt.set(); self._reconnect_evt.set()
        if self._ws is not None: self._ws.close()
        if self._thread is not None and self._thread.is_alive(): self._thread.join(timeout=timeout)
        self._state["state"] = "stopped"; self._notify_state()

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop_evt.is_set()

    def force_reconnect(self) -> None:
        self._reconnect_evt.set()
        if self._ws is not None: self._ws.close()

    def snapshot(self) -> dict:
        return dict(self._state)

    def _notify_state(self) -> None:
        if self.on_state is not None:
            try: self.on_state(self.snapshot())
            except Exception: pass

    def _run_loop(self) -> None:
        delay, first = self.backoff, True
        while not self._stop_evt.is_set():
            if not first: self._state["restarts"] += 1
            first, self._connected_in_round = False, False
            self._reconnect_evt.clear()
            try: self._serve_once()
            except Exception as e:
                self._state.update(state="down", last_error=str(e)[:240], last_exit=f"{_now_iso()} {e}"[:240])
                self._notify_state()
            if self._connected_in_round: delay = self.backoff
            if self._stop_evt.is_set(): break
            self._stop_evt.wait(max(0.01, delay))
            delay = min(delay * 2.0, self.backoff_max)

    def _serve_once(self) -> None:
        ep = self.endpoint_fetcher(self.credentials.get("app_id", ""), self.credentials.get("app_secret", ""),
                                   self.credentials.get("domain", "https://open.larksuite.com"))
        ws_url, read_to = ep["url"], max(0.02, min(1.0, self.ping_interval / 2.0, self.pong_timeout / 2.0))
        ws = _RawWebSocket(ws_url, connect_timeout=max(1.0, self.pong_timeout), read_timeout=read_to)
        self._ws, self._connected_in_round, now_m = ws, True, self.clock()
        self._state.update(state="running", ready_at=_now_iso(), conn_id=ws.conn_id, ws_url=ws_url,
                           last_pong_at=_now_iso(), last_pong_mono=now_m, last_error="")
        self._notify_state()
        last_ping_m, awaiting_pong_since = now_m, None
        try:
            while not self._stop_evt.is_set() and not self._reconnect_evt.is_set():
                try:
                    op, data = ws.recv_frame()
                    now_m, awaiting_pong_since = self.clock(), None
                    self._state.update(last_pong_at=_now_iso(), last_pong_mono=now_m)
                    if op == 0x2: self._handle_binary_frame(ws, data)
                except (socket.timeout, TimeoutError): pass
                now_m = self.clock()
                if awaiting_pong_since is not None and (now_m - awaiting_pong_since) >= self.pong_timeout:
                    self._state["zombie_restarts"] += 1
                    raise TimeoutError(f"half-open zombie detected (no pong in {self.pong_timeout}s)")
                if (now_m - last_ping_m) >= self.ping_interval:
                    self._seq += 1
                    ws.send_frame(0x2, encode_pb_frame(build_ping_frame(ws.service_id, self._seq)))
                    ws.send_frame(0x9, b"ping")
                    last_ping_m, self._state["last_ping_at"] = now_m, _now_iso()
                    if awaiting_pong_since is None: awaiting_pong_since = now_m
        finally:
            self._ws = None
            ws.close()

    def _handle_binary_frame(self, ws: _RawWebSocket, data: bytes) -> None:
        t0, pb = time.perf_counter(), decode_pb_frame(data)
        if pb.method == 0:
            if pb.headers.get("type") == "ping":
                ws.send_frame(0x2, encode_pb_frame(PbFrame(seq_id=pb.seq_id, log_id=pb.log_id, service=pb.service, method=0, headers={**pb.headers, "type": "pong"})))
            return
        if pb.method != 1: return
        is_card = pb.headers.get("type") == "card" or b"card.action.trigger" in pb.payload or b'"action"' in pb.payload
        rt_ms = max(1, int((time.perf_counter() - t0) * 1000))
        ws.send_frame(0x2, encode_pb_frame(build_ack_frame(pb, code=200, toast_text=(self.toast_text if is_card else None), biz_rt_ms=rt_ms)))
        ack_ms = self._state["last_ack_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
        if not (full_payload := self._assemble_payload(pb)): return
        if (norm := normalize_ws_event_payload(json.loads(full_payload.decode("utf-8")), pb.headers)) is not None:
            norm["_ws_ack_ms"], self._state["last_event_at"] = ack_ms, _now_iso()
            self._state["events"] += 1; self._notify_state(); self.on_event(norm)

    def _assemble_payload(self, pb: PbFrame) -> bytes:
        if (total := int(pb.headers.get("sum") or "1")) <= 1: return pb.payload
        mid, seq = pb.headers.get("message_id") or str(pb.seq_id), int(pb.headers.get("seq") or "0")
        parts = self._multipart.setdefault(mid, {})
        parts[seq] = pb.payload
        return b"" if len(parts) < total else b"".join(self._multipart.pop(mid, {}).get(i, b"") for i in range(total))
