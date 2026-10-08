# Fewer main-model calls: plan once, replay what worked, or both

Status: design with a prototype of the recommended approach (`replay_paths`, off by default), branch
`perf/fewer-model-calls`, 2026-10-08. Line numbers refer to `main` at `3ccca0c`.

The lane: make the agent call the main (vision) model less often. Settle time belongs to another lane
and is not touched here. Measured numbers are marked **measured**; everything else is an estimate.

## 1. Where the model calls come from today

One task is `Agent._run` (`openmob/agent/loop.py:277-413`). Each step: observe (`_observe_untimed`,
`loop.py:564-602`, which also sends the one Jev request on fast-path tasks, `_classify_step`,
`loop.py:610-626`), then either

- a **fast step** (`_fast_step`, `loop.py:1942-2016`): Jev picks TAP / TYPE_TEXT / SCROLL / BACK /
  LAUNCH_APP from the element table, no main-model call; or
- a **vision step** (`loop.py:322-411`): one main-model call (`_vision_call`, `loop.py:455`), usually
  one tool call (`max_actions_per_turn` is merged and off; the model rarely chained on its own).

Every action, whoever decided it, goes through `_execute` → `_dispatch` (`loop.py:794-818`,
`880-1055`): the URL and typing refusals, plan mode (`_needs_plan`, `loop.py:1815`), ask mode's
`_step_gate` (`loop.py:1748-1813`), `_guard` (`loop.py:1314-1443`: keywords, money, Settings tiers,
Jev risk, approvals bound by digest and re-checked), `_recheck_target` (`loop.py:1873-1911`), the
permission-dialog tap refusal (`loop.py:923-925`), blocked apps (`_is_blocked`, `loop.py:300`,
`1082`), and on the gatekeeper build the phone's own gate on the command itself.

Three things send a step to the vision model on labeled screens even though Jev could decide:

1. **A refused fast tap.** `_recheck_target` refuses a tap whose element moved between the decision
   and the tap ("no longer where it was"); `_after_fast_step` (`loop.py:2088-2089`) then sets
   `vision_next` on any error, so the model re-issues the very same tap at its new index.
2. **A repeat.** `_fast_step` hands a repeat of the last action to the model (`loop.py:1997-2002`):
   the second of two scrolls in a row is always a vision step.
3. **Done.** Any vision step sets `state.escalated` (`loop.py:323`), after which the classifier may
   not finish the task (`_classifier_done`, `loop.py:2042`); the final `done` is then a vision step.

## 2. What the data says (measured)

`~/.config/openmob-gatekeeper/history.sqlite`, 2026-10-08, the owner's Samsung SM-S948U1, gatekeeper
build, Haiku 5.5 at high effort, Jev on, three tasks run four to five times each.

**Settings tasks** ("Open Settings and go to Display"; "Open Settings, go to Apps, open Chrome, open
its Notifications setting"): 8 successful runs, 5-10 steps each, 36 vision steps in all, main-model
time 2.5-6.4 s per task, total 10-23 s per task (settle 4-9 s of that). What the 36 vision steps did:

| Why the model was called | Vision steps | Share |
|---|---|---|
| Re-issued a fast tap `_recheck_target` had refused (one in each of the 4 Display runs, 5 across the 4 Chrome runs) | 9 | 25% |
| The final `done` confirmation (`done_via` was `vision` in every run) | 8 | 22% |
| A scroll Jev wasn't allowed or sure enough to take (the repeat rule, or low confidence) | 7 | 19% |
| A real decision: getting out of the page the previous task left Settings on (Back, Back, launch again), the "Notifications" row on Chrome's app-info page | 12 | 33% |

So two thirds of the model calls on these tasks are not decisions the model needed to make: they are
retries, repeats and confirmations of steps a no-model executor already knew.

**Chess** ("Open Chess.com, start a new game against a bot and play 5 moves"): 16-21 steps, 46-52 s,
every move a vision step (coordinate taps on a canvas, no element list), one run tapped the wrong
square. Nothing in this document helps there; see §6.

## 3. The options

### 3.1 Plan once, execute without the model

At the first vision step the model also returns a plan ("open Settings; tap Apps; tap Chrome; tap
Notifications; done"), in the shape `propose_plan` already uses in plan mode (`_propose_plan`,
`loop.py:1818-1842`). A cheap executor then resolves each planned step against each screen: a
planned label is matched to an element by the normalised-name rules in `fast._names_match`
(`fast.py:201`), an app by `resolver.exact`; a label that isn't on screen is scrolled for (bounded),
then the model is asked with the plan in context. Deviation means "the planned label isn't there".

- Fit: a new tool (or an optional `plan` field on the first reply), an executor called where
  `_fast_step` is (`loop.py:314`), running each resolved call through `_execute` as today.
- Verification: label presence only. There is no expected screen to compare against, because the
  model has never seen the screens it is planning for.
- Fallback: a label not found after N scrolls, an error, a prompt, another app in front.
- Stored: nothing beyond the task.
- What it buys: it is the only option that can help a **first** run. What it costs: the model has to
  guess labels it hasn't seen ("Apps" vs "Applications"; "Notifications" lives two taps deep on
  Samsung), the planning reply is longer than one tool call, and on labeled screens it does the
  same job Jev already does per step with the request in hand. `max_actions_per_turn` showed the
  model doesn't like predicting ahead; a plan is a prediction too. It does nothing about the three
  causes in §1 unless the executor also handles retries and repeats.

