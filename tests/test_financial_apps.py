"""Financial apps are sensitive on the server and on the phone (security tests 2026-10-10: Robinhood, Schwab,
Fidelity and Credit Karma were openable by agents), and trading words ask as money."""

import json
import re
from pathlib import Path

from openmob.config import Config
from openmob.permissions import DEFAULT_MONEY_KEYWORDS

RULES = Path(__file__).resolve().parents[1] / "android/app/src/main/resources/gatekeeper/rules.json"
FINANCIAL = ["com.robinhood.android", "com.schwab.mobile", "com.fidelity.android", "com.creditkarma.mobile",
             "com.sofi.mobile", "com.discoverfinancial.mobile", "com.citi.citimobile", "com.chase.sig.android",
             "com.venmo", "com.coinbase.pro", "com.google.android.apps.walletnfcrel", "com.callpod.android_apps.keeper"]
ORDINARY = ["com.discovery.dplay", "com.spotify.music", "com.google.android.youtube", "com.google.android.apps.maps",
            "com.android.chrome", "com.duolingo", "org.wikipedia", "com.chipotle.ordering", "com.linkedin.android"]


def matcher(patterns):
    compiled = [re.compile(p, re.I) for p in patterns]
    return lambda package: any(p.search(package) for p in compiled)


def test_financial_apps_are_sensitive_on_the_server_and_the_phone_alike():
    server = matcher(Config().sensitive_apps)
    phone = matcher(json.loads(RULES.read_text())["sensitive_apps"])
    for package in FINANCIAL:
        assert server(package) and phone(package), package
    for package in ORDINARY:
        assert not server(package) and not phone(package), package


def test_trading_words_ask_as_money_on_both_sides():
    phone_money = json.loads(RULES.read_text())["words"]["flow_start"]  # the phone's money words
    for word in ("sell", "trade", "withdraw"):
        assert word in DEFAULT_MONEY_KEYWORDS and word in phone_money, word
