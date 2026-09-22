"""Bounded native Claude streaming transport; no provider policy overrides.

The caller owns the process, its argv and its cleanup. This module owns only
its JSONL frames, private stdout observation and native result interpretation.
A native echo means the input was received, never that it was answered, and a
native consumption observation never substitutes for a durable Multithread
acknowledgement or for workflow completion.
"""

import json
import os
from pathlib import Path
import selectors
import subprocess
import time
import uuid as uuid_module

from .native_io import MAX_OUTPUT, Observation, ProtocolError, decode, identity
from .native_io import (USAGE_SCOPES, MODEL_USAGE_SCOPES, COST_SCOPES,
                        claude_measurements, measurement_scope, replace_measurement_errors,
                        provider_version_observation)


_MAX_INPUTS = 128
_MAX_DETAILS = 8
_MAX_RESULTS = 16
_MAX_UUIDS = 64
_MAX_TEXT = 64 * 1024
_MAX_PARTIAL = 64 * 1024
_MAX_CONTROLS = 4096
_ERROR_SUBTYPES = ("error_max_turns", "error_during_execution", "error_max_budget_usd",
                   "error_max_structured_output_retries")


def _canonical(value):
    """Each submitted frame needs a canonical UUID the provider can echo back."""
    try:
        return isinstance(value, str) and str(uuid_module.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


class _Driver:
    def __init__(self, process, task, repo, resume, envelope, timeout, control):
        self.process = process
        self.task = task.decode("utf-8")
        self.repo = repo
        self.envelope = envelope
        self.requested = envelope.get("requested_session_id") or resume
        self.deadline = time.monotonic() + timeout
        self.timeout = timeout
        self.control = control
        self.session = None
        self.initial = None
        self.outgoing = bytearray()
        self.queued = 0
        self.flushed = 0
        self.marks = []
        self.inputs = {}
        self.order = []
        self.results = []
        self.last_related = None
        self.failed_related = False
        self.results_truncated = False
        self.answer = None
        self.texts = []
        self.text_size = 0
        self.denials = []
        self.errors = []
        self.unknown_requests = []
        self.details_truncated = False
        self.subagent_frames = 0
        self.seen_controls = set()
        self.stdin_closed = False
        self.accepting = True
        self.observation_only = False
        self.outcome_recorded = False
        self.had_problem = False
        self.envelope.update(state="uncertain", requested_session_id=self.requested,
                             needs_attention=True)

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(self.process.args, self.timeout)
        return remaining

    def detail(self, collection, value):
        if len(collection) < _MAX_DETAILS:
            collection.append(value)
        else:
            self.details_truncated = True

    # --- submitted input -------------------------------------------------

    def start(self):
        if not identity(self.requested):
            raise ProtocolError("This call has no exact requested native session identity; no task was submitted.")
        identifier = str(uuid_module.uuid4())
        # Record the actual identity before any of its bytes can reach the pipe.
        self.envelope["initial_message_uuid"] = identifier
        self.initial = identifier
        self.submit(identifier, self.task, initial=True)
        # The queued task has not yet crossed the native stdin pipe.
        self.envelope["task_delivery"] = "in_progress"

    def submit(self, identifier, text, initial=False):
        frame = {"type": "user", "uuid": identifier, "session_id": self.requested,
                 "message": {"role": "user", "content": text}, "parent_tool_use_id": None}
        body = (json.dumps(frame, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        if self.stdin_closed or self.observation_only or self.queued + len(body) > MAX_OUTPUT:
            raise ProtocolError("Native input is unavailable or exceeded its bound; inspect retained evidence.")
        self.inputs[identifier] = {"uuid": identifier, "kind": "task" if initial else "session_input",
                                   "bytes": len(body), "written": False, "echoed": False,
                                   "consumption": "unobserved", "result_covered": False,
                                   "receipt": None}
        self.order.append(identifier)
        self.outgoing.extend(body)
        self.queued += len(body)
        self.marks.append((self.queued, identifier))

    def wrote(self, count):
        """Delivery is what the pipe accepted, not what this call intended to send."""
        self.flushed += count
        while self.marks and self.marks[0][0] <= self.flushed:
            self.inputs[self.marks.pop(0)[1]]["written"] = True
        if self.inputs[self.initial]["written"]:
            self.envelope["task_delivery"] = "written"

    def unwritable(self):
        """Undeliverable bytes stay undelivered; a later result never covers them."""
        self.envelope["native_input_write_error"] = "the native input pipe closed with unwritten bytes"
        if not self.inputs[self.initial]["written"]:
            self.envelope["task_delivery"] = "uncertain"
        self.marks.clear()
        del self.outgoing[:]
        self.close_stdin()
        self.stop_input("The native input pipe closed; no further input can be dispatched.")

    def receipt(self, identifier, state, detail):
        record = self.inputs[identifier]
        if record["kind"] != "session_input" or record["receipt"] is not None:
            return
        if self.control is not None:
            self.control.resolve(identifier, state, detail)
        record["receipt"] = state

    def echo(self, identifier):
        record = self.inputs.get(identifier)
        if record is not None:
            record["echoed"] = True

    def answered(self, uuids, persist=True):
        for identifier in uuids:
            record = self.inputs.get(identifier)
            if record is None:
                continue
            record["consumption"] = "consumed"
        if persist:
            for identifier in uuids:
                if identifier in self.inputs:
                    self.receipt(identifier, "consumed", "A native main-session frame named this input as answered in a turn; consumption is not task completion.")

    def cover(self, uuids):
        """Only named inputs are covered by a result, including the initial task."""
        for identifier in self.order:
            record = self.inputs[identifier]
            if identifier in uuids:
                record["result_covered"] = True

    def settled(self):
        return all(record["result_covered"] for record in self.inputs.values())

    def done(self):
        return self.last_related is not None and not self.outgoing and self.settled()

    def stop_input(self, reason):
        if self.accepting:
            self.accepting = False
            if self.control is not None:
                self.control.stop_accepting(reason)

    def poll_control(self):
        if self.control is None or self.session is None or not self.accepting or self.observation_only or self.stdin_closed:
            return
        for request in self.control.pending():
            identifier = request.get("request_id")
            if not _canonical(identifier) or identifier in self.seen_controls:
                raise ProtocolError("Invalid or repeated session input request; no duplicate input was submitted.")
            if len(self.seen_controls) >= _MAX_CONTROLS:
                raise ProtocolError("Session input request bound reached; inspect retained evidence.")
            self.seen_controls.add(identifier)
            if request.get("session_id") != self.session or request.get("turn_id") is not None:
                self.control.resolve(identifier, "rejected", "This call only accepts input addressed to its exact native session with no turn identity.")
                continue
            text = request.get("text")
            if not isinstance(text, str) or not text.strip() or "\0" in text or len(text.encode("utf-8")) > _MAX_TEXT:
                self.control.resolve(identifier, "rejected", "Session input requires bounded nonempty UTF-8 text.")
                continue
            if len(self.inputs) >= _MAX_INPUTS:
                self.control.resolve(identifier, "rejected", "This call reached its native input capacity.")
                continue
            self.submit(identifier, text)

    # --- native frames ---------------------------------------------------

    def uuids(self, value):
        single = value.get("user_message_uuid")
        listed = value.get("user_message_uuids")
        found = []
        if single is not None:
            if not identity(single):
                raise ProtocolError("Native frame carried an invalid answered-message identity.")
            found.append(single)
        if listed is not None:
            if (not isinstance(listed, list) or len(listed) > _MAX_UUIDS
                    or any(not identity(item) for item in listed)):
                raise ProtocolError("Native frame carried an invalid answered-message list.")
            if single is not None and single not in listed:
                raise ProtocolError("Native answered-message list omitted the message it names.")
            found.extend(item for item in listed if item != single)
        return found

    def attributed(self, value):
        """Only the requested main session answers this call's input."""
        if value.get("parent_tool_use_id") is not None:
            self.subagent_frames += 1
            return False
        session = value.get("session_id")
        if value.get("type") in ("assistant", "user", "result") and session is None:
            raise ProtocolError("Native main-session output omitted its session identity; inspect retained output.")
        if session is not None and not identity(session):
            raise ProtocolError("Native frame carried an invalid session identity.")
        if session is not None and session != self.requested:
            self.envelope["observed_session_id"] = session
            raise ProtocolError("A native frame reported a different session identity; inspect retained output without resuming either identity.")
        return True

    def initialization(self, value):
        first = self.session is None
        session = value.get("session_id")
        if not identity(session):
            raise ProtocolError("Native initialization carried no valid session identity; no task is confirmed delivered.")
        cwd = value.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or cwd != self.repo):
            raise ProtocolError("The native session working directory does not match the enrolled checkout.")
        # Always replace this observation: missing or unfamiliar metadata in a
        # repeated init must not inherit a version from an earlier init.
        self.envelope["provider_version"] = provider_version_observation(
            value.get("claude_code_version"), "claude_system_init")
        for name, key in (("model", "native_model"), ("claude_code_version", "native_version"),
                          ("permissionMode", "native_permission_mode")):
            if identity(value.get(name)):
                self.envelope[key] = value[name]
        self.session = session
        self.envelope["session_id"] = session
        # Streaming resume and native background turns can repeat init for the
        # same session. Validate identity/cwd every time; never reset the task,
        # its receipts or the closed input target. Raw frames retain settings
        # history. provider_version describes this init; legacy native settings
        # retain the latest supplied valid values.
        self.envelope["native_initialization_count"] = self.envelope.get("native_initialization_count", 0) + 1
        if first and self.control is not None and self.accepting and not self.observation_only:
            self.control.set_target(session, None)

    def system(self, value):
        subtype = value.get("subtype")
        if subtype == "init":
            self.initialization(value)
        elif subtype == "permission_denied":
            self.detail(self.denials, {"tool_name": value.get("tool_name") if identity(value.get("tool_name")) else None,
                                       "tool_use_id": value.get("tool_use_id") if identity(value.get("tool_use_id")) else None})

    def reply(self, value):
        message = value.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            raise ProtocolError("Malformed native assistant message; inspect retained output.")
        text = []
        for block in message["content"]:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise ProtocolError("Malformed native assistant content block; inspect retained output.")
            # Reasoning and tool blocks are progress, not the turn's answer.
            if block["type"] == "text" and isinstance(block.get("text"), str):
                text.append(block["text"])
        joined = "".join(text)
        if joined and self.text_size < _MAX_PARTIAL:
            kept = joined[:_MAX_PARTIAL - self.text_size]
            self.texts.append(kept)
            self.text_size += len(kept)
        self.answered(self.uuids(value))

    def result(self, value):
        if value.get("session_id") != self.requested or self.session != self.requested:
            raise ProtocolError("A native result lacks the initialized main session's exact identity; inspect retained output.")
        subtype, is_error = value.get("subtype"), value.get("is_error")
        text, errors = value.get("result"), value.get("errors", [])
        denials = value.get("permission_denials", [])
        success = subtype == "success"
        if (not isinstance(subtype, str) or type(is_error) is not bool
                or not identity(value.get("uuid"))
                or (success and not isinstance(text, str))
                or (not success and subtype not in _ERROR_SUBTYPES)):
            raise ProtocolError("Unsupported final native result; inspect retained output before continuing.")
        if (not isinstance(errors, list) or any(not isinstance(item, str) for item in errors)
                or not isinstance(denials, list) or any(not isinstance(item, dict) for item in denials)):
            raise ProtocolError("Unsupported result error or permission-denial shape; inspect retained output.")
        uuids = self.uuids(value)
        related = bool(set(uuids) & self.inputs.keys())
        for item in errors if related else []:
            self.detail(self.errors, item[:2000])
        for item in denials if related else []:
            self.detail(self.denials, {"tool_name": item.get("tool_name") if identity(item.get("tool_name")) else None,
                                       "tool_use_id": item.get("tool_use_id") if identity(item.get("tool_use_id")) else None})
        observation = {}
        measurements = claude_measurements(value, observation)
        origin = value.get("origin")
        origin_kind = origin.get("kind") if isinstance(origin, dict) else None
        record = {"uuid": value["uuid"], "subtype": subtype, "is_error": is_error,
                  "num_turns": measurements["provider_turns"], "answered": uuids, "related": related,
                  "origin_kind": origin_kind if identity(origin_kind) else None,
                  "stop_reason": value.get("stop_reason") if isinstance(value.get("stop_reason"), str) else None,
                  "terminal_reason": value.get("terminal_reason") if identity(value.get("terminal_reason")) else None,
                  "duration_ms": measurements["provider_duration_ms"],
                  "has_result_text": isinstance(text, str),
                  "result_excerpt": text[:2000] if isinstance(text, str) else None,
                  "result_excerpt_truncated": isinstance(text, str) and len(text) > 2000,
                  "usage_scope": "this native turn", "usage": measurements["usage"],
                  "cumulative_cost_usd": measurements["estimated_cost_usd"]}
        if observation.get("measurement_errors"):
            record["measurement_errors"] = observation["measurement_errors"]
        if len(self.results) < _MAX_RESULTS:
            self.results.append(record)
        else:
            self.results_truncated = True
            self.results[-1] = record
        if related and success and not is_error and isinstance(text, str):
            self.answer = text
        # Later totals restate the same cumulative scope; they are never summed.
        self.envelope["estimated_cost_usd"] = record["cumulative_cost_usd"]
        measurement_scope(self.envelope, "cost_scope", "cumulative_through_latest_native_result", COST_SCOPES)
        self.envelope["model_usage"] = measurements["model_usage"]
        measurement_scope(self.envelope, "model_usage_scope", "native_query_cumulative", MODEL_USAGE_SCOPES)
        replace_measurement_errors(self.envelope, observation, "estimated_cost_usd", "model_usage")
        if not related:
            # Background/task-notification results can share this session while
            # answering none of our messages. Retain them without routing their
            # answer, errors or terminal status to this call's task.
            return
        self.last_related = record
        replace_measurement_errors(self.envelope, observation, "usage", "provider_turns", "provider_duration_ms")
        self.failed_related = self.failed_related or is_error or not success
        self.envelope["usage"] = record["usage"]
        measurement_scope(self.envelope, "usage_scope", "latest_related_native_result", USAGE_SCOPES)
        self.cover(uuids)
        self.answered(uuids, persist=False)
        # Record the observed result before an independent mailbox write can
        # fail. This also preserves results emitted during owned cleanup.
        self.finish()
        self.answered(uuids)
        self.stop_input("The native session produced its first result answering this call's input.")

    def callback(self, value):
        request = value.get("request")
        subtype = request.get("subtype") if isinstance(request, dict) else None
        self.detail(self.unknown_requests, str(subtype)[:200])
        self.stop_input("The native provider asked for an unsupported client decision.")
        raise ProtocolError("The native provider requested an unsupported client decision; approvals stay with the provider. No protocol response was invented; inspect retained output.")

    def message(self, body):
        value = decode(body)
        kind = value.get("type")
        if not isinstance(kind, str):
            raise ProtocolError("Native stream record carried no valid type; inspect retained output.")
        if kind in ("control_request", "control_cancel_request"):
            self.callback(value)
        elif kind == "control_response":
            raise ProtocolError("The native provider answered a control request this call never sent; inspect retained output.")
        elif kind == "system":
            if self.attributed(value):
                self.system(value)
        elif kind == "assistant":
            if self.attributed(value):
                self.reply(value)
        elif kind == "user":
            if self.attributed(value) and identity(value.get("uuid")):
                self.echo(value["uuid"])  # received, never answered
        elif kind == "result":
            if self.attributed(value):
                self.result(value)

    # --- outcome ---------------------------------------------------------

    def close_stdin(self):
        if not self.stdin_closed:
            self.stdin_closed = True
            self.process.stdin.close()

    def preserve(self, resolve_pending=True):
        self.envelope["permission_denials"] = self.denials
        self.envelope["unsupported_native_requests"] = self.unknown_requests
        self.envelope["provider_errors"] = self.errors
        self.envelope["provider_errors_truncated"] = self.details_truncated
        self.envelope["native_input"] = [{key: value for key, value in self.inputs[identifier].items()
                                          if key != "receipt"} for identifier in self.order]
        self.envelope["native_input_unwritten_bytes"] = len(self.outgoing)
        self.envelope["native_results"] = self.results
        self.envelope["native_results_truncated"] = self.results_truncated
        self.envelope["native_subagent_frames"] = self.subagent_frames
        parts = list(self.texts)
        if self.answer is not None and self.answer not in parts:
            parts.append(self.answer)
        partial = "\n\n".join(part for part in parts if part)[:_MAX_PARTIAL]
        if partial and self.envelope.get("state") != "returned":
            self.envelope["partial_result"] = partial
        if resolve_pending:
            task = self.inputs.get(self.initial)
            if task is not None:
                self.envelope["task_delivery"] = "written" if task["written"] else "uncertain"
            for identifier in self.order:
                record = self.inputs[identifier]
                if record["consumption"] == "consumed":
                    self.receipt(identifier, "consumed", "A native main-session frame named this input as answered in a turn; consumption is not task completion.")
                elif not record["written"]:
                    self.receipt(identifier, "uncertain", "This input was not fully written to the native input pipe before the call ended; do not resend automatically.")
                else:
                    self.receipt(identifier, "uncertain", "No native result covering this delivered input was observed; do not resend automatically.")

    def problem(self, message):
        self.had_problem = True
        if not self.outcome_recorded:
            self.envelope.update(state="uncertain", result=None)
        self.envelope.update(needs_attention=True, message=message)

    def finish(self):
        if not self.results:
            raise ProtocolError("Native output ended without a validated main session result; work may have occurred.")
        last = self.last_related
        if last is not None:
            self.envelope.update(provider_subtype=last["subtype"], provider_is_error=last["is_error"],
                                 terminal_reason=last["terminal_reason"], provider_turns=last["num_turns"],
                                 provider_duration_ms=last["duration_ms"])
        task = self.inputs.get(self.initial, {"written": False})
        unsettled = [identifier for identifier in self.order if not self.inputs[identifier]["result_covered"]]
        self.envelope["task_delivery"] = "written" if task["written"] else "uncertain"
        if not task["written"]:
            state = "uncertain"
            message = "The task was not fully written to the native input pipe; any observed result may answer other input. Inspect retained output."
        elif unsettled:
            state = "uncertain"
            message = "Submitted native input has no observed result covering it; its outcome is unknown. Do not resend it automatically."
        elif self.failed_related:
            state = "provider_error"
            message = "A native result reported an error; earlier results and text are retained. Inspect retained output and Multithread evidence."
        elif self.answer is None:
            state = "uncertain"
            message = "No native result carried final answer text; inspect retained output before continuing."
        else:
            state = "returned"
            message = "Assess the answer and durable Multithread evidence; a returned turn is not workflow completion."
        stopped = last is not None and (last["terminal_reason"] not in (None, "completed", "end_turn")
                                       or last["stop_reason"] not in (None, "end_turn", "stop_sequence"))
        if stopped:
            message += " The native stopping reason requires attention before continuing."
        self.envelope.update(state=state, result=self.answer if state == "returned" else None,
                             message=message, needs_attention=bool(
                                 state != "returned" or stopped or self.denials
                                 or self.unknown_requests or self.had_problem))
        self.outcome_recorded = state in ("returned", "provider_error")


def run(process, task: bytes, repo: str, resume: str | None, directory: Path,
        envelope: dict, timeout: float, control=None, observer=None, feedback=None) -> None:
    """Observe one native streaming session separately from the caller's cleanup."""
    driver = _Driver(process, task, repo, resume, envelope, timeout, control)
    owned_observer = observer is None
    observation = observer if observer is not None else Observation(process, directory, envelope)
    observation.driver = driver
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            writing = False
            # The documented CLI needs no handshake before the first prompt.
            driver.start()
            while not observation.eof:
                if feedback is not None:
                    feedback()
                driver.poll_control()
                if driver.outgoing and not writing:
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                    writing = True
                elif not driver.outgoing and writing:
                    selector.unregister(process.stdin)
                    writing = False
                if driver.done():
                    driver.close_stdin()
                    driver.finish()
                    return
                for key, ready in selector.select(min(0.1, driver.remaining())):
                    if key.data == "stdin":
                        try:
                            count = os.write(process.stdin.fileno(), driver.outgoing)
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            selector.unregister(process.stdin)
                            writing = False
                            driver.unwritable()
                            continue
                        if count <= 0:
                            raise ProtocolError("Native input closed before a submitted message was delivered; inspect retained output.")
                        driver.wrote(count)
                        del driver.outgoing[:count]
                        continue
                    observation.read()
            driver.close_stdin()
            driver.finish()
    except ProtocolError as exc:
        observation.fault(str(exc))
    except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
        driver.problem("Native observation was interrupted or unavailable; preserve partial work and inspect evidence before retrying.")
        raise
    finally:
        driver.observation_only = True
        try:
            observation.snapshot()
        finally:
            driver.close_stdin()
            if owned_observer:
                observation.close()
