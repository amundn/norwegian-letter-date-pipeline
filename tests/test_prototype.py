import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from letter_pipeline import prototype


class ScriptedRunner:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.model = "fake-model"
        self.profile = {"model": self.model}
        self.profile_hash = "test-profile"
        self.args = SimpleNamespace(date_uncertain=False, calendar_before_days=45,
            calendar_after_days=90, tokens_scan=1024, tokens_base=2048,
            tokens_audit=2048, tokens_events=4096, event_chunk_lines=40, event_overlap_lines=4)

    def run_stage(self, letter_dir, label, stage, context, validator, budget):
        expected, answer = next(self.answers)
        if expected != stage:
            raise AssertionError(f"Forventet {expected}, fikk {stage}")
        if answer is None:
            return None, ["Simulert feil"]
        errors = validator(answer)
        if errors:
            raise AssertionError(errors)
        return answer, []


def proposal(title="Person A reiser", suggested_date="1971-12-19"):
    return {"title": title, "line_ids": ["L00001", "L00003"], "date_line_ids": ["L00003"],
            "time_expression": "søndag 19.", "suggested_date": suggested_date, "alternatives": [], "reason": "Søndagen i samme måned.",
            "occurrence": "planned", "people": ["Person A"], "location": None, "category": "Reise"}


