# Safety & approvals

An API key can operate a real phone, and the model holding it may read text that someone else
wrote. These rules hold for every key, over REST and MCP alike.

## Risky actions need the owner's approval

Before a tap that would buy, send, delete, post or similar, the task pauses with status
`needs_confirmation`, and the question says what it's about to do:

```json
{"id": "e111505afeb9", "status": "needs_confirmation",
 "question": "I'm about to tap 'Buy now'. Should I go ahead?", …}
```

The owner then gets an approval prompt on **every one of their phones that is online**, not only
the one acting: they may be holding another. The prompt names the key that is asking and the phone
that would act ("On Pixel 9: …"), and the owner confirms with that phone's screen lock. The first
answer wins and the other phones' prompts are dismissed. If nobody answers within about 2 minutes,
the answer is no. Your webhook, if you have one, gets `task.needs_confirmation`.

**Only that screen-lock prompt can approve.** Typing or saying "yes" (in the OpenMob app's chat, by
voice, or in the dashboard) never approves a risky action; "no" or "stop" still declines or stops
it. And no task may operate the OpenMob app itself: if it's on screen, the agent goes Home first,
and a task that ends up in it stops. So an agent that read malicious screen text can't open the app
and approve its own purchase.

**Approvals are signed by the phone's screen lock.** When a phone pairs, its app creates a key in
Android's secure key store that can only be used right after the owner passes the screen lock
(fingerprint, face or PIN), and gives the server its public half. Every "approve" must carry a fresh
signature from that key, so even someone who steals a phone's connection key can't approve a
purchase: they'd need the owner's screen lock too. Declining needs no signature.

