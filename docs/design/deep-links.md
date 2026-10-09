# Deep links: going straight to a destination

Status: built 2026-10-08 (branches `feature/deep-links` and `feature/deep-links-gk`), not yet measured on a
phone. The owner's focus is latency and navigation; this is the largest untried lever for Settings tasks.

## 1. Why

Measured on the owner's Samsung (2026-10-08, Haiku 5.5, Jev on): "Open Settings and go to Display" takes
**5–10 steps and 14–21 s**: launch, scroll, tap, each step about 2–3 s (observe ~0.3 s, Jev ~0.15 s, settle
0.5–1.5 s, a Claude decision ~1.1–1.5 s when one is needed). Replaying a stored path brought it to 7.4 s,
near the floor for doing it tap by tap. Going straight to the page skips the taps altogether.

MVP targets (owner, 2026-10-08): first-run labeled-app navigation median ≤ 12 s, p90 ≤ 20 s; repeat
navigation ≤ 6 s; per step ≤ 1.5 s; first action ≤ 2 s; success ≥ 95 %.

## 2. Design

### 2.1 The phone holds the table

A new phone method, **`open_settings {page, package?}`** (docs/phone-protocol.md §4.17), opens one Settings
page. `page` is a **key** from a fixed table in the app (`SettingsPages.kt`, the same in both builds); the
phone maps it to an Android Settings intent action itself. The server never sends an action, a component, an
extra or a URI, so a compromised server reaches only the pages in the table, and unknown keys are refused on
the phone. The server's copy of the table (`openmob/phone/settings_pages.py`) is compared with the Kotlin
file by a test.

| Key | Intent action | Tier | Notes |
|---|---|---|---|
| `display` | `android.settings.DISPLAY_SETTINGS` | everyday | |
| `wifi` | `android.settings.WIFI_SETTINGS` | everyday | |
| `bluetooth` | `android.settings.BLUETOOTH_SETTINGS` | everyday | Pairing a new device still asks at tap time |
| `sound` | `android.settings.SOUND_SETTINGS` | everyday | |
| `battery` | `android.intent.action.POWER_USAGE_SUMMARY` | everyday | The Battery page on every Android |
| `location` | `android.settings.LOCATION_SOURCE_SETTINGS` | everyday | |
| `notifications` | `android.settings.NOTIFICATION_SETTINGS` | everyday | Not a public SDK constant (AOSP Settings handles the action); an OEM without it errors and the agent navigates |
| `apps` | `android.settings.MANAGE_APPLICATIONS_SETTINGS` | everyday | The all-apps list; App info actions are judged at tap time |
| `storage` | `android.settings.INTERNAL_STORAGE_SETTINGS` | everyday | |
| `airplane_mode` | `android.settings.AIRPLANE_MODE_SETTINGS` | everyday | |
| `date_time` | `android.settings.DATE_SETTINGS` | **ask** | Manual date and time asks (phone-gatekeeper.md, changes since review 2, N1) |
| `app_details` | `android.settings.APPLICATION_DETAILS_SETTINGS` + `package:<pkg>` | everyday, **package required** | Refused for blocked apps and OpenMob itself |
| `app_notifications` | `android.settings.APP_NOTIFICATION_SETTINGS` + extra `APP_PACKAGE` | everyday, **package required** | Refused for blocked apps and OpenMob itself |

**No key exists** for the pages the gatekeeper design hard-denies (§2.3 of docs/design/phone-gatekeeper.md):
accessibility, language and region, security and screen lock, accounts and users, developer options and
wireless debugging, VPN and Private DNS, special app access, default apps, keyboards. The gatekeeper also
checks every key's action against its hard-deny list, so a key added for one of those pages later would still
be refused on a gatekeeper phone.

