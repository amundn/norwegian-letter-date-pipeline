"""CLI runner. All inference stays on the configured Ollama service."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

import jsonschema
from jsonschema import Draft202012Validator, FormatChecker

from . import __version__, prompts, schema

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
STAGES = (("sessions", "01_sessions", schema.MODEL_STAGE1_SCHEMA, 12288),
          ("timeline", "02_timeline", schema.MODEL_STAGE2_SCHEMA, 12288),
          ("events", "03_events", schema.MODEL_STAGE3_SCHEMA, 12288))

def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def index_source_lines(source: str):
    lines = source.splitlines()
    rows = [{"id": f"L{index:05d}", "text": line} for index, line in enumerate(lines, 1)]
    line_map = {row["id"]: row["text"] for row in rows}
    return rows, line_map, "\n".join(lines)

def materialize_line_quotes(stage, value, line_map):
    """Replace model-selected line IDs with exact source text for final artifacts."""
    value = json.loads(json.dumps(value, ensure_ascii=False))
    errors = []

    def groups(ids, where):
        unknown = [line_id for line_id in ids if line_id not in line_map]
        if unknown:
            errors.append(f"{where}: ukjente line_ids: {', '.join(unknown)}.")
            return []
        if any(not re.fullmatch(r"L\d{5}", line_id) for line_id in ids):
            errors.append(f"{where}: line_id-formatet er ugyldig.")
            return []
        ordered = sorted(ids, key=lambda line_id: int(line_id[1:]))
        chunks = []
        for line_id in ordered:
            number = int(line_id[1:])
            if not chunks or number != int(chunks[-1][-1][1:]) + 1:
                chunks.append([])
            chunks[-1].append(line_id)
        return ["\n".join(line_map[line_id] for line_id in chunk) for chunk in chunks]

    def quote(ids, where):
        passages = groups(ids, where)
        if len(passages) != 1:
            errors.append(f"{where}: velg én sammenhengende passasje.")
            return ""
        return passages[0]

    if stage == "sessions":
        for session in value["sessions"]:
            session["opening_quote"] = quote(session.pop("opening_line_ids"), f"Økt {session['id']} åpning")
            for index, clue in enumerate(session["date_clues"], 1):
                clue["quote"] = quote(clue.pop("line_ids"), f"Økt {session['id']} date_clue {index}")
        for reference in value["time_references"]:
            reference["quote"] = quote(reference.pop("line_ids"), f"Tidsreferanse {reference['id']}")
    elif stage == "timeline":
        for hypothesis in value["session_hypotheses"]:
            for kind in ("support", "against"):
                for index, evidence in enumerate(hypothesis[kind], 1):
                    evidence["quotes"] = groups(evidence.pop("line_ids"),
                        f"Hypotese {hypothesis['session_id']} {kind} {index}")
    else:
        for index, event in enumerate(value["events"], 1):
            event["quote"] = quote(event.pop("line_ids"), f"Hendelse {index}")
    return value, errors

def canonicalize_model_values(stage, value, stage2=None):
    """Map common model synonyms to the pipeline's canonical enums."""
    value = json.loads(json.dumps(value, ensure_ascii=False))
    changes = []
    if stage == "sessions":
        occurrence_aliases = {"scheduled": "planned", "ongoing": "unclear", "writing": "unclear",
                              "in_progress": "unclear", "completed": "happened"}
        relation_aliases = {"one_day_before": "day_before", "one_day_after": "day_after",
                            "next_day": "day_after", "previous_night": "before"}
        for reference in value.get("time_references", []):
            old = reference.get("occurrence")
            if old in occurrence_aliases:
                reference["occurrence"] = occurrence_aliases[old]
                changes.append(f"{reference.get('id')}.occurrence: {old}->{reference['occurrence']}")
            old = reference.get("relation")
            if old in relation_aliases:
                reference["relation"] = relation_aliases[old]
                changes.append(f"{reference.get('id')}.relation: {old}->{reference['relation']}")
    elif stage == "timeline":
        for hypothesis in value.get("session_hypotheses", []):
            if hypothesis.get("certainty") == "derived":
                hypothesis["certainty"] = "probable"
                changes.append(f"{hypothesis.get('session_id')}.certainty: derived->probable")
            for evidence in hypothesis.get("support", []) + hypothesis.get("against", []):
                if "time_reference_ids" not in evidence:
                    evidence["time_reference_ids"] = []
                    changes.append(f"{hypothesis.get('session_id')}.evidence.time_reference_ids: added empty list")
                if "explanation" not in evidence:
                    for alias in ("reasoning", "reason"):
                        if alias in evidence:
                            evidence["explanation"] = evidence.pop(alias)
                            changes.append(f"{hypothesis.get('session_id')}.evidence.{alias}->explanation")
                            break
                evidence.pop("reasoning", None)
                evidence.pop("reason", None)
            for alternative in hypothesis.get("alternatives", []):
                if "explanation" not in alternative:
                    for alias in ("reasoning", "reason"):
                        if alias in alternative:
                            alternative["explanation"] = alternative.pop(alias)
                            changes.append(f"{hypothesis.get('session_id')}.alternative.{alias}->explanation")
                            break
                for alias in ("reasoning", "reason"):
                    alternative.pop(alias, None)
        hypothesis_map = {item["session_id"]: item for item in value.get("session_hypotheses", [])}
        entry_map = {item.get("time_reference_id"): item for item in value.get("calendar_entries", [])}
        for _ in range(len(entry_map) + 1):
            changed = False
            for entry in value.get("calendar_entries", []):
                basis = entry.get("basis")
                if basis == "hypothesis" and entry.get("date") is not None:
                    entry["suggested_date"] = entry.get("suggested_date") or entry["date"]
                    entry["date"] = None
                    changes.append(f"{entry.get('time_reference_id')}: moved hypothesis date to suggested_date")
                    changed = True
                if basis not in ("explicit", "derived"):
                    continue
                uncertain = any(hypothesis_map.get(session_id, {}).get("certainty") == "probable"
                                for session_id in entry.get("depends_on_session_ids", []))
                uncertain = uncertain or any(entry_map.get(reference_id, {}).get("basis") == "hypothesis"
                                             for reference_id in entry.get("depends_on_time_reference_ids", []))
                if uncertain:
                    candidate = entry.get("suggested_date") or entry.get("date")
                    if candidate is not None:
                        entry["basis"] = "hypothesis"
                        entry["date"] = None
                        entry["suggested_date"] = candidate
                        changes.append(f"{entry.get('time_reference_id')}: demoted dependent date to hypothesis")
                        changed = True
            if not changed:
                break
    elif stage == "events":
        valid_ids = {item["session_id"] for item in (stage2 or {}).get("session_hypotheses", [])}
        for event in value.get("events", []):
            if "session_hypothesis_ids" in event:
                kept = []
                for hypothesis_id in event["session_hypothesis_ids"]:
                    if hypothesis_id not in valid_ids and re.fullmatch(r"h\d+", hypothesis_id):
                        changes.append(f"event {event.get('title')}: removed invented hypothesis ID {hypothesis_id}")
                    else:
                        kept.append(hypothesis_id)
                event["session_hypothesis_ids"] = kept
    return value, changes

