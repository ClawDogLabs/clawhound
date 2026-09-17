# AGENTS.md: running clawhound for a user

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
Keys must be IN THE FILE, not just created in a provider console: promptfoo reads
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
1. **eval-discovery** skill: scope (which systems have real stakes), pain
   (regressions, cost, the hardest real tasks), and the owner's **preference tests**
   (tacit expectations no doc holds).
2. **suite-generation** skill: mine the repo (CLAUDE.md, gotcha/memory notes,
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
Add `-j 2 --no-cache` when local (ollama) models are in the field: they share one
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
- Which models to compare? (Anthropic / OpenAI / Google / local, and which tiers.)
- Cost or latency the priority? (On a flat-rate plan, usually latency.)
- Any tacit preferences to encode as preference tests (formatting, defaults, house rules)?

## Gotchas (learned from real runs)
- **Keys live in `.env`, not just the console**: promptfoo reads the file.
- **Reasoning models need output headroom.** Set `config: { max_tokens: 16000 }` on
  thinking models AND the judge, or they can spend the whole budget thinking and
  return EMPTY output (scored as a failure). Symptom: `finishReason: length`.
- **Local models:** lower `-j`; pull the models first or the run errors on a missing one.
  Include at least one in every suite you can: it's the only genuinely-free baseline;
  a suite with zero local models has nothing to sanity-check "$0 (free)" against.
- **Model IDs drift.** A wrong id errors only that one provider: fix the string and rerun.
- **A "floor" test that most models fail is usually the TEST, not the models**
  (too strict, or naked-recall). The report's suite-health block flags these; fix the suite.
- **A "floor" test with no floor cases in a category ≠ a failed category.** If a
  category is pure discriminating (no floor tests at all), the report says so
  explicitly ("NO FLOOR TESTS HERE" / "no floor tests in this category") rather than
  the alarming "NO MODEL CLEARS THE BAR" (that phrase is reserved for a REAL floor
  test every model actually failed). If you see the alarming one, check the category
  actually has floor cases before assuming every model is broken.
- **Rerunning a suite after editing only some tests? promptfoo caches unchanged
  (provider, prompt, config) tuples.** Only the tests you touched hit the API fresh;
  the rest replay from the last run. This is correct/desired for cost, but two
  side effects to know about: (1) a cache replay reports near-zero latency and often
  zeroed cost/tokens for that record: `recommend.py` now excludes these from the
  cost/latency averages, but if a NEW provider shape slips past that detection, a
  suspiciously-perfect "$0 (free)" or "0.0s" for a PAID model is the tell something's
  off, not a genuinely free/instant response; (2) a provider's own server-side prompt
  caching (seen with xAI: `tokenUsage.cached` carries the real count, `completion: 0`)
  looks similar but has REAL latency: `recommend.py` treats that as a cost-accounting
  gap only, keeping the latency sample, since the generation genuinely happened.
- **A safety/content-filter refusal looks identical to context exhaustion**
  (empty output + some tokens spent on hidden reasoning before the block) but is a
  different problem with a different fix. Check `response.finishReason` (`"content_filter"`
  or `"refusal"`) / `response.guardrails.flagged` before assuming a bigger `max_tokens`
  will fix it. It won't; that's a provider policy block, not a budget problem.
  `recommend.py` marks these separately (`†` vs `*`) for exactly this reason.
- **Discriminating `g-eval`/`llm-rubric` assertions need an explicit `threshold`,
  always** (see the suite-generation skill). Without one, promptfoo's default pass/fail
  is lenient enough that a suite can read as ~99% passing while the underlying 0-1
  scores show real spread. The SCORE still ranks correctly, but the boolean (and
  therefore the suite-health SATURATED warning) becomes noise.
- **A discriminating prompt that narrates the answer inside the scenario measures
  reading comprehension, not the reasoning it's supposed to test** (see the
  suite-generation skill's "No narrated-resolution tests" section). This is the single
  biggest reason a freshly-mined suite comes back near-saturated on a strong model
  bracket. Re-read every discriminating prompt for a stated verdict, not just a
  described mechanism.
- **The judge's grading cost needs its price in `postprocess/recommend_lib/report.py`'s
  `_PRICE_PER_M` table, or it shows "not auto-priced".** This table is intentionally
  NOT live-fetched (report generation stays offline/deterministic); it's refreshed as
  a separate occasional step: an agent session re-verifies prices against current
  provider docs (WebSearch/WebFetch, never guessed) before trusting a grading-cost
  estimate on a suite with a new judge, and dates the table on each refresh pass.
- **A `.env` shared across multiple suites (one file at the suites root, one
  subfolder per suite) is NOT auto-loaded when you run `promptfoo eval` from inside
  a suite's own subfolder.** promptfoo's dotenv loading is scoped to the current
  working directory, not parent directories, in any shell, bash included. A bash
  session that "just works" almost always turns out to have explicitly `source`d the
  parent `.env` first, not discovered it automatically. Pass it explicitly every
  time: `promptfoo eval --env-file ../.env ...` (adjust the relative path to wherever
  the shared file actually lives). A provider erroring with a plain "API key is not
  set" message, when the key genuinely exists in a `.env` one directory up, is this,
  not a real missing-credential problem.
- **`--filter-errors-only` filters which TEST DEFINITIONS had errors, not which
  PROVIDERS to rerun them against.** Re-grading one provider's errors (say, a local
  model that failed because the judge had no API key) still reruns every matched
  test against EVERY provider currently in the config unless you also pass
  `--filter-providers <that-provider>`. Forgetting it turns a handful of tests that
  need fixing into (tests x every other provider) real, billed calls to every other
  cloud provider in the suite for work that's either already done or unrelated to
  what actually failed. Always pair `--filter-errors-only` with an explicit
  `--filter-providers` scoped to the provider you're actually fixing.

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