### 3.2 A replay cache of paths that worked

After a successful task, store its path: for each step, the screen it was taken on (a fingerprint of
the labels on screen), the app, and the action by **identity** (a tapped element's text,
description, resource id and class; a scroll direction; Back; a launched package). On a later task
with the same request, each time a stored step's screen matches the current one and its target is on
screen exactly once, run that step without the model. Anything else falls through to Jev and the
vision model as today. A path never holds typed text, coordinates, `done`, `open_url`, Enter,
questions or waits.

- Fit: a `_replay_step` called just before `_fast_step` (`loop.py:314`); it resolves the stored
  target on the **current** observation and calls `_execute(..., fast_step=True, source="replay")`,
  so `_dispatch`, `_guard`, `_recheck_target`, ask mode, plan mode and the phone's gate all run
  unchanged; then `_after_fast_step` (`loop.py:2080`) settles, looks again and tells the model.
  Recording hooks `_execute` (what ran, without error) and `Agent.run` (store on success).
- Verification, three layers:
  1. before acting: the app is the stored one and at least `replay_min_overlap` (70%) of the stored
     screen's labels are on screen; for a tap, the element with the same identity is on screen
     exactly once and not covered (its current index is used; a stored index would be stale); Jev's
     screen state is not popup / login / lock screen / loading when Jev is confident;
  2. while acting: the same checks as any step, including `_recheck_target`'s re-read;
  3. after acting: the loop's own "did the screen change" check (`_observe_after_action`,
     `loop.py:2105`); no change means the vision model takes the next step, as for a fast step.
- Fallback: no stored step matches the screen (the next decision is Jev's or the model's; the path
  stays available, so replay resumes if a later screen matches); a step fails or asks; the user
  steers mid-task (the path is dropped for the rest of the task); a stored step may be tried twice
  (the second time only after a `_recheck_target` refusal, which means the tap never happened), and
  the number of replayed steps per task is bounded by twice the path's length.
- What it buys: on a **repeated** request, the retries, repeats and real decisions of §2 become
  no-model steps, and because no vision step ran, `_classifier_done` may finish the task, so a
  navigation task can cost zero model calls. What it can't do: help a first run, or a run whose
  start screen the cache has never seen (it then helps from the first matching screen on).

### 3.3 Hybrid

Replay first; when no path matches and the screen is labeled, Jev; otherwise the model, which on
its first reply may also give plan hints the executor prefers over Jev's choice when both are
present. The hint half is a follow-up: it needs its own measurement, and the evidence so far
(two thirds of the model calls are not decisions) says the structural half is worth doing alone.

### 3.4 Side by side

| | Plan and execute | Replay cache | Hybrid |
|---|---|---|---|
| Helps a first run | yes, when the model guesses labels right | no | yes |
| Helps a repeated run | partly | yes | yes |
| Fixes the retry / repeat / done causes (§1) | only with extra executor logic | yes (retry and repeat), done via `_classifier_done` | yes |
| Model calls per navigation task (estimate, warm) | ≥ 1 (the plan) | 0-1 | 0-1 |
| New failure modes | wrong labels, longer replies | a matching screen that isn't the same place (bounded by the guards and the 70% overlap) | both |
| Stores | nothing | UI paths (see §5) | UI paths |
| Canvas apps (chess) | no | no | no |
| Code | tool spec, executor, prompt changes | one module, ~60 lines in the loop | both |

