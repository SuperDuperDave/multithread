"""Account-level provider hooks: one installation that every session reaches.

Multithread appends one handler per lifecycle event to each provider's user
hook file, so a session in an enrolled checkout reaches its ledger however it
was started. It never edits, reorders or removes another hook. Codex runs a user
hook only once trusted; Multithread records that trust only through Codex's own
config API, for listed hooks whose definition and hash match what it installed.
Provider session records are read for metadata only: identity, directory, time.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid

from . import account_launcher, codex_peer
from .native_io import ProtocolError


EVENTS = {"codex": ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "Interrupt"),
          "claude": ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")}
_NAMES = {"codex": "Codex", "claude": "Claude Code"}
_LABELS = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit",
           "Stop": "stop", "SessionEnd": "session_end", "Interrupt": "interrupt"}
_LISTED = {"SessionStart": "sessionStart", "UserPromptSubmit": "userPromptSubmit",
           "Stop": "stop", "SessionEnd": "sessionEnd", "Interrupt": "interrupt"}
_TIMEOUT = 3
_MAX_FILE = 1024 * 1024
_MAX_SESSIONS = 64
WINDOW_HOURS = 24
_GIT_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}


class HooksError(Exception):
    """A refusal; its message names the recovery."""


# --- The definition Multithread installs ------------------------------------

def command(client):
    """The exact hook command for this account; one spelling, no checkout."""
    launcher = str(account_launcher())
    if client == "codex":
        # Codex trusts exact command text per event slot. This is the same text
        # launch and peer pass as invocation hooks; Codex runs it in the
        # session's working directory, which selects the checkout.
        return shlex.join([launcher, "provider-hook", "--client", "codex"])
    # Claude keeps no per-hook trust, and its working directory follows cd.
    # Anchor to the directory the session started in, which Claude exports.
    return shlex.quote(launcher) + ' --repo "$CLAUDE_PROJECT_DIR" provider-hook --client claude'


def handler(client):
    return {"type": "command", "command": command(client), "timeout": _TIMEOUT}


def codex_hash(event):
    """Codex's trust hash for Multithread's handler, derived independently.

    Codex hashes a normalized identity, not source text: the event label and a
    matcher group holding only this handler, as sorted compact JSON.
    """
    identity = {"event_name": _LABELS[event],
                "hooks": [{"type": "command", "command": command("codex"),
                           "timeout": _TIMEOUT, "async": False}]}
    body = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def provider_home(client, environ=None):
    """The directory the provider itself reads user settings from."""
    environ = os.environ if environ is None else environ
    value = environ.get("CODEX_HOME" if client == "codex" else "CLAUDE_CONFIG_DIR")
    if value and os.path.isabs(value):
        return Path(value)
    home = environ.get("HOME")
    base = Path(home) if home and os.path.isabs(home) else Path(pwd.getpwuid(os.getuid()).pw_dir)
    return base / (".codex" if client == "codex" else ".claude")


def hook_file(client, environ=None):
    return provider_home(client, environ) / ("hooks.json" if client == "codex" else "settings.json")


def _launcher_command(*arguments):
    return [str(account_launcher()), *arguments]


# --- Reading and classifying the user hook file -----------------------------

def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("repeated key")
        value[key] = item
    return value


def _read(path):
    """The file's bytes and strict JSON object, or (None, None) when absent."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None
    if stat.S_ISLNK(info.st_mode):
        raise HooksError(f"{path} is a symbolic link, and Multithread edits only a regular file. Add the "
                         "entries from `multithread hooks install --json` to the link's target yourself.")
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE:
        raise HooksError(f"{path} is not a regular settings file within 1 MiB; inspect it before changing hooks.")
    body = Path(path).read_bytes()
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeError, ValueError, RecursionError):
        raise HooksError(f"{path} is not valid JSON, or repeats a key. Fix it with the provider's own "
                         "settings, then run this again.") from None
    if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
        raise HooksError(f"{path} does not hold a JSON object whose hooks value is an object; inspect it "
                         "before changing hooks.")
    return body, value


def _ours(item):
    return isinstance(item, dict) and codex_peer._multithread_hook(item.get("command"), str(account_launcher()))


def _slots(value):
    """Every handler slot in file order: (event, group, handler, group dict, handler dict)."""
    for event, groups in ((value or {}).get("hooks") or {}).items():
        if not isinstance(groups, list):
            continue
        for g, group in enumerate(groups):
            handlers = group.get("hooks") if isinstance(group, dict) else None
            for h, item in enumerate(handlers if isinstance(handlers, list) else ()):
                yield event, g, h, group, item


