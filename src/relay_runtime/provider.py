#!/usr/bin/env python3
"""Native provider integration outside the confined ledger worker.

Launch preserves interactive provider behavior. Peer makes one bounded native
call and returns its result; it never infers ledger acknowledgement or
workflow completion. Provider configuration comes from the installed worker.
"""

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from . import account_launcher
from .native_io import (USAGE_SCOPES, MODEL_USAGE_SCOPES, COST_SCOPES,
                        claude_measurements, measurement_scope, canonical_provider_version)


class LaunchError(Exception):
    pass


def _hook_events(client):
    return ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"] + (
        ["Interrupt"] if client == "codex" else [])


def check_native_arguments(client, command, arguments):
    """Accept only the invocation-only hook shape emitted by public schema 1."""
    events = _hook_events(client)
    expected = {name: [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}]
                for name in events}
    try:
        if client == "claude":
            if len(arguments) != 2 or arguments[0] != "--settings":
                raise ValueError()
            parsed = json.loads(arguments[1])
        else:
            if len(arguments) != 2 * len(events):
                raise ValueError()
            hooks = {}
            for index in range(0, len(arguments), 2):
                if arguments[index] != "-c":
                    raise ValueError()
                entry = tomllib.loads(arguments[index + 1])
                value = entry.get("hooks")
                if set(entry) != {"hooks"} or not isinstance(value, dict) or set(hooks) & set(value):
                    raise ValueError()
                hooks.update(value)
            parsed = {"hooks": hooks}
        if parsed != {"hooks": expected}:
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        raise LaunchError("Native arguments do not match the invocation-only hook plan; no provider was started.") from None


def executable(value, name):
    selected = str(value) if value is not None else shutil.which(name)
    if not selected:
        raise LaunchError(f"{name} was not found; select its reviewed absolute path with --provider.")
    path = Path(selected)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise LaunchError(f"{name} must be an executable at an absolute path.")
    # Preserve the entry path: resolving a provider's symlink can change how its
    # ordinary launcher finds companion executables or selects configuration.
    return str(path)


