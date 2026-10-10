"""Bounded native Codex App Server transport; no provider policy overrides.

The caller owns the process and cleanup. This module owns only its JSONL pipes,
private stdout observation, and native response interpretation. Native request
acceptance never substitutes for a durable Multithread acknowledgement. A model
or effort the caller requested travels with the turn; Codex decides what it uses.
"""

import base64
import binascii
import collections
import hashlib
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import stat
import subprocess
import time

from .native_io import MAX_OUTPUT as _MAX_OUTPUT, Observation, ProtocolError as _ProtocolError, decode, identity as _identity
from .native_io import (USAGE_SCOPES, measurement_error, measurement_number,
                        measurement_fields, measurement_scope, replace_measurement_errors,
                        provider_version_observation, setting_relation)


_MAX_PENDING = 128
_MAX_DETAILS = 8
_MAX_LISTING = 1024 * 1024
# Reading the model list is bounded; when it fails or runs long, the call proceeds unchecked.
_MODEL_LIST_PAGES = 32
_MODEL_LIST_SECONDS = 10
_MCP_STATUS_PAGES = 5  # bounded: what runs is listed before the task, not browsed
_MCP_STATUS_PAGE_SIZE = 100  # enforce the requested size; at most 500 server identities
_FEATURE_PAGES = 5  # Codex 0.161 lists its 160 features in one page
# Child threads are always off for a peer; plugins and apps each unless the call allows them. Codex's own per-thread
# feature list must confirm each before the task goes out.
_ALWAYS_OFF = ("multi_agent", "multi_agent_v2")
_APPS_SERVER = "codex_apps"  # Codex's one server for the account's ChatGPT apps (connectors); it has no pluginId
_APPROVAL_POLICIES = ("untrusted", "on-failure", "on-request", "never")
# A peer never escalates: asked for in every request that can carry it, and confirmed in Codex's response.
_NO_ESCALATION = {"approvalPolicy": "never", "approvalsReviewer": "user"}


def _server_kind(name, plugin):
    """What a listed MCP server is, judged from Codex's report. The apps' server name is reserved."""
    if not _identity(name) or not (plugin is None or _identity(plugin)):
        return "malformed"
    if name == _APPS_SERVER:
        return "apps" if plugin is None else "reserved"
    return "plugin" if plugin is not None else "configured"


def _remedy(kinds):
    """The next step for refused servers: an opt-in only where one would admit them."""
    steps = (["pass --allow-plugins to admit the account's plugin servers"] if "plugin" in kinds else []) + (
        ["pass --allow-apps to admit its ChatGPT apps (connectors)"] if "apps" in kinds else [])
    if kinds - {"plugin", "apps"}:
        steps.append("no option admits a configured, duplicate, malformed or misnamed server; remove or fix it in "
                     "Codex's configuration")
    return "; ".join(steps)
# Where a setting the call does not request comes from: Codex keeps a thread's settings.
_KEPT = {"new_thread": "the new thread's configured", "resumed_thread": "this thread's current"}
_CLIENT_INFO = {"name": "multithread", "title": "Multithread", "version": "0.4.1"}
_HOOK_EVENTS = ("sessionStart", "userPromptSubmit", "postToolUse", "stop", "sessionEnd", "interrupt")
# Statuses a person resolves in Codex's /hooks review; any other is configuration.
_REVIEWABLE = frozenset({"untrusted", "modified", "disabled"})
# Codex reports each generated image inline as base64 beside the file it saved.
_MAX_IMAGES = 64


def _saved_file(path, digest, size):
    """Compare Codex's saved image with its inline payload; never copy the file."""
    if (not isinstance(path, str) or not os.path.isabs(path) or len(path) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in path)):
        return "not_reported"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            return "differs"
        observed, read = hashlib.sha256(), 0
        while read <= size:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            observed.update(chunk)
            read += len(chunk)
        return "matches" if read == size and observed.hexdigest() == digest else "differs"
    except OSError:
        return "unreadable"
    finally:
        os.close(fd)


def _multithread_hook(command, launcher):
    """Recognize any Multithread provider-hook command, current or older."""
    try:
        argv = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return False
    return (bool(argv) and "provider-hook" in argv
            and (argv[0] == launcher or Path(argv[0]).name in ("multithread", "relay")))


def hook_readiness(result, repo, expected_hook, user_file=None):
    """Classify each Multithread hook exactly as Codex lists it for this checkout.

    The one handler per event comes from invocation flags, or from the user hook
    file when user_file names it. Every session-flag entry for an event, and any
    Multithread provider hook from another source (including an older
    per-checkout command), counts before commands are compared, so a second
    handler cannot sit unseen beside ours.
    """
    groups = result.get("data") if isinstance(result, dict) else None
    if not isinstance(groups, list) or any(not isinstance(group, dict) for group in groups):
        raise _ProtocolError("Native hook readiness could not be read; no task was submitted.")
    groups = [group for group in groups if group.get("cwd") == repo]
    if len(groups) != 1 or not isinstance(groups[0].get("hooks"), list):
        raise _ProtocolError("Native hook readiness did not identify the selected checkout; no task was submitted.")
    if not all(isinstance(hook, dict) and isinstance(hook.get("eventName"), str) for hook in groups[0]["hooks"]):
        # A skipped entry could hide a second handler; the whole answer is unusable.
        raise _ProtocolError("Native hook readiness could not be read; no task was submitted.")
    listed = {name: [] for name in _HOOK_EVENTS}
    launcher = shlex.split(expected_hook)[0]
    for hook in groups[0]["hooks"]:
        if (hook["eventName"] in listed
                and (hook.get("source") == "sessionFlags" or _multithread_hook(hook.get("command"), launcher))):
            listed[hook["eventName"]].append(hook)

    def expected_source(hook):
        if user_file is None:
            return hook.get("source") == "sessionFlags"
        return hook.get("source") == "user" and _same_path(hook.get("sourcePath"), user_file)

    events = {}
    for name, hooks in listed.items():
        hook = hooks[0] if len(hooks) == 1 else {}
        if not hooks:
            events[name] = "missing"
        elif len(hooks) > 1:
            events[name] = "duplicate"
        elif (not expected_source(hook)
              or hook.get("command") != expected_hook or hook.get("handlerType") != "command"
              or hook.get("async", False) is not False or hook.get("matcher") is not None
              or type(hook.get("timeoutSec")) is not int or hook["timeoutSec"] != 3):
            events[name] = "mismatched"
        elif hook.get("enabled") is not True:
            events[name] = "disabled"
        elif hook.get("trustStatus") in ("trusted", "untrusted", "modified"):
            events[name] = hook["trustStatus"]
        else:
            events[name] = "unrecognized"
    unready = [name for name in _HOOK_EVENTS if events[name] != "trusted"
               and not (user_file is not None and name == "postToolUse" and events[name] == "missing")]
    state = ("ready" if not unready else "needs_review"
             if all(events[name] in _REVIEWABLE for name in unready) else "needs_configuration")
    return {"state": state, "events": events, "unready_events": sorted(unready),
            "ready_events": sorted(name for name in events if events[name] == "trusted"),
            "optional_events_missing": ["postToolUse"] if events["postToolUse"] == "missing" else []}


