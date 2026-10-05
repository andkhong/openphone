# OpenMob developer API (v1)

Run tasks on your own phones from code: "open YouTube and play lo-fi", "check my last Uber receipt",
"what's on the screen?". The API is served by any OpenMob server running in accounts mode
(`openmob run --accounts`, or the hosted server), at `https://<server>/v1`.

Phase 1 covers **your own phones**: the ones paired to your account. Access to other people's
phones (with their consent) comes later.

AI agents (Claude Code, Cursor, Claude Desktop, the Claude and OpenAI APIs) can use the same API
as MCP tools at `https://<server>/mcp`, with the same key and the same rules: see [mcp.md](mcp.md).

## Authentication

Create a key at **`/developer`** (Dashboard → ⋯ → Developer API) or on the server with
`openmob key create EMAIL`. Keys look like `om_live_<id>_<secret>`. A key is shown once and only
its hash is stored, so copy it when you create it. Send it on every request:

```
Authorization: Bearer om_live_…
```

Treat a key like a password: it can operate your phones. Keep it in an environment variable, not
in code. Rotate or revoke it in `/developer`; a revoked key fails immediately and its running
tasks are cancelled.

| Status | Meaning |
|---|---|
| 401 | Missing, malformed, wrong or revoked key |
| 403 | The key lacks the scope this route needs, or API access was switched off for the key or account. Also `{"code": "confirmation_requires_owner"}` when a key approves a risky action (see below). |
| 404 | No such phone or task **for your account**. Other accounts' phones and tasks always look like this. |
| 409 | The phone is offline, or the task isn't in a state for that action |
| 429 | Rate limit, concurrent-task cap or daily quota reached; see `Retry-After` |

## Scopes

| Scope | Allows |
|---|---|
| `devices:read` | `GET /v1/devices` |
| `tasks:run` | Create, answer and cancel tasks |
| `tasks:read` | Read tasks and their event streams |
| `screen:read` | `GET /v1/devices/{id}/screenshot`. **Sensitive**: it sees everything on screen. Never a default. |
| `history:read` | `GET /v1/history`: your chat history across the dashboard and the API |
| `memory:read` | `GET /v1/memory`: the notes your agent remembers about you and your apps |
| `memory:write` | `POST /v1/memory`, `DELETE /v1/memory/{id}`: add or delete notes |

New keys get `devices:read`, `tasks:run` and `tasks:read` unless you choose otherwise.

## Tiers and credits

Every task runs at a tier. Higher tiers are smarter, slower and cost more per step.

| Tier | Credits per step |
|---|---|
| `fast` | 1 |
| `balanced` (default) | 3 |
| `best` | 10 |

A **step** is one action on the phone (a tap, a scroll, typing, opening an app). A task's credits
are `steps × tier rate` and are known when it finishes. A task cancelled before it starts costs 0.
(These are the defaults; a server operator can change them with `api_tiers` in config.yaml.)

## Limits (per key, defaults)

| Limit | Default |
|---|---|
| Requests per minute | 60 |
| Unfinished tasks at once | 2 |
| Tasks per UTC day | 200 |
| Steps per task | 40 (`max_steps` can lower it) |

A phone runs one task at a time. Further tasks wait in its queue with status `queued`.

## Endpoints

### `GET /v1/devices`, scope `devices:read`

```json
{"devices": [{"id": "app-3f2a…", "name": "Samsung SM-S928U", "platform": "android",
              "online": true, "busy": false, "last_seen": 1790000000.0}]}
```

### `POST /v1/devices/{device_id}/tasks`, scope `tasks:run`

```json
{"instruction": "open YouTube and search for lo-fi", "tier": "balanced", "max_steps": 20, "memory": "on"}
```

Returns `201` with the task (below). `memory` is `on` (the default: use your notes, and save what
you ask it to remember), `read_only` (use notes, save nothing) or `off` (incognito). API tasks never
see or extend the dashboard chat's recent requests. `instruction` is 1–2000 characters. It's always run as a
task: words like "stop" in it are never treated as commands to the phone.

### `GET /v1/tasks/{task_id}`, scope `tasks:read`

```json
{
  "id": "9c1e0b7a2f44",
  "device_id": "app-3f2a…",
  "status": "succeeded",
  "tier": "balanced",
  "instruction": "open YouTube and search for lo-fi",
  "created": 1790000000.1, "started": 1790000000.2, "finished": 1790000012.9,
  "steps": 4,
  "question": null,
  "outcome": {"success": true, "summary": "Opened YouTube and searched for lo-fi."},
  "credits": 12
}
```

`status` is one of:

| Status | Meaning | What to do |
|---|---|---|
| `queued` | Waiting for the phone | Wait |
| `running` | Working | Wait or follow the events |
| `needs_input` | Asked a question, in `question` | `POST …/answer` with `{"answer": "…"}` |
| `needs_confirmation` | About to do something risky (buy, send, delete, post…), described in `question` | The owner approves on their phone. You may decline with `{"approve": false}` |
| `succeeded` / `failed` / `cancelled` | Finished; see `outcome` and `credits` | |

`GET /v1/tasks?limit=20` lists your recent API tasks, newest first.

### `POST /v1/tasks/{task_id}/answer`, scope `tasks:run`

`{"answer": "the work account"}` for `needs_input`, or `{"approve": true}` / `{"approve": false}` for
`needs_confirmation`. Returns `409` if the task isn't waiting.

**Risky actions are approved by the phone's owner, not by the key.** When a task reaches
`needs_confirmation`, the owner gets an approval prompt on the phone and confirms with the screen
lock (no answer within 2 minutes counts as "no"). `{"approve": true}` from a key returns
`403 {"code": "confirmation_requires_owner"}`, because the model calling the API may have read the
very screen text that asks for the purchase. Declining (`{"approve": false}`) always works.

