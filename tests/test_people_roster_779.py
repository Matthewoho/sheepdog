"""规格 7.79（DEMO-149-ROSTER-AND-PERSONA-CONTRACT）回归测试（REG-031）：
1. 原生 SQLite people_roster 双向种子、中英文/花名反查与 sheepdog roster CLI；
2. 语音/文字自然语言实时自学习落盘与私聊检索词展开（expand_contact_query_terms）；
3. 对主人汇报称呼归一化（canonicalize_names_for_owner）；
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
        """REG-031：验证自动种子双向映射（浩宇<->Tovi、思远<->Florian Yuan、阿魏<->Owen Wei 等）与 sheepdog roster CLI。"""
        rows = ensure_people_roster_seeded(self.db_path)
        self.assertGreaterEqual(len(rows), 8)
        expected_pairs = [
            ("浩宇", "Tovi", "浩宇"),
            ("思远", "Florian Yuan", "思远"),
            ("阿魏", "Owen Wei", "阿魏"),
            ("饶总", "Garrett Rao", "饶总"),
            ("昭明", "Zhaoming Sui", "昭明"),
            ("Rui", "Rui Song", "Rui"),
            ("Miles", "Miles Qiu", "Miles"),
            ("Darcy", "Darcy Vale", "Darcy"),
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
        self.assertTrue(any(item["person_key"] == "haoyu" and item["preferred_name"] == "浩宇" for item in parsed_list))

        code_lk, out_lk = self._run_cli(["roster", "lookup", "Tovi", "--db", str(self.db_path), "--json"])
        self.assertEqual(code_lk, 0)
        self.assertEqual(json.loads(out_lk)["preferred_name"], "浩宇")

        code_set, out_set = self._run_cli(
            ["roster", "set", "--person", "Tovi", "--prefer", "浩宇", "--add-alias", "小严,严浩宇", "--db", str(self.db_path), "--json"]
        )
        self.assertEqual(code_set, 0)
        self.assertIn("严浩宇", json.loads(out_set)["aliases"])
        hit_new = lookup_person("严浩宇", self.db_path)
        self.assertIsNotNone(hit_new)
        assert hit_new is not None
        self.assertEqual(hit_new.person_key, "haoyu")

        code_per, out_per = self._run_cli(["roster", "persona", "--config", str(self.cfg_path), "--json"])
        self.assertEqual(code_per, 0)
        self.assertEqual(json.loads(out_per)["name"], "Mattheuu")

    def test_reg_031_voice_natural_language_self_learning_and_query_expansion(self) -> None:
        """REG-031：验证自然语言指令实时自学习落盘 SQLite，且 expand_contact_query_terms 展开出英文名与 p2p_chat_id。"""
        ensure_people_roster_seeded(self.db_path)
        rec_zk = extract_and_apply_roster_learning("浩宇就是Tovi，以后叫他浩宇", self.db_path)
        self.assertIsNotNone(rec_zk)
        assert rec_zk is not None
        self.assertEqual(rec_zk.person_key, "haoyu")
        self.assertEqual(rec_zk.preferred_name, "浩宇")

        rec_wb = extract_and_apply_roster_learning("把 Florian Yuan 的花名记成思远", self.db_path)
        self.assertIsNotNone(rec_wb)
        assert rec_wb is not None
        self.assertEqual(rec_wb.person_key, "florian_yuan")
        self.assertEqual(rec_wb.preferred_name, "思远")

        rec_custom = extract_and_apply_roster_learning("把 Rui Song 的花名记成阿瑞仔", self.db_path)
        self.assertIsNotNone(rec_custom)
        reloaded_kf = lookup_person("阿瑞仔", self.db_path)
        self.assertIsNotNone(reloaded_kf)
        assert reloaded_kf is not None
        self.assertEqual(reloaded_kf.person_key, "rui_song")
        self.assertEqual(reloaded_kf.preferred_name, "阿瑞仔")

        aliases, oids, cids = expand_contact_query_terms("浩宇和思远最近私聊跟我说了啥", self.db_path)
        self.assertIn("tovi", aliases)
        self.assertIn("浩宇", aliases)
        self.assertIn("florian yuan", aliases)
        self.assertIn(rec_wb.open_id, oids)
        self.assertIn(rec_wb.p2p_chat_id, cids)


    def test_reg_031_canonicalize_names_for_owner(self) -> None:
        """REG-031：验证 canonicalize_names_for_owner 将 Florian Yuan->思远、人名 Tovi->浩宇。"""
        ensure_people_roster_seeded(self.db_path)
        raw = "私聊里最晚找您的是 Florian Yuan，Tovi 也在群里回了消息"
        out = canonicalize_names_for_owner(raw, self.db_path)
        self.assertEqual(out, "私聊里最晚找您的是 思远，浩宇 也在群里回了消息")
        self.assertIn("思远", out)
        self.assertIn("浩宇 也在群里回了消息", out)
        self.assertNotIn("Florian Yuan", out)

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

        wb_prof = resolve_speaker_profile("Florian Yuan", self.db_path, persona=persona)
        self.assertFalse(wb_prof.is_owner)
        self.assertEqual(wb_prof.preferred_name, "思远")
        self.assertIn("我是 Mattheuu，Matthew 的 AI 助理", wb_prof.assistant_intro)

        tovi_prof = resolve_speaker_profile("Tovi", self.db_path, persona=persona)
        self.assertFalse(tovi_prof.is_owner)
        self.assertEqual(tovi_prof.preferred_name, "浩宇")
        self.assertIn("我是 Mattheuu，Matthew 的 AI 助理", tovi_prof.assistant_intro)

        lh_prof = resolve_speaker_profile("Owen Wei", self.db_path, persona=persona)
        self.assertFalse(lh_prof.is_owner)
        self.assertTrue(lh_prof.is_vip)
        self.assertEqual(lh_prof.preferred_name, "阿魏")
        self.assertIn("我是 Mattheuu，Matthew 的 AI 助理", lh_prof.assistant_intro)

    def test_reg_032_gus_vs_tovi_haoyu_entity_isolation_and_seed_self_healing(self) -> None:
        """REG-032：验证 haoyu(Tovi) 与 gus_han(Gus Han) 实体隔离、旧库脏别名与错绑 ID 自愈，以及 Gus 不被替换为浩宇。"""
        import sqlite3
        from sheepdog.people_roster import BUILTIN_ROSTER_SEEDS, _connect

        with _connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO people_roster VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "haoyu",
                    BUILTIN_ROSTER_SEEDS["gus_han"].open_id,
                    "Tovi",
                    "浩宇",
                    json.dumps(["浩宇", "tovi", "haoyu", "hao yu", "gus", "gus han", "bygus"], ensure_ascii=False),
                    BUILTIN_ROSTER_SEEDS["gus_han"].p2p_chat_id,
                    "colleague:浩宇(Tovi)",
                    "2026-01-06T04:08:00Z",
                ),
            )

        ensure_people_roster_seeded(self.db_path)
        by_gus = lookup_person("Gus", self.db_path)
        by_tovi = lookup_person("Tovi", self.db_path)
        self.assertIsNotNone(by_gus)
        self.assertIsNotNone(by_tovi)
        assert by_gus is not None and by_tovi is not None
        self.assertEqual(by_gus.person_key, "gus_han")
        self.assertEqual(by_gus.open_id, BUILTIN_ROSTER_SEEDS["gus_han"].open_id)
        self.assertEqual(by_tovi.person_key, "haoyu")
        self.assertEqual(by_tovi.open_id, "")
        self.assertEqual(by_tovi.p2p_chat_id, "")
        self.assertNotIn("gus", [a.lower() for a in by_tovi.aliases])
        self.assertNotIn("gus han", [a.lower() for a in by_tovi.aliases])
        self.assertNotIn("bygus", [a.lower() for a in by_tovi.aliases])

        self.assertEqual(
            canonicalize_names_for_owner("哦哦，Gus呀，查了后台记录，Gus目前没有参与，Tovi还在跟进", self.db_path),
            "哦哦，Gus呀，查了后台记录，Gus目前没有参与，浩宇还在跟进",
        )
        self.assertEqual(canonicalize_names_for_owner("说的是Gus，Tovi 发来消息", self.db_path), "说的是Gus，浩宇 发来消息")


if __name__ == "__main__":
    unittest.main()
