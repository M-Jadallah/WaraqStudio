"""Audited execution and UI-action boundary for the petrol application.

The fixture was recorded from 066c67b (current main before integration) before any presentation changes. Update it
deliberately when a future task intentionally changes a backend/UI contract.
The approved idle fix records six explicit exceptions and their previous hashes;
all other protected modules, QML controller functions and actions remain guarded.
The requested-stage display change separately records the previous Bridge hash;
only its job-step projection changes, with execution and UI actions preserved.
The approved reviewed-source/worker-choice change records its former Bridge
hash, four controller functions, replaced source calls and one retired button;
all other execution modules, actions and controller functions stay guarded.
"""
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "ui_contract.json"
TOKEN = re.compile(r'''//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|[^\s]''')

# Keep every existing dark color and every non-rail light color stable.
APPROVED_PALETTES = {
    "stageDownloadCompleted": ("#2AA8A6", "#0B7478"),
    "stageTranscriptCompleted": ("#5C9DCF", "#1B62A0"),
    "stageWordCompleted": ("#BA8C44", "#815A1F"),
    "stageCompletedInk": ("#06272B", "#FFFFFF"),
    "bg": ("#0C242A", "#F2F7F7"),
    "panel": ("#14363E", "#FFFFFF"),
    "inset": ("#0F2E35", "#E5F0F1"),
    "rail": ("#082026", "#E5F0F1"),
    "ink": ("#E8F3EF", "#183C43"),
    "muted": ("#A7C3C8", "#4D6E75"),
    "accent": ("#4BC6C3", "#0B7478"),
    "accentInk": ("#06272B", "#FFFFFF"),
    "gold": ("#D8AB64", "#815A1F"),
    "goldInk": ("#06272B", "#FFFFFF"),
    "line": ("#39646C", "#9FB8BD"),
    "hover": ("#1C4650", "#DCECED"),
    "success": ("#6BD6AD", "#126B4C"),
    "warning": ("#D8AB64", "#815A1F"),
    "danger": ("#FF9D9D", "#B42332"),
    "info": ("#8FCBFF", "#1B62A0"),
    "railInk": ("#E8F3EF", "#183C43"),
    "railMuted": ("#A7C3C8", "#4D6E75"),
    "railActive": ("#4BC6C3", "#0B7478"),
    "railHover": ("#14363E", "#DCECED"),
    "railSelectedInk": ("#06272B", "#FFFFFF"),
}
NAV_ICONS = ("home", "plus", "workers", "library", "folder", "settings")
ORIGINAL_NAV_ICON_SHA256 = {
    "home.svg": "2c1c874ce8491f598903c1d074c7d87f27515eb5deb8893695da528830dfdd5a",
    "home-active.svg": "8b5915ff870bd756894050c803e8fd0f39acec0c5f7efd9683d8cac7be158900",
    "plus.svg": "a51eb30db80596bc4d7c390cd5b86c4be8a1ba4e47c5f45092c62e2342aacc3b",
    "plus-active.svg": "8501f590189ec61ff68dde3b20681fdea3eaac6d54f46b6d95a371482ec1e954",
    "workers.svg": "c5745be8e8e19206f24d49694eacdd2a84aaf81f3682dcecc6621084863a22d0",
    "workers-active.svg": "53038e1f371acdb744b004d4208ad94b6f5002e12cfe4b147e8ebaff4bc44d1b",
    "library.svg": "33f5d57edd43f0fca1783331f6f5be60c30aa0c93ef24a442cf344f4e6830967",
    "library-active.svg": "6ab609eb457f23306c7512782249c6579ee908806c06ae3708ace57b4e55ccfc",
    "folder.svg": "6d9400e4158af856f0f4f5eee6c80211303a9fe9f7f62c140cbaa59af5dade09",
    "folder-active.svg": "a35bb977eb6537692b645f3e8d0c0f1d4cad806c77c9ab04affb83aff3ad76fe",
    "settings.svg": "1e2f20b15b217bbf8e2dd76aefe0414bf05223a65c54d6e9c6de5471caabdc48",
    "settings-active.svg": "f9c77e07a499d3fb9b5162effb821891779d3a30841306dc8a6f54ff0e41ad26",
}


