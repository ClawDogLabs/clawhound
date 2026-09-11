---
name: suite-generation
description: Mine a repo's own accumulated knowledge (CLAUDE.md, memory/gotcha notes, domain rules, style guides, past incidents, existing tests) and draft a promptfoo eval suite from it. The agent-facing half of clawhound: it writes the tests a team would never sit down and hand-write.
---

# Suite generation (mine, do not interview)

promptfoo makes you write the tests. That is the gap. This skill drafts them by
reading a repo's own record, because the documented rules and the bugs a team
already got burned by are the highest-value tests, and a codebase remembers them
better than a person does in a meeting.

Output is a **promptfoo config** (`promptfooconfig.yaml`): a `providers` block and
a `tests` list. Tag EVERY test with ALL THREE:
- `metadata.layer: floor | discriminating` so `postprocess/recommend.py` can split
  the must-pass floor from the graded layer,
- `metadata.category: <topic-or-service>` so recommend.py can do per-category
  routing (the cheapest model that clears the bar within each category), and
- `metadata.plain: <one line>` a jargon-free, business-owner-readable one-line
  summary of what the test checks, in plain language a product manager or owner
  understands (no odds notation, no code, no internal terms). This is REQUIRED on
  every test, not optional.

So a test's metadata reads for example
`{ layer: floor, category: odds-math, plain: "checks the model devigs a deep underdog's price correctly instead of overstating its chances" }`.
Start from `templates/promptfooconfig.yaml`.

`postprocess/digest.py --html` renders `metadata.plain` as the COLLAPSED,
top-level line a PM or owner reads to scan the whole suite, and reveals the
technical description, the prompt, and the grading on expand, for an engineer. A
test with no `plain` falls back to its technical description in that top line,
which is exactly the jargon a non-technical reader cannot follow, so treat a
missing `plain` as an incomplete test.

## Emit a `categories.yaml` sidecar (alongside the config)

Write a `categories.yaml` next to `promptfooconfig.yaml`, one entry per category:

```yaml
categories:
  <category-name>:
    label: <what the service IS, e.g. "data layer", "API layer">
    graded_on: <the competence the model is judged on for this service>
```

`graded_on` is phrased to follow the words "models are graded on how well they ...",
so `digest.py` renders one line per category, `<label>: models are graded on
<graded_on>.`, as the reviewer's header for that whole group of tests.

`graded_on` doubles as a statement of COVERAGE INTENT: it names the competences this
category's tests are supposed to check. That makes any competence it names but the
tests do not actually exercise a visible, loud gap the reviewer should catch. Example:
if the relay `graded_on` says the category covers handling error codes, rate limits,
and nulls, and no test in that category actually checks any of those, the sidecar has
just advertised a gap. Write `graded_on` as the honest scope you intend to cover, then
make sure the tests earn it (or say plainly in the report which parts are not yet
covered).

The sidecar is optional to the renderer (a category with no entry just gets no header
line, no error), but the miner should always emit it so every category carries its
label and grading intent.

## Step 0: SURVEY the whole tree first (mandatory, before any mining)

Before you read a single rule, SURVEY the entire project tree and write down every
surface that could carry model behavior. Mining a suite without this step is how a
whole service ends up with zero tests and nobody notices.

Enumerate every sub-repo, service, and major top-level code area:
- Look for nested `.git` directories (each is its own deployable repo), `package.json`
  / `pyproject.toml` / `requirements.txt` (each marks a service root), service folders,
  and per-area docs and style guides (`README`, `CLAUDE.md`, `AGENTS.md`, `docs/`,
  `STYLE_GUIDE.md`).
- For each surface, record a ROUGH SIZE (a code-file count is enough) and WHICH DOCS
  it has. Size tells you how much coverage a surface deserves; a 400-file frontend and
  a 9-file microservice are not the same claim on the suite.

Do NOT rely only on the root `CLAUDE.md`, a root doc, or the human's named list of
things to test. Humans set projects up imperfectly and then stop seeing the gaps: a
service gets cloned in, works, and drops out of the mental model. The miner's job is
to surface the surfaces the human may have forgotten, not to inherit their blind spot.
If the root doc names four services and the tree has five, the fifth is exactly the one
that needs surfacing.

