# OpenMob agent platform: architecture

Status: proposal, 2026-10-04. This is the merged design. Two independent architecture passes
were cross-reviewed to produce it:
- the learning pass: [research/learning-architecture.md](research/learning-architecture.md)
- the platform pass: [research/platform-architecture.md](research/platform-architecture.md)
- their reviews of each other: [research/review-learning-of-platform.md](research/review-learning-of-platform.md)
  and [research/review-platform-of-learning.md](research/review-platform-of-learning.md)

Those documents hold the evidence, citations, schemas and full interface tables. This page holds
the decisions.

Implementation update (2026-10-06): the saved-notes feature and "What I've learned" page were
removed. Memory and learning below describe proposals, not currently available functionality.

## Goal

**An OpenRouter for agentic mobile access:** one neutral layer with any model on one side and any
phone on the other. One protocol and one API let any agent (Claude, ChatGPT, Gemini, open models,
custom agents) operate a phone through any control backend: today the Android app, later an adb or
wireless-debugging helper, iOS, emulators and cloud phones. Users switch models and providers
freely, and what their agent learned travels with them.

Anyone can give *their own* agent direct access to *their own* phone, and that agent gets better
at the person's apps and games over time, the way ChatGPT and Claude get better at knowing a
user. Other developers integrate the same capability through one MCP server. The user can switch
models and providers at any time without losing what the agent has learned. Friction for an
average user is the top constraint.

## The shape in one picture

```
 Claude / ChatGPT / Cursor / custom agents        OpenMob dashboard + phone app
        │  remote MCP (/mcp, OAuth)   REST (/v1, keys)      │ session
        └──────────────┬──────────────────────┬─────────────┘
                       ▼                      ▼
            Principal resolver (key │ OAuth token │ grant)
                       ▼
            V1Service: the one enforcement point
            scopes · device grants · 404 isolation · quotas · sensitive-app deny · audit · kill switch
                       ▼
   ┌──────────── Phone runtime (machine holding the phone's socket) ────────────┐
   │ Coordinator (task queue, control lease)                                     │
   │   Agent loop ── ModelAdapter (Anthropic │ OpenAI │ Google │ open models)    │
   │      ▲ ContextBuilder ◄── Knowledge: memory (pinned notes, app notes),      │
   │      │                    skills (SKILL.md, macros), routines (games)       │
   │   TrajectoryRecorder → Learner (proposals, never silent writes)             │
   │   MacroRunner / RoutineExecutor (fast, no LLM per frame)                    │
   └──────────────────────────────── WSS ─────────────────────────────────────────┘
                       ▲
     Android app: accessibility actions, screen streaming, approvals with screen lock
```

## Decisions

### 1. One enforcement point, many front doors

`api_v1.py` splits into a `Principal` resolver and a transport-free `V1Service`. The REST routes,
the MCP tools, the dashboard and the later third-party grants all call it. That means scopes,
404 isolation, quotas, sensitive-app blocking, audit and kill switches exist once, and the
existing `/v1` test matrix runs against both REST and MCP.

### 2. The MCP server is the product surface

- **Remote MCP at `/mcp`** (Streamable HTTP, official Python SDK v2, stateless).
  - **Claude.ai and ChatGPT connectors** only support OAuth, so OpenMob becomes an OAuth 2.1
    authorization server: Client ID Metadata Documents first, dynamic registration as a fallback,
    PKCE and audience-bound tokens.
  - **Developer tools** (Claude Code, Cursor, scripts) can also use `om_live_` API keys.
  - **A local `openmob mcp` (stdio) shim** forwards to the same server.
- **Delegation by default.** `run_task` hands a goal to OpenMob's agent. It works in every client,
  OpenMob's own guard and the user's skills and memory apply, and it's fast per step. Low-level
  `phone_*` tools (observe and act under a per-phone lease) are opt-in behind `device:control`, for
  developers who want their own model to drive.
- **Long tasks:** `run_task` waits up to ~45 s, then returns with "call `get_task`". Progress
  notifications go to clients that show them, and the MCP Tasks extension is used where clients
  support it. A dropped connection never cancels a phone task.

### 3. Any model, any provider

The user picks the model and provider; what the agent learned stays with the user, not the model.