def _classify(client, value):
    """Per-event state of Multithread's handlers, and counts of everyone else's."""
    exact = handler(client)
    found = {event: [] for event in EVENTS[client]}
    unexpected, others = [], {}
    for event, g, h, group, item in _slots(value):
        if not _ours(item):
            others[event] = others.get(event, 0) + 1
        elif event not in found:
            unexpected.append({"event": event, "group": g, "handler": h})
        else:
            found[event].append({"group": g, "handler": h,
                                 "exact": item == exact and group.get("matcher") is None})
    events = {}
    for event, entries in found.items():
        state = ("missing" if not entries else "duplicate" if len(entries) > 1
                 else "installed" if entries[0]["exact"] else "mismatched")
        events[event] = {"state": state, "slots": [[e["group"], e["handler"]] for e in entries]}
    states = {entry["state"] for entry in events.values()}
    if unexpected or states & {"duplicate", "mismatched"}:
        overall = "needs_repair"
    elif states == {"installed"}:
        overall = "installed"
    elif states == {"missing"}:
        overall = "absent"
    else:
        overall = "incomplete"
    return {"state": overall, "events": events, "unexpected": unexpected, "other_hooks": others}


def inspect(client, environ=None):
    """Read-only: what Multithread finds in the provider's user hook file."""
    path = hook_file(client, environ)
    result = {"client": client, "file": str(path), "command": command(client)}
    if not path.parent.is_dir():
        return {**result, "state": "provider_not_found", "events": {}, "unexpected": [], "other_hooks": {}}
    _, value = _read(path)
    return {**result, **_classify(client, value)}


def delivery(client, environ=None):
    """Where launch and peer get this provider's hooks, so each event runs once.

    Installed user-level hooks replace the invocation copies; with none, the
    invocation passes its own. Anything in between refuses: a partial or
    altered installation beside invocation copies would fire twice or not at all.
    """
    state = inspect(client, environ)
    if state["state"] == "installed":
        return {"source": "user", "file": state["file"], "command": state["command"]}
    if state["state"] in ("absent", "provider_not_found"):
        return {"source": "session_flags", "file": None, "command": None}
    raise HooksError(
        f"Multithread's user-level {_NAMES[client]} hooks in {state['file']} are {_condition(state['state'])} "
        f"({_event_summary(state)}), so this launch cannot tell whether each event would run once. Fix: "
        + shlex.join(_launcher_command("hooks", "install", "--client", client))
        + (" (or `hooks remove`, then install, for a duplicate)." if state["state"] == "needs_repair" else "."))


def _condition(state):
    """How an installation state reads in a sentence."""
    return {"needs_repair": "altered or duplicated", "provider_not_found": "not set up (no provider settings "
            "directory)", "absent": "not installed"}.get(state, state.replace("_", " "))


def _event_summary(state):
    parts = [event + ": " + entry["state"] for event, entry in state["events"].items()
             if entry["state"] != "installed"]
    parts += [item["event"] + ": unexpected" for item in state["unexpected"]]
    return "; ".join(parts) or "none"


# --- Writing the user hook file ---------------------------------------------

def _serialize(value, like=None):
    """JSON in the file's own style (indent, ASCII escapes), so only our entries change."""
    indent = re.search(rb"\n( +)\S", like) if like else None
    return (json.dumps(value, indent=len(indent.group(1)) if indent else 2,
                       ensure_ascii=bool(like) and like.isascii()) + "\n").encode("utf-8")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _sha(body):
    return None if body is None else hashlib.sha256(body).hexdigest()


def _plan(client, action, environ=None):
    """The exact new file for install or remove; nothing is written."""
    path = hook_file(client, environ)
    plan = {"client": client, "action": action, "file": str(path), "command": command(client),
            "changes": [], "moved_hooks": [], "reformatted": False}
    if not path.parent.is_dir():
        return {**plan, "state": "provider_not_found", "before_sha256": None, "after_sha256": None, "_body": None}
    body, value = _read(path)
    state = _classify(client, value)
    data = copy.deepcopy(value) if value is not None else {}
    hooks = data.setdefault("hooks", {})
    if action == "install":
        if state["unexpected"] or any(entry["state"] == "duplicate" for entry in state["events"].values()):
            raise HooksError(f"{path} holds more than one Multithread hook for an event ({_event_summary(state)}). "
                             "Run " + shlex.join(_launcher_command("hooks", "remove", "--client", client))
                             + " to take them all out, then install again.")
        for event, entry in state["events"].items():
            if entry["state"] == "installed":
                continue
            if entry["state"] == "mismatched":
                g, h = entry["slots"][0]
                group = hooks[event][g]
                if group.get("matcher") is not None and len(group["hooks"]) > 1:
                    raise HooksError(f"An older Multithread hook for {event} in {path} shares a matcher group "
                                     "with other hooks. Run " + shlex.join(_launcher_command(
                                         "hooks", "remove", "--client", client)) + ", then install again.")
                group.pop("matcher", None)
                group["hooks"][h] = handler(client)
                plan["changes"].append({"event": event, "change": "replace_older_multithread_hook", "slot": [g, h]})
            else:
                # Append after everything else: existing slots keep their
                # positions, so Codex's trust in other hooks is untouched.
                groups = hooks.setdefault(event, [])
                if not isinstance(groups, list):
                    raise HooksError(f"{path} has a {event} value that is not a list; fix it before installing.")
                groups.append({"hooks": [handler(client)]})
                plan["changes"].append({"event": event, "change": "append", "slot": [len(groups) - 1, 0]})
    else:
        before = [(event, g, h) for event, g, h, _, item in _slots(value) if not _ours(item)]
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            removed_any = False
            for g in reversed(range(len(groups))):
                group = groups[g]
                handlers = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(handlers, list):
                    continue
                removed = False
                for h in reversed(range(len(handlers))):
                    if _ours(handlers[h]):
                        del handlers[h]
                        removed = removed_any = True
                        plan["changes"].append({"event": event, "change": "remove", "slot": [g, h]})
                if removed and not handlers:
                    del groups[g]
            if removed_any and not groups:
                del hooks[event]
        after = [(event, g, h) for event, g, h, _, _ in _slots(data)]
        for old, new in zip(before, after):
            if old != new:
                plan["moved_hooks"].append({"event": old[0], "from": list(old[1:]), "to": list(new[1:])})
        plan["changes"].sort(key=lambda change: (change["event"], change["slot"]))
    new = _serialize(data, body) if plan["changes"] else body
    plan["reformatted"] = bool(plan["changes"]) and body is not None and _serialize(value, body) != body
    plan.update(state=state["state"], before_sha256=_sha(body), after_sha256=_sha(new), _body=new)
    return plan