The phone validates `package` (a well-formed application id, installed, not OpenMob's own), resolves the
intent before starting it, and answers `{"package": <the Settings app that opened>}`. A phone without the page
(an OEM that doesn't handle the action) replies with a plain error, so the agent falls back to navigating.

The app advertises the capability `open_settings` in its `hello`; older apps aren't asked.

### 2.2 The server

- **Tool `open_settings_page {page, package?}`** (`openmob/agent/tools.py`), with the keys as an enum, offered
  only when the phone can open pages directly (the capability, or an adb phone: `am start -a <action>` from the
  same table). A model that calls it anyway on an older phone gets a clear error.
- The prompt says to prefer it for Settings pages and to navigate if it errors.
- It runs through `_execute`/`_dispatch` like every tool: **Ask every step** asks before it, **Plan first**
  needs the plan, and the page's tier is judged on the server before the phone is asked: the **stricter** of the
  table's tier and this server's own Settings word lists (`settings_refuse_terms`, `settings_ask_terms`, the
  ones that judge taps on Settings pages), so a deep link reaches nothing a tap couldn't. With the default
  lists, `date_time` is therefore **refused** by the server ("date & time" is on the refuse list in
  `openmob/permissions.py`; docs/safety.md says so), though the gatekeeper design would ask. The owner can align
  the two by editing the list.
- An ask-tier page asks the owner with the usual action-bound prompt, and is refused under Don't ask.
- `app_details` and `app_notifications` take the app's name or package (resolved like `launch_app`) and are
  refused for an app the task may not touch (blocked apps, OpenMob itself).
- It settles like a launch (`launch_settle_delay`, `loading_done`), records a step like any other, and ends a
  payment's follow-up like an app switch.
- **The direct step** (`deep_links_fast`, on by default; `OPENMOB_DEEP_LINKS_FAST=off` to compare): a request
  that plainly names one Settings page opens it as the task's **first action**, before any classifier or
  main-model decision, the way a plain link request does (docs/design/app-links.md §2.4): "Open Settings and go
  to Display", "show me the battery page", and an app's own pages: "open Chrome's notification settings", "Open
  Settings, go to Apps, open Chrome, open its Notifications settings, and tell me whether notifications are
  allowed" (`app_notifications`), "show me Chrome's app info", "Open Settings, go to Apps, open Chrome"
  (`app_details`). The request must only navigate (`fast.navigation_and_read_back`), may end with a read-back
  clause ("tell me whether…", "is it on?") that the main model answers from the page that opens, must name the
  page exactly, and must name no other app on the way. The app's name is resolved with the resolver's **exact**
  lookup (an installed app's label or an alias, case and spacing aside): no fuzzy match, no model, so "Chrom's
  notifications" is the main model's call. A page that asks or is refused, a blocked app and OpenMob itself are
  never opened this way (the model's own call is then refused by the tool as before). It runs through
  `_execute` like any tool: Ask every step asks first, Plan first needs the plan, the gatekeeper judges it on
  the phone. The step is recorded `fast = 1`, `source = "deep_link"`, and the variant gains `+pages`. Jev isn't
  needed. The task's first look doesn't ask the classifier about the screen it is leaving (the look after the
  step does, as usual); when the step steps aside after all, the classifier is asked before the usual path.
- **The classifier's deep link:** with the direct step off (or a request it doesn't read), the fast path still
  opens the page instead of launching Settings when the classifier decides to launch an app and the request is
  plain navigation to one page (`Agent._direct_settings_page` inside `_fast_step`), as built first. The
  classifier's DONE then works as before (the page's title is on screen).

### 2.3 The gatekeeper (`feature/deep-links-gk`)

`open_settings` is an **act** method, judged like opening that Settings page under §2.3 of the gatekeeper design:

| Page | Decision |
|---|---|
| everyday pages | allowed |
| `date_time` | asks (kind `settings`, screen lock; refused under `never`) |
| `app_details` / `app_notifications` for a blocked app or OpenMob | refused, `gate_blocked_app` |
| an unknown key | refused, `gate_denied_page` |
| a key whose action or title is on the hard-deny list (none today) | refused, `gate_denied_page`, in every mode |

Nothing existing is relaxed: the gate's tap rules on the page that opens are unchanged, the Python reference
engine (`tests/gatekeeper_ref.py`) judges the same way, and the shared scenarios cover each row.

## 3. Expected gains (estimates, not measured)

- "Open Settings and go to Display": today 5–10 steps, 14–21 s. With the deep link: **1 action + 1 confirming
  look**, so about **3–5 s** when the main model decides (one ~1.5 s decision, a ~1 s launch settle, one
  observe), and **~2–3 s** on the fast path (no main-model call before the action; the classifier confirms
  done, or the main model does in one more look).
- An app's notification page ("turn off Chrome notifications"): today launch Settings, Apps, find the app,
  Notifications (4–6 steps); with the deep link one action, so the same 3–5 s plus the toggle.
- Nothing changes for apps other than Settings.

These are estimates. The measurement plan below is what decides.

## 4. Other deep links worth adding later

- **App `https` links already work** through `open_url`, including verified app links (YouTube, Maps, Spotify
  web links open in the app when the app is the verified handler). That covers most "open X in app Y" requests
  without any new method.
- **App-specific intents and custom schemes need care.** The server refuses custom schemes on purpose
  (`intent:`, `upi:`, app schemes into payment apps). A per-app table like this one (key → intent) would work,
  but each entry needs the gatekeeper tiering done by hand, and OEM variance is higher than for Settings. Not
  before measurement shows Settings pages pay off.
- **More Settings pages**, if measured use calls for them: mobile data and hotspot (`DATA_ROAMING_SETTINGS`,
  `WIRELESS_SETTINGS`), NFC, battery saver, Do Not Disturb (no public action), About phone
  (`DEVICE_INFO_SETTINGS`: the Build number row there is in the ask tier at tap time), a specific app's
  permission page (needs a package and the permission dialogs' rules).

## 5. Measurement plan

Four tasks, each cold (Settings not running, from the home screen) and warm, deep links **on vs off**
(`capabilities` with and without `open_settings` on the phone, or the tool removed), on the Samsung
(SM-S948U1) and on an emulator (Pixel, API 35):

1. "Open Settings and go to Display";
2. "Open the Battery settings";
3. "Open the Wi-Fi settings";
4. "Open Chrome's notification settings" (`app_notifications`).

Record per run from the steps table in `<data>/history.sqlite` (`observe_ms`, `model_ms`, `settle_ms`,
`total_ms`, `fast`, `source`): steps, total time, first-action time, success. Five runs per cell; report the
median and p90, and whether the first action was the deep link. Expect the Samsung to differ from the
emulator on `notifications` and `apps` (OEM Settings), and on how long the Settings activity takes to settle.
Jev on for the fast-path cells, off for the main-model cells. Keep measured numbers separate from the
estimates in §3.

Not done yet: no phone was driven for this work (owner rule), so every number in §3 is an estimate.

## 6. First-action polish (2026-10-08, branch `perf/first-action-polish`)

Target: the first action on screen within 2 s (median). Measured on the owner's Samsung that day (`main`
53112da; the first step's `total_ms` in the steps table): **median 2.2 s** over the first-run task set.

### 6.1 Where the first step's time went (measured, 179 first steps on 2026-10-08)

| First step | n | total (median) | where it goes |
|---|---|---|---|
| a fast link (`open_link`, YouTube/Maps/Play/Spotify/Amazon/Google searches) | 25 | **2.3 s** | settle inside the step 1.8 s (the page landing and its quiet/loading settle), classifier 0.16, observe 0.07, action 0.06, other 0.07 |
| a fast Settings page (`open_settings_page`) | 10 | **1.1 s** | the landing wait 0.7 s (until the page drew), classifier 0.14, observe 0.07, action 0.05, other 0.07 |
| the main model decides (vision step) | 42 | **2.5 s** | model 1.3–1.4 s, observe 0.45 s (the screenshot), classifier 0.15, other 0.15–0.2, action 0.06–0.19 |
| a fast launch, the text model names the app | 14 | **1.15 s** | text model 0.7 s, classifier 0.16, observe 0.08, action 0.1 |
| a fast launch, exact app name | 23 | **0.63 s** | other 0.35, classifier 0.16, observe 0.07, action 0.06 |

"Other" (total minus the phases) was 117 ms median, 621 ms p90, 1.6 s max on first steps against 2 ms on
second steps. It spiked on the first task after an idle gap (0.5–1.2 s), which matches the wake: the app's
`wake_unlock` returns at once when the screen is on and waits up to 2 s for it to turn on plus 1.5 s for a
swipe keyguard otherwise. The Chrome-notifications request on the Samsung: Haiku chose
`open_settings_page app_notifications` in 5 of 7 evening runs (2 steps, 4.4–5.4 s); on the emulator 0 of 8.

So the 2.2 s median was mostly the **landing settle counted inside the link step**: the `open_url` itself was
sent within ~0.4 s of the request. The other half of the picture: a vision first step can't get under 2 s
(the model alone is 1.3 s plus the screenshot), so the median depends on how many first steps are direct.

### 6.2 What changed, with the gain each should bring (estimates, not measured)

1. **Link steps end when their app is in front.** `_open_destination` used to settle the whole page load
   inside the step "so the result can say where it landed". Now it waits only until the screen left the one
   the link was opened from (`_await_new_screen`) and reads the app in front: when that is the entry's app
   (or a browser where that is fine), the step ends and the quiet/loading settle is the next step's, like a
   launch and as the steps table defines settle ("on the step it delayed"). When the quick look can't place
   the landing (the home screen, or neither the app nor a browser: the old screen may just have redrawn),
   the step settles fully and looks again, exactly as before, so the landing report keeps its accuracy.
   *Estimate:* the first step of a link task from ~2.3 s to ~1.0–1.3 s; the task's total unchanged (the same
   wait, on the next step). adb phones (no quick element list) keep the full in-step settle.
2. **The direct Settings step** (§2.2): a plain Settings-page request, an app's page included, opens before
   any classifier call. *Estimate:* for "Open Settings, go to Apps, open Chrome, open its Notifications
   settings, and tell me whether…", 5 model-decided steps (12.5–13.1 s on the emulator) become one direct
   step plus one model read-back: about 3–4 s on the Samsung, 4–6 s on the emulator; where Haiku already
   chose the page (the Samsung, 2.4–2.7 s first step) the first step drops to about 1.0–1.2 s (no model
   call, no screenshot, no classifier call before it).
3. **The first look skips the classifier when the first action is known** (a link or a page):
   *estimate* −0.13 to −0.24 s (the measured `jev_ms` on first steps) on every direct first step. If the
   step steps aside after all, the classifier is asked then, so the cost of a wrong guess is nothing.
4. **HOME only when the launcher isn't in front** (`go_home`: phone-page requests, `openmob bench`, the API's
   `clean_start`): one `current_app` read (~20–50 ms) instead of the press and its settle. *Estimate:* −0.3 to
   −0.8 s on such tasks when the phone is already home; nothing otherwise.
5. **The wake and the HOME press are timed** as the first step's `action_ms` (they were "other"), so the next
   measurement attributes them. No gain; the wake itself is the phone's.
6. **The prompt nudge** (`SETTINGS_PAGE_LINE`, the tool's description): a request that spells out the taps
   still wants the page; `app_notifications` with the app's package gets there; the read-back is answered
   from the page. *Estimate:* for the main model on the emulator, 5 steps to 2 when it follows the nudge.

Not changed: `_guard`, the gatekeeper, approvals, blocked apps, the tool's tier check. Nothing opens that
the model's own `open_settings_page` call couldn't.

### 6.3 How to measure

The same plan as §5, plus the Chrome-notifications request, cold, five runs per cell, `deep_links_fast` on
vs off (`OPENMOB_DEEP_LINKS_FAST`). From the steps table: the first step's `total_ms`, `source`
(`deep_link`, `link`, or none), `jev_ms` (0 on a direct first step), `settle_ms` on steps 1 and 2 (the link
settle moved), `action_ms` on step 1 (the wake), and the task's step count and model calls. Expect the
first-step median over the first-run set to fall to about 1.0–1.3 s if the estimates hold; a vision first
step stays at about 2.5 s.
