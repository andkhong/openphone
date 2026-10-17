# Owner decisions

The product and security decisions made by the owner (andkhong), newest first. Each entry says what was decided and where it lives. Agents: don't reopen a decision without new evidence, and add new ones here.

## Open
- **Installs named only by server-sent task text (review 2026-10-17, L2).** The fix on `fix/review-2026-10-17` trusts an app name for a free install only when the phone holds the task text: typed on the phone, or a task grant approved there. Dashboard and API install tasks then get one prompt. This partly reverses 2026-10-09's "a free install the request names doesn't ask". The recommendation is to keep the fix. **Awaiting the owner.**
- **Financial apps as sensitive, and trading words as money** (Robinhood, Schwab, Fidelity, Credit Karma, ...; "sell", "trade", "withdraw"). Deferred on 2026-10-10. The work is ready on branch `parked/financial-apps`.
- **A phone-side widened sensitive check before Faster mode sends frames** (account and routing numbers, money context, financial domains), and J5 (an image "is it sensitive" signal). Deferred on 2026-10-17.
- **The two-app split** (a Play companion plus a sideloaded service): not for the MVP. Build it only if Play confirms it accepts the companion (`docs/design/two-app-split.md`, 2026-10-10).
- Older, still open: "My people" phone memory; rules.json word edits; hiding the app list in developer logs.

## 2026-10-17
- **Faster mode** (Jev images; `docs/design/faster-mode.md`):
  - **Who can turn it on:** off by default. Only the owner on the phone turns it on, with the screen lock. Turning it off is one tap anywhere (phone, notification, dashboard). The API and dashboard can only turn it off.
  - **Scope:** "This session only" lasts until the owner disconnects or the session is idle for 1 hour. A reconnect within 5 minutes keeps it on, if the phone re-confirms its signed grant. "Always on this phone" is the other option.
  - **On a server without OCR, nothing is sent,** and the dashboard says it's inactive.
  - **Drive sessions:** a key's drive sessions use it only after the owner approves on the phone with the screen lock. The dashboard asks and the phone approves; the developer console can only turn it off.
  - **The warning includes:** "OpenMob blocks banking apps and detected card and ID numbers, but it can't recognize every sensitive image."
- **Jev images:** build step 1 (screen state and loading detection on screens without text) behind Faster mode. Pattern B (text model decides, Jev locates) isn't built: it was measured slower.
- **The agent indicator:**
  - a border while an agent acts, with colour presets and a custom colour;
  - a Stop pill;
  - settings to turn each on or off, changeable only in OpenMob's own UI on the phone.
- **The Stop pill moves out of the agent's way** (option 1), never off-screen, and is never tappable by the agent.
- **The Stop pill stops only the current task or drive session and stays connected.** The full Stop/Disconnect still disconnects.

## 2026-10-10
- **No approvals during testing:** the owner doesn't approve prompts in test runs. Tests count an ask as a pass and cancel.
- **Shipped first:** the review fixes, the financial safeguards and release signing (steps 2, 3 and 5). Financial apps as sensitive (step 1) and a separate review (step 4) were skipped.
- **Release signing:**
  - the owner's own key, created with `om keygen`; never seen by agents;
  - a `release` build (https only) for the hosted download;
  - a `local` build for the owner's phone and server;
  - `debug` stays for emulators and tests (`docs/release-signing.md`).
- **Play Store:** the automation app stays off public Play. The policy was verified that day (`docs/design/two-app-split.md` §1).
- **Prod deploys:** the owner runs `fly deploy` from their own terminal. Agents can't deploy (the permission system blocks it).

## 2026-10-09
- **Free Play Store installs:** an install the request names runs unasked; any other install asks, and is refused under Don't ask. See Open above for the L2 change.
- **The MCP drive level:**
  - capped at `trusted` (never Don't ask);
  - screenshots are opt-in with `screen:read`;
  - screen text comes with `devices:drive`;
  - one-time codes are never typed by drive sessions;
  - limits: 120 s idle, 30 minutes, the task step cap, 1 credit per step.
- **Calls** stay excluded from "Don't confirm everyday actions". **MMI and USSD codes** ask once, at the dialing (a `tel:` link only opens the dialer).
- **Production v13** was deployed before the security review, because the owner is the only user.
- **The two builds** (standard and gatekeeper) merged into `main` on 2026-10-10 (PR #26).

## 2026-10-08
- **Clef dropped; Jev kept.** The latency targets were adopted (`docs/benchmarks.md`).
- **Model effort:** Haiku 5.5 effort is low on labeled screens and high on canvas (`canvas_effort`).
- **Replay** is on in the owner's config. **Plan-execute** is off: measured slower. `max_actions_per_turn` and `parallel_observe` are off: no gain.
- **Two builds kept** (no gatekeeper flag), later merged.
- **Signup is invite-only.**

## 2026-10-06 and earlier
- **Permission dialogs** are dismissed with Back only: no owner prompt, never `isAccessibilityTool`.
- **Free installs don't ask.** Superseded on 2026-10-09.
- **Security reviews are batched,** not run per fix.
