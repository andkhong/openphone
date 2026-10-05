# Quickstart

Run your first task on your own phone from code or from an AI agent in about five minutes. You
need an OpenMob account with a paired phone (Dashboard → account menu → Connect a phone).

## 1. Create a key

In **`/developer` → API keys**, create a key. The default scopes (`devices:read`, `tasks:run`,
`tasks:read`) are enough to run tasks. The key is shown **once**: copy it into an environment
variable.

```sh
export OPENMOB_API_KEY=om_live_…
export OPENMOB_SERVER=https://<server>
```

## 2. Find your phone

```sh
curl -s -H "Authorization: Bearer $OPENMOB_API_KEY" $OPENMOB_SERVER/v1/devices
```

```json
{"devices": [{"id": "app-3f2a…", "name": "Pixel 9", "platform": "android", "online": true, "busy": false}]}
```

## 3. Run a task and follow it

```sh
TASK=$(curl -s -X POST -H "Authorization: Bearer $OPENMOB_API_KEY" -H "Content-Type: application/json" \
  -d '{"instruction": "open YouTube and search for lo-fi", "tier": "fast"}' \
  $OPENMOB_SERVER/v1/devices/app-3f2a…/tasks | jq -r .id)

curl -N -H "Authorization: Bearer $OPENMOB_API_KEY" $OPENMOB_SERVER/v1/tasks/$TASK/events
```

The event stream shows each step as it happens and ends with a `done` event carrying the finished
task: its `status`, `outcome.summary`, `steps` and `credits`. Every task also appears in
**`/developer` → Sessions**, with its timeline.

## 4. Or let an AI agent do it

The same key works over MCP. In Claude Code:

```sh
claude mcp add --transport http openmob $OPENMOB_SERVER/mcp \
  --header "Authorization: Bearer $OPENMOB_API_KEY"
```

Then ask it: "On my phone, open YouTube and play some lo-fi." Cursor, Claude Desktop, the Claude
API and the OpenAI API are covered in [MCP](mcp.md).

## What to read next

- [API reference](api.md): every endpoint, task statuses, tiers and credits.
- [Webhooks](api.md#webhooks): get called when a task finishes or needs you.
- [Errors & limits](errors.md): what each error means and the per-key limits.
- [Safety](api.md#safety): risky actions, sensitive apps and the audit log.
