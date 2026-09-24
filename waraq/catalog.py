"""Model choices for the worker editor, fetched without blocking Qt's UI thread."""

import os
import re
import subprocess
from pathlib import Path

import requests

from .services import resolve_opencode


def deepgram_models():
    """Public, batch-capable speech-to-text models and their supported languages."""
    response = requests.get("https://api.deepgram.com/v1/models", timeout=12)
    response.raise_for_status()
    grouped = {}
    for item in response.json().get("stt", []):
        if not item.get("batch"):
            continue
        ident = str(item.get("canonical_name") or "").strip()
        if not ident:
            continue
        grouped.setdefault(ident, set()).update(
            code for code in item.get("languages", []) if isinstance(code, str) and code
        )
    if not grouped:
        raise ValueError("لم تُرجع Deepgram نماذج تفريغ صالحة.")
    preferred = {"nova-3-general": 0, "nova-2-general": 1, "whisper-large": 2}
    return [{"id": ident, "languages": sorted(grouped[ident])}
            for ident in sorted(grouped, key=lambda value: (preferred.get(value, 3), value))]


def opencode_models(executable, context=""):
    """Read the installed CLI's configured model list; never use a shell."""
    cwd = str(Path(context)) if context and Path(context).is_dir() else None
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run(
        [str(resolve_opencode(executable)), "models"], cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=20,
        creationflags=flags, check=True,
    )
    models = {line.strip() for line in result.stdout.splitlines()
              if re.fullmatch(r"[A-Za-z0-9._-]+/[^\s]+", line.strip())}
    if not models:
        raise ValueError("لم يُرجع OpenCode نماذج محلية صالحة.")
    return sorted(models)
