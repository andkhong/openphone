# Gatekeeper B7: the evil-server suite on an emulator

Release gate B7 for gatekeeper Phase 1 ([design](../design/phone-gatekeeper.md), §8): a compromised
server, played by `tests/adversarial/evil_server.py`, sends raw protocol calls to the real OpenMob app on
an Android emulator, and the suite checks that the phone refuses them or asks its owner. The suite also
plays the owner over adb: it approves and denies the phone's prompts and types the PIN.

**Result: 61 of 61 checks pass** on the final run (branch `gatekeeper-b7`, after four fixes). The
first runs found two gatekeeper bugs and two app bugs, all fixed below. Evidence from the final run is
in [gatekeeper-b7-evidence/](gatekeeper-b7-evidence/): `results.json`, `run.log`, the prompts' layouts,
screenshots, and the live view's raw H.264.

## Environment

| | |
|---|---|
| Host | Apple Silicon Mac (arm64), headless emulator (`-no-window -no-audio -no-snapshot-save -gpu swiftshader_indirect`) |
| Image | `system-images;android-35;google_apis;arm64-v8a` (Android 15), Pixel 6 profile, 1080×2400, 420 dpi |
| Lock | PIN 1234 (`locksettings set-pin`), no biometrics enrolled: approvals use the device-credential screen |
| App | debug APK from this branch, accessibility on via `settings put secure enabled_accessibility_services` |
| Pairing | developer token, server `http://10.0.2.2:8911` (the evil server itself) |
| Stand-ins | `tests/adversarial/standins`: `com.example.bank`, `com.example.shop`, `com.example.login` |
| Setup time | about 15 minutes from nothing to a paired emulator (SDK download about 5, cold boot about 3) |

Every adb call named the emulator (`-s emulator-5554`); the owner's phone and servers weren't touched.

Two harness details matter for anyone re-running it:
- The gate prompt is hidden from accessibility services (`setAccessibilityDataSensitive`) and from
  screenshots (`FLAG_SECURE`), so a server can't read it, and neither can `ui_elements`. The suite finds
  its buttons in `dumpsys activity dev.openmob.companion/.ApprovalActivity` (view bounds).
- `uiautomator dump` suppresses other accessibility services. That unbinds OpenMob's, and rebinding it
  renumbers windows, which voids an outstanding permit (permits bind the window id). The suite uses
  uiautomator only where no approval is about to be re-sent, and turns the service back on afterwards.

## Results