def prepare(client, repo, relay, provider):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise LaunchError("This Multithread release requires a supported x86-64 Linux environment; see docs/SUPPORT.md.")
    # Multithread must see the supplied components before any normalization: resolving
    # an alias here would erase a symlink its enrollment boundary should refuse.
    checkout = Path(repo)
    if not checkout.is_absolute():
        checkout = Path.cwd() / checkout
    if not checkout.is_dir():
        raise LaunchError("--repo must identify the enrolled checkout directory.")
    if relay is None:
        relay = account_launcher()
    launcher = executable(relay, "Multithread")
    provider_path = executable(provider, client)
    command = [launcher, "--repo", str(checkout), "--json", "provider-config", "--client", client]
    if Path(launcher).name == "multithread":
        command.extend(["--launcher-name", "multithread"])
    try:
        result = subprocess.run(command, cwd=checkout, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        raise LaunchError("Multithread configuration timed out; observation is unavailable. Inspect installed status before retrying.") from None
    except UnicodeError:
        raise LaunchError("Multithread returned unreadable configuration output; observation is unavailable.") from None
    if result.returncode != 0:
        # Give the exact native command for diagnosis without copying arbitrary
        # diagnostics into the structured launch plan.
        raise LaunchError(
            f"Multithread refused launch preparation (exit {result.returncode}); run: {shlex.join(command)}")
    try:
        plan = json.loads(result.stdout)
    except (ValueError, RecursionError):
        raise LaunchError("Multithread did not return a valid configuration plan; no provider was started.") from None
    if not isinstance(plan, dict) or type(plan.get("schema")) is not int or plan["schema"] != 1:
        raise LaunchError("Unsupported configuration plan; no provider was started.")
    if plan.get("provider") != client or plan.get("repo") != str(checkout):
        raise LaunchError("The configuration plan does not match this provider and checkout.")
    if any(plan.get(key) is not False for key in
           ("launches_provider", "changes_provider_settings", "changes_permissions")):
        raise LaunchError("The configuration plan exceeds invocation-only setup.")
    hook_command = plan.get("hook_command")
    if not isinstance(hook_command, str):
        raise LaunchError("The configuration plan has an invalid hook command.")
    try:
        hook = shlex.split(hook_command)
    except ValueError:
        raise LaunchError("The configuration plan has an invalid hook command.") from None
    if hook != [launcher, "--repo", str(checkout), "provider-hook", "--client", client]:
        raise LaunchError("The hook command does not match the selected Multithread and checkout.")
    arguments = plan.get("native_arguments")
    if (not isinstance(arguments, list) or not arguments
            or any(not isinstance(arg, str) or "\0" in arg for arg in arguments)):
        raise LaunchError("The configuration plan has invalid native arguments.")
    check_native_arguments(client, hook_command, arguments)
    return {"schema": 1, "state": "launch_prepared", "provider": client,
            "repo": str(checkout), "argv": [provider_path, *arguments],
            "relay_plan": plan, "provider_started": False,
            "hook_delivery": "unknown", "provider_tools": "unknown"}


def _display_text(value):
    """Render local diagnostic data without terminal control characters."""
    return "".join(character if character.isprintable() else
                   json.dumps(character, ensure_ascii=True)[1:-1]
                   for character in str(value))


def _display_command(label, argv):
    if all(argument.isprintable() for argument in argv):
        print(label + ": " + shlex.join(argv))
    else:
        # Escaped shell text could silently name another file. Keep unusual
        # arguments exact and explicitly represented as data instead.
        print(label + " (JSON argv): " + json.dumps(argv, ensure_ascii=True))


def _display_launch(plan):
    print("Multithread launch review")
    print("Provider: " + _display_text(plan["provider"]))
    print("Executable: " + _display_text(plan["argv"][0]))
    print("Checkout: " + _display_text(plan["repo"]))
    # prepare validated these exact hooks in the native arguments; display
    # their enforced shape, not optional descriptive fields in the plan.
    print("Invocation hooks: " + ", ".join(_hook_events(plan["provider"])))
    print("Each hook runs the following command with a 3-second timeout:")
    hook_command = plan["relay_plan"]["hook_command"]
    # Native trust may identify literal hook text. Do not normalize accepted
    # whitespace or quoting when presenting the command to be reviewed.
    if hook_command.isprintable():
        print("Hook command: " + hook_command)
    else:
        print("Hook command (JSON string): " + json.dumps(hook_command, ensure_ascii=True))
    print("Hook delivery and provider tools: unknown until observed in the native session.")
    print("Existing provider settings and permissions remain in effect. Native hook trust is a separate step.")
    print("Use launch --json with the same options to inspect the complete invocation plan without starting a provider.")


def launch_main(argv=None):
    parser = argparse.ArgumentParser(prog="multithread launch", description="Review invocation-only Multithread hooks and start an interactive provider.")
    parser.add_argument("client", choices=("codex", "claude"))
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="enrolled checkout; default: current directory")
    parser.add_argument("--multithread", "--relay", dest="relay", type=Path, help="reviewed absolute installed launcher; --relay is a compatibility spelling")
    parser.add_argument("--provider", type=Path, help="reviewed absolute provider entry point; default: PATH lookup")
    parser.add_argument("--json", action="store_true", help="print a plan without starting a provider or asking for input")
    args = parser.parse_args(argv)
    try:
        plan = prepare(args.client, args.repo, args.relay, args.provider)
        if args.json:
            print(json.dumps(plan, sort_keys=True))
            return 0
        _display_launch(plan)
        if not sys.stdin.isatty():
            raise LaunchError("Use --json to prepare a plan here, or run this command in an interactive terminal to launch.")
        if input("Type launch to start this provider: ").strip() != "launch":
            print("Provider was not started.")
            return 0
        # Normal inherited environment and terminal: no provider sandbox wrapper,
        # credentials/config relocation, shell eval, or permission-policy flags.
        return subprocess.call(plan["argv"], cwd=plan["repo"])
    except (LaunchError, OSError, KeyError) as exc:
        message = str(exc) if isinstance(exc, LaunchError) else "A selected path or command is unavailable; check your arguments."
        if args.json:
            print(json.dumps({"schema": 1, "state": "unavailable", "provider_started": False,
                              "hook_delivery": "unknown", "provider_tools": "unknown", "message": message}))
        else:
            print("multithread launch: " + _display_text(message), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("multithread launch: interrupted; inspect the provider if it had already started.", file=sys.stderr)
        return 130


_MAX_TASK = 64 * 1024
_MAX_RESULT = 16 * 1024 * 1024


def _session(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise argparse.ArgumentTypeError("use the exact lowercase session UUID returned by the previous call") from None
    return value


def _native_identity(value):
    if not isinstance(value, str) or not 0 < len(value) <= 256 or any(
            ord(character) < 32 or ord(character) == 127 for character in value):
        raise argparse.ArgumentTypeError("use the exact native identity returned by the previous call")
    return value


def _model_selection(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 128 or value.startswith("-") or any(
            ord(character) < 33 or ord(character) > 126 for character in value)):
        raise argparse.ArgumentTypeError("use a nonempty printable model name without spaces (at most 128 characters)")
    return value


def _positive(value):
    number = int(value)
    if not 1 <= number <= 3600:
        raise argparse.ArgumentTypeError("use an integer from 1 through 3600")
    return number


def _task(path):
    if path == "-":
        body = sys.stdin.buffer.read(_MAX_TASK + 1)
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise LaunchError("The task must be a regular UTF-8 file or stdin (-).")
            body = stream.read(_MAX_TASK + 1)
    if len(body) > _MAX_TASK:
        raise LaunchError("The task exceeds 64 KiB; reference larger artifacts from a scoped task instead.")
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        raise LaunchError("The task must be UTF-8 text.") from None
    if not text.strip() or "\0" in text:
        raise LaunchError("The task must contain nonempty text without NUL bytes.")
    return body


def _private_file(directory, name):
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "wb")


def _record(directory, name, value):
    with _private_file(directory, name) as stream:
        stream.write(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(directory):
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _atomic_record(directory, name, value, *, sync_directory=False):
    """Publish a complete private JSON file; a torn write leaves the old file."""
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / name)
        if sync_directory:
            _sync_directory(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _call_checkpoint(envelope, phase):
    # A checkpoint is never a terminal receipt. A killed caller may have
    # crossed the spawn boundary before it could update this observation.
    return {"schema": 1, "kind": "peer_checkpoint", "phase": phase,
            "provider": envelope["provider"],
            "requested_session_id": envelope["requested_session_id"],
            "provider_start_observation": "confirmed" if phase == "spawned" else "unknown",
            "outcome": "unknown"}


def _drain(observer):
    from .peer_control import ControlError
    try:
        return observer.drain()
    except (ControlError, OSError):
        # An unavailable receipt must not prevent termination of our process.
        # Freeze interpretation; retain whatever raw output can still be read.
        observer.interpret = False
        observer.envelope.update(needs_attention=True,
                                 evidence_recording="Native cleanup or input receipt observation is unavailable; inspect retained evidence.")
        return False


_WAIT_FEEDBACK_SECONDS = 30


class _WaitingFeedback:
    """Sparse local observations, never a provider heartbeat or task transcript."""

    def __init__(self, started, timeout, envelope, control):
        self.started, self.timeout = started, timeout
        self.envelope, self.control = envelope, control
        self.next_at = started + _WAIT_FEEDBACK_SECONDS
        self.disabled = False

    def __call__(self):
        now = time.monotonic()
        if self.disabled or now < self.next_at:
            return
        self.next_at = now + _WAIT_FEEDBACK_SECONDS
        if self.envelope.get("state") == "returned":
            stage = "native return observed; waiting for owned process exit"
        elif self.envelope.get("task_submission") == "not_submitted":
            stage = "waiting for native setup; task not submitted"
        elif self.envelope.get("task_submission") == "requested":
            stage = "task submission requested; acceptance not yet observed"
        elif self.envelope.get("task_submission") == "accepted":
            stage = "waiting for provider return"
        elif self.envelope.get("task_delivery") == "in_progress":
            stage = "writing task to provider stdin; consumption unknown"
        elif self.envelope.get("task_delivery") == "written":
            stage = "task written to provider stdin; waiting for result; consumption unknown"
        elif self.envelope.get("task_delivery") == "uncertain":
            stage = "task delivery incomplete; waiting for provider exit"
        else:
            stage = "waiting; submission stage not recorded"
        activity = self.envelope.get("native_progress")
        if (isinstance(activity, dict) and activity.get("last_event") in
                ("initialized", "assistant_message", "subagent_frame", "native_result")
                and type(activity.get("observed_at_seconds")) in (int, float)
                and activity["observed_at_seconds"] >= 0):
            progress = (f"last observed native event {activity['last_event']} at "
                        f"~{activity['observed_at_seconds']:.0f}s; "
                        f"{activity.get('assistant_messages', 0)} assistant messages, "
                        f"{activity.get('tool_requests', 0)} tool-use blocks, "
                        f"{activity.get('subagent_frames', 0)} subagent frames; "
                        "later progress unknown")
        else:
            progress = ("provider progress unknown (final JSON has no intermediate events; "
                        "use --stream-progress on a new Claude call)"
                        if self.envelope.get("native_output_mode") == "final_json" else
                        "provider progress unknown")
        if self.control is None:
            channel = "input not enabled for this call"
        elif "control_fault" in self.envelope:
            channel = "input observation unavailable"
        elif self.control.closed or not self.control.accepting:
            channel = "input closed"
        elif self.control.target is None:
            channel = "input target not advertised"
        else:
            channel = "input target advertised; new input acceptance unknown"
        try:
            print(f"multithread peer: {stage}; {now - self.started:.0f}s elapsed / "
                  f"{self.timeout}s call limit; {channel}; {progress}.",
                  file=sys.stderr, flush=True)
        except (OSError, UnicodeError, ValueError):
            # A lost diagnostic sink must not interrupt the owned provider.
            self.disabled = True


def _call_final_json(process, task, timeout, feedback, envelope):
    """Deliver stdin once, then wait; output already goes to evidence files.

    Retrying communicate(input=None) can leave a partially written input pipe
    open on supported Python versions. Own the small nonblocking write here,
    as the streaming drivers do, without accessing Popen's private buffers.
    """
    deadline = time.monotonic() + timeout
    pending = memoryview(task)
    envelope["task_delivery"] = "in_progress"

    def remaining():
        allowance = deadline - time.monotonic()
        if allowance <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        return min(allowance, _WAIT_FEEDBACK_SECONDS)

    try:
        os.set_blocking(process.stdin.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            while pending:
                feedback()
                if not selector.select(remaining()):
                    continue
                try:
                    written = os.write(process.stdin.fileno(), pending)
                except BlockingIOError:
                    continue
                if written <= 0:
                    raise BrokenPipeError()
                pending = pending[written:]
    except BrokenPipeError:
        # Like communicate(), allow an early native refusal to be interpreted
        # from its output rather than replacing it with a pipe diagnostic.
        pass
    finally:
        envelope["native_input_unwritten_bytes"] = len(pending)
        envelope["task_delivery"] = "uncertain" if pending else "written"
        process.stdin.close()
    while True:
        feedback()
        try:
            return process.wait(timeout=remaining())
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                raise


def _wait(process, timeout, observer=None, feedback=None):
    if observer is None:
        return process.wait(timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        progressed = _drain(observer)
        if process.poll() is not None and (observer.eof or observer.truncated):
            return process.returncode
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        if feedback is not None:
            feedback()
        if not progressed:
            time.sleep(min(0.01, remaining))


def _stop(process, observer=None):
    # This call owns this process group only. Give the provider its normal
    # SIGTERM cleanup before escalation; never touch another native session.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        _wait(process, 5, observer)
    except subprocess.TimeoutExpired:
        pass
    # Descendants can outlive the leader; terminate only the group we created.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if observer is None:
        process.wait()
    else:
        try:
            _wait(process, 1, observer)
        except subprocess.TimeoutExpired:
            # A descriptor can outlive the process group that owns this call.
            # Reap the owned leader independently; incomplete stdout is an
            # observation limit, not grounds to suppress a validated answer.
            observer.envelope.update(needs_attention=True,
                                     stdout_completion="incomplete after owned cleanup")
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                observer.envelope["owned_process_cleanup"] = "termination requested; process exit remains unverified"
        finally:
            _drain(observer)


def _call_problem(envelope, message):
    # A validated native terminal observation survives a separate cleanup fault.
    if envelope["state"] not in ("returned", "provider_error"):
        envelope["state"] = "uncertain"
    envelope.update(needs_attention=True, message=message)


@contextmanager
def _call_signals():
    """Route ordinary caller termination through owned-process cleanup."""
    state = {"signal": None, "starting": False, "stopping": False}

    def interrupt(number, frame):
        if not state["stopping"]:
            state["signal"] = number
            if not state["starting"]:
                raise KeyboardInterrupt

    previous = {}
    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            handler = signal.getsignal(number)
            if handler != signal.SIG_IGN:
                previous[number] = signal.signal(number, interrupt)
        yield state
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _observe_stdout(directory, envelope):
    path = directory / "stdout.json"
    # Bound the read itself, not just a prior stat: a native descendant may
    # still hold the output descriptor. Bind the summary to the bytes observed.
    try:
        with path.open("rb") as stream:
            body = stream.read(_MAX_RESULT + 1)
    except OSError:
        envelope["stdout_observation_error"] = "unavailable; byte count and digest are unknown"
        raise
    envelope["stdout_observation"] = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                                      "truncated": len(body) > _MAX_RESULT, "scope": "bounded_read"}
    return body


def _interpret(directory, envelope):
    body = _observe_stdout(directory, envelope)
    if len(body) > _MAX_RESULT:
        envelope["message"] = "Provider output exceeded the summary bound; inspect retained output before continuing."
        return
    try:
        native = json.loads(body)
        # JSON can represent lone surrogates that cannot be delivered as UTF-8.
        # Preserve the raw result, but do not turn it into a success then fail
        # while recording or returning the interpreted evidence.
        json.dumps(native, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        envelope["message"] = "No readable final provider result; inspect retained output before continuing."
        return
    if (not isinstance(native, dict) or native.get("type") != "result"
            or type(native.get("is_error")) is not bool
            or not isinstance(native.get("subtype"), str)):
        envelope["message"] = "Unsupported final provider result; inspect retained output before continuing."
        return
    if native.get("session_id") != envelope["requested_session_id"]:
        try:
            envelope["observed_session_id"] = _session(native.get("session_id"))
        except argparse.ArgumentTypeError:
            pass
        envelope["message"] = "Returned session identity does not match the requested peer. The unverified answer and denials remain in stdout.json; inspect them without automatically resuming either identity."
        return
    text = native.get("result")
    denials = native.get("permission_denials", [])
    errors = native.get("errors", [])
    if ((not native["is_error"] and native["subtype"] == "success" and not isinstance(text, str))
            or (text is not None and not isinstance(text, str)) or not isinstance(denials, list)
            or any(not isinstance(item, dict) for item in denials)
            or not isinstance(errors, list) or any(not isinstance(item, str) for item in errors)):
        envelope["message"] = "Unsupported result or permission-denial shape; inspect retained output."
        return
    envelope.update({
        "state": "provider_error" if (native["is_error"] or native["subtype"] != "success"
                                      or envelope["process_exit_code"] != 0) else "returned",
        "session_id": native["session_id"], "result": text,
        "provider_subtype": native["subtype"], "provider_is_error": native["is_error"],
        "terminal_reason": native.get("terminal_reason"),
        "provider_errors": [item[:2000] for item in errors[:8]],
        "provider_errors_truncated": len(errors) > 8 or any(len(item) > 2000 for item in errors),
        # Tool inputs remain in private raw output, not the routine summary.
        "permission_denials": [{key: item.get(key) for key in ("tool_name", "tool_use_id")}
                               for item in denials],
        "actual_billed_cost": "unknown",
    })
    envelope.update(claude_measurements(native, envelope))
    measurement_scope(envelope, "usage_scope", "native_main_loop", USAGE_SCOPES)
    measurement_scope(envelope, "model_usage_scope", "native_query_cumulative", MODEL_USAGE_SCOPES)
    measurement_scope(envelope, "cost_scope", "cumulative_through_latest_native_result", COST_SCOPES)
    envelope["needs_attention"] = bool(denials) or envelope["state"] != "returned" or (
        native.get("terminal_reason") not in (None, "end_turn", "completed"))
    if envelope["state"] == "provider_error":
        causes = []
        if native["is_error"]:
            causes.append("The provider marked its result as an error.")
        elif native["subtype"] != "success":
            causes.append("The provider returned a non-success result.")
        if envelope["process_exit_code"] is None:
            causes.append("The provider process exit was not observed.")
        elif envelope["process_exit_code"] != 0:
            causes.append(f"The provider process exited with code {envelope['process_exit_code']}.")
        envelope["message"] = " ".join(causes) + " Inspect retained output and evidence before continuing."
    else:
        envelope["message"] = "Assess the answer and durable Multithread evidence; a returned turn is not workflow completion."
    if envelope.get("task_delivery") == "uncertain":
        envelope["needs_attention"] = True
        if envelope["state"] == "returned":
            # Matching session identity cannot attribute an answer to a task
            # we know was not fully delivered. Keep useful text as partial.
            envelope["state"] = "uncertain"
            envelope["partial_result"] = envelope["result"]
            envelope["result"] = None
        envelope["message"] += " The task was not fully written to provider stdin; any observed text may answer incomplete or other input. Inspect retained evidence before follow-up."


def peer_main(argv=None, *, report_entry=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "report":
        return report_main(raw[1:])
    if raw and raw[0] == "control":
        from .peer_control import control_main
        return control_main(raw[1:])
    if raw and raw[0] == "packet":
        from .review_packet import packet_main
        return packet_main(raw[1:])
    parser = argparse.ArgumentParser(prog="multithread peer", description="Call a native provider and return its observed result to the initiating task.",
                                     epilog="For an existing call: peer report --call-dir PATH [--json] gives a read-only summary; peer packet --help freezes a scoped review diff; peer control --help covers live input.")
    parser.add_argument("client", choices=("claude", "codex"))
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="enrolled peer checkout")
    parser.add_argument("--multithread", "--relay", dest="relay", type=Path, help="reviewed absolute installed launcher; --relay is a compatibility spelling")
    parser.add_argument("--provider", type=Path, help="reviewed absolute provider entry point; default: PATH")
    parser.add_argument("--task-file", required=True, help="UTF-8 task packet; - reads stdin, at most 64 KiB")
    parser.add_argument("--resume", type=_native_identity, help="exact peer session identity from a previous result; no latest-session lookup")
    parser.add_argument("--output-dir", type=Path, help="new private evidence directory; default: retained temporary directory")
    parser.add_argument("--timeout", type=_positive, default=600, help="call wall-time limit in seconds, 1 through 3600 (default: 600)")
    parser.add_argument("--max-turns", type=_positive, help="optional Claude agentic-turn cap, 1 through 3600; tool/source work can consume it before the final answer; no cap by default")
    parser.add_argument("--model", type=_model_selection, help="request this Claude model for this call; the provider decides what it actually uses")
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"),
                        help="request this Claude effort level for this call; effective effort is not verified")
    parser.add_argument("--live-input", action="store_true", help="enable Claude session input while this call runs; queued input may start later turns within the call timeout. Codex always exposes exact-turn input")
    parser.add_argument("--stream-progress", action="store_true", help="use Claude's native event stream for content-free progress observations, without enabling live input; the default remains final JSON")
    parser.add_argument("--dry-run", action="store_true", help="validate task/configuration and print a plan; no provider or evidence writes")
    parser.add_argument("--json", action="store_true", help="return a structured result; this DOES launch unless --dry-run is used; exit 0 means a returned turn, so also check needs_attention and task evidence")
    args = parser.parse_args(raw)
    args.report_entry = report_entry
    if args.client == "codex" and args.max_turns is not None:
        parser.error("--max-turns is a Claude option; Codex returns one native turn with its normal tool loop")
    if args.client == "codex" and (args.model is not None or args.effort is not None):
        parser.error("--model and --effort are Claude options in this peer release")
    if args.client == "codex" and args.stream_progress:
        parser.error("Codex already uses native streaming; --stream-progress is a Claude option")
    if args.client == "claude" and args.resume is not None:
        try:
            _session(args.resume)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    with _call_signals() as interruption:
        return _run_peer(args, interruption)


def _producer_runtime():
    """Observe this module's retained manifest identity, never current selection."""
    loader = getattr(globals().get("__spec__"), "loader", None)
    runtime = getattr(loader, "runtime", None)
    digest = getattr(runtime, "digest", None)
    if (isinstance(digest, str) and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)):
        return {"status": "recorded", "runtime_manifest_sha256": digest}
    return {"status": "unavailable", "runtime_manifest_sha256": None}


def _follow_up_preparation(args, plan, envelope):
    """Preserve an ended call's invocation as incomplete, dry-run-only argv."""
    if (plan is None or envelope.get("state") != "returned"
            or envelope.get("needs_attention") is not False
            or envelope.get("provider_started") is not True
            or envelope.get("process_exit_code") != 0
            or not envelope.get("session_id")
            or any(key in envelope for key in
                   ("observed_session_id", "server_cleanup", "control_fault", "evidence_recording"))):
        return None
    # prepare checked this exact hook against the selected launcher. Retain its
    # entry path, as well as the provider's entry, without resolving symlinks.
    launcher = shlex.split(plan["relay_plan"]["hook_command"])[0]
    entry = args.report_entry if args.report_entry is not None else [launcher, "peer"]
    prefix = [*entry, args.client, "--repo", plan["repo"], "--multithread", launcher,
              "--provider", plan["argv"][0], "--resume=" + envelope["session_id"],
              "--timeout", str(args.timeout)]
    if args.max_turns is not None:
        prefix.extend(["--max-turns", str(args.max_turns)])
    if args.model is not None:
        prefix.extend(["--model", args.model])
    if args.effort is not None:
        prefix.extend(["--effort", args.effort])
    if args.live_input:
        prefix.append("--live-input")
    if args.stream_progress:
        prefix.append("--stream-progress")
    # Never copy the old task/evidence destination. A bare final option requires
    # a new task path before parsing can reach stdin or launch preparation.
    prefix.extend(["--dry-run", "--json", "--task-file"])
    return {"argv_prefix": prefix}


def _run_peer(args, interruption):
    from .peer_control import CallControl, ControlError, ObservedControl
    session = args.resume or (str(uuid.uuid4()) if args.client == "claude" else None)
    envelope = {"schema": 1, "provider": args.client, "state": "unavailable",
                "requested_session_id": session, "session_id": None,
                "provider_started": False, "process_exit_code": None, "resumed": bool(args.resume),
                "evidence_directory": None, "result": None, "needs_attention": True,
                "hook_delivery": "unknown", "provider_tools": "unknown",
                "relay_acknowledgement": "not_checked", "workflow_completion": "not_checked",
                "authentication": "inherited from provider; not verified",
                "elapsed_seconds": None, "usage": None, "actual_billed_cost": "unknown",
                "requested_model": args.model, "requested_effort": args.effort,
                "effective_effort": "unknown",
                "model_observation": {"source": "unavailable", "reported_model": None,
                                      "relation": "unknown"},
                "producer_runtime": _producer_runtime()}
    directory = None
    plan = None
    process = None
    observer = None
    control = None
    code = 1
    stage = "task_read"
    streaming = args.client == "codex" or args.live_input or args.stream_progress
    envelope["native_output_mode"] = "stream_json" if streaming else "final_json"
    try:
        task = _task(args.task_file)
        stage = "relay_configuration"
        plan = prepare(args.client, args.repo, args.relay, args.provider)
        if args.client == "claude":
            native = [*plan["argv"], "--print", "--output-format",
                      "stream-json" if streaming else "json", "--permission-prompts", "none"]
            if streaming:
                native.extend(["--verbose", "--input-format", "stream-json", "--replay-user-messages"])
            if args.max_turns is not None:
                native.extend(["--max-turns", str(args.max_turns)])
            if args.model is not None:
                native.extend(["--model", args.model])
            if args.effort is not None:
                native.extend(["--effort", args.effort])
            native.extend(["--resume" if args.resume else "--session-id", session])
        else:
            native = [*plan["argv"], "app-server", "--listen", "stdio://"]
        envelope["repo"] = plan["repo"]
        if args.dry_run:
            print(json.dumps({**envelope, "state": "call_prepared", "argv": native,
                              "task_sha256": hashlib.sha256(task).hexdigest(),
                              "timeout_seconds": args.timeout}, sort_keys=True))
            return 0
        stage = "evidence_setup"
        if args.output_dir is None:
            directory = Path(tempfile.mkdtemp(prefix="relay-peer-"))
        else:
            candidate = args.output_dir.absolute()
            try:
                candidate.mkdir(mode=0o700)  # Refuse an existing directory; never overwrite another call.
            except FileExistsError:
                raise LaunchError(f"--output-dir already exists: {candidate}. Choose a new directory; redirect command output outside it.") from None
            except FileNotFoundError:
                raise LaunchError(f"--output-dir parent does not exist: {candidate.parent}. Create the parent or choose a new path.") from None
            except PermissionError:
                raise LaunchError(f"--output-dir parent is not writable: {candidate.parent}. Choose a writable private location.") from None
            directory = candidate
        envelope["evidence_directory"] = str(directory)
        if args.client == "codex" or args.live_input:
            control = CallControl(directory, args.client)
            envelope["control"] = {"call_id": control.call["call_id"],
                                   "input_mode": control.call["input_mode"],
                                   "call_directory": str(directory)}
        _record(directory, "request.json", {"schema": 1, "argv": native, "repo": plan["repo"],
                                           "producer_runtime": envelope["producer_runtime"],
                                           "requested_session_id": session, "resumed": bool(args.resume),
                                           "requested_model": args.model, "requested_effort": args.effort,
                                           "task_sha256": hashlib.sha256(task).hexdigest(),
                                           "timeout_seconds": args.timeout})
        with _private_file(directory, "task.txt") as stream:
            stream.write(task)
            stream.flush()
            os.fsync(stream.fileno())
        _record(directory, "checkpoint.json", _call_checkpoint(envelope, "before_spawn"))
        _sync_directory(directory)
        # This durable breadcrumb survives an interrupted caller. Provider stdout
        # and stderr can contain private task context; they are never auto-published.
        print("multithread peer: session " + _display_text(session or "assigned by provider")
              + "; local evidence " + _display_text(directory), file=sys.stderr, flush=True)
        started = time.monotonic()
        feedback = _WaitingFeedback(started, args.timeout, envelope, control)
        from contextlib import nullcontext
        output_context = nullcontext(subprocess.PIPE) if streaming else _private_file(directory, "stdout.json")
        with output_context as output, _private_file(directory, "stderr.txt") as errors:
            try:
                stage = "provider_spawn"
                # Keep a cancellation pending until we own the returned handle.
                # This is parent-only deferral, not a signal mask inherited by
                # the provider and not a change to its execution permissions.
                interruption["starting"] = True
                try:
                    process = subprocess.Popen(native, cwd=plan["repo"], stdin=subprocess.PIPE,
                                               stdout=output, stderr=errors, start_new_session=True)
                finally:
                    interruption["starting"] = False
                try:
                    _atomic_record(directory, "checkpoint.json", _call_checkpoint(envelope, "spawned"),
                                   sync_directory=True)
                except OSError:
                    envelope.update(needs_attention=True,
                                    evidence_recording="Recovery checkpoint durability could not be confirmed after provider spawn; inspect retained evidence.")
                if streaming:
                    if args.client == "codex":
                        from . import codex_peer as driver
                    else:
                        from . import claude_peer as driver
                    observer = driver.Observation(process, directory, envelope)
                if interruption["signal"] is not None:
                    raise KeyboardInterrupt
                envelope.update(state="uncertain", provider_started=True)
                stage = "provider_call"
                if not streaming:
                    _call_final_json(process, task, args.timeout, feedback, envelope)
                else:
                    driver.run(process, task, plan["repo"], args.resume,
                               directory, envelope, args.timeout,
                               control=ObservedControl(control, envelope) if control is not None else None, observer=observer,
                               feedback=feedback,
                               **({"expected_hook": plan["relay_plan"]["hook_command"]} if args.client == "codex" else {}))
                    # EOF is the ordinary end of this owned stdio server.
                    # Retain a valid returned turn even if server shutdown
                    # needs cleanup; shutdown is not a second provider turn.
                    try:
                        # Claude may finish a reply while native background
                        # work is still running. Preserve its remaining call
                        # allowance instead of treating five seconds as a task
                        # deadline. Codex's owned server closes after its turn.
                        grace = (max(0, args.timeout - (time.monotonic() - started))
                                 if args.client == "claude" and observer.interpret else 5)
                        _wait(process, grace, observer, feedback)
                    except subprocess.TimeoutExpired:
                        interruption["stopping"] = True
                        envelope["caller_stop_reason"] = "shutdown_timeout"
                        _stop(process, observer)
                        envelope["server_cleanup"] = "owned process stopped after stdin closed"
                        envelope["needs_attention"] = True
                    code = 0 if envelope["state"] == "returned" else 1
                # The provider has exited. Finish interpreting and recording
                # its actual outcome even if an ordinary signal arrives now.
                interruption["stopping"] = True
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                interruption["stopping"] = True
                interrupted = isinstance(exc, KeyboardInterrupt)
                envelope["caller_stop_reason"] = "interrupted" if interrupted else "timeout"
                if process is not None:
                    _stop(process, observer) if observer is not None else _stop(process)
                if process is not None:
                    envelope["provider_started"] = True
                reason = "Call interrupted" if interrupted else "Call timed out"
                _call_problem(envelope, reason + "; inspect the observed turn, retained output and Multithread state before any follow-up.")
                code = (128 + (interruption["signal"] or signal.SIGINT)) if interrupted else 1
            finally:
                envelope["elapsed_seconds"] = round(time.monotonic() - started, 3)
                if process is not None:
                    envelope["process_exit_code"] = process.returncode
        if not streaming and "message" not in envelope:
            stage = "result_read"
            _interpret(directory, envelope)
            code = 0 if envelope["state"] == "returned" else 1
        elif streaming and envelope["state"] == "returned" and process.returncode != 0:
            envelope["needs_attention"] = True
            envelope["message"] = "A native turn returned, but the provider process did not exit cleanly; inspect retained evidence."
    except (LaunchError, ControlError, OSError, UnicodeError) as exc:
        envelope["unavailable_stage"] = stage
        envelope["needs_attention"] = True
        envelope["message"] = (str(exc) if isinstance(exc, (LaunchError, ControlError)) else
                               f"Unavailable during {stage}; inspect the selected path or retained evidence.")
    except (KeyboardInterrupt, EOFError) as exc:
        interruption["stopping"] = True
        if isinstance(exc, KeyboardInterrupt):
            envelope.setdefault("caller_stop_reason", "interrupted")
        if process is not None:
            _stop(process, observer) if observer is not None else _stop(process)
            envelope["provider_started"] = True
        _call_problem(envelope, "Interrupted; inspect any retained evidence before retrying.")
        code = 128 + (interruption["signal"] or signal.SIGINT)
    finally:
        # Cleanup and the receipt should survive repeated ordinary termination
        # signals. Restore the caller's handlers when peer_main returns.
        interruption["stopping"] = True
        if process is not None:
            if process.poll() is None:
                _stop(process, observer) if observer is not None else _stop(process)
                _call_problem(envelope, "The owned provider required cleanup; inspect its observed turn and retained evidence.")
            envelope.update(provider_started=True, process_exit_code=process.returncode)
        try:
            if observer is not None:
                observer.close()
        except (ControlError, OSError) as exc:
            envelope.update(needs_attention=True, evidence_recording="Observer cleanup or input acknowledgement is unavailable; inspect retained evidence.")
            code = 1
        finally:
            if control is not None:
                try:
                    control.close("The owned provider call has ended.")
                except (ControlError, OSError):
                    envelope.update(needs_attention=True, evidence_recording="Input receipt closure is unavailable; inspect retained evidence.")
                    code = 1
        if streaming and envelope["state"] == "returned":
            code = 1 if "evidence_recording" in envelope else 0
        if "control_fault" in envelope:
            envelope["needs_attention"] = True
            code = 1
        if "evidence_recording" in envelope:
            envelope["needs_attention"] = True
            code = 1
        if (process is not None and not streaming
                and "stdout_observation" not in envelope and "stdout_observation_error" not in envelope):
            # An interrupted final-JSON call has no validated result. Preserve
            # only the observation after cleanup; never promote captured bytes
            # into a returned turn or overwrite the original interruption.
            try:
                _observe_stdout(directory, envelope)
            except OSError:
                envelope["needs_attention"] = True
                code = code or 1
    preparation = _follow_up_preparation(args, plan, envelope) if code == 0 else None
    if preparation is not None:
        envelope["follow_up_preparation"] = preparation
    if directory is not None:
        try:
            _atomic_record(directory, "result.json", envelope)
        except OSError:
            envelope.pop("follow_up_preparation", None)
            envelope["needs_attention"] = True
            envelope["evidence_recording"] = "unavailable; preserve this returned result"
            code = 1
    if args.json:
        print(json.dumps(envelope, ensure_ascii=True, sort_keys=True))
    else:
        _display_peer(envelope, report_entry=args.report_entry)
    return code


def _report_number(record, key, *, integer=False, minimum=0):
    value = record.get(key)
    if value is None:
        return None
    if (type(value) not in ((int,) if integer else (int, float))
            or not math.isfinite(value) or value < minimum or value > 2**53 - 1):
        raise ValueError()
    return value


def _report_count(record, key, item_type):
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, item_type) for item in value):
        raise ValueError()
    return len(value)


