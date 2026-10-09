# Step timings and the Jev comparison (internal)

Internal notes for measuring where an agent step's time goes, and what the Jev classifier costs
or saves.

## MVP latency targets (owner, 2026-10-08)

A task is a chain of steps, so targets cover responsiveness, cadence and whole tasks, with first
runs and repeats apart. Measured on the owner's Samsung that day: a step is ~2–3 s (observe ~0.3 s,
Jev ~0.15 s, action ~0.2 s, settle 0.5–1.5 s, the main model ~1.1–1.5 s median when it decides);
Settings tasks took 14–21 s on a first run, and 7.4 s replayed with no model call.

| Metric | Target |
|---|---|
| First action on screen after the request | median ≤ 2 s |
| Time per step | median ≤ 1.5 s |
| Labeled-app navigation, first run (5–10 screens) | median ≤ 12 s, p90 ≤ 20 s |
| Repeated navigation (a stored path applies) | median ≤ 6 s |
| Success / prompts | ≥ 95% / ≤ 1 per typical task |
| Canvas apps (games, chess) | reliability first; no latency target |

How to measure: a fixed task set (~10 labeled-app tasks plus a canvas control), medians and p90
rather than single runs, cold and warm reported apart, on at least two phones (the Samsung plus an
emulator or another phone), and a clean-state check that stops a run on a stuck screen. This page isn't public documentation: it names Jev (TypeSafe), which public output never
does.

