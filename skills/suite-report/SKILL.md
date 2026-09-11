---
name: suite-report
description: Turn a promptfoo eval suite (and, if a run exists, its results) into a plain-language, shareable report a non-technical stakeholder can read. Narrates the deterministic digest faithfully and never invents coverage.
---

# Suite report (narrate the digest for humans)

`postprocess/digest.py` is the practitioner's terminal scan. This skill turns that
same content into a plain-language report a non-technical stakeholder reads: what the
suite tests, per service, in English, plus the honest gaps. It is the "what we test"
half of the client story; `recommend.py`'s HTML chart is the "how the models did" half.

## Ground truth first (do not skip)

Run `python postprocess/digest.py <promptfooconfig.yaml>` and treat its output as the
SOURCE OF TRUTH. You NARRATE the digest, you never add coverage that is not in it.
Every claim in the report must trace to a real test in the config. If a service has
thin or no coverage, say so plainly; do not paper over it.

## What to write

Open with two sentences: how many tests across how many services, and what the suite
is for. Define the two layers once, in plain language: floor = must-pass correctness
and guardrails (you want these at 100 percent, any drop is the alarm); discriminating =
hard reasoning that separates a strong model from a weak one (used to rank models).

Then, grouped by category (service), for each:
- One plain-language paragraph: what this service is, and what the suite checks about
  the model's behavior on it, expanding the terse test lines into readable prose (turn
  "combat-draw is Push [javascript check]" into "confirms a drawn boxing or MMA
  moneyline settles as a push, not a loss").
- The counts: how many floor versus discriminating checks, in one sentence.
- The gaps: areas of this service the suite does NOT cover, from the digest's coverage
  view. Name them. A stakeholder needs to know the edges of what was tested.

If a `results.json` / `report.html` exists for this suite, add a closing section in
plain language: which model is recommended per service and roughly what it costs, plus
any saturated or failing tests worth attention. If no run exists yet, stop at coverage
and say the results section will follow the first run.

## Output

A self-contained HTML file (inline styles, no external assets, no dependencies) so it
opens in any browser and can be shared or printed to PDF from the browser. Save it next
to the suite (for example `<project>/suite-report.html`). Plain Markdown is fine if the
reader prefers it.

## Guardrails

- Faithful to the digest: describe only tests that exist, never inflate coverage.
- Plain language: define any jargon once, the reader is a non-technical owner.
- Honest about gaps: an untested service or a thin area is stated, not hidden.
- No em dashes or en dashes anywhere.
