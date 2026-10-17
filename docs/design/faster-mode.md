# Faster mode: Jev images, step 1 (design, 2026-10-17)

Faster mode lets OpenMob send **small screenshots (768 px)** of what the agent sees to **TypeSafe** (Jev, a second
AI service), so it can recognise screens that have no usable text faster. It is **off by default**, an **owner
setting kept on the phone**, and only the phone's owner can turn it on, in the OpenMob app with the **screen lock**.

Step 1 (this change) uses images for two things only, and only on screens Jev text can't read:

- **J1, screen state:** loading screens don't go to the main model (the loop waits and looks again); every other
  state is a one-line hint for the main model.
- **J3, loading vs ready:** in the wait on a loading screen, Jev looks at fresh frames and the wait ends as soon as
  the screen is ready.

**No taps from Jev images in step 1.** Pattern A (Jev Box taps on profiled canvas screens) is step 2.

The evidence is the offline benchmark `docs/research/jev-images-benchmark-2026-10-17.md` (branch
`bench/jev-images`, 163 emulator screenshots). **Measured there:** single-question Jev image latency 83 ms p50 and
165 ms p90 at 768 px, against about 0.8 s for Haiku vision; J1 95.1% (100% on canvas) against 24.5% for Jev text
on canvas; J3 96.9% (recall 88.9%) against 22.2% recall for today's one-frame heuristic. Images are TypeSafe
"Dreamer-ready", not production, so the model is pinned to the version the benchmark measured (`jev-1.14.0`).

## 1. The switch

| | How | Lock |
|---|---|---|
| **Turn on: always on this phone** | The OpenMob app's Faster mode card → **Always on this phone** | Screen lock |
| **Turn on: this session only** | The card → **Turn on for this session**. Ends when the phone's connection to the server ends (the app disconnects, the phone goes offline or reconnects, or the server restarts) | Screen lock |
| **Turn off** | One tap: the card's **Turn off Faster mode**, the ongoing notification's **Turn off Faster mode**, the dashboard (the **Faster mode on · Turn off** chip, or the Permissions dialog), the API (`PATCH /v1/devices/{id}/permissions {"faster_mode": "off"}`), MCP (`change_permissions faster_mode=off`) | None |

**Who can turn it on: only the phone's owner, on that phone.** Never an API key, the dashboard, MCP, the server's
config, a web page or a drive client:

- The dashboard's and the API's schemas accept only `faster_mode: "off"` (anything else is a 422).
- `DeviceManager.change_permissions` refuses any `faster:` loosening (`403 mode_not_allowed`) unless it came from
  that phone's own socket (`faster_mode_request`), alone, and the phone is connected.
- The config has no "on": `faster_mode` is `phone_opt_in` (phones may opt in) or `disabled` (never; phones'
  settings are ignored). Any other value refuses to start.

**The signed approval, reused.** Turning it on is the existing `mode_change` flow (the one that turns on Don't ask),
so nothing new is trusted:

1. The app sends `{"type": "faster_mode_request", "scope": "phone" | "session"}`.
2. The server makes a `mode_change` approval for **that phone only**, whose action input is
   `<question>\n{"faster_mode": "<scope>"}` and whose digest binds it (`phone/approval_keys.py`).
3. The phone words the prompt **itself** from the signed change (`gate/ModeChange.describe`): "Turn on Faster mode
   on this phone? Faster mode sends screenshots of what the agent is looking at to TypeSafe, a second AI service,
   so it can recognize screens faster. Banking, wallet and password apps, and screens the phone blacks out, are
   never sent. You can turn this off at any time." The owner passes the screen lock; the phone signs
   `<approval id>|<task id>|<digest>|true|<time>` with its Keystore key, which only signs after the screen lock.
4. The server verifies the signature against that phone's registered key (hosted), applies the setting, records who
   and when, and pushes `faster_mode` to the phone. A signature by another phone, another key, an unsigned yes or
   a stale one (over 5 minutes) is ignored. A phone without a registered approval key can't turn it on.
5. The phone shows "Faster mode is on" (the ongoing notification's text, first, and the card) only for the scope
   its owner approved there (`FasterMode.kt`). If the server's push claims Faster mode is on in a way the phone
   never approved (or "always" after a session-only approval), the phone tells the server to turn it off and says
   so in its activity list.

Token/dev mode (no accounts) has no approval keys, as for Don't ask: an app phone still asks its owner on its own
prompt (the screen lock, unsigned), and a USB/adb phone without the app can't turn Faster mode on at all.

**Turning it off takes effect from the next look.** The agent asks the live, phone-signed setting before every
image (`Agent.faster_mode`, wired to `DeviceManager.faster_on`), and again when an answer arrives. An image
request already in flight may finish, but its answer is discarded (the step records `jev_image_skip:
off_discarded`).

**Audit.** Every on and off is recorded with who and when:

- the phone's own `faster_log` (last 50 entries, in its permission settings, shown in the dashboard's Permissions
  dialog payload): `{at, on, scope, by}`, where `by` is "the phone (screen lock)", "the phone app", "the phone's
  notification", "Your dashboard", an API key's name, or "the session's end";
- the hosted audit log: `device.faster_mode` with `on:<scope> by …` or `off by …`;
- a status line in the phone's chat feed;
- per step: `faster` (on for that phone at that step), `jev_images` (screenshots sent), `jev_image_ms`, `jev_state`
  and `jev_image_skip` (why none was sent) in the steps table.

## 2. What is sent, and when

**Trigger:** the screens Jev text can't read: no accessibility element with text (a canvas game, a web canvas, an
app that draws its own UI), including the case where `ui_tree: auto` has stopped reading the tree for that app.
A screen with labeled elements never sends an image.

