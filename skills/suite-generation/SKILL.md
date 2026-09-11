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

## Finish

Write `promptfooconfig.yaml` into the client's private suites location (one folder
per project). Report a summary: case count split by layer and by assertion type,
the categories covered, and which sources each case came from.
