# Review of Architect A's learning design, by Architect B (platform)

Reviewed: `arch_A_learning.md` (2026-10-04), mainly §3 (architecture) and §5 (interfaces).
Against: `arch_B_platform.md` (now revised; see its "Revisions after review").

**Verdict.** A's design is sound and I agree with most of it. The good parts: typed knowledge stores,
learned items stay candidates until promoted, every write goes through a proposal, deterministic
routines handle games, the account owns everything, and screen text can never write memory. Five
points conflict with the platform design and need a decision:

1. **Where the data lives.** A puts everything in `learning.sqlite` on one host.
2. **Where the routine executor runs.** It is pinned to the phone's machine, and its frames depend on screen-cast consent.
3. **How long a call can block.** `run_routine` "blocks until it ends", but runs last 15 minutes to hours.
4. **What routines and learning cost.** A gives no credit model for routines or Learner jobs.
5. **What third parties get by default.** `skills:read`, `skills:run` and `feedback:write` default to on, which is wrong for third-party OAuth grants.

None of these is fatal. All are fixed by putting learning artifacts on the same `Principal`/`Grant`/`Task`
model as the rest of the platform.

---

## 1. Conflicts with the platform design

### 1.1 Data location once the platform is on Postgres

| A proposes | Problem | Recommendation |
|---|---|---|
| `learning.sqlite`, a file next to `history.sqlite` | Works for one machine only. In platform stage S1+ any machine serves `/v1/skills` and `/v1/memory`, and Learner jobs run anywhere. | Put the same tables, with A's schema unchanged, in **Postgres** under a `learning` schema. Every row has `account_id`, and the access layer filters by owner. A's DDL ports almost 1:1: `TEXT` JSON becomes `jsonb`, `REAL` times become `timestamptz`. |
| `templates.png BLOB`; teach-mode JPEGs with a 24 h TTL | These are screen data. Blobs in Postgres bloat backups and WAL, and the TTL needs a reaper. | Store them in **object storage** (Fly Tigris / S3-compatible), encrypted per account, under keys like `acct/<id>/templates/<id>.png`. Demo frames go under a lifecycle rule that expires them after 1 day. Postgres keeps only metadata and the object key. |
| Learner as "async jobs" (in-process implied) | With N machines and restarts, in-process jobs get lost or run twice. | Use a **Postgres job queue** (`learning_jobs` with `SELECT … FOR UPDATE SKIP LOCKED`), allow one running job per account, retry with backoff, and make jobs idempotent on `(turn_id, kind)`. These run on worker machines, not on the machines that hold phone sockets. |
| `steps` rows written per tool call | The phone's machine writes many rows per second at peak. | Batch-insert at the end of each step, or flush every 250 ms. Store steps in the same transaction scope as task events, which the platform already persists for cross-machine `get_task`. |
| `ledger.audit` with hashes | Agree. | Keep it. The audit log moves to Postgres along with everything else. |

### 1.2 Multi-machine: the routine executor and its frames

- A's 4a executor decodes the phone's H.264 stream with PyAV and OpenCV. That stream only exists on the
  machine holding the phone's WebSocket, so **the executor must run on that machine**. The platform's
  presence registry and `fly-replay` already route `run_routine` there; the executor is part of the
  phone runtime, not a separate service.
- **CPU.** Decoding 720p H.264 and running detectors at 5 Hz costs roughly 10-30 % of a core per
  active routine, which is real load on `shared-cpu` machines. The platform needs **admission
  control**: at most N concurrent routines per machine, otherwise return 429 with `Retry-After`. When
  routines become common, run phone runtimes on `performance` CPUs, or move the phone's socket to a
  routine-capable machine group when a routine starts. A close-code-and-reconnect with a
  `fly-replay` region/instance hint can do that move.
- **Consent friction A missed.** The H.264 stream comes from MediaProjection. On Android 14+ that needs
  **a fresh user consent for every capture session**, and a `mediaProjection` foreground service
  cannot start from the background. So "Claude, gather herbs for 15 minutes" sent while the phone is in
  a pocket cannot start a 4a routine without the user tapping a system dialog. Recommendations:
  1. Add a frame-source fallback: accessibility `takeScreenshot` at about 2 Hz. It is rate-limited but
     needs no projection consent, and is enough for idle and menu loops.
  2. When projection is required, send an explicit phone notification: "Start screen capture for
     routine X".
  3. Raise the priority of on-device 4b. It needs the same consent, but holds it for the whole run and
     keeps frames on the phone.
