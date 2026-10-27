"""Independent adversarial verification of WBX-B2 (``skills/wb-inbox/wb_inbox.py``), seams S4 (subprocess) and S5.

Expected values come from the web-board-ux SPEC "`wb-inbox` skill과 `wb_inbox.py`" table, BRIEF WBX-D08/D09/D17/D23/
D25/D26 and the "외부 요청 API" table, not from the worker's tests. Focus: key hygiene on every path, ``.env`` parser
oddities, per-variable lookup, redirects, hostile servers, idempotency, cancel/result via the CLI.

Every CLI run is ``python -I <copy of wb_inbox.py>`` with a temp HOME and a fully controlled environment. Hostile
servers are in-test stdlib HTTP servers; the real REST surface is ``support.rest_hub.RestHub``.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import http.server
import itertools
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support.rest_hub import RestHub  # noqa: E402

SKILL = REPO / 'skills' / 'wb-inbox'
SCRIPT = SKILL / 'wb_inbox.py'
JSON_HEADERS = {'Content-Type': 'application/json'}


def new_key() -> str:
    return 'wbk_' + secrets.token_urlsafe(24)


class Out:
    def __init__(self, done: subprocess.CompletedProcess, seconds: float):
        self.code = done.returncode
        self.out = done.stdout.decode('utf-8', 'replace')
        self.err = done.stderr.decode('utf-8', 'replace')
        self.raw_out = done.stdout
        self.seconds = seconds

    @property
    def json(self):
        return json.loads(self.out)

    @property
    def both(self) -> str:
        return self.out + '\n' + self.err


class Req:
    def __init__(self, method, path, headers, body):
        self.method, self.path, self.headers, self.body = method, path, headers, body

    @property
    def auth(self):
        return self.headers.get('authorization')

    @property
    def json(self):
        return json.loads(self.body)


class Fake:
    """A programmable stdlib HTTP server. ``respond(req)`` returns ``(status, headers, body)`` or an action string
    (``'close'``, ``'garbage'``, ``'hang'``, ``'partial'``)."""

    def __init__(self, case: unittest.TestCase, respond):
        self.respond = respond
        self.requests: list[Req] = []
        self.release = threading.Event()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *args):
                pass

            def handle_any(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length else b''
                req = Req(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                owner.requests.append(req)
                try:
                    out = owner.respond(req)
                    if out == 'close':
                        return
                    if out == 'garbage':
                        self.wfile.write(b'\x00\x01 not http at all\r\n\r\n')
                        return
                    if out == 'hang':
                        owner.release.wait(120)
                        return
                    if out == 'partial':
                        self.wfile.write(b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n'
                                         b'Content-Length: 5000\r\n\r\n{"projects": [')
                        return
                    status, headers, payload = out
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = handle_any

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        case.addCleanup(self.stop)

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


def ok_json(obj, status=200, headers=None):
    return status, {**JSON_HEADERS, **(headers or {})}, json.dumps(obj).encode()


def projects_ok(req):
    return ok_json({'projects': []})


class CliCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='wbi-ind-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.skill = self.base / 'skill'
        self.skill.mkdir()
        shutil.copy(SCRIPT, self.skill / 'wb_inbox.py')
        self.home = self.base / 'home'
        self.home.mkdir()
        self.xdg = self.base / 'xdg'
        self.xdg.mkdir()
        self.cwd = self.base / 'cwd'
        self.cwd.mkdir()
        self.leaks: list[str] = []

    # --- files ---------------------------------------------------------------------------------

    @property
    def dotenv(self) -> Path:
        return self.skill / '.env'

    @property
    def client_env(self) -> Path:
        return self.xdg / 'omp-workbench' / 'client.env'

    def put(self, path: Path, content, mode: int = 0o600) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
        os.chmod(path, mode)
        return path

    # --- runs ----------------------------------------------------------------------------------

    def run_cli(self, *args: str, env: dict | None = None, stdin: bytes | str | None = None, xdg: bool = True,
                locale: str = 'C.UTF-8', timeout: float = 60, python: str | None = None) -> Out:
        environment = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'LC_ALL': locale}
        if xdg:
            environment['XDG_CONFIG_HOME'] = str(self.xdg)
        environment.update(env or {})
        data = stdin.encode('utf-8') if isinstance(stdin, str) else stdin
        started = time.monotonic()
        done = subprocess.run([python or sys.executable, '-I', str(self.skill / 'wb_inbox.py'), *args],
                              input=data if data is not None else b'', capture_output=True, env=environment,
                              cwd=self.cwd, timeout=timeout)
        out = Out(done, time.monotonic() - started)
        self.assertNotIn('Traceback', out.err, out.err)
        return out

    def no_key(self, out: Out, *secrets_: str) -> None:
        for secret in secrets_:
            self.assertNotIn(secret, out.both)
            if secret.startswith('wbk_'):
                self.assertNotIn(secret[4:], out.both)
        self.assertNotRegex(out.both, r'(?i)authorization:?\s*bearer')
        self.assertNotRegex(out.both, r'Bearer\s+(?!\[redacted\])\S')

    def tree(self) -> dict:
        snapshot = {}
        for root in (self.skill, self.home, self.xdg, self.cwd):
            for path in sorted(root.rglob('*')):
                if path.is_file() and not path.is_symlink():
                    snapshot[str(path)] = path.read_bytes()
        return snapshot


# ================================================================================================
# 1. Per-variable lookup order
# ================================================================================================

class LookupOrderTests(CliCase):
    def test_every_combination_of_layers_is_resolved_per_variable(self):
        servers = [Fake(self, projects_ok) for _ in range(3)]  # layer 0 process env, 1 skill .env, 2 client.env
        keys = [new_key() for _ in range(3)]
        subsets = [subset for size in (1, 2, 3) for subset in itertools.combinations(range(3), size)]
        runs = 0
        for url_layers in subsets:
            for key_layers in subsets:
                for server in servers:
                    server.requests.clear()
                env: dict = {}
                dotenv, client = [], []
                for layer in url_layers:
                    target = (env, dotenv, client)[layer]
                    (target.update({'WB_HUB_URL': servers[layer].url}) if layer == 0
                     else target.append(f'WB_HUB_URL={servers[layer].url}'))
                for layer in key_layers:
                    if layer == 0:
                        env['WB_API_KEY'] = keys[0]
                    else:
                        (dotenv, client)[layer - 1].append(f'WB_API_KEY={keys[layer]}')
                self.put(self.dotenv, '\n'.join(dotenv) + '\n') if dotenv else self.dotenv.unlink(missing_ok=True)
                self.put(self.client_env, '\n'.join(client) + '\n') if client else self.client_env.unlink(missing_ok=True)
                out = self.run_cli('projects', env=env)
                label = f'url={url_layers} key={key_layers}'
                self.assertEqual(out.code, 0, f'{label}\n{out.both}')
                hit = [i for i, s in enumerate(servers) if s.requests]
                self.assertEqual(hit, [min(url_layers)], label)
                self.assertEqual(servers[min(url_layers)].requests[0].auth, f'Bearer {keys[min(key_layers)]}', label)
                self.no_key(out, *keys)
                runs += 1
        self.assertEqual(runs, 49)

    def test_process_env_pair_never_reads_files_and_gives_no_permission_warning(self):
        server = Fake(self, projects_ok)
        decoy = Fake(self, projects_ok)
        key = new_key()
        self.put(self.dotenv, f'WB_HUB_URL={decoy.url}\nWB_API_KEY=wbk_decoy\n', mode=0o666)
        self.put(self.client_env, f'WB_HUB_URL={decoy.url}\nWB_API_KEY=wbk_decoy2\n', mode=0o644)
        out = self.run_cli('projects', env={'WB_HUB_URL': server.url, 'WB_API_KEY': key})
        self.assertEqual(out.code, 0, out.both)
        self.assertEqual((len(server.requests), len(decoy.requests)), (1, 0))
        self.assertNotIn('warning', out.err)  # the files were not read, so no mode complaint either
        self.no_key(out, key)

    def test_empty_process_variable_falls_through_to_the_next_source(self):
        server = Fake(self, projects_ok)
        key = new_key()
        self.put(self.dotenv, f'WB_HUB_URL={server.url}\nWB_API_KEY={key}\n')
        out = self.run_cli('projects', env={'WB_HUB_URL': '', 'WB_API_KEY': ''})
        self.assertEqual(out.code, 0, out.both)
        self.assertEqual(server.requests[0].auth, f'Bearer {key}')

    def test_relative_xdg_config_home_is_ignored_for_home_config(self):
        server = Fake(self, projects_ok)
        key = new_key()
        self.put(self.home / '.config' / 'omp-workbench' / 'client.env',
                 f'WB_HUB_URL={server.url}\nWB_API_KEY={key}\n')
        trap = Fake(self, projects_ok)
        self.put(self.cwd / 'rel' / 'omp-workbench' / 'client.env', f'WB_HUB_URL={trap.url}\nWB_API_KEY=wbk_trap\n')
        out = self.run_cli('projects', env={'XDG_CONFIG_HOME': 'rel'})
        self.assertEqual(out.code, 0, out.both)
        self.assertEqual((len(server.requests), len(trap.requests)), (1, 0))
        self.assertEqual(server.requests[0].auth, f'Bearer {key}')

    def test_unset_xdg_uses_home_config(self):
        server = Fake(self, projects_ok)
        key = new_key()
        self.put(self.home / '.config' / 'omp-workbench' / 'client.env',
                 f'WB_HUB_URL={server.url}\nWB_API_KEY={key}\n')
        out = self.run_cli('projects', xdg=False)
        self.assertEqual(out.code, 0, out.both)

    def test_missing_key_message_names_variable_and_all_places(self):
        server = Fake(self, projects_ok)
        out = self.run_cli('projects', env={'WB_HUB_URL': server.url})
        self.assertEqual(out.code, 3, out.both)
        self.assertEqual(server.requests, [])
        expected = (f'wb_inbox: WB_API_KEY is not set; looked in: process environment, {self.dotenv}, '
                    f'{self.client_env}')
        self.assertIn(expected, out.err.splitlines())
        self.assertEqual(out.out.strip(), '')

    def test_missing_url_message_and_no_request(self):
        out = self.run_cli('projects', env={'WB_API_KEY': new_key()})
        self.assertEqual(out.code, 3, out.both)
        self.assertIn(f'wb_inbox: WB_HUB_URL is not set; looked in: process environment, {self.dotenv}, '
                      f'{self.client_env}', out.err.splitlines())

    def test_missing_both_names_a_variable_and_exits_3(self):
        out = self.run_cli('projects')
        self.assertEqual(out.code, 3, out.both)
        self.assertRegex(out.err, r'WB_(API_KEY|HUB_URL) is not set; looked in: process environment, ')

    def test_missing_key_message_with_home_config_path_when_xdg_relative(self):
        server = Fake(self, projects_ok)
        out = self.run_cli('projects', env={'WB_HUB_URL': server.url, 'XDG_CONFIG_HOME': 'relative'})
        self.assertEqual(out.code, 3, out.both)
        self.assertIn(str(self.home / '.config' / 'omp-workbench' / 'client.env'), out.err)
        self.assertNotIn('relative/omp-workbench', out.err)

    def test_config_reports_sources_and_never_the_key(self):
        server = Fake(self, projects_ok)
        key = new_key()
        self.put(self.dotenv, f'WB_API_KEY={key}\n')
        out = self.run_cli('config', env={'WB_HUB_URL': server.url})
        self.assertEqual(out.code, 0, out.both)
        self.assertEqual(server.requests, [])
        data = out.json
        text = json.dumps(data)
        self.assertIn(server.url, text)
        self.assertIn(str(self.dotenv), text)
        self.assertIn('set', text)
        self.no_key(out, key)

    def test_config_with_missing_key_says_missing(self):
        server = Fake(self, projects_ok)
        out = self.run_cli('config', env={'WB_HUB_URL': server.url})
        self.assertEqual(out.code, 3, out.both)
        self.assertIn('missing', out.out)
        self.assertEqual(server.requests, [])


# ================================================================================================
# 2. .env syntax
# ================================================================================================

MISSING = object()


class DotenvParserTests(CliCase):
    def received(self, content, *, mode=0o600, env=None) -> tuple[Out, object]:
        server = Fake(self, projects_ok)
        self.put(self.dotenv, content, mode=mode)
        out = self.run_cli('projects', env={'WB_HUB_URL': server.url, **(env or {})})
        got = server.requests[0].auth if server.requests else MISSING
        return out, got

    def check(self, content, expected, **kwargs):
        out, got = self.received(content, **kwargs)
        if expected is MISSING:
            self.assertIs(got, MISSING, out.both)
            self.assertEqual(out.code, 3, out.both)
        else:
            self.assertEqual(out.code, 0, out.both)
            self.assertEqual(got, f'Bearer {expected}', out.both)

    def test_documented_syntax(self):
        cases = [
            ('WB_API_KEY=plain\n', 'plain'),
            ('WB_API_KEY=plain', 'plain'),
            ('WB_API_KEY="double"\n', 'double'),
            ("WB_API_KEY='single'\n", 'single'),
            ('export WB_API_KEY=exp\n', 'exp'),
            ('export WB_API_KEY="expq"\n', 'expq'),
            ('# header comment\n\n   \nWB_API_KEY=aftercomment\n', 'aftercomment'),
            ('#WB_API_KEY=commented\nWB_API_KEY=real\n', 'real'),
            ('WB_API_KEY=a=b==c\n', 'a=b==c'),
            ('WB_API_KEY=ab#cd\n', 'ab#cd'),
            ('WB_API_KEY=pre$HOME${X}\n', 'pre$HOME${X}'),
            ('WB_API_KEY=crlf\r\n', 'crlf'),
            ('WB_API_KEY="crlfq"\r\nOTHER=1\r\n', 'crlfq'),
            ('OTHER=1\nnot an assignment\nWB_API_KEY\nWB_API_KEY=afterjunk\n', 'afterjunk'),
            ('WB_API_KEY_OLD=old\nWB_API_KEY=new\n', 'new'),
            ('wb_api_key=lower\n', MISSING),
            ('XWB_API_KEY=other\n', MISSING),
            ('# WB_API_KEY=only a comment\n', MISSING),
            ('WB_API_KEY=\n', MISSING),
            ('WB_API_KEY=""\n', MISSING),
            ("WB_API_KEY=''\n", MISSING),
            ('', MISSING),
        ]
        for content, expected in cases:
            with self.subTest(content=content):
                self.check(content, expected)

    def test_mismatched_and_lone_quotes_are_kept_verbatim_not_sliced(self):
        for content, expected in [('WB_API_KEY="abc\'\n', '"abc\''), ('WB_API_KEY="abc\n', '"abc'),
                                  ('WB_API_KEY="\n', '"'), ("WB_API_KEY='\n", "'")]:
            with self.subTest(content=content):
                out, got = self.received(content)
                self.assertNotIn('Traceback', out.err)
                # verbatim is the documented rule ("같은 따옴표" on both ends); anything else must at least not slice
                self.assertIn(got, (f'Bearer {expected}', MISSING), out.both)

    def test_trailing_and_leading_whitespace_is_not_part_of_the_key(self):
        for content in ('WB_API_KEY=abc   \n', 'WB_API_KEY=abc\t\n', '   WB_API_KEY=abc\n'):
            with self.subTest(content=content):
                out, got = self.received(content)
                self.assertIn(got, ('Bearer abc', MISSING), out.both)
                if got is not MISSING:
                    self.assertEqual(got, 'Bearer abc')

    def test_bom_crlf_and_spaced_equals_do_not_break_or_leak(self):
        key = 'bomkey'
        for content in ('﻿WB_API_KEY=bomkey\n', '﻿WB_API_KEY=bomkey\r\n', 'WB_API_KEY = bomkey\n'):
            with self.subTest(content=content):
                out, got = self.received(content)
                self.assertIn(got, (f'Bearer {key}', MISSING), out.both)
                self.assertIn(out.code, (0, 3))

    def test_bom_first_line_is_honoured(self):
        # Contract-silent but a real-world Windows editor default: a lost first line looks like "key missing".
        out, got = self.received('﻿WB_API_KEY=bomkey\n')
        self.assertEqual(got, 'Bearer bomkey', out.both)

    def test_duplicate_assignments_pick_exactly_one_deterministically(self):
        out, got = self.received('WB_API_KEY=first\nWB_API_KEY=second\n')
        self.assertIn(got, ('Bearer first', 'Bearer second'), out.both)

    def test_weird_bytes_do_not_crash_or_leak(self):
        key = new_key()
        for content in (b'\xff\xfe\x00garbage\n', b'WB_API_KEY=' + key.encode() + b'\x00\n',
                        b'WB_API_KEY=' + b'A' * 2_000_000 + b'\n', b'\n' * 100000 + b'WB_API_KEY=' + key.encode(),
                        b'WB_API_KEY=' + key.encode() + b'\xff\n', b'export\nexport \nexport =x\n'):
            with self.subTest(size=len(content), head=content[:12]):
                server = Fake(self, projects_ok)
                self.put(self.dotenv, content)
                out = self.run_cli('projects', env={'WB_HUB_URL': server.url})
                self.assertIn(out.code, (0, 1, 3, 4), out.both)
                self.no_key(out, key)

    def test_binary_dotenv_does_not_block_other_layers_from_being_reported(self):
        server = Fake(self, projects_ok)
        key = new_key()
        self.put(self.dotenv, b'\xff\xfe\xfd')
        self.put(self.client_env, f'WB_API_KEY={key}\n')
        out = self.run_cli('projects', env={'WB_HUB_URL': server.url})
        self.assertIn(out.code, (0, 3), out.both)  # decoded leniently (0) or reported as a config problem (3)
        self.no_key(out, key)

    def test_symlinked_dotenv_checks_the_mode_of_the_target(self):
        server = Fake(self, projects_ok)
        key = new_key()
        target = self.put(self.base / 'real.env', f'WB_API_KEY={key}\n', mode=0o600)
        self.dotenv.symlink_to(target)
        out = self.run_cli('projects', env={'WB_HUB_URL': server.url})
        self.assertEqual(out.code, 0, out.both)
        self.assertNotIn('warning', out.err)  # an lstat of the link (0777) would wrongly warn
        os.chmod(target, 0o644)
        out = self.run_cli('projects', env={'WB_HUB_URL': server.url})
        self.assertEqual(out.code, 0, out.both)
        self.assertIn('mode 0644', out.err)
        self.no_key(out, key)

    def test_dangling_loop_and_directory_dotenv_do_not_crash(self):
        server = Fake(self, projects_ok)
        key = new_key()
        for make in (lambda: self.dotenv.symlink_to(self.skill / 'nowhere'),
                     lambda: self.dotenv.symlink_to(self.dotenv),
                     lambda: self.dotenv.mkdir()):
            if self.dotenv.is_dir() and not self.dotenv.is_symlink():
                self.dotenv.rmdir()
            else:
                self.dotenv.unlink(missing_ok=True)
            make()
            out = self.run_cli('projects', env={'WB_HUB_URL': server.url, 'WB_API_KEY': key}, timeout=30)
            self.assertEqual(out.code, 0, out.both)  # key is in the process env: files must not even matter
            out = self.run_cli('projects', env={'WB_HUB_URL': server.url}, timeout=30)
            self.assertEqual(out.code, 3, out.both)
            self.assertIn('WB_API_KEY is not set', out.err)

    def test_fifo_dotenv_does_not_hang_the_cli(self):
        server = Fake(self, projects_ok)
        os.mkfifo(self.dotenv)
        try:
            out = self.run_cli('projects', env={'WB_HUB_URL': server.url}, timeout=10)
        except subprocess.TimeoutExpired:
            self.fail('a FIFO named .env blocks the CLI forever (open() without O_NONBLOCK / regular-file check)')
        self.assertEqual(out.code, 3, out.both)

    def test_unreadable_dotenv_is_reported_without_traceback_or_leak(self):
        server = Fake(self, projects_ok)
        key = new_key()
        self.put(self.dotenv, f'WB_API_KEY={key}\n', mode=0o000)
        self.addCleanup(os.chmod, self.dotenv, 0o600)
        self.put(self.client_env, f'WB_API_KEY={key}\n')
        out = self.run_cli('projects', env={'WB_HUB_URL': server.url})
        self.assertIn(out.code, (0, 3), out.both)
        self.no_key(out, key)

    def test_client_env_gets_the_same_syntax(self):
        server = Fake(self, projects_ok)
        self.put(self.client_env, 'export WB_API_KEY="from-client"\r\n')
        out = self.run_cli('projects', env={'WB_HUB_URL': server.url})
        self.assertEqual(out.code, 0, out.both)
        self.assertEqual(server.requests[0].auth, 'Bearer from-client')


class ProcessEnvOddityTests(CliCase):
    def test_newline_and_non_latin1_keys_never_echo_the_key(self):
        server = Fake(self, projects_ok)
        for key in ('wbk_abc\ndef: x', 'wbk_abc\r\nX-Evil: 1', 'wbk_한글secret', 'wbk_café€secret',
                    'wbk_tab\there', 'wbk_nul\x00here'):
            with self.subTest(key=key):
                try:
                    out = self.run_cli('projects', env={'WB_HUB_URL': server.url, 'WB_API_KEY': key})
                except ValueError:  # NUL cannot even be put in an environment
                    continue
                self.assertIn(out.code, (0, 3, 4), out.both)
                for fragment in ('abc', 'def', 'secret', 'here', 'é', '한', '€'):
                    if fragment in key:
                        self.assertNotIn(fragment, out.both, out.both)
                self.assertNotIn('X-Evil', ''.join(r.headers.get('x-evil', '') for r in server.requests))
        self.assertTrue(all('x-evil' not in r.headers for r in server.requests))


# ================================================================================================
# 3. .env permission warning
# ================================================================================================

class PermissionWarningTests(CliCase):
    def run_with(self, path_kind: str, mode: int) -> tuple[Out, Path]:
        server = Fake(self, projects_ok)
        key = new_key()
        self.dotenv.unlink(missing_ok=True)
        self.client_env.unlink(missing_ok=True)
        if path_kind == 'skill':
            path = self.put(self.dotenv, f'WB_API_KEY={key}\n', mode=mode)
        else:
            path = self.put(self.client_env, f'WB_API_KEY={key}\n', mode=mode)
        out = self.run_cli('projects', env={'WB_HUB_URL': server.url})
        self.assertEqual(out.code, 0, out.both)
        self.no_key(out, key)
        return out, path

    def test_any_group_or_other_bit_warns_with_exact_text_on_stderr(self):
        for kind in ('skill', 'client'):
            for mode in (0o644, 0o640, 0o604, 0o660, 0o602, 0o601, 0o666, 0o777, 0o620, 0o610, 0o664):
                with self.subTest(kind=kind, mode=oct(mode)):
                    out, path = self.run_with(kind, mode)
                    line = (f'wb_inbox: warning: {path} is accessible by other users (mode 0{mode:o}); '
                            f'run: chmod 600 {path}')
                    self.assertIn(line, out.err.splitlines())
                    self.assertNotIn('warning', out.out)
                    json.loads(out.out)  # stdout stays pure JSON

    def test_owner_only_modes_do_not_warn(self):
        for mode in (0o600, 0o400, 0o700, 0o500, 0o200):
            with self.subTest(mode=oct(mode)):
                server = Fake(self, projects_ok)
                key = new_key()
                self.put(self.dotenv, f'WB_API_KEY={key}\n', mode=mode)
                os.chmod(self.dotenv, mode | 0o400)  # keep it readable for the run
                out = self.run_cli('projects', env={'WB_HUB_URL': server.url})
                self.assertEqual(out.code, 0, out.both)
                self.assertNotIn('warning', out.err)

    def test_file_is_still_used_after_warning(self):
        server = Fake(self, projects_ok)
        key = new_key()
        self.put(self.dotenv, f'WB_HUB_URL={server.url}\nWB_API_KEY={key}\n', mode=0o644)
        out = self.run_cli('projects')
        self.assertEqual(out.code, 0, out.both)
        self.assertEqual(server.requests[0].auth, f'Bearer {key}')
        self.assertIn('warning', out.err)

    def test_warning_is_emitted_once_per_file(self):
        server = Fake(self, projects_ok)
        self.put(self.dotenv, f'WB_HUB_URL={server.url}\nWB_API_KEY={new_key()}\n', mode=0o644)
        out = self.run_cli('projects')
        self.assertEqual(out.err.count('is accessible by other users'), 1, out.err)

    def test_warning_also_for_config_and_when_only_one_variable_is_read(self):
        server = Fake(self, projects_ok)
        self.put(self.dotenv, f'WB_API_KEY={new_key()}\n', mode=0o640)
        out = self.run_cli('config', env={'WB_HUB_URL': server.url})
        self.assertIn('mode 0640', out.err)

    def test_missing_key_still_warns_about_the_broad_file_that_was_read(self):
        server = Fake(self, projects_ok)
        self.put(self.dotenv, 'OTHER=1\n', mode=0o644)
        out = self.run_cli('projects', env={'WB_HUB_URL': server.url})
        self.assertEqual(out.code, 3)
        self.assertIn('wb_inbox: WB_API_KEY is not set', out.err)


# ================================================================================================
# 4. URL validation
# ================================================================================================

class UrlTests(CliCase):
    def test_valid_urls_hit_the_documented_path_without_double_slash(self):
        server = Fake(self, projects_ok)
        key = new_key()
        for suffix in ('', '/'):
            server.requests.clear()
            out = self.run_cli('projects', env={'WB_HUB_URL': server.url + suffix, 'WB_API_KEY': key})
            self.assertEqual(out.code, 0, out.both)
            self.assertEqual(server.requests[0].path, '/api/v1/projects')
            self.assertEqual(server.requests[0].auth, f'Bearer {key}')

    def test_query_parameters_are_encoded_not_injected(self):
        server = Fake(self, lambda r: ok_json({'projects': []}))
        out = self.run_cli('projects', '--query', 'a b&x=1#frag?é', env={'WB_HUB_URL': server.url, 'WB_API_KEY': new_key()})
        self.assertEqual(out.code, 0, out.both)
        path = server.requests[0].path
        self.assertTrue(path.startswith('/api/v1/projects?query='), path)
        self.assertNotIn('#', path)
        self.assertEqual(path.count('?'), 1)
        self.assertEqual(path.count('&'), 0)
        self.assertNotIn(' ', path)

    def test_invalid_urls_exit_3_before_any_request_and_never_echo_userinfo(self):
        key = new_key()
        server = Fake(self, projects_ok)
        u = server.url
        host = f'127.0.0.1:{server.port}'
        bad = [f'ftp://{host}', f'file:///etc/passwd', host, f'//{host}', 'http://', 'http:///x',
               f'http://user:pa55w0rd@{host}', f'http://user@{host}', f'{u}/?a=b', f'{u}?a=b', f'{u}/#f',
               f'{u}#f', f'{u}/api', f'{u}//', f'{u}/api/v1', 'http://127.0.0.1:99999', 'http://127.0.0.1:abc',
               'http://:80', 'http:127.0.0.1', f' {u}x y', f'{u}/\r\nX-Evil: 1', f'{u}\n', 'javascript:alert(1)',
               f'{u}/%2e%2e', f'{u}/.', f'{u}/;params', 'http://127.0.0.1:-1', 'https://']
        for url in bad:
            with self.subTest(url=url):
                server.requests.clear()
                out = self.run_cli('projects', env={'WB_HUB_URL': url, 'WB_API_KEY': key})
                self.assertEqual(out.code, 3, out.both)
                self.assertEqual(server.requests, [])
                self.assertNotIn('pa55w0rd', out.both)
                self.no_key(out, key)

    def test_valid_forms_pass_config(self):
        for url in ('http://example.com', 'https://example.com:8443/', 'http://127.0.0.1:8780', 'http://[::1]:8780',
                    'https://hub.internal'):
            with self.subTest(url=url):
                out = self.run_cli('config', env={'WB_HUB_URL': url, 'WB_API_KEY': new_key()})
                self.assertEqual(out.code, 0, out.both)


# ================================================================================================
# 5. Real Hub flows
# ================================================================================================

class HubCase(CliCase):
    def setUp(self):
        super().setUp()
        self.hub = RestHub(self)
        self.url = f'http://127.0.0.1:{self.hub.port}'
        self.r1 = self.hub.key('r1')
        self.r2 = self.hub.key('r2', 'requester', {'projects': [self.hub.b]})
        self.ed = self.hub.key('ed', 'editor')
        self.keys = (self.r1, self.r2, self.ed)

    def cli(self, key, *args, **kwargs) -> Out:
        out = self.run_cli(*args, env={'WB_HUB_URL': self.url, 'WB_API_KEY': key}, **kwargs)
        self.no_key(out, *self.keys)
        return out

    def submit(self, key, *extra, project=None, title='T', body='B') -> Out:
        args = ['submit', '--project', project or self.hub.a, '--title', title]
        if body is not None:
            args += ['--body', body]
        return self.cli(key, *args, *extra)

    def rid(self, out: Out) -> str:
        self.assertEqual(out.code, 0, out.both)
        return out.json['request_id']

    def requests(self, key=None) -> list:
        return self.hub.rest(key or self.r1).get('/requests', include_closed=True).json['requests']


class ProjectsAndSubmitTests(HubCase):
    def test_projects_equals_the_rest_answer_and_query_filters(self):
        out = self.cli(self.r1, 'projects')
        self.assertEqual(out.code, 0, out.both)
        self.assertEqual(out.json, self.hub.rest(self.r1).get('/projects').json)
        names = {p['name'] for p in out.json['projects']}
        self.assertEqual(names, {'alpha', 'beta'})
        out = self.cli(self.r1, 'projects', '--query', 'alp')
        self.assertEqual({p['name'] for p in out.json['projects']}, {'alpha'})
        self.assertEqual(self.cli(self.r2, 'projects').json['projects'][0]['name'], 'beta')

    def test_same_content_twice_is_one_request_and_new_forces_another(self):
        first = self.submit(self.r1)
        second = self.submit(self.r1)
        self.assertEqual((first.code, second.code), (0, 0), first.both + second.both)
        self.assertEqual(first.json['request_id'], second.json['request_id'])
        self.assertIs(first.json['created'], True)
        self.assertIs(second.json['created'], False)
        self.assertEqual(first.json['idempotency_key'], second.json['idempotency_key'])
        self.assertEqual(len(self.requests()), 1)
        forced = self.submit(self.r1, '--new')
        self.assertNotEqual(forced.json['request_id'], first.json['request_id'])
        self.assertRegex(forced.json['idempotency_key'], r'^wbi-new-[0-9a-f]{32}$')
        forced2 = self.submit(self.r1, '--new')
        self.assertNotEqual(forced2.json['request_id'], forced.json['request_id'])
        self.assertNotEqual(forced2.json['idempotency_key'], forced.json['idempotency_key'])
        self.assertEqual(len(self.requests()), 3)

    def test_different_project_title_body_make_different_requests(self):
        base = self.rid(self.submit(self.r1))
        ids = {base,
               self.rid(self.submit(self.r1, project=self.hub.b)),
               self.rid(self.submit(self.r1, title='T2')),
               self.rid(self.submit(self.r1, body='B2'))}
        self.assertEqual(len(ids), 4)
        # NUL-separated: moving a character between title and body must not collide
        self.assertNotEqual(self.rid(self.submit(self.r1, title='ab', body='c')),
                            self.rid(self.submit(self.r1, title='a', body='bc')))

    def test_explicit_key_and_new_conflict_is_usage_error_without_a_request(self):
        before = len(self.requests())
        out = self.submit(self.r1, '--new', '--idempotency-key', 'k')
        self.assertEqual(out.code, 2, out.both)
        self.assertEqual(len(self.requests()), before)

    def test_explicit_idempotency_key_semantics(self):
        a = self.submit(self.r1, '--idempotency-key', 'mine-1')
        b = self.submit(self.r1, '--idempotency-key', 'mine-1')
        self.assertEqual(a.json['request_id'], b.json['request_id'])
        self.assertEqual(a.json['idempotency_key'], 'mine-1')
        conflict = self.submit(self.r1, '--idempotency-key', 'mine-1', body='different')
        self.assertEqual(conflict.code, 1, conflict.both)
        self.assertEqual(conflict.json['error']['code'], 'idempotency_conflict')
        self.assertRegex(conflict.err, r'^wb_inbox: error 409 idempotency_conflict: ', )

    def test_body_sources_give_the_same_request(self):
        body = 'multi\nline 한글 é'
        path = self.put(self.base / 'body.txt', body)
        via_arg = self.rid(self.submit(self.r1, body=body))
        via_file = self.rid(self.cli(self.r1, 'submit', '--project', self.hub.a, '--title', 'T', '--body-file', str(path)))
        via_stdin = self.rid(self.cli(self.r1, 'submit', '--project', self.hub.a, '--title', 'T', '--body-file', '-',
                                      stdin=body))
        self.assertEqual({via_arg, via_file, via_stdin}, {via_arg})
        got = self.cli(self.r1, 'get', via_arg).json['request']['body']
        self.assertEqual(got, body)

    def test_trailing_newline_is_preserved_consistently(self):
        path = self.put(self.base / 'nl.txt', 'line\n')
        a = self.rid(self.cli(self.r1, 'submit', '--project', self.hub.a, '--title', 'N', '--body-file', str(path)))
        b = self.rid(self.cli(self.r1, 'submit', '--project', self.hub.a, '--title', 'N', '--body-file', '-',
                              stdin='line\n'))
        c = self.rid(self.submit(self.r1, title='N', body='line\n'))
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_submit_uses_the_documented_derived_key(self):
        out = self.submit(self.r1, title='k-title', body='k-body')
        digest = hashlib.sha256(f'{self.hub.a}\0k-title\0k-body'.encode()).hexdigest()[:32]
        self.assertEqual(out.json['idempotency_key'], f'wbi-{digest}')
        # the server saw exactly that key: re-sending it by hand is the same request
        again = self.hub.rest(self.r1).post('/requests', {'project_id': self.hub.a, 'title': 'k-title',
                                                           'body': 'k-body', 'idempotency_key': f'wbi-{digest}'})
        self.assertEqual((again.status, again.json['request_id']), (200, out.json['request_id']))

    def test_korean_roundtrip_under_a_non_utf8_locale(self):
        title = '한글 제목'
        for locale in ('C', 'POSIX', 'C.UTF-8'):
            out = self.run_cli('submit', '--project', self.hub.a, '--title', title, '--body', '본문',
                               env={'WB_HUB_URL': self.url, 'WB_API_KEY': self.r1}, locale=locale)
            self.assertEqual(out.code, 0, out.both)
            self.assertEqual(out.raw_out.decode('utf-8'), out.out)
            got = self.cli(self.r1, 'get', out.json['request_id'], locale=locale)
            self.assertIn('한글', got.out)  # ensure_ascii=False
            self.assertEqual(got.json['request']['title'], title)

    def test_body_flag_errors(self):
        before = len(self.requests())
        for args in (['submit', '--project', self.hub.a, '--title', 'T'],
                     ['submit', '--project', self.hub.a, '--title', 'T', '--body', 'x', '--body-file', '-'],
                     ['submit', '--title', 'T', '--body', 'x'],
                     ['submit', '--project', self.hub.a, '--body', 'x']):
            with self.subTest(args=args):
                out = self.cli(self.r1, *args, stdin='ignored')
                self.assertEqual(out.code, 2, out.both)
        for args in (['--body-file', str(self.base / 'does-not-exist')],):
            out = self.cli(self.r1, 'submit', '--project', self.hub.a, '--title', 'T', *args)
            self.assertNotEqual(out.code, 0)
            self.assertIn(out.code, (2, 3), out.both)
        bad = self.put(self.base / 'bad.bin', b'\xff\xfe\xfd')
        out = self.cli(self.r1, 'submit', '--project', self.hub.a, '--title', 'T', '--body-file', str(bad))
        self.assertNotEqual(out.code, 0, out.both)
        self.assertEqual(len(self.requests()), before)

    def test_oversize_stdin_body_is_rejected_by_server_with_exit_1(self):
        out = self.cli(self.r1, 'submit', '--project', self.hub.a, '--title', 'big', '--body-file', '-',
                       stdin='x' * 400_000)
        self.assertEqual(out.code, 1, out.both[:300])
        self.assertIn(out.json['error']['code'], ('too_large', 'invalid_input'))

    def test_requester_outside_scope_gets_exit_1_with_error_line(self):
        out = self.submit(self.r2, project=self.hub.a)
        self.assertEqual(out.code, 1, out.both)
        self.assertRegex(out.err.strip(), r'^wb_inbox: error (403|404) [a-z_]+: .+$')
        self.assertEqual(len(out.err.strip().splitlines()), 1)
        self.assertIn('error', out.json)


class ListGetCommentTests(HubCase):
    def seed(self, count=5):
        return [self.rid(self.submit(self.r1, title=f'T{n}', body=f'B{n}')) for n in range(count)]

    def test_list_pages_with_cursor_through_the_cli(self):
        ids = set(self.seed(5))
        seen, cursor, pages = [], None, 0
        while True:
            args = ['list', '--limit', '2'] + (['--cursor', cursor] if cursor else [])
            out = self.cli(self.r1, *args)
            self.assertEqual(out.code, 0, out.both)
            seen += [r['request_id'] for r in out.json['requests']]
            cursor = out.json['next_cursor']
            pages += 1
            if cursor is None:
                break
            self.assertLess(pages, 10)
        self.assertEqual(sorted(seen), sorted(ids))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(pages, 3)

    def test_list_filters_are_forwarded(self):
        rid = self.seed(2)[0]
        beta = self.rid(self.submit(self.r1, project=self.hub.b, title='beta-one'))
        only_b = self.cli(self.r1, 'list', '--project', self.hub.b).json['requests']
        self.assertEqual([r['request_id'] for r in only_b], [beta])
        q = self.cli(self.r1, 'list', '--query', 'beta-one').json['requests']
        self.assertEqual([r['request_id'] for r in q], [beta])
        self.hub.rest(self.ed).post(f'/requests/{rid}/state', {'base_version': 1, 'to': 'NEEDS_INFO'})
        both = self.cli(self.r1, 'list', '--state', 'NEEDS_INFO', '--state', 'NEW').json['requests']
        self.assertEqual({r['state'] for r in both}, {'NEEDS_INFO', 'NEW'})
        only = self.cli(self.r1, 'list', '--state', 'NEEDS_INFO').json['requests']
        self.assertEqual([r['request_id'] for r in only], [rid])
        self.assertEqual(self.cli(self.r1, 'cancel', beta).code, 0)
        self.assertNotIn(beta, [r['request_id'] for r in self.cli(self.r1, 'list').json['requests']])
        closed = self.cli(self.r1, 'list', '--include-closed').json['requests']
        self.assertIn(beta, [r['request_id'] for r in closed])

    def test_list_bad_arguments_are_clean_failures(self):
        for args in (['--limit', 'abc'], ['--limit', '0'], ['--limit', '101'], ['--limit', '-1'],
                     ['--state', 'BOGUS']):
            with self.subTest(args=args):
                out = self.cli(self.r1, 'list', *args)
                self.assertIn(out.code, (1, 2), out.both)

    def test_get_and_get_errors(self):
        rid = self.seed(1)[0]
        out = self.cli(self.r1, 'get', rid)
        self.assertEqual(out.code, 0, out.both)
        self.assertEqual(out.json['request']['request_id'], rid)
        missing = self.cli(self.r1, 'get', 'no-such-request')
        self.assertEqual(missing.code, 1)
        self.assertRegex(missing.err.strip(), r'^wb_inbox: error (400|404) (not_found|invalid_input): ')
        self.assertIn('error', missing.json)
        other = self.cli(self.r2, 'get', rid)  # beta-only key must not see an alpha request
        self.assertEqual(other.code, 1)

    def test_request_id_cannot_escape_its_path_segment(self):
        server = Fake(self, lambda r: ok_json({'error': {'code': 'not_found', 'message': 'x'}}, 404))
        for rid in ('x/../projects', 'x?state=NEW', 'x#f', 'a/comments', 'x y', 'é', '%2e%2e'):
            with self.subTest(rid=rid):
                server.requests.clear()
                out = self.run_cli('get', rid, env={'WB_HUB_URL': server.url, 'WB_API_KEY': new_key()})
                self.assertIn(out.code, (1, 2, 3), out.both)
                for req in server.requests:
                    path = req.path
                    self.assertTrue(path.startswith('/api/v1/requests/'), path)
                    rest = path[len('/api/v1/requests/'):]
                    self.assertNotIn('/', rest)
                    self.assertNotIn('?', rest)
                    self.assertNotIn('#', rest)
                    self.assertNotIn(' ', rest)
                    self.assertNotEqual(rest, '..')

    def test_dot_segment_request_ids_do_not_become_path_traversal(self):
        server = Fake(self, lambda r: ok_json({'error': {'code': 'not_found', 'message': 'x'}}, 404))
        for rid in ('.', '..'):
            with self.subTest(rid=rid):
                server.requests.clear()
                out = self.run_cli('get', rid, env={'WB_HUB_URL': server.url, 'WB_API_KEY': new_key()})
                self.assertIn(out.code, (1, 2, 3), out.both)
                for req in server.requests:  # '/api/v1/requests/..' is normalised to '/api/v1/' by any proxy
                    self.assertNotIn(req.path.rsplit('/', 1)[-1], ('.', '..'), req.path)

    def test_comment_via_arg_file_and_stdin(self):
        rid = self.seed(1)[0]
        a = self.cli(self.r1, 'comment', rid, '--body', 'hello')
        self.assertEqual(a.code, 0, a.both)
        self.assertIn('comment_id', a.json)
        f = self.put(self.base / 'c.txt', 'from file')
        self.assertEqual(self.cli(self.r1, 'comment', rid, '--body-file', str(f)).code, 0)
        self.assertEqual(self.cli(self.r1, 'comment', rid, '--body-file', '-', stdin='from stdin 한').code, 0)
        comments = self.cli(self.r1, 'get', rid).json['request']['comments']
        self.assertEqual([c['body'] for c in comments], ['hello', 'from file', 'from stdin 한'])
        self.assertEqual(self.cli(self.r1, 'comment', rid).code, 2)
        self.assertEqual(self.cli(self.r1, 'comment', rid, '--body', 'x', '--body-file', '-').code, 2)
        self.assertEqual(self.cli(self.r1, 'comment', '--body', 'x').code, 2)


class EditorAndCancelTests(HubCase):
    def make(self) -> str:
        return self.rid(self.submit(self.r1, title='E', body='orig'))

    def test_update_and_set_state_as_editor(self):
        rid = self.make()
        version = self.cli(self.ed, 'get', rid).json['request']['version']
        out = self.cli(self.ed, 'update', rid, '--base-version', str(version), '--title', 'new title', '--body', 'new body')
        self.assertEqual(out.code, 0, out.both)
        got = self.cli(self.ed, 'get', rid).json['request']
        self.assertEqual((got['title'], got['body']), ('new title', 'new body'))
        stale = self.cli(self.ed, 'update', rid, '--base-version', str(version), '--title', 'late')
        self.assertEqual(stale.code, 1, stale.both)
        self.assertEqual(stale.json['error']['code'], 'conflict')
        self.assertRegex(stale.err.strip(), r'^wb_inbox: error 409 conflict: ')
        version = got['version']
        file = self.put(self.base / 'u.txt', 'body from stdin')
        self.assertEqual(self.cli(self.ed, 'update', rid, '--base-version', str(version), '--body-file', str(file)).code, 0)
        version = self.cli(self.ed, 'get', rid).json['request']['version']
        out = self.cli(self.ed, 'set-state', rid, '--base-version', str(version), '--to', 'NEEDS_INFO', '--reason', 'why')
        self.assertEqual(out.code, 0, out.both)
        self.assertEqual(out.json['state'], 'NEEDS_INFO')
        version = out.json['version']
        out = self.cli(self.ed, 'set-state', rid, '--base-version', str(version), '--to', 'NEW')
        self.assertEqual(out.json['state'], 'NEW')

    def test_requester_gets_403_exit_1_on_editor_endpoints(self):
        rid = self.make()
        for args in (['update', rid, '--base-version', '1', '--title', 'x'],
                     ['set-state', rid, '--base-version', '1', '--to', 'NEEDS_INFO']):
            with self.subTest(args=args):
                out = self.cli(self.r1, *args)
                self.assertEqual(out.code, 1, out.both)
                self.assertEqual(out.json['error']['code'], 'forbidden')
                self.assertRegex(out.err.strip(), r'^wb_inbox: error 403 forbidden: ')

    def test_usage_errors_for_editor_commands(self):
        rid = self.make()
        for args in (['update', rid, '--title', 'x'], ['update', rid, '--base-version', 'one', '--title', 'x'],
                     ['set-state', rid, '--base-version', '1', '--to', 'READY'], ['set-state', rid, '--to', 'NEW'],
                     ['set-state', rid, '--base-version', '1'], ['update', '--base-version', '1']):
            with self.subTest(args=args):
                self.assertEqual(self.cli(self.ed, *args).code, 2)
        out = self.cli(self.ed, 'update', rid, '--base-version', '1')  # nothing to change: clean refusal
        self.assertNotEqual(out.code, 0)

    def test_cancel_before_execution_is_one_step(self):
        rid = self.make()
        out = self.cli(self.r1, 'cancel', rid, '--reason', 'changed my mind')
        self.assertEqual(out.code, 0, out.both)
        self.assertEqual((out.json['status'], out.json['state']), ('cancelled', 'CLOSED'))
        self.assertEqual(self.cli(self.r1, 'get', rid).json['request']['state'], 'CLOSED')

    def test_running_cancel_needs_the_confirm_token(self):
        rid = self.make()
        self.hub.link_running(rid)
        first = self.cli(self.r1, 'cancel', rid)
        self.assertEqual(first.code, 0, first.both)  # confirm_required is a success exit
        self.assertEqual(first.json['status'], 'confirm_required')
        token = first.json['confirm_token']
        self.assertEqual(self.hub.cancels, [])
        wrong = self.cli(self.r1, 'cancel', rid, '--confirm', token + 'x')
        self.assertEqual(wrong.code, 1, wrong.both)
        self.assertEqual(wrong.json['error']['code'], 'token_mismatch')
        other_key = self.cli(self.ed, 'cancel', rid, '--confirm', token)
        self.assertEqual(other_key.code, 1, other_key.both)
        self.assertEqual(self.hub.cancels, [])
        done = self.cli(self.r1, 'cancel', rid, '--confirm', token, '--reason', 'stop')
        self.assertEqual(done.code, 0, done.both)
        self.assertEqual(done.json['status'], 'cancel_requested')
        self.assertEqual(len(self.hub.cancels), 1)
        reuse = self.cli(self.r1, 'cancel', rid, '--confirm', token)
        self.assertEqual(reuse.code, 1, reuse.both)
        self.assertEqual(reuse.json['error']['code'], 'token_mismatch')
        self.assertEqual(len(self.hub.cancels), 1)
        # the token is a capability, not the API key: the key still never appears (no_key ran in cli())

    def test_cancel_token_is_bound_to_its_request(self):
        one, two = self.make(), self.rid(self.submit(self.r1, title='second', body='b'))
        self.hub.link_running(one, task_id='task-1')
        self.hub.link_running(two, task_id='task-2')
        token = self.cli(self.r1, 'cancel', one).json['confirm_token']
        out = self.cli(self.r1, 'cancel', two, '--confirm', token)
        self.assertEqual(out.code, 1, out.both)
        self.assertEqual(self.hub.cancels, [])


class ResultTests(HubCase):
    def setUp(self):
        super().setUp()
        import test_inbox_results as fixtures  # module import only: its test classes stay uncollected
        from workbench.hub.inbox import results
        self.fx = fixtures
        self.backend = fixtures.FakeBackend()
        self.hub.api.results = results.Results(self.hub.inbox, port=self.backend)
        self.rid_ = self.rid(self.submit(self.r1, title='R', body='run'))
        self.hub.link_running(self.rid_, task_id=fixtures.TASK, view={'status': 'running', 'runs': [
            {'run_id': fixtures.RUN, 'status': 'running', 'exit_code': None}]})
        self.big = bytes((i * 7 + i // 251) % 256 for i in range(300_000))
        (self.hub.root_a / 'big.bin').write_bytes(self.big)
        (self.hub.root_a / 'out.txt').write_text('ARTIFACT-OK')

    def test_default_summary_equals_rest_and_full_flag_is_forwarded(self):
        out = self.cli(self.r1, 'result', self.rid_)
        self.assertEqual(out.code, 0, out.both)
        self.assertEqual(out.json, self.hub.rest(self.r1).get(f'/requests/{self.rid_}/result').json)
        full = self.cli(self.r1, 'result', self.rid_, '--full')
        self.assertEqual(full.code, 0, full.both)
        self.assertEqual(full.json, self.hub.rest(self.r1).get(f'/requests/{self.rid_}/result', full=True).json)
        self.assertNotEqual(out.json, full.json)

    def test_artifact_paging_through_the_cli_reassembles_the_file(self):
        offset, data, pages = 0, b'', 0
        while offset is not None:
            out = self.cli(self.r1, 'result', self.rid_, '--artifact-path', 'big.bin', '--offset', str(offset),
                           '--length', '131072')
            self.assertEqual(out.code, 0, out.both)
            page = out.json['artifact']
            self.assertEqual(page['offset'], offset)
            data += base64.b64decode(page['data_base64']) if 'data_base64' in page else page['text'].encode()
            offset = page['next_offset']
            pages += 1
            self.assertLess(pages, 10)
        self.assertEqual(data, self.big)
        self.assertEqual(pages, 3)

    def test_explicit_full_with_artifact_path_and_raw_run_page(self):
        out = self.cli(self.r1, 'result', self.rid_, '--full', '--artifact-path', 'out.txt')
        self.assertEqual(out.code, 0, out.both)
        self.assertIn('artifact', out.json)
        raw = self.cli(self.r1, 'result', self.rid_, '--run-id', self.fx.RUN, '--length', '64')
        self.assertEqual(raw.code, 0, raw.both)
        self.assertIn('raw', raw.json)

    def test_run_id_and_artifact_path_are_mutually_exclusive(self):
        out = self.cli(self.r1, 'result', self.rid_, '--run-id', self.fx.RUN, '--artifact-path', 'out.txt')
        self.assertEqual(out.code, 2, out.both)

    def test_bad_paging_values_are_clean_failures(self):
        for args in (['--artifact-path', 'big.bin', '--length', '0'], ['--artifact-path', 'big.bin', '--length', '131073'],
                     ['--artifact-path', 'big.bin', '--offset', '-1'], ['--artifact-path', 'big.bin', '--offset', 'x'],
                     ['--artifact-path', '../outside.txt'], ['--artifact-path', '/etc/passwd']):
            with self.subTest(args=args):
                out = self.cli(self.r1, 'result', self.rid_, *args)
                self.assertIn(out.code, (1, 2), out.both)
                self.assertNotIn('root:x:', out.both)

    def test_rest_ignores_run_id_without_full_so_the_cli_must_add_it(self):
        # evidence for judging the worker's "auto full=true" deviation
        reply = self.hub.rest(self.r1).get(f'/requests/{self.rid_}/result', run_id=self.fx.RUN)
        self.assertNotIn('raw', reply.json)


# ================================================================================================
# 6. Hostile / broken servers and key hygiene on every transport path
# ================================================================================================

class HostileServerTests(CliCase):
    def setUp(self):
        super().setUp()
        self.key = new_key()

    def go(self, respond, *args, timeout=60, env=None) -> tuple[Out, Fake]:
        server = Fake(self, respond)
        out = self.run_cli(*(args or ('projects',)), env={'WB_HUB_URL': server.url, 'WB_API_KEY': self.key,
                                                           **(env or {})}, timeout=timeout)
        return out, server

    def test_server_echoing_the_key_in_headers_or_body_never_reaches_output(self):
        def echo_body(req):
            return ok_json({'echo': req.auth, 'raw': req.auth.split()[-1] if req.auth else None,
                            'headers': dict(req.headers)})

        def echo_error(req):
            return ok_json({'error': {'code': 'internal_error', 'message': f'bad header {req.auth}'}}, 500)

        def echo_error_key_only(req):
            return ok_json({'error': {'code': 'forbidden', 'message': req.auth.split()[-1]}}, 403)

        def echo_header(req):
            return 200, {**JSON_HEADERS, 'X-Debug': req.auth, 'Set-Cookie': f'k={req.auth}'}, b'{"projects": []}'

        def echo_redirect(req):
            return 302, {'Location': f'/leak?k={req.auth.split()[-1]}'}, b''

        def echo_html(req):
            return 500, {'Content-Type': 'text/html'}, f'<html>Authorization: {req.auth}</html>'.encode()

        def echo_plain(req):
            return 200, {'Content-Type': 'text/plain'}, req.auth.encode()

        def echo_unicode_escaped(req):
            token = req.auth.split()[-1]
            return 200, JSON_HEADERS, json.dumps({'x': ''.join(f'\\u{ord(c):04x}' for c in token)}).encode()

        for respond in (echo_body, echo_error, echo_error_key_only, echo_header, echo_redirect, echo_html,
                        echo_plain):
            with self.subTest(respond=respond.__name__):
                out, _ = self.go(respond)
                self.no_key(out, self.key)
                self.assertNotIn('Traceback', out.err)

    def test_http_error_prints_server_json_on_stdout_and_one_line_on_stderr(self):
        out, _ = self.go(lambda r: ok_json({'error': {'code': 'forbidden', 'message': 'nope'}}, 403))
        self.assertEqual(out.code, 1)
        self.assertEqual(out.json, {'error': {'code': 'forbidden', 'message': 'nope'}})
        self.assertEqual(out.err.strip().splitlines(), ['wb_inbox: error 403 forbidden: nope'])

    def test_server_error_message_cannot_inject_extra_lines_or_escapes(self):
        out, _ = self.go(lambda r: ok_json({'error': {'code': 'x', 'message': 'line1\nline2\r\x1b[31mred'}}, 500))
        self.assertEqual(out.code, 1)
        self.assertEqual(len(out.err.strip().splitlines()), 1, repr(out.err))
        self.assertNotIn('\x1b', out.err)

    def test_odd_error_shapes_do_not_crash(self):
        for status, body in [(500, {'error': 'text'}), (500, {'error': None}), (500, {}), (500, []), (400, 'str'),
                             (404, {'error': {'code': 7, 'message': ['a']}}), (409, {'error': {'code': 'conflict'}}),
                             (500, {'error': {'message': 'no code'}}), (503, None), (200, [1, 2]), (200, 'x'),
                             (200, None), (200, 3.5)]:
            with self.subTest(status=status, body=body):
                out, _ = self.go(lambda r, s=status, b=body: ok_json(b, s))
                self.assertEqual(out.code, 0 if status < 300 else 1, out.both)
                self.no_key(out, self.key)

    def test_non_json_bodies_exit_4_for_2xx_and_never_success_or_leak(self):
        for status, headers, body in [(200, {'Content-Type': 'text/html'}, b'<html>proxy login</html>'),
                                      (200, JSON_HEADERS, b''), (200, JSON_HEADERS, b'{not json'),
                                      (200, JSON_HEADERS, b'\xff\xfe\xfd'), (204, {}, b''),
                                      (200, JSON_HEADERS, b'\xef\xbb\xbf{"projects": []}')]:
            with self.subTest(status=status, body=body[:20]):
                out, _ = self.go(lambda r, s=status, h=headers, b=body: (s, h, b))
                self.assertIn(out.code, (0, 4), out.both)  # BOM-prefixed JSON may be accepted; the rest is exit 4
                if body not in (b'\xef\xbb\xbf{"projects": []}',):
                    self.assertEqual(out.code, 4, out.both)
                self.no_key(out, self.key)

    def test_non_json_error_pages_are_failures_without_leak(self):
        for status in (400, 401, 403, 404, 500, 502, 503):
            with self.subTest(status=status):
                out, _ = self.go(lambda r, s=status: (s, {'Content-Type': 'text/html'},
                                                       b'<html><body>nginx ' + (r.auth or '').encode() + b'</body></html>'))
                self.assertIn(out.code, (1, 4), out.both)
                self.no_key(out, self.key)

    def test_redirects_are_never_followed_and_the_key_goes_nowhere_else(self):
        elsewhere = Fake(self, projects_ok)
        same_host_target = '/api/v1/projects?followed=1'
        for status in (301, 302, 303, 307, 308):
            for kind in ('other-host', 'other-port', 'same-host', 'relative', 'protocol-relative', 'https'):
                with self.subTest(status=status, kind=kind):
                    elsewhere.requests.clear()
                    target = {'other-host': f'http://localhost:{elsewhere.port}/api/v1/projects',
                              'other-port': f'{elsewhere.url}/api/v1/projects',
                              'same-host': None, 'relative': same_host_target,
                              'protocol-relative': f'//127.0.0.1:{elsewhere.port}/api/v1/projects',
                              'https': f'https://127.0.0.1:{elsewhere.port}/'}[kind]

                    def respond(req, status=status, target=target):
                        loc = target or f'http://127.0.0.1:{server_box[0].port}/api/v1/projects?followed=1'
                        return status, {'Location': loc}, b''

                    server_box = []
                    server = Fake(self, respond)
                    server_box.append(server)
                    out = self.run_cli('projects', env={'WB_HUB_URL': server.url, 'WB_API_KEY': self.key})
                    self.assertEqual(out.code, 4, out.both)
                    self.assertEqual(len(server.requests), 1, 'redirect must not be followed to the same host either')
                    self.assertEqual(elsewhere.requests, [], 'key leaked to a redirect target')
                    self.no_key(out, self.key)
                    self.assertEqual(out.out.strip().startswith('{"projects"'), False)

    def test_redirect_on_post_is_not_replayed(self):
        server = Fake(self, lambda r: (307, {'Location': '/api/v1/requests'}, b''))
        out = self.run_cli('submit', '--project', 'p', '--title', 't', '--body', 'b',
                           env={'WB_HUB_URL': server.url, 'WB_API_KEY': self.key})
        self.assertEqual(out.code, 4, out.both)
        self.assertEqual(len(server.requests), 1)

    def test_connection_failures_exit_4_without_leak(self):
        closed = socket.socket()
        closed.bind(('127.0.0.1', 0))
        port = closed.getsockname()[1]
        closed.close()  # nothing listens there now
        cases = {
            'refused': f'http://127.0.0.1:{port}',
            'dns': 'http://no-such-host.invalid',
            'tls-to-plain': None,
        }
        for name, url in cases.items():
            with self.subTest(name=name):
                if url is None:
                    plain = Fake(self, projects_ok)
                    url = f'https://127.0.0.1:{plain.port}'
                out = self.run_cli('projects', env={'WB_HUB_URL': url, 'WB_API_KEY': self.key})
                self.assertEqual(out.code, 4, out.both)
                self.no_key(out, self.key)
                self.assertEqual(out.out.strip(), '')
                self.assertEqual(len(out.err.strip().splitlines()), 1, out.err)

    def test_broken_http_exits_4(self):
        for action in ('close', 'garbage', 'partial'):
            with self.subTest(action=action):
                out, _ = self.go(lambda r, a=action: a)
                self.assertEqual(out.code, 4, out.both)
                self.no_key(out, self.key)

    def test_huge_responses_finish_cleanly(self):
        big_ok = json.dumps({'projects': [{'id': 'x' * 100, 'name': 'n' * 100}] * 60_000}).encode()
        out, _ = self.go(lambda r: (200, JSON_HEADERS, big_ok), timeout=120)
        self.assertIn(out.code, (0, 4), out.both[:200])
        if out.code == 0:
            self.assertEqual(len(out.json['projects']), 60_000)
        out, _ = self.go(lambda r: (500, {'Content-Type': 'text/html'}, b'x' * 20_000_000 + (r.auth or '').encode()),
                         timeout=120)
        self.assertIn(out.code, (1, 4), out.both[:200])
        self.no_key(out, self.key)
        out, _ = self.go(lambda r: ok_json({'error': {'code': 'internal_error', 'message': 'm' * 5_000_000}}, 500),
                         timeout=120)
        self.assertEqual(out.code, 1)

    def test_hanging_server_times_out_with_exit_4_around_30_seconds(self):
        out, server = self.go(lambda r: 'hang', timeout=90)
        self.assertEqual(out.code, 4, out.both)
        self.assertGreater(out.seconds, 20)
        self.assertLess(out.seconds, 45)
        self.no_key(out, self.key)

    def test_request_headers_sent_are_exactly_bearer_and_json(self):
        server = Fake(self, lambda r: ok_json({'request_id': 'r', 'created': True}, 201))
        out = self.run_cli('submit', '--project', 'p', '--title', 't', '--body', 'b',
                           env={'WB_HUB_URL': server.url, 'WB_API_KEY': self.key})
        self.assertEqual(out.code, 0, out.both)
        req = server.requests[0]
        self.assertEqual((req.method, req.path), ('POST', '/api/v1/requests'))
        self.assertEqual(req.auth, f'Bearer {self.key}')
        self.assertTrue(req.headers['content-type'].startswith('application/json'))
        self.assertNotIn(self.key, req.body.decode())  # never in the body or URL
        self.assertNotIn(self.key, req.path)
        self.assertEqual(set(req.json), {'project_id', 'title', 'body', 'idempotency_key'})
        self.assertEqual(req.json['idempotency_key'],
                         'wbi-' + hashlib.sha256(b'p\0t\0b').hexdigest()[:32])
        self.assertIn(out.json.get('idempotency_key'), (req.json['idempotency_key'], None))

    def test_key_is_not_in_the_url_or_any_non_authorization_header_for_every_command(self):
        server = Fake(self, lambda r: ok_json({'ok': True}))
        commands = [['projects'], ['list'], ['get', 'r1'], ['comment', 'r1', '--body', 'x'],
                    ['update', 'r1', '--base-version', '1', '--title', 't'],
                    ['set-state', 'r1', '--base-version', '1', '--to', 'NEW'], ['cancel', 'r1'],
                    ['cancel', 'r1', '--confirm', 'tok'], ['result', 'r1'], ['result', 'r1', '--full']]
        for command in commands:
            with self.subTest(command=command):
                server.requests.clear()
                out = self.run_cli(*command, env={'WB_HUB_URL': server.url, 'WB_API_KEY': self.key})
                self.assertEqual(out.code, 0, out.both)
                self.no_key(out, self.key)
                for req in server.requests:
                    self.assertEqual(req.auth, f'Bearer {self.key}')
                    blob = req.path + req.body.decode() + ''.join(v for k, v in req.headers.items()
                                                                    if k != 'authorization')
                    self.assertNotIn(self.key[4:], blob)

    def test_https_scheme_is_used_for_https_urls_not_downgraded(self):
        plain = Fake(self, projects_ok)
        out = self.run_cli('projects', env={'WB_HUB_URL': f'https://127.0.0.1:{plain.port}', 'WB_API_KEY': self.key})
        self.assertEqual(out.code, 4, out.both)
        self.assertEqual(plain.requests, [])  # no cleartext request reached a server we asked to speak TLS to


# ================================================================================================
# 7. Usage errors, help, no side effects
# ================================================================================================

class UsageAndHygieneTests(CliCase):
    def setUp(self):
        super().setUp()
        self.key = new_key()
        self.env = {'WB_HUB_URL': 'http://127.0.0.1:9', 'WB_API_KEY': self.key}

    def test_usage_errors_exit_2_before_config_lookup_and_never_print_the_key(self):
        usages = [[], ['bogus'], ['submit'], ['submit', '--project', 'p'], ['list', '--limit', 'x'], ['get'],
                  ['set-state', 'r', '--base-version', '1', '--to', 'READY'], ['result', 'r', '--offset', 'x'],
                  ['projects', '--no-such-flag'], ['cancel'], ['--no-such']]
        for args in usages:
            for env in (self.env, {}):
                with self.subTest(args=args, has_env=bool(env)):
                    out = self.run_cli(*args, env=env)
                    self.assertEqual(out.code, 2, out.both)
                    self.no_key(out, self.key)

    def test_help_works_without_configuration_and_hides_the_key(self):
        for args in (['--help'], ['-h'], ['submit', '--help'], ['list', '-h'], ['result', '--help'], ['cancel', '--help'],
                     ['update', '--help'], ['set-state', '--help'], ['comment', '--help'], ['config', '--help'],
                     ['projects', '--help'], ['get', '--help']):
            for env in (self.env, {}):
                with self.subTest(args=args, has_env=bool(env)):
                    out = self.run_cli(*args, env=env)
                    self.assertEqual(out.code, 0, out.both)
                    self.no_key(out, self.key)
                    self.assertIn('usage', out.out.lower())
        top = self.run_cli('--help').out
        for command in ('projects', 'submit', 'list', 'get', 'comment', 'update', 'set-state', 'cancel', 'result',
                        'config'):
            self.assertIn(command, top)

    def test_config_never_prints_the_key_in_any_source_combination(self):
        self.put(self.dotenv, f'WB_API_KEY={self.key}\n')
        for env in ({}, {'WB_HUB_URL': 'http://127.0.0.1:9'}, {'WB_API_KEY': self.key}, self.env):
            with self.subTest(env=sorted(env)):
                out = self.run_cli('config', env=env)
                self.no_key(out, self.key)
                json.loads(out.out) if out.out.strip() else None

    def test_no_files_are_created_or_modified_by_any_outcome(self):
        server = Fake(self, lambda r: ok_json({'error': {'code': 'internal_error', 'message': 'x'}}, 500))
        self.put(self.dotenv, f'WB_API_KEY={self.key}\n')
        before = self.tree()
        runs = [('projects', {'WB_HUB_URL': server.url}), ('config', {'WB_HUB_URL': server.url}),
                ('projects', {'WB_HUB_URL': 'http://127.0.0.1:9'}), ('bogus', {}), ('projects', {}),
                ('projects', {'WB_HUB_URL': 'ftp://x'})]
        for command, env in runs:
            out = self.run_cli(command, env=env)
            self.no_key(out, self.key)
        after = self.tree()
        self.assertEqual(before, after)
        leftovers = [p for p in (self.base.rglob('*')) if p.name == '__pycache__' or p.suffix in ('.log', '.pyc')]
        self.assertEqual(leftovers, [])

    def test_runs_isolated_from_planted_stdlib_shadow_modules(self):
        marker = self.base / 'imported-marker'
        for name in ('json', 'ssl', 'urllib', 'argparse', 'hashlib', 'sitecustomize', 'usercustomize'):
            (self.skill / f'{name}.py').write_text(f'open({str(marker)!r}, "w").write("x")\n')
            (self.cwd / f'{name}.py').write_text(f'open({str(marker)!r}, "w").write("x")\n')
        out = self.run_cli('config', env=self.env)
        self.assertEqual(out.code, 0, out.both)
        self.assertFalse(marker.exists(), 'a planted module was imported: not isolated')

    def test_runs_with_python3_from_path_and_without_pip_packages(self):
        out = self.run_cli('--help', python='/usr/bin/python3')
        self.assertEqual(out.code, 0, out.both)


# ================================================================================================
# 8. S5 static
# ================================================================================================

class StaticTests(unittest.TestCase):
    FILES = ('README.md', 'wb-inbox/SKILL.md', 'wb-inbox/wb_inbox.py', 'wb-inbox/.env.example')

    def read(self, name: str) -> str:
        return (REPO / 'skills' / name).read_text(encoding='utf-8')

    def test_files_and_readme_first_line(self):
        for name in self.FILES:
            self.assertTrue((REPO / 'skills' / name).is_file(), name)
        self.assertEqual(self.read('README.md').splitlines()[0],
                         'This folder holds Workbench user skills for agents outside Workbench; it is not '
                         '`.agents/skills/` (Workbench development skills).')

    def test_readme_contract_items(self):
        text = self.read('README.md')
        self.assertIn('curl -H "Authorization: Bearer $WB_API_KEY" "$WB_HUB_URL/api/v1/projects"', text)
        self.assertTrue(any(line.strip() == '<!-- wb:skill-repo -->' for line in text.splitlines()), 'own line')
        self.assertIn('SSL_CERT_FILE', text)
        self.assertIn('chmod 600', text)
        self.assertIn('.env', text)
        self.assertIn('/skill/', text)

    def test_placeholder_is_one_exact_string_in_all_three_docs(self):
        for name in ('README.md', 'wb-inbox/SKILL.md', 'wb-inbox/.env.example'):
            text = self.read(name)
            self.assertIn('http://YOUR-HUB-HOST:8780', text, name)
            for hostlike in re.findall(r'https?://[A-Za-z0-9._-]*(?:YOUR|your|example|EXAMPLE)[A-Za-z0-9._:-]*', text):
                self.assertEqual(hostlike, 'http://YOUR-HUB-HOST:8780', f'{name}: {hostlike}')

    def test_no_new_environment_variables(self):
        for name in self.FILES:
            variables = set(re.findall(r'\bWB_[A-Z0-9_]+\b', self.read(name)))
            self.assertLessEqual(variables, {'WB_HUB_URL', 'WB_API_KEY'}, f'{name}: {variables}')

    def test_no_real_key_and_english_only(self):
        for name in self.FILES:
            text = self.read(name)
            self.assertNotRegex(text, r'wbk_(?!replace_me|\.\.\.|YOUR)[A-Za-z0-9_-]{16,}', name)
            self.assertNotRegex(text, r'[가-힯]', f'{name} must be English')

    def test_skill_md_frontmatter_and_commands(self):
        text = self.read('wb-inbox/SKILL.md')
        match = re.match(r'---\n(.*?)\n---\n', text, re.S)
        self.assertIsNotNone(match)
        front = match.group(1)
        self.assertRegex(front, r'(?m)^name:\s*wb-inbox\s*$')
        self.assertRegex(front, r'(?m)^description:\s*\S')
        for command in ('projects', 'submit', 'list', 'get', 'comment', 'update', 'set-state', 'cancel', 'result'):
            self.assertIn(command, text)
        self.assertIn('python3 -I', text)

    def test_env_example_has_both_variables_only(self):
        text = self.read('wb-inbox/.env.example')
        assigned = re.findall(r'(?m)^([A-Z_]+)=', text)
        self.assertEqual(sorted(assigned), ['WB_API_KEY', 'WB_HUB_URL'])

    def test_gitignore_covers_dotenv_and_example_is_not_ignored(self):
        self.assertIn('skills/**/.env', (REPO / '.gitignore').read_text().splitlines())
        for path, ignored in (('skills/wb-inbox/.env', True), ('skills/wb-inbox/.env.example', False)):
            done = subprocess.run(['git', 'check-ignore', '-q', path], cwd=REPO)
            self.assertEqual(done.returncode == 0, ignored, path)
        self.assertFalse((SKILL / '.env').exists())

    def test_script_imports_only_the_standard_library(self):
        tree = ast.parse(SCRIPT.read_text(encoding='utf-8'))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {alias.name.split('.')[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, 'relative import in a single-file skill')
                modules.add((node.module or '').split('.')[0])
        self.assertTrue(modules)
        self.assertLessEqual(modules, set(sys.stdlib_module_names), modules - set(sys.stdlib_module_names))
        self.assertFalse(modules & {'requests', 'httpx', 'aiohttp', 'workbench', 'certifi', 'urllib3'})
        source = SCRIPT.read_text(encoding='utf-8')
        for dynamic in ('__import__(', 'importlib', 'eval(', 'exec(', 'os.system', 'subprocess', 'pickle'):
            self.assertNotIn(dynamic, source)
        self.assertIn('urllib', modules | {'http'})


if __name__ == '__main__':
    unittest.main()
