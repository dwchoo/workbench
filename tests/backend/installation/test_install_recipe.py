"""Predetermined executable/official-metadata fixtures for the recipe public API."""
from pathlib import Path
import tempfile
import unittest

from workbench.backend.install_recipe import inspect_existing


class ExistingOMPTests(unittest.TestCase):
    def test_absent_candidate_is_missing_without_creating_files(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "omp"
            self.assertEqual(inspect_existing(path).status, "missing")
            self.assertFalse(path.exists())

    def test_working_unknown_version_is_reusable_executable(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "omp"
            path.write_text("#!/bin/sh\nprintf 'omp/99.7.2\\n'\n")
            path.chmod(0o700)
            before = path.read_bytes()
            result = inspect_existing(path)
            self.assertEqual((result.status, result.version), ("executable", "99.7.2"))
            self.assertEqual(path.read_bytes(), before)

    def test_broken_candidate_is_reported_without_replacement(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "omp"
            path.write_text("#!/bin/sh\necho installation_broken >&2\nexit 42\n")
            path.chmod(0o700)
            before = path.read_bytes()
            result = inspect_existing(path)
            self.assertEqual(result.status, "broken")
            self.assertIn("installation_broken", result.reason)
            self.assertEqual(path.read_bytes(), before)

    def test_timeout_is_broken_and_owned_process_is_stopped(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "omp"
            path.write_text("#!/bin/sh\nexec sleep 60\n")
            path.chmod(0o700)
            result = inspect_existing(path, timeout=0.05)
            self.assertEqual(result.status, "broken")
            self.assertEqual(result.reason, "version_timeout")

    def test_excessive_version_output_is_broken_with_bounded_capture(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "omp"
            path.write_text("#!/usr/bin/python3\nimport sys\nsys.stdout.write('x'*100000)\n")
            path.chmod(0o700)
            result = inspect_existing(path)
            self.assertEqual((result.status, result.reason), ("broken", "version_output_limit"))

    def test_successful_non_omp_executable_is_not_a_reusable_omp(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "omp"
            path.write_text("#!/bin/sh\necho unrelated-program\n")
            path.chmod(0o700)
            result = inspect_existing(path)
            self.assertEqual((result.status, result.reason), ("broken", "invalid_version_output"))

    def test_dangling_existing_selection_is_broken_instead_of_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "omp"
            path.symlink_to(Path(raw) / "removed-version")
            result = inspect_existing(path)
            self.assertEqual(result.status, "broken")
            self.assertTrue(path.is_symlink())


class StableRecipeTests(unittest.TestCase):
    def test_official_stable_pins_exact_target_artifact_and_integrity(self):
        from workbench.backend.install_recipe import resolve_stable
        metadata = {
            "build": {"version": "18.12.2", "channel": "stable"},
            "file": {"name": "omp-linux-x64", "platform": "linux", "arch": "x86_64",
                     "sha256": "1" * 64, "size": 1234},
            "download": "https://build.stencil.so/d/omp/fixed/omp-linux-x64",
        }
        seen = []
        def fetch(url):
            seen.append(url)
            return metadata
        recipe = resolve_stable(target="linux-x86_64", fetch_json=fetch)
        self.assertEqual(recipe.version, "18.12.2")
        self.assertEqual(recipe.target, "linux-x86_64")
        self.assertEqual((recipe.sha256, recipe.size), ("1" * 64, 1234))
        self.assertEqual(recipe.download_url, metadata["download"])
        self.assertEqual(recipe.method, "official_binary")
        self.assertEqual(seen, ["https://build.stencil.so/api/products/omp/latest/linux-x86_64?channel=stable"])

    def test_malformed_or_mismatched_stable_metadata_is_rejected(self):
        from copy import deepcopy
        from workbench.backend.install_recipe import resolve_stable
        base = {"build": {"version": "18.12.2", "channel": "stable"},
                "file": {"name": "omp-linux-x64", "platform": "linux", "arch": "x86_64",
                         "sha256": "1" * 64, "size": 1234},
                "download": "https://build.stencil.so/d/omp/fixed/omp-linux-x64"}
        changes = [("build", "version", "18.12.2-beta.1"), ("build", "channel", "nightly"),
                   ("file", "platform", "linux-musl"), ("file", "arch", "arm64"),
                   ("file", "name", "../../omp"), ("file", "sha256", "unknown"),
                   ("file", "size", -1), ("file", "size", True)]
        for section, key, value in changes:
            with self.subTest(section=section, key=key, value=value):
                bad = deepcopy(base)
                bad[section][key] = value
                with self.assertRaises(ValueError):
                    resolve_stable(target="linux-x86_64", fetch_json=lambda _: bad)
        for bad_url in ("http://build.stencil.so/omp", "file:///tmp/omp", "https://user:secret@build.stencil.so/omp"):
            with self.subTest(url=bad_url):
                with self.assertRaises(ValueError):
                    resolve_stable(target="linux-x86_64", fetch_json=lambda _: {**base, "download": bad_url})

    def test_host_target_is_linux_only_and_does_not_guess_unknown_abi(self):
        from workbench.backend.install_recipe import host_target
        self.assertEqual(host_target(system="Linux", machine="x86_64", libc="glibc"), "linux-x86_64")
        self.assertEqual(host_target(system="Linux", machine="aarch64", libc="musl"), "linux-musl-arm64")
        for system, arch, libc in (("Windows", "x86_64", "glibc"), ("Darwin", "arm64", "glibc"),
                                  ("Linux", "riscv64", "glibc"), ("Linux", "x86_64", "unknown")):
            with self.subTest(system=system, arch=arch, libc=libc):
                with self.assertRaises(ValueError):
                    host_target(system=system, machine=arch, libc=libc)

    def test_official_metadata_request_identifies_wb_for_build_service(self):
        import json
        from io import BytesIO
        from email.message import Message
        from urllib.response import addinfourl
        from unittest.mock import patch
        from workbench.backend.install_recipe import resolve_stable
        metadata = {"build": {"version": "18.12.2", "channel": "stable"},
                    "file": {"name": "omp-linux-x64", "platform": "linux", "arch": "x86_64",
                             "sha256": "1" * 64, "size": 1234},
                    "download": "https://build.stencil.so/fixture/omp"}
        def transport(request):
            self.assertEqual(request.get_header("User-agent"), "workbench/0.1")
            response = addinfourl(BytesIO(json.dumps(metadata).encode()), Message(), request.full_url, 200)
            response.msg = "OK"
            return response
        with patch("urllib.request.HTTPSHandler.https_open", side_effect=transport):
            self.assertEqual(resolve_stable(target="linux-x86_64").version, "18.12.2")


class DownloadRecipeTests(unittest.TestCase):
    def recipe(self):
        from workbench.backend.install_recipe import BinaryRecipe
        return BinaryRecipe("18.12.2", "linux-x86_64", "omp-linux-x64",
                            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824", 5,
                            "https://build.stencil.so/fixture/omp", "https://build.stencil.so/fixture/metadata")

    def response(self, value):
        from io import BytesIO
        stream = BytesIO(value)
        stream.geturl = lambda: "https://build.stencil.so/fixture/omp"
        return stream

    def test_download_publishes_only_verified_bytes_as_private_executable(self):
        from workbench.backend.install_recipe import download_verified
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "omp"
            result = download_verified(self.recipe(), destination,
                                       open_url=lambda url, timeout: self.response(b"hello"))
            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), b"hello")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertEqual(sorted(p.name for p in Path(raw).iterdir()), ["omp"])

    def test_corrupt_or_truncated_download_never_becomes_executable(self):
        from workbench.backend.install_recipe import download_verified
        for payload in (b"HELLO", b"hell", b"hello-extra"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as raw:
                destination = Path(raw) / "omp"
                with self.assertRaises(ValueError):
                    download_verified(self.recipe(), destination,
                                      open_url=lambda url, timeout: self.response(payload))
                self.assertFalse(destination.exists())
                self.assertEqual(list(Path(raw).iterdir()), [])

    def test_existing_destination_is_preserved_even_when_download_would_pass(self):
        from workbench.backend.install_recipe import download_verified
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "omp"
            destination.write_bytes(b"existing-installation")
            with self.assertRaises(FileExistsError):
                download_verified(self.recipe(), destination,
                                  open_url=lambda url, timeout: self.response(b"hello"))
            self.assertEqual(destination.read_bytes(), b"existing-installation")
            self.assertEqual(sorted(p.name for p in Path(raw).iterdir()), ["omp"])

    def test_total_download_budget_rejects_stalled_stream_and_cleans_staging(self):
        import time
        from workbench.backend.install_recipe import download_verified
        stream = self.response(b"hello")
        original = stream.read
        def delayed(size):
            time.sleep(0.025)
            return original(size)
        stream.read = delayed
        stream.read1 = delayed
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "omp"
            with self.assertRaisesRegex(ValueError, "download_timeout"):
                download_verified(self.recipe(), destination, total_timeout=0.01,
                                  open_url=lambda url, timeout: stream)
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(raw).iterdir()), [])

    def test_official_download_rejects_https_downgrade_before_following_redirect(self):
        from io import BytesIO
        from email.message import Message
        from urllib.response import addinfourl
        from unittest.mock import patch
        from workbench.backend.install_recipe import download_verified
        headers = Message()
        headers["Location"] = "http://127.0.0.1:9/untrusted"
        response = addinfourl(BytesIO(b""), headers, "https://build.stencil.so/fixture/omp", 302)
        response.msg = "Found"
        with tempfile.TemporaryDirectory() as raw:
            with patch("urllib.request.HTTPSHandler.https_open", return_value=response), \
                 patch("urllib.request.HTTPHandler.http_open", side_effect=AssertionError("insecure redirect followed")), \
                 patch("urllib.request._opener", None):
                with self.assertRaisesRegex(ValueError, "untrusted_artifact_url"):
                    download_verified(self.recipe(), Path(raw) / "omp")
            self.assertEqual(list(Path(raw).iterdir()), [])
