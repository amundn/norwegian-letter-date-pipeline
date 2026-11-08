"""Schemas for the session-based letter workflow."""

LINE = {"type": "string", "pattern": r"^L[0-9]{5,}$"}
LINES = {"type": "array", "minItems": 1, "uniqueItems": True, "items": LINE}
OPTIONAL_LINES = {"type": "array", "uniqueItems": True, "items": LINE}
AUDIT_LINES = {**OPTIONAL_LINES, "maxItems": 8}
DATE = {"anyOf": [{"type": "null"}, {"type": "string", "format": "date"}]}
TEXT = {"type": "string", "minLength": 1, "maxLength": 200}
IDS = {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}}
ROOT = "https://json-schema.org/draft/2020-12/schema"

SCAN = {
    "$schema": ROOT, "type": "object", "additionalProperties": False,
    "required": ["new_sessions"],
    "properties": {"new_sessions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["start_line_id"], "properties": {"start_line_id": LINE},
    }}},
}

REFERENCE = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "line_ids", "kind", "anchor_id", "day_offset"],
    "properties": {
        "id": {"type": "string", "pattern": r"^r[0-9]+$"}, "line_ids": LINES,
        "kind": {"enum": ["session_day", "previous_day", "next_day", "previous_weekday", "explicit_date", "relative_reference", "other"]},
        "anchor_id": {"anyOf": [{"type": "null"}, {"type": "string", "pattern": r"^(?:r[0-9]+|s[0-9]+-r[0-9]+)$"}]},
        "day_offset": {"enum": [None, -1, 1]},
    },
}
EXTRACT = {
    "$schema": ROOT, "type": "object", "additionalProperties": False,
    "required": ["references"],
    "properties": {"references": {"type": "array", "items": REFERENCE}},
}

BASE = {
    "$schema": ROOT, "type": "object", "additionalProperties": False,
    "required": ["preferred_date", "candidates"],
    "properties": {
        "preferred_date": DATE,
        "candidates": {"type": "array", "maxItems": 4, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["date", "support_line_ids", "against_line_ids"],
            "properties": {"date": {"type": "string", "format": "date"},
                           "support_line_ids": LINES, "against_line_ids": OPTIONAL_LINES},
        }},
    },
}

AUDIT = {
    "$schema": ROOT, "type": "object", "additionalProperties": False,
    "required": ["checks"], "properties": {"checks": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["date", "verdict", "support_line_ids", "against_line_ids"],
        "properties": {"date": {"type": "string", "format": "date"},
            "verdict": {"enum": ["supported", "contradicted", "undetermined"]},
            "support_line_ids": AUDIT_LINES, "against_line_ids": AUDIT_LINES},
    }}},
}

MODEL_EVENT = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "line_ids", "time_reference_ids", "occurrence", "people", "location", "category"],
    "properties": {
        "title": TEXT, "line_ids": {**LINES, "maxItems": 16}, "time_reference_ids": IDS,
        "occurrence": {"enum": ["happened", "planned", "unclear"]},
        "people": {"type": "array", "items": TEXT},
        "location": {"anyOf": [{"type": "null"}, TEXT]},
        "category": {"enum": ["Familie", "Reise", "Helse", "Arbeid", "Utdanning", "Kultur", "Religion", "Annet"]},
    },
}
EVENTS = {
    "$schema": ROOT, "type": "object", "additionalProperties": False,
    "required": ["events"],
    "properties": {"events": {"type": "array", "items": MODEL_EVENT}},
}

DATE_STATUS_RULES = [
    {"if": {"properties": {"date_basis": {"enum": ["explicit", "derived"]}}},
     "then": {"properties": {"date": {"type": "string", "format": "date"}, "suggested_date": {"type": "null"}}}},
    {"if": {"properties": {"date_basis": {"const": "hypothesis"}}},
     "then": {"properties": {"date": {"type": "null"}, "suggested_date": {"type": "string", "format": "date"}}}},
    {"if": {"properties": {"date_basis": {"const": "unresolved"}}},
     "then": {"properties": {"date": {"type": "null"}, "suggested_date": {"type": "null"}}}},
]
REF_RESULT = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "session_id", "line_ids", "quote", "kind", "anchor_id", "day_offset",
                 "date", "suggested_date", "date_basis", "date_candidates", "reason"],
    "properties": {
        "id": TEXT, "session_id": TEXT, "line_ids": LINES, "quote": {"type": "string", "minLength": 1},
        "kind": REFERENCE["properties"]["kind"], "anchor_id": {"anyOf": [{"type": "null"}, TEXT]},
        "day_offset": {"enum": [None, -1, 1]}, "date": DATE, "suggested_date": DATE,
        "date_basis": {"enum": ["explicit", "derived", "hypothesis", "unresolved"]},
        "date_candidates": {"type": "array", "uniqueItems": True, "items": {"type": "string", "format": "date"}},
        "reason": TEXT,
    }, "allOf": DATE_STATUS_RULES,
}
EVENT_RESULT = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "session_id", "title", "line_ids", "quote", "time_reference_ids", "occurrence",
                 "people", "location", "category", "date", "suggested_date", "date_basis", "date_candidates",
                 "time_expression", "date_reason", "review_note"],
    "properties": {
        "id": TEXT, "session_id": TEXT, "title": TEXT, "line_ids": LINES,
        "quote": {"type": "string", "minLength": 1}, "time_reference_ids": IDS,
        "occurrence": MODEL_EVENT["properties"]["occurrence"],
        "people": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["mention", "resolved_name"], "properties": {"mention": TEXT, "resolved_name": {"type": "null"}}}},
        "location": MODEL_EVENT["properties"]["location"], "category": MODEL_EVENT["properties"]["category"],
        "date": DATE, "suggested_date": DATE,
        "date_basis": {"enum": ["explicit", "derived", "hypothesis", "unresolved"]},
        "date_candidates": {"type": "array", "uniqueItems": True, "items": {"type": "string", "format": "date"}},
        "time_expression": {"type": "string"}, "date_reason": {"type": "string", "minLength": 1},
        "review_note": {"anyOf": [{"type": "null"}, TEXT]},
    }, "allOf": DATE_STATUS_RULES,
}

