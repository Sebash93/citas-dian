"""Small JSON state persisted between runs (restored from the Actions cache)."""
import json
import os

MAX_HISTORY = 500


def empty():
    return {
        "open_since": None,         # ISO time the current Medellin opening was first seen
        "open_matches": [],
        "alerts_for_opening": 0,
        "last_alert_at": None,
        "consecutive_failures": 0,
        "failure_alert_sent": False,
        "blocks": 0,
        "blocked_until": None,
        "last_heartbeat_date": None,
        "checks_today": 0,
        "errors_today": 0,
        "today": None,
        "last_check_at": None,
        "last_possible_at": None,
        "slow_events": [],
        # open_since of the opening for which "a booking is already recorded" was sent.
        "booked_notice_for": None,          # [{"at": iso, "seconds": float, "timed_out": bool}]
        "history": [],              # [{"opened": iso, "closed": iso|None, "matches": [...]}]
    }


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return empty()
    state = empty()
    state.update(stored)
    return state


def save(path, state):
    state["history"] = state["history"][-MAX_HISTORY:]
    state["slow_events"] = state["slow_events"][-MAX_HISTORY:]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
