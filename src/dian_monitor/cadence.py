"""How often to poll, by Bogota day and time.

Reported pattern (anecdotal, from another monitor's advisor): new slots mostly open
on Fridays around 9:30 (sometimes 15:00) and cancellations come back Tuesday and
Wednesday. Tune these windows with the openings recorded in the state history.
"""
from datetime import datetime, timedelta, timezone

BOGOTA = timezone(timedelta(hours=-5))  # Colombia has no DST


def now_bogota():
    return datetime.now(BOGOTA)


def interval_for(now):
    """Seconds to wait between polls at this moment (before jitter)."""
    dow = now.weekday()  # 0 = Monday
    minutes = now.hour * 60 + now.minute

    def within(start_h, start_m, end_h, end_m):
        return start_h * 60 + start_m <= minutes < end_h * 60 + end_m

    if dow == 4 and (within(8, 30, 12, 30) or within(14, 0, 17, 0)):
        return 45
    if dow in (1, 2) and within(8, 0, 12, 0):
        return 50
    if dow <= 4 and within(7, 0, 19, 0):
        return 75
    if dow <= 4 and (within(6, 0, 7, 0) or within(19, 0, 21, 0)):
        return 300
    return 900