def _report_scope(record, field, scopes):
    # Explicit unknown IDs stay unknown. Only old receipts need prose matching.
    if field + "_id" in record:
        value = record[field + "_id"]
        return value if isinstance(value, str) and value in scopes else "unknown"
    return next((key for key, text in scopes.items() if record.get(field) == text), "unknown")


def _report_provider_version(record):
    """Project only attributed, recognized metadata; never inspect today's CLI."""
    result = {"status": "not_recorded", "version": None, "source": None}
    if "provider_version" not in record:
        return result
    result["status"] = "invalid"
    value = record["provider_version"]
    source = {"codex": "codex_initialize_user_agent", "claude": "claude_system_init"}[record["provider"]]
    if not isinstance(value, dict) or value.get("source") != source:
        return result
    status = value.get("status")
    version = canonical_provider_version(value.get("version"))
    if (status == "reported" and version is not None
            or status in ("not_reported", "unrecognized") and value.get("version") is None):
        return {"status": status, "version": version, "source": source}
    return result


def _caller_stop_reason(record):
    if "caller_stop_reason" not in record:
        return "not_recorded"
    reason = record["caller_stop_reason"]
    return reason if reason in ("timeout", "interrupted", "shutdown_timeout") else "unknown"


def _report_projection(record):
    """Positive typed projection: never return arbitrary text or native objects."""
    if (record.get("provider") not in ("claude", "codex")
            or record.get("state") not in ("unavailable", "uncertain", "provider_error", "returned")
            or type(record.get("provider_started")) is not bool
            or type(record.get("needs_attention")) is not bool):
        raise ValueError()
    call = {key: record[key] for key in ("provider", "state", "provider_started", "needs_attention")}
    call["provider_version"] = _report_provider_version(record)
    requested_effort = record.get("requested_effort")
    call["requested_effort"] = (requested_effort if requested_effort in
                                ("low", "medium", "high", "xhigh", "max") else None)
    model_observation = record.get("model_observation")
    call["model_relation"] = (model_observation.get("relation") if isinstance(model_observation, dict)
                              and model_observation.get("relation") in
                              ("same_literal", "different_name_unverified", "prior_init_only",
                               "not_requested", "unknown") else "unknown")
    call["caller_stop_reason"] = _caller_stop_reason(record)
    call["elapsed_seconds"] = _report_number(record, "elapsed_seconds")
    call["process_exit_code"] = _report_number(record, "process_exit_code", integer=True, minimum=-(2**31))
    call["invalid_measurements"] = []
    for key in ("provider_turns", "provider_duration_ms", "estimated_cost_usd"):
        try:
            call[key] = _report_number(record, key, integer=key == "provider_turns")
        except (ValueError, OverflowError):
            # Some native metrics are copied without validation into the original
            # receipt. Preserve the call observation while identifying unusable
            # auxiliary measurements; never copy their values or coerce them.
            call[key] = None
            call["invalid_measurements"].append(key)
    errors = record.get("measurement_errors")
    if isinstance(errors, list):
        for field in ("usage", "model_usage", "model_context_window", "provider_turns",
                      "provider_duration_ms", "estimated_cost_usd"):
            if field not in call["invalid_measurements"] and any(
                    isinstance(item, str) and (item == field or item.startswith(field + "."))
                    for item in errors[:32]):
                call["invalid_measurements"].append(field)
    call["provider_measurement_scope"] = _report_scope(record, "usage_scope", USAGE_SCOPES)
    call["cost_scope"] = _report_scope(record, "cost_scope", COST_SCOPES)
    call["actual_billed_cost"] = "unknown"
    call["permission_denial_count"] = _report_count(record, "permission_denials", dict)
    call["provider_error_count"] = _report_count(record, "provider_errors", str)
    call["unsupported_native_request_count"] = _report_count(record, "unsupported_native_requests", str)
    call["task_submission"] = (
        "not_recorded" if "task_submission" not in record else record["task_submission"]
        if record["task_submission"] in ("not_submitted", "requested", "accepted") else "unknown")
    call["task_delivery"] = (
        "not_recorded" if "task_delivery" not in record else record["task_delivery"]
        if record["task_delivery"] in ("written", "uncertain") else "unknown")
    call["native_input_unwritten_bytes"] = _report_number(record, "native_input_unwritten_bytes", integer=True)
    producer = record.get("producer_runtime")
    producer_status = "not_recorded" if "producer_runtime" not in record else "invalid"
    if isinstance(producer, dict):
        digest = producer.get("runtime_manifest_sha256")
        if (producer.get("status") == "recorded" and isinstance(digest, str)
                and len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)):
            producer_status = "recorded"
        elif producer.get("status") == "unavailable" and digest is None:
            producer_status = "unavailable"
    call["producer_runtime_identity"] = producer_status
    call["unavailable_stage"] = (record.get("unavailable_stage") if record.get("unavailable_stage") in
                                  ("task_read", "relay_configuration", "evidence_setup", "provider_spawn",
                                   "provider_call", "result_read") else "unknown")
    identities = {key: record.get(key) for key in
                  ("session_id", "requested_session_id", "observed_session_id")}
    if any(value is not None and (not isinstance(value, str) or not value) for value in identities.values()):
        raise ValueError()
    call["session_identity"] = (
        "mismatch_observed" if identities["observed_session_id"] else
        "verified" if identities["session_id"] else
        "requested_only" if identities["requested_session_id"] else "not_recorded")
    observation = {"status": "not_recorded", "bytes": None, "truncated": None, "scope": "unknown"}
    if "stdout_observation_error" in record:
        if not isinstance(record["stdout_observation_error"], str) or not record["stdout_observation_error"]:
            raise ValueError()
        observation["status"] = "unavailable"
    elif "stdout_observation" in record:
        value = record["stdout_observation"]
        if (not isinstance(value, dict) or type(value.get("bytes")) is not int
                or type(value.get("truncated")) is not bool):
            raise ValueError()
        observation.update(status="recorded", bytes=_report_number(value, "bytes", integer=True),
                           truncated=value["truncated"],
                           scope="bounded_read" if value.get("scope") == "bounded_read" else "unknown")
    call["stdout_observation"] = observation
    fault = record.get("control_fault")
    if "control_fault" in record and (not isinstance(fault, dict) or fault.get("state") != "unavailable"
            or any(not isinstance(fault.get(key), str) or not fault[key] for key in ("operation", "detail"))):
        raise ValueError()
    call["faults"] = {"control": "reported" if "control_fault" in record else "not_recorded"}
    for name, keys in (("recording", ("evidence_recording",)),
                       ("cleanup", ("server_cleanup", "owned_process_cleanup", "stdout_completion"))):
        present = [key for key in keys if key in record]
        if any(not isinstance(record[key], str) or not record[key] for key in present):
            raise ValueError()
        call["faults"][name] = "reported" if present else "not_recorded"
    # Reporting this receipt does not execute tools or inspect actual ledger state.
    for key in ("hook_delivery", "provider_tools", "relay_acknowledgement", "workflow_completion"):
        call[key] = "not_checked"
    return call


