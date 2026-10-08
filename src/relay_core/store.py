"""SQLite authority for Multithread.

The ledger is shared by every worktree through the repository's Git common
directory.  Events are immutable; claims are the one intentionally mutable
projection and may move only from active to released/broken.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import threading
import time
from typing import Any, Iterator, Mapping, Sequence

from .protocol import (
    BusyError,
    ConflictError,
    DECISION_ROLLOUT_FENCE,
    PROVIDER_REVIEW_CANARIES,
    Event,
    RelayError,
    StateError,
    ValidationError,
    WAKE_BINDING_KINDS,
    WAKE_CONCLUSIONS,
    WAKE_REQUESTS,
    canonical_agent,
    canonical_json,
    session_target,
    canonical_decision_id,
    canonical_decision_option,
    canonical_resource,
    canonical_wake_content,
    canonical_wake_expectation,
    canonical_wake_message_id,
    canonical_wake_project,
    canonical_wake_ref,
    canonical_wake_role,
    canonical_wake_sender,
    canonical_wake_thread,
    canonical_work_id,
    new_event_id,
    normalize_event,
)


SCHEMA_VERSION = 2
_INSTALLED_ACCESS = None
_INSTALLED_ACCESS_LOCK = threading.Lock()


def _bind_installed_access(access) -> None:
    """Private, one-shot admission from the verified installed dispatcher."""
    global _INSTALLED_ACCESS
    with _INSTALLED_ACCESS_LOCK:
        if _INSTALLED_ACCESS is not None:
            raise StateError("installed Multithread admission is already bound")
        access.verify()
        _INSTALLED_ACCESS = access

#: Directory holding this package's own source, used to derive the workspace binding.
_PACKAGE_SOURCE_ROOT = Path(__file__).resolve().parent
CHANNEL_DELIVERY_SIGNAL_KINDS = (
    "work.blocked",
    "work.handoff",
    "review.requested",
    "decision.responded",
)
DELIVERY_SIGNAL_KINDS = (
    *CHANNEL_DELIVERY_SIGNAL_KINDS,
    "decision.requested",
)
CHANNEL_PENDING_LIMIT = 1
BRIEF_DEFAULT_LIMIT = 5
BRIEF_MAX_LIMIT = 10
_ACTIONABLE_RATCHET_STATES = frozenset(
    {"observing", "recurred", "decided", "unchanged", "worse"}
)

def _ratchet_meta_invalid_sql(expression: str) -> str:
    """Return a fail-closed invariant for reducer-visible JSON metadata."""

    return f"""
    CASE
      WHEN json_valid({expression}) = 0 THEN 1
      WHEN json_type({expression}) <> 'object' THEN 1
      WHEN json({expression}) <> {expression} THEN 1
      WHEN (
        SELECT COUNT(*) FROM json_each({expression})
      ) <> (
        SELECT COUNT(DISTINCT key) FROM json_each({expression})
      ) THEN 1
      WHEN (
        SELECT json_group_array(key) FROM json_each({expression})
      ) <> (
        SELECT json_group_array(key)
        FROM (
          SELECT key FROM json_each({expression}) ORDER BY key
        )
      ) THEN 1
      ELSE 0
    END
    """


_RATCHET_META_INVALID_NEW = _ratchet_meta_invalid_sql("NEW.meta_json")
_RATCHET_META_INVALID_STORED = _ratchet_meta_invalid_sql("event.meta_json")
_RATCHET_STATE_TRIGGER_NAMES = (
    "ratchet_decision_must_be_admissible",
    "ratchet_empirical_verification_must_be_admissible",
    "ratchet_drop_closure_must_be_exact",
    "ratchet_meta_json_must_be_canonical",
)

_RATCHET_STATE_TRIGGERS = f"""
CREATE TRIGGER IF NOT EXISTS ratchet_decision_must_be_admissible
BEFORE INSERT ON events
WHEN NEW.kind = 'ratchet.decided'
  AND (
    json_type(NEW.meta_json, '$.fingerprint') IS NOT 'text'
    OR length(json_extract(NEW.meta_json, '$.fingerprint')) NOT BETWEEN 3 AND 80
    OR json_type(NEW.meta_json, '$.home') IS NOT 'text'
    OR length(json_extract(NEW.meta_json, '$.home')) NOT BETWEEN 1 AND 200
    OR json_type(NEW.meta_json, '$.mode') IS NOT 'text'
    OR json_extract(NEW.meta_json, '$.mode')
       NOT IN ('subtract', 'promote', 'drop')
    OR (
      json_type(NEW.meta_json, '$.verify_when') IS NOT NULL
      AND (
        json_type(NEW.meta_json, '$.verify_when') IS NOT 'text'
        OR length(json_extract(NEW.meta_json, '$.verify_when')) NOT BETWEEN 1 AND 200
      )
    )
    OR NOT EXISTS (
      SELECT 1
      FROM events AS observation
      WHERE observation.kind = 'friction.observed'
        AND json_extract(observation.meta_json, '$.fingerprint')
            = json_extract(NEW.meta_json, '$.fingerprint')
    )
    OR EXISTS (
      SELECT 1
      FROM events AS prior_drop
      WHERE prior_drop.kind = 'ratchet.decided'
        AND json_extract(prior_drop.meta_json, '$.fingerprint')
            = json_extract(NEW.meta_json, '$.fingerprint')
        AND json_extract(prior_drop.meta_json, '$.mode') = 'drop'
        AND NOT EXISTS (
          SELECT 1
          FROM events AS recurrence
          WHERE recurrence.kind = 'friction.observed'
            AND recurrence.seq > prior_drop.seq
            AND json_extract(recurrence.meta_json, '$.fingerprint')
                = json_extract(prior_drop.meta_json, '$.fingerprint')
        )
    )
  )
BEGIN
  SELECT RAISE(
    ABORT,
    'ratchet decision violates mode, observation, or drop-recurrence invariant'
  );
END;

CREATE TRIGGER IF NOT EXISTS ratchet_empirical_verification_must_be_admissible
BEFORE INSERT ON events
WHEN NEW.kind = 'ratchet.verified'
  AND json_type(NEW.meta_json, '$.drop_compatibility') IS NULL
  AND (
    json_type(NEW.meta_json, '$.fingerprint') IS NOT 'text'
    OR length(json_extract(NEW.meta_json, '$.fingerprint')) NOT BETWEEN 3 AND 80
    OR json_type(NEW.meta_json, '$.decision_seq') IS NOT 'integer'
    OR json_extract(NEW.meta_json, '$.decision_seq') < 1
    OR json_type(NEW.meta_json, '$.outcome') IS NOT 'text'
    OR json_extract(NEW.meta_json, '$.outcome')
       NOT IN ('improved', 'unchanged', 'worse')
    OR NOT EXISTS (
      SELECT 1
      FROM events AS decision
      WHERE decision.seq = json_extract(NEW.meta_json, '$.decision_seq')
        AND decision.kind = 'ratchet.decided'
        AND json_extract(decision.meta_json, '$.fingerprint')
            = json_extract(NEW.meta_json, '$.fingerprint')
        AND json_extract(decision.meta_json, '$.mode') IN ('subtract', 'promote')
    )
    OR EXISTS (
      SELECT 1
      FROM events AS prior_verification
      WHERE prior_verification.kind = 'ratchet.verified'
        AND json_type(
          prior_verification.meta_json, '$.drop_compatibility'
        ) IS NULL
        AND json_extract(prior_verification.meta_json, '$.decision_seq')
            = json_extract(NEW.meta_json, '$.decision_seq')
    )
  )
BEGIN
  SELECT RAISE(
    ABORT,
    'ratchet empirical verification violates outcome, decision, or uniqueness invariant'
  );
END;

CREATE TRIGGER IF NOT EXISTS ratchet_drop_closure_must_be_exact
BEFORE INSERT ON events
WHEN NEW.kind = 'ratchet.verified'
  AND (
    json_type(NEW.meta_json, '$.drop_compatibility') IS NOT NULL
    OR json_extract(NEW.meta_json, '$.outcome') = 'dropped'
  )
  AND NOT EXISTS (
    SELECT 1
    FROM events AS decision
    WHERE decision.seq = CAST(
      json_extract(NEW.meta_json, '$.decision_seq') AS INTEGER
    )
      AND decision.kind = 'ratchet.decided'
      AND json_extract(decision.meta_json, '$.mode') = 'drop'
      AND json_type(NEW.meta_json, '$.drop_compatibility') = 'true'
      AND json_extract(NEW.meta_json, '$.outcome') = 'dropped'
      AND NEW.event_id = ('drop-close:' || CAST(decision.seq AS TEXT))
      AND NEW.protocol_version = decision.protocol_version
      AND NEW.agent = decision.agent
      AND NEW.session = decision.session
      AND NEW.work_id IS decision.work_id
      AND NEW.target IS decision.target
      AND NEW.scope IS decision.scope
      AND NEW.artifact IS NULL
      AND NEW.summary = (
        'Compatibility closure for terminal ratchet drop '
        || CAST(decision.seq AS TEXT)
      )
      AND json_extract(NEW.meta_json, '$.fingerprint')
          = json_extract(decision.meta_json, '$.fingerprint')
      AND (SELECT COUNT(*) FROM json_each(NEW.meta_json)) = 4
  )
BEGIN
  SELECT RAISE(ABORT, 'invalid ratchet drop compatibility closure');
END;

CREATE TRIGGER IF NOT EXISTS ratchet_meta_json_must_be_canonical
BEFORE INSERT ON events
WHEN NEW.kind IN (
  'friction.observed', 'ratchet.decided', 'ratchet.verified'
)
  AND ({_RATCHET_META_INVALID_NEW})
BEGIN
  SELECT RAISE(
    ABORT,
    'ratchet metadata must be normalized duplicate-free JSON'
  );
END;
"""

_SCHEMA = f"""
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  protocol_version INTEGER NOT NULL,
  kind TEXT NOT NULL,
  agent TEXT NOT NULL,
  session TEXT NOT NULL,
  work_id TEXT,
  target TEXT,
  scope TEXT,
  summary TEXT NOT NULL,
  artifact TEXT,
  meta_json TEXT NOT NULL,
  canonical_json TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  recorded_at TEXT NOT NULL DEFAULT (
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
  )
);

CREATE INDEX events_kind_seq ON events(kind, seq);
CREATE INDEX events_target_seq ON events(target, seq) WHERE target IS NOT NULL;

CREATE TRIGGER events_are_append_only_update
BEFORE UPDATE ON events
BEGIN
  SELECT RAISE(ABORT, 'relay events are append-only');
END;

CREATE TRIGGER events_are_append_only_delete
BEFORE DELETE ON events
BEGIN
  SELECT RAISE(ABORT, 'relay events are append-only');
END;

{_RATCHET_STATE_TRIGGERS}

CREATE TABLE claims (
  claim_id TEXT PRIMARY KEY,
  resource TEXT NOT NULL,
  holder_agent TEXT NOT NULL,
  holder_session TEXT NOT NULL,
  purpose TEXT NOT NULL,
  acquired_event_seq INTEGER NOT NULL REFERENCES events(seq),
  released_event_seq INTEGER REFERENCES events(seq),
  release_kind TEXT CHECK (release_kind IN ('released', 'broken')),
  released_by_agent TEXT,
  released_by_session TEXT,
  release_reason TEXT,
  CHECK (
    (released_event_seq IS NULL AND release_kind IS NULL
      AND released_by_agent IS NULL AND released_by_session IS NULL
      AND release_reason IS NULL)
    OR
    (released_event_seq IS NOT NULL AND release_kind IS NOT NULL
      AND released_by_agent IS NOT NULL AND released_by_session IS NOT NULL)
  )
);

CREATE UNIQUE INDEX claims_one_active_holder
ON claims(resource)
WHERE released_event_seq IS NULL;

CREATE TRIGGER claims_reject_legacy_main_insert
BEFORE INSERT ON claims
WHEN NEW.resource = 'integrate:main'
BEGIN
  SELECT RAISE(ABORT, 'legacy integrate:main claims require a v2 client');
END;

CREATE TRIGGER claims_cannot_be_deleted
BEFORE DELETE ON claims
BEGIN
  SELECT RAISE(ABORT, 'relay claim history cannot be deleted');
END;

CREATE TRIGGER claims_only_release_once
BEFORE UPDATE ON claims
WHEN
  OLD.claim_id != NEW.claim_id
  OR OLD.resource != NEW.resource
  OR OLD.holder_agent != NEW.holder_agent
  OR OLD.holder_session != NEW.holder_session
  OR OLD.purpose != NEW.purpose
  OR OLD.acquired_event_seq != NEW.acquired_event_seq
  OR OLD.released_event_seq IS NOT NULL
  OR NEW.released_event_seq IS NULL
BEGIN
  SELECT RAISE(ABORT, 'relay claims may only transition active to released');
