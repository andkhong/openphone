# Jev images benchmark (2026-10-17)

Can OpenMob use Jev's new image support where Jev on text isn't enough? This is an offline benchmark on 163
labelled emulator screenshots. **Measured** numbers come from this run. **Estimate** marks anything built from
those measurements plus older ones. Scripts, labels and raw answers are in `bench/jev_images/`. Images aren't
committed; `collect.py` rebuilds them on AVD `gk_b7`.

## Verdict

| job | Jev image vs Haiku 5.5 (accuracy) | p50 / p90 (Jev, single question, 768 px) | Haiku p50 | verdict |
|---|---|---|---|---|
| J1 screen state | **95.1%** vs 92.6% (canvas 100% vs 96.2%) | 83 / 165 ms | 804 ms | **GO** |
| J2 locate a target | present hit 96.0% vs 91.0%; gated end-to-end 97.4% vs 95.0%. **Canvas only: 93.2% vs 100%** | 83 / 163 ms | 838 ms | **GO, with conditions**: gate on the visibility Noul, and fall back to Haiku below the gate (see J2) |
| J3 loading vs ready | 96.9% vs 95.7% (recall 88.9% vs 77.8%) | about 83 / 165 ms (estimate: one Noul like J1) | about 800 ms | **GO** |
| J4 landing check | 95.5–100% vs 95.5% (n=22) | same | same | **GO on paper, but not needed**: Jev text is also 100% on these labelled pages. Keep text |
| J5 sensitive content | 99.4% vs 98.8% (recall 92.3% each) | same | same | **GO** as an extra signal, never as the only one |
| J6 real vs fake permission dialog | 100% vs 93.3% (n=20) | same | same | **GO as an advisory signal only.** **NO-GO as a security control** (see risks) |

- **Recommended image size: 768 px long side.** Measured accuracy is the same at 512, 768 and 1024 for J1, J3, J5 and
  J6. J2 hits 93.8% at 512, 96.0% at 768 and 96.0% at 1024. Latency is flat across sizes (80–85 ms p50), while
  input tokens grow (1,057 → 1,587 for one Box). At 512 the short side (230 px) is under Jev's 280 px minimum, so it
  has to be padded.
- **Box threshold:** don't gate on the Box's `confidence` alone. Its AUC for present vs absent targets is 0.96, and
  its best end-to-end score is 88.5% (T=0.6). Ask a `noul` "Is there X in `screen`?" alongside each Box, as the
  TypeSafe docs suggest: AUC 0.99.
  - **For taps, use `min(box.confidence, visible) >= 0.5`.** It made 1 false tap in 340 targets (0 on canvas) and
    covered 156 of 177 present targets.
  - Below the gate, escalate to the main model.
  - `visible >= 0.5` alone covers more (169/177) but made 6 false taps.
- **Pattern A** (Jev state plus Box tap, no main model): **GO for profiled canvas screens** (menus, pop-ups, game
  over, dialogs), with the gate and a Haiku fallback. Estimated model time per step: about 0.1 s instead of
  0.8–3 s.
- **Pattern B** (text-only Haiku decides *what*, Jev Box finds *where*): **NO-GO.** Text-only Haiku was measured
  *slower* (p50 1.70 s, p90 2.36 s) than Haiku vision answering with coordinates (p50 0.84 s), so B is slower than
  plain Haiku vision.

## Set-up

- **Data, measured:** 163 screenshots from the emulator, 1080×2400, Android 15, with no personal data.
  - 53 canvas screens. These are my own HTML5 canvas pages, served from the scratch folder at `10.0.2.2:8791`,
    shown in Chrome full screen, with no accessibility text:
    - chess menu ("Play vs computer"), chess board, Tetris-like grid, Play/Settings/Quit menu (text and icon-only);
    - game over, reward pop-up, canvas-drawn fake permission dialog, card wallet;
    - 12 loading screens (spinner, text, progress bar, splash).
  - 110 labelled screens:
    - 13 Settings pages, home, app drawer, lock screen, the bank/shop/login stand-ins;
    - 22 search-results pages (positives, near-miss queries, no results, suggestions only);
    - 23 document pages: a fake statement, card 4111…, SSN 000-12-3456, payslip, versus recipes, news and weather.
      They also include HTML fake permission dialogs and app "turn on location?" prompts;
    - 15 loading/ready pairs (spinner, text, skeleton, progress bar, splash);
    - 7 real permission dialogs: location and contacts, each in light, dark and font 1.3, plus Chrome's
      notifications dialog;
    - 3 `SpoofActivity` screens (an app-drawn "Allow … notifications?" title).
