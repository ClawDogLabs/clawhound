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
`metadata.layer` and `vars.layer`. If a promptfoo version does not surface test
metadata in its results file, the layers collapse and recommend.py treats every
test as floor for the pass-rate and every score as discriminating, and says so.
Confirm the tags survive on your first live run.
