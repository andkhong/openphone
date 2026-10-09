# Canvas apps and games: chess, Tetris, Where Winds Meet

Status: strategy, 2026-10-09, branch `design/canvas-games`. Nothing in this document was run on a phone.
**Measured** means a number somebody read off a run (all of them come from `AGENTS.md`, `docs/benchmarks.md` and
`docs/design/fewer-model-calls.md`, the owner's Samsung SM-S948U1, 2026-10-08) or a constant read in source.
Every other number is an estimate and says so. Line numbers refer to `main` at `6491b7f`.

This replaces the main session's four-layer draft (perception tiers, per-game adapters, decisions by time
budget, phone-side gestures). §10 lists where it disagrees and why.

## 0. The strategy in ten lines

1. The vision model is the wrong thing to decide a move, and the Accessibility screenshot is the wrong sensor
   for a real-time game. Today every canvas step is screenshot → Haiku 5.5 at high effort → a coordinate tap,
   1.3–3 s of model time per step (**measured**), and chess costs 50–65 s for 5 moves (**measured**).
2. Build one **canvas framework**, not per-game solutions: `profile → reader → policy → gesture plan →
   verifier`, with the model as **supervisor** at phase boundaries (menus, dialogs, game over) and on any
   surprise. Profiles are data; readers and policies are the only per-game code.
3. Three game classes map onto that framework with different parts: **turn-based boards** (chess, cards,
   puzzles: typed state + engine, screenshot cadence is fine), **real-time grids** (Tetris: typed state +
   heuristic, needs frames and no settle), **free-form screens** (3D worlds and every game menu: OCR +
   numbered regions + the model, no typed state at all).
4. Decision tiers that actually exist: code or an engine (milliseconds), Jev (0.15 s, text only, **measured**),
   the vision model (1.3–3 s, **measured**). There is no cheap "small vision model" tier; §3.3 says why.
5. The phone needs three additions, in order: OCR of any treeless screen on the phone (the gatekeeper build
   already ships ML Kit for hidden dialogs), a `gesture_plan` method (several strokes, phone-timed), and agent
   access to the video stream it already encodes for the live view (Tetris only, behind a measurement gate).
6. Safety: canvas-policy taps stay inside the profile's board rectangle (server), the phone judges unlabeled
   taps in games by the OCR line under the point (phone), money still goes through the Play billing sheet the
   gate already reads, and the remaining gap (icon-only in-game spends with no text) is disclosed, not hidden.
7. Terms of service are worse than the draft assumed. Chess.com's **Fair Play Policy** exempts games against
   its computer, but its **User Agreement** (2026-03-25, §4(C)) bans "automation software (bots)" and any
   access other than through its interface. NetEase's Terms of Use (2024-12-15, §4.5(a)) name "simulating game
   user operations", "record macro", "keyboard mapping" and "cloud phone" as violations, and Where Winds Meet
   publishes monthly ban lists. PlayStudios (Tetris) bans "automation software, bots" (§18).
8. So: **chess first, as a capability benchmark**, with an offline open-source chess app as the reader's test
   target and Chess.com only for the owner's own demo, at the owner's stated risk. **Tetris** behind a gate
   (measure the 3 fps path before touching the stream). **Where Winds Meet: not built.** The generic
   free-form tier covers any game's menus without a WWM adapter.
9. Phase 1 (one week): the chess profile and reader, a Stockfish policy, the canvas step in the loop behind
   `canvas_profiles` (off), tests on board fixtures, and a measurement on the owner's phone with the owner's OK.
   Target: "play 5 moves against a bot" with zero mis-taps and a median under 25 s (estimate 12–20 s).
10. Phases 2–3 generalise (profile schema, OCR on the phone, `gesture_plan`, numbered regions measured against
    raw coordinates, drive-level tools), then decide Tetris on numbers.

## 1. What the measurements say

| Fact | Value | Source |
|---|---|---|
| Chess, "open Chess.com, start a game against a bot, play 5 moves" | 46–65 s, 16–21 steps, every move a vision step, one run tapped the wrong square | **measured**, `fewer-model-calls.md` §2, `AGENTS.md` |
| A vision decision on a canvas screen (Haiku 5.5, `canvas_effort` high) | 1.3–3 s | **measured**, the task brief |
| A vision decision on a labeled screen (Haiku 5.5, low effort) | 1.1–1.5 s median | **measured**, `benchmarks.md` |
| Observe (app, elements, screenshot) | 70–580 ms; the screenshot alone about 0.3–0.45 s | **measured**, `benchmarks.md`, `mcp-drive.md` §9 |
| Jev, one request | about 140–150 ms | **measured**, `benchmarks.md` |
| Settle after a tap (adaptive) | 0.3–0.8 s | **measured** cap and quiet time, `benchmarks.md` |
| Labeled apps, 12-task benchmark | median 4.7 s, p90 8.4 s, 30/30 | **measured**, `AGENTS.md` |
| Accessibility screenshot interval | 333 ms (`ACCESSIBILITY_TAKE_SCREENSHOT_REQUEST_INTERVAL_TIMES_MS`, `@hide`) | AOSP source, read 2026-10-09; the app's `MIN_CAPTURE_INTERVAL_MS = 340` |
| Gesture limits | one stroke per protocol call today; AOSP allows 20 strokes per gesture, 60 s total, `continueStroke` since API 26; a dispatched gesture cancels any gesture in progress, the user's included | `PhoneActions.kt:466`, AOSP `GestureDescription.java` |
| ML Kit OCR first use | 3.5 s on an emulator (model load) | **measured** in a code comment, `PermissionOcr.kt` (gatekeeper) |

What the chess number is made of, from the step records in §2 of `fewer-model-calls.md`: about 10 taps (two per
move), 5 waits for the bot, and 3–6 menu steps, each a vision step of 2.5–3.5 s (model + screenshot + settle).
The model's time is roughly half the task; the rest is settle, waits and screenshots. Cutting the model out of
the moves is worth about 25–35 s of the 50–65 s (estimate); the menus stay vision steps until §4.4's free-form
tier earns its keep.

## 2. What exists today, and what a game layer can reuse

Read from `main` (`openmob/`, `android/`) and the gatekeeper worktree (`origin/gatekeeper-p1`).

### 2.1 Capture

- **Accessibility screenshot** (`PhoneActions.screenshot`, `PhoneActions.kt:113`): `takeScreenshot` → hardware
  buffer → ARGB copy → scale to 1280 px → JPEG 80. Rate-limited by the system to one per 333 ms; the app waits
  out the rest of the window instead of retrying. A black frame for `FLAG_SECURE` windows. So the agent's
  sensor runs at **at most 3 fps**, and each frame costs about 0.3–0.45 s end to end (**measured**).
