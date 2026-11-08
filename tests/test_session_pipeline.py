import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from letter_pipeline import schema_v2
from letter_pipeline import prompts_v2
from letter_pipeline.session_pipeline import (
    Runner, attach_event_date, candidate_chronology, canonical, contradicts_elapsed_week, digest, grounded_audit_verdict, has_specific_base_evidence, index_source, parse_source_date, process_letter,
    discard_context_only_events, event_windows, ground_optional_event_fields, possible_duplicate_warnings,
    infer_nearly_week_sunday_correction, resolve_references, schema_errors,
    source_quotes, uncovered_time_cues, validate_audit, validate_events, validate_references,
)


def model_answer(value, reason="stop"):
    return {"content": json.dumps(value, ensure_ascii=False), "done_reason": reason,
            "generated_tokens": 100, "prompt_tokens": 50, "seconds": 0.01}


def fake_runner(answers, retries=1):
    runner = Runner.__new__(Runner)
    runner.args = SimpleNamespace(retry_failed=False, max_repairs=retries, tokens_scan=1000,
        tokens_extract=2000, tokens_base=500, tokens_audit=500, tokens_events=2000,
        event_chunk_lines=80, event_overlap_lines=8, date_uncertain=False, no_json_mode=False,
        calendar_before_days=45, calendar_after_days=90)
    runner.model = "fake-local"
    runner.profile = {"model": runner.model, "version": "test"}
    runner.identity = runner.profile
    runner.profile_hash = digest(canonical(runner.identity).encode())[:10]
    runner.completion = Mock(side_effect=answers)
    return runner


