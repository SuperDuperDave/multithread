"""Compose installed readiness checks without state repair.

The only provider execution is Codex's hook listing, the same question a Codex
peer call asks before submitting any task.
"""

import argparse
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile

from . import codex_peer
from . import provider
from . import account_launcher
from .native_io import ProtocolError


_MAX_OUTPUT = 128 * 1024
_TIMEOUT = 30


def _output(stream):
    size = os.fstat(stream.fileno()).st_size
    stream.seek(0)
    body = stream.read(_MAX_OUTPUT)
    try:
        return body.decode("utf-8"), size > _MAX_OUTPUT, False
    except UnicodeDecodeError:
        # Keep readable diagnostics without treating replacement text as evidence.
        return body.decode("utf-8", errors="replace"), size > _MAX_OUTPUT, True


def _observe(command, *, mutating=False):
    """Keep bounded local diagnostics; a lost mutation receipt is not a retry."""
    result = {"command": command, "state": "unavailable", "exit_code": None,
              "stdout": "", "stderr": "", "stdout_truncated": False,
              "stderr_truncated": False, "stdout_decoding_loss": False,
              "stderr_decoding_loss": False}
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                completed = subprocess.run(command, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=errors,
                                           timeout=_TIMEOUT, check=False)
                result["exit_code"] = completed.returncode
            except subprocess.TimeoutExpired:
                result["state"] = "uncertain" if mutating else "unavailable"
                result["message"] = "Command timed out; inspect current state before retrying."
            except OSError:
                result["message"] = "The installed command could not be started."
            result["stdout"], result["stdout_truncated"], result["stdout_decoding_loss"] = _output(output)
            result["stderr"], result["stderr_truncated"], result["stderr_decoding_loss"] = _output(errors)
    except OSError:
        result["state"] = "uncertain" if mutating and (result["exit_code"] is not None or result["state"] == "uncertain") else "unavailable"
        result["message"] = "Command observation could not be retained; inspect current state before retrying."
        return result
    code = result["exit_code"]
    if code is None:
        return result
    if code != 0:
        result["state"] = ("uncertain" if mutating else "unavailable") if code < 0 or code == 130 else "failed"
        if mutating:
            result["message"] = "Enrollment has no verified success receipt; inspect current state before retrying."
        return result
    if result["stdout_truncated"]:
        result["state"] = "uncertain" if mutating else "unavailable"
        result["message"] = "The command result exceeded the observation limit."
        return result
    if result["stdout_decoding_loss"]:
        result["state"] = "uncertain" if mutating else "unavailable"
        result["message"] = "The command result contained invalid UTF-8; replacement text is diagnostic only."
        return result
    try:
        data = json.loads(result["stdout"])
        if not isinstance(data, dict):
            raise ValueError()
    except (ValueError, RecursionError):
        result["state"] = "uncertain" if mutating else "unavailable"
        result["message"] = "The command did not return a readable object receipt."
        return result
    result.update(state="observed", data=data)
    return result