def _read_checkpoint(directory):
    """Project a private recovery breadcrumb when a terminal receipt is missing."""
    from .peer_control import ControlError, _directory, _object, _private
    try:
        parent = _directory(directory)
        try:
            fd = os.open("checkpoint.json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(fd, "rb") as stream:
            _private(os.fstat(stream.fileno()))
            body = stream.read(2049)
        if len(body) > 2048:
            return None
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_object)
        if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1
                or value.get("kind") != "peer_checkpoint"
                or value.get("provider") not in ("claude", "codex")
                or value.get("phase") not in ("before_spawn", "spawned")
                or value.get("provider_start_observation") !=
                   ("confirmed" if value["phase"] == "spawned" else "unknown")
                or value.get("outcome") != "unknown"):
            return None
        return {"provider": value["provider"], "phase": value["phase"],
                "provider_start_observation": value["provider_start_observation"],
                "outcome": "unknown"}
    except (OSError, ControlError, ValueError, UnicodeError, RecursionError):
        return None


def _comparable_cost_receipt(directory):
    """Read only fields needed to compare two private cumulative estimates."""
    from .peer_control import ControlError, _directory, _object, _private
    def invalid_constant(_):
        raise ValueError()
    try:
        parent = _directory(directory)
        try:
            identity = os.fstat(parent)
            fd = os.open("result.json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(fd, "rb") as stream:
            _private(os.fstat(stream.fileno()))
            body = stream.read(_MAX_RESULT + 1)
        if len(body) > _MAX_RESULT:
            return None
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=invalid_constant)
        if not isinstance(value, dict):
            return None
        _report_projection(value)
        if (type(value.get("schema")) is not int or value["schema"] != 1
                or value.get("provider") != "claude" or value.get("state") != "returned"
                or value.get("provider_started") is not True
                or value.get("needs_attention") is not False
                or not isinstance(value.get("session_id"), str) or not value["session_id"]
                or value.get("cost_scope_id") != "cumulative_through_latest_native_result"
                or type(value.get("estimated_cost_usd")) not in (int, float)
                or not math.isfinite(value["estimated_cost_usd"])
                or not 0 <= value["estimated_cost_usd"] <= 2**53 - 1):
            return None
        _session(value["session_id"])
        return ((identity.st_dev, identity.st_ino), value["session_id"],
                value.get("resumed"), value["estimated_cost_usd"])
    except (OSError, ControlError, ValueError, UnicodeError, RecursionError, OverflowError,
            argparse.ArgumentTypeError):
        return None


