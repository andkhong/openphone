"""The webhook outbox: deliveries are stored first, retried with backoff, carry a stable delivery id,
and survive a restart."""

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from test_remote import wait_for

from openmob.ledger import Ledger
from openmob.platform.service import sign_webhook
from openmob.platform.webhooks import WebhookOutbox

SECRET = "whsec_test"


class Store:
    def __init__(self, url):
        self.url = url

    def webhook(self, account_id):
        return (self.url, SECRET) if self.url else None


@pytest.fixture
def receiver():
    """A local endpoint that fails the first `fail` requests with 500, then answers 204."""
    state = {"fail": 0, "received": []}

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["received"].append((dict(self.headers), body))
            failing = len(state["received"]) <= state["fail"]
            self.send_response(500 if failing else 204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/hook"
    yield state
    server.shutdown()


def outbox(ledger, url, backoff=(0.05, 0.05, 0.05)):
    box = WebhookOutbox(ledger, Store(url), lambda u: True, sign_webhook, backoff=backoff)
    box.start()
    return box


def test_retries_until_delivered_with_one_delivery_id(tmp_path, receiver):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    receiver["fail"] = 2
    box = outbox(ledger, receiver["url"])
    body = json.dumps({"type": "task.finished", "task": {"id": "t1"}})
    delivery = box.enqueue("acct", "task.finished", "t1", body)
    assert wait_for(lambda: ledger.webhook(delivery)["status"] == "delivered")
    assert len(receiver["received"]) == 3 and ledger.webhook(delivery)["attempts"] == 3
    ids = {h["X-OpenMob-Delivery"] for h, _ in receiver["received"]}
    assert ids == {delivery} and delivery.startswith("dlv_")
    headers, sent = receiver["received"][-1]
    assert sent.decode() == body and headers["X-OpenMob-Event"] == "task.finished"
    t, v1 = dict(p.split("=", 1) for p in headers["X-OpenMob-Signature"].split(",")).values()
    assert hmac.compare_digest(v1, hmac.new(SECRET.encode(), f"{t}.".encode() + sent, hashlib.sha256).hexdigest())
    assert any(e["action"] == "webhook.delivered" for e in ledger.audit_log())
    box.stop()


def test_gives_up_after_the_last_retry(tmp_path, receiver):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    receiver["fail"] = 100
    box = outbox(ledger, receiver["url"], backoff=(0.05, 0.05))
    delivery = box.enqueue("acct", "task.finished", "t1", "{}")
    assert wait_for(lambda: ledger.webhook(delivery)["status"] == "failed")
    row = ledger.webhook(delivery)
    assert row["attempts"] == 3 and row["last_error"] == "HTTP 500"
    assert any(e["action"] == "webhook.failed" for e in ledger.audit_log())
    box.stop()


def test_pending_deliveries_survive_a_restart(tmp_path, receiver):
    path = tmp_path / "ledger.sqlite"
    before = Ledger(path)
    before.add_webhook("dlv_waiting", "acct", "task.finished", "t1", "{}")
    before.add_webhook("dlv_midsend", "acct", "task.needs_input", "t2", "{}")
    before.claim_webhooks()  # both were being sent when the server stopped
    after = Ledger(path)  # the restarted server
    box = outbox(after, receiver["url"])
    assert wait_for(lambda: all(after.webhook(d)["status"] == "delivered" for d in ("dlv_waiting", "dlv_midsend")))
    assert {h["X-OpenMob-Delivery"] for h, _ in receiver["received"]} == {"dlv_waiting", "dlv_midsend"}
    box.stop()


def test_removed_webhook_drops_pending_deliveries(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    box = outbox(ledger, None)
    delivery = box.enqueue("acct", "task.finished", "t1", "{}")
    assert wait_for(lambda: ledger.webhook(delivery)["status"] == "dropped")
    box.stop()
