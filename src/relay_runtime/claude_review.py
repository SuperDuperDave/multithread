"""Review one exact Claude Code binary for restricted peer calls.

A restricted call relies on behaviour a session cannot report about itself: that --restricted and
--strict-mcp-config leave project and local hooks, allow rules and project MCP servers out. A review checks
that the binary's restricted surface matches a hand-reviewed one, then proves the behaviour live: canaries
planted in a fresh checkout must stay silent under the restricted flags, and a positive control must show
that the same canaries fire without them. Only a pass is recorded, as an attributed ledger event.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

#: Strings the restricted path depends on; one that disappears means the surface changed.
SURFACE_TOKENS = (
    "--managed-settings", "managedHooksOnly", "managedHooksExcluded", "CLAUDE_CODE_RESTRICTED", "disableAllHooks",
    "remote-settings.json", "policy-limits.json", "managed-mcp.json", "Program Files/ClaudeCode", "/etc/claude-code",
    "Policies\\\\ClaudeCode", "--setting-sources", "blockReadsOutsideWorkingDirectories", "strictMcpConfig",
    "--disable-slash-commands", "CLAUDE_CONFIG_DIR",
)
#: Flags a restricted call passes; their help lines are part of the surface.
SURFACE_FLAGS = ("--print", "--output-format", "--input-format", "--verbose", "--replay-user-messages",
                 "--permission-prompts", "--tools", "--restricted", "--strict-mcp-config", "--disable-slash-commands",
                 "--session-id", "--resume", "--model", "--effort")
CANARIES = ("local-hook", "mcp-server", "project-hook")
RESTRICTED_FLAGS = ("--tools", "", "--restricted", "--strict-mcp-config", "--disable-slash-commands")
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

#: Reviewed by hand (Sol's pre-review of release 2c). Every later binary is reviewed against these surfaces.
BUILT_IN = {
    "a967e7b1d8b4e47ee421d5433027880347952b0c0857abf880e2c942a4ec93b3": {
        "version": "2.1.292",
        "surface_sha256": "ac29ee45ec681d5942ad151fd025a2dba0767aabeca9d53cbd16d0ac3105c133",
        "plugins": ["cc-plugin-agents-md", "cc-plugin-plugin-authoring", "cc-plugin-telemetry"],
        "agents": ["Explore", "Plan", "claude", "general-purpose", "statusline-setup"],
    },
}


class ReviewError(Exception):
    pass


def binary_identity(provider):
    """The binary a call will execute, by resolved path and content digest."""
    path = os.path.realpath(provider)
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        raise ReviewError(f"The Claude Code binary {path} could not be read ({exc}).") from None
    return path, digest.hexdigest()


def surface(path):
    """A digest of the restricted surface: which reviewed strings are present and the help lines of our flags."""
    try:
        body = Path(path).read_bytes()
        help_text = subprocess.run([path, "--help"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=30, check=False).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewError(f"The restricted surface of {path} could not be read ({exc}).") from None
    value = {"tokens": [token for token in SURFACE_TOKENS if token.encode() in body], "flags": _option_blocks(help_text)}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(), value


def _option_blocks(help_text):
    """Each of our flags' whole help entry, wrapped lines joined: the documented semantics we rely on."""
    blocks, current = {}, None
    for line in help_text.splitlines():
        if line.startswith("  -"):
            names = [part.split(" ")[0] for part in line.strip().split(", ")]
            current = next((name for name in names if name in SURFACE_FLAGS), None)
            if current is not None:
                blocks[current] = [line.strip()]
        elif current is not None and line.startswith("    ") and line.strip():
            blocks[current].append(line.strip())
        else:
            current = None
    return {name: " ".join(" ".join(lines).split()) for name, lines in sorted(blocks.items())}


