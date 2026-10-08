# AGENTS.md: handoff for coding agents (Codex and others)

Last updated 2026-10-06 by Claude Code, handing over until its quota resets. The owner is **andkhong**. They want
plain, brutally honest answers, with measured results kept separate from estimates.

## What OpenMob is
"OpenRouter for agentic mobile access": an AI agent operates a real Android phone. Pieces:
- **The server** (`openmob/`, FastAPI): REST `/v1`, MCP at `/mcp`, the web dashboard (`openmob/static/`), and the
  agent loop in `openmob/agent/loop.py`;
- **the phone app** (`android/`, `dev.openmob.companion`): an Accessibility service plus an outbound WebSocket.

The MVP has two pillars:
1. connecting a phone is easy and secure;
2. agent access is secure and low-latency.

Judge new work against those two pillars; defer everything else. Start with `README.md`, `docs/architecture.md`,
`docs/safety.md`, `docs/phone-protocol.md` and `docs/design/phone-gatekeeper.md`.

## Two environments (see `docs/dev-environments.md`)
| | code | data | phone app |
|---|---|---|---|
| **standard** | `~/Desktop/openmob`, branch `main` | `~/.config/openmob` | built from main |
| **gatekeeper** | `~/Desktop/openmob-gatekeeper`, a worktree detached at `origin/gatekeeper-p1` | `~/.config/openmob-gatekeeper` | built from gatekeeper-p1 |

Use `scripts/om` to switch:
- `om run <env>` starts a server on port 8765, one at a time;
- `om phone <env>` installs that environment's app;
- `om build <env>`, `om update <env>` and `om status` do what they say.

The server and the phone app must come from the **same** environment; `om run` warns when they don't. The gatekeeper
build adds on-device enforcement (the phone judges every action with `rules.json`) on top of main.

## State at handoff
- **`main`** = `43e0347`. **`gatekeeper-p1`** = `80b2fee`, which is main's recent work plus the gatekeeper. Both are
  pushed. Tests: main 648 pytest; gatekeeper 964 pytest and 55 Kotlin (`./gradlew testDebugUnitTest`).
- **The owner's phone** (Samsung SM-S948U1, Android 15/16, adb serial `R5GL115W30H`) has the gatekeeper app
  `fc33f97`, one build behind. `80b2fee` is built (`~/.config/openmob-envs/apk/gatekeeper.apk`) but not installed.
  **Ask the owner before installing anything on their phone.**
- **The running server** may hold stale code. The owner restarts it with `om run gatekeeper`.

### Recent fixes (2026-10-06, all from real-phone testing)
1. **Permission dialogs (Android 14+)** hide from accessibility and **drop accessibility-injected taps**
   (`accessibilityDataSensitive`). The phone OCRs them (ML Kit) so the live view shows them. The loop dismisses
   them with **Back** automatically (`openmob/agent/permission_dialogs.py`); model taps on them are blocked.
   Owner decision: Back only, no owner prompt, never `isAccessibilityTool`.
2. **`wait_until`** (`openmob/agent/waiting.py`; phone method `wait_for`) replaces blind waits. Waits end early on
   a still screen.
3. **Archived apps** (Android 15) aren't treated as installed; `launch_app install_if_missing` opens the store.
4. **Free Play Store installs don't ask** (owner decision). Paid ones ask through the money rules.
5. **The dashboard feed** shows "Checked the installed apps (N found)" instead of the phone's app list.

**Lesson:** the emulator passed things that failed on the Samsung (permission-dialog taps). Verify on the real phone,
with the owner's OK, before calling a phone-side fix done.

## Open work, in priority order
1. **Owner re-test of the Chess.com flow** on `80b2fee`: "install chess and play 5 moves against a bot". Expect
   no install prompt and the notification dialog dismissed in about 1 s. Then compare step timings: the steps table
   in `<data>/history.sqlite` (columns observe_ms, model_ms, settle_ms, total_ms, wait_cond, source).
2. **Phase 5:** the test checklist on both builds (`docs/testing.md`), plus the "hacked server" suite on the
   emulator.
3. **Phase 6:** the 20-task benchmark (`bench/`; `openmob bench`). Targets (owner, 2026-10-08; see
   `docs/benchmarks.md`, "MVP latency targets"): ≥95% success and ≤1 prompt per typical task; first action on screen
   ≤2 s; ≤1.5 s per step; labeled-app navigation, first run: median ≤12 s, p90 ≤20 s; repeated navigation: median
   ≤6 s. Canvas apps (games, chess): reliability first, no latency target. Measure cold and warm runs separately,
   on at least two phones. Latency and navigation are the primary focus.
4. **Phase 7:** merge `gatekeeper-p1` into `main`, then deploy to Fly (`openmob.fly.dev`; the Dockerfile needs
   `deploy/OpenMob.apk`, which is gitignored: build it first).
   - **A batch security review comes first.** The owner wants reviews batched, not one per fix. Unreviewed since
     the last review:
     - permission dialogs dismissed with Back, and the model-tap block;
     - the gate holds every act, Back included, until a hidden dialog has been OCR'd;
     - free installs don't ask;
     - list_apps shows a count in the feed.
5. **Latency next:** each step costs about 2–3 s of model time. The next lever is several actions per model
   decision.

## Open owner decisions (don't decide these yourself)
- **"My people" phone memory** (`docs/` has no design yet): awaiting a yes.
- **rules.json word edits:** "submit"/"confirm" only with money context, whole-word "shop"/"store", location-on
  asks, and the owner's banks.
- Whether the developer console's session logs should also hide the app list.

## Rules the owner has set
- **Never print or expose API keys or secrets.** `config.yaml` holds a server token; don't commit it.
- Don't read production data (Fly) and don't read other processes' environments.
- **Agents must not drive the owner's real phone** (serial `R5GL115W30H`) without the owner's OK; use an emulator.
- If a permission check or safety block refuses an action, don't work around it; ask the owner.
- Commit and push only when asked. Each effort goes on its own branch from `main`.
- Keep `main` and `gatekeeper-p1` green: run the full pytest (and the Kotlin tests for Android changes) before
  merging.
- Two tests are known flaky (an orchestrator timing test is one); re-run before assuming a failure is real.

## Commands
```sh
.venv/bin/python -m pytest -q                       # Python tests (from the repo root)
cd android && ./gradlew testDebugUnitTest assembleDebug   # Kotlin tests and the APK
# JAVA_HOME=/opt/homebrew/opt/openjdk@17  ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
# To test the gatekeeper code from a scratch worktree: PYTHONPATH=<worktree> .venv/bin/python -m pytest -q
```
