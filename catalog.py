"""Human-readable index for prototype result/checkpoint directories."""

from __future__ import annotations

import argparse
import json
import os
import re
import datetime as dt
from pathlib import Path
from urllib.parse import quote


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def link(label: str, target: Path, relative_to: Path) -> str:
    path = Path(os.path.relpath(target, relative_to)).as_posix()
    path = "/".join(quote(part, safe="._-") for part in path.split("/"))
    return f"[{label}]({path})"


def stage_sort_key(state_path: Path):
    state = read_json(state_path) or {}
    label = str(state.get("label", state_path.parent.name))
    match = re.match(r"(\d+)_([a-z]+)(?:_(.*))?$", label)
    if not match:
        return (999, 999, label)
    stage_order = {"scan": 0, "relation": 0, "extract": 1, "base": 2,
                   "base_audit": 3, "events": 4, "anchor": 5, "audit": 6}
    return (int(match.group(1)), stage_order.get(match.group(2), 50), label)


def parse_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def run_time(profile: dict, letter_dir: Path, status: str) -> str:
    """Summarize observed run timestamps in UTC; old runs use checkpoint times."""
    checkpoints = []
    for state_path in letter_dir.glob("*/state.json"):
        state = read_json(state_path) or {}
        checkpoints.extend(parse_timestamp(attempt.get("completed_utc"))
                           for attempt in state.get("attempts", []) if isinstance(attempt, dict))
        checkpoints.append(parse_timestamp(state.get("completed_utc")))
    checkpoints = [item for item in checkpoints if item is not None]
    started = parse_timestamp(profile.get("run_started_utc")) or (min(checkpoints) if checkpoints else None)
    ended = max(checkpoints) if checkpoints else None
    if started is None:
        return "ukjent"
    fmt = "%Y-%m-%d %H:%M"
    if status == "pågår":
        return f"{started.strftime(fmt)}–pågår UTC"
    if ended and ended > started:
        return f"{started.strftime(fmt)}–{ended.strftime(fmt)} UTC"
    return f"{started.strftime(fmt)} UTC"


def write_steps(letter_dir: Path, root: Path) -> Path:
    metadata = read_json(letter_dir / "metadata.json") or {}
    profile = read_json(letter_dir / "profile.json") or {}
    result_path = letter_dir / "result.json"
    result = read_json(result_path) if result_path.exists() else None
    review = read_json(letter_dir / "needs-review.json") or {}
    status = ("ferdig" if result and result.get("validation", {}).get("complete") else
              "krever gjennomgang" if result or review else "pågår")
    lines = [f"# Brev {metadata.get('letter_id', letter_dir.name)}", "",
             f"- Kilde: `{metadata.get('source_file', 'ukjent')}`",
             f"- Status: **{status}**",
             f"- Modell: `{profile.get('model', 'ukjent')}`",
             f"- Pipeline: `{profile.get('prototype_version', 'ukjent')}`", "",
             "## Hovedfiler", "",
             f"- {link('Resultat (JSON)', result_path, letter_dir) if result_path.exists() else 'Resultat er ikke skrevet ennå.'}",
             f"- {link('Kildelinjer', letter_dir / 'source-lines.json', letter_dir)}",
             f"- {link('Metadata', letter_dir / 'metadata.json', letter_dir)}",
             f"- {link('Modellprofil', letter_dir / 'profile.json', letter_dir)}", "",
             "## Modellsteg", "",
             "Hvert steg har egen mappe med `state.json`, prompt, råsvar og eventuelt `accepted.json`.", "",
             "| Steg | Status | Kall | Tid | Filer |", "|---|---|---:|---:|---|"]
    states = sorted(letter_dir.glob("*/state.json"), key=stage_sort_key)
    for state_path in states:
        state = read_json(state_path)
        if state is None:
            continue
        folder = state_path.parent
        attempts = state.get("attempts", [])
        elapsed = sum(float(item.get("seconds") or 0) for item in attempts)
        accepted = bool(state.get("accepted"))
        status_text = "validert" if accepted else ("krever gjennomgang" if attempts else "pågår")
        files = [link("status", state_path, letter_dir)]
        for number in range(len(attempts)):
            prompt = folder / f"attempt-{number:02d}-prompt.txt"
            response = folder / f"attempt-{number:02d}.json"
            if prompt.exists():
                files.append(link(f"prompt {number + 1}", prompt, letter_dir))
            if response.exists():
                files.append(link(f"svar {number + 1}", response, letter_dir))
        accepted_path = folder / "accepted.json"
        if accepted_path.exists():
            files.append(link("godkjent JSON", accepted_path, letter_dir))
        label = str(state.get("label", folder.name))
        lines.append(f"| `{label}` | {status_text} | {len(attempts)} | {elapsed:.1f} s | {' · '.join(files)} |")
    if not states:
        lines.append("| Ingen steg registrert ennå | pågår | 0 | 0 s | |")
    lines.append("")
    path = letter_dir / "STEPS.md"
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path


