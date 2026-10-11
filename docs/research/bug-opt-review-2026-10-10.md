# Bug and optimization review, 2026-10-10

Scope: `main` at `4065d2e` (what Fly v14 runs and the owner's phone app is built from), plus the leads from the
day's real-phone runs (owner's Samsung SM-S948U1, gatekeeper build 2dff2b0). Branch `review/bugs-opt-2026-10-10`.

**Evidence rules.** "Measured" means read from `~/.config/openmob-gatekeeper/history.sqlite` (tables `turns` and
`steps`, only the turns named here, read-only) or from a test run. "Inferred" means deduced from the code and that
data without a phone run. "Estimate" means a number I didn't measure. Nothing in this review ran on a phone or the
emulator, and no model was called.

Turns used: `21765ec93c46` (C5, Golden Gate scroll), `2cc0cbb5ca6b` (S1, Build number), `33222a2cfe45` (dark mode),
`8f67d0b06875` (uninstall Wikipedia), `aa7e3db28c3b` (Wi-Fi count), `61bff97bb3e9` / `2f6e1a8f069c` (p4/p6, the
keyboard Go key), `270b8082bf90` (p5, the early stop), and the 0.1 s failures `a185ec3fdd33`, `41ec95e1a8e2`,
`b43e949ddc0f`, `c3c626c7d895`, `ad17fcdce898`, `c4671c1be10e`, `9ac17352001f`.

## Summary by impact

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | A stopped task's phone prompt blocks every later task for 2 min | **High** (looks like an outage) | Fixed, server + phone, tested |
| 2 | A scroll over a web page asks "Allow sliding…" | **High** (friction on every browse) | Fixed, phone + reference, tested |
| 10 | The keyboard's Go key asks "Fill your saved card or password?" | **High** (friction on every typed URL) | Server-side fix tested; phone rule proposed |
| 3 | A new install doesn't resolve for `open_settings_page`; app list cached forever | Medium | Fixed, tested |
| 9 | `names_app` counts "this", "tap", taglines as naming the app | Medium (security) | Fixed in all three, tested |
| 11 | A model refusal ends as "I stopped without finishing." | Medium (diagnosis) | Fixed, tested |
| 8 | YouTube cold start dropping the query goes unnoticed | Medium | Fixed (landing check), tested; not phone-verified |
| 5 | Fast path taps greyed-out options repeatedly | Low-medium | Fixed, tested |
| 6 | A task's first fast step is `press_key BACK` | Low-medium | Fixed, tested |
| 4 | Read tasks: 21 steps / 80 s to count Wi-Fi networks | Medium (latency) | Proposals |
| C | Prompt cache rewritten every ~3 steps | Medium (cost) | Proposal |
| L | Scroll settle never goes idle on the Samsung | Medium (latency) | Proposal |
| 7 | Build number asks rather than refuses | None (intended) | Docs agree; proposal |
| U | `localhost:8799/…` and `host:port/…` read as a URL scheme | Low | Proposal (security-review batch) |

---

## 1. A stopped task's prompt blocks every later task (High) — fixed

**Evidence (measured).** Seven p4/p5 turns ended in 0.1–0.5 s with no steps and the summary `GateRefused: Waiting
for the owner to answer a prompt on the phone.` The step before them, in `21765ec93c46` and `2cc0cbb5ca6b`, was
cancelled while the phone's prompt was up, and its outcome reads "The owner declined this on the phone", although
the summary is "Stopped." (the owner stopped the task, not declined).

**Root cause.**
- Server, `openmob/runner.py:275` `ask`: a cancel makes the wait return `"no"`.
- `Agent._gate_approval` (`openmob/agent/loop.py`) passes `"no"` on, and `RemoteDriver._call` (`openmob/phone/remote.py`)
  raises `gate_denied` ("declined"). Nothing tells the phone.
- Phone, `Gatekeeper.pending` keeps the prompt until `pending_ms` (120 s). While it's open, `admit` refuses every act
  with `gate_prompt_open` (`Gatekeeper.kt` admitChecked, and `requestTask`). The server's `approval_cancel` message
  only closes the UI (`Approvals.cancel`), never the gatekeeper's pending entry, and it isn't sent for `gk` prompts.
