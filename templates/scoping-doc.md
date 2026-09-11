# clawhound scoping doc: <CLIENT>

Date: <date>  ·  Prepared by: <you>

This is the output of the discovery session and, as written, the proposal. Fill every bracket. Keep the pain in the client's own words.

## The problem, in your words

> <one or two sentences quoting the client's actual pain: "we never know if a model update broke something," "we throw the biggest model at everything and the bill is insane," etc.>

## What we will answer

- [ ] Upgrade or stay: is a new model safe to adopt, or does it regress on your tasks? (built)
- [ ] Am I overpaying: cheapest model and thinking level that still passes each task? (roadmap)
- [ ] Which model per agent: right-size each specialized agent. (roadmap)

Check the ones in scope for this engagement.

## In scope

| System / repo | What it does | Model today | Pinned or auto-upgrade | Stakes (why it matters) |
|---------------|--------------|-------------|------------------------|-------------------------|
| <name> | <role> | <model> | <pin/auto> | <revenue / customers / correctness / compliance / cost> |

## Dropped from scope

| System | Why dropped |
|--------|-------------|
| <name> | no real stakes (a wrong answer costs nothing) |

## Proposed cases

Drafted by mining each in-scope repo's own rules, gotchas, and past incidents, then ranked by cost-of-failure with you.

| Case | Category | Scorer | Severity | Ground-truth source |
|------|----------|--------|----------|---------------------|
| <id> | <group> | numeric / regex / function / judge | must_pass / should_pass | logs / SME labels / historical correct outputs |

## Setup

- Incumbent (the floor): <model you run today>
- K (runs per case): <8>
- Margin: <0.15>
- Gate in CI? <yes/no; if yes, note the pipeline and auth (identity federation preferred over a stored key)>

## Deliverables

1. A committed eval suite mined from your repos and confirmed with you.
2. A baseline snapshot of the model you run today.
3. A gate report on the candidate model(s) in question, with a clear PASS / WARN / FAIL and per-case detail.
4. (if in scope) a right-sizing table: dollar-per-task by model and effort, with the cheapest sufficient config per category.

## Retainer

Model regression is not a one-time problem. Every new model release re-opens the question. Recommended: a recurring re-run of the suite on each release, with a short report on what changed and whether to move. <cadence and price>