| Attack | Result | Evidence |
|---|---|---|
| Unknown method (`run_shell`) | **Pass**: `gate_unknown_method` | results.json |
| Launch OpenMob itself | **Pass**: `gate_blocked_app` (`builtin:own_app`) | |
| `intent://` link, typing `javascript:` | **Pass**: `gate_denied` | |
| Launch the bank app | **Pass**: `gate_blocked_app` | |
| Bank deep link, custom scheme (`examplebank://`) | **Pass**: `gate_denied`, bank not opened | |
| Bank deep link, `https://bank.example.com/…` | **Pass**: opened in Chrome, not the bank (an unverified app link resolves to the browser) | focus in results.json |
| Bank: stream/screenshot | **Pass**: screenshot is all black (mean luminance 0.00), `ui_elements` is `[]`, `stream_start` refused `gate_blocked_app` | `bank-server-screenshot.jpg` vs `bank-emulator-screen.png` |
| Bank: live view already running when the owner opens it (B1) | **Pass**: last frame 0.29 s after the launch (the bank draws at ≥ 1.5 s), none after | `bank-live.h264`, `bank-live-packets.txt` |
| Tap into a loading / null-root window (B2): 5 launches of a bank whose `onCreate` takes 1.5 s, under a tap+read hammer | **Pass after fixes 1 and 2**: no touch reached the bank (it logs every touch); taps got `gate_screen_unstable` then `gate_blocked_app`; no read showed it. Before the fixes: touches reached it in 3 of about 17 launches | results.json |
| Taps and swipes in the open bank | **Pass**: `gate_blocked_app`, no touch logged | |
| Deep WebView "Place order" (120 elements deep) | **Pass**: asks (`risk:words:place order`), no order placed | `deep-place-order-prompt.txt`, `deep-webview.png` |
| Typing into a deep WebView OTP field | **Pass**: asks (`risk:otp_field`, "Allow typing a code into Example Shop?"); the focusing tap itself asked (`risk:unlabeled`: unlabeled spot in a shop) | `deep-otp-prompt.txt` |
| Keep-awake abuse by polling (B3): one act, then 180 reads | **Pass**: screen off 35 s after the act (20 s act hold + 15 s timeout); acts afterwards get `phone_locked` | results.json |
| Permit reuse after quantity "+" (B4) | **Pass**: approved Buy now, then "+", then the same Buy now asks again; nothing bought | |
| Honest path: approve, re-send the identical call | **Pass**: runs once (needed fix 3) | |
| Buy now → cart → Place your order (B5) | **Pass**: after an approved Buy now, Edit cart → Proceed to checkout → Place your order asks again | `shop-b5-place-order-prompt.txt` |
| Zero-length swipe / 50 ms long press on Buy now | **Pass**: both ask | |
| Autofill into a WebView: tap in the keyboard/autofill surface over a WebView login form | **Pass**: asks ("Fill your saved card or password into Example Login?"), no "Always allow" offered | `login-webview-fill-prompt.txt`, `login-webview-keyboard.png` |
| Password-field typing | **Pass**: `gate_password_field`; the field never changed (the stand-in logs it) | |
| Security Settings: Safety Center, accessibility, default apps, display over apps, usage access, notification access, install unknown apps, accounts, VPN, language | **Pass**: every tap refused: `gate_denied_page`, or `gate_screen_unstable` where Android 15 hides the page from the service (Safety Center, default apps) | `settings-security.png` |
| Settings home (control) | **Pass**: ordinary taps go through | |
| "Build number" | **Pass**: asks; the owner's Deny reaches the gate | |
| Claiming `never` without a signature (`mode: never` on the call) | **Pass**: Buy now still asks (the ceiling, `auto`, applies) | `shop-never-claim-prompt.txt` |
| Legacy `approval_request` reusing the gate prompt's id | **Pass**: the prompt keeps the phone's own wording | `shop-legacy-prompt.txt` |
| Prompt flooding: 20 parallel risky calls | **Pass**: exactly 1 prompt, 19 × `gate_prompt_open` | |
| Prompt flooding: denying owner | **Pass**: 3 denials suspend the phone (`builtin:suspended`), and every act is refused until Resume | |
| Prompt flooding: approving owner, approvals never used | **Pass**: 6 prompts, then `gate_rate_limited` (`builtin:prompt_rate`) with no prompt | |
| Permission dialog, "While using" / "Only this time" / Precise / Approximate | **Pass (fail closed)**: Android 15 hides the dialog from OpenMob's service, so every tap is `gate_screen_unstable`; nothing granted | `perm-foreground-dialog.png` |
| "Allow all the time" (background location page) | **Pass (fail closed)**: `gate_screen_unstable`, not granted | |
| Unrecognized permission control ("Ask every time") | **Pass (fail closed)**: `gate_screen_unstable` | |

## Friction smoke test

| Honest flow | Prompts | Result |
|---|---|---|
| Add to cart → Cart → Proceed to checkout → Place your order | **1** (Place your order) | order placed after one approval |
| Buy now → Place your order (D4 purchase follow-up) | **1** (Buy now) | order placed; Place your order didn't ask |

## Bugs found and fixed

