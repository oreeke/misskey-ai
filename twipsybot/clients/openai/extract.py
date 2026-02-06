import json
from typing import Any

from loguru import logger

from ...shared.exceptions import APIConnectionError


def extract_responses_text(response: Any) -> str:
    if isinstance((text := getattr(response, "output_text", None)), str) and text:
        return text
    parts = collect_responses_output_text(getattr(response, "output", None))
    if not parts:
        raise APIConnectionError("Empty output")
    return "".join(parts)


def collect_responses_output_text(output: Any) -> list[str]:
    if not isinstance(output, list):
        raise APIConnectionError("Invalid output type")
    return list(iter_responses_output_text(output))


def iter_responses_output_text(output: list[Any]):
    for item in output:
        if getattr(item, "type", None) != "message":
            continue
        yield from iter_responses_message_content(getattr(item, "content", None))


def iter_responses_message_content(content: Any):
    if not isinstance(content, list):
        return
    for c in content:
        if (
            getattr(c, "type", None) == "output_text"
            and isinstance((t := getattr(c, "text", None)), str)
            and t
        ):
            yield t


def process_chat_completions_response(response: Any, call_type: str) -> str:
    generated_text = response.choices[0].message.content
    if not generated_text:
        raise APIConnectionError()
    logger.debug(
        f"OpenAI API {call_type} call succeeded; output length: {len(generated_text)}"
    )
    return generated_text


def coerce_json_substring(text: str) -> str | None:
    s = text.strip()
    if not s:
        return None
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i = s.find(open_ch)
        j = s.rfind(close_ch)
        if 0 <= i < j:
            return s[i : j + 1]
    return None


def parse_json(text: Any) -> Any:
    if not isinstance(text, str):
        raise ValueError()
    if (sub := coerce_json_substring(text)) is None:
        raise ValueError()
    return json.loads(sub)


def validate_structured_output(
    obj: Any,
    *,
    expected_type: type | tuple[type, ...] | None,
    required_keys: tuple[str, ...] | None,
) -> Any:
    if expected_type is not None and not isinstance(obj, expected_type):
        raise ValueError()
    if required_keys:
        if not isinstance(obj, dict):
            raise ValueError()
        missing = [k for k in required_keys if k not in obj]
        if missing:
            raise ValueError()
    return obj


def build_structured_formats(
    schema: dict[str, Any] | None,
    *,
    name: str,
    strict: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if schema:
        rf = {
            "type": "json_schema",
            "json_schema": {"name": name, "schema": schema, "strict": strict},
        }
        tf = {"type": "json_schema", "name": name, "schema": schema, "strict": strict}
        return rf, tf
    return {"type": "json_object"}, {"type": "json_object"}