def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)

def read_frontmatter(text: str):
    """Read the simple key/value frontmatter used by scanned letters."""
    found = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, re.S)
    if not found:
        return {}, text
    values = {}
    for line in found.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip().casefold()] = value.strip().strip("\"'")
    return values, text[found.end():]

def metadata_for(path: Path, rel: Path, text: str, date_uncertain: bool):
    fields, _ = read_frontmatter(text)
    raw_date = fields.get("date", "")
    parsed_date = None
    if raw_date:
        try:
            parsed_date = dt.date.fromisoformat(raw_date).isoformat()
        except ValueError:
            pass
    certainty = fields.get("dateiscertain", "").casefold()
    is_certain = bool(parsed_date) and certainty not in ("false", "no", "0", "uncertain")
    if date_uncertain:
        is_certain = False
    identifier = fields.get("letter_id") or fields.get("id") or path.stem
    return {"letter_id": identifier, "date": parsed_date, "date_is_certain": is_certain,
            "from": fields.get("from") or None, "to": fields.get("to") or None,
            "source_file": rel.as_posix(), "frontmatter_date": raw_date or None}

def calendar_lines(base_date, before_days: int, after_days: int):
    if base_date is None:
        return "Ingen sikker brevdato er oppgitt. Datooversikten under erstatter ikke kildebelegg."
    base = dt.date.fromisoformat(base_date)
    rows = []
    for offset in range(-before_days, after_days + 1):
        day = base + dt.timedelta(days=offset)
        rows.append(f"{day.isoformat()} {WEEKDAYS[day.weekday()]}")
    return f"Sikker brevdato: {base.isoformat()} {WEEKDAYS[base.weekday()]}\n" + "\n".join(rows)

def model_name(value: str):
    return value.removeprefix("local/")

def ollama_request(base_url: str, route: str, body=None, timeout=60):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + route, data=data,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")[:1500]
        raise RuntimeError(f"Ollama returned HTTP {error.code}: {details}") from error

def completion(base_url, model, prompt, max_tokens, context, temperature, think, timeout):
    request = {"model": model, "messages": [{"role": "user", "content": prompt}],
        "stream": False, "keep_alive": "20m", "options": {
            "num_ctx": context, "num_predict": max_tokens, "temperature": temperature,
            "top_p": 0.95, "top_k": 20, "seed": 42, "repeat_penalty": 1.0}}
    if think is not None:
        request["think"] = think
    start = time.perf_counter()
    response = ollama_request(base_url, "/api/chat", request, timeout=timeout)
    message = response.get("message", {})
    content = message.get("content") or ""
    return {"content": content, "model": response.get("model", model),
        "done_reason": response.get("done_reason"),
        "prompt_tokens": response.get("prompt_eval_count"),
        "generated_tokens": response.get("eval_count"),
        "seconds": round(time.perf_counter() - start, 2),
        "tokens_per_second": round(response["eval_count"] * 1e9 / response["eval_duration"], 2)
            if response.get("eval_duration") else None}

