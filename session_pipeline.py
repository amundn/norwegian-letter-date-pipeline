"""Sequential, session-based letter analysis using a local Ollama service."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from . import __version__, holidays, prompts_v2, schema_v2

WEEKDAYS = ("mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag")
MONTHS = {"januar": 1, "februar": 2, "mars": 3, "april": 4, "mai": 5, "juni": 6,
          "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_frontmatter(text):
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, re.S)
    if not match:
        return {}, text
    fields = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().casefold()] = value.strip().strip("\"'")
    return fields, text[match.end():]


def metadata_for(path, relative, text, date_uncertain):
    fields, body = read_frontmatter(text)
    raw_date = fields.get("date", "")
    try:
        date = dt.date.fromisoformat(raw_date).isoformat() if raw_date else None
    except ValueError:
        date = None
    certainty = fields.get("dateiscertain", fields.get("date_is_certain", "")).casefold()
    certain = bool(date) and not date_uncertain and certainty not in ("false", "no", "0", "uncertain")
    return ({"letter_id": fields.get("letter_id") or fields.get("id") or path.stem,
             "date": date, "date_is_certain": certain, "frontmatter_date": raw_date or None,
             "from": fields.get("from") or None, "to": fields.get("to") or None,
             "source_file": relative.as_posix()}, body)


def index_source(text):
    # Normalize newline encoding only; keep page breaks and all other source characters.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [{"id": f"L{number:05d}", "text": line}
            for number, line in enumerate(lines, 1)]


def line_number(line_id):
    return int(line_id[1:])


def source_quotes(line_ids, line_map, contiguous=False):
    """Build exact source passages; never use model-written quote text."""
    unknown = [item for item in line_ids if item not in line_map]
    if unknown:
        raise ValueError("Ukjente linje-ID-er: " + ", ".join(unknown))
    ordered = sorted(line_ids, key=line_number)
    chunks = []
    for line_id in ordered:
        if not chunks or line_number(line_id) != line_number(chunks[-1][-1]) + 1:
            chunks.append([])
        chunks[-1].append(line_id)
    if contiguous and len(chunks) != 1:
        raise ValueError("Linje-ID-ene må vise én sammenhengende passasje.")
    return ["\n".join(line_map[line_id] for line_id in chunk) for chunk in chunks]


def calendar_rows(base_date, before, after):
    if not base_date:
        return []
    base = dt.date.fromisoformat(base_date)
    return [{"date": (base + dt.timedelta(days=offset)).isoformat(),
             "weekday": WEEKDAYS[(base + dt.timedelta(days=offset)).weekday()]}
            for offset in range(-before, after + 1)]


def parse_source_date(text, base_date=None, base_status="unresolved"):
    """Parse a source-written day/month; never promote a guessed year to fact."""
    matches = []
    for match in re.finditer(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", text):
        matches.append((int(match[3]), int(match[2]), int(match[1]), True))
    cleaned = re.sub(r"(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?!\d)", " ", text)
    for match in re.finditer(r"(?<!\d)(\d{1,2})\s*[/.-]\s*(\d{1,2})(?:\s*[/.-]\s*(\d{2,4}))?(?!\d)", cleaned):
        matches.append((int(match[1]), int(match[2]), int(match[3]) if match[3] else None, bool(match[3])))
    month_names = "|".join(MONTHS)
    for match in re.finditer(r"(?i)(?<!\d)(\d{1,2})\.?\s+(" + month_names + r")(?:\s+(\d{2,4}))?\b", text):
        matches.append((int(match[1]), MONTHS[match[2].casefold()], int(match[3]) if match[3] else None, bool(match[3])))
    if not matches:
        return None, "unresolved"
    parsed = []
    year_supplied = all(year is not None for _, _, year, _ in matches)
    anchor = dt.date.fromisoformat(base_date) if base_date else None
    for day, month, year, year_written in matches:
        if year is not None and year < 100:
            if anchor is None or anchor.year % 100 != year:
                return None, "unresolved"
            year = anchor.year
            year_written = False
        if year is None:
            if anchor is None:
                return None, "unresolved"
            year = anchor.year
        try:
            parsed.append((dt.date(year, month, day).isoformat(), year_written))
        except ValueError:
            return None, "unresolved"
    if len({value for value, _ in parsed}) != 1:
        return None, "unresolved"
    result = parsed[0][0]
    if all(written for _, written in parsed):
        return result, "explicit"
    if base_status == "explicit" and (year_supplied or re.search(r"\bi\s*år\b", text, re.I)):
        return result, "derived"
    if base_status in ("explicit", "hypothesis"):
        return result, "hypothesis"
    return None, "unresolved"


def explicit_dateline(rows, metadata):
    nonempty = [row["text"].strip() for row in rows if row["text"].strip()]
    if not nonempty:
        return None
    first = nonempty[0]
    if not re.fullmatch(r"(?:[A-Za-zÆØÅæøå .-]{1,40}\s+)?\d{1,2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{2,4}\.?", first):
        return None
    value, basis = parse_source_date(first, metadata.get("date"),
                                     "explicit" if metadata.get("date_is_certain") else "unresolved")
    return value if basis in ("explicit", "derived") else None


def date_result(value=None, basis="unresolved", reason="Ingen entydig dato i kilden.", candidates=()):
    possibilities = set(candidates)
    if value and basis == "hypothesis":
        possibilities.add(value)
    return {"date": value if basis in ("explicit", "derived") else None,
            "suggested_date": value if basis == "hypothesis" else None,
            "date_basis": basis, "date_candidates": sorted(possibilities), "reason": reason}


def weekday_day_number(text):
    """Identify one weekday followed by a day number without a written month."""
    weekdays = "|".join(WEEKDAYS)
    pattern = r"\b(" + weekdays + r")\s+(?:den\s+)?(\d{1,2})(?:\.(?!\d))?(?![\d./-])"
    matches = list(re.finditer(pattern, text, re.I))
    if len(matches) != 1:
        return None
    match = matches[0]
    # An explicitly written month/year needs the ordinary full-date parser.
    following = text[match.end():]
    if re.match(r"\s*(?:" + "|".join(MONTHS) + r"|\d{2,4}\b)", following, re.I):
        return None
    if re.search(r"\b(?:neste|forrige|sist(?:e)?)\s+(?:måned|år)\b", text, re.I):
        return None
    weekday, number = match.group(1).casefold(), int(match.group(2))
    return (weekday, number) if 1 <= number <= 31 else None


def weekday_day_candidate(anchor, weekday, number):
    """Check the weekday before accepting the anchor month as a hypothesis."""
    if not anchor:
        return None
    day = dt.date.fromisoformat(anchor)
    try:
        candidate = dt.date(day.year, day.month, number)
    except ValueError:
        return None
    return candidate.isoformat() if candidate.weekday() == WEEKDAYS.index(weekday) else None


def resolve_references(references, base, previous_references=()):
    """Resolve only source-verifiable dates and simple relations in Python."""
    ref_map = {ref["id"]: ref for ref in previous_references}
    ref_map.update({ref["id"]: ref for ref in references})
    resolved = {ref["id"]: {**{key: ref[key] for key in ("date", "suggested_date", "date_basis", "reason")},
                            "date_candidates": ref.get("date_candidates", [])} for ref in previous_references}
    visiting = set()
    possible_bases = sorted(set(([base["date"]] if base["date"] else []) + base.get("alternatives", [])))
    markers = {"session_day": r"\b(?:i\s*dag|idag)\b",
               "previous_day": r"\b(?:i\s*g[åa]r|ig[åa]r)(?:kveld(?:en)?|morges)?\b",
               "next_day": r"\b(?:i\s*morgen|imorgen)\b"}

    def resolve(ref_id):
        if ref_id in resolved:
            return resolved[ref_id]
        if ref_id in visiting:
            return date_result(reason="Syklus i referansene; datoen er uløst.")
        visiting.add(ref_id)
        ref = ref_map[ref_id]
        text = ref["quote"].casefold()
        kind = ref["kind"]
        result = date_result()
        holiday = holidays.identify_holiday(text) if kind in ("other", "explicit_date") else None
        if holiday:
            key, label = holiday
            years = {int(year) for year in re.findall(r"(?<!\d)(?:18|19|20)\d{2}(?!\d)", text)}
            if len(years) == 1:
                year = next(iter(years))
                try:
                    value = holidays.holiday_date(key, year).isoformat()
                except ValueError:
                    result = date_result(reason=f"{label}: historisk datering for {year} er ikke støttet.")
                else:
                    result = date_result(value, "hypothesis",
                        f"{label} beregnet for årstallet {year} i sitatet; kontroller at årstallet hører til merkedagen.")
            elif not years and base["date"]:
                anchor_year = dt.date.fromisoformat(base["date"]).year
                possible_years = sorted({dt.date.fromisoformat(day).year + offset
                    for day in possible_bases for offset in (-1, 0, 1)
                    if 1583 <= dt.date.fromisoformat(day).year + offset <= 4099})
                possible_dates = []
                for year in possible_years:
                    try:
                        possible_dates.append(holidays.holiday_date(key, year).isoformat())
                    except ValueError:
                        continue
                result = date_result(reason=f"{label}: årstallet er uløst; mulige datoer rundt øktåret {anchor_year} er ikke uttømmende.",
                                     candidates=possible_dates)
            else:
                result = date_result(reason=f"{label}: årstallet er uløst.")
        elif kind in ("explicit_date", "other") and (weekday_day := weekday_day_number(text)):
            weekday, number = weekday_day
            possible_dates = {candidate for day in possible_bases
                              if (candidate := weekday_day_candidate(day, weekday, number))}
            value = weekday_day_candidate(base["date"], weekday, number)
            if value:
                result = date_result(value, "hypothesis",
                    f"{weekday.capitalize()} {number}. stemmer med {value}; måned og år er antatt fra øktdato {base['date']}.",
                    possible_dates)
            elif possible_dates:
                result = date_result(reason="Ukedag og dagnummer passer noen alternative øktmåneder; datoen er uløst.",
                                     candidates=possible_dates)
            else:
                result = date_result(reason="Ukedag og dagnummer kunne ikke bekreftes i øktmåneden; datoen er uløst.")
        elif kind == "explicit_date":
            value, basis = parse_source_date(text, base["date"], base["status"])
            possible_dates = set()
            for possible_base in possible_bases:
                possible_date, _ = parse_source_date(text, possible_base, "hypothesis")
                if possible_date:
                    possible_dates.add(possible_date)
            if value:
                explanation = (f"Datoen {value} står i kilden med årstall." if basis == "explicit" else
                               f"Datoen {value} er tolket med år fra øktdato {base['date']}." if basis == "derived" else
                               f"Dag og måned er i kilden; årstallet er et usikkert forslag fra øktdato {base['date']}.")
                result = date_result(value, basis, explanation, possible_dates if basis == "hypothesis" else ())
            elif possible_dates:
                result = date_result(reason="Årstallet avhenger av uløste øktdatoer.", candidates=possible_dates)
        elif kind in markers or (kind == "other" and re.search(
                r"\b(?:i\s*g[åa]r|ig[åa]r)\s*,?\s*nei\s+søndag\b", text, re.I)):
            found = [name for name, pattern in markers.items() if re.search(pattern, text, re.I)]
            corrected = bool(re.search(r"\bnei\b", text, re.I))
            sunday_correction = bool(re.search(
                r"\b(?:i\s*g[åa]r|ig[åa]r)\s*,?\s*nei\s+søndag\b", text, re.I))
            if kind in ("previous_day", "other") and sunday_correction and base["date"]:
                anchor_day = dt.date.fromisoformat(base["date"])
                days_since_sunday = (anchor_day.weekday() + 1) % 7 or 7
                value = (anchor_day - dt.timedelta(days=days_since_sunday)).isoformat()
                possible_dates = set()
                for possible_base in possible_bases:
                    possible_day = dt.date.fromisoformat(possible_base)
                    offset = (possible_day.weekday() + 1) % 7 or 7
                    possible_dates.add((possible_day - dt.timedelta(days=offset)).isoformat())
                basis = "hypothesis" if base["status"] == "hypothesis" else "derived"
                result = date_result(value, basis,
                    f"‘I går, nei søndag’ viser til søndagen før øktdato {base['date']}: {value}.",
                    possible_dates if basis == "hypothesis" else ())
            elif found == [kind] and not corrected:
                offset = {"session_day": 0, "previous_day": -1, "next_day": 1}[kind]
                possible_dates = {(dt.date.fromisoformat(day) + dt.timedelta(days=offset)).isoformat()
                                  for day in possible_bases}
                if base["date"]:
                    value = (dt.date.fromisoformat(base["date"]) + dt.timedelta(days=offset)).isoformat()
                    basis = "derived" if base["status"] == "explicit" else "hypothesis"
                    label = "Sikker" if base["status"] == "explicit" else "Antatt"
                    result = date_result(value, basis, f"{label} øktdato {base['date']} med dagforskyvning {offset} gir {value}.",
                                         possible_dates if basis == "hypothesis" else ())
                elif possible_dates:
                    result = date_result(reason="Datoen avhenger av uløste øktdatoer.", candidates=possible_dates)
        elif kind == "previous_weekday":
            result = date_result(reason="Relativ ukedag kan være tvetydig og må vurderes i konteksten.")
        elif kind == "relative_reference" and ref.get("anchor_id") in ref_map:
            offset = ref.get("day_offset")
            phrase = r"\bdagen\s+før\b" if offset == -1 else r"\bdagen\s+etter\b"
            if offset in (-1, 1) and re.search(phrase, text, re.I) and not re.search(r"\bnei\b", text):
                anchor = resolve(ref["anchor_id"])
                anchor_date = anchor["date"] or anchor["suggested_date"]
                anchor_candidates = set(anchor.get("date_candidates", []))
                if anchor_date:
                    anchor_candidates.add(anchor_date)
                possible_dates = {(dt.date.fromisoformat(day) + dt.timedelta(days=offset)).isoformat()
                                  for day in anchor_candidates}
                if anchor_date:
                    value = (dt.date.fromisoformat(anchor_date) + dt.timedelta(days=offset)).isoformat()
                    basis = "hypothesis" if anchor["date_basis"] == "hypothesis" else "derived"
                    qualifier = "hypotetisk " if basis == "hypothesis" else ""
                    result = date_result(value, basis, f"Fra {qualifier}{ref['anchor_id']} ({anchor_date}) med dagforskyvning {offset} til {value}.",
                                         possible_dates if basis == "hypothesis" else ())
                elif possible_dates:
                    result = date_result(reason=f"Datoen avhenger av uløst referanse {ref['anchor_id']}.", candidates=possible_dates)
        visiting.remove(ref_id)
        resolved[ref_id] = result
        return result

    return [{**ref, **resolve(ref["id"])} for ref in references]


def weekday_event_candidates(event, reference, base):
    """Suggest possible weeks only when a cited weekday and direction agree.

    The model's occurrence field is useful evidence, but is insufficient alone:
    the event citation must also contain an unambiguous past or future verb.
    These are candidates, not a claim that the closest week is correct.
    """
    if reference.get("date_basis") != "unresolved" or not base:
        return [], None
    quote = event.get("quote", "").casefold()
    ref_quote = reference.get("quote", "").casefold()
    weekday_pattern = r"\b(?:" + "|".join(WEEKDAYS) + r")\b"
    weekdays = re.findall(weekday_pattern, quote)
    ref_weekdays = re.findall(weekday_pattern, ref_quote)
    if len(weekdays) != 1 or ref_weekdays != weekdays:
        return [], None
    if event.get("line_ids") and reference.get("line_ids") and not (
            set(event["line_ids"]) & set(reference["line_ids"])):
        return [], None
    # Avoid drawing a conclusion from negation, conditionals or mixed tenses.
    if re.search(r"\b(?:ikke|ingen|aldri|dersom|hvis|om|kanskje)\b", quote):
        return [], None
    past = bool(re.search(r"\b(?:var|ble|fikk|kom|reiste|ringte|besøkte|skrev|støpte|hadde)\b", quote))
    future = bool(re.search(r"\b(?:skal|kommer|reiser|blir|vil)\b", quote))
    if past == future:
        return [], None
    direction = "før" if past and event.get("occurrence") == "happened" else (
        "etter" if future and event.get("occurrence") == "planned" else None)
    if direction is None:
        return [], None
    target = WEEKDAYS.index(weekdays[0])
    anchors = set(base.get("alternatives", []))
    if base.get("date"):
        anchors.add(base["date"])
    candidates = set()
    for anchor in anchors:
        day = dt.date.fromisoformat(anchor)
        distance = ((day.weekday() - target) % 7 if direction == "før"
                    else (target - day.weekday()) % 7)
        # "sist" and "forrige" exclude the current day; a bare weekday may
        # refer to earlier or later on the session day itself.
        if distance == 0 and direction == "før" and re.search(r"\b(?:sist|forrige)\b", ref_quote):
            distance = 7
        sign = -1 if direction == "før" else 1
        for weeks in (0, 1):
            candidates.add((day + dt.timedelta(days=sign * (distance + 7 * weeks))).isoformat())
    return sorted(candidates), direction if candidates else None


def attach_event_date(event, references, base=None):
    """Copy date conclusions from the checked references, keeping ambiguity explicit."""
    ref_map = {ref["id"]: ref for ref in references}
    selected = []
    for ref_id in event["time_reference_ids"]:
        ref = ref_map[ref_id]
        # A time cue beginning a new, unfinished sentence may refer to the
        # next action rather than to the event immediately preceding it.
        dangling = re.search(r"[.!?]\s+((?:i\s*g[åa]r|ig[åa]r)(?:\s*kveld(?:en)?)?)[,:]?\s*$",
                             event.get("quote", ""), re.I)
        if dangling and dangling.group(1).casefold() in ref.get("quote", "").casefold():
            ref = {**ref, **date_result(reason="Tidsuttrykket starter en ny setning uten hendelsen det gjelder; koblingen er uavklart.")}
        selected.append(ref)
    preferred_dates = {ref["date"] or ref["suggested_date"] for ref in selected
                       if ref["date"] or ref["suggested_date"]}
    candidates = sorted(preferred_dates | {day for ref in selected for day in ref.get("date_candidates", [])})
    directional = []
    for ref in selected:
        possible, direction = weekday_event_candidates(event, ref, base)
        if possible:
            candidates = sorted(set(candidates) | set(possible))
            directional.append((ref["id"], direction))
    unresolved = not selected or any(ref["date_basis"] == "unresolved" for ref in selected)
    if not unresolved and len(preferred_dates) == 1:
        basis = ("hypothesis" if any(ref["date_basis"] == "hypothesis" for ref in selected)
                 else "derived" if any(ref["date_basis"] == "derived" for ref in selected) else "explicit")
        date = date_result(next(iter(preferred_dates)), basis, "Dato hentet fra validerte tidsreferanser.")
        review = ("Datoen bygger på en usikker forutsetning; kontroller koblingen og deltakerne i kilden."
                  if basis == "hypothesis" else "Kontroller tidsreferansen og deltakerne mot kilden.")
    else:
        date = date_result(reason="Tidsreferansene gir ikke én entydig dato.")
        review = "Kontroller dato, tidsreferanser og deltakere mot brevet."
    explanation = "; ".join(f"{ref['id']}: {ref['reason']}" for ref in selected) or "Ingen tidsreferanse er knyttet til hendelsen."
    if directional:
        explanation += (" Mulige ukedager " + ", ".join(f"{ref_id} {direction}" for ref_id, direction in directional)
                        + " øktdatoen er beregnet fra verb og hendelsesstatus; listen er ikke uttømmende.")
    if date["date_basis"] == "unresolved" and selected:
        explanation = date["reason"] + " " + explanation
    return {**event, "date": date["date"], "suggested_date": date["suggested_date"],
            "date_basis": date["date_basis"], "date_candidates": candidates,
            "time_expression": selected[0]["quote"] if selected else "",
            "date_reason": explanation, "review_note": review}


def letter_task_export(result):
    """Project the detailed result to the fields requested by letter-task.txt."""
    fields = ("title", "date", "date_candidates", "time_expression", "date_reason", "occurrence",
              "people", "location", "category", "quote", "review_note")
    events = []
    for event in result["events"]:
        item = {field: event[field] for field in fields}
        item["date_basis"] = event["date_basis"] if event["date_basis"] in ("explicit", "derived") else "uncertain"
        events.append(item)
    return {"letter_id": result["letter_id"], "events": events, "warnings": result["warnings"]}


def schema_errors(value, shape):
    validator = Draft202012Validator(shape, format_checker=FormatChecker())
    items = sorted(validator.iter_errors(value), key=lambda error: (list(map(str, error.absolute_path)), error.message))
    return ["/" + "/".join(str(part) for part in error.absolute_path) + ": " + error.message for error in items]


def request_json(base_url, path, payload, timeout):
    request = urllib.request.Request(base_url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Ollama HTTP {error.code}: {detail}") from error


class Runner:
    def __init__(self, args):
        self.args = args
        self.model = args.model.removeprefix("local/")
        info = request_json(args.base_url, "/api/show", {"model": self.model}, 60)
        self.think = ("thinking" in info.get("capabilities", [])) if args.thinking == "auto" else args.thinking == "on"
        self.profile = {"pipeline_version": __version__, "model": self.model,
                        "model_fingerprint": digest(canonical(info).encode("utf-8"))[:16],
                        "base_url": args.base_url.rstrip("/"), "context": args.context,
                        "thinking": self.think, "temperature": args.temperature,
                        "max_repairs": args.max_repairs, "json_mode": not args.no_json_mode,
                        "calendar_before_days": args.calendar_before_days,
                        "calendar_after_days": args.calendar_after_days,
                        "tokens_scan": args.tokens_scan, "tokens_extract": args.tokens_extract,
                        "tokens_base": args.tokens_base, "tokens_audit": args.tokens_audit,
                        "tokens_events": args.tokens_events, "event_chunk_lines": args.event_chunk_lines,
                        "event_overlap_lines": args.event_overlap_lines}
        self.identity = {key: self.profile[key] for key in
                         ("pipeline_version", "model", "model_fingerprint", "base_url", "context", "thinking", "temperature", "json_mode")}
        self.profile_hash = digest(canonical(self.identity).encode("utf-8"))[:10]

    def completion(self, prompt, budget, stage=None):
        request = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                   "stream": False, "keep_alive": "20m", "think": self.think,
                   "options": {"num_ctx": self.args.context, "num_predict": budget,
                               "temperature": self.args.temperature, "seed": 42}}
        if not self.args.no_json_mode:
            if stage in prompts_v2.SCHEMAS:
                format_schema = json.loads(json.dumps(prompts_v2.SCHEMAS[stage]))
                format_schema.pop("$schema", None)
                request["format"] = format_schema
            else:
                request["format"] = "json"
        start = time.perf_counter()
        response = request_json(self.args.base_url, "/api/chat", request, self.args.timeout)
        return {"content": response.get("message", {}).get("content") or "",
                "done_reason": response.get("done_reason"), "generated_tokens": response.get("eval_count"),
                "prompt_tokens": response.get("prompt_eval_count"),
                "seconds": round(time.perf_counter() - start, 2)}

    def run_stage(self, letter_dir, label, stage, context, custom_validator, budget):
        prompt = prompts_v2.build(stage, context)
        fingerprint_data = {"prompt": prompt, "profile": self.identity, "budget": budget}
        if stage == "events" and not self.args.no_json_mode:
            fingerprint_data["format_schema"] = prompts_v2.SCHEMAS[stage]
        fingerprint = digest(canonical(fingerprint_data).encode("utf-8"))[:12]
        folder = letter_dir / f"{label}_{fingerprint}"
        folder.mkdir(parents=True, exist_ok=True)
        state_path = folder / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"attempts": []}
        state.update({"stage": stage, "label": label, "budget": budget,
                      "prompt_sha256": digest(prompt.encode("utf-8")), "identity": self.identity})
        if state.get("accepted"):
            print(f"  {label}: gjenbruker validert checkpoint", flush=True)
            return state["result"], []
        if state["attempts"] and not self.args.retry_failed:
            errors = state.get("validation_errors", ["Tidligere forsøk feilet."])
            print(f"  {label}: tidligere forsøk krever gjennomgang (--retry-failed for nytt forsøk)", flush=True)
            return None, errors
        previous, errors = None, []
        for turn in range(self.args.max_repairs + 1):
            current = prompt if previous is None else prompts_v2.repair(stage, context, previous, errors)
            number = len(state["attempts"])
            print(f"  {label}: modellkall {turn + 1} (maks {budget} tokens)", flush=True)
            (folder / f"attempt-{number:02d}-prompt.txt").write_text(current, encoding="utf-8")
            try:
                answer = self.completion(current, budget, stage)
                if answer["done_reason"] == "length":
                    errors = [f"Modellen nådde tokenbudsjettet på {budget}; fasen ble avbrutt uten reparasjonskall."]
                    parsed = None
                else:
                    try:
                        parsed = json.loads(answer["content"], parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                        errors = schema_errors(parsed, prompts_v2.SCHEMAS[stage])
                        if not errors:
                            errors = custom_validator(parsed)
                    except (json.JSONDecodeError, ValueError) as error:
                        parsed, errors = None, [f"Ugyldig JSON eller kildehenvisning: {error}"]
            except (OSError, RuntimeError, ValueError) as error:
                answer = {"content": "", "done_reason": "error", "seconds": 0,
                          "generated_tokens": None, "prompt_tokens": None}
                parsed, errors = None, [f"Modellkallet feilet: {error}"]
            attempt = {**answer, "validation_errors": errors,
                       "completed_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
            state["attempts"].append(attempt)
            state.update({"stage": stage, "accepted": parsed is not None and not errors,
                          "result": parsed if parsed is not None and not errors else None,
                          "validation_errors": errors})
            write_json(folder / f"attempt-{number:02d}.json", attempt)
            write_json(state_path, state)
            if state["accepted"]:
                write_json(folder / "accepted.json", parsed)
                print(f"  {label}: validert ({answer['seconds']} s)", flush=True)
                return parsed, []
            print(f"  {label}: " + " | ".join(errors[:3]), flush=True)
            if answer["done_reason"] in ("length", "error"):
                break
            previous = answer["content"]
        return None, errors


def validate_scan(value, rows):
    by_id = {row["id"]: row["text"] for row in rows}
    first = next((row["id"] for row in rows if row["text"].strip()), None)
    ids = [item["start_line_id"] for item in value["new_sessions"]]
    if any(item not in by_id or not by_id[item].strip() for item in ids):
        return ["Hver øktstart må være en eksisterende, ikke-tom kildelinje."]
    if len(ids) != len(set(ids)) or ids != sorted(ids, key=line_number):
        return ["Øktstarter må være unike og stå i kilderekkefølge."]
    if any(line_number(item) <= line_number(first) for item in ids):
        return ["new_sessions skal bare inneholde økter etter brevets første linje."]
    return []


def validate_references(value, line_map, previous_ids=()):
    refs = value["references"]
    ids = [ref["id"] for ref in refs]
    known_ids = set(ids) | set(previous_ids)
    errors = []
    if len(ids) != len(set(ids)):
        errors.append("Tidsreferanse-ID-er må være unike.")
    for ref in refs:
        try:
            if not source_quotes(ref["line_ids"], line_map, contiguous=True)[0].strip():
                errors.append(f"{ref['id']}: kildelinjene er tomme.")
        except ValueError as error:
            errors.append(f"{ref['id']}: {error}")
        if ref["kind"] == "relative_reference":
            if ref["anchor_id"] not in known_ids or ref["anchor_id"] == ref["id"] or ref["day_offset"] not in (-1, 1):
                errors.append(f"{ref['id']}: relative_reference krever annen kjent anchor_id og day_offset -1 eller 1.")
        elif ref["anchor_id"] is not None or ref["day_offset"] is not None:
            errors.append(f"{ref['id']}: anchor_id og day_offset skal være null for denne typen.")
    graph = {ref["id"]: ref["anchor_id"] for ref in refs if ref["kind"] == "relative_reference"}
    for start in graph:
        seen, current = set(), start
        while current in graph:
            if current in seen:
                errors.append(f"{start}: syklus i tidsreferansene.")
                break
            seen.add(current)
            current = graph[current]
    return errors


def validate_base(value, line_map):
    errors = []
    dates = [item["date"] for item in value["candidates"]]
    if len(dates) != len(set(dates)):
        errors.append("Kandidatdatoene må være unike.")
    if value["preferred_date"] and value["preferred_date"] not in dates:
        errors.append("preferred_date må finnes blant candidates.")
    for index, candidate in enumerate(value["candidates"], 1):
        for key in ("support_line_ids", "against_line_ids"):
            try:
                if any(not quote.strip() for quote in source_quotes(candidate[key], line_map)):
                    errors.append(f"Kandidat {index} {key}: tomme kildelinjer kan ikke brukes som belegg.")
            except ValueError as error:
                errors.append(f"Kandidat {index} {key}: {error}")
    return errors


def has_specific_base_evidence(quotes):
    """A vague interval alone cannot select one exact session date."""
    text = " ".join(quotes).casefold()
    weekdays = r"\b(?:mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\b"
    dates = r"(?<!\d)\d{1,2}\s*[/.-]\s*\d{1,2}(?!\d)|\b\d{1,2}\.?\s+(?:" + "|".join(MONTHS) + r")\b"
    exact_relation = r"\b(?:i\s*g[åa]r|ig[åa]r|i\s*morgen|imorgen|dagen\s+før|dagen\s+etter)\b"
    numeric_interval = bool(re.search(r"\b\d+\s+(?:dager?|uker?)\b", text))
    if re.search(r"\b(?:nesten|omtrent|cirka|rundt|over|under|ca\.?)[\s]+\d+\s+(?:dager?|uker?)\b", text):
        numeric_interval = False
    return bool(re.search(weekdays, text) or re.search(dates, text) or re.search(exact_relation, text) or numeric_interval)


def validate_audit(value, line_map, candidate_dates):
    errors = []
    dates = [item["date"] for item in value["checks"]]
    if len(dates) != len(set(dates)) or set(dates) != set(candidate_dates):
        errors.append("Motprøven må ha nøyaktig én kontroll per kandidatdato.")
    for item in value["checks"]:
        for key in ("support_line_ids", "against_line_ids"):
            try:
                if any(not quote.strip() for quote in source_quotes(item[key], line_map)):
                    errors.append(f"{item['date']} {key}: tomme linjer kan ikke brukes som belegg.")
            except ValueError as error:
                errors.append(f"{item['date']} {key}: {error}")
    return errors


def grounded_audit_verdict(item):
    verdict = item["verdict"]
    if verdict == "supported" and not item["support_line_ids"]:
        return "undetermined"
    if verdict == "contradicted" and not item["against_line_ids"]:
        return "undetermined"
    return verdict


def validate_events(value, line_map, known_refs, core_start=None, core_end=None, context_start=None):
    errors = []
    for index, event in enumerate(value["events"], 1):
        try:
            quote = source_quotes(event["line_ids"], line_map, contiguous=True)[0]
            if not quote.strip():
                errors.append(f"Hendelse {index}: kildelinjene er tomme.")
            normalized = normalize_evidence_text(quote)
            for person in event["people"]:
                mention = normalize_evidence_text(person)
                if mention not in normalized:
                    errors.append(f"Hendelse {index}: personomtalen {person!r} finnes ikke i kildesitatet.")
            if event["location"]:
                place = normalize_evidence_text(event["location"])
                if place not in normalized:
                    errors.append(f"Hendelse {index}: stedet {event['location']!r} finnes ikke i kildesitatet.")
        except ValueError as error:
            errors.append(f"Hendelse {index}: {error}")
        unknown = [ref for ref in event["time_reference_ids"] if ref not in known_refs]
        if unknown:
            errors.append(f"Hendelse {index}: ukjente tidsreferanser {', '.join(unknown)}.")
        if core_start is not None and event["line_ids"]:
            selected = [line_number(line_id) for line_id in event["line_ids"] if line_id in line_map]
            core_first, core_last = line_number(core_start), line_number(core_end)
            context_first = line_number(context_start) if context_start else core_first
            has_core_line = any(core_first <= number <= core_last for number in selected)
            if selected and (min(selected) < context_first or not has_core_line):
                errors.append(f"Hendelse {index}: kildelinjene må være i vinduet og minst én linje må være mellom {core_start} og {core_end}.")
    return errors


def ground_optional_event_fields(value, line_map):
    """Drop unsupported optional people/places and attach a review note to the event."""
    changed = []
    for index, event in enumerate(value["events"], 1):
        try:
            quote = source_quotes(event["line_ids"], line_map, contiguous=True)[0]
        except ValueError:
            continue  # Keep malformed citations for the validator to reject.
        normalized = normalize_evidence_text(quote)
        omitted_people = [person for person in event["people"]
                          if normalize_evidence_text(person) not in normalized]
        if omitted_people:
            event["people"] = [person for person in event["people"] if person not in omitted_people]
        omitted_location = None
        if event["location"] and normalize_evidence_text(event["location"]) not in normalized:
            omitted_location = event["location"]
            event["location"] = None
        notes = []
        if omitted_people:
            notes.append("personomtale uten ordrett støtte i sitatet fjernet: " + ", ".join(omitted_people))
        if omitted_location:
            notes.append("sted uten ordrett støtte i sitatet utelatt: " + omitted_location)
        if notes:
            note = "; ".join(notes)
            event["_evidence_review_note"] = note
            changed.append(f"Hendelse {index}: {note}.")
    return changed


def normalize_evidence_text(text):
    """Compare OCR citations while tolerating words split by a line-end hyphen."""
    value = unicodedata.normalize("NFC", text).casefold()
    value = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", value)
    value = re.sub(r"(?<=\w)-(?=\w)", "", value)
    return " ".join(value.split())


def event_windows(rows, chunk_size, overlap):
    windows = []
    for start in range(0, len(rows), chunk_size):
        stop = min(start + chunk_size, len(rows))
        if not any(row["text"].strip() for row in rows[start:stop]):
            continue
        windows.append({"core_start": rows[start]["id"], "core_end": rows[stop - 1]["id"],
                        "rows": rows[max(0, start - overlap):min(len(rows), stop + overlap)]})
    return windows


def possible_duplicate_warnings(session_id, events):
    warnings = []
    for index, first in enumerate(events):
        for second in events[index + 1:]:
            overlap = set(first["line_ids"]) & set(second["line_ids"])
            shared_refs = set(first["time_reference_ids"]) & set(second["time_reference_ids"])
            same_title = first["title"].casefold().strip() == second["title"].casefold().strip()
            smaller_citation = min(len(set(first["line_ids"])), len(set(second["line_ids"])))
            citation_overlap = len(overlap) / smaller_citation if smaller_citation else 0
            if overlap and (shared_refs or same_title or citation_overlap >= 0.7):
                warnings.append(f"{session_id}: {first['id']} og {second['id']} kan beskrive samme hendelse; kontroller kilden.")
    return warnings


def discard_context_only_events(value, core_start, core_end):
    """Keep an event only in the window whose core contains one of its cited lines."""
    first, last = line_number(core_start), line_number(core_end)
    events = value["events"]
    retained = [event for event in events
                if any(first <= line_number(line_id) <= last for line_id in event["line_ids"])]
    discarded = len(events) - len(retained)
    value["events"] = retained
    return discarded


def partition_sessions(rows, scan):
    first = next(row["id"] for row in rows if row["text"].strip())
    starts = [first] + [item["start_line_id"] for item in scan["new_sessions"]]
    result = []
    for index, start in enumerate(starts):
        end = line_number(starts[index + 1]) - 1 if index + 1 < len(starts) else len(rows)
        section = [row for row in rows if line_number(start) <= line_number(row["id"]) <= end]
        result.append({"id": f"s{index + 1}", "start_line_id": start,
                       "end_line_id": section[-1]["id"], "rows": section})
    return result


def boundary_warnings(rows, sections):
    starts = [line_number(section["start_line_id"]) for section in sections[1:]]
    warnings = []
    for row in rows:
        if re.search(r"\b(?:hallo|hei)\s+igjen\b", row["text"], re.I):
            position = line_number(row["id"])
            if not any(0 <= position - start <= 2 for start in starts):
                warnings.append(f"{row['id']}: mulig ny skriveøkt ble ikke markert; kontroller øktgrensene.")
    return warnings


def contradicts_elapsed_week(candidate, previous, rows):
    opening = " ".join(row["text"] for row in rows[:8]).casefold()
    return (candidate == previous and bool(re.search(r"\bnesten\s+(?:en|ei|en\s+hel)\s+uke\b", opening)))


def candidate_chronology(candidate, previous_certain, rows):
    if previous_certain is None:
        return "not_checked"
    if candidate < previous_certain or contradicts_elapsed_week(candidate, previous_certain, rows):
        return "conflict"
    return "consistent"


def uncovered_time_cues(rows, references):
    """Flag possible missed cues without inventing references or dates."""
    covered = {line_id for ref in references for line_id in ref["line_ids"]}
    pattern = (r"\b(?:i\s*g[åa]r|ig[åa]r|i\s*dag|idag|i\s*morgen|imorgen|"
               r"mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag|dagen\s+før|dagen\s+etter)\b"
               r"|(?<!\d)\d{1,2}\s*[/.-]\s*\d{1,2}(?!\d)"
               r"|\b(?:nesten|over|under|omtrent|cirka|rundt)\s+(?:en|ei|et|\d+)\s+"
               r"(?:dager?|uker?|måneder?|år)\b"
               r"|\b(?:påske\w*|palmesøndag|skjærtorsdag|langfredag|pinse\w*|"
               r"himmelfartsdag|fastelavn\w*|bededag|allehelgens\w*|advent|"
               r"\w*julaften|juledag|nyttårs\w*|sommerdagen|olsok\w*|"
               r"sankthans\w*|st\.?\s*hans\w*|morsdag\w*|årdag\w*|nasjonaldagen)\b")
    return [row["id"] for row in rows if row["id"] not in covered and re.search(pattern, row["text"], re.I)]


def unresolved_base(source="ingen sikker dato"):
    return {"date": None, "status": "unresolved", "alternatives": [],
            "support_quotes": [], "against_quotes": [], "candidates": [], "source": source}


def _matching_line_ids(rows, pattern, max_rows=None, span=2):
    """Find a short source passage matching text that may cross line breaks."""
    search_rows = rows[:max_rows] if max_rows is not None else rows
    for start in range(len(search_rows)):
        for end in range(start + 1, min(len(search_rows), start + span) + 1):
            passage = " ".join(row["text"] for row in search_rows[start:end])
            if re.search(pattern, passage, re.I):
                return [row["id"] for row in search_rows[start:end]]
    return []


def infer_nearly_week_sunday_correction(rows, anchor_date):
    """Infer a narrow date range from 'nearly a week' plus a Sunday correction.

    This intentionally handles only the observed pattern: a secure preceding
    date, an opening 'nesten ei/en uke' clue (5–6 days later), and a later
    'i går, nei søndag' correction. The correction rules out Monday, when
    Sunday would literally have been yesterday, leaving Tuesday as a hypothesis.
    """
    if not anchor_date:
        return None
    duration_lines = _matching_line_ids(
        rows, r"\bnesten\s+(?:ei|en)\s+uke\b", max_rows=8, span=3)
    correction_lines = _matching_line_ids(
        rows, r"\b(?:i\s*g[åa]r|ig[åa]r)\s*,?\s*nei\s+søndag\b", span=2)
    if not duration_lines or not correction_lines:
        return None

    anchor = dt.date.fromisoformat(anchor_date)
    dates = [anchor + dt.timedelta(days=offset) for offset in (5, 6)]
    # The correction says the Sunday reference was not simply “yesterday”.
    # Exclude the Monday candidate; keep the remaining date as a hypothesis.
    rejected = next((day for day in dates
                     if day.weekday() == 0 and (day - dt.timedelta(days=1)).weekday() == 6), None)
    remaining = [day for day in dates if day != rejected]
    if rejected is None or len(remaining) != 1:
        return None

    line_map = {row["id"]: row["text"] for row in rows}
    duration_quote = source_quotes(duration_lines, line_map, contiguous=True)[0]
    correction_quote = source_quotes(correction_lines, line_map, contiguous=True)[0]
    selected = remaining[0]
    candidates = []
    for day in dates:
        is_rejected = day == rejected
        support_ids = duration_lines if is_rejected else sorted(
            set(duration_lines + correction_lines), key=line_number)
        against_ids = correction_lines if is_rejected else []
        support_quotes = source_quotes(support_ids, line_map)
        against_quotes = source_quotes(against_ids, line_map)
        audit = {
            "verdict": "contradicted" if is_rejected else "supported",
            "reported_verdict": "contradicted" if is_rejected else "supported",
            "support_line_ids": support_ids,
            "against_line_ids": against_ids,
            "support_quotes": support_quotes,
            "against_quotes": against_quotes,
        }
        candidates.append({
            "date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()],
            "chronology": "consistent", "support_line_ids": support_ids,
            "against_line_ids": against_ids, "support_quotes": support_quotes,
            "against_quotes": against_quotes, "audit": audit,
        })
    return {
        "date": selected.isoformat(), "status": "hypothesis", "alternatives": [],
        "support_quotes": [duration_quote, correction_quote],
        "against_quotes": [], "candidates": candidates,
        "source": ("Python-regel: ‘nesten ei uke’ gir 5–6 dager etter forrige sikre dato; "
                   "‘i går, nei søndag’ utelukker mandagskandidaten."),
    }


def process_letter(runner, path, input_dir, output_dir):
    relative = path.relative_to(input_dir)
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        return {"source_file": relative.as_posix(), "status": "failed", "error": str(error)}
    metadata, body = metadata_for(path, relative, text, runner.args.date_uncertain)
    rows = index_source(body)
    if not any(row["text"].strip() for row in rows):
        return {"source_file": relative.as_posix(), "status": "failed", "error": "Brevet er tomt."}
    source_hash = digest(raw)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(metadata["letter_id"]).stem).strip("_") or "letter"
    relative_hash = digest(relative.as_posix().encode("utf-8"))[:8]
    model_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", runner.model)
    letter_dir = output_dir / f"{slug}_{relative_hash}" / source_hash[:10] / f"{runner.profile_hash}_{model_slug}"
    letter_dir.mkdir(parents=True, exist_ok=True)
    write_json(letter_dir / "metadata.json", metadata)
    write_json(letter_dir / "profile.json", runner.profile)
    write_json(letter_dir / "source-lines.json", rows)
    print(f"\n[{relative.as_posix()}] ID={metadata['letter_id']} dato={metadata['date'] or 'ukjent'}", flush=True)
    scan_context = {"metadata": metadata, "lines": rows}
    scan, errors = runner.run_stage(letter_dir, "01_scan", "scan", scan_context,
                                    lambda value: validate_scan(value, rows), runner.args.tokens_scan)
    if scan is None:
        result = {"source_file": relative.as_posix(), "letter_id": metadata["letter_id"],
                  "status": "needs_review", "failed_stages": [{"stage": "scan", "errors": errors}],
                  "checkpoint": letter_dir.relative_to(output_dir).as_posix()}
        write_json(letter_dir / "needs-review.json", result)
        return result
    sections = partition_sessions(rows, scan)
    print(f"  Fant {len(sections)} skriveøkter; analyserer dem én om gangen.", flush=True)
    all_sessions, all_refs, all_events, failed = [], [], [], []
    warnings = boundary_warnings(rows, sections)
    for section in sections:
        session_id = section["id"]
        section_rows = section["rows"]
        line_map = {row["id"]: row["text"] for row in section_rows}
        print(f"\n  Økt {session_id}: {section['start_line_id']}–{section['end_line_id']}", flush=True)
        previous_refs = [{"id": ref["id"], "quote": ref["quote"]} for ref in all_refs]
        previous_ids = {ref["id"] for ref in previous_refs}
        extract_context = {"session_id": session_id, "lines": section_rows, "previous_references": previous_refs}
        extracted, errors = runner.run_stage(letter_dir, f"02_extract_{session_id}", "extract", extract_context,
            lambda value, local=line_map, prior=previous_ids: validate_references(value, local, prior), runner.args.tokens_extract)
        raw_refs = extracted["references"] if extracted is not None else []
        if extracted is None:
            failed.append({"stage": f"extract:{session_id}", "errors": errors})
        references = []
        for ref in raw_refs:
            ordered_lines = sorted(ref["line_ids"], key=line_number)
            references.append({"id": f"{session_id}-{ref['id']}", "session_id": session_id,
                "line_ids": ordered_lines, "quote": source_quotes(ordered_lines, line_map, True)[0],
                "kind": ref["kind"],
                "anchor_id": (ref["anchor_id"] if ref["anchor_id"] in previous_ids
                              else f"{session_id}-{ref['anchor_id']}" if ref["anchor_id"] else None),
                "day_offset": ref["day_offset"]})
        direct = metadata["date"] if session_id == "s1" and metadata["date_is_certain"] else explicit_dateline(section_rows, metadata)
        if direct:
            source = "bekreftet metadata" if session_id == "s1" and metadata["date_is_certain"] else "datolinje i kilden"
            base = {"date": direct, "status": "explicit", "alternatives": [],
                    "support_quotes": [], "against_quotes": [], "candidates": [], "source": source}
        elif extracted is None:
            base = unresolved_base("tidsreferanser kunne ikke analyseres")
        else:
            anchor = metadata["date"] or next((s["base_date"]["date"] for s in reversed(all_sessions)
                                               if s["base_date"]["date"]), None)
            prior_explicit_date = next((s["base_date"]["date"] for s in reversed(all_sessions)
                                        if s["base_date"]["status"] == "explicit"), None)
            inferred_base = infer_nearly_week_sunday_correction(section_rows, prior_explicit_date)
            base_context = {"metadata": metadata, "session_id": session_id, "lines": section_rows,
                            "previous_sessions": [{"id": s["id"], "base_date": s["base_date"]} for s in all_sessions],
                            "references": [{"id": ref["id"], "quote": ref["quote"], "kind": ref["kind"]}
                                           for ref in references],
                            "calendar": calendar_rows(anchor, runner.args.calendar_before_days,
                                                      runner.args.calendar_after_days)}
            if inferred_base:
                base = inferred_base
                print(f"  {session_id}: Python-regel fant datohypotese {base['date']} fra "
                      "‘nesten ei uke’ og korrigeringen ‘i går, nei søndag’.", flush=True)
            else:
                proposal, errors = runner.run_stage(letter_dir, f"03_base_{session_id}", "base", base_context,
                    lambda value, local=line_map: validate_base(value, local), runner.args.tokens_base)
            if inferred_base:
                pass
            elif proposal is None:
                failed.append({"stage": f"base:{session_id}", "errors": errors})
                base = unresolved_base("datoforslaget kunne ikke valideres")
            else:
                audit_map = {}
                if proposal["candidates"]:
                    audit_context = {"session_id": session_id, "metadata": metadata, "lines": section_rows,
                        "previous_sessions": [{"id": s["id"], "base_date": s["base_date"]} for s in all_sessions],
                        "candidates": [{"date": candidate["date"],
                                        "weekday": WEEKDAYS[dt.date.fromisoformat(candidate["date"]).weekday()]}
                                       for candidate in proposal["candidates"]]}
                    candidate_dates = [candidate["date"] for candidate in proposal["candidates"]]
                    audit, audit_errors = runner.run_stage(letter_dir, f"03_audit_{session_id}", "audit", audit_context,
                        lambda value, local=line_map, dates=candidate_dates: validate_audit(value, local, dates),
                        runner.args.tokens_audit)
                    if audit is None:
                        failed.append({"stage": f"audit:{session_id}", "errors": audit_errors})
                    else:
                        audit_map = {item["date"]: item for item in audit["checks"]}
                audit_details = {}
                for candidate in proposal["candidates"]:
                    check = audit_map.get(candidate["date"])
                    if check is None:
                        audit_details[candidate["date"]] = {"verdict": "not_checked", "reported_verdict": "not_checked",
                            "support_line_ids": [], "against_line_ids": [], "support_quotes": [], "against_quotes": []}
                        continue
                    verdict = grounded_audit_verdict(check)
                    if verdict != check["verdict"]:
                        warnings.append(f"{session_id}: motprøven oppga {check['verdict']} for {candidate['date']} uten kildelinjer; vurderingen er uavklart.")
                    audit_details[candidate["date"]] = {"verdict": verdict, "reported_verdict": check["verdict"],
                        "support_line_ids": sorted(check["support_line_ids"], key=line_number),
                        "against_line_ids": sorted(check["against_line_ids"], key=line_number),
                        "support_quotes": source_quotes(check["support_line_ids"], line_map),
                        "against_quotes": source_quotes(check["against_line_ids"], line_map)}
                prior_certain = next((item["base_date"]["date"] for item in reversed(all_sessions)
                                      if item["base_date"]["status"] == "explicit"), None)
                candidates = [{"date": candidate["date"],
                               "weekday": WEEKDAYS[dt.date.fromisoformat(candidate["date"]).weekday()],
                               "chronology": candidate_chronology(candidate["date"], prior_certain, section_rows),
                               "support_line_ids": sorted(candidate["support_line_ids"], key=line_number),
                               "against_line_ids": sorted(candidate["against_line_ids"], key=line_number),
                               "support_quotes": source_quotes(candidate["support_line_ids"], line_map),
                               "against_quotes": source_quotes(candidate["against_line_ids"], line_map),
                               "audit": audit_details[candidate["date"]]}
                              for candidate in proposal["candidates"]]
                preferred = next((item for item in candidates if item["date"] == proposal["preferred_date"]), None)
                if preferred and preferred["chronology"] == "conflict":
                    warnings.append(f"{session_id}: foretrukket dato {preferred['date']} strider mot en tidligere sikker økt.")
                    preferred = None
                if preferred and not has_specific_base_evidence(preferred["support_quotes"]):
                    warnings.append(f"{session_id}: foretrukket dato ble fjernet fordi belegget ikke angir et spesifikt tidsanker.")
                    preferred = None
                if preferred and preferred["audit"]["verdict"] != "supported":
                    warnings.append(f"{session_id}: foretrukket dato {preferred['date']} ble ikke bekreftet i motprøven.")
                    preferred = None
                if preferred and preferred["audit"]["against_line_ids"]:
                    warnings.append(f"{session_id}: motprøven fant også innvendinger mot {preferred['date']}; øktdatoen er uløst.")
                    preferred = None
                selected_date = preferred["date"] if preferred else None
                base = {"date": selected_date,
                        "status": "hypothesis" if selected_date else "unresolved",
                        "alternatives": [item["date"] for item in candidates
                                         if item["date"] != selected_date and item["chronology"] != "conflict"
                                         and item["audit"]["verdict"] in ("supported", "undetermined")],
                        "support_quotes": preferred["support_quotes"] if preferred else [],
                        "against_quotes": preferred["against_quotes"] if preferred else [], "candidates": candidates,
                        "source": "modellforslag med kildelinjer" if selected_date else "ingen entydig dato"}
        if base["date"]:
            previous = next((item["base_date"] for item in reversed(all_sessions) if item["base_date"]["date"]), None)
            previous_certain = next((item["base_date"] for item in reversed(all_sessions)
                                     if item["base_date"]["status"] == "explicit"), None)
            if previous and base["date"] < previous["date"]:
                warnings.append(f"{session_id}: foreslått øktdato {base['date']} er før en tidligere økt {previous['date']}; kontroller kilden.")
            if (base["status"] == "hypothesis" and previous_certain and
                (base["date"] < previous_certain["date"] or
                 contradicts_elapsed_week(base["date"], previous_certain["date"], section_rows))):
                warnings.append(f"{session_id}: dato {base['date']} strider mot tidspunktet for en tidligere sikker økt {previous_certain['date']}; forslaget beholdes i kandidatlisten.")
                base["alternatives"] = [candidate for candidate in base["alternatives"] if candidate != base["date"]]
                base.update({"date": None, "status": "unresolved", "support_quotes": [], "against_quotes": [],
                             "source": "forslaget motsier datoen for en tidligere sikker økt"})
        if base["status"] == "hypothesis":
            warnings.append(f"{session_id}: øktdato {base['date']} er en hypotese som må kontrolleres mot kildene.")
        elif base["status"] == "unresolved":
            warnings.append(f"{session_id}: øktdatoen er uløst.")
        print(f"  {session_id}: øktdato {base['date'] or 'uløst'} ({base['status']}, {base['source']})", flush=True)
        all_sessions.append({"id": session_id, "start_line_id": section["start_line_id"],
                             "end_line_id": section["end_line_id"], "base_date": base})
        resolved_refs = resolve_references(references, base, all_refs)
        all_refs.extend(resolved_refs)
        missed = uncovered_time_cues(section_rows, references) if extracted is not None else []
        if missed:
            sample = ", ".join(missed[:20])
            extra = f" og {len(missed) - 20} til" if len(missed) > 20 else ""
            warnings.append(f"{session_id}: mulige oversette tidsuttrykk på {sample}{extra}; kontroller kilden.")
        unresolved_count = sum(ref["date_basis"] == "unresolved" for ref in resolved_refs)
        if unresolved_count:
            warnings.append(f"{session_id}: {unresolved_count} tidsreferanser har uløst dato.")
        hypothesis_count = sum(ref["date_basis"] == "hypothesis" for ref in resolved_refs)
        if hypothesis_count:
            warnings.append(f"{session_id}: {hypothesis_count} tidsreferanser har foreslått, usikker dato.")
        if extracted is None:
            continue
        windows = event_windows(section_rows, runner.args.event_chunk_lines, runner.args.event_overlap_lines)
        session_events = []
        event_failures = 0
        if len(windows) > 1:
            print(f"  {session_id}: hendelsesuttrekk i {len(windows)} overlappende vinduer.", flush=True)
        for window_number, window in enumerate(windows, 1):
            core_rows = [row for row in window["rows"] if line_number(window["core_start"]) <= line_number(row["id"]) <= line_number(window["core_end"])]
            if not any(row["text"].strip() for row in core_rows):
                continue
            window_map = {row["id"]: row["text"] for row in window["rows"]}
            window_refs = [ref for ref in references if any(line_id in window_map for line_id in ref["line_ids"])]
            known_refs = {ref["id"] for ref in window_refs}
            event_context = {"session_id": session_id, "core_start": window["core_start"], "core_end": window["core_end"],
                             "context_start": window["rows"][0]["id"], "lines": window["rows"],
                             "references": [{"id": ref["id"], "quote": ref["quote"], "kind": ref["kind"]}
                                                                        for ref in window_refs]}
            label = f"04_events_{session_id}_{window_number:02d}"
            discarded_context = [0]
            def validate_window(value, local=window_map, ids=known_refs, start=window["core_start"],
                                end=window["core_end"], context_start=window["rows"][0]["id"]):
                discarded_context[0] = discard_context_only_events(value, start, end)
                ground_optional_event_fields(value, local)
                return validate_events(value, local, ids, start, end, context_start)
            events, errors = runner.run_stage(letter_dir, label, "events", event_context,
                validate_window, runner.args.tokens_events)
            if events is None:
                failed.append({"stage": f"events:{session_id}:{window_number}", "errors": errors})
                event_failures += 1
                continue
            if discarded_context[0]:
                count = discarded_context[0]
                warnings.append(f"{session_id}: {count} hendelsesforslag uten linjer i vinduets kjerne ble ignorert; kontroller tilstøtende vindu.")
            for event in events["events"]:
                ordered_lines = sorted(event["line_ids"], key=line_number)
                evidence_note = event.pop("_evidence_review_note", "")
                item = {"id": f"{session_id}-e{len(session_events) + 1}", "session_id": session_id, **event,
                        "people": [{"mention": person, "resolved_name": None} for person in event["people"]],
                        "line_ids": ordered_lines, "quote": source_quotes(ordered_lines, line_map, True)[0]}
                item = attach_event_date(item, resolved_refs, base)
                if evidence_note:
                    item["review_note"] += " " + evidence_note
                    warnings.append(f"{item['id']}: {evidence_note}.")
                session_events.append(item)
        if references and not session_events and not event_failures:
            warnings.append(f"{session_id}: tidsreferanser ble funnet, men ingen hendelser ble trukket ut; kontroller kilden.")
        linked = {ref_id for event in session_events for ref_id in event["time_reference_ids"]}
        unlinked = [ref["id"] for ref in references if ref["id"] not in linked]
        if unlinked and not event_failures:
            warnings.append(f"{session_id}: tidsreferanser uten hendelseskobling: {', '.join(unlinked[:20])}; vurder om de gjelder konkrete hendelser.")
        warnings.extend(possible_duplicate_warnings(session_id, session_events))
        all_events.extend(session_events)
    result = {"schema_version": "2.1", "letter_id": metadata["letter_id"], "source_sha256": source_hash,
              "metadata": metadata, "sessions": all_sessions, "time_references": all_refs, "events": all_events,
              "warnings": warnings, "validation": {"schema_valid": True, "complete": not bool(failed), "model": runner.model,
                  "method": "Øktvis modelluttrekk; datoer beregnet i Python fra kontrollerte kilder."}}
    errors = schema_errors(result, schema_v2.FINAL)
    if errors:
        failed.append({"stage": "final_schema", "errors": errors})
    exported = letter_task_export(result) if not errors else None
    export_errors = schema_errors(exported, schema_v2.LETTER_TASK) if exported is not None else []
    if export_errors:
        failed.append({"stage": "letter_task_schema", "errors": export_errors})
    if failed:
        result["validation"]["schema_valid"] = not bool(errors or export_errors)
        result["validation"]["complete"] = False
        write_json(letter_dir / "partial.json", result)
        summary = {"source_file": relative.as_posix(), "letter_id": metadata["letter_id"],
                   "status": "needs_review", "failed_stages": failed,
                   "checkpoint": letter_dir.relative_to(output_dir).as_posix()}
        write_json(letter_dir / "needs-review.json", summary)
        print(f"  Krever gjennomgang: {', '.join(item['stage'] for item in failed)}", flush=True)
        return summary
    write_json(letter_dir / "letter-task.json", exported)
    write_json(letter_dir / "final.json", result)
    for stale in ("needs-review.json", "partial.json"):
        stale_path = letter_dir / stale
        if stale_path.exists():
            stale_path.unlink()
    print(f"  Ferdig: {len(all_sessions)} økter, {len(all_refs)} tidsreferanser og {len(all_events)} hendelser.", flush=True)
    return {"source_file": relative.as_posix(), "letter_id": metadata["letter_id"], "status": "complete",
            "sessions": len(all_sessions), "references": len(all_refs), "events": len(all_events),
            "result": (letter_dir / "final.json").relative_to(output_dir).as_posix(),
            "letter_task_result": (letter_dir / "letter-task.json").relative_to(output_dir).as_posix()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Analyser lokale brev én skriveøkt av gangen med Ollama.")
    parser.add_argument("--input", type=Path, help="Katalog med UTF-8 .txt-brev, rekursivt.")
    parser.add_argument("--output", type=Path, help="Resultatkatalog, standard er ../letter-pipeline-results.")
    parser.add_argument("--model", default="qwen3.6-local:latest")
    parser.add_argument("--base-url", default="http://ollama:11434")
    parser.add_argument("--context", type=int, default=32768)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--thinking", choices=("auto", "on", "off"), default="off")
    parser.add_argument("--tokens-scan", "--tokens-sessions", dest="tokens_scan", type=int, default=1024)
    parser.add_argument("--tokens-extract", type=int, default=4096)
    parser.add_argument("--tokens-base", "--tokens-timeline", dest="tokens_base", type=int, default=1024)
    parser.add_argument("--tokens-audit", type=int, default=768)
    parser.add_argument("--tokens-events", type=int, default=2048)
    parser.add_argument("--event-chunk-lines", type=int, default=40,
                        help="Antall linjer per hendelsesvindu (standard 40).")
    parser.add_argument("--event-overlap-lines", type=int, default=4,
                        help="Kontekstlinjer rundt hvert hendelsesvindu (standard 4).")
    parser.add_argument("--max-repairs", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--calendar-before-days", type=int, default=45)
    parser.add_argument("--calendar-after-days", type=int, default=90)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--date-uncertain", action="store_true")
    parser.add_argument("--retry-failed", action="store_true", help="Prøv tidligere feilede faser på nytt.")
    parser.add_argument("--no-json-mode", action="store_true", help="Deaktiver Ollamas JSON Schema-formatbegrensning.")
    parser.add_argument("--schema-only", action="store_true", help="Skriv sluttskjema uten å kontakte Ollama.")
    parser.add_argument("--letter-task-schema", action="store_true", help="Skriv skjemaet for letter-task.json uten å kontakte Ollama.")
    args = parser.parse_args(argv)
    if args.schema_only or args.letter_task_schema:
        print(json.dumps(schema_v2.LETTER_TASK if args.letter_task_schema else schema_v2.FINAL, ensure_ascii=False, indent=2))
        return 0
    if args.input is None:
        parser.error("--input må peke på en brevmappe.")
    if (args.context < 1024 or args.timeout < 1 or args.max_repairs < 0 or
        min(args.tokens_scan, args.tokens_extract, args.tokens_base, args.tokens_audit, args.tokens_events) < 1 or
        min(args.calendar_before_days, args.calendar_after_days, args.event_overlap_lines) < 0 or
        args.event_chunk_lines < 1 or args.event_overlap_lines >= args.event_chunk_lines or
        (args.limit is not None and args.limit < 1)):
        parser.error("Kontroller kontekst, tokenbudsjett, tidsavbrudd, reparasjoner og øvrige tallverdier.")
    input_dir = args.input.expanduser().resolve()
    if not input_dir.is_dir():
        parser.error(f"Input-katalogen finnes ikke: {input_dir}")
    output_dir = (args.output.expanduser().resolve() if args.output else input_dir.parent / "letter-pipeline-results")
    output_dir.mkdir(parents=True, exist_ok=True)
    sources = [path for path in sorted(input_dir.rglob("*.txt")) if path.is_file()
               and path.resolve().is_relative_to(input_dir) and not path.resolve().is_relative_to(output_dir)]
    if args.limit is not None:
        sources = sources[:args.limit]
    if not sources:
        parser.error(f"Fant ingen .txt-brev under {input_dir}.")
    try:
        runner = Runner(args)
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(f"Fikk ikke kontakt med lokal Ollama/modell {args.model}: {error}")
    print(f"letter-pipeline {__version__}: {len(sources)} brev, modell={runner.model}, thinking={runner.think}.", flush=True)
    print(f"Output: {output_dir}\nArbeidsform: skriveøkter behandles sekvensielt; Python beregner datoene.", flush=True)
    results = []
    for number, path in enumerate(sources, 1):
        print(f"\n--- Brev {number}/{len(sources)} ---", flush=True)
        try:
            results.append(process_letter(runner, path, input_dir, output_dir))
        except Exception as error:
            results.append({"source_file": path.relative_to(input_dir).as_posix(), "status": "failed",
                            "error": f"{type(error).__name__}: {error}"})
            print(f"  FEIL: {type(error).__name__}: {error}", flush=True)
    write_json(output_dir / "run.json", {"pipeline_version": __version__, "model": runner.model,
        "profile": runner.profile, "input_directory": str(input_dir),
        "completed_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "letters": results})
    complete = sum(result["status"] == "complete" for result in results)
    print(f"\nOppsummering: {complete}/{len(results)} ferdig; {len(results) - complete} trenger arbeid.", flush=True)
    print(f"Kjøringsmanifest: {output_dir / 'run.json'}", flush=True)
    return 0 if complete == len(results) else 2
