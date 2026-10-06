# Quickstart

Run your first task on your own phone, first with curl and then from an AI agent, in about five
minutes.

You need an OpenMob account with a paired phone that is **online** (the OpenMob app is open and
connected) and **unlocked**. To pair one, open the dashboard and click the account button (bottom
left) → **Connect a phone**.

## 1. Create an API key

Click the account button (bottom left) → **Developer API & AI agents** → **API keys** →
**Create key**. The default scopes (`devices:read`, `tasks:run`, `tasks:read`) are enough to run
tasks. The key is shown **once**, so copy it into environment variables:

```sh
export OPENMOB_API_KEY=om_live_…
export OPENMOB_SERVER=https://<server>
```

## 2. Find your phone

```sh
curl -s -H "Authorization: Bearer $OPENMOB_API_KEY" $OPENMOB_SERVER/v1/devices
```

```json
{"devices": [{"id": "app-3f2a9c", "name": "Pixel 9", "platform": "android",
              "online": true, "busy": false, "last_seen": 1791219552.32}]}
```

## 3. Run a task and follow it

```sh
DEVICE=app-3f2a9c
TASK=$(curl -s -X POST $OPENMOB_SERVER/v1/devices/$DEVICE/tasks \
  -H "Authorization: Bearer $OPENMOB_API_KEY" -H "Content-Type: application/json" \
  -d '{"instruction": "open YouTube and search for lo-fi", "tier": "fast"}' | jq -r .id)

curl -N -H "Authorization: Bearer $OPENMOB_API_KEY" $OPENMOB_SERVER/v1/tasks/$TASK/events
```

The event stream shows each step as it happens and ends with a `done` event that carries the
finished task:

```
id: 4
event: action
data: {"id": 4, "time": 1791219561.84, "type": "action", "message": "launch_app: Launched com.google.android.youtube."}

id: 8
event: outcome
data: {"id": 8, "time": 1791219561.85, "type": "outcome", "message": "Opened YouTube and searched for lo-fi.", "success": true}

event: done
data: {"id": "bce01b5f5406", "device_id": "app-3f2a9c", "status": "succeeded", "tier": "fast", "steps": 4,
       "outcome": {"success": true, "summary": "Opened YouTube and searched for lo-fi.", "reason": null}, "credits": 4, …}
```

This task took 4 steps at the `fast` tier (1 credit per step), so it cost 4 credits. It also
appears under **Sessions** in the developer console, with its full timeline.

If the task asks a question (`needs_input`) or wants approval for something risky
(`needs_confirmation`), see [How a task runs](concepts.md#tasks).

## 4. Let an AI agent do it (MCP)

The same key works over MCP. In Claude Code:

```sh
claude mcp add --transport http openmob $OPENMOB_SERVER/mcp \
  --header "Authorization: Bearer $OPENMOB_API_KEY"
```

Then ask it: "On my phone, open YouTube and play some lo-fi." The agent calls `run_task`, waits for
the result, and tells you what happened. Cursor, Claude Desktop, the Claude API and the OpenAI API
are covered in [MCP](mcp.md).

## What to read next

- [Concepts](concepts.md): phones, tasks, steps, tiers, credits and sessions.
- [API reference](api.md): every endpoint, with requests and responses.
- [Webhooks](webhooks.md): get called when a task finishes or needs you, instead of polling.
- [Safety & approvals](safety.md): risky actions, sensitive apps and what is stored.
- [Errors & limits](errors.md): what each error means, and the per-key limits.
- See also: [Set up a phone](phone-setup.md), [Code examples](examples.md), [Testing without a phone](testing.md) and [Run your own server](self-hosting.md).