Cross-reference BOTH ways. The root architecture overview (for example a "Project
Overview" in the root `CLAUDE.md`) is a DECLARED list of surfaces, so seed the coverage
checklist from it as well as from the tree walk. A service the docs explicitly declare
that comes back with zero tests is the loudest gap of all: the map was handed to you and
you tested one corner of it.

Emit the survey (surface -> size -> docs found) as the first thing in your report, and
treat it as the checklist every later step is measured against.



### Category = service, in a multi-repo project

For a MULTI-REPO project, each sub-repo or service is its own category. On EV
Blacksite that is `blackwire` = data, `relay` = api, `visor` = frontend,
`recalibrator` = a specific service. Then per-category routing becomes per-SERVICE
model routing, which is what answers the real buyer question: "Opus for the
math / data services, Haiku for the frontend." A single-repo project can instead
categorize by topic (odds-math, guardrails, devig-theory, etc.); either way every
test carries a category.

## What to read (per in-scope repo)

- `CLAUDE.md` / `AGENTS.md` / contributor docs: architecture rules, invariants, axioms, house style.
- Memory / gotcha notes, changelogs, and post-incident writeups: the hard-won rules and the bugs that already bit.
- Domain rules and formulas, style guides, and existing tests.

**Mine EACH sub-repo's own record, not just the root.** In a multi-repo project
every service has its own `CLAUDE.md`, docs, and conventions. Read each one so
every service is represented in the suite (and therefore gets its own category and
its own routing verdict). A root-only mine leaves whole services untested and
unroutable.

## Two layers, mapped to promptfoo assertions

**Floor cases** (`metadata.layer: floor`, must pass): one documented rule, gotcha,
or axiom each. Use a DETERMINISTIC assertion whenever there is a definite answer,
and set `threshold: 1` so the test must pass:

| Rule shape | promptfoo assertion |
|---|---|
| exact value / math / conversion | `equals`, `contains`, `regex`, `is-json`, `javascript`, `python` |
| forbidden output (a guardrail) | `not-contains`, `not-regex`, or `llm-rubric` phrased as a refusal check |
| must-mention / format | `contains`, `contains-all`, `regex` |

Reach for `llm-rubric` on the floor only when the rule is a genuine judgment
("declines to fabricate a score") that no string check captures.

**Discriminating cases** (`metadata.layer: discriminating`, graded): the repo's
HARDEST reasoning, the tasks that separate a strong model from a weak one. Use
`g-eval` (graded 0 to 1) with a rubric that demands the correct mechanism, so a
weaker model visibly scores lower rather than flatly failing. These are what let
the matrix rank models; a suite of only floor cases saturates and ranks nothing.

## promptfoo assertion gotchas (do this, learned from real runs)

- **Quote every `g-eval` and `llm-rubric` criterion.** A `": "` inside an unquoted
  YAML list item makes YAML parse it as a mapping, not a string, and promptfoo
  rejects it ("g-eval assertion type must have a string or array of strings value").
  Write each criterion quoted: `- "Recommends a ruler: realized ROI"`.
- **Never gate a floor case on a bare `regex` / `not-regex` that a correct answer
  could trip by accident.** A `not-regex` on a rival option fails a right answer that
  names it while explaining; a `not-regex` on a forbidden string fails an answer that
  cites it only to reject it. Reserve raw regex for mechanical checks (an exact
  number, the em/en dash glyphs, a required literal); for anything an explanation
  could contain, use an `llm-rubric` that judges what the answer DOES, not which
  strings it happens to mention.

## Cautions (state these to the human, do not skip them)

- **Circularity.** You are drafting tests from what the repo already "knows," so a
  weak test just restates the model's priors and proves nothing. Prefer
  deterministic assertions where a definite answer exists; save graded judgment
  for genuinely hard reasoning.
- **Mining only finds what is written down.** Tacit expectations live only in the
  human's head. That is the eval-discovery skill's preference-tests step, not this
  one.
- **Human review is mandatory.** Emit the config, then have the human review it with
  `postprocess/digest.py` (one scannable line per test: the rule and, for deterministic
  asserts, the expected value, grouped by category). In one pass they confirm each
  service tests the right set, check the floor math, and spot what is MISSING, then cut
  noise and correct any rule you misread. Never present a mined suite as final.

## Augment an existing suite (do not regenerate)

Fresh generation is the default, but only when the target repo has NO suite yet.
When a suite already exists, switch modes: augment it, never rewrite it.

Augment runs in one of two scopes:
- **Broad** (no focus given): re-mine the whole repo for anything the current suite
  misses. An occasional top-up, or a way to catch first-pass gaps.
- **Focused** (a focus given): the human names a specific target, a feature just
  shipped, a rule the suite missed, a file or subsystem. Mine ONLY that and propose
  cases for it. This is the common case: you shipped X, so add tests for X.

Both scopes follow the same discipline below; a focus just narrows what you read in
step 2.

1. **Read the existing suite first.** Load the project's `promptfooconfig.yaml` (and
   any committed baseline) before mining. You are extending a living artifact, not
   starting from a blank file.
2. **Mine the scope.** Broad: mine the repo as in fresh generation. Focused: read
   only the named feature, files, or area (plus the rules that bear on it). Then
   diff against what the suite already covers.
3. **Propose ONLY net-new cases.** Dedupe by what each test COVERS, not by exact
   text: a new case that checks the same rule, gotcha, or reasoning as an existing
   one is a duplicate even when the prompt is worded differently. Drop it. A case
   earns its place only if it covers a rule the current suite does not.
4. **Never rewrite or remove existing tests, and never touch the committed
   baseline.** No edits to existing prompts, asserts, layers, or thresholds. If you
   believe an existing case is wrong or weak, say so in the report and leave it for
   the human to change; do not change it yourself.
5. **Hand the human only the additions.** Emit the net-new cases as a separate list
   (ready to paste into the existing `tests:` block), tagged by layer like any other
   case, with the source each came from. The human reviews and merges.

If you are unsure whether a suite exists, check for `promptfooconfig.yaml` in the
project folder before choosing a mode. Suite present -> augment. Suite absent ->
fresh generation.

## Finish

Write `promptfooconfig.yaml` into the client's private suites location (one folder
per project). Report a summary: case count split by layer and by assertion type,
the categories covered, and which sources each case came from.

### End EVERY generation with a COVERAGE MAP (mandatory)

Close the report by mapping the Step 0 survey against what you actually generated.
List EVERY surveyed surface with one of two outcomes:
- `<surface> -> N cases` (floor / discriminating split), or
- `<surface> -> UNCOVERED (reason)`.

Never present a suite that silently ignores a surface. A large code area with zero
cases must be reported LOUDLY, called out as a coverage gap, so the human can accept
it ("that service has no live AI behavior") or reject it ("mine it too") on purpose.
Silence is not acceptance. This holds in augment mode as well: map the surfaces the
existing suite already covers against the ones your additions cover, and flag any
surveyed surface still at zero.
