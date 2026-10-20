"""Waraq Studio — durable local media-to-document workflows."""
__version__ = "1.0.0"


def _packaged_version():
    """CI stamps the frozen bundle; source checkouts retain the base version."""
    import re
    from pathlib import Path
    try:
        value = Path(__file__).with_name("_build_version.txt").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"\d+(?:\.\d+){2,3}", value):
            return value
    except OSError:
        pass
    return __version__


__version__ = _packaged_version()
