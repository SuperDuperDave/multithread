"""Immutable-runtime witnesses; every installation root is disposable."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SOURCE))
import relay_bootstrap as subject


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        temporary = tempfile.TemporaryDirectory(prefix="relay-bootstrap-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "approved-source"
        self.installation = subject.Installation(self.base / "account" / "runtime")
        self.payload = {name: b"# synthetic approved fixture\n" for name in subject.PAYLOAD_FILES}
        self.payload["relay_core/__init__.py"] = b"VALUE = 'original core'\n"
        self.payload["relay_core/protocol.py"] = b"VALUE = 'original protocol'\n"
        self.payload["relay_core/cli.py"] = b"from .protocol import VALUE\n"
        self.payload["relay_runtime/__init__.py"] = b"VALUE = 'original runtime'\n"
        self.payload["relay_runtime/enrollment.py"] = b"from . import VALUE\n"
        self.write_source()

    def write_source(self):
        for name, body in self.payload.items():
            path = self.source / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(body)
        self.manifest = subject.manifest_for(self.payload)
        self.digest = hashlib.sha256(self.manifest).hexdigest()

    def test_published_payload_closures_remain_frozen_for_release_management(self):
        historical = subject._PUBLISHED_V0415_PAYLOAD_FILES
        self.assertNotIn("relay_runtime/agent.py", historical)
        self.assertNotIn("relay_runtime/review_packet.py", historical)
        self.assertIn("relay_runtime/agent.py", subject.PAYLOAD_FILES)
        for names in subject._RELEASE_PAYLOAD_SETS:
            payload = {name: b"# synthetic released module\n" for name in names}
            manifest = subject._payload_manifest(payload, release_management=True)
            self.assertEqual(names, frozenset(subject._validate_manifest(
                manifest, release_management=True)["members"]))
            if names != subject.PAYLOAD_FILES:
                with self.assertRaises(subject.BootstrapError):
                    subject._validate_manifest(manifest)

    def install(self, *, activate=True):
        digest = self.installation.install(self.source, self.manifest)
        self.assertEqual(digest, self.digest)
        return self.installation.activate(digest, expected_activation=None) if activate else digest

    def generation(self, digest=None):
        return self.installation.root / "generations" / (digest or self.digest)

    def child(self, code, *, flags=("-I", "-S", "-B"), env=None):
        prefix = f"""