def write_catalog(output_dir: Path) -> Path:
    """Refresh per-letter step indexes and the top-level catalog without model calls."""
    root = output_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for metadata_path in root.rglob("metadata.json"):
        letter_dir = metadata_path.parent
        if not (letter_dir / "profile.json").exists():
            continue
        metadata = read_json(metadata_path) or {}
        profile = read_json(letter_dir / "profile.json") or {}
        result_path = letter_dir / "result.json"
        result = read_json(result_path) if result_path.exists() else None
        if result:
            status = "ferdig" if result.get("validation", {}).get("complete") else "krever gjennomgang"
            events = result.get("events", [])
            hypotheses = sum(item.get("date_basis") == "hypothesis" for item in events)
            unresolved = sum(item.get("date_basis") == "unresolved" for item in events)
            rejected = len(result.get("rejected_events", []))
            missing = len(result.get("possible_missing_events", []))
        else:
            status = "pågår" if list(letter_dir.glob("*/state.json")) else "venter"
            events, hypotheses, unresolved, rejected, missing = [], 0, 0, 0, 0
        steps_path = write_steps(letter_dir, root)
        rows.append({"letter_id": str(metadata.get("letter_id", letter_dir.name)),
            "source_file": str(metadata.get("source_file", "ukjent")),
            "version": str(profile.get("prototype_version", "ukjent")),
            "model": str(profile.get("model", "ukjent")), "status": status,
            "events": len(events), "hypotheses": hypotheses, "unresolved": unresolved,
            "rejected": rejected, "missing": missing,
            "run_time": run_time(profile, letter_dir, status),
            "result": result_path if result else None, "steps": steps_path,
            "mtime": metadata_path.stat().st_mtime})
    rows.sort(key=lambda item: (item["letter_id"], -item["mtime"], item["version"]))

    lines = ["# Prototype-resultater", "",
        "Navigasjonsindeks for fullførte og pågående prototypekjøringer. Hashmappene beholdes for checkpoints og sporbarhet.",
        "Kjør `python3 -m letter_pipeline.catalog --output <resultatkatalog>` for å oppdatere denne indeksen uten modellkall.", "",
        "| Brev | Versjon | Modell | Status | Kjøretid (UTC) | Hendelser | Hypoteser | Uløste | Forkastet | Mulig manglende | Lenker |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---|"]
    for item in rows:
        result_link = link("resultat", item["result"], root) if item["result"] else "resultat skrives ved slutt"
        steps_link = link("steg", item["steps"], root)
        lines.append(f"| {item['letter_id']} (`{item['source_file']}`) | `{item['version']}` | `{item['model']}` | "
            f"{item['status']} | {item['run_time']} | {item['events']} | {item['hypotheses']} | {item['unresolved']} | "
            f"{item['rejected']} | {item['missing']} | {result_link} · {steps_link} |")
    if not rows:
        lines.append("| Ingen resultater funnet | | | | | | | | | |")
    lines.append("")
    catalog_path = root / "CATALOG.md"
    catalog_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return catalog_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lag lesbar indeks over prototyperesultater og modellsteg.")
    parser.add_argument("--output", type=Path, required=True, help="Rotkatalog for resultatene")
    args = parser.parse_args(argv)
    path = write_catalog(args.output)
    print(f"Oppdatert indeks: {path}")


if __name__ == "__main__":
    main()
