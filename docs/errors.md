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
| `409` | The phone isn't usable (`phone_offline`, `phone_locked`, `phone_unresponsive`), an `Idempotency-Key` clash, or the task isn't in a state for that action (for example, answering a task that isn't waiting) | See the `code` below, or check the task's `status` |
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
| `too_many_tasks` | `429` | The key already has its maximum of unfinished tasks | Wait for one to finish, or cancel one |
| `daily_quota` | `429` | The key's tasks for today (UTC) are used up | Wait for `Retry-After` (midnight UTC) |
| `credits_exhausted` | `429` | The key's credits for today (UTC) are used up (only when the server sets a daily credit cap) | Wait for `Retry-After` (midnight UTC) |
| `too_many_orchestrations` | `429` | The key already has 3 orchestrations in progress, or the account 10 | Wait for one to end, or cancel one |
| `idempotency_conflict` | `409` | This `Idempotency-Key` was already used (within 24 h) for a different request | Use a new key for a new request |
| `idempotency_in_progress` | `409` | A request with this `Idempotency-Key` is still being handled | Retry in a moment to get its response |
| `phone_offline` | `409` | The phone isn't connected | Open the OpenMob app on the phone |
| `phone_locked` | `409` | The phone is locked or its screen is off | Unlock the phone, then retry |
| `phone_unresponsive` | `409` | The phone is connected but didn't answer in time (often: asleep) | Wake the phone and open the OpenMob app |

The two daily-limit `429`s (`daily_quota`, `credits_exhausted`) also carry `resets_at`: the Unix
time of the next midnight UTC, when the limits reset.

```json
{"detail": "Daily task quota reached for this key.", "code": "daily_quota", "resets_at": 1791244800.0}
```

## Task outcomes aren't errors

A task that runs but doesn't reach its goal ends with `status: "failed"` and an `outcome.summary`
saying why, for example a blocked app, a phone that didn't reconnect within 45 seconds, or running
out of steps. The HTTP request itself succeeded. When something outside the task's own work ended
it, `outcome.reason` says what: `question_timeout`, `phone_unresponsive`, `phone_error`,
`internal_error` or `server_restarted`. See [the task object](api.md#the-task-object).

## Limits

Each API key has these limits, shared between REST and MCP:

| Limit | Default | When you hit it |
|---|---|---|
| Requests per minute | 60 | `429` `"Rate limit reached. Slow down."` with `Retry-After` (seconds) |
| Unfinished tasks at once | 2 | `429` `too_many_tasks` |
| Tasks per day (UTC) | 200 | `429` `daily_quota`, with `Retry-After` and `resets_at` (midnight UTC) |
| Credits per day (UTC) | Unlimited unless the server sets one | `429` `credits_exhausted`, with `Retry-After` and `resets_at`. Running tasks count; waived credits don't |
| Orchestrations in progress | 3 per key, 10 per account | `429` `too_many_orchestrations` |
| Steps per orchestration | 10 | `400` |
| Steps per task | 40 | Larger `max_steps` values are capped. A task that runs out of steps fails |
| API keys per account | 20 | `400` when creating another. Revoke one first |
| Memory notes per account | 500 | `400` when adding another |

- Every authenticated request counts towards the per-minute limit, including polls of
  `GET /v1/tasks/{task_id}` and each MCP request (however long it waits). Prefer the event stream,
  webhooks or MCP's `wait_seconds` to tight polling.
- A phone also runs one task at a time; tasks for a busy phone wait in its queue as `queued`. They
  count as unfinished.
- [`GET /v1/limits`](api.md#get-v1limits) returns this key's limits, what it has used today and
  `resets_at`.
- A server's operator can change these limits. The current values, and your usage today and over
  the last 30 days, are also on the **Overview** page of the developer console.

## Credits

A finished task costs `steps` × its tier's credits per step (1 for `fast`, 3 for `balanced`, 10
for `best`, by default). A task cancelled before it starts costs 0; one cancelled mid-way pays for
the steps it took. A task that ends through OpenMob's own fault (`internal_error`,
`server_restarted`) costs 0, and the waived amount doesn't count toward a daily credit limit. See
[Tiers and credits](concepts.md#tiers-and-credits).