def _same_path(listed, expected):
    if not isinstance(listed, str):
        return False
    if listed == str(expected):
        return True
    try:
        return os.path.samefile(listed, expected)
    except OSError:
        return False


def hook_remedy(readiness, repo, expected_hook, user_file=None):
    """Name the unready events by status and give the one next step for them."""
    events = readiness["events"]
    statuses = sorted({events[name] for name in readiness["unready_events"]})
    detail = "; ".join(status + ": " + ", ".join(name for name in _HOOK_EVENTS if events[name] == status)
                       for status in statuses)
    launcher = shlex.split(expected_hook)[0]
    launch = [launcher, "launch", "codex", "--repo", repo]
    check = shlex.join([launcher, "setup", "--repo", repo, "--check", "--json"])
    if user_file is not None:
        # User-level hooks: the person chose how trust is recorded at setup.
        if readiness["state"] == "needs_review":
            action = ("Codex skips these user-level hooks until they are trusted. Either the person opens /hooks in "
                      "a Codex terminal and trusts the installed Multithread hooks from " + str(user_file) + " running "
                      + expected_hook + ", or, if the person chose agent-assisted trust, an agent runs "
                      + shlex.join([launcher, "hooks", "trust"]) + " and shows them its plan before recording it.")
        else:
            action = ("Codex did not load the user-level hooks as Multithread installed them: inspect "
                      + shlex.join([launcher, "hooks", "status"]) + " and follow its next step.")
        return "Codex hooks are not ready (" + detail + ")", action
    if readiness["state"] == "needs_review":
        action = ("In their own terminal, the person reviews once in Codex: run " + shlex.join(launch) + ", type launch, open /hooks "
                  "and trust the six Multithread hooks running " + expected_hook
                  + ". That review covers every enrolled checkout and worktree.")
        if "modified" in statuses:
            action += (" Modified means Codex last trusted a different command in that slot, such as an"
                       " older per-checkout Multithread hook; trust only this exact command.")
    elif statuses == ["missing"] and len(readiness["unready_events"]) == len(_HOOK_EVENTS):
        action = ("Codex listed none of these hooks: check that Codex hooks are enabled (features.hooks), then inspect "
                  + check + ".")
    else:
        action = ("Codex did not load these hooks as Multithread generated them: compare the plan in "
                  + check + " with your Codex hook configuration.")
    # The reader may be an agent: launch and hook trust stay with the person.
    action += " This is the person's step; an agent reports it and never runs launch or changes hook trust."
    return "Codex hooks are not ready (" + detail + ")", action


def _settings_refusal(model, model_source, effort, effort_source, efforts):
    """Say which pair would have run, why nothing was submitted, and the exact next step."""
    subject = model if model_source == "requested" else model + ", " + _KEPT[model_source] + " model"
    setting = "effort " + effort if effort_source == "requested" else effort + ", " + _KEPT[effort_source] + " effort,"
    message = "Codex's model list does not advertise " + setting + " for " + subject + "."
    if effort_source != "requested":
        message += " Codex keeps a thread's effort when only the model changes."
    message += " No task was submitted, because the provider could reject that pair or run it unverified."
    if not efforts:
        return message + " It lists no effort at all for " + model + ", so choose another model with --model."
    message += " Repeat the call with --effort set to one " + model + " advertises: " + ", ".join(efforts) + "."
    if model_source != "requested":
        message += " Or add --model with a model that advertises " + effort + "."
    return message


class CodexRejected(_ProtocolError):
    """Codex answered with a well-formed error: it refused, rather than went silent or garbled.

    The error alone does not say whether a write happened first; callers
    decide that from the error's code and Codex's own write path.
    """

    def __init__(self, message, code=None, data=None):
        super().__init__(message)
        self.code, self.data = code, data


