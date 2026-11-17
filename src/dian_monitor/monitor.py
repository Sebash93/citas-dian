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
from . import cases as cases_mod
from .airtable import Airtable, CasesFeed
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
    """Runs `python -m dian_booker --case <id> --trigger <why>` beside the monitor. One at a
    time: every booker uses the same local display and Chrome debugging port."""

    capacity = 1

    def __init__(self, settings):
        self.settings = settings
        self.procs = {}  # case id -> Popen

    def booked(self, case):
        return already_booked(cases_mod.booking_file(self.settings, case.id))

    def running(self, case):
        proc = self.procs.get(case.id)
        return proc is not None and proc.poll() is None

    def active(self):
        return sum(1 for p in self.procs.values() if p.poll() is None)

    def start(self, case, trigger):
        if self.running(case):
            return False
        self.procs[case.id] = subprocess.Popen([sys.executable, "-m", "dian_booker",
                                                "--trigger", trigger, "--case", case.id,
                                                "--city", case.city])
        return True

    def wait(self):
        for proc in self.procs.values():
            if proc.poll() is None:
                log("Waiting for the booker to finish before ending the run.")
                proc.wait()

    def describe(self, case):
        return f"local process {self.procs[case.id].pid}" if self.running(case) else "not running"


def build_feed(settings, events, clock):
    """The Airtable cases feed, or None when Airtable is not configured."""
    airtable = Airtable.from_settings(
        settings, on_call=lambda op, ok: events.emit("airtable_call", op=op, ok=ok))
    if airtable is None:
        return None
    return CasesFeed(airtable, settings.cases_refresh_hours, settings.cases_cache_file, clock,
                     log, refresh_on_start=settings.cases_refresh_on_start)


def build_booker(settings):
    if not settings.booker:
        return None
    if settings.booker_repo:
        from .remote_booker import GitHubBooker
        return GitHubBooker(settings.booker_repo, os.environ.get("GITHUB_DISPATCH_TOKEN", "").strip(),
                            capacity=settings.max_parallel_bookers)
    return BookerProcess(settings)