def _write(plan):
    """Replace the file with the planned bytes, keeping a private copy of the old ones."""
    path = Path(plan["file"])
    try:
        current = path.read_bytes() if os.path.lexists(path) else None
    except OSError:
        current = b""
    if _sha(current) != plan["before_sha256"]:
        raise HooksError(f"{path} changed after the plan was made; nothing was written. Review the new plan.")
    backup = None
    mode = 0o600
    if current is not None:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        backup = path.with_name(path.name + ".multithread-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                                + "-" + uuid.uuid4().hex[:8] + ".bak")
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(current)
            stream.flush()
            os.fsync(stream.fileno())
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".multithread-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(plan["_body"])
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return None if backup is None else str(backup)


def _public(plan):
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def change_files(action, clients, environ=None, *, expected=None):
    """Plan, or with the reviewed plan's digest apply, install or remove."""
    plans = [_plan(client, action, environ) for client in clients]
    digest = _digest([[p["client"], p["file"], p["before_sha256"], p["after_sha256"]] for p in plans])
    result = {"schema": 1, "action": action, "plans": [_public(p) for p in plans], "plan_sha256": digest,
              "changes_provider_settings": any(p["changes"] for p in plans)}
    if expected is None:
        result["state"] = "planned" if result["changes_provider_settings"] else "unchanged"
        return result
    if expected != digest:
        raise HooksError("The hook files or the plan changed since it was shown; nothing was written. Review the "
                         "new plan: " + shlex.join(_launcher_command("hooks", action, "--json")))
    for plan, public in zip(plans, result["plans"]):
        if plan["changes"]:
            public["backup"] = _write(plan)
    result["state"] = "applied" if result["changes_provider_settings"] else "unchanged"
    return result


# --- Codex trust through Codex's own config API ------------------------------

def _codex(explicit):
    selected = str(explicit) if explicit is not None else shutil.which("codex")
    if not selected:
        raise HooksError("Codex was not found on PATH; pass its reviewed absolute path with --codex.")
    path = Path(selected)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise HooksError("--codex must name an executable at an absolute path.")
    return str(path)


def _config_state(path):
    """Codex's recorded hook state, read-only; {} when absent."""
    try:
        with open(path, "rb") as stream:
            state = tomllib.load(stream).get("hooks", {}).get("state", {})
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        raise HooksError(f"{path} could not be read as TOML; open Codex once to repair it, then run this again.") from None
    return state if isinstance(state, dict) else {}


def trust_estimate(environ=None):
    """Offline estimate of Codex trust for Multithread's installed slots.

    Codex decides; this only compares its recorded hashes with the definition,
    for status lines that cannot start Codex. Codex's listing is authoritative.
    """
    installed = inspect("codex", environ)
    if installed["state"] != "installed":
        return "not_installed"
    state = _config_state(provider_home("codex", environ) / "config.toml")
    for event, entry in installed["events"].items():
        g, h = entry["slots"][0]
        record = state.get(f"{installed['file']}:{_LABELS[event]}:{g}:{h}")
        if not isinstance(record, dict) or record.get("enabled") is False or record.get("trusted_hash") != codex_hash(event):
            return "not_trusted"
    return "trusted"


def _user_layer(config):
    layers = config.get("layers") if isinstance(config, dict) else None
    users = [layer for layer in layers or () if isinstance(layer, dict)
             and isinstance(layer.get("name"), dict) and layer["name"].get("type") == "user"]
    if len(users) != 1 or not isinstance(users[0].get("version"), str):
        raise HooksError("Codex did not report exactly one user configuration layer; nothing was trusted.")
    return users[0]["name"].get("file"), users[0]["version"]


def _review(listing, cwd, installed):
    """Match each Multithread event to exactly one listed hook Codex will trust.

    Every field Codex hashes must equal what Multithread installed, the listed
    key must name the slot the file holds, and Codex's own hash must equal the
    one derived here. Anything else refuses; no other hook is ever included.
    """
    entries = [group for group in listing.get("data", ()) if isinstance(group, dict) and group.get("cwd") == cwd]
    if len(entries) != 1 or not isinstance(entries[0].get("hooks"), list):
        raise HooksError("Codex's hook listing did not answer for the directory asked; nothing was trusted.")
    listed = [hook for hook in entries[0]["hooks"] if isinstance(hook, dict)]
    launcher = str(account_launcher())
    reviewed, problems = [], []
    for event, entry in installed["events"].items():
        g, h = entry["slots"][0]
        suffix = f":{_LABELS[event]}:{g}:{h}"
        mine = [hook for hook in listed if hook.get("eventName") == _LISTED[event]
                and (codex_peer._multithread_hook(hook.get("command"), launcher) or hook.get("source") == "sessionFlags")]
        hook = mine[0] if len(mine) == 1 else {}
        checks = (
            (len(mine) == 1, f"Codex lists {len(mine)} Multithread hooks for this event, not one"),
            (hook.get("source") == "user" and codex_peer._same_path(hook.get("sourcePath"), installed["file"]),
             "the listed hook does not come from " + installed["file"]),
            (isinstance(hook.get("key"), str) and hook["key"].endswith(suffix),
             "the listed position differs from the file's"),
            (hook.get("handlerType") == "command" and hook.get("command") == installed["command"],
             "the listed command differs from the one Multithread installed"),
            (hook.get("timeoutSec") == _TIMEOUT and hook.get("async", False) is False
             and hook.get("matcher") is None and hook.get("isManaged") is False,
             "the listed timeout, matcher or execution mode differs"),
            (hook.get("currentHash") == codex_hash(event),
             "Codex's hash differs from the hash of the definition Multithread installed"),
            (hook.get("enabled") is True, "the person disabled this hook in /hooks; re-enable it there first"),
            (hook.get("trustStatus") in ("trusted", "untrusted", "modified"), "Codex reports an unrecognized trust status"),
        )
        failed = next((reason for ok, reason in checks if not ok), None)
        if failed:
            problems.append(event + ": " + failed)
            continue
        reviewed.append({"event": event, "key": hook["key"], "hash": hook["currentHash"],
                         "status": hook["trustStatus"]})
    others = sorted({hook.get("trustStatus") for hook in listed
                     if hook.get("key") not in {item["key"] for item in reviewed}} - {None, "trusted", "managed"})
    pending = sum(1 for hook in listed if hook.get("key") not in {item["key"] for item in reviewed}
                  and hook.get("trustStatus") in ("untrusted", "modified"))
    return reviewed, problems, {"statuses": others, "needing_review": pending}


def trust(codex=None, environ=None, *, revoke=False, expected=None, timeout=20):
    """Plan, or with the reviewed digest apply, Codex trust for Multithread's hooks.

    Trust is recorded exactly as Codex's own /hooks review records it: one
    config/batchWrite of hooks.state entries holding the hash Codex listed,
    guarded by the configuration version read in the same session. Revoking
    removes only trust records holding Multithread's definition hashes.
    """
    executable = _codex(codex)
    installed = inspect("codex", environ)
    if not revoke and installed["state"] != "installed":
        raise HooksError(f"Multithread's Codex hooks are {_condition(installed['state'])} in {installed['file']}; "
                         "install them first: " + shlex.join(_launcher_command("hooks", "install", "--client", "codex")))
    action = "revoke" if revoke else "trust"
    result = {"schema": 1, "client": "codex", "action": action, "hook_file": installed["file"],
              "command": command("codex"), "changes_provider_settings": False}
    with tempfile.TemporaryDirectory(prefix="multithread-hooks-") as neutral:
        # A neutral directory has no project layer; user hooks list for any cwd.
        with codex_peer.AppServer([executable], neutral, timeout=timeout) as server:
            config_file, version = _user_layer(server.call("config/read", {"includeLayers": True}))
            result.update(user_config=config_file, user_config_version=version)
            if revoke:
                hashes = {codex_hash(event) for event in EVENTS["codex"]}
                prefix = installed["file"] + ":"
                records = _config_state(config_file) if isinstance(config_file, str) else {}
                revoked = sorted(key for key, record in records.items() if key.startswith(prefix)
                                 and isinstance(record, dict) and record.get("trusted_hash") in hashes)
                result["hooks"] = [{"key": key, "hash": records[key]["trusted_hash"]} for key in revoked]
                # Remove the whole record when trust is all it holds; keep a
                # person's own setting (enabled) and remove only the hash.
                edits = [{"keyPath": 'hooks.state."' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'
                          + ("" if set(records[key]) == {"trusted_hash"} else ".trusted_hash"),
                          "value": None, "mergeStrategy": "replace"} for key in revoked]
            else:
                reviewed, problems, others = _review(server.call("hooks/list", {"cwds": [neutral]}), neutral, installed)
                result.update(hooks=reviewed, other_hooks=others)
                if problems:
                    result.update(state="refused", problems=problems, message=(
                        "Nothing was trusted: " + "; ".join(problems) + ". Inspect "
                        + shlex.join(_launcher_command("hooks", "status")) + ", reinstall with "
                        + shlex.join(_launcher_command("hooks", "install", "--client", "codex"))
                        + " if the file changed, or trust by hand in a Codex terminal's /hooks."))
                    return result
                pending = [item for item in reviewed if item["status"] != "trusted"]
                edits = [] if not pending else [{"keyPath": "hooks.state", "mergeStrategy": "upsert", "value": {
                    item["key"]: {"trusted_hash": item["hash"]} for item in pending}}]
            result["edits"] = edits
            digest = _digest([action, installed["file"], result["command"], version, result["hooks"], edits])
            result["plan_sha256"] = digest
            if not edits:
                result["state"] = "already_trusted" if not revoke else "nothing_to_revoke"
                return result
            if expected is None:
                result["state"] = "planned"
                result["apply_argv"] = _launcher_command("hooks", "trust", *(["--revoke"] if revoke else []),
                                                         "--codex", executable, "--yes", "--expected-plan", digest, "--json")
                result["apply_command"] = shlex.join(result["apply_argv"])
                return result
            if expected != digest:
                raise HooksError("Codex's hooks or configuration changed since the plan was shown; nothing was "
                                 "written. Review the new plan: " + shlex.join(_launcher_command(
                                     "hooks", "trust", *(["--revoke"] if revoke else []), "--json")))
            written = server.call("config/batchWrite", {"edits": edits, "expectedVersion": version,
                                                        "reloadUserConfig": True})
            result.update(changes_provider_settings=True, written_version=written.get("version"))
            after = server.call("hooks/list", {"cwds": [neutral]})
    statuses = {hook.get("key"): hook.get("trustStatus")
                for group in after.get("data", ()) if isinstance(group, dict)
                for hook in group.get("hooks", ()) if isinstance(hook, dict)}
    for item in result["hooks"]:
        item["status_after"] = statuses.get(item["key"], "not_listed")
    if revoke:
        # A record for a slot the file no longer holds is not listed at all.
        verified = all(item["status_after"] != "trusted" for item in result["hooks"])
    else:
        verified = all(item["status_after"] == "trusted" for item in result["hooks"])
    result["state"] = ("revoked" if revoke else "trusted") if verified else "uncertain"
    if not verified:
        result["message"] = ("Codex accepted the write but its listing does not show the expected result; inspect "
                             + shlex.join(_launcher_command("hooks", "status")) + " before retrying.")
    elif not revoke:
        result["revoke_command"] = shlex.join(_launcher_command("hooks", "trust", "--revoke"))
    return result


def manual_steps(installed=None):
    """The native review, in plain steps a person can follow once."""
    installed = installed or {"file": str(hook_file("codex")), "command": command("codex")}
    return [
        "Open a terminal. Codex's terminal /hooks review records trust; the desktop app's hook screen may not "
        "(openai/codex#47283).",
        "Run codex in your home directory; no task is needed.",
        "Type /hooks. For SessionStart, UserPromptSubmit, Stop, SessionEnd and Interrupt, trust the hook from "
        + installed["file"] + " whose command is exactly: " + installed["command"],
        "Leave other hooks as you choose; Multithread did not install them.",
        "Quit Codex, then run: " + shlex.join(_launcher_command("hooks", "status")),
    ]


def status(clients=("codex", "claude"), environ=None, *, codex=None, listing=True):
    """Read-only: installation, Codex trust and the next step for each provider."""
    result = {"schema": 1, "providers": {}, "next_actions": []}
    for client in clients:
        try:
            entry = inspect(client, environ)
        except HooksError as exc:
            entry = {"client": client, "state": "unreadable", "message": str(exc)}
        result["providers"][client] = entry
        if entry["state"] in ("absent", "incomplete", "needs_repair"):
            result["next_actions"].append({
                "client": client, "action": "Install Multithread's user-level hooks so every "
                + _NAMES[client] + " session in an enrolled repository reaches its ledger, however it was "
                "started. It shows the exact change and asks first.",
                "command": _launcher_command("hooks", "install", "--client", client)})
        if client != "codex" or entry["state"] != "installed":
            continue
        trust_state = {"source": "estimated_from_config", "state": "unknown"}
        try:
            trust_state["state"] = trust_estimate(environ)
            if listing:
                plan = trust(codex, environ)
                trust_state = {"source": "codex_listing", "state": {
                    "already_trusted": "trusted", "planned": "not_trusted"}.get(plan["state"], plan["state"]),
                    "events": {item["event"]: item["status"] for item in plan.get("hooks", ())},
                    "other_hooks": plan.get("other_hooks")}
                if plan.get("message"):
                    trust_state["message"] = plan["message"]
        except (HooksError, ProtocolError, OSError, subprocess.SubprocessError) as exc:
            trust_state["listing"] = "unavailable"
            if isinstance(exc, HooksError):
                trust_state["message"] = str(exc)
        entry["trust"] = trust_state
        if trust_state["state"] == "refused":
            result["next_actions"].append({
                "client": "codex", "action": trust_state["message"],
                "command": _launcher_command("hooks", "install", "--client", "codex")})
        elif trust_state["state"] != "trusted":
            result["next_actions"].append({
                "client": "codex", "action": "Codex skips these hooks until they are trusted. The person chooses how:",
                "options": [
                    {"mode": "manual", "actor": "person", "steps": manual_steps(entry)},
                    {"mode": "agent_assisted", "actor": "agent, only after the person chose this mode",
                     "command": _launcher_command("hooks", "trust"),
                     "detail": "Checks each hook's source, event, exact command and hash against what Multithread "
                               "installed, shows the plan, and records trust through Codex's own config API after "
                               "approval. `hooks trust --revoke` reverses it."}]})
    result["state"] = "ready" if not result["next_actions"] else "needs_attention"
    return result


# --- Coverage: did recent provider sessions here reach the ledger? ----------

def worktrees(root):
    """Every checkout of this repository, as Git lists them."""
    completed = subprocess.run(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
         "-C", str(root), "worktree", "list", "--porcelain"],
        env=_GIT_ENV, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=True)
    roots = [line[len("worktree "):] for line in completed.stdout.splitlines() if line.startswith("worktree ")]
    return roots or [str(root)]