class AppServer:
    """One owned stdio app server for configuration questions: no thread or turn.

    Requests run one at a time and each response must answer the pending one.
    ProtocolError, OSError or TimeoutExpired mean the answer is unavailable,
    never that the asked-for state is absent. Leaving the block ends the server
    and any descendants in the process group this object created.
    """

    def __init__(self, argv, cwd, *, timeout=15, on_start=None, limit=_MAX_LISTING):
        self.argv, self.cwd, self.timeout, self.limit = argv, cwd, timeout, limit
        self.on_start = on_start
        self.process = None
        self.next_id = 0
        self.buffer = bytearray()
        self.received = 0
        self.deadline = None

    def __enter__(self):
        self.process = subprocess.Popen([*self.argv, "app-server", "--listen", "stdio://"], cwd=self.cwd,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            if self.on_start is not None:
                self.on_start()
            self.deadline = time.monotonic() + self.timeout
            self.initialize = self.call("initialize", {"clientInfo": _CLIENT_INFO})
            self._send({"method": "initialized", "params": {}})
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _send(self, value):
        self.process.stdin.write((json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def call(self, method, params):
        """Return the result object, or raise ProtocolError with Codex's refusal."""
        self.next_id += 1
        pending = self.next_id
        self._send({"id": pending, "method": method, "params": params})
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while True:
                while b"\n" in self.buffer:
                    line, _, rest = bytes(self.buffer).partition(b"\n")
                    self.buffer[:] = rest
                    message = decode(line)
                    if "method" in message:
                        continue
                    if message.get("id") != pending:
                        raise _ProtocolError("Codex answered a request that was not pending.")
                    result, error = message.get("result"), message.get("error")
                    if "error" not in message and isinstance(result, dict):
                        return result
                    # Only a well-formed JSON-RPC error is Codex saying no; any
                    # other answer leaves the request's outcome unknown.
                    if ("result" not in message and isinstance(error, dict) and type(error.get("code")) is int
                            and isinstance(error.get("message"), str)):
                        detail = error["message"][:500]
                        raise CodexRejected("Codex rejected " + method + (
                            ": " + detail if detail.isprintable() and detail else "."),
                            error["code"], error.get("data"))
                    raise _ProtocolError("Codex gave an unusable answer to " + method + "; its outcome is unknown.")
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(self.process.args, self.timeout)
                if not selector.select(remaining):
                    continue
                chunk = os.read(self.process.stdout.fileno(), 65536)
                self.received += len(chunk)
                if not chunk or self.received > self.limit:
                    raise _ProtocolError("Codex ended or exceeded its bound before answering " + method + ".")
                self.buffer.extend(chunk)

    def __exit__(self, *_exc):
        process = self.process
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # Descendants can outlive the server; end only the group this call created.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        return False


def list_hooks(argv, repo, *, timeout=15, on_start=None):
    """Ask Codex's app server for one checkout's hooks: initialize and hooks/list only.

    This starts Codex but creates no thread or turn, submits no task and changes
    no trust. ProtocolError, OSError or TimeoutExpired mean the listing is
    unavailable, never that hooks are absent.
    """
    with AppServer(argv, repo, timeout=timeout, on_start=on_start) as server:
        return server.call("hooks/list", {"cwds": [repo]})


def _label(value):
    """A provider-reported name, as a bounded printable label; anything else isn't repeated."""
    return value[:100] if isinstance(value, str) and value and value.isprintable() else "(unprintable)"


class _Driver:
    def __init__(self, process, task, repo, resume, envelope, timeout, control, expected_hook=None,
                 hook_file=None):
        self.process = process
        self.task = task.decode("utf-8")
        self.repo = repo
        self.resume = resume
        self.envelope = envelope
        self.deadline = time.monotonic() + timeout
        self.timeout = timeout
        self.control = control
        self.expected_hook = expected_hook
        self.hook_file = hook_file
        self.next_id = 1
        self.pending = {}
        self.outgoing = bytearray()
        self.session = None
        self.turn = None
        self.turn_requested = False
        self.buffered = []
        self.terminal = None
        self.messages = {}
        self.denials = []
        self.unknown_requests = []
        self.errors = []
        self.details_truncated = False
        self.seen_controls = set()
        self.stdin_closed = False
        self.interrupt_requested = False
        self.observation_only = False
        self.capture = None
        self.outcome_recorded = False
        self.had_problem = False
        self.observation_lost = False
        self.catalog = None
        self.listing = None
        self.listing_deadline = None
        self.listed_pages = 0
        self.abandoned = set()
        self.waiting_turn = None
        self.mcp_pages = 0
        self.mcp_read = 0
        self.feature_pages = 0
        self.admitted = None  # the MCP servers this call allows, once the list is read
        self.early_activity = []
        self.features = {}
        self.mcp_servers = []
        self.envelope.update(state="uncertain", requested_session_id=resume,
                             needs_attention=True, task_submission="not_submitted")

    def after_readiness(self):
        """Read Codex's model list first, and only when the call requests a setting."""
        if self.envelope.get("requested_model") is None and self.envelope.get("requested_effort") is None:
            self.start_thread()
            return
        self.catalog = []
        self.listing_deadline = time.monotonic() + min(_MODEL_LIST_SECONDS, self.remaining() / 4)
        self.list_models(None)

    def list_models(self, cursor):
        # Hidden models stay selectable, so the check reads them too.
        params = {"includeHidden": True}
        if cursor is not None:
            params["cursor"] = cursor
        self.listing = self.request("model/list", params)

    def listed(self, result):
        """Collect one page; an unusable page leaves the settings unverified."""
        data = result.get("data") if isinstance(result, dict) else None
        cursor = result.get("nextCursor") if isinstance(result, dict) else None
        if (not isinstance(data, list) or any(not isinstance(model, dict) for model in data)
                or cursor is not None and not _identity(cursor)):
            return self.listing_done(None)
        self.catalog.extend(data)
        self.listed_pages += 1
        if cursor is None:
            return self.listing_done(self.catalog)
        if self.listed_pages >= _MODEL_LIST_PAGES:
            return self.listing_done(None)
        self.list_models(cursor)

    def listing_done(self, catalog):
        self.listing = None
        self.catalog = catalog
        model, effort = self.envelope.get("requested_model"), self.envelope.get("requested_effort")
        if model is not None and effort is not None:
            # The call names the whole pair, so it is checked before any thread opens.
            self.check_settings(model, "requested", effort, "requested")
        self.start_thread()

    def poll_listing(self):
        """Stop waiting for a slow model list: the call proceeds and Codex decides."""
        if self.listing is not None and time.monotonic() >= self.listing_deadline:
            self.pending.pop(self.listing)
            self.abandoned.add(self.listing)
            self.listing_done(None)

    def check_settings(self, model, model_source, effort, effort_source):
        """Check the model and effort the turn will run with, before any task is submitted.

        A source is "requested", or the opened thread's, since Codex keeps a
        thread's setting that a turn does not override. Codex's advertised
        efforts are the contract: it accepts any effort, and the provider then
        rejects an unadvertised pair or runs it unverified. A model Codex does
        not list may be an alias or another provider's model: it proceeds.
        """
        status, efforts = "unavailable", None
        if self.catalog is not None:
            offered = [entry.get("supportedReasoningEfforts") for entry in self.catalog
                       if model is not None and model in (entry.get("model"), entry.get("id"))]
            if model is None:
                status = "inherited_unknown"
            elif not offered:
                status = "model_unlisted"
            elif all(isinstance(options, list) and all(
                    isinstance(option, dict) and _identity(option.get("reasoningEffort")) for option in options)
                    for options in offered):
                efforts = list(dict.fromkeys(option["reasoningEffort"] for options in offered for option in options))
                status = ("inherited_unknown" if effort is None else
                          "verified" if effort in efforts else "refused")
        self.envelope["settings_check"] = {"status": status, "model": model, "model_source": model_source,
                                           "effort": effort, "effort_source": effort_source,
                                           "advertised_efforts": efforts}
        if status == "refused":
            raise _ProtocolError(_settings_refusal(model, model_source, effort, effort_source, efforts))

    def list_features(self, cursor):
        self.feature_pages += 1
        self.request("experimentalFeature/list", {"threadId": self.session, "limit": 200,
                                                  **({"cursor": cursor} if cursor is not None else {})})

    def features_listed(self, result):
        """Confirm, from Codex's own view of this thread, that the features this call turns off are off; anything
        unconfirmed stops the call with no task submitted."""
        page = result.get("data") if isinstance(result, dict) else None
        cursor = result.get("nextCursor") if isinstance(result, dict) else None
        readable = (isinstance(page, list) and all(isinstance(feature, dict) for feature in page)
                    and (cursor is None or isinstance(cursor, str) and bool(cursor)))
        if readable:
            for feature in page:
                name, enabled = feature.get("name"), feature.get("enabled")
                if isinstance(name, str):
                    # A name listed twice with different values confirms nothing.
                    self.features[name] = enabled if self.features.get(name, enabled) == enabled else None
            if cursor is not None and self.feature_pages < _FEATURE_PAGES:
                return self.list_features(cursor)
        modes = self.envelope.get("provider_plugins") or {}
        off = _ALWAYS_OFF + tuple(name for name, key in (("plugins", "mode"), ("apps", "apps_mode"))
                                  if modes.get(key) != "allowed")
        state = {name: self.features.get(name) for name in off}
        self.envelope["provider_features"] = state
        # Only a list read to its end confirms anything: a later page could still turn a feature on.
        complete = readable and cursor is None
        unconfirmed = [name for name, enabled in state.items() if enabled is not False or not complete]
        if unconfirmed:
            raise _ProtocolError(
                f"Codex didn't confirm that {', '.join(unconfirmed)} {'is' if len(unconfirmed) == 1 else 'are'} off for "
                "this thread, which this call requires; no task was submitted. Next: check the Codex version and "
                "configuration, or inspect retained output.")
        self.list_mcp(None)

    def list_mcp(self, cursor):
        self.mcp_pages += 1
        self.request("mcpServerStatus/list", {"threadId": self.session, "detail": "toolsAndAuthOnly", "limit": _MCP_STATUS_PAGE_SIZE,
                                              **({"cursor": cursor} if cursor is not None else {})})

    def mcp_listed(self, result):
        """Record the MCP servers this thread runs, before its task goes out. A server the call didn't allow, or a
        list that can't be read to its end, stops the call with no task submitted."""
        plugins = self.envelope.setdefault("provider_plugins", {"mode": "off", "apps_mode": "off"})
        page = result.get("data") if isinstance(result, dict) else None
        cursor = result.get("nextCursor") if isinstance(result, dict) else None
        readable = (isinstance(page, list) and len(page) <= _MCP_STATUS_PAGE_SIZE
                    and all(isinstance(server, dict) for server in page)
                    and (cursor is None or isinstance(cursor, str) and bool(cursor)))
        if readable:
            self.mcp_read += 1
            # Exact values are the identity; labels are only for display.
            self.mcp_servers.extend((server.get("name"), server.get("pluginId")) for server in page)
            if cursor is not None and self.mcp_pages < _MCP_STATUS_PAGES:
                return self.list_mcp(cursor)
        complete = readable and cursor is None
        observed = self.mcp_read > 0
        kinds = [(name, plugin, _server_kind(name, plugin)) for name, plugin in self.mcp_servers]
        names = sorted(_label(name) for name, _, _ in kinds)
        plugins.update(
            source="codex_mcp_server_status" if observed else "unavailable", complete=complete,
            server_count=len(names) if observed else None, mcp_servers=names[:50] if observed else None,
            plugin_servers=[{"name": _label(name), "plugin": _label(plugin)} for name, plugin, kind in kinds
                            if kind == "plugin"][:50] if observed else None,
            apps=any(kind == "apps" for _, _, kind in kinds) if observed else None)
        if self.early_activity:
            plugins["activity_before_list"] = [f"{_label(name)}: {status}" for name, status in self.early_activity][:20]
        held = (f" Codex also reported activity before the list ({', '.join(plugins['activity_before_list'][:5])})."
                if self.early_activity else "")
        # Only an explicit opt-in admits a server: --allow-plugins one a plugin provides, --allow-apps the apps' one
        # server. Names must be unique, since later activity is known only by name.
        seen = collections.Counter(name for name, _, kind in kinds if kind != "malformed")
        refused = []
        for name, _, kind in kinds:
            # Malformed names may be arrays or objects; never hash them, even to refuse them.
            if kind == "malformed":
                refused.append((name, kind))
            elif seen[name] > 1:
                refused.append((name, "duplicate"))
            elif not self.admits(kind):
                refused.append((name, kind))
        if refused:
            shown = sorted({_label(name) for name, _ in refused})
            raise _ProtocolError(
                f"Codex runs {'' if complete else 'at least '}{len(refused)} MCP server(s) this call didn't allow "
                f"({', '.join(shown[:5])}); no task was submitted.{held} Next: "
                f"{_remedy({kind for _, kind in refused})}, or inspect retained output.")
        if not complete:
            if readable:
                why = f"it has more than {_MCP_STATUS_PAGES} pages"
            elif result is None and self.mcp_pages == 1:
                why = "it is unavailable"
            else:
                why = f"page {self.mcp_pages} " + ("failed" if result is None else "is unreadable")
            raise _ProtocolError(
                f"Codex's list of MCP servers couldn't be read to its end ({why}), so this call can't show what runs; "
                f"no task was submitted.{held} Next: inspect retained output.")
        self.admitted = {name for name, _, _ in kinds}
        for name, status in self.early_activity:
            self.mcp_started({"name": name, "status": status})
        self.submit_turn()

    def admits(self, kind):
        modes = self.envelope.get("provider_plugins") or {}
        return (kind == "plugin" and modes.get("mode") == "allowed"
                or kind == "apps" and modes.get("apps_mode") == "allowed")

    def mcp_started(self, params):
        """MCP server activity is allowed only for a server this call admitted. Anything else ends the call at once,
        whatever thread it names: this app-server is the call's own. An answer already recorded is withdrawn."""
        name, status = params.get("name"), _label(params.get("status"))
        modes = self.envelope.get("provider_plugins") or {}
        if self.admitted is None and "allowed" in (modes.get("mode"), modes.get("apps_mode")):
            # An allowed server may start before the list says which it is; judge it once the list is read.
            if len(self.early_activity) >= _MAX_PENDING:
                self.end_call("Codex's MCP activity before its server list exceeded the observation bound")
            # The listing can establish plugin provenance, but cannot make an
            # invalid identity or a disabled kind eligible. Stop those now,
            # even if the pending listing never responds.
            kind = "apps" if name == _APPS_SERVER else "plugin"
            if not _identity(name) or not self.admits(kind):
                self.end_call(f"Codex reported MCP server activity ({_label(name)}: {status}) "
                              "for a server this call didn't allow")
            self.early_activity.append((name, status))
            return
        if self.admitted is not None and isinstance(name, str) and name in self.admitted:
            return
        name = _label(name)
        self.end_call(f"Codex reported MCP server activity ({name}: {status}) for a server this call didn't allow")

    def end_call(self, what, remedy=None):
        """A boundary the call set was crossed: withdraw any recorded answer and stop the call's own process group at
        once, wherever this is read, during the turn or while its server shuts down. The group may outlive its
        leader."""
        submitted = {"requested": "its task may already have been sent",
                     "accepted": "Codex had already accepted its task"}.get(self.envelope.get("task_submission"),
                                                                            "no task was submitted")
        self.problem(what)
        raise _ProtocolError(f"{what}; the call was ended and {submitted}. Next: "
                             + (f"{remedy}, or inspect retained output." if remedy else "inspect retained output."))

    def submit_turn(self):
        turn, self.waiting_turn = self.waiting_turn, None
        self.turn_requested = True
        self.envelope["task_submission"] = "requested"
        self.request("turn/start", turn)

    def start_thread(self):
        params = {"cwd": self.repo, **_NO_ESCALATION}
        # A resumed thread keeps its saved permission profile unless the request names this call's own.
        profile = (self.envelope.get("read_scope") or {}).get("profile")
        if profile:
            params["config"] = {"default_permissions": profile}
        if self.resume:
            params["threadId"] = self.resume
        self.request("thread/resume" if self.resume else "thread/start", params)

    def hooks_ready(self, result):
        readiness = hook_readiness(result, self.repo, self.expected_hook, self.hook_file)
        self.envelope["hook_readiness"] = readiness
        if readiness["state"] != "ready":
            problem, action = hook_remedy(readiness, self.repo, self.expected_hook, self.hook_file)
            raise _ProtocolError(problem + "; no task was submitted. " + action
                                 + " A peer call never changes native hook trust.")

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(self.process.args, self.timeout)
        return remaining

    def send(self, value):
        if self.observation_only:
            return
        body = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        if self.stdin_closed or len(self.outgoing) + len(body) > _MAX_OUTPUT:
            raise _ProtocolError("Native input is unavailable or exceeded its bound; inspect retained evidence.")
        self.outgoing.extend(body)

    def request(self, method, params, control_id=None):
        if self.observation_only:
            return
        if len(self.pending) >= _MAX_PENDING:
            raise _ProtocolError("Too many unresolved native requests; inspect retained evidence before continuing.")
        identifier = self.next_id
        self.next_id += 1
        self.pending[identifier] = (method, control_id)
        self.send({"id": identifier, "method": method, "params": params})
        return identifier

    def observe(self, source, settings):
        """Replace each setting a native report covers; an invalid value clears the claim."""
        if "model" in settings:
            model = settings["model"]
            self.envelope["model_observation"] = (
                {"source": source, "reported_model": model,
                 "relation": setting_relation(self.envelope.get("requested_model"), model)}
                if _identity(model) else {"source": "unavailable", "reported_model": None, "relation": "unknown"})
        if "effort" in settings:
            effort = settings["effort"]
            self.envelope["effective_effort"] = effort if _identity(effort) else "unknown"

    def detail(self, collection, value):
        if len(collection) < _MAX_DETAILS:
            collection.append(value)
        else:
            self.details_truncated = True

    def interrupt(self):
        if self.turn and self.terminal is None and not self.interrupt_requested:
            self.interrupt_requested = True
            self.request("turn/interrupt", {"threadId": self.session, "turnId": self.turn})

    def server_request(self, message):
        method, identifier = message["method"], message["id"]
        params = message.get("params")
        if not isinstance(params, dict):
            raise _ProtocolError("Malformed native server request; inspect retained output.")
        if self.observation_only:
            self.detail(self.unknown_requests, method[:200])
            return
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            result = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None}
        elif method in ("execCommandApproval", "applyPatchApproval"):
            result = {"decision": "abort"}
        else:
            self.detail(self.unknown_requests, method[:200])
            self.send({"id": identifier, "error": {
                "code": -32601, "message": "This unattended client cannot answer the native request."}})
            self.interrupt()
            return
        item = params.get("itemId")
        self.detail(self.denials, {"tool_name": method,
                                 "tool_use_id": item if _identity(item) else None})
        self.send({"id": identifier, "result": result})

    def response(self, message):
        identifier = message["id"]
        if identifier in self.abandoned:
            # The call already proceeded without this model list.
            self.abandoned.discard(identifier)
            return
        if identifier not in self.pending:
            raise _ProtocolError("Unmatched or malformed native response; inspect retained output.")
        method, control_id = self.pending[identifier]
        if method == "model/list":
            # A rejected or unusable list leaves the settings unverified; it never stops the call.
            self.pending.pop(identifier)
            self.listed(None if "error" in message else message.get("result"))
            return
        if method == "experimentalFeature/list":
            self.pending.pop(identifier)
            self.features_listed(None if "error" in message else message.get("result"))
            return
        if method == "mcpServerStatus/list":
            # A Codex without this listing leaves what runs unobserved (not "none"); with plugins off that refuses.
            self.pending.pop(identifier)
            self.mcp_listed(None if "error" in message else message.get("result"))
            return
        if ("result" in message) == ("error" in message):
            raise _ProtocolError("Unmatched or malformed native response; inspect retained output.")
        if "error" in message:
            error = message["error"]
            if (not isinstance(error, dict) or type(error.get("code")) is not int
                    or not isinstance(error.get("message"), str)):
                raise _ProtocolError("Malformed native request rejection; inspect retained output.")
            if control_id is not None:
                self.control.resolve(control_id, "rejected", "Native provider rejected the exact steering request.")
                self.pending.pop(identifier)
                return
            self.detail(self.errors, error["message"][:2000])
            if method == "thread/resume" and "active writer" in error["message"]:
                raise _ProtocolError("Conversation " + self.resume + " is open in another Codex app, which holds it, so "
                                     "this call cannot resume it. Reach it there with `multithread bind` and `multithread "
                                     "wake`, or start a fresh peer session with a summary of what it needs.")
            raise _ProtocolError("Native provider rejected " + method + "; inspect retained output before continuing.")
        result = message["result"]
        if not isinstance(result, dict):
            raise _ProtocolError("Malformed native result; inspect retained output.")
        if method == "initialize":
            self.envelope["provider_version"] = provider_version_observation(
                result.get("userAgent"), "codex_initialize_user_agent")
            self.send({"method": "initialized", "params": {}})
            if self.expected_hook is not None:
                self.request("hooks/list", {"cwds": [self.repo]})
            else:
                self.after_readiness()
        elif method == "hooks/list":
            self.hooks_ready(result)
            self.after_readiness()
        elif method in ("thread/start", "thread/resume"):
            thread = result.get("thread")
            if not isinstance(thread, dict) or not _identity(thread.get("id")):
                raise _ProtocolError("Native provider returned no valid thread identity; inspect retained output.")
            if self.resume and thread["id"] != self.resume:
                self.envelope["observed_session_id"] = thread["id"]
                raise _ProtocolError("Returned thread identity does not match the requested peer; no task was submitted.")
            if ("cwd" in thread and thread["cwd"] != self.repo
                    or "cwd" in result and result["cwd"] != self.repo):
                raise _ProtocolError("Native thread working directory does not match the enrolled checkout; no task was submitted.")
            self.session = thread["id"]
            self.envelope["session_id"] = self.session
            status = thread.get("status")
            history = thread.get("turns", [])
            if (status is not None and (not isinstance(status, dict)
                                       or status.get("type") not in ("idle", "notLoaded", "systemError", "active"))
                    or not isinstance(history, list) or any(not isinstance(item, dict) for item in history)):
                raise _ProtocolError("Native provider returned an invalid thread state; no task was submitted.")
            if ((isinstance(status, dict) and status["type"] in ("active", "systemError"))
                    or any(item.get("status") == "inProgress" for item in history)):
                raise _ProtocolError("The native thread is active or unavailable; no task was submitted into an existing turn.")
            native_session = thread.get("sessionId")
            if native_session is not None:
                if not _identity(native_session):
                    raise _ProtocolError("Native provider returned an invalid session-tree identity.")
                self.envelope["native_session_id"] = native_session
            reviewer = result.get("approvalsReviewer")
            if reviewer in ("user", "auto_review", "guardian_subagent"):
                self.envelope["native_approvals_reviewer"] = reviewer
            # A peer never escalates out of its sandbox: no approval is asked for, and none could be granted by a
            # reviewer other than this client, which declines. Codex must report both before the task goes out.
            policy = result.get("approvalPolicy")
            self.envelope["native_approval_policy"] = policy if policy in _APPROVAL_POLICIES else None
            if policy != "never" or reviewer != "user":
                raise _ProtocolError(
                    f"Codex reports approval policy {_label(policy)} with reviewer {_label(reviewer)} for this thread; "
                    "a peer call requires 'never' and 'user', so nothing can escalate out of its sandbox. No task was "
                    "submitted. Next: check the Codex version and configuration, or inspect retained output.")
            # The sandbox is exactly this call's read-only profile: a checkout's own configuration can't widen it.
            scope = self.envelope.get("read_scope")
            if isinstance(scope, dict):
                expected = {"id": scope.get("profile"), "extends": None}
                if (result.get("sandbox") != {"type": "readOnly", "networkAccess": False}
                        or result.get("activePermissionProfile") != expected):
                    raise _ProtocolError(
                        "Codex reports a sandbox other than this call's read-only, offline profile for this thread; no "
                        "task was submitted. Next: check for a .codex configuration in the checkout, or inspect "
                        "retained output.")
            # The same turn request carries any override on a new or resumed
            # thread. The opened thread's settings describe only what it keeps.
            requested = {"model": self.envelope.get("requested_model"),
                         "effort": self.envelope.get("requested_effort")}
            reported = {"model": result.get("model"), "effort": result.get("reasoningEffort")}
            self.observe("codex_" + method.replace("/", "_"),
                         {key: value for key, value in reported.items() if requested[key] is None})
            if (requested["model"] is None) != (requested["effort"] is None):
                # The call names one setting; the turn keeps this thread's value of the other.
                kept = "resumed_thread" if self.resume else "new_thread"
                (model, model_source), (effort, effort_source) = (
                    (requested[key], "requested") if requested[key] is not None
                    else (reported[key] if _identity(reported[key]) else None, kept)
                    for key in ("model", "effort"))
                self.check_settings(model, model_source, effort, effort_source)
            turn = {"threadId": self.session, "input": [{"type": "text", "text": self.task}], **_NO_ESCALATION}
            turn.update((key, value) for key, value in requested.items() if value is not None)
            self.waiting_turn = turn
            self.list_features(None)  # what the thread may do is confirmed before any task goes out
        elif method == "turn/start":
            turn = self.validate_turn(result.get("turn"))
            self.turn = turn["id"]
            self.envelope["turn_id"] = self.turn
            self.envelope["task_submission"] = "accepted"
            buffered, self.buffered = self.buffered, []
            for notification in buffered:
                self.notification(notification)
            if self.control is not None and self.terminal is None and not self.observation_only:
                self.control.set_target(self.session, self.turn)
            if self.unknown_requests:
                self.interrupt()
        elif method == "turn/steer":
            if result.get("turnId") != self.turn:
                if control_id is not None:
                    self.control.resolve(control_id, "uncertain", "Native steering response identified a different turn.")
                    self.pending.pop(identifier)
                raise _ProtocolError("Native steering response did not match the targeted turn.")
            if control_id is not None:
                self.control.resolve(control_id, "accepted", "Native provider accepted input into the exact active turn; consumption is not verified.")
        elif method == "turn/interrupt" and result:
            raise _ProtocolError("Malformed native interrupt acknowledgement; inspect retained output.")
        self.pending.pop(identifier)

    @staticmethod
    def validate_turn(turn):
        if (not isinstance(turn, dict) or not _identity(turn.get("id"))
                or turn.get("status") not in ("inProgress", "completed", "failed", "interrupted")
                or not isinstance(turn.get("items"), list)):
            raise _ProtocolError("Malformed native turn; inspect retained output.")
        return turn

    def item(self, item):
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            return
        if (not _identity(item.get("id")) or not isinstance(item.get("text"), str)
                or item.get("phase") not in (None, "commentary", "final_answer")):
            raise _ProtocolError("Malformed native agent message; inspect retained output.")
        self.messages.pop(item["id"], None)
        self.messages[item["id"]] = {"text": item["text"], "phase": item.get("phase"), "complete": True}

    def notification(self, message):
        method, params = message["method"], message.get("params")
        if not isinstance(params, dict):
            raise _ProtocolError("Malformed native notification; inspect retained output.")
        if method == "mcpServer/startupStatus/updated":
            return self.mcp_started(params)
        if method == "thread/settings/updated":
            # Escalation must stay off for the whole call, on any thread this app-server runs.
            settings = params.get("threadSettings")
            if not isinstance(settings, dict):
                return self.end_call("Codex reported unreadable thread security settings")
            drifted = {key: settings[key] for key, safe in (("approvalPolicy", "never"), ("approvalsReviewer", "user"))
                       if key in settings and settings[key] != safe}
            profile = (self.envelope.get("read_scope") or {}).get("profile")
            if profile and "activePermissionProfile" in settings and settings["activePermissionProfile"] != {
                    "id": profile, "extends": None}:
                drifted["activePermissionProfile"] = settings["activePermissionProfile"]
            sandbox = settings.get("sandboxPolicy")
            if profile and "sandboxPolicy" in settings and (not isinstance(sandbox, dict)
                    or sandbox.get("type") != "readOnly" or sandbox.get("networkAccess", False) is not False):
                drifted["sandboxPolicy"] = sandbox
            if drifted:
                self.envelope.update(native_approval_policy=settings.get("approvalPolicy")
                                     if settings.get("approvalPolicy") in _APPROVAL_POLICIES else None,
                                     native_approvals_reviewer=settings.get("approvalsReviewer")
                                     if settings.get("approvalsReviewer") in ("user", "auto_review", "guardian_subagent")
                                     else None)
                return self.end_call("Codex changed this call's approval settings ("
                                     + ", ".join(f"{key} {_label(value) if isinstance(value, str) else '(structured)'}"
                                                 for key, value in drifted.items())
                                     + ") although a peer call keeps escalation off")
        if "autoApprovalReview" in method or method.lower().startswith("guardian"):
            # Escalation is off; a review of one means a command asked to leave the sandbox anyway.
            return self.end_call(f"Codex started an approval review ({_label(method)}) although this call turned "
                                 "escalation off")
        relevant = ("turn/started", "turn/completed", "item/completed", "item/agentMessage/delta",
                    "thread/tokenUsage/updated", "error", "thread/settings/updated", "model/rerouted")
        if method not in relevant:
            return
        if params.get("threadId") != self.session:
            return
        if method == "thread/settings/updated":
            # Thread settings carry no turn. One sent before Codex accepted this
            # call's turn may predate its override, so only a later one counts.
            if self.turn is not None:
                settings = params["threadSettings"]
                self.observe("codex_thread_settings", {"model": settings.get("model"),
                                                       "effort": settings.get("effort")})
            return
        if self.turn is None:
            if self.turn_requested:
                self.buffered.append(message)
            return
        if method in ("turn/started", "turn/completed"):
            turn = self.validate_turn(params.get("turn"))
            if turn["id"] != self.turn:
                return
            if method == "turn/completed":
                if turn["status"] == "inProgress" or self.terminal is not None:
                    raise _ProtocolError("Inconsistent native turn completion; inspect retained output.")
                error = turn.get("error")
                if turn["status"] == "failed":
                    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                        raise _ProtocolError("Native failed turn had no valid failure description.")
                    self.detail(self.errors, error["message"][:2000])
                elif error is not None:
                    raise _ProtocolError("Native turn status contradicted its failure description.")
                for item in turn["items"]:
                    self.item(item)
                self.terminal = turn["status"]
                self.envelope["provider_duration_ms"] = measurement_number(
                    turn.get("durationMs"), self.envelope, "provider_duration_ms", integer=True)
                self.finish(require_answer=False)
                if self.control is not None:
                    self.control.stop_accepting("The native turn has completed.")
            return
        if params.get("turnId") != self.turn:
            return
        if method == "item/completed":
            self.item(params.get("item"))
            if self.terminal is not None:
                self.finish(require_answer=False)
        elif method == "item/agentMessage/delta":
            identifier, delta = params.get("itemId"), params.get("delta")
            if not _identity(identifier) or not isinstance(delta, str):
                raise _ProtocolError("Malformed native text delta; inspect retained output.")
            item = self.messages.setdefault(identifier, {"text": "", "phase": None, "complete": False})
            if not item["complete"]:
                item["text"] += delta
        elif method == "error":
            error = params.get("error")
            if (not isinstance(error, dict) or not isinstance(error.get("message"), str)
                    or type(params.get("willRetry")) is not bool):
                raise _ProtocolError("Malformed native error observation; inspect retained output.")
            self.detail(self.errors, error["message"][:2000])
        elif method == "model/rerouted":
            self.observe("codex_model_rerouted", {"model": params.get("toModel")})
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage")
            names = ("inputTokens", "outputTokens", "cachedInputTokens", "cacheWriteInputTokens",
                     "reasoningOutputTokens", "totalTokens")
            replace_measurement_errors(self.envelope, {}, "usage", "model_context_window")
            if not isinstance(usage, dict):
                measurement_error(self.envelope, "usage")
                self.envelope["usage"] = None
                self.envelope["model_context_window"] = None
            else:
                counters = {}
                for part in ("last", "total"):
                    values = measurement_fields(usage.get(part), self.envelope,
                                                "usage." + part, names, missing=True)
                    counters[part] = None if values is None else {name: values[name] for name in names}
                self.envelope["usage"] = counters
                self.envelope["model_context_window"] = measurement_number(
                    usage.get("modelContextWindow"), self.envelope, "model_context_window", integer=True)
            measurement_scope(self.envelope, "usage_scope", "native_thread_last_and_total", USAGE_SCOPES)

    def retain(self, body):
        """Retain an image record by reference when its payload is byte-identical to the saved file.

        Anything unverified stays inline and counts toward the output bound.
        """
        if len(body) < 1024 or b'"imageGeneration"' not in body:
            return body
        try:
            value = decode(body)
        except _ProtocolError:
            return body
        params = value.get("params")
        if not isinstance(params, dict):
            return body
        items = [params.get("item")]
        turn = params.get("turn")
        if isinstance(turn, dict) and isinstance(turn.get("items"), list):
            items.extend(turn["items"])
        referenced = False
        for item in items:
            if (not isinstance(item, dict) or item.get("type") != "imageGeneration"
                    or not isinstance(item.get("result"), str) or not item["result"]):
                continue
            try:
                payload = base64.b64decode(item["result"], validate=True)
            except (binascii.Error, ValueError):
                continue
            digest = hashlib.sha256(payload).hexdigest()
            saved = _saved_file(item.get("savedPath"), digest, len(payload))
            self.image(item, digest, len(payload), saved)
            if saved == "matches":
                item["result"] = {"omitted": "image_base64", "sha256": digest, "bytes": len(payload)}
                referenced = True
        if not referenced:
            return body
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def image(self, item, digest, size, saved):
        images = self.envelope.setdefault("generated_images", [])
        identifier = item.get("id") if _identity(item.get("id")) else None
        if any(image["item_id"] == identifier and image["sha256"] == digest for image in images):
            return
        if len(images) >= _MAX_IMAGES:
            self.envelope["generated_images_truncated"] = True
            return
        images.append({"item_id": identifier, "sha256": digest, "bytes": size,
                       "saved_path": item["savedPath"] if saved != "not_reported" else None,
                       "saved_file": saved, "retained": "reference" if saved == "matches" else "inline"})

    def message(self, body):
        value = decode(body)
        if "id" in value and (type(value["id"]) not in (int, str)
                              or isinstance(value["id"], str) and not _identity(value["id"])):
            raise _ProtocolError("Native record has an invalid request identity.")
        if "method" in value:
            if not isinstance(value["method"], str) or "result" in value or "error" in value:
                raise _ProtocolError("Malformed native request or notification.")
            if "id" in value:
                self.server_request(value)
            else:
                self.notification(value)
        elif "id" in value:
            self.response(value)
        else:
            raise _ProtocolError("Unrecognized native JSONL record; inspect retained output.")

    def poll_control(self):
        if self.control is None or self.turn is None or self.terminal is not None or self.observation_only:
            return
        for request in self.control.pending():
            identifier = request.get("request_id")
            if not _identity(identifier) or identifier in self.seen_controls:
                raise _ProtocolError("Invalid or repeated steering control request; no duplicate input was submitted.")
            if len(self.seen_controls) >= 4096:
                raise _ProtocolError("Steering request bound reached; inspect retained evidence.")
            self.seen_controls.add(identifier)
            if (self.terminal is not None or request.get("session_id") != self.session
                    or request.get("turn_id") != self.turn):
                self.control.resolve(identifier, "rejected", "The requested native turn is not active on this call.")
                continue
            text = request.get("text")
            if not isinstance(text, str) or not text.strip() or "\0" in text or len(text.encode("utf-8")) > 64 * 1024:
                self.control.resolve(identifier, "rejected", "Steering requires bounded nonempty UTF-8 text.")
                continue
            self.request("turn/steer", {"threadId": self.session, "expectedTurnId": self.turn,
                                        "input": [{"type": "text", "text": text}]}, identifier)

    def close_stdin(self):
        if not self.stdin_closed:
            self.stdin_closed = True
            self.process.stdin.close()
            if self.capture is not None:
                self.capture.event("stdin_closed")

    def preserve(self, resolve_pending=True):
        self.envelope["permission_denials"] = self.denials
        self.envelope["unsupported_native_requests"] = self.unknown_requests
        self.envelope["provider_errors"] = self.errors
        self.envelope["provider_errors_truncated"] = self.details_truncated
        if self.envelope.get("state") != "returned":
            partial = "\n\n".join(item["text"] for item in self.messages.values() if item["text"])
            if partial:
                self.envelope["partial_result"] = partial
        for method, identifier in self.pending.values():
            if resolve_pending and identifier is not None:
                self.control.resolve(identifier, "uncertain", "Native steering acknowledgement was not observed; do not resend automatically.")

    def problem(self, message, *, observation_lost=True):
        # A Codex peer enforces its boundary throughout shutdown, too. Once interpretation is lost,
        # a previously completed turn no longer proves that boundary held for the whole call.
        self.had_problem = True
        self.observation_lost = self.observation_lost or observation_lost
        if self.observation_lost or not self.outcome_recorded:
            self.envelope.update(state="uncertain", result=None)
        self.envelope.update(needs_attention=True, message=message)
        if observation_lost and getattr(self.process, "_owned_group_retired", False) is not True:
            # Reaping the leader says nothing about descendants in its owned group.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def finish(self, require_answer=True):
        if self.observation_lost:
            return
        if self.terminal is None:
            raise _ProtocolError("Native output ended without a matching terminal turn; work may have occurred.")
        self.envelope.update(provider_subtype=self.terminal,
                             provider_is_error=self.terminal != "completed",
                             terminal_reason=self.terminal, provider_turns=1)
        if self.terminal in ("failed", "interrupted"):
            self.envelope.update(state="provider_error", needs_attention=True,
                                 message="The native turn failed or was interrupted; inspect retained output and Multithread evidence.")
            self.outcome_recorded = True
            return
        finals = [item["text"] for item in self.messages.values()
                  if item["complete"] and item["phase"] == "final_answer"]
        if not finals:
            finals = [item["text"] for item in self.messages.values()
                      if item["complete"] and item["phase"] is None]
            # Older providers omit phase on progress as well as the answer.
            # The last completed agent message is the compatibility candidate.
            finals = finals[-1:]
        if not finals:
            if not require_answer:
                return
            raise _ProtocolError("The native turn completed without a validated final answer; partial text is retained.")
        self.envelope.update(state="returned", result="\n\n".join(finals),
                             needs_attention=bool(self.denials or self.unknown_requests or self.had_problem),
                             message="Assess the answer and durable Multithread evidence; a returned turn is not workflow completion.")
        self.outcome_recorded = True


def run(process, task: bytes, repo: str, resume: str | None, directory: Path,
        envelope: dict, timeout: float, control=None, observer=None, expected_hook=None, feedback=None,
        hook_file=None) -> None:
    """Observe a native terminal turn separately from the caller's cleanup."""
    driver = _Driver(process, task, repo, resume, envelope, timeout, control, expected_hook, hook_file)
    owned_observer = observer is None
    observation = observer if observer is not None else Observation(process, directory, envelope)
    observation.driver = driver
    observation.retain = driver.retain
    driver.capture = getattr(observation, "capture", None)
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            writing = False
            driver.request("initialize", {"clientInfo": _CLIENT_INFO})
            while not observation.eof:
                if feedback is not None:
                    feedback()
                driver.poll_control()
                driver.poll_listing()
                if driver.outgoing and not writing:
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                    writing = True
                elif not driver.outgoing and writing:
                    selector.unregister(process.stdin)
                    writing = False
                if driver.terminal and not driver.pending and not driver.outgoing:
                    driver.close_stdin()
                    driver.finish()
                    return
                for key, ready in selector.select(min(0.1, driver.remaining())):
                    if key.data == "stdin":
                        try:
                            pending = (driver.outgoing[:driver.capture.MAX_CHUNK]
                                       if driver.capture is not None else driver.outgoing)
                            count = os.write(process.stdin.fileno(), pending)
                        except BlockingIOError:
                            continue
                        if count <= 0:
                            raise _ProtocolError("Native input closed before a request was delivered; inspect retained output.")
                        if driver.capture is not None:
                            driver.capture.bytes("stdin", pending[:count])
                        del driver.outgoing[:count]
                        continue
                    observation.read()
            if driver.pending or driver.outgoing:
                if (driver.outcome_recorded and not driver.outgoing and all(
                        method == "turn/steer" and control_id is not None
                        for method, control_id in driver.pending.values())):
                    # EOF is observed and the completed answer is validated. A missing steering
                    # acknowledgement leaves that input uncertain, without losing stream interpretation.
                    driver.problem("Native output ended without steering acknowledgements; inspect retained input receipts.",
                                   observation_lost=False)
                else:
                    raise _ProtocolError("Native output ended with unresolved requests; their outcomes are uncertain.")
            driver.close_stdin()
            driver.finish()
    except _ProtocolError as exc:
        observation.fault(str(exc))
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        # Cancellation does not disable interpretation: bounded shutdown output is still checked.
        driver.problem("Native observation was interrupted; preserve partial work and inspect evidence before retrying.",
                       observation_lost=False)
        raise
    except OSError:
        observation.fault("Native observation was unavailable; preserve partial work and inspect evidence before retrying.")
        raise
    finally:
        driver.observation_only = True
        if driver.capture is not None and (driver.pending or driver.outgoing or driver.had_problem):
            driver.capture.fault()
        try:
            observation.snapshot()
        finally:
            driver.close_stdin()
            if owned_observer:
                observation.close()
