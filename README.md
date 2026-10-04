# OpenMob

Speak a request and an AI agent carries it out on your Android phone.

> "Open Where Winds Meet, and open the menu."

```
 Mac mic (hold right ⌥) ─┐
 Phone web page ─────────┼─> text ─> Agent loop ──────────────> PhoneDriver (adb) ─> Android phone
 Terminal ───────────────┘            │  Claude: sees the screenshot, picks the next action
                                      │  Jev:    classifies the screen from its text (state, risk, app)
                                      └─< screenshot + UI tree + OCR text <─┘
```

Each step works like this:

1. OpenMob takes a screenshot over adb.
2. It reads the screen's text. Normal apps provide it through the Android accessibility tree. Games draw their own UI, so for those it runs Apple Vision OCR on the screenshot.
3. **Jev** (TypeSafe) classifies the screen from that text: loading, pop-up, tap-to-start, menu open, and so on. It also estimates whether the request looks complete.
4. **Claude** looks at the screenshot and makes one tool call: `launch_app`, `tap`, `tap_element`, `swipe`, `scroll`, `type_text`, `press_key`, `wait`, `ask_user`, or `done`.
5. OpenMob runs the action, and the loop repeats until the task is done.

Results are spoken back with macOS `say`.

## How Jev is used

Jev accepts **text only** ([models](https://docs.typesafe.ai/models.md): "No image, audio, or video input"). So the screenshot is first turned into text: foreground app, accessibility labels, and OCR'd words. Jev then answers typed questions about it:

| Question | Type | What OpenMob does with it |
|---|---|---|
| Which screen state is this? | choice | On `loading` with p ≥ 0.8 and enough confidence, it waits and re-checks, without paying for a Claude call. The state is also shown to Claude as a hint. |
| Does the screen show the request is done? | noul | Shown to Claude as a hint. |
| Would tapping this target spend money, send, delete, or change the account? | noul | Above `jev_risk_threshold`, it asks you before tapping. The keyword list is also always checked. |
| Which installed app is "where winds meet"? | choice | Fallback for app-name matching after aliases and fuzzy matching, before Claude. |

Jev is optional. Without `TYPESAFE_API_KEY`, OpenMob still runs using only Claude and the keyword safety check.

## Setup

1. Install adb and connect the phone:
   ```sh
   brew install android-platform-tools
   ```
   - On the phone, go to **Settings → About phone** and tap *Build number* 7 times.
   - Then go to **Developer options** and turn on **USB debugging**.
   - Plug in the phone and accept the "Allow USB debugging" prompt.
   - Xiaomi/POCO phones also need **USB debugging (Security settings)**, or taps are ignored.
   - For wireless use, pair through *Wireless debugging* (Android 11+): `adb pair <ip:port>` then `adb connect <ip:port>`. On older phones, run `adb tcpip 5555 && adb connect <phone-ip>`.
2. Install OpenMob:
   ```sh
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e '.[voice,mac]'        # voice = Whisper + mic + hotkey; mac = Apple Vision OCR
   cp config.example.yaml config.yaml
   export ANTHROPIC_API_KEY=...
   export TYPESAFE_API_KEY=...          # optional, enables Jev
   ```
3. On macOS, give your terminal app **Microphone**, **Input Monitoring**, and **Accessibility** permissions. These are needed for the push-to-talk hotkey.
4. Check the setup with `openmob doctor`.

## Dashboard (demo / hosted mode)

```sh
openmob run --public
```

This prints a `localhost` link and a public **HTTPS** link, both protected by a token. The dashboard shows:
- The live screen of every connected phone, with a device picker.
- Typed commands, or **hold-to-talk** voice (recorded in the browser and transcribed on the server).
- The agent's thoughts and actions, plus Jev's screen classifications.
- **Yes/No** buttons when the agent asks for confirmation, and **Stop**.

For wireless control, `openmob connect` switches USB phones to Wi-Fi. `openmob connect <ip>` reconnects, and `--stay-awake` keeps the screen on. Wi-Fi speed varies a lot; see [DEMO.md](DEMO.md).

## Hosted mode: phones connect to the server

Phones can join a server from any network. Each phone opens an outbound WebSocket to `wss://<server>/phone/ws` and the server's agent drives it with the [phone protocol](docs/phone-protocol.md). The coming Android companion app implements that protocol. Until then, `openmob bridge` is the reference client: it runs on any laptop with an adb-attached phone.

```sh
# Server (e.g. your Mac). --no-adb: only accept phones that connect in.
openmob run --public --no-adb
# → prints the public HTTPS link and token

# Each user, on their own laptop with their phone plugged in:
openmob bridge https://<public-link> --token <token>
```

The phone appears in the dashboard as `connection: app`. Commands can come from the dashboard, or be typed into the bridge terminal (the app will add voice). Agent activity streams back to the phone. A dropped connection reconnects with backoff and keeps the same session.

Code: `openmob/phone/protocol.py` (messages), `openmob/phone/remote.py` (server side: `PhoneLink`, `RemoteDriver`), `openmob/bridge.py` (phone side), `openmob/phone/base.py` (the `Device` interface both drivers implement).

## Use

```sh
openmob do "open where winds meet and open the menu"   # one command, typed
openmob run                                            # hands-free mode
```

`openmob run` accepts commands from three places, one task at a time:

- **Mac mic.** Hold **right ⌥**, speak, and release. Press **Esc** to stop the current task.
- **Phone.** Open the printed `http://<mac-ip>:8765/?t=<token>` link on your phone, then dictate with the keyboard's mic key and press Send. Alternatively, use 🎙 Record to upload a voice clip. The agent presses HOME first, so the browser isn't in the way.
- **Terminal.** Type a line and press Enter.

Saying or typing "stop" cancels the task. When the agent asks a question (for example "About to tap 'Buy'. Go ahead?"), your next utterance from any input is taken as the answer.

### App names

`launch_app` resolves spoken names in this order:

1. `aliases` in config.yaml
2. Cached app labels and fuzzy matching on package names
3. Initials (e.g. `wwm`)
4. Jev
5. Claude Haiku

If nothing matches, the agent opens the app drawer and searches by itself. To find a package for an alias:

```sh
openmob apps | grep -i <word>
openmob apps --resolve "where winds meet"
openmob apps --labels          # reads real app names from APKs; slow, needs aapt2, cached
```

## Safety

- Before tapping anything that matches `confirm_keywords` (buy, pay, send, delete, …), or that Jev rates risky, the agent asks you and waits for "yes".
- It is told never to type passwords, payment details, or one-time codes.
- `max_steps` caps every task. "stop" or Esc cancels it.
- The phone page needs the per-run token, but it's plain HTTP on your LAN. Use it only on networks you trust.

## Limitations

- `type_text` uses `adb input text`, which only supports ASCII. Install ADBKeyboard on the phone for other scripts.
- Screens marked secure (banking apps, some payment pages) come back black in screenshots.
- Fast-moving game screens can change between the screenshot and the tap. Real-time combat is out of scope.

## Development

```sh
pip install -e '.[dev,mac]'
pytest
python -m openmob.phone.adb screenshot out.jpg   # driver checks: tap X Y | launch PKG | tree | current | apps
```

Layout:

| Path | Contents |
|---|---|
| `openmob/phone/adb.py` | Driver |
| `openmob/phone/apps.py` | App-name resolution |
| `openmob/agent/` | Tool definitions and loop |
| `openmob/jev.py` | Jev client and questions |
| `openmob/vision/ocr.py` | Apple Vision OCR |
| `openmob/voice/` | Whisper and `say` |
| `openmob/runner.py` | Task queue and routing |
| `openmob/server.py` | Phone web page |
| `openmob/main.py` | CLI |
# openphone
