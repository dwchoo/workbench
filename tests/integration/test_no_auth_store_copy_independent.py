"""WBX-B12 independent: the static ``agent.db`` copy check really catches copiers and the live helper never opens the store.

Expected values come from WBX-B12.md / SPEC s3 "Test helper" and C-D64: no code under ``src``/``tests``/``tools`` copies
the OMP auth store (``shutil.copy*``, read-then-write, SQLite backup, ``cp``), the temp-HOME placeholder
(``write_bytes(b"")``) and the symlink helper are not copiers, and ``live_real_summary._link_auth`` only ``lstat``s the
source.  Only temp dirs and placeholder files are used; no real store is touched.
"""
from __future__ import annotations

import builtins
import io
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

from tests.integration import live_real_summary as trial
from tests.integration.test_no_auth_store_copy import ROOT, auth_store_copiers

MUST_FLAG = {
    "shutil_copy": "import shutil\ndef f(h, d):\n    shutil.copy(h / '.omp/agent/agent.db', d)\n",
    "shutil_copy2": "import shutil\ndef f(h, d):\n    shutil.copy2(str(h) + '/.omp/agent/agent.db', d)\n",
    "shutil_copyfile": "import shutil\ndef f(h, d):\n    shutil.copyfile(h / '.omp/agent/agent.db', d)\n",
    "copyfileobj": ("import shutil\ndef f(h, d):\n    with open(h / 'agent.db', 'rb') as a, open(d, 'wb') as b:\n"
                    "        shutil.copyfileobj(a, b)\n"),
    "open_read_then_write": ("def f(h, d):\n    data = open(h / '.omp/agent/agent.db', 'rb').read()\n"
                             "    open(d, 'wb').write(data)\n"),
    "path_read_bytes_write_bytes": ("from pathlib import Path\ndef f(h, d):\n"
                                    "    Path(d).write_bytes((h / '.omp/agent/agent.db').read_bytes())\n"),
    "path_read_then_write": ("def f(h, d):\n    blob = (h / '.omp/agent/agent.db').read_bytes()\n"
                             "    d.write_bytes(blob)\n"),
    "sqlite_backup": ("import sqlite3\ndef f(h, d):\n    src = sqlite3.connect(h / '.omp/agent/agent.db')\n"
                      "    dst = sqlite3.connect(d)\n    src.backup(dst)\n"),
    "subprocess_cp": "import subprocess\ndef f(h, d):\n    subprocess.run(['cp', str(h / '.omp/agent/agent.db'), str(d)])\n",
    "os_system_cp": "import os\ndef f(h, d):\n    os.system(f'cp {h}/.omp/agent/agent.db {d}')\n",
    "wal_sibling": "import shutil\ndef f(h, d):\n    shutil.copyfile(str(h / 'agent.db') + '-wal', d)\n",
    "module_constant": ("import shutil\nNAME = 'agent.db'\ndef f(h, d):\n    shutil.copyfile(h / NAME, d)\n"),
    "alias_import_name": "from shutil import copyfile as put\ndef f(h, d):\n    put(h / 'agent.db', d)\n",
    "alias_import_module": "import shutil as sh\ndef f(h, d):\n    sh.copy(h / 'agent.db', d)\n",
    "string_concatenation": "import shutil\ndef f(h, d):\n    shutil.copy(h / ('agent' + '.db'), d)\n",
    "store_helper_call": ("import shutil\nfrom workbench.backend.omp_home import user_auth_store\n"
                          "def f(env, d):\n    shutil.copyfile(user_auth_store(env), d)\n"),
    "module_level_copy": "import shutil\nshutil.copy('/home/u/.omp/agent/agent.db', '/tmp/x')\n",
    "async_function": "import shutil\nasync def f(h, d):\n    shutil.copy(h / 'agent.db', d)\n",
    "nested_function": ("import shutil\ndef outer(h, d):\n    def inner():\n        shutil.copy(h / 'agent.db', d)\n"
                        "    inner()\n"),
}
MUST_NOT_FLAG = {
    "symlink_helper": ("import os\ndef link(source, root):\n    os.symlink(source, root / 'agent/agent.db')\n"),
    "symlink_to": "def link(source, root):\n    (root / 'agent' / 'agent.db').symlink_to(source)\n",
    "empty_placeholder": "def f(home):\n    (home / '.omp/agent/agent.db').write_bytes(b'')\n",
    "placeholder_text": "def f(home):\n    (home / '.omp/agent/agent.db').write_text('')\n",
    "unrelated_copy_with_docstring": ("import shutil\ndef f(a, b):\n    '''Not the agent.db: copies a config.'''\n"
                                      "    shutil.copy(a, b)\n"),
    "unrelated_copy_beside_placeholder": ("import shutil\ndef f(home, fixture):\n"
                                          "    (home / 'agent.db').write_bytes(b'')\n"
                                          "    shutil.copy(fixture / 'config.yml', home / 'config.yml')\n"),
    "lstat_and_remove_link": ("import os\ndef f(p):\n    info = os.lstat(p / 'agent.db')\n"
                              "    os.unlink(p / 'agent.db')\n    return info\n"),
    "rmtree_of_agent_dir": "import shutil\ndef f(root):\n    shutil.rmtree(root / 'agent')\n",
}
# Not the checker's stated job, but a temp-HOME placeholder plus a copy into its directory is a legitimate fixture.
PLAUSIBLE_FALSE_POSITIVE = {
    "placeholder_then_copytree_into_its_dir": ("import shutil\ndef f(home, fixture):\n"
                                               "    store = home / '.omp/agent/agent.db'\n"
                                               "    store.write_bytes(b'')\n"
                                               "    shutil.copytree(fixture, store.parent, dirs_exist_ok=True)\n"),
}


