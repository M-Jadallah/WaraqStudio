"""CLI Proxy API integration for WaraqStudio.

Integrates Waraq with CLIProxyAPI (https://github.com/router-for-me/CLIProxyAPI)
providing proxy URL normalization, model discovery, account limits probing,
and OpenCode configuration generation.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
import re
import time
from typing import Any
import urllib.parse

import requests

from .config import atomic_json

logger = logging.getLogger(__name__)

DEFAULT_CLIPROXY_URL = "http://127.0.0.1:8317"

THINKING_LEVELS = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

THINKING_LEVEL_OPTIONS = [
    {"id": "", "name": "افتراضي النموذج"},
    {"id": "none", "name": "بدون تفكير (none)"},
    {"id": "minimal", "name": "أدنى (minimal)"},
    {"id": "low", "name": "منخفض (low)"},
    {"id": "medium", "name": "متوسط (medium)"},
    {"id": "high", "name": "عالٍ (high)"},
    {"id": "xhigh", "name": "عالٍ جدًا (xhigh)"},
    {"id": "max", "name": "أقصى (max)"},
    {"id": "ultra", "name": "فائق (ultra)"},
]

PROVIDER_LABELS: dict[str, str] = {
    "codex": "OpenAI Codex",
    "openai": "OpenAI",
    "chatgpt": "ChatGPT",
    "anthropic": "Anthropic Claude",
    "claude": "Anthropic Claude",
    "gemini": "Google Gemini",
    "gemini-cli": "Google Gemini CLI",
    "antigravity": "Google Antigravity",
    "copilot": "GitHub Copilot",
    "github-copilot": "GitHub Copilot",
    "deepseek": "DeepSeek",
    "xai": "xAI Grok",
    "grok": "xAI Grok",
    "groq": "Groq",
    "mistral": "Mistral AI",
    "cohere": "Cohere",
    "openrouter": "OpenRouter",
}


def normalize_cliproxy_url(url: str) -> str:
    """Clean and normalize a CLI Proxy API base URL.

    Strips trailing slashes, removes any trailing /v1 case-insensitively,
    and validates scheme (http/https), hostname, absence of user credentials,
    and port validity.
    """
    val = str(url or "").strip()
    if not val:
        raise ValueError("أدخل عنوان خادم CLI Proxy API صالحًا يبدأ بـ http:// أو https://.")

    val = val.rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(val)
    except Exception:
        raise ValueError("أدخل عنوان خادم CLI Proxy API صالحًا يبدأ بـ http:// أو https://.")

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError("أدخل عنوان خادم CLI Proxy API صالحًا يبدأ بـ http:// أو https://.")

    if not parsed.hostname:
        raise ValueError("أدخل عنوان خادم CLI Proxy API صالحًا يبدأ بـ http:// أو https://.")

    if parsed.username or parsed.password:
        raise ValueError("أدخل عنوان خادم CLI Proxy API صالحًا يبدأ بـ http:// أو https://.")

    if "@" in parsed.netloc:
        raise ValueError("أدخل عنوان خادم CLI Proxy API صالحًا يبدأ بـ http:// أو https://.")

    try:
        port = parsed.port
    except ValueError:
        raise ValueError("أدخل عنوان خادم CLI Proxy API صالحًا يبدأ بـ http:// أو https://.")

    if port is not None and not (1 <= port <= 65535):
        raise ValueError("أدخل عنوان خادم CLI Proxy API صالحًا يبدأ بـ http:// أو https://.")

    if parsed.query or parsed.fragment:
        raise ValueError("أدخل عنوان خادم CLI Proxy API صالحًا يبدأ بـ http:// أو https://.")

    path = (parsed.path or "").rstrip("/")
    while path.lower().endswith("/v1"):
        path = path[:-3].rstrip("/")

    netloc = parsed.netloc
    normalized = f"{scheme}://{netloc}{path}".rstrip("/")
    return normalized


def cliproxy_v1_url(url: str) -> str:
    """Return the /v1 endpoint root for an OpenAI-compatible CLI Proxy API."""
    return normalize_cliproxy_url(url) + "/v1"


def _extract_jwt_payload(token_str: str) -> dict[str, Any]:
    """Safely decode JWT payload without external dependencies."""
    try:
        parts = str(token_str or "").strip().split(".")
        if len(parts) >= 2:
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
            return json.loads(decoded.decode("utf-8", errors="ignore"))
    except Exception:
        pass
    return {}


def format_reset_time(
    reset_at: float | int | None = None,
    reset_after_seconds: float | int | None = None,
) -> str:
    """Format rate limit reset countdown in human-friendly Arabic prose."""
    now = time.time()
    diff: int | None = None
    if reset_after_seconds is not None:
        try:
            diff = max(0, int(float(reset_after_seconds)))
        except (ValueError, TypeError):
            diff = None
    elif reset_at is not None:
        try:
            val = float(reset_at)
            if val > 1e11:
                val /= 1000.0
            diff = max(0, int(val - now))
        except (ValueError, TypeError):
            diff = None

    if diff is None:
        return ""
    if diff <= 0:
        return "الآن"
    if diff < 60:
        return "خلال دقيقة"
    if diff < 3600:
        mins = max(1, diff // 60)
        if mins == 1:
            return "خلال دقيقة"
        if mins == 2:
            return "خلال دقيقتين"
        return f"خلال {mins} دقيقة"
    if diff < 86400:
        hours = diff // 3600
        mins = (diff % 3600) // 60
        if mins > 0:
            return f"خلال {hours} س و {mins} د"
        return f"خلال {hours} ساعة"
    days = diff // 86400
    hours = (diff % 86400) // 3600
    if hours > 0:
        return f"خلال {days} ي و {hours} س"
    return f"خلال {days} يوم"


def _parse_model_entry(item: Any) -> dict[str, Any] | None:
    """Extract model metadata dictionary from API item."""
    if isinstance(item, str):
        raw_id = item.strip()
        if not raw_id:
            return None
        while raw_id.startswith("cliproxy/"):
            raw_id = raw_id[len("cliproxy/"):]
        return {
            "id": raw_id,
            "name": raw_id,
            "provider": "",
            "thinkingLevels": list(THINKING_LEVELS[1:]),
        }
    if not isinstance(item, dict):
        return None

    raw_id = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
    if not raw_id:
        return None

    while raw_id.startswith("cliproxy/"):
        raw_id = raw_id[len("cliproxy/"):]

    name = str(item.get("display_name") or item.get("name") or raw_id or "").strip()
    if not name:
        name = raw_id

    provider = str(item.get("owned_by") or item.get("type") or item.get("provider") or "").strip()

    thinking = item.get("thinking")
    levels = None
    if isinstance(thinking, dict):
        levels = thinking.get("levels")
    elif isinstance(item.get("thinking_levels"), list):
        levels = item.get("thinking_levels")
    elif isinstance(item.get("thinkingLevels"), list):
        levels = item.get("thinkingLevels")

    if isinstance(levels, (list, tuple)) and levels:
        thinking_levels = [str(lvl).strip() for lvl in levels if str(lvl).strip()]
    else:
        thinking_levels = list(THINKING_LEVELS[1:])

    return {
        "id": raw_id,
        "name": name,
        "provider": provider,
        "thinkingLevels": thinking_levels,
    }


def fetch_cliproxy_models(
    base_url: str,
    api_key: str = "",
    management_key: str = "",
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """Fetch available models from CLI Proxy API server.

    Queries GET <base>/v1/models and falls back to management endpoints
    if /v1/models is restricted or empty.
    """
    clean_base = normalize_cliproxy_url(base_url)
    v1_url = f"{clean_base}/v1/models"

    token = (api_key or "").strip() or (management_key or "").strip()
    v1_headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        v1_headers["Authorization"] = f"Bearer {token}"

    raw_items: list[Any] = []
    v1_auth_error = False

    try:
        resp = requests.get(v1_url, headers=v1_headers, timeout=timeout)
        if resp.status_code in (401, 403):
            v1_auth_error = True
        elif resp.ok:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    raw_items = data.get("data") or data.get("models") or []
                elif isinstance(data, list):
                    raw_items = data
            except Exception:
                pass
    except requests.exceptions.Timeout:
        raise ValueError("انتهت مهلة الاتصال بخادم CLI Proxy API أثناء جلب النماذج.")
    except requests.exceptions.ConnectionError:
        raise ValueError("تعذر الاتصال بخادم CLI Proxy API؛ تأكد من تشغيل الخادم وصحة العنوان.")
    except requests.exceptions.RequestException:
        raise ValueError("فشل الاتصال بخادم CLI Proxy API أثناء جلب النماذج.")

    # Fallback to management credentials/models if v1 failed or returned empty
    mkey = (management_key or "").strip() or (api_key or "").strip()
    if (v1_auth_error or not raw_items) and mkey:
        mgmt_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {mkey}",
            "X-Management-Key": mkey,
        }
        for endpoint, model_endpoint in (
            ("/v8/management/credentials", "/v8/management/credentials/models"),
            ("/v0/management/auth-files", "/v0/management/auth-files/models"),
        ):
            try:
                c_resp = requests.get(f"{clean_base}{endpoint}", headers=mgmt_headers, timeout=min(timeout, 8.0))
                if c_resp.ok:
                    c_data = c_resp.json()
                    files = c_data.get("files") if isinstance(c_data, dict) else (c_data if isinstance(c_data, list) else [])
                    for f in files or []:
                        fname = f.get("name") if isinstance(f, dict) else None
                        if fname:
                            m_resp = requests.get(
                                f"{clean_base}{model_endpoint}",
                                headers=mgmt_headers,
                                params={"name": fname},
                                timeout=min(timeout, 5.0),
                            )
                            if m_resp.ok:
                                m_json = m_resp.json()
                                items = m_json.get("models") or m_json.get("data") if isinstance(m_json, dict) else (m_json if isinstance(m_json, list) else [])
                                if isinstance(items, list):
                                    raw_items.extend(items)
                    if raw_items:
                        v1_auth_error = False
                        break
            except Exception:
                continue

    if v1_auth_error and not raw_items:
        raise ValueError("رفض خادم CLI Proxy API مفتاح المصادقة؛ يرجى التحقق من المفتاح المدخل.")

    models_by_id: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        parsed = _parse_model_entry(item)
        if parsed and parsed["id"]:
            mid = parsed["id"]
            if mid not in models_by_id:
                models_by_id[mid] = parsed
            else:
                existing = models_by_id[mid]
                if existing["name"] == mid and parsed["name"] != mid:
                    existing["name"] = parsed["name"]
                if not existing["provider"] and parsed["provider"]:
                    existing["provider"] = parsed["provider"]

    result = sorted(models_by_id.values(), key=lambda x: x["id"].lower())
    if not result:
        raise ValueError("لم يُرجع خادم CLI Proxy API أي نماذج متاحة.")

    return result


def _probe_layer1_quota(
    clean_base: str,
    entry: dict[str, Any],
    mgmt_headers: dict[str, str],
    timeout: float = 6.0,
) -> tuple[float, float, str, str, str, list[dict[str, Any]], str]:
    """Layer 1: Probes quota via /v8/management/plugins or /v0/management/quota/fetch."""
    five_hour_rem = -1.0
    five_hour_reset = ""
    weekly_rem = -1.0
    weekly_reset = ""
    plan = ""
    windows: list[dict[str, Any]] = []
    err = ""

    auth_index = entry.get("auth_index")
    if auth_index is None:
        auth_index = entry.get("index") or entry.get("id")
    quota_plugin_id = entry.get("quota_plugin_id")
    provider = str(entry.get("type") or entry.get("provider") or "").strip()

    has_quota_flag = bool(entry.get("supports_quota") or quota_plugin_id or entry.get("quota_probe"))
    if not has_quota_flag or auth_index is None:
        return five_hour_rem, weekly_rem, five_hour_reset, weekly_reset, plan, windows, err

    q_data: dict[str, Any] | None = None
    if quota_plugin_id:
        try:
            url = f"{clean_base}/v8/management/plugins/{urllib.parse.quote(str(quota_plugin_id))}/quota"
            r = requests.get(url, headers=mgmt_headers, params={"auth_index": str(auth_index)}, timeout=timeout)
            if r.ok:
                q_data = r.json()
        except Exception as exc:
            logger.debug("Layer 1 plugin probe error: %s", exc)

    if not q_data:
        try:
            url = f"{clean_base}/v0/management/quota/fetch"
            payload = {"auth_index": auth_index, "provider": provider, "plugin_id": quota_plugin_id}
            r = requests.post(url, headers=mgmt_headers, json=payload, timeout=timeout)
            if r.ok:
                q_data = r.json()
        except Exception as exc:
            logger.debug("Layer 1 quota fetch error: %s", exc)

    if not isinstance(q_data, dict):
        return five_hour_rem, weekly_rem, five_hour_reset, weekly_reset, plan, windows, err

    plan = str(q_data.get("subscription", {}).get("plan") or q_data.get("plan") or "").strip()

    buckets_found: list[dict[str, Any]] = []
    groups = q_data.get("groups")
    if isinstance(groups, list):
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("buckets"), list):
                buckets_found.extend([b for b in g["buckets"] if isinstance(b, dict)])
    elif isinstance(q_data.get("buckets"), list):
        buckets_found.extend([b for b in q_data["buckets"] if isinstance(b, dict)])

    for b in buckets_found:
        rem_pct: float = -1.0
        rem_frac = b.get("remaining_fraction")
        if rem_frac is not None:
            try:
                rem_pct = round(max(0.0, min(100.0, float(rem_frac) * 100.0)), 1)
            except (ValueError, TypeError):
                pass
        elif b.get("remaining_amount") is not None and b.get("total_amount"):
            try:
                tot = float(b["total_amount"])
                if tot > 0:
                    rem_pct = round(max(0.0, min(100.0, (float(b["remaining_amount"]) / tot) * 100.0)), 1)
            except (ValueError, TypeError):
                pass

        w_id = str(b.get("id") or b.get("window") or b.get("description") or "").lower()
        reset_val = b.get("reset_time") or b.get("resets_at") or b.get("reset_at")
        reset_text = format_reset_time(reset_at=reset_val)

        if rem_pct >= 0:
            windows.append({
                "id": w_id or f"b{len(windows)+1}",
                "label": str(b.get("description") or b.get("name") or w_id or f"حد {len(windows)+1}"),
                "remainingPercent": rem_pct,
                "resetText": reset_text,
            })
            if "5h" in w_id or "5 hour" in w_id or "primary" in w_id:
                five_hour_rem = rem_pct
                five_hour_reset = reset_text
            elif "7d" in w_id or "week" in w_id or "secondary" in w_id:
                weekly_rem = rem_pct
                weekly_reset = reset_text

    return five_hour_rem, weekly_rem, five_hour_reset, weekly_reset, plan, windows, err


def _probe_layer2_api_call(
    clean_base: str,
    entry: dict[str, Any],
    mgmt_headers: dict[str, str],
    timeout: float = 8.0,
) -> tuple[float, float, str, str, str, list[dict[str, Any]], str]:
    """Layer 2: Active Upstream Usage via Management api-call."""
    five_hour_rem = -1.0
    five_hour_reset = ""
    weekly_rem = -1.0
    weekly_reset = ""
    plan = ""
    windows: list[dict[str, Any]] = []
    err = ""

    auth_index = entry.get("auth_index")
    if auth_index is None:
        auth_index = entry.get("index") or entry.get("id")
    if auth_index is None:
        return five_hour_rem, weekly_rem, five_hour_reset, weekly_reset, plan, windows, err

    provider = str(entry.get("type") or entry.get("provider") or "").lower().strip()

    def _call_api(body: dict[str, Any]) -> dict[str, Any] | None:
        for path in ("/v8/management/requests/api-call", "/v0/management/api-call"):
            try:
                r = requests.post(f"{clean_base}{path}", headers=mgmt_headers, json=body, timeout=timeout)
                if r.ok:
                    res = r.json()
                    if isinstance(res, dict):
                        if "body" in res:
                            b = res["body"]
                            if isinstance(b, dict):
                                return b
                            if isinstance(b, str):
                                try:
                                    return json.loads(b)
                                except Exception:
                                    pass
                        if "response" in res and isinstance(res["response"], dict):
                            return res["response"]
                        return res
                    elif isinstance(res, str):
                        try:
                            return json.loads(res)
                        except Exception:
                            pass
            except Exception as exc:
                logger.debug("Layer 2 api-call error on %s: %s", path, exc)
        return None

    # Case A: Codex / OpenAI / ChatGPT accounts
    if provider in ("codex", "openai", "chatgpt") or not provider:
        chatgpt_account_id = entry.get("chatgpt_account_id") or entry.get("account_id")
        if not chatgpt_account_id:
            id_token = entry.get("id_token")
            if isinstance(id_token, dict):
                chatgpt_account_id = id_token.get("chatgpt_account_id") or id_token.get("account_id")
            elif isinstance(id_token, str):
                jwt_payload = _extract_jwt_payload(id_token)
                chatgpt_account_id = jwt_payload.get("chatgpt_account_id") or jwt_payload.get("account_id")

        header = {
            "Authorization": "Bearer $TOKEN$",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "codex_cli_rs/0.76.0",
        }
        if chatgpt_account_id:
            header["Chatgpt-Account-Id"] = str(chatgpt_account_id)

        req_body = {
            "auth_index": auth_index,
            "method": "GET",
            "url": "https://chatgpt.com/backend-api/wham/usage",
            "header": header,
        }
        usage_res = _call_api(req_body)
        if isinstance(usage_res, dict):
            plan = str(usage_res.get("plan_type") or usage_res.get("plan") or "").strip()
            rate_limit = usage_res.get("rate_limit") or {}
            if isinstance(rate_limit, dict):
                p_win = rate_limit.get("primary_window") or {}
                s_win = rate_limit.get("secondary_window") or {}

                if isinstance(p_win, dict) and p_win.get("used_percent") is not None:
                    try:
                        used = float(p_win["used_percent"])
                        five_hour_rem = round(max(0.0, min(100.0, 100.0 - used)), 1)
                        five_hour_reset = format_reset_time(
                            reset_at=p_win.get("reset_at"),
                            reset_after_seconds=p_win.get("reset_after_seconds"),
                        )
                    except (ValueError, TypeError):
                        pass

                if isinstance(s_win, dict) and s_win.get("used_percent") is not None:
                    try:
                        used = float(s_win["used_percent"])
                        weekly_rem = round(max(0.0, min(100.0, 100.0 - used)), 1)
                        weekly_reset = format_reset_time(
                            reset_at=s_win.get("reset_at"),
                            reset_after_seconds=s_win.get("reset_after_seconds"),
                        )
                    except (ValueError, TypeError):
                        pass

    # Case B: Google Gemini CLI / Antigravity accounts
    elif provider in ("gemini-cli", "antigravity", "gemini"):
        project_id = entry.get("project_id") or entry.get("projectId")
        if project_id:
            q_body = {
                "auth_index": auth_index,
                "method": "POST",
                "url": f"https://cloudcode-pa.googleapis.com/v1alpha/projects/{project_id}:retrieveUserQuota",
                "header": {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json"},
            }
            quota_res = _call_api(q_body)
            if isinstance(quota_res, dict):
                buckets = quota_res.get("buckets") or []
                min_rem = 100.0
                found_bucket = False
                for b in buckets:
                    if isinstance(b, dict) and b.get("remainingFraction") is not None:
                        try:
                            frac = float(b["remainingFraction"])
                            pct = round(max(0.0, min(100.0, frac * 100.0)), 1)
                            min_rem = min(min_rem, pct)
                            found_bucket = True
                            windows.append({
                                "id": str(b.get("modelId") or f"b{len(windows)+1}"),
                                "label": str(b.get("modelId") or "Gemini Quota"),
                                "remainingPercent": pct,
                                "resetText": "",
                            })
                        except (ValueError, TypeError):
                            pass
                if found_bucket:
                    five_hour_rem = min_rem

    return five_hour_rem, weekly_rem, five_hour_reset, weekly_reset, plan, windows, err


def _probe_layer3_signals(
    entry: dict[str, Any],
) -> tuple[float, float, str, str, str, bool]:
    """Layer 3: Passive Quota Signals from entry headers/signals."""
    five_hour_rem = -1.0
    five_hour_reset = ""
    weekly_rem = -1.0
    weekly_reset = ""
    plan = ""
    limit_reached = False

    raw_signals = (
        entry.get("quota", {}).get("signals", {})
        if isinstance(entry.get("quota"), dict)
        else {}
    )
    if not raw_signals and isinstance(entry.get("signals"), dict):
        raw_signals = entry["signals"]

    if not isinstance(raw_signals, dict):
        return five_hour_rem, weekly_rem, five_hour_reset, weekly_reset, plan, limit_reached

    signals = {str(k).lower().strip(): v for k, v in raw_signals.items()}

    if "x-codex-primary-used-percent" in signals:
        try:
            used = float(signals["x-codex-primary-used-percent"])
            five_hour_rem = round(max(0.0, min(100.0, 100.0 - used)), 1)
        except (ValueError, TypeError):
            pass

    if "x-codex-secondary-used-percent" in signals:
        try:
            used = float(signals["x-codex-secondary-used-percent"])
            weekly_rem = round(max(0.0, min(100.0, 100.0 - used)), 1)
        except (ValueError, TypeError):
            pass

    p_reset_at = signals.get("x-codex-primary-reset-at")
    p_reset_secs = signals.get("x-codex-primary-reset-after-seconds")
    if p_reset_at or p_reset_secs:
        five_hour_reset = format_reset_time(p_reset_at, p_reset_secs)

    s_reset_at = signals.get("x-codex-secondary-reset-at")
    s_reset_secs = signals.get("x-codex-secondary-reset-after-seconds")
    if s_reset_at or s_reset_secs:
        weekly_reset = format_reset_time(s_reset_at, s_reset_secs)

    if "x-codex-plan-type" in signals:
        plan = str(signals["x-codex-plan-type"]).strip()

    if "x-codex-limit-reached" in signals:
        val = signals["x-codex-limit-reached"]
        limit_reached = str(val).lower() in ("true", "1", "yes")

    if "anthropic-ratelimit-unified-5h-utilization" in signals and five_hour_rem < 0:
        try:
            util = float(signals["anthropic-ratelimit-unified-5h-utilization"])
            five_hour_rem = round(max(0.0, min(100.0, (1.0 - util) * 100.0)), 1)
        except (ValueError, TypeError):
            pass

    if "anthropic-ratelimit-unified-7d-utilization" in signals and weekly_rem < 0:
        try:
            util = float(signals["anthropic-ratelimit-unified-7d-utilization"])
            weekly_rem = round(max(0.0, min(100.0, (1.0 - util) * 100.0)), 1)
        except (ValueError, TypeError):
            pass

    if "anthropic-ratelimit-unified-reset" in signals and not five_hour_reset:
        five_hour_reset = format_reset_time(reset_at=signals["anthropic-ratelimit-unified-reset"])

    return five_hour_rem, weekly_rem, five_hour_reset, weekly_reset, plan, limit_reached


def fetch_cliproxy_accounts(
    base_url: str,
    management_key: str = "",
    api_key: str = "",
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """Fetch linked accounts and their remaining quotas from CLI Proxy API."""
    clean_base = normalize_cliproxy_url(base_url)
    key = (management_key or "").strip() or (api_key or "").strip()

    mgmt_headers: dict[str, str] = {"Accept": "application/json"}
    if key:
        mgmt_headers["Authorization"] = f"Bearer {key}"
        mgmt_headers["X-Management-Key"] = key

    files_list: list[dict[str, Any]] = []
    auth_failed = False
    not_found_count = 0

    endpoints = ["/v8/management/credentials", "/v0/management/auth-files"]
    for ep in endpoints:
        try:
            resp = requests.get(f"{clean_base}{ep}", headers=mgmt_headers, timeout=timeout)
            if resp.status_code in (401, 403):
                auth_failed = True
                break
            elif resp.status_code == 404:
                not_found_count += 1
                continue
            elif resp.ok:
                try:
                    payload = resp.json()
                    if isinstance(payload, dict):
                        files_list = (
                            payload.get("files")
                            or payload.get("credentials")
                            or payload.get("auth_files")
                            or []
                        )
                    elif isinstance(payload, list):
                        files_list = payload
                    break
                except Exception:
                    pass
        except requests.exceptions.Timeout:
            raise ValueError("انتهت مهلة الاتصال بواجهة إدارة CLI Proxy API.")
        except requests.exceptions.ConnectionError:
            raise ValueError("تعذر الاتصال بواجهة إدارة CLI Proxy API؛ تأكد من تشغيل الخادم وصحة العنوان.")
        except requests.exceptions.RequestException:
            raise ValueError("تعذر الاتصال بواجهة إدارة CLI Proxy API؛ تأكد من تشغيل الخادم وصحة العنوان.")

    if auth_failed:
        raise ValueError("رفض خادم CLI Proxy API مفتاح الإدارة؛ تحقق من إدخال مفتاح الإدارة الصحيح.")

    if not_found_count >= len(endpoints):
        raise ValueError("واجهة إدارة CLI Proxy API غير مفعّلة على الخادم؛ فعّل secret-key في إعدادات الخادم لعرض الحسابات والحدود.")

    accounts: list[dict[str, Any]] = []
    for idx, raw_entry in enumerate(files_list):
        if not isinstance(raw_entry, dict):
            continue

        ident = str(raw_entry.get("id") or raw_entry.get("name") or raw_entry.get("auth_index") or f"acc_{idx+1}")
        auth_index = str(
            raw_entry.get("auth_index")
            if raw_entry.get("auth_index") is not None
            else (raw_entry.get("index") if raw_entry.get("index") is not None else ident)
        )
        name = str(raw_entry.get("name") or raw_entry.get("display_name") or raw_entry.get("email") or ident)
        email = str(raw_entry.get("email") or raw_entry.get("user_email") or "").strip()
        if not email and "@" in name:
            email = name

        provider = str(raw_entry.get("type") or raw_entry.get("provider") or raw_entry.get("service") or "").lower().strip()
        provider_label = PROVIDER_LABELS.get(provider, provider.title() if provider else "غير محدد")

        # Layer 1
        l1_5h, l1_w, l1_5h_reset, l1_w_reset, l1_plan, l1_windows, l1_err = _probe_layer1_quota(
            clean_base, raw_entry, mgmt_headers
        )

        five_hour_rem = l1_5h
        weekly_rem = l1_w
        five_hour_reset = l1_5h_reset
        weekly_reset = l1_w_reset
        plan = l1_plan
        windows = l1_windows
        err = l1_err

        # Layer 2 if Layer 1 yielded no 5h/weekly windows
        if five_hour_rem < 0 and weekly_rem < 0 and not windows:
            l2_5h, l2_w, l2_5h_reset, l2_w_reset, l2_plan, l2_windows, l2_err = _probe_layer2_api_call(
                clean_base, raw_entry, mgmt_headers
            )
            if l2_5h >= 0:
                five_hour_rem = l2_5h
            if l2_w >= 0:
                weekly_rem = l2_w
            if l2_5h_reset:
                five_hour_reset = l2_5h_reset
            if l2_w_reset:
                weekly_reset = l2_w_reset
            if l2_plan and not plan:
                plan = l2_plan
            if l2_windows:
                windows.extend(l2_windows)
            if l2_err and not err:
                err = l2_err

        # Layer 3
        l3_5h, l3_w, l3_5h_reset, l3_w_reset, l3_plan, l3_limit_reached = _probe_layer3_signals(raw_entry)
        if five_hour_rem < 0 and l3_5h >= 0:
            five_hour_rem = l3_5h
        if weekly_rem < 0 and l3_w >= 0:
            weekly_rem = l3_w
        if not five_hour_reset and l3_5h_reset:
            five_hour_reset = l3_5h_reset
        if not weekly_reset and l3_w_reset:
            weekly_reset = l3_w_reset
        if l3_plan and not plan:
            plan = l3_plan

        if not plan:
            plan = str(raw_entry.get("plan") or raw_entry.get("subscription", {}).get("plan") or "").strip()

        # Compute remainingPercent & hasLimits
        rem_candidates = [v for v in (five_hour_rem, weekly_rem) if v >= 0]
        if rem_candidates:
            remaining_percent = min(rem_candidates)
            has_limits = True
        elif windows:
            w_rems = [w["remainingPercent"] for w in windows if w.get("remainingPercent", -1) >= 0]
            remaining_percent = min(w_rems) if w_rems else -1.0
            has_limits = len(w_rems) > 0
        else:
            remaining_percent = -1.0
            has_limits = False

        # Windows normalization if empty but five_hour / weekly present
        if not windows:
            if five_hour_rem >= 0:
                windows.append({
                    "id": "5h",
                    "label": "نافذة 5 ساعات",
                    "remainingPercent": five_hour_rem,
                    "resetText": five_hour_reset,
                })
            if weekly_rem >= 0:
                windows.append({
                    "id": "7d",
                    "label": "النافذة الأسبوعية",
                    "remainingPercent": weekly_rem,
                    "resetText": weekly_reset,
                })

        # Summary text in Arabic
        if five_hour_rem >= 0 and weekly_rem >= 0:
            t5 = f" ({five_hour_reset})" if five_hour_reset else ""
            tw = f" ({weekly_reset})" if weekly_reset else ""
            limits_summary = f"5 ساعات: {five_hour_rem}%{t5} · أسبوعي: {weekly_rem}%{tw}"
        elif five_hour_rem >= 0:
            t5 = f" ({five_hour_reset})" if five_hour_reset else ""
            limits_summary = f"5 ساعات: {five_hour_rem}%{t5}"
        elif weekly_rem >= 0:
            tw = f" ({weekly_reset})" if weekly_reset else ""
            limits_summary = f"أسبوعي: {weekly_rem}%{tw}"
        elif remaining_percent >= 0:
            limits_summary = f"المتبقي: {remaining_percent}%"
        else:
            limits_summary = "الحدود غير متوفرة"

        # Status & availability
        is_disabled = bool(raw_entry.get("disabled", False))
        raw_status = str(raw_entry.get("status") or "").lower().strip()
        limit_reached = l3_limit_reached or raw_entry.get("limit_reached") or (five_hour_rem == 0.0 or weekly_rem == 0.0)

        if is_disabled or raw_status == "disabled":
            status = "disabled"
            status_label = "معطّل"
            available = False
            disabled = True
        elif raw_status in ("auth_required", "needsverify", "expired", "unauthorized"):
            status = "auth_required"
            status_label = "يلزم تسجيل الدخول"
            available = False
            disabled = False
        elif limit_reached or raw_status in ("cooldown", "rate_limited"):
            status = "cooldown"
            status_label = "مقيّد مؤقتًا"
            available = False
            disabled = False
        elif raw_status in ("error", "failed"):
            status = "error"
            status_label = "خطأ"
            available = False
            disabled = False
        else:
            status = "active"
            status_label = "متاح"
            available = True
            disabled = False

        if not err and raw_entry.get("error"):
            err = str(raw_entry["error"])

        account_record = {
            "id": ident,
            "authIndex": auth_index,
            "name": name,
            "email": email,
            "provider": provider,
            "providerLabel": provider_label,
            "plan": plan,
            "status": status,
            "statusLabel": status_label,
            "available": bool(available),
            "disabled": bool(disabled),
            "remainingPercent": float(remaining_percent),
            "hasLimits": bool(has_limits),
            "fiveHourRemaining": float(five_hour_rem),
            "fiveHourResetText": str(five_hour_reset),
            "weeklyRemaining": float(weekly_rem),
            "weeklyResetText": str(weekly_reset),
            "limitsSummary": str(limits_summary),
            "windows": list(windows),
            "error": str(err),
        }
        accounts.append(account_record)

    return accounts


def write_opencode_cliproxy_config(
    config_dir: Path,
    base_url: str,
    model: str,
    thinking_level: str = "",
) -> Path:
    """Generate and atomically write opencode.json for CLI Proxy API.

    Strips any cliproxy/ prefix or #variant from model to get raw_model,
    builds supported variants list, and outputs configuration referencing
    the CLIPROXY_API_KEY environment variable.
    """
    raw = str(model or "").strip()
    while raw.startswith("cliproxy/"):
        raw = raw[len("cliproxy/"):]
    if "#" in raw:
        raw = raw.split("#", 1)[0]
    raw_model = raw.strip()

    variants = [lvl for lvl in THINKING_LEVELS if lvl]
    custom_thinking = str(thinking_level or "").strip()
    if custom_thinking and custom_thinking not in variants:
        variants.append(custom_thinking)

    cfg_dir = Path(config_dir)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    out_file = cfg_dir / "opencode.json"

    config_data = {
        "providers": {
            "cliproxy": {
                "env": ["CLIPROXY_API_KEY"],
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": cliproxy_v1_url(base_url)},
                "models": {
                    raw_model: {
                        "limit": {"context": 200000, "output": 32000},
                        "variants": variants,
                    }
                },
            }
        },
        "model": f"cliproxy/{raw_model}",
        "snapshots": False,
    }

    atomic_json(out_file, config_data)
    return out_file