class SourceAndDateTests(unittest.TestCase):
    def test_event_requests_send_json_schema_to_ollama(self):
        runner = fake_runner([])
        runner.args.base_url, runner.args.context = "http://ollama:11434", 32768
        runner.args.temperature, runner.args.timeout, runner.args.no_json_mode = 0.2, 90, False
        runner.think = False
        response = {"message": {"content": '{"events": []}'}, "done_reason": "stop"}
        with patch("letter_pipeline.session_pipeline.request_json", return_value=response) as request:
            Runner.completion(runner, "prompt", 100, "events")
        payload = request.call_args.args[2]
        self.assertEqual(payload["format"]["properties"], schema_v2.EVENTS["properties"])
        self.assertNotIn("$schema", payload["format"])

    def test_event_citations_are_bounded_to_one_compact_passage(self):
        event = {"title": "Besøk", "line_ids": [f"L{i:05d}" for i in range(1, 18)],
                 "time_reference_ids": [], "occurrence": "happened", "people": [],
                 "location": None, "category": "Familie"}
        self.assertTrue(schema_errors({"events": [event]}, schema_v2.EVENTS))
        prompt = prompts_v2.build("events", {"core_start": "L00001", "core_end": "L00040", "lines": []})
        self.assertIn("maks 16 linjer", prompt)
        self.assertIn("Ikke ta med flere separate avsnitt", prompt)

    def test_source_quotes_use_exact_selected_lines(self):
        lines = {"L00001": "første linje", "L00002": "andre linje", "L00003": "tredje linje"}
        self.assertEqual(source_quotes(["L00003", "L00001"], lines), ["første linje", "tredje linje"])
        with self.assertRaises(ValueError):
            source_quotes(["L00001", "L00003"], lines, contiguous=True)
        with self.assertRaises(ValueError):
            source_quotes(["L99999"], lines)

    def test_page_breaks_are_preserved_inside_source_lines(self):
        rows = index_source("første\r\n\fandre\r\ntredje")
        lines = {row["id"]: row["text"] for row in rows}
        self.assertEqual(source_quotes(["L00002"], lines), ["\fandre"])

    def test_yearless_date_remains_a_hypothesis(self):
        self.assertEqual(parse_source_date("den 14/12", "1975-12-21", "explicit"),
                         ("1975-12-14", "hypothesis"))
        self.assertEqual(parse_source_date("den 14/12/1975", "1975-12-21", "explicit"),
                         ("1975-12-14", "explicit"))

    def test_correction_and_vague_duration_are_not_given_precise_dates(self):
        base = {"date": "1971-12-07", "status": "hypothesis"}
        refs = [
            {"id": "s1-r1", "quote": "Igår, nei søndag skrev jeg.", "kind": "previous_day", "anchor_id": None},
            {"id": "s1-r2", "quote": "Jeg har vært syk over ei uke.", "kind": "other", "anchor_id": None},
        ]
        results = resolve_references(refs, base)
        self.assertEqual(results[0]["date_basis"], "hypothesis")
        self.assertEqual(results[0]["suggested_date"], "1971-12-05")
        self.assertEqual(results[1]["date_basis"], "unresolved")
        self.assertTrue(all(result["date"] is None for result in results))

    def test_corrected_yesterday_to_sunday_resolves_previous_sunday(self):
        base = {"date": "1971-12-07", "status": "hypothesis", "alternatives": []}
        refs = [{"id": "s2-r1", "quote": "Igår, nei søndag skrev jeg til Madagascar.",
                 "kind": "previous_day", "anchor_id": None}]
        result = resolve_references(refs, base)[0]
        self.assertEqual(result["date_basis"], "hypothesis")
        self.assertEqual(result["suggested_date"], "1971-12-05")

    def test_previous_weekday_is_not_automatically_nearest(self):
        base = {"date": "1971-12-08", "status": "explicit"}
        refs = [{"id": "s1-r1", "quote": "sist tirsdag", "kind": "previous_weekday", "anchor_id": None}]
        result = resolve_references(refs, base)[0]
        self.assertEqual(result["date_basis"], "unresolved")
        self.assertIsNone(result["date"])

    def test_vague_elapsed_week_does_not_select_one_session_date(self):
        self.assertFalse(has_specific_base_evidence(["Nå har det gått\nnesten ei uke."]))
        self.assertFalse(has_specific_base_evidence(["Det er nesten 7 dager siden."]))
        self.assertTrue(has_specific_base_evidence(["Tirsdag skriver jeg igjen."]))
        self.assertTrue(contradicts_elapsed_week("1971-12-01", "1971-12-01", [{"text": "Nå har det gått nesten ei uke."}]))
        self.assertEqual(candidate_chronology("1971-12-01", "1971-12-01", [{"text": "Nå har det gått nesten ei uke."}]), "conflict")

    def test_nearly_week_and_sunday_correction_narrow_session_date(self):
        rows = index_source("Nå har det gått\nnesten ei uke, og her ser du at brevet\n"
                            "Igår, nei søndag skrev jeg til Madagascar.")
        inferred = infer_nearly_week_sunday_correction(rows, "1971-12-01")
        self.assertEqual(inferred["date"], "1971-12-07")
        self.assertEqual(inferred["status"], "hypothesis")
        self.assertEqual([candidate["date"] for candidate in inferred["candidates"]],
                         ["1971-12-06", "1971-12-07"])
        rejected = inferred["candidates"][0]
        self.assertEqual(rejected["audit"]["verdict"], "contradicted")
        self.assertEqual(rejected["audit"]["against_line_ids"], ["L00002", "L00003"])
        self.assertEqual(inferred["candidates"][1]["audit"]["verdict"], "supported")

    def test_nearly_week_without_sunday_correction_stays_unresolved(self):
        rows = index_source("Nå har det gått nesten ei uke.\nJeg skrev litt mer.")
        self.assertIsNone(infer_nearly_week_sunday_correction(rows, "1971-12-01"))

    def test_sunday_correction_without_duration_stays_unresolved(self):
        rows = index_source("Igår, nei søndag skrev jeg til Madagascar.")
        self.assertIsNone(infer_nearly_week_sunday_correction(rows, "1971-12-01"))

    def test_pipeline_uses_python_inference_without_base_model_call(self):
        answers = [
            model_answer({"new_sessions": [{"start_line_id": "L00003"}]}),
            model_answer({"references": []}),
            model_answer({"events": []}),
            model_answer({"references": [{"id": "r1", "line_ids": ["L00005"],
                "kind": "previous_day", "anchor_id": None, "day_offset": None}]}),
            model_answer({"events": []}),
        ]
        runner = fake_runner(answers)
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "01.txt"
            letter.write_text("---\nDate: 1971-12-01\n---\nFørste del.\nSlutt.\n"
                              "Nå har det gått\nnesten ei uke, og her ser du\n"
                              "Igår, nei søndag skrev jeg.\n", encoding="utf-8")
            summary = process_letter(runner, letter, input_dir, output_dir)
            final = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
        self.assertEqual(final["sessions"][1]["base_date"]["date"], "1971-12-07")
        self.assertEqual(final["sessions"][1]["base_date"]["status"], "hypothesis")
        self.assertEqual(final["time_references"][0]["suggested_date"], "1971-12-05")
        self.assertEqual(runner.completion.call_count, 5)

    def test_unsupported_audit_verdict_is_not_promoted(self):
        item = {"date": "1971-12-07", "verdict": "supported", "support_line_ids": [], "against_line_ids": []}
        self.assertEqual(validate_audit({"checks": [item]}, {"L00001": "Tirsdag."}, ["1971-12-07"]), [])
        self.assertEqual(grounded_audit_verdict(item), "undetermined")

    def test_relative_reference_inherits_uncertainty(self):
        base = {"date": "1975-12-21", "status": "explicit"}
        refs = [
            {"id": "s1-r1", "quote": "den 14/12", "kind": "explicit_date", "anchor_id": None, "day_offset": None},
            {"id": "s1-r2", "quote": "Dagen før var vi på besøk.", "kind": "relative_reference", "anchor_id": "s1-r1", "day_offset": -1},
        ]
        results = resolve_references(refs, base)
        self.assertEqual(results[0]["suggested_date"], "1975-12-14")
        self.assertEqual(results[1]["suggested_date"], "1975-12-13")
        self.assertEqual(results[1]["date_basis"], "hypothesis")
        event = attach_event_date({"time_reference_ids": ["s1-r2"]}, results)
        self.assertIsNone(event["date"])
        self.assertEqual(event["suggested_date"], "1975-12-13")

    def test_relative_reference_can_use_a_previous_session(self):
        prior = [{"id": "s1-r1", "date": None, "suggested_date": "1975-12-14",
                  "date_basis": "hypothesis", "reason": "Årstall er usikkert."}]
        current = [{"id": "s2-r1", "quote": "Dagen etter kom Person B.", "kind": "relative_reference",
                    "anchor_id": "s1-r1", "day_offset": 1}]
        result = resolve_references(current, {"date": None, "status": "unresolved"}, prior)[0]
        self.assertEqual(result["suggested_date"], "1975-12-15")
        self.assertEqual(result["date_basis"], "hypothesis")
        raw = {"references": [{"id": "r1", "line_ids": ["L00001"], "kind": "relative_reference",
                               "anchor_id": "s1-r1", "day_offset": 1}]}
        self.assertEqual(validate_references(raw, {"L00001": "Dagen etter kom Person B."}, {"s1-r1"}), [])

    def test_alternative_session_dates_propagate_without_becoming_facts(self):
        base = {"date": "1971-12-07", "status": "hypothesis", "alternatives": ["1971-12-06"]}
        refs = [{"id": "s1-r1", "quote": "I går kom Person B.", "kind": "previous_day", "anchor_id": None}]
        resolved = resolve_references(refs, base)
        event = attach_event_date({"time_reference_ids": ["s1-r1"]}, resolved)
        self.assertEqual(event["suggested_date"], "1971-12-06")
        self.assertEqual(event["date_candidates"], ["1971-12-05", "1971-12-06"])
        self.assertIsNone(event["date"])
        base.update({"date": None, "status": "unresolved", "alternatives": ["1971-12-06", "1971-12-07"]})
        unresolved = attach_event_date({"time_reference_ids": ["s1-r1"]}, resolve_references(refs, base))
        self.assertEqual(unresolved["date_basis"], "unresolved")
        self.assertEqual(unresolved["date_candidates"], ["1971-12-05", "1971-12-06"])
        self.assertIsNone(unresolved["suggested_date"])

    def test_unknown_line_and_cycle_are_rejected(self):
        refs = {"references": [
            {"id": "r1", "line_ids": ["L99999"], "kind": "relative_reference", "anchor_id": "r2", "day_offset": -1},
            {"id": "r2", "line_ids": ["L00001"], "kind": "relative_reference", "anchor_id": "r1", "day_offset": 1},
        ]}
        errors = validate_references(refs, {"L00001": "Dagen etter."})
        self.assertTrue(any("Ukjente" in error for error in errors))
        self.assertTrue(any("syklus" in error for error in errors))

    def test_uncovered_vague_duration_is_flagged(self):
        rows = [{"id": "L00001", "text": "I går kom Person B."},
                {"id": "L00002", "text": "Jeg var syk over ei uke."}]
        refs = [{"line_ids": ["L00001"]}]
        self.assertEqual(uncovered_time_cues(rows, refs), ["L00002"])

    def test_event_people_and_places_must_be_in_cited_lines(self):
        lines = {"L00001": "Person B kom til Place B."}
        event = {"line_ids": ["L00001"], "time_reference_ids": [], "people": ["Person C"], "location": "Place C"}
        errors = validate_events({"events": [event]}, lines, set())
        self.assertTrue(any("personomtalen" in error for error in errors))
        self.assertTrue(any("stedet" in error for error in errors))
        event.update({"people": ["Person B"], "location": "Place B"})
        self.assertEqual(validate_events({"events": [event]}, lines, set()), [])

    def test_event_evidence_comparison_accepts_ocr_line_end_hyphenation(self):
        lines = {"L00001": "Hun lå på 8-", "L00002": "manns rom ved siden av råd-", 
                 "L00003": "mannen."}
        event = {"line_ids": ["L00001", "L00002", "L00003"], "time_reference_ids": [],
                 "people": ["råd-mannen"], "location": "8-manns rom"}
        self.assertEqual(validate_events({"events": [event]}, lines, set()), [])

    def test_event_windows_overlap_but_have_distinct_core_ownership(self):
        rows = [{"id": f"L{i:05d}", "text": f"Linje {i}"} for i in range(1, 8)]
        windows = event_windows(rows, 3, 1)
        self.assertEqual([(w["core_start"], w["core_end"]) for w in windows],
                         [("L00001", "L00003"), ("L00004", "L00006"), ("L00007", "L00007")])
        window_map = {row["id"]: row["text"] for row in windows[1]["rows"]}
        event = {"line_ids": ["L00003"], "time_reference_ids": [], "people": [], "location": None}
        self.assertTrue(validate_events({"events": [event]}, window_map, set(), "L00004", "L00006"))
        event["line_ids"] = ["L00004"]
        self.assertEqual(validate_events({"events": [event]}, window_map, set(), "L00004", "L00006"), [])
        event["line_ids"] = ["L00003", "L00004"]
        self.assertEqual(validate_events({"events": [event]}, window_map, set(), "L00004", "L00006", "L00003"), [])

    def test_context_only_events_are_left_to_the_adjacent_core_window(self):
        value = {"events": [{"line_ids": ["L00003"]}, {"line_ids": ["L00004"]}]}
        self.assertEqual(discard_context_only_events(value, "L00004", "L00006"), 1)
        self.assertEqual(value["events"], [{"line_ids": ["L00004"]}])

    def test_similar_citations_across_windows_trigger_duplicate_warning(self):
        events = [
            {"id": "s1-e1", "line_ids": ["L00001", "L00002", "L00003", "L00004", "L00005"],
             "time_reference_ids": [], "title": "Samling"},
            {"id": "s1-e2", "line_ids": ["L00002", "L00003", "L00004", "L00005", "L00006"],
             "time_reference_ids": [], "title": "Kaffe"},
        ]
        self.assertEqual(len(possible_duplicate_warnings("s1", events)), 1)

    def test_unsupported_optional_event_fields_are_omitted_with_review_note(self):
        value = {"events": [{"line_ids": ["L00001", "L00002"], "people": ["Person E", "Person F"],
                             "location": "Madagascar"}]}
        line_map = {"L00001": "Igår skrev jeg til Madagaskar,", "L00002": "Person F svarte senere."}
        notes = ground_optional_event_fields(value, line_map)
        self.assertEqual(value["events"][0]["people"], ["Person F"])
        self.assertIsNone(value["events"][0]["location"])
        self.assertIn("Person E", value["events"][0]["_evidence_review_note"])
        self.assertIn("Madagascar", value["events"][0]["_evidence_review_note"])
        self.assertEqual(len(notes), 1)


