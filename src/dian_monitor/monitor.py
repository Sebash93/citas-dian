"""Poll on the cadence until the time budget runs out (or forever), alerting as needed."""
import json
import os
import random
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta

from . import cadence, notify
from .events import EventLog, percentile
from .health import Health
from . import state as state_mod
from .check import FOUND, evaluate
from .client import Blocked, DianClient, DianError

MAX_BLOCK_PAUSE_MIN = 120
# Longest single sleep, so the health check keeps getting pings during long waits.
MAX_SLEEP_SECONDS = 300


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

    def __init__(self, booking_file):
        self.booking_file = booking_file
        self.proc = None

    def booked(self):
        return already_booked(self.booking_file)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, trigger):
        if self.running():
            return False
        self.proc = subprocess.Popen([sys.executable, "-m", "dian_booker", "--trigger", trigger])
        return True

    def wait(self):
        if self.running():
            log("Waiting for the booker to finish before ending the run.")
            self.proc.wait()

    def describe(self):
        return f"local process {self.proc.pid}" if self.running() else "not running"


def build_booker(settings):
    if not settings.booker:
        return None
    if settings.booker_repo:
        from .remote_booker import GitHubBooker
        return GitHubBooker(settings.booker_repo, os.environ.get("GITHUB_DISPATCH_TOKEN", "").strip())
    return BookerProcess(settings.booking_file)


class Monitor:
    def __init__(self, settings, notifier, client=None, clock=cadence.now_bogota,
                 sleep=time.sleep, rand=random.uniform, booker=None, health=None):
        self.settings = settings
        self.notifier = notifier
        self.client = client or DianClient()
        self.clock = clock
        self.sleep = sleep
        self.rand = rand
        self.booker = booker or build_booker(settings)
        self.health = health or Health(settings.healthcheck_url)
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
        """Make sure a booker is running, and record why not when it isn't.

        Called on every check while Medellín is open (and when another city opens), so a booker
        that ended, failed or never started is replaced within one check. Each change of
        decision is logged and emitted as `booker_decision`, so an opening without a booker
        can always be explained from the log.
        """
        kind, detail = self._try_booker(trigger)
        decision = f"{kind}: {detail}" if detail else kind
        if kind == "started" or decision != self.state["booker_decision"]:
            log(f"Booker ({trigger}): {decision}")
            self.events.emit("booker_decision", trigger=trigger, decision=kind,
                             detail=detail[:300], opening=self.state["open_since"])
            if kind == "error":
                self.notifier.send(notify.booker_unavailable(detail))
        self.state["booker_decision"] = decision

    def _try_booker(self, trigger):
        """(kind, detail): off, booked, started, running or error. Never raises."""
        if self.booker is None:
            return "off", "DIAN_BOOKER is not 1"
        try:
            if self.booker.booked():
                if trigger == "found":  # a watch is silent: nothing is open in Medellín
                    self._notify_booked_once()
                return "booked", "a Medellín booking is recorded"
            if self.booker.start(trigger):
                return "started", ""
            return "running", self.booker.describe()
        except Exception as e:  # noqa: BLE001 - a booker problem must not stop monitoring
            return "error", str(e)

    def _notify_booked_once(self):
        if self.state["booked_notice_for"] != self.state["open_since"]:
            self.notifier.send(notify.booker_skipped_booked())
            self.state["booked_notice_for"] = self.state["open_since"]

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

    def _on_other_cities(self, now, result):
        """Log every change, and start a silent "watch" booker when a tramite appears that was
        not open at the last check: DIAN releases cities in waves, so Medellín may be next.
        Never an alert: the booker sends the link only if Medellín itself shows up."""
        st = self.state
        previous = st["last_other_cities"]
        st["last_other_cities"] = result.other_cities
        if result.other_cities and result.other_cities != previous:
            log(f"Open elsewhere (not alerting): {result.other_cities}")
            self.events.emit("other_city", names=result.other_cities)
            if result.status != FOUND:  # a Medellín response is captured as such below
                self._capture(now, result.raw, "other-city")
        if (result.status != FOUND and self.settings.booker_on_other_city
                and set(result.other_cities) - set(previous)):
            self._maybe_book("watch")

    def _on_result(self, now, result):
        st = self.state
        self._on_other_cities(now, result)
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
                st.update(open_since=None, open_matches=[], alerts_for_opening=0,
                          booker_decision=None)
            return

        changed = result.matches != st["open_matches"]
        if not st["open_since"] or changed:
            if not st["open_since"]:
                st["open_since"] = _iso(now)
                st["history"].append({"opened": _iso(now), "closed": None,
                                      "matches": result.matches})
                self.events.emit("opening", at=_iso(now), matches=result.matches,
                                 latency=self.client.last_latency)
                # A watch may already be running: log it again under this opening.
                st["booker_decision"] = None
            st.update(open_matches=result.matches, alerts_for_opening=0)
            self._capture(now, result.raw, "medellin")
            first = True
        else:
            last = _parse(st["last_alert_at"])
            due = last is None or (now - last).total_seconds() >= self.settings.realert_seconds
            if not due or st["alerts_for_opening"] > self.settings.max_realerts:
                self._maybe_book("found")  # no alert due, but keep a booker going
                return
            first = False
        self.notifier.send(notify.opening(self.settings, result.matches, now, first=first))
        st["alerts_for_opening"] += 1
        st["last_alert_at"] = _iso(now)
        self._maybe_book("found")  # also on every later check while the opening lasts

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
        self._save()
        self.health.ping()
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
        if self.booker is not None:
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

    def run_forever(self, max_runs=None):
        """Back-to-back runs of run_budget_seconds each (one `run` event per run), sleeping
        through block pauses. Unexpected errors are logged and retried after a minute."""
        runs = 0
        while max_runs is None or runs < max_runs:
            runs += 1
            try:
                marker = self.run()
            except Exception as e:  # noqa: BLE001 - keep watching; the health check sees "fail"
                log(f"Unexpected error, retrying in 60 s: {e!r}")
                traceback.print_exc()
                self.events.emit("crash", error=type(e).__name__, message=str(e)[:300])
                self.stats = {"checks": 0, "errors": 0, "latencies": [], "statuses": {}}
                self.health.ping("fail")
                self._wait(60)
                continue
            print(marker, flush=True)
            if marker == "MONITOR_PAUSED":
                until = _parse(self.state["blocked_until"])
                self._wait(max(60.0, (until - self.clock()).total_seconds()))

    def _wait(self, seconds):
        """Sleep in slices, pinging the health check after each one."""
        while seconds > 0:
            step = min(seconds, MAX_SLEEP_SECONDS)
            self.sleep(step)
            seconds -= step
            self.health.ping()

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
                self._wait((due - now).total_seconds())
            if not self.poll():
                break

    def _marker(self):
        if self.state["blocks"]:
            return "MONITOR_BLOCKED"
        if self.state["consecutive_failures"]:
            return "MONITOR_ERROR"
        return "MONITOR_OK_FOUND" if self.state["open_since"] else "MONITOR_OK_NONE"
