# OpenMob Phone Protocol v1

This is the spec for the **OpenMob Android companion app**. The app connects to an OpenMob server, which runs the AI agent, and carries out the server's commands on the phone: screenshots, UI tree, taps, typing, app launches and so on. You can implement it from this document alone.

Source of truth in the repo: `openmob/phone/protocol.py` (message types, `METHODS` table). The adb driver `openmob/phone/adb.py` is the reference behaviour for every method.

---

## 1. Overview

```
 Android app  ──── wss://<server>/phone/ws?t=<token> ────►  OpenMob server (agent)
              ◄── call {id, method, params}
              ──► result {id, ok, result|error}
              ◄── event {kind, message}          (agent activity: show / speak)
              ──► command {text}                 (user spoke/typed on the phone)
```

* The **phone connects out** to the server, so it works behind NAT and on mobile data. The server never connects to the phone.
* Transport: one WebSocket. Every frame is a **UTF-8 JSON text frame** holding exactly one object with a `"type"` field. Binary data (screenshots) is base64 inside JSON. The one exception: a `screenshot` call with `"binary": true` may be answered with a binary frame (§4.1).
* The server is the **RPC client**: it sends `call`s, and the phone replies with exactly one `result` per call.
* The phone is also the **user's microphone/keyboard**: it sends `command` messages and shows `event`s.

### Conventions

| Topic | Rule |
|---|---|
| Coordinates | Integers in **device pixels**, in the **current orientation**, origin **top-left** of the full display (status bar included). This is the space of `getBoundsInScreen()` and `dispatchGesture`. |
| Durations | Milliseconds (`*_ms`). |
| Package names | Android application IDs, e.g. `com.whatsapp`. |
| Unknown fields | Ignore unknown fields in any message, so the protocol can be extended. |
| Unknown message `type` | Ignore it and log it. |

---

## 2. Connection lifecycle

### 2.1 Connect

URL: `wss://<server>/phone/ws?t=<token>`

* `<server>` and `<token>` come from the user (pasted or scanned as a QR code from the server's console or dashboard). The token is the same one that protects the dashboard (`?t=` in the dashboard URL).
* Accounts mode: no `?t=`. The phone sends its own device key in `hello` (`key`), obtained by pairing (§2.6).
* Use **`wss://` only** in release builds (see §6). Allow `ws://` only in debug builds, for LAN testing.

### 2.2 Handshake

1. The socket opens. The phone **must** send `hello` as its first message.
2. The server replies with `welcome`. Do not answer calls or send commands before `welcome` arrives (in practice the server sends no calls before then).
3. From then on, calls, results, events and commands flow in both directions, in any order.

`device.id` must be **stable across reconnects and app restarts**: generate a random UUID on first launch and persist it. The server uses it to reattach a reconnecting phone to its existing session. `session` in `welcome` echoes that id.

### 2.3 Concurrency

* The server may have **several calls outstanding at once**. For example, a background `app_labels` call (up to 300 s) can run while the agent keeps sending `screenshot`, `tap` and so on.
* Handle calls concurrently, or at least don't let a slow call (`app_labels`, `launchable_packages`) block quick ones. Gestures and input may run on a single serial queue.
* **Results may be sent in any order.** The server matches them by `id`.
* Serialize your **writes** to the socket (one frame at a time).
* If a call exceeds its timeout (§4), the server gives up and reports an error to the agent. A late `result` is ignored, so it does no harm to send it.

### 2.4 Disconnect and reconnect

| Situation | App behaviour |
|---|---|
| Close code **4401** | Bad or missing token. **Do not retry.** Show "Invalid token", stop the service, and ask the user to re-pair. |
| Close code **4400** | The first message wasn't a valid `hello`, or `protocol` isn't `1`. The server waits 10 s for `hello`. Fix the client; don't retry in a loop. |
| Close code **4409** | Another connection with this phone's id and key replaced this one: the server keeps **exactly one live socket per phone**, and whatever an older socket sends is ignored. If this phone didn't just reconnect itself, someone else has its key: tell the owner to unpair it from the dashboard and pair it again. Don't reconnect in a loop. |
| Any other close, network error or failed connect | Reconnect with **exponential backoff**: 1 s, 2 s, 4 s, 8 s, 16 s, 30 s, then 30 s for each further attempt (cap 30 s). Add ±20 % jitter. Reset to 1 s after a successful `welcome`. |
| Network changes (Wi-Fi to cellular) | Reconnect right away. `ConnectivityManager.NetworkCallback` is a reasonable trigger. |
| User taps the **kill switch** (§6) | Close with code 1000, cancel any in-flight gestures, **do not reconnect**. |

When the phone disconnects, the server fails all pending calls. After a reconnect (new `hello` with the same `device.id`), the server attaches the new socket to the same device session. Calls are **never** replayed, so the phone keeps no call state across connections.

Keepalive: no application-level ping is defined. Use WebSocket ping frames from your client library (for example OkHttp `pingInterval(20, SECONDS)`), so that dead connections are detected and NAT mappings stay open.

### 2.5 Foreground service

While connected, run the socket inside a **foreground service** with a **persistent notification** ("OpenMob agent connected", with a **Disconnect** action). Without one, Android will kill the connection in the background. On Android 14+ a foreground service type must be declared (see §5.3).

---

### 2.6 Pairing and unpairing (accounts mode)

Pairing turns a one-time code into this phone's own device key. The phone never gets a dashboard session.

1. The code arrives in a link, `openmob://connect?server=<url-encoded server>&code=<code>`: from the dashboard's QR code (through the server's `/pair` page) or from `openmob phone add` over USB (`am start`). A code is an 8-character id followed by a 128-bit secret; it works once, for 2 minutes, for one account.
2. `POST /api/pair/check` `{"code"}` → `{"email", "expires", "server"}`: which account the code pairs with. It doesn't use the code up.
3. The app asks its owner once, with `BiometricPrompt` (`BIOMETRIC_STRONG | DEVICE_CREDENTIAL`): title "Connect this phone to *\<host\>*?", subtitle "as *\<email\>*". Nothing changes before this.
4. `POST /api/pair/claim` `{"code", "device_id", "model", "approval_key"}` (no session, no cookie) → `{"key", "server", "email", "device_id"}`. `approval_key` is the phone's screen-lock-bound public key (§3.3). Any socket using an older key for this `device_id` is closed (`4401`).
5. The app stores `key` encrypted with a Keystore key and connects with it.

Errors: `400` for a wrong, used, expired or voided code (with `detail`), `429` after too many wrong codes from one address or for one account (`Retry-After`).