- The next task's first phone act gets `gate_prompt_open`. In the 0.1 s failures (no steps), that act was
  `_prepare`'s `wake_unlock` (`loop.py`, before the first step). It's an act on the phone, so `admitChecked` refuses
  it while any prompt is pending, and it runs outside `_execute`. The `GateRefused` escapes `_run`, and the runner
  ends the task with it as the summary (inferred from the code; there are no step rows to confirm it).
- Also: the 1 s `prompt_cooldown` after any prompt refuses the next act with the same code; the step then fails and
  costs a model call.

**Fix.**
- New phone method `gate_cancel {approval_id}`: a meta call (`rules.json` methods.meta), hello capability
  `gate_cancel`. `ConnectionService` calls `GateHost.cancel` (only `gk` ids), which closes the prompt unanswered:
  no permit, no denial counted. It's a tightening, so a hacked server gains nothing.
- `RemoteDriver._call` withdraws the phone's prompt whenever the wait ends without a yes. A stopped task now raises
  `gate_no_answer` with "The task was stopped…" instead of the false "declined".
- `RemoteDriver.prompt_waiter` with `Agent._prompt_wait`: on `gate_prompt_open` the call is tried again every second,
  for up to `gate_prompt_wait_seconds` (new config, 30 s). This covers the cooldown and an older app that can't
  withdraw. A stopped task stops waiting.

**Verified.**
- `tests/test_review_2026_10_10.py`: the withdraw, the retries, giving up, and the waiter's limits.
- A shared gatekeeper scenario: a prompt withdrawn by the server doesn't hold the next act. It runs on both the
  Python reference and the Kotlin JUnit.
- Not run on a phone.

## 2. "Allow sliding 'Proud host of Wikipedia…'" on a scroll (High) — fixed

**Evidence (measured).** `21765ec93c46` step 3: `scroll down` in Chrome on the Wikipedia page was refused after the
owner's prompt. The server's own guard didn't ask (`_guard_swipe` uses the flat element list).

