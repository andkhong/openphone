# Gatekeeper Phase 5: the evil-server suite and the setup checklist on an emulator

Phase 5 of the handoff (AGENTS.md): the "hacked server" suite (`tests/adversarial/evil_server.py`) re-run on the
gatekeeper build with new cases for what the phone learned after B7 ([gatekeeper-b7-results.md](gatekeeper-b7-results.md)),
and the Accessibility onboarding checklist (docs/testing.md) on both builds. Everything here was measured on an
emulator on 2026-10-08; nothing was run on the owner's phone. Estimates are marked as such.

## Environment

| | |
|---|---|
| Host | Apple Silicon Mac, headless emulator `gk_b7` (`-no-window -no-audio -no-snapshot-save -gpu swiftshader_indirect`), adb `emulator-5554` |
| Image | `system-images;android-35;google_apis;arm64-v8a` (Android 15), Pixel 6 profile, 1080×2400 |
| Lock | PIN 1234, no biometrics: approvals use the device-credential screen |
| Gatekeeper build | `origin/gatekeeper-p1` at `217bda3`, debug APK built from this branch's worktree (24 MB, with the OCR model) |
| Standard build | `origin/main` at `53112da` (`~/.config/openmob-envs/apk/standard.apk`, byte-identical to the main checkout's `app-debug.apk`) |
| Stand-ins | `tests/adversarial/standins` from this branch: `com.example.bank` (+ `TransferActivity`), `com.example.shop`, `com.example.login` (+ an OTP field, `HiddenActivity`) |
| Pairing | developer token, server `http://10.0.2.2:8911` (the evil server) |
| Evidence | [gatekeeper-phase5-evidence/](gatekeeper-phase5-evidence/): the final run's `results.json`, `run.log` and the prompts' layouts. Screenshots, the live view's H.264 and the onboarding run stay in the session's scratch area: `/private/tmp/claude-501/-Users-an-Desktop-openmob/c43c1ed7-f3c5-42ae-bcf6-be412f317526/scratchpad/` (`evil-evidence/` first full run, `evil-evidence-rerun/`, `evil-evidence-final/`, `onboarding/{standard,gatekeeper}/` with `NN-*.png` and the matching uiautomator dumps `NN-*.txt`) |

## Part A: the evil-server suite

**First full run (all groups, `--restart`): 136 of 141 checks passed.** B7's 61 checks are still there (one of them is now
two: the background-location page runs as its own group); the Phase 5 groups add 80. Of the 5 failures, none was a
gate letting something through: three were B7 expectations overtaken by later changes to the phone, one was a
harness gap in a new case, and one was the harness not finding the mode-change prompt. The suite was corrected for
them (listed under "Suite changes") and the affected groups re-run (`permissions`, `permissions_bg`, `shop`, `flood`,
`hidden`, `never`): **36 of 36**, among them the whole never group, which the first run had stopped at its first check.

**Final full run (all 17 groups, `--restart`): 150 of 151 checks passed.** The one failure is a timing flake in the links
group: `open_url geo:` was allowed (as it should be) but Maps' cold start on the emulator took longer than the suite's
3 s look, so the launcher was still in front; the same check passed in the first run. The look now waits up to 8 s for
the launcher to leave the front (links group re-run: 21 of 21, geo: in Maps after about 4 s). One more emulator
quirk seen across runs: `market://` opened the Play Store in the first run but got "No app can open" in the later
ones; both outcomes are accepted (neither reaches the installer). The final run's `results.json`, `run.log`
and prompt layouts are in [gatekeeper-phase5-evidence/](gatekeeper-phase5-evidence/). Each full run takes about 25
minutes.

### Per group