def reviewed(digest, launcher, repo):
    """The review that admits this exact binary to restricted calls, or None. Unreadable reviews raise."""
    if digest in BUILT_IN:
        return {**BUILT_IN[digest], "source": "built_in"}
    command = [str(launcher), "--repo", str(repo), "--json", "provider-review", "show", "--binary-sha256", digest]
    try:
        answer = subprocess.run(command, cwd=repo, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=15, check=False)
        reviews = json.loads(answer.stdout)["reviews"] if answer.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        reviews = None
    if not isinstance(reviews, list):
        raise ReviewError("This checkout's recorded Claude Code reviews could not be read, so a restricted call "
                          "cannot establish that its binary was reviewed.")
    for event in reversed(reviews):
        meta = event.get("meta") if isinstance(event, dict) else None
        if isinstance(meta, dict) and meta.get("binary_sha256") == digest:
            names = lambda key: [] if meta.get(key) == "none" else str(meta.get(key, "")).split(",")
            return {"version": meta.get("version"), "surface_sha256": meta.get("surface_sha256"),
                    "plugins": names("plugins"), "agents": names("agents"), "source": f"ledger:{event.get('seq')}"}
    return None


def _canary_call(binary, out, model, restricted):
    """One native call in a fresh checkout planted with a project hook, a local hook, a project MCP server and
    an allow rule; each canary that takes effect leaves a marker file."""
    work = Path(tempfile.mkdtemp(prefix="restricted-canary-" if restricted else "control-canary-", dir=out))
    markers = {name: work / f"FIRED-{name}" for name in CANARIES}
    hook = lambda name: [{"hooks": [{"type": "command", "command": f"touch '{markers[name]}'"}]}]
    (work / ".claude").mkdir()
    (work / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"SessionStart": hook("project-hook"), "UserPromptSubmit": hook("project-hook")},
        "permissions": {"allow": ["Bash(*)", "Read(//**)"]}, "enableAllProjectMcpServers": True}))
    (work / ".claude" / "settings.local.json").write_text(json.dumps({"hooks": {"SessionStart": hook("local-hook")}}))
    (work / ".mcp.json").write_text(json.dumps({"mcpServers": {"canary": {
        "command": "sh", "args": ["-c", f"touch '{markers['mcp-server']}'; sleep 20"]}}}))
    flags = RESTRICTED_FLAGS if restricted else ("--tools", "", "--setting-sources", "project,local")
    argv = [binary, "--print", "--output-format", "stream-json", "--permission-prompts", "none", "--verbose",
            "--input-format", "stream-json", "--model", model, "--effort", "low", *flags,
            "--session-id", str(uuid.uuid4())]
    frame = {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "Reply with the single word: ready"}]}}
    try:
        answer = subprocess.run(argv, cwd=work, input=json.dumps(frame) + "\n", capture_output=True, text=True,
                                timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit": None, "error": str(exc), "fired": sorted(n for n, m in markers.items() if m.exists())}
    (work / "stdout.jsonl").write_text(answer.stdout)
    frames = []
    for line in answer.stdout.splitlines():
        try:
            frames.append(json.loads(line))
        except ValueError:
            pass
    init = next((f for f in frames if f.get("type") == "system" and f.get("subtype") == "init"), {})
    result = next((f for f in frames if f.get("type") == "result"), {})
    names = lambda field, key=None: sorted(item.get(key) if key and isinstance(item, dict) else item
                                           for item in init.get(field) or [])
    return {"exit": answer.returncode, "version": init.get("claude_code_version"),
            "tools": names("tools"), "mcp_servers": names("mcp_servers", "name"),
            "plugins": names("plugins", "name"), "agents": names("agents"),
            "fired": sorted(name for name, marker in markers.items() if marker.exists()),
            "answered": bool(result.get("result")) and not result.get("is_error")}


def review(provider, out, model=DEFAULT_MODEL):
    """Review one binary; returns the report. A pass is the only verdict that may be recorded."""
    path, digest = binary_identity(provider)
    surface_sha256, surface_value = surface(path)
    anchors = [entry for entry in BUILT_IN.values() if entry["surface_sha256"] == surface_sha256]
    try:
        version = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 timeout=30, check=False).stdout.split(" ")[0].strip()
    except (OSError, subprocess.TimeoutExpired):
        version = None
    report = {"schema": 1, "provider": "claude", "binary_path": path, "binary_sha256": digest, "version": version,
              "surface_sha256": surface_sha256, "surface": surface_value, "model": model, "reasons": []}
    reasons = report["reasons"]
    if not anchors:
        reasons.append("the restricted surface differs from every hand-reviewed one")
    control = _canary_call(path, out, model, restricted=False)
    restricted = _canary_call(path, out, model, restricted=True)
    if binary_changed(path, digest):
        reasons.append("the binary changed during its review")
    report.update(control=control, restricted=restricted, control_fired=control["fired"],
                  restricted_fired=restricted["fired"], plugins=restricted.get("plugins", []),
                  agents=restricted.get("agents", []))
    if control["fired"] != sorted(CANARIES):
        reasons.append("the positive control did not fire every canary, so silence would prove nothing")
    if restricted["fired"]:
        reasons.append("a canary fired under the restricted flags: " + ", ".join(restricted["fired"]))
    if restricted.get("exit") != 0 or not restricted.get("answered"):
        reasons.append("the restricted call did not end with an answer")
    if restricted.get("tools") or restricted.get("mcp_servers"):
        reasons.append("the restricted call reported tools or MCP servers")
    if not version or restricted.get("version") != version:
        reasons.append("the session's reported version differs from the binary's")
    if anchors and not (set(report["plugins"]) <= {p for a in anchors for p in a["plugins"]}
                        and set(report["agents"]) <= {g for a in anchors for g in a["agents"]}):
        reasons.append("the session reports built-in plugins or agents no hand review covered")
    report["verdict"] = "needs_review" if reasons else "pass"
    return report