**Root cause (inferred; the step row doesn't record the gate rule).**
- `Gatekeeper.judgeSwipe` judges each end point with `t.labelTokens()`. When the pressed node is clickable, that walks
  up to 40 nodes of its subtree (built for a button whose label sits in its children). In a web page the pressed node
  under the finger is a large clickable container, so page words count: `wordRisk` ("Donate" is a `flow_start`
  word), or a `slide` phrase.
- Any risk there is worded "Allow sliding “<first text>”", which is where the page's first text came from.
- The hit test of 82d0409 (taps) is not involved: swipes use `hitTest`, not `targetsAt`.
- I can't tell from the data whether the rule was `risk:words:*` or `risk:slider`. Proposal P1 records the rule.

**Fix (Kotlin `judgeSwipe`, `tests/gatekeeper_ref.py` `_judge_swipe`).**
- A swipe that is mostly vertical (|dy| ≥ 2|dx|) and starts inside a list or page (`scroller_class` on the start's
  hit path; the rule existed but was unused) is a scroll. A scroll is not judged by words.
- A scroll still asks for what a drag can move:
  - a slider or seekbar **anywhere under the start point** (new `sliderUnder` walk; before, only the hit path);
  - a "slide to" / "swipe to" label;
  - a control whose own words contain "slide" or "swipe" ("Swipe up to pay").
- Sideways swipes and swipes outside lists are unchanged (a row's "Delete" still asks on a sideways swipe).
- So this also closes the "scroll end points judged by the hit path only" weakness for sliders.

**Verified (measured on the reference engine; JUnit runs the same fixtures).**
- New scenarios:
  - Wikipedia-like page, scrolling both ways: allow.
  - A seekbar under a later layer: ask (the old code allowed it).
  - "Swipe up to pay" in a ScrollView: ask (pay).
  - A sideways swipe on a "Delete draft" row: ask.
- All curated scenario and adversarial steps are unchanged.
- 4 of 1,108 fuzz steps changed:
  - 2 scroll asks on random words now ask through the server hint instead;
  - 1 allow became a slider ask;
  - 1 knock-on of that.
- Kotlin: 89 tests, 0 failures.

**Residual.** A vertical drag-to-commit control inside a scroller that is neither slider-class nor labeled with
slide/swipe words (only "Pay $20") now goes unasked on a scroll. I judge that rare. Taps on it still ask.

## 10. The keyboard's Go key asks "Fill your saved card or password into Chrome?" (High) — server fix; phone proposal

**Evidence (measured).** In `61bff97bb3e9` (step 4) and `2f6e1a8f069c` (step 3), the model typed a URL and then sent
`tap {x:323, y:694, target:"Go key on keyboard"}`. The phone refused after its prompt. Both turns then ended "Stopped."

**Root cause.** `Gatekeeper.kt` judgeTarget: `surface = w.type == "input_method" || …`. **Any** tap in the
keyboard's window is treated as a credential fill (`Risk("fill", "autofill", always = true)`), because inline autofill
suggestions live in the IME window. Every key, Go included, asks. The model tapped the key because nothing told it
to use ENTER.

**Fix (server, contained).**
- `type_text`'s description now says to use `submit` or `press_key ENTER`, never the on-screen keyboard.
- A coordinate tap whose target describes the keyboard's Go/Enter/Search/Done key (`KEYBOARD_ENTER`; "Send" is
  excluded) runs as `press_key ENTER`, judged by the ENTER rules.
- Verified by a test.

**Proposal P2 (phone, for the batched security review).** Narrow the IME surface rule: ask only for a node that is
an inline suggestion (class `InlineContentView`/`InlineSuggestion`, an autofill id, or credential words), not for
keys. I didn't change it: it loosens a credential rule.

**Side finding (measured in a test).** The server refuses ENTER on `localhost:8799/p4.html` typed in the URL bar,
because `gate.url_scheme` reads `localhost` as a scheme. The same holds for any `example.com:8080/…`. See U below.

## 3. App list staleness (Medium) — fixed

**Evidence (measured).** In `8f67d0b06875`:
- step 1 `open_settings_page app_details "Wikipedia"` → "needs an installed app", with action_ms 3,047;
- step 2 `list_apps "Wikipedia"` → 15 apps, none of them Wikipedia.

**Root cause.**
- `AppResolver.packages()` (`openmob/phone/apps.py`) caches the phone's list for the process's life.
  `launch_app` and `list_apps` refresh on demand; `open_settings_page` and `wait_until app_in_front` didn't, so a new
  install never resolved there.
- **But** `list_apps` *did* refresh, and an `org.wikipedia` entry would have ranked first (its package words score
  100). So the phone's `launchable_packages` didn't include it.
  - Inferred: it wasn't fully installed, was archived (`AppListing` drops archived apps, so the server can't even say
    "archived"), or was in another profile.
  - I can't tell which without the phone. Proposal P3: let `launchable_packages` return archived apps marked as
    such.

**Fix.**
- `AppResolver.find()` reads the list again when nothing matches. It's used by `launch_app`, `open_settings_page`
  and `wait_until`.
- The list now expires after `PACKAGES_TTL_S` = 120 s, so `exact()` (fast paths) stops using a stale list.

**Verified.** Tests. The extra cost is one `launchable_packages` call per 2 min of use (estimate: under 300 ms).

## 9. `names_app` counts common words (Medium, security) — fixed

**Root cause.** Any title word of 3+ letters not in a short generic list counted, taglines included:
- "tap this" named "Tap Titans";
- "install a music app" named "Spotify: Music and Podcasts";
- "install google lens" named "Google Pay".

