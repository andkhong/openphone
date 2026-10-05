# OpenMob: Learning and Agent-Improvement Architecture (Architect A)

Date: 2026-10-04. Scope: how a user's OpenMob agent gets better at *their* phone, apps and games
through conversation, demonstrations, corrections and feedback; how that is stored, retrieved,
shared (or not) and evaluated; and what the platform/API/MCP layer (Architect B) must expose.

**Thesis in one paragraph.** Do not fine-tune anything in the next year. Every result that matters
for a product like this (AppAgent, Mobile-Agent-E, AWM, MobileGPT, AutoDroid, ChatGPT memory, Claude
Skills) improves the agent *in context*: small, typed, human-readable knowledge that is retrieved at
the right moment. OpenMob should build four stores, in this order: **(1) structured trajectories**
(the raw material, partly logged today), **(2) memory** (user facts + per-app notes), **(3) skills**
(named, parameterized procedures in SKILL.md form, optionally compiled to fast macros), and
**(4) game routines** (deterministic perception-action state machines that an LLM writes and
supervises but does not drive frame by frame). Everything is account-owned, user-visible,
editable, exportable and deletable, and a learned thing is a *candidate* until evidence promotes it.
"Teaching a game" is real for menu/idle/turn-based play and for scripted chores inside action
games; it is not real for reflex-level real-time combat, and the product must say so.

## Revisions after review (round 2, after reading Architect B's platform design)

Full critique and the merged interface table: `review_A_of_B.md`. Changes made to this document:

1. **Adopted B's `Principal` / `V1Service` as the only entry point.** Every learning read or write
   from REST, MCP, the dashboard or the Learner goes through it. This covers §3.4 and §5.
2. **Context keyed by `(account, principal, conversation_id)`** (B §3.6). ContextBuilder (§3.2 C3)
   injects facts into a third-party principal's tasks only with `memory:read`. It uses user skills
   only with `skills:run`, and draws exemplars only from that principal's own trajectories. This
   fixes the `Agent.recent` leak that B found and that my v1 had missed.
3. **Confirmations are approved on the phone, never by the caller** (B §3.3). This now applies
   inside skills, macros and routines too. Budget grants for risky skills (§3.6) can only be
   approved on the phone, and `confirm:approve` never satisfies them.
4. **Scopes renamed to B's set**, keeping write semantics safe (§5.1):
   - `memory:write` from non-owner principals creates *proposals*, so `memory:propose` is dropped.
   - Feedback is folded into `tasks:run`, so `feedback:write` is dropped.
   - Two sensitive scopes are added: `teach:record` and `routines:run`.
5. **Ownership and namespaces** (B §5.1): `namespace` (`user` | `client:<id>`) and `client_id`
   are added to facts, app_notes, skills and routines (§3.3). Revoking a grant deletes that
   client's namespace unless the user keeps it.
6. **Routines are a skill executor, not a separate API object.** Skills have
   `executor: guide | macro | routine`. A routine run is an ordinary task (`run_task` with
   `skill_id`), so B's long-poll, cancel and audit apply unchanged. The separate `/routines`
   endpoints are removed (§5.2).
7. **B's MCP `teach` tool is split** into `save_skill`, `give_feedback` and `start_teaching` /
   `stop_teaching`. A `steer_task` tool is added (§5.3).
8. **Storage moves to Postgres** with B's S0.5 migration. Template crops go to object storage. The
   phase-0 SQLite tables are written portable (§3.3, §6).
9. **Distribution verified against the official policy text** (§3.8). The section now quotes the
   policy and lists the realistic options. New dependency: Android developer verification (from
   Sept 30 2026 in 4 countries, global from 2027). New option: an adb/shell-helper control backend,
   which also unlocks capturing game demonstrations played on the phone (§3.2 C7 Path C, moved up).
10. **Game example and limits** (§4): the "play Subway Surfers" style promise in B's journey is
    called out as infeasible. The routine executor must run where the frames are: on the machine
    holding the phone socket (fly-replay co-location) or on the device.
11. **New risks recorded in §3.6:**
    - poisoning through third-party agents and model-A traces
    - Sybil poisoning of any global AppKnowledge, which is deferred
    - personal strings leaking through skills
    - deletion semantics for derived knowledge

---

## 1. Current state review (what exists, what's missing)

### 1.1 What exists that learning can build on

| Area | Where | What it gives us |
|---|---|---|
| Agent loop | `openmob/agent/loop.py:115-215` (`Agent.run`/`_run`) | One task = a fresh `messages` list: request text + observation (screenshot + UI elements/OCR + Jev assessment), then one tool call per turn. Clean injection points: `_request_text` (`loop.py:221`), `_obs_blocks` (`loop.py:289`), `system_prompt()` (`tools.py:196`), `TOOLS` (`tools.py:17`). |
| Short-term memory | `loop.py:105` `self.recent = deque(maxlen=5)`, filled at `loop.py:132`, rendered at `loop.py:221-225` | Last 5 `(request, summary)` pairs, per phone, in RAM. Restored from a chat on reopen (`runner.py:326-344`, `history.py:205`), wiped by "New chat" (`runner.py:314-324`). |
| Tool vocabulary | `tools.py:17-147` | launch_app, list_apps, open_url, tap_element, tap, long_press, scroll, swipe, type_text, press_key, wait, ask_user, done. Coordinates are *screenshot pixels* mapped by `Screenshot.to_device` (`phone/adb.py:60`). |
| Safety guard | `loop.py:439-475` `_guard` | Keyword + Jev risk score → confirmation card unless the request asked for it; `state.approved` one-shot after a "yes". Every learned macro must keep passing through this. |
| Sensitive-app block | `config.py:34`, `loop.py:152,353`, `api_v1.py:248` | API tasks stop if a banking/password app is in front. Reuse the same predicate as "never learn here". |
| Screen classifier | `openmob/jev.py` | Text-only, fast, calibrated: screen state (loading/popup/tap_to_start/menu…), task_done, tap risk, app pick. Already the "fast perception for menus" piece; useful as a cheap gate for routines and reflection. |
| History | `openmob/history.py:25-63` | `chats → turns(request, model, success, summary, steps, usage, feedback, feedback_note) → events(kind, message, data)`. Account-scoped (`history.py:97-100`), cascade delete, retention purge (`history.py:197`), owner-only file mode. Docstring (`history.py:1-9`) already promises "look up what worked for similar requests, learn app-specific quirks, and measure success rates". |
| Feedback | `server.py:417-424`, `static/index.html:432-440` | 👍/👎 per turn stored via `History.set_feedback`. |
| App naming memory | `phone/apps.py:70-130`, `config.py:97` | `aliases` from config.yaml, a label cache, Jev/LLM picker. A primitive, global, file-based "user memory". |
| Live video | `screencast.py`, `android/.../ScreenStreamer.kt` | Phone already encodes H.264 and pushes it to the server when a viewer is open. This is the frame source a server-side game executor can reuse. |
| Phone RPC | `docs/phone-protocol.md`, `PhoneActions.kt:53-87` | `screenshot, ui_elements, current_app, tap, long_press, swipe, type_text, key, open_url…` over one WebSocket. Extensible with new methods (protocol stays v1 if optional). |
| Developer API | `api_v1.py`, `ledger.py`, `accounts.py:34` (`SCOPES`) | Scoped keys, 404-not-403 isolation, audit log with instruction *hashes*, tiers, credits, webhooks. The MCP surface (B) will sit on this. |

### 1.2 Gaps that block learning (concrete)

1. **Steps are not stored structurally.** `history.record` (`history.py:126-156`) stores each
   action only as its display string, e.g. `"tap_element: Tapped [3] 'Send'."` (`loop.py:192`). The
   tool input (x/y, index, text), the foreground package, the element's resource-id/bounds, the
   screen size and the duration are lost. You cannot replay, distill, or evaluate from this.
   Package isn't on the turn either.
2. **Feedback has no consumer.** `turns.feedback` is written (`history.py:212-225`) and never read
   by the agent. The UI sends only the value, not `feedback_note` (`index.html:440`), so a 👎 says
   *that* it failed, never *why*.
3. **No mid-task correction channel.** While a task runs, anything the user says that isn't an
   answer to a pending question becomes a *new queued task* (`runner.py:90-100`). "No, the other
   button" cannot reach the running agent. Corrections are the highest-value learning signal and
   currently can't be expressed.
4. **No cross-chat memory.** `Agent.recent` is per phone, in RAM, 5 items, request/summary only,
   cleared on New chat. Nothing survives across chats except aliases in config.yaml.