LETTER_TASK_EVENT = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "date", "date_candidates", "date_basis", "time_expression", "date_reason",
                 "occurrence", "people", "location", "category", "quote", "review_note"],
    "properties": {key: value for key, value in EVENT_RESULT["properties"].items() if key in (
        "title", "date", "date_candidates", "time_expression", "date_reason", "occurrence",
        "people", "location", "category", "quote", "review_note")},
}
LETTER_TASK_EVENT["properties"]["date_basis"] = {"enum": ["explicit", "derived", "uncertain"]}
LETTER_TASK_EVENT["allOf"] = [{"if": {"properties": {"date_basis": {"const": "uncertain"}}},
                              "then": {"properties": {"date": {"type": "null"}}}},
                             {"if": {"properties": {"date_basis": {"enum": ["explicit", "derived"]}}},
                              "then": {"properties": {"date": {"type": "string", "format": "date"}}}}]
LETTER_TASK = {
    "$schema": ROOT, "type": "object", "additionalProperties": False,
    "required": ["letter_id", "events", "warnings"],
    "properties": {"letter_id": TEXT, "events": {"type": "array", "items": LETTER_TASK_EVENT},
                   "warnings": {"type": "array", "items": {"type": "string"}}},
}

FINAL = {
    "$schema": ROOT, "title": "Session-based historical letter analysis",
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "letter_id", "source_sha256", "metadata", "sessions",
                 "time_references", "events", "warnings", "validation"],
    "properties": {
        "schema_version": {"const": "2.1"},
        "letter_id": {"type": "string", "minLength": 1},
        "source_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
        "metadata": {"type": "object", "additionalProperties": False,
                     "required": ["letter_id", "date", "date_is_certain", "frontmatter_date", "from", "to", "source_file"],
                     "properties": {"letter_id": TEXT, "date": DATE, "date_is_certain": {"type": "boolean"},
                         "frontmatter_date": {"anyOf": [{"type": "null"}, {"type": "string"}]},
                         "from": {"anyOf": [{"type": "null"}, TEXT]}, "to": {"anyOf": [{"type": "null"}, TEXT]},
                         "source_file": TEXT}},
        "sessions": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "start_line_id", "end_line_id", "base_date"],
            "properties": {
                "id": TEXT, "start_line_id": LINE, "end_line_id": LINE,
                "base_date": {"type": "object", "additionalProperties": False,
                    "required": ["date", "status", "alternatives", "support_quotes", "against_quotes", "candidates", "source"],
                    "properties": {
                        "date": DATE, "status": {"enum": ["explicit", "hypothesis", "unresolved"]},
                        "alternatives": {"type": "array", "items": {"type": "string", "format": "date"}},
                        "support_quotes": {"type": "array", "items": {"type": "string"}},
                        "against_quotes": {"type": "array", "items": {"type": "string"}},
                        "candidates": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                            "required": ["date", "weekday", "chronology", "support_line_ids", "against_line_ids", "support_quotes", "against_quotes", "audit"],
                            "properties": {"date": {"type": "string", "format": "date"}, "weekday": TEXT,
                                "chronology": {"enum": ["consistent", "conflict", "not_checked"]},
                                "support_line_ids": LINES, "against_line_ids": OPTIONAL_LINES,
                                "support_quotes": {"type": "array", "items": {"type": "string"}},
                                "against_quotes": {"type": "array", "items": {"type": "string"}},
                                "audit": {"type": "object", "additionalProperties": False,
                                    "required": ["verdict", "reported_verdict", "support_line_ids", "against_line_ids", "support_quotes", "against_quotes"],
                                    "properties": {"verdict": {"enum": ["supported", "contradicted", "undetermined", "not_checked"]},
                                        "reported_verdict": {"enum": ["supported", "contradicted", "undetermined", "not_checked"]},
                                        "support_line_ids": OPTIONAL_LINES, "against_line_ids": OPTIONAL_LINES,
                                        "support_quotes": {"type": "array", "items": {"type": "string"}},
                                        "against_quotes": {"type": "array", "items": {"type": "string"}}}}}}},
                        "source": TEXT,
                    }, "allOf": [
                        {"if": {"properties": {"status": {"const": "unresolved"}}},
                         "then": {"properties": {"date": {"type": "null"}}}},
                        {"if": {"properties": {"status": {"enum": ["explicit", "hypothesis"]}}},
                         "then": {"properties": {"date": {"type": "string", "format": "date"}}}},
                    ]},
            }}},
        "time_references": {"type": "array", "items": REF_RESULT},
        "events": {"type": "array", "items": EVENT_RESULT},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "validation": {"type": "object", "additionalProperties": False,
                       "required": ["schema_valid", "complete", "model", "method"],
                       "properties": {"schema_valid": {"const": True}, "complete": {"type": "boolean"},
                                      "model": TEXT, "method": TEXT}},
    },
}
