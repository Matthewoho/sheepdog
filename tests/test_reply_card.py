"""7.17 代回改为卡片 + 右下角后缀：reply-card 输出合法 Card 2.0 JSON、宽度取配置、--text-file 与标准输入、
空正文报错；agent_markers 在卡片内容里命中（旧前缀仍命中、普通主人消息不命中）；主人发言背景与防循环都认卡片代回。"""

import io
import json
import os
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.config import OwnerContextConfig, SessionConfig
from sheepdog.engine import BUS_TOPIC_ID, Collector, is_agent_reply
from sheepdog.models import Mention
from sheepdog.playbook import Playbook
from sheepdog.roster import Roster

from test_roster import ME, Base, FakeSource, msg

MARK = "[FX-Reply]"
PREFIX = "FX-PREFIX "


def card_content(text: str) -> str:
    """sheepdog 从飞书拉回来的卡片消息内容形如 <card>\\n正文\\n标记\\n</card>。"""
    return f"<card>\n{text}\n{MARK}\n</card>"


class ReplyCardCliTest(Base):
    def run_cli(self, *argv, config="", stdin=None):
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\n' + config, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}
        patches = [mock.patch.dict(os.environ, env), redirect_stdout(out), mock.patch("sys.stderr", err)]
        if stdin is not None:
            patches.append(mock.patch("sys.stdin", io.StringIO(stdin)))
        for p in patches:
            p.__enter__()
        try:
            rc = cli.main(list(argv))
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)
        return rc, out.getvalue(), err.getvalue()

    def test_structure_and_defaults(self):
        rc, out, _ = self.run_cli("reply-card", "--text", "收到，周五前给你 <at id=ou_test_bob></at> 😀\n第二行")
        self.assertEqual(rc, 0)
        card = json.loads(out)
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["config"]["width_mode"], SessionConfig().reply_card_width)
        body, suffix = card["body"]["elements"]
        self.assertEqual(body, {"tag": "markdown", "content": "收到，周五前给你 <at id=ou_test_bob></at> 😀\n第二行"})
        self.assertEqual(suffix, {"tag": "markdown", "content": f"<font color='grey'>{SessionConfig().reply_suffix}</font>",
                                  "text_align": "right", "text_size": "notation"})
        self.assertIn("😀", out)  # 中文与 emoji 原样，不转义

    def test_config_width_and_suffix(self):
        rc, out, _ = self.run_cli("reply-card", "--text", "x",
                                  config=f'[session]\nreply_card_width = "fill"\nreply_suffix = "{MARK}"\n')
        card = json.loads(out)
        self.assertEqual(card["config"]["width_mode"], "fill")
        self.assertIn(MARK, card["body"]["elements"][1]["content"])
        rc, _, err = self.run_cli("reply-card", "--text", "x", config='[session]\nreply_card_width = "huge"\n')
        self.assertEqual(rc, 2)
        self.assertIn("reply_card_width", err)

    def test_text_file_stdin_and_empty(self):
        f = Path(self.tmp.name) / "body.md"
        f.write_text("**文件里的正文**\n", encoding="utf-8")
        rc, out, _ = self.run_cli("reply-card", "--text-file", str(f))
        self.assertEqual(json.loads(out)["body"]["elements"][0]["content"], "**文件里的正文**\n")
        rc, out, _ = self.run_cli("reply-card", "--text-file", "-", stdin="标准输入的正文")
        self.assertEqual(json.loads(out)["body"]["elements"][0]["content"], "标准输入的正文")
        for argv, stdin in ((("--text", "   "), None), (("--text-file", "-"), "\n\n")):
            rc, out, err = self.run_cli("reply-card", *argv, stdin=stdin)
            self.assertEqual((rc, out), (2, ""))
            self.assertIn("正文为空", err)

    def test_suffix_placeholder(self):
        pb = Playbook(Path(self.tmp.name), {"reply_suffix": MARK})
        (Path(self.tmp.name) / "batch_footer.md").write_text("结尾带 {{reply_suffix}}", encoding="utf-8")
        self.assertEqual(pb.render("batch_footer.md"), f"结尾带 {MARK}")


class AgentMarkerTest(Base):
    def test_is_agent_reply(self):
        oc = OwnerContextConfig(skip_prefixes=[PREFIX], agent_markers=[MARK])
        self.assertTrue(is_agent_reply(card_content("好的，已经安排"), oc))   # 卡片末尾命中
        self.assertTrue(is_agent_reply(PREFIX + "老格式的代回", oc))          # 旧前缀仍命中
        self.assertFalse(is_agent_reply("我自己说的话", oc))                  # 普通主人消息
        self.assertFalse(is_agent_reply(card_content("x"), OwnerContextConfig(skip_prefixes=[PREFIX])))

    def test_owner_context_skips_card_reply_and_loop_counts_it(self):
        self.roster = Roster()
        self.cfg.owner_context.enabled = True
        self.cfg.owner_context.skip_prefixes = [PREFIX]
        self.cfg.owner_context.agent_markers = [MARK]
        d = self.disp(Roster())
        d.init()
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        col = Collector(self.cfg, self.store, FakeSource([
            [msg(message_id="om_test_bot", chat_id="oc_test_loop", sender_type="app", sender_name="机器人",
                 mentions=[Mention(ME)], create_time=now)],
            [msg(message_id=f"om_test_c{i}", chat_id="oc_test_loop", sender_id=ME, content=card_content(f"第{i}次"),
                 create_time=now) for i in range(5)],
        ]), Roster())
        col.poll_once()
        d.dispatch_once()  # 机器人的消息投到总线，之后主人的发言本该跟过去
        col.poll_once()
        self.assertEqual(self.row("om_test_c0")["route"], "self")  # 卡片代回不作为主人背景送回
        self.assertEqual(len(self.store.open_loop_events()), 1)    # 防循环按卡片代回计数
        self.assertTrue(any("疑似循环" in (r["content"] or "") for r in self.store.conn.execute(
            "SELECT content FROM messages WHERE topic_id=? AND chat_type='sheepdog'", (BUS_TOPIC_ID,))))
