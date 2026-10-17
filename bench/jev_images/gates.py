"""J2 detail: Box gates at 768 px (present hit / absent rejected / false taps), all and canvas only, and the
canvas targets where Jev Box and Haiku disagree.  python gates.py --images <scratch images dir>"""

from __future__ import annotations

import argparse
from pathlib import Path

import analyze
import common


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True)
    ap.add_argument("--method", default="jev_img768")
    args = ap.parse_args()
    items = common.load_labels()
    M = analyze.Methods(Path(args.images), items)
    a = M.ans[args.method]
    pres = [(i, t) for i in items for t in i["targets"]]
    ab = [(i, t) for i in items for t in i["absent"]]
    gates = {"conf": lambda x: x["conf"], "vis": lambda x: x["vis"] or 0,
             "both": lambda x: min(x["conf"], x["vis"] or 0),
             "vis+conf>=0.3": lambda x: (x["vis"] or 0) if x["conf"] >= 0.3 else 0}

    def run(gate, T, sub):
        g = gates[gate]
        ph = ft = ar = 0
        P = [(i, t) for i, t in pres if sub(i)]
        A = [(i, t) for i, t in ab if sub(i)]
        for i, t in P:
            x = a[i["id"]]["targets"][t["key"]]
            if g(x) >= T:
                if analyze.inside(*x["xy"], t["box"]):
                    ph += 1
                else:
                    ft += 1
        for i, t in A:
            x = a[i["id"]]["targets"][t["key"]]
            if g(x) < T:
                ar += 1
            else:
                ft += 1
        return f"e2e {(ph + ar) / (len(P) + len(A)):.3f}, present {ph}/{len(P)}, absent rejected {ar}/{len(A)}, false taps {ft}"

    print(f"| gate | T | all | canvas only |\n|---|---|---|---|")
    for gate, T in [("conf", 0.6), ("conf", 0.7), ("vis", 0.5), ("both", 0.3), ("both", 0.5), ("vis+conf>=0.3", 0.5)]:
        print(f"| {gate} | {T} | {run(gate, T, lambda i: True)} | {run(gate, T, lambda i: i['category'] == 'canvas')} |")
    print("\nCanvas targets where Jev Box or Haiku missed:")
    h = M.ans["haiku"]
    for i, t in pres:
        if i["category"] != "canvas":
            continue
        x = a[i["id"]]["targets"][t["key"]]
        y = h[i["id"]]["targets"].get(t["key"])
        jh = analyze.inside(*x["xy"], t["box"])
        hh = bool(y and y.get("xy") and analyze.inside(*y["xy"], t["box"]))
        if not jh or not hh:
            print(f"- {i['id']} {t['desc']}: Jev hit={jh} (conf {x['conf']:.2f}, visible {x['vis']}), Haiku hit={hh}")


if __name__ == "__main__":
    main()
