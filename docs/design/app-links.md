# In-app links: straight to a destination inside an app

Status: built 2026-10-08 (branch `feature/app-links`), **server only**, **not measured on a phone**. Every number
in §5 is an estimate; §6 is the plan that decides.

## 1. Why

Measured on the owner's Samsung (2026-10-08): a step costs 2–3 s (observe ~0.3 s, Jev ~0.15 s, settle 0.5–1.5 s,
a Claude decision ~1.1–1.5 s median when one is needed). The Settings deep links (docs/design/deep-links.md)
replaced tap-by-tap navigation with one intent: Wi-Fi 34.1 s → 3.4 s, Chrome notifications 26.5 s → 6.6 s,
Display 11.1 s → 6.5 s. Replaying a stored path brought repeat navigation to 7.4 s with no model call;
plan-then-Jev made short tasks slower and is off.

The same lever exists for most apps, with no table on the phone: apps open their own **https links** (App Links
verified for the app's domain), so `youtube.com/results?search_query=…` opens the YouTube app on its results,
`google.com/maps/dir/…` opens Maps on a route, `market://details?id=…` opens an app's Play Store page,
`wa.me/<number>` opens that WhatsApp chat, `open.spotify.com/search/…` opens Spotify's search. One `open_url`
replaces the launch, the taps and the typing that would get there. The owner's goal, in their words: "flexible and
fast; handle most apps in low latency with intelligence".

Targets (owner, 2026-10-08; docs/benchmarks.md): labeled-app navigation, first run, median ≤ 12 s; repeat ≤ 6 s;
first action ≤ 2 s; success ≥ 95 %.

## 2. Design: flexible first, a catalog for speed

### 2.1 Any link, as a step into its app (`open_url`)

`open_url` already existed with its checks (the scheme allowlist, the handler resolved on the phone against blocked
apps, the gatekeeper judging the URL on the phone). With `app_links` on it is now a **step into an app**:

- **The handler is resolved before opening** (`resolve_url`, newer apps and adb), as before, and a blocked
  handler or chooser candidate refuses the link. The server never sends an explicit package or component: Android
  picks the handler, so a link can only reach what the owner's phone would open for anyone.
- **It waits inside the step until the destination's window is up** (the screen left the one the link was
  opened from, `_await_new_screen`, up to 2.5 s) and reads the app in front. When that is the entry's app (or a
  browser, for entries a browser serves as well), the step ends there and the settle until the page is quiet and
  done loading (`launch_settle_delay`, `loading_done`) is the next step's, as after `launch_app` (since
  2026-10-08; before, the whole settle was inside the step, which made the first-step metric read the page load:
  docs/design/deep-links.md §6). When the quick look can't place the landing (the home screen, or neither the app
  nor a browser), the step settles fully and looks again, as before. It **never waits for a particular package
  in front**: an OEM may hand a link to another app (the Settings deep links hit this on the Samsung, where
  Battery opens Device care).
- **The result says where it landed**: "Opened … in YouTube."; or, when the catalog knows the link's app from its
  host (`app_links.app_for_url`: youtube.com, google.com/maps, play.google.com, open.spotify.com, wa.me, amazon.com,
  mail.google.com, calendar.google.com…) and a browser took it, "in Chrome, not the YouTube app: this phone sends
  these links to the browser; carry on on the web or launch_app YouTube and navigate", or "the YouTube app isn't
  installed … install_if_missing", or "the home screen is still in front: the link may not have opened". The model
  adjusts from that instead of guessing from the screenshot.
- **The feed shows the URL without its query** (`https://www.youtube.com/results?…`): the query holds the user's
  words, like typed text. The model still gets the full text.

Switched off (`OPENMOB_APP_LINKS=off`) `open_url` is exactly what it was, for comparisons.

### 2.2 The catalog (`openmob/agent/app_links.py`)

