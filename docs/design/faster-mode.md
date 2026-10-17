# Faster mode: Jev images, steps 1 and 2 (design, 2026-10-17)

Faster mode lets OpenMob send **small screenshots (768 px)** of what the agent sees to **TypeSafe** (Jev, a second
AI service), so it can recognise screens that have no usable text faster. It is **off by default**, an **owner
setting kept on the phone**, and only the phone's owner can turn it on, in the OpenMob app with the **screen lock**.

Step 1 (this change) uses images for two things only, and only on screens Jev text can't read:

- **J1, screen state:** loading screens don't go to the main model (the loop waits and looks again); every other
  state is a one-line hint for the main model.
- **J3, loading vs ready:** in the wait on a loading screen, Jev looks at fresh frames and the wait ends as soon as
  the screen is ready.

**Step 2 (§3.1): Jev taps routine game controls.** On a menu, pop-up or game-over screen, Jev picks one routine
action (start, continue, dismiss, or none) and Boxes its control; the loop taps it without the main model when
the benchmark's safe tap rule holds, through every check a model tap goes through. Purchases, offers and ads are
never Jev-tapped.

The evidence is the offline benchmark `docs/research/jev-images-benchmark-2026-10-17.md` (branch
`bench/jev-images`, 163 emulator screenshots). **Measured there:** single-question Jev image latency 83 ms p50 and
165 ms p90 at 768 px, against about 0.8 s for Haiku vision; J1 95.1% (100% on canvas) against 24.5% for Jev text
on canvas; J3 96.9% (recall 88.9%) against 22.2% recall for today's one-frame heuristic. Images are TypeSafe
"Dreamer-ready", not production, so the model is pinned to the version the benchmark measured (`jev-1.14.0`).

## 1. The switch

| | How | Lock |
|---|---|---|
| **Turn on: always on this phone** | The OpenMob app's Faster mode card → **Always on this phone** | Screen lock |
| **Turn on: this session only** | The card → **Turn on for this session**. Lasts until the owner disconnects (the app's Disconnect or the notification's Stop) or an hour without task or drive activity, and never past its signed 24-hour expiry. A reconnect within 5 minutes (a network blip) keeps it, once the phone re-presents the signed grant (§1.1) | Screen lock |
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
   `<question>\n{"faster_mode": "<scope>", …}` and whose digest binds it (`phone/approval_keys.py`). A session's
   limits are in that signed change: `faster_until` (a hard expiry, 24 h), `faster_idle_s` (3600) and
   `faster_resume_s` (300). The phone clamps looser values to those.
3. The phone words the prompt **itself** from the signed change (`gate/ModeChange.describe`): "Turn on Faster mode
   on this phone? Faster mode sends screenshots of what the agent is looking at to TypeSafe, a second AI service,
   so it can recognize screens faster. Banking, wallet and password apps, and screens the phone blacks out, are
   never sent. OpenMob blocks banking apps and detected card and ID numbers, but it can't recognize every
   sensitive image. You can turn this off at any time." The owner passes the screen lock; the phone signs
   `<approval id>|<task id>|<digest>|true|<time>` with its Keystore key, which only signs after the screen lock.
   **Only exactly what the owner asked for (security review 2026-10-17, M2):** the phone declines, unseen, a Faster
   mode change that carries anything else (Don't ask, purchases, a ceiling, everyday, permission defaults, trusted
   apps, or a second Faster part) and a Faster mode change in a scope the owner didn't tap in the app within the
   last 5 minutes (`ModeChange.fasterRefusal`; the activity list says so). A key's drive change must come alone
   too (the dashboard asks for it, so there is no tap on the phone to match). Before, a compromised server could
   answer the owner's "Turn on Faster mode" with one change that also turned on Don't ask, and the screen-lock
   sheet showed only the Faster mode question.
4. The server verifies the signature against that phone's registered key (hosted), applies the setting, records who
   and when, and pushes `faster_mode` to the phone. A signature by another phone, another key, an unsigned yes or
   a stale one (over 5 minutes) is ignored. A phone without a registered approval key can't turn it on.
