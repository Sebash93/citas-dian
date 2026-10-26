"""One invocation: poll on the cadence until the time budget runs out, alerting as needed."""
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta

from . import cadence, notify
from .events import EventLog, percentile
from . import state as state_mod
from .check import FOUND, evaluate
from .client import TIMEOUT as TIMEOUT_SECONDS
from .client import Blocked, DianClient, DianError, TimedOut

MAX_BLOCK_PAUSE_MIN = 120


def log(msg):
    print(f"[{cadence.now_bogota():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def _iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def _parse(iso):
    return datetime.fromisoformat(iso) if iso else None


def already_booked(path):
    try:
        with open(path, encoding="utf-8") as f:
            return bool(json.load(f).get("booked"))
    except (OSError, ValueError, AttributeError):
        return False


class BookerProcess:
    """Runs `python -m dian_booker --trigger <why>` beside the monitor, one at a time."""

    def __init__(self):
        self.proc = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, trigger):
        if self.running():
            return False
        self.proc = subprocess.Popen([sys.executable, "-m", "dian_booker", "--trigger", trigger])
        return True

    def wait(self):
        if self.proc is not None:
            self.proc.wait()


class Monitor:
    def __init__(self, settings, notifier, client=None, clock=cadence.now_bogota,
                 sleep=time.sleep, rand=random.uniform, booker=None):
        self.settings = settings
        self.notifier = notifier
        self.client = client or DianClient()
        self.clock = clock
        self.sleep = sleep
        self.rand = rand
        self.booker = booker or (BookerProcess() if settings.booker else None)
        self.state = state_mod.load(settings.state_file)
        self.events = EventLog(settings.events_file, clock, "monitor")
        self.stats = {"checks": 0, "errors": 0, "latencies": [], "statuses": {}}

    # --- bookkeeping -------------------------------------------------------

    def _save(self):
        state_mod.save(self.settings.state_file, self.state)

    def _roll_day(self, now):
        today = now.date().isoformat()
        if self.state["today"] != today:
            if self.state["today"]:
                self.state["prev_day"] = {"date": self.state["today"],
                                          "checks": self.state["checks_today"],
                                          "errors": self.state["errors_today"]}
            self.state.update(today=today, checks_today=0, errors_today=0)

    def _maybe_heartbeat(self, now):
        today = now.date().isoformat()
        if self.state["last_heartbeat_date"] != today and now.hour >= self.settings.heartbeat_hour:
            self.notifier.send(notify.heartbeat(self.state.get("prev_day"),
                                                bool(self.state["open_since"])))
            self.state["last_heartbeat_date"] = today

    def _capture(self, now, data, tag):
        os.makedirs(self.settings.captures_dir, exist_ok=True)
        path = os.path.join(self.settings.captures_dir, f"{now:%Y%m%d-%H%M%S}-{tag}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log(f"Saved raw response to {path}")

    def _maybe_book(self, trigger):
        if self.booker is None or already_booked(self.settings.booking_file):
            return
        if self.booker.start(trigger):
            log(f"Booker started ({trigger}).")
            self.events.emit("booker_launch", trigger=trigger)

    # --- outcomes ----------------------------------------------------------

    def _on_success(self, now):
        if self.state["failure_alert_sent"]:
            self.notifier.send(notify.recovered())
        self.state.update(consecutive_failures=0, failure_alert_sent=False, blocks=0,
                          blocked_until=None)

    def _on_failure(self, error):
        self.client.reset()
        self.state["errors_today"] += 1
        self.state["consecutive_failures"] += 1
        failures = self.state["consecutive_failures"]
        self.stats["errors"] += 1
        log(f"Check failed ({failures} in a row): {error}")
        self.events.emit("failure", error=type(error).__name__, message=str(error)[:300],
                         streak=failures)
        if failures >= self.settings.failures_before_alert and not self.state["failure_alert_sent"]:
            self.notifier.send(notify.broken(error, failures))
            self.state["failure_alert_sent"] = True

    def _on_blocked(self, now, error):
        self.client.reset()
        self.state["errors_today"] += 1
        self.state["blocks"] += 1
        blocks = self.state["blocks"]
        pause = min(10 * 2 ** (blocks - 1), MAX_BLOCK_PAUSE_MIN)
        until = now + timedelta(minutes=pause)
        self.state["blocked_until"] = _iso(until)
        log(f"Blocked ({blocks} in a row): {error}. Pausing {pause} min.")
        self.stats["errors"] += 1
        self.events.emit("blocked", message=str(error)[:300], blocks=blocks, pause_min=pause)
        if blocks >= 3 and not self.state["failure_alert_sent"]:
            self.notifier.send(notify.blocked(blocks, until))
            self.state["failure_alert_sent"] = True

    def _on_slow(self, now, seconds, timed_out):
        """DIAN slows down when slots open: send a 'posible' alert, rate-limited."""
        st = self.state
        st["slow_events"].append({"at": _iso(now), "seconds": round(seconds, 1),
                                  "timed_out": timed_out})
        log(f"DIAN slow: {seconds:.1f} s{' (timed out)' if timed_out else ''}")
        self.events.emit("slow", seconds=round(seconds, 1), timed_out=timed_out)
        if st["open_since"]:
            return  # a confirmed Medellín alert is already out
        last = _parse(st["last_possible_at"])
        if last and (now - last).total_seconds() < self.settings.possible_cooldown_seconds:
            return
        self.notifier.send(notify.possible(self.settings, seconds, now, timed_out))
        st["last_possible_at"] = _iso(now)
        if self.settings.booker_on_possible:
            self._maybe_book("posible")

    def _on_result(self, now, result):
        st = self.state
        if result.status != FOUND:
            if st["open_since"]:
                log(f"Medellín opening closed (open since {st['open_since']}).")
                opened = _parse(st["open_since"])
                self.events.emit("closed", opened=st["open_since"],
                                 minutes=round((now - opened).total_seconds() / 60, 1))
                for entry in reversed(st["history"]):
                    if entry["closed"] is None:
                        entry["closed"] = _iso(now)
                        break
                st.update(open_since=None, open_matches=[], alerts_for_opening=0)
            if result.other_cities and result.other_cities != st.get("last_other_cities"):
                log(f"Open elsewhere (not alerting): {result.other_cities}")
                self.events.emit("other_city", names=result.other_cities)
                self._capture(now, result.raw, "other-city")
            st["last_other_cities"] = result.other_cities
            return

        changed = result.matches != st["open_matches"]
        if not st["open_since"] or changed:
            if not st["open_since"]:
                st["open_since"] = _iso(now)
                st["history"].append({"opened": _iso(now), "closed": None,
                                      "matches": result.matches})
                self.events.emit("opening", at=_iso(now), matches=result.matches,
                                 latency=self.client.last_latency)
            st.update(open_matches=result.matches, alerts_for_opening=0)
            self._capture(now, result.raw, "medellin")
            first = True
        else:
            last = _parse(st["last_alert_at"])
            due = last is None or (now - last).total_seconds() >= self.settings.realert_seconds
            if not due or st["alerts_for_opening"] > self.settings.max_realerts:
                return
            first = False
        self.notifier.send(notify.opening(self.settings, result.matches, now, first=first))
        st["alerts_for_opening"] += 1
        st["last_alert_at"] = _iso(now)
        self._maybe_book("found")  # no-op while one is running; retries on each re-alert

    # --- loop --------------------------------------------------------------

    def poll(self):
        """One check. Returns False when the run should stop (blocked)."""
        now = self.clock()
        self._roll_day(now)
        self.state["checks_today"] += 1
        self.state["last_check_at"] = _iso(now)
        self.stats["checks"] += 1
        s = self.settings
        try:
            result = evaluate(self.client.find_colas(s.tipo_persona, s.categoria, s.tipo_atencion),
                              s.city)
        except Blocked as e:
            self._on_blocked(now, e)
            self._save()
            return False
        except TimedOut as e:
            self._on_failure(e)
            self._on_slow(now, float(TIMEOUT_SECONDS), timed_out=True)
            self._save()
            return True
        except DianError as e:
            self._on_failure(e)
            self._save()
            return True
        self._on_success(now)
        latency = self.client.last_latency
        if latency is not None:
            self.stats["latencies"].append(latency)
        statuses = self.stats["statuses"]
        statuses[result.status] = statuses.get(result.status, 0) + 1
        log(f"Check: {result.status} {result.matches or ''} "
            f"({latency or 0:.1f} s, requests so far: {self.client.requests_made})")
        self._on_result(now, result)
        if result.status != FOUND and latency is not None and latency >= s.slow_seconds:
            self._on_slow(now, latency, timed_out=False)
        self._save()
        return True

    def run(self, budget_seconds=None, once=False):
        budget = self.settings.run_budget_seconds if budget_seconds is None else budget_seconds
        start = self.clock()
        deadline = start + timedelta(seconds=budget)
        self._roll_day(start)

        blocked_until = _parse(self.state["blocked_until"])
        if blocked_until and start < blocked_until and not once:
            log(f"Paused after a block until {blocked_until:%H:%M}. Skipping this run.")
            self.events.emit("run", marker="MONITOR_PAUSED", checks=0, errors=0)
            return "MONITOR_PAUSED"

        self._maybe_heartbeat(start)
        self._save()
        if once:
            self.poll()
        else:
            self._loop(deadline)
        if self.booker is not None and self.booker.running():
            log("Waiting for the booker to finish before ending the run.")
            self.booker.wait()
        marker = self._marker()
        self._emit_run(marker, start)
        return marker

    def _emit_run(self, marker, start):
        lat = self.stats["latencies"]
        self.events.emit("run", marker=marker, checks=self.stats["checks"],
                         errors=self.stats["errors"], statuses=self.stats["statuses"],
                         latency_median=percentile(lat, 0.5) and round(percentile(lat, 0.5), 2),
                         latency_max=round(max(lat), 2) if lat else None,
                         minutes=round((self.clock() - start).total_seconds() / 60, 1))
        self.stats = {"checks": 0, "errors": 0, "latencies": [], "statuses": {}}
        self.events.trim()  # the booker (the only other writer) has finished by now

    def _loop(self, deadline):
        self.sleep(self.rand(0, 10))  # avoid hitting the server on the exact same second
        while True:
            now = self.clock()
            interval = cadence.interval_for(now)
            last = _parse(self.state["last_check_at"])
            due = now if last is None else last + timedelta(seconds=interval * self.rand(0.85, 1.15))
            if due > deadline:
                break
            if due > now:
                self.sleep((due - now).total_seconds())
            if not self.poll():
                break

    def _marker(self):
        if self.state["blocks"]:
            return "MONITOR_BLOCKED"
        if self.state["consecutive_failures"]:
            return "MONITOR_ERROR"
        return "MONITOR_OK_FOUND" if self.state["open_since"] else "MONITOR_OK_NONE"
