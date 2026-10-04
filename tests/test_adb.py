from pathlib import Path

from conftest import make_png

from openmob.phone.adb import PhoneDriver, Screenshot, format_elements, parse_ui_xml

FIXTURE = Path(__file__).parent / "fixtures" / "settings_ui.xml"


def test_parse_ui_xml_keeps_labelled_and_clickable_nodes():
    elements = parse_ui_xml("UI hierchary dumped\n" + FIXTURE.read_text())
    assert [e.label for e in elements] == ["Settings", "Search settings", "Dark theme"]
    search = elements[1]
    assert search.clickable and search.resource_id == "search"
    assert search.center == (966, 266)
    assert elements[2].checked is False


def test_parse_ui_xml_handles_garbage():
    assert parse_ui_xml("ERROR: could not get idle state.") == []
    assert parse_ui_xml("") == []


def test_format_elements_uses_screenshot_coordinates():
    shot = Screenshot(b"", 540, 1200, 1080, 2400)
    text = format_elements(parse_ui_xml(FIXTURE.read_text()), shot)
    assert "[1] ImageButton desc='Search settings' id=search @ (483,133)" in text
    assert "unchecked" in text
    assert "(not clickable)" in text.splitlines()[0]


def test_screenshot_coordinate_mapping_round_trips_and_clamps():
    shot = Screenshot(b"", 576, 1280, 1080, 2400)
    assert shot.to_device(288, 640) == (540, 1200)
    assert shot.to_device(-5, 99999) == (0, 2399)
    assert shot.to_image(540, 1200) == (288, 640)


class RecordingDriver(PhoneDriver):
    def __init__(self, outputs=None, **kw):
        super().__init__(**kw)
        self.calls = []
        self.outputs = outputs or {}

    def _exec(self, args, timeout=30, device=True):
        self.calls.append(args)
        for key, value in self.outputs.items():
            if key in " ".join(args):
                return value
        return b""


def test_screenshot_is_downscaled_to_max_side():
    driver = RecordingDriver({"screencap": make_png(1080, 2400)}, max_side=1280)
    shot = driver.screenshot()
    assert (shot.width, shot.height) == (576, 1280)
    assert (shot.device_width, shot.device_height) == (1080, 2400)
    assert shot.jpeg[:2] == b"\xff\xd8"


def test_landscape_screenshot_keeps_orientation():
    driver = RecordingDriver({"screencap": make_png(2400, 1080)}, max_side=1280)
    shot = driver.screenshot()
    assert (shot.width, shot.height) == (1280, 576)


def test_type_text_escapes_spaces_and_shell_characters():
    driver = RecordingDriver()
    driver.type_text("it's a test")
    assert driver.calls[-1] == ["shell", "input text 'it'\"'\"'s%sa%stest'"]


def test_current_app_parses_dumpsys():
    out = b"  mCurrentFocus=Window{1a2b u0 com.netease.wwm/com.netease.MainActivity}\n"
    driver = RecordingDriver({"dumpsys": out})
    assert driver.current_app() == "com.netease.wwm"


def test_launchable_packages_from_query_activities():
    out = b"com.android.settings/.Settings\ncom.netease.wwm/.Main\ncom.netease.wwm/.Other\n"
    driver = RecordingDriver({"query-activities": out})
    assert driver.launchable_packages() == ["com.android.settings", "com.netease.wwm"]