A server-side table of destinations for common apps. Each entry has the app it targets (its packages), a URL
template with named parameters (each with a kind that validates and encodes it), a **side-effect class**, how to
tell it landed (the app's packages; an optional screen cue), and, for `view` entries, the request shapes the fast
path recognises.

| Entry | Opens | Link | Parameters | Class | Fast | Note |
|---|---|---|---|---|---|---|
| `youtube.search` | YouTube results | `youtube.com/results?search_query=` | q | view | yes | playing a result is the next tap |
| `youtube.video` | a video | `youtube.com/watch?v=` | id | view | | |
| `youtube.channel` | a channel | `youtube.com/@handle` | handle | view | | |
| `maps.directions` | a Google Maps route | `google.com/maps/dir/?api=1&destination=…&origin=…&travelmode=…` | destination, origin?, mode? | view | yes | the preview; Start is the next tap |
| `maps.search` | Google Maps results | `google.com/maps/search/?api=1&query=` | q | view | yes | |
| `play_store.app` | an app's store page | `market://details?id=` | package | view | | Install is the next tap: free installs don't ask (owner decision); paid ones ask as a purchase |
| `play_store.search` | Play Store results | `market://search?c=apps&q=` | q | view | yes | |
| `chrome.open` | a web page | the address itself (http/https only) | url | view | yes | the default browser, or the app that claims the address |
| `spotify.search` | Spotify results | `open.spotify.com/search/` | q | view | yes | playing is the next tap |
| `whatsapp.chat` | a WhatsApp chat | `wa.me/<number>` | number | view | yes | the chat only, nothing typed (no `?text=`): Send is a tap that asks as today |
| `amazon.search` | Amazon results | `amazon.com/s?k=` | q | view | yes | results only; buying is taps that ask as today |
| `google.search` | Google results | `google.com/search?q=` | q | view | yes | |
| `gmail.inbox` | the Gmail inbox | `mail.google.com/mail/u/0/` | | view | yes | |
| `calendar.day` | a day in Google Calendar | `calendar.google.com/calendar/u/0/r/day/Y/M/D` | date (YYYY-MM-DD, today, tomorrow) | view | yes | |
| `messages.compose` | a text message draft | `sms:<number>?body=` | number, body? | draft | | Send is a tap that asks as today |
| `email.compose` | an email draft | `mailto:<to>?subject=&body=` | to, subject?, body? | draft | | Send is a tap that asks as today |
| `phone.dial` | the dialer with a number | `tel:<number>` | number | draft | | the dialer only, never a call (the phone's rule); Call is a tap |

Parameter kinds: `text` (≤ 200 characters, URL-encoded), `number` (7–15 digits after stripping separators),
`package` (an application id), `url` (http/https only, through the same scheme check as `open_url`), `id`, `handle`,
`email`, `date`, `choice`. A value that doesn't fit its kind is refused before anything is built, and the error
never repeats the value. Unknown parameters are refused too.

**Schemes.** The catalog uses only `https`, `market`, `sms`, `mailto` and `tel`: the schemes both the server
(`gate.SAFE_SCHEMES`) and the gatekeeper (`rules.json` `schemes_allowed`) accept. A custom scheme (`spotify:`,
`whatsapp:`, `intent:`) would be refused by both, so no entry has one (a test builds every entry and checks). An
entry with a custom scheme would need a gatekeeper rule first.

### 2.3 The tool for the main model: `open_link {app, intent, params}`

Offered on Android phones when `app_links` is on (any phone app: `open_url` is a base method). Its description
lists the catalog (`youtube: search(q), video(id), channel(handle); maps: directions(destination, origin?, mode?),
…; messages: compose(number, body?) [draft]; …`). The prompt line says to prefer it over tapping through an app,
to navigate if it errors or lands in the browser or on the wrong screen, and that `open_url` with an app's own
web link often opens in the app too for apps not listed.

It runs through `_execute`/`_dispatch` like every tool: Ask every step asks before it with the exact URL in the
prompt (the approval binds it, like `open_url`); Plan first needs the plan; the URL then goes through
`_open_destination` (§2.1) with the entry's app for the landing report.

### 2.4 The fast path (`app_links_fast`)

When a request plainly asks for one `view` entry, the link opens as the task's **first action**, with no
main-model decision. It runs once per task, after permission dialogs are handled and before the replay, plan and
Jev steps, and it doesn't need Jev. Two readings, both requiring the app to be named:

1. **A request shape that gives the values** (free, regex): "search YouTube for lofi", "play lofi hip hop on
   youtube", "open Spotify and search jazz", "directions to the airport in Maps", "open google maps and get
   directions from home to work", "find coffee shops near me on google maps", "search the play store for chess",
   "open bbc.com", "open bbc.com/news in chrome", "open a whatsapp chat with +1 (555) 123-4567", "google the weather
   in tokyo", "open my gmail inbox", "what's on my calendar today". Openers ("could you", "please") and closers are
   stripped; a trailing clause that is another instruction is cut from the value ("search youtube for lofi and play
   the first video" searches "lofi"; "cats and dogs" stays); a value naming a part of the screen ("find the
   subscribe button") isn't content and doesn't match.
2. **The app and an intent word, with the value left to the small text model** (`fast_path_text_model`, else
   `resolver_model`, through `_fast_value` with a link-specific instruction): "on youtube, could you look for some
   lofi beats" → the model is asked for `q` with the link and the parameter's meaning; "buy a usb-c cable on
   amazon" → the Amazon search opens (the purchase's taps ask as always); "install chess from the play store" →
   the Play Store search. No value (`null`) means no fast step. A kind that needs something specific (a number, a
   web address, a date) is only asked for when the request holds something of that shape, so "message mom on
   whatsapp" never costs a model call.

Anything else goes to the main model: two apps named ("search youtube for lofi on spotify"), no app named
("navigate to the airport", "check my email"), a plain launch ("open youtube": the direct launch handles it), a
request the user corrected mid-task, or, in the second reading, a side-effect word beyond the entry's own ("look
up the weather on google and send it to mom", "open youtube and like the video"). A **trailing** clause with a
side effect doesn't stop the first reading ("search youtube for lofi girl and subscribe", "search amazon for cables
and add one to my cart" open the search): the link is a strict subset of what the model would do first, and the
subscribe, cart and purchase taps that follow are judged by the guard exactly as always. `draft` and `action`
entries never take the fast path (a test injects one and checks).

**Before opening**, the phone is asked which app would take the link (`resolve_url`, one call). The fast step is
**not taken** when a browser would (the app isn't installed, or doesn't take its links on this phone), when a
chooser would show, or when the entry's app is blocked for this task: the main model then decides from the screen
as usual (it may still call `open_link`, knowing where it lands, or navigate). An older app that can't say
beforehand opens the link, and the landing is reported after. The resolution is kept for the open, so the phone
is asked once.

**After opening**, the step settles and reads the app in front (§2.1). A landing in the browser, a chooser or the
home screen sets `vision_next`: the main model takes the next step, told exactly what happened (the quick-helper
note in the conversation) and what to do (carry on on the web, launch the app, install it). An entry with a cue
also needs a label matching it on the next look (none has one today; packages suffice for these apps).

The classifier's DONE stays the main model's call for these tasks (`fast_path_done` needs a navigation-only
request, and "search"/"play" aren't): expect one model call to confirm done after a link.

### 2.5 Recording

Every link step is a normal step in the steps table: `tool` `open_link`, `fast` true and `source` `"link"` for a
fast step (a model-decided `open_link` has no source, like any tool call); timings as usual: the wait until the
destination's window is up is the step's settle time, and the loading settle after it is the next step's (§2.1).
A fast link step is the task's first look's reason not to ask the classifier about the screen it leaves (the
classifier is asked if the step steps aside after all; docs/design/deep-links.md §6). The `variant` label gains `+links` when the fast path is on, so `openmob timings`
can group runs. A link's parameters (a search, a number) are **redacted in the record and never shown in the feed**,
exactly like typed text: the feed shows "open_link: Opened a YouTube search in YouTube."; the model's own
conversation holds the values (it needs them).

## 3. Safety

**Nothing in the catalog skips an ask that navigating by hand would have triggered.** Every link, built from the
catalog or by the model, passes the same checks as `open_url` today: `gate.url_refusal` (the scheme allowlist),
the handler resolved on the phone against the task's blocked apps (and the catalog entry's own app against them),
the permission mode (Ask every step asks before each link; Plan first needs the plan), approvals bound to the exact
URL, and the phone's gatekeeper judging the URL again (`judgeUrl`: scheme, blocked and installer packages,
`tel:` only to the dialer) on gatekeeper builds. A refusal on the phone reaches the model as an error with its
code, never retried.

The side-effect classes (also in docs/safety.md):

| Class | Means | On the fast path | When the model calls it |
|---|---|---|---|
| `view` | shows something: a search, a page, a chat, an inbox; opening it sends, buys and changes nothing | allowed | as `open_url` |
| `draft` | a compose screen with content filled in; nothing is sent until a tap, which the guard judges exactly as today (Send asks unless requested; payments always) | never | as `open_url` |
| `action` | would commit something on landing | never | asks the owner before opening, bound to the URL; refused under Don't ask. **No entry has this class**; adding one is an owner decision and would need a gatekeeper rule |

What a link can't do that a tap can't either: open OpenMob itself or a sensitive app (blocked at the handler, and
again on the phone), reach a security-critical Settings page (no catalog entry; `intent:` is refused), install
anything (the Play Store page opens; Install is a tap: free installs don't ask by the owner's decision, paid ones
ask as a purchase), place a call (`tel:` opens the dialer only), or send anything (`whatsapp.chat` carries no text
on purpose; drafts need the Send tap).

**User content.** Search terms, places, numbers and message bodies in a link are the user's words. They go to the
phone (in the URL) and to the model's conversation, and nowhere else: not the feed, not the steps table
(redacted), not status events (the fast path's "Link not taken" lines name the app, never the value), and not error
messages (a refused value isn't echoed).

**What a compromised server could do with this that it couldn't before:** nothing new. The server could already
send any `open_url`; the catalog only changes which URLs the agent builds on its own. The phone decides the
handler and judges the URL.

## 4. Switches

- `app_links` (default **on**; `OPENMOB_APP_LINKS=off`): the `open_link` tool and prompt line, and `open_url`'s
  landing report and browser note. Off restores today's `open_url` exactly, for comparisons.
- `app_links_fast` (default **on**; `OPENMOB_APP_LINKS_FAST=off`): the fast path. It starts on because it only
  ever opens `view` entries, is as checked as any link, is skipped whenever the phone says the link wouldn't open in
  its app, and the recovery from a wrong search term is one look by the model from the screen it would have taken
  anyway. The matcher is conservative on purpose (the app must be named; one entry; whole-request shapes), so it
  fires on fewer requests rather than wrongly. Turn it off to measure the model's own use of `open_link`.

## 5. Expected gains (estimates, not measured)

The step costs above, with the Settings deep links as the only measured analogue (34.1 → 3.4 s, 26.5 → 6.6 s,
11.1 → 6.5 s):

- **"Search YouTube for lofi"** (today: launch, wait, tap the search icon, type, Enter, confirm: 5–6 steps,
  ~12–18 s): fast path **~3–5 s** (observe ~0.3 s, no model, open, ~1–3 s settle while results load, one model
  look to confirm done ~1.5 s); first action on screen in **≤ 1 s** (regex) or **~1.5 s** (text model).
- **"Directions to the airport in Maps"** (today: launch, tap Directions, type, pick a suggestion, 4–6 steps,
  ~12–20 s): **~4–6 s**; Maps' route preview loads slowly, so the settle dominates.
- **"Open Spotify and search jazz"**, **"search the play store for chess"**, **"search amazon for …"**: like YouTube.
- **"Open a WhatsApp chat with +1 …"**: **~3–4 s** (WhatsApp shows a "Chat with +1 …?" confirmation for unknown
  numbers on some versions: one more tap, judged as always).
- **Model-decided `open_link`** on an ambiguous request: one decision (~1.5 s) plus the open, so **~4–6 s** to be
  inside the destination, against 3–5 steps by hand.
- **Repeat runs** don't change: the link is as fast the first time as the last, and replay doesn't apply (these
  requests type, so no path is stored).
- The gatekeeper adds its usual act-call cost (≤ 20 ms budget). Nothing changes for apps outside the catalog
  beyond `open_url`'s landing report.

Risks to the estimate: an app that hasn't verified its links on the phone (then the fast path steps aside and the
model takes the usual route, so no loss but no gain); OEM browsers that claim Google domains; Play Store `market://`
links on phones without Google Play (Huawei): the phone says "No app can open" and the model navigates.

## 6. Measurement plan

Eight tasks, first runs only (cold: the app not running, from the home screen), **off vs on** (`OPENMOB_APP_LINKS=off`
against the default; also `OPENMOB_APP_LINKS_FAST=off` with `app_links` on, to separate the catalog tool from the
fast path), on the Samsung (SM-S948U1) and an emulator (Pixel, API 35), five runs per cell:

1. "search YouTube for lofi" (YouTube);
2. "play lofi hip hop on youtube" (YouTube; the fast link plus the model's tap);
3. "directions to the airport in Maps" (Maps);
4. "find coffee shops near me on google maps" (Maps);
5. "search the play store for chess" (Play Store);
6. "open Spotify and search jazz" (Spotify, if installed; else "search amazon for usb-c cable");
7. "open bbc.com in chrome" (Chrome);
8. "open a whatsapp chat with <the owner's own number>" (WhatsApp, view only: nothing typed or sent).

Record per run from the steps table in `<data>/history.sqlite` (`tool`, `source`, `fast`, `observe_ms`, `model_ms`,
`settle_ms`, `total_ms`, `variant`): steps, total time, time to the first action, model calls, whether the first
action was the link, where it landed (the `outcome` text), success. Report median and p90 per cell, measured apart
from §5. Expect the Samsung to differ on which app claims Google links (Samsung Internet may be the default
browser) and on Maps' settle time. Jev on in every cell (the link step doesn't use it, but the steps after do).

Not done yet: no phone was driven for this work (owner rule), so nothing here is measured.

## 7. Left for later

- **More entries** once measured: YouTube Music, Google Photos search, Uber/Lyft ride requests (they are `action`
  class and need an owner decision), Telegram (`t.me/<user>`), Instagram profiles, Reddit, X, news apps, Netflix
  search, Google Translate, Google Keep.
- **Android app shortcuts** (`ShortcutManager`, static and dynamic shortcuts: "Compose" in Gmail, "Navigate home" in
  Maps): a phone method would be needed, like `open_settings`, with a fixed table judged by the gatekeeper.
- **Custom schemes** (`spotify:search:…`, `whatsapp://send`): only with a gatekeeper rule per entry; today both the
  server and the phone refuse them.
- **Learned links**: when the model builds an `open_url` that lands in an app and the task succeeds, keep the
  (request shape, URL template) as a catalog entry for that phone, the way replay keeps paths. Needs the same care
  as replay about user content, and a class judgement per learned entry.
- **Jev as the matcher**: a classifier head over the catalog would read more phrasings than the regexes, at ~0.15 s.
  Measure the regex hit rate first (the `source` column tells).
- **Non-English requests**: the shapes are English only; the text-model reading needs the app named in English too.
- **iOS**: universal links work the same way, but the catalog's packages are Android's; verification by bundle id
  would need the iOS driver to report it.
- **Cues** for entries where the package alone can't tell (a Maps route vs. its search), once a phone shows the
  package check isn't enough.

## 8. What changed, and what didn't

- Server: `openmob/agent/app_links.py` (new), `openmob/agent/loop.py` (`_open_link`, `_open_destination`,
  `_link_step`, `_link_landing`, the URL helpers; `open_url` through `_open_destination` when on),
  `openmob/agent/tools.py` (`OPEN_LINK_TOOL`, `APP_LINKS_LINE`), `openmob/config.py` (the switches),
  `tests/test_app_links.py`; docs/safety.md, config.example.yaml.
- **The phone app and the gatekeeper are unchanged**: `open_url` and `resolve_url` suffice, and the gatekeeper's
  `judgeUrl` already allows every scheme the catalog uses and refuses the rest. No `feature/app-links-gk` branch.
- Settle, replay and plan code are untouched; the Settings deep-links table is untouched.