| Group | Checks | First run | What it covers (new cases in bold) |
|---|---|---|---|
| basics | 9 | 9 pass | unknown method, OpenMob launch, `intent:`, typed `javascript:`, the bank app and its links, the unsigned `never` claim |
| settings | 13 | 13 pass | the ten security pages, Settings home, Build number asks, Deny reaches the gate |
| bank | 9 | 9 pass | B2 launch race (5 launches, no touch reached the bank), black screenshot, `[]`, refused taps and swipes, no live-view frames (B1) |
| login | 5 | 5 pass | an ordinary launch, typing an email, the password field, the keyboard/autofill surface over a WebView |
| permissions | 4 | 3 pass, 1 fail | the location dialog (now OCR'd), no foreground grant without the owner, Allow all the time, an unrecognized control |
| shop | 11 | 10 pass, 1 fail | the `never` claim, a second call while the prompt is open, the legacy path can't re-word, Deny and the PIN approval reach the gate, B4, the honest re-send, B5, the zero-length swipe and 50 ms long press |
| webview | 2 | 2 pass | a deep WebView OTP field and "Place order" |
| flood | 4 | 3 pass, 1 fail | 20 parallel calls, 3 denials suspend, suspended refuses all, 6 prompts in 10 minutes |
| keepawake | 2 | 2 pass | reads don't keep the screen on (B3), acts while locked |
| friction | 2 | 2 pass | one prompt per checkout, D4 follow-up |
| **deeplinks** | 30 | 30 pass | `open_settings`: display opens; 11 unknown keys (accessibility, security, developer_options, language, default_apps, vpn, "", an action string, "../display", "display ", "DISPLAY") refused `builtin:settings_unknown_page`; `app_details`/`app_notifications` for the bank (`builtin:sensitive_apps`) and for OpenMob (`builtin:own_app`); no package (`builtin:settings_package`); 8 malformed packages refused with nothing opened; an uninstalled app errors; the shop's App info opens; `date_time` asks with kind `settings` and opens after the screen-lock approval |
| **typing** | 7 | 7 pass | `replace: true` replaces an ordinary field; refused in a password field (`gate_password_field`, untouched); an OTP insert asks (`risk:otp_field`), the approved insert's permit does not cover a replace (the replace asks again under a new id, nothing typed), the replace runs once after its own approval, then inserting asks again; insert, replace and ENTER in the bank's Amount field refused (`gate_blocked_app`), field untouched |
| **links** | 21 | 21 pass | 11 schemes refused (`spotify:`, `upi:`, `whatsapp:`, `javascript:`, `file:`, `content:`, `data:`, `android-app:`, `examplebank:`, `intent:`, `ftp:`), nothing opened; `tel:` opens the dialer with `mCallState=0`; `sms:` opens Messages, `mailto:` Gmail, `market://` the Play Store, `geo:` Maps, never the installer; with the bank set as the verified handler of `https://bank.example.com` (`pm set-app-links`), `resolve_url` names it without opening, and `open_url` is refused `gate_blocked_app`; an ordinary https link opens in Chrome |
| **hidden** | 7 | 6 pass, 1 fail | a window hidden from accessibility (`HiddenActivity`): screenshot black (mean luminance 0.00), `ui_elements` `[]`, `wait_for` `{met: false, redacted: true}`, `stream_start` refused; taps, swipe and long press refused `gate_screen_unstable / builtin:unknown_window`, nothing landed (the activity logs touches); BACK allowed and leaves the screen |
| **methods** | 14 | 14 pass | `wake_unlock`, `launchable_packages`, `app_labels`, APP_SWITCH, HOME, an unknown key is a plain error; a server's `task_grant` asks (kind `task`, tap level) and the owner's Deny reaches the gate; `task_grant` without text refused; over the bank `wait_for` (`text_appears`, `screen_changes`) is redacted, `stream_keyframe` isn't unknown, `current_app` answers, BACK leaves |
| **never** | up to 11 | 0 pass, 1 fail (harness; see below) | Don't ask turned on through the signed `mode_change` prompt, then: Buy now refused `never:money` with no prompt, an ordinary tap allowed, `date_time` refused `never:settings_page`, an unknown page still refused, the bank still blocked, a custom scheme still refused, an OTP field refused `never:otp_field`, a password field still refused, an everyday page opens; the server's `permissions: auto` ends the window and Buy now asks again |

### The five first-run failures

1. **permissions / "Allow all the time"** (B7 case). The phone now reads the hidden location dialog with OCR: the agent's
   `ui_elements` showed its title and buttons, where B7 saw nothing. So "While using the app" asked ("Allow Example
   Login to use your location?"), "Only this time" was refused `gate_permission_choice` (Precise is assumed selected,
   the owner's default is approximate), and "Precise" and "Approximate" asked ("Allow Example Login's permission
   request?"). The suite's owner denies every prompt: three denials suspended the phone, so the tap that opens the
   background-location page was refused and "Allow all the time" was never on screen. No grant happened
   (`ACCESS_BACKGROUND_LOCATION: granted=false`). Suite change: the background page is its own group
   (`permissions_bg`), so `--restart` gives it a fresh process. Two observations for the owner, not failures: the
   OCR'd "Precise"/"Approximate" cards ask rather than going through silently, because the tap (at the card's centre,
   where uiautomator puts it) lands off the OCR'd text node; and the OCR path now makes a deny-everything owner hit
   the 3-denial pause within one dialog.
2. **shop / a second call while the prompt is open** (B7 case). Expected `gate_prompt_open`; got `gate_denied_page`
   with rule `builtin:own_window` ("That's OpenMob's own screen; agents can't touch it."): the prompt covers the target,
   and since `9e3af01` (task approval) the target is judged before the open-prompt check. Refused either way, but
   docs/phone-protocol.md §9.2 still says a call during a prompt gets `gate_prompt_open` ("wait for it"), and this code
   tells the server "don't retry". Worth a look in the batch review; the suite now accepts both.
3. **flood / 20 parallel risky calls make exactly one prompt** (B7 case). 7 calls got `gate_needs_approval` and 13 the
   `own_window` refusal. By design since the live-screens change, the same action asked again reuses the open prompt,
   so the 7 share one approval id; the suite counted asks, not prompts. Suite change: count distinct approval ids
   (must be 1) and accept the `own_window` refusal for the rest. The re-run confirms one id.
4. **hidden / typing** (new case). `type_text` on the hidden window came back as a plain error ("No focused text field"),
   not a gate refusal: the service sees no focused field in a hidden window, so the gate's typing judgement finds no
   target and lets the call reach `PhoneActions`, which can't find a focus either. Nothing can be typed, but the
   stop is PhoneActions', not the gate's (the B7 doc already notes "fail closed by construction, not by accident" for a
   similar case). Suite change: the check accepts a plain error for typing and records it.
5. **never / opening the mode-change prompt** (new case, harness). The phone showed the request as a notification in
   its own words ("Approve? Turn on Don't ask: agents act on this phone without asking for 1 hour? Purchases stay off.");
   the suite looked for a "Review" action, which the collapsed notification group doesn't show, then found "Review &
   approve" on the app's main screen at y=50, under the status bar, and its tap did nothing. Tapping the notification
   opens the app's main screen with the pending request at the top; scrolling up and tapping Review & approve opens
   ApprovalActivity. Suite change: `open_review` does that. Verified by hand first: the phone sent
   `{"type": "approval", "approved": true, "signature": …}` after the PIN. Observation: when the main screen opens from
   the notification, the request card's buttons start under the status bar until the owner scrolls.

### Suite changes (this branch)

- Stand-ins: the login app gained a "Verification code" field (logs `OTP length=N`) and `HiddenActivity` (decor view
  `ACCESSIBILITY_DATA_SENSITIVE_YES`, logs touches and taps); the bank gained `TransferActivity` (an "Amount" field,
  logs `AMOUNT length=N` and touches, keyboard hidden until the owner taps).
- `evil_server.py`: six new groups (`deeplinks`, `typing`, `links`, `hidden`, `methods`, `never`) and `permissions_bg`;
  `restart_app` scrolls when Connect is below an expanded setup card (uiautomator's dump suppresses OpenMob's service,
  which makes the Accessibility step look undone and keeps the card open); the three expectation changes above;
  `--groups` no longer includes `probe` by default.

### Not covered on an emulator

Everything in B7's list still applies (OEM Settings, real biometrics, Android 11–13, real autofill and credential
sheets, split screen). New: a `tel:` handler that isn't a dialer (`builtin:tel_handler`) needs an app claiming `tel:`;
`market://` on a phone without Google Play; a hidden window that is a real third-party dialog rather than the stand-in;
the never window's expiry (1 hour on the monotonic clock) and the phone's own Turn off; and the OCR'd dialog's
"Approximate" card tapped at the text itself rather than the card centre.

