"""Bounded private observation of one owned native provider's stdout.

The caller owns the process, its argv and its termination. Shared observation
handles raw capture, strict JSONL decoding and optional measurement validation,
so a terminal native outcome survives independent recording/measurement faults.
"""

import hashlib
import json
import math
import os
import re
import stat


MAX_OUTPUT = 16 * 1024 * 1024
# One record awaiting its retained form; bounds memory when retention is per record.
MAX_RECORD = 64 * 1024 * 1024

_VERSION_NUMBER = r"(?:0|[1-9][0-9]{0,5})"
_PROVIDER_VERSION = re.compile(
    rf"{_VERSION_NUMBER}\.{_VERSION_NUMBER}\.{_VERSION_NUMBER}"
    rf"(?:-(?:alpha|beta|rc)\.{_VERSION_NUMBER})?")


def canonical_provider_version(value):
    """Select a bounded version, never arbitrary native text or build metadata."""
    if isinstance(value, str) and len(value) <= 40 and _PROVIDER_VERSION.fullmatch(value):
        return value
    return None


def provider_version_observation(value, source):
    """Describe this initialization's optional, self-reported provider version."""
    if source not in ("codex_initialize_user_agent", "claude_system_init"):
        raise ValueError("Unsupported provider version source")
    version = None
    if source == "codex_initialize_user_agent":
        # The leading token describes the server. The platform and trailing
        # client metadata can carry other versions and must never supply it.
        if (isinstance(value, str) and len(value) <= 1024
                and not any(ord(character) < 32 or ord(character) == 127 for character in value)):
            token = value.partition(" ")[0]
            if token.startswith("multithread/"):
                version = canonical_provider_version(token[len("multithread/"):])
    else:
        version = canonical_provider_version(value)
    return {"status": "reported" if version is not None else
                       "not_reported" if value is None else "unrecognized",
            "version": version, "source": source}


# Stable identifiers are the machine contract; prose remains for older readers.
USAGE_SCOPES = {
    "native_main_loop": "this native query's main loop; excludes subagents",
    "latest_related_native_result": "latest related native result; main loop only, not the whole call",
    "native_thread_last_and_total": "native thread's last request and running totals; not this call's incremental usage",
}
MODEL_USAGE_SCOPES = {
    "native_query_cumulative": "latest cumulative query-stream totals; includes native subagents and compaction, not all provider helper calls",
}
COST_SCOPES = {
    "cumulative_through_latest_native_result": "cumulative through the latest native result; an estimate, not billing",
}


def measurement_error(envelope, field):
    """Keep bounded field names, never bad native values or model identities."""
    errors = envelope.setdefault("measurement_errors", [])
    if field not in errors:
        if len(errors) < 32:
            errors.append(field)
        else:
            envelope["measurement_errors_truncated"] = True


def replace_measurement_errors(envelope, observation, *fields):
    """Warnings follow adopted measurements; raw/native records retain history."""
    def selected(name):
        return any(name == field or name.startswith(field + ".") for field in fields)
    retained = [name for name in envelope.get("measurement_errors", []) if not selected(name)]
    incoming = [name for name in observation.get("measurement_errors", []) if selected(name)]
    if retained or incoming:
        envelope["measurement_errors"] = retained
        for name in incoming:
            measurement_error(envelope, name)
    else:
        envelope.pop("measurement_errors", None)


def measurement_number(value, envelope, field, *, integer=False):
    if value is None:
        return None
    if (type(value) not in ((int,) if integer else (int, float))
            or value < 0 or value > 2**53 - 1 or not math.isfinite(value)):
        measurement_error(envelope, field)
        return None
    return value


def measurement_fields(value, envelope, field, names, *, missing=False, costs=()):
    """Qualify known counters; retain uninterpreted native extensions privately."""
    if value is None:
        return None
    if not isinstance(value, dict):
        measurement_error(envelope, field)
        return None
    result = dict(value)
    for name in names:
        if missing or name in value:
            result[name] = measurement_number(value.get(name), envelope, field + "." + name,
                                              integer=name not in costs)
    return result


