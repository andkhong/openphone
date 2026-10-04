"""OpenMob phone protocol v1: JSON messages over a WebSocket.

The phone (OpenMob Android app, or `openmob bridge`) connects OUT to the server at
    wss://<server>/phone/ws?t=<token>
so it works from any network. The server then sends commands; the phone answers.

Phone -> server
    {"type": "hello", "protocol": 1, "device": {"id", "model", "manufacturer", "android"}}   first message
    {"type": "result", "id": "<call id>", "ok": true, "result": <value>}
    {"type": "result", "id": "<call id>", "ok": false, "error": "<message>"}
    {"type": "command", "text": "open youtube"}        user spoke/typed a request on the phone
Server -> phone
    {"type": "welcome", "session": "<device id>"}       reply to hello
    {"type": "call", "id": "<call id>", "method": "<name>", "params": {...}}
    {"type": "event", "kind": "<kind>", "message": "<text>"}   agent activity to show or speak;
        kind is one of: heard, status, thought, action, jev, question, result, error

Close codes: 4401 bad or missing token (don't retry), 4400 malformed hello or wrong protocol.
Coordinates are device pixels in the current orientation (origin top-left).
See docs/phone-protocol.md for the full spec, and METHODS below for every call.
"""

from __future__ import annotations

import base64

from .adb import Screenshot, UIElement

PROTOCOL_VERSION = 1

# method -> (parameters, result, timeout in seconds)
METHODS: dict[str, tuple[dict[str, str], str, float]] = {
    "screenshot": ({}, "Screenshot object (see encode_screenshot)", 30),
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
    "wake_unlock": ({}, "null (wake the screen; dismiss a non-secure keyguard)", 10),
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
    return Screenshot(
        jpeg=base64.b64decode(data["jpeg"]),
        width=int(data["width"]),
        height=int(data["height"]),
        device_width=int(data["device_width"]),
        device_height=int(data["device_height"]),
    )


def encode_element(el: UIElement) -> dict:
    return {
        "text": el.text,
        "desc": el.desc,
        "resource_id": el.resource_id,
        "cls": el.cls,
        "clickable": el.clickable,
        "checked": el.checked,
        "bounds": list(el.bounds),
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
    )
