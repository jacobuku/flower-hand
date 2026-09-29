"""Follow Two-Key runs on Flower SuperGrid and append their EVENT lines to events.jsonl.

Every chat message starts a new run, so this polls the federation for run IDs,
streams each new run's log, and appends each `EVENT {json}` line (prefix stripped)
to ./events.jsonl. Events already in the file are never written again.

Usage (from the project root):
    python3 tools/tail_events.py
"""

import json
import subprocess
import threading
import time
from pathlib import Path

SUPERLINK = "supergrid"
FEDERATION = "@jacobuku/two-key"
EVENTS_FILE = Path("events.jsonl")
POLL_S = 3
REATTACH_S = 2
KILL_AFTER_FINISH_S = 30  # stop a stream that stays open this long after its run ends
EVENT_PREFIX = "EVENT "
REQUIRED_FIELDS = {"ts", "order_id", "run_id", "node", "status", "text"}

_lock = threading.Lock()
_written: set[str] = set()  # canonical JSON of every event in EVENTS_FILE
_status: dict[str, str] = {}  # run_id -> latest status from the federation
_finished_seen_at: dict[str, float] = {}  # run_id -> when we first saw it finished
_procs: dict[str, subprocess.Popen] = {}  # run_id -> live `flwr log` process


def _key(event: dict) -> str:
    return json.dumps(event, sort_keys=True, ensure_ascii=False)


def _load_written() -> None:
    """Remember events already on disk so re-reading a log never duplicates them."""
    if not EVENTS_FILE.exists():
        return
    for line in EVENTS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            _written.add(_key(event))


def _list_runs() -> dict[str, str] | None:
    """Return {run_id: status} for the federation, or None if the CLI call failed."""
    try:
        out = subprocess.run(
            ["uvx", "flwr", "federation", "list", SUPERLINK,
             "--federation", FEDERATION, "--format", "json"],
            capture_output=True, text=True, timeout=60, check=False,
        ).stdout
        runs = json.loads(out)["federation"]["runs"]
        return {str(run["run_id"]): str(run["status"]) for run in runs}
    except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, TypeError) as err:
        print(f"[tail] could not list runs: {err}", flush=True)
        return None


def _append(line: str) -> bool:
    """Append one EVENT line if it is valid and new. Return True if written."""
    try:
        event = json.loads(line[len(EVENT_PREFIX):])
    except json.JSONDecodeError:
        return False
    if not isinstance(event, dict) or not REQUIRED_FIELDS <= event.keys():
        return False
    key = _key(event)
    with _lock:
        if key in _written:
            return False
        with EVENTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
        _written.add(key)
    return True


def _follow(run_id: str) -> None:
    """Stream one run's log until the run is finished and the stream has ended."""
    count = 0
    while True:
        proc = subprocess.Popen(
            ["uvx", "flwr", "log", run_id, SUPERLINK, "--stream"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors="replace",
        )
        _procs[run_id] = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith(EVENT_PREFIX) and _append(line.strip()):
                count += 1
        proc.wait()
        _procs.pop(run_id, None)
        if _status.get(run_id, "").startswith("finished"):
            break
        time.sleep(REATTACH_S)  # stream dropped while the run is still going
    print(f"[tail] run {run_id} done, {count} new events", flush=True)


def _attach(run_id: str, status: str) -> None:
    print(f"[tail] attached to run {run_id} ({status})", flush=True)
    threading.Thread(target=_follow, args=(run_id,), daemon=True).start()


def _stop_stale_streams() -> None:
    now = time.monotonic()
    for run_id, status in _status.items():
        if not status.startswith("finished"):
            continue
        first_seen = _finished_seen_at.setdefault(run_id, now)
        proc = _procs.get(run_id)
        if proc is not None and now - first_seen > KILL_AFTER_FINISH_S:
            proc.terminate()


def main() -> None:
    _load_written()
    runs = None
    while runs is None:
        runs = _list_runs()
        if runs is None:
            time.sleep(POLL_S)
    _status.update(runs)
    # Runs that already finished before we started are history; skip them.
    known = {rid for rid, status in runs.items() if status.startswith("finished")}
    print(
        f"[tail] watching {FEDERATION}, skipping {len(known)} finished runs, "
        f"writing to {EVENTS_FILE.resolve()}",
        flush=True,
    )
    while True:
        for run_id, status in runs.items():
            if run_id not in known:
                known.add(run_id)
                _attach(run_id, status)
        _stop_stale_streams()
        time.sleep(POLL_S)
        runs = _list_runs() or {}
        _status.update(runs)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[tail] stopped", flush=True)
