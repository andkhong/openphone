"""Ask Haiku 5.5 (vision) the same questions, with OpenMob's production settings (effort low, adaptive thinking).

Image size follows the loop: 768 px long side on a labelled screen (>= 8 labelled elements), else 1280.
Modes (results/haiku_<mode>.jsonl):
  combined  every image, one call answering every job as JSON (targets as pixel coordinates)
  iso       latency: the 30 iso images, J1 alone and one target alone, 2 repeats
  textb     pattern B's 'what' step: text only (today's screen description), name the element to tap, 30 images
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import time
from pathlib import Path

import anthropic

import common
import keys
from run_jev import done_keys, iso_items

MODEL = "claude-haiku-5-5"


def size_for(images: Path, item: dict) -> int:
    return 768 if common.labeled_count(images, item) >= 8 else 1280


def prompt(item: dict, prep: common.Prepared, jobs: set[str]) -> str:
    lines = [f"This is a phone screenshot, {prep.width}x{prep.height} px. Foreground app: {item.get('package') or 'unknown'}.",
             "Answer with one JSON object only, no other text. Keys:"]
    if "state" in jobs:
        lines.append('"state": one of ' + json.dumps(list(common.STATES)) + ". " +
                     " ".join(f"{k}: {v}" for k, v in common.STATES.items()))
    if "loading" in jobs:
        lines.append('"loading": true/false. ' + common.LOADING_Q.replace("`screen`", "the screen"))
    if "sensitive" in jobs:
        lines.append('"sensitive": true/false. ' + common.SENSITIVE_Q.replace("`screen`", "the screen"))
    if "dialog" in jobs:
        lines.append('"dialog": one of ["system", "app_drawn", "none"]. ' + " ".join(f"{k}: {v}" for k, v in common.DIALOGS.items()))
    if "landing" in jobs and item.get("landing"):
        lines.append(f'"landing": true/false. Does the screen show search results for exactly the query "{item["landing"]["query"]}" '
                     "(not a different query, no results, or only suggestions)?")
    tg = [t for t in item["targets"] + item["absent"] if t["key"] in jobs or "targets" in jobs]
    if tg:
        lines.append('"targets": an object with one entry per key below: {"present": true/false, "x": int, "y": int}, the centre '
                     "of that element in image pixels (x from the left, y from the top); present false and x, y null when it isn't on screen.")
        for t in tg:
            lines.append(f'  "{t["key"]}": {t["desc"]}')
    return "\n".join(lines)


def parse(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


class Haiku:
    def __init__(self):
        import os
        ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        self.client = anthropic.Anthropic(max_retries=2, timeout=60.0,
                                          default_headers={"anthropic-workspace-id": ws} if ws else None)
        self.count = 0

    def ask(self, content: list) -> dict:
        self.count += 1
        started = time.perf_counter()
        try:
            r = self.client.messages.create(model=MODEL, max_tokens=8192, output_config={"effort": "low"},
                                            messages=[{"role": "user", "content": content}])
        except anthropic.APIStatusError as exc:
            return {"status": exc.status_code, "error": str(exc)[:200], "latency_ms": (time.perf_counter() - started) * 1000}
        except anthropic.APIConnectionError as exc:
            return {"status": "error", "error": type(exc).__name__, "latency_ms": (time.perf_counter() - started) * 1000}
        ms = (time.perf_counter() - started) * 1000
        text = "".join(b.text for b in r.content if b.type == "text")
        u = r.usage
        return {"status": 200, "latency_ms": ms, "text": text, "answers": parse(text), "stop": r.stop_reason,
                "usage": {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens}}


def image_content(prep: common.Prepared, text: str) -> list:
    return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(prep.jpeg).decode()}},
            {"type": "text", "text": text}]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True)
    ap.add_argument("--mode", required=True, choices=["combined", "iso", "textb"])
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    keys.load()
    images = Path(args.images)
    items = common.load_labels()
    if args.limit:
        items = items[: args.limit]
    common.RESULTS.mkdir(exist_ok=True)
    out = common.RESULTS / f"haiku_{args.mode}.jsonl"
    done = done_keys(out)
    h = Haiku()
    f = out.open("a")

    def write(key, item, res, extra):
        f.write(json.dumps({"key": list(key), "id": item["id"], **extra, **res}) + "\n")
        f.flush()
        print(f"{key} {res.get('status')} {round(res['latency_ms'])} ms", flush=True)

    if args.mode == "combined":
        for item in items:
            key = (item["id"],)
            if key in done:
                continue
            size = size_for(images, item)
            prep = common.prepare(images / item["file"], size, min_side=1)
            jobs = {"state", "loading", "sensitive", "dialog", "landing", "targets"}
            res = h.ask(image_content(prep, prompt(item, prep, jobs)))
            write(key, item, res, {"size": size, "w": prep.width, "h": prep.height, "scale": prep.scale})
    elif args.mode == "iso":
        for rep in range(2):
            for item in iso_items(common.load_labels()):
                size = size_for(images, item)
                prep = common.prepare(images / item["file"], size, min_side=1)
                for kind in ("state", "locate"):
                    key = (item["id"], kind, rep)
                    if key in done:
                        continue
                    jobs = {"state"} if kind == "state" else {item["targets"][0]["key"]}
                    res = h.ask(image_content(prep, prompt(item, prep, jobs)))
                    write(key, item, res, {"size": size, "kind": kind, "rep": rep, "w": prep.width, "h": prep.height,
                                           "scale": prep.scale})
    else:
        for item in iso_items(common.load_labels()):
            key = (item["id"],)
            if key in done:
                continue
            desc, _, _, _ = common.screen_text(images, item)
            t = item["targets"][0]
            text = (f"A phone agent's user asked: 'tap {t['desc']}'. The screen is described as JSON (no image): {json.dumps(desc)}.\n"
                    "Reply with JSON only: {\"action\": \"tap\" or \"other\", \"target\": a short description of what to tap, "
                    "precise enough for a visual locator to find it}.")
            res = h.ask([{"type": "text", "text": text}])
            write(key, item, res, {})
    f.close()
    print(f"calls: {h.count}")


if __name__ == "__main__":
    main()