**The first step** (measured 2026-10-08 on the Samsung, 179 first steps; docs/design/deep-links.md §6 has
the table): a fast link step took 2.3 s median, of which 1.8 s was the page's landing settle counted inside
the step; a fast Settings page 1.1 s (0.7 s landing); a vision first step 2.5 s (model 1.3–1.4 s, screenshot
0.45 s); "other" (the wake, before it was timed) 117 ms median, 621 ms p90. Since then a link step ends when
its app is in front (the loading settle is the next step's, as after a launch), a plain Settings-page
request opens its page before any classifier call (`source = "deep_link"`, variant `+pages`), the first look
skips the classifier when the first action is known from the request, HOME is pressed only when the launcher
isn't in front, and the wake is counted as the first step's action. Estimated, not measured: the first-step
median over the first-run set from 2.2 s to about 1.0–1.3 s; vision first steps stay at about 2.5 s.

## What each step records

Every tool call the agent makes is a row in the `steps` table of `history.sqlite`. A step runs from
the end of the previous step's action (or the start of the task) to the end of its own action, so
each millisecond of a task is counted on exactly one step. The phases don't overlap: time inside a
nested phase, such as a classifier call during an observation, counts only in the nested phase.

| Column | What it covers |
|---|---|
| `observe_ms` | The foreground app, the screenshot, the accessibility elements and OCR. Excludes the classifier call and loading-screen waits |
| `jev_ms` | Jev: `assess_screen` on every observation, `assess_tap` in the risky-tap check, and app picking inside `launch_app`; on fast-path tasks with `jev_one_request`, the one request per step that replaces the first two. 0 when Jev is off |
| `wait_ms` | Human time and loading screens: the waits in `_observe_settled`, risky-tap approvals, and `ask_user` answers |
| `model_ms` | The main model call or calls, including a nudge call that returned no tool call |
| `action_ms` | The tool itself: the phone action and the tool's own work, such as app resolution. App picking by the small LLM picker is counted here. On a task's first step, also the wake (`wake_unlock`) and the HOME press before it when one is asked for (since 2026-10-08; before, that time was "other") |
| `settle_ms` | The sleep after an action before the next observation, plus the `wait` tool's sleep. It is counted on the step it delayed, the step after the action that caused it, so a step's row can be written when its action ends. A link or Settings-page step also holds the wait until the screen left the one it was opened from (up to 2.5 s); a link whose landing the quick look can't place (the home screen, neither the app nor a browser) settles fully inside its step, as every link did before 2026-10-08 |
| `total_ms` | The step's wall time. Total minus the phases is bookkeeping, shown as "other" in the report |
| `jev` | 1 or 0: whether Jev was on for the step |
| `model` | The model that decided the step |
| `fast` | 1: the fast path decided the step (Jev's `next_step` plus the small text model), 0: the main model |
| `variant` | The task's latency features: `fixed` or `adaptive` settle (`+q200` and the like when `settle_quiet_ms` isn't 300), plus `+fast` when the fast path was on, followed by its round D features: `+noshot`, `+1req`, `+launch`, `+fastdone` (see "Round D" below) |
| `done_via` | On a `done` step: `vision` (the main model confirmed) or `classifier` (`fast_path_done` finished it). NULL on other steps |
| `settle_idle` | 1 or 0: the adaptive settle before this step saw the screen go quiet, or hit its cap. NULL: a fixed sleep |
| `wait_cond` | A wait on this step: `sleep` (the `wait` tool), a `wait_until` condition such as `element_enabled:Open\|Play`, or the loop's own wait, `auto:loading_done` after a launch (on the step it delayed) and `auto:element_enabled:Open\|Play` after a Play Store Install tap. Its time is in `settle_ms` |
| `wait_met`, `wait_via`, `waited_ms` | Whether the wait ended on its condition (1) or its timeout (0); how it waited (`phone`: the app's `wait_for`, `poll`: the server read the element list, `frames`: tiny screenshots of a screen without elements, `sleep`); and how long |

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
- **The fast path** (`fast_path: off | fast_tier | on`, on by default; needs Jev): on a screen with
  enough labeled elements (`fast_path_min_elements`), one Jev request picks the operation and its
  target (operation head plus TAP and TYPE_TEXT target heads, the pattern of
  browser-use/jev-ultrafast). When both clear `fast_path_min_probability` and
  `fast_path_min_confidence`, the step runs without the main model. TYPE_TEXT and LAUNCH_APP ask
  `fast_path_text_model` (default `resolver_model`) for the text as tiny JSON. ESCALATE, low
  scores, no element list, a repeat of the last action, an action that left the screen unchanged,
  and every DONE (unless `fast_path_done` finishes it, see Round D) go to the vision step. `fast_tier` turns it on only for tasks on the `fast` tier's
  model.

- **Wait until** (`wait_until`, `launch_wait_seconds`, `install_wait_seconds`): the model waits for
  a condition (`screen_changes`, `text_appears`, `text_disappears`, `element_enabled`,
  `app_in_front`, `loading_done`) instead of a fixed `wait`, and the wait ends the moment it holds,
  with no model calls in between. The phone checks it itself where the app offers `wait_for`
  (docs/phone-protocol.md §4.16); otherwise the server polls the element list. The loop waits on
  its own too: after `launch_app` / `open_url`, while a spinner or "Loading" is on screen (up to
  10 s in all), and after an Install tap in the Play Store until the button reads Open or Play again
  (up to 90 s). `openmob timings` lists the waits per group: blind sleeps, `wait_until` and the
  loop's own, with how many met their condition and the time waited. The case it was built for,
  "install chess.com and play a game with a bot", spent about 50 s in blind waits plus a model
  call before each: a 15 s `wait` after Install (the Play Store showed Open well before) and three
  waits (5 + 5 + 10 s) while the app loaded.

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
OPENMOB_ADAPTIVE_SETTLE=off OPENMOB_FAST_PATH=off openmob run   # baseline: run the tasks, then stop the server
OPENMOB_FAST_PATH=off openmob run         # adaptive settle
openmob run                               # adaptive settle + fast path (the default; needs TYPESAFE_API_KEY)
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

## Measured (2026-10-05, Samsung SM-S948U1 through the server and the app, Jev on)

"Open Settings and go to Display", 5 runs per condition after a warm-up, a reset task between runs.
All 15 runs succeeded.

| | Fixed settle | Adaptive settle | Adaptive + fast path |
|---|---|---|---|
| Task time, median (range) | 10.8 s (9.8–13.3) | 8.2 s (7.6–9.1) | **6.0 s** (5.8–6.3) |
| Time to first action | 2.1 s | 2.2 s | 1.35 s |
| Settle per task | 3.8 s | 1.2 s | 1.1 s |
| Main model per task | 5.4 s | 5.1 s | 2.4 s |
| Classifier per task | 0.5 s | 0.45 s | 1.0 s |

With the fast path, opening Settings and tapping Display ran without the main model; only the DONE
confirmation was a vision step. One easy task in a well-labeled app: re-measure on harder tasks
(typing, search, long lists, poorly labeled apps) before reading more into it.

Per step in the fast-path round (round C, medians; settle is on the step after its action):

| Step | observe | classifier | model | action | settle | total |
|---|---|---|---|---|---|---|
| 1 open Settings (fast) | 0.23 | 0.30 | 0.61 (text model names the app) | 0.18 | 0 | 1.29 |
| 2 tap Display (fast) | 0.40 | 0.37 | 0 | 0.06 | 0.45 | 1.41 |
| 3 DONE (vision confirm) | 0.25 | 0.25 | 1.84 | 0 | 0.67 | 3.01 |

## Round D: four more cuts on fast-path tasks

Each has its own switch, so rounds can be compared. The variant names the ones that were on.

| Feature | Switch (default) | Variant tag | What it saves on the task above |
|---|---|---|---|
| The classifier's DONE | `fast_path_done` (true), `fast_path_done_min_probability` (0.85), `OPENMOB_FAST_DONE=off` to disable | `+fastdone` | the vision confirmation, about 1.5 to 1.8 s |
| Exact app names launch directly | `fast_path_direct_launch` (true) | `+launch` | the text model on LAUNCH_APP, about 0.6 s |
| One classifier request per step | `jev_one_request` (true), `jev_risk_heads` (40) | `+1req` | 0.3 to 0.6 s per fast step |
| No screenshot on fast steps | `fast_path_lazy_screenshot` (true) | `+noshot` | 0.1 to 0.3 s per fast step |
| Settle quiet time | `settle_quiet_ms` (300), `OPENMOB_SETTLE_QUIET_MS` | `+q200` | up to 0.1 s per settle at 200 |

- **The classifier's DONE.** On by default since MVP 1 (`OPENMOB_FAST_DONE=off` or
  `fast_path_done: false` turns it off): in a real-phone test it fired only on the navigation task,
  never on tasks with side effects or answers. Without it, a fast-path DONE goes to a vision step. With `fast_path_done`,
  the task ends without it only when all of these hold: (a) the DONE operation and the
  "request looks complete" head both reach `fast_path_done_min_probability`, with the operation's
  confidence at least `fast_path_min_confidence`; (b) the screen shows what the request asked to
  open: a title (a non-tappable label named after it, in the top 30% of the screen and not inside
  a tappable row) or a selected tab, or for "open <app>" that app in front; (c) the task only
  navigated: every clause of the request starts with a navigation verb (open, go to, show, …),
  no word in it implies a side effect (`confirm_keywords` plus book, submit, order, call, share,
  turn, set, change, search, …), no tap was risky (keywords or the classifier) and nothing was
  typed; (d) no vision step ran, the fast path never declined a repeat, no action left the screen
  unchanged, and the user sent no correction. Otherwise the vision model confirms, as before. The
  `done` step records `done_via`, and so does the task's outcome event (internal; the API doesn't
  show it). `openmob timings` prints how many tasks each finished.
