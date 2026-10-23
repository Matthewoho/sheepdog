"""REG-027 ~ REG-029 飞书原生常驻 WebSocket 守护线程、毫秒级 ACK+Toast 与心跳自愈回归测试（7.77）。"""
from __future__ import annotations

import base64, hashlib, json, os, socket, struct, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheepdog import card_callback, observe
from sheepdog.source import bot_events, feishu_ws, lark
from sheepdog.store import Store

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _LocalMockWSServer:
    """本地 Loopback RFC 6455 WebSocket Mock Server，支持静默半开模拟、帧捕获与服务端主动下发。"""

    def __init__(self, silent_conns: set[int] | None = None):
        self.silent_conns = set(silent_conns or ())
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self.conn_count = 0
        self.connected_evt = threading.Event()
        self.second_connected_evt = threading.Event()
        self.rx_frames: list[tuple[int, int, int, bytes, float]] = []
        self.rx_evt = threading.Event()
        self._clients: list[socket.socket] = []
        self._stop = False
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def ws_url(self, conn_id: str = "conn_mock_1", service_id: int = 33554678) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/v2?device_id={conn_id}&service_id={service_id}"

    def _accept_loop(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            self.conn_count += 1
            cid = self.conn_count
            self._clients.append(conn)
            threading.Thread(target=self._serve_client, args=(conn, cid), daemon=True).start()

    def _serve_client(self, conn: socket.socket, cid: int) -> None:
        try:
            conn.settimeout(2.0)
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                req += chunk
            key = ""
            for line in req.decode("latin-1", errors="replace").splitlines():
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            )
            conn.sendall(resp.encode("ascii"))
            self.connected_evt.set()
            if cid >= 2:
                self.second_connected_evt.set()
            silent = cid in self.silent_conns
            while not self._stop:
                op, masked, data = self._recv_ws_frame(conn)
                self.rx_frames.append((cid, op, masked, data, time.perf_counter()))
                self.rx_evt.set()
                if silent:
                    continue
                if op == 0x9:
                    self.send_ws_frame(0xA, data, sock=conn)
                elif op == 0x2:
                    pb = feishu_ws.decode_pb_frame(data)
                    if pb.method == 0 and pb.headers.get("type") == "ping":
                        pong = feishu_ws.PbFrame(
                            seq_id=pb.seq_id, log_id=pb.log_id, service=pb.service,
                            method=0, headers={**pb.headers, "type": "pong"},
                        )
                        self.send_ws_frame(0x2, feishu_ws.encode_pb_frame(pong), sock=conn)
        except Exception:
            pass

    @staticmethod
    def _read_exact(conn: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionResetError("eof")
            buf.extend(chunk)
        return bytes(buf)

    def _recv_ws_frame(self, conn: socket.socket) -> tuple[int, int, bytes]:
        b0, b1 = self._read_exact(conn, 2)
        op, masked, ln = b0 & 0x0F, (b1 >> 7) & 1, b1 & 0x7F
        if ln == 126:
            ln = struct.unpack("!H", self._read_exact(conn, 2))[0]
        elif ln == 127:
            ln = struct.unpack("!Q", self._read_exact(conn, 8))[0]
        mask = self._read_exact(conn, 4) if masked else b""
        data = self._read_exact(conn, ln) if ln else b""
        if masked:
            data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        return op, masked, data

    def send_ws_frame(self, opcode: int, payload: bytes, sock: socket.socket | None = None) -> None:
        target = sock or (self._clients[-1] if self._clients else None)
        if target is None:
            raise RuntimeError("no connected client")
        n = len(payload)
        hdr = bytearray([0x80 | (opcode & 0x0F)])
        if n < 126:
            hdr.append(n)
        elif n < 65536:
            hdr.extend(b"\x7e" + struct.pack("!H", n))
        else:
            hdr.extend(b"\x7f" + struct.pack("!Q", n))
        target.sendall(bytes(hdr) + payload)

    def close(self) -> None:
        self._stop = True
        try:
            self._srv.close()
        except Exception:
            pass
        for c in self._clients:
            try:
                c.close()
            except Exception:
                pass


class NativeWSSupervisorTest(unittest.TestCase):
    def test_reg_027_native_ws_sub10ms_ack_with_toast_and_v2_normalization(self):
        srv = _LocalMockWSServer()
        self.addCleanup(srv.close)
        received_events: list[dict] = []
        ev_arrived = threading.Event()

        def on_event(ev: dict) -> None:
            received_events.append(ev)
            ev_arrived.set()

        client = feishu_ws.FeishuNativeWSClient(
            credentials={"app_id": "cli_test", "app_secret": "sec_test"},
            on_event=on_event,
            ping_interval=10.0,
            pong_timeout=5.0,
            endpoint_fetcher=lambda *_: {"url": srv.ws_url("conn_reg027", 33554678), "client_config": {}},
        )
        client.start()
        self.addCleanup(client.stop)
        self.assertTrue(srv.connected_evt.wait(2.0))

        card_v2_payload = {
            "schema": "2.0",
            "header": {"event_id": "ev_card_101", "event_type": "card.action.trigger", "create_time": "1792680000000"},
            "event": {
                "operator": {"open_id": "ou_vip_1"},
                "action": {
                    "tag": "button",
                    "value": {"choice": "A", "sd_choice": "A", "sd_card_id": "card_001", "label": "立即发布", "topic_id": "tp_alpha"},
                },
                "context": {"open_message_id": "om_card_001", "open_chat_id": "oc_chat_001"},
            },
        }
        frame_in = feishu_ws.PbFrame(
            seq_id=101, log_id=202, service=33554678, method=1,
            headers={"type": "card", "message_id": "msg_card_1", "sum": "1", "seq": "0"},
            payload=json.dumps(card_v2_payload, ensure_ascii=False).encode("utf-8"),
        )
        srv.rx_evt.clear()
        t_send = time.perf_counter()
        srv.send_ws_frame(0x2, feishu_ws.encode_pb_frame(frame_in))
        self.assertTrue(srv.rx_evt.wait(1.0))
        _, op, masked, raw_ack, t_recv = srv.rx_frames[-1]
        rtt_ms = (t_recv - t_send) * 1000.0
        self.assertLess(rtt_ms, 50.0, f"ACK RTT {rtt_ms:.2f}ms 超过 50ms 门限")
        self.assertEqual((op, masked), (0x2, 1))

        ack_pb = feishu_ws.decode_pb_frame(raw_ack)
        self.assertEqual((ack_pb.seq_id, ack_pb.log_id, ack_pb.method), (101, 202, 1))
        self.assertIn("biz_rt", ack_pb.headers)
        resp_obj = json.loads(ack_pb.payload.decode("utf-8"))
        self.assertEqual(resp_obj["code"], 200)
        toast_obj = json.loads(base64.b64decode(resp_obj["data"]).decode("utf-8"))
        self.assertEqual(toast_obj, {"toast": {"type": "info", "content": "🐕 已收到拍板，正在处理..."}})

        self.assertTrue(ev_arrived.wait(1.0))
        ev = received_events[-1]
        self.assertTrue(card_callback.is_card_action_event(ev))
        _, card_mid, chat_id, operator_id, action_tag, val, form_val, input_val, _ = card_callback._unpack_raw_event(ev, "+08:00")
        choice_tuple = card_callback._extract_action_choice(action_tag, val, form_val, input_val)
        self.assertIsNotNone(choice_tuple)
        self.assertEqual((choice_tuple[0], card_mid, chat_id, operator_id), ("A", "om_card_001", "oc_chat_001", "ou_vip_1"))

        im_v2 = {
            "schema": "2.0",
            "header": {"event_id": "ev_im_01", "event_type": "im.message.receive_v1", "create_time": "1792680005000"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_sender_1"}, "sender_type": "user"},
                "message": {
                    "message_id": "om_msg_001", "chat_id": "oc_chat_001", "chat_type": "group",
                    "message_type": "text", "content": '{"text":"@_user_1 你好"}',
                    "mentions": [{"key": "@_user_1", "name": "Sheepdog", "id": {"open_id": "ou_bot"}}],
                },
            },
        }
        norm_im = feishu_ws.normalize_ws_event_payload(im_v2, {"type": "event"})
        self.assertIsNotNone(norm_im)
        parsed_msg = lark.parse_event(norm_im)
        self.assertEqual(parsed_msg.content, "@Sheepdog 你好")
        self.assertEqual((parsed_msg.message_id, parsed_msg.sender_id, parsed_msg.mentions[0].id), ("om_msg_001", "ou_sender_1", "ou_bot"))

    def test_reg_028_half_open_zombie_detection_and_fast_reconnect(self):
        srv = _LocalMockWSServer(silent_conns={1})
        self.addCleanup(srv.close)
        fetch_calls: list[int] = []
        reconnected_events: list[dict] = []
        ev_evt = threading.Event()

        def fetcher(app_id: str, app_secret: str, domain: str) -> dict:
            fetch_calls.append(len(fetch_calls) + 1)
            return {"url": srv.ws_url(f"conn_attempt_{len(fetch_calls)}", 33554678), "client_config": {}}

        client = feishu_ws.FeishuNativeWSClient(
            credentials={"app_id": "cli_test", "app_secret": "sec_test"},
            on_event=lambda e: (reconnected_events.append(e), ev_evt.set()),
            ping_interval=0.08,
            pong_timeout=0.08,
            backoff=0.05,
            backoff_max=0.2,
            endpoint_fetcher=fetcher,
        )
        t0 = time.perf_counter()
        client.start()
        self.addCleanup(client.stop)

        self.assertTrue(srv.second_connected_evt.wait(0.6), "未在 0.6s 内检测到半开死连接并完成重连")
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.6)
        snap = client.snapshot()
        self.assertGreaterEqual(snap["restarts"], 1)
        self.assertGreaterEqual(snap["zombie_restarts"], 1)
        self.assertGreaterEqual(len(fetch_calls), 2)

        im_payload = {
            "header": {"event_id": "ev_after_reconnect", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_u1"}, "sender_type": "user"},
                "message": {"message_id": "om_after_1", "chat_id": "oc_1", "chat_type": "p2p", "message_type": "text", "content": '{"text":"重连恢复"}'},
            },
        }
        srv.send_ws_frame(0x2, feishu_ws.encode_pb_frame(feishu_ws.PbFrame(
            seq_id=202, log_id=303, service=33554678, method=1,
            headers={"type": "event", "message_id": "msg_after_1", "sum": "1", "seq": "0"},
            payload=json.dumps(im_payload, ensure_ascii=False).encode("utf-8"),
        )))
        self.assertTrue(ev_evt.wait(1.0))
        self.assertEqual(reconnected_events[-1]["message_id"], "om_after_1")
        self.assertEqual(reconnected_events[-1]["content"], "重连恢复")

    def test_reg_029_bot_event_consumer_native_supervision_and_observe_health(self):
        srv = _LocalMockWSServer()
        self.addCleanup(srv.close)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            db_path = tmp_dir / "sheepdog.sqlite3"
            store = Store(db_path)
            self.addCleanup(store.close)
            killed_binaries: list[str] = []

            def fake_ws_factory(**kw):
                return feishu_ws.FeishuNativeWSClient(
                    **kw, ping_interval=5.0, pong_timeout=3.0,
                    endpoint_fetcher=lambda *_: {"url": srv.ws_url("conn_reg029", 33554678), "client_config": {}},
                )

            def write_ic_meta(d: dict) -> None:
                if threading.current_thread() is threading.main_thread():
                    store.set_meta("identity_consumer", json.dumps(d, ensure_ascii=False))

            with mock.patch.object(bot_events, "_stop_orphan_lark_bus", new=lambda b="lark-cli": killed_binaries.append(b)):
                consumer = bot_events.BotEventConsumer(
                    binary="lark-cli", backoff=0.05, backoff_max=0.2,
                    state_writer=write_ic_meta,
                    mode="native_ws", credentials={"app_id": "cli_x", "app_secret": "sec_x"},
                    ws_client_factory=fake_ws_factory,
                )
                consumer.start()
                self.addCleanup(consumer.stop)
                self.assertEqual(killed_binaries, ["lark-cli"])
                self.assertTrue(srv.connected_evt.wait(2.0))
                time.sleep(0.03)
                consumer.poll()

            meta_ic = json.loads(store.get_meta("identity_consumer") or "{}")
            self.assertEqual((meta_ic.get("mode"), meta_ic.get("state"), meta_ic.get("conn_id")), ("native_ws", "running", "conn_reg029"))
            self.assertTrue(meta_ic.get("last_pong_at"))

            im_payload = {
                "header": {"event_id": "ev_reg029_1", "event_type": "im.message.receive_v1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_u2"}, "sender_type": "user"},
                    "message": {"message_id": "om_reg029_1", "chat_id": "oc_2", "chat_type": "p2p", "message_type": "text", "content": '{"text":"原生消费测试"}'},
                },
            }
            srv.send_ws_frame(0x2, feishu_ws.encode_pb_frame(feishu_ws.PbFrame(
                seq_id=301, log_id=401, service=33554678, method=1,
                headers={"type": "event", "message_id": "m_301", "sum": "1", "seq": "0"},
                payload=json.dumps(im_payload, ensure_ascii=False).encode("utf-8"),
            )))
            self.assertTrue(consumer.wait(1.0))
            drained = consumer.drain()
            self.assertEqual(len(drained), 1)
            self.assertEqual(lark.parse_event(drained[0]).content, "原生消费测试")

            ps_only_run = "12345 Mon Oct 23 16:00:00 2026 /opt/homebrew/bin/sheepdog run\n"
            obs_ok = observe.run_observe(
                None, db_path=db_path, state_path=tmp_dir / "cursor.json",
                check_processes=True, check_log=False, ps_output=ps_only_run,
            )
            self.assertNotIn("lark-cli event consume 没在跑", obs_ok["warns"])
            self.assertTrue(any("原生 WebSocket 常驻在线" in i for i in obs_ok["infos"]))

            consumer._ws_client.stop()
            consumer.poll()
            self.assertTrue(consumer.alive())
            self.assertGreaterEqual(consumer.state["restarts"], 1)


if __name__ == "__main__":
    unittest.main()
