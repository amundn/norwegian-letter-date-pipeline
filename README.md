# Norwegian letter date extraction

A local Python pipeline that uses an Ollama model to extract dated events and
possible writing-session dates from Norwegian letters. Python handles calendar
arithmetic, validates source-line references, and preserves uncertainty. The
model's output is a draft for human review, not a verified historical record.

The project has two entry points:

- `python -m letter_pipeline` runs the session-based pipeline.
- `python -m letter_pipeline.prototype` runs the experimental model-led pipeline.

The prototype has additional features, including named Norwegian holidays,
optional family-calendar data, event audits, and a result catalog. Its output
format is experimental and may change between versions.

## Requirements

- Python 3.10 or newer
- Ollama with a model that supports JSON output
- `jsonschema` (installed by the project dependencies)

Install from this directory:

```sh
python -m venv .venv
```

Activate the environment, then run:

```sh
python -m pip install -e .
```

If Ollama is not available at the default URL (`http://ollama:11434`), pass
`--base-url` to the command.

## Run the prototype

Put UTF-8 `.txt` letters in a directory that is not tracked by Git. Files can
contain optional YAML-like frontmatter with a `Date` field. For example:

```text
---
Date: 2001-04-24
From: Sender
To: Recipient
---
Letter text...
```

Run one or more letters:

```sh
python -m letter_pipeline.prototype \
  --input ./private-letters \
  --output ./private-results \
  --model qwen3.8:27b \
  --thinking off
```

Use `--date-uncertain` when the frontmatter date is only an approximate date.
Results contain source text, prompts, model output, and extracted personal
information. Keep both input and output directories private and out of Git.

## Optional family calendar

The prototype accepts `--family-config PATH`. It uses an entry only when one of
its aliases occurs in the event's source lines. Annual dates use `month_day`;
one-off dates use `date`.

Copy `family-calendar.example.json` to a private file such as
`family-calendar.json`, then replace the example aliases and dates with facts
for the collection being processed. Do not commit that personalized file.

```sh
python -m letter_pipeline.prototype \
  --input ./private-letters \
  --output ./private-results \
  --model qwen3.8:27b \
  --thinking off \
  --family-config ./family-calendar.json
```

## Review and tests

Each result includes a `result.json`, the source lines used, stage prompts and
responses, warnings, and validation status. A successful schema check confirms
the output structure; it does not establish that a date or event is historically
correct. Review source quotes and uncertain dates manually.

Run the unit tests with:

```sh
python -m unittest discover -s tests
```

## Data handling

The repository intentionally contains no letters, family-specific dates,
generated results, or private analysis notes. The `.gitignore` excludes common
local data and result files. Check `git status` and inspect the staged file list
before committing or pushing.

No license has been selected yet. Add one before making the repository public.
