"""Transport fixtures only: these do not establish customer behavior."""

import io
import json
import os
import sys
import unittest
from unittest.mock import patch

from customer_world.providers import (
    CommandProvider, Completion, OpenAIResponsesProvider, ProviderError,
)


SCHEMA = {
    "type": "object", "properties": {"fixture": {"type": "string"}},
    "required": ["fixture"], "additionalProperties": False,
}


def response_bytes(data=None, **extra):
    result = {
        "id": "transport-fixture-response", "status": "completed", "model": "selected-model",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(data or {"fixture": "transport"})}]}],
        "usage": {"input_tokens": 5, "output_tokens": 4},
    }
    result.update(extra)
    return json.dumps(result).encode()


class OpenAITransportTests(unittest.TestCase):
    def test_one_stateless_structured_call_per_decision(self):
        calls = []

        def fake_open(request, *, timeout):
            calls.append((request, timeout))
            return io.BytesIO(response_bytes())

        first = {"actor": "actor-a", "_instructions": "Use only this actor's context."}
        with patch("urllib.request.urlopen", side_effect=fake_open):
            provider = OpenAIResponsesProvider("selected-model", api_key="private-key", timeout=9)
            result = provider.complete("turn", first, SCHEMA)
            provider.complete("world", {"actor": "actor-b"}, SCHEMA)
        self.assertEqual(len(calls), 2)
        self.assertIsInstance(result, Completion)
        self.assertEqual(result.data, {"fixture": "transport"})
        self.assertEqual(result.usage["input_tokens"], 5)
        self.assertEqual(result.model, "selected-model")
        self.assertIn("_instructions", first, "the input must not be mutated")
        for request, timeout in calls:
            body = json.loads(request.data)
            self.assertEqual(timeout, 9)
            self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(body["model"], "selected-model")
            self.assertIs(body["store"], False)
            self.assertEqual(body["text"]["format"]["type"], "json_schema")
            self.assertTrue(body["text"]["format"]["strict"])
            self.assertEqual(body["text"]["format"]["schema"], SCHEMA)
            self.assertNotIn("previous_response_id", body)
            self.assertNotIn("conversation", body)
            self.assertNotIn("private-key", request.data.decode())
            self.assertEqual(request.get_header("Authorization"), "Bearer private-key")
        first_body, second_body = [json.loads(call[0].data) for call in calls]
        self.assertIn("Use only this actor", first_body["instructions"])
        self.assertNotIn("_instructions", first_body["input"][0]["content"])
        self.assertNotIn("actor-a", json.dumps(second_body))
        self.assertNotIn("transport-fixture-response", json.dumps(second_body))

    def test_credentials_read_only_on_explicit_construction(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env-one"}, clear=True), patch("urllib.request.urlopen") as transport:
            provider = OpenAIResponsesProvider("selected-model")
            transport.assert_not_called()
            os.environ["OPENAI_API_KEY"] = "env-two"
            transport.return_value = io.BytesIO(response_bytes())
            provider.complete("profile", {}, SCHEMA)
            self.assertEqual(transport.call_args.args[0].get_header("Authorization"), "Bearer env-one")
        self.assertNotIn("env-one", repr(provider))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ProviderError, "MissingAPIKey"):
                OpenAIResponsesProvider("selected-model")
        with self.assertRaises(ValueError):
            OpenAIResponsesProvider("", api_key="private-key")
        with self.assertRaises(ValueError):
            OpenAIResponsesProvider("selected-model", api_key="private-key", timeout=float("inf"))

    def test_transport_error_preserves_type_without_secret_content(self):
        with patch("urllib.request.urlopen", side_effect=OSError("private-key /private/path")):
            with self.assertRaises(ProviderError) as caught:
                OpenAIResponsesProvider("selected-model", api_key="private-key").complete("turn", {}, SCHEMA)
        self.assertEqual(caught.exception.error_type, "OSError")
        self.assertNotIn("private-key", str(caught.exception))
        self.assertNotIn("/private/path", str(caught.exception))

    def test_refusal_incomplete_and_invalid_json_are_failures(self):
        variants = [
            (response_bytes(status="incomplete", incomplete_details={"reason": "max_output_tokens"}), "IncompleteResponse"),
            (response_bytes(output=[{"type": "message", "content": [{"type": "refusal", "refusal": "private diagnostic"}]}]), "ResponseRefusal"),
            (response_bytes(output=[]), "MissingOutputText"),
            (response_bytes(output=[{"type": "message", "content": [{"type": "output_text", "text": "[]"}]}]), "InvalidJSONShape"),
            (b"private invalid json", "JSONDecodeError"),
        ]
        for raw, expected in variants:
            with self.subTest(expected=expected), patch("urllib.request.urlopen", return_value=io.BytesIO(raw)):
                with self.assertRaises(ProviderError) as caught:
                    OpenAIResponsesProvider("selected-model", api_key="key").complete("turn", {}, SCHEMA)
                self.assertEqual(caught.exception.error_type, expected)
                self.assertNotIn("private", str(caught.exception))


