# AGENTS.md — running clawhound for a user

You are an AI agent (e.g. Claude Code) helping someone set up and run **clawhound**
on their own repositories. This file is your runbook. A user may simply say:

> "Read AGENTS.md and help me set clawhound up for projects X, Y, and Z. Tell me
> what API keys I need and where to save them."

Follow the flow below. Ask the discovery questions, then do the mechanical setup.

## What clawhound is (say this once, plainly)

clawhound tells the user **which model to run for the tasks they rely on**, decided
from their own work rather than a generic leaderboard. It is a thin layer on top of
[promptfoo](https://www.promptfoo.dev) (the eval runner): clawhound mines their repo
into a test suite, adds their preference tests, runs every candidate model, and
turns the results into a per-service model-routing recommendation with a report.

## The one rule that must never break

**This repo is the public tool. The user's tests, keys, results, and reports are
private and NEVER go in here.** Keep them in a SEPARATE directory:

- **Tool** = this repo (`clawhound/`), shared, public.
- **Suites** = a private sibling directory (default `../clawhound-suites/`), one
  subfolder per project, holding each project's `promptfooconfig.yaml`, the shared
  `.env`, and the generated `results.json` / `report.html`.

Create the suites directory if it does not exist. Never `git add` a user's suite or
keys to this repo (the `.gitignore` guards `.env`, `results.json`, `report.html`,
but the real rule is: keep them in the private sibling dir).

## Setup flow

### 0. Runner
Confirm promptfoo is available: `npm install -g promptfoo` (or use `npx promptfoo@latest`).

### 1. Private suites directory
Create a sibling dir and one subfolder per project:
```
../clawhound-suites/
  .env                  # one shared key file (gitignored)
  <project-a>/promptfooconfig.yaml
  <project-b>/promptfooconfig.yaml
```

### 2. API keys → one `.env` at the suites-dir root
Keys must be IN THE FILE, not just created in a provider console — promptfoo reads
the file (`--env-file`). Ask which providers the user wants to compare, then have
them add only the lines they need:

| Provider | Env var | Where to get it | Notes |
|---|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | console.anthropic.com | pay per token |
| OpenAI | `OPENAI_API_KEY` | platform.openai.com | prepaid credits, add a card |
| Google Gemini | `GOOGLE_API_KEY` | aistudio.google.com | free tier available |
| Local (Ollama / LM Studio) | none | install + pull models | free; point a provider at the local endpoint |

The `.env` is gitignored in the suites dir. Never print full keys back; never commit them.

### 3. Per project, build the suite
For each project, in order:
1. **eval-discovery** skill — scope (which systems have real stakes), pain
   (regressions, cost, the hardest real tasks), and the owner's **preference tests**
   (tacit expectations no doc holds).
2. **suite-generation** skill — mine the repo (CLAUDE.md, gotcha/memory notes,
   domain rules, past incidents, existing tests) into `promptfooconfig.yaml` +
   `categories.yaml`. Multi-repo project → **category = service**.
3. **Review before spending**: `python postprocess/digest.py <project>/promptfooconfig.yaml`
   (add `--html --out <project>/digest.html` for a page). Human confirms the suite.

### 4. Pick the judge
In `defaultTest.options.provider`, pin ONE constant grader, ideally a model NOT in
the field under test. Grading is usually the majority of the spend, so a cheaper
judge (e.g. a mid-tier model) meaningfully cuts cost. Give reasoning judges room:
`config: { max_tokens: 16000 }`.

### 5. Run
```
cd ../clawhound-suites/<project>
promptfoo eval -c promptfooconfig.yaml --env-file ../.env --output results.json
```
Add `-j 2 --no-cache` when local (ollama) models are in the field — they share one
GPU, so a high concurrency makes the runner thrash reloading models.

### 6. Decide
```
python <path-to-clawhound>/postprocess/recommend.py \
  ../clawhound-suites/<project>/results.json --out report.html
```
Useful flags: `--bar 1.0` (floor pass-rate gate), `--optimize cost|latency` (which
lens the headline leads with; both always render), `--incumbent provider:model`
(marks "you are here"), `--top-n 8` (dots shown individually before collapsing the
rest into a gray "+N more" group).

## Discovery questions to ask up front
- Which repos / projects should clawhound cover?
- Which models to compare? (Anthropic / OpenAI / Google / local — and which tiers.)
- Cost or latency the priority? (On a flat-rate plan, usually latency.)
- Any tacit preferences to encode as preference tests (formatting, defaults, house rules)?

## Gotchas (learned from real runs)
- **Keys live in `.env`, not just the console** — promptfoo reads the file.
- **Reasoning models need output headroom.** Set `config: { max_tokens: 16000 }` on
  thinking models AND the judge, or they can spend the whole budget thinking and
  return EMPTY output (scored as a failure). Symptom: `finishReason: length`.
- **Local models:** lower `-j`; pull the models first or the run errors on a missing one.
- **Model IDs drift.** A wrong id errors only that one provider — fix the string and rerun.
- **A "floor" test that most models fail is usually the TEST, not the models**
  (too strict, or naked-recall). The report's suite-health block flags these; fix the suite.

## Layout
```
skills/eval-discovery/     scope + pain + preference tests (human-in-the-loop)
skills/suite-generation/   mine the repo -> promptfoo suite (agent)
postprocess/digest.py      review a suite before running
postprocess/recommend.py   read results -> recommendation + report
docs/eval-design-guide.md  rule -> assertion; the two layers (floor / discriminating)
docs/trajectory-eval-design.md  multi-turn / memory / repeat-mistake eval (v2 module)
templates/                 starter promptfooconfig + scoping doc
```
