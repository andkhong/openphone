# MCP drive level: an outside agent operates the phone step by step

Status: designed and built 2026-10-08 (branch `feature/mcp-drive`), **server only, not run against a phone**.
Every latency figure in §9 is an estimate from the loop's measured step costs; nothing here was measured.

## 1. Why

`/mcp` is task-level today: `run_task` hands a whole goal to OpenMob's own agent loop, which observes, decides
with its model and acts. An outside agent (Claude Code, Claude Desktop, Cursor, a custom agent) that wants to
drive with **its own reasoning** has no way in except raw-control tools such as mobile-mcp, which hand the
caller the phone with no checks at all.

The drive level adds step-by-step tools on the same server with **the same safety model as the loop**: every
act goes through `Agent._execute` → `_dispatch` → `_guard` (blocked apps, password fields, dangerous URLs,
confirm and money keywords, the permission mode, owner approvals signed on the phone and bound to the action)
and then, on gatekeeper builds, the phone's own gatekeeper. The outside agent gets exactly the blocks and
prompts the loop's model gets, and nothing it can't.

That is the differentiator: an untrusted agent can hold the controls because the controls refuse, ask and
record on their own.

## 2. The session is a task

A drive session **is** a task in every existing structure, with decisions coming from outside instead of
from a model:

- `V1Service.start_drive` creates it like `create_task` (scopes, the owned device, the permission mode
  against the key's `max_mode` and the phone's ceiling, the concurrency cap, daily quotas, the phone's lock
  state, the registry entry, the ledger row, an audit entry) and enqueues it on the phone's `Coordinator`
  with `options["drive"] = <DriveChannel>`, source `drive`, tier `drive`.
- `Coordinator._work` runs it on the phone's one worker thread, so **one task or session runs per phone**,
  the owner's "Stop" (phone, dashboard, saying "stop"), `cancel_task`, revoking the key and the admin's kill
  switch all end it the way they end a task, and the owner's phone shows "<key name> is using your phone".