class PrototypeTests(unittest.TestCase):
    def test_calendar_supplies_weekdays_and_holidays(self):
        rows = prototype.calendar_with_holidays("1971-12-24", 0, 2)
        self.assertEqual(rows[0]["weekday"], "fredag")
        self.assertIn("julaften", rows[0]["holidays"])
        self.assertIn("1. juledag", rows[1]["holidays"])

    def test_valid_event_survives_other_invalid_event_and_disjoint_lines(self):
        answers = [("scan", {"new_sessions": []}),
            ("proto_events", {"events": [proposal(), {"title": "Dårlig linje", "line_ids": ["L99999"]}]}),
            ("proto_event_audit", {"checks": [{"event_id": "s1-w01-e1", "verdict": "supported",
                "line_ids": ["L00003"], "reason": "Ukedag og dato stemmer."}]})]
        runner = ScriptedRunner(answers)
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "01.txt"
            letter.write_text("---\nDate: 1971-12-07\n---\nPerson A skal reise.\nMer tekst.\nHan reiser søndag 19.\n", encoding="utf-8")
            summary = prototype.process_letter(runner, letter, input_dir, output_dir)
            result = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "needs_review")
        self.assertEqual(len(result["rejected_events"]), 1)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["quotes"], ["Person A skal reise.", "Han reiser søndag 19."])
        self.assertEqual(result["events"][0]["suggested_date"], "1971-12-19")
        self.assertEqual(result["events"][0]["date_basis"], "hypothesis")
        self.assertTrue(result["validation"]["schema_valid"])

    def test_wrong_weekday_is_not_promoted(self):
        rows = [{"id": "L00001", "text": "Person A skal reise."}, {"id": "L00002", "text": "Annet."},
                {"id": "L00003", "text": "Han reiser søndag 19."}]
        window = {"rows": rows, "core_start": "L00001", "core_end": "L00003"}
        events, rejected, _ = prototype.prepare_events([proposal(suggested_date="1971-12-20")], "s1", 1, window, {})
        self.assertEqual(rejected, [])
        self.assertTrue(events[0]["python_checks"])
        self.assertEqual(events[0]["date_basis"], "unresolved")
        self.assertIn("1971-12-20", events[0]["date_candidates"])

    def test_longer_grounded_event_is_kept_for_review(self):
        rows = [{"id": f"L{i:05d}", "text": f"Kildelinje {i}."} for i in range(1, 25)]
        window = {"rows": rows, "core_start": "L00001", "core_end": "L00024"}
        item = {"title": "Et besøk", "line_ids": [r["id"] for r in rows], "date_line_ids": [],
                "time_expression": None, "suggested_date": None, "alternatives": [],
                "reason": "Datoen fremgår ikke.", "occurrence": "happened", "people": [],
                "location": None, "category": "Familie"}
        events, rejected, _ = prototype.prepare_events([item], "s1", 1, window, {})
        self.assertEqual(len(events), 1)
        self.assertEqual(rejected, [])

    def test_date_checks_use_event_expression_when_two_dates_share_one_line(self):
        rows = [{"id": "L00001", "text": "Idag ringte Person B. Person A reiser søndag 19."}]
        window = {"rows": rows, "core_start": "L00001", "core_end": "L00001"}
        item = {"title": "Person B ringte", "line_ids": ["L00001"], "date_line_ids": ["L00001"],
                "time_expression": "Idag", "suggested_date": "1971-12-07", "alternatives": [],
                "reason": "I dag viser til øktdatoen.", "occurrence": "happened", "people": [],
                "location": None, "category": "Familie"}
        events, _, _ = prototype.prepare_events([item], "s1", 1, window, {})
        self.assertEqual(events[0]["python_checks"], [])

    def test_time_expression_must_be_grounded_in_date_lines(self):
        rows = [{"id": "L00001", "text": "Person A skal reise."}, {"id": "L00002", "text": "Annet."},
                {"id": "L00003", "text": "Han reiser søndag 19."}]
        item = proposal()
        item["time_expression"] = "søndag 12."
        window = {"rows": rows, "core_start": "L00001", "core_end": "L00003"}
        events, _, _ = prototype.prepare_events([item], "s1", 1, window, {})
        self.assertTrue(any("finnes ikke ordrett" in error for error in events[0]["python_checks"]))

    def test_unsupported_review_is_not_accepted(self):
        check = {"checks": [{"event_id": "s1-e1", "verdict": "supported", "line_ids": [], "reason": "Riktig."}]}
        self.assertEqual(prototype.reviews_by_key(check, "event_id", {"s1-e1"}, {"L00001": "tekst"}), {})

    def test_review_status_conflicting_with_its_explicit_verdict_is_uncertain(self):
        check = {"checks": [{"event_id": "s1-e1", "verdict": "supported", "line_ids": ["L00001"],
                             "reason": "Forslaget er feil. Verdict: contradicted."}]}
        result = prototype.reviews_by_key(check, "event_id", {"s1-e1"}, {"L00001": "tekst"})
        self.assertEqual(result["s1-e1"]["verdict"], "uncertain")
        self.assertEqual(result["s1-e1"]["reported_verdict"], "supported")

    def test_later_session_base_requires_grounded_model_review(self):
        answers = [("proto_base", {"preferred_date": "1971-12-07", "candidates": [
            {"date": "1971-12-07", "line_ids": ["L00003"], "reason": "Senere skriveøkt."}]}),
            ("proto_base_audit", {"checks": [{"date": "1971-12-07", "verdict": "uncertain",
                "line_ids": [], "reason": "Sporet er ikke entydig."}]})]
        session = {"id": "s2", "rows": [{"id": "L00003", "text": "Jeg skriver videre senere."}]}
        metadata = {"date": "1971-12-01", "date_is_certain": True}
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            base, errors = prototype.find_session_base(ScriptedRunner(answers), Path(tmp), session, [], metadata, [])
        self.assertEqual(errors, [])
        self.assertEqual(base["status"], "unresolved")
        self.assertIsNone(base["date"])
        self.assertEqual(base["candidates"][0]["date"], "1971-12-07")

    def test_new_session_later_than_certain_date_rejects_same_day_and_retries(self):
        previous = [{"id": "s1", "base_date": {"date": "1971-12-01", "status": "explicit"}}]
        answers = [
            ("proto_relation", {"relation": "later", "line_ids": ["L00003"], "reason": "Flere dager har gått."}),
            ("proto_base", {"preferred_date": "1971-12-01", "candidates": [
                {"date": "1971-12-01", "line_ids": ["L00003"], "reason": "Metadata."}]}),
            ("proto_base", {"preferred_date": "1971-12-07", "candidates": [
                {"date": "1971-12-07", "line_ids": ["L00003"], "reason": "Seks dager senere."}]}),
            ("proto_base_audit", {"checks": [{"date": "1971-12-07", "verdict": "supported",
                "line_ids": ["L00003"], "reason": "Passer kilden."}]})]
        session = {"id": "s2", "rows": [{"id": "L00003", "text": "Jeg skriver videre flere dager senere."}]}
        metadata = {"date": "1971-12-01", "date_is_certain": True, "frontmatter_date": "1971-12-01"}
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            base, errors = prototype.find_session_base(ScriptedRunner(answers), Path(tmp), session, previous, metadata, [])
        self.assertEqual(errors, [])
        self.assertEqual(base["date"], "1971-12-07")
        self.assertEqual(base["status"], "hypothesis")
        self.assertTrue(base["candidates"][0]["python_checks"])
        self.assertEqual(base["relation_to_previous"]["quotes"], ["Jeg skriver videre flere dager senere."])

    def test_base_favorite_is_not_selected_when_another_candidate_is_open(self):
        previous = [{"id": "s1", "base_date": {"date": "1971-12-01", "status": "explicit"}}]
        answers = [("proto_relation", {"relation": "later", "line_ids": ["L00003"], "reason": "Senere."}),
            ("proto_base", {"preferred_date": "1971-12-08", "candidates": [
                {"date": "1971-12-08", "line_ids": ["L00003"], "reason": "Forslag."},
                {"date": "1971-12-07", "line_ids": ["L00003"], "reason": "Alternativ."}]}),
            ("proto_base_audit", {"checks": [
                {"date": "1971-12-08", "verdict": "supported", "line_ids": ["L00003"], "reason": "Mulig."},
                {"date": "1971-12-07", "verdict": "uncertain", "line_ids": [], "reason": "Også mulig."}]})]
        session = {"id": "s2", "rows": [{"id": "L00003", "text": "Jeg skriver videre senere."}]}
        metadata = {"date": "1971-12-01", "date_is_certain": True}
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            base, _ = prototype.find_session_base(ScriptedRunner(answers), Path(tmp), session, previous, metadata, [])
        self.assertIsNone(base["date"])
        self.assertEqual(base["status"], "unresolved")
        self.assertEqual(len(base["candidates"]), 2)

    def test_split_time_expression_repairs_only_exact_adjacent_source(self):
        rows = [{"id": "L00001", "text": "Mannen skulle reise dagen"},
                {"id": "L00002", "text": "etter fødselen."}]
        window = {"rows": rows, "core_start": "L00001", "core_end": "L00002"}
        item = {"title": "Reise", "line_ids": ["L00001"], "date_line_ids": ["L00001"],
                "time_expression": "dagen etter", "suggested_date": "1971-12-05", "alternatives": [],
                "reason": "Dagen etter fødselen.", "occurrence": "planned", "people": [],
                "location": None, "category": "Reise"}
        events, _, _ = prototype.prepare_events([item], "s1", 1, window, {})
        self.assertEqual(events[0]["date_line_ids"], ["L00001", "L00002"])
        self.assertTrue(events[0]["python_repairs"])
        self.assertEqual(events[0]["python_checks"], [])

    def test_unresolved_session_date_does_not_promote_relative_event_date(self):
        answers = [("scan", {"new_sessions": []}),
            ("proto_base", {"preferred_date": None, "candidates": []}),
            ("proto_events", {"events": [proposal()]}),
            ("proto_event_audit", {"checks": [{"event_id": "s1-w01-e1", "verdict": "supported",
                "line_ids": ["L00003"], "reason": "Søndag 19."}], "missing_events": []})]
        runner = ScriptedRunner(answers)
        runner.args.date_uncertain = True
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "01.txt"
            letter.write_text("---\nDate: 1971-12-07\n---\nPerson A skal reise.\nMer tekst.\nHan reiser søndag 19.\n", encoding="utf-8")
            summary = prototype.process_letter(runner, letter, input_dir, output_dir)
            result = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "needs_review")
        self.assertIsNone(result["events"][0]["suggested_date"])
        self.assertEqual(result["events"][0]["date_candidates"], ["1971-12-19"])

    def test_event_reviews_are_split_into_small_batches(self):
        events = [proposal(title=f"Hendelse {i}") for i in range(3)]
        answers = [("scan", {"new_sessions": []}), ("proto_events", {"events": events}),
            ("proto_event_audit", {"checks": [{"event_id": f"s1-w01-e{i}", "verdict": "supported",
                "line_ids": ["L00003"], "reason": "Stemmer."} for i in (1, 2)], "missing_events": []}),
            ("proto_event_audit", {"checks": [{"event_id": "s1-w01-e3", "verdict": "supported",
                "line_ids": ["L00003"], "reason": "Stemmer."}], "missing_events": []})]
        runner = ScriptedRunner(answers)
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "01.txt"
            letter.write_text("---\nDate: 1971-12-07\n---\nPerson A skal reise.\nMer tekst.\nHan reiser søndag 19.\n", encoding="utf-8")
            summary = prototype.process_letter(runner, letter, input_dir, output_dir)
            result = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
        self.assertEqual([event["date_basis"] for event in result["events"]], ["hypothesis"] * 3)

    def test_ambiguous_model_date_stays_unresolved(self):
        answers = [("scan", {"new_sessions": []}), ("proto_events", {"events": [proposal()]}),
            ("proto_event_audit", {"checks": [{"event_id": "s1-w01-e1", "verdict": "uncertain",
                                               "line_ids": [], "reason": "Året er ikke entydig."}]})]
        runner = ScriptedRunner(answers)
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "01.txt"
            letter.write_text("---\nDate: 1971-12-07\n---\nPerson A skal reise.\nMer tekst.\nHan reiser søndag 19.\n", encoding="utf-8")
            summary = prototype.process_letter(runner, letter, input_dir, output_dir)
            result = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
        event = result["events"][0]
        self.assertEqual(event["date_basis"], "unresolved")
        self.assertIsNone(event["suggested_date"])
        self.assertEqual(event["date_candidates"], ["1971-12-19"])

    def test_supported_review_does_not_hide_multiple_date_candidates(self):
        item = proposal()
        item["alternatives"] = ["1972-03-19"]
        answers = [("scan", {"new_sessions": []}), ("proto_events", {"events": [item]}),
            ("proto_event_audit", {"checks": [{"event_id": "s1-w01-e1", "verdict": "supported",
                "line_ids": ["L00003"], "reason": "Mulig, men ikke eneste dato."}], "missing_events": []})]
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "01.txt"
            letter.write_text("---\nDate: 1971-12-07\n---\nPerson A skal reise.\nMer tekst.\nHan reiser søndag 19.\n", encoding="utf-8")
            summary = prototype.process_letter(ScriptedRunner(answers), letter, input_dir, output_dir)
            result = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
        event = result["events"][0]
        self.assertIsNone(event["suggested_date"])
        self.assertEqual(event["date_basis"], "unresolved")
        self.assertEqual(event["date_candidates"], ["1971-12-19", "1972-03-19"])

    def test_compare_checks_source_and_does_not_claim_accuracy(self):
        data = {"source_sha256": "a", "events": [{"title": "Test", "date_basis": "hypothesis"}],
                "validation": {"model": "one"}}
        result = prototype.compare_summary(data, {**data, "source_sha256": "b"})
        self.assertFalse(result["source_sha256_match"])
        self.assertIn("ikke en kvalitetsmåling", result["note"])

    def test_audit_can_flag_event_missing_from_an_empty_first_pass(self):
        answers = [("scan", {"new_sessions": []}), ("proto_events", {"events": []}),
            ("proto_event_audit", {"checks": [], "missing_events": [
                {"line_ids": ["L00001"], "reason": "Besøket mangler i hendelseslisten."}]})]
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "01.txt"
            letter.write_text("---\nDate: 1971-12-07\n---\nVi besøkte Person C.\n", encoding="utf-8")
            summary = prototype.process_letter(ScriptedRunner(answers), letter, input_dir, output_dir)
            result = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
        self.assertEqual(result["events"], [])
        self.assertEqual(result["possible_missing_events"][0]["quotes"], ["Vi besøkte Person C."])

    def test_relative_day_uses_checked_anchor_from_previous_window(self):
        anchor = {"title": "Sankthansfeiring", "line_ids": ["L00001"],
            "date_line_ids": ["L00001"], "time_expression": "St. Hans kvelden",
            "suggested_date": "1966-06-23", "alternatives": [], "reason": "Sankthanskvelden.",
            "occurrence": "happened", "people": [], "location": None, "category": "Familie"}
        relative = {"title": "Besøk i Place A", "line_ids": ["L00003"],
            "date_line_ids": ["L00003"], "time_expression": "dagen før",
            "suggested_date": "1966-06-23", "alternatives": [], "reason": "Modellens feilaktige forslag.",
            "anchor_event_id": "s1-w01-e1", "occurrence": "happened", "people": [],
            "location": None, "category": "Reise"}
        answers = [("scan", {"new_sessions": []}), ("proto_events", {"events": [anchor]}),
            ("proto_event_audit", {"checks": [{"event_id": "s1-w01-e1", "verdict": "supported",
                "line_ids": ["L00001"], "reason": "Kilden angir sankthanskvelden."}], "missing_events": []}),
            ("proto_events", {"events": [relative]}),
            ("proto_event_audit", {"checks": [{"event_id": "s1-w02-e1", "verdict": "supported",
                "line_ids": ["L00001", "L00003"], "reason": "Dagen før feiringen."}], "missing_events": []})]
        runner = ScriptedRunner(answers)
        runner.args.event_chunk_lines = 2
        runner.args.event_overlap_lines = 0
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "03.txt"
            letter.write_text("---\nDate: 1966-07-03\n---\nSt. Hans kvelden feiret vi.\nDet var fest.\nDe var i Place A dagen før.\nDet var hyggelig.\n", encoding="utf-8")
            summary = prototype.process_letter(runner, letter, input_dir, output_dir)
            result = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
        event = result["events"][1]
        self.assertEqual(event["suggested_date"], "1966-06-22")
        self.assertEqual(event["proposed_date"], "1966-06-22")
        self.assertEqual(event["model_proposed_date"], "1966-06-23")
        self.assertEqual(event["model_date_candidates"], ["1966-06-23"])
        self.assertEqual(event["temporal_anchor"]["anchor_event_id"], "s1-w01-e1")
        self.assertEqual(event["temporal_anchor"]["line_ids"], ["L00001"])
        self.assertEqual(event["audit"]["verdict"], "supported")

    def test_relative_day_without_checked_anchor_cannot_be_promoted(self):
        event = {"time_expression": "dagen etter", "date_quotes": ["Alt dagen etter"],
            "quotes": ["Alt dagen etter reiste vi."], "anchor_event_id": "unknown",
            "date_candidates": ["1966-07-03"], "python_checks": []}
        prototype.resolve_relative_anchor(event, [])
        self.assertEqual(event["temporal_anchor"]["status"], "unresolved")
        self.assertTrue(event["python_checks"])
        self.assertEqual(event["date_candidates"], ["1966-07-03"])

    def test_supported_audit_cannot_promote_relative_day_without_anchor(self):
        item = {"title": "Place C-tur", "line_ids": ["L00001"], "date_line_ids": ["L00001"],
            "time_expression": "dagen etter", "suggested_date": "1966-07-03", "alternatives": [],
            "reason": "Modellens antakelse.", "occurrence": "happened", "people": [],
            "location": None, "category": "Reise"}
        answers = [("scan", {"new_sessions": []}), ("proto_events", {"events": [item]}),
            ("proto_event_audit", {"checks": [{"event_id": "s1-w01-e1", "verdict": "supported",
                "line_ids": ["L00001"], "reason": "Modellen godkjente datoen."}], "missing_events": []})]
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "03.txt"
            letter.write_text("---\nDate: 1966-07-03\n---\nDagen etter reiste vi.\n", encoding="utf-8")
            summary = prototype.process_letter(ScriptedRunner(answers), letter, input_dir, output_dir)
            result = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "needs_review")
        self.assertIsNone(result["events"][0]["suggested_date"])
        self.assertEqual(result["events"][0]["temporal_anchor"]["status"], "unresolved")

    def test_day_after_uses_anchor_event_date_not_letter_date(self):
        anchor = {"id": "s1-w03-e4", "date_basis": "hypothesis", "suggested_date": "1966-06-25",
            "title": "Vitnemål", "line_ids": ["L00110"], "quotes": ["Person C fikk vitnemål"],
            "date_line_ids": ["L00108"], "date_quotes": ["lørdag morgen"]}
        event = {"time_expression": "dagen etter", "date_quotes": ["Alt dagen etter"],
            "quotes": ["Alt dagen etter kjørte vi til Place C."], "anchor_event_id": "s1-w03-e4",
            "date_candidates": ["1966-07-03"], "python_checks": [], "date_reason": "Modellens forslag."}
        prototype.resolve_relative_anchor(event, [anchor])
        self.assertEqual(event["temporal_anchor"]["calculated_date"], "1966-06-26")
        self.assertEqual(event["date_candidates"], ["1966-06-26"])
        self.assertEqual(event["model_date_candidates"], ["1966-07-03"])

    def test_unresolved_anchor_candidates_are_calculated_without_promotion(self):
        anchor = {"id": "s1-w03-e5", "date_basis": "unresolved", "suggested_date": None,
            "date_candidates": ["1966-06-24", "1966-06-25"], "title": "Vitnemål",
            "line_ids": ["L00110"], "quotes": ["Person C fikk vitnemål"],
            "date_line_ids": ["L00108"], "date_quotes": ["lørdag morgen"]}
        event = {"time_expression": "dagen etter", "date_quotes": ["Alt dagen etter"],
            "quotes": ["Alt dagen etter kjørte vi til Place C."], "anchor_event_id": "s1-w03-e5",
            "date_candidates": ["1966-07-03"], "python_checks": [], "date_reason": "Modellens forslag."}
        prototype.resolve_relative_anchor(event, [anchor])
        self.assertEqual(event["date_candidates"], ["1966-06-25", "1966-06-26"])
        self.assertEqual(event["model_date_candidates"], ["1966-07-03"])
        self.assertEqual(event["temporal_anchor"]["status"], "unresolved")
        self.assertEqual(event["temporal_anchor"]["candidate_dependencies"][1],
                         {"anchor_date": "1966-06-25", "date": "1966-06-26"})
        self.assertTrue(event["python_checks"])

    def test_past_weekday_is_calculated_before_letter_date_and_model_guess_is_retained(self):
        event = {"time_expression": "Nå onsdag", "date_quotes": ["Nå onsdag var vi på Fredtun."],
            "occurrence": "happened", "letter_date_relation": "after",
            "model_letter_date_relation": "after", "proposed_date": "1966-07-06",
            "date_candidates": ["1966-06-29", "1966-07-06"],
            "model_proposed_date": "1966-07-06", "model_date_candidates": ["1966-06-29", "1966-07-06"],
            "date_reason": "Modellens forslag.", "python_checks": [], "python_repairs": []}
        prototype.constrain_to_letter_date(event, {"date": "1966-07-03"})
        self.assertEqual(event["proposed_date"], "1966-06-29")
        self.assertEqual(event["date_candidates"], ["1966-06-29"])
        self.assertEqual(event["model_proposed_date"], "1966-07-06")
        self.assertEqual(event["model_date_candidates"], ["1966-06-29", "1966-07-06"])
        self.assertEqual(event["letter_date_relation"], "before")
        self.assertEqual(event["model_letter_date_relation"], "after")
        self.assertEqual(event["python_checks"], [])
        self.assertGreaterEqual(len(event["python_repairs"]), 2)

    def test_happened_event_without_a_past_candidate_stays_unresolved(self):
        event = {"time_expression": "den dagen", "date_quotes": ["Person D reiste den dagen."],
            "occurrence": "happened", "letter_date_relation": "after",
            "proposed_date": "1966-07-05", "date_candidates": ["1966-07-05"],
            "model_proposed_date": "1966-07-05", "python_checks": [], "python_repairs": []}
        prototype.constrain_to_letter_date(event, {"date": "1966-07-03"})
        self.assertIsNone(event["proposed_date"])
        self.assertEqual(event["date_candidates"], [])
        self.assertEqual(event["letter_date_relation"], "before")
        self.assertTrue(event["python_checks"])

    def test_wrong_weekday_candidate_falls_back_to_nearest_date_in_direction(self):
        event = {"time_expression": "Nå onsdag", "date_quotes": ["Nå onsdag var vi på Fredtun."],
            "occurrence": "happened", "letter_date_relation": "before",
            "proposed_date": "1966-07-06", "date_candidates": ["1966-07-06"],
            "model_proposed_date": "1966-07-06", "model_date_candidates": ["1966-07-06"],
            "date_reason": "Modellens forslag.", "python_checks": [], "python_repairs": []}
        prototype.constrain_to_letter_date(event, {"date": "1966-07-03"})
        self.assertEqual(event["proposed_date"], "1966-06-29")
        self.assertEqual(event["date_candidates"], ["1966-06-29"])
        self.assertEqual(event["model_proposed_date"], "1966-07-06")
        self.assertEqual(event["model_date_candidates"], ["1966-07-06"])
        self.assertIn("1966-07-06", event["python_repairs"][0])
        self.assertEqual(event["python_checks"], [])

    def test_past_weekday_preserves_valid_historical_candidate_instead_of_nearest_weekday(self):
        event = {"time_expression": "lørdag morgen", "date_quotes": ["reiste lørdag morgen"],
            "occurrence": "happened", "letter_date_relation": "before",
            "proposed_date": "1966-06-25", "model_proposed_date": "1966-06-25",
            "date_candidates": ["1966-06-18", "1966-06-25"],
            "model_date_candidates": ["1966-06-18", "1966-06-25"],
            "python_checks": [], "python_repairs": [], "date_reason": "Datoer fra modellen."}
        prototype.constrain_to_letter_date(event, {"date": "1966-07-03"})
        self.assertEqual(event["date_candidates"], ["1966-06-18", "1966-06-25"])
        self.assertEqual(event["proposed_date"], "1966-06-25")

    def test_chronology_link_filters_candidates_against_prior_event(self):
        anchor = {"id": "s1-w01-e1", "date_basis": "hypothesis", "suggested_date": "1966-06-25"}
        event = {"chronology_anchor_event_id": "s1-w01-e1", "chronology_relation": "after",
            "date_candidates": ["1966-06-24", "1966-06-26"], "proposed_date": "1966-06-24",
            "python_checks": [], "python_repairs": []}
        prototype.constrain_to_chronology(event, [anchor])
        self.assertEqual(event["date_candidates"], ["1966-06-26"])
        self.assertEqual(event["proposed_date"], "1966-06-26")
        self.assertEqual(event["model_date_candidates"], ["1966-06-24", "1966-06-26"])

    def test_event_prompt_keeps_separate_actions_and_requests_time_direction(self):
        instructions = prototype.STAGE_INSTRUCTIONS["proto_events"]
        self.assertIn("letter_date_relation", instructions)
        self.assertIn("Hold selvstendige planer", instructions)

    def test_event_extraction_merges_are_explicitly_discouraged(self):
        self.assertIn("Ikke del én reiseplan opp",
                      prototype.STAGE_INSTRUCTIONS["proto_events"])

    def test_missing_anchor_id_gets_grounded_separate_model_call(self):
        anchor = {"title": "Sankthansfeiring", "line_ids": ["L00001"],
            "date_line_ids": ["L00001"], "time_expression": "St. Hans kvelden",
            "suggested_date": "1966-06-23", "alternatives": [], "reason": "Sankthanskvelden.",
            "occurrence": "happened", "people": [], "location": None, "category": "Familie"}
        relative = {"title": "Besøk i Place A", "line_ids": ["L00003"],
            "date_line_ids": ["L00003"], "time_expression": "dagen før",
            "suggested_date": "1966-06-23", "alternatives": [], "reason": "Kvelden før feiringen.",
            "occurrence": "happened", "people": [], "location": None, "category": "Reise"}
        answers = [("scan", {"new_sessions": []}), ("proto_events", {"events": [anchor]}),
            ("proto_event_audit", {"checks": [{"event_id": "s1-w01-e1", "verdict": "supported",
                "line_ids": ["L00001"], "reason": "Sankthanskvelden."}], "missing_events": []}),
            ("proto_events", {"events": [relative]}),
            ("proto_anchor_link", {"anchor_event_id": "s1-w01-e1", "line_ids": ["L00001", "L00003"],
                "reason": "Besøket var dagen før feiringen."}),
            ("proto_event_audit", {"checks": [{"event_id": "s1-w02-e1", "verdict": "supported",
                "line_ids": ["L00001", "L00003"], "reason": "Dagen før feiringen."}], "missing_events": []})]
        runner = ScriptedRunner(answers)
        runner.args.event_chunk_lines = 2
        runner.args.event_overlap_lines = 0
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "03.txt"
            letter.write_text("---\nDate: 1966-07-03\n---\nSt. Hans kvelden feiret vi.\nDet var fest.\nDe var i Place A dagen før.\nDet var hyggelig.\n", encoding="utf-8")
            summary = prototype.process_letter(runner, letter, input_dir, output_dir)
            result = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
        event = result["events"][1]
        self.assertEqual(event["anchor_link"]["anchor_event_id"], "s1-w01-e1")
        self.assertEqual(event["anchor_link"]["line_ids"], ["L00001", "L00003"])
        self.assertEqual(event["suggested_date"], "1966-06-22")

    def test_anchor_link_rejects_unknown_id_or_missing_anchor_evidence(self):
        event = {"line_ids": ["L00003"], "date_line_ids": ["L00003"]}
        prior = [{"id": "s1-e1", "line_ids": ["L00001"], "date_line_ids": ["L00001"]}]
        lines = {"L00001": "Sankthansaften", "L00003": "dagen før"}
        self.assertTrue(prototype.validate_anchor_link({"anchor_event_id": "bad", "line_ids": ["L00003"],
            "reason": "Forslag."}, event, prior, lines))
        self.assertTrue(prototype.validate_anchor_link({"anchor_event_id": "s1-e1", "line_ids": ["L00003"],
            "reason": "Forslag."}, event, prior, lines))

    def test_weekday_is_checked_without_day_number(self):
        self.assertIsNotNone(prototype.weekday_conflict("1966-07-03", "Nå onsdag"))
        self.assertIsNone(prototype.weekday_conflict("1966-06-29", "Nå onsdag"))


if __name__ == "__main__":
    unittest.main()