def parse_json(content):
    if not content.strip():
        return None, ["Modellen returnerte tomt innhold."]
    def reject(value):
        raise ValueError("JSON cannot contain the numeric token " + value)
    try:
        result = json.loads(content, parse_constant=reject)
    except (json.JSONDecodeError, ValueError) as error:
        return None, ["Svarsteksten er ikke gyldig JSON: " + str(error)]
    if not isinstance(result, dict):
        return None, ["JSON-roten mÃ¥ vÃ¦re et objekt."]
    return result, []

def _errors_from_schema(instance, shape):
    validator = Draft202012Validator(shape, format_checker=FormatChecker())
    errors = []
    for error in sorted(validator.iter_errors(instance), key=lambda item: (list(map(str, item.absolute_path)), item.message)):
        pointer = "/" + "/".join(str(part) for part in error.absolute_path)
        errors.append(f"{pointer}: {error.message}")
    return errors

def _idmap(items):
    return {item["id"]: item for item in items}

def _quote_check(quote, letter, where, errors):
    if not isinstance(quote, str) or not quote:
        errors.append(where + ": sitatet er tomt.")
    elif unicodedata.normalize("NFC", quote.replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))) not in unicodedata.normalize("NFC", letter.replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))):
        errors.append(where + ": sitatet finnes ikke ordrett og sammenhengende i kilden.")

def _validate_relations(entries, references, errors):
    ref_map = _idmap(references)
    entry_map = {entry.get("time_reference_id", entry.get("id")): entry for entry in entries}
    for ref_id, ref in ref_map.items():
        target_id = ref["relation_to"]
        relation = ref["relation"]
        if target_id is None:
            if relation is not None:
                errors.append(f"Tidsreferanse {ref_id}: relation mÃ¥ vÃ¦re null nÃ¥r relation_to er null.")
            continue
        if target_id not in ref_map or target_id == ref_id:
            errors.append(f"Tidsreferanse {ref_id}: relation_to peker pÃ¥ ukjent eller egen referanse {target_id}.")
            continue
        if relation is None:
            errors.append(f"Tidsreferanse {ref_id}: oppgi relasjonen til {target_id}.")
            continue
        left, right = entry_map[ref_id], entry_map[target_id]
        left_date = left["date"] or left["suggested_date"]
        right_date = right["date"] or right["suggested_date"]
        if not left_date or not right_date:
            continue
        first, second = dt.date.fromisoformat(left_date), dt.date.fromisoformat(right_date)
        valid = {"same_day": first == second, "day_before": first + dt.timedelta(days=1) == second,
                 "day_after": first == second + dt.timedelta(days=1),
                 "before": first < second, "after": first > second}.get(relation)
        if valid is False:
            errors.append(f"Tidsreferanse {ref_id}: datoene motsier relasjonen {relation} til {target_id}.")

