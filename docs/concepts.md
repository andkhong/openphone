# Concepts

OpenMob lets you, and the AI agents you trust, operate **your own phones** from code. You describe
a goal in plain words ("check my last Uber receipt") and OpenMob's agent carries it out on the
phone: it reads the screen, taps, types and opens apps until the goal is done. You get back the
outcome, a timeline of what it did, and what it cost.

There are two ways in, with the same keys, scopes, limits and audit log:

| Way in | Address | For |
|---|---|---|
| REST API | `https://<server>/v1` | Your own code, scripts and backends |
| MCP server | `https://<server>/mcp` | AI agents: Claude Code, Cursor, Claude Desktop, the Claude and OpenAI APIs |

Today the API reaches only the phones paired to your own account.

## Phones

A **phone** is an Android device paired to your account through the **OpenMob app**. To pair one,
click the account button (bottom left of the dashboard) → **Connect a phone**. Its id looks like
`app-3f2a9c`; list your phones with [`GET /v1/devices`](api.md#get-v1devices).

The app is how OpenMob sees and operates the phone (through Android's accessibility service), so a
phone can only run tasks while:

- **It is online.** The OpenMob app is running and connected (`"online": true`). Creating a task
  for an offline phone fails with `409`.
- **It is unlocked.** At the start of each task OpenMob wakes the screen and dismisses a simple
  swipe lock, but it never enters a PIN, pattern, password or fingerprint. If a secure lock screen
  is showing, the task sees it and asks you to unlock the phone, or fails.

A phone runs **one task at a time**. Tasks you start while it's busy (from the API or from the
dashboard) wait in its queue with status `queued`. `"busy": true` means it's working right now.

While an API task runs, the phone shows who is using it (the API key's name), and the phone's owner
can stop the task from the phone at any time.

If the phone's connection drops mid-task (Wi-Fi to mobile data, a tunnel hiccup), the task pauses
and waits up to **45 seconds** for it to come back, then carries on. Nothing is repeated. If the
phone doesn't come back in time, the running task fails and its queued tasks are cancelled.

## Tasks

A **task** is one instruction run on one phone, from start to finish. You create it with
`POST /v1/devices/{device_id}/tasks` (or the `run_task` MCP tool) and follow it until it finishes.

| Status | Meaning | What to do |
|---|---|---|
| `queued` | Waiting for the phone to be free | Wait |
| `running` | Working on the phone | Wait, or follow its events |
| `needs_input` | Asked a question, in `question` | Answer it: `{"answer": "…"}` |
| `needs_confirmation` | About to do something risky (buy, send, delete, post…), described in `question` | The phone's owner approves it on the phone. You may decline: `{"approve": false}` |
| `succeeded` | Finished and reached the goal | Read `outcome.summary` |
| `failed` | Finished without reaching the goal, or hit an error | Read `outcome.summary` |
| `cancelled` | Stopped by a cancel, by the owner, or by a revoked key | |

`succeeded`, `failed` and `cancelled` are final. A finished task has an `outcome` with `success`
and a `summary`, and its `credits`.

A task that runs but doesn't reach its goal is `failed`. That isn't an HTTP error: the request
itself succeeded.

The instruction is always treated as a task. Words like "stop" or "yes" in it are never read as
commands to the phone or as answers.

### Following a task

Three ways, from simplest to most efficient:

- **Poll** `GET /v1/tasks/{task_id}`. Each poll counts against your rate limit (60 requests a
  minute by default), so poll every few seconds, not in a tight loop.
- **Stream** `GET /v1/tasks/{task_id}/events` (Server-Sent Events): every step as it happens, then
  a `done` event with the final task.
- **Webhooks**: OpenMob calls your URL when a task finishes or needs you. See [Webhooks](webhooks.md).

Over MCP, `run_task` and `get_task` wait for you (up to 45 seconds per call). See
[MCP](mcp.md#long-running-tasks).

## Steps

A **step** is one turn of OpenMob's agent: one action such as opening an app, tapping, typing,
scrolling, asking you something, or reporting the result. A short task takes a few steps; the task
in the [quickstart](quickstart.md) took 4 (open YouTube, tap Search, type "lo-fi", report the result).

Each task stops after at most **40 steps** by default. Pass `max_steps` to stop sooner; larger
values are capped at the server's maximum. A task that runs out of steps fails ("I gave up after
40 steps.").

## Tiers and credits

Every task runs at a **tier**. Higher tiers are smarter and more reliable on hard tasks, slower,
and cost more per step.

| Tier | Credits per step | Good for |
|---|---|---|
| `fast` | 1 | Simple, well-defined tasks: open an app, search, toggle a setting |
| `balanced` (default) | 3 | Most tasks |
| `best` | 10 | Long or tricky tasks across several apps |

A task costs **steps × its tier's rate** in credits, known when it finishes (`credits` is `null`
until then). A task cancelled before it starts costs 0; one cancelled mid-way costs the steps it
took. These are the defaults: a server's operator can change the tiers and their rates, and the
current values are on the **Overview** page of the developer console.

## Sessions

The developer console lists every task your API keys and AI agents ran under **Sessions**: status,
instruction, phone, key, tier, steps, credits and duration, live while they run. Open one for its
full timeline of events. Each session is a task: the same id as `GET /v1/tasks/{task_id}`.

The console's **Logs** page is your account's audit log: every key change, task, answer, cancel,
screenshot and setting change, with the key that did it.

## Memory

OpenMob can keep short notes about you and your apps ("Use my work account for Uber receipts") and
read them before each task. You choose per task whether a task uses them. See [Memory](memory.md).

## Safety

Risky actions need the owner's approval on the phone, banking and password apps are off-limits by
default, and screenshots are never stored. See [Safety & approvals](safety.md).
