"""Tool definitions the model uses to operate the phone.

All coordinates are pixels on the screenshot the model was shown.
"""

from ..phone.adb import KEYCODES

_POINT = {
    "x": {"type": "integer", "description": "X pixel on the screenshot"},
    "y": {"type": "integer", "description": "Y pixel on the screenshot"},
}
_TARGET = {
    "type": "string",
    "description": "Short description of what you are tapping, e.g. 'Menu icon top-right'",
}

TOOLS = [
    {
        "name": "launch_app",
        "description": "Open an installed app by its display name or package (e.g. 'Where Winds Meet', 'Settings', "
                       "'com.whatsapp'). With install_if_missing, an app that isn't installed (or was archived) opens "
                       "on its Play Store page instead, ready to tap Install.",
        "input_schema": {
            "type": "object",
            "properties": {"app": {"type": "string"},
                           "install_if_missing": {"type": "boolean",
                                                  "description": "Not installed: open its Play Store page."}},
            "required": ["app"],
        },
    },
    {
        "name": "list_apps",
        "description": "List installed apps (display name and package). Pass a query to rank the closest matches "
                       "first. Archived apps (removed but still listed by Android) are marked as not installed.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    },
    {
        "name": "open_url",
        "description": "Open a web link (https://...) or a tel:, mailto:, sms:, geo: or market:// link in the "
                       "default handler. Other schemes (custom app links, javascript:, intent:…) are refused.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "tap_element",
        "description": "Tap a UI element from the element list by its [index]. Most precise option when the target is listed.",
        "input_schema": {
            "type": "object",
            "properties": {"index": {"type": "integer"}, "target": _TARGET},
            "required": ["index", "target"],
        },
    },
    {
        "name": "tap",
        "description": "Tap a point on the screen, measured on the latest screenshot.",
        "input_schema": {
            "type": "object",
            "properties": {**_POINT, "target": _TARGET},
            "required": ["x", "y", "target"],
        },
    },
    {
        "name": "long_press",
        "description": "Press and hold a point on the screen.",
        "input_schema": {
            "type": "object",
            "properties": {
                **_POINT,
                "target": _TARGET,
                "duration_ms": {"type": "integer", "default": 800},
            },
            "required": ["x", "y", "target"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the screen. 'down' reveals content further down (finger moves up).",
        "input_schema": {
            "type": "object",
            "properties": {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}},
            "required": ["direction"],
        },
    },
    {
        "name": "swipe",
        "description": "Drag a finger from one point to another (for joysticks, sliders, custom gestures).",
        "input_schema": {
            "type": "object",
            "properties": {
                "x1": {"type": "integer"},
                "y1": {"type": "integer"},
                "x2": {"type": "integer"},
                "y2": {"type": "integer"},
                "duration_ms": {"type": "integer", "default": 300},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into the focused input field. Tap the field first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "submit": {"type": "boolean", "description": "Press Enter afterwards", "default": False},
            },
            "required": ["text"],
        },
    },
    {
        "name": "press_key",
        "description": "Press a hardware/system key.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string", "enum": sorted(KEYCODES)}},
            "required": ["key"],
        },
    },
    {
        "name": "wait_until",
        "description": (
            "Wait until something you expect happens on the phone, and return as soon as it does (with what "
            "was seen and how long it took), or at timeout_s with the screen as it is then. Prefer this to wait: "
            "it ends the moment the condition holds. until: screen_changes (anything meaningful changes); "
            "text_appears / text_disappears (target: text or a regex, matched against element text, "
            "descriptions and OCR, e.g. 'Open|Play'); element_enabled (target: a button's label, e.g. 'Open|Play': "
            "the button is there and no longer greyed out); app_in_front (target: a package or app name); "
            "loading_done (no spinner, progress bar or 'Loading' text, and the screen is still)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "until": {"type": "string", "enum": ["screen_changes", "text_appears", "text_disappears",
                                                     "element_enabled", "app_in_front", "loading_done"]},
                "target": {"type": "string", "description": "The text, regex, label or package the condition needs"},
                "timeout_s": {"type": "number", "minimum": 1, "maximum": 120, "default": 30},
            },
            "required": ["until"],
        },
    },
    {
        "name": "wait",
        "description": "Sleep a fixed time. Only when nothing observable will change (e.g. an animation you "
                       "must let finish); otherwise use wait_until.",
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "number", "minimum": 0.5, "maximum": 30}},
            "required": ["seconds"],
        },
    },
    {
        "name": "ask_user",
        "description": "Ask the user a question (spoken aloud) and wait for their answer. Use for confirmations and ambiguity.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "done",
        "description": "Finish the task. The summary is spoken to the user, so keep it to one short sentence.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}, "success": {"type": "boolean"}},
            "required": ["summary", "success"],
        },
    },
]

# The same tools in the provider-neutral shape the model adapters take: {name, description, parameters}.
TOOL_SPECS = [{"name": t["name"], "description": t["description"], "parameters": t["input_schema"]} for t in TOOLS]

