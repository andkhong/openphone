# OpenMob

Give AI agents access to your Android phone through one server, REST API, and MCP interface.

OpenMob can run a goal with its own agent, or let an external agent observe and operate the phone one step at a time. The Android companion app connects to your server over an outbound WebSocket, reads the screen through Accessibility, and checks actions on the phone before executing them.

> “Open Settings and go to Display.”
>
> “Search YouTube for lo-fi.”
>
> “Open Maps and show directions to the airport.”

OpenMob is an early-stage, self-hosted project. It operates real apps on a real device; reliability depends on the app, its screen layout, and the model you use.

## What works today

- **Android companion app:** pair with a QR code and screen-lock confirmation, then connect over Wi-Fi or mobile data. Everyday use needs no USB cable or ADB.
- **Dashboard and phone commands:** type a request in either interface or use the phone's speech recognition. Follow actions, view the live screen, answer questions, and stop a task.
- **Two ways to use MCP:** delegate a goal with `run_task`, or use drive sessions to observe, tap, type, scroll, launch apps, open links, and wait for screen changes with your own agent's reasoning.
- **REST API:** scoped API keys, tasks, live SSE events, cancellation, history, screenshots, webhooks, rate limits, quotas, and an audit log.
- **Multiple phones:** accounts own their paired devices. Orchestrations run tasks across phones with dependencies and results passed between steps.
- **Multiple model providers:** adapters for Anthropic, OpenAI, Google, Qwen, xAI, and OpenAI-compatible endpoints. The dashboard selects a model; API tasks select an operator-configured tier (`fast`, `balanced`, or `best`).
- **Faster navigation:** direct Settings pages, a catalog of in-app links, condition-based waits, and an optional Jev classifier that can handle labeled screens without a main-model decision.
- **On-phone safeguards:** the companion app's gatekeeper evaluates actions against the actual screen, blocks sensitive apps and security-critical settings, and requests owner approval for risky actions.
- **Development backends:** direct Android control over ADB, an ADB-to-server bridge, and an experimental iPhone bridge through WebDriverAgent on a Mac.

## How it works

```text
Dashboard / phone commands      Your code or external AI agent
            |                         | REST /v1 · MCP /mcp
            +------------+------------+
                         v
                   OpenMob server
          accounts · scopes · tasks · approvals
                         |
             built-in agent OR drive session
                         |
                outbound phone WebSocket
                         |
                         v
                  Android companion
          screen + Accessibility + local gatekeeper
                         |
                         v
                      Your apps
```

The built-in agent reads the current app, Accessibility elements, and screenshots when needed, chooses an action, and checks the resulting screen. Optional **Jev** (TypeSafe) classifies screen text and can choose simple actions on well-labeled screens. It receives text rather than images, unless the phone's owner turns on Faster mode (small screenshots of screens without readable text). Without its key, the main-model path still works.

Settings and in-app links can skip UI navigation. Experimental navigation replay and plan-then-execute are available behind configuration switches and are **off by default**. Saved-notes memory and learned skills are not current features.

## Run your own server

You need **Python 3.10+**, an API key for a configured model provider, and an Android phone with **Android 11+** and a screen lock. The current Android build targets **ARM64** devices. Building the app requires **JDK 17** and the **Android SDK with API 35**.

```sh
git clone https://github.com/andkhong/openphone.git openmob
cd openmob
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml

# Set this privately in your terminal; never commit a real key.
export ANTHROPIC_API_KEY="<your-api-key>"
```