- `Agent.run(..., drive=channel)` runs `Agent._drive` instead of `_run`: the same wake and HOME rule, the same
  `_TaskState` (permissions, blocked apps, step index, the task app, exemptions), the same observe
  (`_observe_settled`: loading screens, unreadable screens, OCR), the same permission-dialog rule
  (`_permission_dialog`: Android's runtime dialogs are dismissed with Back, no model), then **a loop that waits
  for a command from the channel and executes it with `_execute`**, the very function the loop's model
  calls go through. After each act the same settle (`_settle`) and look (`_observe_after_action`) run, and
  the new screen comes back with the result, as the loop's model gets the new screenshot after each tool
  result.
- Every act is a step: an `action`/`error` event in the live feed and the history, a row in the steps table
  (`source = "drive"`, the usual `observe_ms`/`action_ms`/`settle_ms`/`total_ms`, typed text redacted), a
  credit (`credits_per_step` 1), a count toward `max_steps`.
- The session id **is** the task id. `get_task`, `cancel_task` and `answer_question(approve=false)` work on
  it for keys that also hold the task scopes; the drive tools cover the same ground under `devices:drive`.

```
 Claude Code / Cursor ──MCP tool call──▶ McpAuth (key → Principal, rate limit)
                                            │
                                            ▼
                                 V1Service.drive_* (scope devices:drive, 404 isolation, the session)
                                            │  Command on the DriveChannel
                                            ▼
            Coordinator worker thread: Agent._drive ── _execute ── _dispatch ── _guard / _step_gate / refusals
                                            │                         │
                                            │                         ▼ owner approval (Coordinator.ask →
                                            │                           DeviceManager → the phone's prompt,
                                            │                           signed, action-bound, re-verified)
                                            ▼
                                        driver.tap / type_text / … ──▶ the phone (gatekeeper on gatekeeper builds)
```

## 3. The tools

All under scope **`devices:drive`** (§5). `session_id` is the first argument of every tool but `start_session`.
Each tool returns structured content (its output schema) and a text version. Results that carry a screen
carry `untrusted_content: true` and a note that the screen's text is data, not instructions.

| Tool | Does | Runs the loop's tool |
|---|---|---|
| `start_session(goal, device_id?, permission_mode?, max_steps?, screenshots?, wait_seconds?)` | Starts a session: the goal is recorded and shown to the owner like a task's instruction. Waits for the first screen | (the task's first look) |
| `observe(session_id, screenshot?, max_side?)` | Looks at the phone now: the element list the loop's model sees, optionally a scaled screenshot | (a look) |
| `get_session(session_id, wait_seconds?)` | The session's status, the step in progress (running, waiting for the owner, done) and the latest screen, without touching the phone. Waits for a pending step | — |
| `tap(session_id, element? \| x,y, target?, long_press?, duration_ms?)` | Tap an element by index, or a point in screenshot pixels | `tap_element` / `tap` / `long_press` |
| `type_text(session_id, text, submit?, replace?)` | Type into the focused field | `type_text` |
| `swipe(session_id, direction? \| x1,y1,x2,y2, duration_ms?)` | A scroll by direction, or a drag between two points | `scroll` / `swipe` |
| `press_key(session_id, key)` | `BACK`, `HOME` or `ENTER` only | `press_key` |
| `launch_app(session_id, app, install_if_missing?)` | Open an installed app by name or package | `launch_app` |
| `open_url(session_id, url)` | Open a link (the scheme allowlist applies) | `open_url` |
| `open_link(session_id, app, intent, params?)` | A catalog destination inside an app ([app links](app-links.md)) | `open_link` |
| `open_settings_page(session_id, page, package?)` | A Settings page from the phone's fixed table ([deep links](deep-links.md)) | `open_settings_page` |
| `wait_until(session_id, until, target?, timeout_s?)` | Wait for something observable; returns as soon as it holds | `wait_until` |
| `list_apps(session_id, query?)` | Installed apps (name and package) | `list_apps` |
| `propose_plan(session_id, steps)` | Plan first mode: the plan the owner approves before anything changes | `propose_plan` |
| `decline_step(session_id)` | Decline the act waiting for the owner's approval; the act doesn't run | (answer "no") |
| `end_session(session_id, summary, success?)` | Ends the session with the agent's summary | `done` |

Adjustments to the proposed list, with reasons:

- `tap` takes an element **index** (the loop's `tap_element`, judged by the element's own text, description
  and resource id and re-checked just before the tap) or a **point** (the loop's `tap`, judged by what is at
  that point). `long_press: true` runs `long_press`, judged as a tap of any length. One tool, same arguments
  as the loop.
- `swipe` and `scroll` are one tool: `direction` is the loop's `scroll` (judged on the list it was decided
  from), two points are the loop's `swipe` (a short or quick swipe is a tap; sliders and risky labels at either
  end ask).
- `open_url` is included (the proposal listed `open_link` only): the loop has it, with the scheme allowlist,
  the handler resolved on the phone against blocked apps, and the gatekeeper's `judgeUrl`. An outside agent
  needs arbitrary web links.
- `list_apps` is included: the loop's model has it, and `launch_app`'s "closest matches" error is not enough
  to find an app by a vague name. The feed shows a count, not the list (the existing rule).
- `propose_plan` is included so a phone whose default (or ceiling) is **Plan first** can be driven at all:
  without it every changing act errors with "Plan first: call propose_plan".
- `decline_step` is the agent's half of the approval flow (the owner's half is the phone's prompt). There is
  no `approve_step` and never will be: approving takes the owner's screen lock (or `confirm_approve` on the
  key, through `answer_question`, as for tasks).
- `press_key` is limited to BACK, HOME and ENTER as proposed (the loop allows the whole keycode table, which
  includes POWER). Fewer keys, nothing lost for driving.
- `get_session` is separate from `observe`: `observe` costs a phone round trip and a fresh look; `get_session`
  is free and is how the agent waits for an act the owner is approving.