def _inside(path, roots):
    return isinstance(path, str) and any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def _codex_sessions(home, roots, since):
    """Codex threads whose directory is here: id and time from its state index only."""
    candidates = sorted((int(match.group(1)), path) for path in home.glob("state_*.sqlite")
                        if (match := re.fullmatch(r"state_(\d+)\.sqlite", path.name)))
    if not candidates:
        return {"evidence": "not_found", "sessions": []}
    database = sqlite3.connect(candidates[-1][1].as_uri() + "?mode=ro", uri=True, timeout=0.2)
    try:
        database.execute("PRAGMA query_only = ON")
        columns = {row[1] for row in database.execute("PRAGMA table_info(threads)")}
        if not {"id", "cwd", "updated_at", "source"} <= columns:
            return {"evidence": "unrecognized", "sessions": []}
        # Subagent threads (JSON sources) run inside a parent session's hooks.
        rows = database.execute(
            "SELECT id, cwd, updated_at FROM threads WHERE updated_at >= ? AND source NOT LIKE '{%' "
            "ORDER BY updated_at DESC LIMIT 2000", (int(since),)).fetchall()
    finally:
        database.close()
    sessions = [{"session": row[0], "last_activity": int(row[2])} for row in rows
                if isinstance(row[0], str) and _inside(row[1], roots)]
    return {"evidence": "codex_thread_index", "sessions": sessions[:_MAX_SESSIONS]}


