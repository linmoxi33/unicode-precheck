import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import unicode_precheck as up  # noqa: E402

BIN = os.path.join(ROOT, "bin", "unicode-precheck")
CLEAN = "CREATE TABLE orders (order_id BIGINT, customer_email TEXT, created_at TIMESTAMP);\n"
DIRTY = 'CREATE TABLE "顧客マスタ" ("電話番号" VARCHAR(32), "住所" TEXT);\n'


def run_cli(args, cwd=ROOT):
    return subprocess.run([sys.executable, BIN] + args, cwd=cwd,
                          capture_output=True, text=True, timeout=60)


class UpstreamMirror(unittest.TestCase):
    def test_term_name_collapses_non_latin(self):
        self.assertEqual(up.to_term_name("顧客マスタ"), "")

    def test_term_name_splits_accented_word(self):
        self.assertEqual(up.to_term_name("Société"), "Soci T")

    def test_pii_normalizer_collapses_non_latin(self):
        self.assertEqual(up.normalize("電話番号"), "")

    def test_pii_normalizer_fragments_accented_word(self):
        self.assertEqual(up.normalize("téléphone"), "t l phone")

    def test_synonym_tokenizer_yields_nothing(self):
        self.assertEqual(up.synonym_word_tokens("顧客"), [])

    def test_date_inference_does_not_claim_unparsable(self):
        # Regression: a parse must succeed before a year window is applied.
        self.assertIsNone(up.infer_date_format(["2024.03.15", "2024.04.02"]))
        self.assertEqual(up.infer_date_format(["2024-03-15"]), "YYYY-MM-DD")


class Witness(unittest.TestCase):
    def test_witness_is_shortest_collapsing_substring(self):
        w = up.minimal_collapse_witness("Société")
        self.assertIsNotNone(w)
        self.assertEqual(w["substring"], "é")
        self.assertEqual(w["length"], 1)
        self.assertEqual(up._tokenize(w["substring"]), [])

    def test_no_witness_for_ascii(self):
        self.assertIsNone(up.minimal_collapse_witness("customer_email"))


class Parsing(unittest.TestCase):
    def test_quoted_and_bare_identifiers(self):
        tables = up.parse_ddl("CREATE TABLE `주문_내역` (`주문번호` BIGINT, note TEXT);")
        self.assertEqual(tables[0]["name"], "주문_내역")
        self.assertEqual([c["name"] for c in tables[0]["columns"]], ["주문번호", "note"])

    def test_constraints_are_not_columns(self):
        tables = up.parse_ddl("CREATE TABLE t (id BIGINT PRIMARY KEY, name TEXT, "
                              "CONSTRAINT fk CHECK (id > 0));")
        self.assertEqual([c["name"] for c in tables[0]["columns"]], ["id", "name"])


class CliBehaviour(unittest.TestCase):
    def _write(self, text, suffix=".sql"):
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_clean_schema_exits_zero_and_json_is_one_object(self):
        proc = run_cli(["--json", self._write(CLEAN)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["summary"]["findings"], 0)
        self.assertEqual(report["upstream"], "NVIDIA/auto-ontology")

    def test_dirty_schema_exits_one(self):
        proc = run_cli([self._write(DIRTY)])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("U001", proc.stdout)

    def test_fail_on_high_ignores_medium_only(self):
        ddl = 'CREATE TABLE "Société" ("adresse_ligne_1" TEXT);'
        proc = run_cli(["--json", "--fail-on", "high", self._write(ddl)])
        report = json.loads(proc.stdout)
        self.assertTrue(all(f["severity"] == "medium" for f in report["findings"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_fail_on_high_still_trips_on_high(self):
        proc = run_cli(["--json", "--fail-on", "high", self._write(DIRTY)])
        self.assertEqual(proc.returncode, 1)
        self.assertGreater(json.loads(proc.stdout)["summary"]["by_severity"].get("high", 0), 0)

    def test_missing_file_is_usage_error(self):
        proc = run_cli(["/tmp/definitely-not-here-1234.sql"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cannot read", proc.stderr)

    def test_non_ddl_input_is_usage_error(self):
        proc = run_cli([self._write("SELECT 1;", suffix=".txt")])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no CREATE TABLE", proc.stderr)

    def test_samples_drive_date_finding(self):
        ddl = self._write("CREATE TABLE f (date_facture DATE);")
        samples = self._write(json.dumps({"f.date_facture": ["2024.03.15"]}), suffix=".json")
        proc = run_cli(["--json", "--samples", samples, ddl])
        report = json.loads(proc.stdout)
        self.assertEqual([f["rule"] for f in report["findings"]], ["U006"])
        self.assertEqual(proc.returncode, 1)

    def test_bad_samples_json_is_usage_error(self):
        proc = run_cli(["--samples", self._write("{oops", suffix=".json"), self._write(CLEAN)])
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)