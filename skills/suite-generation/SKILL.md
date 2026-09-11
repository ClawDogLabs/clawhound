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
a `tests` list, each test tagged `metadata.layer: floor | discriminating` so
`postprocess/recommend.py` can split the must-pass floor from the graded layer.
Start from `templates/promptfooconfig.yaml`.

## What to read (per in-scope repo)

- `CLAUDE.md` / `AGENTS.md` / contributor docs: architecture rules, invariants, axioms, house style.
- Memory / gotcha notes, changelogs, and post-incident writeups: the hard-won rules and the bugs that already bit.
- Domain rules and formulas, style guides, and existing tests.

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
- **Human review is mandatory.** Emit the config, then have the human read the case
  list, cut the noise, and correct any rule you misread. Never present a mined
  suite as final.

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