def _claude_sessions(home, roots, since):
    """Claude transcripts for these directories: names and times only, never contents."""
    projects = home / "projects"
    if not projects.is_dir():
        return {"evidence": "not_found", "sessions": []}
    sessions = []
    for root in roots:
        directory = projects / re.sub(r"[^A-Za-z0-9]", "-", root)
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            stem = name[:-len(".jsonl")] if name.endswith(".jsonl") else None
            try:
                if stem is None or str(uuid.UUID(stem)) != stem:
                    continue
                info = os.lstat(directory / name)
            except (ValueError, OSError):
                continue
            if stat.S_ISREG(info.st_mode) and info.st_mtime >= since:
                sessions.append({"session": stem, "last_activity": int(info.st_mtime)})
    sessions.sort(key=lambda item: item["last_activity"], reverse=True)
    return {"evidence": "claude_transcript_names", "sessions": sessions[:_MAX_SESSIONS]}


def coverage_evidence(root, environ=None, *, now=None):
    """Recent provider sessions in this repository and the hooks that should reach it.

    Run outside the confined ledger worker; the worker compares these session
    identities with the ledger. Any failure is unavailable evidence, not absence.
    """
    now = time.time() if now is None else now
    since = now - WINDOW_HOURS * 3600
    result = {"state": "observed", "window_hours": WINDOW_HOURS, "repo": str(root),
              "launcher": str(account_launcher()), "providers": {}}
    try:
        roots = worktrees(root)
    except (OSError, subprocess.SubprocessError):
        roots = [str(root)]
    for client, reader in (("codex", _codex_sessions), ("claude", _claude_sessions)):
        entry = {}
        try:
            entry.update(reader(provider_home(client, environ), roots, since))
        except (OSError, sqlite3.Error, subprocess.SubprocessError, ValueError):
            entry.update(evidence="unavailable", sessions=[])
        try:
            installed = inspect(client, environ)["state"]
            entry["hooks"] = installed
            if client == "codex" and installed == "installed":
                entry["trust"] = trust_estimate(environ)
        except (HooksError, OSError):
            entry["hooks"] = "unreadable"
        result["providers"][client] = entry
    return result