def _cost_comparison(current, earlier):
    latest, prior = _comparable_cost_receipt(current), _comparable_cost_receipt(earlier)
    result = {"status": "unavailable", "estimated_difference_usd": None,
              "scope": "two caller-selected cumulative Claude receipts; intervening session activity is not excluded; not billing"}
    if (latest is None or prior is None or latest[0] == prior[0]
            or latest[1] != prior[1] or latest[2] is not True
            or latest[3] < prior[3]):
        return result
    result.update(status="estimated", estimated_difference_usd=round(latest[3] - prior[3], 9))
    return result


def _read_report(directory):
    from .peer_control import ControlError, _directory, _object, _private
    report = {"schema": 1, "kind": "peer_report", "report_state": "unavailable",
              "receipt_status": "unavailable", "call": None}
    try:
        parent = _directory(directory)
        try:
            try:
                fd = os.open("result.json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                report["receipt_status"] = "missing"
                checkpoint = _read_checkpoint(directory)
                if checkpoint is not None:
                    report.update(report_state="incomplete", checkpoint=checkpoint)
                return report
        finally:
            os.close(parent)
        with os.fdopen(fd, "rb") as stream:
            _private(os.fstat(stream.fileno()))
            # This is an independent receipt-read bound, not the native capture
            # bound. JSON expansion can make a valid receipt larger than this.
            body = stream.read(_MAX_RESULT + 1)
    except (OSError, ControlError):
        return report
    if len(body) > _MAX_RESULT:
        report["receipt_status"] = "too_large"
        return report
    try:
        def invalid_constant(value):
            raise ValueError()
        def finite_float(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError()
            return number
        record = json.loads(body.decode("utf-8"), object_pairs_hook=_object,
                            parse_constant=invalid_constant, parse_float=finite_float)
        if not isinstance(record, dict) or type(record.get("schema")) is not int:
            raise ValueError()
        if record["schema"] != 1:
            report["receipt_status"] = "unsupported_schema"
            return report
        call = _report_projection(record)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        report["receipt_status"] = "malformed"
        return report
    report.update(report_state="reported", receipt_status="available", call=call)
    return report


def report_main(argv=None):
    parser = argparse.ArgumentParser(prog="multithread peer report",
        description="Summarize selected fields from a private result receipt, or an incomplete recovery checkpoint. No provider or ledger operation.")
    parser.add_argument("--call-dir", required=True, type=Path, help="exact private directory retained by the peer call")
    parser.add_argument("--compare-call-dir", type=Path,
                        help="earlier private call directory for an optional same-session cumulative cost difference")
    parser.add_argument("--repo", type=Path, help="accepted for the common command prefix; unused by this receipt-only report")
    parser.add_argument("--json", action="store_true", help="print the structured support report; exit 0 means reported, not a successful call")
    args = parser.parse_args(argv)
    report = _read_report(args.call_dir)
    if args.compare_call_dir is not None:
        report["cost_comparison"] = _cost_comparison(args.call_dir, args.compare_call_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    else:
        print("Multithread peer report: " + report["report_state"])
        print("Result receipt: " + report["receipt_status"])
        comparison = report.get("cost_comparison")
        if comparison is not None:
            print("Selected cumulative cost difference: " +
                  (str(comparison["estimated_difference_usd"]) + " USD estimate"
                   if comparison["status"] == "estimated" else "unavailable")
                  + "; intervening session activity is not excluded; not billing.")
        call = report["call"]
        if call is not None:
            print(f"Recorded call: {call['provider']} / {call['state']}")
            version = call["provider_version"]
            print("Recorded provider version: " + (
                version["version"] + " (provider-reported; " + version["source"] + ")"
                if version["status"] == "reported" else "unknown (" + version["status"] + ")"))
            if call["provider"] == "claude" and call["requested_effort"] is not None:
                print("Requested effort: " + call["requested_effort"]
                      + "; effective effort unknown.")
            if call["provider"] == "claude" and call["model_relation"] != "unknown":
                print("Model observation: " + call["model_relation"]
                      + " (name comparison only; aliases may resolve to another name).")
            print("Recorded task submission: " + call["task_submission"])
            print("Recorded producer runtime identity: " + call["producer_runtime_identity"] + " (digest omitted)")
            print("Needs attention: " + ("yes" if call["needs_attention"] else "no"))
            if call["unavailable_stage"] != "unknown":
                print("Unavailable stage: " + call["unavailable_stage"])
            for key, label in (("permission_denial_count", "Retained permission denials"),
                               ("provider_error_count", "Retained provider errors"),
                               ("unsupported_native_request_count", "Retained unsupported native requests")):
                print(label + ": " + str(call[key] if call[key] is not None else "unknown"))
            for key, value in call["faults"].items():
                if value == "reported":
                    print("Recorded " + key + " fault: reported")
            if call["invalid_measurements"]:
                print("Invalid recorded measurements (values omitted): " + ", ".join(call["invalid_measurements"]))
            print("Process exit: " + str(call["process_exit_code"] if call["process_exit_code"] is not None else "unknown"))
            print("Caller stop reason: " + call["caller_stop_reason"] + "; not a diagnosis of provider behavior.")
            print("Elapsed seconds: " + str(call["elapsed_seconds"] if call["elapsed_seconds"] is not None else "unknown"))
            observation = call["stdout_observation"]
            print("Stdout observation: " + observation["status"] + (
                f"; {observation['bytes']} bytes; truncated={str(observation['truncated']).lower()}; scope={observation['scope']}"
                if observation["status"] == "recorded" else "; byte count unknown"))
            print("Recorded session identity: " + call["session_identity"] + " (identifiers omitted)")
            print("Task pipe delivery: " + call["task_delivery"] + "; recorded native-input pending bytes (mode-dependent): "
                  + str(call["native_input_unwritten_bytes"] if call["native_input_unwritten_bytes"] is not None else "unknown")
                  + "; a pipe write does not prove native consumption.")
        else:
            checkpoint = report.get("checkpoint")
            if checkpoint is not None:
                print("Recovery checkpoint: " + checkpoint["provider"] + " / " + checkpoint["phase"]
                      + "; provider start " + checkpoint["provider_start_observation"]
                      + "; outcome unknown. The caller may have stopped before its terminal receipt.")
            next_step = {
                "missing": "compare the selected call directory with the original call's retained-evidence location.",
                "malformed": "inspect the original private result.json locally for invalid or incomplete data without rewriting it.",
                "unavailable": "verify the selected directory and receipt against the documented access, ownership, privacy and file-type requirements.",
                "unsupported_schema": "select a reviewed reporter that supports the retained receipt's schema, preserving the original receipt.",
                "too_large": "inspect the original private receipt locally in bounded portions without truncating or rewriting it.",
            }[report["receipt_status"]]
            if checkpoint is not None:
                next_step = "inspect the retained private task, native output and durable work; do not infer completion or retry automatically."
            print("Next: " + next_step)
        print("Retained receipt only: provider activity, cause and workflow completion are not checked.")
        print("Review before sharing. Task/answer text, paths, identities, hashes and arbitrary native diagnostics are excluded.")
    return 0 if report["report_state"] == "reported" else 1


def _display_peer(envelope, *, report_entry=None):
    """Show the observed answer and outstanding conditions without changing state."""
    print(f"Multithread peer: {envelope['state']}")
    if envelope.get("needs_attention"):
        print("Needs attention: yes.")
    if "caller_stop_reason" in envelope:
        print("Caller stop reason: " + _caller_stop_reason(envelope) + "; not a diagnosis of provider behavior.")
    if envelope["result"]:
        print(envelope["result"])
    elif isinstance(envelope.get("partial_result"), str):
        print("Partial result: inspect the private result.json's partial_result field; its text may answer incomplete or other input.")
    results = envelope.get("native_results", [])
    if results and not results[-1]["related"] and results[-1].get("result_excerpt"):
        print("Additional native session result (not attributed to this call's submitted input):")
        print(results[-1]["result_excerpt"])
        if results[-1].get("result_excerpt_truncated"):
            print("[Excerpt truncated; inspect the captured native stdout for available detail.]")
    if envelope.get("native_results_truncated"):
        print("Native result history is incomplete; inspect the captured native stdout for available detail.")

    # These are selected diagnostic fields, never whole native objects or tool inputs.
    details = [(label, envelope.get(key)) for key, label in (
        ("evidence_recording", "Evidence recording"),
        ("native_input_write_error", "Native input"),
        ("server_cleanup", "Provider cleanup"),
        ("owned_process_cleanup", "Process exit"),
        ("stdout_completion", "Output completion"),
        ("stdout_observation_error", "Output observation"),
    )]
    if envelope["state"] == "provider_error":
        details.append(("Provider result", envelope.get("provider_subtype")))
        errors = envelope.get("provider_errors")
        if isinstance(errors, list):
            first_error = next((item for item in errors[:8] if isinstance(item, str) and item), None)
            if first_error is not None:
                details.append(("Provider error", first_error))
                if len(errors) > 1:
                    details.append(("Provider errors", f"showing 1 of {len(errors)} retained diagnostics; inspect retained output for the rest."))
        if envelope.get("provider_errors_truncated"):
            details.append(("Provider errors", "details were truncated; inspect retained output for available detail."))
    fault = envelope.get("control_fault")
    if isinstance(fault, dict):
        details.append(("Peer input", fault.get("detail")))
    reason = envelope.get("terminal_reason")
    if reason not in (None, "end_turn", "completed"):
        details.append(("Provider stopping reason", reason))
    for label, value in details:
        if isinstance(value, str) and value:
            print(f"{label}: {value[:2000]}" + (" [Detail truncated.]" if len(value) > 2000 else ""))
    observation = envelope.get("stdout_observation")
    if isinstance(observation, dict):
        size = observation.get("bytes")
        if envelope.get("needs_attention") and type(size) is int and size >= 0:
            if size == 0:
                print("Provider stdout: no bytes observed; provider activity is unknown.")
            else:
                print(f"Provider stdout: {size} bytes observed; inspect retained stdout.json.")
        if observation.get("truncated"):
            if observation.get("scope") == "bounded_read":
                print("Output observation was truncated; the byte count and digest cover only the read prefix. Inspect retained stdout.json for available output.")
            else:
                print("Output capture was truncated; only the captured prefix is available.")
    if envelope.get("permission_denials"):
        print("Permission requests were denied; review them in the local result before continuing.")
    print(_display_text(envelope.get("message", "Inspect the peer result.")))
    if type(envelope.get("resumed")) is bool:
        print("Requested session mode: " + ("resume" if envelope["resumed"] else "fresh"))
    try:
        elapsed = _report_number(envelope, "elapsed_seconds")
    except (ValueError, OverflowError):
        elapsed = None
    if elapsed is not None:
        print(f"Elapsed seconds: {elapsed}")
    if envelope.get("measurement_errors"):
        print("Some provider measurements were invalid; affected values are unavailable. Inspect the private receipt for fields; the task outcome is assessed separately.")
    if envelope.get("needs_attention") and "task_submission" in envelope:
        submission = envelope["task_submission"]
        print("Recorded task submission: " + (submission if isinstance(submission, str)
              and submission in ("not_submitted", "requested", "accepted") else "unknown")
              + "; acceptance is not task completion.")
    if envelope["session_id"]:
        print(f"Peer session: {envelope['session_id']}")
    else:
        requested = envelope.get("requested_session_id")
        if isinstance(requested, str) and requested:
            print(f"Requested session (unverified): {requested[:2000]}" +
                  (" [Detail truncated.]" if len(requested) > 2000 else ""))
            if envelope.get("needs_attention") and envelope.get("provider_started") is True:
                print("This requested identity does not confirm a resumable session. Check native session state before choosing resume or a fresh call.")
    observed_session = envelope.get("observed_session_id")
    if isinstance(observed_session, str) and observed_session:
        print(f"Observed session (unverified): {observed_session[:2000]}" +
              (" [Detail truncated.]" if len(observed_session) > 2000 else ""))
    if envelope["evidence_directory"]:
        print("Local evidence: " + _display_text(envelope["evidence_directory"]))
    if envelope.get("needs_attention"):
        if envelope["evidence_directory"]:
            print("Next: inspect the local evidence and any task artifacts before deciding on follow-up.")
            directory = str(envelope["evidence_directory"])
            print("Private result receipt: " + _display_text(str(Path(directory) / "result.json")))
            entry = report_entry if report_entry is not None else [str(account_launcher()), "peer"]
            _display_command("Support report", [*entry, "report", "--call-dir", directory, "--json"])
            print("The support report omits the answer and private detail; it does not assess task completion.")
        else:
            print("Next: inspect the reported condition before deciding whether to retry.")
    preparation = envelope.get("follow_up_preparation")
    if preparation and envelope["state"] == "returned" and not envelope.get("needs_attention"):
        print("Before follow-up, assess this result and confirm session ownership and the remaining scope.")
        _display_command("Follow-up preparation", preparation["argv_prefix"])
        print("Append a new task file path to this prefix to prepare a dry-run; it does not start the provider.")