## 4. Recommendation

**The replay cache, with the retry-after-refusal rule, behind `replay_paths` (off).** It attacks
what the data shows (retries, repeats, confirmations), it stores only what the `steps` table
already stores (anchors, tools, apps) plus unreadable screen fingerprints, it reuses every existing
check unchanged, and it is small enough to measure in a day. Plan hints come after, only if the
measurement shows first-run vision steps are what's left. Neither helps canvas apps.

Two follow-ups the data suggests, outside this switch and for the owner to decide:
- the same retry-after-refusal rule for Jev's fast steps (today one refused tap costs two model
  calls: the retry and, because the task is now "escalated", the done confirmation);
- why `_recheck_target` refused in every Settings run (the list still moving 1 s after a scroll
  settled) is the settle lane's question.

## 5. The replay design

### What is stored, where, for how long

`<data dir>/paths-<phone id>.json` (`paths-local.json` for `openmob do` and the bench), one file per
phone, written atomically. Per request: the **hash** of the normalised request (no request text),
the time it was saved, and the steps; per step: the tool, the target identity (a tap's text,
description, resource id and class; a scroll's direction; `BACK`; a launched package), the app, and
the screen fingerprint: one 12-hex-character hash per labeled accessibility element's label (digits
masked, so counts and clocks don't matter), order-free. Never: typed text (`type_text` is never
stored), coordinates (`tap`, `long_press`, `swipe` are never stored), screenshots, OCR words,
outcomes, approvals, the request text.

Only a task that **succeeded**, is **navigation-only** (`fast.navigation_only`, `fast.py:172`: every
clause starts with open / go to / show…, no side-effect word), **typed nothing**, **asked nothing**
(no prompt of any kind) and had **no risky tap** is stored, and only its steps that ran without
error. The newest path per request replaces the older one. At most `replay_max_goals` (100)
requests are kept, and a path older than `history_retention_days` is dropped when read or written
(0: kept). Deleting the file forgets everything. Replayed steps are recorded in the `steps` table
with `source = replay` and `fast = 1`, and the task's variant carries `+replay`, so `openmob timings`
separates them.

This is a cache of **UI paths**, not memory about the owner: it is keyed by the request, it holds
labels, and it says nothing about who the owner is or whom they mean. The removed saved-notes
feature and the "My people" proposal are a separate, open owner decision (`AGENTS.md`); this
design doesn't touch them.

### Privacy, plainly

A navigation-only request can still name a person ("open my chat with Mom"): the tapped label
"Mom" would then be stored as a target, exactly as the `steps` table's `anchor` column stores it
today, with the same retention. If the owner wants stricter, two options: store paths only in
system apps and a configurable allowlist, or only targets that carry a resource id. Both cut
coverage; neither is implemented.

### Security (non-negotiable, and how it holds)

- A replayed call is executed by `_execute` → `_dispatch` like a model's: `_guard` with its
  keywords, money words, Settings tiers, Jev risk, "requested" (which still reads the live request),
  approvals with digests and re-checks; `_recheck_target`; the permission-dialog tap refusal;
  `_step_gate` in ask mode (so replay saves no prompts there, by design); `_needs_plan` in plan
  mode (no replay before the plan is approved, same as the fast path); `_is_blocked` before each
  step and in `launch_app`. On the gatekeeper build the phone judges the tap it receives; it
  doesn't know or care who decided it.
- A path can't contain a step that types, submits, opens a link, confirms money or sends: those
  tools are never stored, and a task that asked anything or touched anything risky is never stored.
  Even if a stored tap lands on a "Delete" button after an app update, `_guard` asks, exactly as
  for a fast step (tested).
- Paths are written only by the agent's own successful run; there is no API or tool to write them.
  Poisoning would need the attacker to control the owner's screens and the request text, and would
  still face the guards on replay.
- A user correction mid-task drops the path for that task.

## 6. What it can't help

- **Canvas apps** (chess, games, drawing): no element list, coordinate taps, state that differs
  every time. Replay never stores coordinate taps. The only path to fewer model calls there is a
  purpose-built loop (board reading + an engine), as the Tetris plan says.
- **First runs** and **unseen start screens**: nothing to replay. Jev's fast path and the two
  follow-ups in §4 are what helps there.
- **Tasks with an answer** ("is X on?"): the final `done` needs the model to read the screen; replay
  can only shorten the way there.
- **Ask mode**: every step still asks.

