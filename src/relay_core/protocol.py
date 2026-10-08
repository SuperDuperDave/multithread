"""Validated, deliberately small protocol for the Multithread ledger.

Multithread records coordination facts, not agent transcripts.  The validator is
therefore intentionally restrictive: one-line summaries, bounded identifiers,
and event-specific metadata keys.  If a new fact does not fit, extend the
protocol deliberately instead of pouring an opaque hook payload into SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import unicodedata
import uuid
from typing import Any, Mapping


PROTOCOL_VERSION = 1

PUBLIC_EVENT_KINDS = frozenset(
    {
        "session.started",
        "turn.completed",
        "turn.interrupted",
        "session.ended",
        "work.intent",
        "work.blocked",
        "work.handoff",
        "review.requested",
        "friction.observed",
        "ratchet.decided",
        "ratchet.verified",
    }
)
#: A role's binding to one native conversation. The bound event's sequence is
#: the binding's generation; the other three name the generation they change.
WAKE_BINDING_KINDS = ("wake.bound", "wake.unbound", "wake.paused", "wake.resumed")
#: One delivery attempt: recorded before the transport runs, concluded once after.
WAKE_ATTEMPT_KINDS = ("wake.attempted", "wake.concluded")
INTERNAL_EVENT_KINDS = frozenset(
    {
        "claim.acquired",
        "claim.released",
        "claim.broken",
        "decision.requested",
        "decision.responded",
        "delivery.acknowledged",
        "provider.reviewed",
        *WAKE_BINDING_KINDS,
        *WAKE_ATTEMPT_KINDS,
    }
)
EVENT_KINDS = PUBLIC_EVENT_KINDS | INTERNAL_EVENT_KINDS

FRICTION_CATEGORIES = frozenset(
    {
        "coordination",
        "context",
        "environment",
        "tooling",
        "verification",
        "workflow",
        "other",
    }
)
RATCHET_MODES = frozenset({"drop", "subtract", "promote"})
RATCHET_OUTCOMES = frozenset({"improved", "unchanged", "worse"})
DECISION_AUTHORITY_HINTS = frozenset({"engineering", "uncertain"})
DECISION_RESOLUTIONS = frozenset(
    {"choice", "directive", "escalate", "needs-evidence"}
)
DECISION_AUTHORITY_CLASSES = frozenset(
    {
        "engineering",
        "human-only",
        "external-authorization",
        "undetermined",
    }
)
DECISION_ROLLOUT_FENCE = "active-clients-refreshed"
#: What a sender may ask for; a Claude Code inbox has no separate steer.
WAKE_REQUESTS = frozenset({"queue", "steer"})
WAKE_TRANSPORTS = frozenset({"queue", "steer", "inbox"})
#: Every admissible conclusion per provider, as (outcome, reason, transport
#: that produced it). A transport of None means none ran: nothing was sent.
WAKE_CONCLUSIONS = {
    "codex": frozenset(
        {
            ("steered", "live_turn", "steer"),
            ("queued", "requested", "queue"),
            ("queued", "no_live_turn", "queue"),
            # Codex declined the steer before accepting its input:
            ("queued", "turn_ended", "queue"),
            ("queued", "turn_changed", "queue"),
            ("queued", "turn_not_steerable", "queue"),
            ("not_sent", "daemon_unreachable", None),
            ("not_sent", "daemon_refused", None),
            ("not_sent", "daemon_unreadable", None),
            ("not_sent", "conversation_unknown", None),
            ("not_sent", "codex_unavailable", "queue"),
            ("not_sent", "queue_refused", "queue"),
            ("uncertain", "no_answer", "steer"),
            ("uncertain", "unusable_reply", "steer"),
            ("uncertain", "unrecognized_error", "steer"),
            ("uncertain", "no_answer", "queue"),
            ("uncertain", "no_receipt", "queue"),
            ("uncertain", "queue_failed", "queue"),
        }
    ),
    "claude": frozenset(
        {
            ("delivered", "inbox_accepted", "inbox"),
            ("not_sent", "inbox_missing", None),
            ("not_sent", "inbox_unsafe", None),
            ("not_sent", "inbox_refused", "inbox"),
            ("uncertain", "dropped", "inbox"),
        }
    ),
}
WAKE_PROVIDERS = frozenset(WAKE_CONCLUSIONS)

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
_RESOURCE_ALIASES = {
    "integrate:main": "integration:main",
}
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,199}$")
_RECIPIENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,400}$")
_FINGERPRINT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
_DECISION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
_DECISION_OPTION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_DECISION_ARTIFACT_RE = re.compile(r"^git:[0-9a-f]{40}$")
_WAKE_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_WAKE_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
_WAKE_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_WAKE_SEQUENCE_REF_RE = re.compile(r"^[1-9][0-9]{0,11}$")
# A sequence in another checkout's ledger: the conversation stays where it started, and only the wake crosses.
_WAKE_LEDGER_REF_RE = re.compile(r"^ledger:(/.*)#([1-9][0-9]{0,11})$")
_RESOURCE_RE = re.compile(
    r"^[a-z][a-z0-9-]{0,31}:[a-z0-9][a-z0-9._/+\-]{0,159}$"
)
_ARTIFACT_RE = re.compile(
    r"^(?:git:(?:[0-9a-f]{40}|[0-9a-f]{64})|sha256:[0-9a-f]{64}|receipt:[A-Za-z0-9._:/+\-]{1,180})$"
)
_SECRET_KEY_RE = re.compile(
    r"(?:authorization|cookie|credential|password|private[_-]?key|secret|token)", re.I
)
_SECRET_VALUE_RES = (
    # A bearer credential is a token68 run (RFC 6750); "bearer token" or "bearer device key" is the scheme's name.
    # Any quote, backtick or escape may open it, and JSON text may escape its characters (\/ or \uXXXX).
    re.compile(r"\bBearer\s+[^\sA-Za-z0-9._~+/-]{0,4}(?:[A-Za-z0-9._~+/-]|\\/|\\u[0-9A-Fa-f]{4}){16,}=*", re.I),
    re.compile(r"\b(?:sk[-_]|ghp_|github_pat_|sb_secret_)[A-Za-z0-9_-]{12,}\b", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.I),
    re.compile(
        r"\b[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PRIVATE_KEY)\s*=",
        re.I,
    ),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.I),
)

_META_KEYS: dict[str, frozenset[str]] = {
    "session.started": frozenset({"client", "branch", "commit", "worktree"}),
    "turn.completed": frozenset({"client", "branch", "commit", "worktree"}),
    "turn.interrupted": frozenset({"client", "branch", "commit", "worktree"}),
    "session.ended": frozenset({"client", "branch", "commit", "worktree"}),
    "work.intent": frozenset(
        {
            "resource",
            "commit_oid",
            "commit_subject",
            "relay_trailers_sha256",
            "evidence_sha256",
        }
    ),
    "work.blocked": frozenset(
        {
            "resource",
            "reason",
            "commit_oid",
            "commit_subject",
            "relay_trailers_sha256",
            "evidence_sha256",
        }
    ),
    "work.handoff": frozenset(
        {"commit_oid", "commit_subject", "relay_trailers_sha256", "evidence_sha256"}
    ),
    "review.requested": frozenset(
        {"commit_oid", "commit_subject", "relay_trailers_sha256", "evidence_sha256"}
    ),
    "friction.observed": frozenset(
        {"fingerprint", "category", "cost_seconds", "position", "proposal"}
    ),
    "provider.reviewed": frozenset(
        {"provider", "binary_sha256", "version", "surface_sha256", "plugins", "agents",
         "control_fired", "restricted_fired"}
    ),
    "ratchet.decided": frozenset(
        {"fingerprint", "mode", "home", "verify_when"}
    ),
    "ratchet.verified": frozenset(
        {
            "fingerprint",
            "outcome",
            "decision_seq",
            "drop_compatibility",
            "before_cost_seconds",
            "after_cost_seconds",
            "evidence",
        }
    ),
    "claim.acquired": frozenset(
        {"claim_id", "resource", "holder_agent", "holder_session", "purpose"}
    ),
    "claim.released": frozenset(
        {"claim_id", "resource", "holder_agent", "holder_session"}
    ),
    "claim.broken": frozenset(
        {
            "claim_id",
            "resource",
            "holder_agent",
            "holder_session",
            "actor_agent",
            "actor_session",
            "reason",
        }
    ),
    "delivery.acknowledged": frozenset(
        {"signal_seq", "signal_event_id", "target_agent"}
    ),
    "decision.requested": frozenset(
        {"decision_id", "authority_hint", "option_ids"}
    ),
    "decision.responded": frozenset(
        {
            "decision_id",
            "request_seq",
            "request_event_id",
            "resolution",
            "authority_class",
            "choice",
        }
    ),
    "wake.bound": frozenset(
        {"role", "provider", "thread", "endpoint", "cwd", "replaces",
         "charter", "role_scope", "reason", "approval_ref", "previous_holder"}
    ),
    "wake.unbound": frozenset({"role", "generation", "reason", "approval_ref"}),
    "wake.paused": frozenset({"role", "generation", "reason", "approval_ref"}),
    "wake.resumed": frozenset({"role", "generation", "reason", "approval_ref"}),
    "wake.attempted": frozenset(
        {
            "role",
            "generation",
            "provider",
            "thread",
            "message_id",
            "ref",
            "ref_sha256",
            "ref_size",
            "requested",
            "sender",
        }
    ),
    "wake.concluded": frozenset(
        {
            "role",
            "generation",
            "attempt_seq",
            "message_id",
            "outcome",
            "reason",
            "transport",
            "native_id",
            "detail",
        }
    ),
}


class RelayError(RuntimeError):
    """Base error with a stable process exit code."""

    exit_code = 1


class ValidationError(RelayError):
    exit_code = 64


class StateError(RelayError):
    exit_code = 74


class BusyError(RelayError):
    exit_code = 75


class ConflictError(RelayError):
    exit_code = 73


@dataclass(frozen=True)
class Event:
    v: int
    event_id: str
    kind: str
    agent: str
    session: str
    summary: str
    work_id: str | None
    target: str | None
    scope: str | None
    artifact: str | None
    meta: dict[str, Any]
    canonical_json: str
    body_hash: str

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "v": self.v,
            "id": self.event_id,
            "kind": self.kind,
            "agent": self.agent,
            "session": self.session,
            "summary": self.summary,
        }
        for key, value in (
            ("work_id", self.work_id),
            ("target", self.target),
            ("scope", self.scope),
            ("artifact", self.artifact),
        ):
            if value is not None:
                out[key] = value
        if self.meta:
            out["meta"] = dict(self.meta)
        return out


def new_event_id(prefix: str = "evt") -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def canonical_resource(value: str) -> str:
    resource = _one_line("resource", value, 192).lower()
    if not _RESOURCE_RE.fullmatch(resource):
        raise ValidationError(
            "resource must be canonical namespace:name text (for example deploy:ota)"
        )
    return _RESOURCE_ALIASES.get(resource, resource)


def canonical_agent(value: Any) -> str:
    """Validate an agent identifier at read-only command boundaries."""

    return _identifier("agent", value)


def session_target(agent: Any, session: Any) -> str:
    """An exact recipient outside the legacy agent-identifier namespace."""
    return json.dumps([canonical_agent(agent), canonical_agent(session)], separators=(",", ":"))


def canonical_target(value: Any) -> str | None:
    """Normalize exact recipients before deriving events or retry identities."""
    target = _optional_line("target", value, 4096)
    if target is not None and target.startswith("["):
        try:
            pair = json.loads(target)
        except ValueError as exc:
            raise ValidationError("exact target must be a JSON string containing an agent/session pair") from exc
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValidationError("exact target must contain exactly an agent and session")
        return session_target(*pair)
    return _optional_line("target", target, 407)


def canonical_work_id(value: Any) -> str:
    """Return one exact canonical work identifier for decision routing."""

    work_id = _identifier("work_id", value)
    if work_id != value:
        raise ValidationError("work_id must be an exact canonical identifier")
    return work_id


def canonical_decision_id(value: Any) -> str:
    decision_id = _one_line("decision_id", value, 80)
    if decision_id != value or not _DECISION_ID_RE.fullmatch(decision_id):
        raise ValidationError(
            "decision_id must be an exact lowercase canonical slug"
        )
    return decision_id


def canonical_decision_option(value: Any) -> str:
    option = _one_line("decision option", value, 32)
    if option != value or not _DECISION_OPTION_RE.fullmatch(option):
        raise ValidationError(
            "decision option must be an exact lowercase canonical identifier"
        )
    return option


def canonical_wake_role(value: Any) -> str:
    role = _one_line("role", value, 32)
    if role != value or not _WAKE_ROLE_RE.fullmatch(role):
        raise ValidationError(
            "role must be a lowercase name of letters, digits and dashes "
            "(for example operator)"
        )
    return role


def canonical_wake_thread(value: Any) -> str:
    """One exact native conversation: Codex names it by lowercase UUID."""

    thread = _one_line("thread", value, 36)
    try:
        exact = str(uuid.UUID(thread)) == value
    except ValueError:
        exact = False
    if not exact:
        raise ValidationError(
            "thread must be the conversation's exact lowercase UUID, not a title"
        )
    return thread


def canonical_wake_expectation(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """One complete assertion about the role binding a caller inspected.

    Omission preserves legacy wake behavior. Claude's bound pair identifies the
    recorded binding owner; it does not independently verify the inbox receiver.
    Return a fresh mapping so validation does not retain caller-owned state.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("expected binding must be a complete provider-specific object")
    provider = value.get("provider")
    if not isinstance(provider, str) or provider not in WAKE_PROVIDERS:
        raise ValidationError("expected binding provider must be codex or claude")
    fields = (
        {"generation", "provider", "thread"} if provider == "codex"
        else {"generation", "provider", "bound_agent", "bound_session"}
    )
    if set(value) != fields:
        raise ValidationError("expected binding must contain exactly its provider's complete fields")
    expected: dict[str, Any] = {
        "generation": _positive_integer("expected binding generation", value["generation"]),
        "provider": provider,
    }
    if provider == "codex":
        expected["thread"] = canonical_wake_thread(value["thread"])
    else:
        for field in ("bound_agent", "bound_session"):
            identifier = canonical_agent(value[field])
            if identifier != value[field]:
                raise ValidationError(f"expected binding {field} must be an exact canonical identifier")
            expected[field] = identifier
    return expected