- **Exact app names.** On LAUNCH_APP, the request's app phrases ("open Settings", "go to Display
  in Settings", "launch the Spotify app") are compared with the installed apps' cached labels and
  aliases, ignoring case, diacritics and spacing. One match that isn't already in front launches
  directly (through `launch_app`, so blocked apps stay blocked); none or several ask the text model.
- **One classifier request per step.** On fast-path tasks, on screens the fast path can use, the
  observation sends one request with every head: screen state, "looks complete", the operation,
  the TAP and TYPE_TEXT target heads, and the risky-tap questions for up to `jev_risk_heads`
  candidate elements (tappable ones first), each naming its target in the question (TypeSafe
  answers every question in a request in parallel against the same state). "Requested" questions go
  only to elements a confirm keyword names, the only ones where "requested" can matter. `_guard`
  is unchanged: keywords or classifier risk make a tap risky; requested is keywords and classifier
  (the classifier may veto it, never grant it). A tap whose element wasn't asked about, or a
  coordinate tap, gets the separate `assess_tap` as before. Vision taps on such a screen reuse the
  same answers. Other screens (no element list, fast path off) ask `assess_screen` as before.
- **No screenshot on fast steps.** On fast-path tasks the observation reads the foreground app and
  the elements and decides; the screenshot is taken only when a vision step needs it (the main model
  then sees the current screen and its screenshot, after a note listing the fast steps). Screens
  without a usable element list still take it at once, for OCR. A fast scroll takes it to size the
  swipe. Step records use a recent frame for screen sizes (`screen_w` / `screen_h` may be empty on a
  fresh server). The live view has its own feed and captures on its own when the agent hasn't.

