# Fewer main-model calls: replay what worked, plan once and execute with the classifier

Status: design with prototypes of both halves, each behind its own switch, off by default:
`replay_paths` (the replay cache) and `plan_execute` (the owner's plan-and-execute design). Branch
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

## 3. The two halves

### 3.1 Plan with the model, execute with the classifier (the owner's design)

"Plan with Claude → collect data from the screen → send questions to Jev with input states → run
Jev's output with an executable script." Concretely:

- **Plan.** On a cold run the model's first reply is a `plan_steps` call: 1-8 sub-goals, each with a
  **cue**, a label that is on screen only once that sub-goal is reached (the destination's title or
  something on that page, never the item tapped to get there). The tool's schema and `plan.parse`
  make it validated JSON (`openmob/agent/plan.py`); nothing in it is ever run as code. The model may
  call it again to re-plan.
- **Execute.** Each step observes as today, but the one Jev request (`_classify_step`) takes the
  **current sub-goal and its cue as the request** instead of the whole task (`_jev_request`). Jev's
  operation and target become the next action: a tap by element, a scroll, Back or an app launch.
- **The "executable script" is a list of steps, not code.** Each one runs through `_execute` like a
  model's call, so `_guard`, `_recheck_target`, approvals, ask and plan modes, blocked apps and the
  phone gatekeeper all apply. Typing, Enter, links, money, sending and deleting are never taken from
  the plan or Jev: a TYPE_TEXT (or any other) answer hands the step to the model, and a risky tap
  asks exactly as today.
- **Advancing.** At each look, before Jev is asked, sub-goals whose cue is on screen are reached
  (`_plan_advance`); the cue counts only as the place itself: a title or selected tab
  (`fast.shows_target`), or a label that isn't tappable and isn't inside a tappable row. Jev saying the
  sub-goal is done with confidence (its DONE, or the "looks done" head at `fast_path_done_min_probability`)
  also reaches it, which covers cues worded differently on a given phone.
- **Falling back.** The model takes the next step, told the plan with ticks and why the helper stopped
  (`Plan.note`, put before the screen), when Jev isn't confident (`fast_path_min_probability` /
  `fast_path_min_confidence`), a step fails or changes nothing, a cue hasn't appeared after
  `plan_max_steps_per_goal` steps, Jev confidently sees a popup / login / lock / loading screen, the
  screen has no usable element list, the next step would type, the helper would repeat itself, or a
  correction arrives. The plan is suspended, not dropped: progress (a cue appearing after the model's
  step) hands it back to the helper, and a new `plan_steps` replaces it.
- **Done.** Once the last sub-goal is reached the usual path finishes: Jev's DONE through
  `_classifier_done` with all of its checks (navigation-only request, the requested page's title on
  screen, nothing risky or typed), else the model confirms. A reply that only planned is not counted as
  an escalation, so a plain navigation task the helper carried out can end with no second model call;
  any other model step still rules the classifier's DONE out, as today.
- A tap refused before it ran (its element moved) gets one more look per sub-goal, like a replayed one.

### 3.2 A replay cache of paths that worked

After a successful task, store its path: for each step, the screen it was taken on (a fingerprint of
the labels on screen), the app, and the action by **identity** (a tapped element's text,
description, resource id and class; a scroll direction; Back; a launched package). On a later task
with the same request, each time a stored step's screen matches the current one and its target is on
screen exactly once, run that step without the model. Anything else falls through to the plan
executor, Jev and the vision model as today. A path never holds typed text, coordinates, `done`,
`open_url`, Enter, questions or waits.

- Fit: `_replay_step` runs first in each iteration; it resolves the stored target on the **current**
  observation and calls `_execute(..., fast_step=True, source="replay")`, so every check runs
  unchanged; then `_after_fast_step` settles, looks again and tells the model.
- Verification, three layers: before acting (same app, ≥ `replay_min_overlap` 70% of the stored labels on
  screen, a tap's element on screen exactly once and uncovered, Jev's screen state not popup / login /
  lock / loading); while acting (`_recheck_target`'s re-read and every guard); after acting (the loop's
  "did the screen change" check: no change means the vision model takes the next step).
