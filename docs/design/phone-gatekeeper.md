# Phone as gatekeeper: on-device policy enforcement

Status: reviewed and approved (3 review rounds), 2026-10-05. **Phase 1 implemented** (branch `gatekeeper-p1`): see "Phase 1 as built" at the end for what differs from this text.
Decisions are marked **Decision:** and numbered (D1, D2, …). Changes from review 1 are listed at the end.

## 1. Problem and threat model

Every safety control runs on the server today, and the phone obeys any `call` on its authenticated socket:

- `ConnectionService.handleCall` passes every method except `device_state` and `stream_*` straight to `PhoneActions.handle`, which runs it.
- **Sensitive apps** are a server-side regex list (`DEFAULT_SENSITIVE_APPS`, `openmob/config.py`). `V1Service._blocker` (`platform/service.py`) applies it to API tasks only. `Agent._is_blocked` (`agent/loop.py`) enforces it at `launch_app` and at the start of each step, but `open_url` has no check.
- **Risky taps** are judged by `Agent._guard`: a keyword hit or a classifier risk score. For a coordinate `tap` the description is the model's own `target` string. `type_text`, `key ENTER` and `swipe` are never guarded.
- **Approvals** are signed on the phone (`ApprovalKeys`, `ApprovalActivity`) and verified by the server (`phone/approval_keys.py`), over `<approval id>|<task id>|true|<ts>`. The signature does not bind the action. When the phone has no key, `ApprovalActivity` approves unsigned.
- **The block on OpenMob's own app** is server-side too (`runner.py` `OWN_PACKAGES`).