def validate_artifact(stage, value, letter, metadata, stage1=None, stage2=None, base_date=None):
    shapes = {"sessions": schema.STAGE1_SCHEMA, "timeline": schema.STAGE2_SCHEMA,
              "events": schema.STAGE3_SCHEMA}
    errors = _errors_from_schema(value, shapes[stage])
    if errors:
        return errors
    if stage == "sessions":
        sessions = value["sessions"]
        refs = value["time_references"]
        session_map = _idmap(sessions)
        ref_ids = [item["id"] for item in refs]
        if len(session_map) != len(sessions):
            errors.append("SkriveÃ¸kt-ID-er mÃ¥ vÃ¦re unike.")
        if len(set(ref_ids)) != len(ref_ids):
            errors.append("Tidsreferanse-ID-er mÃ¥ vÃ¦re unike.")
        if metadata["date"] and metadata["date_is_certain"]:
            if sessions[0]["explicit_date"] != metadata["date"]:
                errors.append(f"FÃ¸rste skriveÃ¸kt mÃ¥ bruke den bekreftede brevdatoen {metadata['date']}.")
        for index, session in enumerate(sessions):
            _quote_check(session["opening_quote"], letter, f"SkriveÃ¸kt {session['id']} opening_quote", errors)
            if index == 0 and metadata["date"] and metadata["date_is_certain"] and session["explicit_date"] != metadata["date"]:
                continue
            for clue in session["date_clues"]:
                _quote_check(clue["quote"], letter, f"SkriveÃ¸kt {session['id']} date_clues", errors)
        for ref in refs:
            if ref["session_id"] not in session_map:
                errors.append(f"Tidsreferanse {ref['id']} peker pÃ¥ ukjent skriveÃ¸kt {ref['session_id']}.")
            _quote_check(ref["quote"], letter, f"Tidsreferanse {ref['id']}", errors)
        _validate_relation_graph(refs, errors)
    elif stage == "timeline":
        refs = _idmap(stage1["time_references"])
        sessions = _idmap(stage1["sessions"])
        hypotheses = value["session_hypotheses"]
        hyp_ids = [item["session_id"] for item in hypotheses]
        if len(hyp_ids) != len(set(hyp_ids)) or set(hyp_ids) != set(sessions):
            errors.append("MÃ¥ ha nÃ¸yaktig Ã©n datohypotese per skriveÃ¸kt, med samme ID-er.")
        hyp_map = ({item["session_id"]: item for item in hypotheses}
                   if len(hyp_ids) == len(set(hyp_ids)) else {})
        if len(value["calendar_entries"]) != len(refs):
            errors.append("MÃ¥ ha nÃ¸yaktig Ã©n kalenderoppfÃ¸ring per tidsreferanse.")
        entry_ids = [entry["time_reference_id"] for entry in value["calendar_entries"]]
        if len(entry_ids) != len(set(entry_ids)) or set(entry_ids) != set(refs):
            errors.append("KalenderoppfÃ¸ringene mÃ¥ referere Ã©n gang til hver kjent tidsreferanse.")
        entry_map = {entry["time_reference_id"]: entry for entry in value["calendar_entries"]}
        for hypothesis in hypotheses:
            session_id = hypothesis["session_id"]
            preferred = hypothesis["preferred_date"]
            if hypothesis["certainty"] == "explicit" and preferred is None:
                errors.append(f"Hypotese {session_id}: explicit krever preferred_date.")
            if hypothesis["certainty"] == "probable" and preferred is None:
                errors.append(f"Hypotese {session_id}: probable krever preferred_date.")
            if hypothesis["certainty"] == "unresolved" and preferred is not None:
                errors.append(f"Hypotese {session_id}: unresolved krever preferred_date=null.")
            for ref_id in hypothesis["covered_reference_ids"]:
                if ref_id not in refs:
                    errors.append(f"Hypotese {session_id}: ukjent covered_reference_id {ref_id}.")
            for evidence in hypothesis["support"] + hypothesis["against"]:
                for quote in evidence["quotes"]:
                    _quote_check(quote, letter, f"Hypotese {session_id} evidens", errors)
                for ref_id in evidence["time_reference_ids"]:
                    if ref_id not in refs:
                        errors.append(f"Hypotese {session_id}: ukjent evidensreferanse {ref_id}.")
        if metadata["date"] and metadata["date_is_certain"] and stage1["sessions"]:
            first_id = stage1["sessions"][0]["id"]
            first = hyp_map.get(first_id)
            if first and (first["certainty"] != "explicit" or first["preferred_date"] != metadata["date"]):
                errors.append(f"FÃ¸rste skriveÃ¸kt mÃ¥ forankres som explicit pÃ¥ brevdatoen {metadata['date']}.")
        for entry in value["calendar_entries"]:
            ref_id = entry["time_reference_id"]
            if ref_id not in refs:
                continue
            for dependency in entry["depends_on_time_reference_ids"]:
                if dependency not in refs or dependency == ref_id:
                    errors.append(f"KalenderoppfÃ¸ring {ref_id}: ugyldig tidsavhengighet {dependency}.")
                elif dependency in entry_map and entry_map[dependency]["basis"] == "hypothesis" and entry["basis"] != "hypothesis":
                    errors.append(f"KalenderoppfÃ¸ring {ref_id}: en avhengighet av hypotese {dependency} mÃ¥ forbli usikker.")
            for dependency in entry["depends_on_session_ids"]:
                if dependency not in sessions:
                    errors.append(f"KalenderoppfÃ¸ring {ref_id}: ukjent Ã¸ktavhengighet {dependency}.")
                elif dependency in hyp_map and hyp_map[dependency]["certainty"] == "probable" and entry["basis"] != "hypothesis":
                    errors.append(f"KalenderoppfÃ¸ring {ref_id}: dato avhengig av sannsynlig skriveÃ¸kt {dependency} mÃ¥ ha basis hypothesis.")
            if entry["basis"] == "explicit" and entry["date"] is None:
                errors.append(f"KalenderoppfÃ¸ring {ref_id}: explicit krever dato.")
            if entry["basis"] == "derived" and entry["date"] is None:
                errors.append(f"KalenderoppfÃ¸ring {ref_id}: derived krever dato.")
            if entry["basis"] == "derived" and not (entry["depends_on_time_reference_ids"] or entry["depends_on_session_ids"]):
                errors.append(f"KalenderoppfÃ¸ring {ref_id}: derived krever minst Ã©n eksplisitt datoavhengighet.")
            if entry["basis"] == "hypothesis" and (entry["date"] is not None or entry["suggested_date"] is None):
                errors.append(f"KalenderoppfÃ¸ring {ref_id}: hypothesis krever date=null og suggested_date.")
            if entry["basis"] == "unresolved" and (entry["date"] is not None or entry["suggested_date"] is not None):
                errors.append(f"KalenderoppfÃ¸ring {ref_id}: unresolved krever begge datofeltene null.")
        if not errors:
            _validate_relations(value["calendar_entries"], stage1["time_references"], errors)
            for ref_id, ref in refs.items():
                hypothesis = hyp_map.get(ref["session_id"])
                entry = next(e for e in value["calendar_entries"] if e["time_reference_id"] == ref_id)
                if hypothesis and hypothesis["certainty"] == "explicit" and hypothesis["preferred_date"]:
                    expr = unicodedata.normalize("NFC", ref["effective_expression"].casefold())
                    expected = None
                    if re.search(r"\bi\s+gÃ¥r\b", expr):
                        expected = dt.date.fromisoformat(hypothesis["preferred_date"]) - dt.timedelta(days=1)
                    elif re.search(r"\bi\s+dag\b", expr):
                        expected = dt.date.fromisoformat(hypothesis["preferred_date"])
                    elif re.search(r"\bi\s+morgen\b", expr):
                        expected = dt.date.fromisoformat(hypothesis["preferred_date"]) + dt.timedelta(days=1)
                    if expected and entry["basis"] == "derived" and entry["date"] != expected.isoformat():
                        errors.append(f"KalenderoppfÃ¸ring {ref_id}: {ref['effective_expression']} fra sikker Ã¸ktdato gir {expected.isoformat()}, ikke {entry['date']}.")
    else:
        refs = _idmap(stage1["time_references"])
        sessions = _idmap(stage1["sessions"])
        entries = _idmap([{**entry, "id": entry["time_reference_id"]} for entry in stage2["calendar_entries"]])
        hypotheses = {item["session_id"]: item for item in stage2["session_hypotheses"]}
        for number, event in enumerate(value["events"], 1):
            path = f"Hendelse {number}"
            _quote_check(event["quote"], letter, path, errors)
            if event["session_id"] not in sessions:
                errors.append(f"{path}: ukjent session_id.")
            if any(ref_id not in refs for ref_id in event["time_reference_ids"]):
                errors.append(f"{path}: ukjent time_reference_id.")
            elif any(refs[ref_id]["session_id"] != event["session_id"] for ref_id in event["time_reference_ids"]):
                errors.append(f"{path}: tidsreferanser tilhÃ¸rer en annen skriveÃ¸kt.")
            if any(session_id not in hypotheses for session_id in event["session_hypothesis_ids"]):
                errors.append(f"{path}: ukjent session_hypothesis_id.")
            basis, fact_date, suggestion = event["date_basis"], event["date"], event["suggested_date"]
            if basis in ("explicit", "derived") and (fact_date is None or suggestion is not None):
                errors.append(f"{path}: {basis} krever sikker date og null suggested_date.")
            if basis == "hypothesis" and (fact_date is not None or suggestion is None):
                errors.append(f"{path}: hypothesis krever date=null og suggested_date.")
            if basis == "unresolved" and (fact_date is not None or suggestion is not None):
                errors.append(f"{path}: unresolved krever begge datofeltene null.")
            for hypothesis_id in event["session_hypothesis_ids"]:
                if hypothesis_id in hypotheses and hypotheses[hypothesis_id]["certainty"] == "unresolved":
                    errors.append(f"{path}: kan ikke utlede en datohypotese fra en unresolved skriveÃ¸kt.")
            related = [entries[ref_id] for ref_id in event["time_reference_ids"] if ref_id in entries]
            if basis in ("explicit", "derived") and related and not any(entry["date"] == fact_date and entry["basis"] in ("explicit", "derived") for entry in related):
                errors.append(f"{path}: sikker dato stemmer ikke med noen oppgitt sikker kalenderoppfÃ¸ring.")
            if basis == "hypothesis" and related and not any(entry["suggested_date"] == suggestion and entry["basis"] == "hypothesis" for entry in related):
                errors.append(f"{path}: suggested_date finnes ikke blant tidslinjens hypoteseoppfÃ¸ringer.")
            if basis in ("explicit", "derived") and event["session_id"] in hypotheses and hypotheses[event["session_id"]]["certainty"] == "probable":
                errors.append(f"{path}: kan ikke kalle datoen sikker nÃ¥r skriveÃ¸kten er sannsynlig datert.")
            if basis in ("explicit", "derived") and any(
                    hypothesis_id in hypotheses and hypotheses[hypothesis_id]["certainty"] == "probable"
                    for hypothesis_id in event["session_hypothesis_ids"]):
                errors.append(f"{path}: sikker dato avhenger av en sannsynlig datohypotese.")
    return errors

