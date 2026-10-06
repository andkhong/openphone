"""Resolve a spoken app name ("where winds meet") to an installed package."""

from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rapidfuzz import fuzz

from .adb import PhoneDriver

DEFAULT_LABEL_CACHE = Path(os.environ.get("OPENMOB_DATA_DIR") or Path.home() / ".cache" / "openmob") / "labels.json"

# Package-name segments that say nothing about which app it is.
GENERIC_TOKENS = {
    "com", "org", "net", "io", "co", "android", "app", "apps", "mobile", "global",
    "gp", "google", "game", "games", "official", "release", "prod", "client",
    "main", "free", "lite", "www", "inc",
}

FUZZY_THRESHOLD = 85

LlmPicker = Callable[[str, list[str], dict[str, str]], "str | None"]


@dataclass
class Resolution:
    package: str
    method: str  # alias | package | label | fuzzy | acronym | llm
    score: float = 100.0


def fold(text: str) -> str:
    """Drop diacritics: 'Café' -> 'Cafe'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", fold(text).lower()).split())


def package_words(package: str) -> str:
    """'com.netease.where_winds_meet' -> 'netease where winds meet'."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", package)
    words = re.split(r"[._\s]+", spaced.lower())
    return " ".join(w for w in words if w and w not in GENERIC_TOKENS)


def _score(query: str, candidate: str) -> float:
    if not candidate:
        return 0.0
    return max(
        fuzz.token_sort_ratio(query, candidate),
        fuzz.ratio(query.replace(" ", ""), candidate.replace(" ", "")),
    )


def _subset_score(query: str, label: str) -> float:
    """'play store' fully inside 'google play store': strong match, weaker per extra word."""
    q, l = query.split(), label.split()
    if not q or not set(q) <= set(l):
        return 0.0
    return 95.0 - 5.0 * (len(l) - len(q))


