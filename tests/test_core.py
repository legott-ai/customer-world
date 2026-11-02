"""Transport fixtures verify orchestration; these are not research participants."""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from customer_world import core


@dataclass
class FixtureCompletion:
    data: dict
    model: str = "transport-fixture"
    usage: dict | None = None


def turn(kind="choose", tool_id="", *, decision=None, memory="", action="", target="", value=""):
    return {"kind": kind, "tool_id": tool_id, "reason": "Transport fixture choice",
            "action": action, "target": target, "value": value,
            "decision": decision or ("stop" if not tool_id or kind == "finish" else "continue"), "memory": memory}


class FixtureProvider:
    """Explicit scripted transport fixture. Never exposed as a research runner."""
    def __init__(self, turns=None):
        self.calls = []
        self.turns = defaultdict(list)
        self.lock = threading.Lock()
        self.callback = None
        for actor_id, entries in (turns or {}).items():
            self.turns[actor_id] = list(entries)

    def complete(self, task, payload, schema):
        with self.lock:
            self.calls.append((task, copy.deepcopy(payload)))
            if self.callback:
                result = self.callback(task, payload)
                if result is not None:
                    if isinstance(result, Exception):
                        raise result
                    return FixtureCompletion(result)
            if task == "profile":
                actor_id = payload["source"]["id"]
                return FixtureCompletion({"name": actor_id, "goal": f"Own goal {actor_id}",
                                          "constraints": [f"Own constraint {actor_id}"],
                                          "incumbent": "existing workflow", "source_ids": [actor_id]})
            if task == "world":
                return FixtureCompletion({"event": "Fixture ordinary day", "available_minutes": 25,
                                          "energy": "medium", "unresolved": []})
            actor_id = payload["profile"]["source_ids"][0]
            return FixtureCompletion(copy.deepcopy(self.turns[actor_id].pop(0) if self.turns[actor_id] else turn()))