class StaticCheckTests(unittest.TestCase):
    def scan(self, code: str, directory: str = "tests", name: str = "sample.py") -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / directory).mkdir()
            (root / directory / name).write_text(textwrap.dedent(code))
            return auth_store_copiers(root)

    def test_every_listed_way_to_copy_the_store_is_caught(self):
        for name, code in MUST_FLAG.items():
            with self.subTest(sample=name):
                self.assertTrue(self.scan(code), f"the copier sample {name!r} is not caught")

    def test_the_symlink_helper_and_placeholders_are_not_flagged(self):
        flagged = {name: found for name, code in MUST_NOT_FLAG.items() if (found := self.scan(code))}
        self.assertEqual(flagged, {})

    def test_a_placeholder_with_a_copy_into_its_directory_is_not_flagged(self):
        flagged = [name for name, code in PLAUSIBLE_FALSE_POSITIVE.items() if self.scan(code)]
        self.assertEqual(flagged, [])

    def test_all_three_scanned_top_directories_are_checked(self):
        for top in ("src", "tests", "tools"):
            with self.subTest(top=top):
                self.assertTrue(self.scan(MUST_FLAG["shutil_copy"], directory=top), top)

    def test_a_copier_in_a_subdirectory_or_a_helper_named_like_the_check_is_caught(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deep = root / "tests" / "a" / "b"
            deep.mkdir(parents=True)
            (deep / "live_x.py").write_text(MUST_FLAG["shutil_copy"])
            (root / "tests" / "test_no_auth_store_copy_other.py").write_text(MUST_FLAG["shutil_copy"])
            found = auth_store_copiers(root)
        self.assertEqual(len(found), 2, found)

    def test_the_report_names_file_line_and_function(self):
        found = self.scan("import shutil\n\n\ndef stage(h, d):\n    shutil.copy(h / 'agent.db', d)\n")
        self.assertEqual(found, ["tests/sample.py:4:stage"])

    def test_the_repository_is_clean_and_so_are_non_python_files(self):
        self.assertEqual(auth_store_copiers(), [])
        pattern = re.compile(r"(?i)(\bcp\b|rsync|\binstall\b|\bdd\b|cat\b.*>|sqlite3.*\.backup|\.clone).*agent\.db"
                             r"|agent\.db.*(\bcp\b|rsync|\.backup)")
        hits = []
        for top in ("src", "tests", "tools"):
            for path in (ROOT / top).rglob("*"):
                if (not path.is_file() or path.suffix in {".py", ".pyc"} or ".venv" in path.parts
                        or "__pycache__" in path.parts or "node_modules" in path.parts):
                    continue
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (UnicodeDecodeError, OSError):
                    continue
                hits += [f"{path.relative_to(ROOT)}:{number}" for number, line in enumerate(lines, 1)
                         if pattern.search(line)]
        self.assertEqual(hits, [])

    def test_the_previous_live_helper_from_head_is_flagged_by_the_check(self):
        # The check's RED against the real code it replaced (git history), not only against a hand-made sample.
        done = subprocess.run(["git", "show", "HEAD:tests/integration/live_real_summary.py"], cwd=ROOT,
                              capture_output=True, text=True)
        if done.returncode != 0 or "_copy_auth" not in done.stdout:
            self.skipTest("HEAD does not hold the copying helper")
        found = self.scan_nested(done.stdout)
        self.assertTrue(any(item.endswith(":_copy_auth") for item in found), found)

    def scan_nested(self, code: str) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests" / "integration").mkdir(parents=True)
            (root / "tests" / "integration" / "live_real_summary.py").write_text(code)
            return auth_store_copiers(root)


class LinkAuthNeverOpensTheStoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="b12-auth-")
        self.addCleanup(self._dir.cleanup)
        self.tmp = Path(self._dir.name)
        self.store = self.tmp / "user" / "agent.db"
        self.store.parent.mkdir()
        self.store.write_bytes(b"PLACEHOLDER-NOT-A-REAL-STORE")
        self.root = self.tmp / "root"
        (self.root / "agent").mkdir(parents=True)

    def guarded(self):
        """Patches every way of opening or copying a file so that touching ``agent.db`` fails loudly."""
        def is_store(file) -> bool:
            try:
                return str(os.fspath(file)).endswith("agent.db") or str(os.fspath(file)) == str(self.store)
            except TypeError:
                return False

        def wrap(real, name):
            def spy(file, *args, **kwargs):
                if is_store(file):
                    raise AssertionError(f"{name} touched the auth store {file}")
                return real(file, *args, **kwargs)
            return spy

        def wrap_two(real, name):
            def spy(src, dst, *args, **kwargs):
                if is_store(src) or is_store(dst):
                    raise AssertionError(f"{name} touched the auth store")
                return real(src, dst, *args, **kwargs)
            return spy

        stack = mock.patch.multiple(builtins, open=wrap(builtins.open, "open"))
        patches = [stack,
                   mock.patch.object(io, "open", wrap(io.open, "io.open")),
                   mock.patch.object(os, "open", wrap(os.open, "os.open")),
                   mock.patch.object(sqlite3, "connect", wrap(sqlite3.connect, "sqlite3.connect")),
                   mock.patch.object(shutil, "copy", wrap_two(shutil.copy, "shutil.copy")),
                   mock.patch.object(shutil, "copy2", wrap_two(shutil.copy2, "shutil.copy2")),
                   mock.patch.object(shutil, "copyfile", wrap_two(shutil.copyfile, "shutil.copyfile")),
                   mock.patch.object(Path, "open", wrap(Path.open, "Path.open")),
                   mock.patch.object(Path, "read_bytes", wrap(Path.read_bytes, "Path.read_bytes")),
                   mock.patch.object(Path, "read_text", wrap(Path.read_text, "Path.read_text"))]
        return patches

    def test_link_auth_only_links_and_never_opens_reads_or_copies_the_store(self):
        before = (self.store.read_bytes(), self.store.stat().st_mtime_ns)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for patch in self.guarded():
                stack.enter_context(patch)
            trial._link_auth(self.store, self.root)
        link = self.root / "agent" / "agent.db"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(self.store))
        self.assertEqual((self.store.read_bytes(), self.store.stat().st_mtime_ns), before)
        self.assertEqual(sorted(path.name for path in self.store.parent.iterdir()), ["agent.db"])  # no -wal/-shm
        self.assertEqual(sorted(path.name for path in (self.root / "agent").iterdir()), ["agent.db"])

    def test_bad_sources_are_refused_before_anything_is_written(self):
        directory = self.tmp / "dir" / "agent.db"
        directory.mkdir(parents=True)
        indirect = self.tmp / "indirect" / "agent.db"
        indirect.parent.mkdir()
        indirect.symlink_to(self.store)
        for name, source in (("missing", self.tmp / "none" / "agent.db"), ("directory", directory),
                             ("symlink", indirect), ("relative", Path("agent.db"))):
            with self.subTest(source=name), self.assertRaises(trial.TrialFailure):
                trial._link_auth(source, self.root)
            self.assertEqual(list((self.root / "agent").iterdir()), [], name)

    def test_removing_the_agent_directory_keeps_the_target(self):
        trial._link_auth(self.store, self.root)
        shutil.rmtree(self.root / "agent")  # what the cleanup does
        self.assertEqual(self.store.read_bytes(), b"PLACEHOLDER-NOT-A-REAL-STORE")

    def test_the_helper_has_no_copy_path_left(self):
        for name in ("_copy_auth", "copy_auth", "_copy_store"):
            self.assertFalse(hasattr(trial, name), name)
        self.assertNotIn("sqlite3", trial.__dict__)
        done = subprocess.run(["grep", "-n", "copyfileobj\\|\\.backup(\\|sqlite3", str(Path(trial.__file__))],
                              capture_output=True, text=True)
        self.assertEqual(done.stdout, "")


if __name__ == "__main__":
    unittest.main()