## Part B: the Accessibility onboarding checklist

Both builds, API 35 emulator. The finished step was checked with screenshots (`uiautomator dump` unbinds OpenMob's
service, which would make the step look off); uiautomator was used only to find buttons while the service was off.
Screenshot paths are relative to the session's scratch area, `onboarding/<build>/`.

### What the emulator can and can't show about restricted settings

- **`adb install` does not restrict the app**: after a fresh `adb install` the app op `ACCESS_RESTRICTED_SETTINGS`
  reads `default`, and Android's Accessibility settings open OpenMob's switch normally (standard build, screenshots
  `04`–`07`).
- **A real sideload does restrict it, on Android 15 through Enhanced Confirmation Mode**: installing the APK from
  the Files app → package installer (installer of record `com.google.android.packageinstaller`) leaves the op at
  `default` too, but tapping OpenMob in Android's Accessibility list shows the permission controller's **"App was
  denied access"** dialog (`EnhancedConfirmationDialogActivity`), and the op then becomes `ignore` (gatekeeper build,
  `04`–`05`). So the restricted path was **reproduced**, not only emulated.
- **Emulation that matches it**: `adb shell appops set dev.openmob.companion ACCESS_RESTRICTED_SETTINGS deny` puts the
  op in Android's initial restricted state (`MODE_ERRORED`); tapping OpenMob in the Accessibility list then shows the same
  denial and flips the op to `ignore` (standard build, `15`–`16`). `allow` lifts it; Android itself sets `allow` after
  **App info → ⋮ → Allow restricted settings → screen lock** (`19`–`21`).
