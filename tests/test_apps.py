from conftest import FakeDriver

from openmob.phone.apps import AppResolver, chain_pickers, make_jev_picker, package_words

PACKAGES = [
    "com.android.settings",
    "com.google.android.youtube",
    "com.google.android.apps.maps",
    "com.google.android.apps.meetings",
    "com.netease.wwm",
    "com.spotify.music",
]


def resolver(tmp_path, **kw):
    return AppResolver(FakeDriver(packages=PACKAGES), label_cache=tmp_path / "labels.json", **kw)


def test_package_words_drops_generic_parts():
    assert package_words("com.google.android.apps.maps") == "maps"
    assert package_words("com.example.whereWindsMeet") == "example where winds meet"


def test_alias_wins(tmp_path):
    r = resolver(tmp_path, aliases={"Where Winds Meet": "com.netease.wwm"})
    assert r.resolve("where winds meet").package == "com.netease.wwm"
    assert r.resolve("Where wind meet").method == "alias"  # mis-heard


def test_fuzzy_on_package_words(tmp_path):
    r = resolver(tmp_path)
    assert r.resolve("YouTube").package == "com.google.android.youtube"
    assert r.resolve("settings").package == "com.android.settings"
    assert r.resolve("Spotify Music").package == "com.spotify.music"


def test_acronym_match(tmp_path):
    found = resolver(tmp_path).resolve("Where Winds Meet")
    assert (found.package, found.method) == ("com.netease.wwm", "acronym")


def test_label_cache_exact_match(tmp_path):
    (tmp_path / "labels.json").write_text('{"com.google.android.apps.meetings": "Google Meet"}')
    assert resolver(tmp_path).resolve("google meet").method == "label"


def test_partial_words_do_not_false_match(tmp_path):
    # "meet" alone must not pull "where winds meet" toward Google Meet.
    found = resolver(tmp_path).resolve("where winds meet")
    assert found.package != "com.google.android.apps.meetings"


def test_llm_picker_fallback(tmp_path):
    r = resolver(tmp_path, llm_picker=lambda name, pkgs, labels: "com.google.android.apps.maps")
    found = r.resolve("navigation thing")
    assert (found.package, found.method) == ("com.google.android.apps.maps", "llm")


def test_unknown_app_returns_none(tmp_path):
    assert resolver(tmp_path, llm_picker=lambda *a: None).resolve("banana phone") is None


class FakeJudge:
    def __init__(self, answer):
        self.answer = answer
        self.offered = None

    def pick_package(self, name, candidates):
        self.offered = candidates
        return self.answer


def test_jev_picker_requires_confidence():
    labels = {}
    assert make_jev_picker(FakeJudge(("com.spotify.music", 0.9, 0.8)))("music", PACKAGES, labels) == "com.spotify.music"
    assert make_jev_picker(FakeJudge(("com.spotify.music", 0.9, 0.2)))("music", PACKAGES, labels) is None
    assert make_jev_picker(FakeJudge((None, 0.9, 0.9)))("music", PACKAGES, labels) is None


def test_jev_picker_offers_closest_candidates_first():
    judge = FakeJudge((None, 0, 0))
    make_jev_picker(judge, max_candidates=2)("spotify", PACKAGES, {})
    assert "com.spotify.music" in judge.offered and len(judge.offered) == 2


def test_chain_pickers_skips_failures():
    def broken(*a):
        raise RuntimeError("down")

    chained = chain_pickers(broken, None, lambda *a: "com.spotify.music")
    assert chained("x", PACKAGES, {}) == "com.spotify.music"
    assert chain_pickers(None) is None


def test_real_app_names_are_read_once_when_package_names_dont_match(tmp_path):
    driver = FakeDriver(packages=PACKAGES + ["com.netease.yysls"], labels={"com.netease.yysls": "Where Winds Meet"})
    r = AppResolver(driver, label_cache=tmp_path / "labels.json")
    found = r.resolve("where winds meet")
    assert (found.package, found.method) == ("com.netease.yysls", "label")
    assert r.resolve("Where wind meet").package == "com.netease.yysls"
    assert driver.label_reads == 1
    # Cached on disk, including apps whose name couldn't be read (stored as null).
    again = AppResolver(FakeDriver(packages=PACKAGES + ["com.netease.yysls"]), label_cache=tmp_path / "labels.json")
    assert again.missing_labels() == [] and again.labels["com.spotify.music"] is None


def test_installed_apps_lists_display_names(tmp_path):
    driver = FakeDriver(packages=PACKAGES, labels={"com.spotify.music": "Spotify"})
    r = AppResolver(driver, label_cache=tmp_path / "labels.json")
    r.ensure_labels()
    assert r.installed_apps("spotfy")[0] == ("Spotify", "com.spotify.music")
    assert ("maps", "com.google.android.apps.maps") in r.installed_apps()


def test_spoken_name_inside_longer_label(tmp_path):
    labels = {"com.android.vending": "Google Play Store", "com.google.android.apps.youtube.music": "YouTube Music",
              "com.sec.android.app.music": "Samsung Music"}
    driver = FakeDriver(packages=list(labels), labels=labels)
    r = AppResolver(driver, label_cache=tmp_path / "labels.json", llm_picker=lambda *a: None)
    assert r.resolve("play store").package == "com.android.vending"
    assert r.resolve("youtube music").package == "com.google.android.apps.youtube.music"
    assert r.resolve("music") is None  # ambiguous


def test_a_makers_name_in_front_doesnt_send_google_maps_to_gmail(tmp_path):
    # Measured on an emulator: "Google Maps" launched Gmail ("matched by acronym": G M) while Maps was installed.
    from conftest import FakeDriver

    labels = {"com.google.android.apps.maps": "Maps", "com.google.android.gm": "Gmail",
              "com.google.android.googlequicksearchbox": "Google", "com.android.vending": "Google Play Store"}
    driver = FakeDriver(packages=list(labels), labels=labels)
    resolver = AppResolver(driver, label_cache=tmp_path / "l.json")
    resolver.ensure_labels()
    assert resolver.resolve("Google Maps").package == "com.google.android.apps.maps"
    assert resolver.resolve("gmail").package == "com.google.android.gm"
    assert resolver.resolve("google").package == "com.google.android.googlequicksearchbox"
    assert resolver.resolve("play store").package == "com.android.vending"
