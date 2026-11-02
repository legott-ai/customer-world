"""Queue, receipt, and proof-integrity tests using host-transport fixtures.

No test browser is opened and these fixtures make no customer behavior claims.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

from customer_world.broker import BrokerError, FileBroker


URL = "https://product.example.test/app"


def png_fixture():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b"")


class FileBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.broker = FileBroker(self.root, timeout=1.2)
        self.executor = ThreadPoolExecutor(max_workers=4)
        operator = self.root / "operator"
        operator.mkdir()
        self.screen = operator / "screen.png"
        self.ax = operator / "tree.txt"
        self.screen.write_bytes(png_fixture())
        self.ax.write_text("Transport fixture: window with a visible button.", encoding="utf-8")

    def tearDown(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.temporary.cleanup()

    def start(self, *, actor="actor-a", session="session-a", operation="step-1", action="open", target=URL):
        return self.executor.submit(
            self.broker.request, actor, session, "product", action,
            target=target, expected_url=URL, operation_id=operation,
        )

    def pending(self, count=1):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            paths = list(self.broker.pending_dir.glob("*.json"))
            if len(paths) == count:
                return [json.loads(path.read_text()) for path in paths]
            time.sleep(0.005)
        self.fail("Expected pending transport fixture did not arrive.")

    def respond(self, request, **kwargs):
        values = dict(text="Observed fixture UI.", url=URL, screenshot=self.screen, ax=self.ax)
        values.update(kwargs)
        return self.broker.respond(request["request_id"], **values)

    def complete(self, **kwargs):
        future = self.start(**kwargs)
        pending = self.pending()[0]
        receipt = self.respond(pending)
        self.assertEqual(future.result(timeout=2), receipt)
        return pending, receipt

    def mutate_response(self, request, receipt):
        path = self.broker.response_dir / f"{request['request_id']}.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")

    def replay(self, operation="step-1", **kwargs):
        return self.broker.request("actor-a", "session-a", "product", "open", target=URL, expected_url=URL, operation_id=operation, **kwargs)

    def test_atomic_handoff_and_scoped_real_file_hashes(self):
        request, receipt = self.complete()
        self.assertFalse(list(self.broker.pending_dir.iterdir()))
        self.assertTrue((self.broker.completed_dir / f"{request['request_id']}.json").exists())
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["observation"]["tool_id"], "product")
        self.assertEqual(len(receipt["evidence"]), 2)
        for proof in receipt["evidence"]:
            expected_prefix = f"broker/evidence/actor-a/session-a/{request['request_id']}/"
            self.assertTrue(proof["path"].startswith(expected_prefix))
            path = self.root / proof["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), proof["sha256"])
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertFalse(list(self.broker.root.rglob("*.tmp")))

    def test_stable_operation_replays_and_changed_parameters_are_rejected(self):
        request, receipt = self.complete()
        self.assertEqual(self.replay(), receipt)
        self.assertFalse(list(self.broker.pending_dir.iterdir()))
        with self.assertRaises(BrokerError) as caught:
            self.broker.request("actor-a", "session-a", "product", "click", target="button", expected_url=URL, operation_id="step-1")
        self.assertEqual(caught.exception.code, "operation_conflict")
        with self.assertRaises(BrokerError) as caught:
            self.respond(request)
        self.assertEqual(caught.exception.code, "duplicate_response")

    def test_actor_and_session_are_part_of_operation_identity(self):
        first, _ = self.complete()
        second, _ = self.complete(actor="actor-b", session="session-b")
        third, _ = self.complete(session="session-c")
        self.assertEqual(len({first["request_id"], second["request_id"], third["request_id"]}), 3)

    def test_threads_serialize_physical_ui_requests(self):
        first = self.start()
        request_one = self.pending()[0]
        second = self.start(actor="actor-b", session="session-b")
        time.sleep(0.06)
        self.assertEqual([p["request_id"] for p in self.pending()], [request_one["request_id"]])
        self.respond(request_one)
        first.result(timeout=2)
        deadline = time.monotonic() + 1
        request_two = None
        while time.monotonic() < deadline:
            pending = self.pending()
            if pending[0]["actor_id"] == "actor-b":
                request_two = pending[0]
                break
            time.sleep(0.005)
        self.assertIsNotNone(request_two)
        self.respond(request_two)
        second.result(timeout=2)

    def test_processes_share_the_host_physical_ui_lock(self):
        first = self.start()
        request_one = self.pending()[0]
        script = (
            "import json,sys; from pathlib import Path; "
            "from customer_world.broker import FileBroker; "
            "result=FileBroker(Path(sys.argv[1]),timeout=2).request("
            "'actor-child','session-child','product','open',target=sys.argv[2],operation_id='child-step'); "
            "print(json.dumps({'request_id':result['request_id']}))"
        )
        process = subprocess.Popen([sys.executable, "-c", script, str(self.root), URL], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.08)
            self.assertIsNone(process.poll())
            self.assertEqual([p["request_id"] for p in self.pending()], [request_one["request_id"]])
            self.respond(request_one)
            first.result(timeout=2)
            deadline = time.monotonic() + 1
            request_two = None
            while time.monotonic() < deadline:
                pending = self.pending()
                if pending[0]["actor_id"] == "actor-child":
                    request_two = pending[0]
                    break
                time.sleep(0.005)
            self.assertIsNotNone(request_two)
            self.respond(request_two)
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(json.loads(stdout)["request_id"], request_two["request_id"])
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_concurrent_duplicate_operators_publish_only_one_receipt(self):
        future = self.start()
        request = self.pending()[0]
        replies = [self.executor.submit(self.respond, request) for _ in range(2)]
        successful = []
        failures = []
        for reply in replies:
            try:
                successful.append(reply.result(timeout=2))
            except BrokerError as exc:
                failures.append(exc.code)
        self.assertEqual(len(successful), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn(failures[0], {"duplicate_proof", "duplicate_response"})
        self.assertEqual(future.result(timeout=2), successful[0])

    def test_timeout_preserves_pending_and_resume_reuses_request(self):
        broker = FileBroker(self.root, timeout=0.04)
        with self.assertRaises(BrokerError) as caught:
            broker.request("actor-a", "session-a", "product", "open", target=URL, operation_id="resume")
        self.assertEqual(caught.exception.code, "response_timeout")
        request = self.pending()[0]
        with self.assertRaises(BrokerError) as caught:
            broker.request("actor-b", "session-b", "product", "open", target=URL, operation_id="other")
        self.assertEqual(caught.exception.code, "unresolved_request")
        receipt = self.respond(request)
        self.assertEqual(broker.request("actor-a", "session-a", "product", "open", target=URL, operation_id="resume"), receipt)

    def test_legacy_timeout_reuses_matching_pending_tuple(self):
        broker = FileBroker(self.root, timeout=0.03)
        with self.assertRaises(BrokerError):
            broker.request("actor-a", "session-a", "product", "open", target=URL)
        request = self.pending()[0]
        receipt = self.respond(request)
        self.assertEqual(broker.request("actor-a", "session-a", "product", "open", target=URL), receipt)

    def test_error_receipt_omits_private_diagnostics_and_replays_error(self):
        future = self.start()
        request = self.pending()[0]
        response = self.broker.respond(request["request_id"], text="private-secret", url="https://private.invalid", error="private-key /private/path")
        self.assertEqual(response["status"], "error")
        self.assertNotIn("private", json.dumps(response))
        with self.assertRaises(BrokerError) as caught:
            future.result(timeout=2)
        self.assertEqual(caught.exception.code, "operator_error")
        self.assertNotIn("private", str(caught.exception))
        with self.assertRaises(BrokerError) as caught:
            self.replay()
        self.assertEqual(caught.exception.code, "operator_error")

    def test_finish_requires_context_closure_attestation_and_post_close_proof(self):
        future = self.start(action="finish", target="")
        request = self.pending()[0]
        with self.assertRaises(BrokerError) as caught:
            self.respond(request, url="")
        self.assertEqual(caught.exception.code, "closure_attestation_required")
        with self.assertRaises(BrokerError) as caught:
            self.respond(request, closed=True)
        self.assertEqual(caught.exception.code, "tool_still_open")
        receipt = self.respond(request, text="Actor session context and all its tabs closed.", url="", closed=True)
        self.assertTrue(receipt["observation"]["closed"])
        self.assertEqual(receipt["observation"]["closed_tool_url"], URL)
        self.assertEqual(future.result(timeout=2), receipt)

    def test_finish_can_observe_another_origin_after_context_closure(self):
        future = self.start(action="finish", target="")
        request = self.pending()[0]
        receipt = self.respond(request, url="https://operator.example.test/", closed=True)
        self.assertEqual(future.result(timeout=2), receipt)

    def test_scope_rejects_credentials_other_origins_and_unsafe_actions(self):
        cases = [
            dict(action="shell", target="id"),
            dict(action="open", target="https://outside.example.test/"),
            dict(action="open", target="javascript:alert(1)"),
            dict(action="open", target="https://name:password@product.example.test/app"),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(BrokerError):
                self.broker.request("actor-a", "session-a", "product", expected_url=URL, **case)
        with self.assertRaises(BrokerError):
            self.broker.request("../actor", "session-a", "product", "open", target=URL)
        self.assertFalse(list(self.broker.pending_dir.iterdir()))

    def test_outside_origin_and_nonfinish_closure_are_rejected_before_receipt(self):
        future = self.start()
        request = self.pending()[0]
        with self.assertRaises(BrokerError) as caught:
            self.respond(request, url="https://outside.example.test/")
        self.assertEqual(caught.exception.code, "observation_outside_scope")
        with self.assertRaises(BrokerError) as caught:
            self.respond(request, closed=True)
        self.assertEqual(caught.exception.code, "invalid_closure_attestation")
        self.respond(request, url="https://PRODUCT.example.test:443/next")
        future.result(timeout=2)

    def test_missing_empty_or_fake_proof_does_not_publish_success(self):
        future = self.start()
        request = self.pending()[0]
        bad_png = self.screen.parent / "fake.png"
        bad_png.write_bytes(b"\x89PNG\r\n\x1a\nnot a PNG image")
        empty_ax = self.screen.parent / "empty.txt"
        empty_ax.write_text(" \n")
        for kwargs in (dict(screenshot=None), dict(screenshot=bad_png), dict(ax=empty_ax), dict(text=" ")):
            with self.subTest(kwargs=kwargs), self.assertRaises(BrokerError):
                self.respond(request, **kwargs)
            self.assertFalse((self.broker.response_dir / f"{request['request_id']}.json").exists())
        self.respond(request)
        future.result(timeout=2)

    def test_proof_source_symlink_and_symlinked_ancestor_rejected(self):
        future = self.start()
        request = self.pending()[0]
        direct = self.screen.parent / "link.png"
        direct.symlink_to(self.screen)
        ancestor = self.root / "operator-link"
        ancestor.symlink_to(self.screen.parent, target_is_directory=True)
        for source in (direct, ancestor / self.screen.name):
            with self.subTest(source=source.name), self.assertRaises(BrokerError) as caught:
                self.respond(request, screenshot=source)
            self.assertEqual(caught.exception.code, "symlink_rejected")
        self.respond(request)
        future.result(timeout=2)

    def test_mutated_proof_hash_is_rejected_on_replay(self):
        _, receipt = self.complete()
        path = self.root / receipt["evidence"][0]["path"]
        path.write_bytes(path.read_bytes() + b"mutated")
        with self.assertRaises(BrokerError) as caught:
            self.replay()
        self.assertEqual(caught.exception.code, "evidence_hash_mismatch")

    def test_receipt_identity_cannot_cross_actor_or_session(self):
        request, receipt = self.complete()
        for field, value in (("actor_id", "actor-b"), ("session_id", "session-b"), ("request_id", "0" * 64), ("tool_id", "other")):
            with self.subTest(field=field):
                changed = dict(receipt)
                changed[field] = value
                self.mutate_response(request, changed)
                with self.assertRaises(BrokerError) as caught:
                    self.replay()
                self.assertEqual(caught.exception.code, "response_identity_mismatch")

    def test_success_receipt_rejects_unstructured_extra_payloads(self):
        request, receipt = self.complete()
        receipt["diagnostic"] = "private diagnostic must not enter a successful journal"
        self.mutate_response(request, receipt)
        with self.assertRaises(BrokerError) as caught:
            self.replay()
        self.assertEqual(caught.exception.code, "invalid_response_record")
        self.assertNotIn("private", str(caught.exception))

    def test_receipt_path_cannot_escape_scope_even_with_matching_hash(self):
        request, receipt = self.complete()
        receipt["evidence"][0] = {"path": "operator/screen.png", "sha256": hashlib.sha256(self.screen.read_bytes()).hexdigest()}
        self.mutate_response(request, receipt)
        with self.assertRaises(BrokerError) as caught:
            self.replay()
        self.assertEqual(caught.exception.code, "evidence_outside_scope")

    def test_receipt_evidence_symlink_rejected_even_with_matching_hash(self):
        _, receipt = self.complete()
        path = self.root / receipt["evidence"][0]["path"]
        path.unlink()
        path.symlink_to(self.screen)
        with self.assertRaises(BrokerError) as caught:
            self.replay()
        self.assertEqual(caught.exception.code, "symlink_rejected")

    def test_queue_directory_symlink_is_rejected(self):
        other = self.root / "different"
        other.mkdir()
        self.broker.pending_dir.rmdir()
        self.broker.pending_dir.symlink_to(other, target_is_directory=True)
        with self.assertRaises(BrokerError) as caught:
            FileBroker(self.root)
        self.assertEqual(caught.exception.code, "symlink_rejected")


if __name__ == "__main__":
    unittest.main()