class Monitor:
    def __init__(self, settings, notifier, client=None, clock=cadence.now_bogota,
                 sleep=time.sleep, rand=random.uniform, booker=None, health=None, cases=None,
                 feed=None):
        """Cases come from `cases` (tests) or the Airtable feed; without Airtable there are none
        and nothing is watched. Routing fields only: the monitor holds no personal data."""
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
        self._booked = {}  # case id -> booked(), cached for one check
        self.cases, self.cities = [], []
        self._skipped_notice = None
        self.feed = feed if cases is not None or feed is not None else build_feed(
            settings, self.events, clock)
        if cases is not None:
            self._set_cases(cases)
        elif self.feed is not None:
            self._feed_result(*self.feed.start())
        else:
            log("Airtable is not configured (AIRTABLE_TOKEN, AIRTABLE_BASE_ID): no cases, "
                "nothing is watched.")
            self.notifier.send(notify.no_cases_source())
            self._set_cases([])

    def cases_in(self, city):
        return [c for c in self.cases if c.city == city]

    def _set_cases(self, cases):
        """Watch these cases (pending only). Openings of cities no longer watched end here, and
        decisions of cases that are gone are forgotten."""
        self.cases = [c for c in cases if c.enabled]
        self.cities = sorted({c.city for c in self.cases})
        now = self.clock()
        for city in [c for c in self.state["openings"] if c not in self.cities]:
            self._on_city_closed(now, city, reason="unwatched")
        ids = {c.id for c in self.cases}
        decisions = self.state["booker_decisions"]
        for cid in [c for c in decisions if c not in ids]:
            decisions.pop(cid)
        watched = ", ".join(f"{c.city_name} [{c.id}]" for c in self.cases) or "no pending case"
        log(f"Cases: {watched}")

    def _feed_result(self, cases, problem):
        feed = self.feed
        if problem:
            if feed.failures == 1:  # once per streak; the cached cases keep being watched
                self.notifier.send(notify.cases_problem(problem, bool(cases)))
        elif feed.fetched_at is not None and self.state.get("cases_problem"):
            self.notifier.send(notify.cases_recovered())
        self.state["cases_problem"] = bool(problem)
        if feed.skipped and feed.skipped != self._skipped_notice:
            self.notifier.send(notify.cases_skipped(feed.skipped))
        self._skipped_notice = list(feed.skipped)
        self.events.emit("cases_refresh", ok=not problem, cases=len(cases),
                         pending=sum(c.enabled for c in cases), skipped=len(feed.skipped),
                         cities=sorted({c.city for c in cases if c.enabled}),
                         error=(problem or "")[:300] or None)
        self._set_cases(cases)

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
            self.notifier.send(notify.heartbeat(
                self.state.get("prev_day"),
                [cases_mod.city_name(c) for c in self.state["openings"]],
                [cases_mod.city_name(c) for c in self.cities]))
            self.state["last_heartbeat_date"] = today

    def _capture(self, now, data, tag):
        os.makedirs(self.settings.captures_dir, exist_ok=True)
        path = os.path.join(self.settings.captures_dir, f"{now:%Y%m%d-%H%M%S}-{tag}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log(f"Saved raw response to {path}")

    def _maybe_book(self, case, trigger):
        """Make sure a booker is running for this case, and record why not when it isn't.

        Called on every check while the case's city is open (and when a new tramite opens
        anywhere, as a silent "watch"), so a booker that ended, failed or never started is
        replaced within one check. Each change of decision is logged and emitted as
        `booker_decision`, so an opening without a booker can always be explained from the log.
        """
        kind, detail = self._try_booker(case, trigger)
        decision = f"{kind}: {detail}" if detail else kind
        decisions = self.state["booker_decisions"]
        if kind == "started" or decision != decisions.get(case.id):
            log(f"Booker {case.id} ({trigger}): {decision}")
            opening = self.state["openings"].get(case.city)
            self.events.emit("booker_decision", trigger=trigger, case=case.id, city=case.city,
                             decision=kind, detail=detail[:300],
                             opening=opening["since"] if opening else None)
            if kind == "error":
                self.notifier.send(notify.booker_unavailable(detail))
        decisions[case.id] = decision

    def _is_booked(self, case):
        if case.id not in self._booked:
            self._booked[case.id] = self.booker.booked(case)
        return self._booked[case.id]

    def _try_booker(self, case, trigger):
        """(kind, detail): off, booked, running, waiting, started or error. Never raises."""
        if self.booker is None:
            return "off", "DIAN_BOOKER is not 1"
        try:
            if self._is_booked(case):
                return "booked", f"a {case.city_name} booking is recorded for this case"
            if self.booker.running(case):
                return "running", self.booker.describe(case)
            active, capacity = self.booker.active(), self.booker.capacity
            if active >= capacity:
                return "waiting", f"{active} bookers running (max {capacity})"
            if self.booker.start(case, trigger):
                return "started", ""
            return "running", self.booker.describe(case)
        except Exception as e:  # noqa: BLE001 - a booker problem must not stop monitoring
            return "error", str(e)

    def _waiting_cases(self, city):
        """Cases of this city without a recorded booking (all of them if that is unknown)."""
        if self.booker is None:
            return self.cases_in(city)
        waiting = []
        for case in self.cases_in(city):
            try:
                if self._is_booked(case):
                    continue
            except Exception:  # noqa: BLE001 - unknown counts as not booked; _try_booker says why
                pass
            waiting.append(case)
        return waiting

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
        """Log every change of what is open in unwatched cities (never an alert)."""
        st = self.state
        previous = st["last_other_cities"]
        st["last_other_cities"] = result.other_cities
        if result.other_cities and result.other_cities != previous:
            log(f"Open elsewhere (not alerting): {result.other_cities}")
            self.events.emit("other_city", names=result.other_cities)
            if result.status != FOUND:  # a response with a watched city is captured as such
                self._capture(now, result.raw, "other-city")

    def _on_result(self, now, result):
        st = self.state
        self._on_other_cities(now, result)
        open_now = result.matches + result.other_cities
        new = set(open_now) - set(st["last_open"])
        st["last_open"] = open_now
        for city in self.cities:
            if city in result.by_city:
                self._on_city_open(now, city, result.by_city[city], result.raw)
            else:
                self._on_city_closed(now, city)
        # DIAN releases cities in waves: a tramite that was not open at the last check starts a
        # silent "watch" booker for each case whose city is not open. It sends the link only if
        # its own city shows up, and a city that stays open can't keep bookers running.
        if new and self.settings.booker_on_other_city:
            for case in self.cases:
                if case.city not in result.by_city:
                    self._maybe_book(case, "watch")

    def _on_city_closed(self, now, city, reason=None):
        st = self.state
        opening = st["openings"].pop(city, None)
        if opening is None:
            return
        log(f"{cases_mod.city_name(city)} opening closed (open since {opening['since']})"
            f"{' because no case watches it anymore' if reason else ''}.")
        opened = _parse(opening["since"])
        self.events.emit("closed", city=city, opened=opening["since"], reason=reason,
                         minutes=round((now - opened).total_seconds() / 60, 1))
        for entry in reversed(st["history"]):
            if entry.get("city") == city and entry["closed"] is None:
                entry["closed"] = _iso(now)
                break
        for case in self.cases_in(city):
            st["booker_decisions"].pop(case.id, None)

    def _on_city_open(self, now, city, matches, raw):
        st = self.state
        opening = st["openings"].get(city)
        name = cases_mod.city_name(city)
        if opening is None or matches != opening["matches"]:
            if opening is None:
                opening = {"since": _iso(now), "matches": matches, "alerts": 0,
                           "last_alert_at": None, "booked_notice": False}
                st["openings"][city] = opening
                st["history"].append({"city": city, "opened": _iso(now), "closed": None,
                                      "matches": matches})
                self.events.emit("opening", city=city, at=_iso(now), matches=matches,
                                 latency=self.client.last_latency)
                # A watch may already be running: log it again under this opening.
                for case in self.cases_in(city):
                    st["booker_decisions"].pop(case.id, None)
            opening.update(matches=matches, alerts=0)
            self._capture(now, raw, city)
            alert, first = True, True
        else:
            last = _parse(opening["last_alert_at"])
            due = last is None or (now - last).total_seconds() >= self.settings.realert_seconds
            alert = due and opening["alerts"] <= self.settings.max_realerts
            first = False
        waiting = self._waiting_cases(city)
        if alert and waiting:
            self.notifier.send(notify.opening(self.settings, name, matches, now, first=first,
                                              cases=[c.id for c in waiting]))
            opening["alerts"] += 1
            opening["last_alert_at"] = _iso(now)
        elif not waiting and not opening["booked_notice"]:
            self.notifier.send(notify.booker_skipped_booked(
                name, [cases_mod.booked_artifact(c.id) for c in self.cases_in(city)]))
            opening["booked_notice"] = True
        for case in self.cases_in(city):  # on every check while the opening lasts
            self._maybe_book(case, "found")

    # --- loop --------------------------------------------------------------

    def poll(self):
        """One check. Returns False when the run should stop (blocked)."""
        now = self.clock()
        self._roll_day(now)
        self.state["checks_today"] += 1
        self.state["last_check_at"] = _iso(now)
        self.stats["checks"] += 1
        s = self.settings
        self._booked = {}
        if self.feed is not None and self.feed.due(now):
            self._feed_result(*self.feed.refresh())
        try:
            result = evaluate(self.client.find_colas(s.tipo_persona, s.categoria, s.tipo_atencion),
                              self.cities, s.service)
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
        return "MONITOR_OK_FOUND" if self.state["openings"] else "MONITOR_OK_NONE"
