"""在岗表第二轮：会话登不登记、登记得对不对，都不能让在岗表算错，也不能改变任何投递和通知的决定。
登记只让信息更全（「手上的事」、撞车提醒的措辞），不是正确性的前提。

- 零登记：会话从不 work set。运行状态照常判对；撞车提醒靠路由事实（带编号的消息送到了谁）照样出现；
  board 上「手上的事」只有自动来源（路由、等人回复、需要你定、转交）。
- 乱登记：自报「进行中」以后再也不管，4 小时没更新、会话一直空闲 → 标「可能过期」，撞车提醒带这几个字；
  自报的状态和实际相反（跑着批次说在等、卡住了说做完了……），运行状态和投递完全不受影响。
- 同一个剧本跑两遍（一遍乱登记、一遍不登记），每一轮投了什么、给主人发了什么、每张卡片的运行状态都一样。
"""

import contextlib
import hashlib
import io
import json
import unittest
from datetime import timedelta
from unittest import mock

from sheepdog import cli, duty
from sheepdog.engine import BUS_TOPIC_ID, forward_messages

from duty_sim import START, World

KEY1, KEY2, KEY3 = "202610120022", "PR #45", "OPS-7"


def run_cli(w: World, *argv) -> tuple[int, str]:
    out = io.StringIO()
    with mock.patch.object(cli, "load_config", return_value=w.cfg), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = cli.main(list(argv))
    return rc, out.getvalue()


def to(w: World, minute: float, step: float = 1) -> None:
    end = START + timedelta(minutes=minute)
    while w.now < end:
        w.round(step)


def snapshot(w: World, seen: dict) -> tuple:
    """这一轮的决定：投了什么（系统消息换成「原因 + 正文摘要」）、新的通知、每张卡片的运行状态和会话状态。"""
    def canon(mid: str) -> str:
        if not mid.startswith("sd_"):
            return mid
        r = w.store.get_message(mid)
        return f"sd:{r['reason']}:{hashlib.sha1((r['content'] or '').encode()).hexdigest()[:12]}"

    dl = w.sink.deliveries[seen.get("d", 0):]
    seen["d"] = len(w.sink.deliveries)
    ns = w.notices()
    new = ns[seen.get("n", 0):]
    seen["n"] = len(ns)
    return (w.now.isoformat(),
            tuple((d["cid"], d["batch"], tuple(canon(m) for m in json.loads(
                w.store.get_dispatch(d["batch"])["message_ids"] or "[]"))) for d in dl if d["batch"]),
            tuple((n["at"], n["what"], n["text"]) for n in new),
            tuple((tid, w.run_state(tid), w.topic(tid)["state"]) for tid in sorted(w.sessions)))