- Fallback: no stored step matches (the path stays available; replay resumes on a later screen that
  fits); a step fails or asks; a correction (the path is dropped for the task); a stored step may be
  tried twice, the second time only after a `_recheck_target` refusal, and replayed steps per task are
  bounded by twice the path's length.

### 3.3 Together

Warm run: replay first. Cold run, or a screen the path doesn't cover: the model plans, the helper
executes. A successful navigation-only run is stored as a path whichever way it ran, with the same
rules (§5), so the second run of a request replays what the plan executor did the first time. Order
in the loop: replay → plan executor → Jev's fast path → the model.

### 3.4 Side by side

| | Plan and execute | Replay cache | Together |
|---|---|---|---|
| Helps a first run | yes, where Jev can take the steps | no | yes |
| Helps a repeated run | yes | yes, with fewer Jev calls to decide | yes |
| Fixes the retry / repeat / done causes (§1) | retry: yes; repeat: yes (the helper allows a path's repeats within its step budget); done: via `_classifier_done` | yes | yes |
| Model calls per navigation task (estimate) | 1 (the plan), 2 with a model confirmation | 0-1 | cold 1-2, warm 0-1 |
| New failure modes | cues that never appear (a handover per `plan_max_steps_per_goal` steps), a false advance on a label that is the way there rather than the place (guarded by the cue rule and Jev's step choice) | a matching screen that isn't the same place (bounded by the overlap, the identity rule and the guards) | both |
| Stores | nothing | UI paths (see §5) | UI paths |
| Canvas apps (chess) | no | no | no |

## 4. Recommendation

Both halves, combined as in §3.3, each behind its own switch so they can be measured apart. The replay
cache covers repeated requests with the fewest decisions; plan-and-execute covers first runs and
screens the cache hasn't seen, and it keeps Jev's job small (one sub-goal at a time). Neither helps
canvas apps.

Two follow-ups the data suggests, outside both switches and for the owner to decide:
- the same retry-after-refusal rule for Jev's plain fast steps (today one refused tap costs two model
  calls: the retry and, because the task is now "escalated", the done confirmation);
- why `_recheck_target` refused in every Settings run (the list still moving 1 s after a scroll
  settled) is the settle lane's question.

## 5. What is stored, where, for how long

The plan is kept in memory for the task only; nothing of it is stored.

The replay cache: `<data dir>/paths-<phone id>.json` (`paths-local.json` for `openmob do` and the
bench), one file per phone, written atomically. Per request: the **hash** of the normalised request
(no request text), the time it was saved, and the steps; per step: the tool, the target identity (a
tap's text, description, resource id and class; a scroll's direction; `BACK`; a launched package),
the app, and the screen fingerprint: one 12-hex-character hash per labeled accessibility element's
label (digits masked, so counts and clocks don't matter), order-free. Never: typed text (`type_text`
is never stored), coordinates (`tap`, `long_press`, `swipe` are never stored), screenshots, OCR
words, outcomes, approvals, the request text.

Only a task that **succeeded**, is **navigation-only** (`fast.navigation_only`, `fast.py:172`),
**typed nothing**, **asked nothing** (no prompt of any kind), had **no risky tap** and came from the
phone's own chat (**not an isolated caller** such as a developer key: the owner's decision for now) is
stored, and only its steps that ran without error. The newest path per request replaces the older
one. At most `replay_max_goals` (100) requests are kept, and a path older than
`history_retention_days` is dropped when read or written (0: kept). Deleting the file forgets
everything. Replayed steps are recorded in the `steps` table with `source = replay`, the plan call and
the helper's steps with `source = plan`, all with `fast = 1`; the task's variant carries `+replay` /
`+plan`, so `openmob timings` separates them.

This is a cache of **UI paths**, not memory about the owner: it is keyed by the request, it holds
labels, and it says nothing about who the owner is or whom they mean. The removed saved-notes
feature and the "My people" proposal are a separate, open owner decision (`AGENTS.md`).

### Privacy, plainly