To compare round C with round D on an adb phone:

```sh
openmob bench tasks.yaml --conditions fast,fastd --serial emulator-5554
openmob bench tasks.yaml --conditions fast,fastd,fastd-q200   # and a 200 ms quiet time
```

`fast` is round C (the fast path with every round D feature off), `fastd` is round D, and
`fast-q200` / `fastd-q200` set `settle_quiet_ms` to 200. The table adds "done by jev (sum)". Through
the server and the app (where `wait_idle` works):

```sh
OPENMOB_ROUND_D=off openmob run    # round C: all four round D features off
OPENMOB_ROUND_D=on openmob run     # round D: all four on (also the default)
OPENMOB_ROUND_D=on OPENMOB_SETTLE_QUIET_MS=200 openmob run   # round D, 200 ms quiet time
openmob timings --since 2h          # one group per variant, e.g. adaptive+fast and adaptive+fast+noshot+1req+launch+fastdone
```

Expected on "Open Settings and go to Display" (round C, 6.0 s): about −0.6 s on step 1 (no text
model), −0.3 to −0.6 s per fast step from the merged request, −0.1 to −0.3 s per fast step from the
skipped screenshot, and about −1.5 s from the classifier's DONE when its checks pass, so roughly
3 to 3.5 s. The DONE check depends on how the phone reports the page title: if Samsung's Display
page shows "Display" only inside a tappable header, the check fails and the vision step stays.
Look at `done_via` in the steps table after the runs.

## Vision steps: four cuts, whatever the model

A vision step (the main model sees the screenshot, the element list and the history, and returns one
tool call) measured 1.5 to 1.8 s on `balanced`. Games and apps without labels are nearly all vision
steps. Each cut below is a config switch:

