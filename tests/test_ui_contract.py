"""Regression boundary for the petrol redesign: UI changes cannot rewrite execution.

The fixture was recorded from 066c67b (current main before integration) before any presentation changes. Update it
deliberately when a future task intentionally changes a backend/UI contract.
"""
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "ui_contract.json"
TOKEN = re.compile(r'''//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|[^\s]''')


def tokens(source):
    return [t for t in TOKEN.findall(source) if not t.startswith(("//", "/*"))]


def balanced_end(items, start, opening="(", closing=")"):
    level = 0
    for index in range(start, len(items)):
        level += items[index] == opening
        level -= items[index] == closing
        if level == 0:
            return index + 1
    raise AssertionError("Unbalanced QML/JavaScript expression")


def backend_calls(source):
    items = tokens(source)
    found = []
    for index in range(len(items) - 3):
        if items[index:index + 2] == ["backend", "."] and items[index + 3] == "(":
            end = balanced_end(items, index + 3)
            found.append(" ".join(items[index:end]))
    return found


def functions(source):
    items = tokens(source)
    result = {}
    for index in range(len(items) - 3):
        if items[index] == "function" and items[index + 2] == "(":
            body = balanced_end(items, index + 2)
            if body < len(items) and items[body] == "{":
                end = balanced_end(items, body, "{", "}")
                result[items[index + 1]] = " ".join(items[index:end])
    return result


def test_execution_modules_unchanged_by_redesign():
    baseline = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for path, expected in baseline["protected_sha256"].items():
        # Git checkouts on Windows may convert LF to CRLF without changing code.
        content = (ROOT / path).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(content).hexdigest() == expected, path


def test_original_backend_calls_and_controller_functions_preserved():
    baseline = json.loads(FIXTURE.read_text(encoding="utf-8"))
    source = (ROOT / "waraq/qml/Main.qml").read_text(encoding="utf-8")
    calls = Counter(backend_calls(source))
    assert not (Counter(baseline["backend_calls"]) - calls)
    current = functions(source)
    for name, body in baseline["controller_functions"].items():
        assert current[name] == body, name


def test_live_models_and_automation_names_survive_redesign():
    baseline = json.loads(FIXTURE.read_text(encoding="utf-8"))
    source = (ROOT / "waraq/qml/Main.qml").read_text(encoding="utf-8")
    for name in baseline["object_names"]:
        assert f'objectName: "{name}"' in source, name
    for name in ("jobsModel", "workersModel", "sourceModel", "resultsModel", "filesModel", "providersModel"):
        assert f"model: backend.{name}" in source, name


def test_text_contrast_for_both_palettes():
    source = (ROOT / "waraq/qml/Theme.qml").read_text(encoding="utf-8")
    pairs = {name: (dark, light) for name, dark, light in re.findall(r'color (\w+): dark \? "(#[\da-fA-F]+)" : "(#[\da-fA-F]+)"', source)}
    def luminance(value):
        channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * w for c, w in zip(channels, (0.2126, 0.7152, 0.0722)))
    for mode in (0, 1):
        for foreground, background in (("ink", "panel"), ("muted", "panel"), ("ink", "inset"), ("muted", "inset"), ("accentInk", "accent"), ("goldInk", "gold"), ("success", "panel"), ("warning", "panel"), ("danger", "panel"), ("info", "panel")):
            a, b = sorted((luminance(pairs[foreground][mode]), luminance(pairs[background][mode])))
            assert (b + 0.05) / (a + 0.05) >= 4.5, (mode, foreground, background)
