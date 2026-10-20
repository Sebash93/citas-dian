"""One invocation: poll on the cadence until the time budget runs out, alerting as needed."""
import json
import os
import random
import time
from datetime import datetime, timedelta

from . import cadence, notify
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


class Monitor:
    def __init__(self, settings, notifier, client=None, clock=cadence.now_bogota,
                 sleep=time.sleep, rand=random.uniform):
        self.settings = settings
        self.notifier = notifier
        self.client = client or DianClient()
        self.clock = clock
        self.sleep = sleep
        self.rand = rand
        self.state = state_mod.load(settings.state_file)

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
        log(f"Check failed ({failures} in a row): {error}")
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
        if blocks >= 3 and not self.state["failure_alert_sent"]:
            self.notifier.send(notify.blocked(blocks, until))
            self.state["failure_alert_sent"] = True

    def _on_slow(self, now, seconds, timed_out):
        """DIAN slows down when slots open: send a 'posible' alert, rate-limited."""
        st = self.state
        st["slow_events"].append({"at": _iso(now), "seconds": round(seconds, 1),
                                  "timed_out": timed_out})
        log(f"DIAN slow: {seconds:.1f} s{' (timed out)' if timed_out else ''}")
        if st["open_since"]:
            return  # a confirmed Medellín alert is already out
        last = _parse(st["last_possible_at"])
        if last and (now - last).total_seconds() < self.settings.possible_cooldown_seconds:
            return
        self.notifier.send(notify.possible(self.settings, seconds, now, timed_out))
        st["last_possible_at"] = _iso(now)

    def _on_result(self, now, result):
        st = self.state
        if result.status != FOUND:
            if st["open_since"]:
                log(f"Medellín opening closed (open since {st['open_since']}).")
                for entry in reversed(st["history"]):
                    if entry["closed"] is None:
                        entry["closed"] = _iso(now)
                        break
                st.update(open_since=None, open_matches=[], alerts_for_opening=0)
            if result.other_cities and result.other_cities != st.get("last_other_cities"):
                log(f"Open elsewhere (not alerting): {result.other_cities}")
                self._capture(now, result.raw, "other-city")
            st["last_other_cities"] = result.other_cities
            return

        changed = result.matches != st["open_matches"]
        if not st["open_since"] or changed:
            if not st["open_since"]:
                st["open_since"] = _iso(now)
                st["history"].append({"opened": _iso(now), "closed": None,
                                      "matches": result.matches})
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

    # --- loop --------------------------------------------------------------

    def poll(self):
        """One check. Returns False when the run should stop (blocked)."""
        now = self.clock()
        self._roll_day(now)
        self.state["checks_today"] += 1
        self.state["last_check_at"] = _iso(now)
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
            return "MONITOR_PAUSED"

        self._maybe_heartbeat(start)
        self._save()
        if once:
            self.poll()
            return self._marker()

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
        return self._marker()

    def _marker(self):
        if self.state["blocks"]:
            return "MONITOR_BLOCKED"
        if self.state["consecutive_failures"]:
            return "MONITOR_ERROR"
        return "MONITOR_OK_FOUND" if self.state["open_since"] else "MONITOR_OK_NONE"
