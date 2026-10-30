"""Append-only JSONL event log for spotting problems and tuning over time.

One line per notable event (run summary, opening, failure, booker stage...). It lives
next to the state (restored from the Actions cache) and is uploaded after each run.
The repo is public: events must never carry personal data or the live-browser link.
Writing an event never raises: logging must not break monitoring.
"""
import json
import os
from datetime import datetime

MAX_LINES = 20000  # run summaries are ~75/day, so this keeps months of history


class EventLog:
    def __init__(self, path, clock, source, run_id=None):
        self.path = path
        self.clock = clock
        self.source = source
        self.run_id = run_id if run_id is not None else os.environ.get("GITHUB_RUN_ID")

    def emit(self, kind, **fields):
        event = {"t": self.clock().isoformat(timespec="seconds"), "src": self.source,
                 "kind": kind, **fields}
        if self.run_id:
            event["run"] = self.run_id
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            # One short write per line in append mode: safe with the booker writing too.
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        return event

    def trim(self, max_lines=MAX_LINES):
        """Keep the newest lines. Call only when no other process is writing."""
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) <= max_lines:
                return
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines[-max_lines:])
            os.replace(tmp, self.path)
        except OSError:
            pass


def read(path):
    events = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return events


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def report(events, since=None):
    """Plain-text summary of the events newer than `since` (aware datetime)."""
    if since is not None:
        events = [e for e in events if datetime.fromisoformat(e["t"]) >= since]
    if not events:
        return "No events."
    by_kind = {}
    for e in events:
        by_kind.setdefault(e["kind"], []).append(e)

    out = [f"Events {events[0]['t']} → {events[-1]['t']} ({len(events)} lines)"]
    runs = by_kind.get("run", [])
    checks = sum(r.get("checks", 0) for r in runs)
    errors = sum(r.get("errors", 0) for r in runs)
    out.append(f"\nMonitor: {len(runs)} runs, {checks} checks, {errors} failed "
               f"({100 * errors / checks:.1f}%)" if checks else f"\nMonitor: {len(runs)} runs")
    markers = {}
    for r in runs:
        markers[r.get("marker")] = markers.get(r.get("marker"), 0) + 1
    if markers:
        out.append("  run endings: " + ", ".join(f"{k} {v}" for k, v in sorted(markers.items())))
    latencies = [r["latency_max"] for r in runs if r.get("latency_max") is not None]
    if latencies:
        out.append(f"  slowest check per run: median {percentile(latencies, 0.5):.1f} s, "
                   f"p95 {percentile(latencies, 0.95):.1f} s, max {max(latencies):.1f} s")

    failures = {}
    for f in by_kind.get("failure", []):
        failures[f.get("error")] = failures.get(f.get("error"), 0) + 1
    if failures:
        out.append("  failures by type: " + ", ".join(f"{k} {v}" for k, v in
                                                      sorted(failures.items(), key=lambda x: -x[1])))
    for e in by_kind.get("blocked", []):
        out.append(f"  blocked {e['t']}: pause {e.get('pause_min')} min")

    out.append(f"\nOpenings (target city): {len(by_kind.get('opening', []))}")
    closes = {e.get("opened"): e for e in by_kind.get("closed", [])}
    decisions = {}
    for d in by_kind.get("booker_decision", []):
        decisions.setdefault(d.get("opening"), []).append(d)
    for e in by_kind.get("opening", []):
        c = closes.get(e["t"])
        dur = f"open {c['minutes']:.0f} min" if c else "close not seen"
        out.append(f"  {e['t']}  {dur}  {e.get('matches')}")
        # What the booker did during this opening; "NO BOOKER" flags the ones to look into.
        steps = decisions.get(e["t"], [])
        if not any(d.get("decision") in ("started", "running") for d in steps):
            out.append("    NO BOOKER during this opening")
        for d in steps:
            detail = f" ({d['detail']})" if d.get("detail") else ""
            out.append(f"    {d['t'][11:19]} booker {d.get('decision')}{detail}")
    others = by_kind.get("other_city", [])
    if others:
        out.append(f"Openings elsewhere: {len(others)} (last {others[-1]['t']}: {others[-1].get('names')})")
    slow = by_kind.get("slow", [])
    if slow:
        out.append(f"Slow-DIAN events: {len(slow)} ({sum(1 for s in slow if s.get('timed_out'))} timeouts)")

    attempts = by_kind.get("booker_end", [])
    out.append(f"\nBooker attempts: {len(attempts)}")
    for e in attempts:
        out.append(f"  {e['t']}  {e.get('trigger')}  → {e.get('result')}"
                   f"{' at ' + e['stage'] if e.get('result') == 'FAILED' else ''}"
                   f"  ({e.get('seconds', 0):.0f} s){'  ' + e['error'] if e.get('error') else ''}")
    stages = {}
    for e in by_kind.get("booker_stage", []):
        stages.setdefault(e["stage"], []).append(e["seconds"])
    if stages:
        out.append("  seconds from start (median): " + ", ".join(
            f"{k} {percentile(v, 0.5):.0f}" for k, v in stages.items()))
    steps = {}
    for e in by_kind.get("dian_step", []):
        steps.setdefault(e["step"], []).append(e["seconds"])
    if steps:
        out.append("  DIAN step latency (median/max s): " + ", ".join(
            f"{k} {percentile(v, 0.5):.1f}/{max(v):.1f}" for k, v in steps.items()))
    return "\n".join(out)