**Data flow.** Phone → OpenMob server (the screenshot it already takes for the main model and for OCR) → the
server shrinks it to 768 px on the long side (padded to TypeSafe's 280 px minimum), JPEG q80 → TypeSafe
`POST /v1/systemone`, multipart, header `TypeSafe-Danger-Zone-Images: true`, model `jev-1.14.0`. Besides the image,
the request carries the foreground app's package name and one fixed question; no request text, no element list,
no typed text. TypeSafe answers with the state's probabilities (J1) or a loading probability (J3). Nothing is
stored on the OpenMob side except the step's numbers above; what TypeSafe keeps is under TypeSafe's terms.

**Never sent, even when on.** Checked on the server before every request (`agent/faster.py`, `refusal`), in this
order:

| Reason (`jev_image_skip`) | The screen |
|---|---|
| `unknown_window` | No app identified in front |
| `system_dialog` | A permission controller, or every labeled element covered by a higher window (a hidden dialog) |
| `blocked_app` | An app this task may not touch |
| `sensitive_app` | Banking, payments, wallets, password managers (`sensitive_apps`), **always**, whatever `allow_sensitive_apps` says |
| `locked` | The lock screen is up |
| `password_field` | A password field on screen |
| `sensitive_number` | A card number or SSN in any element's text or the OCR'd words, alone or joined across boxes (`agent/sensitive.py`, the detector the gatekeeper uses) |
| `no_text_check` | The server has no OCR (it is macOS-only today), so a treeless screen's words couldn't be checked first. **Fail closed: nothing is sent.** |
| `black_frame` | The prepared image is black: a `FLAG_SECURE` window or a gatekeeper-blacked-out read |
| `drive` | A drive session, unless the owner allowed that key (`faster_drive` on the key, default off; developer console `PATCH /api/developer/keys/{id}`) |

## 3. What the loop does with the answers

**J1, on each look** (`_observe_settled` → `_faster_loading`): on a treeless screen that may be sent, one Choice
question over the benchmark's seven states (loading, menu, content, game over, dialog, login, home screen; the
exact wording the benchmark measured, `jev.IMAGE_STATES`).

- Below `jev_image_min_confidence` (0.6): ignored, today's behaviour.
- **loading:** the main model is not asked. The loop waits (J3, below) and looks again, at most
  `jev_image_loading_looks` (2) extra looks and `jev_auto_wait_seconds` (30 s) per task; each wait at most
  `jev_image_loading_wait_seconds` (10 s). Still loading after that: the main model decides, with the hint.
- **any other state:** a hint line in the screen description ("Screen state from a quick screenshot check: menu
  (p=0.98). A hint only: trust the screenshot when they differ.") and `jev_state` on the step row, groundwork for
  step 2's taps.

**J3, in the loading wait** (`_faster_wait_ready`): every 0.25 s a fresh frame (the app in front, the tree when it
is cheap to read, OCR, then every never-sent check again), one Noul question ("still loading?").

- `p_loading <= 0.2` (confidence |2p−1| ≥ 0.6): ready, the wait ends (`wait_via: jev_image`, `wait_met: 1`).
- `p_loading >= 0.8`: keep waiting, within the caps above.
- In between, an error, a timeout, a refused frame: the wait ends (`wait_via: fallback`) and the next look decides,
  as today.
- The screen gained text: the wait ends; Jev text reads it from there.

**Deadlines.** Each image request is capped at `jev_image_timeout_ms` (600 ms): a slower answer is discarded and the
step goes on (`JevImageJudge`, a thread with a hard deadline; the HTTP request finishes on its own). Nothing
waits on TypeSafe longer than that, and an error never fails a step.

**Why J3 is not inside every settle.** Asking on every settle of a treeless screen would add a screenshot, OCR and a
request (about 0.3–0.5 s, estimate) to every step even when nothing is loading. J1 already looks at every treeless
screen once per step, so J3 runs only in the wait that follows a "loading" verdict.

## 4. Time saved: the "Faster mode saved ~N s" line

At the end of a task in which Faster mode did anything, the feed gets one status line and the task's final event
data carries `faster: {images, jev_image_ms, model_calls_skipped, mean_model_s, saved_s, text}`.

**Method** (`Agent._faster_summary`), from this task's own measurements only:

- `model_calls_skipped`: loading looks that didn't go to the main model, counted only when the text classifier
  (if any) didn't also call the screen loading (it would have waited anyway).
- Each is priced at **this task's mean main-model time per call** (`model_ms` ÷ main-model calls).
- Minus the wall time of every image request this task (`jev_image_ms`).
- With no main-model call in the task, the skipped calls are counted but not priced ("this task made no model call
  to price them"). A negative result says "saved no time".

What it leaves out: the counterfactual's own extras (the main model on a loading screen usually answers "wait",
which costs a settle and another call), and the wait itself (the screen had to finish loading either way). So the
line is an estimate built from measured parts; the honest comparison is the same task with Faster mode off and on
(`docs/benchmarks.md`).

### Measured on the emulator (2026-10-17)

AVD `gk_b7` (Android 15, `emulator-5554`) through adb, `claude-haiku-5-5`, Jev text on, OCR on (Apple Vision). A
canvas-only page in Chrome full screen: a spinner for 6 s after a tap, then a canvas menu (Play / Settings / Quit),
no accessibility text. Task: "A game is open in Chrome. Wait for it to finish loading, then tell me the buttons on
its main menu." Four runs each, alternating off and on; the switch was a plain on/off callable in the script (the
phone-signed switch is covered by `tests/test_faster_mode_switch.py`).