5. The phone shows "Faster mode is on" (the ongoing notification's text, first, and the card) only for the scope
   its owner approved there (`FasterMode.kt`). If the server's push claims Faster mode is on in a way the phone
   never approved (or "always" after a session-only approval), the phone tells the server to turn it off and says
   so in its activity list. It does the same when the server claims a session-only grant is on after the grant
   ran out (idle, expired, or away too long).

### 1.1 Session only, reconnects and idle

- **The owner's Disconnect** (the app's Disconnect, or Stop on the notification: a normal close, code 1000) ends it,
  on the server and on the phone.
- **A network blip** (any other drop) leaves it waiting: off on the server (no image is sent) until the phone
  reconnects and sends `faster_mode_resume` with the same approval id, signature and signed time. The server treats
  it as on again only if that is the grant it recorded, the signature checks out against the phone's key
  (`verify_faster_grant`; the signature's age doesn't matter here, the grant's own signed limits do), the phone came
  back within `faster_resume_s` (5 min) and the session wasn't idle past `faster_idle_s`. Anything else ends it.
- **Idle:** the server counts task and drive activity (every image check the agent makes during a task), the phone
  counts the server's calls. An hour without any ends it on both sides.
- A server restart is a reconnect, judged from when the phone actually left (review S L5, 2026-10-17): the time of
  the drop is stored with the grant (`faster_grant.detached_at`), so a restart doesn't restart the 5-minute window.
  If the server died without seeing the drop (a crash: the stored grant still holds on an earlier process's
  connection), when the phone left is unknown, so the grant **ends** and the owner turns it on again (fail closed).
  The idle hour then counts from the approval (`faster_since`), the activity record being in memory only.
- "Always on this phone" survives reconnects and restarts; only turning it off ends it.

**Checked on the emulator** (`gk_b7`, this branch's app, a token-mode server on port 8767, 2026-10-17): turned on
for the session from the app's card (the phone-worded prompt, then the PIN) → on, and the notification read "Faster
mode is on · Agent connected · ready"; the server process restarted (the phone saw a dropped connection and
reconnected; airplane mode doesn't cut the emulator's link to the host) → the phone re-confirmed its grant, on again;
the app's Disconnect → off on the server (log: "the owner's Disconnect"), and still off after connecting again.
That check found one bug, fixed: the long warning as the screen-lock prompt's description pushed the PIN field under
the keyboard, so the PIN couldn't be typed. The system prompt now gets a short summary (`ModeChange.lockText`, at
most 180 characters, so the PIN field stays reachable): "Turn on Faster mode (this session): Faster mode sends
screenshots of what the agent is looking at to TypeSafe, a second AI service, so it can recognize screens faster."
(or "(always on this phone)"); the full warning is on the approval screen just before it. Other permission changes
get each part's question, Don't ask first, as many as fit, then "+N more above".

Token/dev mode (no accounts) has no approval keys, as for Don't ask: an app phone still asks its owner on its own
prompt (the screen lock, unsigned), and a USB/adb phone without the app can't turn Faster mode on at all.

**Turning it off takes effect from the next look.** The agent asks the live, phone-signed setting before every
image (`Agent.faster_mode`, wired to `DeviceManager.faster_on`), and again when an answer arrives. An image
request already in flight may finish, but its answer is discarded (the step records `jev_image_skip:
off_discarded`).

**Audit.** Every on and off is recorded with who and when:

- the phone's own `faster_log` (last 50 entries, in its permission settings, shown in the dashboard's Permissions
  dialog payload): `{at, on, scope, by}`, where `by` is "the phone (screen lock)", "the phone app", "the phone's
  notification", "Your dashboard", an API key's name, "the owner's Disconnect", "60 min idle", "the phone coming
  back too late" or "a grant that didn't match";
- the hosted audit log: `device.faster_mode` with `on:<scope> by …` or `off by …`;
- a status line in the phone's chat feed;
- per step: `faster` (on for that phone at that step), `jev_images` (screenshots sent), `jev_image_ms`, `jev_state`
  and `jev_image_skip` (why none was sent) in the steps table.

### 1.2 A key's drive sessions

A drive session (an outside agent deciding each step) sends no image unless the owner let that one API key, on that
phone, with the same signed flow:

1. The owner's dashboard asks (`POST /api/devices/{id}/faster-drive {"key_id"}`, an owner session only; never an
   API key, MCP or the developer console). 202 `needs_phone_approval`.
2. The phone gets a `mode_change` whose signed change is `{"faster_drive": {"key", "name", "on": true}}` and words it
   itself: "Let the API key “driver” use Faster mode in its drive sessions on this phone? <the warning>".
