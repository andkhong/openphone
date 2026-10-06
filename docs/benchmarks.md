# Step timings and the Jev comparison (internal)

Internal notes for measuring where an agent step's time goes, and what the Jev classifier costs
or saves. This page isn't public documentation: it names Jev (TypeSafe), which public output never
does.

## What each step records

Every tool call the agent makes is a row in the `steps` table of `history.sqlite`. A step runs from
the end of the previous step's action (or the start of the task) to the end of its own action, so
each millisecond of a task is counted on exactly one step. The phases don't overlap: time inside a
nested phase, such as a classifier call during an observation, counts only in the nested phase.

| Column | What it covers |
|---|---|
| `observe_ms` | The foreground app, the screenshot, the accessibility elements and OCR. Excludes the classifier call and loading-screen waits |
| `jev_ms` | Jev: `assess_screen` on every observation, `assess_tap` in the risky-tap check, and app picking inside `launch_app`. 0 when Jev is off |
| `wait_ms` | Human time and loading screens: the waits in `_observe_settled`, risky-tap approvals, and `ask_user` answers |
| `model_ms` | The main model call or calls, including a nudge call that returned no tool call |
| `action_ms` | The tool itself: the phone action and the tool's own work, such as app resolution. App picking by the small LLM picker is counted here |
| `settle_ms` | The sleep after an action before the next observation, plus the `wait` tool's sleep. It is counted on the step it delayed, the step after the action that caused it, so a step's row can be written when its action ends |
| `total_ms` | The step's wall time. Total minus the phases is bookkeeping, shown as "other" in the report |
| `jev` | 1 or 0: whether Jev was on for the step |
| `model` | The model that decided the step |
| `fast` | 1: the fast path decided the step (Jev's `next_step` plus the small text model), 0: the main model |
| `variant` | The task's latency features: `fixed` or `adaptive` settle, plus `+fast` when the fast path was on |
| `settle_idle` | 1 or 0: the adaptive settle before this step saw the screen go quiet, or hit its cap. NULL: a fixed sleep |

When one model response holds several tool calls, the shared observe and model time is on the
first of them. `latency_ms` keeps its earlier meaning: the tool call alone, from dispatch to
result, which includes the risky-tap check, approval or answer waits, and the phone action. It
excludes the model and the settle. Rows written before these columns existed have NULL timings,
and the report skips them.

The same numbers ride on each step's `action` or `error` event as `data.timings` and `data.jev`.
The public API passes them through as `timings`, with `jev_ms` renamed `classifier_ms` and `jev`
renamed `classifier`. A whole task's totals, including any time after the last step, are on
`AgentResult.timings`.

## The report

```sh
openmob timings                          # the last 7 days
openmob timings --since 24h --model claude-sonnet-5-5
openmob timings --since all --chat 3f2a9c0d1e4b5a6c
```

The report reads the `history.sqlite` in the data directory (`OPENMOB_DATA_DIR`, else
`~/.config/openmob`). For each model and Jev setting, and for all models together when there is
more than one, it prints the median, p90 and mean of each phase per step, and steps per task. Groups
are also split by `variant`, and a group with fast-path steps adds a "fast vs vision steps" table:
how many steps each took and their mean phases side by side.
"Agent time" is total minus wait: approvals and loading screens are shown, but they aren't the
agent's own time.

## Running the comparison

`openmob bench` runs each task in a YAML file twice, first with Jev and then without, on a phone
attached over adb. Each run gets a fresh agent, starts from the home screen, and is isolated from
the chat's recent requests. Each run records success, steps, time per phase, credits, and model
and Jev call counts. When the runs finish, the command prints the means per task side by side, a
delta column, and one line per task.

```sh
export TYPESAFE_API_KEY=...        # needed for the "on" runs
openmob bench tests/fixtures/bench_tasks.yaml --serial emulator-5554 --model claude-sonnet-5-5
openmob bench tasks.yaml --only off # one mode only
```

The task file is a list of instructions, or of `{name, instruction, max_steps}`. See
`tests/fixtures/bench_tasks.yaml`. Credits are steps × `credits_per_step` of the API tier that
uses the model, or 1 when no tier does. Choose harmless, repeatable tasks: the runs act on a real
phone. Approvals still ask in the terminal, and the time you take to answer counts as wait, not
agent time.

To switch Jev off anywhere without unsetting the key, set `jev_enabled: false` in `config.yaml` or
`OPENMOB_JEV=off` in the environment. A server started that way runs without Jev, and its steps
record `jev = 0`. Comparing ordinary usage with `openmob timings` across the two settings measures
the same thing on real traffic.

The harness is `openmob/bench.py`. `run_bench(tasks, make_agent, modes)` takes any agent factory,
and the tests drive it with the fake phone (`tests/test_timings.py`, `tests/test_bench_conditions.py`).

## Comparing the latency features

Two features cut the time a step spends outside the model:

- **Adaptive settle** (`adaptive_settle`, on by default): after an action the server asks the
  phone's app to `wait_idle` (docs/phone-protocol.md §4.15), which returns once no window or
  content change has arrived for `settle_quiet_ms` (300 ms). The fixed delays become caps (0.8 s
  after a tap, 3 s after a launch), so a settle is never longer than before. Apps without
  `wait_idle` in their hello (older builds) and iOS keep the fixed sleep; an adb phone ends a launch's
  settle once the app is in front (polling `dumpsys window`), plus the 0.8 s settle.
- **The fast path** (`fast_path: off | fast_tier | on`, off by default; needs Jev): on a screen with
  enough labeled elements (`fast_path_min_elements`), one Jev request picks the operation and its
  target (operation head plus TAP and TYPE_TEXT target heads, the pattern of
  browser-use/jev-ultrafast). When both clear `fast_path_min_probability` and
  `fast_path_min_confidence`, the step runs without the main model. TYPE_TEXT and LAUNCH_APP ask
  `fast_path_text_model` (default `resolver_model`) for the text as tiny JSON. ESCALATE, low
  scores, no element list, a repeat of the last action, an action that left the screen unchanged,
  and every DONE go to the vision step. `fast_tier` turns it on only for tasks on the `fast` tier's
  model.

Three safety checks apply to every step: `tap_element` refuses an element the app marks `covered`
(under a dialog or the keyboard); on app phones it re-reads the elements just before the tap and
refuses a target that moved (`verify_taps`, about one `ui_elements` round trip per tap); and typing
into a search field waits 200 ms longer for suggestions.

On an adb phone, compare them with the harness:

```sh
openmob bench tasks.yaml --conditions baseline,settle,fast --serial emulator-5554
```

`baseline` is fixed sleeps and no fast path, `settle` adds adaptive settle, and `fast` adds the
fast path (it needs `TYPESAFE_API_KEY`). The table shows each condition, its delta from the first
one, and the mean fast steps per task. adb has no `wait_idle`, so there `settle` only shortens
launches.

On a phone running the OpenMob app (where `wait_idle` works), run the same tasks through the
server under each condition and read `openmob timings`, which groups steps by variant:

```sh
OPENMOB_ADAPTIVE_SETTLE=off openmob run   # baseline: run the tasks, then stop the server
openmob run                               # adaptive settle
OPENMOB_FAST_PATH=on openmob run          # adaptive settle + fast path (needs TYPESAFE_API_KEY)
openmob timings --since 2h                # one group per variant: fixed, adaptive, adaptive+fast
```

Expected gains on the measured task ("Open Settings and go to Display", 3 steps, about 10.3 s):
settle was 3.8 to 4.0 s of fixed sleep. With `wait_idle` it should fall to roughly the quiet time
plus the transition per action (about 0.3 to 0.6 s after a tap, and about 0.5 to 1.5 s after a
launch). The fast path replaces about 1.5 s of model time on each step it takes with one Jev call
(about 0.15 s), plus a small-model call when typing. The vision step that confirms DONE remains.

## What to expect from the code

- The model call dominates, and each step has exactly one.
- With Jev on, every observation makes a blocking `assess_screen` call before the model sees the
  screen, including the repeat observations during loading-screen waits. Every tap, long press and
  `tap_element` also makes a blocking `assess_tap` call before the tap, even when no confirm
  keyword matched. That's two serial network round trips on a typical tap step. The client retries
  up to twice, with 0.5 s and 1 s back-offs.
- Jev can also save time. Loading screens it recognises are waited out without a model call
  (polling every 2 s, up to `jev_auto_wait_seconds`). That time shows as `wait_ms`, while the
  model calls it saves show as fewer steps.
- `settle_ms` was a fixed sleep: `settle_delay` (0.8 s by default) after most actions and
  `launch_settle_delay` (3 s) after `launch_app` and `open_url`. On a short task, that can be more
  than the phone actions take. With adaptive settle those are caps (see above).
