"""Cloudflare Workers AI decision API. Experimental; no phone policy lives here."""
from __future__ import annotations

import base64
import io
import math
import re
import time

import httpx
from PIL import Image

from .jev import JevError, JevJudge


def encode_image(data: bytes) -> dict:
    """Validate the actual bytes and emit Cloudflare's embedded-image representation."""
    if len(data) > 4 * 1024 * 1024:
        raise JevError("Clef image exceeds 4 MiB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            content_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image.format]
            if image.width * image.height > 16_000_000:
                raise JevError("Clef image exceeds 16 megapixels")
            image.verify()
    except (OSError, KeyError, ValueError) as exc:
        raise JevError("Clef requires a valid PNG, JPEG, or WebP image") from exc
    return {"content_type": content_type, "base64": base64.b64encode(data).decode("ascii")}


class ClefClient:
    def __init__(self, api_key: str, account_id: str, model: str = "clef-flash", *,
                 timeout: float = 10.0, max_retries: int = 2, http: httpx.Client | None = None):
        if model not in {"clef", "clef-flash"}:
            raise ValueError("Clef model must be clef or clef-flash")
        if not api_key or not re.fullmatch(r"[a-fA-F0-9]{32}", account_id):
            raise ValueError("Clef needs CLOUDFLARE_API_TOKEN and a 32-hex-character CLOUDFLARE_ACCOUNT_ID")
        self.model = model
        self.max_retries = max_retries
        self.on_usage = None
        self.request_count = 0
        self.error_count = 0
        self.served_model = None
        self.input_tokens = 0
        self.path = f"/client/v4/accounts/{account_id}/ai/run/@cf/cloudflare/{model}"
        self._http = http or httpx.Client(base_url="https://api.cloudflare.com", timeout=timeout,
                                         headers={"Authorization": f"Bearer {api_key}"})

    def close(self):
        self._http.close()

    def ask(self, state, questions: dict, *, images: list[dict] | None = None) -> dict:
        try:
            return self._ask(state, questions, images=images)
        except JevError:
            self.error_count += 1
            raise

    def _ask(self, state, questions: dict, *, images: list[dict] | None = None) -> dict:
        if not questions:
            raise JevError("Clef needs at least one question")
        if images and (len(images) > 4 or sum(len(i["base64"]) * 3 // 4 for i in images) > 8 * 1024 * 1024):
            raise JevError("Clef image batch exceeds its limits")
        answers, remote = {}, {}
        for key, question in questions.items():
            # The agent sometimes has exactly one possible target. Cloudflare requires >=2.
            # Resolve that bounded selection locally; operation confidence still gates execution.
            criteria = question.get("criteria", {})
            if question["type"] == "choice" and len(criteria) == 1:
                choice = next(iter(criteria))
                answers[key] = {"type": "choice", "choice": choice,
                                "probabilities": {choice: 1.0}, "confidence": 1.0}
            else:
                remote[key] = question
        items = list(remote.items())
        for start in range(0, len(items), 64):
            chunk = dict(items[start:start + 64])
            body = {"model": self.model, "state": state, "questions": chunk}
            if images:
                body["images"] = images
            result = self._request(body)
            self._validate(result, chunk)
            answers.update(result)
        return answers

    def _request(self, body):
        for attempt in range(self.max_retries + 1):
            try:
                self.request_count += 1
                response = self._http.post(self.path, json=body)
            except httpx.HTTPError:
                # Never echo provider errors, requests, state, or credentials into logs.
                if attempt == self.max_retries:
                    raise JevError("Clef request failed (transport error)") from None
            else:
                if response.status_code in {429, 500, 502, 503, 504, 529} and attempt < self.max_retries:
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                if response.status_code != 200:
                    raise JevError(f"Clef returned HTTP {response.status_code}")
                try:
                    data = response.json()
                    if data.get("success") is False:
                        raise ValueError("unsuccessful envelope")
                    # REST wraps results; Workers binding / test servers may return them directly.
                    result = data.get("result", data)
                    answers = result["answers"]
                    if not isinstance(answers, dict):
                        raise ValueError("invalid answers")
                    tokens = int(result["usage"]["input_tokens"])
                    if tokens < 0:
                        raise ValueError("invalid usage")
                except (KeyError, TypeError, ValueError, AttributeError):
                    raise JevError("Clef returned a malformed response") from None
                self.served_model = result.get("model", self.model)
                self.input_tokens += tokens
                if self.on_usage:
                    self.on_usage(tokens)
                return answers
            time.sleep(0.5 * 2 ** attempt)
        raise JevError("Clef request failed")

    @staticmethod
    def _validate(answers, questions):
        def probability(value):
            return isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
        try:
            for key, question in questions.items():
                answer = answers[key]
                if question["type"] == "noul":
                    valid = probability(answer["noul"])
                else:
                    probs = answer["probabilities"]
                    expected = (set(question["criteria"]) if question["type"] == "choice"
                                else {str(i) for i in range(len(question["criteria"]))})
                    valid = (set(probs) == expected and all(probability(p) for p in probs.values())
                             and abs(sum(probs.values()) - 1) <= 0.02 and probability(answer["confidence"]))
                    if question["type"] == "choice":
                        valid = valid and answer["choice"] in expected
                    else:
                        valid = valid and math.isfinite(answer["score"]) and 0 <= answer["score"] <= len(expected) - 1
                if not valid:
                    raise ValueError("invalid answer")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise JevError("Clef returned invalid or incomplete decision probabilities") from None


class ClefJudge(JevJudge):
    @staticmethod
    def _assessment(answers):
        assessment = JevJudge._assessment(answers)
        assessment.classifier = "Clef"
        return assessment

    @staticmethod
    def _screen_questions():
        questions = JevJudge._screen_questions()
        questions["state"]["instructions"] += " Use the screen image too when supplied."
        return questions
