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


MAX_OUTPUT = 16 * 1024 * 1024

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
    """Keep the bounded reader alive while the caller stops its owned process."""

    def __init__(self, process, directory, envelope):
        self.process = process
        self.envelope = envelope
        self.driver = None
        self.digest = hashlib.sha256()
        self.observed = 0
        self.truncated = False
        self.buffer = bytearray()
        self.eof = False
        self.interpret = True
        self.closed = False
        fd = os.open(directory / "stdout.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.output = os.fdopen(fd, "wb")
        os.set_blocking(process.stdout.fileno(), False)

    def read(self):
        if self.eof or self.truncated:
            return False
        try:
            data = os.read(self.process.stdout.fileno(), min(65536, MAX_OUTPUT + 1 - self.observed))
        except BlockingIOError:
            return False
        if not data:
            self.eof = True
            if self.buffer and self.interpret:
                raise ProtocolError("Native output ended with an incomplete JSONL record; inspect retained output.")
            return False
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
        while b"\n" in self.buffer:
            line, _, remainder = self.buffer.partition(b"\n")
            self.buffer = bytearray(remainder)
            if line.strip():
                previous_session = self.driver.session
                self.driver.message(line)
                if previous_session is None and self.driver.session is not None:
                    # Persist native identity before the queued task can write.
                    os.fsync(self.output.fileno())
        return True

    def fault(self, message):
        self.interpret = False
        self.buffer.clear()
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
        if self.driver is not None:
            self.driver.preserve(resolve_pending=False)

    def close(self):
        if self.closed:
            return
        try:
            self.drain()
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
