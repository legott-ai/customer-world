"""Independent, date-gated simulated-customer contexts with inspectable evidence.

The framework delegates behavior to an explicitly supplied model provider and UI
work to an evidence-producing broker. It never supplies a scripted research actor.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, date, timezone as dt_timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .prompts import PROFILE_SCHEMA, WORLD_SCHEMA, TURN_SCHEMA, TASK_INSTRUCTIONS

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_ZERO_HASH = "0" * 64
_ACTIONS = frozenset({"click", "type", "fill", "press", "keypress", "scroll", "select",
                      "hover", "back", "forward", "reload", "wait", "observe", "screenshot"})
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class ProjectError(ValueError):
    """Invalid project input or damaged project state."""


class ProjectBusyError(ProjectError):
    """Another process is performing this workflow."""


class ValidationError(ProjectError):
    """A model response violates its declared contract."""


class _CallFailure(Exception):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise ValidationError("Value is not finite JSON") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _stamp() -> str:
    return datetime.now(dt_timezone.utc).isoformat()


def _today(timezone: str) -> str:
    """The clock seam is private; public execution never accepts a backfill clock."""
    return datetime.now(ZoneInfo(timezone)).date().isoformat()


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _validate(value: Any, schema: dict, path: str = "output") -> None:
    """Validate the small JSON-schema subset used by the public task contracts."""
    kind = schema["type"]
    valid = ((kind == "object" and isinstance(value, dict))
             or (kind == "array" and isinstance(value, list))
             or (kind == "string" and isinstance(value, str))
             or (kind == "integer" and isinstance(value, int) and not isinstance(value, bool)))
    if not valid:
        raise ValidationError(f"{path}: expected {kind}")
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise ValidationError(f"{path}: missing or unexpected fields")
        for key, sub in schema["properties"].items():
            _validate(value[key], sub, f"{path}.{key}")
    elif kind == "array":
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{index}]")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path}: unsupported value")
    if kind == "integer" and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        raise ValidationError(f"{path}: out of range")
    _canonical(value)


def _profile(value: Any, actor_id: str) -> None:
    _validate(value, PROFILE_SCHEMA)
    if value["source_ids"] != [actor_id] or not value["name"].strip() or not value["goal"].strip():
        raise ValidationError("Profile must identify its own source and have a name and goal")


def _load(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProjectError(f"Unreadable project JSON: {path.name}") from exc


def _safe(root: Path, relative: str) -> Path:
    part = Path(relative)
    if part.is_absolute() or not part.parts or any(p in {".", ".."} for p in part.parts):
        raise ProjectError("Invalid project-relative path")
    current = root
    for component in part.parts:
        current = current / component
        if current.is_symlink():
            raise ProjectError("Symlinks are not allowed in project state")
    return current


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _lock(root: Path, name: str, *, blocking: bool = False):
    path = _safe(root, f"locks/{name}.lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = str(path)
    with _LOCKS_GUARD:
        local = _LOCAL_LOCKS.setdefault(key, threading.RLock())
    if not local.acquire(blocking=blocking):
        raise ProjectBusyError("Project workflow is already running")
    try:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise ProjectBusyError("Project workflow is already running") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        local.release()


def _last_record(path: Path) -> dict | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    # Logs are append-only; a torn line is damage, never silently discarded.
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        handle.seek(end - 1)
        if handle.read(1) != b"\n":
            raise ProjectError("Incomplete hash-chain record")
        position = max(0, end - 8192)
        while True:
            handle.seek(position)
            data = handle.read(end - position)
            lines = data.splitlines()
            if len(lines) > 1 or position == 0:
                try:
                    return json.loads(lines[-1])
                except (ValueError, UnicodeError) as exc:
                    raise ProjectError("Invalid hash-chain record") from exc
            position = max(0, position - 8192)


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "ab") as handle:
        handle.write(_canonical(record) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _chain_record(previous: dict | None, fields: dict) -> dict:
    record = {"seq": (previous["seq"] + 1 if previous else 1),
              "prev_hash": previous["hash"] if previous else _ZERO_HASH, **fields}
    record["hash"] = _hash(record)
    return record


class Project:
    """A private local project. Files isolate contexts logically, not as an OS sandbox."""

    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        if self.root.is_symlink() or not self.root.is_dir():
            raise ProjectError("Project root must be an existing non-symlink directory")
        self._recover()
        self.config = _load(_safe(self.root, "config.json"))
        self._validate_config()

    @classmethod
    def init(cls, root: Path, sources: Path, count: int = 100, timezone: str = "UTC",
             tools: list[dict] | None = None) -> "Project":
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ProjectError("count must be a positive integer")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
            raise ProjectError("timezone must be an installed IANA timezone") from exc
        tool_list = _validate_tools(tools or [])
        rows = []
        ids: set[str] = set()
        try:
            with Path(sources).open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    _validate_source(row)
                    if row["id"] in ids:
                        raise ProjectError(f"Duplicate source id at line {line_number}")
                    ids.add(row["id"])
                    rows.append(row)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectError("Sources must be readable JSONL") from exc
        if count > len(rows):
            raise ProjectError("count exceeds the number of distinct sources")
        destination = Path(root).absolute()
        if destination.is_symlink():
            raise ProjectError("Project root cannot be a symlink")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(destination.iterdir()):
            raise ProjectError("Initialize an empty directory")
        # Initialization is the only operation allowed before config exists.
        staging = Path(tempfile.mkdtemp(prefix=".customer-world-", dir=destination.parent))
        instance = object.__new__(cls)
        instance.root = staging
        instance.config = {"version": 1, "count": count, "timezone": timezone,
                           "created_at": _stamp(), "tools": tool_list,
                           "actor_ids": [row["id"] for row in rows[:count]]}
        try:
            with _lock(staging, "initialize"):
                instance._write("config.json", instance.config)
                for row in rows[:count]:
                    instance._write(f"actors/{row['id']}/source.json", row)
            # Replacing an empty directory is atomic on the supported POSIX hosts.
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return cls(destination)

    def _validate_config(self) -> None:
        config = self.config
        if not isinstance(config, dict) or config.get("version") != 1:
            raise ProjectError("Unsupported project configuration")
        ids = config.get("actor_ids")
        if (not isinstance(ids, list) or not ids or any(not _valid_id(x) for x in ids)
                or len(set(ids)) != len(ids) or config.get("count") != len(ids)):
            raise ProjectError("Invalid project identity list")
        try:
            ZoneInfo(config["timezone"])
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
            raise ProjectError("Invalid project timezone") from exc
        _validate_tools(config.get("tools", []))

    def _recover(self) -> None:
        with _lock(self.root, "ledger", blocking=True):
            self._recover_locked()

    def _recover_locked(self) -> None:
        transaction = _safe(self.root, ".transaction.json")
        if not transaction.exists():
            return
        pending = _load(transaction)
        if not isinstance(pending, dict) or set(pending) != {"record", "content"}:
            raise ProjectError("Invalid interrupted transaction")
        record, content = pending["record"], pending["content"]
        if (not isinstance(content, str) or hashlib.sha256(content.encode()).hexdigest() != record.get("sha256")
                or _hash({k: v for k, v in record.items() if k != "hash"}) != record.get("hash")):
            raise ProjectError("Interrupted transaction has invalid hashes")
        ledger = _safe(self.root, "ledger.jsonl")
        head = _last_record(ledger)
        if head and head.get("hash") == record["hash"]:
            _atomic_bytes(_safe(self.root, record["path"]), content.encode())
        elif (head["hash"] if head else _ZERO_HASH) == record["prev_hash"]:
            _atomic_bytes(_safe(self.root, record["path"]), content.encode())
            _append(ledger, record)
        else:
            raise ProjectError("Interrupted transaction conflicts with ledger")
        transaction.unlink()

    def _write(self, relative: str, value: Any) -> None:
        self._write_content(relative, _canonical(value) + b"\n")

    def _write_content(self, relative: str, content: bytes) -> None:
        path = _safe(self.root, relative)
        with _lock(self.root, "ledger", blocking=True):
            self._recover_locked()
            ledger = _safe(self.root, "ledger.jsonl")
            record = _chain_record(_last_record(ledger), {
                "path": relative, "sha256": hashlib.sha256(content).hexdigest(), "time": _stamp()})
            transaction = _safe(self.root, ".transaction.json")
            _atomic_bytes(transaction, _canonical({"record": record, "content": content.decode()}))
            _atomic_bytes(path, content)
            _append(ledger, record)
            transaction.unlink()

    def _read(self, relative: str) -> Any:
        return _load(_safe(self.root, relative))

    def _exists(self, relative: str) -> bool:
        return _safe(self.root, relative).exists()

    def _observe(self, actor_id: str, session_id: str, event: str, data: dict) -> None:
        # The caller holds the actor lock for the entire generation/day operation.
        path = _safe(self.root, f"actors/{actor_id}/observations.jsonl")
        record = _chain_record(_last_record(path), {
            "version": 1, "actor_id": actor_id, "session_id": session_id,
            "event": event, "time": _stamp(), "data": data})
        # Preserve the exact existing prefix. The logical append is installed
        # atomically through the same recoverable ledger transaction as journals.
        prefix = path.read_bytes() if path.exists() else b""
        self._write_content(f"actors/{actor_id}/observations.jsonl",
                            prefix + _canonical(record) + b"\n")


def _validate_source(row: Any) -> None:
    if (not isinstance(row, dict) or set(row) != {"id", "brief", "provenance"}
            or not _valid_id(row["id"]) or not isinstance(row["brief"], str) or not row["brief"].strip()):
        raise ProjectError("Each source needs a portable id, nonempty brief, and provenance")
    provenance = row["provenance"]
    if (not isinstance(provenance, dict) or set(provenance) != {"kind", "reference"}
            or provenance["kind"] not in {"fictional", "public", "consented"}
            or not isinstance(provenance["reference"], str) or not provenance["reference"].strip()):
        raise ProjectError("Each source needs explicit fictional, public, or consented provenance")


def _validate_tools(tools: Any) -> list[dict]:
    if not isinstance(tools, list):
        raise ProjectError("tools must be a list")
    seen = set()
    result = []
    for tool in tools:
        if (not isinstance(tool, dict) or set(tool) != {"id", "url", "primary"}
                or not _valid_id(tool["id"]) or tool["id"] in seen or type(tool["primary"]) is not bool
                or not isinstance(tool["url"], str)):
            raise ProjectError("Each tool needs a unique id, URL, and boolean primary")
        try:
            parsed = urlsplit(tool["url"])
            valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ProjectError("Tool URL must be HTTP(S), with no embedded credentials")
        seen.add(tool["id"])
        result.append(dict(tool))
    if sum(item["primary"] for item in result) > 1:
        raise ProjectError("Only one tool may be the primary product")
    return result


def _worker_count(workers: int) -> int:
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 64:
        raise ProjectError("workers must be an integer from 1 to 64")
    return workers


def _provider_call(project: Project, provider: Any, actor_id: str, session_id: str,
                   task: str, payload: dict, schema: dict) -> dict:
    payload = json.loads(_canonical({**payload, "_instructions": TASK_INSTRUCTIONS[task]}))
    request_id = uuid.uuid4().hex
    project._observe(actor_id, session_id, "provider_request", {
        "request_id": request_id, "task": task, "payload_sha256": _hash(payload)})
    try:
        completion = provider.complete(task, payload, schema)
    except Exception as exc:
        project._observe(actor_id, session_id, "provider_failure", {
            "request_id": request_id, "task": task, "kind": "provider_error"})
        raise _CallFailure("provider_error") from exc
    try:
        output = json.loads(_canonical(completion.data))
        _validate(output, schema)
        if task == "profile":
            _profile(output, actor_id)
        metadata = {"request_id": request_id, "task": task, "output_sha256": _hash(output),
                    "model": completion.model, "usage": completion.usage}
        # Metadata is restricted to JSON and providers must not echo raw credentials.
        if metadata["model"] is not None and not isinstance(metadata["model"], str):
            raise ValidationError("Invalid model metadata")
        if metadata["usage"] is not None and not isinstance(metadata["usage"], dict):
            raise ValidationError("Invalid usage metadata")
        _canonical(metadata)
    except (AttributeError, ValueError, ValidationError) as exc:
        project._observe(actor_id, session_id, "provider_failure", {
            "request_id": request_id, "task": task, "kind": "invalid_model_output"})
        raise _CallFailure("invalid_model_output") from exc
    project._observe(actor_id, session_id, "provider_response", metadata)
    return output


def generate_profiles(project: Project, provider: Any, workers: int = 3) -> dict:
    """Generate one independent validated profile for each prepared source; resume missing ones."""
    workers = _worker_count(workers)
    failures: list[dict] = []
    guard = threading.Lock()

    def generate(actor_id: str) -> None:
        with _lock(project.root, f"actor-{actor_id}", blocking=True):
            relative = f"actors/{actor_id}/profile.json"
            if project._exists(relative):
                _profile(project._read(relative), actor_id)
                return
            session_id = f"profile-{uuid.uuid4().hex}"
            try:
                output = _provider_call(project, provider, actor_id, session_id, "profile",
                                        {"source": project._read(f"actors/{actor_id}/source.json")}, PROFILE_SCHEMA)
                project._write(relative, output)
                project._observe(actor_id, session_id, "profile_completed", {"profile_sha256": _hash(output)})
            except _CallFailure as exc:
                with guard:
                    failures.append({"actor_id": actor_id, "kind": exc.kind})
    with _lock(project.root, "generate"):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(generate, project.config["actor_ids"]))
    return {**get_summary(project), "generation_errors": sorted(failures, key=lambda item: item["actor_id"])}


def _journal_relative(actor_id: str, day: str) -> str:
    return f"actors/{actor_id}/journals/{day}.json"


def _previous(project: Project, actor_id: str, day: str) -> tuple[dict | None, str]:
    directory = _safe(project.root, f"actors/{actor_id}/journals")
    previous_world = None
    memory = ""
    if directory.exists():
        for path in sorted(directory.glob("*.json"), reverse=True):
            if path.is_symlink():
                raise ProjectError("Symlinked journal")
            if path.stem >= day:
                continue
            journal = _load(path)
            if previous_world is None and journal.get("world") is not None:
                previous_world = journal["world"]
            if journal.get("status") == "completed":
                memory = journal["memory"]
                break
    return previous_world, memory


def _new_journal(actor_id: str, day: str) -> dict:
    return {"version": 1, "actor_id": actor_id, "day": day,
            "session_id": f"{day}-{uuid.uuid4().hex}", "status": "running",
            "world": None, "history": [], "turn_count": 0,
            "pending_turn": None, "pending_operation": None,
            "current_tool": "", "closure_confirmed": False, "opened_tools": [], "visited_tools": [],
            "selected_tools": [], "autonomous_decision": False,
            "memory": "", "decision": "undecided", "voluntary_nonvisit": False,
            "failures": [], "error_kind": None, "started_at": _stamp(), "completed_at": None}


def _validate_turn(turn: dict, journal: dict, tools: dict) -> None:
    _validate(turn, TURN_SCHEMA)
    kind, tool_id = turn["kind"], turn["tool_id"]
    if journal["turn_count"] == 0 and kind != "choose":
        raise ValidationError("First turn must choose a tool or no visit")
    if kind == "choose":
        if tool_id and tool_id not in tools:
            raise ValidationError("Unknown tool")
        if any(turn[key] for key in ("action", "target", "value")):
            raise ValidationError("Choose has no UI action fields")
        if not tool_id and journal["turn_count"] != 0:
            raise ValidationError("Use finish after an initial visit")
    elif kind == "act":
        if (not journal["current_tool"] or tool_id != journal["current_tool"]
                or turn["action"] not in _ACTIONS):
            raise ValidationError("Act must address the current observed tool")
        if not any(item.get("kind") == "observation" for item in journal["history"]):
            raise ValidationError("Act requires an actual UI observation")
        if turn["memory"] or turn["decision"] != "continue":
            raise ValidationError("Act cannot finalize memory or intent")
    elif kind == "finish":
        if tool_id != journal["current_tool"] or any(turn[key] for key in ("action", "target", "value")):
            raise ValidationError("Finish must address the current tool without UI action fields")
    if kind == "choose" and tool_id and (turn["memory"] or turn["decision"] != "continue"):
        raise ValidationError("A tool choice cannot finalize memory or intent")


def _broker_call(project: Project, broker: Any, actor_id: str, journal: dict,
                 tool: dict, action: str, target: str = "", value: str = "", suffix: str = "ui") -> dict:
    operation = {"operation_id": f"turn-{journal['turn_count']}-{suffix}", "tool_id": tool["id"],
                 "action": action, "target": target, "value": value, "expected_url": tool["url"]}
    if journal["pending_operation"] is not None and journal["pending_operation"] != operation:
        raise ProjectError("Conflicting pending UI operation")
    if _today(project.config["timezone"]) != journal["day"]:
        raise _CallFailure("date_boundary")
    journal["pending_operation"] = operation
    project._write(_journal_relative(actor_id, journal["day"]), journal)
    try:
        response = broker.request(actor_id, journal["session_id"], tool["id"], action,
                                  target=target, value=value, expected_url=tool["url"],
                                  operation_id=operation["operation_id"])
        # FileBroker verifies proof files, origin, IDs and hashes. Enforce the
        # observation boundary here too so a malformed custom broker fails closed.
        observation = response["observation"]
        if (response.get("status") != "ok" or response.get("actor_id") != actor_id
                or response.get("session_id") != journal["session_id"]
                or response.get("tool_id", tool["id"]) != tool["id"]
                or not isinstance(observation, dict) or observation.get("tool_id") != tool["id"]
                or not isinstance(observation.get("text"), str) or not observation["text"].strip()
                or not isinstance(observation.get("url"), str)
                or not isinstance(response.get("evidence"), list) or len(response["evidence"]) < 2):
            raise ValueError("Malformed broker response")
        if action == "finish" and (observation.get("closed") is not True
                                    or observation.get("closed_tool_url") != tool["url"]):
            raise ValueError("Closure was not confirmed")
        _canonical(response)
    except Exception as exc:
        raise _CallFailure("tool_error") from exc
    project._observe(actor_id, journal["session_id"], "ui_observation", {
        "operation_id": operation["operation_id"], "action": action,
        "tool_id": tool["id"], "response": response})
    journal["history"].append({"kind": "observation", "action": action, "observation": observation})
    journal["pending_operation"] = None
    return response


def _run_actor(project: Project, provider: Any, broker: Any, actor_id: str, day: str, max_turns: int) -> None:
    with _lock(project.root, f"actor-{actor_id}", blocking=True):
        relative = _journal_relative(actor_id, day)
        if project._exists(relative):
            journal = project._read(relative)
            if journal.get("actor_id") != actor_id or journal.get("day") != day:
                raise ProjectError("Journal identity mismatch")
            if journal["status"] in {"completed", "censored"}:
                return
        else:
            journal = _new_journal(actor_id, day)
        profile_path = f"actors/{actor_id}/profile.json"
        if not project._exists(profile_path):
            # A prepared folder is not a generated or activated customer.
            return
        profile = project._read(profile_path)
        _profile(profile, actor_id)
        previous_world, memory = _previous(project, actor_id, day)
        tools = {tool["id"]: tool for tool in project.config["tools"]}
        journal["status"] = "running"
        journal["error_kind"] = None
        project._write(relative, journal)
        try:
            if _today(project.config["timezone"]) != day:
                raise _CallFailure("date_boundary")
            if journal["world"] is None:
                journal["world"] = _provider_call(project, provider, actor_id, journal["session_id"], "world", {
                    "profile": profile, "previous_world": previous_world, "memory": memory, "date": day}, WORLD_SCHEMA)
                project._write(relative, journal)
            while journal["turn_count"] < max_turns:
                if _today(project.config["timezone"]) != day:
                    raise _CallFailure("date_boundary")
                if journal["pending_turn"] is None:
                    turn = _provider_call(project, provider, actor_id, journal["session_id"], "turn", {
                        "profile": profile, "world": journal["world"], "memory": memory,
                        "date": day, "session_id": journal["session_id"],
                        "tools": project.config["tools"], "current_tool": journal["current_tool"],
                        "history": journal["history"], "allowed_actions": sorted(_ACTIONS)}, TURN_SCHEMA)
                    try:
                        _validate_turn(turn, journal, tools)
                    except ValidationError as exc:
                        raise _CallFailure("invalid_model_output") from exc
                    journal["pending_turn"] = turn
                    journal["autonomous_decision"] = True
                    project._write(relative, journal)
                else:
                    turn = journal["pending_turn"]
                    _validate_turn(turn, journal, tools)
                kind, tool_id = turn["kind"], turn["tool_id"]
                if kind == "choose" and tool_id:
                    if tool_id not in journal["selected_tools"]:
                        journal["selected_tools"].append(tool_id)
                    _broker_call(project, broker, actor_id, journal, tools[tool_id], "open", tools[tool_id]["url"], suffix="open")
                    journal["current_tool"] = tool_id
                    journal["closure_confirmed"] = False
                    if tool_id not in journal["opened_tools"]:
                        journal["opened_tools"].append(tool_id)
                    if tool_id not in journal["visited_tools"]:
                        journal["visited_tools"].append(tool_id)
                elif kind == "act":
                    _broker_call(project, broker, actor_id, journal, tools[tool_id], turn["action"], turn["target"], turn["value"])
                else:
                    if kind == "finish" and journal["current_tool"]:
                        _broker_call(project, broker, actor_id, journal, tools[journal["current_tool"]], "finish", suffix="finish")
                        journal["closure_confirmed"] = True
                    journal["memory"] = turn["memory"]
                    journal["decision"] = turn["decision"]
                    journal["voluntary_nonvisit"] = (kind == "choose" and not journal["visited_tools"] and not journal["failures"])
                    journal["status"] = "completed"
                    journal["completed_at"] = _stamp()
                journal["history"].append({"kind": "decision", "turn": turn})
                journal["turn_count"] += 1
                journal["pending_turn"] = None
                project._write(relative, journal)
                if journal["status"] == "completed":
                    project._observe(actor_id, journal["session_id"], "day_completed", {
                        "day": day, "decision": journal["decision"], "memory_sha256": _hash(journal["memory"]),
                        "voluntary_nonvisit": journal["voluntary_nonvisit"]})
                    return
            # Reaching the action cap is an environmental cutoff, never a choice.
            if journal["current_tool"]:
                try:
                    _broker_call(project, broker, actor_id, journal, tools[journal["current_tool"]], "finish", suffix="cap-close")
                    journal["closure_confirmed"] = True
                except _CallFailure as exc:
                    journal["failures"].append({"kind": exc.kind, "time": _stamp()})
            journal["status"] = "censored"
            journal["error_kind"] = "turn_cap"
            journal["completed_at"] = _stamp()
            project._observe(actor_id, journal["session_id"], "environment_censored", {"day": day, "kind": "turn_cap"})
            project._write(relative, journal)
        except _CallFailure as exc:
            journal["status"] = "censored" if exc.kind == "date_boundary" else "error"
            if exc.kind == "date_boundary":
                journal["completed_at"] = _stamp()
            journal["error_kind"] = exc.kind
            journal["failures"].append({"kind": exc.kind, "time": _stamp()})
            project._observe(actor_id, journal["session_id"], "day_error", {"day": day, "kind": exc.kind})
            project._write(relative, journal)


def run_day(project: Project, provider: Any, broker: Any, day: str | None = None,
            workers: int = 3, max_turns: int = 20) -> dict:
    """Run or resume today only. Completed or capped actor-days cannot be rerun."""
    workers = _worker_count(workers)
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or not 1 <= max_turns <= 1000:
        raise ProjectError("max_turns must be an integer from 1 to 1000")
    current = _today(project.config["timezone"])
    if day is None:
        day = current
    try:
        parsed = date.fromisoformat(day)
    except (TypeError, ValueError) as exc:
        raise ProjectError("day must be an ISO calendar date") from exc
    if parsed.isoformat() != day or day != current:
        raise ProjectError("Only the actual current date in the configured timezone may run")
    with _lock(project.root, f"run-{day}"):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda actor_id: _run_actor(project, provider, broker, actor_id, day, max_turns),
                          project.config["actor_ids"]))
    return get_summary(project, day)


def _all_journals(project: Project, day: str | None = None) -> list[dict]:
    journals = []
    for actor_id in project.config["actor_ids"]:
        directory = _safe(project.root, f"actors/{actor_id}/journals")
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink():
                raise ProjectError("Symlinked journal")
            if day is not None and path.stem != day:
                continue
            journal = _load(path)
            if journal.get("actor_id") != actor_id or journal.get("day") != path.stem:
                raise ProjectError("Journal identity mismatch")
            journals.append(journal)
    return journals


def get_summary(project: Project, day: str | None = None) -> dict:
    """Return aggregate counts; every count uses distinct actors, never request totals.

    For a day, counts describe actor-days on that date. With day=None they describe
    unique actors across all dates; ``actor_days`` separately reports session grain.
    """
    if day is not None:
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise ProjectError("day must be an ISO calendar date") from exc
    journals = _all_journals(project, day)
    primary = {tool["id"] for tool in project.config["tools"] if tool["primary"]}
    alternatives = {tool["id"] for tool in project.config["tools"] if not tool["primary"]}
    distinct = lambda predicate: len({item["actor_id"] for item in journals if predicate(item)})
    generated = 0
    for actor_id in project.config["actor_ids"]:
        relative = f"actors/{actor_id}/profile.json"
        if project._exists(relative):
            _profile(project._read(relative), actor_id)
            generated += 1
    completed = distinct(lambda item: item["status"] == "completed")
    capped = distinct(lambda item: item["status"] == "censored")
    terminal = {item["actor_id"] for item in journals if item["status"] in {"completed", "censored"}}
    return {"day": day, "days": sorted({item["day"] for item in journals}),
            "actor_count": project.config["count"], "prepared": project.config["count"],
            "generated_profiles": generated, "actor_days": len(journals),
            "runtime_contexts": distinct(lambda item: item["world"] is not None),
            "autonomous_decisions": distinct(lambda item: item["autonomous_decision"]),
            "primary_product_ui_visits": distinct(lambda item: bool(primary.intersection(item["visited_tools"]))),
            "alternative_tool_visits": distinct(lambda item: bool(alternatives.intersection(item["visited_tools"]))),
            "voluntary_nonvisits": distinct(lambda item: item["voluntary_nonvisit"]),
            "environment_censored": capped,
            "errors": distinct(lambda item: bool(item["failures"])),
            "completed": completed, "pending": project.config["count"] - len(terminal),
            "active_errors": distinct(lambda item: item["status"] == "error"),
            "intent_counts": {decision: distinct(lambda item, decision=decision: item["status"] == "completed" and item["decision"] == decision)
                              for decision in ("continue", "try_again", "keep_incumbent", "stop", "undecided")}}


def _check_chain(path: Path, *, actor_id: str | None = None) -> list[dict]:
    records = []
    previous = _ZERO_HASH
    if not path.exists():
        return records
    with path.open("rb") as handle:
        for index, raw in enumerate(handle, 1):
            if not raw.endswith(b"\n"):
                raise ProjectError("Incomplete hash-chain line")
            try:
                record = json.loads(raw)
                digest = record["hash"]
                content = {key: value for key, value in record.items() if key != "hash"}
                if (record["seq"] != index or record["prev_hash"] != previous
                        or _hash(content) != digest or (actor_id is not None and record.get("actor_id") != actor_id)):
                    raise ValueError
            except (ValueError, KeyError, TypeError) as exc:
                raise ProjectError("Hash-chain verification failed") from exc
            previous = digest
            records.append(record)
    return records


def verify_project(project: Project) -> dict:
    """Verify the ledger, own identity paths, observations, and broker proof hashes.

    Hash chains detect local damage and inconsistent edits; without an external
    trusted anchor they are not a signature or proof against a malicious operator.
    """
    errors: list[str] = []
    checked_files = checked_observations = checked_evidence = 0
    try:
        with _lock(project.root, "ledger", blocking=True):
            project._recover_locked()
            records = _check_chain(_safe(project.root, "ledger.jsonl"))
            latest = {record["path"]: record for record in records}
            required = {"config.json"} | {f"actors/{actor_id}/source.json" for actor_id in project.config["actor_ids"]}
            if not required.issubset(latest):
                errors.append("missing required ledger identities")
            for relative, record in latest.items():
                path = _safe(project.root, relative)
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
                    errors.append(f"content hash mismatch: {relative}")
                checked_files += 1
            for actor_id in project.config["actor_ids"]:
                source = project._read(f"actors/{actor_id}/source.json")
                _validate_source(source)
                if source["id"] != actor_id:
                    errors.append(f"source identity mismatch: {actor_id}")
                profile = f"actors/{actor_id}/profile.json"
                if project._exists(profile):
                    _profile(project._read(profile), actor_id)
                    if profile not in latest:
                        errors.append(f"untracked profile: {actor_id}")
                observation_path = _safe(project.root, f"actors/{actor_id}/observations.jsonl")
                observations = _check_chain(observation_path, actor_id=actor_id)
                checked_observations += len(observations)
                if observations and f"actors/{actor_id}/observations.jsonl" not in latest:
                    errors.append(f"untracked observations: {actor_id}")
                for record in observations:
                    if record.get("event") != "ui_observation":
                        continue
                    response = record["data"]["response"]
                    if response.get("actor_id") != actor_id or response.get("session_id") != record["session_id"]:
                        errors.append(f"observation identity mismatch: {actor_id}")
                    for proof in response.get("evidence", []):
                        scope = Path("broker") / "evidence" / actor_id / record["session_id"] / response["request_id"]
                        proof_path = Path(proof["path"])
                        if proof_path.parent != scope:
                            errors.append(f"evidence identity scope mismatch: {actor_id}")
                        path = _safe(project.root, proof["path"])
                        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != proof["sha256"]:
                            errors.append(f"evidence hash mismatch: {actor_id}")
                        checked_evidence += 1
            journals = _all_journals(project)
            sessions = set()
            for journal in journals:
                session = journal["session_id"]
                if session in sessions:
                    errors.append("duplicate actor-day session id")
                sessions.add(session)
                relative = _journal_relative(journal["actor_id"], journal["day"])
                if relative not in latest:
                    errors.append(f"untracked journal: {journal['actor_id']}")
                if journal["world"] is not None:
                    _validate(journal["world"], WORLD_SCHEMA)
                if journal["status"] not in {"running", "error", "completed", "censored"}:
                    errors.append(f"invalid journal status: {journal['actor_id']}")
                if not isinstance(journal["memory"], str):
                    errors.append(f"invalid journal memory: {journal['actor_id']}")
                known_tools = {tool["id"] for tool in project.config["tools"]}
                if not set(journal["visited_tools"]).issubset(known_tools):
                    errors.append(f"unknown visited tool: {journal['actor_id']}")
                if (journal["status"] == "completed" and journal["visited_tools"]
                        and journal.get("closure_confirmed") is not True):
                    errors.append(f"unconfirmed completed closure: {journal['actor_id']}")
                if journal["voluntary_nonvisit"] and (journal["visited_tools"] or journal["failures"]
                                                      or journal["status"] != "completed"):
                    errors.append(f"invalid voluntary nonvisit: {journal['actor_id']}")
                for entry in journal["history"]:
                    if entry.get("kind") == "decision":
                        _validate(entry["turn"], TURN_SCHEMA)
            actor_directory = _safe(project.root, "actors")
            for child in actor_directory.iterdir():
                if child.is_symlink() or child.name not in project.config["actor_ids"]:
                    errors.append("unexpected actor identity path")
    except (ProjectError, OSError, KeyError, TypeError) as exc:
        errors.append(str(exc) if isinstance(exc, ProjectError) else "invalid project structure")
    return {"ok": not errors, "checked_files": checked_files,
            "checked_observations": checked_observations, "checked_evidence": checked_evidence,
            "errors": errors}