def theme_palettes():
    source = (ROOT / "waraq/qml/Theme.qml").read_text(encoding="utf-8")
    return {name: (dark.upper(), light.upper()) for name, dark, light in re.findall(
        r'color (\w+): dark \? "(#[\da-fA-F]{6})" : "(#[\da-fA-F]{6})"', source)}


def contrast_ratio(first, second):
    def luminance(value):
        channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * w for c, w in zip(channels, (0.2126, 0.7152, 0.0722)))

    a, b = sorted((luminance(first), luminance(second)))
    return (b + 0.05) / (a + 0.05)


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
        if name == "sourceModel":
            assert "model: win.sourceDraft.itemsModel" in source
        else:
            assert f"model: backend.{name}" in source, name


def test_text_contrast_for_both_palettes():
    pairs = theme_palettes()
    for mode in (0, 1):
        for foreground, background in (("ink", "panel"), ("muted", "panel"), ("ink", "inset"), ("muted", "inset"), ("accentInk", "accent"), ("goldInk", "gold"), ("success", "panel"), ("warning", "panel"), ("danger", "panel"), ("info", "panel")):
            assert contrast_ratio(pairs[foreground][mode], pairs[background][mode]) >= 4.5, (mode, foreground, background)


def test_approved_palettes_preserve_dark_and_non_rail_colors():
    assert theme_palettes() == APPROVED_PALETTES


def test_rail_text_contrast_in_both_palettes_and_states():
    pairs = theme_palettes()
    for mode in (0, 1):
        # Labels, footer text and branding stay readable on the rail and hover.
        for foreground in ("railInk", "railMuted", "railActive"):
            for background in ("rail", "railHover"):
                assert contrast_ratio(pairs[foreground][mode], pairs[background][mode]) >= 4.5, (mode, foreground, background)
        assert contrast_ratio(pairs["railSelectedInk"][mode], pairs["railActive"][mode]) >= 4.5, mode


def test_original_navigation_icons_are_unchanged():
    for filename, expected in ORIGINAL_NAV_ICON_SHA256.items():
        content = (ROOT / "waraq/qml/icons" / filename).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(content).hexdigest() == expected, filename


def svg_geometry(element):
    """Retain paths, viewBox, sizes, fill and stroke widths; omit only color."""
    return (
        element.tag,
        {name: value for name, value in element.attrib.items() if name != "stroke"},
        (element.text or "").strip(),
        tuple(svg_geometry(child) for child in element),
    )


def test_navigation_svg_strokes_have_contrast_and_preserve_geometry():
    pairs = theme_palettes()
    icons = ROOT / "waraq/qml/icons"
    for name in NAV_ICONS:
        for mode in (0, 1):
            for selected in (False, True):
                original = icons / (name + ("-active" if selected else "") + ".svg")
                suffix = ("-rail-selected-light" if selected else "-rail-light") if mode == 1 else ("-active" if selected else "")
                filename = icons / (name + suffix + ".svg")
                svg = ET.parse(filename).getroot()
                assert svg_geometry(svg) == svg_geometry(ET.parse(original).getroot()), filename.name
                expected = pairs["railSelectedInk" if selected else "railInk"][mode]
                # Read actual asset colors: a correct QML text token cannot mask
                # an SVG which still carries the old hard-coded pale stroke.
                strokes = [element.attrib["stroke"].upper() for element in svg.iter() if "stroke" in element.attrib]
                assert strokes and all(stroke == expected for stroke in strokes), filename.name
                backgrounds = ("railActive",) if selected else ("rail", "railHover")
                for stroke in strokes:
                    for background in backgrounds:
                        assert contrast_ratio(stroke, pairs[background][mode]) >= 3, (filename.name, background)
