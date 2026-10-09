"""7.16 缺口 1、4：分页没拉完不推进水位线、下一轮从原 start 重拉、提示总线一次、连续 3 轮显示「采集不完整」；
免打扰群里回复主人的消息送达（关键人发言仍受免打扰约束）。"""

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.config import RoutingConfig
from sheepdog.engine import BUS_TOPIC_ID, Collector
from sheepdog.models import Mention
from sheepdog.roster import Roster
from sheepdog.router import DISPATCH, DROP, RouteContext, route
from sheepdog.source.lark import LarkCliSource

from test_roster import ME, Base, FakeSource, msg


class PartialSource(FakeSource):
    """按轮返回 (消息, 是否没拉完)，记下每轮的 start / end。"""

    def __init__(self, rounds):
        super().__init__([])
        self.rounds = list(rounds)
        self.calls = []
        self.last_fetch_partial = False

    def fetch_since(self, start_iso, end_iso=None):
        self.calls.append((start_iso, end_iso))
        msgs, partial = self.rounds.pop(0) if self.rounds else ([], False)
        self.last_fetch_partial = partial
        return msgs


class PartialTest(Base):
    def setUp(self):
        super().setUp()
        self.d = self.disp(Roster())
        self.d.init()

    def notices(self):
        return self.store.conn.execute("SELECT * FROM messages WHERE reason='collect_partial'").fetchall()

    def test_partial_round_keeps_watermark_and_refetches(self):
        self.store.set_meta("watermark", "2026-01-01T10:00:00+08:00")
        src = PartialSource([
            ([msg(message_id="om_test_p1", chat_type="p2p")], True),
            ([msg(message_id="om_test_p1", chat_type="p2p"), msg(message_id="om_test_p2", chat_type="p2p")], True),
            ([msg(message_id="om_test_p3", chat_type="p2p")], True),
            ([msg(message_id="om_test_p4", chat_type="p2p")], False),
        ])
        col = Collector(self.cfg, self.store, src, Roster())
        st = col.poll_once()
        self.assertTrue(st["partial"])
        self.assertEqual(self.store.get_meta("watermark"), "2026-01-01T10:00:00+08:00")  # 不推进
        self.assertIsNotNone(src.calls[0][1])  # 固定窗口：传了 end
        col.poll_once()
        self.assertEqual(src.calls[1][0], src.calls[0][0])  # 下一轮从原 start 重拉
        self.assertEqual(len(self.notices()), 1)  # 同一缺口只提示一次
        self.assertIsNotNone(self.row("om_test_p2"))  # 按 message_id 去重入账
        col.poll_once()
        self.assertEqual(self.store.get_meta("partial_streak"), "3")
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\n', encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}), \
                redirect_stdout(out):
            cli.main(["sessions"])
        self.assertIn("采集不完整", out.getvalue())
        col.poll_once()  # 拉完了：推进水位线、清掉缺口
        self.assertEqual(self.store.get_meta("watermark"), src.calls[3][1])
        self.assertEqual((self.store.get_meta("partial_start"), self.store.get_meta("partial_streak")), ("", "0"))
        self.d.dispatch_once()
        self.assertIn("采集不完整", self.sink.to("conv_test_bus")[-1])

    def test_first_round_partial_without_watermark(self):
        src = PartialSource([([], True), ([], False)])
        col = Collector(self.cfg, self.store, src, Roster())
        col.poll_once()
        self.assertEqual(self.store.get_meta("watermark"), "")
        col.poll_once()
        self.assertEqual(src.calls[1][0], src.calls[0][0])  # 首轮的 lookback 起点也保留下来重拉


class LarkPagingTest(unittest.TestCase):
    """报告场景：20 页后仍 has_more —— 旧代码默认 max_pages=20 时静默丢掉后面的消息。"""

    def run_fake(self, pages: int, has_more_last: bool, token_last: str, max_pages: int):
        with tempfile.TemporaryDirectory() as t:
            state = Path(t) / "n"
            state.write_text("0")
            script = Path(t) / "lark-cli"
            script.write_text(
                "#!/usr/bin/env python3\nimport json, sys\n"
                f"p = open({str(state)!r}); n = int(p.read()); p.close()\n"
                f"open({str(state)!r}, 'w').write(str(n + 1))\n"
                f"last = n + 1 >= {pages}\n"
                "msgs = [{'message_id': 'om_test_%d' % n, 'chat_id': 'oc_test_c', 'chat_type': 'p2p', 'content': 'x',\n"
                "         'create_time': '2026-01-01 10:00', 'sender': {'id': 'ou_test_a', 'sender_type': 'user'}}]\n"
                f"more = {has_more_last!r} if last else True\n"
                f"token = {token_last!r} if last else 'tok%d' % n\n"
                "print(json.dumps({'ok': True, 'data': {'messages': msgs, 'has_more': more, 'page_token': token}}))\n",
                encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            src = LarkCliSource(binary=str(script), max_pages=max_pages)
            got = src.fetch_since("2026-01-01T09:00:00+08:00", "2026-01-01T11:00:00+08:00")
            return len(got), src.last_fetch_partial

    def test_more_pages_than_limit_is_partial(self):
        self.assertEqual(self.run_fake(pages=25, has_more_last=False, token_last="", max_pages=20), (20, True))

    def test_has_more_without_token_is_partial(self):
        self.assertEqual(self.run_fake(pages=3, has_more_last=True, token_last="", max_pages=100), (3, True))

    def test_complete(self):
        self.assertEqual(self.run_fake(pages=3, has_more_last=False, token_last="", max_pages=100), (3, False))


class MutedReplyTest(unittest.TestCase):
    def test_reply_to_me_in_muted_chat_dispatched_vip_still_dropped(self):
        ctx = RouteContext(ME, muted_chat_ids={"oc_test_muted"}, is_my_message=lambda mid: mid == "om_test_mine")
        cfg = RoutingConfig(vip_sender_ids=["ou_test_boss"])
        d = route(msg(chat_id="oc_test_muted", reply_to="om_test_mine"), ctx, cfg)
        self.assertEqual((d.route, d.reason), (DISPATCH, "reply_to_me"))
        self.assertEqual(route(msg(chat_id="oc_test_muted", sender_id="ou_test_boss"), ctx, cfg).route, DROP)
        self.assertEqual(route(msg(chat_id="oc_test_muted"), ctx, cfg).route, DROP)