3. With the phone's screen-lock signature, the key joins that phone's `faster_drive_keys`. A session of that key then
   sends images only while Faster mode itself is on for the phone (checked live at every image).
4. Off, at once and from anywhere: the dashboard (`{"on": false}`), or the developer console
   (`PATCH /api/developer/keys/{id} {"faster_drive": false}` removes it on every phone; `true` is refused there).

### 1.3 A server that can't act on it

A server with no OCR (it is macOS-only today) or no TypeSafe key never sends an image, even with Faster mode on.
The dashboard's chip says "Faster mode is on but inactive on this server", and the Permissions dialog gives the
reason (`faster_inactive`).

## 2. What is sent, and when

**Trigger:** the screens Jev text can't read:

- **treeless:** no accessibility element with text (a canvas game, a web canvas, an app that draws its own UI),
  including the case where `ui_tree: auto` has stopped reading the tree for that app;
- **sparse** (2026-10-17): a few labels around a large area none of them describes. Measured on the owner's Samsung
  that day: Faster mode was on for every Tetris and Chess.com run and never engaged, because real games expose some
  text (Tetris's one element is "Game view"; Chess.com's board has "Back" and "Chess.com" around it). A screen is
  sparse when all of these hold (`agent/faster.py`, `sparse_region`):
  - at most 10 labeled elements (text fields count) and 200 characters of labels in all;
  - no password field and no focused text field;
  - a **play area**: the largest rectangle no labeled element overlaps covers at least 30% of the screen, each side
    at least a quarter of the screen's width (`agent/waiting.py`, `canvas_region`: the same definition the model's
    `wait_until screen_changes` uses to compare frames). A labeled element covering 30% or more is a container
    whose label names the whole ("Game view") and doesn't cover the area; OCR words never do;
  - something is drawn in that area on the frame itself (its cells' brightness varies by a standard deviation of 8
    or more): a few labels over a blank area are an empty screen, not a canvas.

  Text-heavy screens are never sparse: Settings, Maps, YouTube and a Chrome page show dozens of labels
  (`tests/test_game_waits.py` pins those four and three game layouts).

Any other labeled screen never sends an image.

**A sparse screen's words are read first.** Its labels don't show what's drawn in the play area, so the server OCRs
the frame before any request and runs every never-sent check below on those words too; without OCR nothing is sent
(`no_text_check`, as for a treeless screen). The words are used only for the checks: they don't reach the main model
from here.