- **MediaProjection H.264 stream** (`ScreenStreamer.kt`): for the dashboard's live view only. The owner approves
  once per connection; on Android 14+ one `createVirtualDisplay` per approval is allowed, so the app creates the
  virtual display once with no surface and points it at an encoder surface whenever a viewer watches
  (`ScreenStreamer.kt:63-70`). 1280 px long side, VBR, keyframe interval 4 s, `KEY_LATENCY 1`, no B-frames,
  frames dropped under a weak upload. **The server never decodes it**: `RemoteDriver.on_video`
  (`openmob/phone/remote.py:105`) fans packets out to viewer sockets. On the gatekeeper build `StreamGate.kt`
  holds every packet 250 ms and blacks out blocked apps. The draft's "Tetris likely needs MediaProjection" is
  right about the sensor and wrong about the cost: the consent and the virtual display already exist; what is
  missing is a consumer of the frames (§4.6).
- **Frames comparator** (`openmob/agent/waiting.py:183-201`): a screenshot reduced to 18×32 grayscale cells;
  "changed" needs 2 % of cells (12 of 576) to move by more than 24. That is how `wait_until screen_changes`
  works on a treeless screen. **Risk, from reading the code, not measured:** a chess piece moving two squares
  on a 1080×2400 screen touches about 4–6 cells, below the threshold, so the opponent's move may not register
  as a change. The canvas step needs its own "state changed" condition (§4.5).
- **OCR**: the server's OCR is Apple Vision (`openmob/vision/ocr.py`), **macOS only**, so the hosted Linux
  server has none and production canvas screens reach the model with no words. The gatekeeper build has ML Kit
  text recognition on the phone (`PermissionOcr.kt`), used only to read Android's hidden permission dialogs;
  its lines appear in `ui_elements` as class `OCRText`. `docs/architecture.md` §6 already lists on-device OCR as
  the prerequisite for games.

### 2.2 The loop

- `_effort_for` (`loop.py:549`) raises Claude's effort to `canvas_effort` on a screen with no readable labels.
  This is the owner's measured choice and the reason a canvas decision costs 1.3–3 s rather than 1.1–1.5 s.
- `ui_tree: auto` stops asking for the element list after two empty dumps in an app (`loop.py:687`).
- `wait_until` with `screen_changes` / `text_appears` / `loading_done` (`waiting.py`), on the phone where
  `wait_for` exists, else polled; the system prompt tells the model to use `screen_changes` for an opponent's
  move (`tools.py:256`).
- `max_actions_per_turn` (off; **measured** no gain) lets one reply tap a piece then its square, with a look in
  between; `_batch_unexpected` can't judge treeless screens so it carries on (`loop.py:527`).
- Replay and plan-execute never issue coordinate taps, by design (`fewer-model-calls.md` §6).
- Every act goes through `_execute` → `_dispatch` → `_guard` → the phone's gatekeeper. A coordinate tap is
  judged by the real element under the point, with the model's `target` words adding caution only.
- The MCP drive level (`feature/mcp-drive`, uncommitted on 2026-10-09) runs an outside agent's step-by-step
  commands through the same `_execute`; `observe` returns the element list and optionally a screenshot; no
  batching of acts per call, on purpose (`mcp-drive.md` §8).

### 2.3 The gatekeeper on canvas screens

