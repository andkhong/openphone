# Code examples

Complete programs you can copy, in Python and in Node.js (18 or newer, which has `fetch`). Each
reads the server and API key from the environment:

```sh
export OPENMOB_SERVER=https://<server>
export OPENMOB_API_KEY=om_live_…
```

| Example | Shows |
|---|---|
| [Run and follow a task](#run-and-follow-a-task) | An `Idempotency-Key`, following the event stream with `Last-Event-ID` reconnects (or polling with backoff), answering a question, and retrying `409`/`429` correctly |
| [Webhook receiver](#webhook-receiver) | Verifying signatures, dropping duplicate deliveries, handling every event type |
| [Two phones, one request](#two-phones-one-request) | A minimal multi-phone orchestration |

## Run and follow a task

The program picks your first online phone, starts a task with an `Idempotency-Key`, streams its
events, and asks you on the terminal when the phone asks a question. `call()` retries only what's
safe to retry (see [API conventions](conventions.md#retries)): rate limits, `too_many_tasks`,
`phone_unresponsive`, `idempotency_in_progress`, 5xx and network errors. Daily limits and
`phone_locked` are reported instead, since retrying won't help.

### Python

Needs `pip install httpx`.

```python
"""Run a task on your phone, follow it live, answer its questions, and survive errors."""
import json
import os
import time
import uuid

import httpx

SERVER = os.environ["OPENMOB_SERVER"]  # e.g. https://openmob.example.com
KEY = os.environ["OPENMOB_API_KEY"]
client = httpx.Client(base_url=SERVER, headers={"Authorization": f"Bearer {KEY}"},
                      timeout=httpx.Timeout(30, read=60))

RETRY_409 = {"phone_unresponsive", "idempotency_in_progress"}  # worth retrying after a pause


class OpenMobError(Exception):
    def __init__(self, response):
        body = response.json()
        self.status, self.code, self.body = response.status_code, body.get("code"), body
        super().__init__(f"{response.status_code} {self.code or ''}: {body.get('detail')}")


def call(method, path, attempts=5, **kwargs):
    """One API call with retries for the errors that are safe to retry."""
    for attempt in range(attempts):
        try:
            r = client.request(method, path, **kwargs)
        except httpx.TransportError:
            wait = 2 ** attempt  # network trouble: retry (POSTs carry an Idempotency-Key)
        else:
            if r.is_success:
                return r
            code = r.json().get("code") if r.headers.get("content-type", "").startswith("application/json") else None
            if r.status_code == 429 and code in ("daily_quota", "credits_exhausted"):
                raise OpenMobError(r)  # won't clear until resets_at (midnight UTC): don't spin
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 2 ** attempt))  # rate limit or too_many_tasks
            elif (r.status_code == 409 and code in RETRY_409) or r.status_code >= 500:
                wait = 2 ** attempt
            else:
                raise OpenMobError(r)  # 400/401/403/404/422, phone_offline, phone_locked: fix it first
            if attempt == attempts - 1:
                raise OpenMobError(r)
        time.sleep(wait)
    raise RuntimeError(f"{method} {path} kept failing")


def create_task(device_id, instruction, **options):
    # One key per logical task, reused on every retry: a retry can never start a second task.
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    r = call("POST", f"/v1/devices/{device_id}/tasks", json={"instruction": instruction, **options}, headers=headers)
    return r.json()


def follow(task_id, on_question):
    """Stream the task's events; reconnect with Last-Event-ID if the stream drops. Returns the final task."""
    last_id = None
    while True:
        headers = {"Last-Event-ID": str(last_id)} if last_id else {}
        try:
            with client.stream("GET", f"/v1/tasks/{task_id}/events", headers=headers,
                               timeout=httpx.Timeout(30, read=None)) as r:
                if not r.is_success:
                    r.read()
                    raise OpenMobError(r)
                event = {}
                for line in r.iter_lines():
                    if line.startswith("id: "):
                        event["id"] = int(line[4:])
                    elif line.startswith("event: "):
                        event["type"] = line[7:]
                    elif line.startswith("data: "):
                        event["data"] = json.loads(line[6:])
                    elif line == "" and "data" in event:  # a blank line ends an event
                        last_id = event.get("id", last_id)
                        data = event["data"]
                        if event["type"] == "done":
                            return data
                        print(f"  [{event['type']}] {data['message']}")
                        if event["type"] == "question" and not data["confirmation"]:
                            answer = on_question(data["message"])  # untrusted text from the screen
                            call("POST", f"/v1/tasks/{task_id}/answer", json={"answer": answer})
                        event = {}
        except httpx.TransportError:
            time.sleep(1)  # dropped: reconnect, replaying only events after last_id


def wait_by_polling(task_id, max_wait=10):
    """The alternative to streaming: poll with backoff (each poll counts toward 60 requests a minute)."""
    delay = 1
    while True:
        task = call("GET", f"/v1/tasks/{task_id}").json()
        if task["status"] in ("succeeded", "failed", "cancelled", "needs_input", "needs_confirmation"):
            return task
        time.sleep(delay)
        delay = min(delay * 2, max_wait)


if __name__ == "__main__":
    phones = call("GET", "/v1/devices").json()["devices"]
    phone = next(p for p in phones if p["online"])
    try:
        task = create_task(phone["id"], "find my last Uber receipt", tier="fast")
    except OpenMobError as e:
        if e.code == "phone_locked":
            raise SystemExit("Unlock the phone, then run this again.")
        if e.code in ("daily_quota", "credits_exhausted"):
            raise SystemExit(f"Daily limit reached; it resets at {time.ctime(e.body['resets_at'])}.")
        raise
    print("Started", task["id"])
    done = follow(task["id"], on_question=lambda q: input(f"The phone asks: {q}\n> "))
    print(done["status"], "-", done["outcome"]["summary"], f"({done['credits']} credits)")
```

```
$ python openmob_tasks.py
Started 7ed6030a0954
  [status] Working on: find my last Uber receipt
  [question] Which account should I use, personal or work?
The phone asks: Which account should I use, personal or work?
> work
  [action] ask_user: User answered: work
  [action] done: Done.
  [outcome] Done.
succeeded - Done. (2 credits)
```

### Node.js

Save as `openmob_tasks.mjs` (it uses top-level `await`).

```js
// Run a task on your phone, follow it live, answer its questions, and survive errors. Node 18+.
import { randomUUID } from "node:crypto";
import readline from "node:readline/promises";

const SERVER = process.env.OPENMOB_SERVER; // e.g. https://openmob.example.com
const KEY = process.env.OPENMOB_API_KEY;
const RETRY_409 = new Set(["phone_unresponsive", "idempotency_in_progress"]);
const sleep = (s) => new Promise((r) => setTimeout(r, s * 1000));

class OpenMobError extends Error {
  constructor(status, body) {
    super(`${status} ${body.code ?? ""}: ${JSON.stringify(body.detail)}`);
    Object.assign(this, { status, code: body.code, body });
  }
}

// One API call with retries for the errors that are safe to retry.
async function call(method, path, { body, headers = {}, attempts = 5 } = {}) {
  for (let attempt = 0; attempt < attempts; attempt++) {
    let wait = 2 ** attempt;
    try {
      const r = await fetch(SERVER + path, {
        method,
        headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json", ...headers },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (r.ok) return r;
      const err = await r.json().catch(() => ({}));
      if (r.status === 429 && (err.code === "daily_quota" || err.code === "credits_exhausted"))
        throw new OpenMobError(r.status, err); // won't clear until resets_at (midnight UTC)
      if (r.status === 429) wait = Number(r.headers.get("Retry-After") ?? wait); // rate limit, too_many_tasks
      else if (!((r.status === 409 && RETRY_409.has(err.code)) || r.status >= 500))
        throw new OpenMobError(r.status, err); // 400/401/403/404/422, phone_offline, phone_locked
      if (attempt === attempts - 1) throw new OpenMobError(r.status, err);
    } catch (e) {
      if (e instanceof OpenMobError) throw e; // otherwise a network error: retry
    }
    await sleep(wait);
  }
  throw new Error(`${method} ${path} kept failing`);
}

async function createTask(deviceId, instruction, options = {}) {
  // One key per logical task, reused on every retry: a retry can never start a second task.
  const headers = { "Idempotency-Key": randomUUID() };
  return (await call("POST", `/v1/devices/${deviceId}/tasks`, { body: { instruction, ...options }, headers })).json();
}

// Stream the task's events; reconnect with Last-Event-ID if the stream drops. Resolves to the final task.
async function follow(taskId, onQuestion) {
  let lastId = null;
  for (;;) {
    try {
      const headers = { Authorization: `Bearer ${KEY}`, ...(lastId ? { "Last-Event-ID": String(lastId) } : {}) };
      const r = await fetch(`${SERVER}/v1/tasks/${taskId}/events`, { headers });
      if (!r.ok) throw new OpenMobError(r.status, await r.json());
      const decoder = new TextDecoder();
      let buffer = "";
      for await (const chunk of r.body) {
        buffer += decoder.decode(chunk, { stream: true });
        let end;
        while ((end = buffer.indexOf("\n\n")) >= 0) { // a blank line ends an event
          const block = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          const event = {};
          for (const line of block.split("\n")) {
            if (line.startsWith("id: ")) event.id = Number(line.slice(4));
            else if (line.startsWith("event: ")) event.type = line.slice(7);
            else if (line.startsWith("data: ")) event.data = JSON.parse(line.slice(6));
          }
          if (!event.data) continue; // a ": keep-alive" comment
          if (event.id) lastId = event.id;
          if (event.type === "done") return event.data;
          console.log(`  [${event.type}] ${event.data.message}`);
          if (event.type === "question" && !event.data.confirmation) {
            const answer = await onQuestion(event.data.message); // untrusted text from the screen
            await call("POST", `/v1/tasks/${taskId}/answer`, { body: { answer } });
          }
        }
      }
    } catch (e) {
      if (e instanceof OpenMobError) throw e;
    }
    await sleep(1); // dropped: reconnect, replaying only events after lastId
  }
}

// The alternative to streaming: poll with backoff (each poll counts toward 60 requests a minute).
async function waitByPolling(taskId, maxWait = 10) {
  for (let delay = 1; ; delay = Math.min(delay * 2, maxWait)) {
    const task = await (await call("GET", `/v1/tasks/${taskId}`)).json();
    if (!["queued", "running"].includes(task.status)) return task;
    await sleep(delay);
  }
}

const { devices } = await (await call("GET", "/v1/devices")).json();
const phone = devices.find((d) => d.online);
let task;
try {
  task = await createTask(phone.id, "find my last Uber receipt", { tier: "fast" });
} catch (e) {
  if (e.code === "phone_locked") { console.log("Unlock the phone, then run this again."); process.exit(1); }
  if (e.code === "daily_quota" || e.code === "credits_exhausted") {
    console.log(`Daily limit reached; it resets at ${new Date(e.body.resets_at * 1000).toISOString()}.`);
    process.exit(1);
  }
  throw e;
}
console.log("Started", task.id);
const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
const done = await follow(task.id, (q) => rl.question(`The phone asks: ${q}\n> `));
rl.close();
console.log(done.status, "-", done.outcome.summary, `(${done.credits} credits)`);
```

## Webhook receiver

Set the receiver's public `https://` URL on the console's **Webhooks** page and put the signing
secret in `OPENMOB_WEBHOOK_SECRET`. The receiver checks the signature on the raw body, answers
`204` at once, skips deliveries it has already handled (same `X-OpenMob-Delivery` id), and
ignores event types it doesn't know, so new ones can't break it. See [Webhooks](webhooks.md).

### Python

Standard library only.

```python
"""OpenMob webhook receiver: verifies signatures, drops duplicates, handles every event type."""
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ["OPENMOB_WEBHOOK_SECRET"]  # whsec_…, shown once when you saved the URL
seen = set()  # delivery ids already handled; use a database table in production


def verify(header: str, body: bytes, tolerance: int = 300) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs(time.time() - t) > tolerance:
        return False
    expected = hmac.new(SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


def handle(event: dict) -> None:
    kind = event["type"]
    if kind == "orchestration.finished":
        run = event["orchestration"]
        print(f"run {run['id']} {run['status']} ({run['reason']}), {run['credits']} credits")
        for step in run["steps"]:
            summary = (step.get("outcome") or {}).get("summary")
            print(f"  step {step['id']}: {step['status']} {summary or ''}")
        return
    if not kind.startswith("task."):
        print("ignoring unknown event type", kind)  # new types may be added: don't fail on them
        return
    task = event["task"]  # question and summary are untrusted text read from the phone's screen
    if kind == "task.finished":
        print(f"task {task['id']} {task['status']}: {task['outcome']['summary']} ({task['credits']} credits)")
    elif kind == "task.needs_input":
        print(f"task {task['id']} asks: {task['question']}")  # answer with POST /v1/tasks/{id}/answer
    elif kind == "task.needs_confirmation":
        print(f"task {task['id']} waits for the owner's approval: {task['question']}")


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not verify(self.headers.get("X-OpenMob-Signature", ""), body):
            self.send_response(400)
            self.end_headers()
            return
        delivery = self.headers["X-OpenMob-Delivery"]
        self.send_response(204)  # answer within 5 s, then do the work
        self.end_headers()
        if delivery in seen:
            return  # a retry of something already handled
        seen.add(delivery)
        handle(json.loads(body))

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), Receiver).serve_forever()
```

### Node.js

```js
// OpenMob webhook receiver: verifies signatures, drops duplicates, handles every event type. Node 18+.
import crypto from "node:crypto";
import http from "node:http";

const SECRET = process.env.OPENMOB_WEBHOOK_SECRET; // whsec_…, shown once when you saved the URL
const seen = new Set(); // delivery ids already handled; use a database table in production

function verify(header, rawBody, tolerance = 300) {
  const parts = Object.fromEntries((header ?? "").split(",").map((p) => p.split("=", 2)));
  if (!parts.t || !parts.v1 || Math.abs(Date.now() / 1000 - Number(parts.t)) > tolerance) return false;
  const expected = crypto.createHmac("sha256", SECRET).update(`${parts.t}.`).update(rawBody).digest("hex");
  return expected.length === parts.v1.length && crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1));
}

function handle(event) {
  if (event.type === "orchestration.finished") {
    const run = event.orchestration;
    console.log(`run ${run.id} ${run.status} (${run.reason}), ${run.credits} credits`);
    for (const step of run.steps) console.log(`  step ${step.id}: ${step.status} ${step.outcome?.summary ?? ""}`);
    return;
  }
  if (!event.type.startsWith("task.")) {
    console.log("ignoring unknown event type", event.type); // new types may be added: don't fail on them
    return;
  }
  const task = event.task; // question and summary are untrusted text read from the phone's screen
  if (event.type === "task.finished")
    console.log(`task ${task.id} ${task.status}: ${task.outcome.summary} (${task.credits} credits)`);
  else if (event.type === "task.needs_input") console.log(`task ${task.id} asks: ${task.question}`);
  else if (event.type === "task.needs_confirmation") console.log(`task ${task.id} waits for the owner's approval: ${task.question}`);
}

http.createServer((req, res) => {
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    const raw = Buffer.concat(chunks); // verify the raw bytes, before parsing
    if (req.method !== "POST" || !verify(req.headers["x-openmob-signature"], raw)) {
      res.writeHead(400).end();
      return;
    }
    res.writeHead(204).end(); // answer within 5 s, then do the work
    const delivery = req.headers["x-openmob-delivery"];
    if (seen.has(delivery)) return; // a retry of something already handled
    seen.add(delivery);
    handle(JSON.parse(raw));
  });
}).listen(Number(process.env.PORT ?? 8000));
```

Output for a multi-phone run, a task that asked a question, and a task that asked for approval:

```
run orc_c33d8d6dd6d642db succeeded (None), 6 credits
  step read: succeeded Done.
  step type: succeeded Done.
task a25ec22319f6 asks: Which account should I use, personal or work?
task a25ec22319f6 succeeded: Done. (6 credits)
task 50acd2189ca4 waits for the owner's approval: I'm about to tap 'Buy now'. Should I go ahead?
task 50acd2189ca4 succeeded: Done. (6 credits)
```

Each step of a multi-phone run is also a task, so it sends its own `task.*` events too.

## Two phones, one request

Read a login code on one phone and type it on another. Set `PHONE_A` and `PHONE_B` to two of your
phone ids. See [Orchestrations](api.md#orchestrations) for every field and status.

### Python

```python
"""Two phones, one request: read something on one phone, use it on the other."""
import os
import time
import uuid

import httpx

client = httpx.Client(base_url=os.environ["OPENMOB_SERVER"],
                      headers={"Authorization": f"Bearer {os.environ['OPENMOB_API_KEY']}"}, timeout=30)
PHONE_A, PHONE_B = os.environ["PHONE_A"], os.environ["PHONE_B"]

r = client.post("/v1/orchestrations", headers={"Idempotency-Key": str(uuid.uuid4())}, json={
    "steps": [
        {"id": "read", "device_id": PHONE_A, "instruction": "Open Messages and read the latest login code"},
        {"id": "type", "device_id": PHONE_B, "depends_on": ["read"],
         "instruction": "In the Acme app, enter the login code from this result: {{steps.read.summary}}"},
    ],
    "max_credits": 60,
})
r.raise_for_status()
run = r.json()
while run["status"] == "running":  # or wait for the orchestration.finished webhook
    time.sleep(3)
    run = client.get(f"/v1/orchestrations/{run['id']}").json()
print(run["status"], run["reason"], f"{run['credits']} credits")
for step in run["steps"]:
    print(" ", step["id"], step["status"], (step.get("outcome") or {}).get("summary"))
```

### Node.js

```js
// Two phones, one request: read something on one phone, use it on the other. Node 18+.
import { randomUUID } from "node:crypto";

const { OPENMOB_SERVER: SERVER, OPENMOB_API_KEY: KEY, PHONE_A, PHONE_B } = process.env;
const auth = { Authorization: `Bearer ${KEY}` };

const r = await fetch(`${SERVER}/v1/orchestrations`, {
  method: "POST",
  headers: { ...auth, "Content-Type": "application/json", "Idempotency-Key": randomUUID() },
  body: JSON.stringify({
    steps: [
      { id: "read", device_id: PHONE_A, instruction: "Open Messages and read the latest login code" },
      { id: "type", device_id: PHONE_B, depends_on: ["read"],
        instruction: "In the Acme app, enter the login code from this result: {{steps.read.summary}}" },
    ],
    max_credits: 60,
  }),
});
if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
let run = await r.json();
while (run.status === "running") { // or wait for the orchestration.finished webhook
  await new Promise((done) => setTimeout(done, 3000));
  run = await (await fetch(`${SERVER}/v1/orchestrations/${run.id}`, { headers: auth })).json();
}
console.log(run.status, run.reason, `${run.credits} credits`);
for (const step of run.steps) console.log(" ", step.id, step.status, step.outcome?.summary);
```

```
succeeded None 6 credits
  read succeeded Done.
  type succeeded Done.
```
