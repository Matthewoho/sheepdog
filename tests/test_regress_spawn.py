"""回归 7.2：spawn 只把新 conversation_id 记在账本、不回写名册，`sheepdog sessions` 要把名册和账本的差异、前任显示出来。
全部使用合成数据。"""

import io
import os
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from sheepdog import cli
from sheepdog.roster import parse_roster

from test_handover_watch import _toml, handover_data
from test_roster import ME, Base


def blocks(text: str) -> dict[str, str]:
    """sessions 的输出按会话拆开：topic_id → 这个会话的几行。"""
    out: dict[str, str] = {}
    for part in re.split(r"(?m)^(?=- )", text):
        m = re.search(r"topic=(\S+)", part)
        if m:
            out[m.group(1)] = part
    return out


class SpawnSessionsViewTest(Base):
    def test_regression_7_2_sessions_shows_roster_vs_ledger_and_predecessor(self):
        """回归 7.2：名册里 conversation_id 留空的条目 spawn 以后，sessions 写明「名册留空、账本是新会话」和前任 id。
        坏了会怎样：名册是人写的、spawn 不回写，sessions 不显示差异，人看名册以为这个会话还没建，也看不出它接的是谁。"""
        (Path(self.tmp.name) / "roster.toml").write_text(_toml(handover_data()), encoding="utf-8")
        cfg_file = Path(self.tmp.name) / "config.toml"
        cfg_file.write_text(f'self_open_id = "{ME}"\nroster_path = "roster.toml"\n', encoding="utf-8")
        env = {"SHEEPDOG_CONFIG": str(cfg_file), "SHEEPDOG_STATE_DIR": self.tmp.name}

        def sessions() -> dict[str, str]:
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), redirect_stdout(out):
                self.assertEqual(cli.main(["sessions"]), 0)
            return blocks(out.getvalue())

        self.roster = parse_roster(handover_data())
        d = self.disp()
        d.sync_roster()
        self.assertIn("前任: conv_test_old", sessions()["tp_beta"])  # 还没 spawn 时也看得出它要接谁

        cid = d.spawn("beta")["conversation_id"]
        after = sessions()
        self.assertIn(f"名册 conversation: （留空：由 sheepdog 新建）  账本: {cid}  spawn 于 ", after["tp_beta"])
        self.assertIn("前任: conv_test_old", after["tp_beta"])
        # 名册里写了 id、和账本一致的条目不显示差异
        self.assertNotIn("名册 conversation:", after["tp_alpha"])


if __name__ == "__main__":
    unittest.main()