def coverage_report(evidence, observed):
    """Name each provider whose recent sessions here never reached the ledger, with its fix."""
    launcher = evidence["launcher"]
    report = {"state": "no_recent_sessions", "window_hours": evidence["window_hours"], "providers": {}, "messages": []}
    unavailable = False
    for client, entry in evidence["providers"].items():
        sessions = [item["session"] for item in entry.get("sessions", ())]
        silent = [session for session in sessions if session not in observed.get(client, set())]
        summary = {"evidence": entry.get("evidence"), "hooks": entry.get("hooks"),
                   "recent_sessions": len(sessions), "silent_sessions": len(silent), "silent": silent[:5]}
        if "trust" in entry:
            summary["trust_estimate"] = entry["trust"]
        unavailable = unavailable or entry.get("evidence") in ("unavailable", "unrecognized")
        if silent:
            noun = ("a Codex conversation" if client == "codex" else "a Claude Code session") if len(silent) == 1 else (
                f"{len(silent)} Codex conversations" if client == "codex" else f"{len(silent)} Claude Code sessions")
            install = shlex.join([launcher, "hooks", "install", "--client", client])
            if entry.get("hooks") in ("absent", "provider_not_found"):
                cause, fix = f"Multithread's user-level {_NAMES[client]} hooks aren't installed", install
            elif entry.get("hooks") != "installed":
                cause, fix = f"Multithread's user-level {_NAMES[client]} hooks are {_condition(str(entry.get('hooks')))}", install
            elif client == "codex" and entry.get("trust") != "trusted":
                cause = "Codex hasn't trusted Multithread's user-level hooks, so it skips them"
                fix = (shlex.join([launcher, "hooks", "trust"]) + " (agent-assisted, with your go) or /hooks in a "
                       "Codex terminal")
            else:
                one = len(silent) == 1
                cause = ("the hooks are installed" + (" and trusted" if client == "codex" else "") + ", so "
                         + ("it" if one else "they") + " likely started before that, or "
                         + ("its" if one else "their") + " hook could not reach the ledger (look for a "
                         "MULTITHREAD WARNING in " + ("it" if one else "them") + ")")
                fix = ("start a new " + ("conversation" if client == "codex" else "session") + "; if the gap remains, run "
                       + shlex.join([launcher, "setup", "--repo", evidence["repo"], "--check"]))
            summary.update(cause=cause, fix=fix)
            report["messages"].append(f"{noun[0].upper() + noun[1:]} worked here in the last "
                                      f"{evidence['window_hours']} hours with no ledger events: {cause}. Fix: {fix}.")
            report["state"] = "gap"
        elif sessions and report["state"] != "gap":
            report["state"] = "covered"
        report["providers"][client] = summary
    if unavailable and report["state"] != "gap":
        report["state"] = "partly_unavailable" if report["state"] == "covered" else "unavailable"
    return report


