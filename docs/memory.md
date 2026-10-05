# Memory

OpenMob can remember short notes about you and your apps, and reads them before each task, so you
don't have to repeat yourself:

```sh
curl -s -X POST https://<server>/v1/memory \
  -H "Authorization: Bearer $OPENMOB_API_KEY" -H "Content-Type: application/json" \
  -d '{"text": "Use my work account for Uber receipts"}'
```

```json
{"id": "799fd944c960", "text": "Use my work account for Uber receipts", "app": null, "status": "active"}
```

From then on, a task like "find my last Uber receipt" uses the work account without asking.

Notes are plain text that belongs to your account. They work the same at every tier, and your
own agents can read them through the API.

## Two kinds of note

| Kind | `app` | Read |
|---|---|---|
| About you | `null` | Before every task (up to 40 notes) |
| About one app | Its Android package name, e.g. `com.ubercab` | When that app is in front (up to 25 notes) |

```json
{"notes": [
  {"id": "cdf1a8e1530a", "text": "Use my second account in games", "app": null, "status": "active", "created": 1791219552.32, "uses": 8},
  {"id": "ba252e793f8d", "text": "Daily quests are under the scroll icon, top right", "app": "com.netease.yysls", "status": "active", "created": 1791219552.32, "uses": 0}
]}
```

`uses` counts the tasks that read the note.

## Active notes and proposals

A note's `status` is either:

- **`active`**: used in tasks.
- **`proposed`**: the agent suggested it, and it isn't used until you approve it.

How a note is saved decides which:

| Who saves it | Status |
|---|---|
| You, on the **What I've learned** page (account button → **What I've learned**) | `active` |
| Your API key, with `POST /v1/memory` or the `remember` MCP tool | `active` |
| A task whose instruction asks to remember something ("remember that…", "from now on…", "always…", "never…") | `active` |
| A task that decided on its own that something is worth remembering | `proposed` |

Proposals wait on the **What I've learned** page, where you **Keep** or **Dismiss** each one. This
rule means text the agent read on the screen (a web page, a message from someone else) can at most
*propose* a note: it can never plant an instruction for your future tasks by itself.

When a task saves or proposes a note, its event stream shows a `memory` event:

```
event: memory
data: {"id": 67, "time": 1791219687.39, "type": "memory", "message": "Remembered: Prefers dark mode in every app"}
```

## Memory modes for a task

Choose per task with the `memory` field of `POST /v1/devices/{device_id}/tasks` (or the `run_task`
MCP tool):

| `memory` | Reads your notes | Saves or proposes notes |
|---|---|---|
| `on` (default) | Yes | Yes |
| `read_only` | Yes | No. The task is told "Memory is off for this task, so nothing was saved." |
| `off` | No | No: an incognito task |

Use `read_only` for automations that should benefit from your notes without changing them, and
`off` when a task must not depend on anything personal.

## Scopes

| Scope | Allows |
|---|---|
| `memory:read` | `GET /v1/memory`, the `list_memory` tool |
| `memory:write` | `POST /v1/memory`, `DELETE /v1/memory/{note_id}`, the `remember` tool |

Neither is a default scope. A task uses your notes (per its `memory` mode) whatever scopes its key
has: the scopes only control reading and writing notes directly.

## Limits and rules

- A note is 1–300 characters. Extra whitespace is collapsed.
- An account has at most 500 notes.
- Saving the same text again (for the same app) returns the existing note instead of a duplicate.
- Deleting a proposal rejects it.
- Adding and deleting notes is recorded in your audit log (**Logs** in the developer console),
  without the note's text.

Endpoint details: [`GET /v1/memory`](api.md#get-v1memory), [`POST /v1/memory`](api.md#post-v1memory),
[`DELETE /v1/memory/{note_id}`](api.md#delete-v1memorynote_id). Over MCP: [`list_memory`](mcp.md#list_memory)
and [`remember`](mcp.md#remember).