def binary_changed(path, digest):
    try:
        return binary_identity(path)[1] != digest
    except ReviewError:
        return True


def review_main(argv, launcher):
    parser = argparse.ArgumentParser(prog="multithread peer review-claude",
                                     description="Review one exact Claude Code binary for restricted peer calls and, "
                                                 "on a pass, record the review in this checkout's ledger.")
    parser.add_argument("--provider", required=True, help="the Claude Code binary to review (resolved, then hashed)")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="checkout whose ledger records the review")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="model for the two short canary calls")
    parser.add_argument("--agent", default=os.environ.get("RELAY_AGENT"), help="reviewer identity for the record")
    parser.add_argument("--session", default=os.environ.get("RELAY_SESSION"), help="reviewer session for the record")
    parser.add_argument("--json", action="store_true", help="accepted for symmetry; output is always JSON")
    args = parser.parse_args(argv)
    if not args.agent or not args.session:
        parser.error("--agent and --session name the reviewer the record is attributed to")
    out = Path(tempfile.mkdtemp(prefix="relay-claude-review-"))
    try:
        report = review(args.provider, out, args.model)
    except ReviewError as exc:
        print(json.dumps({"verdict": "unavailable", "message": str(exc)}), file=sys.stdout)
        return 1
    (out / "review.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    receipt = None
    if report["verdict"] == "pass":
        command = [str(launcher), "--repo", str(args.repo), "--json", "provider-review", "record",
                   "--agent", args.agent, "--session", args.session]
        answer = subprocess.run(command, cwd=args.repo, input=json.dumps(report), capture_output=True, text=True,
                                timeout=30, check=False)
        if answer.returncode != 0:
            print(json.dumps({"verdict": "pass", "recorded": False, "evidence_directory": str(out),
                              "message": "The review passed but was not recorded: "
                                         + (answer.stderr.strip() or answer.stdout.strip())[:400]}))
            return 1
        receipt = json.loads(answer.stdout)
    print(json.dumps({"verdict": report["verdict"], "reasons": report["reasons"], "version": report["version"],
                      "binary_sha256": report["binary_sha256"], "recorded": receipt, "evidence_directory": str(out)},
                     sort_keys=True))
    return 0 if report["verdict"] == "pass" else 1