def _validate_relation_graph(refs, errors):
    ref_map = _idmap(refs)
    for ref in refs:
        relation = ref["relation"]
        target = ref["relation_to"]
        if relation is not None and target is None:
            errors.append(f"Tidsreferanse {ref['id']}: {relation} krever relation_to.")
        if target is not None and (target not in ref_map or target == ref["id"]):
            errors.append(f"Tidsreferanse {ref['id']}: relation_to mÃ¥ peke pÃ¥ en annen kjent referanse.")
        if target is None and relation is not None:
            continue
    graph = {ref["id"]: ref["relation_to"] for ref in refs if ref["relation_to"] is not None}
    for start in graph:
        visited = set()
        current = start
        while current in graph:
            if current in visited:
                errors.append(f"Syklus i relasjonskjeden for tidsreferanse {start}.")
                break
            visited.add(current)
            current = graph[current]

class LetterPipeline:
    def __init__(self, args):
        self.args = args
        self.model = model_name(args.model)
        self.model_info = ollama_request(args.base_url, "/api/show", {"model": self.model}, timeout=60)
        capabilities = self.model_info.get("capabilities", [])
        self.think = True if args.thinking == "on" else False if args.thinking == "off" else ("thinking" in capabilities)
        if args.temperature is None:
            args.temperature = 0.6 if self.think else 0.2
        self.profile = {"model": self.model, "context": args.context, "temperature": args.temperature,
                        "thinking": self.think, "max_repairs": args.max_repairs,
                        "calendar_before_days": args.calendar_before_days,
                        "calendar_after_days": args.calendar_after_days,
                        "pipeline_version": __version__}
        self.profile_hash = sha_bytes(canonical(self.profile).encode())[:10]

    def run_stage(self, letter_dir, stage, prompt, source, metadata, line_map, stage1=None, stage2=None):
        stage_name, folder_name, shape, max_tokens = next(row for row in STAGES if row[0] == stage)
        fingerprint = sha_bytes((prompt + canonical(self.profile)).encode("utf-8"))
        stage_dir = letter_dir / f"{folder_name}_{fingerprint[:12]}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        state_path = stage_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"attempts": []}
        state.update({"stage": stage, "prompt_sha256": sha_bytes(prompt.encode()),
                      "source_sha256": sha_bytes(source.encode()), "profile": self.profile})
        if state.get("accepted") and state.get("parsed") is not None:
            print(f"  Resume: {stage} allerede validert", flush=True)
            return state["parsed"], state
        previous = state["attempts"][-1]["content"] if state["attempts"] else None
        total_repairs = max(0, len(state["attempts"]) - 1)
        while True:
            if previous is None:
                current_prompt = prompt
            else:
                errors = state["attempts"][-1]["validation_errors"]
                source_lines = [{"id": key, "text": value} for key, value in line_map.items()]
                current_prompt = prompts.repair(stage, metadata, source_lines, previous, errors)
            number = len(state["attempts"])
            print(f"  {stage}: modellkall {number + 1} (kontekst {self.args.context}, maks {max_tokens} tokens)", flush=True)
            answer = completion(self.args.base_url, self.model, current_prompt, max_tokens,
                self.args.context, self.args.temperature, self.think, self.args.timeout)
            parsed, parse_errors = parse_json(answer["content"])
            errors = list(parse_errors)
            normalizations = []
            normalized = None
            if parsed is not None:
                parsed, normalizations = canonicalize_model_values(stage, parsed, stage2=stage2)
                raw_errors = _errors_from_schema(parsed, shape)
                errors.extend(raw_errors)
                if not raw_errors:
                    normalized, line_errors = materialize_line_quotes(stage, parsed, line_map)
                    errors.extend(line_errors)
                    if not line_errors:
                        errors.extend(validate_artifact(stage, normalized, source, metadata, stage1, stage2,
                            metadata["date"] if metadata["date_is_certain"] else None))
            attempt = {**answer, "prompt_sha256": sha_bytes(current_prompt.encode()),
                       "normalizations": normalizations, "validation_errors": errors,
                       "completed_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
            if not answer["content"] and answer.get("done_reason") == "length":
                errors = [f"Modellen brukte hele tokenbudsjettet ({answer.get('generated_tokens')} tokens) uten Ã¥ sende svar. "
                          "PrÃ¸v --thinking off, eller Ã¸k budsjettet for aktuell fase."]
                attempt["validation_errors"] = errors
            state["attempts"].append(attempt)
            atomic_json(stage_dir / f"attempt-{number:02d}.json", attempt)
            state["parsed"] = normalized
            state["accepted"] = normalized is not None and not errors
            state["validation_errors"] = errors
            atomic_json(state_path, state)
            if state["accepted"]:
                atomic_json(stage_dir / "accepted.json", normalized)
                print(f"  {stage}: JSON Schema og semantiske kildekontroller bestod ({answer['seconds']} s).", flush=True)
                return normalized, state
            print("  Valideringsfeil: " + " | ".join(errors[:4]), flush=True)
            if total_repairs >= self.args.max_repairs:
                return None, state
            total_repairs += 1
            previous = answer["content"]

    def process_one(self, source_path, input_dir, output_dir):
        rel = source_path.relative_to(input_dir)
        try:
            raw = source_path.read_bytes()
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as error:
            return {"source_file": rel.as_posix(), "status": "failed", "error": str(error)}
        metadata = metadata_for(source_path, rel, text, self.args.date_uncertain)
        letter_body = read_frontmatter(text)[1]
        source_hash = sha_bytes(text.encode("utf-8"))
        source_lines, line_map, source = index_source_lines(letter_body)
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(metadata["letter_id"]).stem).strip("_") or "letter"
        rel_hash = sha_bytes(rel.as_posix().encode())[:8]
        letter_dir = output_dir / (slug + "_" + rel_hash) / source_hash[:10] / (self.profile_hash + "_" + self.model.replace(":", "_").replace("/", "_"))
        letter_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(letter_dir / "metadata.json", metadata)
        atomic_json(letter_dir / "profile.json", self.profile)
        print(f"\n[{rel.as_posix()}] ID={metadata['letter_id']} dato={metadata['date'] or 'ukjent'}\n  Kilde-SHA256 {source_hash}", flush=True)
        prompt1 = prompts.render("sessions", metadata, source_lines)
        stage1, state1 = self.run_stage(letter_dir, "sessions", prompt1, source, metadata, line_map)
        if stage1 is None:
            return self.failed(letter_dir, metadata, source_hash, "sessions", state1)
        calendar = calendar_lines(metadata["date"] if metadata["date_is_certain"] else None,
                                  self.args.calendar_before_days, self.args.calendar_after_days)
        prompt2 = prompts.render("timeline", metadata, source_lines, stage1=stage1, calendar=calendar)
        stage2, state2 = self.run_stage(letter_dir, "timeline", prompt2, source, metadata, line_map, stage1=stage1)
        if stage2 is None:
            return self.failed(letter_dir, metadata, source_hash, "timeline", state2)
        prompt3 = prompts.render("events", metadata, source_lines, stage1=stage1, stage2=stage2)
        stage3, state3 = self.run_stage(letter_dir, "events", prompt3, source, metadata, line_map,
                                        stage1=stage1, stage2=stage2)
        if stage3 is None:
            return self.failed(letter_dir, metadata, source_hash, "events", state3)
        final = {"schema_version": "1.2", "letter_id": metadata["letter_id"],
            "source_sha256": source_hash, "metadata": metadata,
            "sessions": stage1["sessions"], "time_references": stage1["time_references"],
            "session_hypotheses": stage2["session_hypotheses"],
            "calendar_entries": stage2["calendar_entries"], "events": stage3["events"],
            "contradictions": stage2["contradictions"],
            "warnings": list(dict.fromkeys(stage1["warnings"] + stage2["warnings"] + stage3["warnings"])),
            "validation": {"schema_valid": True, "model": self.model,
                "passes": 3, "repairs": sum(max(0, len(s["attempts"]) - 1) for s in (state1, state2, state3)),
                "source_quote_errors": 0, "date_errors": 0, "reference_errors": 0}}
        errors = _errors_from_schema(final, schema.FINAL_SCHEMA)
        if errors:
            return self.failed(letter_dir, metadata, source_hash, "final assembly", {"validation_errors": errors})
        atomic_json(letter_dir / "final.json", final)
        print(f"  Ferdig: {len(final['events'])} hendelser. Lagret {letter_dir / 'final.json'}", flush=True)
        return {"source_file": rel.as_posix(), "letter_id": metadata["letter_id"], "status": "complete",
                "source_sha256": source_hash, "events": len(final["events"]),
                "repairs": final["validation"]["repairs"], "result": (letter_dir / "final.json").relative_to(output_dir).as_posix()}

    @staticmethod
    def failed(letter_dir, metadata, source_hash, stage, state):
        errors = state.get("validation_errors", state.get("validation_errors", []))
        result = {"source_file": metadata["source_file"], "letter_id": metadata["letter_id"],
                  "status": "needs_review", "failed_stage": stage,
                  "validation_errors": errors, "source_sha256": source_hash,
                  "checkpoint": str(letter_dir.relative_to(letter_dir.parents[2]))}
        atomic_json(letter_dir / "needs-review.json", result)
        print(f"  Krever gjennomgang i steg {stage}: " + " | ".join(errors[:5]), flush=True)
        return result

