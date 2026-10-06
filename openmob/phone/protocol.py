"""OpenMob phone protocol v1: JSON messages over a WebSocket.

The phone (OpenMob Android app, or `openmob bridge`) connects OUT to the server at
    wss://<server>/phone/ws?t=<token>
so it works from any network. The server then sends commands; the phone answers.

Phone -> server
    {"type": "hello", "protocol": 1, "device": {"id", "model", "manufacturer", "android",
                                                "capabilities"?: ["wait_idle"]}}   first message
    {"type": "result", "id": "<call id>", "ok": true, "result": <value>}
    {"type": "result", "id": "<call id>", "ok": false, "error": "<message>"}
    binary frame: a screenshot result asked for with "binary": true (see encode_binary_result)
    binary frame: an H.264 packet of the live-view video stream (see encode_video_packet)
    {"type": "video_state", "available": false, "reason": "..."}   screen streaming ended (or resumed)
    {"type": "command", "text": "open youtube"}        user spoke/typed a request on the phone
Server -> phone
    {"type": "welcome", "session": "<device id>"}       reply to hello
    {"type": "call", "id": "<call id>", "method": "<name>", "params": {...}}
    {"type": "event", "kind": "<kind>", "message": "<text>"}   agent activity to show or speak;
        kind is one of: heard, status, thought, action, jev, question, result, error

Gatekeeper phones (hello device.capabilities has "gatekeeper"; docs/phone-protocol.md, "Gatekeeper"):
    the phone checks every call first and may refuse it:
    {"type": "result", "id", "ok": false, "code": "<GATE_CODES>", "error": "<why>",
     "gate": {"rule", "package", "ms"}}            report it; never retry the same call
    {"type": "result", "id", "ok": false, "code": "gate_needs_approval", "approval_id", "gate": {"question", "kind"}}
        the phone asked its owner itself; on gate_event approved, re-send the identical call once
    Phone -> server: {"type": "gate_event", "approval_id", "state": "pending|approved|denied|expired"}
                     {"type": "device_lock", "locked": bool, "secure": bool}   a secure lock mid-task / unlocked
    Server -> phone, act calls only: params "mode" (the task's mode: the phone takes the tighter of it and its
    owner's signed ceiling), "task" (the task id), "hint" (the server's own caution: can only add a prompt).
    Gatekeeper phones ignore approval_request for actions (they word their own prompts).

Close codes: 4401 bad or missing token (don't retry), 4400 malformed hello or wrong protocol.
Coordinates are device pixels in the current orientation (origin top-left).
See docs/phone-protocol.md for the full spec, and METHODS below for every call.
"""

from __future__ import annotations

import base64
import json
import struct

from .adb import Screenshot, UIElement

PROTOCOL_VERSION = 1

# Stable refusal codes from a gatekeeper phone (docs/phone-protocol.md, "Gatekeeper").
GATE_CODES = (
    "gate_no_session", "gate_blocked_app", "gate_denied_page", "gate_rate_limited", "gate_needs_approval",
    "gate_denied", "gate_screen_changed", "gate_screen_unstable", "gate_prompt_open", "gate_password_field",
    "gate_unknown_method", "gate_policy_invalid", "phone_locked",
)

# method -> (parameters, result, timeout in seconds)
METHODS: dict[str, tuple[dict[str, str], str, float]] = {
    # All screenshot params are optional and only the live view sends them; an app that ignores
    # them answers with its normal full-size JSON frame, which the server accepts too.
    "screenshot": (
        {"max_side": "int, optional", "quality": "int 1-100, optional", "binary": "bool, optional"},
        "Screenshot object (see encode_screenshot), or a binary frame if binary was true",
        30,
    ),
    "ui_elements": ({}, "list of UIElement objects (see encode_element); [] if unavailable", 15),
    "current_app": ({}, "foreground package name or null", 10),
    "launchable_packages": ({}, "list of package names with a launcher activity", 30),
    "app_labels": ({"packages": "list[str]"}, "object: package -> display name or null", 300),
    "launch_app": ({"package": "str"}, "null", 15),
    "tap": ({"x": "int", "y": "int"}, "null", 10),
    "long_press": ({"x": "int", "y": "int", "duration_ms": "int"}, "null", 15),
    "swipe": ({"x1": "int", "y1": "int", "x2": "int", "y2": "int", "duration_ms": "int"}, "null", 15),
    "type_text": ({"text": "str"}, "null", 15),
    "key": ({"name": "HOME|BACK|ENTER|APP_SWITCH|DELETE|... (see adb.KEYCODES)"}, "null", 10),
    "open_url": ({"url": "str"}, "null", 15),
    # Newer apps (hello device.capabilities has "resolve_url"): which app would open this URL, before
    # opening it, so the server can refuse a link into a blocked app. package is null for a chooser.
    "resolve_url": ({"url": "str"}, "{package: str or null, candidates: list[str]}", 10),
    "wake_unlock": ({}, "null (wake the screen; dismiss a non-secure keyguard)", 10),
    # Newer apps: is the phone usable right now? Older apps answer "Unknown method" (the check is skipped).
    "device_state": ({}, "{interactive, locked, secure: bool}: screen on, keyguard showing, PIN-type lock", 5),
    # Newer apps (hello device.capabilities has "wait_idle"): return once no window or content change
    # has arrived for quiet_ms, or at max_ms. The agent's adaptive settle; others get a fixed sleep.
    "wait_idle": ({"quiet_ms": "int", "max_ms": "int"}, "{waited_ms: int, idle: bool}", 10),
    # Live-view video, only for phones whose hello says "video": true (the owner allowed screen
    # streaming). The agent never uses these; it keeps taking screenshots.
    "stream_start": (
        {"max_side": "int", "bitrate": "int, bits per second", "fps": "int"},
        "object: {width, height} of the encoded video; H.264 packets follow as binary frames",
        15,
    ),
    "stream_stop": ({}, "null", 10),
    "stream_keyframe": ({}, "null (the next packet will be a keyframe)", 10),
}


