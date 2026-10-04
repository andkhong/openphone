import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from openmob.vision import ocr

pytestmark = pytest.mark.skipif(not ocr.available(), reason="Apple Vision OCR not available")


def test_recognizes_text_with_top_left_boxes():
    img = Image.new("RGB", (600, 400), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=48)
    draw.text((40, 40), "MENU", fill="black", font=font)
    draw.text((300, 300), "START", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG")

    boxes = ocr.recognize_text(buf.getvalue(), 600, 400)
    by_text = {b.text.upper(): b for b in boxes}
    assert {"MENU", "START"} <= set(by_text)
    menu, start = by_text["MENU"], by_text["START"]
    assert menu.y1 < 150 and menu.x1 < 150  # top-left, not flipped
    assert start.y1 > 250 and start.x1 > 250
    assert boxes[0].text.upper() == "MENU"  # sorted top to bottom