END;
"""


@dataclass(frozen=True)
class RelayPaths:
    repo_root: Path
    git_common_dir: Path
    state_dir: Path
    database: Path
    hook_error_log: Path


def resolve_paths(
    repo: str | os.PathLike[str] | None = None,
    state_home: str | os.PathLike[str] | None = None,
    *,
    create_state: bool = True,
) -> RelayPaths:
    if _INSTALLED_ACCESS is not None:
        return _INSTALLED_ACCESS.resolve_paths(repo, state_home, create_state=create_state)
    cwd = Path(repo or os.getcwd()).resolve()
    common = _git_path(cwd, "--git-common-dir")
    root = _git_path(cwd, "--show-toplevel")
    # Identity is now known and nothing has been written.  Refuse here: every
    # branch below either derives a write location from `common` or opens state
    # the caller named, and a foreign workspace may do neither.  State location
    # never confers repository authority, so this precedes the `state_home` and
    # `create_state` branches alike.
    _assert_workspace_binding(common)

    if state_home is not None:
        state_dir = Path(os.path.abspath(Path(state_home).expanduser()))
    elif os.environ.get("RELAY_HOME"):
        state_dir = Path(
            os.path.abspath(Path(os.environ["RELAY_HOME"]).expanduser())
        )
    else:
        # Linked worktrees all point at the primary checkout's .git directory.
        # Keeping runtime state beside (not inside) .git makes it shared while
        # remaining writable inside normal repository sandboxes.
        common_owner = common.parent if common.name == ".git" else root
        state_dir = common_owner / ".relay"

    database = state_dir / "relay.sqlite3"
    hook_error_log = state_dir / "hook-errors.log"
    if create_state:
        _secure_state_dir(state_dir)
    else:
        _assert_no_symlink_components(state_dir)
        try:
            state_mode = state_dir.lstat().st_mode
            database_mode = database.lstat().st_mode
        except FileNotFoundError as exc:
            raise StateError("Multithread read-only state is unavailable") from exc
        except OSError as exc:
            raise StateError(f"cannot inspect Multithread read-only state: {exc}") from exc
        if not stat.S_ISDIR(state_mode) or not stat.S_ISREG(database_mode):
            raise StateError("Multithread read-only state is not a regular database")
    for path in (database, hook_error_log):
        _assert_no_symlink_components(path)
    return RelayPaths(
        repo_root=root,
        git_common_dir=common,
        state_dir=state_dir,
        database=database,
        hook_error_log=hook_error_log,
    )


class RelayStore:
    def __init__(
        self,
        paths: RelayPaths,
        *,
        busy_timeout_ms: int = 5_000,
        read_only: bool = False,
    ):
        self.paths = paths
        self._read_only = read_only
        try:
            database: str | Path = paths.database
            connect_options: dict[str, Any] = {}
            if read_only:
                database = f"{paths.database.resolve().as_uri()}?mode=ro"
                connect_options["uri"] = True
            if _INSTALLED_ACCESS is not None:
                self._db = _INSTALLED_ACCESS.connect(paths, read_only=read_only,
                    timeout=max(busy_timeout_ms, 0) / 1_000)
            else:
                self._db = sqlite3.connect(
                    database,
                    timeout=max(busy_timeout_ms, 0) / 1_000,
                    isolation_level=None,
                    **connect_options,
                )
            self._db.row_factory = sqlite3.Row
            self._db.execute(f"PRAGMA busy_timeout = {max(busy_timeout_ms, 0)}")
            self._db.execute("PRAGMA foreign_keys = ON")
            if read_only:
                self._db.execute("PRAGMA query_only = ON")
            initial_version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if initial_version > SCHEMA_VERSION:
                raise StateError(
                    f"Multithread database schema {initial_version} is newer than this client "
                    f"({SCHEMA_VERSION})"
                )
            if read_only:
                if initial_version != SCHEMA_VERSION:
                    raise StateError(
                        f"unsupported Multithread database schema {initial_version}"
                    )
                integrity = self._db.execute("PRAGMA integrity_check").fetchall()
                if len(integrity) != 1 or str(integrity[0][0]) != "ok":
                    raise StateError("Multithread database failed integrity check")
                return
            self._establish_wal()
            self._db.execute("PRAGMA synchronous = FULL")
            self._initialize_schema()
            self._secure_database_files()
        except RelayError:
            raise
        except sqlite3.OperationalError as exc:
            raise _translate_sqlite(exc) from exc
        except sqlite3.DatabaseError as exc:
            raise StateError(f"Multithread database is unreadable: {exc}") from exc

    @classmethod
    def open(
        cls,
        repo: str | os.PathLike[str] | None = None,
        state_home: str | os.PathLike[str] | None = None,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> "RelayStore":
        return cls(
            resolve_paths(repo=repo, state_home=state_home),
            busy_timeout_ms=busy_timeout_ms,
        )

    @classmethod
    def open_readonly(
        cls,
        repo: str | os.PathLike[str] | None = None,
        state_home: str | os.PathLike[str] | None = None,
        *,
        busy_timeout_ms: int = 150,
    ) -> "RelayStore":
        """Open an existing ledger without initializing or migrating it."""

        return cls(
            resolve_paths(
                repo=repo,
                state_home=state_home,
                create_state=False,
            ),
            busy_timeout_ms=busy_timeout_ms,
            read_only=True,
        )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "RelayStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def emit(self, raw: Mapping[str, Any], *, internal: bool = False) -> dict[str, Any]:
        if internal:
            raise ValidationError(
                "generic emit cannot acquire internal event authority"
            )
        event = normalize_event(raw)
        with self._transaction():
            seq, duplicate = self._insert_event(event)
        return self._event_receipt(seq, duplicate)

    def claim(
        self,
        resource: str,
        *,
        agent: str,
        session: str,
        purpose: str,
        claim_id: str | None = None,
    ) -> dict[str, Any]:
        resource = canonical_resource(resource)
        claim_id = claim_id or new_event_id("clm")
        event = normalize_event(
            {
                "v": 1,
                "id": f"claim:{claim_id}:acquired",
                "kind": "claim.acquired",
                "agent": agent,
                "session": session,
                "target": resource,
                "summary": f"Claimed {resource}: {purpose}",
                "meta": {
                    "claim_id": claim_id,
                    "resource": resource,
                    "holder_agent": agent,
                    "holder_session": session,
                    "purpose": purpose,
                },
            },
            internal=True,
        )
        purpose = str(event.meta["purpose"])

        with self._transaction():
            prior_id = self._db.execute(
                "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            if prior_id is not None:
                if (
                    prior_id["resource"] == resource
                    and prior_id["holder_agent"] == event.agent
                    and prior_id["holder_session"] == event.session
                    and prior_id["purpose"] == purpose
                ):
                    return self._claim_receipt(prior_id, duplicate=True)
                raise ConflictError(f"claim id {claim_id} already names different authority")

            active = self._db.execute(
                "SELECT * FROM claims WHERE resource = ? AND released_event_seq IS NULL",
                (resource,),
            ).fetchone()
            if active is not None:
                raise ConflictError(
                    f"{resource} is already claimed by "
                    f"{active['holder_agent']}:{active['holder_session']} "
                    f"as {active['claim_id']}"
                )

            seq, _ = self._insert_event(event)
            self._db.execute(
                """
                INSERT INTO claims (
                  claim_id, resource, holder_agent, holder_session, purpose,
                  acquired_event_seq
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (claim_id, resource, event.agent, event.session, purpose, seq),
            )
            row = self._db.execute(
                "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            assert row is not None
            return self._claim_receipt(row, duplicate=False)

    def release(self, claim_id: str, *, agent: str, session: str) -> dict[str, Any]:
        return self._end_claim(
            claim_id,
            actor_agent=agent,
            actor_session=session,
            kind="released",
            reason=None,
        )

    def break_claim(
        self,
        claim_id: str,
        *,
        actor_agent: str,
        actor_session: str,
        reason: str,
    ) -> dict[str, Any]:
        return self._end_claim(
            claim_id,
            actor_agent=actor_agent,
            actor_session=actor_session,
            kind="broken",
            reason=reason,
        )

    def events(self, *, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if after < 0:
            raise ValidationError("after must be nonnegative")
        if limit < 1 or limit > 500:
            raise ValidationError("limit must be between 1 and 500")
        rows = self._execute(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit)
        ).fetchall()
        return [self._event_row(row) for row in rows]

    def observed_sessions(self, agent: str, sessions: list[str]) -> set[str]:
        """Which of these provider sessions left any event here as this agent."""
        sessions = [session for session in sessions if isinstance(session, str)][:256]
        if not sessions:
            return set()
        marks = ",".join("?" * len(sessions))
        rows = self._execute(
            f"SELECT DISTINCT session FROM events WHERE agent = ? AND session IN ({marks})",
            (agent, *sessions),
        ).fetchall()
        return {row[0] for row in rows}

    def active_claims(self) -> list[dict[str, Any]]:
        rows = self._execute(
            """
            SELECT c.*, e.recorded_at AS acquired_at
            FROM claims c
            JOIN events e ON e.seq = c.acquired_event_seq
            WHERE c.released_event_seq IS NULL
            ORDER BY c.resource
            """
        ).fetchall()
        return [self._claim_row(row) for row in rows]

    def status(self, *, compact: bool = False) -> dict[str, Any]:
        claim_limit = 5 if compact else 50
        signal_limit = 8 if compact else 30
        ratchet_limit = 5 if compact else 30
        claims = self.active_claims()
        recent = self._execute(
            """
            SELECT * FROM events
            WHERE kind IN (
              'work.intent', 'work.blocked', 'work.handoff', 'review.requested',
              'friction.observed', 'ratchet.decided', 'ratchet.verified'
            )
              AND json_type(meta_json, '$.drop_compatibility') IS NULL
            ORDER BY seq DESC LIMIT ?
            """,
            (signal_limit + 1,),
        ).fetchall()
        ratchet = self.ratchet_review()
        max_seq = self._execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events"
        ).fetchone()[0]
        return {
            "schema_version": SCHEMA_VERSION,
            "database": str(self.paths.database),
            "last_seq": max_seq,
            "compact": compact,
            "limits": {
                "active_claims": claim_limit,
                "recent_signals": signal_limit,
                "ratchet": ratchet_limit,
            },
            "truncated": {
                "active_claims": len(claims) > claim_limit,
                "recent_signals": len(recent) > signal_limit,
                "ratchet": len(ratchet) > ratchet_limit,
            },
            "active_claims": [
                self._brief_claim(item) if compact else item
                for item in claims[:claim_limit]
            ],
            "recent_signals": [
                (
                    self._brief_event(self._event_row(row))
                    if compact
                    else self._event_row(row)
                )
                for row in reversed(recent[:signal_limit])
            ],
            "ratchet": [
                self._compact_ratchet_item(item) if compact else item
                for item in ratchet[:ratchet_limit]
            ],
        }

    def brief(
        self,
        agent: str,
        *,
        limit: int = BRIEF_DEFAULT_LIMIT,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Return bounded coordination context for one agent.

        Exact-session work is shown newest-first before the oldest generic
        and broadcast work; pages remain sequence ordered. Signals stay
        visible until their intended recipient explicitly acknowledges them.
        This is at-least-once delivery without a mutable inbox table.
        """

        agent = canonical_agent(agent)
        limit = _brief_limit(limit)
        claims = self.active_claims()
        intent_rows = self._execute(
            """
            SELECT * FROM events
            WHERE kind = 'work.intent'
            ORDER BY seq DESC LIMIT ?
            """,
            (limit + 1,),
        ).fetchall()
        pending = self.inbox(agent, session=session, limit=limit)
        signal_rows = pending["pending_signals"]
        actionable_ratchet = [
            item
            for item in self.ratchet_review()
            if item["state"] in _ACTIONABLE_RATCHET_STATES
        ]
        max_seq = int(
            self._execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
        )
        elsewhere = self._wakes_elsewhere(agent, session, limit) if session is not None else []
        return {
            "brief_version": 1,
            "agent": agent,
            "session": session,
            "pending_count": pending["pending_count"],
            "targeted_count": pending["targeted_count"],
            "last_seq": max_seq,
            "limit_per_section": limit,
            "truncated": {
                "active_claims": len(claims) > limit,
                "recent_intents": len(intent_rows) > limit,
                "pending_signals": pending["has_more"],
                "ratchet_items": len(actionable_ratchet) > limit,
                "wakes_elsewhere": len(elsewhere) > limit,
            },
            "wakes_elsewhere": elsewhere[:limit],
            "active_claims": [
                self._brief_claim(item) for item in claims[:limit]
            ],
            "recent_intents": [
                self._brief_event(self._event_row(row))
                for row in intent_rows[:limit]
            ],
            "pending_signals": [self._brief_event(row) for row in signal_rows],
            "ratchet_items": [
                self._compact_ratchet_item(item)
                for item in actionable_ratchet[:limit]
            ],
        }

    def _wakes_elsewhere(self, agent: str, session: str, limit: int) -> list[dict[str, Any]]:
        """Recent wakes recorded here for this exact recipient that point at another checkout's ledger. The
        signal is read and acknowledged there, so without this a lost wake would leave its recipient unaware."""
        since = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 86400))
        rows = self._execute(
            """
            SELECT attempt.seq, attempt.agent, attempt.session, attempt.target, attempt.recorded_at,
                   json_extract(attempt.meta_json, '$.ref') AS ref
            FROM events AS attempt JOIN events AS bound
              ON bound.seq = json_extract(attempt.meta_json, '$.generation') AND bound.kind = 'wake.bound'
            WHERE attempt.kind = 'wake.attempted' AND json_extract(attempt.meta_json, '$.ref') LIKE 'ledger:%'
              AND attempt.recorded_at >= ?
              AND ((json_extract(bound.meta_json, '$.provider') = 'codex' AND ? = 'codex'
                    AND json_extract(bound.meta_json, '$.thread') = ?)
                OR (json_extract(bound.meta_json, '$.provider') = 'claude' AND bound.agent = ? AND bound.session = ?))
            ORDER BY attempt.seq DESC LIMIT ?
            """,
            (since, agent, session, agent, session, limit + 1),
        ).fetchall()
        return [{"seq": int(row["seq"]), "ref": row["ref"], "role": row["target"],
                 "sender": f"{row['agent']}:{row['session']}", "recorded_at": row["recorded_at"]} for row in rows]

    def inbox(
        self, agent: str, *, session: str | None = None,
        limit: int = BRIEF_DEFAULT_LIMIT, after: int | None = None,
    ) -> dict[str, Any]:
        """Bounded pending delivery. Exact-session work leads a brief; pages use seq order."""
        agent = canonical_agent(agent)
        exact = session_target(agent, session) if session is not None else None
        limit = _inbox_limit(limit)
        if after is not None and (not isinstance(after, int) or isinstance(after, bool) or after < 0):
            raise ValidationError("inbox --after must be a nonnegative sequence")
        placeholders = ",".join("?" for _ in DELIVERY_SIGNAL_KINDS)
        where = f"""
            signal.kind IN ({placeholders})
            AND (signal.target IN (?, ?) OR signal.target IS NULL)
            AND NOT EXISTS (
                SELECT 1 FROM events AS ack
                WHERE ack.kind = 'delivery.acknowledged'
                AND ack.target = CASE WHEN signal.target = ? THEN ? ELSE ? END
                AND ack.scope = ('signal:' || CAST(signal.seq AS TEXT))
            )
        """
        params = (*DELIVERY_SIGNAL_KINDS, agent, exact, exact, exact, agent)
        counts = self._execute(
            f"SELECT COUNT(*), COALESCE(SUM(signal.target = ?), 0) FROM events AS signal WHERE {where}",
            (exact, *params),
        ).fetchone()
        if after is None:
            order = "CASE WHEN signal.target = ? THEN 0 ELSE 1 END, " \
                    "CASE WHEN signal.target = ? THEN -signal.seq ELSE signal.seq END"
            rows = self._execute(
                f"SELECT signal.* FROM events AS signal WHERE {where} ORDER BY {order} LIMIT ?",
                (*params, exact, exact, limit + 1),
            ).fetchall()
        else:
            rows = self._execute(
                f"SELECT signal.* FROM events AS signal WHERE {where} AND signal.seq > ? "
                "ORDER BY signal.seq LIMIT ?",
                (*params, after, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        return {
            "agent": agent, "session": session, "pending_count": int(counts[0]),
            "targeted_count": int(counts[1]), "has_more": len(rows) > limit,
            "next_after": int(selected[-1]["seq"]) if selected and after is not None else None,
            "pending_signals": [self._event_row(row) for row in selected],
        }

    def channel_pending(
        self,
        *,
        agent: str,
        source_agent: str,
        work_id: str,
        limit: int,
    ) -> dict[str, Any]:
        """Project one typed Codex-to-Claude signal without exposing its body."""

        agent = canonical_agent(agent)
        source_agent = canonical_agent(source_agent)
        if agent != "claude" or source_agent != "codex":
            raise ValidationError(
                "channel pending permits only source-agent codex to agent claude"
            )
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit != CHANNEL_PENDING_LIMIT
        ):
            raise ValidationError("channel pending limit must be exactly 1")
        if not isinstance(work_id, str):
            raise ValidationError("channel pending work_id must be a string")
        try:
            canonical_work_id = canonical_agent(work_id)
        except ValidationError as exc:
            raise ValidationError(
                "channel pending work_id contains unsupported characters"
            ) from exc
        if canonical_work_id != work_id:
            raise ValidationError(
                "channel pending work_id must be an exact canonical identifier"
            )

        placeholders = ",".join("?" for _ in CHANNEL_DELIVERY_SIGNAL_KINDS)
        signal_rows = self._execute(
            f"""
            SELECT signal.* FROM events AS signal
            WHERE signal.kind IN ({placeholders})
              AND signal.target = ?
              AND signal.agent = ?
              AND signal.work_id = ?
              AND NOT EXISTS (
                SELECT 1 FROM events AS acknowledgement
                WHERE acknowledgement.kind = 'delivery.acknowledged'
                  AND acknowledgement.target = ?
                  AND acknowledgement.scope =
                      ('signal:' || CAST(signal.seq AS TEXT))
              )
            ORDER BY signal.seq ASC
            LIMIT ?
            """,
            (
                *CHANNEL_DELIVERY_SIGNAL_KINDS,
                agent,
                source_agent,
                canonical_work_id,
                agent,
                CHANNEL_PENDING_LIMIT + 1,
            ),
        ).fetchall()

        for row in signal_rows:
            self._assert_canonical_event_row(row)
        selected = signal_rows[0] if signal_rows else None
        pending = (
            {"seq": int(selected["seq"]), "kind": str(selected["kind"])}
            if selected is not None
            else None
        )
        return {
            "v": 1,
            "pending": pending,
            "more": len(signal_rows) > CHANNEL_PENDING_LIMIT,
        }

    def decision_request(
        self,
        *,
        agent: str,
        session: str,
        decision_id: str,
        work_id: str,
        scope: str,
        summary: str,
        artifact: str,
        authority_hint: str,
        option_ids: Sequence[str] | None,
        rollout_fence: str,
    ) -> dict[str, Any]:
        """Append one fenced Claude-to-Codex engineering decision request.

        Rollout law: callers may not emit these version-1 internal kinds until
        this implementation is integrated and every active Multithread client has
        refreshed to a build that understands them.
        """

        _require_decision_rollout(rollout_fence)
        agent = canonical_agent(agent)
        if agent != "claude":
            raise ValidationError("decision request requires agent claude")
        decision_id = canonical_decision_id(decision_id)
        work_id = canonical_work_id(work_id)
        if isinstance(option_ids, (str, bytes)):
            raise ValidationError("decision option_ids must be an array")
        options = (
            [canonical_decision_option(item) for item in option_ids]
            if option_ids is not None
            else None
        )
        semantic_key = json.dumps(
            [work_id, decision_id],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(semantic_key.encode("utf-8")).hexdigest()
        event = normalize_event(
            {
                "v": 1,
                "id": f"decision-request:{digest}",
                "kind": "decision.requested",
                "agent": agent,
                "session": session,
                "work_id": work_id,
                "target": "codex",
                "scope": scope,
                "summary": summary,
                "artifact": artifact,
                "meta": {
                    "decision_id": decision_id,
                    "authority_hint": authority_hint,
                    **({"option_ids": options} if options is not None else {}),
                },
            },
            internal=True,
        )
        if event.scope != scope:
            raise ValidationError("decision scope must be exact canonical text")

        with self._transaction():
            intent = self._db.execute(
                """
                SELECT * FROM events
                WHERE kind = 'work.intent' AND work_id = ?
                ORDER BY seq ASC LIMIT 1
                """,
                (work_id,),
            ).fetchone()
            if intent is None:
                raise ConflictError(
                    f"decision request references unknown work_id {work_id}"
                )
            self._assert_canonical_event_row(intent)
            seq, duplicate = self._insert_session_neutral_event(
                event,
                conflict_message=(
                    f"decision {decision_id} already names a different question "
                    f"for work_id {work_id}"
                ),
            )
        return self._event_receipt(seq, duplicate)

    def decision_respond(
        self,
        request_seq: int,
        *,
        agent: str,
        session: str,
        judgment: str,
        resolution: str,
        authority_class: str,
        choice: str | None,
        rollout_fence: str,
    ) -> dict[str, Any]:
        """Atomically append one Codex judgment and ACK its exact request."""

        _require_decision_rollout(rollout_fence)
        if (
            not isinstance(request_seq, int)
            or isinstance(request_seq, bool)
            or request_seq < 1
        ):
            raise ValidationError("decision request sequence must be positive")
        agent = canonical_agent(agent)
        if agent != "codex":
            raise ValidationError("decision response requires agent codex")

        with self._transaction():
            request = self._db.execute(
                "SELECT * FROM events WHERE seq = ?", (request_seq,)
            ).fetchone()
            if request is None:
                raise ConflictError(
                    f"unknown decision request sequence: {request_seq}"
                )
            self._assert_canonical_event_row(request)
            if request["kind"] != "decision.requested":
                raise ConflictError(
                    f"event {request_seq} is not a decision request"
                )
            if request["agent"] != "claude" or request["target"] != "codex":
                raise StateError(
                    f"decision request {request_seq} has invalid routing"
                )
            if (
                request["work_id"] is None
                or request["scope"] is None
                or request["artifact"] is None
            ):
                raise StateError(
                    f"decision request {request_seq} has incomplete linkage"
                )

            request_meta = json.loads(request["meta_json"])
            decision_id = canonical_decision_id(
                request_meta.get("decision_id")
            )
            requested_options = request_meta.get("option_ids")
            if resolution == "choice":
                selected = canonical_decision_option(choice)
                if (
                    not isinstance(requested_options, list)
                    or selected not in requested_options
                ):
                    raise ValidationError(
                        "decision choice must name one requested option"
                    )

            response_digest = hashlib.sha256(
                str(request["event_id"]).encode("utf-8")
            ).hexdigest()
            response = normalize_event(
                {
                    "v": 1,
                    "id": f"decision-response:{response_digest}",
                    "kind": "decision.responded",
                    "agent": agent,
                    "session": session,
                    "work_id": request["work_id"],
                    "target": request["agent"],
                    "scope": request["scope"],
                    "summary": judgment,
                    "artifact": request["artifact"],
                    "meta": {
                        "decision_id": decision_id,
                        "request_seq": request_seq,
                        "request_event_id": request["event_id"],
                        "resolution": resolution,
                        "authority_class": authority_class,
                        **({"choice": choice} if choice is not None else {}),
                    },
                },
                internal=True,
            )
            response_seq, duplicate = self._insert_session_neutral_event(
                response,
                conflict_message=(
                    f"decision request {request_seq} already has a different "
                    "response"
                ),
            )
            acknowledgement = self._acknowledge_decision_request_in_transaction(
                request_seq,
                agent=agent,
                session=session,
                signal=request,
            )
            receipt = self._event_receipt(response_seq, duplicate)
            return {
                **receipt,
                "acknowledgement": acknowledgement,
            }

    def provider_review(self, report: Mapping[str, Any], *, agent: str, session: str) -> dict[str, Any]:
        """Append one passing review of an exact provider binary, attributed to its reviewer.

        The report is the review's own output; its canonical digest is the event's artifact. Only a pass whose
        control fired every canary and whose restricted call fired none can be recorded.
        """
        if report.get("schema") != 1 or report.get("verdict") != "pass":
            raise ValidationError("only a passing schema-1 provider review can be recorded")
        names = lambda key: report.get(key) if isinstance(report.get(key), list) else None
        listed = {key: names(key) for key in ("plugins", "agents", "control_fired", "restricted_fired")}
        if any(value is None or not all(isinstance(item, str) for item in value) for value in listed.values()):
            raise ValidationError("provider review lists must be arrays of names")
        # The report must agree with itself: no reasons, and its own runs show what its lists declare.
        control, restricted = report.get("control"), report.get("restricted")
        if (report.get("reasons") != [] or not isinstance(control, Mapping) or not isinstance(restricted, Mapping)
                or sorted(control.get("fired") or []) != sorted(listed["control_fired"])
                or sorted(restricted.get("fired") or []) != sorted(listed["restricted_fired"])
                or restricted.get("tools") != ["Read"] or restricted.get("mcp_servers") != []
                or restricted.get("attempted") is not True or restricted.get("leaked") is not False
                or restricted.get("version") != report.get("version") or restricted.get("exit") != 0
                or restricted.get("answered") is not True):
            raise ValidationError("a provider review report must agree with its own runs")
        if sorted(listed["control_fired"]) != PROVIDER_REVIEW_CANARIES.split(","):
            raise ValidationError("a provider review is recorded only when its control fired every canary")
        joined = {key: ",".join(sorted(value)) or "none" for key, value in listed.items()}
        meta = {"provider": report.get("provider"), "binary_sha256": report.get("binary_sha256"),
                "version": report.get("version"), "surface_sha256": report.get("surface_sha256"), **joined}
        digest = hashlib.sha256(canonical_json(report).encode("utf-8")).hexdigest()
        event = normalize_event({"kind": "provider.reviewed", "agent": agent, "session": session,
                                 "summary": f"Claude Code {meta['version']} reviewed for restricted peer calls: pass",
                                 "artifact": f"sha256:{digest}", "meta": meta}, internal=True)
        with self._transaction():
            seq, duplicate = self._insert_event(event)
        return self._event_receipt(seq, duplicate)

    def provider_reviews(self, binary_sha256: str) -> list[dict[str, Any]]:
        """Every recorded passing review of one exact binary, oldest first."""
        rows = self._execute(
            "SELECT * FROM events WHERE kind = 'provider.reviewed' "
            "AND json_extract(meta_json, '$.binary_sha256') = ? ORDER BY seq LIMIT 50",
            (binary_sha256,),
        ).fetchall()
        return [self._event_row(row) for row in rows]

    def acknowledge(
        self,
        signal_seq: int,
        *,
        agent: str,
        session: str,
    ) -> dict[str, Any]:
        """Append acknowledgement of one signal delivered to ``agent``.

        Acknowledgement is keyed by signal and recipient. Exact-session targets
        require that session; generic/broadcast ACKs remain agent scoped.
        """

        with self._transaction():
            return self._acknowledge_in_transaction(
                signal_seq,
                agent=agent,
                session=session,
            )

    def _acknowledge_in_transaction(
        self,
        signal_seq: int,
        *,
        agent: str,
        session: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(signal_seq, int)
            or isinstance(signal_seq, bool)
            or signal_seq < 1
        ):
            raise ValidationError("signal sequence must be a positive integer")
        signal = self._db.execute(
            "SELECT * FROM events WHERE seq = ?", (signal_seq,)
        ).fetchone()
        if signal is None:
            raise ConflictError(f"unknown Multithread signal sequence: {signal_seq}")
        if signal["kind"] == "decision.requested":
            raise ConflictError(
                "decision requests may be acknowledged only by decision respond"
            )
        if signal["kind"] not in DELIVERY_SIGNAL_KINDS:
            raise ConflictError(
                f"event {signal_seq} is not an acknowledgeable delivery signal"
            )
        return self._append_delivery_acknowledgement(
            signal_seq,
            agent=agent,
            session=session,
            signal=signal,
        )

    def _acknowledge_decision_request_in_transaction(
        self,
        request_seq: int,
        *,
        agent: str,
        session: str,
        signal: sqlite3.Row,
    ) -> dict[str, Any]:
        if (
            int(signal["seq"]) != request_seq
            or signal["kind"] != "decision.requested"
        ):
            raise StateError("decision response has an invalid request row")
        return self._append_delivery_acknowledgement(
            request_seq,
            agent=agent,
            session=session,
            signal=signal,
        )

    def _append_delivery_acknowledgement(
        self,
        signal_seq: int,
        *,
        agent: str,
        session: str,
        signal: sqlite3.Row,
    ) -> dict[str, Any]:
        agent = canonical_agent(agent)
        acknowledgement_scope = f"signal:{signal_seq}"

        exact = session_target(agent, session)
        if signal["target"] not in {None, agent, exact}:
            raise ConflictError(
                f"signal {signal_seq} is targeted to {signal['target']}, not {exact}. "
                "Signal targets are agent identities; wake role bindings do not redirect ACKs."
            )
        recipient = exact if signal["target"] == exact else agent
        digest = hashlib.sha256(recipient.encode("utf-8")).hexdigest()[:24]
        event = normalize_event(
            {
                "v": 1,
                "id": f"ack:{signal_seq}:{digest}",
                "kind": "delivery.acknowledged",
                "agent": agent,
                "session": session,
                "work_id": signal["work_id"],
                "target": recipient,
                "scope": acknowledgement_scope,
                "summary": f"Acknowledged delivery of Relay signal {signal_seq}",
                "meta": {
                    "signal_seq": signal_seq,
                    "signal_event_id": signal["event_id"],
                    "target_agent": agent,
                },
            },
            internal=True,
        )
        existing = self._db.execute(
            """
            SELECT * FROM events
            WHERE kind = 'delivery.acknowledged'
              AND target = ? AND scope = ?
            ORDER BY seq LIMIT 1
            """,
            (recipient, acknowledgement_scope),
        ).fetchone()
        if existing is not None:
            return self._event_receipt(int(existing["seq"]), duplicate=True)

        seq, duplicate = self._insert_event(event)
        return self._event_receipt(seq, duplicate)

    # --- Wake: role bindings and delivery attempts ---------------------------
    #
    # A binding names where a wake goes; it is not a queue. Its generation is
    # the sequence of its wake.bound event. An attempt is recorded before the
    # transport runs and concluded once afterwards, so a send whose outcome was
    # never recorded stays visible and is never repeated under the same id.

    @property
    def ledger_home(self) -> Path:
        """The checkout that names this ledger: the primary checkout its linked worktrees share, which outlives
        any one of them, or the worktree itself when no .git directory owns the ledger."""
        common = self.paths.git_common_dir
        return common.parent if common.name == ".git" else self.paths.repo_root

    def wake_bindings(self, role: str | None = None) -> dict[str, Any]:
        """The current binding of one role, or of every role bound here."""

        if role is not None:
            roles = [canonical_wake_role(role)]
        else:
            roles = [
                str(row[0])
                for row in self._execute(
                    "SELECT DISTINCT target FROM events "
                    "WHERE kind = 'wake.bound' ORDER BY target"
                ).fetchall()
            ]
        try:
            project = canonical_wake_project(self.ledger_home.name)
        except ValidationError:
            project = None
        return {
            "ledger": str(self.ledger_home),
            "project": project,
            "bindings": [self._wake_state(item) for item in roles],
        }

    def wake_bind(
        self,
        role: str,
        *,
        endpoint: str,
        replace: bool,
        agent: str,
        session: str,
        provider: str = "codex",
        thread: str | None = None,
        cwd: str | None = None,
        charter: str | None = None,
        role_scope: str | None = None,
        expected_generation: int | None = None,
        reason: str | None = None,
        approval_ref: str | None = None,
    ) -> dict[str, Any]:
        """Bind a role to a Codex conversation, or to a Claude Code session's inbox."""

        role = canonical_wake_role(role)
        if provider == "codex":
            thread = canonical_wake_thread(thread)
        with self._transaction():
            current = self._wake_binding(role)
            if current is None and expected_generation is not None:
                raise ConflictError("role binding changed; expected generation is no longer bound")
            if current is not None:
                if expected_generation is not None and expected_generation != current["generation"]:
                    raise ConflictError("role binding changed; inspect the current generation before handover")
                if current["thread"] == thread and current["endpoint"] == endpoint:
                    if charter is None and role_scope is None:
                        return {"duplicate": True, "binding": self._wake_state(role)}
                if not replace:
                    where = (
                        f"through {current['endpoint']}" if current["thread"] == thread
                        else f"to conversation {current['thread']}"
                    )
                    raise ConflictError(
                        f"{role} is already bound {where} (binding "
                        f"{current['generation']}); pass --replace to move it"
                    )
                same_owner = (canonical_agent(agent), canonical_agent(session)) == (current["bound_agent"], current["bound_session"]) or (
                    current["provider"] == "codex" and agent == "codex" and session == current["thread"]
                )
                same_recipient = provider == current["provider"] and (
                    provider == "claude" or thread == current["thread"]
                )
                if not (same_owner and same_recipient):
                    if expected_generation is None or not reason or not approval_ref:
                        raise ConflictError(
                            "moving another holder's role requires its release, or explicit user authorization "
                            "with --expected-generation, --reason and --approval-ref; --replace alone is insufficient"
                        )
            summary = (
                f"Bound {role} to Codex conversation {thread}" if provider == "codex"
                else f"Bound {role} to the Claude Code inbox {endpoint.removeprefix('unix://')}"
            )
            meta: dict[str, Any] = {
                "role": role,
                "provider": provider,
                "endpoint": endpoint,
                **({"thread": thread} if thread is not None else {}),
                **({"cwd": cwd} if cwd is not None else {}),
            }
            if current is not None:
                meta["replaces"] = current["generation"]
                meta["previous_holder"] = (
                    f"codex:{current['thread']}" if current["provider"] == "codex" else current["bound_by"]
                )
                summary += f", replacing binding {current['generation']}"
            for key, value in (("charter", charter), ("role_scope", role_scope),
                               ("reason", reason), ("approval_ref", approval_ref)):
                if value is not None:
                    meta[key] = value
            if current is not None:
                for key in ("charter", "role_scope"):
                    if key not in meta and current.get(key) is not None:
                        meta[key] = current[key]
            event = normalize_event(
                {
                    "v": 1,
                    "kind": "wake.bound",
                    "agent": agent,
                    "session": session,
                    "target": role,
                    "summary": summary,
                    "meta": meta,
                },
                internal=True,
            )
            seq, _ = self._insert_event(event)
            if current is not None and current["paused_seq"] is not None:
                self._insert_event(normalize_event({
                    "kind": "wake.paused", "agent": agent, "session": session,
                    "target": role, "summary": f"Preserved paused wakes to {role} (binding {seq})",
                    "meta": {"role": role, "generation": seq},
                }, internal=True))
            if current is not None and not (same_owner and same_recipient):
                for recipient, detail in (
                    (session_target("codex", current["thread"]) if current["provider"] == "codex"
                     else session_target(current["bound_agent"], current["bound_session"]),
                     "Your role binding was explicitly handed over"),
                    (session_target("codex", thread) if provider == "codex" else session_target(agent, session),
                     "Review outstanding work from the previous binding before accepting a handover"),
                ):
                    notice = normalize_event({
                        "kind": "work.handoff", "agent": agent, "session": session,
                        "target": recipient, "artifact": f"receipt:binding:{seq}",
                        "summary": f"{detail}: {role}; inspect roles {role} --history. "
                                   "Original messages and claim ownership are unchanged.",
                    })
                    self._insert_event(notice)
            return {
                "duplicate": False,
                "binding": self._wake_state(role),
                "replaced": current,
            }

    def wake_control(
        self, action: str, role: str, *, agent: str, session: str,
        expected_generation: int | None = None, reason: str | None = None,
        approval_ref: str | None = None,
    ) -> dict[str, Any]:
        """Unbind, pause or resume the current binding; a no-op is a duplicate."""

        kinds = {"unbind": "wake.unbound", "pause": "wake.paused", "resume": "wake.resumed"}
        if action not in kinds:
            raise ValidationError("wake control is unbind, pause or resume")
        role = canonical_wake_role(role)
        with self._transaction():
            current = self._wake_binding(role)
            if current is None:
                if action == "unbind":
                    return {"duplicate": True, "binding": self._wake_state(role)}
                raise ConflictError(
                    f"{role} isn't bound in this ledger, so there is nothing to "
                    f"{action}; bind it first: multithread bind {role} "
                    "--thread <codex conversation id>, or --claude-socket "
                    "\"$CLAUDE_CODE_MESSAGING_SOCKET\" from a Claude Code session"
                )
            if expected_generation is not None and expected_generation != current["generation"]:
                raise ConflictError("role binding changed; inspect the current generation")
            owner = (canonical_agent(agent), canonical_agent(session)) == (current["bound_agent"], current["bound_session"]) or (
                current["provider"] == "codex" and agent == "codex" and session == current["thread"]
            )
            if not owner and (expected_generation is None or not reason or not approval_ref):
                raise ConflictError("changing another holder's role requires explicit user authorization "
                                    "with --expected-generation, --reason and --approval-ref")
            paused = current["paused_seq"] is not None
            if (action == "pause" and paused) or (action == "resume" and not paused):
                return {"duplicate": True, "binding": self._wake_state(role)}
            generation = current["generation"]
            summary = {
                "unbind": f"Unbound {role} (binding {generation})",
                "pause": f"Paused wakes to {role} (binding {generation})",
                "resume": f"Resumed wakes to {role} (binding {generation})",
            }[action]
            event = normalize_event(
                {
                    "v": 1,
                    "kind": kinds[action],
                    "agent": agent,
                    "session": session,
                    "target": role,
                    "summary": summary,
                    "meta": {"role": role, "generation": generation,
                             **({"reason": reason} if reason is not None else {}),
                             **({"approval_ref": approval_ref} if approval_ref is not None else {})},
                },
                internal=True,
            )
            self._insert_event(event)
            return {
                "duplicate": False,
                "binding": self._wake_state(role),
                "ended": current if action == "unbind" else None,
            }

    def wake_plan(
        self,
        role: str,
        *,
        ref: str,
        requested: str,
        message_id: str | None = None,
        ref_sha256: str | None = None,
        ref_size: int | None = None,
        expected_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """What wake_begin would decide, without recording anything."""

        return self._wake_decide(
            role, ref=ref, requested=requested, message_id=message_id,
            ref_sha256=ref_sha256, ref_size=ref_size,
            expected_binding=expected_binding,
        )

    def wake_begin(
        self,
        role: str,
        *,
        ref: str,
        requested: str,
        agent: str,
        session: str,
        message_id: str | None = None,
        ref_sha256: str | None = None,
        ref_size: int | None = None,
        expected_binding: Mapping[str, Any] | None = None,
        sender: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically check the binding and the id, then record the attempt."""

        # Reject malformed assertions before acquiring a write transaction.
        # Recipient comparison itself remains inside the atomic decision below.
        expected_binding = canonical_wake_expectation(expected_binding)
        sender = canonical_wake_sender(sender)
        with self._transaction():
            decision = self._wake_decide(
                role, ref=ref, requested=requested, message_id=message_id,
                ref_sha256=ref_sha256, ref_size=ref_size,
                expected_binding=expected_binding,
            )
            if decision["status"] != "ready":
                return decision
            binding = decision["binding"]
            event = normalize_event(
                {
                    "v": 1,
                    "kind": "wake.attempted",
                    "agent": agent,
                    "session": session,
                    "target": decision["role"],
                    "summary": (
                        f"Wake {decision['role']} ({requested} requested): "
                        f"{decision['ref']}"
                    ),
                    "meta": {
                        "role": decision["role"],
                        "generation": binding["generation"],
                        "provider": binding["provider"],
                        **({"thread": binding["thread"]} if binding["thread"] else {}),
                        "message_id": decision["message_id"],
                        "ref": decision["ref"],
                        **(
                            {"ref_sha256": ref_sha256, "ref_size": ref_size}
                            if ref_sha256 is not None else {}
                        ),
                        "requested": requested,
                        **({"sender": sender} if sender is not None else {}),
                    },
                },
                internal=True,
            )
            seq, _ = self._insert_event(event)
            return {**decision, "status": "begun", "attempt_seq": seq,
                    **({"sender": sender} if sender is not None else {})}

    def wake_conclude(
        self,
        attempt_seq: int,
        *,
        outcome: str,
        reason: str,
        transport: str | None,
        native_id: str | None,
        agent: str,
        session: str,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Record the one outcome of an attempt; an identical retry is a duplicate."""

        if (
            not isinstance(attempt_seq, int)
            or isinstance(attempt_seq, bool)
            or attempt_seq < 1
        ):
            raise ValidationError("attempt sequence must be a positive integer")
        with self._transaction():
            attempt = self._execute(
                "SELECT * FROM events WHERE seq = ?", (attempt_seq,)
            ).fetchone()
            if attempt is None or attempt["kind"] != "wake.attempted":
                raise ConflictError(f"event {attempt_seq} is not a wake attempt")
            self._assert_canonical_event_row(attempt)
            meta = json.loads(attempt["meta_json"])
            requested = meta["requested"]
            if (outcome, reason, transport) not in WAKE_CONCLUSIONS[meta["provider"]]:
                raise ValidationError(
                    f"attempt {attempt_seq} went to {meta['provider']}, which cannot "
                    f"conclude {outcome} ({reason})"
                )
            if (
                transport == "steer"
                or reason in {"no_live_turn", "turn_ended", "turn_changed", "turn_not_steerable"}
            ) and requested != "steer":
                raise ValidationError(
                    f"attempt {attempt_seq} asked to queue, so it cannot conclude "
                    f"{outcome} ({reason})"
                )
            if reason == "requested" and requested != "queue":
                raise ValidationError(
                    f"attempt {attempt_seq} asked to steer; a queued fallback "
                    "names why it queued"
                )
            conclusion: dict[str, Any] = {
                "role": meta["role"],
                "generation": meta["generation"],
                "attempt_seq": attempt_seq,
                "message_id": meta["message_id"],
                "outcome": outcome,
                "reason": reason,
            }
            if transport is not None:
                conclusion["transport"] = transport
            if native_id is not None:
                conclusion["native_id"] = native_id
            if detail is not None:
                conclusion["detail"] = detail
            event = normalize_event(
                {
                    "v": 1,
                    "id": f"wake-concluded:{attempt_seq}",
                    "kind": "wake.concluded",
                    "agent": agent,
                    "session": session,
                    "target": meta["role"],
                    "summary": (
                        f"Wake {meta['message_id']} to {meta['role']}: "
                        f"{outcome} ({reason.replace('_', ' ')})"
                    ),
                    "meta": conclusion,
                },
                internal=True,
            )
            seq, duplicate = self._insert_session_neutral_event(
                event,
                conflict_message=(
                    f"wake attempt {attempt_seq} already has a different outcome"
                ),
            )
            return self._event_receipt(seq, duplicate)

    def _wake_decide(
        self,
        role: str,
        *,
        ref: str,
        requested: str,
        message_id: str | None,
        ref_sha256: str | None,
        ref_size: int | None,
        expected_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        expected_binding = canonical_wake_expectation(expected_binding)
        role = canonical_wake_role(role)
        ref = canonical_wake_ref(ref)
        canonical_wake_content(ref, ref_sha256, ref_size)
        if requested not in WAKE_REQUESTS:
            raise ValidationError("requested transport must be queue or steer")
        if message_id is not None:
            message_id = canonical_wake_message_id(message_id)
        if ref.isdigit() and self._execute(
            "SELECT 1 FROM events WHERE seq = ?", (int(ref),)
        ).fetchone() is None:
            latest = self._execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
            raise ValidationError(
                f"ledger sequence {ref} doesn't exist here (the latest is {latest})"
            )
        binding = self._wake_binding(role)
        if expected_binding is not None and (
            binding is None
            or any(binding.get(field) != expected for field, expected in expected_binding.items())
        ):
            raise ConflictError(
                "wake recipient binding changed; inspect the current role binding before sending"
            )
        decision: dict[str, Any] = {
            "ledger": str(self.ledger_home),
            "role": role,
            "ref": ref,
            "requested": requested,
            "binding": binding,
        }
        if binding is None:
            return {**decision, "status": "unbound", "message_id": message_id}
        if message_id is None:
            # A file is named by its bytes too: the same path with new content
            # is a new message; a sequence is already immutable. The binding
            # generation is part of the name, so after a rebind the same file
            # is a new message that a person may send deliberately. Automatic
            # delivery (a later phase) must not rely on this: it must never
            # replay a signal already accepted under an old binding merely
            # because the binding changed.
            content = [ref_sha256] if ref_sha256 is not None else []
            identity = json.dumps(
                [str(self.paths.git_common_dir), role, binding["generation"], ref, *content],
                ensure_ascii=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
            message_id = f"wake-{role}-{binding['generation']}-{digest}"
        decision["message_id"] = message_id
        if binding["paused_seq"] is not None:
            return {**decision, "status": "paused"}
        rows = self._execute(
            "SELECT * FROM events WHERE kind = 'wake.attempted' "
            "AND json_extract(meta_json, '$.message_id') = ? ORDER BY seq",
            (message_id,),
        ).fetchall()
        for row in rows:
            prior = self._wake_attempt(row)
            if prior["outcome"] != "not_sent":
                return {**decision, "status": "already_sent", "prior": prior}
        return {**decision, "status": "ready"}

    def _wake_binding(self, role: str) -> dict[str, Any] | None:
        rows = self._execute(
            "SELECT * FROM events WHERE target = ? AND kind IN (?, ?, ?, ?) ORDER BY seq",
            (role, *WAKE_BINDING_KINDS),
        ).fetchall()
        binding: dict[str, Any] | None = None
        for row in rows:
            self._assert_canonical_event_row(row)
            meta = json.loads(row["meta_json"])
            if row["kind"] == "wake.bound":
                binding = {
                    "generation": int(row["seq"]),
                    "provider": meta["provider"],
                    "thread": meta.get("thread"),
                    "endpoint": meta["endpoint"],
                    "cwd": meta.get("cwd"),
                    "bound_at": row["recorded_at"],
                    "bound_by": f"{row['agent']}:{row['session']}",
                    "bound_agent": row["agent"],
                    "bound_session": row["session"],
                    "charter": meta.get("charter"),
                    "role_scope": meta.get("role_scope"),
                    "paused_seq": None,
                }
                continue
            if binding is None or meta["generation"] != binding["generation"]:
                raise StateError(
                    f"Multithread event {int(row['seq'])} changes a binding "
                    "that is not current"
                )
            if row["kind"] == "wake.unbound":
                binding = None
            else:
                binding["paused_seq"] = (
                    int(row["seq"]) if row["kind"] == "wake.paused" else None
                )
        return binding

    def _wake_state(self, role: str) -> dict[str, Any]:
        binding = self._wake_binding(role)
        last = self._execute(
            "SELECT * FROM events WHERE target = ? AND kind = 'wake.attempted' "
            "ORDER BY seq DESC LIMIT 1",
            (role,),
        ).fetchone()
        state = (
            "unbound" if binding is None
            else "paused" if binding["paused_seq"] is not None
            else "active"
        )
        return {
            "role": role,
            "state": state,
            **(binding or {}),
            "last_attempt": self._wake_attempt(last) if last is not None else None,
        }

    def wake_history(self, role: str, *, limit: int = 30, before: int | None = None,
                     ref: str | None = None) -> dict[str, Any]:
        """Read outstanding original-generation attempts; never redirect or resend."""
        role = canonical_wake_role(role)
        limit = _inbox_limit(limit)
        if before is not None and (not isinstance(before, int) or isinstance(before, bool) or before < 1):
            raise ValidationError("history --before must be a positive sequence")
        where = "target = ? AND kind = 'wake.attempted'"
        params: list[Any] = [role]
        if before is not None:
            where += " AND seq < ?"
            params.append(before)
        if ref is not None:
            where += " AND json_extract(meta_json, '$.ref') = ?"
            params.append(canonical_wake_ref(ref))
        rows = self._execute(f"SELECT * FROM events WHERE {where} ORDER BY seq DESC LIMIT ?",
                             (*params, limit + 1)).fetchall()
        attempts = [self._wake_attempt(row) for row in rows[:limit]]
        return {"ledger": str(self.ledger_home), "binding": self._wake_state(role),
                "attempts": attempts, "has_more": len(rows) > limit,
                "next_before": attempts[-1]["seq"] if attempts else None}

    def _wake_attempt(self, row: sqlite3.Row) -> dict[str, Any]:
        self._assert_canonical_event_row(row)
        meta = json.loads(row["meta_json"])
        attempt: dict[str, Any] = {
            "seq": int(row["seq"]),
            "at": row["recorded_at"],
            "by": f"{row['agent']}:{row['session']}",
            "generation": meta["generation"],
            "provider": meta["provider"],
            "thread": meta.get("thread"),
            "message_id": meta["message_id"],
            "ref": meta["ref"],
            "ref_sha256": meta.get("ref_sha256"),
            "ref_size": meta.get("ref_size"),
            "requested": meta["requested"],
            "outcome": "open",
        }
        if "sender" in meta:
            attempt["sender"] = canonical_wake_sender(meta["sender"])
        bound = self._execute("SELECT * FROM events WHERE seq = ?", (meta["generation"],)).fetchone()
        if bound is None or bound["kind"] != "wake.bound":
            raise StateError("wake attempt has no original binding")
        self._assert_canonical_event_row(bound)
        holder = json.loads(bound["meta_json"])
        recipient_agent, recipient_session = ("codex", holder["thread"]) if holder["provider"] == "codex" else (bound["agent"], bound["session"])
        recipient = f"{recipient_agent}:{recipient_session}"
        attempt.update(recipient=recipient, consumption_state="unknown")
        # A sequence in another checkout's ledger is acknowledged there; its consumption is not observed here.
        signal = self._execute("SELECT kind FROM events WHERE seq = ?", (int(meta["ref"]),)).fetchone() if meta["ref"].isdigit() else None
        if signal is not None and signal["kind"] in DELIVERY_SIGNAL_KINDS:
            acknowledgement = self._execute(
                "SELECT * FROM events WHERE kind = 'delivery.acknowledged' AND scope = ? "
                "ORDER BY CASE WHEN agent = ? AND session = ? THEN 0 ELSE 1 END, seq LIMIT 1",
                (f"signal:{meta['ref']}", recipient_agent, recipient_session)
            ).fetchone()
            attempt["consumption_state"] = "not_acknowledged"
            if acknowledgement is not None:
                self._assert_canonical_event_row(acknowledgement)
                consumed_by = f"{acknowledgement['agent']}:{acknowledgement['session']}"
                attempt.update(consumption_state="acknowledged" if (acknowledgement['agent'], acknowledgement['session']) == (recipient_agent, recipient_session) else "acknowledged_elsewhere",
                               acknowledgement_seq=int(acknowledgement["seq"]), consumed_by=consumed_by)
        conclusion = self._execute(
            "SELECT * FROM events WHERE event_id = ?",
            (f"wake-concluded:{int(row['seq'])}",),
        ).fetchone()
        if conclusion is not None:
            self._assert_canonical_event_row(conclusion)
            result = json.loads(conclusion["meta_json"])
            if (
                conclusion["kind"] != "wake.concluded"
                or result["attempt_seq"] != attempt["seq"]
                or result["message_id"] != attempt["message_id"]
            ):
                raise StateError(
                    f"Multithread event {int(conclusion['seq'])} does not "
                    f"conclude wake attempt {attempt['seq']}"
                )
            attempt.update(
                outcome=result["outcome"],
                reason=result["reason"],
                transport=result.get("transport"),
                native_id=result.get("native_id"),
                detail=result.get("detail"),
                concluded_seq=int(conclusion["seq"]),
                concluded_at=conclusion["recorded_at"],
            )
        return attempt

    def ratchet_decide(
        self,
        fingerprint: str,
        *,
        mode: str,
        home: str,
        agent: str,
        session: str,
        summary: str,
        work_id: str | None = None,
        verify_when: str | None = None,
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "fingerprint": fingerprint,
            "mode": mode,
            "home": home,
        }
        if verify_when is not None:
            meta["verify_when"] = verify_when
        event = normalize_event(
            {
                "v": 1,
                "kind": "ratchet.decided",
                "agent": agent,
                "session": session,
                "work_id": work_id,
                "target": fingerprint,
                "summary": summary,
                "meta": meta,
            },
            internal=mode == "drop",
        )
        with self._transaction():
            if not self._fingerprint_exists(fingerprint, "friction.observed"):
                raise ConflictError(
                    f"cannot decide {fingerprint}: no friction observation exists"
                )
            blocking_drop = self._latest_drop_without_recurrence(fingerprint)
            if blocking_drop is not None:
                raise ConflictError(
                    f"cannot decide {fingerprint}: terminal drop {blocking_drop} "
                    "requires a newer friction observation"
                )
            seq, duplicate = self._insert_event(event)
            if event.meta["mode"] == "drop":
                terminal_receipt = normalize_event(
                    {
                        "v": event.v,
                        "id": f"drop-close:{seq}",
                        "kind": "ratchet.verified",
                        "agent": event.agent,
                        "session": event.session,
                        "work_id": event.work_id,
                        "target": event.target,
                        "scope": event.scope,
                        "summary": (
                            "Compatibility closure for terminal ratchet drop "
                            f"{seq}"
                        ),
                        "meta": {
                            "fingerprint": event.meta["fingerprint"],
                            "outcome": "dropped",
                            "decision_seq": seq,
                            "drop_compatibility": True,
                        },
                    },
                    internal=True,
                )
                self._insert_event(terminal_receipt)
        return self._event_receipt(seq, duplicate)

    def ratchet_verify(
        self,
        fingerprint: str,
        *,
        outcome: str,
        agent: str,
        session: str,
        summary: str,
        before_cost_seconds: int | None = None,
        after_cost_seconds: int | None = None,
        evidence: str | None = None,
        work_id: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction():
            decision_seq = self._latest_fingerprint_seq(fingerprint, "ratchet.decided")
            if decision_seq is None:
                raise ConflictError(
                    f"cannot verify {fingerprint}: no ratchet decision exists"
                )
            decision = self._db.execute(
                "SELECT meta_json FROM events WHERE seq = ?", (decision_seq,)
            ).fetchone()
            assert decision is not None
            decision_mode = json.loads(decision["meta_json"]).get("mode")
            if decision_mode == "drop":
                raise ConflictError(
                    f"cannot verify {fingerprint}: drop decision {decision_seq} is "
                    "terminal and cannot be verified; record a new subtract/promote "
                    "decision if the friction recurs"
                )
            if self._decision_has_verification(fingerprint, decision_seq):
                raise ConflictError(
                    f"cannot verify {fingerprint}: decision {decision_seq} was already "
                    "verified; record a new decision before verifying again"
                )
            meta: dict[str, Any] = {
                "fingerprint": fingerprint,
                "outcome": outcome,
                "decision_seq": decision_seq,
            }
            if before_cost_seconds is not None:
                meta["before_cost_seconds"] = before_cost_seconds
                meta["after_cost_seconds"] = after_cost_seconds
            if evidence is not None:
                meta["evidence"] = evidence
            event = normalize_event(
                {
                    "v": 1,
                    "kind": "ratchet.verified",
                    "agent": agent,
                    "session": session,
                    "work_id": work_id,
                    "target": fingerprint,
                    "summary": summary,
                    "meta": meta,
                }
            )
            seq, duplicate = self._insert_event(event)
        return self._event_receipt(seq, duplicate)

    def ratchet_review(self) -> list[dict[str, Any]]:
        rows = self._execute(
            """
            SELECT * FROM events
            WHERE kind IN ('friction.observed', 'ratchet.decided', 'ratchet.verified')
            ORDER BY seq
            """
        ).fetchall()
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            event = self._event_row(row)
            fingerprint = event["meta"].get("fingerprint")
            if not isinstance(fingerprint, str):
                continue
            group = groups.setdefault(
                fingerprint,
                {
                    "fingerprint": fingerprint,
                    "observations": [],
                    "decisions": [],
                    "verifications": [],
                    "terminal_receipts": [],
                },
            )
            if event["kind"] == "friction.observed":
                group["observations"].append(event)
            elif event["kind"] == "ratchet.decided":
                group["decisions"].append(event)
            elif event["meta"].get("drop_compatibility") is True:
                group["terminal_receipts"].append(event)
            else:
                group["verifications"].append(event)

        out: list[dict[str, Any]] = []
        for fingerprint, group in groups.items():
            observations = group["observations"]
            decisions = group["decisions"]
            verifications = group["verifications"]
            terminal_receipts = group["terminal_receipts"]
            if not observations:
                continue
            latest_decision = decisions[-1] if decisions else None
            eligible_verifications = [
                item
                for item in verifications
                if latest_decision is not None
                and item["meta"].get("decision_seq") == latest_decision["seq"]
            ]
            latest_verification = eligible_verifications[-1] if eligible_verifications else None
            dropped = (
                latest_decision is not None
                and latest_decision["meta"].get("mode") == "drop"
            )
            eligible_terminal_receipts = [
                item
                for item in terminal_receipts
                if dropped
                and item["meta"].get("decision_seq") == latest_decision["seq"]
            ]
            terminal_receipt = (
                eligible_terminal_receipts[-1]
                if eligible_terminal_receipts
                else None
            )
            recurrence_anchor = (
                terminal_receipt or latest_decision
                if dropped
                else latest_verification
            )
            recurrence = (
                recurrence_anchor is not None
                and any(
                    item["seq"] > recurrence_anchor["seq"] for item in observations
                )
            )
            if recurrence:
                state = "recurred"
            elif dropped:
                state = "dropped"
            elif latest_verification is not None:
                state = latest_verification["meta"]["outcome"]
            elif latest_decision is not None:
                state = "decided"
            else:
                state = "observing"
            cost = sum(
                int(item["meta"].get("cost_seconds", 0)) for item in observations
            )
            recurrence_count = (
                sum(item["seq"] > recurrence_anchor["seq"] for item in observations)
                if recurrence_anchor is not None
                else 0
            )
            out.append(
                {
                    "fingerprint": fingerprint,
                    "state": state,
                    "count": len(observations),
                    "sessions": len({item["session"] for item in observations}),
                    "total_cost_seconds": cost,
                    "post_verification_recurrence": recurrence_count,
                    "first_seq": observations[0]["seq"],
                    "last_seq": max(
                        item["seq"]
                        for item in (
                            observations
                            + decisions
                            + verifications
                            + terminal_receipts
                        )
                    ),
                    "latest_summary": observations[-1]["summary"],
                    "decision": latest_decision,
                    "verification": latest_verification,
                    "terminal_receipt": terminal_receipt,
                }
            )
        out.sort(key=lambda item: (-item["count"], -item["total_cost_seconds"], item["fingerprint"]))
        return out

    @staticmethod
    def _brief_claim(claim: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: claim[key]
            for key in (
                "claim_id",
                "resource",
                "holder_agent",
                "holder_session",
                "purpose",
                "acquired_event_seq",
            )
        }

    @staticmethod
    def _brief_event(event: Mapping[str, Any]) -> dict[str, Any]:
        out = {
            key: event[key]
            for key in (
                "seq",
                "kind",
                "agent",
                "session",
                "work_id",
                "target",
                "scope",
                "summary",
                "artifact",
            )
            if event.get(key) is not None
        }
        allowed_meta = (
            "resource",
            "reason",
            "commit_oid",
            "commit_subject",
            "relay_trailers_sha256",
            "evidence_sha256",
            "decision_id",
            "authority_hint",
            "option_ids",
            "request_seq",
            "request_event_id",
            "resolution",
            "authority_class",
            "choice",
        )
        meta = {
            key: event["meta"][key]
            for key in allowed_meta
            if key in event.get("meta", {})
        }
        if meta:
            out["meta"] = meta
        return out

    @staticmethod
    def _compact_ratchet_item(item: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            key: item[key]
            for key in (
                "fingerprint",
                "state",
                "count",
                "sessions",
                "total_cost_seconds",
                "post_verification_recurrence",
                "first_seq",
                "last_seq",
                "latest_summary",
            )
        }
        decision = item.get("decision")
        if isinstance(decision, Mapping):
            out["decision"] = {
                "seq": decision["seq"],
                "mode": decision["meta"]["mode"],
                "home": decision["meta"]["home"],
            }
            if "verify_when" in decision["meta"]:
                out["decision"]["verify_when"] = decision["meta"]["verify_when"]
        verification = item.get("verification")
        if isinstance(verification, Mapping):
            out["verification"] = {
                "seq": verification["seq"],
                "outcome": verification["meta"]["outcome"],
            }
        terminal_receipt = item.get("terminal_receipt")
        if isinstance(terminal_receipt, Mapping):
            out["terminal_receipt"] = {"seq": terminal_receipt["seq"]}
        return out

    def integrity_check(self) -> str:
        row = self._execute("PRAGMA integrity_check").fetchone()
        return str(row[0])

    def diagnostics(self) -> dict[str, Any]:
        return {
            "integrity": self.integrity_check(),
            "journal_mode": str(self._execute("PRAGMA journal_mode").fetchone()[0]),
            "synchronous": int(self._execute("PRAGMA synchronous").fetchone()[0]),
            "foreign_keys": int(self._execute("PRAGMA foreign_keys").fetchone()[0]),
            "schema_version": int(self._execute("PRAGMA user_version").fetchone()[0]),
        }

    def _initialize_schema(self) -> None:
        with self._transaction():
            # A second cold-start process may have initialized the schema while
            # this connection waited for BEGIN IMMEDIATE. Re-read under lock.
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise StateError(
                    f"Multithread database schema {version} is newer than this client "
                    f"({SCHEMA_VERSION})"
                )
            if version == 0:
                for statement in _split_sql_script(_SCHEMA):
                    self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                return
            if version == 1:
                self._migrate_v1_to_v2()
                self._db.execute("PRAGMA user_version = 2")
                version = 2
            if version == 2:
                # The ratchet hardening is additive: no table or column shape
                # changes, and the terminal receipt intentionally remains
                # legible to an already-open v2 reducer. Keep user_version at
                # 2 so old hooks can coexist while these persistent guards are
                # installed idempotently.
                self._harden_v2_ratchet_state()
            if version != SCHEMA_VERSION:
                raise StateError(f"unsupported Multithread database schema {version}")

    def _migrate_v1_to_v2(self) -> None:
        # v1 accepted both spellings as distinct raw resources. Never let a
        # v2 client silently pick a winner if a legitimate old ledger already
        # has both active: retain the untouched v1 state and require an audited
        # release/break before retrying the migration.
        active = self._db.execute(
            """
            SELECT claim_id, resource
            FROM claims
            WHERE released_event_seq IS NULL
              AND resource IN ('integrate:main', 'integration:main')
            ORDER BY claim_id
            """
        ).fetchall()
        if len(active) > 1:
            claim_ids = ", ".join(str(row["claim_id"]) for row in active)
            raise StateError(
                "Multithread schema v2 cannot canonicalize main integration claims "
                f"while both legacy and canonical authorities are active: {claim_ids}"
            )

        # Claim history is a mutable projection with an append-only release
        # transition. Temporarily remove that guard inside this one IMMEDIATE
        # transaction, canonicalize every historical row, then restore the
        # exact guard. Immutable v1 acquisition events retain their original
        # spelling as historical evidence.
        self._db.execute("DROP TRIGGER claims_only_release_once")
        self._db.execute(
            "UPDATE claims SET resource = 'integration:main' "
            "WHERE resource = 'integrate:main'"
        )
        self._db.execute(
            """
            CREATE TRIGGER claims_only_release_once
            BEFORE UPDATE ON claims
            WHEN
              OLD.claim_id != NEW.claim_id
              OR OLD.resource != NEW.resource
              OR OLD.holder_agent != NEW.holder_agent
              OR OLD.holder_session != NEW.holder_session
              OR OLD.purpose != NEW.purpose
              OR OLD.acquired_event_seq != NEW.acquired_event_seq
              OR OLD.released_event_seq IS NOT NULL
              OR NEW.released_event_seq IS NULL
            BEGIN
              SELECT RAISE(ABORT, 'relay claims may only transition active to released');
            END
            """
        )

        # A v1 process that opened before this migration must not recreate the
        # split alias afterward. SQLite applies this schema-level guard even to
        # that already-open connection, so mixed-client overlap fails closed.
        self._db.execute(
            """
            CREATE TRIGGER claims_reject_legacy_main_insert
            BEFORE INSERT ON claims
            WHEN NEW.resource = 'integrate:main'
            BEGIN
              SELECT RAISE(ABORT, 'legacy integrate:main claims require a v2 client');
            END
            """
        )

    def _harden_v2_ratchet_state(self) -> None:
        placeholders = ",".join("?" for _ in _RATCHET_STATE_TRIGGER_NAMES)
        installed = {
            str(row["name"])
            for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                f"AND name IN ({placeholders})",
                _RATCHET_STATE_TRIGGER_NAMES,
            ).fetchall()
        }
        if installed == set(_RATCHET_STATE_TRIGGER_NAMES):
            return
        self._audit_and_install_v2_ratchet_guards()

    def _audit_and_install_v2_ratchet_guards(self) -> None:
        malformed = self._db.execute(
            f"""
            SELECT event.seq
            FROM events AS event
            WHERE event.kind IN (
              'friction.observed', 'ratchet.decided', 'ratchet.verified'
            )
              AND ({_RATCHET_META_INVALID_STORED})
            ORDER BY event.seq
            LIMIT 6
            """
        ).fetchall()
        if malformed:
            sample = ", ".join(str(row["seq"]) for row in malformed[:5])
            suffix = ", ..." if len(malformed) > 5 else ""
            raise StateError(
                "Multithread v2 ratchet guards require normalized duplicate-free "
                f"metadata; invalid events: {sample}{suffix}"
            )

        invalid_decisions = self._db.execute(
            """
            SELECT decision.seq AS decision_seq
            FROM events AS decision
            WHERE decision.kind = 'ratchet.decided'
              AND (
                json_type(decision.meta_json, '$.fingerprint') IS NOT 'text'
                OR length(
                  json_extract(decision.meta_json, '$.fingerprint')
                ) NOT BETWEEN 3 AND 80
                OR json_type(decision.meta_json, '$.home') IS NOT 'text'
                OR length(
                  json_extract(decision.meta_json, '$.home')
                ) NOT BETWEEN 1 AND 200
                OR json_type(decision.meta_json, '$.mode') IS NOT 'text'
                OR json_extract(decision.meta_json, '$.mode')
                   NOT IN ('subtract', 'promote', 'drop')
                OR (
                  json_type(decision.meta_json, '$.verify_when') IS NOT NULL
                  AND (
                    json_type(
                      decision.meta_json, '$.verify_when'
                    ) IS NOT 'text'
                    OR length(
                      json_extract(decision.meta_json, '$.verify_when')
                    ) NOT BETWEEN 1 AND 200
                  )
                )
                OR NOT EXISTS (
                  SELECT 1
                  FROM events AS observation
                  WHERE observation.kind = 'friction.observed'
                    AND observation.seq < decision.seq
                    AND json_extract(observation.meta_json, '$.fingerprint')
                        = json_extract(decision.meta_json, '$.fingerprint')
                )
                OR EXISTS (
                  SELECT 1
                  FROM events AS prior_drop
                  WHERE prior_drop.kind = 'ratchet.decided'
                    AND prior_drop.seq < decision.seq
                    AND json_extract(prior_drop.meta_json, '$.fingerprint')
                        = json_extract(decision.meta_json, '$.fingerprint')
                    AND json_extract(prior_drop.meta_json, '$.mode') = 'drop'
                    AND NOT EXISTS (
                      SELECT 1
                      FROM events AS recurrence
                      WHERE recurrence.kind = 'friction.observed'
                        AND recurrence.seq > prior_drop.seq
                        AND recurrence.seq < decision.seq
                        AND json_extract(
                          recurrence.meta_json, '$.fingerprint'
                        ) = json_extract(
                          prior_drop.meta_json, '$.fingerprint'
                        )
                    )
                )
              )
            ORDER BY decision.seq
            LIMIT 6
            """
        ).fetchall()
        if invalid_decisions:
            sample = ", ".join(
                str(row["decision_seq"]) for row in invalid_decisions[:5]
            )
            suffix = ", ..." if len(invalid_decisions) > 5 else ""
            raise StateError(
                "Multithread v2 ratchet guards found decisions outside the mode, "
                "observation, or drop-recurrence invariant: "
                f"{sample}{suffix}"
            )

        invalid_empirical = self._db.execute(
            """
            SELECT verification.seq AS verification_seq
            FROM events AS verification
            WHERE verification.kind = 'ratchet.verified'
              AND json_type(
                verification.meta_json, '$.drop_compatibility'
              ) IS NULL
              AND (
                json_type(
                  verification.meta_json, '$.fingerprint'
                ) IS NOT 'text'
                OR length(
                  json_extract(verification.meta_json, '$.fingerprint')
                ) NOT BETWEEN 3 AND 80
                OR json_type(
                  verification.meta_json, '$.decision_seq'
                ) IS NOT 'integer'
                OR json_extract(
                  verification.meta_json, '$.decision_seq'
                ) < 1
                OR json_type(
                  verification.meta_json, '$.outcome'
                ) IS NOT 'text'
                OR json_extract(verification.meta_json, '$.outcome')
                   NOT IN ('improved', 'unchanged', 'worse')
                OR NOT EXISTS (
                  SELECT 1
                  FROM events AS decision
                  WHERE decision.seq = json_extract(
                    verification.meta_json, '$.decision_seq'
                  )
                    AND decision.seq < verification.seq
                    AND decision.kind = 'ratchet.decided'
                    AND json_extract(decision.meta_json, '$.fingerprint')
                        = json_extract(
                          verification.meta_json, '$.fingerprint'
                        )
                    AND json_extract(decision.meta_json, '$.mode')
                        IN ('subtract', 'promote')
                )
                OR EXISTS (
                  SELECT 1
                  FROM events AS prior_verification
                  WHERE prior_verification.kind = 'ratchet.verified'
                    AND prior_verification.seq < verification.seq
                    AND json_type(
                      prior_verification.meta_json, '$.drop_compatibility'
                    ) IS NULL
                    AND json_extract(
                      prior_verification.meta_json, '$.decision_seq'
                    ) = json_extract(
                      verification.meta_json, '$.decision_seq'
                    )
                )
              )
            ORDER BY verification.seq
            LIMIT 6
            """
        ).fetchall()
        if invalid_empirical:
            sample = ", ".join(
                str(row["verification_seq"])
                for row in invalid_empirical[:5]
            )
            suffix = ", ..." if len(invalid_empirical) > 5 else ""
            raise StateError(
                "Multithread v2 ratchet guards found empirical verification outside "
                "the outcome, decision, or uniqueness invariant: "
                f"{sample}{suffix}"
            )

        violations = self._db.execute(
            """
            SELECT verification.seq AS verification_seq,
                   decision.seq AS decision_seq
            FROM events AS verification
            LEFT JOIN events AS decision
              ON decision.seq = CAST(
                json_extract(verification.meta_json, '$.decision_seq') AS INTEGER
              )
            WHERE verification.kind = 'ratchet.verified'
              AND (
                (
                  decision.kind = 'ratchet.decided'
                  AND json_extract(decision.meta_json, '$.mode') = 'drop'
                )
                OR json_extract(verification.meta_json, '$.outcome') = 'dropped'
                OR json_type(
                  verification.meta_json, '$.drop_compatibility'
                ) IS NOT NULL
              )
              AND COALESCE(
                (
                  decision.kind = 'ratchet.decided'
                  AND json_extract(decision.meta_json, '$.mode') = 'drop'
                  AND
                  json_type(
                    verification.meta_json, '$.drop_compatibility'
                  ) = 'true'
                  AND json_extract(verification.meta_json, '$.outcome') = 'dropped'
                  AND verification.event_id = (
                    'drop-close:' || CAST(decision.seq AS TEXT)
                  )
                  AND verification.protocol_version = decision.protocol_version
                  AND verification.agent = decision.agent
                  AND verification.session = decision.session
                  AND verification.work_id IS decision.work_id
                  AND verification.target IS decision.target
                  AND verification.scope IS decision.scope
                  AND verification.artifact IS NULL
                  AND verification.summary = (
                    'Compatibility closure for terminal ratchet drop '
                    || CAST(decision.seq AS TEXT)
                  )
                  AND json_extract(verification.meta_json, '$.fingerprint')
                      = json_extract(decision.meta_json, '$.fingerprint')
                  AND (
                    SELECT COUNT(*) FROM json_each(verification.meta_json)
                  ) = 4
                ),
                0
              ) = 0
            ORDER BY verification.seq
            LIMIT 6
            """
        ).fetchall()
        if violations:
            sample = ", ".join(
                f"{row['verification_seq']}->{row['decision_seq']}"
                for row in violations[:5]
            )
            suffix = ", ..." if len(violations) > 5 else ""
            raise StateError(
                "Multithread v2 ratchet guards cannot enforce terminal drop decisions because "
                "incompatible ratchet verification or closure events already exist: "
                f"{sample}{suffix}"
            )

        unpaired_drops = self._db.execute(
            """
            SELECT decision.seq AS decision_seq
            FROM events AS decision
            WHERE decision.kind = 'ratchet.decided'
              AND json_extract(decision.meta_json, '$.mode') = 'drop'
              AND NOT EXISTS (
                SELECT 1
                FROM events AS closure
                WHERE closure.kind = 'ratchet.verified'
                  AND closure.event_id = (
                    'drop-close:' || CAST(decision.seq AS TEXT)
                  )
                  AND closure.protocol_version = decision.protocol_version
                  AND closure.agent = decision.agent
                  AND closure.session = decision.session
                  AND closure.work_id IS decision.work_id
                  AND closure.target IS decision.target
                  AND closure.scope IS decision.scope
                  AND closure.artifact IS NULL
                  AND closure.summary = (
                    'Compatibility closure for terminal ratchet drop '
                    || CAST(decision.seq AS TEXT)
                  )
                  AND json_type(
                    closure.meta_json, '$.drop_compatibility'
                  ) = 'true'
                  AND json_extract(closure.meta_json, '$.outcome') = 'dropped'
                  AND json_extract(closure.meta_json, '$.decision_seq')
                      = decision.seq
                  AND json_extract(closure.meta_json, '$.fingerprint')
                      = json_extract(decision.meta_json, '$.fingerprint')
                  AND (
                    SELECT COUNT(*) FROM json_each(closure.meta_json)
                  ) = 4
              )
            ORDER BY decision.seq
            LIMIT 6
            """
        ).fetchall()
        if unpaired_drops:
            sample = ", ".join(
                str(row["decision_seq"]) for row in unpaired_drops[:5]
            )
            suffix = ", ..." if len(unpaired_drops) > 5 else ""
            raise StateError(
                "Multithread v2 ratchet guards found unpaired terminal drop decisions; "
                f"refusing a partial upgrade: {sample}{suffix}"
            )

        for statement in _split_sql_script(_RATCHET_STATE_TRIGGERS):
            self._db.execute(statement)

    def _establish_wal(self) -> None:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(20):
            try:
                mode = str(self._db.execute("PRAGMA journal_mode = WAL").fetchone()[0])
                if mode.lower() != "wal":
                    raise StateError(f"Multithread requires SQLite WAL mode, got {mode!r}")
                return
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise _translate_sqlite(exc) from exc
                time.sleep(min(0.01 * (attempt + 1), 0.1))
        assert last_error is not None
        raise BusyError(f"Multithread could not establish WAL mode: {last_error}")

    def _secure_database_files(self) -> None:
        if _INSTALLED_ACCESS is not None:
            # Installed files were privately reserved and inode-pinned before
            # confinement. Never chmod a pathname that could now be replaced.
            _INSTALLED_ACCESS.verify()
            return
        _chmod_private(self.paths.database)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.paths.database}{suffix}")
            if sidecar.exists():
                _chmod_private(sidecar)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield
            self._db.execute("COMMIT")
        except RelayError:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        except sqlite3.OperationalError as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise _translate_sqlite(exc) from exc
        except sqlite3.DatabaseError as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise StateError(f"Multithread database transaction failed: {exc}") from exc
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        try:
            return self._db.execute(sql, params)
        except sqlite3.OperationalError as exc:
            raise _translate_sqlite(exc) from exc
        except sqlite3.DatabaseError as exc:
            raise StateError(f"Multithread database read failed: {exc}") from exc

    def _insert_event(self, event: Event) -> tuple[int, bool]:
        try:
            cursor = self._db.execute(
                """
                INSERT INTO events (
                  event_id, protocol_version, kind, agent, session, work_id,
                  target, scope, summary, artifact, meta_json, canonical_json,
                  body_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.v,
                    event.kind,
                    event.agent,
                    event.session,
                    event.work_id,
                    event.target,
                    event.scope,
                    event.summary,
                    event.artifact,
                    json.dumps(
                        event.meta,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    event.canonical_json,
                    event.body_hash,
                ),
            )
            return int(cursor.lastrowid), False
        except sqlite3.IntegrityError as exc:
            existing = self._db.execute(
                "SELECT seq, body_hash, canonical_json FROM events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing is None:
                raise StateError(f"Multithread event insert failed: {exc}") from exc
            if (
                existing["body_hash"] == event.body_hash
                and existing["canonical_json"] == event.canonical_json
            ):
                return int(existing["seq"]), True
            raise ConflictError(
                f"event id {event.event_id} was already used for different content"
            ) from exc

    def _insert_session_neutral_event(
        self,
        event: Event,
        *,
        conflict_message: str,
    ) -> tuple[int, bool]:
        """Deduplicate one semantic fact even when its retry session changed."""

        existing = self._db.execute(
            "SELECT * FROM events WHERE event_id = ?", (event.event_id,)
        ).fetchone()
        if existing is None:
            return self._insert_event(event)
        self._assert_canonical_event_row(existing)
        expected_meta = json.dumps(
            event.meta,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        semantic_fields = {
            "protocol_version": event.v,
            "kind": event.kind,
            "agent": event.agent,
            "work_id": event.work_id,
            "target": event.target,
            "scope": event.scope,
            "summary": event.summary,
            "artifact": event.artifact,
            "meta_json": expected_meta,
        }
        if all(
            existing[name] == value
            for name, value in semantic_fields.items()
        ):
            return int(existing["seq"]), True
        raise ConflictError(conflict_message)

    def _end_claim(
        self,
        claim_id: str,
        *,
        actor_agent: str,
        actor_session: str,
        kind: str,
        reason: str | None,
    ) -> dict[str, Any]:
        event_kind = "claim.released" if kind == "released" else "claim.broken"
        with self._transaction():
            row = self._db.execute(
                "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            if row is None:
                raise ConflictError(f"unknown claim id: {claim_id}")

            if kind == "released" and (
                actor_agent != row["holder_agent"]
                or actor_session != row["holder_session"]
            ):
                raise ConflictError("only the exact claim holder may release a claim")

            meta: dict[str, Any] = {
                "claim_id": claim_id,
                "resource": row["resource"],
                "holder_agent": row["holder_agent"],
                "holder_session": row["holder_session"],
            }
            if kind == "broken":
                meta.update(
                    {
                        "actor_agent": actor_agent,
                        "actor_session": actor_session,
                        "reason": reason,
                    }
                )
            event = normalize_event(
                {
                    "v": 1,
                    "id": f"claim:{claim_id}:{kind}",
                    "kind": event_kind,
                    "agent": actor_agent,
                    "session": actor_session,
                    "target": row["resource"],
                    "summary": (
                        f"Released {row['resource']}"
                        if kind == "released"
                        else f"Broke {row['resource']} claim: {reason}"
                    ),
                    "meta": meta,
                },
                internal=True,
            )

            if row["released_event_seq"] is not None:
                if (
                    row["release_kind"] == kind
                    and row["released_by_agent"] == actor_agent
                    and row["released_by_session"] == actor_session
                    and row["release_reason"] == reason
                ):
                    prior = self._db.execute(
                        "SELECT body_hash, canonical_json FROM events WHERE seq = ?",
                        (row["released_event_seq"],),
                    ).fetchone()
                    if prior is not None and (
                        prior["body_hash"] == event.body_hash
                        and prior["canonical_json"] == event.canonical_json
                    ):
                        return self._claim_receipt(row, duplicate=True)
                raise ConflictError(f"claim {claim_id} is already inactive")

            seq, _ = self._insert_event(event)
            updated = self._db.execute(
                """
                UPDATE claims
                SET released_event_seq = ?, release_kind = ?,
                    released_by_agent = ?, released_by_session = ?,
                    release_reason = ?
                WHERE claim_id = ? AND released_event_seq IS NULL
                """,
                (seq, kind, actor_agent, actor_session, reason, claim_id),
            )
            if updated.rowcount != 1:
                raise ConflictError(f"claim {claim_id} changed while ending it")
            ended = self._db.execute(
                "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            assert ended is not None
            return self._claim_receipt(ended, duplicate=False)

    def _fingerprint_exists(self, fingerprint: str, kind: str) -> bool:
        return self._latest_fingerprint_seq(fingerprint, kind) is not None

    def _latest_drop_without_recurrence(self, fingerprint: str) -> int | None:
        rows = self._db.execute(
            "SELECT seq, meta_json FROM events "
            "WHERE kind = 'ratchet.decided' ORDER BY seq DESC"
        ).fetchall()
        latest_drop = next(
            (
                int(row["seq"])
                for row in rows
                if (
                    (meta := json.loads(row["meta_json"])).get("fingerprint")
                    == fingerprint
                    and meta.get("mode") == "drop"
                )
            ),
            None,
        )
        if latest_drop is None:
            return None
        latest_observation = self._latest_fingerprint_seq(
            fingerprint, "friction.observed"
        )
        if latest_observation is not None and latest_observation > latest_drop:
            return None
        return latest_drop

    def _latest_fingerprint_seq(self, fingerprint: str, kind: str) -> int | None:
        rows = self._db.execute(
            "SELECT seq, meta_json FROM events WHERE kind = ? ORDER BY seq DESC", (kind,)
        ).fetchall()
        for row in rows:
            if json.loads(row["meta_json"]).get("fingerprint") == fingerprint:
                return int(row["seq"])
        return None

    def _decision_has_verification(self, fingerprint: str, decision_seq: int) -> bool:
        rows = self._db.execute(
            "SELECT meta_json FROM events WHERE kind = 'ratchet.verified' ORDER BY seq DESC"
        ).fetchall()
        for row in rows:
            meta = json.loads(row["meta_json"])
            if (
                meta.get("fingerprint") == fingerprint
                and meta.get("decision_seq") == decision_seq
            ):
                return True
        return False

    def _event_receipt(self, seq: int, duplicate: bool) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
        assert row is not None
        return {"duplicate": duplicate, "event": self._event_row(row)}

    def _claim_receipt(self, row: sqlite3.Row, *, duplicate: bool) -> dict[str, Any]:
        return {"duplicate": duplicate, "claim": self._claim_row(row)}

    @staticmethod
    def _assert_canonical_event_row(row: sqlite3.Row) -> None:
        """Reject a stored event whose columns drift from its canonical body."""

        try:
            raw = json.loads(row["canonical_json"])
            if not isinstance(raw, Mapping):
                raise ValueError("canonical event is not an object")
            event = normalize_event(raw, internal=True)
            expected = {
                "protocol_version": event.v,
                "event_id": event.event_id,
                "kind": event.kind,
                "agent": event.agent,
                "session": event.session,
                "work_id": event.work_id,
                "target": event.target,
                "scope": event.scope,
                "summary": event.summary,
                "artifact": event.artifact,
                "meta_json": json.dumps(
                    event.meta,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "canonical_json": event.canonical_json,
                "body_hash": event.body_hash,
            }
        except (RelayError, TypeError, ValueError, json.JSONDecodeError) as exc:
            seq = row["seq"] if "seq" in row.keys() else "unknown"
            raise StateError(
                f"Multithread event {seq} has invalid canonical state"
            ) from exc
        mismatched = [
            name for name, value in expected.items()
            if row[name] != value
        ]
        if mismatched:
            raise StateError(
                f"Multithread event {int(row['seq'])} failed canonical integrity"
            )

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": int(row["seq"]),
            "recorded_at": row["recorded_at"],
            "v": int(row["protocol_version"]),
            "id": row["event_id"],
            "kind": row["kind"],
            "agent": row["agent"],
            "session": row["session"],
            "work_id": row["work_id"],
            "target": row["target"],
            "scope": row["scope"],
            "summary": row["summary"],
            "artifact": row["artifact"],
            "meta": json.loads(row["meta_json"]),
            "body_hash": row["body_hash"],
        }

    @staticmethod
    def _claim_row(row: sqlite3.Row) -> dict[str, Any]:
        out = {
            "claim_id": row["claim_id"],
            "resource": row["resource"],
            "holder_agent": row["holder_agent"],
            "holder_session": row["holder_session"],
            "purpose": row["purpose"],
            "acquired_event_seq": int(row["acquired_event_seq"]),
            "released_event_seq": (
                int(row["released_event_seq"])
                if row["released_event_seq"] is not None
                else None
            ),
            "release_kind": row["release_kind"],
            "released_by_agent": row["released_by_agent"],
            "released_by_session": row["released_by_session"],
            "release_reason": row["release_reason"],
        }
        if "acquired_at" in row.keys():
            out["acquired_at"] = row["acquired_at"]
        return out


def record_hook_failure(paths: RelayPaths, client: str, message: str) -> None:
    """Best-effort, sanitized observation failure log.

    Lifecycle hooks must never block an agent turn.  This log contains only the
    client name and exception class/message emitted by Multithread itself—never hook
    stdin or environment values.
    """

    if _INSTALLED_ACCESS is not None:
        # Installed observation errors go to stderr only. No path-based log
        # write or permission repair is admitted alongside SQLite objects.
        return
    try:
        safe_client = "".join(ch for ch in client if ch.isalnum() or ch in "._-")[:32]
        safe_message = message.replace("\r", " ").replace("\n", " ")[:500]
        with paths.hook_error_log.open("a", encoding="utf-8") as handle:
            handle.write(f"{safe_client}: {safe_message}\n")
        _chmod_private(paths.hook_error_log)
    except OSError:
        return


def _git_path(cwd: Path, flag: str) -> Path:
    env = dict(os.environ)
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(key, None)
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", flag],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        if "not a git repository" in (exc.stderr or "").lower():
            raise StateError(
                f"{json.dumps(str(cwd), ensure_ascii=True)} is not a Git checkout: run from an "
                "enrolled checkout or pass --repo <checkout>"
            ) from exc
        raise StateError(f"cannot resolve Multithread repository identity: {exc}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise StateError(f"cannot resolve Multithread repository identity: {exc}") from exc
    value = result.stdout.strip()
    if not value:
        raise StateError("git returned an empty repository path")
    return Path(value).resolve()


def _expected_workspace_binding() -> Path | None:
    """The canonical Git common directory that owns this shipping package.

    Multithread resolves *which* repository a caller is standing in, but that answer has
    never been checked against *which* repository this Multithread belongs to.  The
    binding is that missing second half, and it is derived from one place only:
    the Git common directory containing this package's own resolved ``__file__``.
    A requested repository can therefore never authorize itself.

    Returns ``None`` when no binding can be derived.  That covers a materialized
    runtime outside any repository, but also a missing or unusable ``git`` and a
    timed-out query, so neither this function nor its diagnostic may claim the
    package is definitively outside Git.  Callers fail closed on ``None`` rather
    than guess; the runtime binding is a later phase's contract, not this one's.
    """

    try:
        return _git_path(_PACKAGE_SOURCE_ROOT, "--git-common-dir")
    except StateError:
        return None


def _assert_workspace_binding(actual_common: Path) -> None:
    """Refuse a workspace this Multithread installation is not bound to.

    Raised before any mutation: no directory is created, no database is opened,
    and no failure log is written for a workspace we are about to reject.
    """

    expected = _expected_workspace_binding()
    if expected is None:
        raise StateError(
            "Multithread refuses an unbound workspace: no expected workspace binding "
            f"could be derived for the shipping package at {_PACKAGE_SOURCE_ROOT}; "
            f"the requested workspace resolved to {actual_common}"
        )
    if actual_common != expected:
        raise StateError(
            "Multithread refuses a foreign workspace: the requested workspace resolved to "
            f"{actual_common} but this Multithread is bound to {expected}"
        )


def _secure_state_dir(path: Path) -> None:
    _assert_no_symlink_components(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    except OSError as exc:
        raise StateError(f"cannot create private Multithread state directory {path}: {exc}") from exc
    _assert_no_symlink_components(path)


def _assert_no_symlink_components(path: Path) -> None:
    """Refuse an existing symlink anywhere in an absolute Multithread state path."""

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise StateError(f"cannot inspect Multithread state path {current}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise StateError(f"Multithread refuses symlinked state path component: {current}")


def _chmod_private(path: Path) -> None:
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        if current != 0o600:
            path.chmod(0o600)
    except OSError as exc:
        raise StateError(f"cannot secure Multithread state file {path}: {exc}") from exc


def _translate_sqlite(exc: sqlite3.OperationalError) -> RelayError:
    message = str(exc)
    if "locked" in message.lower() or "busy" in message.lower():
        return BusyError(f"Multithread database is busy: {message}")
    return StateError(f"Multithread database operation failed: {message}")


def _split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                statements.append(statement)
            pending = ""
    if pending.strip():
        raise StateError("Multithread schema contains an incomplete SQL statement")
    return statements


def _require_decision_rollout(value: Any) -> None:
    """Fence new v1 decision kinds until every active client can parse them."""

    if value != DECISION_ROLLOUT_FENCE:
        raise ValidationError(
            "decision protocol rollout fence requires active-clients-refreshed"
        )


def _inbox_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise ValidationError("inbox/history limit must be between 1 and 100")
    return value


def _brief_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError("brief limit must be an integer")
    if value < 1 or value > BRIEF_MAX_LIMIT:
        raise ValidationError(
            f"brief limit must be between 1 and {BRIEF_MAX_LIMIT}"
        )
    return value
