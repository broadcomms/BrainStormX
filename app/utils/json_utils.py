# app/utils/json_utils.py
import json
import re
from flask import current_app


def _find_balanced_json(text: str, start_char: str, end_char: str) -> str:
    start = text.find(start_char)
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    candidate = text[start : idx + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        break
        start = text.find(start_char, start + 1)
    return ""


def extract_json_block(text: str) -> str:
    """
    Extracts the first complete JSON object or array from a string,
    handling optional markdown code fences (```json ... ```).
    Returns an empty string if no valid JSON block is found.
    """
    if not text:
        return ""

    fence_pattern = r"```json\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```"
    fence_match = re.search(fence_pattern, text, re.IGNORECASE | re.DOTALL)

    if fence_match:
        potential_json = fence_match.group(1).strip()
        try:
            json.loads(potential_json)
            current_app.logger.debug("[extract_json_block] Extracted JSON from fenced block.")
            return potential_json
        except json.JSONDecodeError:
            current_app.logger.warning("[extract_json_block] Found fenced block, but content is invalid JSON. Falling back.")

    stripped = text.lstrip()

    if stripped.startswith("["):
        candidate = _find_balanced_json(text, "[", "]")
        if candidate:
            current_app.logger.debug("[extract_json_block] Extracted JSON array from freeform text.")
            return candidate

    candidate = _find_balanced_json(text, "{", "}")
    if candidate:
        current_app.logger.debug("[extract_json_block] Extracted JSON object from freeform text.")
        return candidate

    if not stripped.startswith("["):
        candidate = _find_balanced_json(text, "[", "]")
        if candidate:
            current_app.logger.debug("[extract_json_block] Extracted JSON array from freeform text.")
            return candidate

    current_app.logger.warning("[extract_json_block] No valid JSON object or array found in the text.")
    return ""
