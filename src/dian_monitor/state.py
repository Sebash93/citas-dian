"""Small JSON state persisted between runs (restored from the Actions cache)."""
import json
import os

MAX_HISTORY = 500


def empty():
    return {
        # Open watched cities: {city: {"since": iso, "matches": [...], "alerts": n,
        #                              "last_alert_at": iso, "booked_notice": bool}}
        "openings": {},
        "consecutive_failures": 0,
        "failure_alert_sent": False,
        "blocks": 0,
        "blocked_until": None,
        "last_heartbeat_date": None,
        "checks_today": 0,
        "errors_today": 0,
        "today": None,
        "last_check_at": None,
        # Tramites open in unwatched cities at the last check (logged on change).
        "last_other_cities": [],
        # Every tramite open at the last check: a new one starts "watch" bookers.
        "last_open": [],
        # Last booker decision per case id ("running: GitHub run 1 in_progress"...); logged
        # only on change.
        "booker_decisions": {},
        # [{"city": c, "opened": iso, "closed": iso|None, "matches": [...]}]
        "history": [],
    }


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return empty()
    state = empty()
    state.update(_migrate(stored))
    return state


LEGACY_KEYS = ("open_since", "open_matches", "alerts_for_opening", "last_alert_at",
               "booked_notice_for", "booker_decision")


def _migrate(stored):
    """State from before cases had one Medellín opening at the top level."""
    if "openings" in stored or not isinstance(stored, dict):
        return stored
    stored = dict(stored)
    openings = {}
    if stored.get("open_since"):
        openings["medellin"] = {
            "since": stored["open_since"], "matches": stored.get("open_matches") or [],
            "alerts": stored.get("alerts_for_opening") or 0,
            "last_alert_at": stored.get("last_alert_at"),
            "booked_notice": stored.get("booked_notice_for") == stored["open_since"]}
    stored["last_open"] = (stored.get("open_matches") or []) + (stored.get("last_other_cities")
                                                                 or [])
    for entry in stored.get("history") or []:
        entry.setdefault("city", "medellin")
    for key in LEGACY_KEYS:
        stored.pop(key, None)
    stored["openings"] = openings
    return stored


def save(path, state):
    state["history"] = state["history"][-MAX_HISTORY:]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
