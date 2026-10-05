"""Durable webhook delivery: every task event is written to the ledger's outbox first, and a worker
sends it, retrying with backoff for about an hour. Pending deliveries survive a restart.

Each delivery carries `X-OpenMob-Delivery: <id>` (the same id on every retry, so receivers can drop
duplicates), `X-OpenMob-Event` and `X-OpenMob-Signature` (signed at send time; see sign_webhook).
"""

from __future__ import annotations

import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

TIMEOUT = 5.0
# Waits before attempts 2, 3, …: about 63 minutes in all, then the delivery is given up.
BACKOFF = (10, 30, 60, 120, 300, 600, 900, 900, 900)
IDLE_CHECK = 30.0  # the worker also wakes up on every new delivery


class WebhookOutbox:
    def __init__(self, ledger, store, url_ok, sign, backoff=BACKOFF):
        self.ledger = ledger
        self.store = store
        self.url_ok = url_ok  # (url) -> bool: SSRF check, repeated at send time (DNS can change)
        self.sign = sign  # (secret, body, timestamp) -> signature header
        self.backoff = tuple(backoff)
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="openmob-webhook")
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.ledger.requeue_interrupted_webhooks()
        self._thread = threading.Thread(target=self._run, name="openmob-webhooks", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()

    def enqueue(self, account_id: str, event: str, task_id: str | None, body: str) -> str:
        delivery_id = "dlv_" + secrets.token_hex(12)
        self.ledger.add_webhook(delivery_id, account_id, event, task_id, body)
        self._wake.set()
        return delivery_id

    def _run(self) -> None:
        while not self._stopped.is_set():
            for row in self.ledger.claim_webhooks():
                self._pool.submit(self._attempt, row)
            due = self.ledger.next_webhook_due()
            wait = IDLE_CHECK if due is None else min(IDLE_CHECK, max(0.05, due - time.time()))
            self._wake.wait(wait)
            self._wake.clear()

    def _attempt(self, row: dict) -> None:
        try:
            status, error = self._send(row)
        except Exception as exc:  # never lose a delivery to a bug in one attempt
            status, error = "retry", type(exc).__name__
        attempt = row["attempts"] + 1  # this one
        if status == "retry" and attempt <= len(self.backoff):
            self.ledger.webhook_attempted(row["id"], "pending", error, time.time() + self.backoff[attempt - 1])
            self._wake.set()
            return
        final = "failed" if status == "retry" else status
        self.ledger.webhook_attempted(row["id"], final, error)
        if final != "dropped":
            self.ledger.audit("webhook." + final, row["account_id"], task_id=row["task_id"], outcome=row["event"])

    def _send(self, row: dict) -> tuple[str, str | None]:
        """("delivered" | "blocked" | "dropped" | "retry", error)."""
        hook = self.store.webhook(row["account_id"])
        if not hook:
            return "dropped", "the owner removed the webhook"
        url, secret = hook
        if not self.url_ok(url):
            return "blocked", "address not allowed"
        body = row["body"].encode()
        try:
            r = httpx.post(url, content=body, timeout=TIMEOUT, follow_redirects=False, headers={
                "Content-Type": "application/json", "User-Agent": "OpenMob-Webhooks/1",
                "X-OpenMob-Event": row["event"], "X-OpenMob-Delivery": row["id"],
                "X-OpenMob-Signature": self.sign(secret, body, int(time.time())),
            })
        except httpx.HTTPError as exc:
            return "retry", type(exc).__name__
        if 200 <= r.status_code < 300:
            return "delivered", None
        return "retry", f"HTTP {r.status_code}"