class AppResolver:
    def __init__(
        self,
        driver: PhoneDriver,
        aliases: dict[str, str] | None = None,
        label_cache: Path = DEFAULT_LABEL_CACHE,
        llm_picker: LlmPicker | None = None,
    ):
        self.driver = driver
        self.aliases = {normalize(k): v for k, v in (aliases or {}).items()}
        self.label_cache = label_cache
        self.llm_picker = llm_picker
        self._packages: list[str] | None = None
        self.labels: dict[str, str | None] = self._load_labels()

        self._labels_lock = threading.Lock()

    def _load_labels(self) -> dict[str, str | None]:
        """Cached display names; None marks apps whose name couldn't be read."""
        try:
            return json.loads(self.label_cache.read_text())
        except (OSError, ValueError):
            return {}

    def missing_labels(self) -> list[str]:
        return [p for p in self.packages() if p not in self.labels]

    def ensure_labels(self, refresh: bool = False) -> int:
        """Read display names for apps not cached yet (all apps the first time). Returns how many were read."""
        with self._labels_lock:
            if refresh:
                self.packages(refresh=True)
            missing = self.missing_labels()
            if not missing:
                return 0
            self.labels.update(self.driver.app_labels(missing))
            self.label_cache.parent.mkdir(parents=True, exist_ok=True)
            self.label_cache.write_text(json.dumps(self.labels, indent=2, sort_keys=True))
            return len(missing)

    def display_name(self, package: str) -> str:
        return self.labels.get(package) or package_words(package) or package

    def installed_apps(self, query: str = "") -> list[tuple[str, str]]:
        """(display name, package) pairs, best matches first when a query is given."""
        apps = [(self.display_name(p), p) for p in self.packages()]
        if not query:
            return sorted(apps, key=lambda a: a[0].lower())
        q = normalize(query)
        return sorted(apps, key=lambda a: -max(_score(q, normalize(a[0])), _score(q, package_words(a[1]))))

    def packages(self, refresh: bool = False) -> list[str]:
        if self._packages is None or refresh:
            self._packages = self.driver.launchable_packages()
        return self._packages

    def resolve(self, name: str) -> Resolution | None:
        query = normalize(name)
        if not query:
            return None

        if query in self.aliases:
            return Resolution(self.aliases[query], "alias")
        for alias, package in self.aliases.items():
            score = _score(query, alias)
            if score >= 90:
                return Resolution(package, "alias", score)

        packages = self.packages()
        if name.strip() in packages:
            return Resolution(name.strip(), "package")

        if found := self._match_names(query, packages):
            return found
        if self.missing_labels():
            # Package names often look nothing like the app's name (com.netease.yysls is
            # Where Winds Meet), so read the real names from the phone and try again.
            self.ensure_labels()
            if found := self._match_names(query, packages):
                return found

        # "where winds meet" -> "wwm", a common package naming pattern.
        initials = "".join(word[0] for word in query.split())
        if len(initials) >= 2:
            hits = [p for p in packages if initials in re.split(r"[._]", p.lower())]
            if len(hits) == 1:
                return Resolution(hits[0], "acronym", 90)

        if self.llm_picker:
            picked = self.llm_picker(name, packages, self.labels)
            if picked in packages:
                return Resolution(picked, "llm", 80)
        return None

    def exact(self, name: str) -> str | None:
        """The one installed package whose alias or display name is `name`, compared case-, diacritic-
        and space-insensitively (with no fuzzy guessing), else None. Reads only names already cached,
        so it never waits on the phone beyond the package list."""
        query = normalize(name)
        if not query:
            return None
        if query in self.aliases:
            return self.aliases[query]
        squeezed = query.replace(" ", "")
        hits = {p for p in self.packages() if (label := normalize(self.labels.get(p) or ""))
                and (label == query or label.replace(" ", "") == squeezed)}
        return hits.pop() if len(hits) == 1 else None

    def _match_names(self, query: str, packages: list[str]) -> Resolution | None:
        scored: list[tuple[float, str]] = []
        for package in packages:
            label = normalize(self.labels.get(package) or "")
            if label == query:
                return Resolution(package, "label")
            score = max(_subset_score(query, label), _score(query, label), _score(query, package_words(package)))
            if score >= FUZZY_THRESHOLD:
                scored.append((score, package))
        if not scored:
            return None
        scored.sort(reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None  # "music" could be YouTube Music or Samsung Music; let something smarter decide
        return Resolution(scored[0][1], "fuzzy", scored[0][0])

def make_llm_picker(client, model: str) -> LlmPicker:
    """Ask a model which installed package matches a spoken app name. `client` is a ModelRouter
    (any provider) or an Anthropic-shaped client."""

    def pick(name: str, packages: list[str], labels: dict[str, str]) -> str | None:
        listing = "\n".join(f"{p} ({labels[p]})" if labels.get(p) else p for p in packages)
        prompt = (
            f"These Android packages are installed:\n{listing}\n\n"
            f'Which one is the app the user calls "{name}"? The name came from speech '
            "recognition, so allow for mis-hearings. Reply with only the exact package "
            "name, or NONE if none of them matches."
        )
        if hasattr(client, "complete_text"):
            text = client.complete_text(model, prompt, max_tokens=100)
        else:
            response = client.messages.create(model=model, max_tokens=100, messages=[{"role": "user", "content": prompt}])
            text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text if text in packages else None

    return pick


def make_jev_picker(judge, min_confidence: float = 0.6, max_candidates: int = 60) -> LlmPicker:
    """Let Jev choose the package. Only the closest fuzzy matches are offered, to keep its input small."""

    def pick(name: str, packages: list[str], labels: dict[str, str]) -> str | None:
        query = normalize(name)
        descriptions = {p: labels.get(p) or package_words(p) or p for p in packages}
        ranked = sorted(packages, key=lambda p: _score(query, normalize(descriptions[p])), reverse=True)
        candidates = {p: f"{descriptions[p]} ({p})" for p in ranked[:max_candidates]}
        package, probability, confidence = judge.pick_package(name, candidates)
        if package and probability >= 0.5 and confidence >= min_confidence:
            return package
        return None

    return pick


def chain_pickers(*pickers: LlmPicker | None) -> LlmPicker | None:
    """Try each picker in turn until one returns a package."""
    active = [p for p in pickers if p]
    if not active:
        return None

    def pick(name: str, packages: list[str], labels: dict[str, str]) -> str | None:
        for picker in active:
            try:
                package = picker(name, packages, labels)
            except Exception:  # a failing backend shouldn't block the next one
                continue
            if package:
                return package
        return None

    return pick