| setting | default | what it does |
|---|---|---|
| `history_trim: batch`, `history_max_screenshots: 4` | on | Screenshots pile up to 4, then the oldest go down to `keep_screenshots` (2) at once. Before, an older screenshot was replaced by "(earlier screen omitted)" at every step, which changed an earlier message and so broke the prompt cache. Now earlier messages stay byte-identical between trims. `rolling` is the old behavior. |
| `history_cache` | on | Anthropic: cache breakpoints at the end of the last two user messages (with system and tools, 4 breakpoints, Anthropic's maximum). Each step reads the whole conversation up to the previous step from the cache. Other providers cache prefixes on their own, so for them only the batch trimming applies. |
| `vision_max_side_labeled: 768` | on | On a screen with at least `vision_labeled_min_elements` (8) labeled elements and no OCR words, the screenshot's long side is 768 px instead of 1280: about 350 image tokens instead of about 1,000. The model mostly taps by element index there. The observation's screenshot is replaced by the smaller one (same device size), so element centers, `tap`/`swipe` coordinates and step records all map back through it. Games, OCR and sparse screens keep 1280. `0` turns it off. |
| `vision_brief`, `vision_max_tokens: 512` | on | The system prompt asks for at most one short sentence before the tool call, and the cap is 512 output tokens. A reply that hits the cap before its tool call is complete is asked again with the model's full `max_tokens`. The thought still reaches the dashboard. |
| `vision_stream` | off | Anthropic: stream the reply and run each tool call as soon as its block is complete, before `message_stop`. It is off by default because it saves only the tail of the reply (tens of ms) and keeps the stream open while an approval is pending. A failed stream falls back to the plain call. |

What to expect per vision step (Sonnet 5.5, 2 to 4 screenshots in the history):
- **Shorter replies:** the largest cut. Each output token is about 10 to 15 ms; a reply that used to
  restate the screen (60 to 120 tokens) becomes 20 to 40, so about −0.4 to −0.8 s.
- **Cached history:** −0.1 to −0.3 s time to first token from the second vision step on, and the
  history's input is billed at the cache-read rate (one tenth). Every third step after the history
  holds 4 screenshots re-writes the cache once (a trim step).
- **768 px screenshots:** about 650 fewer input tokens per new screen, and less image work on the
  provider's side: −50 to −150 ms on labeled screens.
- **Streaming:** 0 to 50 ms.

### Per-step tokens

Steps now record what their model call cost: `input_tokens` (uncached), `output_tokens`,
`cache_read_tokens`, `cache_write_tokens` and `vision_image_side` (the long side of the screenshot
the model saw). They are nullable columns on `steps` (added to older databases on start), filled on
steps that made a main-model call (a vision step, or a fast step that asked the small text model).
`openmob timings` adds one line per group:

```
  tokens per model step (12): input 1,840 · cache read 9,310 (79% of the prompt) · cache write 1,620 · output 31 · screenshot 768 px
```

Compare before and after with the old behavior in `config.yaml` (`history_trim: rolling`,
`history_cache: false`, `vision_max_side_labeled: 0`, `vision_brief: false`, `vision_max_tokens: 0`).
None of this is in the public API's events: per-step token counts stay internal.

## Model comparison (`openmob bench-api`)

`openmob bench` drives an adb phone from its own process. `openmob bench-api` measures what an API
caller gets instead: every run is a developer-API task on a running server and a real phone. Tiers
switch per task (`fast` = Haiku 5.5, `balanced` = Sonnet 5.5, `best`), so no server restart is
needed. Permission modes are passed through as `permission_mode` on the task (for servers that support
them).

```sh
export OPENMOB_API_KEY=om_live_...      # or --key-file ~/.openmob-bench-key; never printed or saved
openmob bench-api bench/vision_tasks.yaml --url http://localhost:8765 \
    --tiers fast,balanced --runs 5 --warmup 1 --out bench-results
# crossed with permission modes:  --permission-modes ask,auto
# a phone other than the only online one:  --device app-abc123
```

How a run works:
- `POST /v1/devices/{id}/tasks` with the instruction, the tier (and permission mode),
  and the task's `max_steps`. Then **one** `GET /v1/tasks/{id}/events` request (SSE) follows it to
  the `done` event. Nothing polls, so a run costs 2 requests (4 with its reset), and the client keeps
  itself under 55 requests a minute (the server allows 60 per key) and honors `Retry-After`.
- A task's `reset` runs before every run (warm-ups included). It is in the JSONL marked
  `kind: reset`, and left out of the summary.
- First a warm-up round (each condition once, `--warmup 0` skips it), then `--runs` rounds with the
  conditions interleaved: fast, balanced, fast, balanced… so drift (phone heat, network, the
  provider's load) hits every condition alike.
- Nobody answers questions during a bench: a task that asks (`ask_user`, or a risky-tap
  confirmation) is cancelled and counts as unsuccessful ("asked a question").

Collected per run, from the public task and its events: success, steps, credits, wall time
(the server's `created` → `finished`), time to first action (`created` → the first action event),
and the per-phase timings of every step (`observe`, `classifier`, `model`, `action`, `settle`,
`wait`, `total`), summed per run.

Results: `bench-results/bench-api-<time>.jsonl` (one line per run, warm-ups and resets included and
marked) and `bench-api-<time>.md`: per task, a table per condition with the median and (min–max) of
wall time, steps, credits and time to first action, a per-phase table, and a list of unsuccessful
runs. The markdown is printed at the end too.

`bench/vision_tasks.yaml` holds:
1. `settings-display`: "Open Settings and go to Display", the labeled baseline (reset: go home).
2. `clock-stopwatch`: start, pause and reset the Clock app's stopwatch. Its controls are icon-only on
   Pixel, so the model works from the screenshot. It is self-reversing (it ends at zero), and its
   reset puts the stopwatch back to zero if a run stopped halfway.
3. A commented placeholder for a game task ("open <game> and open its main menu"): fill in a game
   installed on the test phone.

Tasks must stay read-only or self-reversing: nothing that sends, buys or deletes.