`Gatekeeper.kt` (gatekeeper worktree) judges a tap by the hit-tested node. On a canvas there is one node (the
game's view), so the label is empty and the tap is "unlabeled". An unlabeled tap asks **only** in shopping,
payment, food, travel or ride apps or in payment context (`Gatekeeper.kt:1326-1328`; design §2.4). In a game it
is allowed. Real-money purchases go through the Play billing sheet, a labeled system window the word rules read.
The documented residual (`phone-gatekeeper.md` §5) is **icon-only spends of in-game currency**. Act calls are
rate-limited to 5/s, burst 15 (§2.7), which bounds any real-time loop.

### 2.4 Reuse table

| Needed by a game layer | Exists | Missing |
|---|---|---|
| A screenshot at ≤ 3 fps | yes | — |
| Frames at 5–15 fps | the phone encodes them for the live view | a decoder on the server, or an `ImageReader` on the phone; the agent as a stream consumer |
| Words on a treeless screen | macOS server only; ML Kit on the gatekeeper phone for dialogs | ML Kit for any treeless screen, in both builds, listed in `ui_elements` |
| A typed game state | — | profiles, readers (§4.2) |
| A no-model decision | Jev for screen phase; replay/plan for labeled apps | engines and heuristics (§4.3) |
| Exact taps | `tap x,y` judged by the element under the point | taps at computed square centres from the canvas step |
| Several timed strokes | one stroke per call, serialized | `gesture_plan` (§4.5) |
| Waiting for a move | `wait_until screen_changes` on frames | a reader-based `state_changes` (§4.5) |
| Supervision and escalation | the vision step, `canvas_effort` | the canvas step's handover rules (§4.4) |
| Safety on canvas | unlabeled-in-money-apps rule, billing sheet words, rate limits | OCR-judged canvas taps, board-rectangle bound, a per-game acknowledgment (§5) |

## 3. What the field does, and what it implies here

Dated sources; the full list is in §12.

### 3.1 Language-model agents at games

- **lmgame-Bench** (Hu et al., arXiv 2505.15146, May 2025; ICLR 2026) tests 13 models on six games including
  Sokoban, 2048 and Tetris. Its harness's **perception module hands the model text tables of the grid** instead
  of the picture, because "visual state extraction" is where models fail; the authors report that the harness
  improves every game and that for non-puzzle games "perception is critical". o3 and o1 led in 2025. The
  implication is direct: **read the board into text (or a typed state) and never ask the vision model to
  perceive a grid.** Once the state is text, a chess engine or a Tetris heuristic beats any model at both speed
  and strength, so the model doesn't even need to decide.
- **UI-TARS-2** (ByteDance, arXiv 2509.02544, Sep 2025): an RL-trained native GUI agent, 230B MoE with 23B active,
  self-reported mean 59.8 on a 15-game suite (about 60 % of human) and 73.3 on AndroidWorld. Not runnable on
  OpenMob's budget, and its game play still trails humans; it shows that a native agent needs a frontier-size
  model and RL to reach 60 % of a human at games.
- **SIMA 2** (Google DeepMind, 2025-11-13): Gemini-based, plays 3D games from pixels with keyboard and mouse,
  research preview only, "constrained" on "precise low-level actions". Not available, and the wrong shape (a
  single large model in the loop at frame cadence).
- **Cradle** (arXiv 2403.03186, NeurIPS 2024 workshop, ICML 2025): GPT-4o playing Red Dead Redemption 2 from
  screenshots; it **pauses input while it waits for the model**, so the world has to hold still; combat is where
  it fails. The same shape OpenMob has today, with the same ceiling.
- **AppAgent** (arXiv 2312.13771, Dec 2023), **Mobile-Agent** (arXiv 2401.16158, Jan 2024) and **OS-Copilot**
  (arXiv 2402.07456, Feb 2024): the mobile and desktop agents the draft names. AppAgent labels XML elements;
  Mobile-Agent works from OCR plus icon detection with no XML. Both are the labeled-app loop OpenMob already
  has, and neither plays games. **Mobile-Agent-v3 / GUI-Owl** (arXiv 2508.15144, Aug 2025) reached 73.3 on
  AndroidWorld; 2026 aggregator snapshots put closed models in the 80s and the benchmark is near saturation,
  with the harder MobileWorld successor at about 52 % for the best framework. None of this is about canvases.

### 3.2 Grounding: numbers, marks and parsers

- **Set-of-Mark** (Yang et al., arXiv 2310.11441, Oct 2023): overlay numbered marks on **segmented regions**;
  GPT-4V then refers to "9" instead of guessing a coordinate, and zero-shot beat fine-tuned referring models on
  RefCOCOg. The marks sit on segments, not on a uniform grid; a grid overlay was not what the paper tested. For
  OpenMob: on a canvas menu, numbered boxes over OCR lines and detected icons let the model answer with an index
  (as it does with `tap_element` on labeled screens) rather than a pixel; whether that beats Haiku's raw
  coordinates on this phone is a measurement, not a given (§8, phase 2).
- **OmniParser V2** (Microsoft, Feb 2025): YOLOv8 icon detector + Florence-2 captioner, 0.6 s per frame on an
  A100, 0.8 s on an RTX 4090; the icon detector is AGPL. Rejected (§9): a GPU and a second per frame to get
  marks that OCR plus a connected-components pass gives in tens of milliseconds for most game menus.
- **Small grounding models** (ScreenSpot-Pro, 2026 snapshots): Holo2-4B 57.2 %, Holo1.5-3B 51.4 % (vendor
  tables), frontier models around 85 % (BenchLM, Sept 2026). A 4B VLM still needs a GPU server call; on the phone
  a 2B VLM runs on the CPU at tens of tokens per second with image encoding on top, so seconds per screenshot
  (Arm's Qwen3-VL-2B walkthrough runs on the CPU; a June 2026 Snapdragon 8 Elite study shows the NPU helps the
  vision encoder 20–45× but decode only 1.18×). Neither gives the "0.3–1 s small model" tier the draft assumed.

### 3.3 Android facts the draft got right, wrong or incomplete

| Draft claim | Verdict |
|---|---|
| Accessibility screenshots are rate-limited to about 3 per second | **Right.** 333 ms in AOSP (`@hide` constant); the app already paces to 340 ms |
| Tetris likely needs MediaProjection; consent per session on Android 14+ | **Half right.** Consent is per session and a session is one `createVirtualDisplay`; the app already handles it once per connection and keeps the display for the connection. What's missing is a frame consumer, not consent. The projection also shows Android's screen-sharing chip the whole time, which doubles as the "being viewed" indicator the gatekeeper design asks for |
| Phone-side gesture sequences with exact timing | **Needed and feasible.** One `GestureDescription` may hold 20 strokes over 60 s; holds continue across gestures with `continueStroke`. Today's app dispatches one stroke per call and serializes them, so a two-tap chess move costs two round trips plus the server's checks between them |
| (not in the draft) Android 17 Advanced Protection Mode | Revokes accessibility access for apps not classified as accessibility tools (Beta 2, 2026-02-26; announced October 2026). Owners who turn it on can't run OpenMob at all. Google Play's Accessibility API policy (2026-01-28) already bars autonomous action. Neither changes the game plan, but a game feature that needs the Accessibility service plus MediaProjection plus a long session is the most exposed thing OpenMob ships |

### 3.4 Chess, Tetris and Where Winds Meet on bots and automation

- **Chess.com.** Fair Play Policy (2026-03-25): "it does not apply to your games that are not against other
  users, such as in the game mode versus Chess.com computer"; against people, "Do not use chess engines,
  software of any kind, bots, plugins, browser extensions". User Agreement (2026-03-25): §2(B) "You agree not to
  access the Service by any means other than through the interface that is provided by Chess.com"; §4(C) do not
  "use automation software (bots), hacks, modifications (mods) or any other unauthorized third-party software";
  §4(D) restricts "automated or artificial intelligence (AI) tools" accessing the service. Chess.com has no play
  API (its public API is read-only; app and board developers apply through a form). So a bot game is clean under
  fair play and still a breach of the User Agreement's automation clause. The draft's "bot bans" risk is the
  smaller one; the agreement is the real one.
- **Lichess.** Board API: a regular account, a client that a human operates, engine assistance "strictly
  forbidden"; Bot API: engines allowed, but only on a dedicated BOT account, which needs no phone at all. The
  compliant route for "an engine plays chess" exists and doesn't involve a screen, which is why it is an
  alternative to reject for *this* product (§9) and the honest answer for a user who just wants an engine to play.
- **Offline open-source chess apps** (DroidFish is the usual one: GPL, no account, Stockfish on the device,
  a custom-drawn board) carry no terms-of-service stake at all. They are the right **benchmark target** for the
  reader and the canvas step; whether the board is a canvas with no accessibility nodes must be verified on
  the phone (likely, from how it is drawn).
- **Tetris (PlayStudios).** Terms (2024-09-27) §18: do not "develop, distribute, use, or publicly inform other
  members of cheats, automation software, bots, hacks, mods". No play exemption.
- **Where Winds Meet (NetEase).** Terms of Use (2024-12-15) §4.5(a): do not "use cheats, automation software
  (bots), hacks, mods, or any other unauthorized third-party software … such as to simulating game user
  operations, changing the operating environment, … including but not limited to scripts (robots), plug-ins,
  button wizard software or third-party tools (e.g. the multi-open function under the simulator, synchronizer,
  record macro, keyboard mapping, cloud phone, etc.)". The game publishes suspension notices with partly masked
  names: about 5,000 accounts for 2025-11-15 to 2025-12-05 (Icy Veins, Mein-MMO), 402 for 2025-08-25 to
  2025-09-26 in the notice dated 2026-09-28 on the official site ("cheating, speed hacks, macros, and other
  actions that disrupt fair play"; penalties "leaderboard removal, gameplay restrictions, or
  temporary/permanent bans"). Mobile launched 2025-12-12 with cross-play. "Chores only" is not an exemption
  the terms offer. See §6.

## 4. The strategy

### 4.1 One principle, one framework

**Read the screen into a typed state once per game type; decide with the cheapest thing that can; act with a
gesture plan; verify with the cheapest read; let the model supervise, not drive.**

The flexibility the owner asked for ("it can't be one solution") comes from *interfaces plus data*, not from
per-game code paths through the loop:

```
            profile (data: app, geometry, capture mode, reader kind, policy kind, phases)
                 │
   screenshot or frame ──▶ Reader ──▶ State ──▶ Policy ──▶ GesturePlan ──▶ phone
                 ▲                                              │
                 └──────── Verifier (re-read, cheap) ◀──────────┘
                                   │ confident?  no ──▶ the vision step (today's loop), with the state as text
```

- **Profile**: JSON shipped with the server, keyed by package (and screen size): which reader, which policy,
  where the board can be, how moves are entered (tap-tap or drag), which screen phases exist (menu, playing,
  opponent's turn, game over), and what is *never* tapped (anything outside the board).
- **Reader**: `screenshot → State | None` with a confidence. Per game *class*, not per game: a `GridReader`
  (checker boards, Tetris wells, 2048) parameterised by the profile; a `TextReader` (OCR lines, any screen).
- **State**: a typed value (`FEN`, a Tetris grid plus current piece, a list of OCR lines with boxes). Never free
  text the model could mistake for instructions.
- **Policy**: `State → Decision` in milliseconds: an engine (Stockfish over UCI), a heuristic (Dellacherie for
  Tetris), or `Escalate(reason)`.
- **GesturePlan**: a list of strokes with timing, executed as today's `tap`/`swipe` calls (phase 1) and later as
  one `gesture_plan` phone call (phase 2), each stroke judged by the gatekeeper.
- **Verifier**: the reader again, after the cheapest wait that fits (a frames or state change), with a budget of
  one retry before escalating.
- **Supervisor**: the existing vision step, reached by escalation, given the last typed state as text ("Board:
  FEN …; your move; last read confidence 0.62; reason: board not found"). The model handles menus, dialogs,
  promotions, game over, "tap to start", ads, and anything the profile didn't foresee, then hands back when the
  reader is confident again.

This is the shape the replay cache and the plan executor already have in the loop for labeled apps: a no-model
executor that runs first on each iteration and falls through to the model. The canvas step is a third such
executor (§4.4), with coordinate taps that the other two never issue.

### 4.2 Three game classes, one framework

| Class | Examples | Capture | Reader | Policy | Verify | Model's job |
|---|---|---|---|---|---|---|
| **Turn-based board** | chess, checkers, cards, 2048, Sokoban-likes | Accessibility screenshot (3 fps is plenty: the opponent's bot replies in under a second and we move once) | `GridReader` with templates calibrated from a known start position | engine or solver, 10–200 ms (estimate) | re-read after `state_changes` | menus, dialogs, game over; choose the mode the request asked for |
| **Real-time grid** | Tetris, block drops, match-3 timers | frames at 5–15 fps from the stream (phase 3), the screenshot path at 3 fps first | `GridReader` on the well; current piece from the top rows and the "next" box | heuristic, under 5 ms (estimate); inputs fired with no settle | the next frame | menus, game over; never inside a drop |
| **Free-form** | 3D worlds, every game's menus and shops, drawing apps | screenshot | `TextReader` (OCR on the phone) plus numbered regions | none: the model decides, with marks to point at | the loop's existing checks | everything; this is today's loop with better input |

The first two classes need a reader per board *shape*, parameterised by profile; the third needs no reader
and no profile beyond "this app is a game" for the safety rules in §5. Where Winds Meet, if it were ever in
scope, is class three throughout: its "chores" are menus. A HUD reader (health bars, minimap) is the draft's
idea; it is only useful for navigation and combat, which are out of scope, so it is not built.

### 4.3 Decision tiers, by what exists

| Tier | Cost | Good for | Not for |
|---|---|---|---|
| Code, engines, heuristics | 1–200 ms (estimate; Stockfish `go movetime 50` plus process overhead) | choosing a move from a typed state; legality; state tracking | anything the reader couldn't type |
| Jev | about 150 ms (**measured**), text in, labels out | classifying the screen *phase* from OCR words (menu, playing, popup, game over, "tap to start": the heads in `openmob/jev.py` already name these) | picking moves: it is a text classifier, not a planner (this contradicts the 2026-10-05 note "let Jev or a classic algorithm pick moves": the algorithm picks, Jev tells which phase we are in) |
| The vision model | 1.3–3 s canvas, 1.1–1.5 s labeled (**measured**) | menus, dialogs, surprises, the request's intent | every move of a game |

The draft's fourth tier, "a small model ~0.3–1 s", does not exist at that price for images (§3.2). Don't plan
around it. If one appears (a hosted grounding model under 300 ms), it slots into the free-form class as the
thing that turns a screenshot into numbered regions; nothing else changes.

"Think during opponent turns or animations" (the draft) is rejected: against a bot the reply takes under a
second, so there is nothing to think during; in menus a decision made on a stale screen is what
`_batch_unexpected` exists to stop; and plan-then-execute, the one place a pre-computed plan was tried, was
**measured** slower on short tasks. The engine thinks in 50 ms; the model is not in the move loop at all.

### 4.4 The canvas step in the loop

A fourth no-model executor, tried before `_fast_step` and only when a profile matches `obs.package`:

1. **Phase.** With the phone's OCR words (phase 2) Jev says the phase; without them (phase 1) the reader's own
   answer does: board found with confidence ≥ 0.9 means *playing*, else the model's turn.
2. **Read.** `GridReader` on `obs.shot`. The board rectangle is found each time (a banner or an ad can shift it:
   the draft's "calibrate once" is only safe for the *templates*, which are cut from the start position at the
   first confident read of a new game and cached per `(package, screen size, orientation)`).
3. **Track.** The state machine holds the game: after each read, the diff against the last state must be one
   legal move (castling moves four squares, en passant three, a promotion changes a piece type). Legal moves and
   the resulting position come from Stockfish (`go perft 1` lists moves; `position … moves …` then `d` prints the
   FEN), so no chess logic is written in Python. A diff that isn't one legal move: re-read once after 300 ms,
   then escalate with the two boards as text.
4. **Decide.** `go movetime 50` (phase 1: a fixed low strength so the bot game lasts and the owner can watch).
5. **Act.** `_execute("tap", {"x", "y", "target": "e2 (white pawn)"}, source="canvas")` then the destination
   square, with `settle = 0` between them and the usual settle after; both points are inside the board rectangle
   by construction (§5.1). The gatekeeper sees two unlabeled taps in a game app and allows them, as today.
6. **Verify.** `wait_until state_changes` (§4.5), then a read; the expected position must match. One retry
   (the app may have needed a drag), then escalate.
7. **Opponent.** `wait_until state_changes` with the bot's budget (10 s), then step 3.
8. **Hand back.** The task's request decides the stop ("5 moves", "until the game ends", "one game"); the
   canvas step counts and ends with the existing `done` path (the classifier's done can't fire after a vision
   step, so a game that opened through menus ends with one model confirmation, as today).

Escalation reasons are recorded on the step (`source = canvas`, a new `canvas_reason`) so the timings report
can say why the model was called. Nothing from the reader is stored beyond the task except the step rows.

### 4.5 Phone additions, in the order they pay

1. **OCR of treeless screens on the phone** (phase 2, both builds). `PermissionOcr` already does it for one
   window on the gatekeeper build; generalise: when `ui_elements` would return nothing labeled, read the screen
   with ML Kit and list the lines as `OCRText` with boxes, as the server's macOS OCR does today. Costs: the APK
   grows by the bundled Latin model; a read is an estimated 100–400 ms per frame on a flagship (the 3.5 s first
   load is **measured** on an emulator, so warm the model at connect). Gains: words on canvas screens in
   production (today none), Jev can classify game phases, the free-form tier gets its marks, and the gatekeeper
   can judge canvas taps by text (§5.2).
2. **`state_changes`** (phase 1, server only): a `wait_until` condition evaluated with the profile's reader,
   polling screenshots at the 333 ms cadence, so a one-piece move counts as a change even when the 18×32 frames
   comparator can't see it. Falls back to `screen_changes`.
3. **`gesture_plan`** (phase 2, phone): one call carrying up to 20 strokes with start offsets and durations,
   built into one `GestureDescription` (or chained with `continueStroke` for holds longer than one gesture),
   judged by the gatekeeper **per stroke** with the same rules as `tap`/`swipe`/`long_press` (swipe-as-tap,
   sliders, unlabeled-in-money-context), with `reverify` before dispatch, and refused as a whole if any stroke
   is refused. Timing is exact on the phone instead of two server round trips per chess move; Tetris needs it
   for tap-tap-tap-swipe sequences and soft-drop holds. Rate limit: a plan counts as its number of strokes
   against the 5/s budget, so a plan of 15 strokes is the burst.
4. **Agent frames from the stream** (phase 3, gated): the agent calls `stream_start` as a viewer (it is a
   phone method already; the owner's one approval per connection covers it) and the server decodes the H.264
   packets it already receives (PyAV, a new dependency; software decode of 1280 px frames at 10 fps is well
   within one core, estimate). The gatekeeper's `StreamGate` still blacks out blocked apps and still holds 250 ms,
   so frame age is about 300–450 ms on that build (estimate: hold + encode + network + decode); on the standard
   build about 50–200 ms (estimate; scrcpy's own figure for its similar pipeline is 35–70 ms). A phone-side
   `ImageReader` would be lower latency but needs a second surface on the one virtual display, which is the
   expensive route; not planned unless the server decode is measured too slow.

### 4.6 Generic versus per-game

| Generic (built once) | Per game class | Per game (data, or small code) |
|---|---|---|
| the canvas step and its escalation rules | `GridReader` (any checker or cell board) | profile JSON: package, board bounds hints, cell counts, move input, phases, stop rules |
| profile loading and matching | `TextReader` (OCR lines) | piece or cell templates (auto-cut from the start position, or shipped) |
| `state_changes`, `gesture_plan`, OCR on the phone | the engine or heuristic adapter interface | which engine: Stockfish for chess; Dellacherie weights for Tetris |
| numbered regions for the model | | a few lines of phase cues ("Game over", "Play again", "Rematch") |
| the step records, the timings report, the safety bounds | | |

A new board game should be a profile plus templates and an existing policy; a new real-time grid game is a
profile plus a heuristic; a new 3D game is nothing (free-form).

### 4.7 Latency budgets (estimates, per layer)

| Layer | Budget | Basis |
|---|---|---|
| Screenshot | 0.35–0.6 s | 333 ms interval plus encode and transfer; **measured** observe 0.3–0.45 s |
| Reader (chess, numpy template match on 64 cells) | 20–80 ms | estimate; measure in phase 1 |
| Policy (Stockfish, movetime 50) | 60–150 ms | estimate; the UCI round trip dominates |
| Two taps | 0.15–0.3 s | 50 ms strokes plus two round trips (phase 1); one `gesture_plan` call in phase 2 |
| Verify (`state_changes` + read) | 0.4–0.7 s | one screenshot cadence plus the read |
| **Own chess move, total** | **1.0–1.8 s** | against 2.5–3.5 s per vision step today, and two of them per move |
| Opponent's move detected | the bot's time + 0.35–0.7 s | polling at the screenshot cadence |
| **"5 moves against a bot", cold** | **12–20 s** | 3–6 menu vision steps (7–15 s) + 5 × (move + bot reply) |
| Tetris input, screenshot path | 0.45–0.7 s | capture-bound; enough for gravity under about 1 cell/s |
| Tetris input, stream path | 0.15–0.35 s | frame age + heuristic + a stroke; bounded by the 5/s act limit |
| Free-form (menus) vision step | 2.5–3.5 s today; perhaps −0.3–0.5 s with OCR words and marks | estimate; measure in phase 2 |

The menus remain the floor for chess: a task that spends 10 s getting to a bot game can't go under 10 s whatever
the moves cost. In-app links (`market://`, Chess.com's own links) and the replay cache can't help because the
screens are canvases; the free-form tier's marks are the only lever there, and it is unmeasured.

## 5. Safety

### 5.1 Server side: what the canvas step may do

- A canvas-policy act is only ever a `tap`, `swipe` or (later) `gesture_plan` whose every point lies inside the
  profile's current board rectangle, checked in the canvas step before `_execute` and again in `_dispatch` for
  `source = canvas`. Anything outside the board (a "Rematch", an "Upgrade", a shop icon) is the model's
  decision through the vision step, where `_guard`, Jev's risk heads and the owner's prompts apply as today.
- The reader's output is typed; the model only ever sees it as a line of text in the handover note, marked as
  state, not as instructions. An engine cannot be prompt-injected. OCR words reach the model as today, under the
  `untrusted_content` rule.
- Permission modes: in `ask` mode every changing act asks, which would be ten prompts for five moves. Owner
  decision (§11): treat a canvas-policy tap inside the board as "navigation" for `ask` mode, or keep the rule.
  `plan` mode: the canvas step runs only after the owner approved a plan that names the game and the stop rule.
- The step cap: a game can run long; the canvas step counts each move as a step against `max_steps` and asks
  the request's stop rule ("5 moves", "one game", "15 minutes") up front; an open-ended "play chess" ends at the
  cap with `done(success=false, "stopped after N moves")`.
- A task is one game in one app: if `current_app` changes mid-game the canvas step stops and the model looks.

### 5.2 On the phone: closing the canvas gap as far as it closes

The gatekeeper's residual, "icon-only spends of in-game currency", is real and stays partly open. In order of
value:

1. **Real money.** Google Play's billing sheet is a labeled system window; the gate's payment-context and
   commit-word rules already ask on "Buy", "Subscribe" and a price. Verify on the phone that Chess.com's and a
   Tetris shop's purchase flows reach that sheet (they should: Play's policy requires its billing for in-app
   digital goods). This is the bound that matters most.
2. **OCR-judged canvas taps** (phase 2, gatekeeper build). With OCR lines available on the phone, an unlabeled
   tap in an app whose `ApplicationInfo.category` is `CATEGORY_GAME` (or whose package the shipped category
   table lists as a game) is judged by the OCR line whose box contains the point: "BUY", "SHOP", "CONFIRM",
   "$4.99", "GEMS" then hit the word rules and ask, as a labeled button would. Cost: an OCR read before the
   tap when the cache is older than 1.5 s (`PermissionOcr` caches per window and change stamp), an estimated
   100–400 ms. Residual: an icon with no text, and text the lexicon doesn't cover.
3. **A per-game acknowledgment grant** (`architecture.md` §5 already proposes it, signed on the phone): "Let
   agents play <game>? They can tap anywhere in it and can't tell a shop button from a move. Purchases still
   ask." Shown once per game, revocable under Activity → Always allowed. This is the honest disclosure of
   residual 2 and the control the owner has over it.
4. **Rate limits** already bound a runaway loop (5 acts/s, burst 15; three denials suspend the session).
5. **Not done:** a server-declared "no-spend region". The phone must not trust a server's geometry (design
   G3); the board-rectangle rule is a server-side bound on the server's own policy, nothing more.

### 5.3 The MCP drive level

Outside agents drive with their own model and get `observe` (elements, optional screenshot). For canvases:

- `read_state(session_id)`: the profile's typed state as structured content (`{"kind": "chess", "fen": …,
  "confidence": …}`), a read like `observe`, no act. Lets an outside agent play chess without a grounding
  model of its own: it decides the move in text and sends two taps.
- `gesture_plan(session_id, strokes)`: the phone method through `_execute`, every stroke judged; the same
  board-rectangle bound applies when a profile is active, so an outside agent can't use it to tap a shop faster
  than the gate reads.
- Profiles are server-owned data in phase 1 and 2: outside agents can't add or edit them. A profile that moved
  the "board" over a shop would turn the bound into a hole; if profiles ever come from clients they are
  proposals, like skills and memory in `architecture.md` §5.
- The drive level's existing refusals stand: one act per call, no approving, screenshots under `screen:read`.

## 6. Terms of service and legal guardrails

Plainly: **every one of the three reference games forbids what OpenMob does on it**, by the operators' own
text (§3.4). Chess.com's fair-play carve-out for bot games protects the user from a fair-play ban, not from the
User Agreement's automation clause. The product rules that follow:

1. **Never against humans, by default.** The chess profile only enters games through the app's "vs computer"
   path and stops (escalates with `done(success=false)`) on any screen with an opponent clock and username.
   `ask_user` before any online, rated or ranked mode; refuse PvP modes the user names (`architecture.md` §6).
2. **A per-game acknowledgment** on the phone (§5.2.3) that states the operator's rule in one line ("Chess.com's
   terms forbid automation software; playing here with OpenMob is at your own risk"), signed once per game and
   shown in Activity. No game profile runs without it. Not a legal shield; an honest disclosure.
3. **Benchmark on an app with no stake.** The reader, the canvas step and the latency numbers are measured on an
   offline open-source chess app; Chess.com is the owner's own demo. The 20-task benchmark's canvas control
   (`docs/benchmarks.md`) should move to that app.
4. **Where Winds Meet is not built** (§4.2, §7). NetEase's terms list exactly OpenMob's mechanism, enforcement is
   monthly and public, and the owner's account would be the one banned. If the owner wants a 3D-RPG demo of
   the free-form tier anyway, it needs the acknowledgment above and no WWM-specific code exists to maintain.
5. **Never spend, never trade.** Money commits ask in every mode (gatekeeper D4); a game profile adds nothing
   that could pass a purchase, and the free-form tier runs under the existing rules.
6. **Human cadence online.** Where a game is online and the owner accepted the risk, inputs are capped at a
   human rate (the profile's `max_inputs_per_minute`), so OpenMob isn't the fastest automation on the server.
7. **No evasion.** No input-timing jitter "to look human", no hiding of the Accessibility service, no device
   spoofing. The product's position is disclosure, not disguise; if a game detects and blocks it, that is the
   game's right.

## 7. Phased plan

### Phase 0: instrument and collect (2 days, inside the phase 1 week)

- Add `source = canvas` and `canvas_reason` to the steps table and `openmob timings` (variant `+canvas`).
- With the owner's OK, collect a fixture set from the owner's Samsung: 40+ board screenshots across the offline
  chess app and Chess.com, two themes each, from the start position to mid-game, plus their FENs (typed by
  hand once), plus 10 non-board screens from the same apps (menus, game over, promotion dialog). An emulator
  with the offline app, which an agent may drive, covers the rest. Fixtures are the reader's tests.
- Re-run the chess task three times as the baseline with the current build, recorded per step.

### Phase 1: the chess profile (the one-week deliverable)

Behind `canvas_profiles: false` (`OPENMOB_CANVAS=on|off`); nothing changes when off.

- `openmob/canvas/profile.py`: the schema and loader; `openmob/canvas/profiles/*.json`: the offline chess app
  and Chess.com (package, board hints, move input, phases, stop rules).
- `openmob/canvas/grid.py`: board detection (the 8×8 alternating pattern by row and column colour
  transitions; the board is the largest square region with cell pitch = width / 8 and 8 alternating rows),
  template calibration from the start position (every square's occupant is known, so the 12 piece templates and
  the two square colours are cut from the first confident read: no hand labeling, any theme), per-square
  classification with the square's background colour neutralised (highlighted last-move squares), a confidence
  from the margin between the best two matches. numpy is a new dependency; OpenCV is not needed.
- `openmob/canvas/chess.py`: state tracking and the policy through Stockfish over UCI (`position … moves`,
  `d`, `go perft 1`, `go movetime 50`); no chess rules in Python; the engine is an optional system binary
  (Debian package in the Dockerfile; absent means the profile is inactive and the task runs as today).
- `openmob/agent/loop.py`: `_canvas_step` before `_fast_step`; `state_changes` in `waiting.py`; the handover
  note; `source = canvas` records.
- Tests: the reader on the fixtures; the state tracker on scripted boards; the canvas step on a fake phone that
  renders boards; a fake engine; the board-rectangle bound (a tap outside is never sent); escalation paths.
- Measurement on the owner's phone, with the owner's OK, 10 runs each of "open <offline app> and play 5 moves
  against the computer" and the Chess.com control, cold starts, from the home screen, via the dashboard chat.

Success metrics (all from the steps table and the fixtures, none from a single run):

| Metric | Target | How measured |
|---|---|---|
| Reader accuracy on fixtures | ≥ 99.5 % of squares, ≥ 95 % of boards exactly right | the test |
| Mis-placed or illegal moves | 0 in 10 runs | the verifier's post-move read, logged per move |
| Success, "play 5 moves against the computer" | ≥ 9 / 10 | `turns.success` |
| Task time, median (cold) | ≤ 25 s, against 50–65 s today | `total_ms` per task; estimate 12–20 s |
| Vision steps per task | ≤ 5 (menus, dialogs, done) | `fast = 0` rows |
| Canvas steps per task | ≥ 10 (two taps per move) | `source = canvas` |
| Per own move (read + decide + act + verify, no opponent time) | median ≤ 1.8 s | `canvas` step rows |
| Gatekeeper refusals or prompts in the bot game | 0 | `gate_*` codes in the feed |
| Taps outside the board from the canvas step | 0 | the bound's test, and the feed |

If the reader misses the 99.5 % on Chess.com's themes but not on the offline app, phase 1 still ships for the
offline app and Chess.com stays a vision-step game until the templates are fixed; say so in the report.

### Phase 2: generalise (1–2 weeks)

- OCR of treeless screens on the phone (both builds), listed in `ui_elements`; Jev phase classification on
  canvas screens; the gatekeeper's OCR-judged canvas taps (§5.2.2) and the per-game acknowledgment (§5.2.3).
- `gesture_plan` on the phone and in the protocol (`docs/phone-protocol.md` §4.18), per-stroke gating, tests
  in the evil-server suite (a plan whose last stroke lands on "Buy").
- Numbered regions for the model on free-form screens (OCR boxes plus a connected-components pass for
  icons), **measured** against raw coordinates on 10 canvas-menu tasks: taps on target, steps per task, time.
  Kept only if it wins.
- MCP drive: `read_state`, `gesture_plan`.
- A second board profile (checkers or 2048) to prove "a profile plus a policy" and nothing else.

### Phase 3: Tetris, behind a gate (1–2 weeks if the gate opens)

1. On the screenshot path first: a Tetris profile, `GridReader` on the well, the current piece from the top
   rows, Dellacherie's heuristic, inputs with no settle. Measure the gravity level at which inputs arrive too
   late. Expected (estimate): fine at level 1–3, not beyond.
2. Only if the owner wants higher levels: the agent as a stream viewer plus server-side decode (§4.5.4).
   Measure frame age on both builds. Expected (estimate): 50–200 ms standard, 300–450 ms gatekeeper.
3. The terms problem is the same as Chess.com's and the acknowledgment applies. Product value: none beyond
   proving the real-time path; say so before spending the weeks.

### Not planned

- **Where Winds Meet**: §6.4. The free-form tier is the only thing a 3D game needs, and it is phase 2.
- **An adapter catalog over MCP**: profiles are server data; what the drive level needs is `read_state` and
  `gesture_plan` (phase 2). A catalog that clients add to is an untrusted-profile problem (§5.3) for later.

## 8. Alternatives rejected

| Alternative | Why not |
|---|---|
| Keep the vision model per move, add `max_actions_per_turn` for piece-then-square | **Measured** no gain for labeled apps; on canvases it halves the model calls per move at best (one per move instead of two) and keeps the mis-taps. Not a fix |
| A hosted grounding model (Holo2-4B class) per step | A GPU call of 0.3–1 s to produce a coordinate the reader computes exactly in 50 ms; 57 % on ScreenSpot-Pro is not a reliability win |
| OmniParser V2 as the generic parser | 0.6 s per frame on an A100, AGPL detector, and marks that OCR plus connected components give for game menus |
| A VLM on the phone | Seconds per image on the CPU; the NPU helps encode, not decode; thermal throttling within minutes |
| UI-TARS-2, SIMA 2 or an RL-trained native agent | Not available (SIMA 2) or 230B parameters (UI-TARS-2); both still below humans at games; the wrong cost shape for a phone product |
| Lichess Bot API as the chess product | Compliant and screen-free, which is why it isn't this product; offer it as the honest answer to "I want an engine to play for me" |
| Chess.com as the benchmark target | Its User Agreement bans automation; results on it are the owner's demo, not a number to publish |
| python-chess (GPL-3) as a library | The repo has no license file and `docs/self-hosting.md` distributes the server; Stockfish as a separate process over UCI avoids the question. Owner decision |
| A Python chess move generator | A week of work that `go perft 1` replaces |
| "Think during the opponent's turn" | §4.3: nothing to think during; stale-screen decisions are what the batch checks stop |
| A WWM HUD adapter | Only useful for navigation and combat, both out of scope; the terms make the whole game out of scope |
| `getevent`/shell-helper input for games | Lower latency than `dispatchGesture` but needs the wireless-debugging helper the roadmap defers, and skips the gatekeeper, which is the point of the product |
| Training a Tetris policy | Dellacherie's hand weights are enough at the levels the sensor allows |
| A phone-side `ImageReader` for frames first | Needs a second surface on the single Android 14+ virtual display; the server can decode the stream it already receives |
| Calibrate once, cache forever | Boards shift under banners; templates are cheap to re-cut at each new game |

## 9. Risks, honestly

1. **The reader on real themes.** Template matching reports 89 % on photographed boards in one open-source
   test set; clean screenshots with one theme should do much better (estimate ≥ 99 %), but Chess.com's piece
   sets, highlights, arrows, coordinates drawn on squares and premove shading are untested. Phase 0's fixtures
   and phase 1's metric decide; the fallback is the vision step, as today.
2. **The frames comparator may miss moves** (§2.1). `state_changes` is the fix and is in phase 1; until it is
   measured, the opponent-wait could time out and cost model calls.
3. **Menus stay slow.** 7–15 s of the 12–20 s estimate is the vision model in menus and ads. The free-form
   tier's gains are unmeasured; the estimate could be wrong by half.
4. **Terms of service.** §6. The owner's own account is the one at risk in every demo on a live service.
5. **Android 17 Advanced Protection.** Users who enable it lose OpenMob entirely; a game feature leaning on
   Accessibility plus MediaProjection is the most exposed part of the product.
6. **The gate's canvas gap** stays partly open (icon-only spends). §5.2 narrows it and discloses the rest.
7. **Scope creep.** Every game wants its own reader; the discipline is "a profile plus a policy, or nothing".
8. **Jev's role.** Phase classification from OCR words is plausible from its heads but unmeasured on games.

## 10. Where this disagrees with the draft

1. **Perception tiers as a fixed stack** (tree → OCR → template matching → grid detection → vision model):
   template and grid matching are not generic tiers; they are the reader a profile selects. The generic order
   is tree → OCR on the phone → *profile reader if one matches* → marks → vision model.
2. **Per-game state adapters including a WWM HUD**: readers are per board *shape*; a HUD reader serves
   navigation and combat, both out of scope and both against the terms. Free-form screens get no state.
3. **A "small model ~0.3–1 s" decision tier**: doesn't exist for images at that price (§3.2). The tiers are
   code, Jev and the vision model.
4. **The big model sets the plan**: the profile *is* the plan; a model planning call was **measured** slower on
   short tasks. The model supervises at phase boundaries.
5. **Think during opponent turns or animations**: rejected, §4.3.
6. **Calibrate-once caching**: templates once per game, the board rectangle every read, keyed by package and
   screen size.
7. **Tetris needs MediaProjection**: the consent and the virtual display exist; the missing piece is a frame
   consumer, and the screenshot path should be measured first.
8. **WWM chores only**: not built. NetEase's terms name the mechanism, not the activity.
9. **Chess.com's risk is "bot bans"**: the User Agreement's automation clause is the risk; bot games are clean
   under fair play only.
10. **An adapter catalog over MCP**: `read_state` and `gesture_plan` instead; profiles stay server-owned.
11. **Risks: "adapter maintenance"**: agreed, and the answer is the rule in §9.7, plus auto-cut templates so a
    theme change doesn't need a code change.

## 11. Owner decisions needed

1. Accept that Chess.com's User Agreement forbids this, and keep Chess.com as the owner's own demo only? Which
   offline chess app is the benchmark target (DroidFish suggested; verify its board is a canvas)?
2. Stockfish as a system binary (GPL, separate process) in the Docker image and as an optional local
   dependency: yes? python-chess as an import: no, unless a license is chosen for the repo.
3. numpy as a server dependency.
4. Tetris: still wanted given PlayStudios §18 and no product value beyond the real-time proof? Which app?
5. Where Winds Meet: drop, as recommended, or owner-risk demo of the free-form tier only?
6. `ask` mode and canvas-policy taps inside the board: ten prompts per five moves, or treat them as navigation?
7. The per-game acknowledgment grant (§5.2.3): text and binding (per app, revocable).
8. Phone work order: OCR of treeless screens in both builds first (it also fixes production's "no words on
   canvas"), then `gesture_plan`, then stream frames only if phase 3 opens.
9. The 20-task benchmark's canvas control: move it to the offline app.

## 12. Sources

Repository (read 2026-10-09): `AGENTS.md`; `docs/benchmarks.md`; `docs/design/fewer-model-calls.md` §2, §6;
`docs/design/phone-gatekeeper.md` §2.4, §2.7, §5; `docs/design/mcp-drive.md` (worktree, uncommitted) §3, §8, §9;
`docs/architecture.md` §5–§7; `docs/phone-protocol.md` §4; `openmob/agent/loop.py`, `waiting.py`, `tools.py`,
`vision/ocr.py`, `phone/remote.py`; `android/.../PhoneActions.kt`, `ScreenStreamer.kt`,
`OpenMobAccessibilityService.kt`; gatekeeper worktree `PermissionOcr.kt`, `StreamGate.kt`, `gate/Gatekeeper.kt`.

Android:
- AOSP `frameworks/base` `AccessibilityService.java` (main, read 2026-10-09): `ACCESSIBILITY_TAKE_SCREENSHOT_REQUEST_INTERVAL_TIMES_MS = 333`; `ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT`; dispatched gestures cancel gestures in progress.
- AOSP `GestureDescription.java` (main, read 2026-10-09): `MAX_STROKE_COUNT = 20`, `MAX_GESTURE_DURATION_MS = 60 * 1000`, `continueStroke`.
- Android developers, "Media projection" (read 2026-10-09): consent before each session; a session is one `createVirtualDisplay`; Android 14 single-app capture.
- Android Authority, 2026-03 (Android 17 Beta 2, 2026-02-26) and The Hacker News, 2026-10: Advanced Protection Mode revokes accessibility access for apps that aren't accessibility tools.
- Google Play Accessibility API policy update, effective 2026-01-28 (the policy page `docs/architecture.md` §7 links, answer 10964491; the January 2026 wording per Android Authority and Heise coverage).
- scrcpy README: 35–70 ms latency for its screen-capture pipeline.

Research:
- Hu et al., "lmgame-Bench: How Good are LLMs at Playing Games?", arXiv 2505.15146 (May 2025), ICLR 2026.
- Yang et al., "Set-of-Mark Prompting Unleashes Extraordinary Visual Grounding in GPT-4V", arXiv 2310.11441 (Oct 2023).
- ByteDance, "UI-TARS-2 Technical Report", arXiv 2509.02544 (Sep 2025).
- Google DeepMind, SIMA 2 research preview, 2025-11-13 (TechCrunch, The Decoder, InfoQ coverage).
- Tan et al., "Cradle: Empowering Foundation Agents Towards General Computer Control", arXiv 2403.03186 (2024).
- Zhang et al., "AppAgent", arXiv 2312.13771 (Dec 2023); Wang et al., "Mobile-Agent", arXiv 2401.16158 (Jan 2024); Wu et al., "OS-Copilot", arXiv 2402.07456 (Feb 2024); "Mobile-Agent-v3", arXiv 2508.15144 (Aug 2025).
- Microsoft Research, "OmniParser V2" (Feb 2025): 0.6 s/frame A100, 0.8 s RTX 4090; AGPL `icon_detect`.
- ScreenSpot-Pro and AndroidWorld aggregator snapshots (BenchLM, Benchmarklist, H Company's model table), 2026-03 to 2026-09; self-reported figures flagged as such.
- Arm, "Run an optimized vision-language model from the Arm AI Portal on Android" (Qwen3-VL-2B, CPU); "Phase Matters: Characterizing Heterogeneous Vision-Language Inference on a Mobile SoC", arXiv 2606.27906 (June 2026).
- Automatik/Detect-Chessboard-FEN (template matching, 89 % on 64 photos); linrock/chessboard-recognizer (CNN).

Terms:
- Chess.com Fair Play Policy and User Agreement, both "Last updated: March 25, 2026" (chess.com/legal/fair-play, chess.com/legal/user-agreement); Chess.com support, "What is the PubAPI" (read-only).
- Lichess, "Welcome Lichess Bots" and the Board API page (lichess.org/page/eboards).
- PLAYSTUDIOS Terms of Service, "DATE LAST MODIFIED: September 27, 2024", §18 (playstudios.com/terms).
- NetEase Terms of Use for Where Winds Meet, "LAST UPDATED: December 15, 2024", §4.5(a) (wherewindsmeetgame.com/termsofuse.html); "Account Suspension Notice (August 25–September 26)", 2026-09-28 (wherewindsmeetgame.com/news/official/Banreport926.html); Icy Veins and Mein-MMO on the 2025-11-15 to 2025-12-05 suspensions; mobile launch 2025-12-12 (TechRadar, RPGamer).