def script(declare: bool) -> tuple[list, dict]:
    """同一个剧本：发版会话碰了批次又卡住、主人在总线里说话、总线断网又恢复、留言、等人回复、转交……
    declare=True 时会话还会自报、乱报（登错编号、和实际相反、该收不收、重复登记）。返回 (每一轮的决定, 观察到的东西)。"""
    w = World(["rel", "auth", "ops"])
    trace, seen, seen_text = [], {}, {}
    try:
        rel, auth, ops, bus = w["rel"], w["auth"], w["ops"], w[BUS_TOPIC_ID]

        def step(minute, every=1):
            while w.now < START + timedelta(minutes=minute):
                w.round(every)
                trace.append(snapshot(w, seen))

        def say(tid, *argv):
            if declare:
                rc, out = run_cli(w, "work", *argv)
                assert rc == 0 or "被拒绝" in out, out

        say("rel", "set", "--topic", "tp_rel", "--key", KEY1, "--title", "发版", "--state", "doing")
        say("ops", "set", "--topic", "tp_ops", "--key", "OPS-999", "--title", "登错的编号", "--state", "doing")
        w.message("rel", f"审批单 {KEY1} 可以发了")
        w.message("auth", f"{KEY2} 要不要合")
        step(2)
        rel.act("Trigger build", cmd="gcloud builds triggers run demo")
        auth.act("Read the PR")
        say("rel", "set", "--topic", "tp_rel", "--key", KEY1, "--state", "waiting_other")  # 跑着批次说在等
        say("auth", "set", "--topic", "tp_auth", "--key", KEY2, "--state", "done", "--result", "合了")  # 其实没做完
        step(6)
        auth.act("Reply to colleague")
        auth.receipt()
        w.message("bus", f"{KEY1} 到哪了？{KEY2} 呢？")
        step(8)
        bus.act("Route")
        bus.receipt()
        rc, out = run_cli(w, "watch", "--topic", "tp_auth", "--person", "ou_test_bob", "--note", f"等 bob 确认 {KEY3}")
        assert rc == 0, out
        # rel 卡住不动：中断、失联；它自报「进行中」、还在别处说做完了
        say("rel", "set", "--topic", "tp_rel", "--key", KEY1, "--state", "done", "--result", "发完了")  # 卡住了说做完了
        say("rel", "set", "--topic", "tp_rel", "--key", KEY1, "--state", "doing")                      # 又改回去
        say("rel", "set", "--topic", "tp_rel", "--key", KEY1, "--state", "doing")                      # 重复登记
        w.message("rel", f"{KEY1} 好了吗")
        step(40)
        rel.act("Back, check build")
        rel.receipt()
        step(45)
        # 主人在总线里说话、总线断网：中断、仍卡着、已恢复
        w.message("bus", "主人在的时候来的消息")
        step(46)
        bus.owner_says("我来看看")
        bus.answer_only()
        say("bus", "set", "--topic", "tp_bus", "--key", "BUS-1", "--title", "总线自报", "--state", "waiting_owner")
        step(70)
        bus.net_down = True
        w.message("bus", f"断网时来的 {KEY3}")
        step(150)
        bus.net_down = False
        bus.act("Back online")
        step(152)
        bus.act("Handle backlog")
        bus.receipt()
        # ops 自报「进行中」以后再也不管、一直空闲：4 小时后可能过期
        say("ops", "set", "--topic", "tp_ops", "--key", KEY3, "--title", "盯一下", "--state", "doing")
        step(160)
        w.message("bus", f"{KEY3} 谁在跟？")
        step(162)
        bus.act("Route")
        bus.receipt()
        step(422, every=10)  # 09:02 起每 10 分钟一轮，正好落在 16:02
        w.message("bus", f"{KEY3} 还有人管吗？")
        step(423)
        bus_texts = [d["content"] for d in w.sink.to("conv_test_bus")]
        seen_text["bus_last"] = bus_texts[-1]
        seen_text["board"] = run_cli(w, "board")[1]
        seen_text["board_json"] = json.loads(run_cli(w, "board", "--json")[1])
        seen_text["notices"] = w.notices()
    finally:
        w.close()
    return trace, seen_text


class TwinRunTest(unittest.TestCase):
    """同一个剧本跑两遍：乱登记 vs 不登记。决定（投递、通知、运行状态）必须一模一样；能看出差别的只有「手上的事」。"""

    @classmethod
    def setUpClass(cls):
        cls.a, cls.a_seen = script(declare=True)
        cls.b, cls.b_seen = script(declare=False)

    def test_decisions_identical(self):
        self.assertEqual(len(self.a), len(self.b))
        for ra, rb in zip(self.a, self.b):
            self.assertEqual(ra[0], rb[0])
            self.assertEqual(ra[1], rb[1], f"{ra[0]} 投递不一样")
            self.assertEqual(ra[2], rb[2], f"{ra[0]} 通知不一样")
            self.assertEqual(ra[3], rb[3], f"{ra[0]} 运行状态不一样")
        # 剧本真的走到了这些情况
        whats = [n["what"] for n in self.a_seen["notices"]]
        self.assertIn("羊群看板通知", whats)
        self.assertIn("attention 通知", whats)
        self.assertIn("恢复通知", whats)
        states = {s for r in self.a for (_, s, _) in r[3]}
        self.assertTrue({duty.STALLED, duty.LOST, duty.OCCUPIED, duty.WORKING, duty.WAITING, duty.IDLE} <= states, states)

    def test_only_on_hand_differs(self):
        a, b = self.a_seen, self.b_seen
        cards_a = {c["topic_id"]: c for c in a["board_json"]}
        cards_b = {c["topic_id"]: c for c in b["board_json"]}
        for tid in cards_a:
            self.assertEqual(cards_a[tid]["run_state"], cards_b[tid]["run_state"], tid)
            self.assertTrue(all(not x["declared"] for x in cards_b[tid]["work"]), tid)  # 不登记：只有自动来源
        # 乱登记那一遍：ops 自报的 KEY3 4 小时没更新、ops 一直空闲 → 可能过期，撞车提醒里也写着
        ops = {x["key"]: x for x in cards_a["tp_ops"]["work"]}
        self.assertEqual(ops[KEY3]["stale"], "可能过期（4 小时没更新）")
        self.assertIn("可能过期（4 小时没更新）", a["board"])
        self.assertIn(f"🔀 撞车提醒：{KEY3} 在「会话ops」（topic `tp_ops`）手上；会话自报：进行中（更新于 4 小时前，"
                      "可能过期（4 小时没更新））", a["bus_last"])
        self.assertNotIn("会话ops", b["bus_last"])  # 不登记：ops 手上没有 KEY3