| # | Attacker | With the gatekeeper |
|---|---|---|
| A1 | Compromised server | Can only *request*: blocked apps and dangerous settings are refused, and risky actions need the owner's screen lock on a prompt the phone words itself |
| A2 | Leaked device key on another client | Same as A1 (on top of today's one-socket rule, close code 4409) |
| A3 | Malicious agent, or a stolen API key | Server checks plus phone checks |
| A4 | Prompt-injected agent | The phone hit-tests the real target, whatever the model claims |

**Out of scope:** a malicious app on the phone, physical access with the PIN, a compromised OS or Keystore, and a malicious OpenMob APK. Release signing keys never touch the server.

**Goals.**
- **G1:** no socket command can open a blocked app, change dangerous settings, or do a risky action without the owner's on-device consent.
- **G2:** the prompt is worded from the phone's own view.
- **G3:** the server can't weaken the policy.
- **G4:** no measurable latency on the fast path.
- **G5:** at most one prompt per irreversible commit, and a median of one prompt or fewer per typical task (§6). Too much friction defeats the purpose, because owners then pick `never` or uninstall.

**Non-goals.** Stopping reads of non-blocked screens inside an allowed session, matching the AI's judgment on unlabeled UI, and protecting old apps.

## 2. Design

### 2.1 Enforcement point

**Decision D1:** A `Gatekeeper` is the first statement in `ConnectionService.handleCall`'s executor block, ahead of the `device_state` and `stream_*` branches.
- **`admit(method, params)`** returns Allow, Refuse(code) or Ask.
- **`reverify(permit)`** runs inside `PhoneActions` immediately before each side effect: in `gesture()` under `gestureLock` before `dispatchGesture`, in `typeText` before `setText`/`paste`, in `key`, and in `launchApp`/`openUrl` before `startActivity`.
- **Unknown methods are refused.** A parity test fails if any `protocol.py` `METHODS` entry or `stream_*` method lacks a class.
- **While a gate prompt is showing, every act call is refused** (`gate_prompt_open`).

| Class | Methods | Gate |
|---|---|---|
| meta | `device_state`, `wait_idle` | Always allowed: no screen data, on the fast path |
| read | `screenshot`, `ui_elements`, `stream_*`, `current_app`, `launchable_packages`, `app_labels` | Session (Phase 2). A black frame or `[]` whenever a blocked app is visible in *any* window (split screen, PiP, bubble) |
| act | `tap`, `long_press`, `swipe`, `type_text`, `key`, `launch_app`, `open_url`, `wake_unlock` | Full order (§2.3) |

### 2.2 Inputs the phone uses

- **Target package:** the package of the window that owns the hit node, not the cached `currentPackage`.
- **Tap target:** a hit test at the point over `service.windows` in descending layer order (the layering `ui_elements` uses for `covered`). The deepest visible node, then its nearest clickable ancestor. Label: its text, description and descendant text, plus its resource id, class and window type.
- **Typing and keys:** the `focusedEditable()` node with `isPassword`, `inputType`, hint, resource id, any WebView ancestor and current text.
- **Launches and URLs:** the component resolved with `pm.resolveActivity` / `queryIntentActivities`.
- **Screen context**, computed on act calls only:
  - **payment context:** card, CVV, IBAN or expiry fields; autofill hints for credit cards; or "Pay", "Place order" or "Buy now" next to a currency amount;
  - **screen language:** the share of visible-text tokens found in the shipped UI lexicons.

### 2.3 Evaluation order

1. **Session.** No active session covering this app gives `gate_no_session` (Phase 2).
2. **Hard denies and blocked apps** give `gate_blocked_app` / `gate_denied_page`.
3. **Rate limit** gives `gate_rate_limited`.
4. **Risk** (§2.4) gives Ask: the phone prompts and the call returns `gate_needs_approval`.
5. **Execute and log:** `reverify`, act, local log.

**Hard denies.** No layer can relax these.
- **Apps:** OpenMob itself, and taps into its windows.
- **Prompt surfaces:** the BiometricPrompt and keyguard (systemui windows identified by window type and title).
- **Installers:** the package installer.
- **Recognized Settings pages:** accessibility, notification-listener access and all other special app access (usage, overlay, all files, device admin, install unknown apps), default apps, accounts and users, security and screen lock, developer options and **wireless debugging**, VPN and Private DNS, and language and region.

Recognition works by intent action, window title and fragment class, across AOSP, GMS (`com.google.android.gms` account settings) and OEM packages (`com.samsung.accessibility`, `com.miui.securitycenter` and others).

**Decision D2 (Settings: three tiers, revised for friction):** Settings pages in the AOSP, OEM and GMS packages fall into three tiers.

| Tier | Pages | Gate |
|---|---|---|
| Security-critical | The hard-deny list above, plus **enabling a keyboard** (on-screen keyboards / manage keyboards) | Refused in every mode |
| **Everyday (generous allowlist)** | Wi-Fi, mobile data, hotspot, Bluetooth on/off and connecting to already-paired devices, NFC toggle, airplane mode, display (brightness, dark mode, font size, screen timeout), sound, vibration, Do Not Disturb, battery and battery saver, storage overview, location toggle, per-app notifications, per-app permissions and runtime-permission dialogs **for the session's app** (see below), app info **Force stop and Clear cache only**, switching among **already-enabled** keyboards, About, search inside Settings | **Don't asks** |
| Unrecognized, plus these named pages: manual date and time, pairing a new Bluetooth device, app-info actions other than Force stop and Clear cache (Clear storage, Disable, Uninstall) | Any page in a Settings package that isn't on either list | Asks. "Always allow" is bound to the exact page fingerprint (D20) |

Scroll and BACK are always allowed.

Three limits on the allowlist:
- **It only exempts a page from the Settings tier.** It never overrides §2.4: a "Delete", "Uninstall" or commit word on an allowlisted page still asks.
- **App-info actions on a blocked app are refused** (`gate_blocked_app`). That covers even Force stop, and blocks wiping an authenticator through Settings.
- **"The session's app"** is decided by the phone: the requesting package named in the permission dialog must equal the package of the session (Phase 2), or in Phase 1 the foreground app of the gated call just before the dialog appeared. A server's claim is never used.

**Decision D22 (one trusted clock):** Every expiry is checked on the phone's own monotonic clock: grants (D17, D20), permits (D5, D4), plans (D19), sessions and `never`.
- **Issued grants** store `issued_elapsed = SystemClock.elapsedRealtime()` and a duration, plus a persisted **high-water mark** of elapsed time.
- **After a reboot**, the elapsed clock restarts at zero. Remaining durations carry over as follows:
  1. at shutdown (and every minute while running), the phone persists each grant's remaining time;
  2. after the reboot, it counts down from that saved value;
  3. if the remaining time can't be read, the grant expires (fail closed).
- **Wall-clock time** is used only for display. Setting the date back extends nothing.
- **Known limits.** A crash or power loss can stretch a grant by up to about 1 minute per reboot, because remaining time is saved once a minute. Time while the phone is off doesn't count down. An optional cap bounds a grant's total life: count boots against a maximum, or tie its validity to a Keystore key with a validity end date.

**The trade-off.** The reviewer's B4 property is "a dangerous page can't be changed silently". Asking on *unrecognized* pages keeps that property: an OEM page we failed to recognize fails closed. The generous allowlist plus the OEM fixtures keep the "unrecognized" tier rare in practice. A permission dialog for an app *other* than the one the task is operating also asks, because granting permissions to an unrelated app is a known abuse route.

### 2.4 Local risk rules

**Decision D3:** The judge is rules only and ships in the APK. It may only add caution: server hints turn Allow into Ask, never the reverse.

| Target | Rule | Result |
|---|---|---|
| Tap, long press, and the start and end points of any swipe | Words (NFKC, case-folded, diacritics stripped; lists per shipped language), resource-id regex `(?i)(buy|checkout|purchase|pay|place_?order|send|submit|delete|remove|transfer|confirm)`, server `hint` | Ask |
| Swipe within twice the touch slop, or shorter than 150 ms | **It is a tap**, judged as one | As a tap |
| `long_press` | Always judged by tap rules; duration clamped up to `ViewConfiguration.getLongPressTimeout()` | As a tap |
| Slider or SeekBar at the swipe start, or "slide to …" labels | Ask | Ask |
| Unlabeled target (no text, description or id) in shopping, payment, food, travel or ride apps, or in payment context | Ask | Ask |
| **Uncovered language:** screen language below 60% lexicon coverage. Applies **only** in shopping, payment, food, travel and ride apps and in payment context; never globally, so messaging and browsing in any language work as usual | Every non-navigation tap asks (fail closed) | Ask |
| Tap in an **autofill, inline-suggestion or Credential Manager** surface: the IME window, the current autofill service's windows (`AutofillManager`), `com.android.credentialmanager`, the GMS credential sheet | "Fill your saved card or password into *site/app*?" | Ask, always |
| Share sheet / chooser (`com.android.intentresolver`, Quick Share, Nearby Share) target | "Share to *target*?" | Ask |
| Notification action buttons in systemui | Ask | Ask |
| `type_text` | Password field: refuse. OTP, card or CVV field: ask. Text starting with `javascript:`, `intent:`, `file:` or `content:`: refuse | |
| `key ENTER` | Ask in messaging, email and social apps; in any browser or WebView form field (unless it looks like search: id or hint "search", class `SearchView`); and in payment context. **In a browser URL bar** (focused field in a browser whose text parses as a URL), run the full `open_url` scheme and target checks on the text | Ask, or as `open_url` |
| `launch_app`, `open_url` | Resolve the target first. Blocked, payment-category or hard-denied target: refuse. Schemes allowed: `https`/`http` (browser or verified app link, target checked), `tel` (dialer only), `geo`, `mailto`, `sms`, `market`. Everything else is refused (`intent:`, `upi:`, `bitcoin:`, custom schemes into payment apps) | Refuse or allow |

**Decision D4 (ask only at commit points, revised for friction):** Words come in two classes.
- **Commit words** perform an irreversible step. Money commits come in two kinds:
  - **flow-starting:** buy now, subscribe, book, top up, donate, transfer;
  - **terminal:** place order, confirm purchase, pay, confirm.

  Non-money commits: send, post, publish, delete (in a confirmation), uninstall.
- **Pre-commit words** only move toward a commit: add to cart, checkout, proceed to checkout, continue, review order, select delivery. **They never ask.**

**Money commits always ask**, even when requested, under `auto`, `plan` and `ask`. They ask exactly **once per checkout** (Q7 ruling from review 2):
- **When a permit is minted:** an approved *flow-starting* money commit mints a **purchase-flow permit**. An approved *terminal* commit mints none.
- **What it covers:** the permit covers **one** follow-up tap whose label is a *terminal* word, in the same app and the same task, within 3 minutes on the monotonic clock (D22). Each tap still passes `reverify` (D8).
- **When it ends:** at an app switch, or once used. A new flow-starting word always asks again.

"Buy now" followed by "Place your order" is one prompt; a second order is a second prompt. If Phase 0 finds real flows with two terminal steps, the count may rise to two.

**What isn't bound:** the approval binds the tapped control and the crop, not the item, quantity or price inside the flow. The crop is how the owner checks them.

Two notes on the rules above:
- **Category data:** `ApplicationInfo.category` has no shopping or finance category, so a category table ships in the APK, along with a port of `DEFAULT_SENSITIVE_APPS`.
- **Why the language test is per screen:** coverage is judged on the whole screen rather than on each label, so product names on an English shopping page don't count as "uncovered". A switch of the system language goes through Settings, which is hard-denied. An app's own language menu is caught by the fail-closed language rule.

### 2.5 Local approval and permits

**Decision D5:** Ask does not hold the call open (`tap` times out after 10 s).
1. The phone replies `ok:false, code:"gate_needs_approval", approval_id` and shows its prompt.
2. The prompt reuses `ApprovalActivity` (FLAG_SECURE, `filterTouchesWhenObscured`).
3. The owner passes BiometricPrompt with a **CryptoObject**. The auth-bound approval key signs `gk1|device|approval_id|action_digest`, where the digest covers method, params, target fingerprint and crop hash.
4. The phone verifies its own signature, then mints a one-shot permit that lasts 60 s.

A permit can be minted only from that unlocked signing operation. **There is no unsigned fallback for gated actions**: today's "no key, approve unsigned" path in `ApprovalActivity` stays for legacy server approvals only. The server then re-sends the identical call once, and the call consumes the permit after `reverify`.

The prompt:

> **Allow tapping "Buy now" in Amazon?** [crop with crosshair]
> *Server says:* requested by "CI bot"; "I'm buying the USB-C cable"

The bold line and the crop come from the phone. Server text sits in a separate, labeled line. Deny needs no authentication.

**Decision D20 ("Always allow this" on every prompt; the main friction reducer):**
- **What it offers:** every eligible Ask prompt has a checkbox, **off by default**, that says what the grant allows: "☐ Always allow: for 7 days, OpenMob (or a compromised server) could send any WhatsApp message as you without asking".
- **One touch:** the same screen-lock pass that approves the action also signs a `trusted` grant into the owner policy (D17). A `pay` grant needs a second confirm screen before the screen lock.
- **Binding:** set per kind, so the grant follows where the risk lies.

  | Kind | Bound to |
  |---|---|
  | send, post, delete | App |
  | pay | App (default 24 h, at most 7 days) |
  | share | (source app, target) |
  | settings | The exact page fingerprint: package, fragment or class, and title |
  | autofill, credentials (native apps) | The package being filled |
  | autofill, credentials (browsers, WebViews) | **Not offered**: the site, not the app, is where the risk lies |

- **Expiry:** kinds other than pay default to 7 days, with a maximum of 30, all on the monotonic clock (D22).
- **Ceiling:** accepting it raises the ceiling for that binding only.
- **Where it's hidden:** the option isn't offered where MDM forbids `trusted` for that app (D18), on autofill prompts for a password manager, or on hard-denied pages (which never prompt).
- **Management:** grants are listed under **Activity → Always allowed**, where each can be revoked with one tap.

Because the grant is created on the phone, signed by the auth-bound policy key and checked locally, a server can't create or extend one (D9, D14).

### 2.6 Sessions and "requested"

A session is `{pairing, scope: read|act, apps, expires_at, label, mode}`.

| Start | Friction |
|---|---|
| The owner speaks or types a request in the app | Implicit; tied to the task, 30 min |
| A remote task's first gated call | One notification ("CI bot wants to use this phone · 30 min / 2 h") plus the screen lock; the label is shown as server-asserted |
| A standing grant set in the app with the screen lock (headless devices) | None after setup; up to 30 days |

**Decision D6 ("requested" is one-shot; this is the `auto` mode's rule, §2.10):** Only **phone-known** instructions count as "requested":
- an instruction typed or spoken on this phone;
- a remote instruction the owner saw and approved with the screen lock in the session prompt.

Even then, only **non-money** risky words count (send, post, delete). Each word exempts **one** matching action, in the app the session is on, and is then used up. "Send hi to Mom" waves through exactly one Send.

The residual: the exemption binds the word and the app, **not the recipient or the text**, so the server chooses both for that one message (the `ask` mode removes it). An instruction the owner never saw, such as one sent under a standing grant, exempts nothing. Server-side `confirm_approve` therefore has no effect on gatekeeper phones.

**Decision D7:** Skipping prompts for chosen apps is the `trusted` mode (§2.10), not a separate `no_ask` flag. There are no amount caps, because the phone can't parse amounts reliably.

**Reads (decided by owner, 2026-10-05):** agent reads need a session.
- **Developers:** a standing read grant is fine if the phone shows its own persistent "being viewed" indicator.
- **The owner's live view:** implicit while the OS screen-capture consent is active and the indicator shows.

### 2.7 Rate limits and kill switch

- **Act calls:** at most 5/s, burst 15.
- **Prompts:** at most 6 Asks per 10 min, and at most 2 session requests and 2 `policy_proposal` prompts per hour.
- **Denials:** three within 10 min suspend the session until the owner resumes it.
- **Kill switch:** Stop/Disconnect (`ConnectionService.shutDown`, the tile) also revokes every session and permit locally.
- **Indicator:** driven by calls the phone actually executes, so a server that never sends `controller` cannot hide.

### 2.8 Logging and reporting

**Local log:** every gated call goes into a 7-day ring log (method, package, label, decision, rule; typed text only as its length), shown under **Activity**.

**Refusals in the result:**

```json
{"type":"result","id":"…","ok":false,"code":"gate_blocked_app",
 "error":"Blocked on the phone: Chase is a banking app. Don't open it another way; call done.",
 "gate":{"rule":"builtin:sensitive_apps","package":"com.chase.sig.android","ms":6}}
```

- **Codes:** `gate_no_session`, `gate_blocked_app`, `gate_denied_page`, `gate_rate_limited`, `gate_needs_approval`, `gate_denied`, `gate_screen_changed`, `gate_screen_unstable`, `gate_prompt_open`, `gate_password_field`, `gate_unknown_method`, `gate_policy_invalid`.
- **New messages:** phone to server, `gate_event` (approval pending, approved, denied or expired; session started or ended). The hello adds `capabilities: ["gatekeeper"]` and `policy_hash`.
- **Server side:** `RemoteDriver._call` (`phone/remote.py`) raises `GateRefused(DeviceError)` carrying the code.

**API state machine on gatekeeper phones:**
- `gate_needs_approval` puts the task in `needs_confirmation`, with `approver: "phone"`.
- `POST /v1/tasks/{id}/answer` with `approve:true` returns 409 `approval_on_phone` for every key. `approve:false` makes the server tell the agent "no" and send nothing.
- On `gate_event approved`, the server re-sends the call **at most once per approval_id**.
- `gate_screen_changed` after an approval goes back to the agent; it is never retried automatically.
- Denied, or expired after 2 min, is reported to the agent as "not approved".

This replaces the `confirm_approve` branch in `service.py`'s answer handler for these phones.

### 2.9 TOCTOU

**Decision D8:**
1. `admit` records the target fingerprint: window id, package, view id, text, description, class and bounds of the node and its clickable ancestor.
2. `reverify`, inside `gestureLock`, requires the same fingerprint. For risky or payment-context decisions it also requires a quiet screen (`lastChangeAt` at least 150 ms old).
3. A mismatch returns `gate_screen_changed`. A permit is bound to the fingerprint, so a screen that changed during the prompt voids it.

The remaining window is about 50–100 ms between re-verify and the gesture landing, and a log entry flags any window change seen during the gesture.

### 2.10 Permission modes

These are the modes the server and dashboard are adding, enforced on the phone. Hard denies (§2.3), blocked apps, password-typing refusal and `javascript:` refusal apply in **every** mode.

| Mode | What asks on the phone |
|---|---|
| `ask` | Every changing act: taps, typing, ENTER, non-scroll swipes, launches and URLs. Not scroll, BACK, HOME or reads |
| `plan` | Only actions outside the owner-approved plan, and money (always) |
| `auto` (default) | Risky actions (§2.4), except one-shot phone-known requested non-money words (D6); money always asks |
| `trusted` | Like `auto`, except that risky actions in the owner's chosen apps or categories run without asking until the expiry. `pay` only by an explicit per-app entry |
| `never` | Nothing asks. Anything that would always ask (autofill and credential surfaces, unrecognized Settings pages, permission dialogs for other apps) is **refused** instead, because nobody can be asked. **Money commits are refused too**, unless the owner turned on **"Also allow purchases"** when enabling this `never` window (signed on the phone, D21); a per-app `pay` grant doesn't count under `never` |

**Decision D16 (the phone owns the mode):**
- **Ranks:** `ask` 0 < `plan` = `auto` 1 < `trusted` 2 < `never` 3.
- **Ceiling:** the phone keeps a **ceiling**, stored in the owner-signed policy, default `auto`; MDM can lower its maximum (D18).
- **Server requests:** the server sends a requested mode on each session request and as an optional `mode` field on calls. The dashboard picker and per-key `max_mode` are server-side, so the server sends the result.
- **Effective mode:** `min(requested, session mode, ceiling, MDM max)` by rank.
- **Tightening** is accepted from anywhere, including a server claim, because a lie can only cost the attacker. When `plan` and `auto` meet, the stricter combination applies: both modes' asks.
- **Loosening past the ceiling** happens only through a phone prompt ("Allow *trusted* for WhatsApp (send) until Friday?") passed with the screen lock. That prompt mints a signed grant.

A compromised server claiming `never` or `trusted` gets the ceiling, which is `auto` by default.

**Decision D17 (storage):**
- **Signed grants:** the ceiling, `trusted` entries `{app | category, actions: send|post|delete|share|autofill|settings|pay, expires}` (`pay` per app only) and `never` grants live in the owner policy file. A `never` grant lasts at most 24 h (default 8 h), or at most 30 days on a phone marked **test phone** (D21). Each change is signed with the auth-bound policy key (§4) and verified on load.
- **Expired or badly signed grants** fall back to `auto` (fail closed).
- **Server-requested tightening** is an in-memory overlay per session. It needs no signature, because it can only tighten.
- **`never` banner:** while `never` is active, the phone shows its own persistent banner and notification ("No approvals: OpenMob acts without asking until 18:00 · Stop"). Stop drops the mode back to the ceiling's default at once.

**Decision D18 (MDM):** Managed config may set `max_mode` (for example forbid `never` and `trusted`), `default_mode`, and a list of apps where `trusted` is forbidden or `pay` is never allowed. The owner can tighten further but cannot exceed MDM.

**Decision D19 (`plan` binds through permits):**
1. **Submission:** the server sends `plan_request {plan_id, steps}`. Each step is `{app, action: tap|type|send|post|delete|launch|open_url, target_words, count}`, plus a free-text summary shown under "Server says".
2. **Approval:** the phone renders the steps from these structured fields, with app labels it resolves itself. The owner approves with the screen lock, and the approval key signs `gk1|plan|plan_id|digest(steps)|expires`.
3. **Plan permits:** the phone turns each risky step into a plan permit that is one-shot and counted down: `(app, action category, target words)`. A plan permit binds the app, the kind and the target words, **not the recipient, the text or the item**; money steps always ask anyway.
4. **Matching:** at Ask time (§2.3 step 4) a risky action that matches an unspent plan permit gets **no prompt**. It still goes through `reverify` (D8), and the permit is spent. Labels match on whole normalized words.
5. **Re-asks:** an action in an app the plan doesn't list, a risky action with no matching permit, and any money action all ask with the usual D5 prompt. A yes to that prompt covers that one action only.

The plan expires with its task, or after 30 min.

## 3. Coexistence with server-side checks

All server checks stay (scopes, `_guard`, `_blocker`, `OWN_PACKAGES`, quotas). They catch problems early and give the model better errors.

**Decision D9:** On gatekeeper phones the phone owns approval. When `_guard` fires, the server sends the call with `hint` instead of `approval_request`. The owner gets one prompt covering both detectors. The server-verified path (`verify_approval`) remains for old apps. **Gatekeeper phones ignore `approval_request` entirely.** A server-worded prompt behind the screen lock would be phishing, even though its signature is useless to the gate.

### 3.1 Approving on another device

Today `_ask_owner`/`_approval_targets` (`devices.py`) fan a request out to every online phone. The first signed yes wins, and the server checks it.

**Decision D10 (before cross-device trust ships):** For gatekeeper phones, **only the acting phone can approve**. The owner's other phones get an unsigned notice, "Pixel 9 needs your approval", with no Approve button. Keeping the unbound fan-out would break G1.

**Decision D11 (cross-device trust, Phase 3): pairwise cross-signing with an authenticated ceremony.**

Each phone holds three Keystore keys:
- the auth-bound **approval key**;
- a non-auth-bound **request key**;
- an auth-bound **policy key** (§4).

All three have `setUnlockedDeviceRequired(true)`, StrongBox where available, and **key attestation**, so peers check that the keys are hardware-backed against the Google attestation roots. Each phone stores an approver list signed by its own policy key.

**Default route: one QR scan.** Both phones are in hand, since both screen locks are needed.
1. A shows a QR with `fp(A's keys)` and a fresh 128-bit MAC key K.
2. B scans it, fetches A's keys through the server and checks them against `fp`.
3. B sends its keys with `MAC_K(keysB ‖ keysA ‖ account)`.

The server never sees K, so it can substitute neither key.

**Fallback: a 6-digit code with commit-then-reveal** (as in ZRTP and Bluetooth SSP).
1. A sends `C = H(keysA ‖ nA)`.
2. B sends `keysB ‖ nB`.
3. A reveals `keysA ‖ nA`, and B checks C.
4. Both show `trunc6(H(C ‖ nA ‖ nB ‖ keysA ‖ keysB ‖ account))`.

The server must fix its substitute key before it sees nB, so a man-in-the-middle gets one guess at 10⁻⁶ per attempt, and attempts are rate-limited (3 per hour).

**Joining.** The owner confirms with the screen lock on both phones. A then signs an "add B" record that its other trusted phones accept, as in Matrix cross-signing. The first phone trusts only itself.

**Approval messages.**
- **The request** carries the acting phone's wording, the crop bytes and the action digest, signed by its request key. The approver verifies the signature *and* that the crop bytes match the signed hash before showing anything.
- **The approval** is signed over `gk1|acting|approval_id|digest`.
- **Freshness:** the acting phone checks against **its own** monotonic issue time for `approval_id`, with a 2-minute life, and marks the id used. It never trusts the approver's timestamp.

**Revocation.**
- **Removing a phone:** any trusted phone signs a "remove X" record with its screen lock, and each phone can also remove an approver locally.
- **Withheld removals:** a server can withhold removal records. A removed key still needs that lost phone's screen lock, which is physical access and out of scope.
- **Re-pairing** wipes a phone's lists.

**UX cost:** about 20 s once per added phone.

**Decision D12 (laptop approvals):** WebAuthn and passkeys are not accepted for gated actions. The dashboard's JavaScript is served by the server, which can register its own credential or show different wording, and WebAuthn has no trusted display. The dashboard can still decline. A signed native desktop companion is deferred (decided by owner, 2026-10-05). Revisit it if Phase 0 shows owners are often away from all their phones; it would enroll with the D11 ceremony and show the phone-signed wording.

### 3.2 Migration and old apps

- **Capability check:** the server branches on `"gatekeeper" in capabilities`.
- **Old apps** keep today's behavior, and the dashboard says "Phone protection: off, update the app".
- **Version floor:** an honest operator can set `min_app_version`.

## 4. Policy distribution and integrity

| Layer | Source | Integrity |
|---|---|---|
| Built-in | JSON in the APK | APK signature; Android blocks downgrades without an uninstall, and an uninstall wipes the keys |
| Enterprise | MDM managed config (`RestrictionsManager`, key `gatekeeper_policy`) | Only the device or profile owner can write it. EMMs usually push it only to apps distributed through managed Google Play (a private app is fine), so plain sideloaded installs may not receive it |
| Owner | App-private file, AES-encrypted with a Keystore key | Signed by the auth-bound policy key and verified on load, so a change needs the screen lock by construction |

**Decision D13:** Precedence is enterprise > owner > built-in. Hard denies are immutable, and enterprise can lock the owner layer and cap modes (D18). A bad owner signature means the built-ins apply (fail closed), and the owner is notified.

**Decision D14:** There is no server-to-phone policy write. Console toggles send a `policy_proposal`, which applies only after the owner passes the screen lock (rate-limited, §2.7).

Owner policies carry a monotonic `version`. Replaying an older signed file needs root, which is out of scope. The remaining-time file (D22) isn't signed on each update, so its integrity rests on app-private storage, the same root-only assumption as policy rollback.

## 5. Coverage gaps

| Gap | Mitigation | Residual |
|---|---|---|
| Games, canvas | Unlabeled and payment-context rules (Play billing sheet words) | Icon-only spends of in-game currency |
| Gestures | Swipe-as-tap, start and end points, sliders | Custom gesture views with no node |
| Keyboard | Field rules, ENTER rules, scheme refusal | The text of an allowed draft is server-chosen |
| Deep links | Resolve, then check scheme and target (§2.4) | A deep link to a non-payment confirm screen; the tap on it is still checked |
| Autofill, credentials | Always ask (§2.4) | None known |
| Share sheets | Chooser targets ask | "Copy" inside apps |
| Notification actions | Action buttons ask; the opened app is then checked | Quick settings toggles (Phase 2: system category asks) |
| Clipboard | **New work:** `PhoneActions.paste()` today overwrites the clip and never restores it, so typed text (OTPs included) stays there. It will restore the previous clip and clear its own | Paste-then-screenshot reads the clipboard within a session |
| Messaging in an uncovered language | Phase 1 ships an English-only lexicon, and the language fallback is limited to money categories, so a Send in, say, a Polish WhatsApp is caught only by its resource id (`send`) or description. D15 is measured per locale; more lexicons come in Phase 3 | Icon-only Send buttons with obfuscated ids in uncovered languages |
| Opening blocked apps | **Not stopped:** a blocked app can be opened via the launcher, recents (`APP_SWITCH`), a share target or an app link. Every act inside it is refused, and reads are blacked out while it is visible in any window | The app is visible on the owner's screen |
| Reading during sessions | Black or `[]` for blocked apps; FLAG_SECURE respected | Non-blocked screens, notification-shade OTPs (Phase 4: redact the shade) |

**What A1 can still do:**
- Inside a session: non-risky actions in non-blocked apps, and the one exempted non-money action per spoken instruction (D6).
- At any time: show a misleading "Server says" line, send prompts up to the limits, and refuse service.
- Until sessions ship in Phase 2: read screens and act without asking on non-risky targets at any time.

## 6. Friction budget

The product guidance is that the gate must not be so tight that it defeats the purpose. Where a security rule and friction conflict, this design keeps the security property and picks the lowest-friction way to enforce it:
- **D4:** ask once, at the commit point.
- **D20:** "Always allow this", so each kind of prompt appears once per app.
- **D2:** a generous Settings allowlist.
- **B5:** the language fallback applies only in money categories.

| Use (default `auto`) | Today | Gatekeeper, first week | After "Always allow" |
|---|---|---|---|
| Browse, search, read, navigate, change Wi-Fi or dark mode | 0 | 0 | 0 |
| "Buy X" (requested) | 0 | **1**, at checkout | 1 (or 0 with a per-app `pay` grant) |
| "Send hi to Mom" | 0 to 1 | 0 (one-shot requested) | 0 |
| Unrequested send, post or delete | 1 | 1 | 0 for that app and kind |
| Shopping app, icon-only or non-English UI | 0 | 1 to 2 | Fewer, via per-app grants |
| Remote task | 0 | 1 session prompt per 30 min (Phase 2) | 0 with a standing grant |
| Developer test phone (`never`) | 0 | 0 | 0 |

**Decision D15 (measured budget; a Phase 1 go/no-go with equal weight to security):** measured in Phase 0 shadow mode, then in a Phase 1 beta.
- **Security no-go:** any missed money commit or send in the labeled set.
- **Friction go:** **≤ 1 prompt per typical task at the median** (p90 ≤ 2).
- **Friction go:** **fewer than 20% of prompts approved in under 2 s**. A reflexive approval means the prompt was unnecessary, and it trains owners to approve without reading.
- **Friction go:** the share of Asks that owners label "harmless" is at most 30%.

Failing a friction criterion means tuning the word classes, the categories or the allowlist before launch. It is not a reason to relax a hard deny.

**Decision D21 (audience defaults):**

| Audience | Default |
|---|---|
| Personal phone | `auto`, ceiling `auto` |
| Developer **test phone** (chosen at pairing, confirmed with the screen lock) | `never`, renewable up to 30 days on the monotonic clock, with the phone's own persistent banner. Hard denies, blocked apps and the refusal of money commits still apply, unless the owner opts into **"Don't ask + allow purchases"** for that window: a separate option, off by default, signed on the phone with the explicit wording "…including purchases for 1 hour?" and a second confirmation, expiring with the window and shown in the banner ("…including purchases"). MDM caps and blocked apps still win (decided by owner, 2026-10-05) |
| Enterprise | Whatever MDM sets (D18), typically `auto` with `never` and `pay` forbidden |

## 7. Performance

- **Hit test:** about 20–60 binder reads at 0.1–0.3 ms, so 2–15 ms.
- **Rules:** microseconds.
- **`reverify`:** 1–5 ms.
- **Budget:** p95 ≤ 20 ms per act call, against a ~50 ms stroke and a 300 ms `settle_quiet_ms`.
- **Fast path:** `wait_idle` and `device_state` bypass the gate, and reads cost a session lookup (microseconds).
- **Reuse:** if `lastChangeAt` hasn't moved since the last `ui_elements`, that walk is reused.
- **Measurement:** `gate.ms` is fed into `Agent.timer`.

## 8. Testing

- **Policy engine:** a pure JVM function, `decide(call, ScreenFacts, Policy, Session)`. Table tests cover every §2.4 row, the language fallback, scheme handling, precedence, fail-closed signatures, and parity with `protocol.py` `METHODS`.
- **Instrumented tests:** a fixture app (Buy now, icon-only cart, WebView checkout with a card form and ENTER submit, slide-to-pay, chat ENTER, OTP field, a button moved 50 ms after the check).
- **Settings fixtures:** captured accessibility trees from AOSP, Pixel, Samsung One UI and Xiaomi settings, including wireless debugging, notification access and language.
- **Ceremony tests:** a server that substitutes keys; an attempt to grind the code (one guess, then rate-limited).
- **Adversarial suite (`tests/adversarial/evil_server.py`):** a fake server speaking protocol v1 to the real app on an emulator, building on `test_phone_security.py` T5–T9 and `test_approval_security.py`. Every attack above, plus:
  - zero-length swipe and 50 ms long-press on "Buy now";
  - `javascript:` typed into the URL bar, then ENTER;
  - a tap on an autofill chip;
  - a share-sheet target;
  - a tap on the Approve button or the BiometricPrompt;
  - a system-language switch;
  - prompt and proposal floods;
  - an `approve:true` API loop;
  - a replayed `gate_event`;
  - reads while a blocked app is in split screen.

## 9. Rollout

| Phase | Content |
|---|---|
| 0: shadow (1–2 weeks) | The gate decides and logs without enforcing; counts measure D15 |
| **1: first slice** | D1 checkpoint and fail-closed methods; hard denies and the **three Settings tiers** with OEM and GMS fixtures (D2); blocked apps at launch, URL and act time; the **`open_url` scheme allowlist with resolved targets**; tap rules for tap, long press and swipe-as-tap; ENTER and URL-bar rules; password, OTP, card and `javascript:` rules; **autofill and credential** asks; share sheets; **English lexicon, fail-closed for other languages** in sensitive categories; commit-point asks with purchase-flow permits (D4); **"Always allow this"** grants (D20); mode ceiling enforcement with `ask`, `auto`, `trusted` and test-phone `never` (D16, D17, D21); signed permits (D5); TOCTOU (D8); **prompt rate limits**; **acting-phone-only approval** (D10); refusal codes and the API state machine; clipboard restore; adversarial suite |
| 2 | Sessions (read and act) with a phone-driven indicator; standing grants; the `plan` mode (D19); `policy_proposal`; quick-settings asks |
| 3 | More languages; cross-device trust (D11) |
| 4 | MDM floors; notification-shade redaction |

**Gaps that remain after Phase 1:**
- No sessions yet, so A1 can read screens and do non-risky actions at any time.
- Remote risky actions prompt until the owner chooses "Always allow" for that app and kind.
- Non-English users in sensitive apps see more prompts.
- Owners must approve on the acting phone.
- Quick-settings toggles and the notification shade are unprotected.

## 10. Open questions and alternatives

**Open questions:**
1. **Reads need a session?** Decided by owner, 2026-10-05: yes. Agent reads need a session, with a phone-driven indicator (the reviewer's default stands); the owner's live view is implicit under the OS capture consent.
2. **Remote "buy X" prompts by default?** Decided by owner, 2026-10-05: purchases always ask (and under `never` are refused) unless the owner opts into **"Don't ask + allow purchases"** for a session: a separate, explicit option when enabling `never`, signed on that phone with its screen lock (with explicit "including purchases" wording and a second confirmation for the money part), and ending with that `never` window. MDM caps and blocked apps still win, and an API key's `max_mode` still caps its tasks. See the updated D21 and N3; the server implements it as `allow_money` / `never_money` (docs/safety.md, "Don't ask").
3. **Desktop companion?** Decided by owner, 2026-10-05: deferred (D12). Approvals stay on the owner's phones; the dashboard can only decline.
4. Should enterprise policy be signed for fleets without MDM?
5. Is the 60% lexicon threshold right? Phase 0 decides.
6. Is the Settings allowlist generous enough, and is it maintainable across OEMs? Phase 0 counts how often the unrecognized-page tier asks.
7. ~~Purchase-flow bound~~: resolved in review 2 (one terminal follow-up, same app and task, 3 min; D4).

**Alternatives considered:**
- **Remote-attested server:** attests the code, not the model's judgment, and breaks self-hosting.
- **End-to-end signed commands:** don't stop prompt injection, and key distribution runs through the server.
- **Device-owner mode** (`setPackagesSuspended`): needs a factory reset; an enterprise option only.
- **Server-issued action tokens:** superseded by D5 and D9.
- **On-device small-model judge:** future; may add asks, never allow.
- **For cross-device trust:**
  - a shared per-account key (Keystore keys can't be exported);
  - a key-transparency log (trusts its operator unless phones gossip);
  - server-attested key lists (circular).

## Changes since review 1

| Item | Handling |
|---|---|
| B1 | Accepted. QR with fingerprint plus MAC key is the default; the code fallback is commit-then-reveal, derived from the commitment, both nonces and both keys; attempts are rate-limited (D11) |
| B2 | Accepted. "Requested" is one-shot, bound to the app, non-money only (D6). Money always asks, but only **once per purchase, at the commit point**, through a bounded purchase-flow permit (D4; the trade-off is stated there) |
| B3 | Accepted. Swipe-as-tap, start and end points; long press always judged; ENTER in browser and WebView forms and in payment context; URL-bar ENTER runs `open_url` checks; dangerous schemes refused in `type_text` (§2.4) |
| B4 | Accepted, revised for friction. Security-critical pages are refused, a generous everyday allowlist never asks, and unrecognized pages ask, so the property still fails closed (D2). OEM fixtures added |
| B5 | Accepted, with two refinements: coverage is judged per screen, not per label, so product names don't trigger it; and the fallback applies only in money categories and payment context, never globally |
| B6 | Accepted. Autofill, inline and credential surfaces always ask; card fields count as payment context |
| B7 | Accepted. Phase 1 widened, its remaining gaps listed, acting-phone-only approval until D11 (D10) |
| S1 | Accepted (D5) |
| S2 | Accepted (D11) |
| S3 | Accepted (§2.4) |
| S4 | Accepted (§2.1, §5) |
| S5 | Accepted. Marked as new work |
| S6 | Accepted, further: permission dialogs for the task's own app don't ask; other apps' dialogs ask (D2) |
| S7 | Accepted. Hard denies on OpenMob's windows, BiometricPrompt and keyguard; `gate_prompt_open` |
| S8 | Accepted (§4, D13) |
| S9 | Accepted (§2.7) |
| S10 | Accepted. API state machine (§2.8) |
| S11 | Accepted (D11) |
| S12 | Accepted, merged into the friction budget (D15) |
| Permission modes (owner request) | New §2.10, D16–D19: the phone owns the mode and its ceiling; tightening is accepted from anywhere, loosening needs the screen lock; signed storage; `plan` binds to plan permits; `never` turns would-ask cases into refusals, and hard denies hold in every mode; MDM caps. D6 is now `auto`'s rule, D7 is replaced by `trusted`, and D6 also accepts a remote instruction the owner approved in the session prompt |
| Friction guidance (owner) | New §6 "Friction budget". Changes: D4 asks only at commit points, once per purchase; D20 adds "Always allow this" on every prompt, a signed `trusted` grant per app and kind (pay per app only); D2 adds a generous Settings allowlist; B5 is scoped to money categories; D15 sets the measured budget (median ≤ 1 prompt per task, < 20% of approvals in under 2 s) as a Phase 1 go/no-go; D21 sets audience defaults (test phones `never` with a banner, personal `auto`, enterprise via MDM). Phase 1 now includes "Always allow" and mode enforcement |

## Changes since review 2

| Item | Handling |
|---|---|
| N1 date and time | Every expiry is checked on the monotonic clock with a persisted high-water mark, and time remaining is carried across reboots, failing closed (D22). Manual date and time also moved to the ask tier |
| N1 scope of the allowlist | Stated: it only exempts pages from the Settings tier and never overrides §2.4 commit and delete rules (D2) |
| N1 app info | Only Force stop and Clear cache are on the allowlist. Clear storage, Disable and Uninstall ask. Any app-info action on a blocked app is refused |
| N1 keyboards | Enabling a keyboard is security-critical; switching among already-enabled keyboards stays on the allowlist |
| N2 | "Always allow" is bound per kind: not offered for autofill in browsers or WebViews; for native apps, bound to the filled package; settings grants bound to the exact page fingerprint; share grants bound to (source, target) (D20) |
| N3 | Under `never`, money commits are refused unless the owner opted into "Don't ask + allow purchases" for that `never` window, signed on the phone and time-limited (§2.10, D21; decided by owner, 2026-10-05). A per-app `pay` grant doesn't count under `never`. No new prompts |
| Q7 | Accepted: one terminal follow-up tap, minted only by a flow-starting commit, same app and task, 3 min on the monotonic clock; flow-starting words always ask again (D4) |
| S1 | Accepted: listed as a gap in §5, with D15 measured per locale |
| S2 | Accepted: the phone decides "the session's app" from the dialog's requesting package (D2) |
| S3 | Accepted: gatekeeper phones ignore `approval_request` (D9) |
| S4 | Accepted: grant wording states what it allows, the checkbox is off by default, `pay` needs a second confirm (D20); D4, D6 and `plan` state what isn't bound |
| S5 | Accepted: pairing a new Bluetooth device asks; connecting to an already-paired device stays on the allowlist |

## Phase 1 as built

Code: `android/app/src/main/java/dev/openmob/companion/gate/` (engine `Gatekeeper.kt`, screen model `GateModel.kt`,
Android glue `GateScreens.kt` and `GateHost.kt`, permission changes `ModeChange.kt`), the rule file
`android/app/src/main/resources/gatekeeper/rules.json`, and on the server `phone/remote.py` (`GateRefused`, the
re-send), `devices.py` (`gate_event`, `device_lock`, D10), `agent/loop.py` and `platform/service.py`
(`approval_on_phone`). Protocol: docs/phone-protocol.md §9.

**Testing approach.** One JSON rule file, read by the Kotlin engine and by a Python reference engine
(`tests/gatekeeper_ref.py`); shared fixtures (`tests/fixtures/gatekeeper/`: screens, scenarios, attacks) run in
both JUnit (`GateScenarioTest`) and pytest (`test_gatekeeper_conformance.py`), and a differential fuzz fixture
(`fuzz.json`: 400 random screens and calls with the reference engine's answers) runs through the Kotlin engine.
That catches drift on the fixtures and the fuzzed space; it can't prove the two agree everywhere. The
adversarial suite (`test_gatekeeper_adversarial.py`) puts a fake compromised server in front of a simulated
gatekeeper phone running the reference engine over raw protocol messages; every attack is also a fixture the
Kotlin engine runs. The pure Android pieces (tree reading, live-view gating, keep-awake) have JVM tests. §8's
emulator suite (`tests/adversarial/evil_server.py`, stand-in apps in `tests/adversarial/standins`,
docs/testing.md) passed 61 of 61 checks on Android 15 after four fixes (release gate B7, results in
docs/research/gatekeeper-b7-results.md). It found the launch race below (a tap judged on a window list that
hadn't caught up landed in a starting blocked app) and that token-paired phones couldn't approve. On Android
14+ the permission controller (runtime dialogs, Safety Center, default apps) is hidden from OpenMob's service,
so those taps fail closed and the permission-dialog rules only apply on older or OEM builds. There are no
captured OEM Settings dumps yet (the Settings fixtures are hand-built trees).

**Where it differs from the text above:**
- **Settings, unrecognized pages:** only *changing* taps ask (a switch or checkbox, or a change word such as
  Turn on, Allow, Reset); plain navigation into a sub-page doesn't. Pages are recognized by window title, the
  headers at the top that aren't rows, and the activity class (accessibility doesn't expose fragment classes).
- **Resource ids** are matched as whole words of the id (`sendButton` → send, but not `sender_name`), and
  `checkout` in an id counts as pre-commit like the word (D4).
- **Delete** asks wherever it appears, not only in a confirmation dialog.
- **Requested (D6):** only instructions typed or spoken in the app count; each credits one send, post or
  delete for 30 minutes, not bound to an app (sessions are Phase 2).
- **Purchase follow-up** is bound to the call's `task` param, which the server asserts (the app and the
  3-minute limit are the phone's).
- **`plan` is enforced as `auto`** on the phone, and the server skips its plan approval on gatekeeper phones
  (plan permits are Phase 2).
- **Policy changes:** Phase 1 has no `policy_proposal`. The server's `mode_change` approval request plays that
  role: the phone words it from the signed change (the server's question is only "Server says"), and the one
  screen-lock signature (the server's approval format over the change's digest) is kept as the record's proof
  and re-verified on load. Trusted *category* entries aren't enforced as allows (the phone keeps asking);
  trusted app entries use the D20 default durations rather than the server's expiry; `never` is capped at
  24 hours. The 30-day test-phone `never` (D21) and MDM (D18) aren't implemented.
- **Keys:** the auth-bound approval key also signs policy records (no separate policy key); the policy file is
  app-private and every record's signature is checked on load, but the file isn't separately encrypted.
- **Crop:** its hash is in the signed message; the permit is bound to method, params and target fingerprint,
  since a re-sent call can't reproduce the crop.
- **Reverify:** for risky or payment-context decisions it needs the same fingerprint and a quiet screen
  (waiting up to 1 s); for other allowed actions it re-judges the current target and refuses only if that is
  now risky or refused (fewer spurious refusals from animations). Before that, every admitted tap, long press
  and swipe waits (within the same second) for the window list to settle: 250 ms with no change, and some
  listed window having focus (an app's launch screen isn't listed at all). If nothing has focus by then it's
  refused, `gate_screen_unstable` (B7).
- **`never` and the always-ask class** also covers typing into OTP and card fields, and an unlabeled tap in
  payment context counts as money.
- **Denials that suspend** are the owner's denials of prompts (not the gate's refusals); a notification offers
  Resume.
- **New:** code `phone_locked` (act calls while a secure lock is up, checked before the keyguard hard deny) and
  the `device_lock` message; the app keeps the screen on only while working (owner report).
- **Review round 1 additions:** a window whose tree can't be read refuses taps and blacks out reads; nodes
  under the touched point are always read in full, and a branch that still can't be read asks; any other
  admitted action voids outstanding permits; the purchase follow-up starts only when the approved tap runs and
  ends at cart/checkout words, BACK/HOME/recents, launches, links or after 3 other actions; fill grants bind
  to the focused field's app (never a browser or WebView field); the live view drops every frame while a
  protected app is visible (250 ms hold-back, restart from a keyframe); keep-awake holds only after admitted
  actions and for at most 10 minutes after the owner last used the phone; a screen-off mid-task pauses rather
  than unlocking; Build number and the VPN consent dialog ask; permission-change prompts are limited to 2 an
  hour; gate prompt ids start with `gk` and the legacy path refuses them.
- **Permission dialogs (owner request, after review round 1):** the session app's runtime permission dialogs
  follow the owner's settings rather than the D2 "never asks" for the session's app: per type `ask` (default)
  | `once` | `while_using` | `deny`, location accuracy `approximate` (default) | `precise`, notifications `deny`
  (default) | `allow`, kept as signed `permission_default` records (tightening unsigned), with per-app
  overrides as signed `permission` grants from the prompt's "Always allow for <app>". "Allow all the time"
  always asks; denying and Approximate never do.
- **After the B7 emulator run:** Android 14+ hides permission dialogs, Safety Center and default-apps screens
  from accessibility. The phone detects such a dialog (an application window with no readable tree, plus the
  window-state event's package or the window's own package) and refuses acts with `needs_owner_dialog`; the
  task pauses until the owner answers it (`owner_dialog`), so the per-permission defaults apply only on Android
  11–13. A tap where no window is under the point is refused (`builtin:no_window`), and every act is refused
  with `gate_unavailable` while the accessibility service is unbound. Not done: checking the screenshot for a
  dialog (the detection uses windows and events only).
- **Live screens (real-device report, Kalshi):** target identity and fingerprints blank numbers; D5 stillness
  is target-scoped (the target's normalized fingerprint unchanged over 150 ms) and the launch-race wait counts
  window changes only; transient unreadable windows are re-read for up to 0.6 s before refusing; money-commit
  permits bind the currency amounts (button plus total/cost/price/amount/payout lines) and any change re-asks;
  only prompts actually shown count toward the 6-per-10-minutes limit, and re-asking the same action reuses
  the open prompt; the language rule uses script and lower-case words, not dictionary misses on tickers;
  "Submit" is a terminal money word in money apps (now including prediction markets and trading apps). A
  Compose text field missing from the walked tree is resolved from findFocus.
- **Decision D23 (task approval, by owner request, 2026-10-06):** at task start the phone asks once, worded
  from the task's request text with its source on the "Server says" line, and signs a task grant bound to the task
  id and the request text's digest (30 minutes on the monotonic clock, at most 25 risky steps, in-memory, cleared
  by Stop). Under it, non-money risky steps of that task run without prompts; money commits (unless "Also allow
  purchases"), browser/WebView autofill and permission dialogs still ask; hard denies still refuse. Requests
  typed on the phone are granted without a prompt. Not offered in `ask` or `never`; `plan` uses it. Trade-off:
  inside an approved task a compromised server chooses the steps (recipients, texts) within the approved
  request's wording, which the phone can't check; the owner accepted this for friction. "Subscribe"/"Join" are
  money only with a price nearby or a Play billing sheet.
- **Decision D24 (consequence-based risk, by owner request, 2026-10-06):** risk is judged by content (public link
  vs private content), recipient (self vs others, unknown = others), visibility (private vs public),
  reversibility (toggles vs permanent) and money, instead of keywords, in both engines and the server's `_guard`.
  A link shared or sent to yourself, copying, like/subscribe/save are no prompt; Subscribe/Join are money only
  with a price nearby. Self is recognized by "Note to self"-style labels, "(You)", and the owner's names the
  phone knows (the paired account email); no contacts profile is read. The server's classifier no longer hints
  the phone on its own.
- **Decision D25 (three confirmation levels, owner principle, 2026-10-06):** none / tap / fingerprint. Tap-level
  asks are OpenMob's own Allow/Deny with no screen lock, signed by a separate non-auth Keystore key in a
  separate domain (`gk1|tap|…`); the security basis is that agents can't tap OpenMob's windows and Allow filters
  obscured touches. Fingerprint: money, browser/web autofill, OTP/card fields, permission grants. "Don't confirm
  everyday actions" (signed, loosening via the phone) skips tap-level asks and the task prompt; the task
  approval itself is tap-level. This relaxes D5's "screen lock for every gated action" for tap-level actions:
  a malicious app on the phone (out of scope) or an accessibility overlay that defeats
  filterTouchesWhenObscured could approve tap-level asks.
- **Not yet:** the local 7-day Activity log and the "Always allowed" list UI (the engine can list and revoke),
  the dashboard's "Phone protection" indicator, `min_app_version`, `gate.ms` in the agent's timings, and
  session/proposal prompt limits (Phase 2).