- **Lock screen (applies to both designs).** `wake_unlock` can only dismiss a *non-secure* keyguard.
  Most users have a PIN or biometric lock, so a task or routine started remotely while the phone is
  locked fails. The product must say so clearly ("unlock your phone, or keep it unlocked while
  charging"), and the platform must report `device.locked` in `list_devices` and the task status. I
  missed this in round 1 too; it is now in my design.

### 1.3 Run-to-completion MCP vs routines that run for hours

- A's tool `run_routine(name, params, max_seconds)` "blocks until it ends". That is fine as an
  *agent-internal* tool inside an OpenMob task. It is **wrong as an MCP or REST call**: MCP tool calls
  have to return within a client's timeout (about 45 s in my design), and on 2026-07-28 clients,
  closing the stream counts as cancellation.
- **Recommendation: a routine run is a Task.** Add `kind: "instruction" | "skill" | "routine" | "control"`
  to the Task object.
  - `POST /v1/devices/{id}/routines/{name}/run` and the MCP `run_routine` return the Task straight
    away, with `status: running`.
  - Progress is read with `get_task`, which long-polls and returns throttled `routine` events (ticks,
    actions, escalations).
  - It stops with the standard `cancel_task`, the phone's Stop action, or the kill switch.
  - Completion goes out through the existing webhooks.
  - This puts routines under the queue, quotas, audit, kill switch, 404 isolation and the
    "Controlled by" indicator for free.
- Escalations that need the human (`any_price`) become `needs_confirmation` on the same Task, and the
  phone approves them. The calling model never does.
- Hard caps:
  - `max_seconds` ≤ 3600 by default, with an owner setting up to 4 h.
  - The run auto-stops on screen off, when the user touches the screen, or when the app leaves the
    foreground. These are A's kill rules, enforced by the platform too.
- Use an explicit `stop_routine` only as an alias of `cancel_task`, so models that look for it find it.

### 1.4 Quotas and credits for routines, macros and reflection

| Workload | LLM cost | Proposal |
|---|---|---|
| LLM task step | per step | unchanged: `steps × tier rate` |
| Macro step (anchored replay, no LLM) | none (except when it falls back) | 0.2 credit per step. Steps after a fallback bill at the tier rate. |
| Routine | none per tick; LLM only on escalation | **1 credit per routine-minute**, plus each escalation billed as an LLM step at the tier rate. Daily cap of 4 routine-hours per phone by default. |
| Learner (distill, reflect, promote) | cheap model, asynchronous | **Free to the user, paid by OpenMob**, capped at 50 jobs per account per day. Never billed to a third-party developer. Reflection on a developer app's tasks runs only if the owner opted that app into learning. |
| Canaries / scheduled evals | LLM steps on the user's phone, unprompted | Opt-in only. Free, limited to one per app per week, and shown on the phone ("OpenMob is checking skill X"). |

The platform enforces these limits in its shared rate store. A's §5 doesn't list them.

### 1.5 What external developers can see or contribute

Agreements:
- The account owns all learned knowledge.
- A key or client is provenance, not an owner.
- Developer skills live in the `developer:<app_id>` namespace and are installed into an account with consent.
- Developers see only aggregate run stats.
- Cross-account sharing is off in v1.
- `clean_text` is applied to Learner output.

Corrections:

1. **Defaults depend on who the principal is.** A gives `skills:read`, `skills:run` and
   `feedback:write` a default of "yes". That is right for the owner's own `om_live_` keys but wrong
   for third-party OAuth grants: a skill body is a personal procedure that can contain addresses, the
   "usual" order, or which account to use. The fix:
   - Owner keys get A's defaults.
   - The consent screen for third-party and MCP clients offers `skills:run` checked by default. It
     covers listing skill **names and descriptions** and running them.
   - `skills:read` (bodies, stats, versions), `memory:*`, `teach:record` and `routines:run` start
     unchecked.
2. **`memory_mode` for third parties.** Facts are injected into OpenMob's agent prompt, and the agent's
   `done` summary and questions go back to the caller. So a third-party task with `memory: on` leaks
   facts ("what's my home address?" → the agent answers from memory). The rules:
   - Third-party grants without `memory:read` always run with `memory: off` for facts.
   - App notes, which are about the app and not about the user, still apply.
   - Owner keys keep A's default of `on`.
3. **Proposals from third parties** (`memory:propose`, `skills:write`) are always `candidate`, never
   auto-accepted. They are rate-limited (20 per client per day) and labelled with the client's name in
   the review card. Accepting a proposal is **owner-only**: on the dashboard, on the phone, or through
   an owner key that has `memory:write`. A third-party token can never accept one, even with
   `memory:write`. I recommend never offering `memory:write` to third-party clients at all.
4. **Learning from a third party's tasks** is off by default per grant (`grant.learn = false`). When
   it is off, the trajectory is still recorded for the owner's history, but the Learner does not
   distill or reflect on it, so a client can't steer the owner's agent by running crafted tasks.
5. **Webhooks for learning events** (`learning.proposal_created`, `skill.demoted`) go only to the
   owner's webhook. Routine webhooks (`routine.finished`, `routine.escalated`) also go to the client
   that started the routine.
6. **Export** (`/v1/learning/export`) is for the owner only: a dashboard session or an owner key with
   every read scope. Never for OAuth third parties.

---

## 2. Platform-side risks A missed

| # | Risk | Mitigation (platform) |
|---|---|---|
| R1 | Standing pre-approvals (`risk: budgeted`, the "may use 3 stamina potions/day" budget) are effectively **grants**. Kept in a skill row, a skill edit or proposal could widen them. | Store budgets in the platform's `grants/policies` table, not in the skill. Only the owner can create them, on the phone with screen lock; they have an expiry, appear on the phone's "Agents" screen, and can be revoked there. `_guard` reads them from the policy, never from skill text. Third-party clients can never hold budgets. |
| R2 | The ToS/ban acknowledgment is stored as a *fact* (`ack_tos_risk`), which is editable, can be poisoned by proposals, and is injected into prompts. | Make it a structured consent record (`consents` table: account, package, kind, time, UI version), written only by the owner's UI and checked by the platform before `run_routine`. |
| R3 | Templates and demo frames are **screen data** held beyond a task. Today the platform promises that screenshots are never stored. | Gate with `teach:record` (equivalent to `screen:read`), encrypt at rest, keep a 24 h lifecycle, honour deletion on account delete, no API read access to raw frames (only `?templates=1` on the owner's export), and update the privacy policy. |
| R4 | Routine executors are bots at scale: game farming, ad click fraud, social automation. | Routines run only on the owner's phone, started by the owner or a grant with `routines:run`. Third-party developer apps cannot run routines in Phase 3 without review. Per-account caps on routine-hours. Anomaly flags (24/7 routines, many phones per account). Admins can switch routines off per account. |
| R5 | The steer channel mixes principals: who may steer whose task? | Only the task's own principal or the phone owner may steer. The owner's steer beats a third-party task (it always lands, and is logged as `correction` from `owner`). A third party steering the owner's task gets a 404. Steer text from a caller model gets `provenance: client:<id>`. The Learner treats it as *untrusted* for proposals: it can't auto-accept, unlike the owner's own words. |
| R6 | `propose_skill(skill_md)` imports arbitrary SKILL.md from a caller's model, which may carry injected instructions. | A's lint plus candidate status, plus: the platform strips any `openmob.risk` above `confirm` from non-owner submissions, drops `macro` fields from external proposals (macros are compiled only by OpenMob's own distiller), caps size at 8 KB, and forbids URLs to sensitive packages. |
| R7 | Canaries and scheduled tasks need a **scheduler**, and they run on a user's phone without a request. | A platform scheduler (cron rows in Postgres plus a leader-elected tick), opt-in, shown in the FGS notification, cancellable, never while the phone is in use (screen on with the user interacting). |
| R8 | Prompt-cache prefix per account: facts and the skill index change, and every change invalidates the cache across machines. | Fine. Note only: version the prefix (`knowledge_version` on the account) so all machines build byte-identical prefixes. Avoid timestamps in the prefix. |
| R9 | Dashboard remote control (A's teach path A) is a new **input path to the phone from a browser**. It is the same capability as my low-level `phone_act`, and needs the same lease, guard and audit. | Use one mechanism: a **control lease** (`Task.kind = "control"`), whether the client is the dashboard, an MCP `phone_*` client, or a teach session (`record: true`). Risky-tap guards apply to human remote taps too, but only as a warning. |
| R10 | Learned items reference `device_id` and resolution-specific coordinates and templates. Re-pairing a phone creates a new device id, so routines are orphaned. | The platform keeps a stable `phone_identity` (hardware model + resolution class) and offers to carry learned device-scoped items over when re-pairing. |
| R11 | Platform policy: A's §3.8 hypothesis that "deterministic user-taught routines are closer to Play's allowed category". | I verified the policy text below: deterministic "If Trigger X occurs, perform Action Y" automation is explicitly allowed. So a **Play build that only runs owner-approved macros and routines, with all LLM planning off-phone and every artifact approved by the user**, is a credible distribution path. I adopt it in my roadmap as an option to validate (not a guarantee: an LLM writing the script could still be judged "autonomous planning"). |

---

## 3. Merged interface table

Names are exact. Status is **agree** (A's name or shape adopted as is) or **conflict → recommendation**.
"Owner" = the account's own dashboard session or an `om_live_` key. "3P" = a third-party or MCP OAuth grant.

### 3.1 Scopes

| Scope | Allows | Owner key default | 3P consent default | Status |
|---|---|---|---|---|
| `devices:read` | list phones | on | on | agree (existing) |
| `tasks:run` | create/answer/cancel tasks, **`steer`** | on | on | agree (A puts steer under it) |
| `tasks:read` | read tasks and events | on | on | agree |
| `screen:read` | screenshot/observe | off | off | agree (existing) |
| `device:control` | low-level `phone_*` lease (implies `screen:read`) | off | off; the phone must approve | B-only. A agrees in §5.3 ("low-level mode") |
| `history:read` | chat history | off | off | agree (existing) |
| `memory:read` | read facts and app notes; lets facts be injected into this principal's tasks | off | off | **conflict → B:** this scope also controls whether facts are injected for 3P (see 1.5.2) |
| `memory:propose` | create fact/app-note proposals | off | off | agree (A's name; replaces my `memory:write` for 3P) |
| `memory:write` | write active memory and accept proposals | off, sensitive | **never offered** | **conflict → B:** owner-only |
| `skills:read` | skill bodies, stats, versions | on | **off** | **conflict → B:** off for 3P |
| `skills:run` | list skill names and descriptions; run a skill by name | on | on | **conflict (small) → B:** includes name/description listing, so `skills:read` isn't needed to run |
| `skills:write` | create/edit skills (as candidates) | off | off | agree |
| `teach:record` | start/stop demonstrations (screen frames) | off | off; the phone must approve | agree, plus phone approval |
| `feedback:write` | rate tasks with a note | on | **off** | **conflict → B:** off for 3P (feedback steers the owner's learning; 3P feedback is ignored unless `grant.learn = true`) |
| `routines:run` | start routines (stop = `cancel_task`) | off | off; 3P apps need review in Phase 3 | agree |
| `confirm:approve` | caller may approve risky steps | off (owner can enable per key) | never | B-only |

My round-1 names `memory:write` (for 3P) and `skills:read/run/write` without the 3P split are
superseded by this table.

### 3.2 REST endpoints (all through `V1Service`)

| Endpoint | Scope | Status |
|---|---|---|
| `POST /v1/devices/{id}/tasks` + `memory: "on"\|"read_only"\|"off"`, `skill: "<name>"`, `params: {}` | tasks:run (+ skills:run if `skill`) | agree with A's field names. I drop my `skill_id`. `conversation_id` (B) added. 3P without `memory:read` is forced to `memory: "off"` for facts. |
| Task object + `kind`, `knowledge_used: [{kind,id,name}]`, `proposals: [ids]` | – | agree with A's fields. `kind` comes from B. I drop my `skill_used`/`learned`. |
| `POST /v1/tasks/{id}/steer` `{text}` | tasks:run | agree. Only the task's principal or the owner may call it; otherwise 404. |
| `POST /v1/tasks/{id}/feedback` `{rating: 1\|-1, note}` | feedback:write | agree with A (`note`, not my `correction`) |
| `GET /v1/memory/facts`, `POST /v1/memory/facts` (proposal), `DELETE /v1/memory/facts/{id}` | memory:read / memory:propose / memory:write | agree with A's paths. POST always creates a proposal unless the caller is the owner with `memory:write`. |
| `GET /v1/apps/{package}/notes` (+ POST proposal) | memory:read / memory:propose | agree |
| `GET /v1/skills?app=`, `GET /v1/skills/{name}`, `POST /v1/skills` (SKILL.md), `DELETE /v1/skills/{name}` | skills:run (list) / skills:read (body) / skills:write | agree on name-addressed paths (`{name}` = slug, unique per account and namespace; `?namespace=developer:<app>` for developer skills) |
| `POST /v1/tasks/{id}/save-as-skill` `{name?}` | skills:write | agree on A's hyphenated path; I drop `save_as_skill` |
| `GET /v1/proposals?status=pending`, `POST /v1/proposals/{id}` `{accept}` | memory:read / memory:write | agree, with the restriction that accepting is owner-only |
| `POST /v1/devices/{id}/demonstrations`, `POST /v1/devices/{id}/demonstrations/{demo_id}/stop`, `GET /v1/devices/{id}/demonstrations/{demo_id}` | teach:record | agree. Implemented as a control lease with `record: true`; starting one triggers the phone approval. |
| `POST /v1/devices/{id}/routines/{name}/run` `{max_seconds, params?}` | routines:run | **conflict → B:** returns a **Task** (`kind: "routine"`) immediately, never blocks |
| `POST /v1/devices/{id}/routines/{name}/stop` | routines:run | **conflict → B:** keep it as an alias that cancels the device's running routine task. The canonical call is `POST /v1/tasks/{id}/cancel`. |
| Routine events on the existing SSE (`event: routine`) | tasks:read | agree |
| `GET /v1/learning/export` | owner only | **conflict → B:** owner session or owner key only, never 3P |
| Webhooks `learning.proposal_created`, `skill.demoted`, `routine.finished`, `routine.escalated` | – | agree on names. Routing per 1.5.5. `routine.escalated` is delivered as `task.needs_confirmation` on the routine task as well. |

### 3.3 MCP tools

| Tool | Maps to | Status |
|---|---|---|
| `list_devices`, `run_task`, `get_task`, `answer_question`, `cancel_task`, `get_screenshot` | /v1 | B's names. A uses `run_task(device, instruction, memory?, skill?, params?)`; agree on adding `memory`, `skill`, `params`. Arg name `device_id` (B), not `device`. |
| `steer_task(task_id, text)` | POST …/steer | agree (A's name) |
| `rate_task(task_id, rating, note?)` | POST …/feedback | agree (A's name; replaces my `give_feedback`) |
| `list_skills(app?)` | GET /v1/skills | agree |
| `get_skill(name)` | GET /v1/skills/{name} | agree (needs skills:read) |
| `save_skill_from_task(task_id, name?)` | POST …/save-as-skill | agree (A's name; replaces my `teach`) |
| `propose_skill(skill_md)` | POST /v1/skills | agree, with R6 restrictions |
| `propose_memory(text, app?)` | POST /v1/memory/facts or app notes | agree (A's name; replaces my `remember`/`forget`/`recall`. Reading uses resources, deleting is owner/dashboard only) |
| `start_teaching(device_id)` / `stop_teaching(demo_id)` | demonstrations | agree (arg `device_id`) |
| `run_routine(device_id, name, max_seconds?, params?)` | routines/run | **conflict → B:** returns a Task; the model polls `get_task` |
| `stop_routine(task_id)` | cancel | agree on the name, as an alias of `cancel_task` |
| `phone_acquire`, `phone_observe`, `phone_act`, `phone_release` | control lease | B-only. A asks that these calls be recorded as steps with `source='api_lowlevel'`; agree, but only when the grant has `learn = true` or the caller is the owner. |

### 3.4 MCP resources and prompts

| Resource | Scope | Status |
|---|---|---|
| `openmob://skills` | skills:run | agree |
| `openmob://skills/{name}` | skills:read | agree |
| `openmob://apps/{package}/notes` | memory:read | agree |
| `openmob://memory/facts` | memory:read | agree (never offered to 3P by default) |
| `openmob://tasks/{id}/trajectory` (redacted steps) | tasks:read **+ history:read** | **conflict → B:** trajectories contain on-screen labels (contact names, message previews), so require `history:read` as well |
| `openmob://devices`, `openmob://devices/{id}/screen`, `openmob://tasks/{id}`, `openmob://devices/{id}/apps` | as in B | B-only, no conflict |
| Prompts `operate_phone`, `teach_skill` | – | B; A has no conflict |

---

## 4. Verified claims (re-checked 2026-10-04)

Quotes are exact text from the pages fetched today.

1. **MCP authorization: CIMD vs DCR.** https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
   - "Authorization servers and MCP clients **SHOULD** support OAuth Client ID Metadata Documents"
   - "Authorization servers and MCP clients **MAY** support the OAuth 2.0 Dynamic Client Registration Protocol (RFC7591). Note that Dynamic Client Registration is deprecated and retained for backwards compatibility with authorization servers that do not support Client ID Metadata Documents."
   - "MCP servers **MUST** implement OAuth 2.0 Protected Resource Metadata (RFC9728)."
   - On the `resource` parameter: "**MUST** be included in both authorization requests and token requests."
   - "MCP servers **MUST** validate that access tokens were issued specifically for them as the intended audience" and "MCP servers **MUST NOT** accept or transit any other tokens."
   - RFC 9207: "MCP authorization servers **SHOULD** include the `iss` parameter in authorization responses… A future revision… is expected to upgrade… from **SHOULD** to **MUST**."
   - **Result: confirmed.** One addition to my design: emit `iss` and advertise `authorization_response_iss_parameter_supported: true`. ChatGPT's stable redirect needs it too.
   - The spec also says servers "**SHOULD** include all scopes required for the current operation in a single challenge". My step-up design complies, since it challenges per tool.
2. **Python SDK v2.** https://pypi.org/project/mcp/ shows **2.3.0, released October 2, 2026**, "Production/Stable". The migration guide https://py.sdk.modelcontextprotocol.io/migration/index.md says:
   - "`FastMCP` has been renamed to `MCPServer` to better reflect its role as the main server class in the SDK"
   - The import path is `from mcp.server.mcpserver import MCPServer, Context`.
   - On protocol versions: "In v2 it is the newest revision the SDK speaks in any era, currently `"2026-07-28"`", with 2025-11-25 also supported.
   - `stateless_http` and `json_response` "have been relocated from the constructor to runtime methods".
   - **Result: confirmed.** Correction: in round 1 the research said there were no `stateless_http`/`json_response` options in v2. They exist, but are passed at run time, not to the constructor. Design updated.
3. **claude.ai connector auth.** https://claude.com/docs/connectors/building/authentication
   - "A `401` is required to start sign-in, and Claude ignores a `WWW-Authenticate` header on a `200` response"
   - "Claude uses only the first entry in your metadata's `authorization_servers` list"
   - "Claude selects CIMD only when your authorization server metadata advertises both `"client_id_metadata_document_supported": true` and `"none"` in `token_endpoint_auth_methods_supported`… If either is missing, Claude falls back to DCR."
   - Callback: "register exactly this redirect URI: `https://claude.ai/api/mcp/auth_callback`"
   - "Claude waits up to 10 seconds for a response from your OAuth discovery, registration, and token endpoints, and up to 30 seconds for refresh token requests."
   - Static headers: "`static_headers` | Fixed credential… entered by an organization Owner… | Beta, for a limited set of organizations"
   - "For servers expecting high traffic from the directory, prefer CIMD or `oauth_anthropic_creds` over DCR. DCR causes Claude to register a new client on every fresh connection".
   - **Result: confirmed.** Corrections and additions:
     - My round-1 note to "also allow the claude.com callback variant" is **not** in the current doc. The doc says to register *exactly* the claude.ai URI. I keep the claude.com variant only as a harmless extra.
     - New: the AS must advertise `code_challenge_methods_supported: ["S256"]`.
     - New: refresh errors must use `invalid_grant`.
     - New: `oauth_anthropic_creds` (an Anthropic-held confidential client) is an option for the directory listing.
     - New: static headers are an *org-level* credential, so a per-user `om_live_` key must not be used that way. The design now says consumer Claude means OAuth only.
4. **ChatGPT.** https://developers.openai.com/apps-sdk/build/auth
   - ChatGPT "does **not** support machine-to-machine OAuth grants such as client credentials, service accounts, or JWT bearer assertions, nor can it present custom API keys."
   - CIMD is preferred and DCR is supported.
   - Token endpoint auth is `none` or `private_key_jwt`.
   - Stable redirect `https://chatgpt.com/connector_platform_oauth_redirect` for issuer-identification-compliant servers.
   - https://developers.openai.com/api/docs/guides/developer-mode: "Available to Pro, Plus, Business, Enterprise, and Education accounts on the web", "Supported MCP protocols: SSE and streaming HTTP", "Authentication supported: OAuth, No Authentication, and Mixed Authentication", "Write actions by default require confirmation".
   - **Correction:** my round-1 claim that "write-capable tools may be limited by plan (Business/Enterprise)" came from third-party sources and is **not** in the official doc. Removed from the design.
5. **Google Play Accessibility policy.** https://support.google.com/googleplay/android-developer/answer/10964491
   - "Any use of the Accessibility API that enables an app to autonomously initiate, plan, and execute actions or decisions is strictly prohibited."
   - "This does not prohibit deterministic, rule-based automation, where behavior follows a static, human-defined script (for example, 'If Trigger X occurs, perform Action Y')."
   - Apps with `isAccessibilityTool="true"` "may use the Accessibility API for autonomous functionality, provided this functionality is for the app's core purpose of assisting people with disabilities."
   - https://support.google.com/googleplay/android-developer/answer/16585319 lists that the API "cannot be requested for: … an app that autonomously initiates, plans, and executes actions or decisions".
   - **Result: confirmed, plus a correction.** The deterministic-automation carve-out was missing from my round-1 summary. It is now in the design (Play "routines-only" build as an option).
   - The effective date is not stated on either page as fetched today; the page only shows the 2021 date for the general policy. I don't repeat A's "announced Oct 30 2025, effective Jan 28 2026" as verified.
6. **Android 17 Advanced Protection.** Search results (2026-03), e.g. https://thehackernews.com/2026/03/android-17-blocks-non-accessibility.html and https://www.androidauthority.com/android-17-beta-2-advanced-protection-mode-accessibility-apps-3648860:
   - "Android 17 Beta 2 now enables a stricter Advanced Protection feature that blocks apps that aren't classified as accessibility tools from accessing the AccessibilityService API"
   - Turning it on "automatically revokes existing permissions for non-accessibility apps, and users won't be able to grant the permission while the mode is active."
   - **Result: confirmed, with a caveat.** These are secondary press sources about a beta. The behaviour applies only to users who enable Advanced Protection (opt-in). Marked [F] in the design.

---

## 5. Where A changed my design (applied to `arch_B_platform.md`)

1. Adopted A's scope names (`memory:propose`, `feedback:write`, `teach:record`, `routines:run`), with the owner/3P default split.
2. Task model gains `kind` (`instruction|skill|routine|control`). Routines are non-blocking tasks with caps.
3. Added `steer_task` and A's learning tool names, replacing my `teach`/`give_feedback`/`remember`.
4. Unified dashboard remote control, teach recording and `phone_*` into one control lease.
5. Learning tables move to Postgres, blobs to object storage, the Learner to a Postgres job queue on worker machines.
6. The routine executor is pinned to the phone's machine, with admission control. Added the MediaProjection-consent and lock-screen constraints.
7. Credits for macro steps, routine-minutes and Learner jobs.
8. Budgets and the ToS acknowledgment move to platform grants and consents.
9. Distribution: Play "routines-only" build added as an option to validate.
10. Corrected the ChatGPT plan claim, SDK v2 transport options and claude.ai auth details.