5. **Static system prompt, no prompt caching.** `messages.create` (`loop.py:156-162`) sends the
   system prompt and tools every step with no `cache_control`. Adding memory/skills to the prefix
   without caching would raise per-step cost; with caching it's close to free.
6. **No OCR when hosted.** OCR is Apple Vision, macOS-only (`vision/ocr.py:1-30`, `available()`
   returns False off darwin). The hosted server runs on Fly (Linux), so in hosted mode a game gives
   the agent *no text at all* and Jev nothing to classify. Any game learning in hosted mode needs
   on-device OCR (ML Kit) or Linux OCR first.
7. **Game frames are not observable fast.** The agent loop uses one RPC screenshot per step
   (Android's `takeScreenshot` is rate-limited, ~3/s, `phone-protocol.md:283`), plus a model call
   (seconds). Real-time play is explicitly out of scope today (`README.md:159`).
8. **Element anchors are thin.** `PhoneActions.element()` (`PhoneActions.kt:239-257`) sends text,
   desc, resource_id, class, clickable, checked, bounds; it doesn't send `isPassword`,
   `isEditable`, or a stable ancestor path. Macros need anchors that survive layout shifts, and
   redaction needs `isPassword`.
9. **Accessibility service listens to almost nothing.** It subscribes only to
   `typeWindowStateChanged|typeWindowsChanged` (`res/xml` accessibility config) and uses them for
   the foreground package (`OpenMobAccessibilityService.kt:24-31`). Recording a user demonstration
   needs `typeViewClicked`, `typeViewTextChanged`, `typeViewScrolled`, `typeViewFocused`.
10. **Ownership granularity.** History rows are keyed by `account_id` + `device_id`; there's no
    notion of app version, screen resolution, or "who taught this" (user, dashboard, API key),
    which learned artifacts need.

---

## 2. Research findings

Legend: **[P]** proven (primary paper / official docs), **[S]** secondary or not verified by us,
**[Spec]** our interpretation. Numbers are as reported by the authors; benchmarks differ, so
compare deltas within a paper, not across papers.

### 2.1 What makes GUI agents improve (the evidence)

| Lever | Evidence | Effect |
|---|---|---|
| Action-space abstraction | AppAgent ablation, 45 tasks / 9 apps [P] https://arxiv.org/html/2312.13771 | raw actions 2.2% → simplified 48.9% success |
| Per-app knowledge docs | same paper [P] | no docs 48.9% → exploration docs 73.3% → **docs from human demos 84.4%** → hand-written docs 95.6% |
| App memory from exploration | AutoDroid (MobiCom'24) [P] https://arxiv.org/pdf/2308.15272 | 71.3% task success, +39.7 pts over GPT-4 baseline |
| Reusable sub-tasks | MobileGPT (MobiCom'24) [P] https://arxiv.org/html/2312.03003v3 | on repeats: latency −62.5%, cost −68.8%; adapts learned tasks 98.75% |
| Tips + Shortcuts (self-evolving) | Mobile-Agent-E [P] https://arxiv.org/html/2501.11733v1 | satisfaction 75.1% → 86.9% with evolution; Tips alone +7.8 (GPT-4o) / +9.6 (Claude 3.5 Sonnet) pts; 58 Tips + 7 Shortcuts after 25 tasks; Shortcuts ≈14% of actions |
| Evolved shortcuts (efficiency) | AppAgentX [P] https://arxiv.org/abs/2503.02268 | steps 9.1 → 5.7, time/tokens up to −47%, accuracy held |
| Abstract workflows vs raw trajectories | AWM (ICML'25) [P] https://arxiv.org/html/2409.07429v1 | WebArena 23.5% → 35.5%; abstract workflows beat retrieved concrete trajectories (Synapse-style) and human-written workflows |
| Verified programmatic skills | ASI [P] https://arxiv.org/html/2504.06821v2 | +23.5% vs static agent, +11.3% vs *text* skills, 10–15% fewer steps; **verification before storing is the key** |
| Skill synthesis by exploration | SkillWeaver [P] https://arxiv.org/pdf/2504.07079 | +31.8% rel. WebArena; skills transfer to weaker agents (+54.3%) |
| Exemplar memory | Synapse (ICLR'24) [P] https://github.com/ltzheng/synapse | +53% rel. step success on Mind2Web |
| Demo refinement into abstractions | ICAL (NeurIPS'24) [P] https://arxiv.org/abs/2406.14596 | VisualWebArena 14.3% → 22.7% |
| Planner/reflector split | Mobile-Agent-v2 [P] https://arxiv.org/abs/2406.01014; MobileUse [P] https://arxiv.org/abs/2507.16853 | v2 >30% over v1; reflection prevents loops; MobileUse 62.9% AndroidWorld |
| Personal memory for mobile agents | MobiMem [P] https://arxiv.org/abs/2512.15784 | up to +50.3% success, up to 9× lower latency |
| Tool accumulation | OS-Copilot/FRIDAY [P] https://arxiv.org/abs/2402.07456v2 | GAIA L1 30.3% → 40.86% |
| Skill library (games) | Voyager [S] (−8.3 pts ablation unverified) | |
| RL fine-tuning / scale | UI-TARS-2 [P] https://arxiv.org/abs/2509.02544 (AndroidWorld 73.3); GUI-Owl / Mobile-Agent-v3 [P] https://arxiv.org/html/2508.15144v1 (73.3); Agent S3 best-of-N [P] https://arxiv.org/abs/2510.02250v1 | Biggest absolute benchmark gains in 2025–26 came from training and test-time scaling, not memory |

**Proven, and relevant to OpenMob:**
- Per-app knowledge is the largest cheap lever (+25–40 pts), and **knowledge from human
  demonstrations beats self-exploration** (+11 pts in AppAgent). This justifies teach mode.
- **Abstracted, verified procedures** beat raw trajectory replay (AWM, ASI). Store SKILL.md-style
  workflows plus run statistics, not just transcripts; keep trajectories as raw material/exemplars.
- **Reusing learned procedures mainly saves steps, time and money** (MobileGPT, AppAgentX:
  −37% to −69%). Users feel this as "it got faster at my stuff".
- Reflection prevents loops but is not itself memory; lessons must be written somewhere (Tips).

**Speculative / not transferable:**
- Benchmarks are saturating (AndroidWorld leaderboard entries self-report ~95–97% [S]
  https://benchlm.ai/md/benchmarks/androidworld.md); per-user, long-tail apps and games are where
  OpenMob's gains will be, and no benchmark measures that.
- Fine-tuning gains come from 10k+ trajectories (AndroidLab fine-tuning on 10.5k traces: 4.6% →
  21.5% for open models [P] https://arxiv.org/abs/2410.24024v2; AndroidControl shows poor
  out-of-domain scaling [P] https://arxiv.org/abs/2406.03679). A single user won't produce that.

### 2.2 How production assistants improve per user

- **ChatGPT**: explicit "saved memories" plus implicit "reference chat history", each with its own
  toggle; Temporary Chat neither reads nor writes memory [S]
  https://openai.com/index/memory-and-new-controls-for-chatgpt/. A 2026 automatic "Dreaming"
  memory-refresh with an editable summary page is reported [S, unverified]
  https://gigazine.net/gsc_news/en/20260605-chatgpt-memory-dreaming-v3.
- **Claude app memory**: topic-structured, view/edit/delete in Settings, Pause vs Reset, scoped per
  Project, incognito chats excluded, import/export; deleting a chat does **not** delete memories
  derived from it [P] https://claude.com/blog/memory, https://support.claude.com/en/articles/11817273.
  (OpenMob should decide this explicitly; I recommend provenance links so the user can choose.)
- **Claude API memory tool** (`memory_20250818`): client-side file operations under `/memories`
  that the app maps to per-user storage; docs require path-traversal checks, size caps, expiry and
  stripping sensitive data [P] https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool.
  Anthropic reports memory + context editing +39% on an internal agentic eval, and 84% fewer tokens
  on a 100-turn task [P] https://www.anthropic.com/news/context-management.
- **Agent Skills**: folder with `SKILL.md` (YAML `name`, `description`), progressive disclosure:
  only metadata in context, body loaded when relevant [P]
  https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills.
- **OpenAI Responses/Conversations**: server-side conversation state (`previous_response_id`,
  Conversations objects; responses kept 30 days, `store:false` to opt out) [P]
  https://developers.openai.com/api/docs/guides/conversation-state. That is state, not learning.
- **Takeaway [Spec]**: the products converge on (a) a small, typed, user-editable memory injected
  into context, (b) on-demand procedural knowledge (skills), (c) an incognito switch, (d) explicit
  user control. OpenMob's design copies (a)–(d) and adds what phones need: per-app keying and
  run-statistics-based trust.

### 2.3 Games

- **LLM step loops are too slow for real-time play** [P]: Orak measured 19.6–99.2 s per step for
  reflective agents in StarCraft II and every model scored 0 in real-time hard mode
  (https://arxiv.org/html/2506.03610); VideoGameBench best models finished 0.48% (1.6% with
  pausing) (https://arxiv.org/abs/2505.18134v2); BALROG NetHack ~1–1.6%
  (https://arxiv.org/abs/2411.13543v2). OpenMob's own loop is ~2–6 s/step (model call + settle).
- **Turn-based works, slowly** [S]: Claude/Gemini Pokémon runs took hundreds of hours with custom
  harnesses (https://techcrunch.com/2025/02/25/anthropics-claude-ai-is-playing-pokemon-on-twitch-slowly,
  https://www.cryptopolitan.com/googles-gemini-just-beat-pokemon-blue/).
- **What does work in real time** [P]: (1) a slow planner + fast executor split. Lumine reads pixels
  at 5 Hz, acts at 30 Hz and reasons only when needed; it completed a 5-hour Genshin storyline
  (https://arxiv.org/abs/2511.08892v1). Latent Bridge (1.5 s planner, 15 Hz controller) helps only
  when slow reasoning already beats reaction (https://arxiv.org/abs/2606.24470). UI-TARS-2 reaches
  ~60% of human level on a 15-game suite after heavy RL. These are trained models, not prompts.
  (2) Classic deterministic bots: MAA (Arknights) uses OpenCV template matching + PaddleOCR + small
  ONNX models over ADB/minitouch (https://github.com/MaaAssistantArknights/MaaAssistantArknights);
  Alas (Azur Lane) similar, 0.3–1 s per screenshot (https://github.com/LmeSzinc/AzurLaneAutoScript).
  This is the proven pattern for mobile-game chores and loops.
- **Learning from human play** [P]: VPT (70k h of video + inverse dynamics,
  https://openai.com/index/vpt) and NVIDIA NitroGen (40k h across 1,000+ games,
  https://arxiv.org/abs/2601.02427). Behaviour cloning needs orders of magnitude more data than
  one user produces, so per-user BC for reflex play is out [Spec].
- **Terms of service** [P]: Supercell bans "automation software, emulators, bots"
  (https://supercell.com/en/terms-of-service/en/2024-09-01/); Pokémon GO treats scripts/third-party
  software as cheating with strikes and instant bans
  (https://niantic.helpshift.com/hc/it/6-pokemon-go/faq/525-violating-the-terms-of-service/).
  Assume any online game (including NetEase titles) forbids it [Spec].

### 2.4 Platform policy (affects the whole product, not just learning)

- **Google Play AccessibilityService policy** [P]: "Any use of the Accessibility API that enables
  an app to autonomously initiate, plan, and execute actions or decisions is strictly prohibited";
  deterministic "if X then Y" automation remains allowed; announced Oct 30 2025, effective
  Jan 28 2026; prominent disclosure + declaration still required
  (https://support.google.com/googleplay/android-developer/answer/10964491,
  https://support.google.com/googleplay/android-developer/answer/16585319).
  **[Spec] Consequence:** the companion app as an LLM agent driving other apps via accessibility is
  very likely not distributable on Play; sideloading/F-Droid/enterprise, ADB, or Android
  AppFunctions (Android 16+, experimental [S] https://www.infoq.com/news/2026/03/android-appfunctions-agents)
  are the paths. Interesting twist: a *user-taught deterministic routine* ("if template X, tap Y")
  is closer to the allowed category than the LLM loop. Flag for the user and Architect B.

### 2.5 Demonstrations on Android

- Accessibility events give *semantic* events (TYPE_VIEW_CLICKED, TEXT_CHANGED, SCROLLED,
  WINDOW_STATE_CHANGED, TOUCH_INTERACTION_START/END) with the source node, not touch coordinates
  [P/Spec] https://developer.android.com/reference/android/view/accessibility/AccessibilityEvent.
  API 34 added motion-event source observation, but touch exploration changes how the user
  interacts [P] https://developer.android.com/sdk/api_diff/34/changes/android.accessibilityservice.AccessibilityServiceInfo.
  So for games (one SurfaceView), on-phone capture without ADB is effectively unavailable; capture
  through our own input path (dashboard remote control) is the clean route [Spec].
- Coordinate record-and-replay is fragile: RERAN (getevent/sendevent) needs a shell
  (https://2013.icse-conferences.org/content/reran-timing-and-touch-sensitive-record-and-replay-android.html);
  a 2025 study found 4 tools failed to replay 17% of feature scenarios
  (https://arxiv.org/abs/2504.20237); SARA adapts across screen sizes
  (https://conf.researchr.org/details/issta-2019/issta-2019-Technical-Papers/7/SARA-Self-replay-Augmented-Record-and-Replay-for-Android-in-Industrial-Cases).
  → macros must be anchor-based with LLM fallback, never blind coordinate replay.
- Datasets for reference: AitW 715k episodes [P]
  https://research.google/pubs/android-in-the-wild-a-large-scale-dataset-for-android-device-control/;
  AndroidControl 15k demos; GUI-Odyssey 8.3k cross-app episodes [P] https://arxiv.org/abs/2406.08451.

### 2.6 Evaluation

- AndroidWorld: 116 parameterized tasks with state-based checkers [P]
  https://arxiv.org/html/2405.14573v3; near-saturated by self-reported leaderboard entries [S].
  Good as a *platform regression* suite, useless for per-user improvement.
- AndroidLab (138 tasks) [P], MobileAgentBench (100 tasks) [P] https://arxiv.org/abs/2406.08184v1,
  A3 (201 tasks, LLM evaluator) [P] https://arxiv.org/html/2501.01149v1.
- Offline step accuracy only moderately predicts online success (R² ≈ 0.48 offline vs 0.62
  semi-online) [S, source unconfirmed] https://arxiv.org/pdf/2512.22047. → use offline replay as
  a regression *filter*, live success rates as the truth.

### 2.7 Safety

- Mobile prompt injection is effective: AEIA-MN up to 93% attack success on AndroidWorld
  (https://arxiv.org/abs/2502.13053); adversarial pop-ups clicked 86% of the time, "ignore pop-ups"
  instructions ineffective (https://arxiv.org/abs/2411.02391); MobileWorldSafety 40–67% attack
  success on real apps (https://arxiv.org/abs/2608.17659). [P]
- **Memory poisoning** is the learning-specific risk: MINJA injected records 98.2% of the time via
  ordinary queries (https://arxiv.org/html/2503.03704v2); AgentPoison ≥80% at <0.1% poison rate
  (https://arxiv.org/abs/2407.12784); MemoryGraft shows poisoned "successful experiences" get
  retrieved and imitated across sessions (https://arxiv.org/abs/2512.16962), which is exactly a
  Tips/Shortcuts store. SpAIware wrote persistent instructions into ChatGPT memory [S]. [P]
- Trajectory privacy: screenshots leak rich PII and risk accumulates over a trajectory (GUIGuard,
  https://arxiv.org/abs/2601.18842v1) [P]. Supports the "no screenshots, abstracted skills"
  default.

---

## 3. Architecture

### 3.1 Principles (opinionated)

1. **In-context learning first, weights never (for now).** Memory, app notes, skills and
   exemplars, retrieved per task. Fine-tuning needs screenshots we deliberately don't keep, data
   volumes a single user never produces, and an eval harness we don't have. Revisit only for a
   small shared grounding/perception model, and only with opt-in data.
2. **Typed knowledge, not a vector soup.** Four kinds, each with a clear trigger and retrieval key:
   *facts* (about the user), *app notes* (about an app), *skills* (how to do a named thing),
   *routines* (how to run a loop fast). Retrieval is mostly by **key** (foreground package, skill
   name) and only secondarily by similarity. This is what MobileGPT/AutoDroid/Mobile-Agent-E do,
   and it's debuggable.
3. **The user can see and edit everything the agent learned.** A "What I've learned" page per
   account, per app. Like ChatGPT/Claude memory: view, edit, delete, pause, export.
4. **Learned ≠ trusted.** Every artifact has `status` (`candidate → active → disabled`),
   `provenance` (user said / demo / reflection / API key), and run statistics. Auto-learned items
   (reflection) are candidates until they succeed N times or the user approves. Screen text is
   never allowed to write memory without the user's confirmation (prompt-injection → memory
   poisoning).
5. **Learning never widens permissions.** A skill or routine can't skip `_guard`, can't open a
   sensitive app, and can't carry a pre-approval for risky taps beyond an explicit, scoped,
   user-granted budget.
6. **LLM plans; deterministic code executes the fast part.** For games and repetitive flows the
   LLM writes and supervises a routine/macro; a cheap executor runs it.

### 3.2 Components

```
                ┌──────────────────────────── Server (FastAPI) ────────────────────────────┐
 Dashboard /    │  Coordinator ──► Agent loop ──► ContextBuilder ◄── KnowledgeStore         │
 phone app /    │     │   ▲          │  ▲            (facts, app notes, skill index,        │
 API / MCP ────►│     │   │ steer    │  │ remember/   exemplars; prompt-cached prefix)     │
                │     │   │          │  │ use_skill                                       │
                │     ▼   │          ▼  │                                                 │
                │  TrajectoryRecorder ─► steps table ─► Learner (async jobs):             │
                │  (structured steps,                     • distill skill from turn/demo  │
                │   redaction)                            • reflect on 👎 / correction    │
                │                                         • app-note extraction           │
                │  DemoRecorder ◄── dashboard remote      • promote/demote by stats       │
                │                   control + a11y events                                  │
                │  MacroRunner (anchored replay, LLM fallback)                             │
                │  RoutineSupervisor ──► RoutineExecutor (server: H.264 frames → OpenCV)   │
                │  Evaluator (per-app metrics, replay suites, canaries)                    │
                └───────────────────────────────┬──────────────────────────────────────────┘
                                                │ WebSocket (phone protocol v1 + optional methods)
                ┌───────────────────────────────▼─────────── Phone (Android app) ──────────┐
                │ PhoneActions (+ ocr via ML Kit, + record_start/stop, + routine_run/stop) │
                │ A11y event recorder (teach mode) · ScreenStreamer (H.264)                │
                │ On-device RoutineExecutor (phase 4b): capture → detect → dispatchGesture │
                └──────────────────────────────────────────────────────────────────────────┘
```

**C1. TrajectoryRecorder** (server). Wraps `Agent._execute`. For each tool call writes a `steps`
row: tool, args (screenshot px *and* normalized 0-1 device coords), foreground package, app
version, the element anchor if `tap_element` (text/desc/resource_id/class/bounds/index), a
**screen signature** (hash of sorted element labels + package; for treeless screens a 64-bit
perceptual hash of the screenshot, which is not reversible to content), Jev state, outcome text,
is_error, latency. Redaction: `type_text` values are stored only if the field is not a password
and the app isn't sensitive, else `"<redacted>"`; in the skill distiller they become parameters.
No screenshots (unchanged policy) except the opt-in cases in 3.6.

**C2. KnowledgeStore** (server; SQLite `learning.sqlite` in phase 0-1, moving to Postgres with B's S0.5 migration; template crops in object storage; account-scoped like `history.py`, and namespaced `user` | `client:<id>` per B).
Holds facts, app notes, skills (+versions), routines, templates, demonstrations, proposals and
run stats. Data model in 3.3.

**C3. ContextBuilder** (server). Replaces the ad-hoc `_request_text`. Keyed by `(account, principal, conversation_id)` (B §3.6): the owner's dashboard/keys get everything; a third-party principal gets facts only with `memory:read`, the user's skills only with `skills:run`, its own `client:<id>` namespace always, and exemplars only from its own trajectories. Builds:
- *Cached prefix* (system, `cache_control`): base prompt + **user facts** (≤ ~40 short lines) +
  **skill index** (name + one-line description + app, ≤ ~50 entries, progressive disclosure as in
  Anthropic Agent Skills). Stable across steps and tasks, so it caches.
- *Request block*: recent requests (existing) + top-k **exemplars** (≤3 compressed successful
  trajectories for the most similar past requests on this account, AWM/Synapse-style, as
  step lists, never screenshots).
- *Per-observation block*: when the foreground package changes, append that app's **app notes**
  (≤ ~25 lines: where things are, pop-ups to dismiss, quirks, user's preferences in this app) to
  the next observation text. Keyed by package (+ version range), so cost is paid only in that app.

**C4. New agent tools** (in `tools.py`):
- `use_skill(name, params)` → returns the skill's full procedure text (the agent then follows it)
  *or*, if the skill has a compiled macro and the user/caller allowed fast mode, runs it via
  MacroRunner and returns the outcome. Mirrors Agent Skills' "load on demand".
- `remember(kind, text, app?)` → creates a **proposal** (fact or app note). The dashboard shows a
  small "Remember this? ✓ / ✕" card; the phone app can speak it. Auto-accept only when the user's
  own utterance in this turn contained an explicit "remember / learn / from now on / always /
  never" (then it's the user's statement, not screen text).
- `note_app(text)` → shorthand for an app note on the current package (same proposal flow).
- `run_routine(name, params, max_seconds)` → hands control to the routine executor (games), blocks
  until it ends or escalates, returns a summary.
The `ask_user` and `done` tools are unchanged. `done` gains an optional `learned` field where the
agent can propose a note ("Pop-up 'Daily Sign-in' appears on first launch; close with X top-right").

**C5. Steer channel** (Coordinator). While a task runs and no question is pending, a user
utterance that isn't "stop" is delivered to the running task as a **correction** (new
`Coordinator.steer(text)` → a queue the agent drains at the top of each step and appends as a user
text block: `"User correction: …"`), instead of being queued as a new task (`runner.py:97-100`).
A UI affordance ("➕ queue as new task") keeps the old behavior. Every correction is logged with
`kind="correction"` and becomes a reflection input.

**C6. Learner** (server, async, cheap model e.g. Haiku-tier; never on the hot path):
- *Distill* (on "learn this", "save as skill", 👍 on a ≥3-step success, or a finished demo):
  steps → SKILL.md draft: name, description, app, params (from redacted/typed values), numbered
  procedure in UI terms ("tap the scroll icon top-right (Menu)"), success check, known pitfalls,
  and a compiled macro if every step has a robust anchor.
- *Reflect* (on 👎, failure, or correction): given the turn's steps, correction text, and the
  feedback note, output ≤3 proposals: edit app note, edit skill step, add a fact. Proposals are
  shown to the user ("I'll remember that 'Archive' in Gmail is the box icon, not the trash. ✓/✕").
  Mobile-Agent-E's Tips (reflection → reusable guidance) and Shortcuts (→ skills) are the model.
- *Promote/demote*: candidate skills/notes become active after ≥3 uses with Wilson lower bound
  success ≥ the app's baseline, or immediately on user approval; active ones drop to candidate
  after 3 consecutive failures, and the user is told ("My 'claim dailies' routine broke after the
  game updated; want to re-teach it?").

**C7. DemoRecorder** (teach mode). Two capture paths, because the accessibility API can't see raw
touches in other apps:
- **Path A, dashboard remote control (primary, works for games).** The user drives the phone from
  the dashboard's live view (click = tap, drag = swipe, keyboard = type). We forward the gesture
  over the existing RPC *and* record it with the current frame's signature and, in teach mode
  only, a JPEG at each action. We own the input path, so capture is exact. Cost: new live-view
  input handling (we already have the frame stream and `tap/swipe` RPCs).
- **Path B, on-phone a11y event recording (normal apps).** The user taps on the phone itself.
  The service subscribes to `TYPE_VIEW_CLICKED / LONG_CLICKED / TEXT_CHANGED / SCROLLED /
  WINDOW_STATE_CHANGED` and sends `{event, package, node anchor, bounds, text(redacted if
  password), time}` while a "Teaching…" notification is up. Gives semantic events ("clicked
  'Archive'"), not coordinates; useless for games (one SurfaceView, no click events).
- **Path C (power users, games on the phone itself): shell-helper `getevent`** raw touch capture.
  Needs wireless-debugging pairing (Shizuku-style helper). The same helper is the distribution
  alternative in §3.8, so build it once for both; promoted from "deferred" to Phase 3-4.
Narration: in teach mode the user can talk ("this is how I claim the daily rewards; skip the
battle pass pop-up"). Utterances are attached to the nearest step and are the most valuable input
for the distiller.

**C8. MacroRunner** (server). Replays a compiled skill: for each step, observe → check anchor
(element with same resource_id/text, or template match for treeless) → act → check expected
post-signature. On mismatch: hand the remainder to the LLM loop with the skill text as guidance
(i.e. degrade gracefully, never blind-tap). Each step still calls `_guard`. Typical win: 5-10×
fewer model calls on repeated chores. Uses the *existing* RPCs.

**C9. Routine system (games).** Section 3.5.

**C10. Evaluator.** Section 3.7.

### 3.3 Data model (SQLite, `learning.sqlite`; every row has `account_id`)

```sql
-- C1: structured trajectory (also the eval/replay corpus). Child of history.turns.
CREATE TABLE steps (
  turn_id TEXT NOT NULL, idx INTEGER NOT NULL, account_id TEXT NOT NULL, device_id TEXT NOT NULL,
  t REAL NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL,          -- JSON, redacted
  norm_xy TEXT,                                                      -- JSON [x,y] in 0..1 device space
  package TEXT, app_version TEXT, screen_w INTEGER, screen_h INTEGER,
  anchor TEXT,                                                       -- JSON {text,desc,resource_id,cls,bounds}
  screen_sig TEXT, jev_state TEXT, outcome TEXT, is_error INTEGER, latency_ms INTEGER,
  source TEXT NOT NULL DEFAULT 'agent',                              -- agent | user_demo | macro | routine
  PRIMARY KEY (turn_id, idx));

-- C2: what the agent knows about the user (ChatGPT "saved memories" analogue)
-- Every knowledge table below also has (added after review, per B §5.1):
--   namespace TEXT NOT NULL DEFAULT 'user'   -- user | client:<client_id>
--   client_id TEXT                           -- the principal that created it (NULL = owner)
-- skills additionally: executor TEXT NOT NULL DEFAULT 'guide'  -- guide | macro | routine
-- and routines.spec is stored as skill_versions.macro when executor = 'routine'.
-- Phase 0-1: SQLite; S0.5: same DDL in Postgres (B), PNG templates in object storage.
CREATE TABLE facts (
  id TEXT PRIMARY KEY, account_id TEXT NOT NULL, text TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'account',     -- account | device:<id>
  status TEXT NOT NULL,                      -- candidate | active | disabled
  provenance TEXT NOT NULL,                  -- JSON {source: user_said|reflection|api, turn_id, key_id}
  created REAL, updated REAL, last_used REAL, uses INTEGER DEFAULT 0);

-- per-app knowledge (AppAgent docs / Mobile-Agent-E Tips analogue)
CREATE TABLE app_notes (
  id TEXT PRIMARY KEY, account_id TEXT NOT NULL, package TEXT NOT NULL,
  version_min TEXT, version_max TEXT, text TEXT NOT NULL,
  kind TEXT NOT NULL,                        -- layout | popup | quirk | preference
  status TEXT, provenance TEXT, created REAL, updated REAL, uses INTEGER, helped INTEGER);

-- skills: SKILL.md + optional compiled macro; versions are immutable
CREATE TABLE skills (
  id TEXT PRIMARY KEY, account_id TEXT NOT NULL, name TEXT NOT NULL,   -- slug, unique per account
  description TEXT NOT NULL, packages TEXT,  -- JSON list
  current_version INTEGER, status TEXT, visibility TEXT DEFAULT 'private',  -- private | shared:<grant>
  owner_kind TEXT DEFAULT 'user',            -- user | developer:<key_or_app_id>
  risk TEXT DEFAULT 'none',                  -- none | confirm | budgeted
  created REAL, updated REAL, UNIQUE (account_id, name));
CREATE TABLE skill_versions (
  skill_id TEXT, version INTEGER, skill_md TEXT NOT NULL,   -- frontmatter + procedure
  macro TEXT,                                -- JSON steps with anchors + expected post-signatures
  params TEXT,                               -- JSON schema
  source TEXT,                               -- demo:<id> | turn:<id> | edit | reflection:<id> | api:<key>
  created REAL, PRIMARY KEY (skill_id, version));
CREATE TABLE skill_runs (
  id TEXT PRIMARY KEY, skill_id TEXT, version INTEGER, turn_id TEXT, mode TEXT,  -- guide | macro
  success INTEGER, fell_back_at INTEGER, steps INTEGER, started REAL, finished REAL);

-- games
CREATE TABLE routines (
  id TEXT PRIMARY KEY, account_id TEXT, name TEXT, package TEXT, spec TEXT NOT NULL,  -- JSON (3.5)
  version INTEGER, status TEXT, budgets TEXT, created REAL, updated REAL);
CREATE TABLE templates (                     -- small image crops the user approved, for matching
  id TEXT PRIMARY KEY, account_id TEXT, package TEXT, label TEXT, png BLOB,
  region TEXT, screen_w INTEGER, screen_h INTEGER, created REAL);
CREATE TABLE routine_runs (
  id TEXT PRIMARY KEY, routine_id TEXT, started REAL, ended REAL, end_reason TEXT,
  ticks INTEGER, actions INTEGER, escalations INTEGER, stats TEXT);

-- teaching
CREATE TABLE demonstrations (
  id TEXT PRIMARY KEY, account_id TEXT, device_id TEXT, package TEXT, path TEXT,  -- dashboard | a11y | getevent
  started REAL, ended REAL, narration TEXT, status TEXT,   -- recording | distilling | reviewed | discarded
  frames_expire REAL);                       -- teach-mode JPEGs auto-deleted at this time (default 24 h)

-- every learned change goes through a proposal; this is also the audit of learning
CREATE TABLE proposals (
  id TEXT PRIMARY KEY, account_id TEXT, target_kind TEXT, target_id TEXT, op TEXT,  -- create|edit|disable
  body TEXT, reason TEXT, source TEXT, status TEXT,  -- pending | accepted | rejected | auto_accepted
  created REAL, decided REAL);
```
History gets two small additions: `turns.package` (last foreground app) and `events.kind =
'correction'`. Deleting a chat deletes its `steps`; deleting a fact/skill is a hard delete with
its versions; "Delete everything learned" wipes `learning.sqlite` rows for the account.

**SKILL.md format** (compatible with Anthropic Agent Skills frontmatter so skills export to
Claude/other agents unchanged; OpenMob-specific fields under `openmob:`):
```markdown
---
name: wwm-claim-daily-rewards
description: Claim the daily sign-in, mail and battle-pass rewards in Where Winds Meet. Use when the user asks to "do my dailies" or "claim rewards" in Where Winds Meet.
openmob:
  packages: [com.netease.yysls]
  params: {}
  risk: none                # none | confirm | budgeted
  success_check: "Mail shows no red dot; sign-in calendar shows today claimed"
  macro: true               # a compiled macro exists for fast mode
---
1. Launch Where Winds Meet; wait through loading; tap anywhere on "Tap to start".
2. Close announcements (X, top-right). If "Battle Pass" promo appears, tap "Later", never "Buy".
3. Tap the scroll icon (top-right) → "Events" → "Daily Sign-in" → "Claim".
4. Back to menu → Mail (envelope) → "Claim all".
Pitfalls: the gem shop button sits next to Mail; never tap anything showing a price.
```

### 3.4 How it plugs into the existing loop (minimal diffs)

| Hook | Change |
|---|---|
| `Agent.run` (`loop.py:115`) | Accept `memory_mode` (`on` / `read_only` / `off`, default on; API callers can choose; "incognito" chat = off). Build context via ContextBuilder. After the task, enqueue Learner jobs. |
| `messages.create` (`loop.py:156`) | `system=[{text: base+facts+skill_index, cache_control: ephemeral}]`, tools cached too. |
| `_obs_blocks` (`loop.py:289`) | When `obs.package` differs from the previous observation's, append app notes for it. |
| `_execute` (`loop.py:334`) | TrajectoryRecorder writes the `steps` row (before/after), including anchors. |
| top of the step loop (`loop.py:149`) | Drain `steer` queue → append `User correction: …` to the next user message. |
| `_dispatch` | New tools: `use_skill`, `remember`, `run_routine`. |
| `Coordinator.submit` (`runner.py:85`) | Route non-stop utterances to `steer` while a task runs. |
| `History.set_feedback` | Also enqueue a Learner reflect job (👎) or distill offer (👍). UI asks "What went wrong?" on 👎 (note already supported by the API). |
| Dashboard | "Learned" page (facts / apps / skills / routines, each with edit, disable, delete, history, success rate), proposal cards in chat, "Teach" button on the live view. |

**Triggers summary**

| Signal | Example | Result |
|---|---|---|
| Explicit memory | "Remember my gym is 24 Hour Fitness on Main" | fact, auto-accepted (user's own words) |
| Explicit teaching | "Learn this: when I say 'dailies', do …" / "watch me" | demo → skill draft → user reviews |
| Save success | 👍 on a 9-step task, or "save that as 'order my usual'" | distill → candidate skill (params inferred) |
| Correction | "No, the archive icon is the box, not the trash" mid-task | steers the run now; reflection → app-note proposal |
| 👎 + note | "it opened the wrong account" | reflection → proposals |
| Automatic | task succeeds after initial errors; repeated pop-up | candidate app note, needs N confirmations or user ✓ |
| Drift | skill fails 3× after app update | demote; ask to re-teach |

### 3.5 Game routines (the fast executor)

A **routine** is a JSON state machine the LLM authors from the user's teaching and then supervises:

```json
{
  "name": "wwm-auto-gather", "package": "com.netease.yysls",
  "tick_hz": 5,
  "detectors": {
    "gather_prompt": {"type": "template", "template": "tpl_gather_btn", "region": [0.70,0.55,0.95,0.80], "min_score": 0.82},
    "inventory_full": {"type": "ocr_text", "region": [0.3,0.4,0.7,0.6], "match": "(?i)bag is full"},
    "low_hp": {"type": "pixel_ratio", "region": [0.05,0.04,0.25,0.06], "color": "#c8302c", "below": 0.3},
    "any_price": {"type": "ocr_text", "region": [0,0,1,1], "match": "[0-9]+\\s*(gems|jade|\\$)"}
  },
  "rules": [
    {"when": "any_price",      "do": {"escalate": "Purchase screen visible"}},
    {"when": "low_hp",         "do": {"stop": "Low health"}},
    {"when": "inventory_full", "do": {"stop": "Bag full"}},
    {"when": "gather_prompt",  "do": {"tap": "gather_prompt.center"}, "cooldown_ms": 1500},
    {"else": true,             "do": {"swipe_hold": {"from":[0.18,0.75],"to":[0.18,0.62],"ms": 1200}}, "cooldown_ms": 1300}
  ],
  "budgets": {"max_seconds": 900, "max_actions_per_min": 40, "forbidden_regions": ["shop_button"], "max_escalations": 5},
  "kill": ["user_touch", "app_left", "screen_off"]
}
```
- **Detectors**: template match (OpenCV `matchTemplate` on user-approved crops), OCR text in a
  region (ML Kit on device / RapidOCR on server), pixel-color ratio (HP bars, cooldown rings), Jev
  state (slow path only). All cheap: single-digit to tens of ms per frame.
- **Actions**: tap/long-press/swipe/hold in normalized coordinates or at a detector's match.
  `escalate` wakes the LLM (one screenshot + context) which can resolve and resume, or stop.
- **Safety**: `forbidden_regions` and an always-on `any_price`/purchase detector; budgets; kill on
  user touch (candidate signals: a11y `TYPE_TOUCH_INTERACTION_START`, which may only be delivered
  in touch-exploration mode (**verify on device**), or a tiny non-intrusive overlay with
  `FLAG_WATCH_OUTSIDE_TOUCH`, which reports that a touch happened but not where); also a
  persistent "Stop" notification action and the volume-down key via `onKeyEvent`
  (`flagRequestFilterKeyEvents`); kill when another package comes to front.
- **Where it runs.**
  *4a, server-side* (first): consume the H.264 stream we already have (`screencast.py`), decode
  with PyAV, run detectors, send `tap/swipe` RPCs. Reaction latency ≈ encode+network+decode+detect
  +RPC+gesture ≈ 200-500 ms. Good enough for idle/auto-battle/gathering/fishing-style loops,
  menus, turn-based combat. Not for dodging or rhythm games.
  *4b, on-device*: a Kotlin RoutineExecutor using MediaProjection frames (ScreenStreamer already
  holds a MediaProjection + VirtualDisplay, `ScreenStreamer.kt:21-66`; add an `ImageReader`
  surface at low resolution), OpenCV-Android or plain pixel ops + ML Kit OCR, and `dispatchGesture`.
  ~50-150 ms reaction, works when the server link is slow, and keeps frames on the phone (privacy
  win). The server sends the routine spec + templates; the phone reports tick stats and
  escalations.
- **What the LLM does**: (1) during teaching, turns the user's narration + demo into detectors,
  rules and template crops (asks the user to confirm each crop: "Is this the Gather button?");
  (2) at run time, starts the routine, handles escalations, and summarizes; (3) after runs, reads
  `routine_runs.stats` and proposes edits ("gather prompt missed 30% of the time at night;
  lowering threshold to 0.78?").

### 3.6 Privacy and safety model

- **What's stored by default**: text only, as today, plus structured steps (redacted args, anchors
  with on-screen labels, normalized coordinates, perceptual hashes). Screen labels can contain PII
  (contact names, message previews in element text): steps follow the chat's retention and
  deletion; exemplars injected into prompts are compressed to tool + target, not full trees.
- **Stored only on opt-in**: teach-mode JPEGs (needed to distill game demos; auto-deleted after
  24 h or when the demo is reviewed), and **template crops** the user explicitly approves (small
  game-UI crops, kept with the routine). Never in sensitive apps; never from `FLAG_SECURE` screens.
- **Never stored / learned**: password fields (`isPassword` → add to `element()` in
  `PhoneActions.kt`), anything while a sensitive app is in front, payment details, OTPs. The
  recorder pauses itself in those apps.
- **Isolation**: every table keyed by `account_id`, queried through owner-checked methods (copy
  `History.chat()`'s 404 pattern). Device-specific items carry `device_id`. No cross-account
  reads in Learner jobs (one job = one account).
- **Memory-poisoning defense**: proposals that originate from screen text (reflection, `remember`
  called without the user's words) are never auto-accepted; skill text is rendered to the model
  inside a delimited block labeled as user-authored procedure; skills cannot contain instructions
  to bypass confirmation (lint at save: reject "don't ask", "without confirming", raw coordinates
  on a forbidden region).
- **Risky skills**: `risk: confirm` keeps per-step confirmation; `risk: budgeted` lets the user
  pre-approve a bounded action set at teach time ("may spend up to 0 real money; may use in-game
  stamina potions up to 3/day"), stored as an explicit grant with expiry; `_guard` consults it.
- **Third-party principals (after review)**: writes from non-owner principals (`memory:write`,
  `skills:write`, `save_skill`, lease traces) are always proposals or client-namespace
  candidates; they can never modify the `user` namespace or carry approvals. Owner can bulk-delete
  "everything client X taught" (`provenance.client_id`). Skills used for a third-party task have
  literal personal values parameterized, so summaries can't echo the owner's data back.
- **Deletion semantics**: deleting a chat deletes candidates derived from it; accepted items stay,
  marked "source deleted", with a one-click cascade (Claude's memory docs leave this implicit;
  we make it explicit).
- **Global AppKnowledge (B §5.1)**: deferred. Sybil poisoning (many accounts teaching the same bad
  note) is the threat; if built: app notes/SKILL.md only, multi-account agreement, human review.
- **User controls**: per-account toggles (learning on/off, auto-accept off), per-chat incognito,
  "What I've learned" page, export (JSON + SKILL.md zip), delete-all, and a learning audit log
  (the `proposals` table).

### 3.7 Evaluation: how we know it improved

1. **Online metrics (always on, cheap)**: per account × app: success rate (agent `done(success)`
   corrected by 👎 and by Jev `task_done`), steps per success, model calls per success, tokens,
   👎 rate, correction rate, time-to-done. Compare **with-knowledge vs without** by (a) per
   artifact, tasks where it was injected vs similar tasks on the same app before it existed, and
   (b) on the platform level, a small randomized holdout (e.g. 5% of tasks on accounts that opt in
   to experiments) run with `memory=off`. Single-user numbers are small, so report counts with
   confidence intervals, never bare percentages.
2. **Per-artifact stats**: each skill/note records `uses` and `helped` (task succeeded and the
   artifact was used/loaded). Wilson lower bounds drive promotion/demotion (C6).
3. **Replay suites (offline, text-only)**: from successful turns, build step-level cases
   `(observation text: package + element list + recent steps) → expected action`. Run the agent
   with a candidate memory/skill version and measure action-match accuracy (AndroidControl-style
   step accuracy). Catches regressions from bad edits without touching the phone. Treeless (game)
   screens are only replayable with opt-in frames.
4. **Canaries (live, opt-in)**: user-approved, read-only tasks ("open Settings › Battery and read
   the percentage") scheduled after app updates to detect skill drift.
5. **Platform regression (not per-user)**: nightly AndroidWorld subset on an emulator to make sure
   changes to the core loop, prompts or model tiers don't regress the base agent.

### 3.8 Distribution constraint that shapes this design (verified 2026-10-04)

Official text, Google Play "Use of the AccessibilityService API"
(https://support.google.com/googleplay/android-developer/answer/10964491):
> "Any use of the Accessibility API that enables an app to autonomously initiate, plan, and execute
> actions or decisions is strictly prohibited."
> "This does not prohibit deterministic, rule-based automation, where behavior follows a static,
> human-defined script (for example, 'If Trigger X occurs, perform Action Y')."

The exemption covers only verified `isAccessibilityTool` apps whose core purpose is assisting
people with disabilities. "Automation tools, assistants" are explicitly *not* accessibility tools.
The update was announced Oct 30 2025
(https://support.google.com/googleplay/android-developer/answer/16550159). Google's summary gives
January 28 2026 as the effective date.

Implications for learning:
- **The LLM loop is not shippable on Play.** Distribution is direct APK (B's plan), which needs
  **Android developer verification**: from Sept 30 2026 in BR/ID/SG/TH, and globally from 2027.
  Other routes are alternative stores, enterprise/MDM, or an **adb/shell-helper backend** that
  doesn't use AccessibilityService at all. Details are in `review_A_of_B.md` §4.
- **The shell helper also serves learning.** It gives `getevent` capture of the user's own touches
  for game demonstrations (C7 Path C) and faster input and capture for routines. Recommend
  building it as the second control backend.
- **Learning moves work toward the allowed side.** A user-defined, deterministic macro or routine
  matches "static, human-defined script", but only if the human defines or approves it and there
  is **no on-device LLM escalation**. A Play build would be "routines only, escalation disabled".
  That is a hypothesis for Play policy review, not a plan.

### 3.9 Why these choices (traceability to research)

| Choice | Because |
|---|---|
| Per-app notes keyed by package | App docs are the biggest cheap lever (AppAgent +25–40 pts; AutoDroid), and Mobile-Agent-E Tips add +8–10 pts |
| Teach mode / demonstrations | Demo-derived docs beat self-exploration by 11 pts (AppAgent) |
| SKILL.md workflows over raw replay | AWM: abstract workflows beat concrete trajectories; ASI: verified skills +11 pts over text skills |
| Candidate → active only after verified success | ASI's gain comes from verification; it is also the main defense against MINJA/MemoryGraft-style poisoning |
| Macros with anchors + LLM fallback | Coordinate record-replay fails often (17% of scenarios in a 2025 study); MobileGPT/AppAgentX show reuse cuts steps and cost 37–69% |
| Routines for games, LLM as supervisor | LLM step loops score ~0 in real time (Orak, VideoGameBench); MAA/Alas prove template+OCR bots work for mobile-game loops; Lumine shows the slow-planner/fast-actor split |
| No fine-tuning | Gains need 10k+ trajectories (AndroidLab, AndroidControl), which single users and a no-screenshot policy can't supply |
| Proposals, provenance, incognito, export | Mirrors ChatGPT/Claude memory controls; screen-sourced writes are the poisoning vector |

---

## 4. The game example end to end

*User: "I want you to play Where Winds Meet for me."* (`com.netease.yysls`, a real-time open-world
action RPG by NetEase; the README demo app.)

**Step 0, set expectations (agent, in chat).** The agent classifies the request with a small
taxonomy (menus/chores · turn-based · semi-real-time loops · reflex combat) and answers honestly:
"I can learn your routines in this game: daily rewards, mail, crafting, gathering or fishing
loops, auto-battle and auto-path. I can't play live combat at human speed. Also, NetEase's terms
likely forbid automation and could get your account banned; want to continue?" The user's
acknowledgment is stored as a fact (`ack_tos_risk: com.netease.yysls`).

**Step 1, teach a chore by conversation (works today with tiny changes).** "Do my dailies:
claim sign-in, mail and the event reward." The agent runs its normal loop, with OCR text for the
treeless screens (needs on-device ML Kit when hosted, gap 6). It struggles: wrong icon, a battle
pass pop-up. The user corrects mid-task via **steer**: "the menu is the scroll icon top right",
"press Later on that pop-up, never buy". The run succeeds in 23 steps. The user taps 👍.
→ Learner distills `wwm-claim-daily-rewards` (SKILL.md above), creates two app notes (menu icon;
battle-pass pop-up → Later), and a fact ("never buy anything in WWM"). User approves the cards.
→ Next day "do my dailies" matches the skill from the index; the agent `use_skill`s it and
finishes in 9 steps with 0 corrections. After 3 successes the macro compiles; the 4th run takes
~40 s with 3 model calls (escalation only at the unfamiliar event pop-up).

**Step 2, teach a loop by demonstration.** "Watch me gather herbs." The user opens the dashboard
live view, clicks **Teach**, and plays with the mouse for two minutes while narrating ("walk
forward until the gather button shows, tap it, if the bag is full stop"). DemoRecorder stores
gestures + frames (24 h TTL). Learner proposes a routine: detectors (template crop of the Gather
button, which the user confirms; OCR "bag is full"), rules, budgets. Dry run: the server-side
executor runs it for 60 s while the user watches; stats show 14 gathers, 1 miss. User saves.

**Step 3, "play for me".** "Gather herbs for 15 minutes." Agent: launches the game (skill),
navigates to the gathering area (LLM loop with app notes), then `run_routine("wwm-auto-gather",
max_seconds=900)`. The executor ticks at 5 Hz on decoded frames; the LLM is asleep. A purchase
dialog appears → `any_price` detector escalates → the LLM sees one screenshot, taps "Close",
resumes the routine. At 15 min the routine ends; the agent summarizes: "Gathered 112 herbs,
1 escalation (shop pop-up), stopped on time."

**Step 4, improvement.** Learner reads `routine_runs.stats`, sees gather prompts missed at
night-time lighting, proposes a second template crop; the user confirms. Success rises from 93% to
98% detection on the next run. This is the improvement loop the user sees.

**Limits (state them in product):**
- Reflex combat (dodging, PvP, timing parries): out of reach. Even on-device, a template-matching
  routine is a bot, not a player; a learned policy would need behavior cloning on large
  demonstration data and per-game model training (research, not product).
- Open-world navigation by joystick is unreliable without a minimap/position read-out; prefer the
  game's own auto-path/auto-battle features and let the routine trigger them.
- Anti-cheat: injected gestures and accessibility automation can be detectable; online games
  typically ban automation. Default: game routines allowed only after explicit acknowledgment,
  never for PvP/ranked modes the user names, and never in a way that buys anything.
- Game updates break templates → demotion + "re-teach?" prompt.
- Hosted mode needs on-device OCR (ML Kit) before any of this works for treeless screens.
- From an MCP client (Claude/ChatGPT) the flow is identical: the client calls `run_task` (or
  `run_task(skill_id=wwm-auto-gather)`), polls `get_task`, and never sees frames; escalations are
  handled by OpenMob's agent, risky prompts go to the phone. Reflex games advertised as "play a
  round of Subway Surfers" (B's journey example) should get an honest refusal-with-alternative.
- Server-side routines must execute on the machine holding the phone's socket (B's fly-replay
  presence), which puts frame decoding on that machine; this is a capacity cost B's plan should
  budget, and an argument for the on-device executor (4b) sooner.

---

## 5. Interfaces with Architect B (merged after review)

The agreed table, with every row marked agree or conflict, is in `review_A_of_B.md` §3. This
section is the learning side's normative summary of it. Everything goes through B's `V1Service`
with a `Principal`. Errors and isolation follow B: 404 outside the grant, 403
`insufficient_scope` for step-up.

### 5.1 Scopes (B's names; A's semantics for writes)

| Scope | Allows | Default |
|---|---|---|
| `skills:read` | list and read skills of every executor, with stats | yes |
| `skills:run` | run a skill. Also lets OpenMob's agent auto-use the user's skills in this principal's tasks. | yes |
| `skills:write` | create or edit skills in the principal's namespace. Third parties write `candidate` skills in `client:<id>` only. | opt-in |
| `memory:read` | read facts and app notes. Facts are injected into this principal's tasks. | opt-in |
| `memory:write` | Owner principals write directly. Everyone else creates proposals that the owner approves on the phone or dashboard. | opt-in |
| `teach:record` | start or stop a demonstration. Frames are kept with a TTL. Needs phone approval. | opt-in, sensitive |
| `routines:run` | run `executor: routine` skills with budgets. Needs a ToS acknowledgment on the phone. Not offered to unverified clients. | opt-in |
| feedback, steer | part of `tasks:run` | – |
| `confirm:approve` (B) | never satisfies a skill's budget grant or any learning approval | – |

### 5.2 REST

- `POST /v1/devices/{id}/tasks` gains these fields:
  - `skill_id` and `params`
  - `conversation_id`
  - `memory`: `on`, `read_only` or `off`. The default is `on` for owner principals and
    `read_only` for third parties.
  - `max_seconds`, for routines
- The Task object gains `skill_used {id, version, executor}`, `knowledge_used[]` and
  `learned[{type, id, summary, status}]`.
- New task endpoints:
  - `POST /v1/tasks/{id}/feedback {rating, correction?}`
  - `POST /v1/tasks/{id}/steer {text}`
  - `POST /v1/tasks/{id}/save_as_skill {name?}`
- Skills:
  - `GET /v1/skills?app=&executor=` and `GET /v1/skills/{id}` return SKILL.md, versions and stats.
  - `POST /v1/skills` takes a SKILL.md body, which is linted server-side.
  - `DELETE /v1/skills/{id}`
- Memory and app notes:
  - `GET/POST /v1/memory` and `DELETE /v1/memory/{id}`
  - `GET /v1/apps/{package}/notes`
- Proposals (owner only): `GET /v1/proposals?status=pending` and `POST /v1/proposals/{id} {accept}`.
- Demonstrations: `POST /v1/devices/{id}/demonstrations`, `POST …/{demo}/stop` and `GET …/{demo}`.
- Routine runs reuse the task lifecycle: start with `run_task` / `skill_id`, stop with
  `cancel_task`.
- Owner-only bulk operations: `GET /v1/learning/export` and
  `DELETE /v1/learning?source=client:<id>`.
- Webhooks, through B's outbox: `learning.proposal_created`, `skill.demoted`, `routine.escalated`.

### 5.3 MCP

- Tools:
  - `run_task` (+`skill_id`, `params`, `memory`)
  - `steer_task`
  - `give_feedback`
  - `list_skills`, `get_skill`
  - `save_skill`
  - `start_teaching` / `stop_teaching`
  - `remember` / `forget` / `recall`. `remember` returns `pending_approval` or `saved`.
  - B's `get_task`, `answer_question`, `cancel_task` and `phone_*`
- Resources:
  - `openmob://skills` and `openmob://skills/{id}`
  - `openmob://apps/{package}/notes` and `openmob://memory`, with `memory:read`
  - `openmob://tasks/{id}/trajectory`: owner or `history:read` only
- Prompt: `teach_skill`.

### 5.4 Contract points B must honor (learning-side requirements)

1. Skill, macro and routine execution runs as a Coordinator task: same lease, guard, blocker,
   audit and credits. Billing:
   - macros at a lower per-step rate
   - routines per minute, plus escalations billed as steps
2. A routine executes where the phone's frames are: on the machine that holds the socket, or on
   the device. MCP clients only start, stop and poll it.
3. Every learning write records `provenance` (`principal`, `client_id`, `key_id`, `source`) and
   goes to the audit log as a hash.
4. A proposal approval is an owner action, made on the dashboard or by phone notification.
   `answer_question` and `confirm:approve` can't accept proposals.
5. Revoking a grant triggers deletion of that client's namespace, unless the user chose to keep it.
6. Screen-derived text in SKILL.md or notes stays labeled as such in prompts. Only `user_said`
   items and user-approved edits count as user-authored.

---

## 6. Phased roadmap

| Phase | Scope | Effort | Risk |
|---|---|---|---|
| **0. Foundations** (after B's P1.5 `V1Service` split, or alongside it) | `steps` table + TrajectoryRecorder with redaction; `turns.package`; `isPassword` in `element()`; 👎 asks "what went wrong?" (note already supported); **steer** channel (runner + loop + UI + `/v1/tasks/{id}/steer`); prompt caching on system/tools. | 3-5 days | Low. Steer changes UX semantics of mid-task speech: keep "queue as new task" visible. |
| **1. Memory v1** | `facts` + `app_notes`; `remember`/`note_app` tools with proposal cards; injection (prefix facts, per-package notes); "Learned" page with edit/delete/export; incognito chats; per-app metrics view. | 1.5-2 wks | Prompt bloat (cap sizes); low-quality auto-notes (keep reflection off until Phase 2). |
| **2. Skills v1 + reflection** | SKILL.md skills, skill index + `use_skill`; "save as skill" from 👍/turn; Learner distill + reflect jobs (cheap model); promotion/demotion; skill_runs stats; exemplar retrieval (top-3 similar successful turns). | 2-3 wks | Distilled skills too specific/too vague; mitigate with user review and run stats. |
| **3. Teach mode + macros** | Dashboard remote control with recording (Path A); a11y event recorder (Path B); demo → skill; MacroRunner with anchored replay and LLM fallback; on-device ML Kit OCR as a new optional phone method `ocr` (fixes hosted games). | 3-4 wks | Live-view input latency; Play policy wording for accessibility use; ML Kit adds ~MBs to APK. |
| **4a. Routines (server)** | Routine spec + validator; templates from user-confirmed crops; server executor on decoded H.264 (PyAV + OpenCV); escalation to LLM; budgets, forbidden regions, purchase detector, user-touch kill; routine stats → Learner tuning. | 3-5 wks | Video only flows while a viewer is open today: needs a "routine viewer" server-side subscriber; CPU cost per phone on Fly; ToS/ban risk messaging. |
| **4b. Routines (on-device)** | Kotlin interpreter of the same spec on MediaProjection frames; server sends spec/templates; phone reports stats. | 4-6 wks | MediaProjection consent per session; battery/thermal; device fragmentation. |
| **5. Platform** | Scopes/endpoints/MCP per §5; export; developer namespaces; offline replay suite + AndroidWorld nightly. | 2-3 wks (with B) | Sharing semantics; abuse (bots at scale via API → rate limits on routines). |
| Policy | Register as a verified Android developer (sideload requirement from Sept 30 2026 in BR/ID/SG/TH, global 2027); build the adb/shell-helper backend (control + `getevent` demos + fast capture) as a second driver; optionally a policy-reviewed "routines only, no LLM escalation" Play build. | verification: days; helper: 2-3 wks | Play remains closed to the LLM loop; helper setup friction (pairing after reboot). |
| Later | Opt-in community app notes; small perception model (UI-TARS-class or a grounding model) for treeless screens; fine-tuning only with explicit data donation. | – | Privacy, poisoning. |

**First PR I'd write** (fits today's code, ~1 day): `steps` table + recorder hook in
`Agent._execute` + `turns.package`, with tests mirroring `tests/test_history.py`. Every later
phase depends on it, and it starts accumulating data immediately.

---

## 7. Open questions for the user

1. Which game exactly, and which parts do you want automated (dailies/menus, a farming loop,
   combat)? Is it Where Winds Meet? That decides whether Phase 4 is needed at all.
2. Are you OK with the ToS/ban risk for online games, and should OpenMob refuse PvP/ranked modes?
3. May OpenMob keep **teach-mode screenshots** (24 h) and **user-approved template crops**
   (indefinitely, per routine)? Without them, game teaching can't work; everything else can.
4. Hosted or local Mac first? (Hosted needs on-device OCR before any game learning.)
5. Should learned knowledge follow the *account* across phones (my default) or stay per phone?
   Coordinates/templates are per resolution; skills and notes are portable.
6. Auto-accept policy: should explicit "remember…" from the user auto-save (my default), and should
   reflection-generated notes ever auto-save after N confirmations, or always ask?
7. For developers via MCP: may a developer's key *write* to a user's memory/skills (as proposals),
   or read-only? And do you want a public skill/app-note marketplace eventually?
8. Distribution: is OpenMob meant to ship on Google Play? The 2026 Accessibility policy (§3.8)
   likely forbids the LLM-driven mode there; that changes how much to invest in deterministic
   macros/routines (which may be the Play-compatible core).
9. Is teaching via the dashboard (mouse on the live view) acceptable, or must demonstrations be
   captured on the phone itself (harder: needs adb/Shizuku for games)?