def _runtime_healthy(observation, launcher):
    data = observation.get("data", {})
    activation = data.get("activation")
    return (observation["state"] == "observed" and data.get("installed") is True
            and data.get("preferred_command_available") is not False
            and data.get("launcher") == launcher and isinstance(activation, dict)
            and isinstance(activation.get("release_id"), str)
            and re.fullmatch(r"[0-9a-f]{64}", activation["release_id"]) is not None
            and isinstance(activation.get("activation_id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", activation["activation_id"]) is not None)


def _verified(observation, healthy, message, *, invalid_state="not_ready"):
    if observation["state"] == "observed":
        observation["state"] = "verified" if healthy else invalid_state
        if not healthy:
            observation["message"] = message
    return observation


def _next(report, stage, action, command=None):
    entry = {"stage": stage, "action": action}
    if command is not None:
        entry["command"] = command
    report["next_actions"].append(entry)


def _codex_hooks(report, plan):
    """Ready only when a Codex peer call's hook gate would pass for this checkout."""
    expected = plan["relay_plan"]["hook_command"]
    try:
        listing = codex_peer.list_hooks(plan["argv"], plan["repo"],
                                        on_start=lambda: report.update(provider_started=True))
        readiness = codex_peer.hook_readiness(listing, plan["repo"], expected)
    except (ProtocolError, OSError, subprocess.SubprocessError):
        return {"state": "unavailable", "hook_trust": {"state": "unavailable"},
                "message": "Codex's hook listing is unavailable; a Codex peer call checks the same listing before any task."}
    if readiness["state"] == "ready":
        return {"hook_trust": readiness}
    problem, action = codex_peer.hook_remedy(readiness, plan["repo"], expected)
    state = "needs_hook_review" if readiness["state"] == "needs_review" else "needs_hook_configuration"
    return {"state": state, "message": problem + "; Codex peer calls refuse until then",
            "hook_trust": {**readiness, "action": action}}


def setup_report(repo, *, apply=False, codex=None, claude=None):
    """Inspect the account installation; only explicit apply may call init."""
    result = {"schema": 1, "state": "not_ready", "mode": "apply" if apply else "check",
              "launcher": None, "repo": None,
              "runtime": {"state": "not_checked"},
              "repository": {"state": "not_checked", "enrollment": {"state": "not_requested"}},
              "providers": {client: {"state": "not_checked"} for client in ("codex", "claude")},
              "provider_started": False, "changes_provider_settings": False,
              "changes_permissions": False, "hook_delivery": "unknown",
              "provider_tools": "unknown", "provider_authentication": "unknown",
              "path_note": "Use the exact launcher path below; Multithread does not edit PATH.",
              "next_actions": []}
    try:
        checkout = Path(repo)
        if not checkout.is_absolute():
            checkout = Path.cwd() / checkout
        # Do not resolve aliases before the installed enrollment boundary sees them.
        selected = str(checkout)
        launcher = str(account_launcher())
    except (OSError, KeyError):
        result["runtime"] = {"state": "unavailable", "message": "Account or checkout identity is unavailable."}
        _next(result, "runtime", "Restore access to the current checkout and normal OS account, then check again.")
        return result
    result.update(launcher=launcher, repo=selected)
    base = [launcher, "--repo", selected, "--json"]
    check = [launcher, "setup", "--repo", selected, "--check"]
    runtime = _observe([launcher, "runtime", "status"])
    result["runtime"] = _verified(runtime, _runtime_healthy(runtime, launcher),
                                  "The installed runtime identity is not verified.")
    if runtime["state"] != "verified":
        _next(result, "runtime", "Inspect the installation; use the separately reviewed release installer if its launcher is unavailable.",
              [launcher, "runtime", "inspect"])
        return result

    repository = result["repository"]
    if apply:
        enrollment = _observe(base + ["init"], mutating=True)
        data = enrollment.get("data", {})
        repository["enrollment"] = _verified(
            enrollment, data.get("ok") is True and data.get("initialized") is True,
            "Enrollment did not return a verified initialization receipt; inspect current state before retrying.",
            invalid_state="uncertain")
    doctor = _observe(base + ["doctor"])
    diagnostics = doctor.get("data", {})
    doctor_ok = (diagnostics.get("ok") is True and diagnostics.get("integrity") == "ok"
                 and all(isinstance(diagnostics.get(key), str) and Path(diagnostics[key]).is_absolute()
                         for key in ("repo_root", "git_common_dir", "database")))
    repository["doctor"] = _verified(doctor, doctor_ok, "Repository integrity and identity are not verified.")
    if doctor["state"] == "verified":
        status = _observe(base + ["status"])
        state = status.get("data", {})
        status_ok = (state.get("database") == diagnostics["database"]
                     and type(state.get("last_seq")) is int and state["last_seq"] >= 0
                     and isinstance(state.get("active_claims"), list)
                     and isinstance(state.get("recent_signals"), list))
        repository["status"] = _verified(status, status_ok, "Repository status is not a matching ledger observation.")
        repository["identity"] = {key: diagnostics[key] for key in ("repo_root", "git_common_dir", "database")}
    else:
        repository["status"] = {"state": "not_checked"}
    failures = [entry for entry in (repository["enrollment"], doctor, repository["status"])
                if entry["state"] not in {"verified", "not_requested", "not_checked"}]
    repository["state"] = failures[0]["state"] if failures else "verified"
    if repository["state"] != "verified":
        _next(result, "repository", "Inspect the reported failure or unavailable observation; preserve existing state.",
              base + ["doctor"])
        if apply:
            _next(result, "repository", "Check the current state before deciding whether enrollment needs another attempt.", check)
        else:
            _next(result, "repository", "If this is the intended, unenrolled repository, explicitly enroll it after resolving any reported state issue.",
                  [launcher, "setup", "--repo", selected, "--apply"])
        return result

    result["state"] = "ready"
    for client, explicit in (("codex", codex), ("claude", claude)):
        path = str(explicit) if explicit is not None else shutil.which(client)
        if path is None:
            result["providers"][client] = {"state": "missing", "version": "not_checked"}
            _next(result, client, "Install or locate the provider through its normal interface, then select its reviewed executable with --" + client + ".")
            continue
        try:
            plan = provider.prepare(client, checkout, Path(launcher), Path(path))
        except (provider.LaunchError, OSError, KeyError) as exc:
            message = str(exc)[:2048] if isinstance(exc, provider.LaunchError) else "Provider launch preparation is unavailable."
            result["providers"][client] = {"state": "unavailable", "executable": path,
                                           "version": "not_checked", "message": message}
            _next(result, client, "Inspect launch preparation; provider sign-in and trust use the provider's normal interface.",
                  [launcher, "launch", client, "--repo", selected, "--provider", path, "--json"])
            continue
        command = [launcher, "launch", client, "--repo", selected, "--provider", plan["argv"][0]]
        entry = {"state": "prepared", "executable": plan["argv"][0],
                 "version": "not_checked", "plan": plan, "launch_command": command}
        result["providers"][client] = entry
        if client == "codex":
            entry.update(_codex_hooks(result, plan))
        if entry["state"] in ("needs_hook_review", "needs_hook_configuration"):
            _next(result, client, entry["hook_trust"]["action"], command)
        else:
            _next(result, client, "When a provider launch is authorized, run this in an interactive terminal and review its displayed invocation.", command)
    result["first_collaboration_url"] = "https://github.com/SuperDuperDave/multithread/blob/main/docs/PEER.md#first-collaboration"
    return result


def _display(report):
    text = provider._display_text
    codex = report["providers"]["codex"]["state"]
    print("Multithread setup needs attention." if report["state"] != "ready" else
          "Multithread is ready for this repository; Codex peer calls need one hook review first."
          if codex == "needs_hook_review" else
          "Multithread is ready for this repository; Codex hooks need attention before peer calls."
          if codex == "needs_hook_configuration" else "Multithread is ready for this repository.")
    print("Runtime: " + text(report["runtime"]["state"]))
    print("Repository: " + text(report["repository"]["state"]))
    if report["runtime"]["state"] == "verified":
        activation = report["runtime"]["data"]["activation"]
        version = activation.get("version")
        print("Version: " + (text(version) if isinstance(version, str) and version else "not reported"))
        print("Release: " + text(activation["release_id"]))
    if report["repo"]:
        print("Checkout: " + text(report["repo"]))
    identity = report["repository"].get("identity")
    if identity:
        print("Git common directory: " + text(identity["git_common_dir"]))
    for client, entry in report["providers"].items():
        summary = text(client) + ": " + text(entry["state"])
        if isinstance(entry.get("message"), str) and entry["message"]:
            summary += "; " + text(entry["message"])
        print(summary)
    print("Provider sign-in, hook delivery and tool execution: not checked.")
    print(text(report["path_note"]))
    if report["launcher"]:
        print("Launcher: " + text(report["launcher"]))
    for entry in report["next_actions"]:
        print(text(entry["stage"]) + ": " + text(entry["action"]))
        if "command" in entry:
            provider._display_command("  Command", entry["command"])
    if report.get("first_collaboration_url"):
        print("First collaboration, when you authorize provider use: "
              + text(report["first_collaboration_url"]))
    observations = [report["runtime"], *(report["repository"].get(key, {}) for key in ("enrollment", "doctor", "status"))]
    for entry in observations:
        if entry.get("state") in {"verified", "not_checked", "not_requested", None}:
            continue
        if entry.get("message"):
            print(text(entry["message"]))
        if entry.get("stderr"):
            for line in entry["stderr"].splitlines():
                print("Diagnostic: " + text(line))
        if entry.get("stderr_truncated") or entry.get("stdout_truncated"):
            print("Diagnostic output was truncated; run the exact check above for details.")


def setup_main(argv=None):
    parser = argparse.ArgumentParser(prog="multithread setup", description="Check Multithread readiness; explicitly enroll with --apply. The only provider run is Codex's hook listing: no thread, task or trust change.")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="chosen Git checkout; default: current directory")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only readiness check (default)")
    mode.add_argument("--apply", action="store_true", help="explicitly initialize this repository, then check readiness")
    parser.add_argument("--codex", type=Path, help="reviewed absolute Codex executable; default: PATH lookup")
    parser.add_argument("--claude", type=Path, help="reviewed absolute Claude executable; default: PATH lookup")
    parser.add_argument("--json", action="store_true", help="return structured readiness and exact next actions")
    args = parser.parse_args(argv)
    try:
        result = setup_report(args.repo, apply=args.apply, codex=args.codex, claude=args.claude)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            _display(result)
        return 0 if result["state"] == "ready" else 1
    except KeyboardInterrupt:
        print("multithread setup: interrupted; inspect current state before retrying enrollment.", file=sys.stderr)
        return 130