# --- Command line ------------------------------------------------------------

def _display_plan(result):
    for plan in result["plans"]:
        name = _NAMES[plan["client"]]
        if plan["state"] == "provider_not_found":
            print(f"{name}: no settings directory at {Path(plan['file']).parent}; skipped.")
            continue
        if not plan["changes"]:
            print(f"{name}: {plan['file']} already has exactly Multithread's hooks." if result["action"] == "install"
                  else f"{name}: {plan['file']} has no Multithread hooks.")
            continue
        print(f"{name}: {plan['file']}")
        for change in plan["changes"]:
            print(f"  {change['change'].replace('_', ' ')} {change['event']} at position {change['slot'][0]}")
        if result["action"] == "install":
            print("  Hook command (3-second timeout): " + plan["command"])
        for moved in plan["moved_hooks"]:
            print(f"  Another hook for {moved['event']} moves from {moved['from']} to {moved['to']}"
                  + ("; Codex will ask you to review it again." if plan["client"] == "codex" else "."))
        if plan["reformatted"]:
            print("  The file's layout is normalized; everything else in it stays the same.")
        if plan.get("backup"):
            print("  The previous file is kept at " + plan["backup"])
        if plan["client"] == "codex" and result["action"] == "install":
            print("  Codex skips new hooks until they are trusted; `multithread hooks trust` or /hooks does that next.")


def _display_trust(result):
    print(("Revoke" if result["action"] == "revoke" else "Trust") + " Multithread's Codex hooks")
    print("Hook file: " + result["hook_file"])
    print("Command: " + result["command"])
    if result.get("user_config"):
        print("Codex user config: " + str(result["user_config"]))
    for item in result.get("hooks", ()):
        print(f"  {item.get('event', 'record')}: {item['key']} {item['hash']}"
              + (f" ({item['status']})" if "status" in item else "")
              + (f" -> {item['status_after']}" if "status_after" in item else ""))
    others = result.get("other_hooks") or {}
    if others.get("needing_review"):
        print(f"{others['needing_review']} other hooks await your own review in /hooks; Multithread leaves them to you.")