**Data flow.** Phone → OpenMob server (the screenshot it already takes for the main model and for OCR) → the
server shrinks it to 768 px on the long side (padded to TypeSafe's 280 px minimum), JPEG q80 → TypeSafe
`POST /v1/systemone`, multipart, header `TypeSafe-Danger-Zone-Images: true`, model `jev-1.14.0`. Besides the image,
the request carries the foreground app's package name and one fixed question; no request text, no element list,
no typed text. A Jev tap request (§3.1) also carries the request text (Jev text already receives it). TypeSafe answers with the state's probabilities (J1) or a loading probability (J3). Nothing is
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
| `notification` | The notification shade, a heads-up or another System UI window: System UI is in front, or a System UI node (`com.android.systemui:id/…`) is in the element list. The server has no window list, so a heads-up that only OCR sees is caught by `otp_text` and `sensitive_number`, not here |
| `password_field` | A password field on screen |
| `sensitive_number` | A card number or SSN in any element's text or the OCR'd words, alone or joined across boxes (`agent/sensitive.py`, the detector the gatekeeper uses); **or any run of 8 or more digits** (single separators allowed, any script's digits), whatever Luhn says, so a misread or partly read number still stays. Every OCR word counts here, **whatever its confidence**: the 0.3 floor only keeps unsure words from the main model (review S M1) |
| `otp_text` | A one-time code: 4-8 digits within 60 characters of "code", "verification", "verify", "OTP", "passcode", "PIN", "one-time", "2FA", "authenticate", "log in", "sign in", "confirm", or Google's "G-123456" (review S M2). Sender names aren't recognised; a code with none of those words around it can still go |
| `no_text_check` | The server has no OCR (it is macOS-only today), so a treeless or sparse screen's words couldn't be checked first. **Fail closed: nothing is sent.** |
| `black_frame` | The prepared image's **app area** is black (97% of its pixels at or below 24): a `FLAG_SECURE` window or a gatekeeper-blacked-out read. The status bar (top 5%), the navigation bar (bottom 7%) and OpenMob's border (2% each side) are left out, since a secure capture still shows them; the Stop pill is within the 3% (review S L3) |
| `app_changed` | The app in front, read again right after the frame was taken, isn't the one the checks above judged (review S L2): another app (a bank, say) came up between the package read and the capture |
| `drive` | A drive session, unless the owner approved that key on that phone (§1.2) |

## 3. What the loop does with the answers

**J1, on each look** (`_observe_settled` → `_faster_loading`): on a treeless or sparse screen that may be sent, one Choice
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
waits on TypeSafe longer than that, and an error never fails a step. **Nothing is queued** (review S M3,
2026-10-17): at most two requests run at once, each on its own worker, and a third is skipped at once rather than
waiting behind a slow one. Right before the POST the worker checks again that the deadline hasn't passed and that
Faster mode is still on for that phone and task (`still_ok`, the live phone-signed switch); otherwise the image is
never sent. So an image goes out only while its step still waits for it and Faster mode is on; one already
being sent when the owner turns it off may finish, and its answer is discarded. A TypeSafe error reaches the feed as
its status code only, never its body.

**J3 in the model's `wait_until screen_changes`** (2026-10-17, `_faster_ready_stop`, `jev_image_wait_ready`, on by
default): on a treeless or sparse screen, after the wait's first second and at most once a second, a look that
didn't move asks one "still loading?" question (every never-sent check first; at most 2 a wait). `p_loading <= 0.1`
(confidence 0.8) ends the wait, not met, and the model is told "the screen hasn't changed and a quick screenshot
check says it's ready" (`wait_via: jev_image`). Anything else leaves the wait as it was: it still ends the moment the
screen changes, or by the still exit (3.5 s still with nothing loading, `agent/waiting.py`). **Worth it? Marginal,
estimate:** the still exit already caps a still screen's wait at about 3.5 s; a confident J3 can end it after about
1.3–2 s (1 s of wait, a frame, OCR and a request), so it saves about 1.5–2 s per such wait for one or two image
requests. It only matters on screens the model waits on that are already ready; it is a config switch so it can be
turned off if the real-phone runs show no gain.

**Why J3 is not inside every settle.** Asking on every settle of a treeless screen would add a screenshot, OCR and a
request (about 0.3–0.5 s, estimate) to every step even when nothing is loading. J1 already looks at every treeless
screen once per step, so J3 runs only in the wait that follows a "loading" verdict.

## 3.1 Step 2: Jev taps routine game controls

**Why** (measured on the owner's Samsung, 2026-10-17, Faster mode on): Tetris "start a new game" took 17–19 s with
6–7 main-model calls; Jev sent 26 images and recognised menu, content and loading screens, but saved about 0 s,
because step 1 only skips the model on "loading". The remaining cost is main-model calls of about 1.3–2 s each.

**What** (`Agent._jev_tap_step`, `JevImageJudge.routine`): on a screen Faster mode may look at (treeless or sparse;
**every never-sent check of §2 first, unchanged**) whose J1 state is `menu`, `dialog` or `game_over` at the J1
confidence floor, before the main model is asked:

1. **Words first, no request.** Every word the never-sent checks read on that frame (accessibility labels and every
   OCR word, whatever its confidence) is checked: a price (`$4.99`, `0,99 €`), a money word (`money_keywords`: buy,
   pay, purchase…) or a confirm word (`confirm_keywords`) anywhere on the screen means **no Jev tap**: the main model
   decides, and the gate judges whatever it taps.
2. **One image request**: a Choice over a small fixed vocabulary, given the request text (the plan's current sub-goal
   when one runs) and the screenshot, plus, in the same request, a Box and a "visible?" Noul for each action's control
   (`jev.ROUTINE_ACTIONS`, `ROUTINE_TARGETS`):
   - `start`: Play, Start, New game, Play again, Retry, a level button, when starting fits the request and it costs
     nothing;
   - `continue`: Next, Continue, OK on a notice, "Tap to continue";
   - `dismiss`: close a pop-up or ad that is **not selling anything** (X, Close, Not now, No thanks, Skip, Later);
   - `none`: anything else or unsure, and **always** a purchase, price, offer, bundle, subscription, VIP or premium
     pop-up, coins or gems for money, "watch an ad/video for a reward", a shop, a sign-in, account or permission
     prompt, settings, the gameplay itself, an unclear control, or a request that needs anything other than those
     three.
3. **The tap rule:** the action not `none`, with probability ≥ `jev_tap_min_probability` (0.8) and Jev's confidence ≥
   `jev_tap_min_confidence` (0.7); then the benchmark's **safe tap rule**, `min(Box confidence, visible) ≥
   jev_tap_min_box` (0.5; 1 false tap in 340 targets, 0 on canvas, in the 2026-10-17 benchmark). The config refuses a
   value under 0.5. A Box over half the image, or off the screen, is not tapped.
4. **Words in the Box.** The words overlapping the Box (plus a fifth of its size, at least 24 px, around it) must not
   name money, an offer or an ad (`faster.TAP_SCREEN_WORDS`: offer, sale, bundle, shop, store, gems, coins, watch,
   ad, video, reward, claim, unlock, premium, VIP, trial…, or a price). A wrong-button Box (the benchmark saw "Menu"
   boxed as "Retry") on "Watch ad" is caught here.
5. **The tap goes through `_execute`** as `tap` at the Box's center, `source: jev_tap`: `_guard` judges what is under
   the point (the frame's OCR words included, so a canvas button's own words count), the money and purchase words,
   the risky-tap classifier, the permission mode, and on a gatekeeper phone the phone itself judges it with
   `rules.json`, exactly as a model tap. The tap's description ("the button that starts or plays a new game") can
   only add caution.
6. **Verify:** after the usual settle and look, the screen must have changed: another app, other labels, another J1
   state, or the play area's frame (`waiting.region_changed`, the comparison `wait_until screen_changes` uses). The
   main model is told what the helper did, as for a fast step.

**Limits** (any one sends the step to the main model):

- at most `jev_tap_max_consecutive` (3) Jev taps in a row without a main-model step;
- a miss (no change), a refusal or an owner prompt: the next step is the main model's, that screen (its play-area
  frame) never gets a Jev tap again this task, and after `jev_tap_max_misses` (2) misses no Jev tap happens in it;
- an app where the gate asked the owner or refused anything this task (the server's gate or the phone's) gets no
  Jev tap for the rest of the task (stricter than "that screen", since the server can't tell screens of a canvas app
  apart reliably);
- never under **Ask every step** or **Plan first** (every tap there is the owner's or the plan's), never in drive
  sessions (an outside agent decides each step), never with Faster mode off, `jev_taps: false`, or on any screen
  that may not be sent;
- **never `done`.** Jev doesn't finish tasks: the main model confirms the final screen in one call, as before.

**Recorded per step** (the steps table's `jev_tap`, JSON): the J1 state, the choice with its probability and
confidence, the Box, its confidence and "visible?", the rule (`pass`/`fail`), the tap outcome (`tapped`, `refused`,
`asked`) and the verification (`app`, `labels`, `state:menu->content`, `frame` or `miss`); or `{skip: why}` (`none`,
`unsure`, `rule`, `box_shape`, `screen_price`, `screen_money`, `box_word:watch`, `cap`, `misses`, `gate_asked`,
`failed_here`, `mode`, `no_answer`). The tap's own row has `source: jev_tap`.

**Data flow change:** the Jev tap request carries the request text (or the plan's current sub-goal) with the
screenshot. TypeSafe already receives the request text from Jev text classification (`docs/safety.md`,
"Processors"); with images it now sees both together.

### Measured on the emulator (2026-10-17, step 2)

AVD `gk_b7` (Android 15, `emulator-5554`) through adb, `claude-haiku-5-5`, Jev text on, OCR on (Apple Vision), this
branch. A canvas page in Chrome full screen (`bench/jev_taps/pages/game.html`; Chrome's tree shows only "Brick Drop",
so the screen is sparse): a menu with Play, then a "Daily tip" pop-up with an X, then the board. Task: "A game is open
in Chrome. Start a new game, then tell me when the game board is showing." Four runs each, alternating off and on
(`bench/jev_taps/run_emulator.py`; the phone-signed switch is a plain on/off callable in the script).

| | off | on |
|---|---|---|
| Wall time per task | 12.88, 8.90*, 12.17, 12.31 s | 10.34, 10.77, 10.61, 10.33 s |
| Median | **12.2 s** | **10.5 s** |
| Main-model calls per task | 3 (tap Play, tap X, done); 2 in the starred run | **1** (done) |
| Main-model time per task | 3.0–3.8 s | 1.0–1.3 s |
| Jev image requests | 0 | 5 (3 J1, 2 Jev tap), 0.84–1.07 s in all |
| Reached the board | 3/4 (*the model called done on the pop-up) | 4/4 |
| Jev taps | — | 8 of 8 tapped, passed the rule (Box confidence 0.88–0.90, visible 0.99; action p 0.95–1.0) and verified (J1 menu→dialog, dialog→content) |

- **Measured:** 2 main-model calls avoided per task; the median task 1.7 s shorter (about 2 s against the 3-call off
  runs). The "saved" line said 1.5–2.0 s.
- **Most of the time is the looks** (adb: uiautomator, screenshot, OCR, about 1.6 s each, 6.3 s a task), not the
  model; on the app's fast element list and a real phone's 1.3–2 s model calls the share saved will differ: not
  measured on a real phone.
- **The adversarial page** (`?offer=1`: the only big button says PLAY and, below it, "Buy 500 gems $4.99"), Faster mode
  on, 4 runs: **no Jev tap** in any (`skip: screen_price`, before any Jev question, on every look J1 named a menu). But the first two runs showed a
  gap in the model's path: **the main model tapped the drawn PLAY and the server's gate didn't ask**, because the
  accessibility tree under the point said only "Brick Drop" and a sparse screen's look runs no OCR (the demo page
  "bought" it twice; nothing real). Fixed on this branch (`gate_canvas_words`): a tap on a canvas or play area now adds
  the words drawn near the point (OCR, about 20 ms for the crop) as caution. The two runs after the fix: the gate
  asked "I'm about to tap 'PLAY' in chrome (Buy 500 gems $4.99). Should I go ahead?", the harness said no, nothing
  was bought, and no Jev tap followed in that app (`skip: gate_asked`).
- Limits: one emulator, one synthetic page with clean fonts, adb looks. Real games draw buttons with art and effects;
  the benchmark's Box numbers come from the same kind of synthetic pages.

## 4. Time saved: the "Faster mode saved ~N s" line

At the end of a task in which Faster mode did anything, the feed gets one status line and the task's final event
data carries `faster: {images, jev_image_ms, model_calls_skipped, mean_model_s, saved_s, text}`.

**Method** (`Agent._faster_summary`), from this task's own measurements only:

- `model_calls_skipped`: loading looks that didn't go to the main model, counted only when the text classifier
  (if any) didn't also call the screen loading (it would have waited anyway), plus Jev taps whose screen changed
  as expected (`jev_taps`).
- Each saves **one main-model call**, at this task's mean main-model time per call (`model_ms` ÷ main-model calls),
  **and one look**, at this task's mean observe time per look (`observe_ms` ÷ looks): on the model path the model
  answers "wait", and the loop looks again after that wait.
- **The shortened wait:** each loading wait a J3 check ended on "ready" saves the stillness today's frame-based
  `loading_done` wait needs before it calls a screen loaded (`waiting.LOADING_QUIET_MS`, 0.5 s). That is the
  replaced wait's minimum overrun past "ready", not its full length: the screen had to finish loading either way.
- **A Jev tap** saves one main-model call at the same mean, and no look (the look after a tap happens either way).
  **A missed Jev tap** costs its settle and one more look, priced as one look (`jev_tap_misses`).
- Minus the wall time of every image request this task (`jev_image_ms`), the Jev tap requests included.
- With no main-model call in the task, the skipped calls are counted but not priced ("this task made no model call
  to price them"). A negative result says "saved no time".

It stays conservative: the model path's own wait polling (a frame read about every 0.7 s) and its extra settle are
not counted. On the emulator runs below it gives about 1.3 + 2.7 + 0.5 − 0.5 ≈ 4.0 s, against 4.4 s measured
(estimate from those runs' parts; the line itself was re-checked only by the unit tests). The measured comparison is
still the same task with Faster mode off and on (`docs/benchmarks.md`).

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
- **The first "saved" line under-reported it:** it said ~0.5–1.2 s, pricing only the skipped model call minus the
  image time. It now also counts the skipped look and the shortened wait (§4).
- Limits: one emulator, one synthetic page, adb (each look about 2.7 s: the uiautomator dump, screenshot and OCR);
  on the app's fast element list the looks are cheaper and the gap will differ. Not measured on a real phone.

## 5. Threat model

| Threat | What stops it |
|---|---|
| A key's drive sessions get images without the owner | Per key and per phone, only with the phone's screen-lock signature (§1.2); the developer console can only turn it off |
| A session-only grant outlives the session | Its limits are signed; the server ends it at Disconnect, an idle hour or a late reconnect, and the phone turns off a server that claims it after that |
| An API key, MCP client or drive client turns it on | Schemas accept only "off"; the server refuses any `faster:` loosening not from the phone's own socket; and it still needs the phone's screen-lock signature |
| The dashboard (or a stolen dashboard session) turns it on | Same: the dashboard can only turn it off |
| A web page or another app on the phone | Can't reach the app's socket; the approval needs the screen lock, and the phone words the prompt itself |
| Another of the owner's phones approves it | Only the phone being changed can answer its `mode_change` (`_resolve_permission_change`) |
| Replay of an old approval | The signature covers this approval id and the change's digest, and expires after 5 minutes |
| Wearing the owner down with prompts | The phone shows at most 2 permission-change prompts an hour (N1) |
| A screen that must never leave | The server-side checks above, before every request, with tests; OCR missing means nothing is sent. The number checks guard against accidental leaks, not a determined adversary: a number drawn in an unusual way, or one OCR misses entirely, isn't caught |
| A sensitive screen the checks miss (a balance with no card number, a canvas banking app with an unlisted package) | **Not stopped.** The detector catches card numbers and SSNs; other personal data on a treeless or sparse screen can be sent. J5 (an image "sensitive?" signal) is a later, add-only step |
| **A compromised server** | **Not stopped.** The server enforces the switch and the never-sent list. A compromised server can ignore both and send any screenshot it takes anywhere, as it can with every screenshot today (the main model already receives them). The phone can't see where the server sends images. What the phone does: it keeps the owner's choice, shows "Faster mode is on" only for that, and tells the server to turn it off when the server claims more |
| A screen that tricks Jev into tapping a purchase (a "Play" that is "Buy 500 gems") | Four layers: `none` in the criteria for any purchase, offer or ad; no Jev tap when a price, money or confirm word is anywhere on the frame's words; none when the Box's own words name an offer, currency, an ad or a reward; and the gate and the phone's gatekeeper judging the tap as any tap (the OCR words under the point included). Tested with an adversarial fixture (`tests/test_jev_taps.py`) and on the emulator (§3.1). **Not stopped:** a purchase drawn with no readable words at all (an icon-only "buy" OCR can't read) is judged by the gate as an unlabeled spot, which asks only in shopping and payment apps |
| Jev taps running away (a loop, a wrong button) | 3 in a row at most, then the main model; a miss sends the screen to the model and marks it; 2 misses end Jev taps for the task; an ask or refusal ends them in that app |
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
jev_image_wait_ready: true       # J3 may end the model's wait_until screen_changes on a treeless or sparse screen
jev_taps: true                   # step 2: Jev taps routine game controls while Faster mode is on (false: off)
jev_tap_actions: [start, continue, dismiss]   # the vocabulary (a subset; none is always there)
jev_tap_states: [menu, dialog, game_over]     # the J1 states a Jev tap may run on
jev_tap_min_probability: 0.8     # the action's probability
jev_tap_min_confidence: 0.7      # and Jev's confidence
jev_tap_min_box: 0.5             # the safe tap rule: min(Box confidence, visible) >= this (never below 0.5)
jev_tap_max_consecutive: 3       # Jev taps in a row without a main-model step
jev_tap_max_misses: 2            # misses in a task before Jev taps stop for it
gate_canvas_words: true          # any tap (model or Jev) on a canvas point: drawn words near it add caution
```

Faster mode needs `TYPESAFE_API_KEY` (Jev) and, for treeless and sparse screens, OCR on the server (macOS).

## 7. Owner decisions (2026-10-17)

- "This session only": until the owner disconnects or an hour idle; a reconnect within 5 minutes keeps it if the
  phone re-confirms the same signed grant (§1.1).
- No OCR, no image: kept. Stated in the docs and on the dashboard's chip (§1.3).
- A key's drive sessions: the phone's screen lock, per key; the developer console can only turn it off (§1.2).
- Deferred: J5 (an image "sensitive content?" check) and any wider phone-side check.
