"""Settings pages the agent may open directly (docs/design/deep-links.md).

The phone's `open_settings` method takes a page *key* from this fixed table and maps it to an Android
Settings intent action itself: the server never sends an action, component or extra, so a compromised
server can reach only these pages. The Android table (`SettingsPages.kt`, in both apps) must hold the
same keys, actions, tiers and package rules; a test compares the two files.

Tiers follow docs/design/phone-gatekeeper.md §2.3 (D2): `everyday` pages open without asking, `ask`
pages ask the owner first (and are refused under Don't ask). Security-critical pages (accessibility,
language, security, developer options, special app access…) have no key at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .apps import normalize

EVERYDAY = "everyday"
ASK = "ask"

# An Android application id: dotted identifiers, each starting with a letter.
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
OWN_PACKAGE = "dev.openmob.companion"


@dataclass(frozen=True)
class SettingsPage:
    key: str
    action: str  # the intent action the phone starts (android.provider.Settings.ACTION_* unless noted)
    title: str  # the page's name, for prompts and messages
    tier: str = EVERYDAY
    needs_package: bool = False  # an app's own page: `package` is required and judged (blocked apps refused)
    aliases: tuple[str, ...] = ()  # how a request names the page ("Wi-Fi settings"), for the fast path


_PAGES = (
    SettingsPage("display", "android.settings.DISPLAY_SETTINGS", "Display",
                 aliases=("display", "display settings", "screen settings", "brightness")),
    SettingsPage("wifi", "android.settings.WIFI_SETTINGS", "Wi-Fi",
                 aliases=("wifi", "wi-fi", "wi fi", "wifi settings", "wi-fi settings", "wireless")),
    SettingsPage("bluetooth", "android.settings.BLUETOOTH_SETTINGS", "Bluetooth",
                 aliases=("bluetooth", "bluetooth settings")),
    SettingsPage("sound", "android.settings.SOUND_SETTINGS", "Sound",
                 aliases=("sound", "sound settings", "sounds", "volume", "sound & vibration", "sound and vibration")),
    # Intent.ACTION_POWER_USAGE_SUMMARY: the Battery page on every Android.
    SettingsPage("battery", "android.intent.action.POWER_USAGE_SUMMARY", "Battery",
                 aliases=("battery", "battery settings", "battery usage", "power")),
    SettingsPage("location", "android.settings.LOCATION_SOURCE_SETTINGS", "Location",
                 aliases=("location", "location settings", "location services")),
    # Not a public constant (the action is handled by AOSP Settings; OEMs may lack it: the phone then errors
    # and the agent navigates instead).
    SettingsPage("notifications", "android.settings.NOTIFICATION_SETTINGS", "Notifications",
                 aliases=("notifications", "notification settings", "app notifications")),
    SettingsPage("apps", "android.settings.MANAGE_APPLICATIONS_SETTINGS", "Apps",
                 aliases=("apps", "all apps", "app list", "apps settings", "applications", "installed apps")),
    SettingsPage("storage", "android.settings.INTERNAL_STORAGE_SETTINGS", "Storage",
                 aliases=("storage", "storage settings")),
    SettingsPage("airplane_mode", "android.settings.AIRPLANE_MODE_SETTINGS", "Airplane mode",
                 aliases=("airplane mode", "airplane mode settings", "flight mode", "aeroplane mode")),
    # Manual date and time asks (phone-gatekeeper.md, changes since review 2, N1).
    SettingsPage("date_time", "android.settings.DATE_SETTINGS", "Date & time", tier=ASK,
                 aliases=("date & time", "date and time", "date time", "time settings", "date settings")),
    # The app's own pages: the package is required, validated on the phone, and judged like the app itself.
    SettingsPage("app_details", "android.settings.APPLICATION_DETAILS_SETTINGS", "App info", needs_package=True),
    SettingsPage("app_notifications", "android.settings.APP_NOTIFICATION_SETTINGS", "App notifications",
                 needs_package=True),
)
PAGES: dict[str, SettingsPage] = {p.key: p for p in _PAGES}
PAGE_KEYS: list[str] = list(PAGES)


def page_for(text: str | None) -> SettingsPage | None:
    """The page a request names exactly ("Display", "Wi-Fi settings", "the battery page"), compared like app
    names (case, diacritics and spacing aside; a trailing "settings"/"page"/"screen" dropped), else None.
    App pages (`app_details`, `app_notifications`) are never matched: they need a package the model picks."""
    query = re.sub(r"^(?:the|my|a|an)\s+", "", normalize(text or "")).strip()
    if not query:
        return None
    stripped = re.sub(r"\s+(?:settings|setting|page|screen|section|menu|options)$", "", query).strip()
    for page in PAGES.values():
        for alias in page.aliases:
            a = normalize(alias)
            if query in (a, a.replace(" ", "")) or stripped in (a, a.replace(" ", "")) \
                    or query.replace(" ", "") == a.replace(" ", ""):
                return page
    return None


def valid_package(package: str | None) -> bool:
    return bool(package) and bool(PACKAGE_RE.match(package)) and len(package) <= 255
