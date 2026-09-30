#!/usr/bin/env python3
"""Two-Key · Pharmacy Node skill server (runs on Jolin's Mac).

    POST /skill   {"skill": "place_drug_A", "order_id": "001"}
        -> asks the pharmacist in THIS terminal: y/n
        -> y: drives the gripper through bridge (port 8764), returns {"status": "done", ...}
        -> n / no answer: arm does not move, returns {"status": "error", ...}
    GET  /health  -> {"ok": true}

Only the Python standard library is used. Every decision is logged locally in
pharmacy_log.jsonl (the hospital's log stays on the hospital node).
"""
import json
import select
import sys
import termios
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST, PORT = "127.0.0.1", 8765          # the address LJ's node calls
BRIDGE_URL = "http://127.0.0.1:8764/gripper"  # bridge_8764.py drives the real gripper
KEY_TIMEOUT_S = 30                      # how long to wait for the pharmacist
LOG_FILE = "pharmacy_log.jsonl"

SKILLS = {
    "place_drug_A":  {"label": "Drug A",   "verified": True},
    "place_drug_AX": {"label": "Drug A-X", "verified": True},
}
# One "dispense" motion: (gripper value 0=closed..100=open, seconds to wait after)
MOTION = [(100, 1.0), (0, 1.5), (100, 1.0)]

GREEN, RED, YELLOW, BOLD, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[0m"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(entry):
    entry["ts"] = now()
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def move_gripper(value):
    body = json.dumps({"gripper": value}).encode()
    req = urllib.request.Request(BRIDGE_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read() or b"{}")


def ask_pharmacist(label, order_id):
    """Returns True (approved), False (declined) or None (no answer in time)."""
    try:
        termios.tcflush(sys.stdin, termios.TCIFLUSH)  # ignore keys pressed earlier
    except Exception:
        pass
    print(f"\n{YELLOW}{BOLD}🔑 PHARMACIST KEY — order {order_id}: dispense {label}?  (y/n){RESET} ",
          end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], KEY_TIMEOUT_S)
    if not ready:
        print(f"\n{RED}No answer in {KEY_TIMEOUT_S}s — not dispensing.{RESET}")
        return None
    answer = sys.stdin.readline().strip().lower()
    return answer in ("y", "yes")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # keep the terminal clean for the pharmacist prompt
        pass

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "skills": list(SKILLS)})
        else:
            self._send(404, {"status": "error", "detail": "not found"})

    def do_POST(self):
        if self.path != "/skill":
            self._send(404, {"status": "error", "detail": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, {"status": "error", "detail": "bad JSON"})
            return

        skill = str(req.get("skill", ""))
        order_id = str(req.get("order_id", "?"))
        spec = SKILLS.get(skill)
        if not spec or not spec.get("verified"):
            print(f"{RED}✗ Unknown / unverified skill: {skill!r} — refused.{RESET}")
            log({"order_id": order_id, "skill": skill, "result": "refused_unknown_skill"})
            self._send(200, {"status": "error", "detail": f"unknown or unverified skill: {skill}"})
            return

        approved = ask_pharmacist(spec["label"], order_id)
        if approved is None:
            log({"order_id": order_id, "skill": skill, "result": "no_pharmacist_response"})
            self._send(200, {"status": "timeout", "detail": "no pharmacist response"})
            return
        if not approved:
            print(f"{RED}✗ Declined by pharmacist — arm not moved.{RESET}")
            log({"order_id": order_id, "skill": skill, "result": "declined_by_pharmacist"})
            self._send(200, {"status": "rejected", "detail": "pharmacist said no"})
            return

        print(f"{GREEN}✓ Approved — dispensing {spec['label']}…{RESET}", flush=True)
        try:
            for value, wait in MOTION:
                move_gripper(value)
                time.sleep(wait)
        except Exception as e:
            print(f"{RED}✗ Arm error: {e}{RESET}")
            log({"order_id": order_id, "skill": skill, "result": "arm_error", "detail": str(e)})
            self._send(200, {"status": "error", "detail": f"arm error: {e}"})
            return

        print(f"{GREEN}{BOLD}✓ Done. Logged on the hospital node.{RESET}")
        log({"order_id": order_id, "skill": skill, "result": "done"})
        self._send(200, {"status": "done", "detail": f"{spec['label']} dispensed for order {order_id}"})


def main():
    # Single-threaded on purpose: one order at a time, so the y/n prompt never overlaps.
    server = HTTPServer((HOST, PORT), Handler)
    print(f"{BOLD}Two-Key Pharmacy Node{RESET} — listening on http://{HOST}:{PORT}/skill")
    print(f"Skills: {', '.join(SKILLS)} · gripper via {BRIDGE_URL} · Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