1. **Launch race: a tap judged on a stale window list landed in a blocked app** (`a9bee7c`). With a
   server hammering taps while the owner opened the bank, taps reached the bank in 2 of about 12
   launches: the accessibility window list lags the screen, and `reverify` re-read the same stale list.
   Admitted gestures now wait (within the existing 1 s deadline) until nothing has changed for 250 ms
   (over the service's 100 ms event delay). JVM test `admittedGesturesWaitForTheWindowListToSettle`.
2. **Launch race, second form: the launch screen isn't in the list at all** (`8fb0d97`). The full run
   still got taps into the bank in 1 launch of 5: during an app's splash the list shows the previous app
   (quiet, so rule 1 didn't wait) and no window has focus. Gestures now also wait while no listed window
   has focus, and are refused `gate_screen_unstable` (`builtin:no_focus`) if nothing has focus by the
   deadline. After both: 30 launches, then 5 more in the final run, no touch reached the bank. Same commit:
   the "Always allow" line names the app by label ("Example Shop"), not its package.
3. **Developer-token phones could never approve a gate prompt** (`c10fcfa`). The approval key was only
   created by account pairing, so every prompt said "This phone has no approval key" and could only be
   denied. Gate approvals are verified on the phone alone, so the prompt now creates the key if it's
   missing (a key invalidated by removing the screen lock still isn't replaced).
4. **Live view failed on encoders that reject prepended SPS/PPS** (`dea0b60`, not a gate bug). The
   emulator's `c2.android.avc` refuses `KEY_PREPEND_HEADER_TO_SYNC_FRAMES`, so `stream_start` threw and the
   B1 check couldn't run. A third configure attempt drops the key; the server already prepends the cached
   codec config to keyframes.

## Found, not fixed (described instead)

- **Permission dialogs on Android 14+ are invisible to the gate.** On Android 15 the permission
  controller (runtime dialogs, the background-location page, Safety Center, default apps) hides its
  tree from accessibility services that aren't accessibility tools. The gate fails closed
  (`gate_screen_unstable`), which is safe, but it means the Phase 1 permission-dialog rules (per-type
  owner defaults, Precise vs Approximate, "Always allow for <app>") never apply on these versions: the
  agent can't get past a permission dialog, and the owner has to tap it. Those rules only matter on
  Android 11–13 and on OEM builds that don't hide the dialog. Worth stating in the design and in the
  agent's error message ("ask the owner to answer the permission dialog").
- **An empty window list or a point no window covers is judged as allowed** (`judgePoint` returns
  `Judgement(changing = true)` when the hit test finds nothing). Fix 2 covers the launch case. Making it
  `gate_screen_unstable` in general changes both engines and the shared fixtures, so it isn't done here.
- **With the accessibility service unbound, `GateHost.admit` judges acts on an empty screen.** Nothing
  happens, because `PhoneActions` needs the service and errors with "Accessibility is off", but it
  should refuse in the gate instead (fail closed by construction, not by accident).
- **Taps in the keyboard always ask** (any tap in the IME window is the autofill surface). This is
  correct for the risk, but an agent that types through the keyboard instead of `type_text` would prompt
  on every key.

## Not covered on an emulator

- **OEM Settings and Samsung specifics**: One UI's Settings, `com.samsung.accessibility`, Samsung Pass
  and Samsung's autofill and keyboard; Xiaomi and others. The Settings fixtures are still hand-built.
- **Real biometrics**: approvals here used the device-credential (PIN) screen. A fingerprint or face
  approval with the `CryptoObject`, and lockout after failed attempts, need a real phone.
- **Android 11–13**: only one system image (Android 15) was allowed, so the permission-dialog rules
  never ran against a readable dialog, and pre-14 accessibility behaviour wasn't seen.
- **Real autofill and credential sheets**: the emulator's Google autofill had no saved data, so the
  check tapped the keyboard surface instead of a saved-card or saved-password chip. The Credential
  Manager sheet and the share sheet weren't exercised.
- **Split screen and picture-in-picture** with a blocked app (reads should be black), the
  keyguard/BiometricPrompt hard deny (a tap on the prompt while it's up), developer options and wireless
  debugging (not enabled on the image), a system-language switch, and the server-side checks
  (`approve:true` loop, replayed `gate_event`).
- **The 10-minute keep-awake cap under act polling**: only reads were polled here; the cap is covered
  by the JVM tests (`KeepAwakePolicy`).