Not offered (§8): `ask_user` (the outside agent talks to its own user), the fixed `wait` (use `wait_until`),
`plan_steps` (the classifier's executor), batching several acts in one call.

### 3.1 What a result holds

An act returns, once the act has run and the screen has settled:

```json
{"session_id": "4656b5e68dd9", "status": "active", "steps": 3, "max_steps": 40,
 "step": {"id": 3, "tool": "tap", "status": "done",
          "outcome": {"text": "Tapped [7] 'Search'.", "is_error": false, "refused": false}},
 "screen": {"seq": 4, "package": "com.google.android.youtube", "app": "YouTube",
            "screenshot": {"width": 540, "height": 1200, "device_width": 1080, "device_height": 2400},
            "elements": [{"index": 0, "class": "EditText", "text": "", "hint": "Search YouTube", "resource_id": "search_edit_text",
                          "clickable": true, "editable": true, "focused": true, "center": [270, 60], "bounds": [0, 80, 1080, 160]}, "…"],
            "blocked_app": false, "notes": [], "owner_messages": []},
 "next": "Decide the next step from `screen`, or call observe to look again.",
 "untrusted_content": true}
```

- `screen.elements` is the loop's element list (`UIElement`, 150 at most, the phone's cap): index, class, text,
  description, resource id, clickable, checked, enabled, covered (under a dialog or the keyboard: a tap would
  miss), editable, password, focused, hint, `center` in **screenshot pixels** (what `tap x,y` takes, exactly
  as the loop's model is told), `bounds` in device pixels. The text block carries the same lines the loop's
  model reads (`format_elements`), plus the classifier's screen summary when one ran.
- **Redacted:** a password field's text is `<redacted>` (the loop's record redacts typed text; the drive view
  also hides what a password field shows). Nothing else is redacted: the loop's model sees the rest, and so
  must an agent that is to drive.
