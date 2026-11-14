"""scripts/check_private_data.py：通用规则能拦住，真实账号/代号靠 ~/.config/sheepdog/private_terms.txt 注入。"""

import importlib.util
import unittest
from pathlib import Path


def _load():
    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("check_private_data", repo_root / "scripts" / "check_private_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CheckPrivateDataTest(unittest.TestCase):
    def test_generic_patterns_and_private_terms(self):
        cpd = _load()
        self.assertEqual(len(cpd.scan_text("safe text ou_test_seed_1 200000000001 Demo-Passw0rd!c0ffee42", terms=[])), 0)
        bad_sample = "\n".join([
            "def " + "_oid(a, b): return a + b",
            "id = 'ou_9e8d7c6b" + "5a4f3e2d'",
            "password = 'Example-" + "Passw0rd-sample'",
            "path = '/Users/" + "alice/notes.md'",
            "key = 'sk-" + "exampleexampleexample0001'",
        ])
        hits = cpd.scan_text(bad_sample, terms=[])
        self.assertGreaterEqual(len(hits), 5, msg=f"Expected >=5 hits, got: {hits}")
        term_sample = "\n".join(["uin = '100000000" + "999'", "role = 'Codename" + "-Verify'"])
        self.assertEqual(len(cpd.scan_text(term_sample, terms=[])), 0)
        term_hits = cpd.scan_text(term_sample, terms=["100000000999", "codename-verify"])
        self.assertEqual(len(term_hits), 2, msg=f"Expected 2 private-term hits, got: {term_hits}")


if __name__ == "__main__":
    unittest.main()