| | off | on |
|---|---|---|
| Wall time per task (each run) | 15.17, 14.91, 15.14, 15.20 s | 10.80, 10.68, 10.65, 11.17 s |
| Median | **15.2 s** | **10.7 s** |
| Main-model calls per task | 2 (`wait_until loading_done`, then `done`) | **1** (`done`) |
| Main-model time per task | 2.2–2.7 s | 1.3–1.6 s |
| Loading wait | 6.0–6.3 s (the model's `wait_until`, ended when the frames stood still) | 2.4–2.7 s (J3 said ready) |
| Jev image requests | 0 in 4 runs (nothing sent while off) | 5 per run (2 J1, 3 J3); 0.42–0.74 s in all per run |
| Success | 4/4 | 4/4 |

- **Measured:** about 4.4 s less per task (median), and one main-model call fewer. On the "on" runs the loading look
  went to Jev, not the model, and the menu got the hint "menu".
- **The "saved" line underestimates it:** it said ~0.5–1.2 s, because it prices only the skipped model call minus
  the image time; the extra observation (about 2.6 s on adb with OCR) and the model's own longer wait aren't in it.
- Limits: one emulator, one synthetic page, adb (each look about 2.7 s: the uiautomator dump, screenshot and OCR);
  on the app's fast element list the looks are cheaper and the gap will differ. Not measured on a real phone.

## 5. Threat model

| Threat | What stops it |
|---|---|
| An API key, MCP client or drive client turns it on | Schemas accept only "off"; the server refuses any `faster:` loosening not from the phone's own socket; and it still needs the phone's screen-lock signature |
| The dashboard (or a stolen dashboard session) turns it on | Same: the dashboard can only turn it off |
| A web page or another app on the phone | Can't reach the app's socket; the approval needs the screen lock, and the phone words the prompt itself |
| Another of the owner's phones approves it | Only the phone being changed can answer its `mode_change` (`_resolve_permission_change`) |
| Replay of an old approval | The signature covers this approval id and the change's digest, and expires after 5 minutes |
| Wearing the owner down with prompts | The phone shows at most 2 permission-change prompts an hour (N1) |
| A screen that must never leave | The server-side checks above, before every request, with tests; OCR missing means nothing is sent |
| A sensitive screen the checks miss (a balance with no card number, a canvas banking app with an unlisted package) | **Not stopped.** The detector catches card numbers and SSNs; other personal data on a treeless screen can be sent. J5 (an image "sensitive?" signal) is a later, add-only step |
| **A compromised server** | **Not stopped.** The server enforces the switch and the never-sent list. A compromised server can ignore both and send any screenshot it takes anywhere, as it can with every screenshot today (the main model already receives them). The phone can't see where the server sends images. What the phone does: it keeps the owner's choice, shows "Faster mode is on" only for that, and tells the server to turn it off when the server claims more |
| TypeSafe's handling | Their terms; images are "Dreamer-ready" (not production). The owner opts in knowing it's a second processor |

## 6. Configuration

```yaml
faster_mode: phone_opt_in        # or disabled: this server never sends an image (nothing here turns it on)
jev_image_model: jev-1.14.0      # pinned; never jev-latest / jev-preview (refused at start)
jev_image_side: 768              # long side of the image sent
jev_image_timeout_ms: 600        # per image request; a later answer is discarded
jev_image_min_confidence: 0.6    # below it an answer is ignored
jev_image_loading_looks: 2       # extra looks on a loading screen before the main model decides anyway
jev_image_loading_wait_seconds: 10
```

Faster mode needs `TYPESAFE_API_KEY` (Jev) and, for treeless screens, OCR on the server (macOS).

## 7. Open decisions (owner)

- "This session only" means the phone's current connection. A Wi-Fi blip ends it (fail toward off).
- No OCR, no image: on a Linux server (Fly today) Faster mode does nothing until there is a text check.
- Whether allowing a key's drive sessions (`faster_drive`) should also need the phone's screen lock (today: the
  owner's developer console).
- Whether to add J5 (an image "sensitive content?" check, add-only) before widening the trigger.
