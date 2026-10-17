"""Ask Jev about every labelled screenshot (HTTP API, multipart image upload as docs.typesafe.ai/images.md).

Modes (each writes results/jev_<mode>.jsonl; reruns skip finished rows):
  combined  jev-preview (1.14), the image at 512/768/1024 px, all jobs' questions in one request (+ Box per target)
  text      jev-latest (1.13, production today), today's text description, the same non-Box questions
  latest    jev-latest (1.13), the image at 768 px, the non-Box questions (does 1.13 read images as well?)
  iso       latency only: 30 images, one J1 question alone and one Box question alone, each size, 3 repeats
"""

from __future__ import annotations

import argparse
import json
import random
import time
import uuid
from pathlib import Path

import httpx

import common
import keys

API = "https://api.typesafe.ai"
SIZES = (512, 768, 1024)


def questions(item: dict, with_box: bool) -> dict:
    q = {
        "state": {"type": "choice", "instructions": common.STATE_Q, "criteria": common.STATES},
        "loading": {"type": "noul", "instructions": common.LOADING_Q},
        "sensitive": {"type": "noul", "instructions": common.SENSITIVE_Q},
        "dialog": {"type": "choice", "instructions": common.DIALOG_Q, "criteria": common.DIALOGS},
    }
    if item.get("landing"):
        q["landing"] = {"type": "noul", "instructions": common.landing_instructions(item["landing"]["query"])}
    if with_box:
        for t in item["targets"] + item["absent"]:
            q[f"box_{t['key']}"] = {"type": "box", "instructions": common.box_q(t["desc"])}
            q[f"vis_{t['key']}"] = {"type": "noul", "instructions": common.visible_q(t["desc"])}
    return q


class Jev:
    def __init__(self):
        import os
        self.http = httpx.Client(base_url=API, timeout=30.0,
                                 headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}"})
        self.count = 0

    def ask(self, model: str, state, qs: dict, image: bytes | None = None) -> dict:
        body = {"model": model, "state": state, "questions": qs}
        headers = {}
        files = None
        if image is not None:
            ref = state["screen"]
            body["assets"] = {ref: {"type": "image", "part": "screen-image"}}
            headers["TypeSafe-Danger-Zone-Images"] = "true"
        for attempt in range(3):
            self.count += 1
            started = time.perf_counter()
            try:
                if image is not None:
                    files = {"request": (None, json.dumps(body), "application/json"),
                             "screen-image": ("screen.jpg", image, "image/jpeg")}
                    r = self.http.post("/v1/systemone", files=files, headers=headers)
                else:
                    r = self.http.post("/v1/systemone", json=body)
            except httpx.HTTPError as exc:
                ms = (time.perf_counter() - started) * 1000
                if attempt == 2:
                    return {"status": "error", "error": type(exc).__name__, "latency_ms": ms}
                continue
            ms = (time.perf_counter() - started) * 1000
            if r.status_code in (429, 529) and attempt < 2:
                time.sleep(1.0 * 2 ** attempt)
                continue
            if r.status_code != 200:
                return {"status": r.status_code, "error": r.text[:300], "latency_ms": ms}
            data = r.json()
            return {"status": 200, "latency_ms": ms, "answers": data.get("answers"), "usage": data.get("usage"),
                    "model": data.get("model"), "attempts": attempt + 1}
        return {"status": "error", "error": "retries", "latency_ms": None}


def done_keys(path: Path) -> set:
    if not path.exists():
        return set()
    return {tuple(json.loads(line)["key"]) for line in path.read_text().splitlines() if line.strip()}


def iso_items(items: list[dict]) -> list[dict]:
    """30 images for the latency runs: canvas and labelled, each with a target."""
    rnd = random.Random(7)
    with_t = [i for i in items if i["targets"]]
    canvas = [i for i in with_t if i["category"] == "canvas"]
    other = [i for i in with_t if i["category"] != "canvas"]
    return rnd.sample(canvas, 15) + rnd.sample(other, 15)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True)
    ap.add_argument("--mode", required=True, choices=["combined", "text", "latest", "iso"])
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    keys.load()
    images = Path(args.images)
    items = common.load_labels()
    if args.limit:
        items = items[: args.limit]
    common.RESULTS.mkdir(exist_ok=True)
    out = common.RESULTS / f"jev_{args.mode}.jsonl"
    done = done_keys(out)
    jev = Jev()
    f = out.open("a")

    def write(key, item, res, extra=None):
        row = {"key": list(key), "id": item["id"], **(extra or {}), **res}
        f.write(json.dumps(row) + "\n")
        f.flush()
        print(f"{key} {res.get('status')} {res.get('latency_ms') and round(res['latency_ms'])} ms", flush=True)

    if args.mode in ("combined", "latest"):
        sizes = SIZES if args.mode == "combined" else (768,)
        model = "jev-preview" if args.mode == "combined" else "jev-latest"
        # Interleave sizes per image so network drift hits every size alike.
        for item in items:
            for size in sizes:
                key = (item["id"], size)
                if key in done:
                    continue
                prep = common.prepare(images / item["file"], size)
                ref = f"img-{uuid.uuid4()}"
                state = {"foreground_app": item.get("package") or "unknown", "screen": ref}
                res = jev.ask(model, state, questions(item, with_box=args.mode == "combined"), prep.jpeg)
                write(key, item, res, {"size": size, "w": prep.width, "h": prep.height, "scale": prep.scale,
                                        "off_x": prep.off_x, "off_y": prep.off_y, "bytes": len(prep.jpeg)})
    elif args.mode == "text":
        for item in items:
            key = (item["id"], 0)
            if key in done:
                continue
            desc, _, used_ocr, _ = common.screen_text(images, item)
            res = jev.ask("jev-latest", {"screen": desc}, questions(item, with_box=False))
            write(key, item, res, {"used_ocr": used_ocr})
    else:
        for rep in range(3):
            for item in iso_items(common.load_labels()):
                for size in SIZES:
                    for kind in ("state", "box"):
                        key = (item["id"], size, kind, rep)
                        if key in done:
                            continue
                        prep = common.prepare(images / item["file"], size)
                        ref = f"img-{uuid.uuid4()}"
                        state = {"foreground_app": item.get("package") or "unknown", "screen": ref}
                        t = item["targets"][0]
                        qs = ({"state": {"type": "choice", "instructions": common.STATE_Q, "criteria": common.STATES}} if kind == "state"
                              else {f"box_{t['key']}": {"type": "box", "instructions": common.box_q(t["desc"])}})
                        res = jev.ask("jev-preview", state, qs, prep.jpeg)
                        write(key, item, res, {"size": size, "kind": kind, "rep": rep, "w": prep.width, "h": prep.height,
                                                "scale": prep.scale, "off_x": prep.off_x, "off_y": prep.off_y})
    f.close()
    print(f"requests sent: {jev.count}")


if __name__ == "__main__":
    main()
