"""Explicit public-release acquisition; installation authority stays in bootstrap.

The release packager also renders this stdlib-only file as a version-pinned
installer. The standalone asset is a publisher trust root, not a signature.
"""

import argparse
import gzip
import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request


PINNED_RELEASE = None
PROJECT = "https://github.com/SuperDuperDave/multithread"
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class UpdateError(Exception):
    pass


def _display_text(value):
    # Keep these stdlib-only display helpers aligned with provider.py: this file
    # also runs as install.py before any Relay package is installed.
    return "".join(character if character.isprintable() else
                   json.dumps(character, ensure_ascii=True)[1:-1]
                   for character in str(value))


def _display_command(label, argv):
    if all(argument.isprintable() for argument in argv):
        print(label + ": " + shlex.join(argv))
    else:
        # Preserve the exact arguments as data; escaped shell text could refer
        # to a different path from the one the user actually selected.
        print(label + " (JSON argv): " + json.dumps(argv, ensure_ascii=True))


def _https(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UpdateError("Release download URL is malformed.") from exc
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or port not in (None, 443)
            or not (host == "github.com" or host.endswith(".githubusercontent.com"))):
        raise UpdateError("Release download left the supported HTTPS publisher/CDN route.")
    return url


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _https(newurl))


def download(url, limit):
    request = urllib.request.Request(_https(url), headers={"User-Agent": "Multithread-release-installer"})
    try:
        with urllib.request.build_opener(_Redirect()).open(request, timeout=30) as response:
            _https(response.url)
            body = response.read(limit + 1)
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise UpdateError("Release download unavailable; installed state is not evidence of being up to date.") from exc
    if len(body) > limit:
        raise UpdateError("Release download exceeded its size limit.")
    return body


def validate_release(value, requested=None):
    fields = {"schema", "version", "source_commit", "archive", "archive_sha256", "release_id", "files"}
    if not isinstance(value, dict) or set(value) != fields or type(value["schema"]) is not int or value["schema"] != 1:
        raise UpdateError("Unsupported release metadata; use the release page for the supported installer.")
    version = value["version"]
    if not isinstance(version, str) or not VERSION.fullmatch(version) or (requested and version != requested):
        raise UpdateError("Release version does not match the requested selection.")
    if (not isinstance(value["source_commit"], str)
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value["source_commit"])
            or value["archive"] != f"relay-{version}-linux-x86_64.tar.gz"):
        raise UpdateError("Release source/archive identity is invalid.")
    for key in ("release_id", "archive_sha256"):
        if not isinstance(value[key], str) or not DIGEST.fullmatch(value[key]):
            raise UpdateError("Release digest is invalid.")
    files = value["files"]
    root = f"relay-{version}/"
    if not isinstance(files, dict) or not 5 <= len(files) <= 128:
        raise UpdateError("Release file selection is invalid.")
    for name, digest in files.items():
        if (not isinstance(name, str) or not name.startswith(root)
                or PurePosixPath(name).as_posix() != name or ".." in PurePosixPath(name).parts
                or "\\" in name or "\0" in name or name.endswith("/")
                or not isinstance(digest, str) or not DIGEST.fullmatch(digest)):
            raise UpdateError("Release file selection contains an unsafe path or digest.")
    if not {root + n for n in ("LICENSE", "README.md", "SHA256SUMS", "runtime/bootstrap.py", "runtime/release.json")} <= set(files):
        raise UpdateError("Release file selection is incomplete.")
    if files[root + "runtime/release.json"] != value["release_id"]:
        raise UpdateError("Release record hash does not match its runtime identity.")
    return value


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise UpdateError("Release JSON contains duplicate fields.")
        result[name] = value
    return result


