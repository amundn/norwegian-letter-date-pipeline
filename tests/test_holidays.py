import unittest

from letter_pipeline.holidays import holiday_date, identify_holiday
from letter_pipeline.session_pipeline import resolve_references


class HolidayTests(unittest.TestCase):
    def test_named_days_for_collection_years(self):
        examples = {
            "palmesøndag": (1971, "1971-04-04"),
            "skjærtorsdag": (1971, "1971-04-08"),
            "3. dag påske": (1971, "1971-04-13"),
            "Kristi Himmelfartsdag": (1971, "1971-05-20"),
            "1. pinsedag": (1971, "1971-05-30"),
            "2. pinsedag": (1971, "1971-05-31"),
            "sommerdagen": (1976, "1976-04-14"),
            "Olsok-dagen": (1973, "1973-07-29"),
            "St. Hansaften": (1971, "1971-06-23"),
            "3. dag jul": (1971, "1971-12-27"),
            "morsdag": (1971, "1971-02-14"),
            "1. søndag i advent": (1971, "1971-11-28"),
            "Allehelgensdag": (1971, "1971-11-07"),
            "Bots- og bededag": (1971, "1971-10-31"),
        }
        for expression, (year, expected) in examples.items():
            with self.subTest(expression=expression):
                match = identify_holiday(expression)
                self.assertIsNotNone(match)
                self.assertEqual(holiday_date(match[0], year).isoformat(), expected)

    def test_unqualified_periods_are_not_assigned_a_day(self):
        for expression in ("3. dag", "St. Hans", "advent", "påske", "årdag"):
            with self.subTest(expression=expression):
                self.assertIsNone(identify_holiday(expression))

    def test_year_near_session_is_only_a_candidate(self):
        refs = [{"id": "r1", "kind": "other", "anchor_id": None, "quote": "Vi besøkte dem olsok."}]
        result = resolve_references(refs, {"date": "1971-12-01", "status": "explicit"})[0]
        self.assertEqual(result["date_basis"], "unresolved")
        self.assertIsNone(result["suggested_date"])
        self.assertIn("1971-07-29", result["date_candidates"])
        self.assertIn("1972-07-29", result["date_candidates"])

    def test_written_year_gives_hypothesis(self):
        refs = [{"id": "r1", "kind": "other", "anchor_id": None,
                 "quote": "Søndag - Olsok-dagen 29. juli 1973."}]
        result = resolve_references(refs, {"date": "1973-08-01", "status": "explicit"})[0]
        self.assertEqual(result["suggested_date"], "1973-07-29")
        self.assertEqual(result["date_basis"], "hypothesis")

    def test_historical_bededag_is_not_assumed(self):
        with self.assertRaises(ValueError):
            holiday_date("bededag", 1940)

