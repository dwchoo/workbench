"""Requirement-derived checks at the public recipe/transport/process boundaries.

Fixtures are local and deterministic; they do not establish real SDK support.
"""
from email.message import Message
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.response import addinfourl

from workbench.backend.install_recipe import (
    BinaryRecipe, RecipeError, download_verified, inspect_existing, resolve_stable,
)


PAYLOAD = b"independent official artifact\n"
OFFICIAL = "https://build.stencil.so/d/omp/exact/omp-linux-x64"


def metadata():
    return {
        "build": {"version": "42.17.3", "channel": "stable"},
        "file": {"name": "omp-linux-x64", "platform": "linux", "arch": "x86_64",
                 "sha256": sha256(PAYLOAD).hexdigest(), "size": len(PAYLOAD)},
        "download": OFFICIAL,
    }


def recipe():
    return BinaryRecipe("42.17.3", "linux-x86_64", "omp-linux-x64",
                        sha256(PAYLOAD).hexdigest(), len(PAYLOAD), OFFICIAL,
                        "https://build.stencil.so/api/products/omp/latest/linux-x86_64?channel=stable")


def response(payload=PAYLOAD, url=OFFICIAL):
    stream = BytesIO(payload)
    stream.geturl = lambda: url
    return stream


class RecipeBoundaryTests(unittest.TestCase):
    def test_resolved_transaction_keeps_exact_version_source_and_bytes_after_latest_changes(self):
        upstream = metadata()
        resolved = resolve_stable(target="linux-x86_64", fetch_json=lambda _: upstream)
        # Upstream latest can move between resolve and transfer; the transaction
        # must still use the previously observed artifact, including its digest.
        upstream["build"]["version"] = "42.17.4"
        upstream["file"]["sha256"] = "0" * 64
        upstream["download"] = "https://build.stencil.so/d/omp/new/omp-linux-x64"
        seen = []
        def transport(url, timeout):
            seen.append(url)
            return response()
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "omp"
            download_verified(resolved, destination, open_url=transport)
            self.assertEqual(destination.read_bytes(), PAYLOAD)
        self.assertEqual(resolved.version, "42.17.3")
        self.assertEqual(resolved.sha256, sha256(PAYLOAD).hexdigest())
        self.assertEqual(seen, [OFFICIAL])

    def test_stable_metadata_protocol_rejects_nonobject_invalid_and_excessive_json(self):
        for payload in (b"null", b"[]", b"{", b"\xff", b"x" * (2 * 1024 * 1024 + 1)):
            with self.subTest(payload_prefix=payload[:20]):
                def transport(request):
                    result = addinfourl(BytesIO(payload), Message(), request.full_url, 200)
                    result.msg = "OK"
                    return result
                with patch("urllib.request.HTTPSHandler.https_open", side_effect=transport):
                    with self.assertRaises(RecipeError) as caught:
                        resolve_stable(target="linux-x86_64")
                self.assertIn(caught.exception.code, {"invalid_metadata", "metadata_too_large"})

    def test_metadata_network_failure_is_not_a_successful_or_incompatible_recipe(self):
        def unavailable(_):
            raise URLError("independent_network_unavailable")
        with self.assertRaisesRegex(URLError, "independent_network_unavailable"):
            resolve_stable(target="linux-x86_64", fetch_json=unavailable)

    def test_untrusted_https_redirect_is_rejected_before_second_request(self):
        seen = []
        def transport(request):
            seen.append(request.full_url)
            headers = Message()
            headers["Location"] = "https://untrusted.example/omp?signature=fixture-secret"
            result = addinfourl(BytesIO(b""), headers, request.full_url, 302)
            result.msg = "Found"
            return result
        with tempfile.TemporaryDirectory() as raw:
            with patch("urllib.request.HTTPSHandler.https_open", side_effect=transport):
                with self.assertRaises(RecipeError) as caught:
                    download_verified(recipe(), Path(raw) / "omp")
            self.assertEqual(caught.exception.code, "untrusted_artifact_url")
            self.assertNotIn("fixture-secret", str(caught.exception))
            self.assertEqual(seen, [OFFICIAL])
            self.assertEqual(list(Path(raw).iterdir()), [])