def claude_measurements(value, envelope):
    """Optional metadata cannot reject an independently attributed native answer."""
    usage = measurement_fields(value.get("usage"), envelope, "usage", (
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    if usage is not None:
        for name, fields in (
            ("cache_creation", ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")),
            ("output_tokens_details", ("thinking_tokens",)),
            ("server_tool_use", ("web_search_requests", "web_fetch_requests")),
        ):
            if name in usage:
                usage[name] = measurement_fields(usage[name], envelope, "usage." + name, fields)
    models = value.get("modelUsage")
    if models is not None:
        if not isinstance(models, dict):
            measurement_error(envelope, "model_usage")
            models = None
        else:
            models = {name: measurement_fields(counts, envelope, "model_usage.model", (
                "inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens",
                "webSearchRequests", "costUSD", "contextWindow", "maxOutputTokens"), costs=("costUSD",))
                      for name, counts in models.items()}
    return {
        "usage": usage, "model_usage": models,
        "provider_turns": measurement_number(value.get("num_turns"), envelope, "provider_turns", integer=True),
        "provider_duration_ms": measurement_number(value.get("duration_ms"), envelope, "provider_duration_ms", integer=True),
        "estimated_cost_usd": measurement_number(value.get("total_cost_usd"), envelope, "estimated_cost_usd"),
    }


def measurement_scope(envelope, field, identifier, scopes):
    envelope[field + "_id"] = identifier
    envelope[field] = scopes[identifier]


class ProtocolError(Exception):
    pass


def identity(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(
        ord(character) < 32 or ord(character) == 127 for character in value)


def setting_relation(requested, reported):
    """Compare names literally: an alias can resolve to another reported name."""
    return ("not_requested" if requested is None else
            "same_literal" if requested == reported else "different_name_unverified")


def observe_usage_model(envelope):
    """Record the model Claude's usage report names, when it names exactly one.

    Usage accumulates over the native query, so a single name is the only
    model that served it. Several names do not say which one answered, so
    the earlier observation stands. An unusable name clears the observation.
    """
    models = envelope.get("model_usage")
    if not isinstance(models, dict) or len(models) != 1:
        return
    (model,) = models
    envelope["model_observation"] = (
        {"source": "claude_model_usage", "reported_model": model,
         "relation": setting_relation(envelope.get("requested_model"), model)}
        if identity(model) else {"source": "unavailable", "reported_model": None, "relation": "unknown"})


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON member")
        value[key] = item
    return value


def _constant(value):
    raise ValueError("Non-finite JSON number")


def decode(body):
    """Read one JSONL record without duplicate, non-finite or undeliverable members."""
    try:
        value = json.loads(body, object_pairs_hook=_object, parse_constant=_constant)
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("Unreadable native JSONL output; inspect retained output before continuing.") from None
    if not isinstance(value, dict):
        raise ProtocolError("Native JSONL record was not an object; inspect retained output.")
    return value


class Observation:
    """Keep the bounded reader alive while the caller stops its owned process.

    stdout.json is the raw output prefix. A driver may set ``retain`` to give a
    complete record a smaller retained form, such as a payload the provider has
    already saved and the driver has verified. Each record is then written in
    that form before it is delivered, under the same bound, and the whole
    observed stream is still measured as native_output.
    """

    def __init__(self, process, directory, envelope):
        self.process = process
        self.envelope = envelope
        self.driver = None
        self.retain = None
        self.digest = hashlib.sha256()
        self.observed = 0
        self.native_digest = hashlib.sha256()
        self.native_observed = 0
        self.retained_by_reference = 0
        self.deferred = False
        self.truncated = False
        self.buffer = bytearray()
        self.searched = 0
        self.eof = False
        self.interpret = True
        self.closed = False
        self.capture = None
        fd = os.open(directory / "stdout.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.output = os.fdopen(fd, "wb")
        os.set_blocking(process.stdout.fileno(), False)

    def keep(self, data):
        """Write within the bound; True while the retained output is complete."""
        part = data[:MAX_OUTPUT + 1 - self.observed]
        if part:
            self.output.write(part)
            self.output.flush()
            self.digest.update(part)
            self.observed += len(part)
        self.truncated = self.observed > MAX_OUTPUT
        return not self.truncated

    def settle_deferred(self):
        """An unfinished record has no retained form; keep its bytes raw, within the bound."""
        if self.deferred:
            self.deferred = False
            self.keep(bytes(self.buffer))

    def read(self):
        if self.eof or self.truncated:
            return False
        records = self.retain is not None and self.interpret and self.driver is not None
        if not records:
            self.settle_deferred()
            if self.truncated:
                return False
        limit = (MAX_RECORD + 1 - len(self.buffer)) if records else (MAX_OUTPUT + 1 - self.observed)
        try:
            data = os.read(self.process.stdout.fileno(), min(65536, max(1, limit)))
        except BlockingIOError:
            return False
        except OSError:
            if self.capture is not None:
                self.capture.fault()
            raise
        if self.capture is not None:
            if data:
                self.capture.bytes("stdout", data)
            else:
                self.capture.event("stdout_eof")
        if not data:
            self.eof = True
            if self.buffer and self.interpret:
                self.settle_deferred()
                if self.truncated:
                    raise ProtocolError("Native output exceeded its bound; inspect the retained prefix before continuing.")
                raise ProtocolError("Native output ended with an incomplete JSONL record; inspect retained output.")
            return False
        self.native_digest.update(data)
        self.native_observed += len(data)
        if records:
            return self.read_records(data)
        self.output.write(data)
        self.output.flush()
        self.digest.update(data)
        self.observed += len(data)
        if self.observed > MAX_OUTPUT:
            self.truncated = True
            raise ProtocolError("Native output exceeded its bound; inspect the retained prefix before continuing.")
        if not self.interpret or self.driver is None:
            return True
        self.buffer.extend(data)
        consumed = 0
        try:
            while True:
                newline = self.buffer.find(b"\n", self.searched)
                if newline < 0:
                    self.searched = len(self.buffer)
                    break
                line = self.buffer[consumed:newline]
                consumed = newline + 1
                self.searched = consumed
                if line.strip():
                    previous_session = self.driver.session
                    self.driver.message(line)
                    if previous_session is None and self.driver.session is not None:
                        # Persist native identity before the queued task can write.
                        os.fsync(self.output.fileno())
        finally:
            # Consume each delivered line even if its callback fails, while
            # retaining the unprocessed suffix. Compact once per read.
            if consumed:
                del self.buffer[:consumed]
                self.searched -= consumed
        return True

    def read_records(self, data):
        """Write each complete record's retained form, then deliver the record itself."""
        self.deferred = True
        self.buffer.extend(data)
        consumed = 0
        try:
            while True:
                newline = self.buffer.find(b"\n", self.searched)
                if newline < 0:
                    self.searched = len(self.buffer)
                    if len(self.buffer) - consumed > MAX_RECORD:
                        raise ProtocolError("A native record exceeded its bound; inspect retained output before continuing.")
                    break
                line = bytes(self.buffer[consumed:newline])
                kept = line
                if line.strip():
                    try:
                        kept = self.retain(line)
                    except Exception:  # the raw record is always valid evidence
                        kept = line
                    if kept != line:
                        self.retained_by_reference += 1
                complete = self.keep(kept + b"\n")
                consumed = newline + 1
                self.searched = consumed
                if not complete:
                    # The bound precedes delivery, as for raw output.
                    raise ProtocolError("Native output exceeded its bound; inspect the retained prefix before continuing.")
                if line.strip():
                    previous_session = self.driver.session
                    self.driver.message(line)
                    if previous_session is None and self.driver.session is not None:
                        os.fsync(self.output.fileno())
        finally:
            if consumed:
                del self.buffer[:consumed]
                self.searched -= consumed
        return True

    def fault(self, message):
        if self.capture is not None:
            self.capture.fault()
        self.interpret = False
        self.settle_deferred()
        self.buffer.clear()
        self.searched = 0
        if self.driver is not None:
            self.driver.problem(message)
        else:
            self.envelope.update(needs_attention=True, message=message)

    def drain(self):
        """Read at most four ready chunks; never wait, send, retry or exceed cap."""
        if self.closed:
            return False
        progressed = False
        if self.driver is not None:
            self.driver.observation_only = True
        for _ in range(4):
            try:
                if not self.read():
                    break
                progressed = True
            except ProtocolError as exc:
                progressed = True
                self.fault(str(exc))
            except OSError:
                self.fault("Native cleanup output could not be observed; preserve the retained prefix.")
                break
        self.snapshot()
        return progressed

    def snapshot(self):
        self.envelope["stdout_observation"] = {"bytes": self.observed, "sha256": self.digest.hexdigest(),
                                               "truncated": self.truncated}
        if self.retained_by_reference:
            # stdout.json is no longer the raw stream; describe what was observed.
            self.envelope["native_output"] = {"bytes": self.native_observed,
                                              "sha256": self.native_digest.hexdigest(),
                                              "records_retained_by_reference": self.retained_by_reference}
        if self.driver is not None:
            self.driver.preserve(resolve_pending=False)

    def close(self):
        if self.closed:
            return
        try:
            self.drain()
            self.settle_deferred()
            self.output.flush()
            os.fsync(self.output.fileno())
            self.snapshot()
            if self.driver is not None:
                self.driver.preserve()
        finally:
            self.closed = True
            self.output.close()
            self.process.stdout.close()
            if self.process.stdin is not None:
                self.process.stdin.close()


def capture_entry(path):
    """Bounded entry-file observation, not proof of the executed binary/tree."""
    fd = None
    try:
        resolved = os.path.realpath(path)
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 512 * 1024 * 1024:
            raise OSError("unsupported executable entry")
        digest = hashlib.sha256()
        size = 0
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            size += len(data)
            if size > 512 * 1024 * 1024:
                raise OSError("entry grew beyond bound")
            digest.update(data)
        after = os.fstat(fd)
        visible = os.stat(resolved, follow_symlinks=False)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (after.st_dev, after.st_ino) != (visible.st_dev, visible.st_ino)
                or size != after.st_size):
            raise OSError("entry changed")
        return {"status": "entry_file_observed", "resolved_path": resolved, "bytes": size,
                "sha256": digest.hexdigest(), "execution_identity_verified": False,
                "device": after.st_dev, "inode": after.st_ino, "mode": after.st_mode,
                "mtime_ns": after.st_mtime_ns, "ctime_ns": after.st_ctime_ns}
    except (OSError, ValueError, TypeError):
        return {"status": "unavailable", "execution_identity_verified": False}
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


class TransportCapture:
    """Opt-in byte custody for this driver's owned stdio transport only.

    File/journal consistency and complete observed EOF are not authenticated
    provenance, sandbox qualification or accepted review. Capture faults are
    sticky and never alter an otherwise useful returned answer.
    """
    MAX_BYTES = 16 * 1024 * 1024
    MAX_EVENTS = 8192
    MAX_CHUNK = 65536
    NAMES = {"stdin": "stdin.bin", "stdout": "stdout.bin",
             "journal": "journal.jsonl", "context": "context.json"}

    def __init__(self, directory, context):
        self.path = os.fspath(directory)
        self.directory_fd = None
        self.files = {}
        self.counts = {name: 0 for name in self.NAMES}
        self.hashes = {name: hashlib.sha256() for name in self.NAMES}
        self.sequence = 0
        self.failed = False
        self.eof = self.stdin_closed = self.finished = False
        self.report = {"status": "incomplete", "capture_authenticated": False,
                       "review_accepted": False, "release_approved": False}
        try:
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(parent)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise OSError("call directory is not private")
                os.mkdir("transport", mode=0o700, dir_fd=parent)
                self.directory_fd = os.open("transport", os.O_RDONLY | os.O_DIRECTORY |
                                            os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
            for kind, name in self.NAMES.items():
                self.files[kind] = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                                          os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.directory_fd)
            data = (json.dumps(context, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
            if len(data) > 65536:
                raise ValueError("capture context exceeded its bound")
            self._append("context", data)
        except (OSError, ValueError, TypeError):
            self.failed = True

    def _append(self, kind, data):
        pending = memoryview(data)
        while pending:
            count = os.write(self.files[kind], pending)
            if count <= 0:
                raise OSError("capture file write made no progress")
            pending = pending[count:]
        self.hashes[kind].update(data)
        self.counts[kind] += len(data)

    def _event(self, kind, **fields):
        if self.sequence >= self.MAX_EVENTS:
            raise ValueError("capture journal exceeded its bound")
        self.sequence += 1
        data = (json.dumps({"seq": self.sequence, "kind": kind, **fields},
                           sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        self._append("journal", data)

    def bytes(self, stream, data):
        """Record only a successful syscall's transferred bytes, before interpretation."""
        if self.failed or self.finished:
            return
        try:
            if (stream not in ("stdin", "stdout") or not 0 < len(data) <= self.MAX_CHUNK
                    or stream == "stdin" and self.stdin_closed or stream == "stdout" and self.eof
                    or self.counts[stream] + len(data) > self.MAX_BYTES):
                raise ValueError("capture stream exceeded its bound or closed")
            offset = self.counts[stream]
            self._append(stream, data)
            self._event(stream, offset=offset, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        except (OSError, ValueError, TypeError):
            self.failed = True

    def event(self, kind):
        if self.failed or self.finished:
            return
        try:
            if kind == "stdout_eof" and not self.eof:
                self.eof = True
            elif kind == "stdin_closed" and not self.stdin_closed:
                self.stdin_closed = True
            else:
                raise ValueError("unknown or duplicate close event")
            self._event(kind)
        except (OSError, ValueError, TypeError):
            self.failed = True

    def fault(self):
        # Never copy native exception contents, user text or secret values.
        self.failed = True
        self.report["status"] = "incomplete"
        self.report.pop("inventory_sha256", None)

    def finish(self, exit_code, *, clean):
        """Inventory after the existing owner finishes cleanup; never wait or kill."""
        if self.finished:
            return self.report
        try:
            if (self.failed or not clean or type(exit_code) is not int or exit_code != 0
                    or not self.eof or not self.stdin_closed
                    or not self.counts["stdin"] or not self.counts["stdout"]):
                raise ValueError("capture did not reach a clean whole end")
            self._event("leader_reaped", exit_code=exit_code)
            retained = os.fstat(self.directory_fd)
            visible = os.stat(os.path.join(self.path, "transport"), follow_symlinks=False)
            if (retained.st_dev, retained.st_ino) != (visible.st_dev, visible.st_ino):
                raise OSError("capture directory path changed")
            if set(os.listdir(self.directory_fd)) != set(self.NAMES.values()):
                raise OSError("capture directory inventory changed")
            members = []
            for kind, name in self.NAMES.items():
                fd = self.files[kind]
                os.fsync(fd)
                info = os.fstat(fd)
                visible = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
                if (info.st_uid != os.getuid() or info.st_nlink != 1
                        or (info.st_dev, info.st_ino) != (visible.st_dev, visible.st_ino)
                        or info.st_mode & 0o777 != 0o600
                        or not stat.S_ISREG(info.st_mode) or info.st_size != self.counts[kind]):
                    raise OSError("capture file changed")
                observed = hashlib.sha256()
                offset = 0
                while offset < info.st_size:
                    data = os.pread(fd, min(65536, info.st_size - offset), offset)
                    if not data:
                        raise OSError("capture file became unavailable")
                    observed.update(data)
                    offset += len(data)
                if observed.digest() != self.hashes[kind].digest():
                    raise OSError("capture file bytes changed")
                members.append({"path": name, "bytes": info.st_size, "sha256": observed.hexdigest()})
            # An inventory is never an approval or sufficient completion marker.
            # Completion requires the matching caller receipt after directory fsync.
            inventory = (json.dumps({"schema": 1, "status": "inventory_only", "members": members,
                                     "capture_authenticated": False, "review_accepted": False,
                                     "release_approved": False}, sort_keys=True) + "\n").encode()
            fd = os.open("inventory.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.directory_fd)
            try:
                pending = memoryview(inventory)
                while pending:
                    count = os.write(fd, pending)
                    if count <= 0:
                        raise OSError("capture inventory write made no progress")
                    pending = pending[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            # The newly created private directory has no pre-existing inventory.
            os.link("inventory.tmp", "inventory.json", src_dir_fd=self.directory_fd,
                    dst_dir_fd=self.directory_fd, follow_symlinks=False)
            os.unlink("inventory.tmp", dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)
            visible = os.stat(os.path.join(self.path, "transport"), follow_symlinks=False)
            if (retained.st_dev, retained.st_ino) != (visible.st_dev, visible.st_ino):
                raise OSError("capture directory path changed during publication")
            self.report.update(status="complete", inventory_sha256=hashlib.sha256(inventory).hexdigest(),
                               path="transport/inventory.json", stdout_eof=True, events=self.sequence)
        except (OSError, ValueError, TypeError):
            self.failed = True
        finally:
            self.finished = True
            for fd in [*self.files.values(), self.directory_fd]:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        self.failed = True
            self.files.clear()
            self.directory_fd = None
            if self.failed:
                self.report["status"] = "incomplete"
                self.report.pop("inventory_sha256", None)
        return self.report
