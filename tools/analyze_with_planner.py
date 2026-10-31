"""Open WebUI tool client for the isolated v0.5b planner API only."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


PLANNER_ENDPOINT = "http://planner-service:8105/analyze"
PLANNER_TOKEN_FILE = "/run/secrets/planner_api_token"


class Tools:
    """No local filesystem, credentials, Docker, executor, or approval access."""

    def analyze_with_planner(self, user_request: str) -> str:
        if not isinstance(user_request, str) or not user_request.strip() or len(user_request) > 4000:
            return json.dumps({"valid": False, "error": "request is invalid or too large"})
        try:
            token = open(PLANNER_TOKEN_FILE, "r", encoding="utf-8").read().strip()
        except OSError:
            token = ""
        if not token or len(token) > 512:
            return json.dumps({"valid": False, "error": "planner API unavailable"})
        body = json.dumps({"request": user_request}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            PLANNER_ENDPOINT,
            data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=75) as response:
                return response.read(96 * 1024).decode("utf-8")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            return json.dumps({"valid": False, "error": "planner API unavailable"})
