"""Stateless JSON transports for independently scoped model decisions.

These adapters make one transport call per ``complete``.  The caller owns all
conversation state and must supply only the current actor's permitted context.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import subprocess
import tempfile
from typing import Protocol
import urllib.request


MAX_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass
class Completion:
    data: dict
    model: str | None = None
    usage: dict | None = None


class Provider(Protocol):
    def complete(self, task: str, payload: dict, schema: dict) -> Completion: ...


class ProviderError(RuntimeError):
    """A transport failure whose public message never includes remote content."""

    def __init__(self, error_type: str):
        self.error_type = error_type
        super().__init__(f"Provider request failed ({error_type}).")


def _json_object(raw: bytes | str) -> dict:
    try:
        def reject_constant(value):
            raise ValueError("Non-finite JSON number")
        result = json.loads(raw, parse_constant=reject_constant)
    except (ValueError, UnicodeError) as exc:
        raise ProviderError(type(exc).__name__) from None
    if not isinstance(result, dict):
        raise ProviderError("InvalidJSONShape")
    return result


def _request_json(task: str, payload: dict, schema: dict) -> bytes:
    if not isinstance(task, str) or not task or not isinstance(payload, dict) or not isinstance(schema, dict):
        raise ProviderError("InvalidRequest")
    try:
        return json.dumps(
            {"task": task, "payload": payload, "schema": schema},
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProviderError(type(exc).__name__) from None


class OpenAIResponsesProvider:
    """OpenAI Responses structured outputs, with no stored conversation.

    A model must be explicitly selected. The key is read only during explicit
    construction and is used only in the HTTP authorization header. No response
    IDs, prior outputs, or actor state are retained by this adapter.
    """

    def __init__(self, model: str, api_key: str | None = None, timeout: float = 120):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("An explicit model is required.")
        key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        if not isinstance(key, str) or not key.strip():
            raise ProviderError("MissingAPIKey")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number.")
        self.model = model
        self._api_key = key
        self.timeout = float(timeout)

    def complete(self, task: str, payload: dict, schema: dict) -> Completion:
        # Round-trip through the shared wire format to validate serializability.
        wire = _json_object(_request_json(task, payload, schema))
        instructions = wire["payload"].pop("_instructions", "")
        if not isinstance(instructions, str):
            raise ProviderError("InvalidRequest")
        body = {
            "model": self.model,
            "store": False,
            "instructions": (
                "Complete the supplied task using only the supplied payload. "
                "Return a JSON object matching the supplied response schema.\n" + instructions
            ),
            "input": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {"task": wire["task"], "payload": wire["payload"]},
                        ensure_ascii=False, allow_nan=False,
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "customer_world_decision",
                    "schema": wire["schema"],
                    "strict": True,
                },
            },
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except Exception as exc:
            # HTTP error bodies and URLs may contain private data; never echo them.
            raise ProviderError(type(exc).__name__) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("ResponseTooLarge")
        result = _json_object(raw)
        if result.get("status") not in (None, "completed") or result.get("error"):
            raise ProviderError("IncompleteResponse")
        output = result.get("output")
        if not isinstance(output, list):
            raise ProviderError("InvalidResponse")
        texts = []
        for item in output:
            if not isinstance(item, dict):
                raise ProviderError("InvalidResponse")
            if item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                raise ProviderError("InvalidResponse")
            for part in content:
                if not isinstance(part, dict):
                    raise ProviderError("InvalidResponse")
                if part.get("type") == "refusal":
                    raise ProviderError("ResponseRefusal")
                if part.get("type") == "output_text":
                    if not isinstance(part.get("text"), str):
                        raise ProviderError("InvalidResponse")
                    texts.append(part["text"])
        if not texts:
            raise ProviderError("MissingOutputText")
        data = _json_object("".join(texts))
        returned_model = result.get("model")
        usage = result.get("usage")
        return Completion(
            data=data,
            model=returned_model if isinstance(returned_model, str) else self.model,
            usage=usage if isinstance(usage, dict) else None,
        )


class CommandProvider:
    """Run one fresh command per decision using an explicit JSON protocol.

    The command receives ``{task, payload, schema}`` on stdin and must emit one
    JSON object on stdout. stderr is discarded. Only a fixed minimal environment
    is inherited; credentials must be arranged explicitly by the command's owner.
    This process boundary is not an operating-system sandbox.
    """

    def __init__(self, argv: list[str], timeout: float = 120):
        if not isinstance(argv, list) or not argv or any(
            not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv
        ):
            raise ValueError("argv must be a nonempty list of nonempty strings.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number.")
        self.argv = list(argv)
        self.timeout = float(timeout)

    def complete(self, task: str, payload: dict, schema: dict) -> Completion:
        wire = _request_json(task, payload, schema)
        # A file bounds memory use even if a faulty command writes a large output.
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen(
                    self.argv,
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                    start_new_session=True,
                )
                try:
                    process.communicate(input=wire, timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    # Kill the fresh process group, including ordinary child commands.
                    import signal
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                    raise
                if process.returncode:
                    raise ProviderError("CalledProcessError")
                if output.tell() > MAX_RESPONSE_BYTES:
                    raise ProviderError("ResponseTooLarge")
                output.seek(0)
                raw = output.read(MAX_RESPONSE_BYTES + 1)
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError(type(exc).__name__) from None
        return Completion(data=_json_object(raw))
