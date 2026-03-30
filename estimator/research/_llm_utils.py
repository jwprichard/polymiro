"""Internal LLM utility: Ollama JSON call with retry on parse failure."""

import json

import ollama


def ollama_json_call(
    messages: list[dict],
    model: str,
    max_retries: int = 2,
) -> dict:
    """Call Ollama with format="json" and return a parsed dict.

    On JSON parse failure the function appends a correction message and
    retries up to `max_retries` additional times.  If all attempts are
    exhausted a ValueError is raised.
    """
    correction_message = {
        "role": "user",
        "content": (
            "Your response was not valid JSON. "
            "Please respond with only a valid JSON object, no markdown or prose."
        ),
    }

    # Work on a local copy so the caller's list is not mutated.
    current_messages = list(messages)

    attempts = max_retries + 1  # initial attempt + retries
    for attempt in range(attempts):
        response = ollama.chat(model=model, messages=current_messages, format="json")
        raw_content = response["message"]["content"]

        try:
            return json.loads(raw_content)
        except (json.JSONDecodeError, ValueError):
            if attempt < attempts - 1:
                # Append correction and retry.
                current_messages = current_messages + [correction_message]
                continue

    raise ValueError("LLM failed to return valid JSON after retries")
