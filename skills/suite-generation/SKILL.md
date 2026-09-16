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

Mine **wrong-proxy cases** here specifically, they are high-value discriminators.
A gotchas / post-incident doc often records a failure where the model measured the
WRONG true thing and returned a confident, consistent, wrong number (kept the
lowest vertex on the floor instead of the toe's world pitch; read a pre-constraint
transform to check a post-constraint effect; a denominator that silently excluded
some inputs). Turn each into a case that gives the goal and the system and asks
what to MEASURE to verify it, then grade the CHOICE of proxy with `g-eval`, not the
arithmetic. A naive "did it report a number" assertion passes all of these, which
is exactly why they discriminate. See the eval-design guide's "Wrong-proxy tests"
section. Docs that pair a silent failure with the measurement that caught it and a
number mine almost directly into these.

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
- **Set an explicit `threshold` on every discriminating `g-eval`, always.**
  promptfoo's default pass/fail for `g-eval` is lenient enough that a suite can
  read as ~99% "passed" while the underlying 0-1 scores show real spread (e.g.
  a 0.77-1.0 band with most models perfect) - the score still ranks correctly,
  but the boolean, and therefore `recommend.py`'s SATURATED warning, becomes
  noise. `threshold: 1` (require every criterion) is the default worth reaching
  for on a multi-criterion rubric; document the reasoning if you pick lower. A
  missing threshold is not neutral, it is a silent decision to accept
  promptfoo's default.

## No naked-recall tests (mine the fact, do not test recall of it)

**A mined fact must never become a naked recall test.** If the correct answer is a
private fact not derivable from the prompt (a hex code, a route path, an internal
name, a config value, a vendor name), a context-free model cannot know it, and the
test measures nothing except whether the model happened to memorize your repo. That
is an invalid test: a strong model and a weak model both "fail" it for the same
reason (neither was told the fact), so it ranks nothing and gates nothing.

Turn every such fact into one of two valid shapes:

- **Context-provided (apply the rule).** Put the convention in the prompt or a
  system block exactly as the model has it in production (its system prompt, or a
  retrieved context snippet), then test whether the model correctly APPLIES or obeys
  it. This mirrors deployment, where the model is handed the rule and must act on it
  (write the call to the right base path, use the header, obey the brand token).
- **Behavioral (handle the situation).** Give the model a scenario, an API error, a
  null, an empty response, a task, and grade how it HANDLES it. This needs no private
  recall at all.

Litmus test for every test you write: "could the model answer this from the prompt
alone, or is it being asked to have memorized our repo?" If the latter, rebuild it
into one of the two shapes above.

Copy `blackwire` as the model to follow: it GIVES the axiom or rule in the prompt
(the deflate / inflate odds convention, the draw-is-a-push settlement rule, the
raw-feed vs graded fee split) and then tests whether the model APPLIES it, rather
than asking the model to recall a value it was never given. A test that reads "what
is the hex value of our accent color?" or "what is our API base path?" with the
answer nowhere in the prompt is broken; rebuild it so the prompt supplies the token
or path and the test checks that the model uses it correctly.

## No narrated-resolution tests (describe the mechanism, never the verdict)

**A discriminating prompt must describe WHAT the system does, never WHICH
outcome is correct.** This is a distinct failure mode from naked-recall (that
one hands the model a fact it can't know; this one hands the model the
answer it was supposed to derive), and it is easy to introduce by accident
while writing a careful, precise scenario - explaining the mechanism well
tips into explaining the conclusion.

Bad (states the verdict inline): *"...where taking the absolute value
correctly round-trips it back... a reinstatement credit... must NEVER be
flipped to positive, since doing so silently destroys the credit... Is this
safe?"* The model is just confirming what it was already told.

Good (mechanism only, verdict withheld): *"...The restore step sets
chargeback=0 and takes the absolute value of commission, in one atomic
update, for that specific row. A reinstatement credit is stored as a
permanently negative value with no chargeback flag involved. A developer
proposes a startup check that takes the absolute value of every negative
commission value, independent of any flag. Walk through what this does to
each situation, and say whether it's a good idea."* Same facts, no verdict -
the model has to trace the mechanism itself to reach one.

Litmus test for every discriminating case: read the prompt back and ask "does
this sentence tell the model which answer is right, or only what the system
does?" If a sentence states the correct/incorrect judgment (correctly, safe,
must never, the bug is, silently destroys, overstates) rather than a fact
about behavior, rewrite it as a description of mechanism and let the model
reach the judgment. This failure is invisible in isolation - a prompt with
a stated verdict reads as MORE rigorous, not less, because it is more
precise - so check for it explicitly rather than trusting a read-through to
catch it; a suite where every model scores near-perfect on a hard-looking
rubric is the symptom to watch for (see the threshold gotcha above - without
an explicit threshold this symptom is invisible in the pass-rate too).

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