A navigation-only request can still name a person ("open my chat with Mom"): the tapped label
"Mom" would then be stored as a target, exactly as the `steps` table's `anchor` column stores it
today, with the same retention. Stricter options (system apps and an allowlist, or only targets that
carry a resource id) cut coverage; neither is implemented. The plan's sub-goal texts go to Jev as the
request, as the request itself does today.

### Security (non-negotiable, and how it holds)

- A replayed or planned call is executed by `_execute` → `_dispatch` like a model's: `_guard` with
  its keywords, money words, Settings tiers, Jev risk, "requested" (which still reads the live
  request, never the sub-goal), approvals with digests and re-checks; `_recheck_target`; the
  permission-dialog tap refusal; `_step_gate` in ask mode (so neither half saves prompts there, by
  design); `_needs_plan` in plan mode (no step before the owner approves the plan); `_is_blocked`
  before each step and in `launch_app`. On the gatekeeper build the phone judges the tap it receives;
  it doesn't know or care who decided it.
- Neither half can type, submit, open a link, confirm money or send: the executor runs only TAP /
  SCROLL / BACK / LAUNCH_APP answers and hands anything else to the model; a path never holds those
  tools, and a task that asked anything or touched anything risky is never stored. Even if a stored
  or chosen tap lands on a "Delete" button, `_guard` asks, exactly as for a fast step (tested).
- The plan is data validated by schema and by `plan.parse` (texts of at most 100 characters); it is
  matched against labels and sent to Jev as text. Nothing generated is evaluated.
- Paths are written only by the agent's own successful run; there is no API or tool to write them.
- A user correction mid-task hands over to the model (plan) or drops the path (replay).

## 6. What it can't help

- **Canvas apps** (chess, games, drawing): no element list, coordinate taps, state that differs
  every time. Neither half ever issues a coordinate tap. The only path to fewer model calls there is a
  purpose-built loop (board reading + an engine), as the Tetris plan says.
- **Unseen screens whose labels Jev can't work from**: the plan hands over; the model drives until a
  cue appears.
- **Tasks with an answer** ("is X on?"): the final `done` needs the model to read the screen; both
  halves can only shorten the way there.
- **Ask mode**: every step still asks.

## 7. Expected gains (estimates, not measured)

On the two Settings tasks of §2 (4-6 vision steps, 5-6.5 s of model time per run today):
- **Cold run, plan-and-execute on:** 1 model call (the plan) when Jev takes every step and
  `_classifier_done`'s title check passes on this phone; otherwise 2 (plus one per handover: a
  cue that doesn't appear, an unsure Jev answer, a start screen the plan didn't foresee, which §2
  says is common when the previous task left Settings deep). So **2-3 model calls instead of 4-6**,
  about 2-4 s less per task.
- **Warm run, replay on:** 0-1 model calls (the `done`, unless the classifier finishes), **about 3-5 s
  less per task**.
- Total task time stays 10-16 s until the settle lane lands (settle is 8-9 s of these runs).
- Chess: no change, cold or warm.

Whether `_classifier_done` fires on Samsung's Settings titles is unknown: `done_via` was never
`classifier` in the measured runs, but every run had escalated, which alone rules it out.

## 8. The prototypes

- `openmob/agent/plan.py`: the `plan_steps` tool's validation, the `Plan` state (sub-goals, cursor,
  step budget, suspension reason, the progress note), the cue check.