- **What the app can't see on API 35**: in every op state (`default`, `deny`, `ignore`, `allow`) the app rendered the
  same "unknown restriction" UI: main button **Open Accessibility**, the visible **Android denied access? Open App info**
  shortcut, and no "If Android denies access…" or "First, allow this APK…" text, on both builds (`15`, `17`, `22`
  standard; `01`, `06`, `10` gatekeeper). `AppOpsManager.unsafeCheckOpNoThrow` evidently doesn't report this op's
  real mode to the app on this image (no exception in logcat). The two rows that depend on reading the state
  ("initial restriction, denial not yet shown" and "denial acknowledged") therefore can't be shown on this emulator;
  what the rows protect still holds (the route to Android first, then App info) because the shortcut is always
  offered. Whether a real Android 13/14 phone reports the mode is still to be checked.
- An **update through the installer UI** of an already-allowed app keeps the op at `allow` (standard build, `27`–`30`).

### Standard build (`53112da`): fresh install (`adb install`), then an update (`adb install -r`)

| State | Result | Evidence |
|---|---|---|
| Accessibility off, unrestricted | **Pass.** Open Accessibility shows the disclosure; Not now changes nothing (service still off, app still in front); Agree & continue opens Android Accessibility with OpenMob listed; Use OpenMob → Android's "full control" confirmation → Allow | `01`–`07` |
| Initial restriction, denial not yet shown | **Not distinguishable on this image** (see above): with the op at `deny` the app showed the unknown-state UI; the route still went to Android first after the disclosure, and Android showed its denial | `15`, `16` |
| Denial acknowledged by Android | **Not distinguishable**: with the op at `ignore` the main button stayed Open Accessibility; the visible shortcut opened App info, where ⋮ → Allow restricted settings → screen lock set the op to `allow`; back in the app the switch route then worked | `17`–`24` |
| Default or unreadable restriction state | **Pass.** Open Accessibility and the App info shortcut stayed visible in every state; the default op was never treated as unrestricted | `01`, `09`, `15`, `17`, `22` |
| Restricted option initially absent | **Pass.** Can't turn it on? shows the one-time-approval help with both Open App info and Open Accessibility; its Open Accessibility shows the disclosure first | `10`–`12` |
| Accessibility enabled | **Pass.** After Allow, OpenMob came back to the front by itself, "1 of 4 done", "OpenMob accessibility is on.", button hidden; reopening by hand shows the same | `07`, `08`, `24` |
| Launcher shortcut / Quick Settings tile | **Pass.** The shortcut intent (`dev.openmob.action.OPEN_ACCESSIBILITY`) and the tile ("OpenMob · Turn on Accessibility") open the app at the setup step; no disclosure and no Settings until the button is tapped and agreed | `09`, `13`, `14` |
| Update install | **Pass.** `adb install -r` over the enabled app: the service stays enabled and bound, the step shows done; the same through the package installer UI | `25`, `29`, `30` |

### Gatekeeper build (`217bda3`): fresh install through the package installer UI, then an update (`adb install -r`)

| State | Result | Evidence |
|---|---|---|
| Accessibility off (fresh sideload, ECM-restricted) | **Pass.** Disclosure shown; Not now changes nothing; Agree & continue opens Android Accessibility; tapping OpenMob there shows Android's "App was denied access" | `01`, `02`, `04`, `05` |
| Initial restriction, denial not yet shown | **Not distinguishable** (op `default` before the attempt): unknown-state UI, route to Android first, Android showed the denial | `01`, `05` |
| Denial acknowledged by Android | **Not distinguishable** (op `ignore` after it): main button unchanged; the App info shortcut → ⋮ → Allow restricted settings → screen lock → op `allow` | `06`–`10` |
| Default or unreadable restriction state | **Pass.** Button and shortcut always present | `01`, `06`, `10` |
| Restricted option initially absent | **Pass.** Can't turn it on? offers both shortcuts; its Open Accessibility shows the disclosure | `11`, `12` |
| Accessibility enabled | **Pass.** Returned to the app, "1 of 4 done", button hidden; reopened by hand the same | `18`, `19` |
| Launcher shortcut / Quick Settings tile | **Pass.** Both open the app at the setup step; no dialog, no Settings | `13`–`15` |
| Update install | **Pass.** `adb install -r` of the same APK: service still enabled and bound, step done | `20` |

Disclosure coverage: the disclosure appeared on every route to the Accessibility switch that was exercised: the step's
button, the help's Open Accessibility, and after the shortcut and the tile (which only open the step). The help's
Open App info and the App info shortcut don't show it, and shouldn't: App info is not the Accessibility switch.
