# Errors & limits

Errors are JSON with a human-readable `detail`, plus a stable machine-readable `code` where one
applies. Branch on the status and `code`, not on the wording of `detail`.

```json
{"detail": "This API key lacks the screen:read scope.", "code": "insufficient_scope"}
```

Over MCP, authentication and rate-limit errors arrive as HTTP errors and everything else as tool
errors. See [MCP errors](mcp.md#errors).

## Status codes

| Status | Meaning | What to do |
|---|---|---|
| `400` | The request can't be carried out as asked: an unknown tier, an answer the task can't take (`answer` for a confirmation, or `approve` missing), or too many memory notes | Fix the request |
| `401` | Missing, malformed, wrong or revoked API key. Comes with `WWW-Authenticate: Bearer` | Send `Authorization: Bearer om_live_…` with a live key |
| `403` | See the `code` below. Without a `code`: an admin switched off API access for this key or account | Ask the server's admin |
| `404` | No such phone, task or note **on your account**. Other accounts' phones and tasks look exactly the same | Check the id |
| `409` | The phone is offline, or the task isn't in a state for that action (for example, answering a task that isn't waiting) | Open the OpenMob app on the phone, or check the task's `status` |
| `422` | The body failed validation: an empty or too long `instruction`, `max_steps` below 1, an unknown `memory` mode, a note over 300 characters | Fix the field named in `detail` |
| `429` | Rate limit, unfinished-task cap or daily quota reached | Wait. See `Retry-After` and [Limits](#limits) |
| `501` | Memory isn't enabled on this server | Use the other endpoints |
| `503` | The phone couldn't capture a screenshot right now | Try again |

`422` responses list each problem in `detail`:

```json
{"detail": [{"type": "string_too_short", "loc": ["body", "instruction"],
             "msg": "String should have at least 1 character", "input": "", "ctx": {"min_length": 1}}]}
```

## Error codes

| `code` | Status | Meaning | What to do |
|---|---|---|---|
| `insufficient_scope` | `403` | The key lacks the scope this route or tool needs; `detail` names it | Create a key with that scope (scopes can't be added to an existing key) |
| `confirmation_requires_owner` | `403` | The key tried to approve a risky action, which only the phone's owner may do | Wait for the owner to approve on the phone, or decline with `{"approve": false}`. The owner can turn on **Approve risky actions** for the key: see [Safety & approvals](safety.md#let-a-key-approve-confirm_approve) |

## Task outcomes aren't errors

A task that runs but doesn't reach its goal ends with `status: "failed"` and an `outcome.summary`
saying why, for example a blocked app, a phone that didn't reconnect within 45 seconds, or running
out of steps. The HTTP request itself succeeded.

## Limits

Each API key has these limits, shared between REST and MCP:

| Limit | Default | When you hit it |
|---|---|---|
| Requests per minute | 60 | `429` `"Rate limit reached. Slow down."` with `Retry-After` (seconds) |
| Unfinished tasks at once | 2 | `429` `"This key already has 2 unfinished tasks. Wait for one to finish or cancel it."` |
| Tasks per day (UTC) | 200 | `429` `"Daily task quota reached for this key."` with `Retry-After` until midnight UTC |
| Steps per task | 40 | Larger `max_steps` values are capped. A task that runs out of steps fails |
| API keys per account | 20 | `400` when creating another. Revoke one first |
| Memory notes per account | 500 | `400` when adding another |

- Every authenticated request counts towards the per-minute limit, including polls of
  `GET /v1/tasks/{task_id}` and each MCP request (however long it waits). Prefer the event stream,
  webhooks or MCP's `wait_seconds` to tight polling.
- A phone also runs one task at a time; tasks for a busy phone wait in its queue as `queued`. They
  count as unfinished.
- A server's operator can change these limits. The current values, and your usage today and over
  the last 30 days, are on the **Overview** page of the developer console.

## Credits

A finished task costs `steps` × its tier's credits per step (1 for `fast`, 3 for `balanced`, 10
for `best`, by default). A task cancelled before it starts costs 0; one cancelled mid-way pays for
the steps it took. See [Tiers and credits](concepts.md#tiers-and-credits).
