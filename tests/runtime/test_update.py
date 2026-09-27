"""Public release acquisition boundaries using local archives and mocked I/O."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import http.client
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import update
import relay_bootstrap

REAL_DOWNLOAD = update.download
REAL_COMMAND = update._command


def digest(body):
    return hashlib.sha256(body).hexdigest()


def archive_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for name, body, kind, target in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(body) if kind == tarfile.REGTYPE else 0
            member.linkname = target
            archive.addfile(member, io.BytesIO(body) if kind == tarfile.REGTYPE else None)
    return output.getvalue()


class ReleaseFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-update-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.destination = self.base / "extracted"
        self.prefix = "relay-0.2.0/"
        self.source = "a" * 40
        record = b'{"synthetic":"reviewed runtime record"}\n'
        self.files = {self.prefix + name: body for name, body in {
            "LICENSE": b"Synthetic fixture license\n",
            "README.md": ("Source: " + self.source + "\nRuntime: " + digest(record) + "\n").encode(),
            "SHA256SUMS": b"Synthetic fixture checksum list\n",
            "runtime/bootstrap.py": b"raise AssertionError('fixture code must never run')\n",
            "runtime/release.json": record,
        }.items()}
        self.entries = [(name, body, tarfile.REGTYPE, "") for name, body in self.files.items()]
        self.archive = archive_bytes(self.entries)
        self.release = {"schema": 1, "version": "0.2.0", "source_commit": self.source,
                        "archive": "relay-0.2.0-linux-x86_64.tar.gz",
                        "archive_sha256": digest(self.archive), "release_id": digest(record),
                        "files": {name: digest(body) for name, body in self.files.items()}}

    def reject_archive(self, body, *, release=None):
        selected = copy.deepcopy(release or self.release)
        selected["archive_sha256"] = digest(body)
        with self.assertRaises(update.UpdateError):
            update.extract_release(body, selected, self.destination)
        self.assertFalse(self.destination.exists(), "archive refusal must happen before extraction writes")


class ReleaseMetadataTests(ReleaseFixture):
    def test_exact_public_metadata_and_version_selection(self):
        self.assertEqual(self.release, update.validate_release(self.release, "0.2.0"))
        with self.assertRaises(update.UpdateError):
            update.validate_release(self.release, "0.1.0")

    def test_runtime_identity_must_match_selected_release_record_bytes(self):
        with self.assertRaises(update.UpdateError):
            update.validate_release({**self.release, "release_id": "f" * 64})

    def test_malformed_metadata_identity_and_unknown_fields_refuse(self):
        changes = ({"schema": True}, {"version": "v0.2.0"}, {"version": "../0.2.0"},
                   {"source_commit": "A" * 40}, {"archive": "other.tar.gz"},
                   {"release_id": "a" * 63}, {"archive_sha256": "G" * 64},
                   {"files": {}}, {"unexpected": "member"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(update.UpdateError):
                update.validate_release({**self.release, **change})

    def test_unsafe_manifest_paths_and_missing_required_members_refuse(self):
        for name in ("/absolute", self.prefix + "../escape", self.prefix + "runtime//file",
                     self.prefix + "runtime/./file", self.prefix + "back\\slash",
                     self.prefix + "nul\0name", self.prefix + "directory/", "other-root/file"):
            modified = copy.deepcopy(self.release)
            modified["files"][name] = "b" * 64
            with self.subTest(name=name), self.assertRaises(update.UpdateError):
                update.validate_release(modified)
        modified = copy.deepcopy(self.release)
        del modified["files"][self.prefix + "LICENSE"]
        with self.assertRaises(update.UpdateError):
            update.validate_release(modified)

    def test_candidate_uses_exact_publisher_route_and_rejects_duplicate_json(self):
        with mock.patch.object(update, "download", return_value=json.dumps(self.release).encode()) as download:
            self.assertEqual(self.release, update.candidate("0.2.0"))
        download.assert_called_once_with(update.PROJECT + "/releases/download/v0.2.0/relay-release.json", 256 * 1024)
        duplicate = json.dumps(self.release).replace('"schema": 1', '"schema": 1, "schema": 1')
        with mock.patch.object(update, "download", return_value=duplicate.encode()), self.assertRaises(update.UpdateError):
            update.candidate()
        with mock.patch.object(update, "download") as download, self.assertRaises(update.UpdateError):
            update.candidate("../../other")
        download.assert_not_called()

    def test_candidate_bad_json_and_unavailable_network_remain_unknown(self):
        for body in (b"not json", b"\xff", b"[]"):
            with mock.patch.object(update, "download", return_value=body), self.assertRaises(update.UpdateError):
                update.candidate()
        with mock.patch.object(update, "download", side_effect=update.UpdateError("synthetic unavailable")), self.assertRaises(update.UpdateError):
            update.candidate()

    def test_https_publisher_and_cdn_routes_exclude_other_origins(self):
        for url in ("https://github.com/example", "https://release-assets.githubusercontent.com/fixture"):
            self.assertEqual(url, update._https(url))
        for url in ("http://github.com/file", "https://github.com.evil.invalid/file",
                    "https://evil.invalid/github.com", "https://githubusercontent.com/file",
                    "https://user@github.com/file", "https://user:password@github.com/file",
                    "https://github.com:444/file", "file:///tmp/file"):
            with self.subTest(url=url), self.assertRaises(update.UpdateError):
                update._https(url)

    def test_malformed_redirect_port_has_normal_release_refusal(self):
        for url in ("https://github.com:not-a-port/file", "https://github.com:99999/file"):
            with self.subTest(url=url), self.assertRaises(update.UpdateError):
                update._https(url)

    def test_redirect_handler_validates_every_new_location(self):
        request = urllib.request.Request("https://github.com/fixture")
        handler = update._Redirect()
        redirected = handler.redirect_request(request, None, 302, "Found", {},
                                                "https://release-assets.githubusercontent.com/file")
        self.assertEqual("https://release-assets.githubusercontent.com/file", redirected.full_url)
        with self.assertRaises(update.UpdateError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://github.com/file")

    def test_download_checks_final_route_and_bound_without_real_network(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.url = "https://release-assets.githubusercontent.com/fixture"
        response.read.return_value = b"fixture"
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(update.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(b"fixture", update.download("https://github.com/fixture", 20))
            response.read.assert_called_with(21)
            response.url = "https://other.invalid/file"
            with self.assertRaises(update.UpdateError):
                update.download("https://github.com/fixture", 20)
            response.url = "https://github.com/file"
            response.read.return_value = b"x" * 21
            with self.assertRaises(update.UpdateError):
                update.download("https://github.com/fixture", 20)

    def test_incomplete_http_body_has_normal_download_error(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.url = "https://github.com/fixture"
        response.read.side_effect = http.client.IncompleteRead(b"synthetic partial body", 100)
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(update.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(update.UpdateError) as caught:
                update.download("https://github.com/fixture", 20)
        self.assertIn("unavailable", str(caught.exception).lower())

    def test_failed_command_diagnostic_never_recommends_reexecuting_mutation_or_deleted_bootstrap(self):
        command = ["/usr/bin/python3", "-I", "-S", "-B",
                   "/tmp/synthetic-deleted-download/runtime/bootstrap.py", "install",
                   "--release", "/tmp/synthetic-deleted-download/runtime", "--expected-activation", "none"]
        completed = subprocess.CompletedProcess(command, 1, "", "synthetic lost operation receipt")
        with mock.patch.object(update.subprocess, "run", return_value=completed):
            with self.assertRaises(update.UpdateError) as caught:
                REAL_COMMAND(command)
        message = str(caught.exception)
        self.assertIn("synthetic lost operation receipt", message)
        self.assertNotIn("synthetic-deleted-download", message)
        self.assertNotIn("Inspect:", message)


class ReleaseArchiveTests(ReleaseFixture):
    def test_compressed_header_expansion_is_bounded_before_tar_parsing(self):
        import gzip
        body = gzip.compress(b"x" * (update.MAX_TOTAL + 256 * 1024 + 1))
        self.reject_archive(body)

    def test_complete_bound_archive_extracts_only_exact_regular_file_bytes(self):
        runtime = update.extract_release(self.archive, update.validate_release(self.release), self.destination)
        self.assertEqual(self.destination / self.prefix / "runtime", runtime)
        actual = {str(path.relative_to(self.destination)): path.read_bytes()
                  for path in self.destination.rglob("*") if path.is_file()}
        self.assertEqual(self.files, actual)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600
                            for path in self.destination.rglob("*") if path.is_file()))

    def test_changed_archive_checksum_and_unreadable_archive_refuse_before_writes(self):
        with self.assertRaises(update.UpdateError):
            update.extract_release(self.archive + b"changed", self.release, self.destination)
        self.assertFalse(self.destination.exists())
        self.reject_archive(b"not a tar.gz archive")

    def test_duplicate_missing_extra_traversal_and_absolute_members_refuse_before_writes(self):
        variants = [self.entries + [self.entries[0]], self.entries[:-1],
                    self.entries + [(self.prefix + "unexpected", b"extra", tarfile.REGTYPE, "")],
                    self.entries + [(self.prefix + "../escape", b"escape", tarfile.REGTYPE, "")],
                    self.entries + [(str(self.base / "escape"), b"escape", tarfile.REGTYPE, "")]]
        for entries in variants:
            with self.subTest(names=[entry[0] for entry in entries]):
                self.reject_archive(archive_bytes(entries))
        self.assertFalse((self.base / "escape").exists())

    def test_symlink_hardlink_directory_and_device_members_refuse_before_writes(self):
        name = self.entries[0][0]
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.CHRTYPE):
            target = "/tmp/fixture-must-not-follow" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
            with self.subTest(kind=kind):
                self.reject_archive(archive_bytes([(name, b"", kind, target), *self.entries[1:]]))

    def test_changed_member_and_all_size_limits_refuse_before_writes(self):
        self.reject_archive(archive_bytes([(self.entries[0][0], b"changed", tarfile.REGTYPE, ""), *self.entries[1:]]))
        for name, value in (("MAX_ARCHIVE", len(self.archive) - 1), ("MAX_FILE", 10), ("MAX_TOTAL", 50)):
            with self.subTest(bound=name), mock.patch.object(update, name, value):
                self.reject_archive(self.archive)

    def test_extraction_preserves_source_built_runtime_bytes_and_release_identity(self):
        bundle = self.base / "source-built-bundle"
        release = relay_bootstrap.build_release(ROOT / "src", bundle, "0.2.0")
        files = {self.prefix + name: body for name, body in {
            "LICENSE": (ROOT / "LICENSE").read_bytes(),
            "README.md": ("Source: " + self.source + "\nRuntime: " + release.digest + "\n").encode(),
            "SHA256SUMS": b"Fixture outer hashes are supplied by release metadata.\n",
        }.items()}
        files.update({self.prefix + "runtime/" + str(path.relative_to(bundle)): path.read_bytes()
                      for path in bundle.rglob("*") if path.is_file()})
        body = archive_bytes([(name, data, tarfile.REGTYPE, "") for name, data in files.items()])
        metadata = {**self.release, "archive_sha256": digest(body), "release_id": release.digest,
                    "files": {name: digest(data) for name, data in files.items()}}
        extracted = update.extract_release(body, update.validate_release(metadata), self.destination)
        verified = relay_bootstrap.read_release(extracted, metadata["release_id"])
        self.assertEqual(release.digest, verified.digest)
        self.assertEqual(release.bootstrap_sha, verified.bootstrap_sha)
        self.assertEqual(dict(release.bodies), dict(verified.bodies))


class UpdateCommandTests(ReleaseFixture):
    def setUp(self):
        super().setUp()
        self.account = self.base / "account"
        self.launcher = str(self.account / ".local/bin/multithread")
        self.repo = self.base / "repo 'quoted' ;$(touch injected)"
        self.repo.mkdir()
        self.active = self.active_state("0.1.0", "c" * 64, "d" * 32)
        self.commands = []
        self.setup_calls = []
        self.expected = "d" * 32
        self.install_error = None
        self.after_install = None
        self.bootstrap_observation = None
        self.setup_response = subprocess.CompletedProcess([], 0, '{"state":"ready","next_actions":[]}', "")
        for patcher in (
            mock.patch.object(update, "_platform"),
            mock.patch.object(update, "candidate", return_value=self.release),
            mock.patch.object(update, "PINNED_RELEASE", self.release),
            mock.patch.object(update, "download", return_value=self.archive),
            mock.patch.object(update, "_command", side_effect=self.command),
            mock.patch.object(update.subprocess, "run", side_effect=self.setup_command),
            mock.patch.object(update.pwd, "getpwuid", return_value=mock.Mock(pw_dir=str(self.account))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def active_state(self, version, release_id, activation_id):
        return {"installed": True, "launcher": self.launcher,
                "activation": {"version": version, "release_id": release_id, "activation_id": activation_id}}

    def command(self, argv, **options):
        self.commands.append(argv)
        if argv[1:3] == ["runtime", "status"]:
            if self.after_install is not None and any("install" in row for row in self.commands):
                if isinstance(self.after_install, BaseException):
                    raise self.after_install
                return self.after_install
            return self.active
        operation = argv[5]
        if operation == "plan":
            return {"expected_activation": self.expected, "launcher": self.launcher, "release_id": self.release["release_id"]}
        if operation == "status":
            return self.bootstrap_observation if self.bootstrap_observation is not None else self.active
        if operation == "install":
            if self.install_error:
                raise self.install_error
            self.active = self.active_state("0.2.0", self.release["release_id"], "e" * 32)
            return self.active
        self.fail("Unexpected command: " + repr(argv))

    def setup_command(self, argv, **options):
        self.setup_calls.append(argv)
        self.assertEqual(self.launcher, argv[0])
        self.assertEqual("setup", argv[1])
        self.assertEqual(subprocess.DEVNULL, options["stdin"])
        self.assertNotIn("shell", options)
        if isinstance(self.setup_response, BaseException):
            raise self.setup_response
        return self.setup_response

    def invoke(self, *extra, install=False):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = (update.install_main if install else update.update_main)(["--json", *extra])
        return code, json.loads(output.getvalue())

    def invoke_human(self, *extra, install=False):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = (update.install_main if install else update.update_main)(list(extra))
        return code, output.getvalue(), errors.getvalue()

    def ready_setup(self):
        return {"state": "ready", "repo": str(self.repo),
                "runtime": {"state": "verified"},
                "repository": {"state": "verified", "enrollment": {"state": "verified"},
                               "identity": {"git_common_dir": str(self.repo / ".git")}},
                "providers": {"codex": {"state": "prepared"}, "claude": {"state": "missing"}},
                "first_collaboration_url": update.PROJECT + "/blob/main/docs/PEER.md#first-collaboration",
                "next_actions": [{"stage": "codex", "action": "When provider launch is authorized, review the invocation.",
                                  "command": [self.launcher, "launch", "codex", "--repo", str(self.repo)]}]}

    def approval(self):
        return ["--version", "0.2.0", "--approve-sha256", self.release["release_id"],
                "--expected-activation", "d" * 32, "--yes"]

    def test_json_check_reports_exact_apply_without_downloading_or_writing(self):
        code, result = self.invoke()
        self.assertEqual(0, code)
        self.assertEqual("release_available", result["state"])
        self.assertEqual("not_checked", result["package_verification"])
        self.assertIn(self.release["release_id"], result["apply_command"])
        self.assertIn("d" * 32, result["apply_command"])
        update.download.assert_not_called()
        self.assertEqual([[self.launcher, "runtime", "status"]], self.commands)
        self.assertEqual([], self.setup_calls)

    def test_check_apply_arguments_retain_explicit_repository_and_shell_literals(self):
        code, result = self.invoke("--enroll-repo", str(self.repo))
        self.assertEqual(0, code)
        self.assertEqual(["--enroll-repo", str(self.repo)], result["apply_argv"][-2:])
        self.assertEqual(result["apply_argv"], shlex.split(result["apply_command"]))
        self.assertEqual([], self.setup_calls)

    def test_human_check_shows_the_exact_guarded_update_with_optional_repository(self):
        for repository in ([], ["--enroll-repo", str(self.repo)]):
            with self.subTest(repository=bool(repository)):
                code, report = self.invoke("--check", *repository)
                human_code, output, _ = self.invoke_human("--check", *repository)
                self.assertEqual((0, 0), (code, human_code))
                command = next(line.split(": ", 1)[1] for line in output.splitlines()
                               if line.startswith("Apply this exact update selection: "))
                self.assertEqual(report["apply_argv"], shlex.split(command))
                self.assertNotIn("setup --apply", output)
                self.assertIn("Selected release: 0.2.0", output)
        update.download.assert_not_called()
        self.assertEqual([], self.setup_calls)

    def test_human_current_release_only_offers_setup_for_a_chosen_repository(self):
        self.active = self.active_state("0.2.0", self.release["release_id"], "d" * 32)
        code, output, _ = self.invoke_human("--check")
        self.assertEqual(0, code)
        self.assertIn("Multithread: up to date", output)
        self.assertIn("Repository setup: not requested.", output)
        self.assertNotIn("setup --", output)
        code, output, _ = self.invoke_human("--check", "--repo", str(self.repo))
        self.assertEqual(0, code)
        for label, option in (("Check repository", "--check"),
                              ("To explicitly enroll/check this repository", "--apply")):
            line = next(line for line in output.splitlines() if line.startswith(label + ": "))
            argv = shlex.split(line.split(": ", 1)[1])
            self.assertEqual([self.launcher, "setup", "--repo", str(self.repo), option], argv)
        self.assertEqual([], self.setup_calls)
        update.download.assert_not_called()

    def test_human_code_only_install_and_update_do_not_assume_current_repository(self):
        for install in (False, True):
            with self.subTest(install=install):
                self.active = ({"installed": False, "activation": None, "launcher": self.launcher}
                               if install else self.active_state("0.1.0", "c" * 64, "d" * 32))
                self.expected = None if install else "d" * 32
                code, output, _ = self.invoke_human(*(["--yes"] if install else self.approval()), install=install)
                self.assertEqual(0, code)
                self.assertIn("Multithread: runtime " + ("installed" if install else "updated"), output)
                self.assertIn("Repository setup: not requested.", output)
                self.assertNotIn("ready for setup", output)
                self.assertNotIn("setup --", output)
                self.assertNotIn(str(Path.cwd()), output)
        self.assertEqual([], self.setup_calls)

    def test_human_install_surfaces_captured_readiness_providers_and_first_collaboration(self):
        self.active = {"installed": False, "activation": None, "launcher": self.launcher}
        self.expected = None
        report = self.ready_setup()
        self.setup_response = subprocess.CompletedProcess([], 0, json.dumps(report), "")
        code, output, _ = self.invoke_human("--yes", "--repo", str(self.repo), install=True)
        self.assertEqual(0, code)
        for expected in ("Runtime installation: installed", "Repository readiness: ready",
                         "Runtime: verified", "Repository: verified", "codex: prepared", "claude: missing",
                         "Git common directory: " + str(self.repo / ".git"),
                         "First collaboration, when you authorize provider use: " + report["first_collaboration_url"]):
            self.assertIn(expected, output)
        self.assertIn("Provider authentication, hook delivery and tools are not checked", output)
        self.assertEqual(1, len(self.setup_calls), "display must reuse the captured setup result")
        command = next(line.split(": ", 1)[1] for line in output.splitlines() if line.startswith("  Command: "))
        self.assertEqual(report["next_actions"][0]["command"], shlex.split(command))

    def test_human_install_labels_a_persons_launch_as_theirs(self):
        self.active = {"installed": False, "activation": None, "launcher": self.launcher}
        self.expected = None
        report = self.ready_setup()
        report["next_actions"][0]["actor"] = "person"
        self.setup_response = subprocess.CompletedProcess([], 0, json.dumps(report), "")
        code, output, _ = self.invoke_human("--yes", "--repo", str(self.repo), install=True)
        self.assertEqual(0, code)
        self.assertIn("  Your command: " + shlex.join(report["next_actions"][0]["command"]), output)
        self.assertFalse(any(line.startswith("  Command: ") for line in output.splitlines()))

    def test_human_setup_retains_provider_preparation_failure_without_changing_readiness(self):
        report = self.ready_setup()
        report["providers"]["codex"] = {"state": "unavailable", "message": "Synthetic provider executable is not approved."}
        self.setup_response = subprocess.CompletedProcess([], 0, json.dumps(report), "")
        code, output, _ = self.invoke_human(*self.approval(), "--repo", str(self.repo))
        self.assertEqual(0, code)
        self.assertIn("Repository readiness: ready", output)
        self.assertIn("codex: unavailable; Synthetic provider executable is not approved.", output)
        self.assertEqual(1, len(self.setup_calls))

    def test_malformed_captured_setup_actions_preserve_success_and_raw_receipt(self):
        action = self.ready_setup()["next_actions"][0]
        cases = [(value, "Next actions unavailable:") for value in (None, False, 7, "synthetic text", {})]
        cases += [([action, value, action], "Next action unavailable:") for value in (None, False, 7, [])]
        cases += [([action, {**action, "command": value}, action], "Command unavailable:")
                  for value in (None, False, 7, {}, "synthetic command", [], [self.launcher, None], [self.launcher, 7])]
        for actions, warning in cases:
            with self.subTest(actions=actions):
                self.active = self.active_state("0.1.0", "c" * 64, "d" * 32)
                self.commands.clear()
                self.setup_calls.clear()
                report = self.ready_setup()
                report["next_actions"] = actions
                self.setup_response = subprocess.CompletedProcess([], 0, json.dumps(report), "")
                with mock.patch.object(update, "_finish", wraps=update._finish) as finish:
                    code, output, _ = self.invoke_human(*self.approval(), "--repo", str(self.repo))
                self.assertEqual(0, code)
                finish.assert_called_once()
                captured = finish.call_args.args[0]
                self.assertEqual("setup_checked", captured["state"])
                self.assertEqual("updated", captured["installation"])
                self.assertEqual(report, captured["repository"])
                self.assertIn("Runtime installation: updated", output)
                self.assertIn("Repository readiness: ready", output)
                self.assertIn(warning, output)
                if isinstance(actions, list):
                    self.assertEqual(2, output.count("  Command: " + shlex.join(action["command"])),
                                     "unusable actions must not hide the valid actions around them")
                self.assertEqual(1, sum("install" in row for row in self.commands))
                self.assertEqual([[self.launcher, "setup", "--apply", "--repo", str(self.repo), "--json"]], self.setup_calls)
                structured = io.StringIO()
                with redirect_stdout(structured):
                    update._finish(captured, update._parser(False).parse_args(["--json"]))
                self.assertEqual(captured, json.loads(structured.getvalue()))

    def test_absent_or_empty_captured_actions_do_not_claim_an_observation_failure(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.active = self.active_state("0.1.0", "c" * 64, "d" * 32)
                report = self.ready_setup()
                report["next_actions"] = []
                if missing:
                    report.pop("next_actions")
                self.setup_response = subprocess.CompletedProcess([], 0, json.dumps(report), "")
                code, output, _ = self.invoke_human(*self.approval(), "--repo", str(self.repo))
                self.assertEqual(0, code)
                self.assertIn("Repository readiness: ready", output)
                self.assertNotIn("actions unavailable", output)
                self.assertNotIn("action unavailable", output)
                self.assertNotIn("Command unavailable", output)

    def test_malformed_actions_keep_setup_failure_and_distinguish_runtime_detail(self):
        report = self.ready_setup()
        report.update(state="not_ready", next_actions=None,
                      runtime={"state": "unavailable", "message": "Synthetic runtime observation could not be read."})
        self.setup_response = subprocess.CompletedProcess([], 1, json.dumps(report), "")
        with mock.patch.object(update, "_finish", wraps=update._finish) as finish:
            code, output, _ = self.invoke_human(*self.approval(), "--repo", str(self.repo))
        self.assertEqual(1, code)
        finish.assert_called_once()
        captured = finish.call_args.args[0]
        self.assertEqual("needs_attention", captured["state"])
        self.assertEqual("updated", captured["installation"])
        self.assertEqual(report, captured["repository"])
        self.assertIn("Runtime: unavailable", output)
        self.assertIn("Runtime detail: Synthetic runtime observation could not be read.", output)
        self.assertNotIn("Runtime: Synthetic", output)
        self.assertIn("Next actions unavailable:", output)
        self.assertIn("Check repository before retrying setup:", output)
        self.assertEqual(1, sum("install" in row for row in self.commands))
        self.assertEqual(1, len(self.setup_calls))

    def test_human_partial_setup_exposes_captured_failure_and_read_only_continuation(self):
        report = self.ready_setup()
        report.update(state="not_ready", providers={"codex": {"state": "not_checked"}}, next_actions=[])
        report.pop("first_collaboration_url")
        report["repository"].update(state="uncertain", enrollment={
            "state": "uncertain", "message": "Synthetic enrollment receipt unavailable; inspect before retrying.",
            "stderr": "first diagnostic\nsecond diagnostic\x1b[31m", "stderr_truncated": True},
            doctor={"state": "verified"}, status={"state": "verified"})
        self.setup_response = subprocess.CompletedProcess([], 1, json.dumps(report), "")
        code, output, _ = self.invoke_human(*self.approval(), "--repo", str(self.repo))
        self.assertEqual(1, code)
        for expected in ("Runtime installation: updated", "Repository readiness: not ready",
                         "Repository: uncertain", "Enrollment observation: uncertain",
                         "Synthetic enrollment receipt unavailable", "Enrollment diagnostic: first diagnostic",
                         "Enrollment diagnostic: second diagnostic\\u001b[31m", "diagnostic output was truncated"):
            self.assertIn(expected, output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("First collaboration", output)
        check = next(line.split(": ", 1)[1] for line in output.splitlines()
                     if line.startswith("Check repository before retrying setup: "))
        self.assertEqual([self.launcher, "setup", "--check", "--repo", str(self.repo), "--json"], shlex.split(check))
        self.assertEqual(1, len(self.setup_calls))

    def test_human_uncertain_setup_does_not_suggest_repeating_enrollment(self):
        self.setup_response = subprocess.TimeoutExpired([self.launcher], 90)
        code, output, _ = self.invoke_human(*self.approval(), "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertIn("Runtime installation: updated", output)
        self.assertIn("Repository readiness: uncertain", output)
        self.assertIn("Check repository before retrying setup:", output)
        self.assertNotIn("--apply", output)
        self.assertEqual(1, len(self.setup_calls))

    def test_human_uncertain_install_shows_inspection_without_setup(self):
        self.install_error = update.UpdateError("Synthetic lost receipt\x1b[2J; inspect current state.")
        code, output, errors = self.invoke_human(*self.approval(), "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertIn("Runtime installation: unknown", output)
        self.assertIn("Synthetic lost receipt\\u001b[2J", output)
        self.assertNotIn("\x1b", output + errors)
        self.assertIn("Inspect: " + shlex.join([self.launcher, "runtime", "inspect"]), output)
        self.assertNotIn("--apply", output)
        self.assertNotIn("Active release:", output)
        self.assertEqual(1, sum("install" in row for row in self.commands))
        self.assertEqual([], self.setup_calls)

    def test_unsafe_paths_are_displayed_as_exact_json_argv_without_changing_json_or_execution(self):
        self.repo = self.base / "repo\n\x1b[31m\t\u202e 'quoted'"
        self.repo.mkdir()
        self.launcher = str(self.account / "synthetic\n\x1b-launcher")
        self.active["launcher"] = self.launcher
        report = self.ready_setup()
        report["providers"]["codex"] = {"state": "unavailable", "message": "Synthetic\r\x1b[2J provider error"}
        self.setup_response = subprocess.CompletedProcess([], 0, json.dumps(report), "")
        code, output, errors = self.invoke_human(*self.approval(), "--repo", str(self.repo))
        self.assertEqual(0, code)
        self.assertTrue(all(character.isprintable() or character == "\n" for character in output + errors))
        self.assertIn("Synthetic\\r\\u001b[2J provider error", output)
        command = next(line.split(": ", 1)[1] for line in output.splitlines() if line.startswith("  Command (JSON argv): "))
        self.assertEqual(report["next_actions"][0]["command"], json.loads(command))
        self.assertEqual([self.launcher, "setup", "--apply", "--repo", str(self.repo), "--json"], self.setup_calls[0])
        self.active = self.active_state("0.1.0", "c" * 64, "d" * 32)
        code, result = self.invoke(*self.approval(), "--repo", str(self.repo))
        self.assertEqual(0, code)
        self.assertEqual(report, result["repository"], "human rendering must not alter the captured JSON receipt")
        self.assertEqual(self.setup_calls[0], shlex.split(result["setup_command"]))

    def test_unsafe_update_check_continuation_retains_original_arguments(self):
        self.repo = self.base / "repo\n\x1b[2J\t\u202e"
        self.repo.mkdir()
        code, report = self.invoke("--check", "--repo", str(self.repo))
        human_code, output, _ = self.invoke_human("--check", "--repo", str(self.repo))
        self.assertEqual((0, 0), (code, human_code))
        self.assertTrue(all(character.isprintable() or character == "\n" for character in output))
        command = next(line.split(": ", 1)[1] for line in output.splitlines()
                       if line.startswith("Apply this exact update selection (JSON argv): "))
        self.assertEqual(report["apply_argv"], json.loads(command))
        self.assertEqual(report["apply_argv"], shlex.split(report["apply_command"]))
        self.assertEqual([], self.setup_calls)

    def test_interactive_review_escapes_paths_before_approval_without_execution_changes(self):
        self.repo = self.base / "repo\n\x1b[2J"
        self.repo.mkdir()
        self.launcher = str(self.account / "synthetic\nlauncher")
        self.active["launcher"] = self.launcher
        for install in (False, True):
            with (self.subTest(install=install),
                  mock.patch.object(update.sys, "stdin", mock.Mock(isatty=lambda: True)),
                  mock.patch("builtins.input", return_value="cancel")):
                code, output, errors = self.invoke_human("--repo", str(self.repo), install=install)
                self.assertEqual(0, code)
                self.assertIn("repo\\n\\u001b[2J", output)
                self.assertTrue(all(character.isprintable() or character == "\n" for character in output + errors))
        self.assertFalse(any("install" in row for row in self.commands))
        self.assertEqual([], self.setup_calls)

    def test_noninteractive_update_requires_exact_version_digest_and_activation(self):
        for arguments in (["--yes"], ["--yes", "--version", "0.2.0"],
                          ["--yes", "--version", "0.2.0", "--approve-sha256", self.release["release_id"]]):
            code, result = self.invoke(*arguments)
            self.assertEqual(1, code)
            self.assertEqual("unchanged", result["installation"])
        update.download.assert_not_called()

    def test_explicit_candidate_mismatch_refuses_before_package_or_local_commands(self):
        code, result = self.invoke("--yes", "--approve-sha256", "0" * 64)
        self.assertEqual(1, code)
        self.assertEqual("release_selection", result["stage"])
        self.assertEqual([], self.commands)
        update.download.assert_not_called()

    def test_corrupt_archive_stops_before_bootstrap_execution(self):
        update.download.return_value = self.archive + b"synthetic changed bytes"
        code, result = self.invoke(*self.approval())
        self.assertEqual(1, code)
        self.assertEqual("unchanged", result["installation"])
        self.assertEqual([[self.launcher, "runtime", "status"]], self.commands)
        self.assertEqual([], self.setup_calls)

    def test_incomplete_http_body_returns_structured_unavailable_without_incoming_code(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.url = "https://github.com/fixture"
        response.read.side_effect = http.client.IncompleteRead(b"synthetic partial body", 100)
        opener = mock.Mock()
        opener.open.return_value = response
        with (mock.patch.object(update, "download", REAL_DOWNLOAD),
              mock.patch.object(update.urllib.request, "build_opener", return_value=opener)):
            code, result = self.invoke(*self.approval())
        self.assertEqual(1, code)
        self.assertEqual("unavailable", result["state"])
        self.assertEqual("unchanged", result["installation"])
        self.assertEqual([[self.launcher, "runtime", "status"]], self.commands)
        self.assertEqual([], self.setup_calls)

    def test_approved_update_runs_incoming_bootstrap_once_and_observes_new_release(self):
        code, result = self.invoke(*self.approval())
        self.assertEqual(0, code)
        self.assertEqual("updated", result["installation"])
        self.assertEqual("ready_for_setup", result["state"])
        installs = [argv for argv in self.commands if "install" in argv]
        self.assertEqual(1, len(installs))
        self.assertEqual(["/usr/bin/python3", "-I", "-S", "-B"], installs[0][:4])
        self.assertTrue(installs[0][4].endswith("/runtime/bootstrap.py"))
        self.assertEqual(["--expected-activation", "d" * 32], installs[0][-2:])
        self.assertEqual(self.release["release_id"], result["current"]["activation"]["release_id"])
        self.assertEqual([], self.setup_calls)

    def test_plan_activation_race_refuses_before_install_attempt(self):
        self.expected = "f" * 32
        code, result = self.invoke(*self.approval())
        self.assertEqual(1, code)
        self.assertEqual("unchanged", result["installation"])
        self.assertFalse(any("install" in row for row in self.commands))

    def test_reused_selection_with_new_activation_after_plan_refuses(self):
        self.bootstrap_observation = self.active_state("0.2.0", self.release["release_id"], "f" * 32)
        code, result = self.invoke(*self.approval())
        self.assertEqual(1, code)
        self.assertFalse(any("install" in row for row in self.commands))
        self.assertIn("changed", result["message"])

    def test_uncertain_install_never_retries_or_reports_unchanged(self):
        previous = copy.deepcopy(self.active)
        self.install_error = update.UpdateError("Synthetic lost install receipt; inspect current state")
        code, result = self.invoke(*self.approval())
        self.assertEqual(1, code)
        self.assertEqual("unknown", result["installation"])
        self.assertIsNone(result["current"])
        self.assertEqual(previous, result["previous"])
        self.assertEqual([self.launcher, "runtime", "inspect"], shlex.split(result["inspect_command"]))
        self.assertIn("/docs/engineering/INSTALLATION.md", result["recovery_url"])
        self.assertEqual(1, sum("install" in row for row in self.commands))
        self.assertEqual([], self.setup_calls)

    def test_lost_final_status_separates_previous_from_applied_and_unknown_current(self):
        previous = copy.deepcopy(self.active)
        self.after_install = update.UpdateError("Synthetic final runtime observation unavailable")
        code, result = self.invoke(*self.approval(), "--enroll-repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertEqual("updated", result["installation"])
        self.assertIsNone(result["current"])
        self.assertEqual(previous, result["previous"])
        self.assertEqual(self.release["release_id"], result["applied"]["activation"]["release_id"])
        self.assertEqual("e" * 32, result["applied"]["activation"]["activation_id"])
        self.assertEqual([self.launcher, "runtime", "inspect"], shlex.split(result["inspect_command"]))
        self.assertIn("/docs/engineering/INSTALLATION.md", result["recovery_url"])
        self.assertEqual(1, sum("install" in row for row in self.commands))
        self.assertEqual([], self.setup_calls)

    def test_post_install_identity_change_preserves_applied_stage_and_stops_setup(self):
        self.after_install = self.active_state("0.3.0", "f" * 64, "f" * 32)
        code, result = self.invoke(*self.approval(), "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertEqual("updated", result["installation"])
        self.assertEqual([], self.setup_calls)

    def test_setup_failure_keeps_successful_runtime_installation(self):
        self.setup_response = subprocess.CompletedProcess([], 1, '{"state":"not_ready","repository":{"state":"refused"}}', "")
        code, result = self.invoke(*self.approval(), "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertEqual("updated", result["installation"])
        self.assertEqual("needs_attention", result["state"])
        self.assertEqual("not_ready", result["repository"]["state"])
        self.assertEqual(1, len(self.setup_calls))
        self.assertIn(str(self.repo), self.setup_calls[0])

    def test_invalid_or_nonready_setup_receipt_cannot_turn_zero_exit_into_success(self):
        for body in ("not json", "[]", "{}", '{"state":"not_ready"}'):
            with self.subTest(body=body):
                self.active = self.active_state("0.1.0", "c" * 64, "d" * 32)
                self.setup_response = subprocess.CompletedProcess([], 0, body, "")
                code, result = self.invoke(*self.approval(), "--repo", str(self.repo))
                self.assertEqual(1, code)
                self.assertEqual("needs_attention", result["state"])
                self.assertEqual("updated", result["installation"])

    def test_unreadable_setup_output_keeps_runtime_success_and_reports_attention(self):
        self.setup_response = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic invalid output")
        code, result = self.invoke(*self.approval(), "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertEqual("updated", result["installation"])
        self.assertEqual("needs_attention", result["state"])

    def test_same_release_check_reuses_without_downloading_or_setup(self):
        self.active = self.active_state("0.2.0", self.release["release_id"], "d" * 32)
        code, result = self.invoke("--check", "--repo", str(self.repo))
        self.assertEqual(0, code)
        self.assertEqual("up_to_date", result["state"])
        self.assertEqual("reused", result["installation"])
        update.download.assert_not_called()
        self.assertEqual([], self.setup_calls)

    def test_same_release_json_check_returns_exact_repository_continuation_commands(self):
        self.active = self.active_state("0.2.0", self.release["release_id"], "d" * 32)
        code, result = self.invoke("--enroll-repo", str(self.repo))
        self.assertEqual(0, code)
        self.assertEqual("up_to_date", result["state"])
        self.assertEqual("not_checked", result["repository"])
        for key, option in (("setup_command", "--apply"), ("check_command", "--check")):
            command = shlex.split(result[key])
            self.assertEqual([self.launcher, "setup"], command[:2])
            self.assertIn(option, command)
            self.assertEqual(str(self.repo), command[command.index("--repo") + 1])
        self.assertEqual([], self.setup_calls)
        update.download.assert_not_called()

    def test_same_release_apply_still_completes_requested_repository_setup(self):
        self.active = self.active_state("0.2.0", self.release["release_id"], "d" * 32)
        code, result = self.invoke(*self.approval(), "--repo", str(self.repo))
        self.assertEqual(0, code)
        self.assertEqual("reused", result["installation"])
        self.assertEqual("ready", result["repository"]["state"])
        self.assertEqual(1, len(self.setup_calls))
        self.assertFalse(any("install" in row for row in self.commands))

    def test_same_release_interactive_request_confirms_and_completes_setup(self):
        self.active = self.active_state("0.2.0", self.release["release_id"], "d" * 32)
        output = io.StringIO()
        with (redirect_stdout(output), redirect_stderr(io.StringIO()),
              mock.patch.object(update.sys, "stdin", mock.Mock(isatty=lambda: True)),
              mock.patch("builtins.input", return_value="setup") as answer):
            code = update.update_main(["--enroll-repo", str(self.repo)])
        self.assertEqual(0, code)
        answer.assert_called_once()
        self.assertEqual(1, len(self.setup_calls))
        self.assertIn("setup checked", output.getvalue())
        update.download.assert_not_called()

    def test_current_release_enrollment_approval_interrupt_cancels_without_recovery(self):
        self.active = self.active_state("0.2.0", self.release["release_id"], "d" * 32)
        with (mock.patch.object(update.sys, "stdin", mock.Mock(isatty=lambda: True)),
              mock.patch("builtins.input", side_effect=KeyboardInterrupt),
              mock.patch.object(update, "_finish", wraps=update._finish) as finish):
            code, output, _ = self.invoke_human("--enroll-repo", str(self.repo))
        self.assertEqual(130, code)
        result = finish.call_args.args[0]
        self.assertEqual("cancelled", result["state"])
        self.assertEqual("reused", result["installation"])
        self.assertEqual("not_checked", result["repository"])
        self.assertEqual(self.active, result["current"])
        self.assertIn("Multithread: cancelled", output)
        self.assertIn("no installation or enrollment was applied", output)
        for unwanted in ("Inspect:", "Recovery:", "Check repository:", "--apply", "retrying"):
            self.assertNotIn(unwanted, output)
        self.assertEqual([[self.launcher, "runtime", "status"]], self.commands)
        self.assertEqual([], self.setup_calls)
        update.download.assert_not_called()

    def test_current_release_setup_interrupt_still_reports_uncertain_enrollment(self):
        self.active = self.active_state("0.2.0", self.release["release_id"], "d" * 32)
        self.setup_response = KeyboardInterrupt()
        code, result = self.invoke(*self.approval(), "--enroll-repo", str(self.repo))
        self.assertEqual(130, code)
        self.assertEqual("repository_setup", result["stage"])
        self.assertEqual("needs_attention", result["state"])
        self.assertEqual("reused", result["installation"])
        self.assertEqual({"state": "uncertain"}, result["repository"])
        self.assertEqual([self.launcher, "setup", "--check", "--repo", str(self.repo), "--json"],
                         shlex.split(result["check_command"]))
        self.assertEqual([[self.launcher, "runtime", "status"]], self.commands)
        self.assertEqual(1, len(self.setup_calls))
        update.download.assert_not_called()

    def test_reporting_interrupt_preserves_captured_setup_success_or_refusal(self):
        finish = update._finish
        receipts = ((self.ready_setup(), 0, "setup_checked", "complete"),
                    ({"state": "not_ready", "repository": {"state": "refused",
                      "message": "Synthetic enrollment refusal"}}, 1, "needs_attention", "repository_setup"))
        for receipt, setup_code, state, stage in receipts:
            with self.subTest(state=state):
                self.active = self.active_state("0.2.0", self.release["release_id"], "d" * 32)
                self.commands.clear()
                self.setup_calls.clear()
                self.setup_response = subprocess.CompletedProcess([], setup_code, json.dumps(receipt), "")
                interrupted = False

                def interrupt_once(result, args, code=0):
                    nonlocal interrupted
                    if not interrupted:
                        interrupted = True
                        raise KeyboardInterrupt()
                    return finish(result, args, code)

                with mock.patch.object(update, "_finish", side_effect=interrupt_once):
                    code, result = self.invoke(*self.approval(), "--enroll-repo", str(self.repo))
                self.assertEqual(130, code)
                self.assertEqual(receipt, result["repository"])
                self.assertEqual(state, result["state"])
                self.assertEqual(stage, result["stage"])
                self.assertEqual("reused", result["installation"])
                self.assertIn("captured repository setup result is preserved", result["message"])
                self.assertEqual([[self.launcher, "runtime", "status"]], self.commands)
                self.assertEqual(1, len(self.setup_calls))
                update.download.assert_not_called()

    def test_post_reuse_status_interrupt_remains_unavailable_with_inspection(self):
        self.active = self.active_state("0.2.0", self.release["release_id"], "d" * 32)

        def interrupt_status(argv):
            if argv[1:3] == ["runtime", "status"]:
                self.commands.append(argv)
                raise KeyboardInterrupt()
            return self.command(argv)

        with mock.patch.object(update, "_command", side_effect=interrupt_status):
            code, result = self.invoke("--yes", "--enroll-repo", str(self.repo), install=True)
        self.assertEqual(130, code)
        self.assertEqual("unavailable", result["state"])
        self.assertEqual("installation", result["stage"])
        self.assertEqual("reused", result["installation"])
        self.assertIsNone(result["current"])
        self.assertEqual([self.launcher, "runtime", "inspect"], shlex.split(result["inspect_command"]))
        self.assertFalse(any("install" in row for row in self.commands))
        self.assertEqual([], self.setup_calls)

    def test_standalone_install_uses_pinned_metadata_without_executing_unknown_launcher(self):
        self.active = {"installed": False, "activation": None, "launcher": self.launcher}
        self.expected = None
        code, result = self.invoke("--yes", install=True)
        self.assertEqual(0, code)
        self.assertEqual("installed", result["installation"])
        update.candidate.assert_not_called()
        first = self.commands[0]
        self.assertEqual("/usr/bin/python3", first[0])
        self.assertEqual("plan", first[5])
        installs = [row for row in self.commands if "install" in row]
        self.assertEqual(["--expected-activation", "none"], installs[0][-2:])

    def test_standalone_matching_release_reuses_activation_and_runs_requested_setup(self):
        self.active = self.active_state("0.2.0", self.release["release_id"], "d" * 32)
        code, result = self.invoke("--yes", "--enroll-repo", str(self.repo), install=True)
        self.assertEqual(0, code)
        self.assertEqual("reused", result["installation"])
        self.assertEqual("d" * 32, result["current"]["activation"]["activation_id"])
        self.assertEqual(1, len(self.setup_calls))
        self.assertFalse(any("install" in row for row in self.commands))

    def test_standalone_unknown_selector_refusal_never_executes_that_command(self):
        with mock.patch.object(update, "_command", side_effect=update.UpdateError("synthetic unknown selector preserved")) as command:
            code, result = self.invoke("--yes", install=True)
        self.assertEqual(1, code)
        self.assertEqual("unchanged", result["installation"])
        command.assert_called_once()
        self.assertEqual("/usr/bin/python3", command.call_args.args[0][0])
        self.assertEqual("plan", command.call_args.args[0][5])
        self.assertFalse(result.get("inspect_command"))
        self.assertIn("/docs/engineering/INSTALLATION.md", result["recovery_url"])
        self.assertEqual([], self.setup_calls)

    def test_standalone_without_generated_pin_refuses_before_network(self):
        with mock.patch.object(update, "PINNED_RELEASE", None):
            code, result = self.invoke("--yes", install=True)
        self.assertEqual(1, code)
        self.assertEqual("unchanged", result["installation"])
        self.assertEqual([], self.commands)
        update.download.assert_not_called()

    def test_newer_active_version_refuses_downgrade(self):
        self.active = self.active_state("0.3.0", "c" * 64, "d" * 32)
        code, result = self.invoke(*self.approval())
        self.assertEqual(1, code)
        self.assertIn("rollback", result["message"])
        update.download.assert_not_called()

    def test_standalone_older_pin_refuses_downgrade_before_install(self):
        self.active = self.active_state("0.3.0", "c" * 64, "d" * 32)
        code, result = self.invoke("--yes", install=True)
        self.assertEqual(1, code)
        self.assertIn("rollback", result["message"])
        self.assertFalse(any("install" in row for row in self.commands))
        self.assertEqual([], self.setup_calls)

    def test_interactive_cancellation_makes_no_install_or_setup_attempt(self):
        output = io.StringIO()
        with (redirect_stdout(output), redirect_stderr(io.StringIO()),
              mock.patch.object(update.sys, "stdin", mock.Mock(isatty=lambda: True)),
              mock.patch("builtins.input", return_value="cancel")):
            code = update.update_main(["--repo", str(self.repo)])
        self.assertEqual(0, code)
        self.assertIn("cancelled", output.getvalue())
        self.assertEqual([[self.launcher, "runtime", "status"]], self.commands,
                         "candidate bootstrap must not execute before publisher/selection approval")
        self.assertFalse(any("install" in row for row in self.commands))
        self.assertEqual([], self.setup_calls)

    def test_standalone_approval_interrupt_reports_no_mutation_without_traceback(self):
        with (mock.patch.object(update.sys, "stdin", mock.Mock(isatty=lambda: True)),
              mock.patch("builtins.input", side_effect=KeyboardInterrupt)):
            code, output, errors = self.invoke_human("--enroll-repo", str(self.repo), install=True)
        self.assertEqual(130, code)
        self.assertIn("Runtime installation: unchanged", output)
        self.assertIn("Interrupted; no installation or enrollment was applied.", output)
        self.assertNotIn("Traceback", output + errors)
        self.assertNotIn("Recovery:", output)
        self.assertTrue(self.commands)
        self.assertTrue(all(row[0] == "/usr/bin/python3" and row[5] in {"plan", "status"}
                            for row in self.commands))
        self.assertEqual([], self.setup_calls)

    def test_standalone_install_interrupt_retains_unknown_outcome_and_recovery(self):
        previous = copy.deepcopy(self.active)
        self.install_error = KeyboardInterrupt()
        code, result = self.invoke("--yes", "--enroll-repo", str(self.repo), install=True)
        self.assertEqual(130, code)
        self.assertEqual("installation", result["stage"])
        self.assertEqual("unavailable", result["state"])
        self.assertEqual("unknown", result["installation"])
        self.assertIsNone(result["current"])
        self.assertEqual(previous, result["previous"])
        self.assertIn("inspect", result["message"])
        self.assertIn("/docs/engineering/INSTALLATION.md", result["recovery_url"])
        self.assertNotIn("inspect_command", result)
        self.assertEqual(1, sum("install" in row for row in self.commands))
        self.assertEqual([], self.setup_calls)

    def test_post_install_interrupt_preserves_success_receipt_and_unknown_current(self):
        self.active = {"installed": False, "activation": None, "launcher": self.launcher}
        self.expected = None
        self.after_install = KeyboardInterrupt()
        code, result = self.invoke("--yes", "--enroll-repo", str(self.repo), install=True)
        self.assertEqual(130, code)
        self.assertEqual("installation", result["stage"])
        self.assertEqual("installed", result["installation"])
        self.assertIsNone(result["current"])
        self.assertEqual(self.release["release_id"], result["applied"]["activation"]["release_id"])
        self.assertEqual([self.launcher, "runtime", "inspect"], shlex.split(result["inspect_command"]))
        self.assertEqual(1, sum("install" in row for row in self.commands))
        self.assertEqual([], self.setup_calls)

    def test_setup_interrupt_retains_runtime_success_and_read_only_recovery(self):
        self.setup_response = KeyboardInterrupt()
        code, result = self.invoke(*self.approval(), "--enroll-repo", str(self.repo))
        self.assertEqual(130, code)
        self.assertEqual("repository_setup", result["stage"])
        self.assertEqual("needs_attention", result["state"])
        self.assertEqual("updated", result["installation"])
        self.assertEqual(self.release["release_id"], result["current"]["activation"]["release_id"])
        self.assertEqual({"state": "uncertain"}, result["repository"])
        self.assertEqual([self.launcher, "setup", "--check", "--repo", str(self.repo), "--json"],
                         shlex.split(result["check_command"]))
        self.assertEqual(1, sum("install" in row for row in self.commands))
        self.assertEqual(1, len(self.setup_calls))
        output = io.StringIO()
        with redirect_stdout(output):
            update._finish(result, update._parser(False).parse_args(["--enroll-repo", str(self.repo)]), code)
        self.assertIn("Runtime installation: updated", output.getvalue())
        self.assertIn("Repository readiness: uncertain", output.getvalue())
        self.assertIn("Check repository before retrying setup: " + result["check_command"], output.getvalue())
        self.assertNotIn("--apply", output.getvalue())

    def test_installed_update_interrupt_returns_json_before_dispatcher_fallback(self):
        from relay_runtime import cli
        self.install_error = KeyboardInterrupt()
        output, errors = io.StringIO(), io.StringIO()
        alias_check = mock.Mock()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(["update", "--json", *self.approval()], command_alias_check=alias_check)
        self.assertEqual(130, code)
        result = json.loads(output.getvalue())
        self.assertEqual("unknown", result["installation"])
        self.assertEqual("installation", result["stage"])
        self.assertEqual([self.launcher, "runtime", "inspect"], shlex.split(result["inspect_command"]))
        alias_check.assert_called_once_with()
        self.assertNotIn("multithread: interrupted", errors.getvalue())
        self.assertEqual(1, sum("install" in row for row in self.commands))
        self.assertEqual([], self.setup_calls)

    def test_eof_at_interactive_update_approval_cancels_before_incoming_code(self):
        output = io.StringIO()
        with (redirect_stdout(output), redirect_stderr(io.StringIO()),
              mock.patch.object(update.sys, "stdin", mock.Mock(isatty=lambda: True)),
              mock.patch("builtins.input", side_effect=EOFError)):
            code = update.update_main(["--enroll-repo", str(self.repo)])
        self.assertEqual(0, code)
        self.assertIn("cancelled", output.getvalue())
        self.assertEqual([[self.launcher, "runtime", "status"]], self.commands)
        self.assertEqual([], self.setup_calls)
        update.download.assert_not_called()

    def test_standalone_cancellation_only_observes_through_reviewed_pinned_bootstrap(self):
        output = io.StringIO()
        with (redirect_stdout(output), redirect_stderr(io.StringIO()),
              mock.patch.object(update.sys, "stdin", mock.Mock(isatty=lambda: True)),
              mock.patch("builtins.input", return_value="cancel")):
            code = update.install_main(["--enroll-repo", str(self.repo)])
        self.assertEqual(0, code)
        self.assertIn("cancelled", output.getvalue())
        self.assertTrue(self.commands)
        self.assertTrue(all(row[0] == "/usr/bin/python3" and row[5] in {"plan", "status"}
                            for row in self.commands))
        self.assertEqual([], self.setup_calls)


if __name__ == "__main__":
    unittest.main()
