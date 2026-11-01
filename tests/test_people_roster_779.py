"""规格 7.79（SD-779-ROSTER-AND-PERSONA-CONTRACT）回归测试（REG-031）：
1. 原生 SQLite people_roster 双向种子、中英文/花名反查与 sheepdog roster CLI；
2. 语音/文字自然语言实时自学习落盘与私聊检索词展开（expand_contact_query_terms）；
3. 对主人汇报称呼归一化（canonicalize_names_for_owner）与 Mac 桌面端平台词防误伤；
4. Mattheuu 单一人格契约（PersonaContract）与多说话人身份反查（resolve_speaker_profile）。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from sheepdog import cli
from sheepdog.people_roster import (
    PersonaContract,
    canonicalize_names_for_owner,
    ensure_people_roster_seeded,
    expand_contact_query_terms,
    extract_and_apply_roster_learning,
    lookup_person,
    resolve_speaker_profile,
)


class PeopleRoster779Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "sheepdog_test.sqlite3"
        self.cfg_path = self.root / "config.toml"
        self.cfg_path.write_text(
            '[persona]\nname = "Mattheuu"\nowner = "Matthew"\nrole = "Matthew 的 AI 助理与工作分身"\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        return code, buf.getvalue().strip()

    def test_reg_031_auto_seed_and_bidirectional_lookup_and_cli(self) -> None:
        """REG-031：验证自动种子双向映射（泽楷<->Mac、文博<->Wenbo Wang、老胡<->Andy Hu 等）与 sheepdog roster CLI。"""
        rows = ensure_people_roster_seeded(self.db_path)
        self.assertGreaterEqual(len(rows), 8)
        expected_pairs = [
            ("泽楷", "Mac", "泽楷"),
            ("文博", "Wenbo Wang", "文博"),
            ("老胡", "Andy Hu", "老胡"),
            ("曹总", "Sherwin Cao", "曹总"),
            ("姜磊", "Jianglei Li", "姜磊"),
            ("Kai", "Kai Feng", "Kai"),
            ("Jerry", "Jerry Chu", "Jerry"),
            ("Sammy", "Sammy Shi", "Sammy"),
        ]
        for cn_alias, en_name, pref in expected_pairs:
            by_cn = lookup_person(cn_alias, self.db_path)
            by_en = lookup_person(en_name, self.db_path)
            self.assertIsNotNone(by_cn, f"lookup({cn_alias}) 应命中")
            self.assertIsNotNone(by_en, f"lookup({en_name}) 应命中")
            assert by_cn is not None and by_en is not None
            self.assertEqual(by_cn.person_key, by_en.person_key)
            self.assertEqual(by_cn.preferred_name, pref)
            self.assertEqual(by_en.feishu_name, en_name)

        code_list, out_list = self._run_cli(["roster", "list", "--db", str(self.db_path), "--json"])
        self.assertEqual(code_list, 0)
        parsed_list = json.loads(out_list)
        self.assertTrue(any(item["person_key"] == "zekai" and item["preferred_name"] == "泽楷" for item in parsed_list))

        code_lk, out_lk = self._run_cli(["roster", "lookup", "Mac", "--db", str(self.db_path), "--json"])
        self.assertEqual(code_lk, 0)
        self.assertEqual(json.loads(out_lk)["preferred_name"], "泽楷")

        code_set, out_set = self._run_cli(
            ["roster", "set", "--person", "Mac", "--prefer", "泽楷", "--add-alias", "小罗,罗泽楷", "--db", str(self.db_path), "--json"]
        )
        self.assertEqual(code_set, 0)
        self.assertIn("罗泽楷", json.loads(out_set)["aliases"])
        hit_new = lookup_person("罗泽楷", self.db_path)
        self.assertIsNotNone(hit_new)
        assert hit_new is not None
        self.assertEqual(hit_new.person_key, "zekai")

        code_per, out_per = self._run_cli(["roster", "persona", "--config", str(self.cfg_path), "--json"])
        self.assertEqual(code_per, 0)
        self.assertEqual(json.loads(out_per)["name"], "Mattheuu")

    def test_reg_031_voice_natural_language_self_learning_and_query_expansion(self) -> None:
        """REG-031：验证自然语言指令实时自学习落盘 SQLite，且 expand_contact_query_terms 展开出英文名与 p2p_chat_id。"""
        ensure_people_roster_seeded(self.db_path)
        rec_zk = extract_and_apply_roster_learning("泽楷就是Mac，以后叫他泽楷", self.db_path)
        self.assertIsNotNone(rec_zk)
        assert rec_zk is not None
        self.assertEqual(rec_zk.person_key, "zekai")
        self.assertEqual(rec_zk.preferred_name, "泽楷")

        rec_wb = extract_and_apply_roster_learning("把 Wenbo Wang 的花名记成文博", self.db_path)
        self.assertIsNotNone(rec_wb)
        assert rec_wb is not None
        self.assertEqual(rec_wb.person_key, "wenbo_wang")
        self.assertEqual(rec_wb.preferred_name, "文博")

        rec_custom = extract_and_apply_roster_learning("把 Kai Feng 的花名记成凯神", self.db_path)
        self.assertIsNotNone(rec_custom)
        reloaded_kf = lookup_person("凯神", self.db_path)
        self.assertIsNotNone(reloaded_kf)
        assert reloaded_kf is not None
        self.assertEqual(reloaded_kf.person_key, "kai_feng")
        self.assertEqual(reloaded_kf.preferred_name, "凯神")

        aliases, oids, cids = expand_contact_query_terms("泽楷和文博最近私聊跟我说了啥", self.db_path)
        self.assertIn("mac", aliases)
        self.assertIn("泽楷", aliases)
        self.assertIn("wenbo wang", aliases)
        self.assertIn(rec_wb.open_id, oids)
        self.assertIn(rec_wb.p2p_chat_id, cids)

        plat_aliases, plat_oids, plat_cids = expand_contact_query_terms("Mac 桌面端安装包下载链接", self.db_path)
        self.assertNotIn("泽楷", plat_aliases)
        self.assertEqual(len(plat_oids), 0)
        self.assertEqual(len(plat_cids), 0)

    def test_reg_031_canonicalize_names_for_owner_and_mac_platform_guard(self) -> None:
        """REG-031：验证 canonicalize_names_for_owner 将 Wenbo Wang->文博、人名 Mac->泽楷，且保留 Mac 桌面端不被误伤。"""
        ensure_people_roster_seeded(self.db_path)
        raw = "私聊里最晚找您的是 Wenbo Wang，Mac 也在群里回了消息，Mac 桌面端安装包已就绪"
        out = canonicalize_names_for_owner(raw, self.db_path)
        self.assertEqual(out, "私聊里最晚找您的是 文博，泽楷 也在群里回了消息，Mac 桌面端安装包已就绪")
        self.assertIn("文博", out)
        self.assertIn("泽楷 也在群里回了消息", out)
        self.assertIn("Mac 桌面端安装包已就绪", out)
        self.assertNotIn("Wenbo Wang", out)

    def test_reg_031_persona_contract_and_multi_speaker_counterparty_profile(self) -> None:
        """REG-031：验证 PersonaContract.from_config 读取 Mattheuu 及 resolve_speaker_profile 多说话人身份反查。"""
        ensure_people_roster_seeded(self.db_path)
        persona = PersonaContract.from_config(self.cfg_path)
        self.assertEqual(persona.name, "Mattheuu")
        self.assertEqual(persona.owner, "Matthew")
        self.assertIn("Matthew 的 AI 助理", persona.role)

        owner_prof = resolve_speaker_profile("owner(Matthew)", self.db_path, persona=persona)
        self.assertTrue(owner_prof.is_owner)
        self.assertEqual(owner_prof.preferred_name, "Matthew")

        wb_prof = resolve_speaker_profile("Wenbo Wang", self.db_path, persona=persona)
        self.assertFalse(wb_prof.is_owner)
        self.assertEqual(wb_prof.preferred_name, "文博")
        self.assertIn("我是 Mattheuu，Matthew 的 AI 助理", wb_prof.assistant_intro)

        mac_prof = resolve_speaker_profile("Mac", self.db_path, persona=persona)
        self.assertFalse(mac_prof.is_owner)
        self.assertEqual(mac_prof.preferred_name, "泽楷")
        self.assertIn("我是 Mattheuu，Matthew 的 AI 助理", mac_prof.assistant_intro)

        lh_prof = resolve_speaker_profile("Andy Hu", self.db_path, persona=persona)
        self.assertFalse(lh_prof.is_owner)
        self.assertTrue(lh_prof.is_vip)
        self.assertEqual(lh_prof.preferred_name, "老胡")
        self.assertIn("我是 Mattheuu，Matthew 的 AI 助理", lh_prof.assistant_intro)

    def test_reg_032_max_vs_mac_zekai_entity_isolation_and_seed_self_healing(self) -> None:
        """REG-032：验证 zekai(Mac) 与 max_luo(Max Luo) 实体隔离、旧库脏别名与错绑 ID 自愈，以及 Max 不被替换为泽楷。"""
        import sqlite3
        from sheepdog.people_roster import BUILTIN_ROSTER_SEEDS, _connect

        with _connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO people_roster VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "zekai",
                    BUILTIN_ROSTER_SEEDS["max_luo"].open_id,
                    "Mac",
                    "泽楷",
                    json.dumps(["泽楷", "mac", "zekai", "ze kai", "max", "max luo", "bymax"], ensure_ascii=False),
                    BUILTIN_ROSTER_SEEDS["max_luo"].p2p_chat_id,
                    "colleague:泽楷(Mac)",
                    "2026-10-23T00:00:00Z",
                ),
            )

        ensure_people_roster_seeded(self.db_path)
        by_max = lookup_person("Max", self.db_path)
        by_mac = lookup_person("Mac", self.db_path)
        self.assertIsNotNone(by_max)
        self.assertIsNotNone(by_mac)
        assert by_max is not None and by_mac is not None
        self.assertEqual(by_max.person_key, "max_luo")
        self.assertEqual(by_max.open_id, BUILTIN_ROSTER_SEEDS["max_luo"].open_id)
        self.assertEqual(by_mac.person_key, "zekai")
        self.assertEqual(by_mac.open_id, "")
        self.assertEqual(by_mac.p2p_chat_id, "")
        self.assertNotIn("max", [a.lower() for a in by_mac.aliases])
        self.assertNotIn("max luo", [a.lower() for a in by_mac.aliases])
        self.assertNotIn("bymax", [a.lower() for a in by_mac.aliases])

        self.assertEqual(
            canonicalize_names_for_owner("哦哦，Max呀，查了后台记录，Max目前没有参与，Mac还在跟进", self.db_path),
            "哦哦，Max呀，查了后台记录，Max目前没有参与，泽楷还在跟进",
        )
        self.assertEqual(canonicalize_names_for_owner("说的是Max，Mac 发来消息，Mac 桌面端正常", self.db_path), "说的是Max，泽楷 发来消息，Mac 桌面端正常")


if __name__ == "__main__":
    unittest.main()
