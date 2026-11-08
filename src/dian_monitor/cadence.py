"""How often to poll, by Bogota day and time.

Reported pattern (anecdotal, from another monitor's advisor): new slots mostly open
on Fridays around 9:30 (sometimes 15:00) and cancellations come back Tuesday and
Wednesday. Tune these windows with the openings recorded in the state history.

Observed 2026-10-27..30 (Render logs): Medellín openings at Wed 10:26, 10:30, 15:24, 15:30
and Fri 7:53, 9:53, 11:12; all but one lasted about a minute, so the gap between checks
must stay well under that. Other cities opened between 6:19 and 16:37.
Observed 2026-11-03..04: Medellín at Tue 14:32, 14:39 and Wed 7:50, 15:32, all outside the old
Tue/Wed morning window (75 s then), so every weekday is now polled closely until 17:00.
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

    if dow == 4 and within(6, 30, 17, 0):
        return 20
    if dow <= 3 and within(7, 0, 17, 0):
        return 35
    if dow <= 4 and within(7, 0, 19, 0):
        return 75
    if dow <= 4 and (within(6, 0, 7, 0) or within(19, 0, 21, 0)):
        return 300
    return 900
