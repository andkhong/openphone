# Errors & limits

Errors are JSON: `{"detail": "…"}`, plus a machine-readable `code` where one applies. Over MCP the
same errors arrive as HTTP errors (authentication, limits) or as tool errors (everything else); see
[MCP](mcp.md#5-errors).

## HTTP status codes

| Status | Meaning | What to do |
|---|---|---|
| 400 | The request is malformed: a bad tier, an empty instruction, an answer the task can't take | Fix the request |
| 401 | Missing, malformed, wrong or revoked key | Check `Authorization: Bearer om_live_…` |
| 403 | The key lacks the scope, or API access was switched off for the key or account | Add the scope (create a new key), or ask the server's admin |
| 403 `confirmation_requires_owner` | A key tried to approve a risky action | The owner approves on the phone, or turn on **Approve risky actions** for the key |
| 403 `insufficient_scope` | The key lacks the scope this route needs | Create a key with that scope |
| 404 | No such phone or task **for your account** (other accounts' things look the same) | Check the id |
| 409 | The phone is offline, or the task isn't in a state for that action | Wait, or check the task's `status` |
| 429 | Rate limit, concurrent-task cap or daily quota reached | Wait for `Retry-After` seconds |
| 503 | The phone couldn't be reached for a screenshot | Try again |

## Task outcomes

A task that runs but doesn't reach its goal ends with `status: "failed"` and an
`outcome.summary` saying why. That isn't an HTTP error: the request itself succeeded. Tasks that
lose their phone mid-task wait up to 45 s for it to come back before failing.

## Limits (per key, defaults)

| Limit | Default |
|---|---|
| Requests per minute | 60 |
| Unfinished tasks at once | 2 |
| Tasks per UTC day | 200 |
| Steps per task | 40 (`max_steps` can lower it) |

The limits apply across REST and MCP together. A server operator can change them; the current
values are on the **Overview** page of `/developer`. Credits per step by tier are in the
[API reference](api.md#tiers-and-credits).
