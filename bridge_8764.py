"""HTTP bridge for the hackathon demo: drives the gripper (ID 6) ONLY.

  POST /gripper  {"gripper": 0..100}   0 = closed (raw 1500), 100 = open (raw 2900)
  GET  /health   -> {"ok": true, "raw": <last read Present_Position>}

Only ID 6 is registered on the bus, so the library cannot address IDs 1-5.
All bus I/O happens on the main thread; HTTP handlers only store the latest target.
Registers written (ID 6, volatile SRAM only): Goal_Position, Goal_Velocity, Acceleration, Torque_Enable.
"""
import json
import math
import signal
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lerobot.motors.hiwonder import HiwonderMotorsBus
from lerobot.motors.motors_bus import Motor, MotorNormMode

PORT = "/dev/cu.usbmodem5C4C1241061"
HOST, HTTP_PORT = "127.0.0.1", 8764
GRIPPER = "gripper"
RAW_CLOSED, RAW_OPEN = 1850, 2900  # re-mapped after 14:59 calibration (white gripper range 1334..3151)
GOAL_VELOCITY = 1000  # fixed; never 0 (0 = unlimited speed)
ACCELERATION = 30  # STS units of 100 steps/s^2
MIN_DELTA = 5  # only write when the target moves by >= this many raw counts
LOOP_HZ = 20
MAX_BODY = 1024


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {msg}", flush=True)


def clamp_raw(v: int) -> int:
    return max(RAW_CLOSED, min(RAW_OPEN, int(v)))


def gripper_to_raw(g: float) -> int:
    g = max(0.0, min(100.0, g))
    return clamp_raw(round(RAW_CLOSED + g / 100.0 * (RAW_OPEN - RAW_CLOSED)))


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending: tuple[float, int] | None = None  # latest (gripper, raw) not yet sent; newer overwrites older
        self.last_raw: int | None = None


state = State()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):  # CORS preflight so a browser page can POST JSON
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path != "/health":
            return self._send(404, {"ok": False, "error": "not found"})
        with state.lock:
            raw = state.last_raw
        self._send(200, {"ok": True, "raw": raw})

    def do_POST(self):
        if self.path != "/gripper":
            return self._send(404, {"ok": False, "error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 < length <= MAX_BODY:
                raise ValueError("bad Content-Length")
            payload = json.loads(self.rfile.read(length))
            g = payload["gripper"]
            if isinstance(g, bool) or not isinstance(g, (int, float)) or not math.isfinite(g):
                raise ValueError("gripper must be a finite number")
        except (ValueError, KeyError, TypeError) as e:
            return self._send(400, {"ok": False, "error": str(e) or "malformed request"})
        g = max(0.0, min(100.0, float(g)))
        raw = gripper_to_raw(g)
        with state.lock:
            state.pending = (g, raw)
        self._send(200, {"ok": True, "gripper": g, "raw_target": raw})

    def log_message(self, *args):  # silence default per-request logging
        pass


def main() -> None:
    # SIGTERM behaves like Ctrl-C so the finally block always runs.
    def on_sigterm(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)

    bus = HiwonderMotorsBus(port=PORT, motors={GRIPPER: Motor(6, "hx30hm", MotorNormMode.RANGE_0_100)})
    bus.connect(handshake=False)
    server = None
    torque_touched = False
    orig_vel = orig_acc = None
    try:
        # --- read-only preflight ---
        mode = bus.read("Operating_Mode", GRIPPER, normalize=False)
        torque = bus.read("Torque_Enable", GRIPPER, normalize=False)
        orig_vel = bus.read("Goal_Velocity", GRIPPER, normalize=False)
        orig_acc = bus.read("Acceleration", GRIPPER, normalize=False)
        start = bus.read("Present_Position", GRIPPER, normalize=False, num_retry=2)
        log(f"Preflight ID 6: Operating_Mode={mode} Torque_Enable={torque} "
            f"Goal_Velocity={orig_vel} Acceleration={orig_acc} Present_Position={start}")
        if mode != 0:
            raise RuntimeError(f"Operating_Mode is {mode}, expected 0 (position mode). Aborting before any write.")

        # --- arm without a jump: goal = current position (a hold, not a motion target, so unclamped) ---
        torque_touched = True
        bus.write("Acceleration", GRIPPER, ACCELERATION, normalize=False)
        bus.write("Goal_Velocity", GRIPPER, GOAL_VELOCITY, normalize=False)
        bus.write("Goal_Position", GRIPPER, start, normalize=False)
        bus.write("Torque_Enable", GRIPPER, 1, normalize=False)
        last_written = start
        with state.lock:
            state.last_raw = start
        log(f"Torque ON (ID 6), holding at {start}")

        server = ThreadingHTTPServer((HOST, HTTP_PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log(f"Listening on http://{HOST}:{HTTP_PORT}  (Ctrl-C to stop)")

        period = 1.0 / LOOP_HZ
        next_tick = time.monotonic()
        while True:
            try:
                pos = bus.read("Present_Position", GRIPPER, normalize=False, num_retry=1)
                with state.lock:
                    state.last_raw = pos
            except Exception as e:
                log(f"read failed: {type(e).__name__}: {e}")

            with state.lock:
                pending, state.pending = state.pending, None
            if pending is not None:
                g, raw = pending
                if abs(raw - last_written) >= MIN_DELTA:
                    try:
                        bus.write("Goal_Position", GRIPPER, clamp_raw(raw), normalize=False)
                        last_written = raw
                        log(f"write gripper={g:.1f} raw={raw}")
                    except Exception as e:
                        log(f"write failed (gripper={g:.1f} raw={raw}): {type(e).__name__}: {e}")
                        with state.lock:  # retry next tick unless a newer target arrived
                            if state.pending is None:
                                state.pending = pending

            next_tick += period
            time.sleep(max(0.0, next_tick - time.monotonic()))
            if time.monotonic() - next_tick > period:  # fell behind; don't burst to catch up
                next_tick = time.monotonic()

    except KeyboardInterrupt:
        log("Ctrl-C: shutting down.")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if torque_touched:
            for attempt in range(5):
                try:
                    bus.write("Torque_Enable", GRIPPER, 0, normalize=False, num_retry=2)
                    log("Torque OFF (ID 6)")
                    break
                except Exception as e:
                    log(f"Torque-off attempt {attempt + 1} failed: {type(e).__name__}: {e}")
            else:
                log("!!! COULD NOT DISABLE TORQUE ON ID 6 - cut 12V power !!!")
            try:
                if orig_vel is not None:
                    bus.write("Goal_Velocity", GRIPPER, orig_vel, normalize=False)
                if orig_acc is not None:
                    bus.write("Acceleration", GRIPPER, orig_acc, normalize=False)
                log(f"Restored Goal_Velocity={orig_vel}, Acceleration={orig_acc}")
            except Exception as e:
                log(f"Restore failed (harmless, SRAM resets on power cycle): {type(e).__name__}: {e}")
        bus.disconnect(disable_torque=False)  # default would write torque-off to every motor; skip it
        log("Port closed")


if __name__ == "__main__":
    main()
