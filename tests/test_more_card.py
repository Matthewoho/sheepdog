"""7.62 卡片只露四刀法白话总结，原文收进 more 默认折叠区。"""

import json
from pathlib import Path

from sheepdog.card import MORE_DELIMITER, MORE_PANEL_TITLE, build_owner_card, build_reply_card, plain_text
from sheepdog.config import EscalationConfig
from test_owner_card import MD, PROD_ATTR_RX, PROD_HEADER_RX, card_of
from test_roster import ME
from test_threads import ALICE, ALPHA_P2P, ESC_CHAT, MARK, ThreadsBase, _card_text, _opt

SUMMARY = (
    "**已切 10% 灰度并稳住，生产全量等你拍板。**\n"
    "老客户端重试风暴已熔断；代价是旧版超时缩短 2 秒。\n"
    "要你定：回 `A` 今晚全量，回 `B` 保持 10% 观察。"
)

LONG_DETAIL = (
    "### 排查与验证明细\n"
    "- 灰度节点：`tap-prism-00136-jwj`\n"
    "- P99 延迟：128ms，5xx 率 0.00%\n"
    "- 变更文件：`proxy.go`、`breaker.go`\n"
    "```bash\n"
    "kubectl -n prod rollout status deploy/tap-prism\n"
    "```"
)