import importlib.util, json, os, pathlib, sys, types
spec = importlib.util.spec_from_file_location('trusted_bootstrap_fixture', {str(SOURCE / 'relay_bootstrap.py')!r})
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
spec.loader.exec_module(bootstrap)
installation = bootstrap.Installation(pathlib.Path({str(self.installation.root)!r}))
"""
        return subprocess.run(["/usr/bin/python3", *flags, "-c", textwrap.dedent(prefix) + textwrap.dedent(code)],
                              env=env or {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
                              text=True, capture_output=True, timeout=15)

    def test_install_is_inactive_and_idempotent_without_project_enrollment(self):
        digest = self.install(activate=False)
        path = self.generation() / "manifest.json"
        before = (path.stat().st_mtime_ns, path.read_bytes())
        self.assertEqual(digest, self.installation.install(self.source, self.manifest))
        self.assertEqual(before, (path.stat().st_mtime_ns, path.read_bytes()))
        with self.assertRaises(subject.BootstrapError):
            self.installation.active()
        self.assertFalse(list(self.base.rglob("relay.sqlite3")))
        self.assertFalse(list(self.base.rglob("enrollment.json")))

    def test_unapproved_source_refuses_before_creating_installation_state(self):
        (self.source / "relay_core/protocol.py").write_text("raise AssertionError('unapproved')\n")
        with self.assertRaises(subject.BootstrapError):
            self.installation.install(self.source, self.manifest)
        self.assertFalse(self.installation.root.parent.exists())

    def test_invalid_manifests_refuse_before_creation(self):
        malformed = [b"{", b"{}", b' {"format":1}', b'{"format":1,"format":1}', self.manifest + b"\n"]
        for manifest in malformed:
            with self.subTest(manifest=manifest[:20]), self.assertRaises(subject.BootstrapError):
                self.installation.install(self.source, manifest)
        self.assertFalse(self.installation.root.parent.exists())

    def test_source_symlink_and_hardlink_refuse(self):
        path = self.source / "relay_core/protocol.py"
        held = self.base / "held.py"
        path.rename(held)
        path.symlink_to(held)
        with self.assertRaises(OSError):
            self.installation.install(self.source, self.manifest)
        path.unlink()
        os.link(held, path)
        with self.assertRaises(subject.BootstrapError):
            self.installation.install(self.source, self.manifest)
        self.assertFalse(self.installation.root.parent.exists())

    def test_source_change_between_member_reads_refuses_approved_manifest(self):
        original = subject._read
        fired = False
        def read(*args, **kwargs):
            nonlocal fired
            result = original(*args, **kwargs)
            if not fired:
                fired = True
                (self.source / "relay_runtime/enrollment.py").write_text("CHANGED = True\n")
            return result
        with mock.patch.object(subject, "_read", side_effect=read), self.assertRaises(subject.BootstrapError):
            self.installation.install(self.source, self.manifest)
        self.assertFalse(self.installation.root.parent.exists())

    def test_source_change_after_snapshot_cannot_change_installed_bytes(self):
        original = subject.snapshot_source
        def snapshot(*args):
            result = original(*args)
            (self.source / "relay_core/protocol.py").write_text("CHANGED = True\n")
            return result
        with mock.patch.object(subject, "snapshot_source", side_effect=snapshot):
            self.install()
        self.assertEqual(self.payload["relay_core/protocol.py"], self.installation.load_active().bodies["relay_core/protocol.py"])

    def test_closed_manifest_does_not_copy_unlisted_source_files(self):
        (self.source / "relay_core/private-note.txt").write_text("synthetic unlisted source")
        self.install()
        self.assertFalse((self.generation() / "relay_core/private-note.txt").exists())

    def test_unknown_or_partial_generation_is_not_overwritten(self):
        partial = self.generation()
        partial.mkdir(mode=0o700, parents=True)
        sentinel = partial / "unknown"
        sentinel.write_bytes(b"do not overwrite")
        before = (sentinel.stat().st_mtime_ns, sentinel.read_bytes())
        with self.assertRaises(subject.BootstrapError):
            self.installation.install(self.source, self.manifest)
        self.assertEqual(before, (sentinel.stat().st_mtime_ns, sentinel.read_bytes()))
        self.assertFalse((self.installation.root / "active.json").exists())

    def test_recalculated_local_manifest_cannot_change_active_digest(self):
        self.install()
        self.payload["relay_core/protocol.py"] = b"VALUE = 'tampered'\n"
        (self.generation() / "relay_core/protocol.py").write_bytes(self.payload["relay_core/protocol.py"])
        (self.generation() / "manifest.json").write_bytes(subject.manifest_for(self.payload))
        with self.assertRaises(subject.BootstrapError):
            self.installation.load_active()

    def test_unknown_generation_file_refuses_before_import(self):
        self.install()
        (self.generation() / "relay_core/extra.py").write_text("raise AssertionError('must not run')\n")
        with self.assertRaises(subject.BootstrapError):
            self.installation.load_active()

    def test_runtime_member_symlink_and_hardlink_refuse(self):
        self.install()
        path = self.generation() / "relay_core/protocol.py"
        held = self.base / "held-runtime.py"
        path.rename(held)
        path.symlink_to(held)
        with self.assertRaises(OSError):
            self.installation.load_active()
        path.unlink()
        os.link(held, path)
        with self.assertRaises(subject.BootstrapError):
            self.installation.load_active()

    def test_activation_cas_and_rollback_have_no_aba(self):
        first = self.install()
        a = first.digest
        self.payload["relay_core/protocol.py"] = b"VALUE = 'second generation'\n"
        self.write_source()
        b = self.installation.install(self.source, self.manifest)
        second = self.installation.activate(b, expected_activation=first.activation_id)
        rolled_back = self.installation.activate(a, expected_activation=second.activation_id)
        self.assertEqual(rolled_back.digest, a)
        self.assertNotEqual(first.activation_id, rolled_back.activation_id)
        with self.assertRaises(subject.BootstrapError):
            self.installation.activate(b, expected_activation=first.activation_id)
        self.assertEqual(self.installation.active(), rolled_back)

    def test_concurrent_activation_has_one_winner(self):
        first = self.install()
        def attempt(_):
            try:
                return self.installation.activate(first.digest, expected_activation=first.activation_id).activation_id
            except subject.BootstrapError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(len([value for value in results if value is not None]), 1)
        self.assertIn(self.installation.active().activation_id, results)

    def test_corrupt_active_record_is_not_treated_as_no_activation(self):
        self.install()
        (self.installation.root / "active.json").write_bytes(b"{}")
        with self.assertRaises(subject.BootstrapError):
            self.installation.activate(self.digest, expected_activation=None)
        self.assertEqual((self.installation.root / "active.json").read_bytes(), b"{}")

    def test_replaced_lock_refuses_activation(self):
        first = self.install()
        original = self.installation._verify
        def verify(*args):
            runtime = original(*args)
            lock = self.installation.root / "activation.lock"
            lock.rename(lock.with_name("old-lock"))
            lock.write_bytes(b"")
            return runtime
        with mock.patch.object(self.installation, "_verify", side_effect=verify), self.assertRaises(subject.BootstrapError):
            self.installation.activate(self.digest, expected_activation=first.activation_id)
        self.assertEqual(first, self.installation.active())

    def test_changed_lock_permissions_refuse_activation(self):
        first = self.install()
        original = self.installation._verify
        def verify(*args):
            result = original(*args)
            (self.installation.root / "activation.lock").chmod(0o666)
            return result
        with mock.patch.object(self.installation, "_verify", side_effect=verify), self.assertRaises(subject.BootstrapError):
            self.installation.activate(self.digest, expected_activation=first.activation_id)
        self.assertEqual(first, self.installation.active())

    def test_changed_ancestor_permissions_refuse_before_activation(self):
        first = self.install()
        original = self.installation._verify
        active = (self.installation.root / "active.json").read_bytes()
        def verify(*args):
            result = original(*args)
            self.installation.root.parent.chmod(0o777)
            return result
        with mock.patch.object(self.installation, "_verify", side_effect=verify), self.assertRaises(subject.BootstrapError):
            self.installation.activate(self.digest, expected_activation=first.activation_id)
        self.assertEqual(active, (self.installation.root / "active.json").read_bytes())

    def test_substituted_pending_record_cannot_produce_a_false_success(self):
        first = self.install()
        original = subject.os.replace
        substituted_id = "c" * 32
        def substitute(source, destination, **kwargs):
            if source.startswith(".activation-"):
                path = self.installation.root / source
                path.unlink()
                path.write_bytes(subject._canonical({"format": 1, "digest": self.digest,
                    "activation_id": substituted_id, "previous_id": first.activation_id}))
            return original(source, destination, **kwargs)
        with mock.patch.object(subject.os, "replace", side_effect=substitute), self.assertRaisesRegex(subject.BootstrapError, "uncertain"):
            self.installation.activate(self.digest, expected_activation=first.activation_id)
        self.assertEqual(self.installation.active().activation_id, substituted_id)

    def test_pending_substitution_before_publish_preserves_unknown_file(self):
        first = self.install()
        original = subject._write_new
        unknown = []
        def substitute(directory, name, body, **kwargs):
            held = original(directory, name, body, **kwargs)
            if name.startswith(".activation-"):
                path = self.installation.root / name
                path.rename(path.with_name("retained-original"))
                path.write_bytes(b"unrelated replacement; preserve me")
                unknown.append(path)
            return held
        with mock.patch.object(subject, "_write_new", side_effect=substitute), self.assertRaisesRegex(subject.BootstrapError, "uncertain"):
            self.installation.activate(self.digest, expected_activation=first.activation_id)
        self.assertEqual(first, self.installation.active())
        self.assertEqual(unknown[0].read_bytes(), b"unrelated replacement; preserve me")

    def test_installation_symlink_refuses_without_writing_target(self):
        foreign = self.base / "foreign"
        foreign.mkdir()
        self.installation.root.parent.mkdir()
        self.installation.root.symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(OSError):
            self.installation.install(self.source, self.manifest)
        self.assertEqual(list(foreign.iterdir()), [])

    def test_ancestor_substitution_writes_only_through_held_descriptors(self):
        foreign = self.base / "foreign"
        foreign.mkdir()
        original = subject._write_new
        moved = self.base / "moved-account"
        fired = False
        def write(*args):
            nonlocal fired
            if not fired:
                fired = True
                self.installation.root.parent.rename(moved)
                self.installation.root.parent.symlink_to(foreign, target_is_directory=True)
            return original(*args)
        with mock.patch.object(subject, "_write_new", side_effect=write), self.assertRaises(subject.BootstrapError):
            self.installation.install(self.source, self.manifest)
        self.assertEqual(list(foreign.iterdir()), [])
        self.assertFalse((moved / "runtime/active.json").exists())

    def test_read_only_missing_installation_does_not_create_state(self):
        with self.assertRaises(FileNotFoundError):
            self.installation.load_active()
        self.assertFalse(self.installation.root.parent.exists())

    def test_environment_cannot_select_account_installation_root(self):
        expected = subject.default_install_root()
        with mock.patch.dict(os.environ, {"HOME": str(self.base), "XDG_DATA_HOME": str(self.base), "RELAY_HOME": str(self.base)}):
            self.assertEqual(expected, subject.default_install_root())

    def test_loader_executes_retained_members_including_package_initializers(self):
        self.install()
        result = self.child("""
