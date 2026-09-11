# Eval design guide

How to turn a rule or expectation into the right promptfoo assertion, and how the
two layers do two different jobs. promptfoo is the runner; this guide is how
clawhound uses it.

## Two layers

A suite is not one flat list. Tag every test `metadata.layer`:

- **Floor** (`layer: floor`, must pass). Correctness and guardrails. You WANT
  these at 100%, and the signal is any drop below it. Set `threshold: 1` so the
  test must pass. Deterministic assertions wherever a definite answer exists.
  Floor answers "is it safe and correct." Mined from the repo's rules and past
  incidents.
- **Discriminating** (`layer: discriminating`, graded). A difficulty gradient,
  some cases at or past what current models reliably do. These deliberately spread
  models apart so you can RANK them. A suite everyone aces cannot rank anything.
  Graded with `g-eval`. Discriminating answers "which model is actually better,"
  and comes from the client's hardest real tasks, not the docs.

`postprocess/recommend.py` reads these tags: floor pass-rate is the gate,
discriminating score ranks and can be a second gate.

## Category (tag every test, alongside layer)

Layer is not the only tag. Tag every test with `metadata.category` IN ADDITION to
`metadata.layer`, so a test's metadata reads for example
`{ layer: floor, category: odds-math }`. recommend.py aggregates per category and
prints a "Per-category routing" table: the cheapest model that clears the bar
WITHIN each category. One model rarely wins everywhere, so routing lets you send
the hard categories to a strong model and the easy ones to a cheap one.

For a MULTI-REPO project, make each sub-repo or service its own category. On EV
Blacksite: `blackwire` = data, `relay` = api, `visor` = frontend, `recalibrator`
= a specific service. Per-category routing then becomes per-SERVICE model routing,
which answers "Opus for the math / data services, Haiku for the frontend"
directly. Mine EACH sub-repo's own CLAUDE.md, docs, and conventions (not just the
root) so every service is represented and gets its own routing verdict. A
single-repo project can instead categorize by topic. Either way, no test ships
without a layer, a category, AND a plain summary.

## Category sidecar (`categories.yaml`)

Alongside `promptfooconfig.yaml`, the miner emits a `categories.yaml` sidecar, one
entry per category:

```yaml
categories:
  <category-name>:
    label: <what the service IS, e.g. "data layer", "API layer">
    graded_on: <the competence the model is judged on for this service>
```

`graded_on` is phrased to follow "models are graded on how well they ...", so
`postprocess/digest.py` prints one line per category, `<label>: models are graded on
<graded_on>.`, as a header over that group of tests (a `.cat-desc` line inside the
collapsed category in `--html`, and a line under the `== category (N) ==` header in
the terminal view). The sidecar is optional to the renderer: a category with no entry
renders no header line and raises no error.

Treat `graded_on` as a statement of COVERAGE INTENT, not just a label. It names the
competences the category's tests are meant to check, so any competence it names that no
test actually exercises is a visible gap the reviewer should catch. If the relay
`graded_on` claims the category covers error codes, rate limits, and nulls but no test
checks them, the sidecar has advertised a hole in the suite. Write it as honest scope,
then make the tests earn it.

## Plain summary (tag every test, alongside layer and category)

Every test also carries `metadata.plain`: a jargon-free, business-owner-readable
one-line summary of what the test checks. Not odds notation, not code, not internal
terms, just plain language a product manager or owner follows. For the
multiplicative-devig favorite-longshot case, `plain` reads for example "checks the
model devigs a deep underdog's price correctly instead of overstating its chances."
A test's metadata therefore reads for example
`{ layer: floor, category: odds-math, plain: "..." }`. It is REQUIRED, not optional.

`postprocess/digest.py --html` renders a progressive-disclosure page: `metadata.plain`
is the COLLAPSED, top-level line a PM or owner reads to scan the whole suite, and the
technical description, the prompt, and the grading are revealed on expand, for an
engineer. If a test has no `plain`, that top line falls back to the technical
description (the jargon a non-technical reader cannot follow), so a missing `plain`
is an incomplete test.

Survey before you mine, and map coverage after. Enumerate every sub-repo / service
/ major code area (nested `.git` dirs, `package.json` / `pyproject`, service folders,
per-area docs) with a rough size BEFORE writing tests, rather than trusting the root
doc or the human's named list, since a forgotten-but-live service is exactly the one
that ends up untested. Then close every generation with a coverage map (each surveyed
surface -> case count, or UNCOVERED with a reason); a large code area at zero cases is
reported loudly for the human to accept or reject, never dropped in silence.

## Rule to assertion

| The expectation | promptfoo assertion |
|---|---|
| Definite answer (math, value, format, valid JSON) | `equals`, `contains`, `regex`, `is-json`, `javascript`, `python` |
| Must never do X (guardrail) | `not-contains`, `not-regex`, or `llm-rubric` phrased as a refusal check |
| Judgment / quality, no single answer | `llm-rubric` (pass/fail) or `g-eval` (graded 0 to 1) |
| Hard reasoning that should rank models | `g-eval` with a rubric demanding the correct mechanism |

Prefer deterministic over model-graded whenever the task has a definite answer:
it is cheaper, faster, and not subject to judge noise. Reserve the graded judge
for genuine reasoning. Hold the judge model constant (set it once in
`defaultTest.options.provider`) so it is never a moving variable.

## Assertion gotchas (from real runs)

- **Quote every `g-eval` and `llm-rubric` criterion string.** A `": "` inside an
  unquoted YAML list item makes YAML read it as a mapping, not a string, and
  promptfoo rejects it: "g-eval assertion type must have a string or array of
  strings value." Wrap each criterion: `- "Recommends a ruler: realized ROI"`.
- **Do not put a bare `regex` / `not-regex` where a correct answer could contain the
  pattern by accident.** A `not-regex` on a rival tier name fails a right answer that
  names the tier while explaining; a `not-regex` on a forbidden address fails an
  answer that names it only to reject it. Keep raw regex for truly mechanical checks
  (an exact number, the em/en dash glyphs, a required literal). For anything an
  explanation could trip, use an `llm-rubric` that judges what the answer DOES, not
  which strings it happens to mention.

## Weights and thresholds

promptfoo scores a test by the weighted combination of its assertions and marks
it pass/fail against the test's `threshold`. Floor cases use `threshold: 1`
(everything must pass). Use assertion `weight` when some checks in a test matter
more than others.

## The incumbent as the floor

"Regression" means a model you rely on getting worse. Express it by setting the
floor at the level the model you run today achieves: baseline the incumbent, and
a candidate that drops below the floor threshold fails the run (promptfoo exits
non-zero, so it is CI-able). recommend.py can mark the incumbent as "you are
here" on the chart so you see both whether a candidate regresses and whether a
cheaper model already clears the same bar.

## Note on layer tags

recommend.py reads the layer from `testCase.metadata.layer` with fallbacks to
`metadata.layer` and `vars.layer`, and the category the same way from
`testCase.metadata.category` (fallbacks `metadata.category`, `vars.category`). If a
promptfoo version does not surface test metadata in its results file, the layers
collapse and recommend.py treats every test as floor for the pass-rate and every
score as discriminating, and says so; likewise if no test carries a category the
per-category routing section is skipped and noted as absent. Confirm the tags
survive on your first live run.