class FixtureBroker:
    """Idempotent evidence transport fixture; performs no real UI work."""
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.receipts = {}
        self.fail = False
        self.closed = True
        self.lock = threading.Lock()

    def request(self, actor_id, session_id, tool_id, action, target="", value="", expected_url="", operation_id=None):
        with self.lock:
            key = (actor_id, session_id, operation_id)
            if self.fail:
                raise RuntimeError("private raw error must not enter journal")
            if key in self.receipts:
                return copy.deepcopy(self.receipts[key])
            self.calls.append((actor_id, session_id, operation_id, tool_id, action, target, value))
            proofs = []
            for suffix, content in ((".txt", b"Transport fixture accessibility content"),
                                    (".png", b"Transport fixture image bytes; not a UI capture")):
                relative = f"broker/evidence/{actor_id}/{session_id}/fixture-{len(self.calls)}/proof{suffix}"
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                proofs.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
            response = {"request_id": f"fixture-{len(self.calls)}", "actor_id": actor_id,
                        "session_id": session_id, "tool_id": tool_id, "status": "ok",
                        "observation": {"text": "Transport fixture accessibility content", "tool_id": tool_id,
                                        "url": "about:blank" if action == "finish" else expected_url},
                        "evidence": proofs}
            if action == "finish":
                response["observation"].update(closed=self.closed, closed_tool_url=expected_url)
            self.receipts[key] = response
            return copy.deepcopy(response)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.sources = self.base / "sources.jsonl"
        self.rows = [{"id": actor_id, "brief": f"Fictional workflow {actor_id}",
                      "provenance": {"kind": "fictional", "reference": "unit-test transport fixture"}}
                     for actor_id in ("alpha", "beta", "gamma")]
        self.write_sources(self.rows)
        self.clock = patch.object(core, "_today", return_value="2026-11-01")
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def write_sources(self, rows):
        self.sources.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def project(self, count=2, tools=None):
        return core.Project.init(self.base / "project", self.sources, count=count,
                                 tools=tools if tools is not None else [
                                     {"id": "product", "url": "http://localhost:3000", "primary": True},
                                     {"id": "alternative", "url": "https://alternative.example", "primary": False}])

    def ready(self, count=2, turns=None):
        project = self.project(count)
        provider = FixtureProvider(turns)
        core.generate_profiles(project, provider)
        return project, provider, FixtureBroker(project.root)

    def journal(self, project, actor_id="alpha", day="2026-11-01"):
        return project._read(f"actors/{actor_id}/journals/{day}.json")

    def test_preparation_is_not_generation_or_activation(self):
        project = self.project()
        summary = core.get_summary(project)
        self.assertEqual(summary["prepared"], 2)
        self.assertEqual(summary["generated_profiles"], 0)
        self.assertEqual(summary["runtime_contexts"], 0)
        self.assertEqual(summary["primary_product_ui_visits"], 0)
        provider = FixtureProvider()
        summary = core.run_day(project, provider, FixtureBroker(project.root))
        self.assertEqual(provider.calls, [])
        self.assertEqual(summary["pending"], 2)
        self.assertTrue(core.verify_project(project)["ok"])

    def test_sources_validate_uniqueness_count_paths_and_provenance(self):
        cases = [self.rows + [self.rows[0]],
                 [{**self.rows[0], "id": "../escape"}],
                 [{**self.rows[0], "provenance": {"kind": "scraped-private", "reference": "fixture"}}]]
        for rows in cases:
            with self.subTest(rows=rows):
                self.write_sources(rows)
                with self.assertRaises(core.ProjectError):
                    self.project(count=1)
        self.write_sources(self.rows)
        with self.assertRaises(core.ProjectError):
            self.project(count=4)
        with self.assertRaises(core.ProjectError):
            core.Project.init(self.base / "bad-zone", self.sources, 1, timezone="No/Such_Zone")

    def test_generation_uses_only_own_source_and_resumes_validated_profiles(self):
        project = self.project()
        provider = FixtureProvider()
        summary = core.generate_profiles(project, provider, workers=2)
        self.assertEqual(summary["generated_profiles"], 2)
        calls = list(provider.calls)
        core.generate_profiles(project, provider)
        self.assertEqual(calls, provider.calls)
        for task, payload in provider.calls:
            self.assertEqual(task, "profile")
            self.assertEqual(set(payload), {"source", "_instructions"})
        self.assertTrue(core.verify_project(project)["ok"])
        self.assertFalse((project.root / ".transaction.json").exists())

    def test_invalid_profile_not_counted_and_raw_provider_error_not_saved(self):
        project = self.project(1)
        provider = FixtureProvider()
        provider.callback = lambda task, payload: {"name": "wrong", "goal": "g", "constraints": [],
                                                 "incumbent": "", "source_ids": ["someone_else"]}
        summary = core.generate_profiles(project, provider)
        self.assertEqual(summary["generated_profiles"], 0)
        self.assertEqual(summary["generation_errors"][0]["kind"], "invalid_model_output")
        provider.callback = lambda task, payload: RuntimeError("secret-provider-error")
        core.generate_profiles(project, provider)
        self.assertNotIn("secret-provider-error", (project.root / "actors/alpha/observations.jsonl").read_text())
        provider.callback = None
        self.assertEqual(core.generate_profiles(project, provider)["generated_profiles"], 1)

    def test_actual_date_only_and_timezone_configuration(self):
        project, provider, broker = self.ready(1)
        for day in ("2026-10-31", "2026-11-02", "20261101", "not-a-date"):
            with self.subTest(day=day), self.assertRaises(core.ProjectError):
                core.run_day(project, provider, broker, day)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(core.run_day(project, provider, broker, "2026-11-01")["completed"], 1)

    def test_voluntary_no_visit_completed_no_ui_and_no_repeat(self):
        project, provider, broker = self.ready(2)
        summary = core.run_day(project, provider, broker)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["runtime_contexts"], 2)
        self.assertEqual(summary["autonomous_decisions"], 2)
        self.assertEqual(summary["voluntary_nonvisits"], 2)
        self.assertEqual(broker.calls, [])
        calls = copy.deepcopy(provider.calls)
        self.assertEqual(core.run_day(project, provider, broker), summary)
        self.assertEqual(provider.calls, calls)
        self.assertTrue(core.verify_project(project)["ok"])

    def test_distinct_visits_switching_and_confirmed_closure(self):
        turns = {"alpha": [turn(tool_id="product"),
                           turn("act", "product", action="click", target="Observed button"),
                           turn(tool_id="alternative"),
                           turn("finish", "alternative", decision="try_again", memory="Own alpha memory")],
                 "beta": [turn(memory="Own beta memory")]}
        project, provider, broker = self.ready(2, turns)
        summary = core.run_day(project, provider, broker)
        self.assertEqual(summary["primary_product_ui_visits"], 1)
        self.assertEqual(summary["alternative_tool_visits"], 1)
        self.assertEqual(summary["autonomous_decisions"], 2)
        self.assertEqual(summary["voluntary_nonvisits"], 1)
        self.assertEqual(summary["intent_counts"]["try_again"], 1)
        self.assertEqual([item[4] for item in broker.calls], ["open", "click", "open", "finish"])
        self.assertTrue(self.journal(project)["closure_confirmed"])
        verification = core.verify_project(project)
        self.assertTrue(verification["ok"], verification)
        self.assertEqual(verification["checked_evidence"], 8)

    def test_followup_contexts_only_own_latest_completed_memory(self):
        project, provider, broker = self.ready(2, {"alpha": [turn(memory="alpha-private-memory")],
                                                  "beta": [turn(memory="beta-private-memory")]})
        core.run_day(project, provider, broker)
        with patch.object(core, "_today", return_value="2026-11-02"):
            core.run_day(project, provider, broker)
        world_calls = [payload for task, payload in provider.calls if task == "world" and payload["date"] == "2026-11-02"]
        self.assertEqual(len(world_calls), 2)
        for payload in world_calls:
            actor_id = payload["profile"]["source_ids"][0]
            other = "beta" if actor_id == "alpha" else "alpha"
            self.assertEqual(payload["memory"], f"{actor_id}-private-memory")
            self.assertNotIn(f"{other}-private-memory", json.dumps(payload))
            self.assertEqual(set(payload), {"profile", "previous_world", "memory", "date", "_instructions"})
        summary = core.get_summary(project)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["actor_days"], 4)
        self.assertEqual(len({self.journal(project, actor, day)["session_id"]
                              for actor in ("alpha", "beta") for day in ("2026-11-01", "2026-11-02")}), 4)

    def test_censored_day_does_not_overwrite_last_completed_memory(self):
        project, provider, broker = self.ready(1, {"alpha": [turn(memory="completed memory")]})
        core.run_day(project, provider, broker)
        provider.turns["alpha"] = [turn(tool_id="product")]
        with patch.object(core, "_today", return_value="2026-11-02"):
            result = core.run_day(project, provider, broker, max_turns=1)
        self.assertEqual(result["environment_censored"], 1)
        provider.turns["alpha"] = [turn()]
        with patch.object(core, "_today", return_value="2026-11-03"):
            core.run_day(project, provider, broker)
        world = [payload for task, payload in provider.calls if task == "world"][-1]
        self.assertEqual(world["memory"], "completed memory")
        self.assertIsNotNone(world["previous_world"])

    def test_broker_failure_is_error_never_voluntary_nonvisit_and_resumes_same_turn(self):
        project, provider, broker = self.ready(1, {"alpha": [turn(tool_id="product"), turn("finish", "product")]})
        broker.fail = True
        result = core.run_day(project, provider, broker)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["voluntary_nonvisits"], 0)
        self.assertEqual(result["primary_product_ui_visits"], 0)
        original = self.journal(project)
        self.assertIsNotNone(original["pending_turn"])
        self.assertNotIn("private raw error", json.dumps(original))
        broker.fail = False
        result = core.run_day(project, provider, broker)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["primary_product_ui_visits"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["active_errors"], 0)
        self.assertEqual(original["session_id"], self.journal(project)["session_id"])
        self.assertEqual(len([task for task, _ in provider.calls if task == "turn"]), 2)

    def test_finish_without_closure_attestation_is_not_completed(self):
        project, provider, broker = self.ready(1, {"alpha": [turn(tool_id="product"), turn("finish", "product", memory="unconfirmed")]})
        broker.closed = False
        result = core.run_day(project, provider, broker)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["active_errors"], 1)
        self.assertEqual(self.journal(project)["memory"], "")
        self.assertFalse(self.journal(project)["closure_confirmed"])

    def test_invalid_world_boolean_extra_field_and_unobserved_action_fail_closed(self):
        for invalid in ({"event": "fixture", "available_minutes": True, "energy": "low", "unresolved": []},
                        {"event": "fixture", "available_minutes": 10, "energy": "low", "unresolved": [], "extra": "x"}):
            project, provider, broker = self.ready(1)
            provider.callback = lambda task, payload, invalid=invalid: invalid if task == "world" else None
            result = core.run_day(project, provider, broker)
            self.assertEqual(result["runtime_contexts"], 0)
            self.assertEqual(result["active_errors"], 1)
            self.assertEqual(broker.calls, [])
            # Use a fresh workspace for the next subcase.
            self.base = self.base / "next"
            self.base.mkdir()
            self.sources = self.base / "sources.jsonl"
            self.write_sources(self.rows)
        project, provider, broker = self.ready(1, {"alpha": [turn("act", "product", action="click")]})
        result = core.run_day(project, provider, broker)
        self.assertEqual(result["autonomous_decisions"], 0)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(broker.calls, [])

    def test_cap_is_environment_censoring_with_cleanup_and_no_voluntary_intent(self):
        project, provider, broker = self.ready(1, {"alpha": [turn(tool_id="product")]})
        result = core.run_day(project, provider, broker, max_turns=1)
        self.assertEqual(result["environment_censored"], 1)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["voluntary_nonvisits"], 0)
        self.assertEqual(result["primary_product_ui_visits"], 1)
        self.assertEqual([item[4] for item in broker.calls], ["open", "finish"])
        self.assertTrue(self.journal(project)["closure_confirmed"])
        calls = list(provider.calls)
        core.run_day(project, provider, broker, max_turns=20)
        self.assertEqual(calls, provider.calls)

    def test_midnight_boundary_censors_instead_of_backfilling(self):
        project, provider, broker = self.ready(1, {"alpha": [turn(tool_id="product")]})
        dates = iter(["2026-11-01", "2026-11-01", "2026-11-02"])
        with patch.object(core, "_today", side_effect=lambda timezone: next(dates)):
            result = core.run_day(project, provider, broker)
        self.assertEqual(result["environment_censored"], 1)
        self.assertEqual(result["voluntary_nonvisits"], 0)
        self.assertEqual(broker.calls, [])

    def test_crash_after_receipt_replays_operation_without_duplicate_physical_request(self):
        project, provider, broker = self.ready(1, {"alpha": [turn(tool_id="product"), turn("finish", "product")]})
        original_write = project._write
        crashed = False

        def interrupt(relative, value):
            nonlocal crashed
            if (not crashed and "/journals/" in relative and value.get("turn_count") == 1
                    and value.get("pending_turn") is None):
                crashed = True
                raise KeyboardInterrupt("simulated process loss after receipt")
            return original_write(relative, value)

        with patch.object(project, "_write", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            core._run_actor(project, provider, broker, "alpha", "2026-11-01", 20)
        self.assertEqual(len(broker.calls), 1)
        project = core.Project(project.root)
        result = core.run_day(project, provider, broker)
        self.assertEqual(result["completed"], 1)
        self.assertEqual([item[4] for item in broker.calls], ["open", "finish"])
        self.assertEqual(len([task for task, _ in provider.calls if task == "turn"]), 2)
        self.assertTrue(core.verify_project(project)["ok"])

    def test_workflow_lock_blocks_overlapping_run(self):
        project, provider, broker = self.ready(1)
        with core._lock(project.root, "run-2026-11-01"):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(core.run_day, project, provider, broker)
                with self.assertRaises(core.ProjectBusyError):
                    future.result()

    def test_wal_recovers_crash_between_file_write_and_ledger_append(self):
        project = self.project(1)
        append = core._append

        def interrupt(path, record):
            if path.name == "ledger.jsonl":
                raise KeyboardInterrupt("fixture interruption")
            return append(path, record)

        with patch.object(core, "_append", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            project._write("actors/alpha/checkpoint.json", {"fixture": "recover me"})
        self.assertTrue((project.root / ".transaction.json").exists())
        recovered = core.Project(project.root)
        self.assertEqual(recovered._read("actors/alpha/checkpoint.json"), {"fixture": "recover me"})
        self.assertFalse((project.root / ".transaction.json").exists())
        self.assertTrue(core.verify_project(recovered)["ok"])

    def test_verify_detects_content_edits_observation_truncation_and_foreign_identity(self):
        project, provider, broker = self.ready(1)
        core.run_day(project, provider, broker)
        path = project.root / "actors/alpha/observations.jsonl"
        original = path.read_bytes()
        path.write_bytes(b"\n".join(original.splitlines()[:-1]) + b"\n")
        self.assertFalse(core.verify_project(project)["ok"])
        path.write_bytes(original)
        profile = project.root / "actors/alpha/profile.json"
        profile.write_text(profile.read_text().replace("Own goal alpha", "Edited goal"))
        self.assertFalse(core.verify_project(project)["ok"])
        (project.root / "actors/foreign").mkdir()
        self.assertIn("unexpected actor identity path", core.verify_project(project)["errors"])

    def test_symlinks_are_rejected_and_evidence_hashes_are_checked(self):
        project, provider, broker = self.ready(1, {"alpha": [turn(tool_id="product"), turn("finish", "product")]})
        core.run_day(project, provider, broker)
        evidence = next((project.root / "broker/evidence").rglob("*.txt"))
        evidence.write_text("changed transport fixture")
        self.assertFalse(core.verify_project(project)["ok"])
        source = project.root / "actors/alpha/source.json"
        source.unlink()
        source.symlink_to(self.sources)
        self.assertFalse(core.verify_project(project)["ok"])
        with self.assertRaises(core.ProjectError):
            project._read("actors/alpha/source.json")


if __name__ == "__main__":
    unittest.main()
