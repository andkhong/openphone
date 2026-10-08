# API conventions

The rules that hold across the whole API.

## Requests and responses

- **JSON in, JSON out.** Send `Content-Type: application/json` with a body. Responses are JSON,
  except the screenshot (JPEG) and the event stream (Server-Sent Events).
- **Timestamps** are Unix time in seconds, as numbers with a fractional part (`1791219561.83`). They
  are UTC; daily limits reset at midnight UTC (`resets_at`).
- **Unknown fields** in responses may appear over time (see [Versioning](#versioning)): ignore
  fields you don't know rather than failing on them. The same goes for new webhook event types and
  new `reason` values.
- **Sizes** are limited per field rather than per request: an instruction is 1–2000 characters, an
  answer up to 500, a webhook URL up to 500, an `Idempotency-Key` up to
  255, and an orchestration up to 10 steps. API key names are trimmed to 60 characters.

## Ids

Treat ids as opaque strings; these formats are for recognising them, not parsing them.

| Object | Format | Example |
|---|---|---|
| Task | 12 hex characters | `bce01b5f5406` |
| Orchestration | `orc_` + 16 hex | `orc_c33d8d6dd6d642db` |
| API key id | 8 hex; the key itself is `om_live_<id>_<secret>` | `2a6e6485` |
| Phone | `app-` + the id the phone registered with | `app-3f2a9c` |
| Webhook delivery | `dlv_` + 24 hex | `dlv_bb641c8f1e9f03e95c279497` |
| Webhook signing secret | `whsec_` + a random string | |

## Calling from a browser

The API sends no CORS headers, so browsers block calls to it from web pages on other sites. That's
deliberate: an API key operates real phones and must never reach a browser. Call the API from your
server, and have your web page talk to your server.

## Rate-limit headers

The only rate-limit header is `Retry-After` (seconds), on the `429` responses that have a known
wait. There are no `X-RateLimit-*` headers: read your limits and today's use with
[`GET /v1/limits`](api.md#get-v1limits) instead.

## Versioning

The API is versioned in the path: `/v1`. Within v1, changes are **additive only**: new endpoints,
new optional request fields, new response fields, new event types, new `code` and `reason`
values. Anything that would break a correct v1 client (removing or renaming a field, changing a
type or a meaning) will come as a new version, `/v2`, with `/v1` kept running alongside it.

## Retries

Retry only when it's safe, wait before retrying, and **always send an `Idempotency-Key`** when you
retry `POST /v1/devices/{device_id}/tasks` or `POST /v1/orchestrations`, so a retry can never start
a second task (see [Idempotent retries](api.md#idempotent-retries)).

| Response | Retry? |
|---|---|
| Network error or timeout | Yes, with the same `Idempotency-Key`, after a backoff |
| `400`, `401`, `403`, `404`, `422` | No: fix the request, key or scope first |
| `409 phone_offline` | Only after the owner reconnects the phone |
| `409 phone_locked` | Only after the owner unlocks the phone |
| `409 phone_unresponsive` | Yes, after a pause (the phone may be waking up), a few times at most |
| `409 idempotency_in_progress` | Yes, after a second or two: you'll get the first request's response |
| `409 idempotency_conflict` | No: you reused a key for a different request |
| `409` without a `code` (the task isn't waiting, for example) | No: read the task's `status` |
| `429` with `Retry-After` (rate limit) | Yes, after `Retry-After` seconds |
| `429 too_many_tasks` | Yes, once one of the key's tasks has finished (there's no `Retry-After`) |
| `429 too_many_orchestrations` | Yes, once one of the runs has ended |
| `429 daily_quota`, `429 credits_exhausted` | Not before `resets_at` (midnight UTC) |
| `503`, and other `5xx` | Yes, with backoff |

`GET` requests and cancels are always safe to repeat. Repeating an answer is harmless: once the task
has its answer, another gets `409`. The [code examples](examples.md) implement this table.

## Task lifecycle

```
 create
   │
   ▼
 queued ──── starts ────▶ running ──── finishes ────▶ succeeded or failed
                           │   ▲
                      asks │   │ answered, approved or declined
                           ▼   │
               needs_input / needs_confirmation ──── question timeout ────▶ failed

 Any unfinished status ──── cancel, Stop on the phone, key revoked ────▶ cancelled
```

| From | To | When |
|---|---|---|
| (new) | `queued` | The task was created; it waits for the phone to be free |
| `queued` | `running` | The phone started it |
| `queued` | `cancelled` | Cancelled before it started (costs 0), or the phone was lost and didn't come back |
| `running` | `needs_input` | It asked a question |
| `running` | `needs_confirmation` | It wants approval for a risky action |
| `needs_input`, `needs_confirmation` | `running` | Answered, approved or declined |
| `needs_input` | `failed` | Nobody answered within the question timeout (`outcome.reason: "question_timeout"`) |
| `needs_confirmation` | `running` | Nobody approved within about 2 minutes: that counts as declined, and the task carries on without the risky action |
| `running` | `succeeded` / `failed` | It finished, or gave up, ran out of steps, lost the phone, or hit an error |
| any unfinished | `cancelled` | Cancelled over the API, stopped on the phone, or its key was revoked or switched off |

`succeeded`, `failed` and `cancelled` are final: a finished task never changes again.
