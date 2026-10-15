"""Explicit public mobile assets shared by the desktop and gateway servers."""

def library_query(values):
    """Decode HTTP query scalars; command validation remains strict and shared."""
    result = dict(values)
    if "limit" in result:
        value = result["limit"]
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 3:
            raise ValueError("عدد الملفات المطلوب غير صالح.")
        result["limit"] = int(value)
    if "refresh" in result:
        if result["refresh"] not in ("0", "1"):
            raise ValueError("قيمة تحديث المكتبة غير صالحة.")
        result["refresh"] = result["refresh"] == "1"
    return result

ASSET_TYPES = {
    "app.js": "application/javascript", "style.css": "text/css", "icon.svg": "image/svg+xml",
    "pwa.js": "application/javascript", "vendor/jsQR.js": "application/javascript",
    "vendor/LICENSE-jsQR.txt": "text/plain",
    **{f"icons/{name}.png": "image/png" for name in (
        "icon-192", "icon-512", "maskable-192", "maskable-512", "apple-touch-icon")},
}

PWA_PAGES = {
    "/sw.js": ("sw.js", "application/javascript"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/offline.html": ("offline.html", "text/html"),
    "/pwa-shell.html": ("index.html", "text/html"),
}

MOBILE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
              "connect-src 'self'; manifest-src 'self'; worker-src 'self'; media-src 'self' blob:; "
              "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