runtime = installation.load_active()
(runtime.origin / 'relay_core/protocol.py').write_text("raise AssertionError('changed after verification')\\n")
runtime.install_importer()
import relay_core, relay_core.cli, relay_runtime, relay_runtime.enrollment
assert relay_core.VALUE == 'original core'
assert relay_core.cli.VALUE == 'original protocol'
assert relay_runtime.enrollment.VALUE == 'original runtime'
print('retained bytes verified')
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retained bytes verified", result.stdout)

    def test_peer_producer_identity_retains_loaded_runtime_after_activation_and_file_changes(self):
        self.payload = {name: (SOURCE / name).read_bytes() for name in subject.PAYLOAD_FILES}
        self.write_source()
        first = self.install()
        expected = {"status": "recorded", "runtime_manifest_sha256": self.digest}
        self.payload["relay_runtime/provider.py"] += b"\n# Different disposable runtime fixture.\n"
        self.write_source()
        second_digest = self.install(activate=False)
        self.assertNotEqual(first.digest, second_digest)
        result = self.child(f"""
runtime = installation.load_active()
runtime.install_importer()
from relay_runtime import provider
before = provider._producer_runtime()
provider.__dict__.pop('__loader__', None)
assert provider._producer_runtime() == before
installation.activate({second_digest!r}, expected_activation={first.activation_id!r})
(runtime.origin / 'relay_runtime/provider.py').write_text("raise AssertionError('changed after import')\\n")
(runtime.origin / 'relay_runtime/__init__.py').unlink()
assert installation.load_active().digest == {second_digest!r}
assert provider._producer_runtime() == before
print(json.dumps(before))
""")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(expected, json.loads(result.stdout))

    def test_loader_rejects_preloaded_namespace_descendant(self):
        self.install()
        result = self.child("""
runtime = installation.load_active()
sys.modules['relay_core.preloaded'] = types.ModuleType('relay_core.preloaded')
try:
    runtime.install_importer()
except bootstrap.BootstrapError:
    print('refused')
else:
    raise AssertionError('preload accepted')
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refused", result.stdout)

    def test_loader_refuses_unlisted_namespace_import_after_verification(self):
        self.install()
        result = self.child("""
runtime = installation.load_active()
(runtime.origin / 'relay_core/extra.py').write_text("raise AssertionError('unlisted module executed')\\n")
runtime.install_importer()
try:
    import relay_core.extra
except ModuleNotFoundError:
    print('refused')
else:
    raise AssertionError('unlisted import accepted')
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refused", result.stdout)

    def test_loader_requires_site_disabled_and_rejects_custom_import_hooks(self):
        self.install()
        for flags, tamper in ((("-I", "-B"), ""), (("-I", "-S", "-B"), "sys.meta_path.insert(0, object())\n")):
            code = "runtime = installation.load_active()\n" + tamper + """
try:
    runtime.install_importer()
except bootstrap.BootstrapError:
    print('refused')
else:
    raise AssertionError('unsafe startup accepted')
"""
            result = self.child(code, flags=flags)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("refused", result.stdout)

    def test_python_environment_startup_canary_does_not_run(self):
        self.install()
        canary = self.base / "startup-ran"
        (self.base / "sitecustomize.py").write_text(f"open({str(canary)!r}, 'w').write('unexpected')\n")
        result = self.child("runtime = installation.load_active(); runtime.install_importer(); import relay_core\n",
                            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(self.base), "PYTHONSTARTUP": str(self.base / "sitecustomize.py")})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(canary.exists())

    def test_process_exit_during_generation_install_never_activates_partial_bytes(self):
        result = self.child(f"""
original = bootstrap._write_new
count = 0
def cut(*args):
    global count
    original(*args)
    count += 1
    if count == 2:
        os._exit(73)
bootstrap._write_new = cut
installation.install(pathlib.Path({str(self.source)!r}), {self.manifest!r})
""")
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertFalse((self.installation.root / "active.json").exists())
        with self.assertRaises(subject.BootstrapError):
            self.installation.install(self.source, self.manifest)

    def test_process_exit_after_activation_publication_has_inspectable_new_identity(self):
        first = self.install()
        result = self.child(f"""
original = bootstrap.os.replace
def cut(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(74)
bootstrap.os.replace = cut
installation.activate({self.digest!r}, expected_activation={first.activation_id!r})
""")
        self.assertEqual(result.returncode, 74, result.stderr)
        active = self.installation.active()
        self.assertEqual(active.digest, first.digest)
        self.assertNotEqual(active.activation_id, first.activation_id)
        self.assertEqual(active.previous_id, first.activation_id)

    def test_actual_core_payload_imports_without_opening_a_ledger(self):
        self.payload = {name: (SOURCE / name).read_bytes() for name in subject.PAYLOAD_FILES}
        self.write_source()
        self.install()
        result = self.child("""
runtime = installation.load_active()
runtime.install_importer()
import relay_core.cli, relay_core.protocol, relay_core.store, relay_runtime.enrollment
assert relay_core.store.SCHEMA_VERSION == 2
assert callable(relay_core.cli.main)
assert callable(relay_runtime.enrollment.Registry.for_account)
print('actual modules loaded')
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list(self.base.rglob("relay.sqlite3")))


if __name__ == "__main__":
    unittest.main()