class DownloadFailureBoundaryTests(unittest.TestCase):
    def test_midstream_network_failure_and_interrupt_leave_no_executable_or_staging(self):
        for error in (ConnectionResetError("independent_connection_lost"),
                      TimeoutError("independent_socket_timeout"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as raw:
                stream = response()
                reads = 0
                def read(size):
                    nonlocal reads
                    reads += 1
                    if reads == 1:
                        return PAYLOAD[:4]
                    raise error
                stream.read1 = read
                destination = Path(raw) / "omp"
                with self.assertRaises(type(error)):
                    download_verified(recipe(), destination, open_url=lambda url, timeout: stream)
                self.assertTrue(stream.closed)
                self.assertEqual(list(Path(raw).iterdir()), [])

    def test_digest_failure_is_specific_and_cannot_publish_size_matching_corruption(self):
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "omp"
            with self.assertRaises(RecipeError) as caught:
                download_verified(recipe(), destination,
                                  open_url=lambda url, timeout: response(b"X" * len(PAYLOAD)))
            self.assertEqual(caught.exception.code, "artifact_checksum_mismatch")
            self.assertEqual(list(Path(raw).iterdir()), [])

    def test_existing_dangling_symlink_is_preserved_without_network_access(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            destination = root / "omp"
            destination.symlink_to(root / "removed-user-installation")
            with self.assertRaises(FileExistsError):
                download_verified(recipe(), destination,
                                  open_url=lambda *args, **kwargs: self.fail("existing selection triggered network"))
            self.assertEqual(destination.readlink(), root / "removed-user-installation")
            self.assertEqual(list(root.iterdir()), [destination])

    def test_destination_racing_download_publication_is_never_clobbered(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                destination = root / "omp"
                existing = root / "existing"
                existing.write_bytes(b"user installation")
                def transport(url, timeout):
                    if symlink:
                        destination.symlink_to(existing)
                    else:
                        destination.write_bytes(b"competing setup")
                    return response()
                with self.assertRaises(FileExistsError):
                    download_verified(recipe(), destination, open_url=transport)
                self.assertEqual(existing.read_bytes(), b"user installation")
                if symlink:
                    self.assertEqual(destination.readlink(), existing)
                else:
                    self.assertEqual(destination.read_bytes(), b"competing setup")
                self.assertEqual({p.name for p in root.iterdir()}, {"omp", "existing"})

    def test_untrusted_final_response_never_becomes_executable(self):
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "omp"
            with self.assertRaises(RecipeError) as caught:
                download_verified(recipe(), destination, open_url=lambda url, timeout:
                                  response(url="https://build.stencil.so.evil.example/omp"))
            self.assertEqual(caught.exception.code, "untrusted_artifact_url")
            self.assertEqual(list(Path(raw).iterdir()), [])


class ExistingProcessBoundaryTests(unittest.TestCase):
    def test_nonexecutable_existing_file_remains_broken_without_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "omp"
            path.write_bytes(b"#!/bin/sh\necho omp/42.17.3\n")
            path.chmod(0o600)
            before = path.stat()
            result = inspect_existing(path)
            self.assertEqual(result.status, "broken")
            self.assertIsNone(result.version)
            self.assertEqual((path.stat().st_mode, path.stat().st_mtime_ns),
                             (before.st_mode, before.st_mtime_ns))

    def test_existing_prerelease_version_is_executable_evidence_only(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "omp"
            path.write_text("#!/bin/sh\nprintf 'omp/77.9.1-preview.4+local\\n'\n")
            path.chmod(0o700)
            result = inspect_existing(path)
            self.assertEqual((result.status, result.version), ("executable", "77.9.1-preview.4+local"))
            self.assertIsNone(result.reason)
            self.assertFalse(hasattr(result, "sdk_compatible"))

    def test_timeout_stops_descendant_holding_pipe_after_parent_exits(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pid_file = root / "owned-pids.json"
            path = root / "omp"
            path.write_text(
                f"#!{sys.executable}\n"
                "import json, os, subprocess, sys\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"open({str(pid_file)!r}, 'w').write(json.dumps([os.getpid(), child.pid]))\n"
                "print('omp/42.17.3', flush=True)\n"
            )
            path.chmod(0o700)
            started = time.monotonic()
            pids = []
            try:
                result = inspect_existing(path, timeout=0.5)
                self.assertEqual((result.status, result.reason), ("broken", "version_timeout"))
                self.assertIsNone(result.version)
                self.assertLess(time.monotonic() - started, 3)
                self.assertTrue(pid_file.exists(), "fixture subprocess did not execute")
                pids = json.loads(pid_file.read_text())
                for pid in pids:
                    stat = Path(f"/proc/{pid}/stat")
                    deadline = time.monotonic() + 2
                    while stat.exists():
                        # A reparented zombie cannot execute or retain pipes;
                        # OS reaping belongs to its adoptive parent. SIGKILL
                        # delivery itself can race the observer by a timeslice.
                        try:
                            state = stat.read_text().rsplit(')', 1)[1].split()[0]
                        except FileNotFoundError:
                            break
                        if state == 'Z':
                            break
                        self.assertLess(time.monotonic(), deadline,
                                        f"owned descendant {pid} is still live: {state}")
                        time.sleep(0.01)
            finally:
                if pid_file.exists():
                    pids = json.loads(pid_file.read_text())
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
