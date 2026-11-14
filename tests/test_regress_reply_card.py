"""回归 7.17：配置里的代回标记 session.reply_suffix 要进 playbook 的 {{reply_suffix}}，会话的开场和每批末尾都照配置写。
全部使用合成数据。"""

import shutil
import unittest
from pathlib import Path

from sheepdog.engine import BUS_TOPIC_ID

from test_roster import FIXTURES, Base, msg

MARK = "[FX-Reply-Mark]"


class ReplySuffixFromConfigTest(Base):
    def test_regression_7_17_reply_suffix_from_config_reaches_prompts(self):
        """回归 7.17：配置的 reply_suffix 替换进 common.md（开场）和 batch_footer.md（每批末尾）里的 {{reply_suffix}}。
        坏了会怎样：会话被告知的代回标记和 reply-card 实际打的不一样（或是空的），认代回、防循环都认不出它发的卡片。"""
        pdir = Path(self.tmp.name) / "playbook"
        shutil.copytree(FIXTURES, pdir)
        (pdir / "common.md").write_text("FX-COMMON suffix={{reply_suffix}}", encoding="utf-8")
        (pdir / "batch_footer.md").write_text("FX-FOOTER suffix={{reply_suffix}}", encoding="utf-8")
        self.cfg.playbook_dir = str(pdir)
        self.cfg.session.reply_suffix = MARK
        d = self.disp()
        d.init()
        self.assertIn(f"FX-COMMON suffix={MARK}", self.sink.created[0][1])       # 总线开场
        self.assertIn(f"FX-COMMON suffix={MARK}", self.sink.to("conv_test_alpha")[0])  # 接管会话的登记通知
        self.poll([msg(message_id="om_test_sf1", chat_type="p2p", chat_id="oc_test_stranger")])
        d.dispatch_once()
        batch = self.sink.to(self.store.get_topic(BUS_TOPIC_ID)["conversation_id"])[-1]
        self.assertEqual(batch.rstrip().splitlines()[-1], f"FX-FOOTER suffix={MARK}")


if __name__ == "__main__":
    unittest.main()
