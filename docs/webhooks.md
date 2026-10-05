# Webhooks

Instead of polling, let OpenMob call you when a task finishes or needs you. OpenMob sends a signed
`POST` to your URL:

```
POST /openmob-webhook HTTP/1.1
Content-Type: application/json
User-Agent: OpenMob-Webhooks/1
X-OpenMob-Event: task.needs_confirmation
X-OpenMob-Delivery: dlv_bb641c8f1e9f03e95c279497
X-OpenMob-Signature: t=1791219672,v1=95481291e9052f921b30e20e48fd65c305e0805f883c830301aac4d8b01fcea6

{"type": "task.needs_confirmation", "created": 1791219672.49, "task": {"id": "e111505afeb9", "device_id": "app-3f2a9c",
 "status": "needs_confirmation", "tier": "balanced", "instruction": "check out the headphones in my cart",
 "created": 1791219672.48, "started": 1791219672.48, "finished": null, "steps": 0,
 "question": "I'm about to tap 'Buy now'. Should I go ahead?", "outcome": null, "credits": null}}
```

## Set it up

In the developer console, open **Webhooks**, enter your URL and click **Save**. The URL must be a
public `https://` address (up to 500 characters). Saving shows the **signing secret**
(`whsec_…`) **once**: store it with your receiver's configuration.

- Saving a URL again, even the same one, makes a new signing secret.
- **Remove** stops all deliveries, including retries still pending.
- One webhook per account. It receives events for tasks started by any of the account's keys,
  over REST or MCP.

## Events

| `type` | Sent when |
|---|---|
| `task.finished` | A task ended: `succeeded`, `failed` or `cancelled` |
| `task.needs_input` | A task asked a question (`needs_input`) |
| `task.needs_confirmation` | A task wants approval for a risky action (`needs_confirmation`) |

The body is `{"type", "created", "task"}`. `created` is when the event happened, and `task` is the
[task object](api.md#the-task-object) as it was at that moment, the same shape as
`GET /v1/tasks/{task_id}`.

Events are sent for tasks started through the API or MCP, not for tasks you start from the
dashboard or the phone.

## Verify the signature

Every request carries `X-OpenMob-Signature: t=<unix time>,v1=<signature>`, where `v1` is the hex
HMAC-SHA256 of `"{t}.{raw body}"` keyed with your signing secret. Check it against the **raw**
request body (before any JSON parsing), compare in constant time, and reject a `t` more than 5
minutes old, which stops replays.

Python:

```python
import hashlib, hmac, time

def verify(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    if abs(time.time() - int(parts["t"])) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{parts['t']}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])
```

Node.js:

```js
const crypto = require("crypto");

function verify(secret, header, rawBody, tolerance = 300) {
  const parts = Object.fromEntries(header.split(",").map((p) => p.split("=", 2)));
  if (Math.abs(Date.now() / 1000 - Number(parts.t)) > tolerance) return false;
  const expected = crypto.createHmac("sha256", secret).update(`${parts.t}.`).update(rawBody).digest("hex");
  return expected.length === parts.v1.length &&
    crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1));
}
```

A request is signed when it is sent, so a retry carries a new `t` and a new signature.

## Respond quickly

Answer with any `2xx` status within **5 seconds**, then do the work. Anything else counts as a
failed attempt: a non-2xx status, a timeout, a connection error, or a redirect (redirects aren't
followed).

## Retries

Each event is stored before it is sent, so deliveries survive server restarts. A failed attempt is
retried after 10 s, 30 s, 1 min, 2 min, 5 min, 10 min, then 15 min three times: 10 attempts over
about an hour. After the last one the delivery is given up.

Retries go to the webhook URL saved at the time of the retry. If the URL stops resolving to a
public address, the delivery is blocked. Delivered, failed and blocked deliveries appear under
**Logs** in the developer console.

## Idempotency and ordering

The same event can arrive more than once (for example, when your `2xx` was lost). Every attempt of
one event carries the same `X-OpenMob-Delivery: dlv_…` id, so record the ids you've handled and
skip repeats.

Deliveries can arrive out of order, especially after retries. Use the event's `created` time and
the task's `status`: a `task.finished` is final for its task, so ignore an older
`task.needs_input` that arrives after it. When in doubt, read the task's current state with
`GET /v1/tasks/{task_id}`.

## Example receiver

```python
from flask import Flask, abort, request

app = Flask(__name__)
SECRET = "whsec_…"
seen = set()  # use a database in production

@app.post("/openmob-webhook")
def openmob_webhook():
    if not verify(SECRET, request.headers.get("X-OpenMob-Signature", ""), request.get_data()):
        abort(400)
    delivery = request.headers["X-OpenMob-Delivery"]
    if delivery not in seen:
        seen.add(delivery)
        event = request.get_json()
        task = event["task"]
        if event["type"] == "task.needs_input":
            print("Task", task["id"], "asks:", task["question"])  # untrusted text from the screen
        elif event["type"] == "task.finished":
            print("Task", task["id"], task["status"], task["outcome"]["summary"])
    return "", 204
```
