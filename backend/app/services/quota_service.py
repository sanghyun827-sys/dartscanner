import json
import os
from datetime import datetime

_PATH = os.environ.get("QUOTA_FILE", "/app/data/dart_quota.json")
_DEFAULT_LIMIT = 10000


def _load() -> dict:
    if os.path.exists(_PATH):
        try:
            with open(_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"date": "", "used": 0, "limit": _DEFAULT_LIMIT}


def _save(data: dict):
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    with open(_PATH, "w") as f:
        json.dump(data, f)


def get() -> dict:
    today = datetime.now().strftime("%Y%m%d")
    data = _load()
    if data.get("date") != today:
        data = {"date": today, "used": 0, "limit": data.get("limit", _DEFAULT_LIMIT)}
        _save(data)
    return {**data, "remaining": max(0, data["limit"] - data["used"])}


def increment():
    data = _load()
    today = datetime.now().strftime("%Y%m%d")
    if data.get("date") != today:
        data = {"date": today, "used": 1, "limit": data.get("limit", _DEFAULT_LIMIT)}
    else:
        data["used"] = data.get("used", 0) + 1
    _save(data)


def set_limit(limit: int):
    data = _load()
    data["limit"] = limit
    _save(data)
