"""Local page server for the Jev image benchmark (emulator reaches it at 10.0.2.2:<port>).

Serves bench/jev_images/pages/ and two endpoints for the canvas game page:
  GET  /scene   -> the scene the controller wants drawn (JSON)
  POST /report  <- the page's report of what it drew (target boxes as fractions of the canvas)
Never bind port 8765 (the OpenMob server's).
"""

from __future__ import annotations

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGES = Path(__file__).parent / "pages"


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.scene: dict = {"id": 0, "scene": "blank"}
        self.report: dict = {}
        self.reported = threading.Condition(self.lock)


STATE = State()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PAGES), **kwargs)

    def log_message(self, *args):  # quiet
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/scene"):
            with STATE.lock:
                body = json.dumps(STATE.scene).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):
        if self.path.startswith("/report"):
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
            with STATE.reported:
                STATE.report = data
                STATE.reported.notify_all()
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


def set_scene(scene: dict) -> None:
    with STATE.lock:
        STATE.scene = scene


def wait_report(scene_id: int, timeout: float = 15.0) -> dict:
    with STATE.reported:
        ok = STATE.reported.wait_for(lambda: STATE.report.get("id") == scene_id, timeout=timeout)
        if not ok:
            raise TimeoutError(f"no report for scene {scene_id}")
        return dict(STATE.report)


def start(port: int) -> ThreadingHTTPServer:
    assert port != 8765, "port 8765 belongs to the OpenMob server"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    import sys, time
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    start(port)
    print(f"serving {PAGES} on 127.0.0.1:{port}")
    while True:
        time.sleep(3600)
