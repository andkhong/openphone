# The agent indicator: a glowing border and a Stop pill (2026-10-17)

While an agent acts on the phone, OpenMob's Android app draws a thin glowing border around the whole screen and a
small **■ Stop** pill in a corner. Code: `IndicatorModel.kt` (settings, colours, the show/hide policy; plain Kotlin,
tested in `ControlIndicatorTest`), `IndicatorOverlay.kt` (the two windows), `ControlIndicator.kt` (the glue).

## The windows

Both are `TYPE_ACCESSIBILITY_OVERLAY` windows added by the accessibility service, so no new permission is needed.

- **The border:** full screen, `FLAG_NOT_TOUCHABLE | FLAG_NOT_FOCUSABLE | FLAG_LAYOUT_IN_SCREEN | FLAG_LAYOUT_NO_LIMITS`,
  laid out over cutouts (`LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS`, `fitInsetsTypes = 0`). It follows the display's
  rounded corners (API 31+), is 5 dp wide, fades in over 150 ms and out over 300 ms.
- **The pill:** 48 dp tall, at least 96 dp wide, `FLAG_NOT_FOCUSABLE`. It sits in the owner's chosen corner, 8 dp
  inside the status bar (top corners) or the navigation bar (bottom corners), 12 dp from the side. It's the only
  touchable part. One tap does what the notification's Stop does (`ConnectionService.stop`: the socket closes with
  1000, so the server stops the task and clears its queue; every permit, prompt and purchase flow is revoked), after
  first withdrawing every open phone prompt (`GateHost.withdrawPrompts`, which sends `gate_event` `expired`). During a
  preview from settings, a tap only ends the preview.

If the accessibility service dies, Android removes both windows with it. That's accepted: without the service, the
phone can't act either.

## States (pattern as well as colour)

| State | When | Looks like |
|---|---|---|
| working | an agent is acting | a steady line with a soft inner glow; an optional slow pulse (off when "Remove animations" is on) |
| approval | the phone's own prompt is open (`Gatekeeper.promptPending`) | an amber band twice as thick, with the owner's colour as a thin line inside it |
| paused | a session is active but waits on the owner: locked mid-task, a hidden system dialog (`ownerDialog`), or paused after denials | dashed, at 55% opacity |

Every colour gets a hairline along its inner edge when it's faint on white or black content (green, white, light
custom colours), so each preset has at least 3:1 contrast against both (tested). The orange preset is a red-orange
(`#FF5722`), kept away from the approval amber (`#FFB300`). A custom colour close to amber gets a black line between
the bands.

## Show and hide (the phone decides)

Only the phone's own knowledge turns it on. A server flag alone can't turn it on or off.

- **Shown:** by every action the gatekeeper admits (`ConnectionService.handleCall`, any `act` method, `wake_unlock`
  included), *before* the action's side effect. `ControlIndicator.beforeAct` blocks the call's thread until the
  windows have been added and the next frame has drawn, for at most 500 ms. It's also shown by the phone's own
  approval prompt, which can come before any action (a task prompt).
- **Kept up:** for 10 s after the last admitted action. Any other call from the server (screenshots, `wait_for`,
  `wait_idle`) keeps an already active session up for 10 s after that call, but no more than 5 minutes after the last
  action. A paused session stays up, dashed, for up to 5 minutes after its last action.
- **Hidden early:** when the server says the task ended (an `event` of kind `result` or `error`, or the controller line
  cleared), the border stays only until 1.5 s after the last action, then fades. If a server says "ended" and then
  acts, that action shows the indicator again before its gesture runs. So a compromised server can't act while the
  indicator is hidden; at most it can end the indicator 1.5 s after an action instead of 10 s.
- **Hidden at once:** by Stop (the pill, the notification, Disconnect), and when the connection shuts down.
- Reads alone never show it.

OpenMob's own overlay windows appearing or going are not "changes on screen". The accessibility service ignores
their `TYPE_WINDOWS_CHANGED` events, so the gate's settle wait and `wait_idle` aren't delayed by the border.

## The gatekeeper and the overlays