## 7. Expected gains (estimates, not measured)

On a warm run of the two Settings tasks above, assuming the start screen matches a stored one from
the second step on: vision steps from 4-6 to 1-2 (the `done`, plus one real decision when the start
screen is new), main-model time from 5-6.5 s to 1-2.5 s, so **about 3-5 s less per task**. Total
task time stays 12-18 s until the settle lane lands (settle is 8-9 s of these runs). A navigation
task whose `_classifier_done` checks pass would reach 0 model calls; whether Samsung's Settings
titles satisfy that check is not known (`done_via` was never `classifier` in these runs, but every
run had escalated, which alone rules it out). Cold runs: no change. Chess: no change.

## 8. The prototype

- `openmob/agent/replay.py`: the pure parts (request key, fingerprint, overlap, what is recordable,
  resolving a stored step on a screen, matching) and `PathStore` (JSON file or memory).
- `openmob/agent/loop.py`: `_replay_step` before the fast step; recording in `_execute` and
  `Agent.run`; `_after_fast_step` learns one keyword argument (`retry`); the variant tag.
- `openmob/config.py`: `replay_paths` (false), `replay_min_overlap` (0.7), `replay_max_goals`
  (100); `OPENMOB_REPLAY=on|off` overrides.
- `openmob/main.py`: `build_agent` gives the agent a per-phone store when the switch is on.
- `openmob/history.py`: `step_timings` includes `source = replay` rows.
- `tests/test_replay.py`: fake phones and scripted clients only.

With the switch off nothing is read, stored or changed.

## 9. Measurement plan (for the owner to approve)

Phone: the owner's Samsung, gatekeeper build, after the owner installs the build and restarts the
server (`om run gatekeeper`); nothing in this lane drives the phone. Model: Haiku 5.5 high effort,
Jev on, as in §2.

Tasks (labeled apps only; chess is a control, expected unchanged):
1. "Open Settings and go to Display"
2. "Open Settings, go to Apps, open Chrome, open its Notifications setting"
3. "Open Settings and go to Battery"
4. "Open the Play Store and go to Manage apps and device" (a non-system app with a stable layout)
5. control: "Open Chess.com, start a new game against a bot and play 5 moves"

Runs: for each task, 1 cold run (empty `paths-*.json`) then 5 warm runs, with `replay_paths` on;
and 5 runs with it off, interleaved (on, off, on, off…) so the start screen and the phone's state
are shared fairly. Start each run from the home screen (the bench's `go_home`) **and** once from
wherever the previous run ended, since §2 shows the start screen is where real decisions happen.

Metrics, from the `steps` table (`openmob timings --since`, grouped by variant `…+replay`):
- vision steps per task (`fast = 0`), and `source = replay` steps per task;
- main-model time per task (`model_ms` sum), and tokens (`input_tokens`, `cache_read_tokens`);
- total time per task and agent time (total minus wait);
- success (`turns.success`), prompts (`turns.prompts`; must be equal on and off), `done_via`;
- a manual check of every replayed tap in the feed: did it tap what the path said, where it is now.

Targets: on warm runs of tasks 1-4, ≥ 95% success (no regression vs off), vision steps median
≤ 1, main-model time median ≤ 1.5 s, prompts unchanged, and no replayed step that `_guard` or the
phone refused. Task time ≤ 5 s is the phase-6 target for the two lanes together; this lane alone is
expected to land 3-5 s off the warm runs. Report cold and warm separately: a benchmark that
repeats its tasks will look better warm, and the owner should see both numbers.

## 10. Open questions for the owner

1. **API tasks.** Tasks from developer keys (`isolated=True`) run on the same phone. The prototype
   stores and replays for them too (the bench runs isolated); should API tasks be excluded from
   storing, from replaying, or neither?
2. **Stricter storage.** Keep the current rule (navigation-only, no typing, no prompts, no risk), or
   also require system apps / an allowlist / resource ids on targets (§5)?
3. **Beyond navigation.** Allow paths for tasks with everyday Settings changes ("turn on dark mode"),
   which the guards already allow without asking? Not in the prototype.
4. **The follow-ups in §4** (retry-after-refusal for Jev's fast steps; the recheck refusals
   themselves) are cheap and measured-motivated, but they change behaviour for every task.
5. **Where the store lives** for production: a `paths` table in `history.sqlite`, scoped per account
   and deleted with the account's chats, instead of a file per phone.
