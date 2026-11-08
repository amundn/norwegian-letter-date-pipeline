"""JSON schemas for every model-produced pipeline artifact."""

from datetime import date
from copy import deepcopy

STRING = {"type": "string", "minLength": 1}
NULLABLE_DATE = {"anyOf": [{"type": "null"}, {"type": "string", "format": "date"}]}
STRINGS = {"type": "array", "items": {"type": "string"}}
OCCURRENCE = {"enum": ["happened", "planned", "unclear"]}

SESSION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "opening_quote", "explicit_date", "date_clues", "uncertainties"],
    "properties": {
        "id": STRING,
        "opening_quote": STRING,
        "explicit_date": NULLABLE_DATE,
        "date_clues": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["quote", "observation"],
            "properties": {"quote": STRING, "observation": STRING},
        }},
        "uncertainties": STRINGS,
    },
}

TIME_REFERENCE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "session_id", "quote", "effective_expression", "describes",
                 "relation_to", "relation", "occurrence"],
    "properties": {
        "id": STRING, "session_id": STRING, "quote": STRING,
        "effective_expression": STRING, "describes": STRING,
        "relation_to": {"anyOf": [{"type": "null"}, STRING]},
        "relation": {"enum": [None, "before", "after", "same_day", "day_before", "day_after"]},
        "occurrence": OCCURRENCE,
    },
}

STAGE1_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "required": ["sessions", "time_references", "warnings"],
    "properties": {
        "sessions": {"type": "array", "minItems": 1, "items": SESSION_SCHEMA},
        "time_references": {"type": "array", "items": TIME_REFERENCE_SCHEMA},
        "warnings": STRINGS,
    },
}

EVIDENCE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["time_reference_ids", "quotes", "explanation"],
    "properties": {
        "time_reference_ids": {"type": "array", "uniqueItems": True, "items": STRING},
        "quotes": {"type": "array", "minItems": 1, "items": STRING}, "explanation": STRING,
    },
}

HYPOTHESIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["session_id", "preferred_date", "certainty", "support", "against",
                 "alternatives", "covered_reference_ids", "scope_note"],
    "properties": {
        "session_id": STRING,
        "preferred_date": NULLABLE_DATE,
        "certainty": {"enum": ["explicit", "probable", "unresolved"]},
        "support": {"type": "array", "items": EVIDENCE_SCHEMA},
        "against": {"type": "array", "items": EVIDENCE_SCHEMA},
        "alternatives": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["date", "explanation"],
            "properties": {"date": {"type": "string", "format": "date"}, "explanation": STRING},
        }},
        "covered_reference_ids": {"type": "array", "uniqueItems": True, "items": STRING},
        "scope_note": STRING,
    },
}

CALENDAR_ENTRY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["time_reference_id", "date", "suggested_date", "basis",
                 "depends_on_time_reference_ids", "depends_on_session_ids", "explanation"],
    "properties": {
        "time_reference_id": STRING,
        "date": NULLABLE_DATE, "suggested_date": NULLABLE_DATE,
        "basis": {"enum": ["explicit", "derived", "hypothesis", "unresolved"]},
        "depends_on_time_reference_ids": {"type": "array", "uniqueItems": True, "items": STRING},
        "depends_on_session_ids": {"type": "array", "uniqueItems": True, "items": STRING},
        "explanation": STRING,
    },
}

STAGE2_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "required": ["session_hypotheses", "calendar_entries", "contradictions", "warnings"],
    "properties": {
        "session_hypotheses": {"type": "array", "items": HYPOTHESIS_SCHEMA},
        "calendar_entries": {"type": "array", "items": CALENDAR_ENTRY_SCHEMA},
        "contradictions": STRINGS, "warnings": STRINGS,
    },
}

PERSON_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["mention", "resolved_name"],
    "properties": {"mention": STRING, "resolved_name": {"anyOf": [{"type": "null"}, STRING]}},
}

EVENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "session_id", "time_reference_ids", "date", "suggested_date",
                 "date_basis", "session_hypothesis_ids", "date_candidates", "time_expression",
                 "date_reason", "occurrence", "people", "location", "category", "quote", "review_note"],
    "properties": {
        "title": STRING, "session_id": STRING,
        "time_reference_ids": {"type": "array", "minItems": 1, "uniqueItems": True, "items": STRING},
        "date": NULLABLE_DATE, "suggested_date": NULLABLE_DATE,
        "date_basis": {"enum": ["explicit", "derived", "hypothesis", "unresolved"]},
        "session_hypothesis_ids": {"type": "array", "uniqueItems": True, "items": STRING},
        "date_candidates": {"type": "array", "uniqueItems": True, "items": {"type": "string", "format": "date"}},
        "time_expression": STRING, "date_reason": STRING, "occurrence": OCCURRENCE,
        "people": {"type": "array", "items": PERSON_SCHEMA},
        "location": {"anyOf": [{"type": "null"}, STRING]},
        "category": {"enum": ["Familie", "Reise", "Helse", "Arbeid", "Utdanning", "Kultur", "Religion", "Annet"]},
        "quote": STRING,
        "review_note": {"anyOf": [{"type": "null"}, {"type": "string"}]},
    },
}

STAGE3_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "required": ["events", "warnings"],
    "properties": {
        "events": {"type": "array", "items": EVENT_SCHEMA},
        "warnings": STRINGS,
    },
}

