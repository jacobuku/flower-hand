"""Two-Key AgentApp: a planner proposes a skill, an independent Safety node must
approve it before the Pharmacy node executes it on the robot."""

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import requests
from flwr.agentapp import AgentApp, AgentSession
from flwr.app import Context
from flwr.common.constant import SUPERLINK_NODE_ID
from openai import OpenAI

from agent.skills import SKILLS
from agent.utils import MODEL

SAFETY_NODE_NAME = "Safety Node"
PHARMACY_NODE_NAME = "Pharmacy Node"
MODEL_TIMEOUT_S = 12  # hard cap on the Planner model call
SAFETY_TIMEOUT_S = 20  # measured round trip 8.4s; node agentapp startup + deterministic check
PHARMACY_TIMEOUT_S = 70  # ~8s node startup + 40s bridge approval + margin
BRIDGE_URL = "http://localhost:8765/skill"
INJECT_MARKER = "[inject]"
INJECTED_SKILL = "place_drug_AX"

PLANNER_INSTRUCTIONS = (
    "You are a pharmacy robot planner. Given a medication order, choose exactly "
    "one skill whose label matches the ordered drug. Reply with the skill key "
    "only, no other text. If no skill matches, reply NONE.\n"
    "Skills:\n"
    + "\n".join(f"- {key}: {info['label']}" for key, info in SKILLS.items())
)

app = AgentApp()


@app.main()
def main(agent: AgentSession, context: Context) -> None:
    """Route to the coordinator or node role."""
    if context.node_id == SUPERLINK_NODE_ID:
        _coordinator(agent, context)
    else:
        _node(agent)


# ---------------------------------------------------------------- coordinator


class _Events:
    """Print EVENT lines for the dashboard."""

    def __init__(self, order_id: str, run_id: int) -> None:
        self.order_id = order_id
        self.run_id = run_id

    def __call__(self, node: str, status: str, text: str) -> None:
        event = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "order_id": self.order_id,
            "run_id": str(self.run_id),
            "node": node,
            "status": status,
            "text": text,
        }
        print("EVENT " + json.dumps(event), flush=True)


def _grid(agent: AgentSession, name: str, **arguments: Any) -> dict[str, Any]:
    """Call one Grid tool directly and decode its output."""
    output_item = agent.grid.call(
        {"name": name, "call_id": f"call_{uuid.uuid4().hex}", "arguments": arguments}
    )
    return json.loads(output_item["output"])


def _say(agent: AgentSession, text: str) -> None:
    """Show one human-readable line in Flower Chat and end the response."""
    agent.events.emit({"type": "response.output_text.delta", "delta": text})
    agent.events.emit({"type": "response.completed"})


