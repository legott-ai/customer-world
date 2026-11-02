"""An atomic, scoped file handoff to a trusted host UI operator.

Receipts attest to operator-performed UI work and verify file integrity. The host
keeps one isolated browser context per actor/session. A successful finish attests
that the entire context, including all its tabs, was closed. Receipts cannot
establish image provenance, provide an OS sandbox, or guarantee exactly-once
physical effects after an operator crash.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid
import zlib


ALLOWED_ACTIONS = frozenset({
    "open", "click", "type", "fill", "press", "keypress", "scroll", "select",
    "hover", "back", "forward", "reload", "wait", "observe", "screenshot", "finish",
})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_OP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REQUEST_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{64})\Z")
_HOST_UI_LOCK = threading.Lock()
_MAX_JSON = 1024 * 1024
_MAX_PROOF = 32 * 1024 * 1024


class BrokerError(RuntimeError):
    """A public-safe handoff failure with a stable, non-sensitive code."""

    def __init__(self, code: str, error_type: str | None = None):
        self.code = code
        self.error_type = error_type
        suffix = f" ({error_type})" if error_type else ""
        super().__init__(f"UI broker failed: {code}{suffix}.")


def _identifier(value: str, *, operation: bool = False) -> str:
    pattern = _OP_ID if operation else _ID
    if not isinstance(value, str) or not pattern.fullmatch(value) or value in {".", ".."}:
        raise BrokerError("invalid_identifier")
    return value


def _request_identifier(value: str) -> str:
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        raise BrokerError("invalid_request_id")
    return value


def _origin(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url or "\\" in url or any(ord(c) < 32 or c.isspace() for c in url):
        raise BrokerError("invalid_url")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or "%" in parsed.hostname or parsed.port == 0:
            raise ValueError
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, UnicodeError):
        raise BrokerError("invalid_url") from None


def _no_symlinks(path: Path, anchor: Path) -> None:
    try:
        relative = path.relative_to(anchor)
    except ValueError:
        raise BrokerError("path_outside_scope") from None
    current = anchor
    for part in ("", *relative.parts):
        if part:
            current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise BrokerError("symlink_rejected")
        except FileNotFoundError:
            continue


def _read_plain(path: Path, *, anchor: Path, maximum: int) -> bytes:
    _no_symlinks(path, anchor)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise BrokerError("invalid_file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise BrokerError("invalid_file")
        return data
    finally:
        os.close(fd)


def _valid_image(raw: bytes, suffix: str) -> bool:
    """Check image structure without depending on a third-party image decoder."""
    if suffix == ".png":
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        position, image_header, image_data = 8, False, False
        while position + 12 <= len(raw):
            length = int.from_bytes(raw[position:position + 4], "big")
            kind = raw[position + 4:position + 8]
            end = position + 12 + length
            if end > len(raw):
                return False
            content = raw[position + 8:end - 4]
            crc = int.from_bytes(raw[end - 4:end], "big")
            if zlib.crc32(kind + content) & 0xFFFFFFFF != crc:
                return False
            if not image_header:
                if kind != b"IHDR" or length != 13:
                    return False
                width, height = struct.unpack(">II", content[:8])
                if not width or not height:
                    return False
                image_header = True
            elif kind == b"IHDR":
                return False
            if kind == b"IDAT" and content:
                image_data = True
            if kind == b"IEND":
                return length == 0 and image_data and end == len(raw)
            position = end
        return False
    if suffix not in {".jpg", ".jpeg"} or not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
        return False
    # Read marker segments up to the scan. A screenshot needs dimensions and a
    # nonempty encoded scan; this is structural validation, not image provenance.
    position, dimensions = 2, False
    while position + 4 <= len(raw):
        if raw[position] != 0xFF:
            return False
        while position < len(raw) and raw[position] == 0xFF:
            position += 1
        if position >= len(raw):
            return False
        marker = raw[position]
        position += 1
        if marker in {0x00, 0xD8, 0xD9}:
            return False
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        if position + 2 > len(raw):
            return False
        size = int.from_bytes(raw[position:position + 2], "big")
        if size < 2 or position + size > len(raw) - 2:
            return False
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            dimensions = size >= 8 and int.from_bytes(raw[position + 3:position + 5], "big") > 0 and int.from_bytes(raw[position + 5:position + 7], "big") > 0
        if marker == 0xDA:
            return dimensions and position + size < len(raw) - 2
        position += size
    return False


class FileBroker:
    def __init__(self, project_root: Path, timeout: float = 300):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number.")
        original = Path(project_root).expanduser().absolute()
        if original.is_symlink():
            raise BrokerError("symlink_rejected")
        try:
            original.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise BrokerError("filesystem_error", type(exc).__name__) from None
        self.project_root = original.resolve()
        self.timeout = float(timeout)
        self.root = self.project_root / "broker"
        self.pending_dir = self.root / "pending"
        self.response_dir = self.root / "responses"
        self.completed_dir = self.root / "completed"
        self.evidence_dir = self.root / "evidence"
        try:
            for path in (self.root, self.pending_dir, self.response_dir, self.completed_dir, self.evidence_dir):
                _no_symlinks(path, self.project_root)
                path.mkdir(exist_ok=True, mode=0o700)
                if not path.is_dir():
                    raise BrokerError("invalid_directory")
        except OSError as exc:
            raise BrokerError("filesystem_error", type(exc).__name__) from None

    def _read_json(self, path: Path) -> dict:
        try:
            def reject_constant(value):
                raise ValueError("Non-finite JSON number")
            result = json.loads(_read_plain(path, anchor=self.project_root, maximum=_MAX_JSON), parse_constant=reject_constant)
        except BrokerError:
            raise
        except (OSError, ValueError, UnicodeError) as exc:
            raise BrokerError("invalid_record", type(exc).__name__) from None
        if not isinstance(result, dict):
            raise BrokerError("invalid_record")
        return result

    def _atomic_bytes(self, path: Path, data: bytes) -> None:
        _no_symlinks(path, self.project_root)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Link publishes atomically and refuses duplicate destinations.
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            raise BrokerError("duplicate_record") from None
        except OSError as exc:
            raise BrokerError("filesystem_error", type(exc).__name__) from None
        finally:
            temporary.unlink(missing_ok=True)

    def _atomic_json(self, path: Path, data: dict) -> None:
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")
        if len(encoded) > _MAX_JSON:
            raise BrokerError("record_too_large")
        self._atomic_bytes(path, encoded)

    @contextmanager
    def _physical_ui_lock(self):
        deadline = time.monotonic() + self.timeout
        if not _HOST_UI_LOCK.acquire(timeout=self.timeout):
            raise BrokerError("ui_busy")
        fd = None
        try:
            lock_path = Path(tempfile.gettempdir()).resolve() / f"customer-world-ui-{os.getuid()}.lock"
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise BrokerError("invalid_lock")
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BrokerError("ui_busy")
                    time.sleep(0.02)
            yield
        except OSError as exc:
            raise BrokerError("filesystem_error", type(exc).__name__) from None
        finally:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            _HOST_UI_LOCK.release()

    def _request_record(self, request_id: str) -> dict:
        _request_identifier(request_id)
        pending = self.pending_dir / f"{request_id}.json"
        completed = self.completed_dir / f"{request_id}.json"
        if pending.exists() or pending.is_symlink():
            return self._validate_request(self._read_json(pending), request_id)
        if completed.exists() or completed.is_symlink():
            return self._validate_request(self._read_json(completed), request_id)
        raise BrokerError("unknown_request")

    def _validate_request(self, request: dict, request_id: str) -> dict:
        required = {"request_id", "actor_id", "session_id", "tool_id", "action", "target", "value", "expected_url"}
        if set(request) not in (required, required | {"operation_id"}) or request.get("request_id") != request_id:
            raise BrokerError("invalid_request_record")
        for field in ("actor_id", "session_id", "tool_id"):
            _identifier(request[field])
        action = request["action"]
        if not isinstance(action, str) or action not in ALLOWED_ACTIONS or not isinstance(request["target"], str) or not isinstance(request["value"], str):
            raise BrokerError("invalid_request_record")
        expected = _origin(request["expected_url"])
        if action == "open" and _origin(request["target"]) != expected:
            raise BrokerError("target_outside_scope")
        if "operation_id" in request:
            operation = _identifier(request["operation_id"], operation=True)
            identity = json.dumps([request["actor_id"], request["session_id"], operation], separators=(",", ":")).encode()
            if hashlib.sha256(identity).hexdigest() != request_id:
                raise BrokerError("request_identity_mismatch")
        return request

    def _archive(self, request: dict) -> None:
        request_id = request["request_id"]
        pending = self.pending_dir / f"{request_id}.json"
        completed = self.completed_dir / f"{request_id}.json"
        _no_symlinks(pending, self.project_root)
        _no_symlinks(completed, self.project_root)
        if completed.exists():
            if self._read_json(completed) != request:
                raise BrokerError("request_conflict")
            pending.unlink(missing_ok=True)
        elif pending.exists():
            os.replace(pending, completed)

    def _validate_response(self, request: dict, response: dict) -> dict:
        if set(response) != {"request_id", "actor_id", "session_id", "tool_id", "status", "observation", "evidence"}:
            raise BrokerError("invalid_response_record")
        for field in ("request_id", "actor_id", "session_id", "tool_id"):
            if response.get(field) != request.get(field):
                raise BrokerError("response_identity_mismatch")
        if response.get("status") not in ("ok", "error"):
            raise BrokerError("invalid_response_status")
        observation = response.get("observation")
        evidence = response.get("evidence")
        if not isinstance(observation, dict) or observation.get("tool_id") != request["tool_id"] or not isinstance(evidence, list):
            raise BrokerError("invalid_observation")
        if response["status"] == "error":
            # Deliberately discard any operator-supplied diagnostic text.
            raise BrokerError("operator_error")
        if not isinstance(observation.get("text"), str) or not observation["text"].strip():
            raise BrokerError("empty_observation")
        ordinary_fields = {"text", "url", "tool_id"}
        allowed_fields = ordinary_fields | ({"closed", "closed_tool_url"} if request["action"] == "finish" else {"closed"})
        if not ordinary_fields.issubset(observation) or not set(observation).issubset(allowed_fields):
            raise BrokerError("invalid_observation")
        url = observation.get("url")
        expected_origin = _origin(request["expected_url"])
        if request["action"] == "finish":
            if observation.get("closed") is not True or observation.get("closed_tool_url") != request["expected_url"]:
                raise BrokerError("closure_attestation_required")
            if url != "" and _origin(url) == expected_origin:
                raise BrokerError("tool_still_open")
        else:
            if observation.get("closed") not in (None, False) or _origin(url) != expected_origin:
                raise BrokerError("observation_outside_scope")
        if len(evidence) != 2:
            raise BrokerError("proof_required")
        scope = PurePosixPath("broker", "evidence", request["actor_id"], request["session_id"], request["request_id"])
        kinds = set()
        seen = set()
        for entry in evidence:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256"} or not isinstance(entry.get("path"), str):
                raise BrokerError("invalid_evidence")
            relative = PurePosixPath(entry["path"])
            if relative.is_absolute() or ".." in relative.parts or relative.parent != scope or relative.name in seen:
                raise BrokerError("evidence_outside_scope")
            seen.add(relative.name)
            digest = entry["sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise BrokerError("invalid_evidence_hash")
            path = self.project_root.joinpath(*relative.parts)
            try:
                raw = _read_plain(path, anchor=self.project_root, maximum=_MAX_PROOF)
            except OSError as exc:
                raise BrokerError("missing_evidence", type(exc).__name__) from None
            if hashlib.sha256(raw).hexdigest() != digest:
                raise BrokerError("evidence_hash_mismatch")
            suffix = relative.suffix.lower()
            if suffix == ".txt":
                try:
                    text = raw.decode("utf-8")
                except UnicodeError:
                    raise BrokerError("invalid_ax_proof") from None
                if not text.strip() or "\x00" in text:
                    raise BrokerError("invalid_ax_proof")
                kinds.add("ax")
            elif _valid_image(raw, suffix):
                kinds.add("image")
            else:
                raise BrokerError("invalid_image_proof")
        if kinds != {"ax", "image"}:
            raise BrokerError("proof_required")
        return response

    def request(self, actor_id, session_id, tool_id, action, target="", value="", expected_url="", operation_id=None) -> dict:
        actor_id, session_id, tool_id = (_identifier(v) for v in (actor_id, session_id, tool_id))
        if not isinstance(action, str) or action not in ALLOWED_ACTIONS or not isinstance(target, str) or not isinstance(value, str):
            raise BrokerError("invalid_action")
        if not expected_url and action == "open":
            expected_url = target
        expected_origin = _origin(expected_url)
        if action == "open" and _origin(target) != expected_origin:
            raise BrokerError("target_outside_scope")
        if operation_id is not None:
            operation_id = _identifier(operation_id, operation=True)
            identity = json.dumps([actor_id, session_id, operation_id], separators=(",", ":")).encode()
            request_id = hashlib.sha256(identity).hexdigest()
        else:
            request_id = uuid.uuid4().hex
        record = {
            "request_id": request_id, "actor_id": actor_id, "session_id": session_id,
            "tool_id": tool_id, "action": action, "target": target, "value": value,
            "expected_url": expected_url,
        }
        if operation_id is not None:
            record["operation_id"] = operation_id
        with self._physical_ui_lock():
            existing = None
            pending_path = self.pending_dir / f"{request_id}.json"
            completed_path = self.completed_dir / f"{request_id}.json"
            if operation_id is not None and (pending_path.exists() or completed_path.exists() or pending_path.is_symlink() or completed_path.is_symlink()):
                existing = self._request_record(request_id)
                if existing != record:
                    raise BrokerError("operation_conflict")
            for path in sorted(self.pending_dir.glob("*.json")):
                candidate = self._read_json(path)
                candidate_id = _request_identifier(candidate.get("request_id"))
                if path.name != f"{candidate_id}.json":
                    raise BrokerError("request_identity_mismatch")
                self._validate_request(candidate, candidate_id)
                if existing is not None and candidate_id == request_id:
                    continue
                # Retry a legacy request after timeout without inventing a new UI action.
                if operation_id is None and "operation_id" not in candidate and {k: v for k, v in candidate.items() if k != "request_id"} == {k: v for k, v in record.items() if k != "request_id"}:
                    existing, record, request_id = candidate, candidate, candidate_id
                    pending_path = path
                    continue
                if (self.response_dir / f"{candidate_id}.json").exists():
                    try:
                        self._validate_response(candidate, self._read_json(self.response_dir / f"{candidate_id}.json"))
                    except BrokerError as exc:
                        if exc.code != "operator_error":
                            raise
                    self._archive(candidate)
                else:
                    raise BrokerError("unresolved_request")
            response_path = self.response_dir / f"{request_id}.json"
            if existing is None:
                if response_path.exists() or response_path.is_symlink():
                    raise BrokerError("orphan_response")
                self._atomic_json(pending_path, record)
            deadline = time.monotonic() + self.timeout
            while not response_path.exists() and not response_path.is_symlink():
                if time.monotonic() >= deadline:
                    raise BrokerError("response_timeout")
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            try:
                response = self._validate_response(record, self._read_json(response_path))
            except BrokerError as exc:
                if exc.code == "operator_error":
                    self._archive(record)
                raise
            self._archive(record)
            return response

    def respond(self, request_id, *, text, url, screenshot: Path | None = None, ax: Path | None = None, error: str | None = None, closed: bool = False) -> dict:
        """Publish one immutable receipt after the operator has performed UI work.

        ``closed=True`` attests that the entire actor/session browser context,
        including all tabs it created, was closed. The host owns and isolates
        those contexts. Error receipts need no proof; private diagnostics are
        deliberately omitted.
        """
        request_id = _request_identifier(request_id)
        request = self._request_record(request_id)
        if request.get("request_id") != request_id:
            raise BrokerError("request_identity_mismatch")
        for field in ("actor_id", "session_id", "tool_id"):
            _identifier(request.get(field))
        response_path = self.response_dir / f"{request_id}.json"
        if response_path.exists() or response_path.is_symlink():
            raise BrokerError("duplicate_response")
        observation = {"text": text, "url": url, "tool_id": request["tool_id"]}
        response = {
            "request_id": request_id, "actor_id": request["actor_id"],
            "session_id": request["session_id"], "tool_id": request["tool_id"],
            "status": "error" if error is not None else "ok",
            "observation": observation, "evidence": [],
        }
        if error is not None:
            # Private operator errors, paths, and exception messages never enter receipts.
            response["observation"] = {"text": "UI operator reported an error.", "url": "", "tool_id": request["tool_id"]}
            self._atomic_json(response_path, response)
            return response
        if type(closed) is not bool or (request["action"] != "finish" and closed):
            raise BrokerError("invalid_closure_attestation")
        if request["action"] == "finish":
            observation.update(closed=closed, closed_tool_url=request["expected_url"])
        if screenshot is None or ax is None:
            raise BrokerError("proof_required")
        scope = self.evidence_dir / request["actor_id"] / request["session_id"] / request_id
        _no_symlinks(scope, self.project_root)
        # Refuse concurrent duplicate operators before either can publish proof.
        try:
            scope.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            scope.mkdir(mode=0o700)
        except FileExistsError:
            raise BrokerError("duplicate_proof") from None
        except OSError as exc:
            raise BrokerError("filesystem_error", type(exc).__name__) from None
        created = []
        try:
            for source_value, stem in ((screenshot, "screen"), (ax, "ax")):
                source = Path(source_value).expanduser().absolute()
                # Require canonical proof-source paths as well as plain destination
                # paths; a symlinked ancestor must not hide the supplied file.
                _no_symlinks(source, Path(source.anchor))
                suffix = source.suffix.lower()
                if (stem == "ax" and suffix != ".txt") or (stem == "screen" and suffix not in {".png", ".jpg", ".jpeg"}):
                    raise BrokerError("invalid_proof_type")
                raw = _read_plain(source, anchor=source.parent, maximum=_MAX_PROOF)
                destination = scope / f"{stem}{suffix}"
                self._atomic_bytes(destination, raw)
                created.append(destination)
                response["evidence"].append({"path": destination.relative_to(self.project_root).as_posix(), "sha256": hashlib.sha256(raw).hexdigest()})
            self._validate_response(request, response)
            self._atomic_json(response_path, response)
        except (BrokerError, OSError) as exc:
            for path in created:
                path.unlink(missing_ok=True)
            try:
                scope.rmdir()
            except OSError:
                pass
            if isinstance(exc, BrokerError):
                raise
            raise BrokerError("filesystem_error", type(exc).__name__) from None
        return response