class ZeroRegistrationTest(unittest.TestCase):
    """所有会话从不 work set：运行状态照常判对，撞车提醒靠路由事实出现，board 上只有自动来源。"""

    def setUp(self):
        self.w = World(["rel", "auth"])
        self.addCleanup(self.w.close)

    def test_routes_drive_collisions_and_board(self):
        w = self.w
        m1 = w.message("rel", f"审批单 {KEY1} 可以发了")
        to(w, 1)
        w["rel"].act("Trigger build")
        to(w, 3)
        # 总线收到提到 KEY1 的消息：提醒里写的是事实（消息最近送到了发版会话），不是谁登记了什么
        w.message("bus", f"{KEY1} 到哪了")
        to(w, 4)
        text = w.sink.to("conv_test_bus")[-1]["content"]
        self.assertIn(f"🔀 撞车提醒：{KEY1} 的消息最近送到了「会话rel」（topic `tp_rel`）：最近一次 3 分钟前，共 1 条；"
                      "它现在在做，最近动作「Trigger build」3 分钟前。", text)
        self.assertIn("basis=路由", text)
        self.assertEqual(w.store.get_message(m1)["topic_id"], "tp_rel")
        # board：rel 手上的事只有路由事实；一件自报的都没有
        rc, out = run_cli(w, "board", "--topic", "tp_rel")
        self.assertIn("手上的事：", out)
        self.assertIn(f"{KEY1} · 消息最近送到这里（自动·路由：最近一次 3 分钟前，共 1 条；最近一条：审批单 {KEY1} 可以发了）", out)
        self.assertNotIn("会话自报", out)
        card = json.loads(run_cli(w, "board", "--json")[1])
        for c in card:
            self.assertTrue(all(not x["declared"] for x in c["work"]))
        # 运行状态照常：rel 停住 15 分钟判中断
        to(w, 17)
        self.assertEqual(w.run_state("rel"), duty.STALLED)
        # 转交：总线把一条提到 KEY2 的消息转给 auth，总线手上的 KEY2 收掉，之后提醒指向 auth
        w["bus"].act("Route")
        w["bus"].receipt()
        m2 = w.message("bus", f"{KEY2} 需要 auth 看")
        to(w, 18)
        w["bus"].act("Forward")
        w["bus"].receipt()
        forward_messages(w.store, "tp_auth", [m2])
        to(w, 19)
        self.assertEqual(w.store.active_routes("", topic_id=BUS_TOPIC_ID, norm_key=duty.norm_key(KEY2)), [])
        self.assertEqual(len(w.store.active_routes("", topic_id="tp_auth", norm_key=duty.norm_key(KEY2))), 1)
        w["bus"].act("Done")
        w.message("bus", f"{KEY2} 怎样了")
        to(w, 20)
        text = w.sink.to("conv_test_bus")[-1]["content"]
        self.assertIn(f"🔀 撞车提醒：{KEY2} 的消息最近送到了「会话auth」（topic `tp_auth`）", text)
        self.assertEqual(text.count("撞车提醒"), 1)
        # 24 小时没有新消息送到：不再算手上的事，也不再提醒
        w["bus"].act("Done")
        w["bus"].receipt()
        to(w, 20 + 24 * 60 + 5, step=60)
        w.message("bus", f"{KEY1} 和 {KEY2} 都怎样了")
        to(w, 20 + 24 * 60 + 66, step=60)
        text = w.sink.to("conv_test_bus")[-1]["content"]
        self.assertNotIn("撞车提醒", text)
        self.assertEqual(duty.board_cards(w.store, w.cfg, topic="tp_rel")[0]["routes"], [])

    def test_route_counts_distinct_messages_and_self_done_closes(self):
        w = self.w
        for i in range(3):
            w.message("rel", f"{KEY1} 第 {i + 1} 条")
        to(w, 1)
        r = w.store.active_routes("", topic_id="tp_rel")[0]
        self.assertEqual((r["key"], r["count"]), (KEY1, 3))
        # 会话自报做完：这之前的路由不再算；之后又送到就重新算
        rc, out = run_cli(w, "work", "done", "--topic", "tp_rel", "--key", KEY1, "--result", "发了")
        self.assertEqual(rc, 0, out)  # 没登记过、只有路由事实的编号也能 done
        self.assertEqual(w.store.active_routes("", topic_id="tp_rel"), [])
        w["rel"].act("Handle")
        w["rel"].receipt()
        w.message("rel", f"{KEY1} 又来了")
        to(w, 3)
        self.assertEqual(w.store.active_routes("", topic_id="tp_rel")[0]["count"], 4)