class WorkflowTests(unittest.TestCase):
    def test_end_to_end_sessions_dates_and_resume(self):
        answers = [
            model_answer({"new_sessions": [{"start_line_id": "L00003"}]}),
            model_answer({"references": [{"id": "r1", "line_ids": ["L00002"], "kind": "session_day", "anchor_id": None, "day_offset": None}]}),
            model_answer({"events": [{"title": "Telefon", "line_ids": ["L00002"], "time_reference_ids": ["s1-r1"], "occurrence": "happened", "people": ["Person B"], "location": None, "category": "Familie"}]}),
            model_answer({"references": [{"id": "r1", "line_ids": ["L00004"], "kind": "previous_day", "anchor_id": None, "day_offset": None}]}),
            model_answer({"preferred_date": "1971-12-07", "candidates": [
                {"date": "1971-12-07", "support_line_ids": ["L00003"], "against_line_ids": []},
                {"date": "1971-12-06", "support_line_ids": ["L00003"], "against_line_ids": []}]}),
            model_answer({"checks": [
                {"date": "1971-12-07", "verdict": "supported", "support_line_ids": ["L00003"], "against_line_ids": []},
                {"date": "1971-12-06", "verdict": "undetermined", "support_line_ids": [], "against_line_ids": []}]}),
            model_answer({"events": [{"title": "Besøk", "line_ids": ["L00004"], "time_reference_ids": ["s2-r1"], "occurrence": "happened", "people": [], "location": None, "category": "Familie"}]}),
        ]
        runner = fake_runner(answers)
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "01.txt"
            letter.write_text("---\nDate: 1971-12-01\nFrom: Person G\n---\nPlace B 1-12-71\nI dag ringte Person B.\nTirsdag skriver jeg igjen.\nI går besøkte jeg Person C.\n", encoding="utf-8")
            summary = process_letter(runner, letter, input_dir, output_dir)
            self.assertEqual(summary["status"], "complete")
            final = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
            self.assertEqual(schema_errors(final, schema_v2.FINAL), [])
            self.assertEqual(len(final["sessions"]), 2)
            self.assertEqual(final["events"][0]["date"], "1971-12-01")
            self.assertEqual(final["events"][0]["people"], [{"mention": "Person B", "resolved_name": None}])
            self.assertIn("1971-12-01", final["events"][0]["date_reason"])
            self.assertEqual(final["events"][0]["time_expression"], final["time_references"][0]["quote"])
            lines = json.loads(next(output_dir.rglob("source-lines.json")).read_text(encoding="utf-8"))
            self.assertEqual(lines[1], {"id": "L00002", "text": "I dag ringte Person B."})
            self.assertEqual(final["sessions"][1]["base_date"]["status"], "hypothesis")
            self.assertEqual(final["sessions"][1]["base_date"]["candidates"][0]["weekday"], "tirsdag")
            self.assertIsNone(final["events"][1]["date"])
            self.assertEqual(final["events"][1]["suggested_date"], "1971-12-06")
            self.assertEqual(final["events"][1]["date_basis"], "hypothesis")
            exported = json.loads((output_dir / summary["letter_task_result"]).read_text(encoding="utf-8"))
            self.assertEqual(schema_errors(exported, schema_v2.LETTER_TASK), [])
            self.assertEqual(exported["events"][1]["date_basis"], "uncertain")
            self.assertIsNone(exported["events"][1]["date"])
            self.assertIn("1971-12-06", exported["events"][1]["date_candidates"])
            self.assertEqual(runner.completion.call_count, 7)
            audit_prompt = runner.completion.call_args_list[5].args[0]
            audit_data = json.loads(audit_prompt.rsplit("\nDATA:\n", 1)[1])
            self.assertEqual(set(audit_data["candidates"][0]), {"date", "weekday"})
            runner.completion = Mock(side_effect=AssertionError("checkpoint was not reused"))
            self.assertEqual(process_letter(runner, letter, input_dir, output_dir)["status"], "complete")
            runner.completion.assert_not_called()

    def test_contradicted_base_date_does_not_become_an_event_date(self):
        answers = [
            model_answer({"new_sessions": []}),
            model_answer({"references": [{"id": "r1", "line_ids": ["L00002"], "kind": "previous_day", "anchor_id": None, "day_offset": None}]}),
            model_answer({"preferred_date": "1971-12-07", "candidates": [
                {"date": "1971-12-07", "support_line_ids": ["L00001"], "against_line_ids": []}]}),
            model_answer({"checks": [{"date": "1971-12-07", "verdict": "contradicted",
                                      "support_line_ids": [], "against_line_ids": ["L00001"]}]}),
            model_answer({"events": [{"title": "Besøk", "line_ids": ["L00002"], "time_reference_ids": ["s1-r1"],
                                      "occurrence": "happened", "people": [], "location": None, "category": "Familie"}]}),
        ]
        runner = fake_runner(answers)
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "01.txt"
            letter.write_text("Tirsdag skriver jeg.\nI går kom Person B.\n", encoding="utf-8")
            summary = process_letter(runner, letter, input_dir, output_dir)
            final = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
            base = final["sessions"][0]["base_date"]
            self.assertEqual(base["status"], "unresolved")
            self.assertEqual(base["candidates"][0]["audit"]["verdict"], "contradicted")
            self.assertEqual(final["events"][0]["date_basis"], "unresolved")
            self.assertIsNone(final["events"][0]["suggested_date"])
            self.assertEqual(final["events"][0]["date_candidates"], [])

    def test_long_session_events_are_processed_in_separate_windows(self):
        answers = [
            model_answer({"new_sessions": []}),
            model_answer({"references": [
                {"id": "r1", "line_ids": ["L00002"], "kind": "session_day", "anchor_id": None, "day_offset": None},
                {"id": "r2", "line_ids": ["L00004"], "kind": "next_day", "anchor_id": None, "day_offset": None}]}),
            model_answer({"events": [{"title": "Telefon", "line_ids": ["L00002"], "time_reference_ids": ["s1-r1"],
                                      "occurrence": "happened", "people": ["Person B"], "location": None, "category": "Familie"}]}),
            model_answer({"events": [{"title": "Besøk", "line_ids": ["L00004"], "time_reference_ids": ["s1-r2"],
                                      "occurrence": "planned", "people": ["Person C"], "location": None, "category": "Familie"}]}),
        ]
        runner = fake_runner(answers)
        runner.args.event_chunk_lines, runner.args.event_overlap_lines = 3, 1
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "01.txt"
            letter.write_text("---\nDate: 1971-12-01\n---\nPlace B 1-12-71\nI dag ringte Person B.\nHilsen.\nI morgen kommer Person C.\nAlt er klart.\nVi gleder oss.\n", encoding="utf-8")
            summary = process_letter(runner, letter, input_dir, output_dir)
            self.assertEqual(summary["status"], "complete")
            final = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
            self.assertEqual([event["date"] for event in final["events"]], ["1971-12-01", "1971-12-02"])
            self.assertEqual([event["occurrence"] for event in final["events"]], ["happened", "planned"])
            self.assertEqual(runner.completion.call_count, 4)

    def test_token_limit_stops_without_repeating_long_request(self):
        runner = fake_runner([model_answer({}, reason="length")], retries=2)
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            result, errors = runner.run_stage(Path(tmp), "01_scan", "scan", {"lines": []}, lambda _: [], 1000)
            self.assertIsNone(result)
            self.assertIn("tokenbudsjettet", errors[0])
            self.assertEqual(runner.completion.call_count, 1)
            runner.completion.reset_mock()
            result, _ = runner.run_stage(Path(tmp), "01_scan", "scan", {"lines": []}, lambda _: [], 1000)
            self.assertIsNone(result)
            runner.completion.assert_not_called()

    def test_failed_extraction_writes_partial_not_final(self):
        runner = fake_runner([model_answer({"new_sessions": []}), model_answer({}, reason="length")])
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "01.txt"
            letter.write_text("---\nDate: 1975-12-21\n---\nI går kom Person C.\n", encoding="utf-8")
            summary = process_letter(runner, letter, input_dir, output_dir)
            self.assertEqual(summary["status"], "needs_review")
            self.assertFalse(list(output_dir.rglob("final.json")))
            partial = json.loads(next(output_dir.rglob("partial.json")).read_text(encoding="utf-8"))
            self.assertFalse(partial["validation"]["complete"])
            self.assertTrue(partial["validation"]["schema_valid"])

    def test_earlier_hypothesis_is_not_used_after_a_certain_session(self):
        answers = [
            model_answer({"new_sessions": [{"start_line_id": "L00003"}]}),
            model_answer({"references": []}), model_answer({"events": []}),
            model_answer({"references": [{"id": "r1", "line_ids": ["L00004"], "kind": "previous_day", "anchor_id": None, "day_offset": None}]}),
            model_answer({"preferred_date": "1971-11-30", "candidates": [
                {"date": "1971-11-30", "support_line_ids": ["L00003"], "against_line_ids": []}]}),
            model_answer({"checks": [{"date": "1971-11-30", "verdict": "supported",
                                      "support_line_ids": ["L00003"], "against_line_ids": []}]}),
            model_answer({"events": [{"title": "Besøk", "line_ids": ["L00004"], "time_reference_ids": ["s2-r1"],
                                      "occurrence": "happened", "people": [], "location": None, "category": "Familie"}]}),
        ]
        runner = fake_runner(answers)
        with tempfile.TemporaryDirectory(dir="workspace") as tmp:
            root = Path(tmp).resolve()
            input_dir, output_dir = root / "input", root / "output"
            input_dir.mkdir()
            letter = input_dir / "01.txt"
            letter.write_text("---\nDate: 1971-12-01\n---\nPlace B 1-12-71\nHilsen.\nTirsdag skriver jeg igjen.\nI går kom Person B.\n", encoding="utf-8")
            summary = process_letter(runner, letter, input_dir, output_dir)
            final = json.loads((output_dir / summary["result"]).read_text(encoding="utf-8"))
            self.assertEqual(final["sessions"][1]["base_date"]["status"], "unresolved")
            self.assertNotIn("1971-11-30", final["sessions"][1]["base_date"]["alternatives"])
            self.assertEqual(final["sessions"][1]["base_date"]["candidates"][0]["date"], "1971-11-30")
            self.assertEqual(final["sessions"][1]["base_date"]["candidates"][0]["chronology"], "conflict")
            self.assertEqual(final["events"][0]["date_basis"], "unresolved")


if __name__ == "__main__":
    unittest.main()