def canonical_wake_project(value: Any) -> str | None:
    """A safe optional display label, never a repository identity."""

    if value is None:
        return None
    project = _one_line("sender project", value, 80)
    if project != value or not _WAKE_PROJECT_RE.fullmatch(project):
        raise ValidationError("sender project must be an exact lowercase name of letters, digits and dashes")
    return project


def canonical_wake_sender(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """An observed source binding snapshot, not sender authentication or authority.

    Omission preserves old attempts. A supplied snapshot is complete;
    its ledger is a followable absolute pointer, not a cross-ledger assertion.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"ledger", "role", "generation", "project"}:
        raise ValidationError("wake sender must contain exactly ledger, role, generation and project")
    generation = _positive_integer("sender generation", value["generation"])
    if generation > 10**12:
        raise ValidationError("sender generation is out of range")
    return {
        "ledger": _absolute_path("sender ledger", value["ledger"]),
        "role": canonical_wake_role(value["role"]),
        "generation": generation,
        "project": canonical_wake_project(value["project"]),
    }


def canonical_wake_message_id(value: Any) -> str:
    message_id = _one_line("message id", value, 128)
    if message_id != value or not _WAKE_MESSAGE_ID_RE.fullmatch(message_id):
        raise ValidationError(
            "message id must be 1 to 128 letters, digits, dots, colons, "
            "underscores or dashes"
        )
    return message_id


def canonical_wake_ref(value: Any) -> str:
    """A pointer the recipient can follow: an absolute file path, a sequence in the recipient's ledger, or
    ledger:/checkout#N, a sequence in that checkout's ledger."""

    ref = _one_line("ref", value, 400)
    ledger = _WAKE_LEDGER_REF_RE.fullmatch(ref)
    if ledger and (os.path.normpath(ledger.group(1)) != ledger.group(1) or ledger.group(1).startswith("//")):
        raise ValidationError("ledger:/checkout#N needs the checkout's normalized absolute path")
    if ref != value or not (
        _WAKE_SEQUENCE_REF_RE.fullmatch(ref) or ledger or ref.startswith("/")
    ):
        raise ValidationError(
            "ref must be an absolute task-file path, a ledger sequence number or ledger:/checkout#N"
        )
    return ref


def wake_ref_sequence(ref: str) -> tuple[str | None, int] | None:
    """For a sequence reference, the checkout whose ledger holds it (None: the recipient's own) and the
    sequence; None for a task file."""

    if _WAKE_SEQUENCE_REF_RE.fullmatch(ref):
        return None, int(ref)
    match = _WAKE_LEDGER_REF_RE.fullmatch(ref)
    return (match.group(1), int(match.group(2))) if match else None


def canonical_wake_content(ref: str, sha256: Any, size: Any) -> None:
    """A file reference names its bytes by sha256 and size; a sequence names neither."""

    if wake_ref_sequence(ref) is not None:
        if sha256 is not None or size is not None:
            raise ValidationError("a ledger sequence reference carries no file sha256 or size")
        return
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise ValidationError("a file reference needs its lowercase sha256")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValidationError("a file reference needs its size in bytes")


def _absolute_path(name: str, value: Any) -> str:
    path = _one_line(name, value, 400)
    if path != value or not path.startswith("/"):
        raise ValidationError(f"{name} must be an absolute path")
    return path


def _positive_integer(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def _wake_target(meta: Mapping[str, Any]) -> None:
    """A Codex target is a conversation; a Claude Code target is only its inbox."""

    provider = meta.get("provider")
    if provider not in WAKE_PROVIDERS:
        raise ValidationError("a wake provider must be codex or claude")
    if provider == "codex":
        canonical_wake_thread(meta.get("thread"))
    elif "thread" in meta or "cwd" in meta:
        raise ValidationError("a Claude Code wake target is its inbox, with no thread or cwd")


def _validate_wake(kind: str, meta: Mapping[str, Any]) -> None:
    canonical_wake_role(meta.get("role"))
    if "approval_ref" in meta:
        if not _ARTIFACT_RE.fullmatch(str(meta["approval_ref"])):
            raise ValidationError("approval_ref must be an immutable git:, sha256: or receipt: reference")
        _one_line("reason", meta.get("reason"), 300)
    for key in ("charter", "role_scope", "reason"):
        if key in meta:
            _one_line(key, meta[key], 300)
    if "previous_holder" in meta:
        previous_holder = _one_line("previous_holder", meta["previous_holder"], 401)
        if not _RECIPIENT_RE.fullmatch(previous_holder):
            raise ValidationError("previous_holder contains unsupported characters")
    if kind == "wake.bound":
        _wake_target(meta)
        endpoint = _one_line("endpoint", meta.get("endpoint"), 408)
        if not endpoint.startswith("unix://"):
            raise ValidationError("endpoint must be unix:// and an absolute socket path")
        _absolute_path("endpoint", endpoint.removeprefix("unix://"))
        if meta["provider"] == "codex":
            _absolute_path("cwd", meta.get("cwd"))
        if "replaces" in meta:
            _positive_integer("replaces", meta["replaces"])
        return
    _positive_integer("generation", meta.get("generation"))
    if kind == "wake.attempted":
        _wake_target(meta)
        if "sender" in meta and canonical_wake_sender(meta["sender"]) is None:
            raise ValidationError("a supplied wake sender must be a complete object")
        canonical_wake_message_id(meta.get("message_id"))
        canonical_wake_content(
            canonical_wake_ref(meta.get("ref")), meta.get("ref_sha256"), meta.get("ref_size")
        )
        if meta.get("requested") not in WAKE_REQUESTS:
            raise ValidationError("requested transport must be queue or steer")
    elif kind == "wake.concluded":
        _positive_integer("attempt_seq", meta.get("attempt_seq"))
        canonical_wake_message_id(meta.get("message_id"))
        conclusion = (meta.get("outcome"), meta.get("reason"), meta.get("transport"))
        if not any(conclusion in allowed for allowed in WAKE_CONCLUSIONS.values()):
            raise ValidationError(
                "wake outcome, reason and transport are not an admissible conclusion"
            )
        if "detail" in meta:
            _one_line("detail", meta["detail"], 300)
        native_id = meta.get("native_id")
        if native_id is not None:
            canonical_wake_message_id(native_id)
        elif meta.get("outcome") == "steered":
            raise ValidationError("a steered wake records the native turn id")


def normalize_event(raw: Mapping[str, Any], *, internal: bool = False) -> Event:
    if not isinstance(raw, Mapping):
        raise ValidationError("event must be a JSON object")

    allowed_fields = {
        "v",
        "id",
        "kind",
        "agent",
        "session",
        "work_id",
        "target",
        "scope",
        "summary",
        "artifact",
        "meta",
    }
    unknown = sorted(set(raw) - allowed_fields)
    if unknown:
        raise ValidationError(f"unknown event fields: {', '.join(unknown)}")

    version = raw.get("v", PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        raise ValidationError(
            f"unsupported event protocol version {version!r}; expected {PROTOCOL_VERSION}"
        )

    kind = _one_line("kind", raw.get("kind"), 64)
    if kind not in EVENT_KINDS:
        raise ValidationError(f"unsupported event kind: {kind}")
    if kind in INTERNAL_EVENT_KINDS and not internal:
        raise ValidationError(
            f"{kind} is emitted only by a dedicated Multithread transaction"
        )

    event_id = raw.get("id") or new_event_id()
    event_id = _one_line("id", event_id, 200)
    if not _EVENT_ID_RE.fullmatch(event_id):
        raise ValidationError("event id contains unsupported characters or is too short")

    agent = _identifier("agent", raw.get("agent"))
    session = _identifier("session", raw.get("session"))
    summary = _one_line("summary", raw.get("summary"), 500)

    work_id = _optional_identifier("work_id", raw.get("work_id"))
    if kind == "work.intent" and work_id is None:
        raise ValidationError("work.intent requires a stable work_id")
    # Two 200-character identifiers plus compact JSON delimiters: at most 407.
    target = canonical_target(raw.get("target"))
    scope = _optional_line("scope", raw.get("scope"), 240)
    artifact = _optional_line("artifact", raw.get("artifact"), 200)
    if artifact is not None and not _ARTIFACT_RE.fullmatch(artifact):
        raise ValidationError(
            "artifact must be git:<oid>, sha256:<digest>, or receipt:<stable-id>"
        )
    if artifact is not None and artifact.startswith("receipt:"):
        parts = artifact.removeprefix("receipt:").split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValidationError("receipt artifact contains an unsafe path segment")
    if kind in {"work.handoff", "review.requested"} and artifact is None:
        raise ValidationError(f"{kind} requires an immutable artifact")
    if kind == "provider.reviewed" and (artifact is None or not artifact.startswith("sha256:")):
        raise ValidationError("provider review requires its report as a sha256 artifact")
    if kind in {"decision.requested", "decision.responded"}:
        if work_id is None:
            raise ValidationError(f"{kind} requires work_id")
        if scope is None:
            raise ValidationError(f"{kind} requires scope")
        if artifact is None or _DECISION_ARTIFACT_RE.fullmatch(artifact) is None:
            raise ValidationError(
                f"{kind} requires an immutable git:<40-hex> artifact"
            )
        if len(summary) > 300:
            raise ValidationError(f"{kind} summary exceeds 300 characters ({len(summary)} given, "
                                  f"{len(summary) - 300} over)")
        expected_route = (
            ("claude", "codex")
            if kind == "decision.requested"
            else ("codex", "claude")
        )
        if (agent, target) != expected_route:
            raise ValidationError(
                f"{kind} requires {expected_route[0]} to {expected_route[1]} routing"
            )

    meta_raw = raw.get("meta", {})
    if not isinstance(meta_raw, Mapping):
        raise ValidationError("meta must be a JSON object")
    meta = _normalize_meta(kind, meta_raw)
    _validate_semantics(kind, meta, internal=internal)
    if kind.startswith("wake.") and target != meta["role"]:
        raise ValidationError(f"{kind} must target its role")
    commit_oid = meta.get("commit_oid")
    if (
        artifact is not None
        and artifact.startswith("git:")
        and commit_oid is not None
        and artifact != f"git:{commit_oid}"
    ):
        raise ValidationError("git artifact and commit_oid must identify the same object")

    normalized: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "id": event_id,
        "kind": kind,
        "agent": agent,
        "session": session,
        "summary": summary,
    }
    for key, value in (
        ("work_id", work_id),
        ("target", target),
        ("scope", scope),
        ("artifact", artifact),
    ):
        if value is not None:
            normalized[key] = value
    if meta:
        normalized["meta"] = meta

    encoded = canonical_json(normalized)
    if len(encoded.encode("utf-8")) > 8_192:
        raise ValidationError("event exceeds the 8192-byte protocol limit")
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return Event(
        v=PROTOCOL_VERSION,
        event_id=event_id,
        kind=kind,
        agent=agent,
        session=session,
        summary=summary,
        work_id=work_id,
        target=target,
        scope=scope,
        artifact=artifact,
        meta=meta,
        canonical_json=encoded,
        body_hash=digest,
    )


def _validate_semantics(
    kind: str,
    meta: Mapping[str, Any],
    *,
    internal: bool,
) -> None:
    if kind == "friction.observed":
        _fingerprint(meta.get("fingerprint"))
        category = meta.get("category")
        if category not in FRICTION_CATEGORIES:
            raise ValidationError(
                f"friction category must be one of: {', '.join(sorted(FRICTION_CATEGORIES))}"
            )
        _optional_nonnegative_int("cost_seconds", meta.get("cost_seconds"))
    elif kind == "ratchet.decided":
        _fingerprint(meta.get("fingerprint"))
        if meta.get("mode") not in RATCHET_MODES:
            raise ValidationError("ratchet mode must be drop, subtract, or promote")
        if meta.get("mode") == "drop" and not internal:
            raise ValidationError(
                "drop decision is emitted only by the ratchet transaction"
            )
        _one_line("home", meta.get("home"), 200)
        if meta.get("verify_when") is not None:
            _one_line("verify_when", meta.get("verify_when"), 200)
    elif kind == "ratchet.verified":
        _fingerprint(meta.get("fingerprint"))
        drop_compatibility = meta.get("drop_compatibility")
        if drop_compatibility is not None and drop_compatibility is not True:
            raise ValidationError("drop_compatibility must be true when present")
        if drop_compatibility is True:
            if not internal:
                raise ValidationError(
                    "drop compatibility closure is emitted only by the ratchet transaction"
                )
            if meta.get("outcome") != "dropped":
                raise ValidationError(
                    "drop compatibility closure requires outcome dropped"
                )
            if any(
                key in meta
                for key in (
                    "before_cost_seconds",
                    "after_cost_seconds",
                    "evidence",
                )
            ):
                raise ValidationError(
                    "drop compatibility closure cannot claim empirical verification"
                )
        elif meta.get("outcome") not in RATCHET_OUTCOMES:
            raise ValidationError("ratchet outcome must be improved, unchanged, or worse")
        decision_seq = meta.get("decision_seq")
        if not isinstance(decision_seq, int) or isinstance(decision_seq, bool) or decision_seq < 1:
            raise ValidationError("ratchet verification requires a positive decision_seq")
        before = _optional_nonnegative_int(
            "before_cost_seconds", meta.get("before_cost_seconds")
        )
        after = _optional_nonnegative_int(
            "after_cost_seconds", meta.get("after_cost_seconds")
        )
        if (before is None) != (after is None):
            raise ValidationError(
                "before_cost_seconds and after_cost_seconds must be supplied together"
            )
    elif kind in {"work.intent", "work.blocked", "work.handoff", "review.requested"}:
        commit_oid = meta.get("commit_oid")
        if commit_oid is not None and (
            not isinstance(commit_oid, str)
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_oid) is None
        ):
            raise ValidationError("commit_oid must be a full Git object ID")
        for key in ("evidence_sha256", "relay_trailers_sha256"):
            digest = meta.get(key)
            if digest is not None and (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise ValidationError(f"{key} must be a lowercase SHA-256 digest")
    elif kind.startswith("claim."):
        _identifier("claim_id", meta.get("claim_id"))
        canonical_resource(_required_string("resource", meta.get("resource")))
    elif kind.startswith("wake."):
        _validate_wake(kind, meta)
    elif kind == "provider.reviewed":
        _validate_provider_review(meta)
    elif kind == "delivery.acknowledged":
        signal_seq = meta.get("signal_seq")
        if (
            not isinstance(signal_seq, int)
            or isinstance(signal_seq, bool)
            or signal_seq < 1
        ):
            raise ValidationError(
                "delivery acknowledgement requires a positive signal_seq"
            )
        signal_event_id = _one_line(
            "signal_event_id", meta.get("signal_event_id"), 200
        )
        if not _EVENT_ID_RE.fullmatch(signal_event_id):
            raise ValidationError("signal_event_id contains unsupported characters")
        _identifier("target_agent", meta.get("target_agent"))
    elif kind == "decision.requested":
        canonical_decision_id(meta.get("decision_id"))
        if meta.get("authority_hint") not in DECISION_AUTHORITY_HINTS:
            raise ValidationError(
                "decision authority_hint must be engineering or uncertain"
            )
        option_ids = meta.get("option_ids")
        if option_ids is not None:
            if not isinstance(option_ids, list) or not 2 <= len(option_ids) <= 8:
                raise ValidationError(
                    "decision request requires 2 to 8 options when options are present"
                )
            normalized = [canonical_decision_option(item) for item in option_ids]
            if len(normalized) != len(set(normalized)):
                raise ValidationError("decision request options must be unique")
    elif kind == "decision.responded":
        canonical_decision_id(meta.get("decision_id"))
        request_seq = meta.get("request_seq")
        if (
            not isinstance(request_seq, int)
            or isinstance(request_seq, bool)
            or request_seq < 1
        ):
            raise ValidationError(
                "decision response requires a positive request_seq"
            )
        request_event_id = _one_line(
            "request_event_id", meta.get("request_event_id"), 200
        )
        if not _EVENT_ID_RE.fullmatch(request_event_id):
            raise ValidationError(
                "decision response request_event_id is invalid"
            )
        resolution = meta.get("resolution")
        authority_class = meta.get("authority_class")
        if resolution not in DECISION_RESOLUTIONS:
            raise ValidationError("unsupported decision response resolution")
        if authority_class not in DECISION_AUTHORITY_CLASSES:
            raise ValidationError("unsupported decision authority class")
        allowed_authority = {
            "choice": frozenset({"engineering"}),
            "directive": frozenset({"engineering"}),
            "escalate": frozenset(
                {"human-only", "external-authorization"}
            ),
            "needs-evidence": frozenset({"undetermined"}),
        }
        if authority_class not in allowed_authority[resolution]:
            raise ValidationError(
                f"{resolution} resolution is incompatible with "
                f"{authority_class} authority"
            )
        choice = meta.get("choice")
        if resolution == "choice":
            canonical_decision_option(choice)
        elif choice is not None:
            raise ValidationError(
                "decision choice is allowed only for choice resolution"
            )


#: Every canary a review plants; a review is recorded only when its control fired all of them. Reviews
#: recorded before the tool canary stay valid events; calls no longer accept them.
PROVIDER_REVIEW_CANARIES = "local-hook,mcp-server,project-hook,tool-hook"
_EARLIER_REVIEW_CANARIES = "local-hook,mcp-server,project-hook"
_NAMES_RE = re.compile(r"^(?:none|[A-Za-z0-9][A-Za-z0-9._@-]{0,79}(?:,[A-Za-z0-9][A-Za-z0-9._@-]{0,79}){0,63})$")


def _validate_provider_review(meta: Mapping[str, Any]) -> None:
    """A passing review of one exact provider binary for restricted calls, with its evidence."""
    missing = sorted(_META_KEYS["provider.reviewed"] - set(meta))
    if missing:
        raise ValidationError(f"provider review requires {', '.join(missing)}")
    if meta["provider"] != "claude":
        raise ValidationError("provider review covers claude only")
    for key in ("binary_sha256", "surface_sha256"):
        if not isinstance(meta[key], str) or re.fullmatch(r"[0-9a-f]{64}", meta[key]) is None:
            raise ValidationError(f"{key} must be a lowercase SHA-256 digest")
    if not isinstance(meta["version"], str) or re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,6}", meta["version"]) is None:
        raise ValidationError("provider review version must be MAJOR.MINOR.PATCH")
    for key in ("plugins", "agents"):
        if not isinstance(meta[key], str) or _NAMES_RE.fullmatch(meta[key]) is None:
            raise ValidationError(f"{key} must be comma-separated names or none")
    if meta["control_fired"] not in (PROVIDER_REVIEW_CANARIES, _EARLIER_REVIEW_CANARIES):
        raise ValidationError("a provider review is recorded only when its control fired every canary")
    if meta["restricted_fired"] != "none":
        raise ValidationError("a provider review is recorded only when its restricted call fired no canary")


def _normalize_meta(
    kind: str, raw: Mapping[str, Any]
) -> dict[str, Any]:
    allowed = _META_KEYS[kind]
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValidationError(
            f"metadata keys not allowed for {kind}: {', '.join(unknown)}"
        )

    out: dict[str, Any] = {}
    for key, value in raw.items():
        if _SECRET_KEY_RE.search(str(key)):
            raise ValidationError(f"secret-shaped metadata key rejected: {key}")
        if kind == "wake.attempted" and key == "sender":
            sender = canonical_wake_sender(value)
            if sender is None:
                raise ValidationError("a supplied wake sender must be a complete object")
            out[key] = sender
        elif isinstance(value, bool):
            out[str(key)] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            if abs(value) > 10**12:
                raise ValidationError(f"metadata integer is out of range: {key}")
            out[str(key)] = value
        elif isinstance(value, str):
            out[str(key)] = _one_line(f"meta.{key}", value, 500)
        elif kind == "decision.requested" and key == "option_ids":
            if not isinstance(value, (list, tuple)):
                raise ValidationError("decision option_ids must be an array")
            out[str(key)] = [
                canonical_decision_option(item) for item in value
            ]
        else:
            raise ValidationError(
                "metadata values must be strings, integers, booleans, "
                f"or allowlisted decision arrays: {key}"
            )
    encoded = canonical_json(out)
    if len(encoded.encode("utf-8")) > 4_096:
        raise ValidationError("metadata exceeds the 4096-byte limit")
    return out


def _identifier(name: str, value: Any) -> str:
    text = _one_line(name, value, 200)
    if not _IDENT_RE.fullmatch(text):
        raise ValidationError(f"{name} contains unsupported characters")
    return text


def _optional_identifier(name: str, value: Any) -> str | None:
    if value is None:
        return None
    return _identifier(name, value)


def _required_string(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    return value


def _one_line(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise ValidationError(f"{name} must not be empty")
    if len(text) > maximum:
        raise ValidationError(f"{name} exceeds {maximum} characters ({len(text)} given, {len(text) - maximum} over)")
    if any(ch in text for ch in ("\x00", "\r", "\n")):
        raise ValidationError(f"{name} must be one line and contain no NUL")
    if any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in text):
        raise ValidationError(f"{name} contains a control or direction-format character")
    if any(pattern.search(text) for pattern in _SECRET_VALUE_RES):
        raise ValidationError(f"{name} appears to contain a credential")
    return text


def _optional_line(name: str, value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    return _one_line(name, value, maximum)


def _fingerprint(value: Any) -> str:
    text = _one_line("fingerprint", value, 80)
    if not _FINGERPRINT_RE.fullmatch(text):
        raise ValidationError(
            "fingerprint must be a stable lowercase slug (letters, numbers, dot, dash, underscore)"
        )
    return text


def _optional_nonnegative_int(name: str, value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{name} must be a nonnegative integer")
    return value
