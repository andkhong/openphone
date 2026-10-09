# AGENTS.md: handoff for coding agents (Codex and others)

Last updated 2026-10-08 (evening) by Claude Code, from git, `gh`, `fly status` and the day's session. The owner is
**andkhong**. They want plain, brutally honest answers, with measured results kept separate from estimates. In this
file, **measured** means a number somebody read off a run; everything else is an estimate or a report.

## What OpenMob is
"OpenRouter for agentic mobile access": an AI agent operates a real Android phone. Pieces:
- **The server** (`openmob/`, FastAPI): REST `/v1`, MCP at `/mcp`, the web dashboard (`openmob/static/`), and the
  agent loop in `openmob/agent/loop.py`; model providers in `openmob/agent/models.py`;
- **the phone app** (`android/`, `dev.openmob.companion`): an Accessibility service plus an outbound WebSocket.

The MVP has two pillars:
1. connecting a phone is easy and secure;
2. agent access is secure and low-latency.

Judge new work against those two pillars; defer everything else. Start with `README.md`, `docs/architecture.md`,
`docs/safety.md`, `docs/phone-protocol.md`, `docs/design/phone-gatekeeper.md` and `docs/benchmarks.md` ("MVP latency
targets"). The 2026-10-08 designs are `docs/design/deep-links.md`, `docs/design/app-links.md` and
`docs/design/fewer-model-calls.md`.

## Two environments (see `docs/dev-environments.md`)
| | code | data | phone app |
|---|---|---|---|
| **standard** | `~/Desktop/openmob`, branch `main` | `~/.config/openmob` | built from main |
| **gatekeeper** | `~/Desktop/openmob-gatekeeper`, a worktree detached at `origin/gatekeeper-p1` | `~/.config/openmob-gatekeeper` | built from gatekeeper-p1 |

Use `scripts/om` to switch:
- `om run <env>` starts a server on port 8765, one at a time. It serves that environment's own APK and loads model
  keys from `<data>/provider-keys.env`, which it **refuses unless the file is mode 600**; keys already in the
  terminal's environment also work. It warns when the Anthropic key or Jev's key is missing.
- `om phone <env>` installs that environment's app;
- `om build <env>`, `om update <env>` and `om status` do what they say.

The server and the phone app must come from the **same** environment; `om run` warns when they don't. The gatekeeper
build adds on-device enforcement (the phone judges every action with `rules.json`) on top of main.

Environment notes (2026-10-08):
- `~/.config/openmob` (standard) has **no** `provider-keys.env`; the gatekeeper data directory has one. Don't read
  either file.
- The owner's gatekeeper server runs from `~/Desktop/openmob-gatekeeper`, detached at the latest `gatekeeper-p1`
  (`217bda3` at this writing). The owner restarts it with `om run gatekeeper`. A server started in the background
  needs **SIGTERM** to stop; SIGINT doesn't do it.
- The phone has the **gatekeeper** app. `~/.config/openmob-envs/phone-app` records `gatekeeper 777cf52`, installed
  2026-10-08 21:05; `android/` is unchanged between `777cf52` and `217bda3`, so it is current.
- `replay_paths` is off by default and, per the session, on in the owner's gatekeeper `config.yaml` (not verified
  here: that file isn't to be read).

## State at handoff
- **`main`** = `459ecc8`: PRs #1–#22 merged on 2026-10-08/09 (#22 is the first-action polish, after the prod
  deploy). **`gatekeeper-p1`** = `4e158af`: main merged in, plus the gatekeeper-only commits. Both are pushed. The
  owner's server runs `4e158af` (gatekeeper; 1190 pytest passed there).
- **Production (Fly, `openmob.fly.dev`)**: image `openmob:main-53112da`, release v13, one machine in `sjc`, started,
  1/1 health check passing (`fly status -a openmob`, 2026-10-09 04:56 UTC). Deployed with the owner's OK; the owner
  is the only user. **The batch security review was deferred by the owner for that deploy.** Prod runs `main`, so
  no on-device gatekeeper.
- **Tests:** main **827 passed** (full run at `53112da`, 2026-10-08, 3 min 27 s); gatekeeper 1149 collected, not
  run here. Kotlin tests were not re-run (55 at the last count).
- **The owner's phone** (Samsung SM-S948U1, Android 15/16, adb serial `R5GL115W30H`): see the environment notes.
  **Ask the owner before installing anything on their phone.**

