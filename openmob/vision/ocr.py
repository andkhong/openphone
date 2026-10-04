"""On-device OCR with Apple's Vision framework (macOS only).

Turns a screenshot into text boxes. Games have no accessibility tree, so this is how
the agent (and Jev, which only reads text) learns what words are on screen.
Needs the `mac` extra: pip install -e '.[mac]'
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass
class TextBox:
    text: str
    confidence: float
    # Pixel box on the screenshot image, origin top-left.
    x1: int
    y1: int
    x2: int
    y2: int


def available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Vision  # noqa: F401
        from Foundation import NSData  # noqa: F401
    except ImportError:
        return False
    return True


def recognize_text(image_bytes: bytes, width: int, height: int, accurate: bool = True) -> list[TextBox]:
    """OCR an encoded image (JPEG/PNG). Returns boxes sorted top-to-bottom, left-to-right."""
    import Vision
    from Foundation import NSData

    data = NSData.dataWithBytes_length_(image_bytes, len(image_bytes))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(
        Vision.VNRequestTextRecognitionLevelAccurate if accurate else Vision.VNRequestTextRecognitionLevelFast
    )
    request.setUsesLanguageCorrection_(False)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision OCR failed: {error}")

    boxes = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        box = observation.boundingBox()  # normalized, origin bottom-left
        x1 = box.origin.x * width
        x2 = (box.origin.x + box.size.width) * width
        y1 = (1 - box.origin.y - box.size.height) * height
        y2 = (1 - box.origin.y) * height
        boxes.append(TextBox(str(candidate.string()), float(candidate.confidence()), round(x1), round(y1), round(x2), round(y2)))
    boxes.sort(key=lambda b: (b.y1 // 20, b.x1))
    return boxes