class MoreCardTest(ThreadsBase):
    def setUp(self):
        super().setUp()
        self.cfg.escalation = EscalationConfig(
            chat_ids=[ESC_CHAT], header_regex=PROD_HEADER_RX, attribution_regex=PROD_HEADER_RX
        )
        txt = self.cfg_file.read_text(encoding="utf-8")
        self.cfg_file.write_text(
            f'playbook_dir = "{self.cfg.playbook_dir}"\n' + txt,
            encoding="utf-8",
        )

    def _assert_more_panel(self, panel: dict, expected_detail: str) -> None:
        self.assertEqual(panel["tag"], "collapsible_panel")
        self.assertFalse(panel["expanded"])
        self.assertNotIn("background_color", panel)
        self.assertNotIn("border", panel)
        self.assertEqual(panel["header"]["title"]["content"], f"<font color='grey'>{MORE_PANEL_TITLE}</font>")
        self.assertNotIn("background_color", panel["header"])
        self.assertEqual(
            panel["header"]["icon"],
            {"tag": "standard_icon", "token": "down-small-ccm_outlined", "color": "grey", "size": "14px 14px"},
        )
        self.assertEqual(panel["header"]["icon_position"], "follow_text")
        self.assertEqual(panel["header"]["icon_expanded_angle"], -180)
        self.assertEqual(panel["elements"], [{"tag": "markdown", "content": expected_detail}])

    def _assert_reply_same_row(self, row_el: dict, expected_detail: str) -> None:
        self.assertEqual(row_el["tag"], "column_set")
        cols = row_el["columns"]
        self.assertEqual(len(cols), 2)
        self._assert_more_panel(cols[0]["elements"][0], expected_detail)
        self.assertIn(MARK, cols[1]["elements"][0]["content"])

    def test_762_owner_ask_and_report_fold_with_summary_and_more_panel(self):
        # 1) ask 通过 --summary 传白话总结，原文 LONG_DETAIL 一字不改收进 more 面板
        rc, out, err = self.ask("--key", "K762", "--summary", SUMMARY, title="全量切流", text=LONG_DETAIL)
        self.assertEqual(rc, 0, out + err)
        call1 = self.lark.sent()[-1]
        c1 = card_of(call1)
        self.assertEqual(c1["header"]["title"]["content"], "🐕 需要你定 · 全量切流 [tp_alpha]")
        els1 = c1["body"]["elements"]
        self.assertEqual(len(els1), 2)
        self.assertEqual(els1[0], {"tag": "markdown", "content": SUMMARY})
        self._assert_more_panel(els1[1], LONG_DETAIL)

        # 2) 同一把钥匙在话题里追发 report，用内联 ---more--- 分隔；末尾 @ 主人在折叠面板外面
        inline_text = f"{SUMMARY}\n{MORE_DELIMITER}\n{LONG_DETAIL}"
        rc2, out2, err2 = self.ask("--key", "K762", kind="report", title="灰度观察", text=inline_text)
        self.assertEqual(rc2, 0, out2 + err2)
        call2 = self.lark.sent()[-1]
        c2 = card_of(call2)
        els2 = c2["body"]["elements"]
        self.assertEqual(len(els2), 3)
        self.assertEqual(els2[0], {"tag": "markdown", "content": SUMMARY})
        self._assert_more_panel(els2[1], LONG_DETAIL)
        self.assertEqual(els2[2], {"tag": "markdown", "content": f"<at id={ME}></at>"})

    def test_762_reply_and_reply_card_fold_with_summary_and_inline_delimiter(self):
        # 先入账一条同事消息供 reply 回复
        m_alice = self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="请问灰度情况？")
        self.pull(m_alice)
        self.deliver()

        # 1) sheepdog reply --card-text + --summary-file：more 面板与 [🐕Sheepdog Reply] 在同一行（column_set 左 more 右标记）
        sum_file = Path(self.tmp.name) / "sum.txt"
        sum_file.write_text(SUMMARY, encoding="utf-8")
        rc, out, err = self.cli(
            "reply", "--topic", "tp_alpha", "--to", m_alice,
            "--summary-file", str(sum_file), "--card-text", LONG_DETAIL,
        )
        self.assertEqual(rc, 0, out + err)
        call = self.lark.sent("+messages-reply")[-1]
        c = card_of(call)
        els = c["body"]["elements"]
        self.assertEqual(len(els), 2)
        self.assertEqual(els[0], {"tag": "markdown", "content": SUMMARY})
        self._assert_reply_same_row(els[1], LONG_DETAIL)

        # 2) sheepdog reply-card 支持内联 ---more---（以及兼容 ---agent-read---）
        for delim in (MORE_DELIMITER, "---agent-read---"):
            rc_card, out_card, err_card = self.cli("reply-card", "--text", f"{SUMMARY}\n{delim}\n{LONG_DETAIL}")
            self.assertEqual(rc_card, 0, err_card)
            parsed = json.loads(out_card)
            c_els = parsed["body"]["elements"]
            self.assertEqual(len(c_els), 2)
            self.assertEqual(c_els[0], {"tag": "markdown", "content": SUMMARY})
            self._assert_reply_same_row(c_els[1], LONG_DETAIL)

    def test_762_long_message_rejected_with_preserve_original_guidance(self):
        m_alice = self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="长回复测试")
        self.pull(m_alice)
        self.deliver()
        before_sent = len(self.lark.sent())

        # 1) 长消息不带总结：ask / report / reply / reply-card 全部退出码 2，且明确提示「原文不需要改，只需要增加白话总结」
        for argv in (
            ("ask", "--topic", "tp_alpha", "--title", "发版", "--text", MD),
            ("report", "--topic", "tp_alpha", "--title", "进度", "--text", "行1\n行2\n行3\n行4\n行5"),
            ("reply", "--topic", "tp_alpha", "--to", m_alice, "--card-text", "```\necho hi\n```"),
            ("reply-card", "--text", "字" * 161),
        ):
            with self.subTest(cmd=argv[0]):
                rc, out, err = self.cli(*argv)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("原文不需要改，只需要增加白话总结", err)
                self.assertIn("--summary", err)
                self.assertIn("---more---", err)

        # 2) 总结本身违规（超过 6 行、超过 220 字、或含代码块）同样退出码 2 并提示原文不需要改
        for bad_sum in (
            "1\n2\n3\n4\n5\n6\n7",
            "字" * 221,
            "结论如下\n```bash\nls\n```",
        ):
            with self.subTest(bad_sum=bad_sum[:12]):
                rc, out, err = self.ask("--summary", bad_sum, text=LONG_DETAIL)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("原文不需要改，只需要增加白话总结", err)

        # 没有任何消息发往飞书
        self.assertEqual(len(self.lark.sent()), before_sent)

    def test_762_short_message_passes_through_without_more_panel(self):
        short_text = "收到，已在测试环境验证通过。\n无需额外操作。"
        rc, out, err = self.ask(kind="report", title="简报", text=short_text)
        self.assertEqual(rc, 0, out + err)
        c = card_of(self.lark.sent()[-1])
        self.assertEqual(c["body"]["elements"], [{"tag": "markdown", "content": short_text}])

        rc_rc, out_rc, err_rc = self.cli("reply-card", "--text", short_text)
        self.assertEqual(rc_rc, 0, err_rc)
        c_rc = json.loads(out_rc)
        self.assertEqual(len(c_rc["body"]["elements"]), 2)
        self.assertEqual(c_rc["body"]["elements"][0], {"tag": "markdown", "content": short_text})
        self.assertTrue(all(el.get("tag") != "collapsible_panel" for el in c_rc["body"]["elements"]))

    def test_762_readback_and_pending_excerpt_use_summary(self):
        a = self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="催一下上线审批")
        self.pull(a)
        self.deliver()

        short_sum = "**已切 10% 灰度，全量切流等你拍板。**"
        rc, out, err = self.ask(
            "--about", a, "--key", "K762P", "--summary", short_sum, title="审批上线", text=LONG_DETAIL
        )
        self.assertEqual(rc, 0, out + err)
        q_mid = self.store.thread_by_key("tp_alpha", "K762P")["root_message_id"]

        # 拉回这张带 collapsible_panel 的卡片，核实能正常认出「需要你定」并登记 escalation
        raw_card_content = self.lark.msgs[q_mid]["content"]
        self.assertIn("▶ more\n", raw_card_content)
        self.assertIn("🐕 需要你定 · 审批上线 [tp_alpha]", plain_text(raw_card_content))
        self.pull(q_mid)
        esc = self.store.get_escalation(q_mid)
        self.assertIsNotNone(esc)
        self.assertEqual(esc["topic_id"], "tp_alpha")

        # 查「待办」清单：摘录只取白话总结，不把 more 面板里的代码块或长明细漏出来
        rc_p, out_p, err_p = self.cli("pending")
        self.assertEqual(rc_p, 0, err_p)
        self.assertIn("已切 10% 灰度，全量切流等你拍板。", out_p)
        self.assertNotIn("kubectl -n prod", out_p)
        self.assertNotIn("排查与验证明细", out_p)
        self.assertNotIn("more", out_p)

    def test_762_at_mentions_hoisted_outside_more_panel(self):
        m_alice = self.lark.add(chat_id=ALPHA_P2P, sender_id=ALICE, content="请开启测试环境推荐")
        self.pull(m_alice)
        self.deliver()

        # 1) 原文开头写了 <at id=ou_alice></at>，而 --summary 没写：自动提到外层 summary 最前面，且剥掉 more 开头重复的 @
        detail_with_at = f"<at id={ALICE}></at> 已全部配好并验证通过：\n" + LONG_DETAIL
        rc, out, err = self.cli(
            "reply", "--topic", "tp_alpha", "--to", m_alice,
            "--summary", SUMMARY, "--card-text", detail_with_at,
        )
        self.assertEqual(rc, 0, out + err)
        c = card_of(self.lark.sent("+messages-reply")[-1])
        els = c["body"]["elements"]
        self.assertEqual(els[0], {"tag": "markdown", "content": f"<at id={ALICE}></at> {SUMMARY}"})
        self._assert_reply_same_row(els[1], f"已全部配好并验证通过：\n{LONG_DETAIL}")

        # 2) --text 里写了文本格式 <at user_id="ou_alice">Alice</at> + 代码块里的 `<at id=ou_fake></at>`：
        #    真 @ 转成 <at id=ou_alice></at> 提到外层；代码块/反引号里的示例不提；<at> 标签长度不占 220 字额度
        near_limit_sum = f'<at user_id="{ALICE}">Alice</at> ' + ("字" * 215)
        detail_with_code_at = "说明：写法如 `<at id=ou_code_only></at>`\n" + LONG_DETAIL
        rc2, out2, err2 = self.cli("reply-card", "--summary", near_limit_sum, "--text", detail_with_code_at)
        self.assertEqual(rc2, 0, err2)
        c2 = json.loads(out2)
        els2 = c2["body"]["elements"]
        self.assertEqual(els2[0]["content"], f"<at id={ALICE}></at> " + ("字" * 215))
        self.assertNotIn("ou_code_only", els2[0]["content"])
        self._assert_reply_same_row(els2[1], detail_with_code_at)

        # 3) 发给主人的 ask / report：原文里引用了同事的 <at id=ou_alice></at>，不误提到给主人的白话总结开头
        rc3, out3, err3 = self.ask("--summary", SUMMARY, kind="report", title="同步进度", text=detail_with_at)
        self.assertEqual(rc3, 0, out3 + err3)
        c3 = card_of(self.lark.sent()[-1])
        self.assertEqual(c3["body"]["elements"][0], {"tag": "markdown", "content": SUMMARY})
        self._assert_more_panel(c3["body"]["elements"][1], detail_with_at)


