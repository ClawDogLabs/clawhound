# Trajectory eval design (clawhound v2 module)

The single-shot suite (`eval-design-guide.md`) scores one prompt against one
response. It cannot see the three things that actually decide whether a model is
usable over a real session:

- **Multi-turn reasoning** - does it reach the goal across a dialogue, not per reply.
- **Memory management** - does it hold a constraint given early through later turns
  without being re-told, and does that hold up as the context grows.
- **Repeat mistakes** - after being corrected, does it converge, or relitigate a
  settled point and re-commit an error it already made.

Those only exist in a TRAJECTORY: a sequence of turns with state carried between
them. This module is a separate engine from the single-shot suite. Do not merge
them - they answer different questions ("which model per task" vs "which model
holds up over a session"), and forcing a trajectory into the single-shot runner
produces a number that means nothing.

## The core object: a trajectory

A trajectory is a scripted scenario, not a Q&A pair. Its shape:

```
trajectory:
  id: <slug>
  category: <same category vocabulary as the single-shot suite>
  goal: <the end state the model must reach - the diagnosis, the fix, the answer>
  seed: <context given ONCE at turn 0: the system framing, the conventions,
         the constraints the model must remember for the whole run>
  banned_behaviors:        # the heart of the repeat-mistake metric
    - id: <slug>
      desc: <a specific wrong path we want to watch for - a false lead, a
             wrong-proxy measurement, a dead-end hypothesis>
  turns:
    - id: t1
      user: <the message delivered to the model this turn - a task, an
             observation, or a piece of feedback>
      inject: <optional: a NEW fact or false lead introduced this turn,
               often the exact one a human chased in the real session>
      checkpoint:          # graded the moment the model responds this turn
        expect: <what a correct response does here>
        must_avoid: [<banned_behavior ids that would be wrong to commit here>]
      retain: <optional: a seed constraint that MUST still be honored at this
               turn, with no reminder - this is the memory probe>
```

## State model

- **Conversation history is the memory substrate.** Every prior turn (user + model)
  is threaded into the next call. The memory test is simply: a constraint stated in
  `seed` or an early turn, probed by a `retain` clause many turns later with no
  reminder. Track retention as a function of turn depth to get a degradation curve.
- **Optional scratch memory.** For models/harnesses with an explicit memory tool,
  give it one and test whether it USES it (writes the constraint, reads it back).
  Start without this; add it only when comparing memory-augmented harnesses.

## User simulator: scripted first

Each turn's `user` message is one of two kinds:

- **Scripted (default).** The next message is fixed text, including the `inject`
  false leads. Deterministic, reproducible, cheap, and it pins every model to the
  identical path so the comparison is fair. Build this first.
- **Model-simulated (v2).** A held-constant user-sim model reacts to the model's
  actual output (as in tau-bench). More realistic, but adds cost and variance and
  makes runs non-identical across models. Defer until scripted is working.

## Metrics (the three families, defined)

Multi-turn reasoning:
- **goal_success** - did the final state match `goal` (graded at the last
  checkpoint). The headline pass/fail.

Memory:
- **retention@N** - at each `retain` checkpoint, was the seed constraint honored
  with no reminder. Report per depth so you see WHERE it starts forgetting, not
  just whether.

Repeat mistakes (the sharpest, and the reason to build this):
- **convergence_turns** - turns from first attempt to the checkpoint passing. Fewer
  is better. A model that needs five corrections per task is worse than one that
  needs one, even if both eventually pass.
- **trap_avoidance** - fraction of `must_avoid` banned behaviors the model did NOT
  commit.
- **no_reintroduction** - once a banned behavior has been corrected, does the model
  re-commit it (or the same CLASS of it) on a later turn. This is the literal
  "least repeat mistakes" signal.
- **pass^k** - run the whole trajectory k times; pass^k is the fraction of runs that
  succeed on EVERY one of the k (tau-bench's reliability metric). A model that
  passes once and fails twice is not one you can lean on.

## Grading

Same discipline as the single-shot suite: hold the judge constant (one model, set
once), prefer a deterministic check at a checkpoint where a definite answer exists,
reserve g-eval for reasoning. The difference is that grading happens PER CHECKPOINT
across the trajectory, and `banned_behaviors` are graded as "did this appear,"
which is often a cheap keyword/rubric check.

## What makes good trajectory material

The best scenarios are already written down: **a real debugging session where a
human chased false leads before finding the cause.** A post-incident writeup that
records the wrong hypotheses in order IS a trajectory - each false lead becomes an
`inject`, and "did the model chase it too" becomes a `must_avoid`. A gotchas doc
that lists "I measured X, it was wrong, then I measured Y, also wrong, then Z" is a
ready-made repeat-mistake test: the class of error (measuring the wrong thing) is
the banned behavior, and the metric is whether the model stops making it after the
first correction.

## Build order (MVP)

1. Scripted turns only, conversation-history state, one constant judge.
2. 5-10 trajectories mined from real sessions, tagged by category.
3. Metrics: goal_success, convergence_turns, trap_avoidance, no_reintroduction,
   and pass^3 (k=3 is enough to expose flakiness without tripling cost too far).
4. Report per model: a session-scorecard, separate from the single-shot report.

## Cost note

A trajectory of T turns run at pass^k costs about T x k model calls plus per-turn
grading, per model. This multiplies fast, so keep trajectories short (the real ones
converge in 3-6 turns) and k small (3). This is why it is a deliberate, separate
run, not something you fold into every single-shot eval.

## Relationship to the single-shot suite

Complementary, never merged. The single-shot suite ranks per-task capability and
drives the cost/latency routing. The trajectory suite answers the orthogonal
question the single-shot number hides: which model gets through a session without
babysitting. A model can top every single-shot category and still be the worst to
actually work with. Run both; report them side by side, not blended.