The default model and API tiers use Anthropic. To use another provider, install its optional SDK and change the model/tier mapping in `config.yaml`. See [Configuration and providers](docs/self-hosting.md#model-providers).

Build a development APK:

```sh
cd android
./gradlew assembleDebug
cd ..
```

Start the server:

```sh
openmob run --accounts --no-adb --quiet
```

The dashboard is at `http://localhost:8765`. `--accounts` enables account sign-in, phone pairing, `/v1`, and `/mcp`; `--no-adb` uses phones connecting through the app.

The phone must be able to reach the server. For remote access or easy QR pairing, install `cloudflared` and add `--public` to publish an HTTPS quick tunnel, or configure your own HTTPS reverse proxy. Signed release apps require HTTPS; debug/local builds also permit local HTTP addresses.

In another terminal, with the same virtual environment and configuration, create your account and get a one-time sign-in link:

```sh
openmob account add you@example.com --password-file ~/.openmob-password
openmob account link you@example.com
```

The password file is created if it does not exist. Keep it private. Open the printed sign-in link, choose **Connect a phone**, install the APK from the pairing page, and scan the QR code. Confirm pairing with the phone's screen lock, then complete its Accessibility, notifications, and background-running checklist. Keep the phone connected and unlocked while tasks run.

Sign-up is invite-only; server operators create additional invites with `openmob account invite`. See [Phone setup](docs/phone-setup.md) and [Self-hosting](docs/self-hosting.md) for the complete setup.

### Build for distribution

Debug APKs are for development. For distribution, generate your own signing key with `scripts/om keygen`, then build `assembleRelease` in `android/`. The signing key and its properties file live outside the repository; `OPENMOB_SIGNING` can point to another properties file.

Set `OPENMOB_APK` to your signed APK's absolute path before starting the server to serve it at `/download/openmob.apk`. A phone must uninstall a debug-signed app before switching to your release signature, then pair again. See [Release signing](docs/release-signing.md).

### Optional providers and OCR

```sh
pip install -e '.[openai,gemini]'   # SDKs for additional providers
pip install -e '.[mac]'             # Apple Vision OCR for canvas screens on macOS
```

| Provider / feature | Environment variable |
|---|---|
| Anthropic | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Google | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
| Qwen | `QWEN_API_KEY` or `DASHSCOPE_API_KEY` |
| xAI | `XAI_API_KEY` |
| OpenAI-compatible endpoint | `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_API_KEY` when required |
| Optional Jev classifier | `TYPESAFE_API_KEY` |

Qwen, xAI, and compatible endpoints use the `openai` extra. Keys belong in the server environment. Configure models, endpoints, and tier mappings using [config.example.yaml](config.example.yaml). Provider fallback is Claude-only, so configure Anthropic if you want that fallback available.

## Connect an agent or use the API

Create an API key in **Developer API & AI agents** on the dashboard. Default scopes allow listing devices and running/reading tasks; screenshots and drive access need explicit scopes.

For an MCP client, use:

- **URL:** `https://<your-server>/mcp` (Streamable HTTP)
- **Authorization header:** `Bearer <your-openmob-api-key>`

For clients that launch a local stdio server, set `OPENMOB_SERVER` and `OPENMOB_API_KEY` in the client's environment and run `openmob mcp`.

`run_task` delegates to OpenMob's agent. Drive sessions need `devices:drive`, plus `screen:read` for screenshots. They use the same action checks and owner approvals as the built-in agent. See [MCP tools and examples](docs/mcp.md).

For REST, set your server address and key privately, then list phones and start a task:

```sh
export OPENMOB_SERVER="https://<your-server>"
export OPENMOB_API_KEY="<your-openmob-api-key>"

curl -sS "$OPENMOB_SERVER/v1/devices" \
  -H "Authorization: Bearer $OPENMOB_API_KEY"

# Replace <device-id> with an online device from the response.
curl -sS -X POST "$OPENMOB_SERVER/v1/devices/<device-id>/tasks" \
  -H "Authorization: Bearer $OPENMOB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"instruction":"Open Settings and go to Display","tier":"fast"}'
```

Follow `/v1/tasks/<task-id>/events` for live SSE events, or retrieve `/v1/tasks/<task-id>`. A task can pause for an answer or owner approval. Disconnecting the API/MCP client does not cancel it; use cancellation or Stop. See the [Quickstart](docs/quickstart.md), [REST reference](docs/api.md), and [Code examples](docs/examples.md).

## Safety and privacy

OpenMob's Accessibility service can see and operate other apps. Connecting a phone grants substantial access; use a dedicated test device while evaluating it.

- **The phone checks actions locally.** The gatekeeper ships in the current Android app and refuses blocked banking, wallet, password-manager, and authenticator apps, password-field typing, unsafe links, and security-critical Settings changes. These protections are specific to the Android companion; ADB and iPhone bridges do not provide that on-device enforcement.
- **Approvals happen on the phone.** Everyday risky actions can require an Allow/Deny tap; purchases and dangerous actions require fingerprint/PIN approval. Permission modes and owner-approved grants affect when prompts appear. An external agent cannot simply answer “yes” to approve a gatekeeper action.
- **The owner stays in control.** Stop from the phone, dashboard, or API. Revoking a phone or API key cuts off its access. Tasks and drive sessions have limits.
- **Screen content leaves the phone.** The built-in agent sends observations and screenshots as needed to the configured AI providers. Jev, when enabled, receives screen text; with **Faster mode** (off by default, turned on only on the phone with its screen lock) TypeSafe also receives small screenshots of screens without readable text, never of banking, wallet or password apps or blacked-out screens ([details](docs/safety.md#faster-mode)). External drive clients receive observations too. OpenMob does not persist screenshots; task text, outcomes, events, and step records are stored with a configurable retention period (30 days by default). Typed text is redacted from activity and step logs.
- **Guardrails have limits.** Phase 1 still allows reading non-blocked apps and ordinary actions; some icon-only or non-English risky controls can evade recognition. Screen content is untrusted input, and these checks are not a guarantee against prompt injection or unintended actions.

Read [Safety, approvals, and data handling](docs/safety.md) before giving an agent access to a personal phone.

## Limitations

- The phone must be awake and unlocked; OpenMob does not bypass its screen lock.
- App layouts, animations, unlabeled controls, and canvas/game screens can reduce reliability. Real-time gameplay is outside the current scope.
- Android secure screens may be black in captures. Live video requires Android's screen-sharing consent; agent tasks do not require continuous video sharing.
- Run one server process per data directory. Phone connections and running tasks are held in that process; horizontal scaling is not supported.
- iPhone support is an experimental Mac/Xcode/WebDriverAgent development bridge, not an iPhone companion app. See [iOS setup](docs/ios.md).

Latency measurements, their test conditions, and MVP targets are documented in [Benchmarks](docs/benchmarks.md); they are not guarantees for other phones or apps.

## Development

```sh
pip install -e '.[dev]'
python -m pytest -q

cd android
./gradlew testDebugUnitTest assembleDebug
```

For direct Android development with USB debugging enabled and ADB installed:

```sh
openmob doctor
openmob do "Open Settings and go to Display" --quiet
openmob run --quiet
```

ADB is an alternative backend; it is not needed for the companion app's normal network connection. See [Testing](docs/testing.md) for emulator and adversarial tests.

| Path | Purpose |
|---|---|
| `android/` | Companion app, Accessibility actions, approvals, local gatekeeper |
| `openmob/agent/` | Built-in agent, drive sessions, links, navigation optimizations |
| `openmob/platform/` | Shared service, REST, MCP, orchestrations, webhooks |
| `openmob/phone/` | Device interface, remote, ADB, and iOS drivers |
| `openmob/static/` | Dashboard, pairing, developer console, admin UI |
| `openmob/config.py` | Configuration, model allowlist, defaults |
| `tests/` | Server tests, gatekeeper fixtures, adversarial harness |

Additional references: [Phone protocol](docs/phone-protocol.md), [Errors and limits](docs/errors.md), [Webhooks](docs/webhooks.md).