**Fix (`gate.py`, `Gatekeeper.kt`, `gatekeeper_ref.py`, `rules.json`).**
- Only the title's name part counts (before a ` - `, `: `, ` | ` tagline).
- A new `install.stop` list (common English request words and makers' names) counts for nothing.
- A test pins the server's copy to `rules.json`.

**Verified.** Unit tests, a shared scenario (Kotlin and Python), and the existing install tests.

## 11. Unexplained early stop on p5 (Medium) — fixed

**Evidence (measured).** `270b8082bf90`: 2 model calls in the turn's usage (input 8, output 425, cache_read 11,427).
Step 1 holds input 4, output 425, cache_read 5,069. So the second call had **0 output tokens**.

**Root cause.** In `_run` (`loop.py`), a reply without tool calls returns at once only when `stop_reason == "refusal"`;
a plain empty reply would get one nudge and a third call. So the provider's safety check ended the reply, likely
because of the injection-test page, and the loop reported "I stopped without finishing."

**Fix.** A refusal now ends with "The model declined to continue (the provider's safety check stopped its reply)…"
and `reason: "model_refusal"`. Verified by a test.

## 8. YouTube cold start dropping the query (Medium) — fixed, not phone-verified

**Root cause.**
- `youtube.search` had no landing cue, so `_link_landing` always passed.
- A cold YouTube that shows Home instead of the results went on to the classifier, which would tap a Home video.

**Fix.**
- `Link.echo`: a parameter the landing screen shows back. `youtube.search` now has `echo="q"`.
- `app_links.echo_shown` checks that a label holds the query (or its start, when the box cuts it short). If not, the
  main model takes the next step with a note.
- This costs nothing when the query shows. A false miss costs one model step. Verified by tests.
- Unverified: that YouTube's results page exposes the query as a label on the Samsung. Check that in the next run.

## 5. Dark mode taps "did nothing" (Low-medium) — fixed

**Evidence (measured).** In `33222a2cfe45`:
- steps 3, 5 and 6 are fast-path taps on Light / Dark / Light (jev 91–172 ms each), plus one model coordinate tap;
- the model's own summary says the options were greyed out and that Power saving controls dark mode.

**Root cause.**
- Not the gate and not tap placement: Samsung's power saving forces dark mode and disables the radios.
- The phone reports `enabled: false`, and the model's element list shows "(disabled)" (`adb.py:205`).
- But `fast.describe` / `fast.targets` didn't, so the classifier kept tapping disabled elements.

**Fix.** The classifier table marks them "disabled", and they're never fast-tap targets. A second smaller issue, the
task's mode handling, is not a bug: the model correctly reported it couldn't change the setting.

## 6. Every non-direct task starts with `press_key BACK` (Low-medium) — fixed

**Evidence (measured).** Step 1 of `33222a2cfe45` and of `aa7e3db28c3b` is a fast BACK (jev 135 / 156 ms; totals
1,075 / 316 ms) on the Settings screen the previous task left.

**Root cause.**
- Not intended: the classifier (Jev) chose BACK on a screen unrelated to the task. Nothing in the loop asks for a
  clean start.
- Cost: one step (0.3–1.1 s measured). Possible harm: leaving a half-filled form, or a draft in the app in front.

**Fix.** On a task's first action, a classifier BACK goes to the main model instead. Tested.

## 4. Inefficient read tasks (Medium, latency) — proposals

**Evidence (measured), `aa7e3db28c3b`.**
- 21 steps and 80 s in all; 15 of them scrolls, each a full model step.
- Per model step on average: observe 433 ms, model 1,593 ms, action 625 ms, settle 1,189 ms, total 3,979 ms.
- **15 of 15 scrolls hit the 1.2 s settle cap with `settle_idle = 0`.** On this Samsung list, `wait_idle` never saw
  300 ms of quiet.
- The answer, "about 200", came from the model matching overlapping screenshots: unreliable by construction. It
  never answered the first half (which network).
- The Maps C1 turn id wasn't given and I didn't look it up.

**Proposals.**
- **P4, a `read_list` tool (largest lever).** Scroll and read the element list on the phone or server with no model
  calls: dedupe rows by text, stop when two reads match, return the rows (or a count) to the model once.
  - Estimate for the Wi-Fi task: about 15 model calls → 1, with 80 s → about 20–25 s; the count becomes exact.
  - Reads only, so no gate risk.
- **P5, scroll settle.**
  - Find what keeps the Samsung list "busy": likely the scrollbar fade or edge-glow content-change events. Ignore
    `TYPE_WINDOW_CONTENT_CHANGED` from scrollbar-only changes in `wait_idle`, or end the swipe with a ~100 ms hold
    so it doesn't fling.
  - Estimate: 0.5–0.8 s saved per scroll.
- **P6, effort for read-back tasks.** They already use `effort: low`. Not the bottleneck: the structure is.

## C. Prompt cache rewritten every ~3 steps (Medium, cost) — proposal

**Evidence (measured), `aa7e3db28c3b`.**
- 7 of 20 model calls fell back to `cache_read = 5,069` (the system and tools prefix only) and re-wrote the history:
  69,045 cache-write tokens against 187,586 cache-read tokens.
- At the usual 1.25× write and 0.1× read input pricing, writes are about 86k input-equivalents and reads about 19k.
  The re-writes are the larger share of input cost.

**Cause (inferred).** `history_trim: batch` with `history_max_screenshots: 4` and `keep_screenshots: 2`: every
second or third step the trim drops two screenshots, which changes an early message and invalidates the cache past
the system prompt.

**Proposal P7.** Replace an omitted screenshot with a fixed-size placeholder **only at the tail**, or trim at a
cadence aligned to a second cache breakpoint before the oldest kept image.
- Estimate: 40–60% less input cost on long tasks; no latency change.
- Needs an A/B on the bench.

## L. Latency against the targets (≤1.5 s per step) — estimates

- Measured per-step means above: the model (1.6 s) alone exceeds the target. On scroll steps, settle (1.2 s) is the
  next lever, then action (0.6 s, a 400 ms swipe plus the gate's 250 ms window-settle).
- The two biggest cuts that remain:
  - P4 (no model call for list reading);
  - the "several actions per model decision" lever already noted in AGENTS.md.
- P5 is the cheap one.

## 7. "Build number" asks rather than refuses — intended

`docs/safety.md:89`, `phone-gatekeeper.md:588` and the B7 results (`gatekeeper-b7-results.md:60`, "Pass: asks") all
say Build number **asks**. The lead's premise ("B7 says refused") is wrong.

**Proposal P8.** When the request is about developer options or USB debugging, the server could stop before tapping
Build number. Developer options is refused anyway, so the prompt can't lead anywhere useful. That's an owner call.

## U. Host:port read as a URL scheme (Low) — proposal

`gate.url_scheme("localhost:8799/p4.html")` returns `"localhost"`, and so does any `example.com:8080/...`.
- `open_url` and ENTER on such text are refused as an unknown scheme.
- Fix: a "scheme" that is `localhost` or contains a dot, followed by `:<digits>(/|$|?|#)`, is a host and port.
- The same change is needed in `GateText.urlScheme` if the phone should agree.
- This is URL-safety code, so it waits for the batched security review.

## Other proposals

- **P9.** On the phone, don't refuse `wake_unlock` with `gate_prompt_open`: it never touches the screen. With the
  server's retry it now waits instead of failing, but it needn't wait at all. It's a gate change: mirror it in the
  reference engine.

- **P1.** Record the gate's `rule` (and `kind`) on refused steps (`steps.outcome` or a column). Lead 2 could not be
  pinned down without it.
- **P3.** `launchable_packages` could return archived apps with a flag, so the resolver can say "archived" (lead 3).
- `devices.py` starts a `threading.Timer` per gate question that is never cancelled when answered: one idle thread
  per prompt until `gate_timeout`. That's harmless at today's volume.
- `gate_answered.clear()` at 1,000 entries forgets every key at once. A replayed event after that is still stopped by
  the `approval_id` match, so it's low risk.

## What changed (files)

**Server**
- `openmob/phone/remote.py`: `gate_withdraw`, `prompt_waiter`, and the "cancelled" verdict.
- `openmob/agent/loop.py`: `_gate_approval` "cancelled", `_prompt_wait`, `KEYBOARD_ENTER`, first-step BACK, the echo
  check, the refusal summary, and `resolver.find`.
- `openmob/config.py`: `gate_prompt_wait_seconds`.
- `openmob/phone/protocol.py`: `gate_cancel`.
- `openmob/phone/apps.py`: `find` and the TTL.
- `openmob/agent/fast.py`: disabled elements.
- `openmob/agent/app_links.py`: `echo` and `echo_shown`.
- `openmob/agent/gate.py`: `names_app`.
- `openmob/agent/tools.py`: the `type_text` text.

**Phone**
- `Gatekeeper.kt`: `judgeSwipe`, `sliderUnder`, `namesApp`.
- `GateHost.kt`: `cancel` takes only `gk` ids and is null-safe.
- `ConnectionService.kt`: the `gate_cancel` method and capability.
- `rules.json`: meta `gate_cancel` and `install.stop`.

**Shared and docs**
- Reference engine and fixtures: `tests/gatekeeper_ref.py`, `tests/fixtures/gatekeeper/*`.
- New tests: `tests/test_review_2026_10_10.py`.
- Docs: `phone-protocol.md`, `phone-gatekeeper.md`, `safety.md`.

**Unreviewed for security (add to the next batch):**
- `gate_cancel`;
- the scroll rule;
- the `names_app` change;
- the keyboard-key rewrite.
