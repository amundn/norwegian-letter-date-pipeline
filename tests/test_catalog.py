import json
import tempfile
import unittest
from pathlib import Path

from letter_pipeline.catalog import write_catalog


class CatalogTests(unittest.TestCase):
    def test_catalog_links_results_and_stage_artifacts(self):
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            letter = root / "03_abc" / "sourcehash" / "profile_qwen"
            stage = letter / "04_events_s1_01_fingerprint"
            stage.mkdir(parents=True)
            (letter / "metadata.json").write_text(json.dumps({
                "letter_id": "03", "source_file": "03.txt"}), encoding="utf-8")
            (letter / "profile.json").write_text(json.dumps({
                "prototype_version": "prototype-0.2.8", "model": "qwen3.8:27b"}), encoding="utf-8")
            (letter / "source-lines.json").write_text("[]", encoding="utf-8")
            (letter / "result.json").write_text(json.dumps({
                "events": [{"date_basis": "hypothesis"}, {"date_basis": "unresolved"}],
                "rejected_events": [], "possible_missing_events": [],
                "validation": {"complete": False}}), encoding="utf-8")
            (stage / "state.json").write_text(json.dumps({
                "stage": "events", "label": "04_events_s1_01", "accepted": True,
                "attempts": [{"seconds": 2.5, "completed_utc": "2026-11-08T17:00:00+00:00"}]}), encoding="utf-8")
            (stage / "attempt-00-prompt.txt").write_text("Prompt", encoding="utf-8")
            (stage / "attempt-00.json").write_text("{}", encoding="utf-8")
            (stage / "accepted.json").write_text("{}", encoding="utf-8")

            index = write_catalog(root)

            catalog_text = index.read_text(encoding="utf-8")
            steps_text = (letter / "STEPS.md").read_text(encoding="utf-8")
            self.assertIn("prototype-0.2.8", catalog_text)
            self.assertIn("2026-11-08 17:00 UTC", catalog_text)
            self.assertIn("resultat", catalog_text)
            self.assertIn("04_events_s1_01", steps_text)
            self.assertIn("prompt 1", steps_text)
            self.assertIn("godkjent JSON", steps_text)

    def test_catalog_includes_active_checkpoints_without_a_result_yet(self):
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            letter = root / "03_abc" / "sourcehash" / "profile_qwen"
            stage = letter / "01_scan_fingerprint"
            stage.mkdir(parents=True)
            (letter / "metadata.json").write_text(json.dumps({"letter_id": "03"}), encoding="utf-8")
            (letter / "profile.json").write_text(json.dumps({"model": "qwen3.8:27b"}), encoding="utf-8")
            (stage / "state.json").write_text(json.dumps({"label": "01_scan", "attempts": []}), encoding="utf-8")

            write_catalog(root)

            self.assertIn("pågår", (root / "CATALOG.md").read_text(encoding="utf-8"))
            self.assertIn("01_scan", (letter / "STEPS.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