`POST /api/pair/redeem` (the same body, plus an `X-OpenMob-Session` header of the code's own account) remains for older apps and `openmob bridge --email`.

**Unpair:** `POST /api/phone/unpair` `{"device_id"}` with header `X-OpenMob-Device-Key: <key>` → `{"ok": true}`, or `401` for a wrong or revoked key. The server revokes the key and closes the phone's sockets.

**Live view offer:** when someone opens the live view of a phone that didn't allow screen sharing, the server sends once per connection `{"type": "event", "kind": "live_view", "message": …}`. The app may then offer screen sharing (it never asks at setup or connect); older apps show it as an activity line.

## 3. Messages

### 3.1 Phone to server

**`hello`**: the first message after the socket opens.
```json
{"type": "hello", "protocol": 1,
 "device": {"id": "6f1c2a9e-3b0d-4c47-9a51-2f0e8d7c1b44", "model": "Pixel 8",
            "manufacturer": "Google", "android": "14"}}
```
| Field | Type | Source |
|---|---|---|
| `protocol` | int | Always `1` for this spec. |
| `device.id` | string | Persisted random UUID (§2.2). |
| `device.model` | string | `Build.MODEL` |
| `device.manufacturer` | string | `Build.MANUFACTURER` |
| `device.android` | string | `Build.VERSION.RELEASE` |
| `device.capabilities` | string[], optional | Optional methods this app answers, e.g. `["wait_idle", "resolve_url", "wait_for", "open_settings"]`. The server only calls an optional method the hello lists, so older apps (which send none) keep working. `"gatekeeper"`: the app enforces policy itself (§9); `"device_lock"`: it pushes `device_lock` messages. |
| `device.policy_hash` | string, optional | Gatekeeper apps: a short hash of the built-in rules and the owner's signed policy this phone enforces. |

**`result`**: exactly one per `call`, with the same `id`.
```json
{"type": "result", "id": "9b2f0c3e51a84d7e8f6a1b2c3d4e5f60", "ok": true, "result": null}
```
```json
{"type": "result", "id": "9b2f0c3e51a84d7e8f6a1b2c3d4e5f60", "ok": false,
 "error": "Key VOLUME_UP is not supported by the OpenMob app"}
```
`error` is a short message in plain English. It is shown to the AI model, which uses it to pick another approach, so say what failed and why. The server only looks at `ok`, `result` and `error`.

**`command`**: the user spoke or typed a request on the phone.
```json
{"type": "command", "text": "open youtube and play lofi music"}
```
The server handles the text like this:
* If the agent is waiting on a `question`, the text **answers** it (for example `"yes"`).
* `stop`, `cancel`, `abort`, `never mind` or `stop it` **stops** the current task and clears the queue.
* Anything else is **queued** as a new task. Tasks run one at a time.

Do speech-to-text on the phone (`SpeechRecognizer` or similar) and send only text. Empty text is ignored.

### 3.2 Server to phone

**`welcome`**: the reply to `hello`.
```json
{"type": "welcome", "session": "6f1c2a9e-3b0d-4c47-9a51-2f0e8d7c1b44"}
```

**`call`**: run a method (§4) and reply with a `result`.
```json
{"type": "call", "id": "9b2f0c3e51a84d7e8f6a1b2c3d4e5f60", "method": "tap", "params": {"x": 540, "y": 1210}}
```
`id` is an opaque string. Echo it back exactly. For an unknown `method`, reply `ok:false`, `"error": "Unknown method <name>"`.

**`event`**: agent activity, fire-and-forget. Never reply to it.
```json
{"type": "event", "kind": "question", "message": "I'm about to tap 'Send'. Should I go ahead?"}
```
| `kind` | Meaning | Suggested UI |
|---|---|---|
| `heard` | A command the server received, as `[source] text` | Log line |
| `status` | Progress ("Queued…", "Stopping…", "Loading screen…") | Status line |
| `thought` | The model's reasoning text | Log (optional) |
| `action` | An action taken (`tap: Tapped 'Search' at (…)`) | Log line |
| `jev` | Screen-assessment summary | Log (debug) |
| `question` | The agent needs an answer. Reply with a `command`. | **Speak it (TTS)**, show it prominently, open the mic |
| `result` | Task finished: a one-sentence summary | **Speak it**, show it |
| `error` | Something failed | Show it |

Treat `kind` as an open set: show unknown kinds as plain log lines.

### 3.3 Approvals and "who's using my phone" (optional)

Both are optional. An app that doesn't know them ignores them (unknown `type`s and `kind`s are skipped), and `PROTOCOL_VERSION` stays `1`. The server must then treat a missing approval as a **no**.

**`approval_request`** (server to phone): a risky action (a purchase, a send, a delete) waits for the person holding the phone. The calling AI model can never answer it.
```json
{"type": "approval_request", "id": "a1b2c3", "task_id": "t-42",
 "question": "Buy 'USB-C cable' for $12.99 on Amazon?", "requester": "Claude"}
```
| Field | Type | Meaning |
|---|---|---|
| `id` | string | Opaque. Echo it back in the `approval`. |
| `task_id` | string | The task that asked. Informational. |
| `question` | string | One plain sentence saying what will happen. Shown as is. |
| `requester` | string | Who is asking (the client or model name). Shown as "Asked by …". |

**`approval`** (phone to server): the person's answer, sent **at most once per `id`**.
```json
{"type": "approval", "id": "a1b2c3", "approved": true}
```

How the Android app handles it:
* It shows a high-priority (heads-up) notification, `Approve? <question>`, naming the requester, with **Deny** and **Review** actions. The question is hidden on the lock screen. Pending requests also appear at the top of the app's main screen, which is the only place they show when notifications are off.
* **Deny** sends `approved: false` straight away, with no authentication.
* **Review** opens a screen with the question and requester. **Approve** there requires the phone's screen lock (`BiometricPrompt`, strong biometric or device credential), and only then sends `approved: true`. The screen is `FLAG_SECURE`, so it never appears in screenshots or the live view.
* After **2 minutes** with no answer, the request expires: the notification is dismissed and **nothing is sent**. The server should time out on its own (about 2 minutes) and treat it as denied.
* A repeated `approval_request` with an `id` already pending, answered or expired is ignored. An answer is only sent while connected. If sending fails, the request stays pending so the person can try again. When the person disconnects, pending requests are dropped unanswered.

**Approval kinds** (newer servers). An `approval_request` may carry more fields; an app that doesn't
know them shows the `question` as before.

| Field | Meaning |
|---|---|
| `kind` | `action` (a risky tap, the default), `step` (Ask-every-step mode: the next change), `plan` (Plan-first mode: the numbered plan is in `question`, its steps in `plan`) or `mode_change` (agents would ask less on **this** phone; only sent to that phone) |
| `plan` | For `kind: "plan"`: the steps, as strings |
| `change` | For `kind: "mode_change"`: `{loosens, default_mode?, ceiling?, never?, allow_money?}`, informational. The `question` says it in words, e.g. "Allow agents to act without asking on this phone for 1 hour?" (with `allow_money`: "…on this phone including purchases for 1 hour?"). The app reads both from the signed `action` (its `input` is the question, then the change as JSON), shows that question, and when the change has `"allow_money": true` asks a second time ("Also allow purchases?") before the screen lock |
| `always` | Offered only on the phone that would act: `{app, kind, days, text, confirm_twice, target?}`. Shown as an **Always allow this** checkbox, off by default, labelled with `text` ("Agents could send any WhatsApp message as you for 30 days"). With `confirm_twice` (payments) the app asks a second time before the screen lock |

| `action` | What exactly will happen, as fields: `{tool, package, text, desc, resource_id, bounds, input}` (the target element's identity, any typed text, URL or key; for a plan or mode change, the plan or the change in `input`). The app shows it ("Exactly: Tap “Buy now” (id buy_now) in com.amazon…") |
| `digest` | The action's digest (see §8.1, "Approval signing"). The app recomputes it from `action` and refuses to sign when they differ |
| `signature_required` | `true`: the server only accepts signed approvals (accounts mode). An app without an approval key must not send an unsigned yes |
| `can_approve`, `cannot_approve_reason` | `can_approve: false`: this phone can only decline (it has no registered approval key yet), shown with the reason. The app shows Deny only |

Approving with **Always allow this** adds `"always": true` to the `approval`, and the signature
covers it: the signed text ends in `|always`. A `mode_change` is signed like any approval; its
`task_id` names the change (`mode_change:…`). Both loosen the phone's permissions, so the server
accepts them only with a valid signature by that phone's approval key (never from an app without one).

**`permissions`** (server to phone): the phone's permission mode, after the `welcome` and whenever it
changes.
```json
{"type": "permissions", "default_mode": "never", "ceiling": "never", "never_until": 1791223200, "never_money": false}
```
While `ceiling` is `never` and `never_until` (unix seconds) is in the future, the app shows a banner
in its connection notification ("Agents can act without asking until 21:00", or with `never_money`
"Agents can act without asking, including purchases, until 21:00") with a **Turn off** action, and
clears it at `never_until`.

**`permissions_off`** (phone to server): Turn off. `{"type": "permissions_off"}`. The server turns
Don't ask off (a `never` default becomes `auto`). It only tightens, so it needs no signature.

**`event` with `kind: "controller"`** (server to phone): who is driving the phone right now.
```json
{"type": "event", "kind": "controller", "message": "Claude"}
```
`message` is a short display name. The app then shows "Claude is using your phone" as the connection notification's title (its Disconnect action reads **Stop**, and still disconnects), on the main screen and on the Quick Settings tile. An empty `message` means nobody: the normal text comes back. The app also clears it when the connection drops, so send it again after a reconnect if someone is still driving. Send it when control starts and ends, not with every step.

---

## 4. Methods

Every method replies `{"ok": true, "result": <value>}` on success. A method whose result is `null` must still send `"result": null`. "Timeout" is how long the server waits. Aim to finish well inside it.

| Method | Params | Result | Timeout |
|---|---|---|---|
| `screenshot` | optional `max_side`, `quality: int`, `binary: bool` | Screenshot object (or a binary frame) | 30 s |
| `ui_elements` | none | array of elements (`[]` if unavailable) | 15 s |
| `current_app` | none | string or `null` | 10 s |
| `launchable_packages` | none | array of strings | 30 s |
| `app_labels` | `packages: string[]` | object `{package: string or null}` | 300 s |
| `launch_app` | `package: string` | `null` | 15 s |
| `tap` | `x, y: int` | `null` | 10 s |
| `long_press` | `x, y: int`, `duration_ms: int` | `null` | 15 s |
| `swipe` | `x1, y1, x2, y2: int`, `duration_ms: int` | `null` | 15 s |
| `type_text` | `text: string`, `replace?: bool` | `null` | 15 s |
| `key` | `name: string` | `null` | 10 s |
| `open_url` | `url: string` | `null` | 15 s |
| `wake_unlock` | none | `null` | 10 s |
| `device_state` | none | `{"interactive": bool, "locked": bool, "secure": bool}` | 5 s |
| `wait_idle` (optional) | `quiet_ms, max_ms: int` | `{"waited_ms": int, "idle": bool}` | 10 s |
| `wait_for` (optional) | `condition: object, timeout_ms: int` | `{"met": bool, "waited_ms": int, "seen": string, "package": string\|null}` | 15 s |
| `open_settings` (optional) | `page: string`, `package: string` (app pages only) | `{"package": string\|null}` | 15 s |

### 4.1 `screenshot`
Capture the current screen. Downscale it so the **long side ≤ 1280 px**, keeping the aspect ratio (scale = `min(1, 1280 / max(w, h))`, rounded). Never upscale. Encode as **JPEG at quality ~80**, then base64 (standard alphabet, no line breaks).
```json
{"jpeg": "/9j/4AAQSkZJRgABAQAAAQABAAD...", "width": 591, "height": 1280,
 "device_width": 1080, "device_height": 2340}
```
| Field | Meaning |
|---|---|
| `width`, `height` | Pixel size of the **encoded JPEG**. |
| `device_width`, `device_height` | Real screen size in device pixels, **current orientation**: the coordinate space of `tap` and the other gesture methods. |

The server maps model coordinates with `device_x = round(x * device_width / width)`, so both sizes must be exact and in the same orientation. Use the captured bitmap's own size for `device_*`.
Errors: if capture fails, reply with an error. The server retries once. If the app blocks capture (`FLAG_SECURE`), send an **all-black image** of the normal size rather than an error. The agent recognises black frames as "this app blocks screen capture".

**Live-view params (optional).** The agent's calls carry no params and always get the frame above. The dashboard's live view asks for frames sized for the viewer's panel in device pixels: for example `{"max_side": 1280, "quality": 72, "binary": true}` for a 2x display, or `{"max_side": 720, "quality": 55, "binary": true}` when the viewer didn't give its size:

| Param | Meaning |
|---|---|
| `max_side` | Use this long-side limit instead of 1280 (never more than 1280). |
| `quality` | JPEG quality 1-100 instead of ~80. |
| `binary` | You may answer with one **binary** WebSocket frame instead of a JSON `result`: a 4-byte big-endian length `N`, then `N` bytes of the UTF-8 JSON `result` message whose `result` object has every field except `jpeg`, then the raw JPEG bytes. |

```
00 00 00 6c  {"type":"result","id":"9b2f…","ok":true,"result":{"width":324,"height":720,"device_width":1080,"device_height":2400}}  FF D8 FF E0 …
```

All three are optional to implement: an app that ignores them and sends its usual full JSON frame still works (the live view is then just slower). Errors are always JSON `result`s. `PROTOCOL_VERSION` stays `1`, because nothing changes unless the server asks.

### 4.2 `ui_elements`
Flatten the accessibility tree of the current screen into a list. Walk the tree **depth-first, pre-order** (parent before children, children in index order, like `uiautomator dump`). Include a node only if it meets all of these:
* **at least one** of: non-blank `text`, non-blank content description, or `isClickable` / `isLongClickable` / `isCheckable` is true;
* its bounds are non-empty (`right > left` and `bottom > top`);
* it is visible to the user (`isVisibleToUser`). This is recommended, and matches uiautomator.

Stop after **150** elements. On any failure (no root, timeout), reply `ok:true` with `[]`. Never send an error. Games and canvas apps legitimately return `[]` or useless lists.
```json
[{"text": "Search", "desc": "", "resource_id": "search_box", "cls": "EditText",
  "clickable": true, "checked": null, "bounds": [42, 180, 1038, 300]},
 {"text": "", "desc": "Wi-Fi", "resource_id": "switch_widget", "cls": "Switch",
  "clickable": true, "checked": true, "bounds": [900, 420, 1040, 520]}]
```
| Field | Type | Value |
|---|---|---|
| `text` | string | `node.text`, trimmed; `""` if none |
| `desc` | string | `node.contentDescription`, trimmed; `""` if none |
| `resource_id` | string | `viewIdResourceName` **after the last `/`** (`com.app:id/search_box` becomes `search_box`); `""` if none |
| `cls` | string | `className` **after the last `.`** (`android.widget.EditText` becomes `EditText`) |
| `clickable` | bool | `isClickable OR isLongClickable OR isCheckable` |
| `checked` | bool or null | `isChecked` if `isCheckable`, else **`null`** |
| `bounds` | [int ×4] | `[left, top, right, bottom]` from `getBoundsInScreen`, device pixels |
| `password` | bool, optional | `true` when `isPassword`; omitted otherwise |
| `editable` | bool, optional | `true` when `isEditable`; omitted otherwise |
| `covered` | bool, optional | `true` when the centre of `bounds` lies inside a window stacked above the node's own window (a higher `AccessibilityWindowInfo.getLayer()`: a dialog, the keyboard, a system bar), so a tap there would land on that window; omitted otherwise |
| `focused` | bool, optional | `true` for the text field with input focus (`isFocused` on an editable or password node); omitted otherwise |
| `hint` | string, optional | A text field's `hintText` ("Search", "Card number"), trimmed, at most 80 characters; omitted when empty |

`password`, `editable`, `covered`, `focused` and `hint` are optional: servers that don't know them ignore them, and a missing field means `false` (or empty). The server uses `password` to refuse typing into password fields, refuses to `tap_element` a `covered` element (the model is told to deal with what covers it first), and uses `focused` and `hint` to tell where ENTER goes: a search box, a form, or a browser's address bar.

The server judges every tap by the element under the point, not by what the model calls it, so keep `bounds` accurate.

The array order defines the element indices the model uses (`tap_element [3]`), so keep it stable for a given screen. The server taps the centre of `bounds`.

### 4.3 `current_app`
The package name of the foreground app (the window the user is interacting with), or `null` if unknown.
```json
"com.google.android.youtube"
```
Report the app underneath, never OpenMob's own overlay or notification shade, where you can tell the difference. Never send an error: return `null`.

### 4.4 `launchable_packages`
Every package that has a launcher activity (`ACTION_MAIN` + `CATEGORY_LAUNCHER`), with no duplicates. Order doesn't matter (the server sorts).
```json
["com.android.chrome", "com.google.android.youtube", "com.whatsapp"]
```

### 4.5 `app_labels`
Params: `{"packages": ["com.whatsapp", "com.example.gone"]}`. Return the user-visible app name for each package, or `null` if it can't be read (for example the package is not installed). Include a key for every requested package.
```json
{"com.whatsapp": "WhatsApp", "com.example.gone": null}
```
The list can hold 200+ packages. Label lookup is local and fast on the phone. The 300 s timeout exists for the adb implementation.

### 4.6 `launch_app`
Params: `{"package": "com.whatsapp"}`. Start the app's launcher activity (bring it to the front if it is already running). Error if it is not installed or has no launcher activity: `"com.foo has no launchable activity"`.

### 4.7 `tap`
Params: `{"x": 540, "y": 1210}`. A single tap: a stroke of about 50–100 ms at one point. Return when the gesture **completes**. Error if it is cancelled or rejected (`"Gesture was cancelled"`). Coordinates outside the screen give an error.

### 4.8 `long_press`
Params: `{"x": 540, "y": 1210, "duration_ms": 800}`. Press and hold at one point for `duration_ms` (the server's default is 800). Same completion and error rules as `tap`.

### 4.9 `swipe`
Params: `{"x1": 540, "y1": 1755, "x2": 540, "y2": 585, "duration_ms": 400}`. One straight-line drag from (x1,y1) to (x2,y2) over `duration_ms` (the server's default is 300; scrolls use 400). The finger lifts at the end, with no fling compensation. The agent's "scroll down" is a swipe from 75 % to 25 % of the screen height at the horizontal centre.

### 4.10 `type_text`
Params: `{"text": "lofi hip hop", "replace"?: bool}`. Insert `text` into the **currently focused editable field** at the cursor, replacing any selected text. Do not wipe the field: adb `input text` types at the cursor, and the agent relies on that. With `"replace": true`, the field's whole text becomes `text` instead (select all, then paste, when `ACTION_SET_TEXT` is refused): the server asks for it in search and address boxes, which otherwise keep the last query. Older apps ignore `replace` and insert. When the field shows only its hint (`isShowingHintText`, API 26+), treat it as empty. Any Unicode text must work. Do not press Enter: the server sends `key ENTER` separately when it wants to submit. Error if no editable node has focus: `"No focused text field. Tap the field first."`
Error if the focused field is a password field (`isPassword`), whatever the server asked: `"I don't type passwords. Please type it yourself."`

### 4.11 `key`
Params: `{"name": "BACK"}`. Names are uppercase and come from `adb.KEYCODES`:

`HOME`, `BACK`, `VOLUME_UP`, `VOLUME_DOWN`, `POWER`, `TAB`, `ENTER`, `DELETE`, `MENU`, `SEARCH`, `ESCAPE`, `APP_SWITCH`, `WAKEUP`

See §5.1 for how each one maps. For a key you can't perform, reply with an error naming the key (`"Key MENU is not supported by the OpenMob app"`). For a name not in the list: `"Unknown key FOO"`.

### 4.12 `open_url`
Params: `{"url": "https://youtube.com/results?search_query=lofi"}`. Open the URL in its default handler. The server only sends `https:`, `http:`, `tel:`, `mailto:`, `sms:`, `geo:` and `market:` links (it refuses `javascript:`, `intent:`, `file:`, `content:`, `data:` and app-specific schemes itself). Error if no activity handles it (`"No app can open <url>"`). Do not auto-place calls: `tel:` should open the dialer, not call.

### 4.12a `resolve_url` (optional)
Only for apps whose hello lists `"resolve_url"` in `device.capabilities`. Params: `{"url": "https://secure.chase.com/x"}`. Say which app **would** open the URL, without opening it:
```json
{"package": "com.chase.sig.android", "candidates": ["com.android.chrome", "com.chase.sig.android"]}
```
`package` is `PackageManager.resolveActivity(Intent(ACTION_VIEW, uri), MATCH_DEFAULT_ONLY)`'s package, or `null` when the system would show its chooser (`android`) or nothing handles it; `candidates` is every package from `queryIntentActivities` for the same intent. The server refuses the link before calling `open_url` when the handler (or, with a chooser, any candidate) is an app the task may not open. Without this method, the server opens the link and then checks `current_app`, pressing `BACK` (then `HOME`) if a blocked app came up.

### 4.13 `wake_unlock`
Called at the start of every task. Turn the screen on if it is off, and dismiss the lock screen **if it is not secure** (swipe-to-unlock or none). With a secure lock (PIN, pattern, password, biometric), wake the screen and return `null`. The agent sees the lock screen and asks the user. **Never** enter credentials. Reply with an error only if the screen could not be woken.

### 4.14 `device_state`
Is the phone usable right now? `{"interactive": PowerManager.isInteractive, "locked": KeyguardManager.isKeyguardLocked, "secure": KeyguardManager.isKeyguardSecure}`. Answer it even when accessibility is off, and quickly (it only reads three flags). The server asks before a standalone API task and before each orchestration step:

- `locked` and `secure` (PIN, pattern, password, biometric): "unlock to continue" (`phone_locked`). Nothing is attempted.
- screen off, or `locked` but not `secure` (a swipe lock): the server calls `wake_unlock`, then asks again. Still `locked` means the swipe didn't take: `phone_locked`. Otherwise the task runs.
- no answer within 5 s: `phone_unresponsive`.

Optional: an app that answers `Unknown method device_state` is simply not checked. An app that omits `secure` is treated as having a secure lock whenever it's `locked`.

### 4.15 `wait_idle` (optional)
Params: `{"quiet_ms": 300, "max_ms": 800}`. Block until the screen has stopped changing, then reply `{"waited_ms": <ms spent>, "idle": true}`; at `max_ms`, reply with `"idle": false` (an animated screen or a game may never go quiet). The agent calls it after every action instead of sleeping a fixed time: `max_ms` is that fixed time (0.8 s after a tap, 3 s after `launch_app` or `open_url`), so it never waits longer than before.

"Changing" means accessibility events: `TYPE_WINDOW_STATE_CHANGED`, `TYPE_WINDOWS_CHANGED`, `TYPE_WINDOW_CONTENT_CHANGED` and `TYPE_VIEW_SCROLLED` (add the last two to `accessibilityEventTypes`, §5.2). Ignore your own app's events, and content changes from `com.android.systemui` (the status bar's clock and network meters tick on their own). Record only a timestamp per event, then poll it every ~20 ms. Count the quiet time from the later of the last event and the start of the call, so the reply comes at least `quiet_ms` after the call: the events of the action just performed have time to start (a separate minimum isn't needed). Clamp `quiet_ms` to 50–8000 and `max_ms` to 0–8000.

It's optional: list `"wait_idle"` in the hello's `device.capabilities` only if you implement it. The server then uses it; otherwise (and for adb and iOS phones) it sleeps the fixed time. If a call fails with an error anyway, the server stops calling it on that connection.

### 4.16 `wait_for` (optional)
Params: `{"condition": {"until": "element_enabled", "pattern": "Open|Play"}, "timeout_ms": 10000}`. Block until the condition holds on **your own element list** (exactly what `ui_elements` would answer), then reply `{"met": true, "waited_ms": 840, "seen": "'Open' is enabled", "package": "com.android.vending"}`; at `timeout_ms`, reply with `"met": false`. It backs the agent's `wait_until` tool and its own waits after a launch or a Play Store install, so the server doesn't have to poll `ui_elements`.

| `until` | Holds when |
|---|---|
| `screen_changes` | The element list differs from the one read when the call began: the set of (text, desc, resource id, class, checked), digits blanked so a clock or a percentage isn't a change, positions and `OCRText` ignored, plus the foreground package. |
| `text_appears` | An element's `text` or `desc` matches `pattern`. |
| `text_disappears` | The list has labeled elements and none matches `pattern` (an empty list can't tell). |
| `element_enabled` | An element whose whole `text` or `desc` matches `pattern` is enabled and not `covered`. |
| `app_in_front` | `current_app` is `condition.package`. |
| `loading_done` | The list has labeled elements, none looks like loading (class or resource id containing progress, spinner, loading or shimmer; text such as "Loading…", "Please wait", "45%"), and no window or content change for `condition.quiet_ms` (default 500). |

`pattern` is a case-insensitive regex (if it doesn't compile, match it literally). It matches a label when it is the whole label, or (except for `element_enabled`) when it occurs as whole words: `(?<!\w)(?:pattern)(?!\w)`, so `Open` matches "Open" and "Open app" but not "Opening". The server's `openmob/agent/waiting.py` is the reference; the Android app's `WaitCondition.kt` matches it.

Re-read the list on each accessibility event (§4.15's events; at most every ~150 ms) and at least once a second; in between, checking costs only a timestamp. Clamp `timeout_ms` to 0–10000: the server calls again for a longer wait and checks for a stop in between. **A wait never acts**: it only reads, so it isn't an action for approvals or the gatekeeper (a gatekeeper build classifies `wait_for` as a read and evaluates it on the same filtered element list as `ui_elements`: while a protected app is visible it replies `{"met": false, "redacted": true}` without looking).

It's optional: list `"wait_for"` in the hello's `device.capabilities` only if you implement it. Without it (older apps, adb, iOS) the server polls `ui_elements` itself, paced by `wait_idle` where there is one, and compares tiny screenshots on screens without elements. If a call fails with an error, the server stops calling it on that connection and polls.

Elements may also carry `"enabled": false` (only when greyed out): `element_enabled` needs it, and the model sees "(disabled)" next to the element.

### 4.17 `open_settings` (optional)
Params: `{"page": "display"}` or `{"page": "app_notifications", "package": "com.android.chrome"}`. Open one Settings page directly, so the agent needn't navigate there (docs/design/deep-links.md). `page` is a **key** from the app's own fixed table (`SettingsPages.kt`; the server's copy is `openmob/phone/settings_pages.py`): `display`, `wifi`, `bluetooth`, `sound`, `battery`, `location`, `notifications`, `apps`, `storage`, `airplane_mode`, `date_time`, and the app pages `app_details` and `app_notifications`, which need `package`. The phone maps the key to the intent action itself (`android.settings.DISPLAY_SETTINGS`, `android.intent.action.POWER_USAGE_SUMMARY`, …): **the server never sends an action, component, extra or URI**, and a key not in the table is refused (`"Unknown Settings page 'developer_options'. Known pages: …"`), so whatever a server sends, only these pages can be reached. Security-critical pages (accessibility, language, security, developer options, special app access…) have no key.

For the app pages, validate `package` (an application id, installed, never OpenMob's own) and pass it as the intent data `package:<pkg>` (`app_details`) or the `android.provider.extra.APP_PACKAGE` extra (`app_notifications`). Resolve the intent before starting it; if nothing handles it on this phone (an OEM without that page), reply with an error naming the page (`"This phone has no Display Settings page. Navigate there instead."`), and the agent navigates. Reply `{"package": "com.android.settings"}`: the Settings app that answered (`null` if unknown). The agent then settles as after `launch_app`.

It's optional: list `"open_settings"` in the hello's `device.capabilities` only if you implement it; the server offers the model its `open_settings_page` tool only then. A gatekeeper build judges the call as an act: everyday pages open, `date_time` asks, an app page for a blocked app is refused (§9).

---

## 5. Android implementation guide

The core is an **`AccessibilityService`** (which handles screen, tree, gestures and global keys), plus a foreground service that owns the WebSocket. Target **minSdk 30** (Android 11), because `takeScreenshot` needs API 30.

### 5.1 Method to Android API

| Method | Android API | Notes |
|---|---|---|
| `screenshot` | `AccessibilityService.takeScreenshot(Display.DEFAULT_DISPLAY, executor, callback)` (API 30). Then `Bitmap.wrapHardwareBuffer(result.hardwareBuffer, result.colorSpace)`, `.copy(ARGB_8888, false)`, `Bitmap.createScaledBitmap`, and `compress(JPEG, 80)`. | Close the `HardwareBuffer`. The system rate-limits calls (`ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT`, an interval of about 333 ms, **verify**). On that error, wait and retry within the timeout. Secure windows: see §4.1. Whether the error code or a black frame comes back varies by version (**verify**). |
| `ui_elements` | `getRootInActiveWindow()`. If it is null, `getWindows()` and pick the active or focused `TYPE_APPLICATION` window's `root`. Walk with `getChild(i)`. Use `getBoundsInScreen`, `getText`, `getContentDescription`, `getViewIdResourceName`, `getClassName`, `isClickable`, `isLongClickable`, `isCheckable`, `isChecked`, `isVisibleToUser`. | Needs `canRetrieveWindowContent`. Set `flagReportViewIds` for resource ids and `flagRetrieveInteractiveWindows` for `getWindows()`. Run the walk off the main thread, with a deadline below 15 s. |
| `current_app` | `rootInActiveWindow?.packageName`, falling back to the last `TYPE_WINDOW_STATE_CHANGED` event's `packageName` (filter out `com.android.systemui` and your own package). | Cache it from events so the call is instant. |
| `launchable_packages` | `packageManager.queryIntentActivities(Intent(ACTION_MAIN).addCategory(CATEGORY_LAUNCHER), 0)`, then `activityInfo.packageName`, de-duplicated. | Package visibility: see §5.3. |
| `app_labels` | `packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0))` | Return `null` on `NameNotFoundException`. |
| `launch_app` | `packageManager.getLaunchIntentForPackage(pkg)` plus `FLAG_ACTIVITY_NEW_TASK`, then `startActivity` | A `null` intent means an error. Starting activities from the background: apps with a bound `AccessibilityService` are exempt from the Android 10+ restrictions (**verify** on your target versions). |
| `tap`, `long_press`, `swipe` | `GestureDescription.Builder().addStroke(StrokeDescription(path, 0, durationMs))` and `dispatchGesture(gesture, callback, handler)` | Tap path: `moveTo(x,y)`, about 50 ms. Long press: the same path for `duration_ms`. Swipe: `moveTo(x1,y1); lineTo(x2,y2)`. Complete the call on `onCompleted`, and error on `onCancelled` or a `false` return. Clamp the duration to `GestureDescription.getMaxGestureDuration()`. Needs `canPerformGestures`. |
| `type_text` | Find the node with `findFocus(AccessibilityNodeInfo.FOCUS_INPUT)` and check `isEditable`. Build the new string from `text` and `textSelectionStart/End`, then `performAction(ACTION_SET_TEXT, bundleOf(ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE to newText))`. Optionally move the cursor with `ACTION_SET_SELECTION`. | Any language or emoji works, unlike adb `input text` (ASCII only). Some custom views (games, Flutter, WebView forms) may ignore `ACTION_SET_TEXT`. If so, return the error. |
| `key` | See the key table below. | |
| `open_url` | `startActivity(Intent(ACTION_VIEW, Uri.parse(url)).addFlags(FLAG_ACTIVITY_NEW_TASK))` | `ActivityNotFoundException` means an error. |
| `open_settings` | `Intent(<action from the fixed table>)` plus `FLAG_ACTIVITY_NEW_TASK`; app pages add `data = Uri.parse("package:" + pkg)` or `putExtra(Settings.EXTRA_APP_PACKAGE, pkg)`; `pm.resolveActivity` first, then `startActivity` | Never build the action from the call's params. A null resolution or `ActivityNotFoundException` means an error naming the page. |
| `wake_unlock` | Wake: `PowerManager.newWakeLock(SCREEN_BRIGHT_WAKE_LOCK or ACQUIRE_CAUSES_WAKEUP, tag)`, `acquire(3000)` (deprecated flags that still work, **verify**). Alternatively, start a transparent activity with `setTurnScreenOn(true)` / `setShowWhenLocked(true)` (API 27). Dismiss: `KeyguardManager.requestDismissKeyguard(activity, callback)` (API 26, needs an Activity). Check `isKeyguardSecure` first and skip the dismiss if it is secure. | Only non-secure keyguards are dismissed without user input. That is intended. |

**Keys**

| `name` | Implementation |
|---|---|
| `HOME` | `performGlobalAction(GLOBAL_ACTION_HOME)` |
| `BACK` | `performGlobalAction(GLOBAL_ACTION_BACK)` |
| `APP_SWITCH` | `performGlobalAction(GLOBAL_ACTION_RECENTS)` |
| `ENTER` | On the focused input node, `performAction(AccessibilityAction.ACTION_IME_ENTER.id)` (API 30). If that is unsupported, return an error. (**verify** that common IMEs and apps honour it.) The agent sends `ENTER` after `type_text` to submit searches, so this one matters. |
| `DELETE` | Emulate a backspace on the focused editable node: remove the selection, or the character before the cursor, via `ACTION_SET_TEXT`. |
| `POWER` | Optional: `GLOBAL_ACTION_LOCK_SCREEN` (API 28) locks the screen. Otherwise, error. |
| `WAKEUP` | Same as the wake half of `wake_unlock`. |
| `VOLUME_UP`, `VOLUME_DOWN` | No accessibility global action. Either error, or (optionally) `AudioManager.adjustStreamVolume(STREAM_MUSIC, ADJUST_RAISE/LOWER, FLAG_SHOW_UI)`. |
| `TAB`, `ESCAPE`, `MENU`, `SEARCH` | No accessibility equivalent. Return an error. |

An accessibility service cannot inject arbitrary key events. That is why some keys error. The agent reads the error and tries another approach.

### 5.2 Accessibility service declaration

`res/xml/openmob_accessibility.xml`:
```xml
<accessibility-service xmlns:android="http://schemas.android.com/apk/res/android"
    android:description="@string/a11y_description"
    android:accessibilityEventTypes="typeWindowStateChanged|typeWindowsChanged|typeWindowContentChanged|typeViewScrolled"
    android:accessibilityFeedbackType="feedbackGeneric"
    android:accessibilityFlags="flagReportViewIds|flagRetrieveInteractiveWindows|flagIncludeNotImportantViews"
    android:canRetrieveWindowContent="true"
    android:canPerformGestures="true"
    android:canTakeScreenshot="true"
    android:notificationTimeout="100" />
```
`flagIncludeNotImportantViews` makes the tree closer to `uiautomator dump`. You can drop it if trees are too large. In the manifest:
```xml
<service android:name=".OpenMobAccessibilityService"
    android:exported="false"
    android:permission="android.permission.BIND_ACCESSIBILITY_SERVICE">
  <intent-filter><action android:name="android.accessibilityservice.AccessibilityService" /></intent-filter>
  <meta-data android:name="android.accessibilityservice" android:resource="@xml/openmob_accessibility" />
</service>
```
The user must enable the service in **Settings > Accessibility**. Detect when it is disabled and guide the user there. While the service is off, answer every call with an error ("Accessibility service is off").

### 5.3 Manifest permissions

| Permission / declaration | Why |
|---|---|
| `INTERNET` | WebSocket |
| `FOREGROUND_SERVICE` | Persistent connection service |
| `FOREGROUND_SERVICE_<TYPE>` plus `android:foregroundServiceType` on the service | Required on Android 14+. `connectedDevice`/`dataSync`/`specialUse` are candidates. Pick the one Play accepts for this use (**verify**). Use `microphone` while recording voice. |
| `POST_NOTIFICATIONS` (runtime, API 33+) | Show the persistent notification |
| `RECORD_AUDIO` (runtime) | Voice commands |
| `QUERY_ALL_PACKAGES`, **or** `<queries><intent><action android:name="android.intent.action.MAIN"/><category android:name="android.intent.category.LAUNCHER"/></intent></queries>` | Package visibility on Android 11+. Without it, `launchable_packages`, `app_labels` and `launch_app` only see a few packages. The `<queries>` intent filter is enough for launcher apps and is easier to get through Play review. Also add `<intent><action android:name="android.intent.action.VIEW"/><data android:scheme="https"/></intent>` if you check resolvability before `open_url`. |
| `WAKE_LOCK` | `wake_unlock` |
| `BIND_ACCESSIBILITY_SERVICE` (on the service, as above) | Accessibility service |

---

## 6. Security and UX requirements

| Requirement | Detail |
|---|---|
| Token storage | Store the server URL and token encrypted, with a key in the Android Keystore (for example Jetpack Security / an AES-GCM key from Keystore). Never log the token or put it in crash reports. Exclude it from backups (`android:allowBackup="false"` or backup rules). |
| TLS only | `https://` for every server, except a developer's own server on this phone or the local network (loopback, `10/8`, `172.16/12`, `192.168/16`, `169.254/16`, `.local`, IPv6 loopback, link-local and unique-local), which may use `http://`. Check it when a link or the pair form is used **and** again before every connection, so a stored bad URL can't be used. Release builds also turn cleartext off (`usesCleartextTraffic="false"`). Use the default trust store, with no "trust all certificates" option. |
| Pairing links | `openmob://connect?server=…&code=…` (from the dashboard's QR code) must **never change anything by itself**: any web page or message can open it. Show "Connect to **\<host\>**?" naming the server and what it can do, and do nothing until the owner taps Connect. If the phone is already paired, say which server it will leave and require the screen lock. Keep the current pairing (server, device key, session) until the new one has fully succeeded; cancel or failure leaves it as it was. Never accept a developer token (`t=`) from a link: tokens are typed in by hand, behind the same confirmation. |
| Visible indicator | While connected, show the persistent notification. While a task runs, also show an always-visible "Agent active" indicator (an accessibility overlay `TYPE_ACCESSIBILITY_OVERLAY`, or a status chip). The user must always know the phone is being driven. |
| Kill switch | One tap, reachable from the notification and the overlay. It cancels in-flight gestures, sends `command "stop"`, closes the socket (code 1000) and disables auto-reconnect until the user reconnects manually. |
| Credentials | Never auto-fill passwords, PINs or OTP codes. Refuse `type_text` when the focused node `isPassword`: return an error asking the user to type it themselves. Never dismiss a secure keyguard. |
| Secure screens | `FLAG_SECURE` windows (banking, DRM video, some password screens) come back **black** in screenshots. That is expected and must not be worked around. |
| Confirmation | The server asks before risky taps (`question` events). Make questions hard to miss: speak them and show them prominently. |
| Play Store policy | Google Play restricts `AccessibilityService` use to apps that help users with disabilities, or that declare and justify the use (a prominent disclosure plus consent, and a declaration form). General automation agents may be rejected. Plan for **sideloading or closed testing**, and **verify current policy** before any Play submission. |

---

## 7. Testing your implementation

### 7.1 Run a server

```bash
openmob run --public
```
This prints the dashboard URLs with the token, for example `https://<random>.trycloudflare.com/?t=<token>`. The phone URL is the same host and token: `wss://<random>.trycloudflare.com/phone/ws?t=<token>`. For LAN testing, a debug build may use `ws://<lan-ip>:<port>/phone/ws?t=<token>`.

### 7.2 Compare with the reference client

`openmob bridge SERVER_URL --token TOKEN` is the reference phone client (`openmob/bridge.py`). `SERVER_URL` may be the dashboard base URL (`https://…` becomes `wss://…/phone/ws?t=…`). Like your app, it backs off from 1 s to 30 s and stops on close code 4401. It speaks this protocol from a computer and carries out each call with adb on a USB/Wi-Fi-attached phone (the `PhoneDriver` in `openmob/phone/adb.py`). Use it to:
* check the server side end to end before your app exists;
* compare outputs: run the same screen through the bridge and your app, then diff the `ui_elements` lists and screenshot sizes. Your app's results should match the adb-backed results closely. Expected differences: `type_text` with non-ASCII text (the bridge fails, your app succeeds) and keys with no accessibility equivalent (your app errors).

### 7.3 Watch the dashboard

Open the dashboard URL (`…/?t=<token>`) in a browser. A connected phone appears in the device list, with a live screen view (built from `screenshot` calls) and the agent activity log (the same `event` stream your app receives). Type a command there, or send `command` from the app, and watch for:
* taps landing where the model aimed. If they are offset, your `width`/`height` and `device_width`/`device_height` are inconsistent, or they are in the wrong orientation;
* `tap_element` hitting the right control, which checks the `ui_elements` bounds and order;
* questions arriving and your `command` reply answering them;
* recovery when you toggle airplane mode: backoff, reconnect, and the same device session in the dashboard.

### 7.4 Checklist

- [ ] `hello` is sent first; `device.id` survives an app restart.
- [ ] Concurrent calls are handled, and a 30 s `app_labels` does not stall `tap`.
- [ ] Screenshot long side ≤ 1280, valid JPEG, exact sizes, correct after rotation.
- [ ] `ui_elements` never errors, is capped at 150, has `checked: null` for non-checkables, and bounds in device pixels.
- [ ] Every key name returns success or a clear error.
- [ ] `type_text` inserts at the cursor, handles non-ASCII, and refuses password fields.
- [ ] Text that goes in by paste doesn't stay on the clipboard. Manual check: copy "before" in any app;
  have the agent type into a field that rejects `ACTION_SET_TEXT` (Gmail's To: chips); then long-press
  another field and Paste. It pastes "before" (or nothing, if Android didn't let the app read the
  clipboard), never the typed text, and on Android 13+ no clipboard preview shows the typed text.
- [ ] `resolve_url` (if listed) names the app that would open a link, without opening it.
- [ ] An approval shows the request's `action` and signs `<id>|<task>|<digest>|true|<timestamp>`
  only when its own digest of `action` equals `digest`.
- [ ] Close code 4401 stops retries; other closes back off from 1 s to 30 s.
- [ ] The kill switch disconnects immediately and stays disconnected.

---

## 8. Approvals and the controller line (server behaviour)

This section describes what the server sends and expects. The phone-side UI is in §3.3.

### 8.1 Risky actions are approved on the phone

When a task is about to do something risky (buy, send, delete…) and the confirmation must come from
the human, the server sends:
```json
{"type": "approval_request", "id": "5d0c…", "task_id": "9c1e0b7a2f44",
 "question": "I'm about to tap 'Buy now'. Should I go ahead?", "requester": "CI bot"}
```
`requester` is the API key's name when an API or MCP client started the task, or `"You"` for the
owner's own requests (dashboard, phone). Newer servers add `device_id` and `device_name`: the phone
that would act. The request goes to **every** online phone of the owner, so it may name another
phone; show it as "On Pixel 9: Send message to Mom?". The phone confirms with the screen lock and
replies:
```json
{"type": "approval", "id": "5d0c…", "approved": true, "timestamp": 1791219700,
 "signature": "MEUCIQ…"}
```

**Approvals are signed with a screen-lock-bound key.** So that a leaked device key (or anything
else that reaches the phone's socket) can't approve purchases, the app keeps an **approval key**: an
EC P-256 key pair in the Android Keystore, created with `setUserAuthenticationRequired(true)` and
`setUserAuthenticationParameters(0, AUTH_BIOMETRIC_STRONG | AUTH_DEVICE_CREDENTIAL)`, so it can sign
only right after the owner passes the screen lock.

- **Registering it.** Send the public key as `approval_key` (base64 of the X.509
  SubjectPublicKeyInfo, i.e. Java's `PublicKey.encoded`) in `POST /api/pair/redeem` when pairing.
  Phones paired before this existed send it once in the `hello` (`"approval_key": "MFkw…"`); the
  server keeps the first one and never lets a later hello replace it. Only pairing again does.
- **Approval signing.** Approve through `BiometricPrompt` with a `CryptoObject` wrapping a
  `SHA256withECDSA` `Signature` initialised with the private key. On success, sign the UTF-8 bytes
  of `<approval id>|<task id>|<digest>|true|<timestamp>` (the unix time in seconds, also sent as
  `timestamp`; `|always` appended for "Always allow this") and send the base64 DER signature as
  `signature`. `digest` is the request's action digest: the lowercase hex SHA-256 of
  `openmob-action-1` followed, for each of `tool`, `package`, `text`, `desc`, `resource_id`,
  `bounds`, `input` in that order, by `|<UTF-8 byte length>:<value>` (a missing field is empty;
  `bounds` is `l,t,r,b` or `x,y`). Recompute it from the `action` you show and refuse to sign if it
  differs from `digest`. (Apps from before action digests signed `<approval id>|<task id>|true|<timestamp>`;
  current servers refuse that.)
- **What the server accepts.** In accounts mode a `true` counts only with a valid signature by
  **that phone's** key, over **this** approval id, task and action digest, made within the last 5
  minutes; anything else is ignored (the prompt stays open on the owner's other phones). A phone
  without a registered key can't approve: it is told so (`can_approve: false`) and can only
  decline until it registers one on its next connect. Token/dev mode has no keys and accepts the
  phone's yes as before.
- **The action must still be the same.** After a signed yes, the server re-reads the screen and runs
  the action only if its digest is unchanged (same app, same element at the same place). If the
  screen changed while the owner was deciding, the action isn't run and the agent looks again.
- **Denials are never signed.** Anyone may say no: `{"type": "approval", "id": "5d0c…", "approved": false}`.
| Rule | Server behaviour |
|---|---|
| `approved: true` | The task gets "yes" and goes ahead. Anything but the JSON value `true` counts as denied. |
| `approved: false` | The task gets "no"; the agent doesn't do it. |
| Unknown id, or the task is no longer waiting | Ignored. The confirmation may have been answered elsewhere (dashboard), timed out, or the task ended. |
| No reply within **2 minutes** | Treated as denied. The phone sends nothing when its prompt expires. |
| Phone reconnects while the task still waits | The same `approval_request` (same `id`) is sent again after `welcome`. The phone ignores ids it already answered. |
| Several phones asked | The first answer wins. Every other phone that was asked gets `{"type": "approval_cancel", "id": "5d0c…"}` and drops its prompt without answering. The same message is sent when the confirmation is settled some other way (dashboard, API decline, timeout, task ended). |
| Denied on a phone, for a step of an orchestration | The owner's "no" ends the whole run (`cancelled` / `stopped_on_phone`), not just that action. |

API keys can't approve risky actions themselves (`POST /v1/tasks/{id}/answer` with `approve: true`
returns 403 `confirmation_requires_owner`) unless the owner turned on `confirm_approve` for that key.
For such keys no `approval_request` is sent.

### 8.2 Who is using the phone

When a task started by an API or MCP client begins running, the server sends
`{"type": "event", "kind": "controller", "message": "<who>"}` (the key's name), and
`{"type": "event", "kind": "controller", "message": ""}` when it ends. After a reconnect, the server
re-sends the current controller event if such a task is still running.

### 8.3 Reconnect grace

| The socket closes… | Server behaviour |
|---|---|
| with close code **1000** (the user tapped Stop or disconnected on purpose) | Stops the running task and clears the queue right away, as before. If one of them was an orchestration step, the whole run ends as `cancelled` / `stopped_on_phone`; so does saying "stop" on the phone. |
| any other way (network change, dead connection) while a task runs or is queued | The phone shows as offline but the task is kept. Its next phone call waits up to **45 s** for the same `device.id` to reconnect, then continues on the new socket. |
| …and no reconnect within 45 s | The waiting call fails, so the task fails; queued tasks are cancelled. |
| any way, with nothing running | Nothing to keep. |

Calls are never replayed. A read (`screenshot`, `ui_elements`, `current_app`, …) that was cut off is
asked again on the new connection; an action (`tap`, `type_text`, `launch_app`, …) that was cut off is
reported to the agent as "may or may not have happened" and never repeated. Live-view calls don't
wait for a reconnect.

## 9. Gatekeeper (phone-side policy)

An app whose hello lists `"gatekeeper"` in `device.capabilities` enforces policy itself
(docs/design/phone-gatekeeper.md, Phase 1). A compromised server can then at most *request*: the phone refuses
forbidden calls and asks its owner, with its own wording and the screen lock, for risky ones. The server keeps
all its own checks as defense in depth. Apps without the capability keep today's behaviour (§3.3, §8).

### 9.1 Every call goes through the gate first

Methods fall into three classes (the built-in rule file `gatekeeper/rules.json` in the APK): **meta**
(`device_state`, `wait_idle`, always allowed), **read** (`screenshot`, `ui_elements`, `wait_for`, `current_app`,
`launchable_packages`, `app_labels`, `resolve_url`, `stream_*`: allowed, but `screenshot` is a black frame,
`ui_elements` is `[]` and `wait_for` answers `{"met": false, "redacted": true}` while a blocked app is visible in any
window; `wait_for` checks again on every read during the wait, so it never evaluates a protected app's text) and **act** (`tap`, `long_press`, `swipe`,
`type_text`, `key`, `launch_app`, `open_url`, `open_settings`, `wake_unlock`). Anything else is refused with `gate_unknown_method`.
`open_settings` (§4.17) is judged like opening that Settings page: everyday pages are allowed, `date_time` asks (kind
`settings`, the screen lock; refused under `never`), an app page for a blocked app or OpenMob is `gate_blocked_app`, an
unknown key is `gate_denied_page`, and so is any key whose action or title the hard-deny list names, in every mode.
Act calls are re-checked right before the side effect (the target under the point must still be the same).

Act calls may carry three extra params, which the gate reads and which never relax anything:

| Param | Meaning |
|---|---|
| `mode` | The task's permission mode. The phone uses the tighter of it and its owner's **signed** ceiling (and `never` window): a claim of `never`, `trusted` or `allow_money` without the owner's signature gets `auto`. |
| `task` | The task id (a purchase's one follow-up commit is covered only within the same task and app). |
| `hint` | The server's own caution ("the agent thinks this pays"): turns an allowed action into a prompt, never the reverse. |

### 9.2 Refusals

A refused call gets `ok:false` with a stable `code`, the reason in `error`, and `gate`:

```json
{"type": "result", "id": "…", "ok": false, "code": "gate_blocked_app",
 "error": "Blocked on the phone: com.chase.sig.android is a sensitive app. Don't open it another way; call done.",
 "gate": {"rule": "builtin:sensitive_apps", "package": "com.chase.sig.android", "ms": 6}}
```

| Code | Meaning | Server |
|---|---|---|
| `gate_blocked_app` | A blocked app (banking, payments, wallets, password managers, authenticators, OpenMob itself): launching it, a link into it, acting in it, or its app-info page | Report to the agent; don't retry |
| `gate_denied_page` | A security-critical Settings page, the keyguard or a system approval prompt, OpenMob's own windows, installing an app (`builtin:installer`), uninstalling OpenMob or a blocked app (`builtin:uninstall_protected`) | Report; don't retry |
| `gate_denied` | A dangerous link scheme or typed text (`javascript:`, `intent:`, …), a non-dialer `tel:` handler, a declined prompt, or something that would ask while agents act without asking (`never`) | Report; don't retry |
| `gate_password_field` | Typing into a password field | Report; ask the owner to type it |
| `gate_needs_approval` | The phone is asking its owner (below) | Wait for `gate_event` |
| `gate_prompt_open` | Another prompt is still open on the phone | Wait for it to be answered |
| `gate_rate_limited` | More than 5 actions/s (burst 15), more than 6 prompts the owner was shown in 10 minutes (refused calls and prompts never shown don't count, and the same action asked again reuses the open prompt; `error` says how long to wait), or paused after 3 denials in 10 minutes until the owner taps Resume | Report; slow down |
| `gate_screen_changed` | The target changed between the check and the gesture (or during the prompt) | Report to the agent; never retried automatically |
| `gate_screen_unstable` | The screen didn't settle within a second for a risky action | Report; look again |
| `phone_locked` | A secure lock screen is up | Pause the task until `device_lock` says unlocked (§9.4) |
| `needs_owner_dialog` | A system dialog the phone can't read or OCR-classify is up (Safety Center, default-app pickers; a permission dialog only when on-device OCR is unavailable); `error` says what it is ("Kalshi is asking for location: choose on your phone, then the task continues") | Pause the task until `owner_dialog` says it closed (§9.5); never tap it |
| `gate_permission_choice` | A permission-dialog choice the owner's settings never allow: "Allow all the time"; a grant the owner set to Don't allow; a grant while Precise is selected and the owner allows approximate only. `error` says what to tap instead | Tap what `error` says ("Only this time", "Don't allow", "Approximate" first) |
| `gate_unavailable` | OpenMob's accessibility service is off, so the phone can't see the screen: every act is refused | Report; the owner turns it on |
| `gate_no_session`, `gate_policy_invalid` | Reserved (Phase 2) | Report |

### 9.3 Approvals on the phone

For a risky action (a money commit, a send/post/delete, autofill or saved credentials, a share target, an
unrecognized Settings change, an unlabeled tap in a shopping app, typing into an OTP or card field, ENTER in a
chat or a checkout, every changing act in `ask` mode) the phone replies at once:

```json
{"type": "result", "id": "…", "ok": false, "code": "gate_needs_approval", "approval_id": "3f…",
 "error": "Waiting for the owner's approval on the phone.",
 "gate": {"rule": "risk:words:buy now", "kind": "pay", "question": "Allow tapping “Buy now” in Amazon?", "ms": 9}}
```

and shows its own prompt: its wording and a crop of the target, with the server's `hint` only as a labeled
"Server says" line. Approving takes the screen lock; the auth-bound approval key signs
`gk1|<device id>|<approval id>|<action binding>|crop:<sha256 of the crop>[|grant:<grant>]`, the phone verifies it
and mints a **one-shot permit for exactly that call** (method, params and the target's fingerprint) for 60 s on
its monotonic clock. Then it tells the server:

```json
{"type": "gate_event", "approval_id": "3f…", "state": "approved"}
```

`state` is `pending`, `approved`, `denied` or `expired` (2 minutes, or Stop). Only the owner's explicit no
(Deny, or cancelling the screen lock themselves) is `denied`; a prompt the system interrupted (a notification or
call taking focus, the screen turning off, the sensor timing out) stays pending and comes back, and one that
ends without a decision is `expired`. The server reports `expired` to the agent as `gate_no_answer` ("the owner
made no decision"), never as a decline, and only denials count toward the 3-denials pause. The server re-sends the identical
call **once per approval id**; a replayed `gate_event` does nothing. On `denied` or `expired` it tells the agent
"not approved" and sends nothing. An approved "Buy now" (a flow-starting money word) also covers one terminal
commit ("Place your order") in the same app and task within 3 minutes, so a purchase is one prompt.

While the phone is asking, the task is `needs_confirmation` (the dashboard shows "Waiting for your approval on
the phone…"). `POST /v1/tasks/{id}/answer` with `approve:true` returns **409 `approval_on_phone`** for every key;
`approve:false` declines. Gatekeeper phones **ignore `approval_request`** for actions (a server-worded prompt
behind the screen lock would be phishing); only `mode_change` requests are taken, and the phone words those
from the signed change itself. Approval happens only on the acting phone; the owner's other phones get a plain
notice.

### 9.4 The lock screen mid-task

While the agent works the app keeps the screen on (a 1-pixel accessibility overlay with `FLAG_KEEP_SCREEN_ON`,
removed as soon as work stops). If the phone locks anyway, a swipe-only lock is woken and dismissed; a secure
lock makes the phone send

```json
{"type": "device_lock", "locked": true, "secure": true}
```

and show "Unlock your phone to continue". Act calls get `phone_locked` meanwhile. When the owner unlocks, it
sends `{"type": "device_lock", "locked": false}`. The server pauses the task for up to `locked_wait_seconds`
(default 300), then lets the agent look again; if the phone stays locked, the task fails with reason
`phone_locked`. The agent never taps or swipes at a lock screen.

### 9.5 System dialogs the phone can't read

On Android 14 and later the system hides runtime permission dialogs (and Safety Center and default-apps
screens) from accessibility services: the phone sees the window, but not what's in it (or only the window-state
event's package and text). Gestures still reach it and screenshots capture it.

**Permission dialogs are read with on-device OCR** (ML Kit text recognition, the bundled Latin model: offline,
no network). When the top application window has no readable tree (Android 15 on Samsung lists it with no package
and sends no event for it), the phone takes its own screenshot and reads it. It counts as a permission dialog when
the window names the permission controller, or when the OCR shows an "Allow X to …" title together with either a
fresh window-state event from `com.android.permissioncontroller`, `com.google.android.permissioncontroller` or
`com.samsung.android.permissioncontroller`, or a "Don't allow" button. Otherwise it stays an unknown window (black,
acts refused). Of the reading:
the title ("Allow Kalshi to access this device's location?", English, Spanish and German wording) gives the
requesting app and the permission; lines that read exactly as a button ("While using the app", "Only this
time", "Allow", "Don't allow", "Allow all the time", "Precise", "Approximate") become buttons. `ui_elements`
lists them first, class `OCRText` with bounds (the title with `resource_id: "ocr_permission_message"`), and the
live view shows the dialog normally. A tap on it is judged on that reading like a readable dialog (docs/safety.md):
the task's app gets "Only this time" (or the narrowest grant offered), notifications and "Approximate" with no
prompt; "Allow all the time" never (`gate_permission_choice`); another app's request, a tap off every recognized
button, low-confidence text (below 0.5) or an unknown wording asks (a tap-level prompt; refused under Don't
ask). OCR can't see which location card is selected: the phone assumes Precise (Android's pre-selection) until
a tap on Approximate in that dialog goes through.

**Other hidden system surfaces** (Safety Center, default-app pickers), and a permission dialog when OCR isn't
available, are never tapped blindly. Act calls get `needs_owner_dialog`, the phone
shows "Kalshi is asking for location: choose on your phone, then the task continues" (or "A system dialog needs
your answer") in a notification, and tells the server:

```json
{"type": "owner_dialog", "open": true, "message": "Kalshi is asking for location: choose on your phone, then the task continues."}
```

and `{"type": "owner_dialog", "open": false}` once the dialog is gone. The server pauses the task (its events
show the same message) for up to `owner_dialog_wait_seconds` (default 300), then lets the agent look again; if
it's never answered, the task fails with reason `needs_owner_dialog`. While it's open the dashboard's live view
says "<App> is asking for <permission>: answer on your phone" (`owner_dialog` in the device status). Reads are
not blacked out for an identified system dialog; a window with no package from any source still is.

A tap where the phone sees no window under the point is refused as `gate_screen_unstable` (`builtin:no_window`).

### 9.6 Live screens

Screens whose numbers change all the time (odds, prices, timers) don't block actions. The phone recognizes "the
same element" by its id, class, bounds and window with numbers blanked ("DUN · 42%" = "DUN · 47%"); a risky
action waits for *its target* to hold still for 150 ms, not the whole screen; and the launch-race wait counts
window changes, not content updates. A money commit is the exception: its permit binds the currency amounts on
the button and on the window's total, cost, price, amount and payout lines, and any change voids it
(`gate_screen_changed`, rule `builtin:price_changed`): the owner is asked again. Choosing a side or outcome
("DUN", "Yes") doesn't ask; the commit ("Submit", "Place order", "Swipe to buy") does. Ticker-style labels
("YES 63¢", "AAPL +1.2%") never count as an unrecognized language: that rule looks at the script and at
ordinary lower-case words only.

### 9.7 Task approval (just in time)

By owner request, a task can be approved once instead of step by step, and only when it needs it. Nothing is
asked at task start. To phones whose hello lists `"task_grant"`, the server adds the task's request to every act
call: `task` (id), `task_text` (the request, up to 300 characters) and `task_source` (who asked). The first step
that would need a tap-level confirmation and whose kind the request names (a send for "send Bob the link", a
delete for "delete old photos") gets the task prompt instead of a per-step one, worded from the request with
that step as the detail ("Agent wants to: send Bob the channel link on Messenger. Allow the steps this needs?
Next: Allow tapping “Send” in Messenger?"); it replies `gate_needs_approval` with `kind: "task"`. Approving it
grants the task and permits that step (the server re-sends it). A task with no such step sees no prompt at all;
a fingerprint-level step keeps its own fingerprint prompt (never covered by the grant). A request typed or
spoken on the phone itself is granted without a prompt (`rule: task_grant:own_request`). The `task_grant`
method still exists (it asks up front) but the server no longer calls it.

The grant is bound to the task id and the request text's digest (another text for the same id asks again), and
lasts until the task ends, 30 minutes on the monotonic clock, or 25 risky steps, whichever comes first. Under it,
an act call carrying that `task` runs its non-money risky steps without more prompts (send, post, share, delete,
menus, unlabeled taps, ENTER, unrecognized non-security Settings pages, native-app autofill). Still asked: money
commits (unless "Also allow purchases"), browser or WebView autofill and runtime permission dialogs. Still
refused: every hard deny. Declining the task prompt stops the task (`task_declined`); an unanswered one is
not a denial and ends it with `task_no_answer`. The server sends `task_text` in Ask for risky, Trusted apps and
Plan first, not in Ask every step or Don't ask, and not when the owner turned `task_approval` off (then each
risky step asks on its own); turning it back on is a loosening approved on the phone.

### 9.8 Consequence-based risk and confirmation levels

A gate ask (`gate_needs_approval`) has a confirmation level the phone chooses: `tap` (OpenMob's own Allow /
Deny, signed with a tap key that needs no screen lock, message `gk1|tap|<device>|<approval id>|<binding>`) or
`fingerprint` (the screen-lock key, message `gk1|<device>|<approval id>|<binding>…`). Each key verifies only its
own level. Low-risk actions (a link to yourself, copy, toggles such as like or subscribe) don't ask at all; see
docs/safety.md, "How OpenMob decides what's risky". The owner's signed "Don't confirm everyday actions"
(`everyday` in the permission settings; the `permissions` push carries `everyday: false` as a tightening) lets
tap-level actions run unconfirmed. The task approval (§9.7) is a tap-level prompt and isn't asked at all with
that setting on. The server sends `hint` only for concrete risky words or rules, never for its classifier alone.