For your own headless automations you can let a key approve: turn on **Approve risky actions** for
it in `/developer`, or `PATCH /api/developer/keys/{key_id}` with `{"confirm_approve": true}` from
the signed-in console (also accepted on key creation). Keys with it on don't trigger the phone
prompt. Each change is audited.

### `POST /v1/tasks/{task_id}/cancel`, scope `tasks:run`

Returns `{"ok": true, "cancelled": true}`, or `"cancelled": false` if the task had already finished.

### `GET /v1/tasks/{task_id}/events`, scope `tasks:read`

Server-Sent Events. It replays the task's events so far, streams new ones, and ends with a
`done` event that carries the final task. To resume after a dropped connection, send
`Last-Event-ID`.

```
id: 12
event: action
data: {"id": 12, "time": 1790000003.4, "type": "action", "message": "launch_app: Launched com.google.android.youtube."}

id: 15
event: question
data: {"id": 15, "time": 1790000005.0, "type": "question", "message": "I'm about to tap 'Buy now'. Should I go ahead?", "confirmation": true}

event: done
data: {"id": "9c1e0b7a2f44", "status": "succeeded", …}
```

Event types: `status`, `thought`, `action`, `warning`, `question`, `outcome`, plus the closing `done`.

```sh
curl -N -H "Authorization: Bearer $OPENMOB_API_KEY" https://<server>/v1/tasks/$TASK/events
```

### `GET /v1/devices/{device_id}/screenshot`, scope `screen:read`

A JPEG of the phone's current screen. It goes straight from the phone to you: never stored, and
sent with `Cache-Control: no-store`. Every call is written to the audit log.

### `GET /v1/history?limit=50`, scope `history:read`

Your latest requests across the dashboard and the API, newest first: `task_id`, `device_id`,
`instruction`, `started`, `finished`, `steps`, `success`, `summary`.

### Memory: `GET /v1/memory` (`memory:read`), `POST /v1/memory` and `DELETE /v1/memory/{id}` (`memory:write`)

Short notes the agent reads before every task: about you (`app` null), or about one app (`app` is
its package name, used when that app is in front). They're plain text owned by your account, so they
work with whichever model runs the task.

```json
POST /v1/memory  {"text": "Use my second account in games"}
→ 201 {"id": "8c541d3136ac", "text": "Use my second account in games", "app": null, "status": "active"}

GET /v1/memory
→ {"notes": [{"id": "…", "text": "…", "app": "com.netease.yysls", "status": "active", "created": 1790000000.0, "uses": 3}]}
```

`status: "proposed"` marks a note the agent suggested on its own, not yet approved by you on the
"What I've learned" page (`/memory`). Proposed notes aren't used in tasks.

## Webhooks

Set a URL in `/developer`. OpenMob then POSTs JSON when a task finishes or needs you:

```json
{"type": "task.finished", "created": 1790000013.0, "task": { …same as GET /v1/tasks/{id}… }}
```

Event types: `task.finished`, `task.needs_input`, `task.needs_confirmation`. Webhook URLs must be
public `https://` addresses.

Deliveries are durable: each event is stored before it's sent, and one that fails (a non-2xx answer
or no answer within 5 s) is retried after 10 s, 30 s, 1, 2, 5 and 10 minutes, then every 15 minutes,
for about an hour in all, even across server restarts. So the same event can arrive more than once:
every attempt carries the same `X-OpenMob-Delivery: dlv_…` header, so drop ids you've already
handled. Answer with any 2xx quickly and do the work afterwards. Retries go to the URL saved at the
time; clearing the webhook drops what's still pending.

Every request is signed with your webhook's signing secret, shown once when you save the URL:

```
X-OpenMob-Signature: t=1790000013,v1=5f2c…
```

`v1` is the hex HMAC-SHA256 of `"{t}.{raw body}"`. Verify it, and reject `t` older than 5 minutes,
which stops replays:

```python
import hashlib, hmac, time

def verify(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    if abs(time.time() - int(parts["t"])) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{parts['t']}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])
```

## Safety

- **Risky actions need the owner's approval.** Taps that buy, send, delete, post and so on pause
  the task in `needs_confirmation` until the owner approves on the phone (or anyone declines).
- **A connection blip doesn't kill a task.** If the phone's connection drops mid-task, the task
  waits up to 45 s for it to come back, then fails. Taps are never repeated.
- **Sensitive apps are off-limits by default.** API tasks can't open banking, payment, wallet,
  crypto or password-manager apps. A task that finds one in front stops. The phone's owner can
  allow these in `/developer` → Safety.
- **Everything is audited.** Each key creation, task, answer, cancel and screenshot is recorded in
  an append-only log, with a hash of the instruction rather than its text. Admins can switch off a
  key or a whole account's API access, which cancels its running tasks.

## Quick start

```sh
export OPENMOB_API_KEY=om_live_…
BASE=https://<server>

DEVICE=$(curl -s -H "Authorization: Bearer $OPENMOB_API_KEY" $BASE/v1/devices | jq -r '.devices[0].id')
TASK=$(curl -s -X POST -H "Authorization: Bearer $OPENMOB_API_KEY" -H "Content-Type: application/json" \
  -d '{"instruction": "open YouTube", "tier": "fast"}' $BASE/v1/devices/$DEVICE/tasks | jq -r .id)
curl -N -H "Authorization: Bearer $OPENMOB_API_KEY" $BASE/v1/tasks/$TASK/events
```

## Data and privacy

The text of tasks and their outcomes is saved in your chat history (visible in the dashboard),
under the server's retention setting (`history_retention_days`). Screenshots are never stored.
Phone screen content is processed by third-party AI providers to carry out tasks, and the
privacy policy must say so.