def _parse_order(prompt: str) -> str:
    """Extract the drug name from e.g. 'Bed 12 needs Drug A'."""
    text = prompt.replace(INJECT_MARKER, "").strip()
    match = re.search(r"needs\s+(.+?)\s*\.?$", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else text


def _ask_model(order_drug: str) -> str:
    """One streamed model call; return the raw text answer."""
    client = OpenAI(
        base_url=os.environ["FLWR_RUNTIME_BASE_URL"],
        api_key=os.environ["FLWR_RUNTIME_API_KEY"],
        max_retries=0,
        timeout=MODEL_TIMEOUT_S,
    )
    stream = client.responses.create(
        model=MODEL,
        instructions=PLANNER_INSTRUCTIONS,
        input=f"Order: {order_drug}",
        stream=True,
    )
    for item in stream:
        if item.type == "response.completed":
            return item.response.output_text.strip().strip("`\"'")
    raise RuntimeError("stream ended before completion")


def _rule_plan(order_drug: str) -> str:
    """Deterministic fallback: the skill whose label is exactly the ordered drug."""
    for key, info in SKILLS.items():
        if info["label"] == order_drug:
            return key
    return "NONE"


def _plan(order_drug: str, inject: bool) -> tuple[str, str]:
    """Return (skill key or NONE, how it was chosen). Never blocks > MODEL_TIMEOUT_S."""
    if inject:
        return INJECTED_SKILL, "look-alike error injected"
    start = time.monotonic()
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["choice"] = _ask_model(order_drug)
        except Exception as err:  # pylint: disable=broad-exception-caught
            box["error"] = err

    # httpx timeouts are per read, so a slow stream could still run long;
    # the thread join is the hard wall-clock cap. Daemon so it can't block exit.
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(MODEL_TIMEOUT_S)
    elapsed = time.monotonic() - start

    if worker.is_alive():
        reason = "model timed out"
    elif "error" in box:
        reason = f"model error: {type(box['error']).__name__}"
    elif box.get("choice") in SKILLS or box.get("choice") == "NONE":
        reason = None
    else:
        reason = "model returned unknown key"

    if reason is None:
        skill, how = box["choice"], f"Endeavor, {elapsed:.1f}s"
    else:
        skill, how = _rule_plan(order_drug), f"rule fallback, {reason}"
    return skill, how


def _find_nodes(agent: AgentSession) -> dict[str, str]:
    """Map node name to node ID."""
    nodes = _grid(agent, "get_nodes", sample_size=None)["nodes"]
    return {node["name"]: node["id"] for node in nodes if node["name"]}


def _ask(
    agent: AgentSession, node_id: str, payload: dict[str, Any], timeout: float
) -> tuple[dict[str, Any] | None, float]:
    """Push one request and wait for its reply.

    Returns (reply, seconds from push to reply); reply is None on timeout.
    """
    start = time.monotonic()
    pushed = _grid(
        agent,
        "push_messages",
        messages=[
            {
                "dst_node_id": node_id,
                "payload": json.dumps(payload),
                "reply_to_message_id": None,
            }
        ],
    )["results"][0]
    if pushed["message_id"] is None:
        raise RuntimeError(f"push rejected: {pushed['error']}")
    pulled = _grid(
        agent, "pull_messages", message_ids=[pushed["message_id"]], timeout=timeout
    )
    elapsed = time.monotonic() - start
    if not pulled["messages"]:
        return None, elapsed
    reply = pulled["messages"][0]
    if reply["error"] is not None:
        return {"status": "error", "detail": reply["error"]}, elapsed
    try:
        return json.loads(reply["payload"]), elapsed
    except (TypeError, json.JSONDecodeError):
        return {"status": "error", "detail": f"unparseable reply: {reply['payload']}"}, elapsed


def _coordinator(agent: AgentSession, context: Context) -> None:
    order_id = f"ord-{uuid.uuid4().hex[:8]}"
    event = _Events(order_id, context.run_id)
    try:
        status, text, chat = _run_order(agent, event, order_id)
    except Exception as err:  # pylint: disable=broad-exception-caught
        status, text = "error", f"Coordinator error: {err}"
        chat = f"Order {order_id} failed with an internal error; nothing was dispensed."
    # Exactly one final coordinator event per run drives the dashboard banner.
    event("coordinator", status, text)
    _say(agent, chat)


def _run_order(agent: AgentSession, event: _Events, order_id: str) -> tuple[str, str, str]:
    """Run one order; return the final (status, banner text, chat text)."""
    prompt = agent.prompt
    inject = INJECT_MARKER in prompt
    order_drug = _parse_order(prompt)
    event("coordinator", "reset", "New order started.")
    event("order", "info", f"Order received: {order_drug}.")

    # Key 0: planner proposes
    skill, how = _plan(order_drug, inject)
    event("order", "ok", f"Order parsed: {order_drug}.")
    if skill == "NONE":
        event("planner", "escalate", f"Planner found no matching skill ({how}).")
        event("pharmacist", "escalate", "Escalated to a human pharmacist.")
        return (
            "escalate",
            "Escalated to pharmacist.",
            f"No safe skill for '{order_drug}'. Escalated to a human pharmacist.",
        )
    plan_label = SKILLS[skill]["label"]
    event("planner", "ok", f"Planner proposed {skill} ({how}).")

    nodes = _find_nodes(agent)
    fail_closed = (
        "block",
        "Refused — fail-closed. No second key, no motion.",
    )

    # Key 1: Safety must approve (fail-closed)
    safety_id = nodes.get(SAFETY_NODE_NAME)
    if safety_id is None:
        event("safety", "block", "Safety Node offline; blocked by default.")
        return (*fail_closed, "Safety Node is unavailable, so the order was blocked.")
    event("safety", "pending", f"Asking Safety to verify {skill}.")
    verdict, elapsed = _ask(
        agent,
        safety_id,
        {"task": "verify", "order": order_drug, "skill": skill},
        SAFETY_TIMEOUT_S,
    )
    if verdict is None:
        event("safety", "timeout", f"Safety gave no answer after {elapsed:.1f}s; blocked.")
        return (
            *fail_closed,
            f"Safety check timed out after {elapsed:.1f}s, so the order was blocked.",
        )
    if verdict.get("verdict") != "ok":
        reason = verdict.get("reason") or verdict.get("detail") or "unknown reason"
        event(
            "safety",
            "block",
            f"Order {order_drug} ≠ Plan {plan_label} ✗ {reason} ({elapsed:.1f}s)",
        )
        return (
            "block",
            "Stopped. Pharmacist not asked, arm not moved.",
            f"Blocked by Safety: {reason}. Nothing was dispensed.",
        )
    event("safety", "ok", f"Order {order_drug} = Plan {plan_label} ✓ ({elapsed:.1f}s)")

    # Key 2: Pharmacy executes
    pharmacy_id = nodes.get(PHARMACY_NODE_NAME)
    if pharmacy_id is None:
        event("pharmacy", "error", "Pharmacy Node offline.")
        return (
            "error",
            "Pharmacy offline. Safety approved, arm not moved.",
            "Approved, but the Pharmacy Node is unavailable.",
        )
    event("pharmacist", "pending", "Waiting for pharmacist key (y/n)")
    event("pharmacy", "pending", f"Robot executing {skill}.")
    result, elapsed = _ask(
        agent,
        pharmacy_id,
        {"task": "execute", "skill": skill, "order_id": order_id},
        PHARMACY_TIMEOUT_S,
    )
    if result is None:
        event("pharmacy", "timeout", f"Robot gave no report after {elapsed:.1f}s.")
        event("pharmacist", "timeout", "No pharmacist response")
        return (
            "error",
            f"Pharmacy timed out after {elapsed:.0f}s. Check the arm.",
            f"Robot execution timed out after {elapsed:.1f}s; please check the robot.",
        )
    # Fail-closed: only an explicit "done" from the bridge counts as success.
    status = result.get("status") if isinstance(result, dict) else None
    detail = result.get("detail") if isinstance(result, dict) else result
    if status == "done":
        event("pharmacist", "ok", "Approved by pharmacist")
        event("pharmacy", "done", f"Robot placed {plan_label} ✓ ({elapsed:.1f}s)")
        return (
            "done",
            "Dispensed. Both keys turned.",
            f"{plan_label} placed for order {order_id}, approved by Safety and pharmacist.",
        )
    if status == "rejected":
        event("pharmacist", "block", "Pharmacist said no")
        event("pharmacy", "block", "Arm not moved")
        return (
            "block",
            "Stopped by pharmacist. Arm not moved.",
            f"The pharmacist rejected {plan_label}; the arm did not move.",
        )
    if status == "timeout":
        event("pharmacist", "timeout", "No pharmacist response")
        event("pharmacy", "block", "Arm not moved")
        return (
            "block",
            "No human key. Arm not moved.",
            "No pharmacist answer in time; the arm did not move.",
        )
    event("pharmacy", "error", f"Robot error ({status}): {detail} ({elapsed:.1f}s)")
    event("pharmacist", "error", "Unknown — pharmacy failed")
    return (
        "error",
        "Pharmacy error. Arm did not complete.",
        f"Robot failed to execute {skill}: {detail}",
    )


# ---------------------------------------------------------------------- nodes


def _verify(payload: dict[str, Any]) -> dict[str, str]:
    """Deterministic safety rule."""
    skill = payload.get("skill")
    info = SKILLS.get(skill) if isinstance(skill, str) else None
    if info is None:
        return {"verdict": "block", "reason": f"unknown skill {skill}"}
    if not info["verified"]:
        return {"verdict": "block", "reason": f"{skill} is not verified"}
    if info["label"] != payload.get("order"):
        return {
            "verdict": "block",
            "reason": f"{skill} places {info['label']}, order is {payload.get('order')}",
        }
    return {"verdict": "ok", "reason": f"{skill} matches the order"}


def _execute(payload: dict[str, Any]) -> str:
    """Forward the skill to the local robot bridge; return its raw reply."""
    try:
        response = requests.post(
            BRIDGE_URL,
            json={"skill": payload.get("skill"), "order_id": payload.get("order_id")},
            timeout=40,
        )
        return response.text
    except requests.RequestException as err:
        return json.dumps({"status": "error", "detail": str(err)})


def _node(agent: AgentSession) -> None:
    try:
        payload = json.loads(json.loads(agent.prompt)["payload"])
        task = payload.get("task")
        if task == "verify":
            reply = json.dumps(_verify(payload))
        elif task == "execute":
            reply = _execute(payload)
        else:
            reply = json.dumps({"status": "error", "detail": f"unknown task {task}"})
    except Exception as err:  # pylint: disable=broad-exception-caught
        reply = json.dumps({"status": "error", "detail": f"bad request: {err}"})
    _grid(agent, "push_reply_message", payload=reply)