def hooks_main(argv=None):
    parser = argparse.ArgumentParser(prog="multithread hooks", description=(
        "Install, trust and check Multithread's user-level provider hooks, so every Codex and Claude session "
        "in an enrolled repository reaches its ledger however it was started. Changes show their exact plan "
        "and need approval; other hooks are never edited."))
    commands = parser.add_subparsers(dest="action", required=True)
    for name, text in (("status", "read-only installation, trust and next steps"),
                       ("install", "append Multithread's hooks to each provider's user hook file"),
                       ("remove", "take Multithread's hooks out of each provider's user hook file"),
                       ("trust", "record Codex trust for Multithread's installed hooks (agent-assisted mode)")):
        command_parser = commands.add_parser(name, help=text)
        if name in ("status", "install", "remove"):
            command_parser.add_argument("--client", choices=("codex", "claude", "all"), default="all")
        if name in ("status", "trust"):
            command_parser.add_argument("--codex", type=Path, help="reviewed absolute Codex executable; default: PATH")
        if name == "trust":
            command_parser.add_argument("--revoke", action="store_true",
                                        help="remove the trust records holding Multithread's hook hashes")
        if name != "status":
            command_parser.add_argument("--yes", action="store_true", help="apply the reviewed plan without a prompt")
            command_parser.add_argument("--expected-plan", help="plan_sha256 from the reviewed --json plan")
        command_parser.add_argument("--json", action="store_true", help="structured output; plans write nothing")
    args = parser.parse_args(argv)
    try:
        if args.action == "status":
            clients = ("codex", "claude") if args.client == "all" else (args.client,)
            result = status(clients, codex=args.codex)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                for client, entry in result["providers"].items():
                    line = f"{_NAMES[client]}: {entry['state'].replace('_', ' ')}"
                    if entry.get("file"):
                        line += f" ({entry['file']})"
                    if entry.get("trust"):
                        line += f"; trust: {entry['trust']['state'].replace('_', ' ')} ({entry['trust']['source'].replace('_', ' ')})"
                    print(line)
                    if entry.get("command") and entry["state"] != "provider_not_found":
                        print("  Hook command: " + entry["command"])
                    if entry.get("other_hooks"):
                        print("  Other hooks in this file, left as they are: " + ", ".join(
                            f"{event} {count}" for event, count in sorted(entry["other_hooks"].items())))
                    if entry.get("message"):
                        print("  " + entry["message"])
                for action in result["next_actions"]:
                    print("Next: " + action["action"])
                    if "command" in action:
                        print("  " + shlex.join(action["command"]))
                    for option in action.get("options", ()):
                        print(f"  {option['mode'].replace('_', '-')} ({option['actor']}):")
                        for step in option.get("steps", ()):
                            print("    - " + step)
                        if "command" in option:
                            print("    " + shlex.join(option["command"]) + ": " + option["detail"])
            return 0 if result["state"] == "ready" else 1
        if args.yes and not args.expected_plan:
            raise HooksError("--yes applies only a reviewed plan: first run the same command with --json, then pass "
                             "its plan_sha256 as --expected-plan.")
        if args.action == "trust":
            run = lambda expected: trust(args.codex, revoke=args.revoke, expected=expected)
            display = _display_trust
        else:
            clients = ("codex", "claude") if args.client == "all" else (args.client,)
            run = lambda expected: change_files(args.action, clients, expected=expected)
            display = _display_plan
        result = run(args.expected_plan if args.yes else None)
        if not args.yes and not args.json and result["state"] == "planned":
            display(result)
            word = "revoke" if getattr(args, "revoke", False) else args.action
            if not sys.stdin.isatty():
                raise HooksError("Nothing was written. Approve in an interactive terminal, or review the --json plan "
                                 "and pass its plan_sha256 with --yes --expected-plan.")
            if input(f"Type {word} to apply exactly this: ").strip() != word:
                print("Nothing was written.")
                return 0
            result = run(result["plan_sha256"])
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            if result["state"] != "planned":
                display(result)
            print(result.get("message") or {
                "applied": "Done. Check with: " + shlex.join(_launcher_command("hooks", "status")),
                "unchanged": "Nothing needed changing.",
                "trusted": "Codex now trusts exactly these hooks. Undo with: " + str(result.get("revoke_command")),
                "revoked": "Codex no longer trusts these hooks.",
                "already_trusted": "Codex already trusts exactly these hooks.",
                "nothing_to_revoke": "No trust records hold Multithread's hook hashes.",
            }.get(result["state"], "State: " + result["state"]))
        return 0 if result["state"] in ("planned", "applied", "unchanged", "trusted", "revoked",
                                        "already_trusted", "nothing_to_revoke") else 1
    except (HooksError, ProtocolError, OSError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, HooksError) else (
            "Codex could not answer (" + (str(exc)[:300] or exc.__class__.__name__) + "); nothing was changed. "
            "Check that Codex starts in a terminal, then run this again." if isinstance(exc, (ProtocolError, subprocess.SubprocessError))
            else "A settings file or Codex could not be reached (" + exc.__class__.__name__ + "); check the named "
            "paths, then run this again.")
        if args.json:
            print(json.dumps({"schema": 1, "state": "refused", "message": message}, ensure_ascii=False))
        else:
            print("multithread hooks: " + message, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("multithread hooks: interrupted; run the same command with --json to see the current state.", file=sys.stderr)
        return 130
