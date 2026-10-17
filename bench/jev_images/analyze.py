"""Score every method on every job and print Markdown tables (measured numbers only).

  python analyze.py --images <scratch images dir>  > results/summary.md
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

import common
from openmob.agent.waiting import loading_element
from openmob.phone.adb import UIElement

JEV_PRICE = 0.042 / 1e6  # $ per input token (docs.typesafe.ai/models, 2026-10-17); output free
HAIKU_IN, HAIKU_OUT = 0.10 / 1e6, 0.50 / 1e6  # $ per token, Haiku 5.5 (<=100K prompt)
NOUL_T = 0.5


def rows(name: str) -> list[dict]:
    p = common.RESULTS / name
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def pct(values, q):
    v = sorted(x for x in values if x is not None)
    if not v:
        return float("nan")
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def prf(pairs):
    """pairs of (truth bool, predicted bool) -> accuracy, precision, recall, n."""
    tp = sum(1 for t, p in pairs if t and p)
    fp = sum(1 for t, p in pairs if not t and p)
    fn = sum(1 for t, p in pairs if t and not p)
    acc = sum(1 for t, p in pairs if t == p) / len(pairs) if pairs else float("nan")
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    return acc, prec, rec, len(pairs)


def f(x, d=1):
    return "n/a" if x != x else f"{100 * x:.{d}f}%"


def norm(s: str) -> str:
    return s.replace("’", "'").strip().lower()


def inside(x, y, box, margin=0):
    l, t, r, b = box
    return l - margin <= x <= r + margin and t - margin <= y <= b + margin


class Methods:
    """Per-image answers of each method, normalised: {method: {id: {...}}}."""

    def __init__(self, images: Path, items: list[dict]):
        self.images = images
        self.items = items
        self.by_id = {i["id"]: i for i in items}
        self.ans: dict[str, dict] = defaultdict(dict)
        self.lat: dict[str, list] = defaultdict(list)
        self.tok: dict[str, list] = defaultdict(list)
        self.fail: dict[str, int] = defaultdict(int)
        self.cost: dict[str, float] = defaultdict(float)
        self.requests: dict[str, int] = defaultdict(int)
        for r in rows("jev_combined.jsonl"):
            self._jev(f"jev_img{r['size']}", r)
        for r in rows("jev_latest.jsonl"):
            self._jev("jev113_img768", r)
        for r in rows("jev_text.jsonl"):
            self._jev("jev_text", r)
        for r in rows("haiku_combined.jsonl"):
            m = "haiku"
            self.requests[m] += 1
            if r["status"] != 200 or not r.get("answers"):
                self.fail[m] += 1
                continue
            self.lat[m].append(r["latency_ms"])
            u = r["usage"]
            self.tok[m].append((u["input_tokens"], u["output_tokens"]))
            self.cost[m] += u["input_tokens"] * HAIKU_IN + u["output_tokens"] * HAIKU_OUT
            a = r["answers"]
            out = {"state": a.get("state"), "loading": bool(a.get("loading")), "sensitive": bool(a.get("sensitive")),
                   "dialog": a.get("dialog"), "landing": a.get("landing"), "targets": {}}
            for key, t in (a.get("targets") or {}).items():
                if isinstance(t, dict):
                    present = bool(t.get("present"))
                    xy = None
                    if present and t.get("x") is not None and t.get("y") is not None:
                        xy = (t["x"] / r["scale"], t["y"] / r["scale"])
                    out["targets"][key] = {"present": present, "xy": xy}
            self.ans[m][r["id"]] = out
        self._baselines()

    def _jev(self, m: str, r: dict) -> None:
        self.requests[m] += 1
        if r["status"] != 200 or not r.get("answers"):
            self.fail[m] += 1
            return
        self.lat[m].append(r["latency_ms"])
        it = r["usage"]["input_tokens"]
        self.tok[m].append((it, r["usage"].get("output_tokens", 0)))
        self.cost[m] += it * JEV_PRICE
        a = r["answers"]
        out = {"state": a["state"]["choice"], "state_conf": a["state"].get("confidence"),
               "loading_p": a["loading"]["noul"], "loading": a["loading"]["noul"] >= NOUL_T,
               "sensitive_p": a["sensitive"]["noul"], "sensitive": a["sensitive"]["noul"] >= NOUL_T,
               "dialog": a["dialog"]["choice"], "landing": (a["landing"]["noul"] >= NOUL_T) if "landing" in a else None,
               "targets": {}}
        item = self.by_id[r["id"]]
        for t in item["targets"] + item["absent"]:
            bx = a.get(f"box_{t['key']}")
            if not bx:
                continue
            b = bx["box"]
            cx = (b["left"] + b["right"]) / 2 * r["w"]
            cy = (b["top"] + b["bottom"]) / 2 * r["h"]
            dx, dy = (cx - r["off_x"]) / r["scale"], (cy - r["off_y"]) / r["scale"]
            out["targets"][t["key"]] = {"xy": (dx, dy), "conf": bx.get("confidence", 0.0),
                                        "vis": a.get(f"vis_{t['key']}", {}).get("noul")}
        self.ans[m][r["id"]] = out

    def _baselines(self) -> None:
        """Today's non-model signals: element bounds for J2, the loading heuristic for J3."""
        for item in self.items:
            desc, els, used_ocr, ocr_els = common.screen_text(self.images, item)
            all_els = list(els) + [UIElement(1000 + i, txt, "", "", "OCRText", True, None, b) for i, (txt, b) in enumerate(ocr_els)]
            out = {"loading": loading_element(all_els) is not None, "used_ocr": used_ocr, "targets": {}}
            for t in item["targets"] + item["absent"]:
                d = t["desc"]
                word = norm(d[d.index("'") + 1:d.rindex("'")] if d.count("'") >= 2 else
                            d.replace("the ", "").split(" (")[0].replace(" app icon", "").replace(" button", "").replace(" box", ""))
                hit = None
                # accessibility elements only (OCR words aren't element bounds): exact label first, clickable first
                cands = []
                for el in els:
                    lab = norm(el.text or el.desc or el.hint or "")
                    if not lab:
                        continue
                    if lab == word:
                        cands.append((0, not el.clickable, el))
                    elif len(word) > 3 and word in lab and len(lab) < 2 * len(word) + 5:
                        cands.append((1, not el.clickable, el))
                    elif word == "search" and el.editable:
                        cands.append((1, False, el))
                if cands:
                    hit = sorted(cands, key=lambda c: (c[0], c[1]))[0][2]
                out["targets"][t["key"]] = {"present": hit is not None,
                                            "xy": ((hit.bounds[0] + hit.bounds[2]) / 2, (hit.bounds[1] + hit.bounds[3]) / 2) if hit else None}
            self.ans["baseline"][item["id"]] = out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True)
    args = ap.parse_args()
    items = common.load_labels()
    M = Methods(Path(args.images), items)
    P = print
    jev_img = [m for m in ("jev_img512", "jev_img768", "jev_img1024") if M.ans[m]]
    canvas = {i["id"] for i in items if i["category"] == "canvas"}
    P(f"# Jev image benchmark: measured results\n\n{len(items)} labelled screenshots "
      f"({len(canvas)} canvas, {len(items) - len(canvas)} labelled/app). Noul threshold {NOUL_T}.\n")

    # --- J1 -------------------------------------------------------------------------------
    P("## J1: screen state (7-way choice)\n")
    P("| method | all | canvas | labelled | n |\n|---|---|---|---|---|")
    for m in ["jev_text", "jev113_img768"] + jev_img + ["haiku"]:
        a = M.ans[m]
        ok = [(i["id"] in canvas, a[i["id"]]["state"] == i["state"]) for i in items if i["id"] in a]
        allv = sum(o for _, o in ok) / len(ok) if ok else float("nan")
        cv = [o for c, o in ok if c]
        lv = [o for c, o in ok if not c]
        P(f"| {m} | {f(allv)} | {f(sum(cv) / len(cv) if cv else float('nan'))} | {f(sum(lv) / len(lv) if lv else float('nan'))} | {len(ok)} |")
    P("\nConfusions (truth -> predicted, count >= 2):\n")
    for m in ["jev_text", "jev_img768", "haiku"]:
        conf = defaultdict(int)
        for i in items:
            a = M.ans[m].get(i["id"])
            if a and a["state"] != i["state"]:
                conf[(i["state"], a["state"])] += 1
        P(f"- {m}: " + ", ".join(f"{t}->{p} {n}" for (t, p), n in sorted(conf.items(), key=lambda x: -x[1]) if n >= 2))
    P()

    # --- J2 -------------------------------------------------------------------------------
    P("## J2: locate a named target\n")
    P("Present targets: hit = the returned point (box centre) inside the true target box. "
      "Absent targets: correct = the method says not there (Haiku: present=false; elements: no match; Jev: gated by a threshold).\n")
    P("| method | present hit (all) | canvas | labelled | labelled excl. search box | n present |\n|---|---|---|---|---|---|")
    present = [(i, t) for i in items for t in i["targets"]]
    absent = [(i, t) for i in items for t in i["absent"]]
    for m in jev_img + ["haiku", "baseline"]:
        a = M.ans[m]
        res = []
        for i, t in present:
            if i["id"] not in a:
                continue
            ans = a.get(i["id"], {}).get("targets", {}).get(t["key"])
            hit = bool(ans and ans.get("xy") and (ans.get("present", True)) and inside(*ans["xy"], t["box"]))
            res.append((i["id"] in canvas, hit, i["id"].startswith("search_")))
        c = [h for cv, h, _ in res if cv]
        lab = [h for cv, h, _ in res if not cv]
        labx = [h for cv, h, sb in res if not cv and not sb]
        name = "elements (today)" if m == "baseline" else m
        P(f"| {name} | {f(sum(h for _, h, _ in res) / len(res))} | {f(sum(c) / len(c))} | {f(sum(lab) / len(lab))} | {f(sum(labx) / len(labx))} | {len(res)} |")
    avail = [t["src"] == "elements" for _, t in present]
    P(f"\nElement bounds exist (accessibility node for the target): {sum(avail)}/{len(avail)} present targets "
      f"(all labelled ones, no canvas one). 'elements (today)' above is a naive label matcher over those nodes; "
      f"when the right node is picked its bounds are exact by construction.\n")
    # Confidence separation
    P("### Box confidence: present vs absent targets\n")
    P("| method | conf p50 present | conf p50 absent | AUC conf | AUC visible-noul | AUC conf x noul |\n|---|---|---|---|---|---|")

    def auc(pos, neg):
        if not pos or not neg:
            return float("nan")
        s = sum((1.0 if p > n else 0.5 if p == n else 0.0) for p in pos for n in neg)
        return s / (len(pos) * len(neg))

    thresholds = {}
    for m in jev_img:
        a = M.ans[m]
        pc = [a[i["id"]]["targets"][t["key"]]["conf"] for i, t in present if i["id"] in a and t["key"] in a[i["id"]]["targets"]]
        nc = [a[i["id"]]["targets"][t["key"]]["conf"] for i, t in absent if i["id"] in a and t["key"] in a[i["id"]]["targets"]]
        pv = [a[i["id"]]["targets"][t["key"]]["vis"] or 0 for i, t in present if i["id"] in a and t["key"] in a[i["id"]]["targets"]]
        nv = [a[i["id"]]["targets"][t["key"]]["vis"] or 0 for i, t in absent if i["id"] in a and t["key"] in a[i["id"]]["targets"]]
        pm = [c * v for c, v in zip(pc, pv)]
        nm = [c * v for c, v in zip(nc, nv)]
        P(f"| {m} | {pc and statistics.median(pc):.2f} | {nc and statistics.median(nc):.2f} | {auc(pc, nc):.3f} | {auc(pv, nv):.3f} | {auc(pm, nm):.3f} |")
    P()
    P("### Threshold sweep (Box confidence gate): end-to-end J2 accuracy over present + absent targets\n")
    P("Correct = present target: passes the gate and hits; absent target: rejected by the gate. "
      "'false tap' = an absent target let through, or a present one let through that misses.\n")
    P("| method | gate | T | end-to-end acc | present hit+pass | absent rejected | false taps |\n|---|---|---|---|---|---|---|")
    best = {}
    for m in jev_img:
        a = M.ans[m]
        for gate in ("conf", "vis", "both"):
            for T in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
                good = falsetap = ph = ar = 0
                for i, t in present:
                    x = a.get(i["id"], {}).get("targets", {}).get(t["key"])
                    if not x:
                        continue
                    g = {"conf": x["conf"], "vis": x["vis"] or 0, "both": min(x["conf"], x["vis"] or 0)}[gate]
                    hit = inside(*x["xy"], t["box"])
                    if g >= T and hit:
                        good += 1; ph += 1
                    elif g >= T:
                        falsetap += 1
                for i, t in absent:
                    x = a.get(i["id"], {}).get("targets", {}).get(t["key"])
                    if not x:
                        continue
                    g = {"conf": x["conf"], "vis": x["vis"] or 0, "both": min(x["conf"], x["vis"] or 0)}[gate]
                    if g < T:
                        good += 1; ar += 1
                    else:
                        falsetap += 1
                n = len(present) + len(absent)
                key = (m, gate)
                if key not in best or good > best[key][1]:
                    best[key] = (T, good, ph, ar, falsetap)
                if T in (0.5, 0.7) or gate == "conf":
                    P(f"| {m} | {gate} | {T} | {f(good / n)} | {ph}/{len(present)} | {ar}/{len(absent)} | {falsetap} |")
    P("\nBest threshold per gate: " + "; ".join(f"{m} {g}: T={v[0]} ({f(v[1] / (len(present) + len(absent)))})" for (m, g), v in best.items()))
    for m in ["haiku", "baseline"]:
        a = M.ans[m]
        good = falsetap = ph = ar = 0
        pr = [(i, t) for i, t in present if i["id"] in a]
        ab = [(i, t) for i, t in absent if i["id"] in a]
        for i, t in pr:
            x = a.get(i["id"], {}).get("targets", {}).get(t["key"])
            if x and x.get("present") and x.get("xy"):
                if inside(*x["xy"], t["box"]):
                    good += 1; ph += 1
                else:
                    falsetap += 1
        for i, t in ab:
            x = a.get(i["id"], {}).get("targets", {}).get(t["key"])
            if not x or not x.get("present"):
                good += 1; ar += 1
            else:
                falsetap += 1
        P(f"\n- {m}: end-to-end {f(good / (len(pr) + len(ab)))}, present hit {ph}/{len(pr)}, absent rejected {ar}/{len(ab)}, false taps {falsetap}")
    P()

    # --- J3-J6 ---------------------------------------------------------------------------
    def binary(title, truth_fn, key, methods, subset=None):
        P(f"## {title}\n")
        P("| method | accuracy | precision | recall | n |\n|---|---|---|---|---|")
        for m in methods:
            a = M.ans[m]
            pairs = [(truth_fn(i), bool(a[i["id"]][key])) for i in items if i["id"] in a and (subset is None or subset(i))
                     and a[i["id"]].get(key) is not None]
            acc, p, r, n = prf(pairs)
            P(f"| {'heuristic (today)' if m == 'baseline' else m} | {f(acc)} | {f(p)} | {f(r)} | {n} |")
        P()

    binary("J3: loading vs ready (positive = loading)", lambda i: i["loading"], "loading",
           ["baseline", "jev_text", "jev113_img768"] + jev_img + ["haiku"])
    pairs_ids = defaultdict(list)
    for i in items:
        if i.get("pair"):
            pairs_ids[i["pair"]].append(i)
    P(f"Loading/ready pairs: {sum(1 for v in pairs_ids.values() if len(v) == 2)} (HTML) plus {sum(1 for v in pairs_ids.values() if len(v) == 1)} "
      "canvas loading screens; every other image is a 'ready' negative.\n")
    binary("J4: landing check (positive = shows results for the asked query)", lambda i: i["landing"]["truth"], "landing",
           ["jev_text", "jev113_img768"] + jev_img + ["haiku"], subset=lambda i: bool(i.get("landing")))
    binary("J5: sensitive content (positive = financial or identity data visible)", lambda i: i["sensitive"], "sensitive",
           ["jev_text", "jev113_img768"] + jev_img + ["haiku"])

    P("## J6: real vs fake permission dialog\n")
    P("3-way accuracy (system / app_drawn / none) on all images, on the dialog set only, and binary 'real system dialog' precision/recall.\n")
    P("| method | 3-way all | 3-way dialog set | system P | system R | app-drawn called system |\n|---|---|---|---|---|---|")
    dialog_set = [i for i in items if i["dialog"] != "none" or i["category"] == "dialog"]
    for m in ["jev_text", "jev113_img768"] + jev_img + ["haiku"]:
        a = M.ans[m]
        al = [a[i["id"]]["dialog"] == i["dialog"] for i in items if i["id"] in a]
        ds = [a[i["id"]]["dialog"] == i["dialog"] for i in dialog_set if i["id"] in a]
        acc, p, r, n = prf([(i["dialog"] == "system", a[i["id"]]["dialog"] == "system") for i in items if i["id"] in a])
        fake_as_sys = sum(1 for i in items if i["id"] in a and i["dialog"] == "app_drawn" and a[i["id"]]["dialog"] == "system")
        n_fake = sum(1 for i in items if i["dialog"] == "app_drawn")
        P(f"| {m} | {f(sum(al) / len(al))} | {f(sum(ds) / len(ds))} | {f(p)} | {f(r)} | {fake_as_sys}/{n_fake} |")
    P(f"\nDialog set: {sum(1 for i in items if i['dialog'] == 'system')} real system dialogs, {sum(1 for i in items if i['dialog'] == 'app_drawn')} app-drawn.\n")

    # --- latency and cost ----------------------------------------------------------------
    P("## Latency (wall clock per request from this Mac) and cost\n")
    P("Combined requests carry every job's questions (Jev: 4-5 questions + 2 per target; Haiku: one JSON answer for all jobs).\n")
    P("| method | requests | failures | p50 ms | p90 ms | mean input tokens | mean output tokens | $ per 1k requests |\n|---|---|---|---|---|---|---|---|")
    for m in ["jev_text", "jev113_img768"] + jev_img + ["haiku"]:
        lat = M.lat[m]
        tin = [t[0] for t in M.tok[m]]
        tout = [t[1] for t in M.tok[m]]
        n_ok = len(lat)
        P(f"| {m} | {M.requests[m]} | {M.fail[m]} | {pct(lat, .5):.0f} | {pct(lat, .9):.0f} | {statistics.mean(tin) if tin else 0:.0f} | "
          f"{statistics.mean(tout) if tout else 0:.0f} | {1000 * M.cost[m] / n_ok if n_ok else 0:.4f} |")
    P()
    iso = rows("jev_iso.jsonl")
    if iso:
        P("### Single-question latency (30 images x 3 repeats, jev-preview)\n")
        P("| question | size | n | p50 ms | p90 ms | repeat p50s | mean input tokens |\n|---|---|---|---|---|---|---|")
        for kind in ("state", "box"):
            for size in (512, 768, 1024):
                rs = [r for r in iso if r.get("kind") == kind and r.get("size") == size and r["status"] == 200]
                reps = [pct([r["latency_ms"] for r in rs if r["rep"] == k], .5) for k in range(3)]
                tin = [r["usage"]["input_tokens"] for r in rs]
                P(f"| {kind} | {size} | {len(rs)} | {pct([r['latency_ms'] for r in rs], .5):.0f} | {pct([r['latency_ms'] for r in rs], .9):.0f} | "
                  f"{' / '.join(f'{x:.0f}' for x in reps)} | {statistics.mean(tin) if tin else 0:.0f} |")
        fails = sum(1 for r in iso if r["status"] != 200)
        P(f"\nFailures: {fails} of {len(iso)}.\n")
    hiso = rows("haiku_iso.jsonl")
    if hiso:
        P("### Haiku single-job latency (30 images x 2 repeats)\n")
        P("| job | n | p50 ms | p90 ms | mean output tokens |\n|---|---|---|---|---|")
        for kind in ("state", "locate"):
            rs = [r for r in hiso if r.get("kind") == kind and r["status"] == 200]
            P(f"| {kind} | {len(rs)} | {pct([r['latency_ms'] for r in rs], .5):.0f} | {pct([r['latency_ms'] for r in rs], .9):.0f} | "
              f"{statistics.mean(r['usage']['output_tokens'] for r in rs):.0f} |")
        # locate accuracy in iso
        ok = n = 0
        for r in hiso:
            if r.get("kind") != "locate" or r["status"] != 200 or not r.get("answers"):
                continue
            item = M.by_id[r["id"]]
            t = item["targets"][0]
            x = (r["answers"].get("targets") or {}).get(t["key"]) or {}
            n += 1
            if x.get("present") and x.get("x") is not None and inside(x["x"] / r["scale"], x["y"] / r["scale"], t["box"]):
                ok += 1
        P(f"\nHaiku locate-only hit rate on these: {ok}/{n}.\n")
    tb = rows("haiku_textb.jsonl")
    if tb:
        lat = [r["latency_ms"] for r in tb if r["status"] == 200]
        P(f"### Haiku text-only 'what to tap' (pattern B's first half), {len(lat)} calls\n\np50 {pct(lat, .5):.0f} ms, p90 {pct(lat, .9):.0f} ms, "
          f"mean output tokens {statistics.mean(r['usage']['output_tokens'] for r in tb if r['status'] == 200):.0f}.\n")
    jt = sum(M.requests[m] for m in M.requests if m.startswith("jev")) + len(iso)
    ht = M.requests["haiku"] + len(hiso) + len(tb)
    jc = sum(M.cost[m] for m in M.cost if m.startswith("jev")) + sum(r["usage"]["input_tokens"] * JEV_PRICE for r in iso if r["status"] == 200)
    hc = M.cost["haiku"] + sum(r["usage"]["input_tokens"] * HAIKU_IN + r["usage"]["output_tokens"] * HAIKU_OUT for r in hiso + tb if r["status"] == 200)
    P(f"## Totals\n\nJev requests: {jt} (${jc:.4f}). Haiku calls: {ht} (${hc:.4f}). Prices: Jev $0.042/Mtok input, output free; Haiku 5.5 $0.10/$0.50 per Mtok.\n")


if __name__ == "__main__":
    main()