- **Learned knowledge is plain text owned by OpenMob:**
  - pinned notes (memory), app notes, and skills in the open `SKILL.md` format;
  - stored in OpenMob's database, injected into whatever model runs the task.
  
  Provider-specific memory features (ChatGPT memory, Claude's memory tool) are never the source of
  truth. Switching from Claude to GPT to Gemini keeps every note and skill.
- **A `ModelAdapter` seam in the agent loop.** Today `openmob/agent/loop.py` calls the Anthropic
  Messages API directly. Each adapter translates three things:
  - tool definitions;
  - screenshot and observation blocks;
  - prompt caching.

  Adapters for Anthropic, OpenAI and Google come first, then OpenAI-compatible endpoints (local or
  open models). Each adapter declares its capabilities (vision, tool calling, caching, context size)
  so the dashboard only offers models that can operate a phone.
- **Two ways to choose:**
  - **users** pick an exact model in the dashboard, including their own provider key (bring your
    own key), as the existing per-phone model picker already does;
  - **developers** pick a tier (`fast` / `balanced` / `best`) or name a model explicitly.
- **External agents read the same knowledge over MCP:**
  - Claude, ChatGPT or a custom agent reads memory and skills as resources (with `memory:read` /
    `skills:read`), so the user's knowledge follows them into any agent, not just OpenMob's;
  - skills export as `SKILL.md`, which Claude Skills and other agents can use directly.
- **Quality differs by model.** A skill taught with one model is a text procedure, so it carries
  over. Success rates are tracked per model, so the dashboard can show "this skill works 95% with
  Claude Sonnet, 70% with model X".
- **Disclosure:** each provider processes screen content under its own terms. The privacy policy
  lists every enabled provider, and switching providers says so.

### 4. Learning: in context, typed, user-controlled, no fine-tuning

The research is consistent: what improves phone agents is small, typed knowledge retrieved at the
right moment, not retraining. Four stores, built in this order:

| Store | What | Example | Retrieved by |
|---|---|---|---|
| Steps | Structured record of every action (tool, args, app, element anchor, outcome; text only, redacted) | the raw material for everything below | task / replay |
| Memory | **Pinned notes** about the user, and notes about each app | "Use my second game account" · "In Where Winds Meet, quests are under the scroll icon" | always (pinned) / by foreground app |
| Skills | Named procedures in `SKILL.md`, optionally compiled to anchored macros that fall back to the model when a screen doesn't match | "do my dailies" | name / description match |
| Routines | Small state machines for game loops, run by a fast executor (template matching, OCR) while the model supervises | "gather herbs for 15 minutes" | invoked by a skill |

How things get learned:
- **"Remember …"** from the user saves a note directly.
- **"Learn this"**, **"watch me"** (teach mode), a **👍** on a successful task, and a **mid-task
  correction** ("no, the box icon") produce *proposals*, shown as one-tap ✓/✕ cards.
- **Background reflection** on failures and corrections also produces proposals, never silent
  writes.
- **A learned item stays a candidate** until it has verified successes or the user approves it,
  and it's demoted again if it starts failing, e.g. after an app update. This is also the main
  defence against memory poisoning.
- **The user sees, edits, exports and deletes everything** on a "What I've learned" page, with
  incognito chats and a learning on/off switch, like ChatGPT and Claude memory.

### 5. Safety model

- **Risky actions are approved by the human on the phone** (screen lock), never by the calling
  model. A model that read injected screen text can't approve its own purchase. Owners can opt out
  for their own keys.
- **Context is keyed by (account, principal, conversation).** This fixes a real leak today:
  `Agent.recent` and the shared chat expose the owner's recent requests to any caller on the same
  phone.
- **Third-party writes to memory or skills are always proposals** or candidates in that client's
  own namespace. They can never change the user's knowledge directly or carry approvals.
- **Screen-derived text** returned to callers is marked untrusted and length-capped.
- **Sensitive apps** (banking, payments, passwords) are denied by default, as today.
- **Standing pre-approvals and game terms-of-service acknowledgments** are platform grants created
  on the phone, never stored inside editable skill or memory text.

### 6. Games: honest scope

- **Works:** menus, dailies, mail, crafting, farming and gathering loops, auto-battle, turn-based
  play. The model plans and supervises; a routine executes at ~5 Hz without a model call per frame.
- **Doesn't work:** reflex combat, PvP, rhythm or dodge games. Every published language-model agent
  scores near zero in real-time play. The product says so up front.
- **Risk:** online games usually ban automation. Routines need an explicit per-game acknowledgment,
  never buy anything, and refuse ranked/PvP modes the user names.
- **Prerequisite:** text reading on the phone itself (on-device OCR), because the hosted Linux
  server has no OCR today.

### 7. Distribution: the biggest friction fact

- **Google Play prohibits** using the Accessibility API "to autonomously initiate, plan, and
  execute actions or decisions"
  ([policy](https://support.google.com/googleplay/android-developer/answer/10964491)). That is
  OpenMob's core function, so the LLM-driven app can't ship on Play.
- **The plan:** direct download from openmob.app, plus Android developer verification (required
  for sideloaded apps from Sept 30 2026 in four countries and globally in 2027).
- **Setup flow:** a guided in-app setup that walks through Android's "restricted settings" unlock
  and the accessibility toggle (the step where sideload users drop off), signing in with Google *as*
  pairing, and push wake so the user never has to open the app first.
- **To evaluate:**
  - a helper that uses Android's wireless-debugging access instead of accessibility, which avoids
    the policy and can record a user's own game taps for teaching;
  - a Play build that only runs owner-approved routines, since Play allows deterministic "if X then
    Y" automation, pending Play policy review.

### 8. Scaling

- **State moves to Postgres:** accounts, keys, OAuth, tasks, ledger, history, learning.
- **Teach-mode frames and templates** go to object storage, with automatic expiry.
- **Routing:** a `device → machine` presence registry plus Fly's `fly-replay: instance=` header
  route any request about phone X to the machine holding its socket. Stateless MCP makes every
  request independently routable.
- **Routines run on the phone's machine** with admission control, and later on the phone itself.

### 9. Neutral on both sides

The "OpenRouter" property applies to phones as well as models:
- **Models:** the `ModelAdapter` seam (decision 3), with per-model capability and success stats so
  routing can pick the best model for a task, app or budget.
- **Phones:** the phone protocol (`docs/phone-protocol.md`) is already the backend seam. The Android
  app, `openmob bridge` (adb) and iOS (WebDriverAgent) all speak it. New backends (a
  wireless-debugging helper, emulators, cloud phones) plug in the same way, and the API and MCP never
  change.
- **Knowledge:** memory and skills are plain text in open formats (`SKILL.md`), exportable, and
  readable by any agent over MCP.

## Roadmap

Effort is focused engineer-weeks. Each phase ships something a user can feel.

| # | Phase | Contents | Effort |
|---|---|---|---|
| 0 | **Foundations** | Split `V1Service`; per-principal context (fix the leak); reconnect grace (a socket blip no longer kills the task); confirmations on the phone; durable webhook outbox; structured step logging; mid-task correction channel; prompt caching; `ModelAdapter` seam with the Anthropic adapter | 2–2.5 |
| 1 | **Memory: pinned notes** removed | The saved-notes feature, its page, tools and APIs were removed on 2026-10-06. Memory elsewhere in this architecture is a future design proposal; API tasks remain isolated from chat requests | 1.5–2 |
| 2 | **MCP with API keys** | `/mcp` tools (`list_devices`, `run_task`, `get_task`, `answer_question`, `cancel_task`, `get_screenshot`, memory and skill resources); stdio shim; Claude Code and Cursor snippets | 1.5 |
| 3 | **Any provider** | OpenAI and Google adapters, OpenAI-compatible endpoint, bring-your-own key, per-model success stats | 1.5–2 |
| 4 | **Consumer connectors** | OAuth server, Google sign-in, consent plus phone approval of grants, Claude and ChatGPT connectors, elicitation for questions | 3–4 |
| 5 | **Skills + reflection** | `SKILL.md` skills, "save as skill", Learner jobs, promotion and demotion, exemplars | 2–3 |
| 6 | **Distribution and setup** | Download page and updater, guided restricted-settings flow, sign-in = pairing, push wake, boot restart, developer verification, directory submissions | 3–4 |
| 7 | **Postgres + routing** (parallel from phase 2) | Migration, presence registry, fly-replay, shared quotas | 3 |
| 8 | **Teach mode + macros** | Dashboard remote control with recording, accessibility event recorder, anchored macros, on-device OCR | 3–4 |
| 9 | **Game routines** | Server executor on the video stream, then on-device | 3–5, then 4–6 |
| 10 | **Third-party developer apps** | App registration, consent on end-users' phones, "Controlled by <App>" indicator, developer billing | 6–8 |

The critical path to "Claude plays my game's dailies on my phone" is phases 0 → 1 → 2 → 4 → 5,
plus on-device OCR from phase 8. That's roughly 11–14 weeks for one engineer.

## Decisions that need the owner

1. **Distribution:** accept direct download (with Play Protect warnings) as the main channel, and
   fund the guided setup? Or explore the wireless-debugging helper or OEM stores first?
2. **Confirmations:** risky actions approved on the phone by default (recommended), with an opt-out
   for your own keys?
3. **Third-party defaults:** a newly connected agent gets task tools and `skills:run` only, with
   memory, screen and learning off until the user enables them (recommended)?
4. **Teaching data:** may OpenMob keep teach-mode screenshots for 24 h and user-approved image crops
   per routine? Game teaching needs them; everything else doesn't.
5. **Games:** which game first, and what's in scope (dailies and loops vs combat)? Is the
   terms-of-service ban risk acceptable with a per-game acknowledgment?
6. **Providers:** which providers at launch, whether bring-your-own key is allowed, and whether
   developers can name exact models or only tiers.
7. **Hosting:** hosted-only, or self-hostable too? Self-hosting changes the OAuth and routing story.
8. **Business model:** who pays when an external model drives the phone (low-level mode), and do
   developer apps pay for tasks on their users' phones?
9. **Locked phones:** remote tasks need the phone unlocked. Is "unlock when asked" acceptable, or
   should there be a dedicated-device mode for long game routines?
