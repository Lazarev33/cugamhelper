"""Strict, lossless conversion of 'number-DD.MM.YYYY' lines to numbers."""

from datetime import datetime
import re

LINE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d{2}\.\d{2}\.\d{4})\s*$")
PLAIN_RE = re.compile(r"^\s*(\d+)\s*$")


def clean_numbers(text: str) -> tuple[list[str], list[int]]:
    """Return cleaned identifiers and invalid line numbers.

    Preserves sequence, duplicates, and leading zeroes. If any nonblank
    line is invalid, callers must not publish the partial result.
    """
    output: list[str] = []
    invalid: list[int] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        match = LINE_RE.fullmatch(raw)
        if match:
            identifier, date_text = match.groups()
            try:
                datetime.strptime(date_text, "%d.%m.%Y")
            except ValueError:
                invalid.append(line_no)
                continue
            output.append(identifier)
            continue
        match = PLAIN_RE.fullmatch(raw)
        if match:
            output.append(match.group(1))
        else:
            invalid.append(line_no)
    return output, invalid


def chunks(lines: list[str], limit: int = 3800) -> list[str]:
    """Split on line boundaries to respect Telegram's 4096-character limit."""
    result: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        additional = len(line) + (1 if current else 0)
        if current and current_length + additional > limit:
            result.append("\n".join(current))
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + (1 if current_length else 0)
    if current:
        result.append("\n".join(current))
    return result
