---
tags: [agentapp, robotics, safety, healthcare]
dataset: []
framework: []
---

# Flower Hand

**Demo:** a human teaches the arm by hand — then it picks up the medication (and the Flower socks 🧦).

https://github.com/user-attachments/assets/fb4749fc-c1d7-4dc2-8a88-4e688ba2e261

An independent two-key check for AI-driven hospital robots.

A cloud Planner proposes which robot skill to run for a medication order. Before the
arm moves, two independent keys must turn: a rule-based **Safety** check on a separate
machine, and a human **pharmacist** on the Pharmacy machine. Every step is
**fail-closed**: no answer, an unknown answer, or an error means the arm does not move.

Authors: LJ and Jolin.

## The three nodes

| Node | Where | Role |
|---|---|---|
| **Coordinator** (SuperLink) | Flower SuperGrid | Parses the order, runs the Planner (Endeavor `flwrlabs/endeavor-1.0`, 12 s hard timeout, deterministic label-match fallback), then asks Safety and Pharmacy in turn. |
| **Safety Node** | Laptop 1 | Deterministic rules: skill exists, is verified, and its label equals the ordered drug. Replies `ok` or `block`. No reply within 20 s → refused. |
| **Pharmacy Node** | Laptop 2 | Forwards the approved skill to the local robot bridge (`POST http://localhost:8765/skill` with `skill` and `order_id`). The bridge asks the pharmacist y/n and moves the arm. Only `{"status":"done"}` counts as success. |

The SuperNodes must be named exactly `Safety Node` and `Pharmacy Node`.

Skills live in `agent/skills.py`:

```python
SKILLS = {"place_drug_A": {"label": "Drug A", "verified": True},
          "place_drug_AX": {"label": "Drug A-X", "verified": True}}
```

## Demo flow

Send these as chat messages to the app on the federation:

| Message | What happens | Final banner |
|---|---|---|
| `Bed 12 needs Drug A` | Planner picks `place_drug_A` → Safety ok → pharmacist presses **y** → arm moves | Dispensed. Both keys turned. |
| `Bed 12 needs Drug A [inject]` | Planner is forced to the look-alike `place_drug_AX` → Safety blocks (label mismatch) | Stopped. Pharmacist not asked, arm not moved. |
| `Bed 12 needs Drug A` with Safety Node offline | No Safety reply → fail-closed | Refused — fail-closed. No second key, no motion. |
| `Bed 12 needs Drug Z` | No skill matches → escalate | Escalated to pharmacist. |

Pharmacist answers: **n** → "Stopped by pharmacist. Arm not moved."; no answer in 30 s →
"No human key. Arm not moved."

## Build and run

```bash
uv sync
uv run flwr build
```

## Live dashboard

The coordinator prints one line per step, `EVENT {json}`, with fields
`ts / order_id / run_id / node / status / text`. `tools/tail_events.py` follows every
new run in the federation and appends those events to `./events.jsonl`;
`dashboard.html` reads that file.

From the project root, in two terminals:

```bash
python3 tools/tail_events.py          # follows new runs, writes ./events.jsonl
python3 -m http.server 8000           # serves dashboard.html and events.jsonl
```

Open <http://localhost:8000/dashboard.html>, then send an order in Flower Chat.
`dashboard.html` is not part of the Flower Hub package (Hub only accepts code, config
and docs file types); get it from the project repository.
`tail_events.py` uses the `flwr` CLI (`uvx flwr ...`) and must be logged in to
SuperGrid (`uv run flwr login supergrid`). Edit `FEDERATION` at the top of the script
for your own federation.

## Links

- Source code: <https://github.com/jacobuku/flower-hand>

## License

Apache-2.0, see `LICENSE`.
