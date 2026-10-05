# Safety & approvals

An API key can operate a real phone, and the model holding it may read text that someone else
wrote. These rules hold for every key, over REST and MCP alike.

## Risky actions need the owner's approval

Before a tap that would buy, send, delete, post or similar, the task pauses with status
`needs_confirmation`, and the question says what it's about to do:

```json
{"id": "e111505afeb9", "status": "needs_confirmation",
 "question": "I'm about to tap 'Buy now'. Should I go ahead?", …}
```

The owner then gets an approval prompt on **every one of their phones that is online**, not only
the one acting: they may be holding another. The prompt names the key that is asking and the phone
that would act ("On Pixel 9: …"), and the owner confirms with that phone's screen lock. The first
answer wins and the other phones' prompts are dismissed. If nobody answers within about 2 minutes,
the answer is no. Your webhook, if you have one, gets `task.needs_confirmation`.

**Only that screen-lock prompt can approve.** Typing or saying "yes" (in the OpenMob app's chat, by
voice, or in the dashboard) never approves a risky action; "no" or "stop" still declines or stops
it. And no task may operate the OpenMob app itself: if it's on screen, the agent goes Home first,
and a task that ends up in it stops. So an agent that read malicious screen text can't open the app
and approve its own purchase.

A key can't approve on its own:

```sh
curl -s -X POST https://<server>/v1/tasks/$TASK/answer \
  -H "Authorization: Bearer $OPENMOB_API_KEY" -H "Content-Type: application/json" \
  -d '{"approve": true}'
```

```json
{"detail": "Risky actions are approved by the phone's owner, who has been asked on their phone. Check the task again later, or send {\"approve\": false} to decline.",
 "code": "confirmation_requires_owner"}
```

That's on purpose: the model calling the API may have read the very screen text that asks for the
purchase. Declining always works:

```sh
curl -s -X POST https://<server>/v1/tasks/$TASK/answer \
  -H "Authorization: Bearer $OPENMOB_API_KEY" -H "Content-Type: application/json" \
  -d '{"approve": false}'
```

```json
{"ok": true}
```

After a "no", the task doesn't tap and carries on another way, or finishes.

A tap counts as risky when it matches the server's list of risky actions (buy, pay, send, delete
and the like) or is judged risky from the screen. By default, if your instruction itself asks for
that action ("buy the headphones in my cart"), the tap counts as requested and the task doesn't
stop to ask. So put spending, sending and deleting in an instruction only when you mean it. (A
server's operator can make every risky tap ask, requested or not.)

### Let a key approve (`confirm_approve`)

For your own headless automations you can let one key approve risky actions itself. In the
developer console, go to **API keys** and turn on **Approve risky actions** for the key, or tick it
when you create the key. Then:

- The phone doesn't show an approval prompt for that key's tasks.
- `{"approve": true}` from that key goes ahead, and the MCP `next` hint tells the agent to ask its
  user and then approve or decline.
- Each change to the setting is recorded in your audit log.

Only do this for keys that run code you control. A model holding such a key could approve a
purchase it read about on the screen.

## Sensitive apps are off-limits by default

API tasks can't open banking, payment, wallet, crypto, password-manager or authenticator apps. A
task that tries gets a warning event and carries on without that app:

```
event: warning
data: {"id": 46, "time": 1791219674.43, "type": "warning", "message": "launch_app: This app is blocked for API tasks on this phone (its owner can allow it). Don't try to open it another way; call done."}
```

A task that finds one of these apps already in front stops and fails.

The owner can allow them in the developer console: **Settings** → **Safety** → **Allow API tasks
to open banking, payment, wallet and password-manager apps on my phones**. Risky taps still need
approval when it's on.

## Screens and secure locks

- **Screenshots are never stored.** `GET /v1/devices/{device_id}/screenshot` and `get_screenshot`
  send the image straight from the phone to you with `Cache-Control: no-store`. They need the
  `screen:read` scope, which is never a default, and each one is recorded in your audit log.
- **OpenMob never unlocks a secure lock screen.** Before a task (or a multi-phone step) it wakes a
  screen that's off and swipes away a swipe-only lock screen, then the task runs. A PIN, pattern,
  password or biometric lock is left alone: the task is refused with `409 phone_locked` (a step
  waits for the owner to unlock it), and OpenMob never enters a PIN, pattern, password or
  fingerprint.

## Untrusted content

A task's `question`, `outcome.summary`, event messages and screenshots can contain text from the
phone's screen, which may have been written by anyone. Treat it as data, never as instructions to
your own code or agent. Over MCP every task result carries `untrusted_content: true`. See
[MCP](mcp.md#untrusted-content).

## Everyone stays in their own account

A key reaches only its own account's phones and notes, and only the tasks and multi-phone runs
**it** started: another key's tasks, even on the same account, answer `404` exactly like an id that
doesn't exist (getting, listing, events, answering, cancelling, and `GET /v1/history`). Other
accounts' phones and tasks can't even be confirmed to exist. The account owner's dashboard and
developer console still show every key's work. Rotating a key keeps it the same key, with its tasks.

## The owner stays in control

- The phone shows which key is using it while an API task runs, and the owner can stop the task
  from the phone (Stop, or saying "stop").
- For a [multi-phone run](api.md#orchestrations), stopping on a phone that runs one of its steps,
  or denying one of its approvals, ends the **whole run** as `cancelled` with reason
  `stopped_on_phone`, whatever its `on_failure` says. Don't start it again unless the owner asks.
- **Revoking** a key in **API keys** cancels its unfinished tasks at once.
- A server's admin can switch off a key or a whole account's API access, which also cancels its
  running tasks. Requests then get `403`.

## Audit log

Every key change, task, answer, approval, cancel, screenshot, memory change, webhook delivery and
setting change is written to an append-only audit log. Your account's entries are under **Logs** in
the developer console, with the key that did each one. The log stores a hash of each instruction,
never its text.

## Data and privacy

What's kept, where, and for how long (`history_retention_days`, **30 days** by default; `0` keeps
everything forever; one job deletes all of it every 6 hours):

| What | Where | Kept |
|---|---|---|
| Task instructions, outcome summaries, events and step records | Chat history (`history.sqlite`), shown in the dashboard and, for a key's own tasks, `GET /v1/history` | Until the chat hasn't been used for the retention period |
| Multi-phone run plans (instructions) and step results | `orchestrations` in the ledger | The words are blanked after the retention period; statuses, reasons and timing stay |
| Webhook bodies (a snapshot of the task or run) | The webhook outbox in the ledger | Delivered or dead deliveries: the retention period. Pending ones until delivered or given up (about an hour) |
| Idempotency replays (the first response) | The ledger | 24 hours |
| Live task records | Server memory | Until they age out (200 per phone) or the server restarts |
| Billing totals: task id, key, phone, tier, steps, credits, status, an instruction **hash** | `api_tasks` in the ledger | Kept (no text) |
| Audit log (actions, with instruction **hashes**) | The ledger | Kept (no text) |
| Memory notes | `memory.sqlite` | Until you delete them (they're your saved knowledge, not task logs) |

- Screenshots are never stored.
- To carry out a task, the phone's screen content is processed by the AI providers that power
  OpenMob's tiers. The API itself never names them: you choose a tier, not a model.