# The model points at numbered source lines. Python materializes the final
# quote fields from the original letter after these compact response schemas pass.
LINE_IDS = {"type": "array", "minItems": 1, "uniqueItems": True, "items": STRING}

MODEL_SESSION_SCHEMA = deepcopy(SESSION_SCHEMA)
MODEL_SESSION_SCHEMA["required"] = ["id", "opening_line_ids", "explicit_date", "date_clues", "uncertainties"]
MODEL_SESSION_SCHEMA["properties"].pop("opening_quote")
MODEL_SESSION_SCHEMA["properties"]["opening_line_ids"] = LINE_IDS
_clue_schema = MODEL_SESSION_SCHEMA["properties"]["date_clues"]["items"]
_clue_schema["required"] = ["line_ids", "observation"]
_clue_schema["properties"].pop("quote")
_clue_schema["properties"]["line_ids"] = LINE_IDS

MODEL_TIME_REFERENCE_SCHEMA = deepcopy(TIME_REFERENCE_SCHEMA)
MODEL_TIME_REFERENCE_SCHEMA["required"] = [
    "id", "session_id", "line_ids", "effective_expression", "describes",
    "relation_to", "relation", "occurrence",
]
MODEL_TIME_REFERENCE_SCHEMA["properties"].pop("quote")
MODEL_TIME_REFERENCE_SCHEMA["properties"]["line_ids"] = LINE_IDS

MODEL_STAGE1_SCHEMA = deepcopy(STAGE1_SCHEMA)
MODEL_STAGE1_SCHEMA["properties"]["sessions"]["items"] = MODEL_SESSION_SCHEMA
MODEL_STAGE1_SCHEMA["properties"]["time_references"]["items"] = MODEL_TIME_REFERENCE_SCHEMA

MODEL_EVIDENCE_SCHEMA = deepcopy(EVIDENCE_SCHEMA)
MODEL_EVIDENCE_SCHEMA["required"] = ["time_reference_ids", "line_ids", "explanation"]
MODEL_EVIDENCE_SCHEMA["properties"].pop("quotes")
MODEL_EVIDENCE_SCHEMA["properties"]["line_ids"] = LINE_IDS

MODEL_HYPOTHESIS_SCHEMA = deepcopy(HYPOTHESIS_SCHEMA)
MODEL_HYPOTHESIS_SCHEMA["properties"]["support"]["items"] = MODEL_EVIDENCE_SCHEMA
MODEL_HYPOTHESIS_SCHEMA["properties"]["against"]["items"] = MODEL_EVIDENCE_SCHEMA

MODEL_STAGE2_SCHEMA = deepcopy(STAGE2_SCHEMA)
MODEL_STAGE2_SCHEMA["properties"]["session_hypotheses"]["items"] = MODEL_HYPOTHESIS_SCHEMA

MODEL_EVENT_SCHEMA = deepcopy(EVENT_SCHEMA)
MODEL_EVENT_SCHEMA["required"] = [key for key in MODEL_EVENT_SCHEMA["required"] if key != "quote"] + ["line_ids"]
MODEL_EVENT_SCHEMA["properties"].pop("quote")
MODEL_EVENT_SCHEMA["properties"]["line_ids"] = LINE_IDS

MODEL_STAGE3_SCHEMA = deepcopy(STAGE3_SCHEMA)
MODEL_STAGE3_SCHEMA["properties"]["events"]["items"] = MODEL_EVENT_SCHEMA

FINAL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Historical letter calendar extraction",
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "letter_id", "source_sha256", "metadata", "sessions",
                 "time_references", "session_hypotheses", "calendar_entries", "events",
                 "contradictions", "warnings", "validation"],
    "properties": {
        "schema_version": {"const": "1.2"}, "letter_id": STRING, "source_sha256": STRING,
        "metadata": {
            "type": "object", "additionalProperties": False,
            "required": ["letter_id", "source_file", "date", "date_is_certain", "from", "to"],
            "properties": {
                "letter_id": STRING,
                "source_file": STRING, "date": NULLABLE_DATE, "date_is_certain": {"type": "boolean"},
                "frontmatter_date": {"anyOf": [{"type": "null"}, {"type": "string"}]},
                "from": {"anyOf": [{"type": "null"}, {"type": "string"}]},
                "to": {"anyOf": [{"type": "null"}, {"type": "string"}]},
            },
        },
        "sessions": {"type": "array", "minItems": 1, "items": SESSION_SCHEMA},
        "time_references": {"type": "array", "items": TIME_REFERENCE_SCHEMA},
        "session_hypotheses": {"type": "array", "items": HYPOTHESIS_SCHEMA},
        "calendar_entries": {"type": "array", "items": CALENDAR_ENTRY_SCHEMA},
        "events": {"type": "array", "items": EVENT_SCHEMA},
        "contradictions": STRINGS, "warnings": STRINGS,
        "validation": {
            "type": "object", "additionalProperties": False,
            "required": ["schema_valid", "model", "passes", "repairs", "source_quote_errors", "date_errors", "reference_errors"],
            "properties": {
                "schema_valid": {"const": True}, "model": STRING,
                "passes": {"type": "integer", "minimum": 3}, "repairs": {"type": "integer", "minimum": 0},
                "source_quote_errors": {"const": 0}, "date_errors": {"const": 0}, "reference_errors": {"const": 0},
            },
        },
    },
}

def iso_date(value):
    """True when value is a real YYYY-MM-DD date, not merely a date-like string."""
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False