def encode_screenshot(shot: Screenshot) -> dict:
    return {
        "jpeg": base64.b64encode(shot.jpeg).decode(),
        "width": shot.width,
        "height": shot.height,
        "device_width": shot.device_width,
        "device_height": shot.device_height,
    }


def decode_screenshot(data: dict) -> Screenshot:
    jpeg = data["jpeg"]
    return Screenshot(
        jpeg=jpeg if isinstance(jpeg, bytes) else base64.b64decode(jpeg),
        width=int(data["width"]),
        height=int(data["height"]),
        device_width=int(data["device_width"]),
        device_height=int(data["device_height"]),
    )


def encode_binary_result(call_id: str, shot: Screenshot) -> bytes:
    """A screenshot result without base64: a 4-byte big-endian header length, the UTF-8 JSON
    "result" message (its result lacks "jpeg"), then the raw JPEG bytes."""
    result = {
        "width": shot.width, "height": shot.height,
        "device_width": shot.device_width, "device_height": shot.device_height,
    }
    header = json.dumps({"type": "result", "id": call_id, "ok": True, "result": result}).encode()
    return struct.pack(">I", len(header)) + header + shot.jpeg


def encode_video_packet(data: bytes, pts_us: int, key: bool = False, config: bool = False) -> bytes:
    """One H.264 packet (Annex B) of the live-view stream, framed like a binary result: a 4-byte
    big-endian header length, the JSON header {"type": "video", "key", "config", "pts"}, the bytes.
    "config" packets carry the SPS/PPS; "key" packets are keyframes."""
    header = json.dumps({"type": "video", "key": key, "config": config, "pts": pts_us}).encode()
    return struct.pack(">I", len(header)) + header + data


def decode_message(text: str | None, data: bytes | None) -> dict:
    """One phone->server message from a text or binary WebSocket frame. Raises ValueError if malformed."""
    if text is not None:
        return json.loads(text)
    if not data or len(data) < 4:
        raise ValueError("Empty binary message")
    (size,) = struct.unpack(">I", data[:4])
    message = json.loads(data[4 : 4 + size])
    if message.get("type") == "video":
        message["data"] = data[4 + size :]
        return message
    if not isinstance(message.get("result"), dict):
        raise ValueError("Binary message without a result object")
    message["result"]["jpeg"] = data[4 + size :]
    return message


def encode_element(el: UIElement) -> dict:
    return {
        "text": el.text,
        "desc": el.desc,
        "resource_id": el.resource_id,
        "cls": el.cls,
        "clickable": el.clickable,
        "checked": el.checked,
        "bounds": list(el.bounds),
        # Optional flags, only when true (older servers ignore them).
        **{flag: True for flag in ("editable", "password", "covered", "focused") if getattr(el, flag)},
        **({"hint": el.hint} if el.hint else {}),
    }


def decode_element(index: int, data: dict) -> UIElement:
    x1, y1, x2, y2 = (int(v) for v in data["bounds"])
    return UIElement(
        index=index,
        text=data.get("text") or "",
        desc=data.get("desc") or "",
        resource_id=data.get("resource_id") or "",
        cls=data.get("cls") or "",
        clickable=bool(data.get("clickable")),
        checked=data.get("checked"),
        bounds=(x1, y1, x2, y2),
        editable=bool(data.get("editable")),
        password=bool(data.get("password")),
        covered=bool(data.get("covered")),
        focused=bool(data.get("focused")),
        hint=str(data.get("hint") or ""),
    )