- `openmob/agent/replay.py`: the pure parts of the cache and `PathStore` (JSON file or memory).
- `openmob/agent/loop.py`: `_replay_step`, then `_plan_step`, before the fast step; `_plan_advance` at
  each look; `_jev_request` (the sub-goal as Jev's request); `_plan_suspend` / `_plan_tell`
  (the handover and its note); the `plan_steps` handler; recording in `_execute` and storing in
  `Agent.run`; `_after_fast_step` learns one keyword argument (`retry`); the variant tags.
- `openmob/agent/tools.py`: the `plan_steps` tool and the system-prompt note, offered only when on.
- `openmob/config.py`: `replay_paths` (false), `replay_min_overlap` (0.7), `replay_max_goals` (100),
  `plan_execute` (false), `plan_max_steps_per_goal` (4); `OPENMOB_REPLAY` and `OPENMOB_PLAN_EXECUTE`
  (`on|off`) override.
- `openmob/main.py`: `build_agent` gives the agent a per-phone store when `replay_paths` is on.
- `openmob/history.py`: `step_timings` includes `source = replay` rows.
- `tests/test_replay.py`, `tests/test_plan_execute.py`: fake phones, a fake classifier and scripted
  clients only.

With both switches off nothing is read, stored or changed; each works without the other.

## 9. Measurement plan (for the owner to approve)

Phone: the owner's Samsung, gatekeeper build, after the owner installs the build and restarts the
server (`om run gatekeeper`); nothing in this lane drives the phone. Model: Haiku 5.5 high effort,
Jev on, as in §2. Through the dashboard chat, not `openmob bench`: the bench runs tasks isolated,
which (by the owner's decision) never store paths, so it can't warm the cache.

Tasks (labeled apps; chess is a control, expected unchanged):
1. "Open Settings and go to Display"
2. "Open Settings, go to Apps, open Chrome, open its Notifications setting"
3. "Open Settings and go to Battery"
4. "Open the Play Store and go to Manage apps and device"
5. control: "Open Chess.com, start a new game against a bot and play 5 moves"

Conditions, each a variant in `openmob timings`: **off** (both switches off), **plan**
(`OPENMOB_PLAN_EXECUTE=on`), **replay** (`OPENMOB_REPLAY=on`), **both**. Runs: per task and
condition, 1 cold run (an empty `paths-*.json`; for `plan` and `off` every run is cold) then 5 warm
runs, interleaved across conditions so the phone's state is shared fairly; start each run from the
home screen (press Home first) **and** once from wherever the previous run ended, since §2 shows the
start screen is where real decisions happen.

Metrics, from the `steps` and `turns` tables:
- vision steps per task (`fast = 0`), `source = plan` and `source = replay` steps per task, and
  handovers (status events "Plan paused");
- main-model time per task (`model_ms` sum), and tokens (`input_tokens`, `cache_read_tokens`);
- total time per task and agent time (total minus wait);
- success (`turns.success`), prompts (`turns.prompts`; must be equal across conditions), `done_via`;
- a manual check in the feed of every planned or replayed tap: did it tap what was meant, where it is
  now, and did any `_guard` or phone refusal fire.

Targets: on warm runs of tasks 1-4, ≥ 95% success with no regression against **off**, vision steps
median ≤ 1, main-model time median ≤ 1.5 s, prompts unchanged, zero planned or replayed steps
refused by `_guard` or the phone; on cold runs with `plan`, vision steps median ≤ 2 and no regression
in success. Task time ≤ 5 s is the phase-6 target for the two lanes together. Report cold and warm
separately: a benchmark that repeats its tasks will look better warm, and the owner should see both.

## 10. Owner decisions and open questions

Decided (2026-10-08): paths are not stored from isolated (API) tasks; the 70% overlap stays.

Open:
1. **Stricter storage.** Keep the current rule (navigation-only, no typing, no prompts, no risk), or
   also require system apps / an allowlist / resource ids on targets (§5)?
2. **Beyond navigation.** Allow paths and plans for tasks with everyday Settings changes ("turn on
   dark mode"), which the guards already allow without asking? The executor would run them today if
   Jev chose a TAP; only storing is limited to navigation-only tasks.
3. **The plan call and `escalated`.** A reply that only planned doesn't count as an escalation, so the
   classifier may finish the task (§3.1). Keep, or require a model confirmation after every plan?
4. **The follow-ups in §4** (retry-after-refusal for Jev's plain fast steps; the recheck refusals
   themselves) are cheap and measured-motivated, but they change behaviour for every task.
5. **Where the store lives** for production: a `paths` table in `history.sqlite`, scoped per account
   and deleted with the account's chats, instead of a file per phone.
6. **Jev's "requested" head** is asked with the sub-goal as the request while the helper works; it can
   only veto "requested" (never grant it), so the effect is at most an extra prompt. Acceptable, or
   should the risk questions keep the whole request?