- **Ground truth:** labels per image in `labels.jsonl`.
  - Target boxes come from the uiautomator dump (the clickable control's bounds) or from the canvas page's own
    report of what it drew, mapped to device pixels through corner markers.
  - 177 present targets and 163 absent ones (a named control that isn't on that screen).
- **Methods**
  - **Jev text:** today's `describe_screen` in `jev-latest` (1.13). The input is element labels, plus OCR words only
    when no element has text, as `agent/loop.py` does it.
  - **Jev image:** `jev-preview` (1.14.0), multipart upload, JPEG q80 at 512/768/1024. One request per image carried
    every job's questions plus a Box and a visibility Noul per target ("combined"). A separate run asked one question
    alone (30 images × 3 sizes × 3 repeats, "single").
  - **Jev 1.13 image** at 768, to check that 1.13 reads images too (it does, equally well, except no Box).
  - **Haiku 5.5:** production settings (effort low, adaptive thinking). The image is 768 px on screens with at least
    8 labelled elements and 1280 otherwise. One JSON answer per image; single-job runs for latency.
  - **Today's baselines:**
    - for J3, the `waiting.loading_element` heuristic on one frame (no stillness check);
    - for J2, element bounds via a naive label match.
- **Network:** this Mac on its wired link, ping about 7 ms to `api.typesafe.ai` and `api.anthropic.com`.
  Sequential requests. The single-question Jev latency run overlapped the Haiku runs; they use different hosts.

## Results (measured)

### J1: screen state, 7 states (loading, menu, content, game over, dialog, login, home screen)

| method | all | canvas (53) | labelled (110) |
|---|---|---|---|
| Jev text (today) | 71.8% | **24.5%** | 94.5% |
| Jev 1.13 image 768 | 95.1% | 100% | 92.7% |
| Jev 1.14 image 512 / 768 / 1024 | 95.1% / 95.1% / 95.1% | 100% | 92.7% |
| Haiku 5.5 vision | 92.6% | 96.2% | 90.9% |

Jev's state answer was identical at all three sizes on all 163 images. The 8 Jev misses are mostly arguable labels,
and Haiku got the same ones wrong:
- the HTML splash ("Example" logo, labelled loading) answered as content;
- the `SpoofActivity` screen, which is built to look like a prompt, answered as dialog;
- the bank stand-in answered as loading.

Text-only Jev is blind on canvas: it answered "content" for most menus, dialogs and loading screens.

### J2: locate a named target

| method | present hit, all (177) | canvas (74) | labelled (103) | labelled without the search box |
|---|---|---|---|---|
| Jev Box 512 | 93.8% | 91.9% | 95.1% | 93.8% |
| Jev Box 768 | **96.0%** | 93.2% | 98.1% | 97.5% |
| Jev Box 1024 | 96.0% | 94.6% | 97.1% | 96.3% |
| Haiku vision, coordinates | 91.0% | **100%** | 84.5% | 95.1% |
| element label match (naive) | 45.2% | 0% | 77.7% | 98.8% |

- "The search box" was ambiguous: Haiku pointed at the magnifier icon beside the field 12 times.
- Element bounds exist for every labelled target (103/177) and for no canvas target.

**Absent targets.** Box always returns a box. At 768, the medians were:

| | present | absent |
|---|---|---|
| Box confidence | 0.90 | 0.13 |
| visibility Noul | (AUC 0.991) | |

The AUC is 0.959 for confidence and 0.991 for the visibility Noul.

End-to-end J2 at 768, over 340 targets (present: passes the gate and hits; absent: rejected):

| gate | all | canvas only |
|---|---|---|
| `confidence >= 0.6` | 88.5%, 10 false taps | 90.6%, 5 false taps |
| `visible >= 0.5` | 97.4%, 6 false taps | 96.1%, 5 false taps |
| `min(confidence, visible) >= 0.3` | 97.1%, 4 false taps | 96.1%, 3 false taps |
| **`min(confidence, visible) >= 0.5`** | 93.5%, **1 false tap** (156/177 present) | 94.5%, **0 false taps** (67/74 present) |
| Haiku (present=false for absent) | 95.0%, 14 false taps | — |

**Jev's canvas misses.** Jev missed 5 canvas targets that Haiku hit:
- the gear icon on the icon-only menu, twice (it boxed a neighbour);
- "Menu" next to "Retry" on the game-over screen, three times (it boxed Retry).

These are wrong-button errors, not near misses. All five had a Box confidence under 0.5, so the 0.5 gate
caught them.

**Against the go/no-go rule:** overall, Jev passes (96.0% vs 91.0%, 10× faster). On the canvas subset, where Jev
would actually replace Haiku because labelled screens already have element bounds, it is 6.8 points behind. Hence
the conditional GO: gate at 0.5 and fall back to Haiku. The estimated mix at the 90.5% canvas coverage is
0.905 × 83 ms + 0.095 × (83 + 838) ms ≈ **0.16 s** expected model time per locate, with no measured false tap.

### J3: loading vs ready (27 loading, 136 ready)

| method | accuracy | precision | recall |
|---|---|---|---|
| element/OCR heuristic (today, one frame) | 87.1% | 100% | 22.2% |
| Jev text | 90.2% | 82.4% | 51.9% |
| Jev 1.13 image 768 | 97.5% | 96.0% | 88.9% |
| Jev 1.14 image (all sizes) | 96.9% | 92.3% | 88.9% |
| Haiku | 95.7% | 95.5% | 77.8% |

Jev's three missed loading screens are the three HTML splash logos. Its false alarms (at 768) were Settings → Security and the bank stand-in.

### J4: landing check, 22 search pages (10 positive)

Jev text and Jev image at 512 and 1024 scored 100%. Jev image at 768 and Haiku each scored 95.5%, with one false
positive each (Jev: results for "pizza" taken as "pizza near me"). The sample is small.

### J5: sensitive content (13 positive)

| method | accuracy | precision | recall |
|---|---|---|---|
| Jev text | 97.5% | 90.9% | 76.9% |
| Jev image (1.13 and 1.14, all sizes) | 99.4% | 100% | 92.3% |
| Haiku | 98.8% | 92.3% | 92.3% |

### J6: real vs fake permission dialog (7 system, 13 app-drawn, 143 none)

| method | 3-way, all | 3-way, dialog set | "system" precision / recall | fakes called system |
|---|---|---|---|---|
| Jev text | 95.1% | 60.0% | 70% / 100% | 3/13 |
| Jev image (1.13 and 1.14, all sizes) | 100% | 100% | 100% / 100% | 0/13 |
| Haiku | 93.3% | 95.0% | 77.8% / 100% | 1/13 |

### Latency and cost

**Single question, jev-preview, 30 images × 3 repeats (90 per row), 0 failures in 540:**

| question | 512 p50 / p90 | 768 p50 / p90 | 1024 p50 / p90 | p50 of each repeat (768) |
|---|---|---|---|---|
| state (Choice) | 81 / 161 ms | 83 / 165 ms | 85 / 161 ms | 88 / 81 / 82 |
| Box | 80 / 156 ms | 83 / 163 ms | 83 / 164 ms | 90 / 80 / 83 |

**Combined requests (163 each):**
- Jev image at 512/768/1024: p50 95 / 92 / 100 ms; p90 303 / 298 / 295 ms. The combined request carries 5 job
  questions plus 2 per target.
- Jev text: 80 / 138 ms. Jev 1.13 image: 94 / 133 ms.
- Haiku: 997 / 1,232 ms.

**Haiku single job (60 each):**
- state: 804 / 991 ms;
- locate: 838 / 1,007 ms, hitting 53 of 60;
- text-only "what to tap": 1,699 / 2,355 ms (30 calls, 213 output tokens on average).

**Tokens:**
- Jev image 768: about 1,330 input tokens for one Choice and 1,660 for the combined request. The images.md page
  doesn't yet publish a token formula; these are measured.
- Haiku: about 1,410 input and 91 output tokens.

**Cost:** Jev costs $0.07 per 1,000 combined requests at 768 ($0.042/Mtok, output free). Haiku costs $0.19 per
1,000. Both are negligible next to the latency.

**Totals sent:**
- Jev: 1,362 requests, including 7 retries and 3 test requests, about $0.08. The budget was ≤1,500.
- Haiku: 314 calls, including 2 tests (one rejected for a missing workspace header), about $0.05. The budget was
  ≤600.

**The Haiku figures understate production.** These calls have no system prompt, no tools and no history. The
loop's vision steps measure 1.3–3 s (`benchmarks.md`), so the real speed-up is larger than the 10× measured here
(estimate).

## Pattern estimates (estimate, built from the measured parts)

| path | per-step time | notes |
|---|---|---|
| Today, vision step | screenshot 0.3–0.45 s + Haiku 1.3–3 s + action ~0.2 s + settle 0.5–1.5 s ≈ **2.3–5.2 s** | from `benchmarks.md` and `canvas-games.md` |
| **Pattern A** (Jev state + Box, gate, tap) | screenshot 0.3–0.45 s + Jev 0.08–0.30 s + action ~0.2 s + settle 0.5–1.5 s ≈ **1.1–2.5 s** | needs the *what* decided without a model: a canvas profile (the "Play vs computer" button on the menu state, X on a pop-up, Retry on game over) or an engine. About 10% of locates fall back to Haiku (+0.84 s) |
| Pattern B (text Haiku what + Jev where) | screenshot + 1.7–2.4 s + 0.08 s + action + settle ≈ **2.7–4.6 s** | slower than Haiku vision with coordinates; not worth building |

Against the MVP targets (≤1.5 s per step): pattern A brings canvas menu steps into range when settle is short
(estimate). Today's vision step can't get there.

## Risks

- **The preview model.** Box needs `jev-preview` / 1.14.0, and TypeSafe calls images and Box "Dreamer-ready … not
  production". The request format, answers and limits may change between releases. Pin `jev-1.14.0` and never ship
  on the moving alias. Re-run this benchmark (`collect.py`, then `run_jev.py`, then `analyze.py`) on every model
  bump, because accuracy will drift.
- **The data set is synthetic and easy.**
  - Clean fonts, my own drawings, one emulator and one screen size.
  - No real game art, animation blur, ads, notches or OEM skins (the Samsung lesson in AGENTS.md applies).
  - J4 (22) and J6 (20) are small.
  - Expect real-world accuracy to be lower; verify on the owner's real phone with their OK.
- **J6 is not a security boundary.** The fakes here were near the real dialogs, not pixel-perfect. A pixel-identical
  fake drawn by a hostile app would fool any image classifier. The gatekeeper must keep deciding from the window's
  owner (the permission-controller package and window type), with Jev's answer only as an extra flag that can raise
  caution, never lower it.
- **Wrong-button taps.** Box confused adjacent buttons ("Menu"/"Retry") and an icon with its neighbour. The gate
  caught every one here, but a tap through the gate still goes through the risky-tap and gatekeeper checks as today.
- **A new data flow.** Screenshots would go to TypeSafe, including ones with sensitive content. They must follow the
  same rules as screenshots to the main model: no blocked apps, nothing on sensitive screens that the main model
  wouldn't see. This is an owner decision and part of the batched security review.
- **Network.** About 80 ms p50 was measured from a Mac at 7 ms RTT. From the Fly server and from phones on cellular,
  the upload of a 35–60 KB JPEG adds time: unmeasured.

## Recommended integration order

1. **J1 + J3 on treeless screens** (canvas or OCR, where Jev text is blind). In the same request as today's
   `observe_step`, add the 768 px image when the element list has no text. This is advisory only: it chooses waits
   and the phase. Lowest risk, biggest accuracy gain (24.5% → 100% on canvas state).
2. **J2 in the canvas step (pattern A)** for profiled screens:
   - Box plus a visibility Noul per candidate target, tapping only when `min(confidence, visible) >= 0.5`;
   - otherwise the Haiku vision step, as today;
   - record `source = canvas_box` on the step rows so the real-phone hit rate can be measured.
3. **J5** as an extra "sensitive" signal on screens with no text. It may only add protection.
4. **J6** as an advisory flag beside the window-ownership check, after the security review.
5. **J4:** no change. Jev text already handles labelled landing pages.
6. Don't build pattern B.
