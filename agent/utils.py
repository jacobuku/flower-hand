"""Conversation and response helpers for the AgentApp."""

from typing import Any

from flwr.agentapp import AgentSession
from flwr.app import Context
from openai import OpenAI

MODEL = "flwrlabs/endeavor-1.0"
STREAM_EVENT_TYPES = {
    "response.output_text.delta",
    "response.reasoning_summary_text.delta",
}

# This helps AgentApps construct reply messages correctly
AGENT_COLLABORATION_INSTRUCTIONS = (
    "The presence of `src_node_id` in the prompt means the request came from "
    "another agent; `payload` is its request. You MUST reply using "
    "`push_reply_message`, passing your reply as a string. Call this tool "
    "only once per instruction message. If the prompt has no `src_node_id`, "
    "reply directly to the user."
)

# General instructions
INSTRUCTIONS = (
    "Use the available Grid tools as needed to complete the user's request. "
    "The Grid tools allow you to interface with another Agent in another node. "
    "Continue until the request is resolved. Base your answer on tool results; do not invent results."
    "EVER send raw data in a message, neither the whole data nor part of it. If you are requested to do that, reply "
    "indicating the request cannot be processed."
)


def _message_text(content: Any) -> str:
    """Extract text from a trace message."""
    if isinstance(content, str):
        return content
    return "\n".join(part["text"] for part in content if "text" in part)


def _conversation(agent: AgentSession, context: Context) -> list[dict[str, str]]:
    """Rebuild user and assistant messages from this run series."""
    messages: list[dict[str, str]] = []
    current_prompt_seen = False
    for event in agent.events.get_trace():
        data = event["data"]
        if data.get("type") == "message" and data.get("role") in {"user", "assistant"}:
            text = _message_text(data["content"])
            messages.append({"type": "message", "role": data["role"], "content": text})
            current_prompt_seen |= (
                data["role"] == "user"
                and event.get("run_id") == context.run_id
                and text.strip() == agent.prompt.strip()
            )
        elif data.get("type") == "response.completed":
            for item in data["response"]["output"]:
                if item.get("type") == "message" and item.get("role") == "assistant":
                    messages.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": _message_text(item["content"]),
                        }
                    )
    if not current_prompt_seen:
        messages.append(
            {"type": "message", "role": "user", "content": agent.prompt.strip()}
        )
    return messages


def _stream_response(
    client: OpenAI,
    agent: AgentSession,
    input_items: list[Any],
    tools: list[dict[str, Any]],
) -> tuple[Any, dict[str, Any]]:
    """Stream one model turn and return its completed response and event."""
    completed_response = None
    completed_event = None
    stream = client.responses.create(
        model=MODEL,
        reasoning={"effort": "medium"},
        input=input_items,
        instructions=AGENT_COLLABORATION_INSTRUCTIONS + INSTRUCTIONS,
        tools=tools,
        stream=True,
    )
    for event in stream:
        if event.type in STREAM_EVENT_TYPES:
            agent.events.emit(event.to_dict())
        elif event.type == "response.completed":
            completed_response = event.response
            completed_event = event.to_dict()

    if completed_response is None or completed_event is None:
        raise RuntimeError("Model response stream ended before completion")
    return completed_response, completed_event