def main(argv=None):
    parser = argparse.ArgumentParser(description="Analyser norske brev ett om gangen med en lokal Ollama-modell.")
    parser.add_argument("--input", type=Path, help="Katalog med originale .txt-brev (rekursivt).")
    parser.add_argument("--output", type=Path, help="Resultatkatalog; standard er ../letter-pipeline-results.")
    parser.add_argument("--model", default="qwen3.6-local:latest", help="Ollama-modell eller OpenCode-ID local/<model>.")
    parser.add_argument("--base-url", default="http://ollama:11434", help="Ollama API-base inne i devcontaineren.")
    parser.add_argument("--context", type=int, default=32768)
    parser.add_argument("--temperature", type=float, help="Standard er 0,6 med tenking og 0,2 ellers.")
    parser.add_argument("--thinking", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--tokens-sessions", type=int, default=12288,
                        help="Maks genererte tokens i skriveÃ¸ktfasen (standard 12288).")
    parser.add_argument("--tokens-timeline", type=int, default=12288,
                        help="Maks genererte tokens i tidslinjefasen (standard 12288).")
    parser.add_argument("--tokens-events", type=int, default=12288,
                        help="Maks genererte tokens i hendelsesfasen (standard 12288).")
    parser.add_argument("--max-repairs", type=int, default=2, help="Maksimalt antall rettingskall per fase.")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--calendar-before-days", type=int, default=45)
    parser.add_argument("--calendar-after-days", type=int, default=90)
    parser.add_argument("--limit", type=int, help="Behandle hÃ¸yst N brev.")
    parser.add_argument("--date-uncertain", action="store_true", help="Behandle frontmatter-datoer som usikre.")
    parser.add_argument("--schema-only", action="store_true", help="Skriv JSON Schema for ferdigresultatet og avslutt.")
    args = parser.parse_args(argv)
    global STAGES
    STAGES = (("sessions", "01_sessions", schema.MODEL_STAGE1_SCHEMA, args.tokens_sessions),
              ("timeline", "02_timeline", schema.MODEL_STAGE2_SCHEMA, args.tokens_timeline),
              ("events", "03_events", schema.MODEL_STAGE3_SCHEMA, args.tokens_events))
    if args.schema_only:
        print(json.dumps(schema.FINAL_SCHEMA, ensure_ascii=False, indent=2))
        return 0
    if args.input is None:
        parser.error("--input mÃ¥ peke pÃ¥ en brevmappe.")
    if (args.max_repairs < 0 or args.context < 1024 or args.timeout < 1
            or min(args.tokens_sessions, args.tokens_timeline, args.tokens_events) < 1):
        parser.error("max-repairs mÃ¥ vÃ¦re >=0, context >=1024, timeout >=1 og alle tokenbudsjetter >=1.")
    input_dir = args.input.expanduser().resolve()
    if not input_dir.is_dir():
        parser.error(f"Input-katalogen finnes ikke: {input_dir}")
    output_dir = (args.output.expanduser().resolve() if args.output else input_dir.parent / "letter-pipeline-results")
    output_dir.mkdir(parents=True, exist_ok=True)
    sources = []
    for path in sorted(input_dir.rglob("*.txt")):
        resolved = path.resolve()
        if not resolved.is_relative_to(input_dir) or resolved.is_relative_to(output_dir):
            continue
        if path.is_file():
            sources.append(path)
    if args.limit is not None:
        sources = sources[:max(0, args.limit)]
    if not sources:
        parser.error(f"Fant ingen UTF-8 .txt-brev under {input_dir}")
    try:
        pipeline = LetterPipeline(args)
    except Exception as error:
        parser.error(f"Fikk ikke kontakt med lokal Ollama/modell {args.model}: {error}")
    print(f"letter-pipeline {__version__}: {len(sources)} brev, modell={pipeline.model}, thinking={pipeline.think}, sekvensielt.", flush=True)
    print(f"Output: {output_dir}\nSchema: JSON Schema 2020-12 + kilde-, dato- og referansekontroller.", flush=True)
    results = []
    for number, path in enumerate(sources, 1):
        print(f"\n--- Brev {number}/{len(sources)} ---", flush=True)
        try:
            results.append(pipeline.process_one(path, input_dir, output_dir))
        except Exception as error:
            results.append({"source_file": path.relative_to(input_dir).as_posix(),
                            "status": "failed", "error": f"{type(error).__name__}: {error}"})
            print(f"  FEIL: {type(error).__name__}: {error}", flush=True)
    summary = {"pipeline_version": __version__, "model": pipeline.model,
        "profile": pipeline.profile, "input_directory": str(input_dir),
        "completed_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "letters": results}
    atomic_json(output_dir / "run.json", summary)
    print(f"\nOppsummering: {sum(r['status']=='complete' for r in results)}/{len(results)} ferdig; {sum(r['status']!='complete' for r in results)} trenger arbeid.", flush=True)
    print(f"KjÃ¸ringsmanifest: {output_dir / 'run.json'}", flush=True)
    return 0 if all(r["status"] == "complete" for r in results) else 2

if __name__ == "__main__":
    raise SystemExit(main())