class CommandTransportTests(unittest.TestCase):
    def test_json_protocol_fresh_process_literal_argv_and_minimal_environment(self):
        script = (
            "import json,os,sys; request=json.load(sys.stdin); "
            "print(json.dumps({'request':request,'env':dict(os.environ),"
            "'argument':sys.argv[1],'pid':os.getpid()}))"
        )
        literal = "$(this-must-not-run); echo no"
        provider = CommandProvider([sys.executable, "-c", script, literal])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private-env-key", "ACTOR_CONTEXT": "other-actor"}):
            first = provider.complete("turn", {"actor": "actor-a"}, SCHEMA)
            second = provider.complete("turn", {"actor": "actor-b"}, SCHEMA)
        self.assertNotEqual(first.data["pid"], second.data["pid"])
        self.assertEqual(first.data["argument"], literal)
        self.assertEqual(first.data["request"], {"task": "turn", "payload": {"actor": "actor-a"}, "schema": SCHEMA})
        self.assertEqual(second.data["request"]["payload"], {"actor": "actor-b"})
        self.assertNotIn("OPENAI_API_KEY", first.data["env"])
        self.assertNotIn("ACTOR_CONTEXT", first.data["env"])
        self.assertNotIn("HOME", first.data["env"])
        self.assertIsNone(first.model)

    def test_nonzero_stderr_and_invalid_stdout_never_echo_secrets(self):
        scripts = [
            ("import sys; print('secret-key',file=sys.stderr); sys.exit(7)", "CalledProcessError"),
            ("print('secret-key')", "JSONDecodeError"),
            ("print('[]')", "InvalidJSONShape"),
            ("print('{} {}')", "JSONDecodeError"),
            ("print('{\"value\":NaN}')", "ValueError"),
        ]
        for script, expected in scripts:
            with self.subTest(expected=expected), self.assertRaises(ProviderError) as caught:
                CommandProvider([sys.executable, "-c", script]).complete("turn", {}, SCHEMA)
            self.assertEqual(caught.exception.error_type, expected)
            self.assertNotIn("secret-key", str(caught.exception))

    def test_timeout_and_missing_command_have_sanitized_error_classes(self):
        with self.assertRaises(ProviderError) as caught:
            CommandProvider([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.03).complete("turn", {}, SCHEMA)
        self.assertEqual(caught.exception.error_type, "TimeoutExpired")
        with self.assertRaises(ProviderError) as caught:
            CommandProvider(["/nonexistent-private-path/command"]).complete("turn", {}, SCHEMA)
        self.assertEqual(caught.exception.error_type, "FileNotFoundError")
        self.assertNotIn("nonexistent-private-path", str(caught.exception))

    def test_invalid_transport_configuration_and_non_json_payload_rejected(self):
        for argv in ([], "command", [""], ["command", "\x00"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                CommandProvider(argv)
        for timeout in (0, -1, float("nan"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                CommandProvider([sys.executable], timeout=timeout)
        with self.assertRaises(ProviderError) as caught:
            CommandProvider([sys.executable]).complete("turn", {"bad": float("nan")}, SCHEMA)
        self.assertEqual(caught.exception.error_type, "ValueError")


if __name__ == "__main__":
    unittest.main()
