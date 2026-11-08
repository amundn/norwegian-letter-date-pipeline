import unittest

from letter_pipeline.session_pipeline import attach_event_date, resolve_references


class TemporalDirectionTests(unittest.TestCase):
    def setUp(self):
        self.base = {"date": "1971-12-07", "status": "hypothesis", "alternatives": []}

    def event_with_weekday(self, text, occurrence):
        refs = [{"id": "r1", "kind": "other", "anchor_id": None, "quote": text,
                 "line_ids": ["L00001"]}]
        event = {"quote": text, "line_ids": ["L00001"], "occurrence": occurrence,
                 "time_reference_ids": ["r1"]}
        return attach_event_date(event, resolve_references(refs, self.base), self.base)

    def test_past_and_planned_weekdays_point_in_opposite_directions(self):
        past = self.event_with_weekday("Mandag var vi på besøk.", "happened")
        future = self.event_with_weekday("På mandag skal vi på besøk.", "planned")
        self.assertEqual(past["date_candidates"], ["1971-11-29", "1971-12-06"])
        self.assertEqual(future["date_candidates"], ["1971-12-13", "1971-12-20"])
        self.assertEqual(past["date_basis"], "unresolved")
        self.assertIsNone(future["suggested_date"])

    def test_wednesday_plan_keeps_more_than_one_possible_week(self):
        result = self.event_with_weekday("Formannskapet skal behandle søknaden onsdag.", "planned")
        self.assertEqual(result["date_candidates"], ["1971-12-08", "1971-12-15"])
        self.assertIn("ikke uttømmende", result["date_reason"])

    def test_ambiguous_or_conflicting_direction_stays_unresolved(self):
        for text, occurrence in [("Mandag er det møte.", "planned"),
                                 ("Hvis vi skal reise mandag.", "planned"),
                                 ("Vi var der mandag og skal tilbake.", "happened"),
                                 ("Fredag ringte hun, lørdag kom hun.", "happened"),
                                 ("Mandag var vi på besøk.", "planned")]:
            with self.subTest(text=text):
                self.assertEqual(self.event_with_weekday(text, occurrence)["date_candidates"], [])

    def test_same_weekday_can_be_today_and_previous_week(self):
        result = self.event_with_weekday("Tirsdag var vi på besøk.", "happened")
        self.assertEqual(result["date_candidates"], ["1971-11-30", "1971-12-07"])

    def test_yesterday_evening_resolves_but_not_unfinished_next_sentence(self):
        refs = [{"id": "r1", "kind": "previous_day", "anchor_id": None,
                 "quote": "det på dagen. Igårkveld"}]
        resolved = resolve_references(refs, self.base)
        self.assertEqual(resolved[0]["suggested_date"], "1971-12-06")
        wrong_event = {"quote": "Jeg sendte brev til Olav. Igårkveld", "time_reference_ids": ["r1"]}
        result = attach_event_date(wrong_event, resolved, self.base)
        self.assertEqual(result["date_basis"], "unresolved")
        self.assertEqual(result["date_candidates"], [])
        self.assertIn("ny setning", result["date_reason"])

    def test_weekday_and_day_number_use_session_month_as_hypothesis(self):
        refs = [{"id": "r1", "kind": "explicit_date", "anchor_id": None,
                 "quote": "3 søndag 19. og da blir hun med"}]
        result = resolve_references(refs, self.base)[0]
        self.assertEqual(result["suggested_date"], "1971-12-19")
        self.assertEqual(result["date_basis"], "hypothesis")
        self.assertEqual(result["date_candidates"], ["1971-12-19"])
        self.assertIn("måned og år er antatt", result["reason"])
        event = attach_event_date({"time_reference_ids": ["r1"]}, [result], self.base)
        self.assertEqual(event["suggested_date"], "1971-12-19")

    def test_weekday_mismatch_does_not_force_a_date(self):
        refs = [{"id": "r1", "kind": "explicit_date", "anchor_id": None,
                 "quote": "mandag 19."}]
        result = resolve_references(refs, self.base)[0]
        self.assertEqual(result["date_basis"], "unresolved")
        self.assertIsNone(result["suggested_date"])

    def test_weekday_day_at_year_boundary_does_not_force_current_month(self):
        refs = [{"id": "r1", "kind": "explicit_date", "anchor_id": None,
                 "quote": "mandag 1."}]
        result = resolve_references(refs, {"date": "2023-12-30", "status": "explicit"})[0]
        self.assertEqual(result["date_basis"], "unresolved")
        self.assertIsNone(result["suggested_date"])

    def test_weekday_day_preserves_alternative_session_months(self):
        refs = [{"id": "r1", "kind": "other", "anchor_id": None,
                 "quote": "søndag 19."}]
        base = {"date": "1971-12-07", "status": "hypothesis", "alternatives": ["1971-09-07"]}
        result = resolve_references(refs, base)[0]
        self.assertEqual(result["suggested_date"], "1971-12-19")
        self.assertEqual(result["date_candidates"], ["1971-09-19", "1971-12-19"])

    def test_weekday_day_without_session_date_stays_unresolved(self):
        refs = [{"id": "r1", "kind": "explicit_date", "anchor_id": None,
                 "quote": "søndag 19."}]
        result = resolve_references(refs, {"date": None, "status": "unresolved"})[0]
        self.assertIsNone(result["suggested_date"])
        self.assertEqual(result["date_basis"], "unresolved")


if __name__ == "__main__":
    unittest.main()