def candidate(version=None):
    if version is not None and not VERSION.fullmatch(version):
        raise UpdateError("Use an exact version such as 0.2.0, without a v prefix.")
    route = f"download/v{version}" if version else "latest/download"
    try:
        value = json.loads(download(f"{PROJECT}/releases/{route}/relay-release.json", 256 * 1024), object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise UpdateError("Release metadata is unreadable; update availability is unknown.") from exc
    return validate_release(value, version)


def extract_release(body, release, destination):
    """Validate the complete file-only archive before writing any member."""
    if len(body) > MAX_ARCHIVE or hashlib.sha256(body).hexdigest() != release["archive_sha256"]:
        raise UpdateError("Archive checksum mismatch; no installation was attempted.")
    captured = {}
    total = 0
    try:
        # Bound the whole decompressed stream before tarfile parses extended
        # headers; member limits alone do not bound PAX/header expansion.
        expanded_limit = MAX_TOTAL + 256 * 1024
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
            expanded = compressed.read(expanded_limit + 1)
        if len(expanded) > expanded_limit:
            raise UpdateError("Archive exceeded its expanded size limit.")
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            for member in archive:
                if (member.name in captured or member.name not in release["files"]
                        or not member.isfile() or member.issparse() or member.linkname
                        or member.size < 0 or member.size > MAX_FILE):
                    raise UpdateError("Archive has an unexpected, duplicate or non-regular member.")
                total += member.size
                if total > MAX_TOTAL:
                    raise UpdateError("Archive exceeded its expanded size limit.")
                with archive.extractfile(member) as stream:
                    data = stream.read(MAX_FILE + 1)
                if len(data) != member.size or hashlib.sha256(data).hexdigest() != release["files"][member.name]:
                    raise UpdateError("Archive member checksum mismatch.")
                captured[member.name] = data
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise UpdateError("Release archive is unreadable.") from exc
    if set(captured) != set(release["files"]):
        raise UpdateError("Archive is missing expected files.")
    for name, body in captured.items():
        path = destination / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(body)
        path.chmod(0o600)
    return destination / f"relay-{release['version']}" / "runtime"


def _command(argv, timeout=45):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("Command timed out; inspect installed state before retrying an uncertain operation.") from exc
    except (OSError, UnicodeError) as exc:
        raise UpdateError("Required local command/output is unavailable.") from exc
    if result.returncode:
        # Bootstrap diagnostics are bounded, and may contain local paths. Keep
        # them local; no updater report is automatically uploaded.
        raise UpdateError(f"Command did not report success (exit {result.returncode}): {result.stderr.strip()[:1500]}")
    try:
        value = json.loads(result.stdout)
    except (ValueError, RecursionError) as exc:
        raise UpdateError("Command returned no usable state; inspect before retrying.") from exc
    if not isinstance(value, dict):
        raise UpdateError("Command returned an unsupported state.")
    return value


def _platform():
    if (platform.system() != "Linux" or platform.machine() != "x86_64"
            or sys.version_info[:2] != (3, 12) or os.getuid() == 0 or os.getuid() != os.geteuid()):
        raise UpdateError("Use an ordinary x86-64 Linux account with /usr/bin/python3 3.12; see docs/SUPPORT.md. Provider/OS setup is separate.")
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise UpdateError("Run with /usr/bin/python3 -I -S -B.")
    if not Path("/usr/bin/git").is_file():
        raise UpdateError("Git is missing; install it through your normal OS setup before continuing.")


def _parser(install):
    parser = argparse.ArgumentParser(prog="install.py" if install else "multithread update",
        description="Review one public release, then explicitly install using its exact-state offline bootstrap.")
    parser.add_argument("--check", action="store_true", help="report selection without installing or enrolling")
    parser.add_argument("--json", action="store_true", help="structured report; check only unless --yes is also supplied")
    parser.add_argument("--yes", action="store_true", help="approve the selected release and optional repository setup")
    parser.add_argument("--enroll-repo", "--repo", dest="repo", type=Path, help="explicitly enroll/check this repository after installation; otherwise no repository changes")
    parser.add_argument("--version", help="exact published version; default: latest release (installer asset stays pinned)")
    parser.add_argument("--approve-sha256", help="explicit runtime digest; required for noninteractive update application")
    parser.add_argument("--expected-activation", help="exact observed active ID; stale IDs refuse")
    return parser


def _run(argv, *, install):
    args = _parser(install).parse_args(argv)
    result = {"schema": 1, "state": "unavailable", "stage": "prerequisites",
              "installation": "unchanged", "repository": "not_requested", "provider_started": False,
              "hook_delivery": "not_checked", "provider_tools": "not_checked",
              "recovery_url": PROJECT + "/blob/main/docs/engineering/INSTALLATION.md#recover-a-damaged-or-interrupted-installation"}
    try:
        _platform()
        if args.repo is not None:
            # Do not resolve aliases that the enrollment boundary must inspect.
            if not args.repo.is_absolute():
                args.repo = Path.cwd() / args.repo
            if not args.repo.is_dir():
                raise UpdateError("--repo must identify the chosen existing Git checkout.")
        result["stage"] = "release_selection"
        if install:
            if PINNED_RELEASE is None:
                raise UpdateError("Use the versioned install.py release asset, or build a release from reviewed source.")
            release = validate_release(PINNED_RELEASE, args.version)
        else:
            release = candidate(args.version)
        if args.approve_sha256 and args.approve_sha256 != release["release_id"]:
            raise UpdateError("Candidate differs from the explicitly approved runtime digest.")
        result["candidate"] = {k: release[k] for k in ("version", "source_commit", "release_id", "archive_sha256")}
        result["source_url"] = f"{PROJECT}/tree/{release['source_commit']}"
        launcher = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/bin/multithread"
        # The installed dispatch has verified this launcher; the standalone
        # installer must instead let the incoming bootstrap inspect any command.
        current = None if install else _command([str(launcher), "runtime", "status"])
        result["current"] = current
        if current and current.get("installed") is not True:
            raise UpdateError("No healthy active installation was observed.")
        if current:
            result["inspect_command"] = shlex.join([str(launcher), "runtime", "inspect"])
        if current and current["activation"]["release_id"] == release["release_id"]:
            result.update(state="up_to_date", installation="reused", stage="complete")
            if args.repo is not None:
                result["launcher"] = str(launcher)
                result["setup_command"] = shlex.join([str(launcher), "setup", "--repo", str(args.repo), "--apply"])
                result["check_command"] = shlex.join([str(launcher), "setup", "--repo", str(args.repo), "--check"])
            if args.repo is not None and not args.check and (args.yes or not args.json):
                if args.yes and not (args.version and args.approve_sha256 and args.expected_activation):
                    raise UpdateError("Unattended repository enrollment requires the exact approved update selection, or use multithread setup --apply.")
                if args.expected_activation and args.expected_activation != current["activation"]["activation_id"]:
                    raise UpdateError("Activation changed since your observation; no repository setup was attempted.")
                result["launcher"] = str(launcher)
                if not args.yes:
                    print(f"Multithread is already up to date. Explicitly enroll/check {_display_text(args.repo)}; provider settings stay unchanged.")
                    if not sys.stdin.isatty():
                        result.update(state="needs_attention", repository="not_checked",
                            message="Repository setup was requested but not approved. Run the exact setup command.",
                            setup_command=shlex.join([str(launcher), "setup", "--repo", str(args.repo), "--apply"]))
                        return _finish(result, args, 1)
                    if input("Type setup to enroll/check this repository: ").strip() != "setup":
                        result.update(state="cancelled", repository="not_checked")
                        return _finish(result, args)
                return _setup(result, args, launcher)
            if args.repo is not None:
                result["repository"] = "not_checked"
            return _finish(result, args)
        if current:
            old = re.match(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:$|[-+])", current["activation"]["version"])
            if old and tuple(map(int, old.groups())) > tuple(map(int, release["version"].split("."))):
                raise UpdateError("Selected version is older than the active version; use explicit rollback, not update.")
        if args.check or (args.json and not args.yes):
            result.update(state="release_available", stage="selection_checked", package_verification="not_checked")
            if current:
                result["apply_argv"] = [str(launcher), "update", "--version", release["version"],
                    "--approve-sha256", release["release_id"], "--expected-activation", current["activation"]["activation_id"], "--yes", "--json"]
                if args.repo is not None:
                    result["apply_argv"] += ["--enroll-repo", str(args.repo)]
                result["apply_command"] = shlex.join(result["apply_argv"])
            return _finish(result, args)
        if not install and args.yes and not (args.version and args.approve_sha256 and args.expected_activation):
            raise UpdateError("For unattended updates, first inspect multithread update --json and use its exact apply_command.")
        if not install and not args.yes:
            # Approve newly fetched publisher code before the incoming bootstrap
            # is ever executed. The existing verified launcher supplies current
            # identity; the incoming plan must later match that same snapshot.
            print(f"Update Multithread {_display_text(current['activation']['version'])} → {release['version']}\nSource: {result['source_url']}\nRuntime: {release['release_id'][:12]}…\nLauncher: {_display_text(launcher)}\nCurrent activation: {_display_text(current['activation']['activation_id'])}")
            print(f"Repository enrollment: {_display_text(args.repo or 'not requested')}. Provider settings stay unchanged.")
            print("Approve this publisher's release before running its installer. Checksums bind bytes, not publisher authenticity.")
            print("Running workers keep loaded code; subsequent commands use this selection. Check release compatibility before updating active work.")
            if not sys.stdin.isatty():
                raise UpdateError("Run interactively to approve, or inspect multithread update --json for an exact apply command.")
            if input("Type install to apply this selection: ").strip() != "install":
                result.update(state="cancelled", stage="not_applied")
                return _finish(result, args)
        result["stage"] = "package_verification"
        print(f"Multithread: checking release {release['version']}…", file=sys.stderr, flush=True)
        with tempfile.TemporaryDirectory(prefix="relay-download-") as temporary:
            directory = Path(temporary)
            body = download(f"{PROJECT}/releases/download/v{release['version']}/{release['archive']}", MAX_ARCHIVE)
            bundle = extract_release(body, release, directory)
            bootstrap = ["/usr/bin/python3", "-I", "-S", "-B", str(bundle / "bootstrap.py")]
            selection = ["--release", str(bundle), "--approve-sha256", release["release_id"]]
            plan = _command([*bootstrap, "plan", *selection])
            expected = plan["expected_activation"]
            if args.expected_activation is not None and args.expected_activation != (expected or "none"):
                raise UpdateError("Activation changed since your observation; inspect again before approving a new plan.")
            if current and expected != current["activation"]["activation_id"]:
                raise UpdateError("Activation changed during update preparation; no update was attempted.")
            result["plan"] = plan
            result["package_verification"] = "verified"
            observed = _command([*bootstrap, "status"])
            result["current"] = observed
            if observed.get("installed"):
                if observed["activation"]["activation_id"] != expected:
                    raise UpdateError("Activation changed after planning; inspect before retrying.")
                old = re.match(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:$|[-+])", observed["activation"]["version"])
                if old and tuple(map(int, old.groups())) > tuple(map(int, release["version"].split("."))):
                    raise UpdateError("This installer is older than the active version; use multithread update or deliberate rollback.")
            elif expected is not None:
                raise UpdateError("Active installation became unavailable after planning.")
            print(f"Multithread: selected {release['version']}; exact current activation {_display_text(expected or 'none')}; repository enrollment {_display_text(args.repo or 'not requested')}.", file=sys.stderr, flush=True)
            if expected is not None and observed["activation"]["release_id"] != release["release_id"]:
                print("Multithread: running workers keep loaded code; subsequent commands use the new release. Preserve active coordination and follow release compatibility guidance.", file=sys.stderr, flush=True)
            if install and not args.yes:
                print(f"Multithread {release['version']}\nSource: {result['source_url']}\nRuntime: {release['release_id'][:12]}…\nLauncher: {_display_text(plan['launcher'])}\nCurrent activation: {_display_text(expected or 'none')}")
                print(f"Repository setup: {_display_text(args.repo or 'not requested')}\nProvider sign-ins/settings and permissions stay unchanged.")
                print("Running workers keep loaded code; subsequent commands use this selection. Finish active coordination before updating between incompatible releases.")
                print("Approve this publisher's release. Checksums bind the selected bytes; they are not a publisher signature.")
                if not sys.stdin.isatty():
                    raise UpdateError("Run interactively to approve, or use --yes for already-authorized installation.")
                if input("Type install to apply this selection: ").strip() != "install":
                    result.update(state="cancelled", stage="not_applied")
                    return _finish(result, args)
            result["stage"] = "installation"
            # The incoming bootstrap verifies prior state. Do not execute an
            # unknown existing command merely to identify it.
            observed = _command([*bootstrap, "status"])
            result["previous"] = observed
            if observed.get("installed") and observed["activation"]["release_id"] == release["release_id"]:
                if observed["activation"]["activation_id"] != expected:
                    raise UpdateError("Activation changed after planning; inspect before retrying.")
                result["installation"] = "reused"
            else:
                result["current"] = None
                result["installation"] = "unknown"
                applied = _command([*bootstrap, "install", *selection, "--expected-activation", expected or "none"])
                if applied.get("installed") is not True or applied["activation"]["release_id"] != release["release_id"]:
                    raise UpdateError("Installation result did not establish the selected runtime; inspect installed status.")
                result["installation"] = "installed" if expected is None else "updated"
                result["applied"] = applied
            launcher = Path(plan["launcher"])
            result["launcher"] = str(launcher)
            result["inspect_command"] = shlex.join([str(launcher), "runtime", "inspect"])
            result["current"] = None
            result["current"] = _command([str(launcher), "runtime", "status"])
            if result["current"].get("installed") is not True or result["current"]["activation"]["release_id"] != release["release_id"]:
                raise UpdateError("Post-installation runtime identity is unavailable or changed; inspect before retrying.")
            result["launcher"] = str(launcher)
            if args.repo is not None:
                return _setup(result, args, launcher)
            result.update(state="ready_for_setup", stage="complete")
            return _finish(result, args)
    except EOFError:
        result.update(state="cancelled", stage="not_applied", message="Approval input closed; no installation or enrollment was applied.")
        return _finish(result, args)
    except KeyboardInterrupt:
        if isinstance(result.get("repository"), dict):
            result["message"] = "Interrupted while reporting; the captured repository setup result is preserved."
        elif result["stage"] == "repository_setup":
            result.update(state="needs_attention", repository={"state": "uncertain"},
                message="Runtime is installed; repository setup was interrupted and its outcome is unknown. Run the check_command before retrying enrollment.")
        elif result["installation"] == "unchanged" or (
                result["installation"] == "reused" and result["stage"] == "complete"):
            result.update(state="cancelled", repository="not_checked" if args.repo is not None else "not_requested",
                message="Interrupted; no installation or enrollment was applied.")
        else:
            result.update(state="unavailable", message="Interrupted; inspect current installed state before retrying.")
        return _finish(result, args, 130)
    except (UpdateError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        result.update(state="unavailable", message=str(exc)[:2000])
        return _finish(result, args, 1)


def _setup(result, args, launcher):
    result["stage"] = "repository_setup"
    command = [str(launcher), "setup", "--apply", "--repo", str(args.repo), "--json"]
    check = [str(launcher), "setup", "--check", "--repo", str(args.repo), "--json"]
    result["setup_command"] = shlex.join(command)
    result["check_command"] = shlex.join(check)
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
        setup = json.loads(completed.stdout)
        if not isinstance(setup, dict):
            raise ValueError("not an object")
    except (ValueError, UnicodeError, RecursionError, OSError, subprocess.TimeoutExpired):
        result.update(state="needs_attention", repository={"state": "uncertain"},
            message="Runtime is installed; repository setup outcome is unknown. Run the check_command before retrying enrollment.")
        return _finish(result, args, 1)
    result["repository"] = setup
    if completed.returncode != 0 or setup.get("state") != "ready":
        result.update(state="needs_attention", message="Runtime is installed; repository setup needs attention. Preserve state and inspect the setup result.")
        return _finish(result, args, 1)
    result.update(state="setup_checked", stage="complete")
    return _finish(result, args)


def _display_setup(report):
    """Present the captured receipt without importing or rerunning setup."""
    text = _display_text
    state = report.get("state", "not_reported")
    print("Repository readiness: " + text(state).replace("_", " "))
    if report.get("repo"):
        print("Checkout: " + text(report["repo"]))
    runtime = report.get("runtime", {})
    repository = report.get("repository", {})
    for label, entry in (("Runtime", runtime), ("Repository", repository)):
        if isinstance(entry, dict) and entry.get("state"):
            print(label + ": " + text(entry["state"]))
    if not isinstance(runtime, dict):
        runtime = {}
    if not isinstance(repository, dict):
        repository = {}
    identity = repository.get("identity")
    if isinstance(identity, dict) and identity.get("git_common_dir"):
        print("Git common directory: " + text(identity["git_common_dir"]))
    providers = report.get("providers", {})
    if isinstance(providers, dict):
        for client, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            summary = text(client) + ": " + text(entry.get("state", "not_reported"))
            if entry.get("message"):
                summary += "; " + text(entry["message"])
            print(summary)
    observations = [("Runtime", runtime), *((key.title(), repository.get(key, {}))
                    for key in ("enrollment", "doctor", "status"))]
    for label, entry in observations:
        if not isinstance(entry, dict) or entry.get("state") in {"verified", "not_checked", "not_requested", None}:
            continue
        print(label + " observation: " + text(entry["state"]))
        if entry.get("message"):
            print(label + " detail: " + text(entry["message"]))
        if isinstance(entry.get("stderr"), str):
            for line in entry["stderr"].splitlines():
                print(label + " diagnostic: " + text(line))
        if entry.get("stderr_truncated") or entry.get("stdout_truncated"):
            print(label + " diagnostic output was truncated; use the reported check for details.")
    if report.get("first_collaboration_url"):
        print("First collaboration, when you authorize provider use: " + text(report["first_collaboration_url"]))
    actions = report.get("next_actions", [])
    if not isinstance(actions, list):
        print("Next actions unavailable: captured next_actions is not a list.")
        return
    for action in actions:
        if isinstance(action, dict):
            label = text(action.get("stage", "Next"))
            print(label + ": " + text(action.get("action", "")))
            if "command" in action:
                command = action["command"]
                if isinstance(command, list) and command and all(isinstance(argument, str) for argument in command):
                    _display_command("  Command", command)
                else:
                    print("  Command unavailable: captured command is not a nonempty list of strings.")
        elif isinstance(action, str):
            print("Next: " + text(action))
        else:
            print("Next action unavailable: captured action is not an object or text.")


def _finish(result, args, code=0):
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        text = _display_text
        cancelled = result["state"] == "cancelled"
        state = result["state"].replace("_", " ")
        if result["state"] == "ready_for_setup":
            state = {"installed": "runtime installed", "updated": "runtime updated",
                     "reused": "runtime already installed"}.get(result["installation"], state)
        print("Multithread: " + text(state))
        print("Runtime installation: " + text(result["installation"]))
        setup = result.get("repository")
        if result.get("message"):
            print(text(result["message"]))
            if not isinstance(setup, dict) and not cancelled:
                if result.get("inspect_command"):
                    _display_command("Inspect", shlex.split(result["inspect_command"]))
                else:
                    print("Recovery: " + text(result["recovery_url"]))
        if result.get("candidate"):
            print("Selected release: " + text(result["candidate"]["version"]))
        if result.get("current") and result["current"].get("installed"):
            print("Active release: " + text(result["current"]["activation"]["version"]))
        launcher = result.get("launcher") or (result.get("current") or {}).get("launcher")
        if launcher:
            print("Launcher: " + text(launcher))
            print("Use the exact launcher path if multithread is not on PATH; shell configuration was not edited.")
        if isinstance(setup, dict):
            _display_setup(setup)
            if result["state"] != "setup_checked" and not cancelled and result.get("check_command"):
                _display_command("Check repository before retrying setup", shlex.split(result["check_command"]))
        elif args.repo is not None:
            print("Repository setup: not checked for " + text(args.repo))
            if result.get("check_command") and not cancelled:
                _display_command("Check repository", shlex.split(result["check_command"]))
            if result.get("setup_command") and result["state"] in {"up_to_date", "needs_attention"}:
                _display_command("To explicitly enroll/check this repository", shlex.split(result["setup_command"]))
        else:
            print("Repository setup: not requested.")
            if result["state"] == "ready_for_setup" and result["installation"] == "installed":
                print("Choose a repository for setup: " + PROJECT + "/blob/main/docs/SETUP.md")
        if result.get("apply_argv") and not cancelled:
            _display_command("Apply this exact update selection", result["apply_argv"])
        print("Provider authentication, hook delivery and tools are not checked by installation.")
    return code


def update_main(argv=None):
    return _run(argv, install=False)


def install_main(argv=None):
    return _run(argv, install=True)


if __name__ == "__main__":
    raise SystemExit(install_main())