**An approval covers one action, the one it showed.** The prompt says exactly what will happen (the
app, the button's text and id, any text to be typed), and the phone signs a digest of that action.
OpenMob runs the action only if it is still that action: if the screen changed while the owner was
deciding (the button moved, or another one took its place), it doesn't tap, and the agent looks
again. A yes for one action never authorizes another.

**A phone must be able to sign to approve.** On a hosted account, a phone whose app hasn't registered
a screen-lock key (an app older than approval keys) can only **decline**: its prompt says to update
the app and reconnect, which registers the key. Apps older than action-bound approvals are refused
the same way until they update. (Local token mode has no keys, so nothing changes there: an app
phone's screen-lock yes is accepted as before, and a phone over USB is approved from the dashboard.)

## The phone enforces it too (gatekeeper)

Every rule above runs on the server. A phone whose app has the **gatekeeper** (current apps; the hello lists the
`gatekeeper` capability) enforces its own policy as well, so even a compromised server can at most
*request* an action. The design is docs/design/phone-gatekeeper.md (Phase 1); the
messages are in the phone protocol (docs/phone-protocol.md, §9). Older apps keep the
server-side behaviour above.

**Every call is checked on the phone first**, against the screen as the phone sees it (what is really under
the tap, not what the model says is there), and re-checked right before the gesture, typing or launch.
Unknown methods are refused.

**Refused in every mode, on the phone:**
- **Blocked apps**: banking, payments, wallets, crypto, password managers and authenticators, and OpenMob
  itself. Launching them, links into them, acting in them and their app-info page are refused; screenshots
  are black and the element list is empty while one is visible anywhere on screen.
- **Security-critical Settings**: screen lock and security, accounts and users, accessibility, notification
  access and special app access, default apps, developer options and wireless debugging, VPN and Private DNS,
  language and region, and **enabling a keyboard**. Recognized by page title, activity and package across
  AOSP, Pixel, Samsung and Xiaomi Settings and Google account settings.
- **Password fields** (never typed into), and `javascript:`, `intent:`, `file:`, `content:`, `data:` text.
- **Links** other than `https`/`http` (checked against the app Android would open them in), `tel` (the dialer
  only), `geo`, `mailto`, `sms` and `market`.
- **Taps into OpenMob's own screens, the system's biometric prompt and the lock screen**, and the package
  installer.

**Asked on the phone, with its own words and screen lock:** money commits ("Buy now", "Place your order",
"Pay", "Subscribe"), sends, posts and deletes, autofill and saved passwords or cards, share-sheet targets,
notification action buttons, typing into OTP or card fields, ENTER in a chat or a checkout or a web form,
"slide to pay", unlabeled taps (and non-English screens) in shopping, payment, food, travel and ride apps,
manual date and time, pairing a new Bluetooth device, app-info actions other than Force stop and Clear cache,
any change on a Settings page the phone doesn't recognize, and a permission dialog for an app other than the
one the task is using. Swipes that are really taps and long presses are judged as taps. Steps on the way
("Add to cart", "Proceed to checkout", search, scrolling, everyday Settings such as Wi-Fi, Bluetooth, dark mode
or Do Not Disturb) never ask.

- **The prompt is the phone's.** It says what the phone sees ("Allow tapping “Buy now” in Amazon?") with a
  crop of the target; the server's words appear only as a labeled "Server says" line. The phone ignores
  server-worded approval requests for actions.
- **One yes, one action.** The screen-lock signature covers exactly that call and the target under it, for
  60 seconds, once. A changed screen voids it. "Buy now" then "Place your order" in the same app and task
  within 3 minutes is **one** prompt.
- **Only on that phone.** API keys and the dashboard can decline, never approve (`409 approval_on_phone`).
- **"Always allow this"** (off by default) also grants that kind for 7 days (purchases: 24 hours, after a
  second confirmation), per app (a share: per target; a Settings page: that page; never for autofill in
  browsers). Grants are signed with the screen lock and listed on the phone.
- **The mode is the phone's.** The phone takes the tighter of the task's mode and its owner's signed ceiling
  (default **Ask for risky**). **Don't ask** and **"Also allow purchases"** count only when the owner approved
  them on that phone; under Don't ask, purchases (unless allowed), autofill and anything else that would ask
  are refused instead. Every expiry runs on the phone's monotonic clock: setting the date back extends
  nothing, and time left carries across a reboot (failing closed if it can't be read).
- **Limits and Stop.** At most 5 actions a second and 6 prompts per 10 minutes; 3 denials in 10 minutes pause
  agents until the owner taps Resume. Stop (in the app or the notification) drops every approval, prompt and
  Don't-ask window on the phone at once.
- **Refusals are reported, not retried.** The agent is told why, with a stable code
  (`gate_blocked_app`, `gate_denied_page`, …), and picks another way or stops.

**What the gatekeeper doesn't stop yet (Phase 1):** reading screens of apps that aren't blocked and
non-risky actions at any time (sessions come in Phase 2); quick-settings toggles and the notification shade;
icon-only sends with obfuscated ids in languages other than English; and a blocked app opened from the
launcher or recents (everything inside it is still refused and blacked out).

## Phones and their connections

- **One live connection per phone.** If a second connection logs in as the same phone, the older
  one is closed at once and nothing it sends is acted on. When the new connection comes from a
  different network address, the owner's dashboard log notes it ("… reconnected from a new
  connection") and it's written to the audit log; running tasks carry on, since a phone switching
  from Wi-Fi to mobile data looks the same.
- **Unpairing or pairing a phone again** closes every connection that used its old key, at once,
  and the old key can't reconnect.
- **Pairing links can't take over a phone.** Scanning the dashboard's QR code opens the OpenMob app,
  which changes nothing until you pass one screen-lock prompt (strong biometric or the phone's
  PIN, pattern or password) titled "Connect this phone to **\<server\>**?" with the account's email
  under it. Your current pairing stays until the new one has fully worked. Links can't carry
  developer tokens, and the app only talks to `https://` servers (plain `http://` only on your own
  network). `openmob phone add` over USB opens the same prompt: adb access plus that one touch.

### Pairing

- **The pairing code is the credential**, so it's made to be short-lived: the dashboard makes one
  only when you open **Connect a phone**; it works once, for 2 minutes, only for your account, and
  making a new one voids the old. It's an 8-character id plus 128 bits of secret, and the server
  stores only a hash of the secret.
- **Guessing doesn't pay.** Five wrong secrets for a code void it. Wrong codes are limited to 10 per
  network address and 10 per account in 10 minutes (then `429`), and an account can make at most
  30 codes an hour.
- **The phone never holds a dashboard session.** It redeems the code for its own device key
  (`POST /api/pair/claim`, no session), which can only operate that phone. The key is encrypted
  with an AES key in the Android Keystore (StrongBox when the phone has one). Sessions kept by
  older versions of the app are signed out and deleted on update. The sign-in fallback on the phone
  uses a session only to make a code, and signs out before the confirmation.
- **Every phone shows.** The dashboard lists every paired phone, connected or not, and shows **New
  phone added** with one-click **Remove** after a pairing. Pairings, removals and unpairs go in
  the audit log (`device.paired`, `device.removed`, `device.unpaired`, `pairing.code_created`).
- **A new phone can't approve for your other phones until you trust it.** Approval requests go
  to every online phone of the owner, so a phone added with a stolen QR code or a leaked
  `devices:add` key could otherwise approve purchases on the others. A newly paired phone can be
  operated, and it can approve its **own** actions (that's the same exposure as the phone itself),
  but it isn't sent approval requests for your other phones, and its answers to them are rejected
  by the server. You trust it in one click: **Trust for approvals** in the "New phone added" banner
  or under **Your phones**, or from an already-trusted phone signed with that phone's screen lock
  (`POST /api/phone/trust`, signing `openmob-trust-1|trust|<target device id>|<unix time>`: a
  domain-separated message that can't be replayed as an approval, nor an approval as a trust).
  Trust only decides **who is asked**: a trusted phone's yes must still be signed with its own
  key over the action's digest, and only the phone being loosened can approve a permission change
  or "Always allow" for itself. An API key, even with `devices:add`, can't trust a phone. Your
  account's **first** phone is trusted automatically (there's nothing to compare it with), including
  one added with `openmob phone add`. Pairing a phone again resets its trust, because device ids
  aren't secret. Trusting is recorded as `device.trusted` in the audit log. The phone-gatekeeper
  design's cross-signing ceremony (D7/D11 in `docs/design/phone-gatekeeper.md`: a QR or 6-digit
  code between two phones, each confirmed with its own screen lock) is the planned replacement for
  the dashboard click.
- **Unpairing** works from the dashboard (Remove), from the phone (with its device key, no session),
  or from the API's admin tools; each closes the phone's connections at once.
- **Sign-up doesn't reveal accounts.** `POST /api/auth/signup` gives the same reply whether or not
  the email already had an account, and never signs in. With no email sending yet, the remaining
  signal is whether signing in with that password works afterwards, which is the rate-limited
  sign-in (10 failures per address or email in 10 minutes).

A key can't approve on its own:

```sh
curl -s -X POST https://<server>/v1/tasks/$TASK/answer \
  -H "Authorization: Bearer $OPENMOB_API_KEY" -H "Content-Type: application/json" \
  -d '{"approve": true}'
```

```json
{"detail": "Risky actions are approved by the phone's owner, who has been asked on their phone. Check the task again later, or send {\"approve\": false} to decline.",
 "code": "confirmation_requires_owner"}
```

That's on purpose: the model calling the API may have read the very screen text that asks for the
purchase. Declining always works:

```sh
curl -s -X POST https://<server>/v1/tasks/$TASK/answer \
  -H "Authorization: Bearer $OPENMOB_API_KEY" -H "Content-Type: application/json" \
  -d '{"approve": false}'
```

```json
{"ok": true}
```

After a "no", the task doesn't tap and carries on another way, or finishes.

A tap counts as risky when it matches the server's list of risky actions (buy, pay, send, delete
and the like) or is judged risky from the screen. By default, if your instruction itself asks for
that action ("buy the headphones in my cart"), the tap counts as requested and the task doesn't
stop to ask, once, in the app the task works in. Payments ask even then. So put spending, sending
and deleting in an instruction only when you mean it. (A server's operator can make every risky tap
ask, requested or not.) How much a task asks is its [permission mode](#permission-modes).

### Let a key approve (`confirm_approve`)

For your own headless automations you can let one key approve risky actions itself. In the
developer console, go to **API keys** and turn on **This key may approve risky actions** for the key, or tick it
when you create the key. Then:

- The phone doesn't show an approval prompt for that key's tasks.
- `{"approve": true}` from that key goes ahead, and the MCP `next` hint tells the agent to ask its
  user and then approve or decline.
- Each change to the setting is recorded in your audit log.

Only do this for keys that run code you control. A model holding such a key could approve a
purchase it read about on the screen.

## Permission modes

A permission mode decides **whether** a task stops to ask the phone's owner. It never decides who
may approve: approving always takes the owner's screen lock on a phone (or a `confirm_approve` key,
see above). Pick a mode per chat in the dashboard (the chip next to the model picker), per task over
the API and MCP (`permission_mode`), or set a phone's default in its settings.

| Mode | `permission_mode` | Asks the owner |
|---|---|---|
| **Ask every step** | `ask` | Before every change: taps, long-presses, typing (text and its Enter are **one** prompt), a swipe short enough to be a tap, opening an app or a link. Not for scrolling, Back, Home, waiting or finishing. |
| **Plan first** | `plan` | Once for a short numbered plan, before anything happens. Then only for a risky step the plan doesn't name, and for every payment. Saying no ends the task; typing changes makes the agent propose a revised plan. |
| **Ask for risky** (default) | `auto` | At the commit point of a send, delete, post or payment the request didn't ask for. A requested one runs **once**, in the task's own app. Payments always ask (once per purchase, see below). |
| **Trusted apps** | `trusted` | Like Auto, but risky actions in apps or categories the owner trusts run without asking. |
| **Don't ask** | `never` | Never. Purchases (unless the owner also turned on **Also allow purchases**), autofill and blocked apps are **refused** instead. Time-limited (1 h, 24 h or 7 d), only after the owner approves it on that phone. |

### What counts, and when it asks

- **Only the commit point asks.** Searching, opening a product, "Add to cart", "Proceed to checkout"
  or typing a draft never ask. "Send", "Place order", "Delete", "Pay" do. A requested purchase is
  one prompt, worded with the item and price on screen ("I'm about to tap 'Buy now' in Amazon
  (Total $12.99). Should I go ahead?").
- **Payments** (`money_keywords`: buy, pay, order, checkout, subscribe, purchase, transfer, book,
  donate, place order, top up…) always ask, even when requested, unless the app has a trusted `pay`
  entry (in Auto and Trusted; it doesn't count under Don't ask). Under Don't ask they are refused
  unless the owner turned on **Also allow purchases** with it (below). After an approved payment, **one** follow-up commit tap is covered: only for the terminal
  words (place order, confirm purchase, pay, confirm…), in the same app and task, within 3 minutes,
  and never after an app switch. Approving a terminal tap covers nothing more, and words that start
  a new purchase (buy now, subscribe, book, top up, donate) always ask again.
- **The requested-action exemption is used once.** "Send hi to Mom" lets the first Send in the
  task's app run; a second Send, or a Send in another app the agent wandered into, asks.
- **Settings has tiers.** Security-critical pages (screen lock, accounts and users, notification
  access and other special access, default apps, developer options and wireless debugging,
  keyboards, date and time, unknown sources…) are **refused in every mode**, including the way into
  them. Everyday pages (Wi-Fi, Bluetooth, display and dark mode, sound, battery, location, per-app
  notifications) never ask. Pairing a new Bluetooth device and changes on pages OpenMob doesn't
  recognise ask. On an app's info page, Force stop and Clear cache run; Uninstall, Disable and Clear
  storage ask; anything on a blocked app's info page is refused.
- **Autofill and saved credentials** ("Use saved password", passkeys, saved cards) ask, and are
  refused under Don't ask.
- The screen classifier can still only add caution: it can make a tap risky, never make it requested.

### "Always allow this"

Most prompts on the phone offer **Always allow this** (off by default). With the same screen-lock
touch it adds a trusted entry for that app and kind, and the prompt says what it allows ("Agents
could send any WhatsApp message as you for 30 days"). This is the usual way trusted apps get filled
in, and it works in Auto too: no mode switch needed.

| Kind | Lasts | Notes |
|---|---|---|
| send, post, delete | 30 days | A share is bound to its source app **and** target ("Share to Mom") |
| fill (autofill) | 30 days | Only in native apps, bound to that app; never offered in browsers or WebViews |
| pay | 24 hours (7 days at most) | Only for that one app, never a category, and the prompt asks a second time |

It is not offered for Settings (no stable page identity yet), in Ask every step, or on another of
the owner's phones than the one acting. The grant itself is signed by the phone's screen-lock key
(the signed text ends in `|always`), so it carries the screen lock like the approval does.

### The prompt budget

Every prompt is counted per task, with its decision (approved, always, denied, changes) and how long
the owner took to answer. On a self-hosted server, `openmob prompts --since 7d` reports the median
prompts per task and the share of approvals given in under 2 seconds. The budget: at most 1 prompt
for a typical task, and fewer than 20% of approvals under 2 s (faster than that, nobody is reading).
Only counts, kinds, modes and timings are kept, never the prompt's text.

### Ceilings: tightening is free, loosening needs the phone

Each phone has a **default mode**, a **ceiling** (the loosest mode it allows: Ask, Plan, Auto or
Trusted; Auto by default), its **trusted entries** and, while it's on, a **Don't ask expiry**.

- **Tightening** (a stricter default or ceiling, removing a trusted entry, turning Don't ask off)
  takes effect at once, from the dashboard, the API or MCP.
- **Loosening** (a mode past the ceiling, any new or wider trusted entry, turning on Don't ask,
  adding `pay`, allowing purchases under Don't ask) is never applied directly. The server sends a `mode_change` approval to **that phone
  only** ("Allow agents to act without asking on this phone for 1 hour?"); it applies once the phone
  answers with a fresh signature by its own screen-lock key. The dashboard shows "Approve on your
  phone" and waits; the API answers `202 needs_phone_approval` with an `approval_id` to poll.
- A task's mode must be no looser than the phone's ceiling (and, for an API key, the key's
  `max_mode`, `auto` by default). Otherwise it's refused with `403 mode_not_allowed`.
- **Phones without an approval key** (an app older than signed approvals) can't be loosened past
  Auto: update the app and pair it again.
- **Token / dev mode** (`openmob run` without accounts) has no approval keys. An app phone still has
  to approve the change on its own prompt (screen lock, unsigned). A USB/adb phone has no prompt, so
  the dashboard's change applies directly: whoever holds the dashboard token controls the computer
  it's plugged into. Settings are kept in `~/.config/openmob/permissions.json`.
- Pairing a phone again resets its settings (they live on its pairing).

### Don't ask

- Turning it on takes the phone's screen lock; it lasts 1 hour, 24 hours or 7 days, then the phone
  is back to its ceiling and a `never` default reverts to **Ask for risky**.
- While it's on, the phone's OpenMob notification shows a banner ("Agents can act without asking
  until 21:00") with **Turn off**, and the dashboard's live panel shows a warning chip. Turn off works
  from anywhere: the notification, the dashboard, the API (`never: "off"`) or MCP.
- **No prompts, but refusals stay.** Purchases are refused (a trusted `pay` entry doesn't change
  that under Don't ask) unless the owner turned on **Also allow purchases**; autofill and saved-credential suggestions are refused; security-critical Settings stay refused;
  OpenMob itself and sensitive apps (banking, payments, wallets, password managers) are blocked for
  **every** task, the owner's own included, whatever `allow_sensitive_apps` says.
- **Also allow purchases.** A separate checkbox when enabling Don't ask (off by default, never
  remembered), or `allow_money: true` next to `never` in the API or MCP. It is only accepted together
  with turning Don't ask on, and always needs the phone's approval, worded explicitly ("Allow agents to
  act without asking on this phone **including purchases** for 1 hour?"); the flag is inside the signed
  change, and the app asks a second time ("Also allow purchases?") before the screen lock. It ends with
  that Don't ask window (re-enabling Don't ask without it, or `allow_money: false`, turns it off), and
  the banner says so: "Agents can act without asking, including purchases, until 21:00". Blocked apps
  stay blocked, and a key's `max_mode` still caps its tasks (a task in a tighter mode still asks).
- Expiry times are checked by the server against its own clock. (The phone-side gatekeeper will use
  a monotonic clock, so changing the phone's time can't extend it.)

### A test phone for developers

For headless automation, give a **spare phone** the Don't ask mode: from the dashboard pick
**Don't ask → Enable → 7 days** (or `PATCH /v1/devices/{id}/permissions {"never": "7d"}` from a key
whose `max_mode` is `never`) and approve it on the phone. Leave your own phone on Auto and add
trusted apps with "Always allow" as you go. Keep purchases off test phones; turn on **Also allow
purchases** only for a session that must buy, and only on a phone whose payment methods you can
afford to have used.

### How `confirm_approve` relates

`confirm_approve` is unchanged: it means "this key may approve" (say yes to a prompt over the API),
not "don't ask". Modes decide whether a prompt appears at all. A key with `confirm_approve` and the
default Ask for risky mode gets the same prompts as any key; they go to the key instead of the phone. No mode
lets a server, key or dashboard approve on its own.

## What is guarded

Every action that could commit something is judged by **what it really touches**, never by the
model's own description of it. The model's words can only add caution.

| Action | How it's judged |
|---|---|
| `tap`, `long_press` at coordinates | By the element actually at that point on the screen (re-read if the screen may have changed): its text, description and resource id. A long press of any length is judged as a tap. If nothing labeled is there, it asks in shopping, payment, food, travel and ride apps |
| `tap_element` | By the element's own text, description and resource id, plus whatever its centre lands on |
| `swipe`, `scroll` | A swipe that barely moves (within twice the touch slop, 48 px) or is quicker than 150 ms is a tap, judged as one at its start point; so is a swipe that starts and ends on the same button. Any other swipe is judged at each end that is a commit control: a slider ("Slide to pay") or a risky label |
| ENTER (`press_key ENTER`, `type_text` with `submit`) | In messaging, email and social apps it's a **send**. In a browser or web form, and on a payment screen, it's a **submit** that asks (as a payment when money words are on screen). Search boxes never ask. In a browser's address bar, the link checks below run on the URL |
| `open_url` | Only `https`/`http`, `tel`, `mailto`, `sms`, `geo` and `market` links. `javascript:`, `intent:`, `file:`, `content:`, `data:` and app-specific schemes are refused. The phone says which app would open the link **before** it opens, and a link into a blocked app (a bank, say) is refused. With an older app, OpenMob checks the app in front right after opening and backs out (Back, then Home) of a blocked one |
| `type_text` | Never into a password field. Never a `javascript:`, `intent:`, `file:`, `content:` or `data:` URL into an address bar |

Typed text that goes through the clipboard (some fields only accept a paste) doesn't stay there: the
app marks it sensitive (kept out of the clipboard preview on Android 13+) and puts the owner's
previous clipboard back, or clears it.

These are server-side checks. On phones with the gatekeeper the phone runs the same rules itself
([above](#the-phone-enforces-it-too-gatekeeper); [design](design/phone-gatekeeper.md)).

**Android permission dialogs** ("Allow Chess.com to send you notifications?") are answered by rule at
observation time, before any model sees the screen, and recorded as a fast step (`handled_by:
"permission_rule"`): notifications are denied unless the owner set `notifications_permission` to
`allow`; camera, microphone, location, contacts, photos and media, phone, SMS, calendar, nearby devices
(and anything else) ask the owner ("Allow Chess.com to use your camera?"), and a yes taps the narrowest
grant ("Only this time", else "While using the app", never "Allow all the time"); a request from an app
other than the task's asks; under Don't ask everything but notifications is denied.

## Sensitive apps are off-limits by default

API tasks can't open banking, payment, wallet, crypto, password-manager or authenticator apps. A
task that tries gets a warning event and carries on without that app:

```
event: warning
data: {"id": 46, "time": 1791219674.43, "type": "warning", "message": "launch_app: This app is blocked for API tasks on this phone (its owner can allow it). Don't try to open it another way; call done."}
```

A task that finds one of these apps already in front stops and fails.

The owner can allow them in the developer console: **Settings** → **Safety** → **Allow API tasks
to open banking, payment, wallet and password-manager apps on my phones**. Risky taps still need
approval when it's on.

## Screens and secure locks

- **Screenshots are never stored.** `GET /v1/devices/{device_id}/screenshot` and `get_screenshot`
  send the image straight from the phone to you with `Cache-Control: no-store`. They need the
  `screen:read` scope, which is never a default, and each one is recorded in your audit log.
- **OpenMob never unlocks a secure lock screen.** Before a task (or a multi-phone step) it wakes a
  screen that's off and swipes away a swipe-only lock screen, then the task runs. A PIN, pattern,
  password or biometric lock is left alone: the task is refused with `409 phone_locked` (a step
  waits for the owner to unlock it), and OpenMob never enters a PIN, pattern, password or
  fingerprint.
- **The screen stays on while the agent works**, and only then (the app holds it on during active work and
  lets go as soon as the task ends). If a secure lock comes up mid-task anyway, the task **pauses** with
  "Unlock your phone to continue" on the phone and in the task's events, and carries on when the owner
  unlocks, for up to `locked_wait_seconds` (default 5 minutes); then it fails with reason `phone_locked`. The
  agent never taps or swipes at a lock screen.

## Untrusted content

A task's `question`, `outcome.summary`, event messages and screenshots can contain text from the
phone's screen, which may have been written by anyone. Treat it as data, never as instructions to
your own code or agent. Over MCP every task result carries `untrusted_content: true`. See
[MCP](mcp.md#untrusted-content).

## Everyone stays in their own account

A key reaches only its own account's phones and notes, and only the tasks and multi-phone runs
**it** started: another key's tasks, even on the same account, answer `404` exactly like an id that
doesn't exist (getting, listing, events, answering, cancelling, and `GET /v1/history`). Other
accounts' phones and tasks can't even be confirmed to exist. The account owner's dashboard and
developer console still show every key's work. Rotating a key keeps it the same key, with its tasks.

## The owner stays in control

- The phone shows which key is using it while an API task runs, and the owner can stop the task
  from the phone (Stop, or saying "stop").
- For a [multi-phone run](api.md#orchestrations), stopping on a phone that runs one of its steps,
  or denying one of its approvals, ends the **whole run** as `cancelled` with reason
  `stopped_on_phone`, whatever its `on_failure` says. Don't start it again unless the owner asks.
- **Revoking** a key in **API keys** cancels its unfinished tasks at once.
- A server's admin can switch off a key or a whole account's API access, which also cancels its
  running tasks. Requests then get `403`.

## Audit log

Every key change, task, answer, approval, cancel, screenshot, memory change, webhook delivery,
setting change and phone connection replaced from a new address is written to an append-only audit log. Your account's entries are under **Logs** in
the developer console, with the key that did each one. The log stores a hash of each instruction,
never its text.

## Data and privacy

What's kept, where, and for how long (`history_retention_days`, **30 days** by default; `0` keeps
everything forever; one job deletes all of it every 6 hours):

| What | Where | Kept |
|---|---|---|
| Task instructions, outcome summaries, events and step records | Chat history (`history.sqlite`), shown in the dashboard and, for a key's own tasks, `GET /v1/history` | Until the chat hasn't been used for the retention period |
| Multi-phone run plans (instructions) and step results | `orchestrations` in the ledger | The words are blanked after the retention period; statuses, reasons and timing stay |
| Webhook bodies (a snapshot of the task or run) | The webhook outbox in the ledger | Delivered or dead deliveries: the retention period. Pending ones until delivered or given up (about an hour) |
| Idempotency replays (the first response) | The ledger | 24 hours |
| Live task records | Server memory | Until they age out (200 per phone) or the server restarts |
| Billing totals: task id, key, phone, tier, steps, credits, status, an instruction **hash** | `api_tasks` in the ledger | Kept (no text) |
| Audit log (actions, with instruction **hashes**) | The ledger | Kept (no text) |
| Memory notes | `memory.sqlite` | Until you delete them (they're your saved knowledge, not task logs) |

- Screenshots are never stored.
- To carry out a task, the phone's screen content is processed by the AI providers that power
  OpenMob's tiers. The API itself never names them: you choose a tier, not a model.
- When screen classification is on, a text description of each screen (the foreground app, the
  on-screen elements' labels and state, and words read from the screen; never a screenshot) goes to
  a classification service together with the request and a short list of recent actions (never
  typed text). It judges loading screens and risky taps, and on screens with a clear element list it
  may choose a simple next step itself (tap, scroll, back, type) without the main model. Text to
  type is then written by a small, fast model from the same providers. These steps go through the
  same approval checks as any other, and only the main model can declare a task finished.