- **A blocked app in front** (a banking, payment, wallet or password-manager app unless the owner allowed
  them; OpenMob itself; every sensitive app under Don't ask): `elements` is `[]`, there is no screenshot,
  `blocked_app` is true, and the text says so. The only acts accepted then are `press_key BACK` and `HOME`
  (the loop's own way out, `_back_out_if_blocked`); anything else is refused with the blocked-app message.
  The loop ends a task that finds a blocked app in front; the drive session instead goes dark and lets the
  agent leave, because the owner may have opened the app by hand mid-session. Nothing is read or done
  inside it either way.
- `screen.screenshot` is sizes only unless the call asked for pixels (`screenshot: true` on `observe`, or
  `screenshots: true` at `start_session` for every result) **and** the key holds `screen:read`; then the result
  also carries an image block (JPEG, long side `max_side`, 1024 by default) and the call is audited as
  `screen.read`, like `get_screenshot`. Without the scope the structured result says `screenshot_omitted:
  "screen:read"`.
- `screen.notes`: what the loop did on its own between steps, in its words: a permission dialog dismissed
  with Back (and that the app didn't get the permission), a loading screen waited out.
- `screen.owner_messages`: what the owner typed or said into the phone or dashboard while the session ran
  (`Coordinator.steer`). They join the task text for the risky-tap checks, as corrections do for the loop
  (the owner's own words), and are shown to the agent. "stop" from the owner doesn't appear here: it ends the
  session.
- `outcome.refused` is true when the server (or the phone's gatekeeper) refused the act: the text names why
  in the loop's words ("Not allowed on this phone: …"). The `next` hint says to report it, not to retry
  another way.

### 3.2 Approvals

When an act needs the owner (a risky tap, Ask every step, a Settings page that asks, a plan), `_guard`
blocks inside `_execute` on `Coordinator.ask` while the phone shows its prompt. The act's MCP call returns as
soon as the task's status turns `needs_confirmation`:

```json
{"status": "needs_phone_approval", "step": {"id": 4, "tool": "tap", "status": "needs_phone_approval"},
 "question": "I'm about to tap 'Buy now' in Amazon (Total $12.99). Should I go ahead?", "approver": "phone",
 "next": "The owner is approving this on their phone with its screen lock. Call get_session with wait_seconds=45 until the step finishes. You can't approve it; decline_step declines it. Don't submit another act meanwhile."}
```

Then exactly the task flow: the owner approves on the phone (signed with the screen-lock key over the
action's digest; `_approval_mismatch` re-reads the screen and refuses if the target moved), or denies, or
two minutes pass (a no). `get_session` returns the step's outcome and the new screen. `decline_step` answers
"no" the way `answer_question(approve: false)` does. A key the owner let approve (`confirm_approve`) is told
(`approver: "key"`) to approve or decline through `answer_question`, as for tasks. One act is in flight at a
time: another act or `observe` while a step is pending is refused (`step_in_progress`); `get_session` waits.

## 4. The loop, reused

`Agent._drive` is the loop with the model call replaced by "wait for a command":

```
_prepare(state, go_home)                       # wake; HOME only if asked and the launcher isn't in front (as _run)
obs = _observe_settled(state)                  # loading screens, unreadable screens, OCR: as _run
loop:
    stop if cancelled (Stop on the phone or dashboard, cancel_task, revoked key, kill switch, question timeout)
    obs = _dismiss_dialogs(state, obs)         # Android permission dialogs: Back by rule, as _run (no model)
    _apply_corrections(state)                  # the owner's mid-session words join the task text, as _run
    publish the view (observe / get_session read it without touching the phone)
    cmd = channel.next(idle timeout, max duration)
    observe  → obs = _observe_settled(state); reply
    end      → _execute("done", …); return
    act      → if a blocked app is in front and the act isn't BACK/HOME: refuse
               outcome = _execute(name, args, obs, state, source="drive")   # _dispatch → every check
               event (feed, history, steps count), steps += 1
               _settle(...) ; obs = _observe_after_action(state, obs)       # as _run after a tool result
               reply (outcome, new screen); end at the step cap
```

`_execute` is unchanged and shared: it is where `_dispatch` runs the scheme allowlist and the password-field
refusal before anything else, Plan first, Ask every step (`_step_gate`), `_guard` for taps, swipes, ENTER and
Settings pages, `_launch_app`'s blocked-app check, `_open_link`'s and `_open_settings`'s checks, the
`_recheck_target` TOCTOU re-read, the permission-dialog tap refusal, the gatekeeper's refusal codes
(`DeviceError` with a code), and the step record. Two small refactors make the loop's prelude shareable
(`_prepare`, `_dismiss_dialogs`); `_run` calls them and behaves as before.

One tightening the loop doesn't have: **a drive session refuses typing into a field that looks like a one-time
code field** (hint, resource id or description naming a verification/OTP/2FA/security code), with the
password-field refusal. The loop types login codes on purpose (`docs/mcp.md`'s orchestration example
reads a code on one phone and types it on another under the owner's instruction). An outside agent that
read a code off the owner's Messages app and typed it into a login is the account-takeover shape, so the
drive level fails closed there. It is a `_TaskState` flag (`refuse_otp`) checked in the same `_typing_refusal`
the loop uses, not a second guard. Whether the loop should refuse too is an owner question (§10).

Not run in a drive session: the model, the classifier's fast path, replay, plan-execute, the direct first
steps (links and Settings pages from the request). The outside agent is all of those. `wait_until` and the
loop's own waits (after a launch, during an install) run as always: they only read.

## 5. Scopes and modes

- **`devices:drive`**, new, **never a default**, marked sensitive in the developer console (the same
  confirmation as `screen:read`): the key may start and drive sessions and read the element list (the
  screen's text; one can't drive blind). Screenshots additionally need `screen:read`, as everywhere.
- The task scopes are not needed for the drive tools; a key with only `devices:drive` can start, observe,
  act, decline and end. With `tasks:read`/`tasks:run` it can also use `get_task`, `cancel_task` and
  `answer_question(approve=false)` on the session, which is one of its tasks.
- **`max_mode`** caps the session's permission mode exactly as a task's (`V1Service.permission_mode`), and the
  phone's ceiling applies: a looser request is `mode_not_allowed`. The default is the phone's default mode
  tightened to the key's `max_mode`. `never` is allowed when the owner allowed it for the key and the phone
  (an owner question, §10).
- The session's blocked apps are the task's: sensitive apps unless the owner's `allow_sensitive_apps`,
  OpenMob itself always, every sensitive app under Don't ask.

## 6. Limits

| Limit | Value | Where |
|---|---|---|
| Requests per minute | the key's `api_rate_per_minute` (60): every drive call is a request | `McpAuth` → `V1Service.authenticate` |
| Unfinished tasks per key | `api_max_concurrent_tasks` (2): a session is one | `start_drive` |
| Tasks per day, credits per day, the account's | as tasks | `start_drive` |
| Acts per session | `max_steps`, at most `api_max_steps` (40); the session ends at the cap (`step_cap`) | `_drive` |
| Idle | `api_drive_idle_seconds` (120): no command for that long ends the session (`idle_timeout`) | `_drive` |
| Duration | `api_drive_max_seconds` (1800) | `_drive` |
| Acts in flight | one; a second is `step_in_progress` | `DriveChannel` |
| One per phone | a busy phone (a task running or queued, a session active) refuses `start_session` (`phone_busy`); a task started while a session is active is refused the same way | `start_drive`, `create_task` |
| The owner's approval | 2 minutes, then no (`DeviceManager.approval_timeout`); a question nobody answers ends the task after `question_timeout_seconds` | existing |

On gatekeeper phones the phone's own act rate limit (5/s, burst 15) and prompt limits apply on top.

## 7. Visibility

- The dashboard feed: "[drive] <goal>", "Working on: <goal>", a status line naming the key ("Driven step
  by step by <key name>"), one `action`/`error` line per act in the loop's words (the feed sees `shown`
  texts: a link's label, an app count, never typed text), the approval questions, the outcome.
- The phone: "<key name> is using your phone" (the controller line) while the session runs; approval prompts
  worded by the server (or, on gatekeeper builds, by the phone) with the key's name as the requester.
- History: a turn with the goal, the outcome, the mode and the prompt count; one steps row per act with
  timings and `source = "drive"`, so `openmob timings` can compare drive steps with model steps.
- The developer console: the session as a task (tier `drive`), its events, the audit entries `drive.start`,
  `task.finished`, `task.cancel`, `task.answer` (a decline), `task.phone_approval`, `screen.read`.
- `list_devices` shows the phone `busy`.

## 8. Deliberately excluded

- A REST form of the drive tools. The service layer is transport-free, so `/v1/drive/…` could follow; MCP is
  what outside agents use today.
- Several acts per call, or acts without a look in between. The loop's `max_actions_per_turn` batching looks
  between actions anyway; the outside agent does the same with one call per act.
- Keys other than BACK, HOME, ENTER; `ask_user`; a fixed `wait`; the classifier's `plan_steps`.
- Reading the screen without a session (`get_screenshot` exists for that, under `screen:read`).
- Any path to the phone that skips `_execute`: there is none, and the tests pin the ones that matter.
- Sessions on token/dev-mode servers: `/mcp` exists only with accounts, as before.
- Approving from the agent's side, in any mode, ever.

## 9. Latency per tool call (estimates)

Measured loop step costs on the owner's Samsung (2026-10-08, deep-links.md §6, app-links.md §1): observe
0.07–0.45 s (0.07 for the element list alone on the app phone, 0.45 with a screenshot), the classifier
0.15 s, settle 0.5–1.5 s (adaptive: `wait_idle` returns at 300 ms of quiet, cap 0.8 s after a tap, up to 3 s
plus loading after a launch), a phone action 0.05–0.2 s, a Claude decision 1.1–1.5 s. The drive level has no
model call of its own; the outside agent's thinking is on top and is not OpenMob's.

| Call | Estimate | Made of |
|---|---|---|
| `observe` (elements) | 0.1–0.4 s | MCP round trip 10–30 ms on a LAN (100–200 ms through Fly) + `_observe_settled` 0.07–0.3 s |
| `observe` (with a 1024 px screenshot) | 0.3–0.7 s | + the capture 0.3 s + scaling 20–50 ms + 50–150 KB on the wire |
| an act (`tap`, `type_text`, `swipe`, `press_key`) | 0.6–1.5 s | guard µs; `_recheck_target` one element read ~50 ms; the gesture 50–100 ms; settle 0.3–0.8 s; the look 0.07–0.3 s |
| `launch_app`, `open_url`, `open_link`, `open_settings_page` | 1.5–4 s | the launch settle (up to 3 s + loading) dominates, as in the loop |
| `wait_until` | until the condition holds | the phone's `wait_for` chunks of 10 s, or polling |
| an act that asks the owner | 0.3 s + the owner | the call returns when the prompt is up; `get_session` waits |
| `get_session`, `decline_step`, `end_session` | 10–200 ms | no phone round trip |
| `start_session` | 0.5–1.5 s | wake (0 if the screen is on, up to 2 s otherwise) + the first look |

So a step costs about what a loop step costs without its model time: roughly 1–1.5 s for a tap and its look,
against 2–3 s with a Claude decision. Whether an outside agent is faster overall depends on its own model.

## 10. Open owner decisions

1. **Don't ask for drive sessions.** A key with `max_mode: never` on a phone where the owner turned Don't ask
   on can drive without prompts (purchases, autofill and blocked apps still refused). That mirrors `run_task`.
   Should the drive level be capped at `trusted` regardless, since the deciding model is not OpenMob's?
2. **Screenshots by default.** Off here: elements only unless asked (`screenshots: true`) and `screen:read`.
   Should `devices:drive` imply pixels (one scope to grant), or stay as two?
3. **The element list under `devices:drive` alone.** The list holds every text on screen. It is treated as
   part of driving (you can't drive blind) and the scope is marked sensitive. Alternative: require
   `screen:read` for `observe` too.
4. **OTP fields.** The drive level refuses typing into them; the loop doesn't (it types login codes under the
   owner's instruction). Align the loop, or keep the asymmetry?
5. **Idle timeout and duration** (120 s, 30 min) and the step cap (the task cap, 40). An outside agent that
   thinks for a minute between steps is fine; one that goes away holds the phone for 2 minutes.
6. **Credits.** A drive step is charged 1 credit (no model cost to OpenMob). Should it be 0, or the phone's time?

## 11. Threat model

| # | Attacker | What it can do | How it's contained (code) |
|---|---|---|---|
| T1 | **A malicious outside agent** holding a `devices:drive` key: it can read every screen's text and send any act | Only what the loop's model could: each act is `Agent._execute` → `_dispatch` → `_guard` (`openmob/agent/loop.py`). Blocked apps at launch, URL handler and in front (`_is_blocked`, `_blocked_handler`, `_back_out_if_blocked`; the drive loop's dark screen and BACK/HOME-only). Password fields and OTP fields never typed (`_typing_refusal`); `javascript:`/`intent:`/`file:`/`content:`/`data:` never opened or typed (`gate.url_refusal`, `gate.dangerous_text`). Risky taps judged by the **real element** (`_guard_point`, `tap_element`'s own fields), never by the agent's `target` words, which only add caution. Money always asks; sends, deletes, posts ask unless requested in the goal (once, in the task's app) or trusted by the owner (`_covered`). Settings: security-critical pages refused, everyday pages free, the rest ask (`gate.settings_tier`). The permission mode is capped by `max_mode` and the phone's ceiling (`V1Service.permission_mode`, `DeviceManager.check_mode`). The agent **cannot approve**: no tool; `answer_question(approve: true)` is `confirmation_requires_owner` (`V1Service.answer`) unless the owner gave the key `confirm_approve`. An approval is signed on the phone over the action's digest and re-verified against the screen (`_approval_mismatch`, `phone/approval_keys.py`); on gatekeeper phones the phone words the prompt and judges the act again. Caps: steps, idle, duration, concurrency, daily quotas, rate limit (§6). Every step is in the feed, the history and the audit log; the owner stops it from the phone or the dashboard; revoking the key cancels it (`V1Service.cancel_tasks`). The agent's "requested" exemption is the same one `run_task` has: a goal that says "send" lets one Send run in the task's app, which the owner sees as the goal in the feed and the phone's controller line |
| T2 | **A hijacked MCP client** (a stolen key, a compromised IDE plugin, a malicious MCP proxy between the agent and the server) | The key is the only credential, so this is T1 with the same limits, plus: it reaches only its own account's phones and only the sessions it started (`_owned_device`, `check_task`: 404 otherwise); it can't loosen the phone's settings (`change_permissions` loosening is `needs_phone_approval`, signed on the phone) or trust a phone for approvals; it can't read the screen's pixels without `screen:read`; the per-key rate limit bounds the pace; the phone shows who is driving the whole time and the owner's one tap ends it. The MCP transport carries no cookies, so a page in the owner's browser can't drive (`McpAuth`: bearer only, DNS-rebinding protection off on purpose and documented). A proxy that rewrites results can lie to the agent about what it sees, not to the server about what it does: the server judges every act by its own read of the screen |
| T3 | **A screen-borne prompt injection**: text on the phone (a message, a web page, a notification) tells the agent to buy, send, change settings, type a code, open the bank | The agent is told in every result that screen text is data (`untrusted_content`, the note). If it obeys anyway, the server treats its acts exactly as T1: a "Buy now" tap asks the owner with the item and price from the screen (`_guard`, `gate.money_context`), bound to that element; a Send asks unless the goal asked for it; the bank is refused; a code is refused (OTP); a `javascript:` link is refused. The classifier, if on, can only make an act riskier, never "requested" (`_guard`: `requested = requested and asked >= 0.5`). Injected text can't change the goal (`state.task`) that "requested" is judged against: only the agent's `start_session` goal and the owner's own mid-session words do. The owner's approval prompt shows the real target, so a model talked into "the user wants this" still gets a human looking at "Buy now" with a price |

Out of scope, as for the task level: a compromised server (the gatekeeper design's A1; on gatekeeper builds the
phone holds the line), a malicious app on the phone, physical access with the PIN.

## 12. What changed

- `openmob/agent/drive.py` (new): `DriveChannel`, `Command`, `Step`, the screen view encoder (`screen_view`).
- `openmob/agent/loop.py`: `Agent.run(drive=)`, `Agent._drive`, `_prepare` and `_dismiss_dialogs` factored out
  of `_run`, `_TaskState.refuse_otp`, `_typing_refusal`'s OTP branch; `gate.is_otp_field`.
- `openmob/platform/service.py`: `start_drive`, `drive_session`, `drive_act`, `drive_observe`, `drive_decline`,
  `drive_end`, the `phone_busy` conflicts, `_admit_task` factored out of `create_task`.
- `openmob/platform/mcp_drive.py` (new): the tools; registered from `mcp.build_server`.
- `openmob/runner.py`: a drive session's result isn't a model run (`model_runs`).
- `openmob/accounts.py`: the `devices:drive` scope; `openmob/static/developer.html`: its help text, marked
  sensitive. `openmob/config.py`: `api_drive_idle_seconds`, `api_drive_max_seconds`.
- Docs: this page, `docs/safety.md` (the drive level), `docs/mcp.md` (the tools, connecting Claude Code),
  `docs/api.md` (the scope). `docs/phone-protocol.md` is unchanged: nothing new reaches the phone.
- Tests: `tests/test_drive.py` (the loop, with the fake driver), `tests/test_mcp_drive.py` (the tools, over
  the real server with fake phones).
