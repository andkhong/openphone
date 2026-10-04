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
* Transport: one WebSocket. Every frame is a **UTF-8 JSON text frame** holding exactly one object with a `"type"` field. Binary data (screenshots) is base64 inside JSON.
* The server is the **RPC client**: it sends `call`s, and the phone replies with exactly one `result` per call.
* The phone is also the **user's microphone/keyboard**: it sends `command` messages and shows or speaks `event`s.

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
| Any other close, network error or failed connect | Reconnect with **exponential backoff**: 1 s, 2 s, 4 s, 8 s, 16 s, 30 s, then 30 s for each further attempt (cap 30 s). Add ±20 % jitter. Reset to 1 s after a successful `welcome`. |
| Network changes (Wi-Fi to cellular) | Reconnect right away. `ConnectivityManager.NetworkCallback` is a reasonable trigger. |
| User taps the **kill switch** (§6) | Close with code 1000, cancel any in-flight gestures, **do not reconnect**. |

When the phone disconnects, the server fails all pending calls. After a reconnect (new `hello` with the same `device.id`), the server attaches the new socket to the same device session. Calls are **never** replayed, so the phone keeps no call state across connections.

Keepalive: no application-level ping is defined. Use WebSocket ping frames from your client library (for example OkHttp `pingInterval(20, SECONDS)`), so that dead connections are detected and NAT mappings stay open.

### 2.5 Foreground service

While connected, run the socket inside a **foreground service** with a **persistent notification** ("OpenMob agent connected", with a **Disconnect** action). Without one, Android will kill the connection in the background. On Android 14+ a foreground service type must be declared (see §5.3).

---

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

---

## 4. Methods

Every method replies `{"ok": true, "result": <value>}` on success. A method whose result is `null` must still send `"result": null`. "Timeout" is how long the server waits. Aim to finish well inside it.

| Method | Params | Result | Timeout |
|---|---|---|---|
| `screenshot` | none | Screenshot object | 30 s |
| `ui_elements` | none | array of elements (`[]` if unavailable) | 15 s |
| `current_app` | none | string or `null` | 10 s |
| `launchable_packages` | none | array of strings | 30 s |
| `app_labels` | `packages: string[]` | object `{package: string or null}` | 300 s |
| `launch_app` | `package: string` | `null` | 15 s |
| `tap` | `x, y: int` | `null` | 10 s |
| `long_press` | `x, y: int`, `duration_ms: int` | `null` | 15 s |
| `swipe` | `x1, y1, x2, y2: int`, `duration_ms: int` | `null` | 15 s |
| `type_text` | `text: string` | `null` | 15 s |
| `key` | `name: string` | `null` | 10 s |
| `open_url` | `url: string` | `null` | 15 s |
| `wake_unlock` | none | `null` | 10 s |

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
Params: `{"text": "lofi hip hop"}`. Insert `text` into the **currently focused editable field** at the cursor, replacing any selected text. Do not wipe the field: adb `input text` types at the cursor, and the agent relies on that. When the field shows only its hint (`isShowingHintText`, API 26+), treat it as empty. Any Unicode text must work. Do not press Enter: the server sends `key ENTER` separately when it wants to submit. Error if no editable node has focus: `"No focused text field. Tap the field first."`

### 4.11 `key`
Params: `{"name": "BACK"}`. Names are uppercase and come from `adb.KEYCODES`:

`HOME`, `BACK`, `VOLUME_UP`, `VOLUME_DOWN`, `POWER`, `TAB`, `ENTER`, `DELETE`, `MENU`, `SEARCH`, `ESCAPE`, `APP_SWITCH`, `WAKEUP`

See §5.1 for how each one maps. For a key you can't perform, reply with an error naming the key (`"Key MENU is not supported by the OpenMob app"`). For a name not in the list: `"Unknown key FOO"`.

### 4.12 `open_url`
Params: `{"url": "https://youtube.com/results?search_query=lofi"}`. Open the URL in its default handler. It may be `https:`, `tel:`, `geo:`, `market:`, or an app deep link. Error if no activity handles it (`"No app can open <url>"`). Do not auto-place calls: `tel:` should open the dialer, not call.

### 4.13 `wake_unlock`
Called at the start of every task. Turn the screen on if it is off, and dismiss the lock screen **if it is not secure** (swipe-to-unlock or none). With a secure lock (PIN, pattern, password, biometric), wake the screen and return `null`. The agent sees the lock screen and asks the user. **Never** enter credentials. Reply with an error only if the screen could not be woken.

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
    android:accessibilityEventTypes="typeWindowStateChanged|typeWindowsChanged"
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
| TLS only | Release builds refuse `ws://` and any `http` server URL. Use the default trust store, with no "trust all certificates" option. (Public servers run behind HTTPS, for example `openmob run --public`, which uses a cloudflared tunnel.) |
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
- [ ] Close code 4401 stops retries; other closes back off from 1 s to 30 s.
- [ ] The kill switch disconnects immediately and stays disconnected.