### Shipped 2026-10-08 (PRs #1–#21 on `main`; all also on `gatekeeper-p1`)
- **UI review fixes** (#1): honest invite-only access on the landing page, screen-data disclosure, a consent dialog
  before Android's Accessibility settings on every route (`docs/testing.md` has the checks).
- **Removed** (#2): saved-notes memory and server-side voice dictation. `openmob/voice/tts.py` remains.
- **Model providers** (#3): GPT-6 (Responses API), Gemini (thinking level), Qwen, Grok; Claude effort and thinking
  options.
- **`om` launcher** (#4): per-environment APK, `provider-keys.env` (see above).
- **Invite-only sign-up** (#5): web sign-up needs a one-time code from `openmob account invite` (works once, 7 days).
- **Haiku 5.5 effort by screen** (#6, then #15): low effort on labeled screens, `canvas_effort` (high) on canvas
  screens. The owner chose this after measuring.
- **Claude-only fallback** (#7): a failed provider falls back only to Claude; a missing Anthropic key warns.
- **`max_actions_per_turn`** (#8, off: default 1) and **`parallel_observe`** (#9, off; **measured** no gain).
- **Scroll settle 1.2 s** (#10, `scroll_settle_delay`; **measured**: no refused taps, fewer model calls, same task
  time as 0.8 s). Settle trace is opt-in.
- **Stale current app** (#11): `current_app` reports unknown instead of a stale app; a hidden page is left with Back.
- **Latency targets** (#12): `docs/benchmarks.md`.
- **Replay and plan-execute** (#13): `replay_paths` replays stored UI paths without the main model (off by default;
  `replay_store_api` lets benchmarks store paths from API tasks). `plan_execute` plans once with the model and
  executes with the classifier (off; **measured** slower on short tasks).
- **Settings deep links** (#14): phone method `open_settings {page}` (`docs/phone-protocol.md` §4.17, a fixed key
  table on the phone, `SettingsPages.kt` and `openmob/phone/settings_pages.py`), agent tool `open_settings_page`,
  `deep_links` switch (on; `OPENMOB_DEEP_LINKS=off` to compare). The settle no longer waits for a given app in front.
- **In-app links** (#16, #19, #20): the `open_link` catalog tool, a fast link step (`app_links_fast`), `open_url` as
  a step into its app, a Google search that lands in the browser counts as landed, a Play Store page by app name in
  one step (`docs/design/app-links.md`).
- **Loading check** (#17): ignores bare percentages (stock Settings shows "100%"); links wait for the new screen
  before settling.
- **`type_text` replace** (#18): the server asks the phone to replace a search or address box's old query
  (`docs/phone-protocol.md` §4.10, `replace: true`). On the gatekeeper build a replace needs its own permit
  (`777cf52`).
- **App names** (#21): "Google Maps" opens Maps, not Gmail (a label inside the name beats initials).
- **First-action polish** (#22, not on prod): plain Settings-page requests, an app's notification and app-info
  pages included, open before any classifier or model call (`deep_links_fast`); link steps end when their app is in
  front (the settle moves to the next step: an accounting change); HOME only off the launcher; wake time counted in
  step 1 `action_ms`. Gains are estimates until measured on the phone (`docs/design/deep-links.md` §6).

### Decisions (owner, 2026-10-08)
- **Clef dropped; Jev kept.** The Clef comparison build is archived as `origin/archive/clef-replacement`
  (`b49c733`, 2026-10-07). Nothing on `main` names Clef.
- **Latency targets adopted:** `docs/benchmarks.md`, "MVP latency targets". First action ≤ 2 s median; ≤ 1.5 s per
  step; labeled-app navigation first run median ≤ 12 s, p90 ≤ 20 s; repeat ≤ 6 s; ≥ 95 % success, ≤ 1 prompt per
  typical task; canvas apps reliability first.
- Carried over from 2026-10-06: permission dialogs are dismissed with Back only (no owner prompt, never
  `isAccessibilityTool`); free Play Store installs don't ask.

### Measured on the owner's Samsung, 2026-10-08 (the 12-task MVP benchmark, as reported by the main session)
- First run: median **4.7 s**, p90 **8.4 s**, **30/30** success. Against the targets (≤ 12 s / ≤ 20 s / ≥ 95 %): met.
- Repeat "Display": **1.7 s** (target ≤ 6 s: met).
- First action: **2.2 s** median (target ≤ 2 s: **missed** by 0.2 s; the latency lane below is on it).
- Chess, 5 moves against a bot: about **50–65 s** (canvas, no target).
- Not re-measured by this handoff. The older figures in `docs/benchmarks.md` (2026-10-05, 6.0 s Settings→Display)
  and in the design docs (14–21 s Settings tasks before deep links) predate the deep links and in-app links.

**Emulator (2026-10-08):** Settings tasks 1.5–4.7 s. App tasks are limited by the image (few real apps), so the
emulator measures navigation, not the app catalog.

### Lessons
- **The emulator and the Samsung differ.** Samsung hands Battery to Device care; stock Settings shows a bare
  "100%" that the loading check once took for a progress indicator; on 2026-10-06 the emulator passed permission-dialog
  taps that the Samsung dropped. Verify a phone-side fix on the real phone, with the owner's OK.
- **Benchmarks need a clean start state** (force-stop the app, then Home) and **must stop on a stuck screen**;
  otherwise one bad run poisons the medians.
- **Background `om run` servers need SIGTERM**, not SIGINT.
- **Running one environment's tests from another checkout:** Python puts the current directory first on the path, so
  a worktree's `openmob/` shadows the editable install. Use `python -I -m pytest ...` or run from that environment's
  own directory.

## Open work, in priority order
1. **Phase 5: the evil-server suite** rerun with new cases, plus the setup checklist (`docs/testing.md`). A lane is
   running on branch `test/phase5-evil-server` (worktree `.claude/worktrees/agent-a61cdb596b329c044`, at `217bda3`).
   The last recorded run is B7: 61/61 on an Android 15 emulator (`121f053`, 2026-10-06).
2. **The batch security review.** The owner wants reviews batched, not one per fix, and deferred it for the v13
   deploy. Unreviewed since the last review:
   - permission dialogs dismissed with Back, and the model-tap block;
   - the gate holds every act, Back included, until a hidden dialog has been OCR'd;
   - free installs don't ask; `list_apps` shows a count in the feed;
   - **new phone methods:** `open_settings` (§4.17) and `type_text` with `replace` (§4.10), and their gatekeeper
     permits;
   - the `open_link` catalog and `open_url` as a step into its app (handler resolved on the phone, no explicit
     package);
   - replay's stored paths (`replay_paths`, `replay_store_api`): what is stored, and replaying without the model;
   - invites (`openmob account invite`, the login link with the code);
   - effort switching by screen (`canvas_effort`), the Claude-only fallback, and `provider-keys.env` loading.
3. **Merge `gatekeeper-p1` into `main`, then redeploy** to Fly (`openmob.fly.dev`; the Dockerfile needs
   `deploy/OpenMob.apk`, which is gitignored: build it first). Prod has no gatekeeper until then.
4. **The chess board tool** (canvas play: reliability first).
5. **Latency polish.** Merged as #22; measure it on the Samsung (link, Settings and Chrome-notification tasks).
   An **MCP drive level** lane is running (`feature/mcp-drive`: step-by-step tools for outside agents, every act
   through the same checks). Each step still costs about 2–3 s
   when the main model decides; the levers that worked were deep links, in-app links and replay, not more actions
   per decision (`max_actions_per_turn` and `parallel_observe` stayed off).

## Open owner decisions (don't decide these yourself)
- **"My people" phone memory** (`docs/` has no design yet): awaiting a yes. Saved-notes memory was removed in #2;
  this is the separate, on-phone design.
- **rules.json word edits:** "submit"/"confirm" only with money context, whole-word "shop"/"store", location-on
  asks, and the owner's banks.
- Whether the developer console's session logs should also hide the app list.
- **Worktree and branch cleanup:** `docs/research/cleanup-plan-2026-10-08.md` lists every worktree, branch and
  stash with a recommendation; nothing is deleted without the owner's word.

## Rules the owner has set
- **Never print or expose API keys or secrets.** `config.yaml` holds a server token; don't commit it.
  `provider-keys.env` holds model keys; don't read it.
- Don't read production data (Fly) and don't read other processes' environments. `fly status` is fine; logs and
  data are not.
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
# To test the gatekeeper code from a scratch worktree: PYTHONPATH=<worktree> .venv/bin/python -I -m pytest -q
om run gatekeeper | om run standard                 # the owner's server (one at a time, port 8765)
openmob account invite                              # a one-time sign-up code (prints a login link)
openmob timings --since 24h                         # step phases from history.sqlite
openmob bench tasks.yaml --serial emulator-5554     # the adb benchmark harness (docs/benchmarks.md)
fly status -a openmob                               # deployment status only
```

Full pytest on `main` at `53112da`, 2026-10-08 (this handoff's run, **measured**): 827 passed in 3 min 27 s, no
failures, no skips reported. `gatekeeper-p1` was collected (1149) but not run here.
