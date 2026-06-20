import json
import os

_PATH = os.environ.get("SETTINGS_FILE", "/app/data/settings.json")

_DEFAULTS = {
    "notify_email": False,
    "notify_slack": False,
    "notify_kakao": False,
    "alert_crawl_done": True,
    "alert_parse_done": True,
    "alert_on_error": True,
    "alert_daily_summary": True,
    "email_address": "",
    "kakao_token": "",
    "scheduler_enabled": False,
    "scheduler_hour": 2,
    "scheduler_minute": 0,
}


def load() -> dict:
    if os.path.exists(_PATH):
        try:
            with open(_PATH, encoding="utf-8") as f:
                return {**_DEFAULTS, **json.load(f)}
        except Exception:
            pass
    return dict(_DEFAULTS)


def save(data: dict):
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