SYSTEM_PROMPT = """\
You are OpenMob, an agent that operates the user's Android phone to carry out their spoken request. \
It can be any Android phone and any app the user has installed.

Each turn you see a screenshot of the phone and, when the app exposes one, a list of UI elements. \
Make exactly ONE tool call per turn, then check the next screenshot to confirm it worked before moving on.

How to act:
- To open an app, use launch_app. If it can't find the app, use list_apps to see what's installed and \
retry with the exact package. As a last resort, press HOME, open the app drawer, and use its search.
- For a web page (https:// link), open_url can be faster than navigating.
- Inside an app, look for a search field or tab bar before scrolling. Scroll to find things that are off-screen.
- If the keyboard covers what you need, press BACK once to hide it.
- If the target is in the element list, prefer tap_element. Otherwise use tap with pixel coordinates \
measured on the screenshot (origin top-left; the size is given with every screenshot).
- Games and some apps draw their own UI, so there is no element list. Rely on the screenshot. Menus are often \
an icon near a screen corner (☰, a gear, an avatar, a scroll/book icon) or a button labeled "Menu".
- To wait, use wait_until with a concrete condition rather than a fixed wait: it returns the moment the \
condition holds. After tapping Install in the Play Store: wait_until element_enabled "Open|Play" (timeout_s 120; \
the button is greyed out while it installs). After opening an app or a link, or on a loading screen: \
wait_until loading_done (or screen_changes). Waiting for a reply or a result: wait_until text_appears with \
the text you expect. Use wait (fixed seconds) only when nothing observable will change. In games and on \
boards (chess, cards) wait_until screen_changes for the opponent's move rather than guessing at text that may never \
appear; a text wait on a still screen ends early, not met.
- If a request needs an app that isn't installed, launch_app with install_if_missing opens its Play Store page.
- Android's permission dialogs (notifications, location, camera…) can't be tapped: Android ignores OpenMob's \
taps on their buttons. They are dismissed for you with BACK before you see the screen, so the app doesn't get that \
permission. If the request needs a permission granted, carry on without it and say in your summary that the owner \
must tap Allow on the phone themselves.
- After launching a game, wait through loading screens, and close announcements, pop-ups, or "tap to start" \
screens that are in the way.
- If an action didn't change the screen as expected, try something different. Don't repeat the same action \
more than twice.
- The request came from speech recognition, so interpret mis-heard names sensibly.
- A screenshot that is entirely black usually means the app blocks screen capture (banking, DRM video). \
Say so with done(success=false) rather than tapping blindly.
- A message that starts with "User correction:" is the user changing course while you work. Follow it \
from your next action.
- Earlier requests from this session may be listed. Use them to understand follow-ups like "now scroll down".
- When the request is fully done, call done with a one-sentence spoken summary. If it can't be done, call \
done with success=false and say why.

Safety:
- Before anything that spends money, sends a message/post/email, deletes data, or changes account or \
security settings, call ask_user to confirm, unless the user explicitly asked for exactly that action.
- Never type passwords, payment details, or verification codes. Ask the user to do that themselves.
- If the request is ambiguous, use ask_user.
"""


IOS_NOTES = """
This phone is an iPhone (iOS), so some Android habits don't apply:
- There is no BACK key. Go back with the on-screen Back button (usually top left, often labelled with the \
previous screen's name) or by swiping right from the left edge of the screen.
- If launch_app can't find an app, press HOME, swipe down on the middle of the home screen to open Spotlight \
search, type the app's name, and tap the result.
- To hide the keyboard, tap "Done" or "Return", or tap outside the text field.
- APP_SWITCH and the Android-only keys aren't available.
"""


def system_prompt(platform: str = "android") -> str:
    """The agent's instructions, with notes for the phone's platform."""
    if platform == "ios":
        return (
            SYSTEM_PROMPT.replace("the user's Android phone", "the user's iPhone")
            .replace("It can be any Android phone and any app", "It can be any app")
            + IOS_NOTES
        )
    return SYSTEM_PROMPT


# Plan first (permission mode "plan"): offered only in that mode, so other tasks' tool lists don't change.
PLAN_TOOL = {
    "name": "propose_plan",
    "description": (
        "Show the owner a short numbered plan for this request and wait for their one approval. Required "
        "before any other action in this task. Name each risky step's action in its words (\"Send the message "
        "to Mom in WhatsApp\") and the app it happens in."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array", "minItems": 1, "maxItems": 12,
                "items": {"type": "object", "properties": {
                    "text": {"type": "string", "description": "One step, in plain words"},
                    "app": {"type": "string", "description": "The app it happens in, if any"},
                }, "required": ["text"]},
            },
        },
        "required": ["steps"],
    },
}
PLAN_TOOL_SPEC = {"name": PLAN_TOOL["name"], "description": PLAN_TOOL["description"],
                  "parameters": PLAN_TOOL["input_schema"]}

MODE_NOTES = {
    "ask": "\nPermission mode: Ask every step. The owner approves each change on their phone, so don't also "
           "call ask_user to confirm; ask only when the request is ambiguous.\n",
    "plan": "\nPermission mode: Plan first. Before anything else, call propose_plan with a short numbered plan. "
            "After the owner approves it, follow it; a risky step the plan doesn't name, or any payment, still "
            "asks. If the owner asks for changes, propose a revised plan.\n",
    "auto": "",  # today's behavior: the base prompt's safety rules
    "trusted": "",
    "never": "\nPermission mode: Don't ask. The owner chose not to be asked: don't call ask_user to confirm. "
             "Payments, autofill and blocked apps are refused; if one is needed, call done and say so.\n",
}


RISK_NOTE = ("\nLow-risk actions (sharing a link with yourself, copying, liking, subscribing, saving, menus) run "
             "without asking; the phone decides what needs the owner's confirmation. Don't ask the owner to confirm "
             "them yourself.\n")


def mode_notes(mode: str) -> str:
    return MODE_NOTES.get(mode, "") + (RISK_NOTE if mode in ("auto", "trusted", "plan") else "")
