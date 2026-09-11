---
name: eval-discovery
description: The human-facing half of clawhound. Scope which systems are worth evaluating, map how a team uses AI and where it hurts, set the stakes, and capture the tacit preference tests no document holds. Produces the scoping doc and feeds the suite-generation skill.
---

# Eval discovery

The questions are the commodity; a Typeform can ask them. The value is the
mapping (which pain needs which eval) and the two things a codebase cannot tell
you: what the failures are worth, and the expectations that were never written
down. Output is `templates/scoping-doc.md` filled in, which also reads as the
proposal.

## Phase 0. Scope

SURVEY the whole project tree first, do not just take the human's named list. Walk
the tree for every sub-repo / service / major code area (nested `.git` dirs,
`package.json` / `pyproject`, service folders, per-area docs and style guides) and
note each one's rough size. Humans set projects up imperfectly and stop seeing the
gaps, so surface the surfaces they may have forgotten rather than inheriting their
blind spot. The suite-generation skill runs the same survey and must end with a
coverage map, so no surveyed surface silently ends up with zero tests.

Then, for each surface in play, ask: does the model's behavior here have REAL stakes
(revenue, customers, correctness, compliance, cost)? Drop the no-stakes toys (a
weekend side project where a wrong answer costs nothing) unless its behavior
genuinely matters to the owner. This sizes the engagement honestly.

## Phase 1. Surface

What AI is live or being built (chatbot, agent with tools, RAG, extraction,
classification, summarization, codegen)? Which model and version? Do they pin a
version or auto-upgrade? Customer-facing or internal? How do changes ship?

## Phase 2. Pain

Probe BOTH:
- **Regression:** have you been burned when a model changed or updated?
- **Cost / right-sizing:** do you know if you are overpaying, throwing a big model
  at small tasks?

Then: what decision are you stuck on (adopt a new model, ship a change, trust an
agent unattended), and what does a silent failure cost? The cost of failure sizes
the deal.

Also ask the hardest-task question, because it is where the discriminating cases
come from: **what is the hardest thing your AI has to do, and where does it
visibly struggle or where do you not trust it?**

## Phase 3. Stakes and preference tests

The human does not invent the bulk of the tests (mining does that). The human does
two things a repo cannot:

1. **Set the stakes.** Rank the mined cases by cost of failure. Which failures
   cost money, which are cosmetic. This sets which tests are floor (must pass) and
   which are discriminating.
2. **Name the preference tests.** The tacit expectations no doc holds, and no
   benchmark could ever have, because they encode what THIS user wants. Examples:
   "timestamps are always reported in US Eastern"; "a morning brief must lead with
   my favorite teams." These become additional promptfoo `tests` (see
   `templates/promptfooconfig.yaml`, the YOUR PREFERENCE TESTS section). They are
   the personalization that makes the suite theirs.

## Output

Fill in the scoping doc: in-scope systems, the pains in the client's own words,
the proposed floor and discriminating coverage, the preference tests, the
model(s) to compare, and the recommendation the run will produce. Hand the
mined-source list to the suite-generation skill.
