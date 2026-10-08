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
- **Fast path:** when the classifier decides to launch an app and the request is plain navigation to one
  Settings page by name ("Open Settings and go to Display", "show me the battery page"), the fast step opens the
  page directly instead of launching Settings (`Agent._direct_settings_page`). Any other app named, a side
  effect, or a page that would ask leaves the usual launch in place. The classifier's DONE then works as before
  (the page's title is on screen).

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