- **Hit test** (`GateScreen.hitTest`, mirrored in `tests/gatekeeper_ref.py`): OpenMob's own accessibility overlays
  with no clickable node (the border, the keep-awake pixel) never catch a touch. Taps, long presses and swipes pass
  through them and are judged on the app below. An overlay with a clickable node (the pill) catches the touch. A tap,
  long press or either end of a swipe on it is refused with `gate_denied_page` / `builtin:own_indicator`, never judged
  on the app under it. A blocked app under the point still refuses first (`builtin:sensitive_apps`).
- **What the gate sees of them** (`GateScreens.capture` → `GateScreen.ownOverlayView`): one textless node over the
  window, clickable only if the overlay is. The pill's "Stop" is never counted as screen words (payment context,
  typing judgements).
- **Reads** (`Gatekeeper.hidden`, `ocrWindow`): only `application` windows count as unknown windows or permission
  dialog candidates, so the overlays never black out a screenshot or the live view, and never start OCR.
- **OCR** (`PermissionOcr.read`): lines whose centre is inside the pill are dropped. The pill sits in a corner, away
  from a permission dialog's buttons.
- **ui_elements** skips accessibility overlays (as before), so the agent is never offered the pill.

Screenshots (`takeScreenshot`, adb and MediaProjection) include the border and the pill. The agent's system prompt
(`openmob/agent/tools.py`, `INDICATOR_LINE`; Android only) says they're OpenMob's indicator: ignore them, and never
tap Stop.

## Settings (OpenMob's own screen only)

On the main screen, in the **Agent indicator** card, stored in the app-private `openmob_indicator` preferences:

- border on/off (default on);
- border colour: blue (default), purple, green, orange, pink or white (high contrast), or a custom `#RRGGBB`;
- gentle pulse (default on);
- Stop button on/off (default on);
- the Stop button's corner (default top right);
- a 4-second preview.

No server, API or protocol method reads or writes these settings. Agents can't change them either, because the gate
refuses every tap on OpenMob's own windows (`own_packages`, `builtin:own_window`; fixture
`openmob_indicator_settings`).

## The pill dodges (owner decision, 2026-10-17)

The default top-right spot is where many toolbars put their overflow (⋮) or account button. When the gate refuses an
agent's tap, long press or swipe with `builtin:own_indicator` (a point is on the pill), `GateHost.admit` does this:

1. It asks the gate whether the gesture would be acceptable, allowed or asked, without the pill
   (`Gatekeeper.admitsWithoutPill`; mirrored as `admits_without_pill` in the reference engine). This has no side
   effects. If the answer is no (a bank under the pill, say), the refusal stands and the pill doesn't move.
2. It moves the pill (`ControlIndicator.dodge`, `PillPlacement`). The order is the other corner of the same edge, then
   the other edge on the same side, then the diagonal; the first corner whose rect, grown by 8 dp, contains none of the
   gesture's points wins. The window is moved in place (`updateViewLayout`), so it never disappears, and the dodge waits
   two frames, at most 600 ms. If no corner is free, it doesn't move and the gesture stays refused.
3. It waits up to 800 ms for the accessibility window list to show the moved window, then runs the real admit again on
   the new screen. That judges the gesture on the app element now uncovered, with the usual asks and refusals.

The pill keeps its new corner until the indicator goes (the session ends). The next session starts in the owner's
corner, which stays the default in settings. An agent gesture aimed at the moved pill is still refused by the gate.
In practice the pill dodges again, because the corner it left is now free, so an agent can never press Stop. The pill
only ever moves between on-screen corners (`IndicatorGeometry.pillRect` clamps to the screen), and only for a gesture
the gate would accept. A compromised server can make it hop between corners but can't hide it.

## Known limits

- Every dodge adds a little latency to that one gesture: a second capture, two frames, and the window list catching up
  (measured on the emulator in the report, not on a phone).
- A gesture that would cover every corner (impossible with ≤ 2 points on a phone screen) stays refused.
- With the Stop button and the border both off, nothing is shown, which is the owner's choice. The notification stays.
- A task that only reads (no admitted action) shows no indicator.