class WrongRegistrationTest(unittest.TestCase):
    """自报的状态和实际相反：运行状态、投递、通知都不受影响；自报的活单独标来源。"""

    def setUp(self):
        self.w = World(["rel"])
        self.addCleanup(self.w.close)

    def test_declared_states_never_drive_run_state(self):
        w, rel = self.w, self.w["rel"]
        rel.app_alive = False
        w.message("rel", "第一件")
        to(w, 1)
        # 卡住的会话自报「进行中」「做完了」「等别人」，都不能让它不判中断、不通知、不扣消息
        for state in ("doing", "done", "waiting_other", "waiting_owner"):
            argv = ["work", "set", "--topic", "tp_rel", "--key", KEY1, "--state", state]
            self.assertEqual(run_cli(w, *argv)[0], 0)
            to(w, w.round_no + 4)
        to(w, 17)
        self.assertEqual(w.run_state("rel"), duty.STALLED)
        self.assertEqual([n["what"] for n in w.notices()], ["羊群看板通知"])
        m2 = w.message("rel", "第二件")
        to(w, 25)
        self.assertEqual(w.state(m2), "pending")  # 照样排队
        # 空闲的会话自报「等主人」：卡片仍是空闲（等回复只看回执、watch、「需要你定」这些事实）
        rel.app_alive = True
        rel.act("Back")
        to(w, 27)
        rel.act("Handle")
        rel.receipt()
        to(w, 30)
        self.assertEqual(run_cli(w, "work", "set", "--topic", "tp_rel", "--key", "X-1", "--state", "waiting_owner")[0], 0)
        to(w, 31)
        self.assertEqual(w.run_state("rel"), duty.IDLE)
        rc, out = run_cli(w, "board", "--topic", "tp_rel")
        self.assertIn("运行状态：空闲", out)
        self.assertIn("X-1 · 等主人（会话自报；更新于 1 分钟前）", out)

    def test_stale_label_needs_four_idle_hours_and_resets_on_update(self):
        w = self.w
        self.assertEqual(run_cli(w, "work", "set", "--topic", "tp_rel", "--key", KEY3, "--state", "doing")[0], 0)
        to(w, 239, step=10)
        self.assertEqual(duty.board_cards(w.store, w.cfg, topic="tp_rel")[0]["work"][0]["stale"], "")
        to(w, 250, step=10)
        self.assertEqual(duty.board_cards(w.store, w.cfg, topic="tp_rel")[0]["work"][0]["stale"], "可能过期（4 小时没更新）")
        self.assertIn("进行中·自报·可能过期", duty.digest_line(duty.board_cards(w.store, w.cfg, topic="tp_rel")[0]))
        # 会话自己更新一下：不再过期
        self.assertEqual(run_cli(w, "work", "set", "--topic", "tp_rel", "--key", KEY3, "--state", "doing")[0], 0)
        self.assertEqual(duty.board_cards(w.store, w.cfg, topic="tp_rel")[0]["work"][0]["stale"], "")
        # 4 小时没更新，但中间会话不是一直空闲（收到过一批）：不算
        to(w, 300, step=10)
        w.message("rel", "来活了")
        to(w, 301)
        w["rel"].act("Work")
        w["rel"].receipt()
        to(w, 500, step=10)
        self.assertEqual(duty.board_cards(w.store, w.cfg, topic="tp_rel")[0]["work"][0]["stale"], "")
        to(w, 560, step=10)
        self.assertEqual(duty.board_cards(w.store, w.cfg, topic="tp_rel")[0]["work"][0]["stale"], "可能过期（4 小时没更新）")


if __name__ == "__main__":
    unittest.main()
