"""WBX-B2 ``skills/wb-inbox/wb_inbox.py`` run as a subprocess against a real loopback Hub (seam S4).

Each test copies the script into a temp "skill folder" (so a ``.env`` can sit next to it without touching the repo)
and runs ``python -I wb_inbox.py ...`` with a fully controlled environment and a temp ``HOME``, so the developer's
real ``~/.config/omp-workbench/client.env`` is never read. Expected values: the web-board-ux SPEC "`wb-inbox` skill
과 `wb_inbox.py`" table (commands, output, exit codes 0-4, per-variable lookup, ``.env`` syntax and mode warning, URL
rules, no redirects, content-derived idempotency key, key hygiene) and the "외부 요청 API" endpoint table.
Run with ``discover -s tests/hub -p 'test_wb_inbox*.py'``.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from support.rest_hub import RestHub  # noqa: E402

SKILL = REPO / 'skills' / 'wb-inbox'
SCRIPT = SKILL / 'wb_inbox.py'
PROCESS_ENV = 'process environment'


class Result:
    def __init__(self, completed: subprocess.CompletedProcess):
        self.code = completed.returncode
        self.out = completed.stdout
        self.err = completed.stderr

    @property
    def json(self):
        return json.loads(self.out)

    @property
    def both(self) -> str:
        return self.out + '\n' + self.err


class CliCase(unittest.TestCase):
    """Temp skill folder + temp HOME; ``run`` executes the CLI with only the env the test gives."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='wb-cli-')).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.skill = self.base / 'skill'
        self.skill.mkdir()
        shutil.copy(SCRIPT, self.skill / 'wb_inbox.py')
        self.home = self.base / 'home'
        self.home.mkdir()
        self.xdg = self.base / 'xdg'
        self.xdg.mkdir()

    def write_env(self, path: Path, text: str, mode: int = 0o600) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.write_text(text, encoding='utf-8')
        os.chmod(path, mode)
        return path

    @property
    def client_env(self) -> Path:
        return self.xdg / 'omp-workbench' / 'client.env'

    def run_cli(self, *args: str, env: dict | None = None, xdg: bool = True, stdin: str | None = None,
                timeout: float = 60) -> Result:
        environment = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        if xdg:
            environment['XDG_CONFIG_HOME'] = str(self.xdg)
        environment.update(env or {})
        completed = subprocess.run([sys.executable, '-I', str(self.skill / 'wb_inbox.py'), *args], env=environment,
                                   input=stdin, capture_output=True, text=True, encoding='utf-8', timeout=timeout,
                                   cwd=str(self.base))
        return Result(completed)


class ConfigLookupTests(CliCase):
    """WBX-AC-14: per variable, process env -> skill folder ``.env`` -> ``$XDG_CONFIG_HOME/omp-workbench/client.env``."""

    def config(self, **kwargs) -> Result:
        return self.run_cli('config', **kwargs)

    def test_each_variable_comes_from_the_first_place_that_has_it(self):
        self.write_env(self.skill / '.env', 'WB_HUB_URL=http://skill.example:2\nWB_API_KEY=skill-key-value\n')
        self.write_env(self.client_env, 'WB_API_KEY=cfg-key-value\nWB_HUB_URL=http://cfg.example:3\n')
        result = self.config(env={'WB_HUB_URL': 'http://env.example:1'})
        self.assertEqual(result.code, 0, result.err)
        report = result.json
        self.assertEqual(report['WB_HUB_URL'], {'status': 'set', 'source': PROCESS_ENV, 'value': 'http://env.example:1'})
        self.assertEqual(report['WB_API_KEY'], {'status': 'set', 'source': str(self.skill / '.env')})

    def test_falls_back_to_client_env_when_the_skill_folder_lacks_the_variable(self):
        self.write_env(self.skill / '.env', 'WB_HUB_URL=http://skill.example:2\n')
        self.write_env(self.client_env, 'WB_API_KEY=cfg-key-value\nWB_HUB_URL=http://cfg.example:3\n')
        report = self.config().json
        self.assertEqual(report['WB_HUB_URL']['source'], str(self.skill / '.env'))
        self.assertEqual(report['WB_HUB_URL']['value'], 'http://skill.example:2')
        self.assertEqual(report['WB_API_KEY'], {'status': 'set', 'source': str(self.client_env)})

    def test_a_relative_xdg_config_home_is_ignored_for_home_config(self):
        self.write_env(self.home / '.config' / 'omp-workbench' / 'client.env',
                       'WB_HUB_URL=http://home.example:4\nWB_API_KEY=home-key-value\n')
        self.write_env(self.base / 'rel' / 'omp-workbench' / 'client.env', 'WB_API_KEY=wrong\nWB_HUB_URL=http://wrong:1\n')
        report = self.config(env={'XDG_CONFIG_HOME': 'rel'}).json
        self.assertEqual(report['WB_HUB_URL']['source'], str(self.home / '.config' / 'omp-workbench' / 'client.env'))

    def test_without_xdg_config_home_the_home_config_is_used(self):
        self.write_env(self.home / '.config' / 'omp-workbench' / 'client.env',
                       'WB_HUB_URL=http://home.example:4\nWB_API_KEY=home-key-value\n')
        report = self.config(xdg=False).json
        self.assertEqual(report['WB_API_KEY']['source'], str(self.home / '.config' / 'omp-workbench' / 'client.env'))

    def test_env_file_syntax(self):
        self.write_env(self.skill / '.env', '\n'.join([
            '# a comment', '', '   ', 'export WB_HUB_URL="http://quoted.example:5"',
            "WB_API_KEY='single-quoted-key'", 'NOT A LINE', 'OTHER=1', '']))
        report = self.config().json
        self.assertEqual(report['WB_HUB_URL']['value'], 'http://quoted.example:5')
        self.assertEqual(report['WB_API_KEY']['status'], 'set')
        self.assertNotIn('single-quoted-key', json.dumps(report))

    def test_process_environment_alone_does_not_read_env_files(self):
        self.write_env(self.skill / '.env', 'WB_HUB_URL=http://skill.example:2\nWB_API_KEY=skill-key-value\n', 0o644)
        result = self.config(env={'WB_HUB_URL': 'http://env.example:1', 'WB_API_KEY': 'env-key-value'})
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.err, '')
        self.assertEqual(result.json['WB_API_KEY'], {'status': 'set', 'source': PROCESS_ENV})

    def test_config_never_prints_the_key_value(self):
        result = self.config(env={'WB_HUB_URL': 'http://env.example:1', 'WB_API_KEY': 'env-key-value'})
        self.assertNotIn('env-key-value', result.both)


class MissingValueTests(CliCase):
    """A missing value is exit 3 naming the variable and every place looked in; the key is never in the output."""

    def test_missing_key_is_exit_3_with_variable_and_locations(self):
        result = self.run_cli('projects', env={'WB_HUB_URL': 'http://127.0.0.1:9'})
        self.assertEqual(result.code, 3, result.both)
        self.assertEqual(result.out, '')
        self.assertEqual(result.err.strip().splitlines(), [
            f'wb_inbox: WB_API_KEY is not set; looked in: process environment, {self.skill / ".env"}, {self.client_env}'])

    def test_both_missing_names_both_variables(self):
        result = self.run_cli('projects')
        self.assertEqual(result.code, 3, result.both)
        lines = result.err.strip().splitlines()
        self.assertEqual([line.split(';')[0] for line in lines],
                         ['wb_inbox: WB_HUB_URL is not set', 'wb_inbox: WB_API_KEY is not set'])
        for line in lines:
            self.assertTrue(line.endswith(f'looked in: process environment, {self.skill / ".env"}, {self.client_env}'))

    def test_an_empty_value_counts_as_missing_and_lookup_continues(self):
        self.write_env(self.skill / '.env', 'WB_API_KEY=\n')
        self.write_env(self.client_env, 'WB_API_KEY=cfg-key-value\n')
        result = self.run_cli('config', env={'WB_HUB_URL': 'http://127.0.0.1:9', 'WB_API_KEY': ''})
        self.assertEqual(result.json['WB_API_KEY'], {'status': 'set', 'source': str(self.client_env)})

    def test_config_reports_missing_and_exits_3(self):
        result = self.run_cli('config', env={'WB_HUB_URL': 'http://127.0.0.1:9'})
        self.assertEqual(result.code, 3, result.both)
        self.assertEqual(result.json['WB_API_KEY'], {'status': 'missing'})
        self.assertIn('WB_API_KEY is not set', result.err)


class EnvFileModeTests(CliCase):
    """A ``.env`` read with any ``0o077`` bit set warns on stderr (once per file) and still works."""

    BODY = 'WB_HUB_URL=http://file.example:6\nWB_API_KEY=file-key-value\n'

    def test_wide_modes_warn_with_the_exact_sentence(self):
        for mode in (0o644, 0o640, 0o604, 0o660, 0o666, 0o700 | 0o010):
            with self.subTest(mode=oct(mode)):
                path = self.write_env(self.skill / '.env', self.BODY, mode)
                result = self.run_cli('config')
                self.assertEqual(result.code, 0, result.err)
                self.assertEqual(result.err.splitlines(), [
                    f'wb_inbox: warning: {path} is accessible by other users (mode {mode & 0o777:04o}); '
                    f'run: chmod 600 {path}'])
                self.assertEqual(result.json['WB_API_KEY']['source'], str(path))

    def test_owner_only_modes_do_not_warn(self):
        for mode in (0o600, 0o400, 0o700):
            with self.subTest(mode=oct(mode)):
                self.write_env(self.skill / '.env', self.BODY, mode)
                result = self.run_cli('config')
                self.assertEqual((result.code, result.err), (0, ''))

    def test_each_file_that_was_read_warns_once(self):
        first = self.write_env(self.skill / '.env', 'WB_HUB_URL=http://file.example:6\n', 0o644)
        second = self.write_env(self.client_env, 'WB_API_KEY=cfg-key-value\nWB_HUB_URL=http://x:1\n', 0o664)
        result = self.run_cli('config')
        self.assertEqual(result.code, 0, result.err)
        warned = [line.split()[2] for line in result.err.splitlines()]
        self.assertEqual(warned, [str(first), str(second)])

    def test_a_file_that_is_not_read_does_not_warn(self):
        self.write_env(self.client_env, self.BODY, 0o644)
        result = self.run_cli('config', env={'WB_HUB_URL': 'http://e:1', 'WB_API_KEY': 'env-key-value'})
        self.assertEqual((result.code, result.err), (0, ''))

    def test_the_warning_never_contains_the_key(self):
        self.write_env(self.skill / '.env', self.BODY, 0o644)
        self.assertNotIn('file-key-value', self.run_cli('config').both)


class HubUrlTests(CliCase):
    """``WB_HUB_URL``: http/https, host required, optional port, empty or ``/`` path, no userinfo/query/fragment."""

    def test_malformed_urls_are_exit_3(self):
        for url in ('ftp://hub.example', 'hub.example:8780', 'http://', 'http:///x', 'http://user:secretpw@hub.example',
                    'http://user@hub.example', 'http://hub.example/api', 'http://hub.example/?q=1',
                    'http://hub.example?q=1', 'http://hub.example/#frag', 'http://hub.example#', 'http://hub.example?',
                    'http://hub.example:99999', 'http://hub.example:abc', 'http://hub.example:', 'http://hub .example',
                    'http://hub.example\n', 'https:hub.example', '//hub.example', 'http://hub.example//'):
            for command in (('config',), ('projects',)):
                with self.subTest(url=url, command=command[0]):
                    result = self.run_cli(*command, env={'WB_HUB_URL': url, 'WB_API_KEY': 'k-value'})
                    self.assertEqual(result.code, 3, result.both)
                    self.assertTrue(result.err.startswith('wb_inbox: WB_HUB_URL'), result.err)
                    self.assertNotIn('secretpw', result.both)
                    self.assertNotIn('k-value', result.both)

    def test_well_formed_urls_are_accepted(self):
        for url in ('http://hub.example', 'http://hub.example/', 'https://hub.example:8443', 'http://127.0.0.1:8780/',
                    'http://[::1]:8780', 'HTTP://Hub.Example:1'):
            with self.subTest(url=url):
                result = self.run_cli('config', env={'WB_HUB_URL': url, 'WB_API_KEY': 'k-value'})
                self.assertEqual(result.code, 0, result.both)


class FakeServer:
    """A scripted loopback HTTP server: ``respond(handler)`` answers every request; ``seen`` records what came."""

    def __init__(self, case: unittest.TestCase, respond):
        self.seen: list[tuple] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_any(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length else b''
                outer.seen.append((self.command, self.path, self.headers.get('Authorization'), body))
                respond(self)

            do_GET = do_POST = do_PATCH = handle_any

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        case.addCleanup(self.close)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


def reply(handler, status: int, body: bytes, headers: dict | None = None) -> None:
    handler.send_response(status)
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class HubCase(CliCase):
    """The CLI against a real loopback Hub (``support/rest_hub.py``); keys are issued by the Hub's own key store."""

    def setUp(self):
        super().setUp()
        self.hub = RestHub(self)
        self.requester = self.hub.key('requester')
        self.editor = self.hub.key('editor', grade='editor')
        self.url = f'http://127.0.0.1:{self.hub.port}'

    def cli(self, key: str, *args: str, **kwargs) -> Result:
        env = {'WB_HUB_URL': self.url, 'WB_API_KEY': key, **kwargs.pop('env', {})}
        return self.run_cli(*args, env=env, **kwargs)

    def submit(self, key: str | None = None, project: str | None = None, title: str = 'T one',
               body: str = 'body one', *extra: str) -> Result:
        result = self.cli(key or self.requester, 'submit', '--project', project or self.hub.a, '--title', title,
                          '--body', body, *extra)
        self.assertIn(result.code, (0,), result.both)
        return result


class ProjectsTests(HubCase):
    def test_projects_prints_the_server_json_for_the_key(self):
        result = self.cli(self.requester, 'projects')
        self.assertEqual(result.code, 0, result.both)
        self.assertEqual(result.err, '')
        self.assertEqual(result.out, json.dumps(result.json, indent=2, ensure_ascii=False) + '\n')
        projects = {entry['name']: entry for entry in result.json['projects']}
        self.assertEqual(set(projects), {'alpha', 'beta'})
        self.assertEqual(projects['alpha']['id'] if 'id' in projects['alpha'] else projects['alpha']['project_id'],
                         self.hub.a)

    def test_query_filters_by_name(self):
        result = self.cli(self.requester, 'projects', '--query', 'alp')
        self.assertEqual(result.code, 0, result.both)
        self.assertEqual([entry['name'] for entry in result.json['projects']], ['alpha'])

    def test_a_scoped_key_sees_only_its_project(self):
        scoped = self.hub.key('scoped', scope={'projects': [self.hub.b]})
        result = self.cli(scoped, 'projects')
        self.assertEqual([entry['name'] for entry in result.json['projects']], ['beta'])


class SubmitTests(HubCase):
    """``submit``: POST /requests; default idempotency key ``wbi-`` + sha256(project NUL title NUL body)[:32] (D25)."""

    def titles(self, root=None) -> list[str]:
        return [row[0] for row in self.hub.inbox_rows('SELECT title FROM requests ORDER BY seq', root=root)]

    def test_submit_creates_a_request_and_prints_the_derived_key(self):
        result = self.cli(self.requester, 'submit', '--project', self.hub.a, '--title', 'Add \uc800\uc7a5 button',
                          '--body', '\ubcf8\ubb38 text')
        self.assertEqual(result.code, 0, result.both)
        expected = 'wbi-' + hashlib.sha256(f'{self.hub.a}\0Add \uc800\uc7a5 button\0\ubcf8\ubb38 text'.encode()).hexdigest()[:32]
        report = result.json
        self.assertEqual(report['idempotency_key'], expected)
        self.assertEqual((report['project_id'], report['state'], report['created']), (self.hub.a, 'NEW', True))
        self.assertEqual(self.titles(), ['Add \uc800\uc7a5 button'])

    def test_the_same_content_again_is_the_same_request(self):
        first = self.submit()
        second = self.submit()
        self.assertEqual(second.json['request_id'], first.json['request_id'])
        self.assertEqual(second.json['idempotency_key'], first.json['idempotency_key'])
        self.assertFalse(second.json['created'])
        self.assertEqual(len(self.titles()), 1)

    def test_different_content_or_project_makes_a_different_request(self):
        first = self.submit()
        other_body = self.submit(body='body two')
        other_project = self.submit(project=self.hub.b)
        keys = {first.json['idempotency_key'], other_body.json['idempotency_key'], other_project.json['idempotency_key']}
        self.assertEqual(len(keys), 3)
        self.assertEqual(len(self.titles()), 2)
        self.assertEqual(len(self.titles(self.hub.root_b)), 1)

    def test_new_forces_a_new_request_with_a_random_key(self):
        first = self.submit(None, None, 'T one', 'body one', '--new')
        second = self.submit(None, None, 'T one', 'body one', '--new')
        self.assertNotEqual(first.json['request_id'], second.json['request_id'])
        for report in (first.json, second.json):
            self.assertRegex(report['idempotency_key'], r'^wbi-new-[0-9a-f]{32}$')
        self.assertEqual(len(self.titles()), 2)

    def test_new_with_an_explicit_key_is_a_usage_error(self):
        result = self.cli(self.requester, 'submit', '--project', self.hub.a, '--title', 't', '--body', 'b', '--new',
                          '--idempotency-key', 'k1')
        self.assertEqual(result.code, 2, result.both)
        self.assertEqual(result.out, '')
        self.assertIn('--new', result.err)
        self.assertEqual(self.titles(), [])

    def test_an_explicit_key_is_used_and_reuse_with_other_content_conflicts(self):
        first = self.submit(None, None, 'T one', 'body one', '--idempotency-key', 'my-key-1')
        self.assertEqual(first.json['idempotency_key'], 'my-key-1')
        conflict = self.cli(self.requester, 'submit', '--project', self.hub.a, '--title', 'T one', '--body', 'other',
                            '--idempotency-key', 'my-key-1')
        self.assertEqual(conflict.code, 1, conflict.both)
        self.assertEqual(conflict.json['error']['code'], 'idempotency_conflict')
        self.assertTrue(conflict.err.startswith('wb_inbox: error 409 idempotency_conflict: '), conflict.err)
        self.assertEqual(len(conflict.err.splitlines()), 1)

    def test_body_file_and_stdin(self):
        path = self.base / 'body.txt'
        path.write_text('from a file \ud55c\uae00', encoding='utf-8')
        by_file = self.cli(self.requester, 'submit', '--project', self.hub.a, '--title', 'f', '--body-file', str(path))
        by_stdin = self.cli(self.requester, 'submit', '--project', self.hub.a, '--title', 's', '--body-file', '-',
                            stdin='from stdin \ud55c\uae00')
        self.assertEqual((by_file.code, by_stdin.code), (0, 0), by_file.both + by_stdin.both)
        rows = self.hub.inbox_rows('SELECT title, body FROM requests ORDER BY seq')
        self.assertEqual(rows, [('f', 'from a file \ud55c\uae00'), ('s', 'from stdin \ud55c\uae00')])

    def test_usage_errors_are_exit_2_and_send_nothing(self):
        path = self.base / 'b.txt'
        path.write_text('x')
        cases = (('submit', '--project', self.hub.a, '--title', 't'),
                 ('submit', '--project', self.hub.a, '--title', 't', '--body', 'b', '--body-file', str(path)),
                 ('submit', '--title', 't', '--body', 'b'), ('submit', '--project', self.hub.a, '--body', 'b'),
                 ('submit', '--project', self.hub.a, '--title', 't', '--body-file', str(self.base / 'missing.txt')))
        for args in cases:
            with self.subTest(args=args[1:]):
                result = self.cli(self.requester, *args)
                self.assertEqual(result.code, 2, result.both)
                self.assertEqual(result.out, '')
        self.assertEqual(self.titles(), [])

    def test_an_unknown_project_is_a_server_refusal(self):
        result = self.cli(self.requester, 'submit', '--project', '00000000-0000-4000-8000-000000000000', '--title',
                          't', '--body', 'b')
        self.assertEqual(result.code, 1, result.both)
        self.assertEqual(result.json['error']['code'], 'not_found')


class ListAndGetTests(HubCase):
    def setUp(self):
        super().setUp()
        self.ids = [self.submit(None, project, title, f'body of {title}').json['request_id']
                    for project, title in ((self.hub.a, 'first apple'), (self.hub.a, 'second banana'),
                                           (self.hub.b, 'third cherry'), (self.hub.a, 'fourth \ud55c\uae00'))]

    def titles(self, *args: str) -> list[str]:
        result = self.cli(self.requester, 'list', *args)
        self.assertEqual(result.code, 0, result.both)
        return sorted(entry['title'] for entry in result.json['requests'])

    def test_list_shows_every_request_and_ends_with_a_null_cursor(self):
        result = self.cli(self.requester, 'list')
        self.assertEqual(result.code, 0, result.both)
        self.assertEqual(len(result.json['requests']), 4)
        self.assertIsNone(result.json['next_cursor'])

    def test_list_filters(self):
        self.assertEqual(self.titles('--project', self.hub.b), ['third cherry'])
        self.assertEqual(self.titles('--query', 'banana'), ['second banana'])
        self.assertEqual(self.titles('--state', 'NEW', '--project', self.hub.b), ['third cherry'])
        self.assertEqual(self.titles('--state', 'READY'), [])
        self.assertEqual(self.titles('--state', 'READY', '--state', 'NEW', '--project', self.hub.b), ['third cherry'])

    def test_include_closed_shows_cancelled_requests(self):
        reply = self.hub.rest(self.requester).post(f'/requests/{self.ids[1]}/cancel', {})
        self.assertEqual(reply.status, 200, reply.body)
        self.assertNotIn('second banana', self.titles())
        self.assertIn('second banana', self.titles('--include-closed'))

    def test_limit_and_cursor_page_through_the_list(self):
        seen = []
        cursor = None
        for _ in range(5):
            args = ['--limit', '3'] + (['--cursor', cursor] if cursor else [])
            result = self.cli(self.requester, 'list', *args)
            self.assertEqual(result.code, 0, result.both)
            seen += [entry['request_id'] for entry in result.json['requests']]
            cursor = result.json['next_cursor']
            if cursor is None:
                break
        self.assertEqual(sorted(seen), sorted(self.ids))

    def test_bad_list_arguments(self):
        self.assertEqual(self.cli(self.requester, 'list', '--limit', 'abc').code, 2)
        refused = self.cli(self.requester, 'list', '--limit', '0')
        self.assertEqual(refused.code, 1, refused.both)
        self.assertEqual(refused.json['error']['code'], 'invalid_input')

    def test_get_prints_the_full_request(self):
        result = self.cli(self.requester, 'get', self.ids[3])
        self.assertEqual(result.code, 0, result.both)
        request = result.json['request']
        self.assertEqual((request['request_id'], request['title'], request['body']),
                         (self.ids[3], 'fourth \ud55c\uae00', 'body of fourth \ud55c\uae00'))
        self.assertIn('\ud55c\uae00', result.out)

    def test_get_unknown_id_and_out_of_scope_id_are_not_found(self):
        unknown = self.cli(self.requester, 'get', '00000000-0000-4000-8000-000000000000')
        self.assertEqual(unknown.code, 1, unknown.both)
        self.assertEqual(unknown.json['error']['code'], 'not_found')
        self.assertTrue(unknown.err.startswith('wb_inbox: error 404 not_found: '), unknown.err)
        scoped = self.hub.key('scoped', scope={'projects': [self.hub.a]})
        hidden = self.cli(scoped, 'get', self.ids[2])
        self.assertEqual((hidden.code, hidden.json['error']['code']), (1, 'not_found'))

    def test_an_id_with_path_characters_cannot_reach_another_route(self):
        result = self.cli(self.requester, 'get', f'{self.ids[0]}/comments')
        self.assertEqual(result.code, 1, result.both)
        self.assertEqual(result.json['error']['code'], 'not_found')


class CommentTests(HubCase):
    def setUp(self):
        super().setUp()
        self.rid = self.submit().json['request_id']

    def comments(self) -> list[tuple]:
        return self.hub.inbox_rows('SELECT kind, author_kind, body FROM comments WHERE request_id = ? ORDER BY seq',
                                   self.rid)

    def test_comment_adds_to_the_request(self):
        result = self.cli(self.requester, 'comment', self.rid, '--body', '\ucd94\uac00 \uc124\uba85')
        self.assertEqual(result.code, 0, result.both)
        self.assertEqual(set(result.json), {'comment_id', 'state', 'version'})
        self.assertEqual(result.json['state'], 'NEW')
        self.assertIn(('comment', 'requester', '\ucd94\uac00 \uc124\uba85'), self.comments())

    def test_comment_from_file_and_stdin(self):
        path = self.base / 'c.txt'
        path.write_text('file comment', encoding='utf-8')
        self.assertEqual(self.cli(self.requester, 'comment', self.rid, '--body-file', str(path)).code, 0)
        self.assertEqual(self.cli(self.requester, 'comment', self.rid, '--body-file', '-', stdin='stdin comment').code, 0)
        self.assertEqual([row[2] for row in self.comments()][-2:], ['file comment', 'stdin comment'])

    def test_comment_needs_exactly_one_body_source(self):
        self.assertEqual(self.cli(self.requester, 'comment', self.rid).code, 2)
        self.assertEqual(self.cli(self.requester, 'comment', self.rid, '--body', 'a', '--body-file', '-').code, 2)

    def test_comment_on_a_hidden_request_is_not_found(self):
        scoped = self.hub.key('scoped', scope={'projects': [self.hub.b]})
        result = self.cli(scoped, 'comment', self.rid, '--body', 'x')
        self.assertEqual((result.code, result.json['error']['code']), (1, 'not_found'))
        self.assertEqual(self.comments(), [])


class EditorCommandTests(HubCase):
    """``update`` and ``set-state`` are editor-grade; a requester key is refused by the server (403 -> exit 1)."""

    def setUp(self):
        super().setUp()
        self.rid = self.submit(None, None, 'orig title', 'orig body').json['request_id']

    def row(self) -> tuple:
        return self.hub.inbox_rows('SELECT state, version, title, body FROM requests WHERE request_id = ?', self.rid)[0]

    def test_update_edits_title_and_body(self):
        result = self.cli(self.editor, 'update', self.rid, '--base-version', '1', '--title', '\ubc14\ub010 title')
        self.assertEqual(result.code, 0, result.both)
        self.assertEqual(result.json, {'state': 'NEW', 'version': 2, 'revision': 1, 'mode': 'in_place'})
        self.assertEqual(self.row(), ('NEW', 2, '\ubc14\ub010 title', 'orig body'))
        path = self.base / 'new-body.txt'
        path.write_text('body from file', encoding='utf-8')
        result = self.cli(self.editor, 'update', self.rid, '--base-version', '2', '--body-file', str(path))
        self.assertEqual(result.code, 0, result.both)
        self.assertEqual(self.row(), ('NEW', 3, '\ubc14\ub010 title', 'body from file'))

    def test_update_with_a_stale_version_is_a_conflict(self):
        self.assertEqual(self.cli(self.editor, 'update', self.rid, '--base-version', '1', '--body', 'x').code, 0)
        result = self.cli(self.editor, 'update', self.rid, '--base-version', '1', '--title', 'y')
        self.assertEqual(result.code, 1, result.both)
        self.assertEqual((result.json['error']['code'], result.json['error']['version']), ('conflict', 2))
        self.assertTrue(result.err.startswith('wb_inbox: error 409 conflict: '), result.err)

    def test_update_usage_errors(self):
        self.assertEqual(self.cli(self.editor, 'update', self.rid, '--title', 't').code, 2)
        self.assertEqual(self.cli(self.editor, 'update', self.rid, '--base-version', '1').code, 2)
        self.assertEqual(self.cli(self.editor, 'update', self.rid, '--base-version', 'one', '--title', 't').code, 2)
        self.assertEqual(self.row(), ('NEW', 1, 'orig title', 'orig body'))

    def test_set_state_moves_between_new_and_needs_info(self):
        result = self.cli(self.editor, 'set-state', self.rid, '--base-version', '1', '--to', 'NEEDS_INFO',
                          '--reason', 'need more detail')
        self.assertEqual(result.code, 0, result.both)
        self.assertEqual(result.json, {'state': 'NEEDS_INFO', 'version': 2})
        back = self.cli(self.editor, 'set-state', self.rid, '--base-version', '2', '--to', 'NEW')
        self.assertEqual(back.json, {'state': 'NEW', 'version': 3})

    def test_set_state_rejects_other_targets_before_sending(self):
        result = self.cli(self.editor, 'set-state', self.rid, '--base-version', '1', '--to', 'READY')
        self.assertEqual(result.code, 2, result.both)
        self.assertEqual(self.row()[:2], ('NEW', 1))

    def test_a_requester_key_is_refused_by_the_server(self):
        for args in (('update', self.rid, '--base-version', '1', '--title', 'x'),
                     ('set-state', self.rid, '--base-version', '1', '--to', 'NEEDS_INFO')):
            with self.subTest(command=args[0]):
                result = self.cli(self.requester, *args)
                self.assertEqual(result.code, 1, result.both)
                self.assertEqual(result.json['error']['code'], 'forbidden')
                self.assertTrue(result.err.startswith('wb_inbox: error 403 forbidden: '), result.err)
        self.assertEqual(self.row(), ('NEW', 1, 'orig title', 'orig body'))


class CancelTests(HubCase):
    """``cancel``: before dispatch it closes at once; for a RUNNING request the first call only returns a
    ``confirm_token`` (exit 0) and ``--confirm TOKEN`` carries it out (the Hub rule is in the REST tests)."""

    COMMAND = {'command_id': 'cmd-1', 'label': 'python train.py', 'state': 'running'}

    def setUp(self):
        super().setUp()
        self.rid = self.submit().json['request_id']

    def state(self) -> tuple:
        return self.hub.inbox_rows('SELECT state, outcome, cancel_state FROM requests WHERE request_id = ?', self.rid)[0]

    def test_a_request_that_has_not_started_is_cancelled_at_once(self):
        result = self.cli(self.requester, 'cancel', self.rid, '--reason', 'not needed')
        self.assertEqual(result.code, 0, result.both)
        self.assertEqual(result.json, {'status': 'cancelled', 'state': 'CLOSED'})
        self.assertEqual(self.state(), ('CLOSED', 'cancelled', None))

    def test_a_running_request_needs_the_two_step_confirmation(self):
        self.hub.link_running(self.rid, view={'command': self.COMMAND})
        first = self.cli(self.requester, 'cancel', self.rid)
        self.assertEqual(first.code, 0, first.both)
        self.assertEqual(first.json['status'], 'confirm_required')
        self.assertEqual(first.json['expires_in'], 60)
        token = first.json['confirm_token']
        self.assertEqual((self.hub.cancels, self.state()), ([], ('RUNNING', None, None)))
        second = self.cli(self.requester, 'cancel', self.rid, '--confirm', token)
        self.assertEqual(second.code, 0, second.both)
        self.assertEqual(second.json['status'], 'cancel_requested')
        self.assertEqual([event.request_id for event in self.hub.cancels], [self.rid])

    DASH_TOKEN = '-' + 'Xy_9' * 8

    def confirm_with_a_dash_token(self, confirm_args):
        """The Hub issues DASH_TOKEN (the server no longer does; the CLI must cope with any token a Hub sends)."""
        self.hub.link_running(self.rid, view={'command': self.COMMAND})
        with mock.patch('workbench.hub.inbox.limits.confirm_token', return_value=self.DASH_TOKEN):
            first = self.cli(self.requester, 'cancel', self.rid)
        self.assertEqual(first.json['confirm_token'], self.DASH_TOKEN, first.both)
        second = self.cli(self.requester, 'cancel', self.rid, *confirm_args(self.DASH_TOKEN))
        self.assertEqual(second.code, 0, second.both)
        self.assertEqual(second.json['status'], 'cancel_requested')
        self.assertEqual([event.request_id for event in self.hub.cancels], [self.rid])

    def test_a_token_that_starts_with_a_dash_works_as_a_separate_argument(self):
        self.confirm_with_a_dash_token(lambda token: ('--confirm', token))

    def test_a_token_that_starts_with_a_dash_works_as_confirm_equals_token(self):
        self.confirm_with_a_dash_token(lambda token: (f'--confirm={token}',))

    def test_a_wrong_token_is_refused_and_stops_nothing(self):
        self.hub.link_running(self.rid, view={'command': self.COMMAND})
        result = self.cli(self.requester, 'cancel', self.rid, '--confirm', 'not-the-token')
        self.assertEqual(result.code, 1, result.both)
        self.assertEqual(result.json['error']['code'], 'token_mismatch')
        self.assertTrue(result.err.startswith('wb_inbox: error 409 token_mismatch: '), result.err)
        self.assertEqual((self.hub.cancels, self.state()), ([], ('RUNNING', None, None)))


class ResultTests(HubCase):
    """``result``: summary by default, ``--full``, and raw/artifact pages read with ``--offset``/``--length``."""

    def setUp(self):
        super().setUp()
        import test_inbox_results as fixtures  # module import: its test classes stay uncollected here
        from workbench.hub.inbox import results
        self.fx = fixtures
        self.backend = fixtures.FakeBackend()
        self.hub.api.results = results.Results(self.hub.inbox, port=self.backend)
        self.rid = self.submit().json['request_id']
        self.hub.link_running(self.rid, task_id=fixtures.TASK, view={'status': 'running', 'runs': [
            {'run_id': fixtures.RUN, 'status': 'running', 'exit_code': None}]})

    def result(self, *args: str) -> dict:
        reply = self.cli(self.requester, 'result', self.rid, *args)
        self.assertEqual(reply.code, 0, reply.both)
        return reply.json

    def test_the_default_is_the_summary(self):
        value = self.result()
        self.assertEqual(set(value), {'summary', 'state', 'outcome', 'task', 'untrusted_text', 'note'})
        self.assertIn(f'run {self.fx.RUN}: running', value['summary'])

    def test_full_is_the_full_view(self):
        full = self.result('--full')['full']
        self.assertEqual((full['available'], full['task']['task_id']), (True, self.fx.TASK))

    def test_a_raw_page_is_read_to_the_end_with_next_offset(self):
        page = self.result('--run-id', self.fx.RUN, '--offset', '0', '--length', '17')['raw']
        self.assertEqual((page['offset'], page['length'], page['next_offset']), (0, 17, 17))
        data, offset = b'', 0
        while offset is not None and offset < len(self.fx.RUN_LOG):
            page = self.result('--full', '--run-id', self.fx.RUN, '--offset', str(offset), '--length', '300')['raw']
            data += page['text'].encode() if 'text' in page else base64.b64decode(page['data_base64'])
            offset = page['next_offset']
        self.assertEqual(data, self.fx.RUN_LOG)

    def test_an_artifact_page(self):
        (self.hub.root_a / 'out.txt').write_text('ARTIFACT-TEXT')
        value = self.result('--artifact-path', 'out.txt', '--offset', '2', '--length', '6')
        self.assertEqual(value['artifact']['text'], 'TIFACT')

    def test_run_id_and_artifact_path_exclude_each_other(self):
        reply = self.cli(self.requester, 'result', self.rid, '--run-id', self.fx.RUN, '--artifact-path', 'out.txt')
        self.assertEqual(reply.code, 2, reply.both)

    def test_server_refusals_are_exit_1(self):
        self.backend.down = 'backend_not_running'
        self.hub.backend_up = False
        reply = self.cli(self.requester, 'result', self.rid, '--full', '--run-id', self.fx.RUN)
        self.assertEqual((reply.code, reply.json['error']['code']), (1, 'backend_not_running'))
        bad = self.cli(self.requester, 'result', self.rid, '--run-id', self.fx.RUN, '--length', '0')
        self.assertEqual((bad.code, bad.json['error']['code']), (1, 'invalid_input'))


class ConnectionTests(CliCase):
    """Exit 4: DNS, refused connection, TLS, a reset, a non-JSON answer and any redirect (never followed)."""

    KEY = 'wbk_testkey0123456789secretvalue'

    def run_against(self, url: str, *args: str) -> Result:
        return self.run_cli(*(args or ('projects',)), env={'WB_HUB_URL': url, 'WB_API_KEY': self.KEY})

    def assert_connection_error(self, result: Result) -> None:
        self.assertEqual(result.code, 4, result.both)
        self.assertEqual(result.out, '')
        self.assertRegex(result.err, r'^wb_inbox: [^\n]+\n$')
        self.assertNotIn(self.KEY, result.both)
        self.assertNotIn('Traceback', result.err)

    def test_connection_refused(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        result = self.run_against(f'http://127.0.0.1:{port}')
        self.assert_connection_error(result)
        self.assertIn('ConnectionRefusedError', result.err)

    def test_unresolvable_host(self):
        self.assert_connection_error(self.run_against('http://no-such-host.invalid'))

    def test_tls_to_a_plain_http_server(self):
        server = FakeServer(self, lambda handler: reply(handler, 200, b'{}'))
        self.assert_connection_error(self.run_against(server.url.replace('http://', 'https://')))

    def test_a_server_that_hangs_up(self):
        def hang_up(handler):
            handler.connection.close()
        server = FakeServer(self, hang_up)
        self.assert_connection_error(self.run_against(server.url))

    def test_non_json_answers(self):
        cases = ((200, b'<html>ok</html>'), (200, b''), (200, b'{"projects": ['), (200, b'\xff\xfe'),
                 (502, b'<html>bad gateway</html>'), (500, b''), (404, b'not json'), (401, b'\xff'))
        for status, body in cases:
            with self.subTest(status=status, body=body[:12]):
                server = FakeServer(self, lambda handler, s=status, b=body: reply(handler, s, b))
                self.assert_connection_error(self.run_against(server.url))

    def test_redirects_are_not_followed_and_the_key_goes_nowhere_else(self):
        target = FakeServer(self, lambda handler: reply(handler, 200, b'{"projects": []}'))
        for status in (301, 302, 303, 307, 308):
            for args in (('projects',), ('comment', 'rid-1', '--body', 'hello')):
                with self.subTest(status=status, command=args[0]):
                    origin = FakeServer(self, lambda handler, s=status: reply(
                        handler, s, b'', {'Location': target.url + '/stolen'}))
                    result = self.run_against(origin.url, *args)
                    self.assert_connection_error(result)
                    self.assertEqual(len(origin.seen), 1)
        self.assertEqual(target.seen, [])

    def test_a_2xx_json_that_is_not_an_object_or_array_is_still_printed(self):
        server = FakeServer(self, lambda handler: reply(handler, 200, b'{"projects": []}'))
        result = self.run_against(server.url)
        self.assertEqual((result.code, result.json), (0, {'projects': []}))
        self.assertEqual(server.seen[0][:3], ('GET', '/api/v1/projects', f'Bearer {self.KEY}'))


class KeyHygieneTests(HubCase):
    """WBX-AC-14: the key is only in the ``Authorization`` header; no stdout/stderr path shows it (or its header)."""

    KEY = 'wbk_hygiene0123456789abcdefSECRETVALUE'

    def run_key(self, url: str, *args: str, key: str | None = None, **kwargs) -> Result:
        return self.run_cli(*args, env={'WB_HUB_URL': url, 'WB_API_KEY': key or self.KEY}, **kwargs)

    def assert_clean(self, result: Result, key: str | None = None) -> None:
        for secret in (key or self.KEY, f'Bearer {key or self.KEY}'):
            self.assertNotIn(secret, result.both)
        self.assertNotIn('Traceback', result.err)

    def test_every_hub_command_on_every_outcome_keeps_the_key_out(self):
        rid = self.submit().json['request_id']
        runs = (
            (self.requester, ('projects',)), (self.requester, ('config',)), (self.requester, ('list',)),
            (self.requester, ('get', rid)), (self.requester, ('get', 'no-such-request')),
            (self.requester, ('comment', rid, '--body', 'hi')), (self.requester, ('update', rid, '--base-version', '1',
                                                                                  '--title', 'x')),
            (self.editor, ('update', rid, '--base-version', '1', '--title', 'x')),
            (self.requester, ('set-state', rid, '--base-version', '1', '--to', 'NEW')),
            (self.requester, ('cancel', rid, '--confirm', 'wrong')), (self.requester, ('result', rid)),
            (self.requester, ('submit', '--project', self.hub.a, '--title', 't', '--body', 'b')),
            (self.requester, ('submit', '--project', self.hub.a)), (self.requester, ('bogus',)),
            ('wbk_notarealkey000000000000', ('projects',)), (self.requester, ('list', '--limit', '0')))
        for key, args in runs:
            with self.subTest(args=args[:2], key=key[:8]):
                self.assert_clean(self.cli(key, *args), key)

    def test_the_key_only_travels_in_the_authorization_header(self):
        server = FakeServer(self, lambda handler: reply(handler, 200, b'{"ok": true}'))
        result = self.run_key(server.url, 'comment', 'rid-1', '--body', 'hello')
        self.assertEqual(result.code, 0, result.both)
        method, path, authorization, body = server.seen[0]
        self.assertEqual(authorization, f'Bearer {self.KEY}')
        self.assertNotIn(self.KEY, path)
        self.assertNotIn(self.KEY.encode(), body)
        self.assert_clean(result)

    def test_a_server_that_echoes_the_header_does_not_make_the_cli_print_it(self):
        def echo(handler):
            header = handler.headers.get('Authorization')
            reply(handler, 401, json.dumps({'error': {'code': 'unauthorized', 'message': f'bad {header}'}}).encode())
        server = FakeServer(self, echo)
        result = self.run_key(server.url, 'projects')
        self.assertEqual(result.code, 1, result.both)
        self.assert_clean(result)
        self.assertEqual(result.json['error']['code'], 'unauthorized')

    def test_a_success_body_that_contains_the_key_is_scrubbed(self):
        server = FakeServer(self, lambda handler: reply(
            handler, 200, json.dumps({'projects': [{'name': self.KEY}], 'echo': handler.headers['Authorization']}).encode()))
        result = self.run_key(server.url, 'projects')
        self.assertEqual(result.code, 0, result.both)
        self.assert_clean(result)

    def test_usage_errors_that_quote_the_key_are_scrubbed(self):
        server = FakeServer(self, lambda handler: reply(handler, 200, b'{}'))
        for args in ((self.KEY,), ('list', '--limit', self.KEY), ('set-state', 'r', '--base-version', '1', '--to', self.KEY),
                     ('submit', '--project', 'p', '--title', 't', '--body', 'b', self.KEY), ('--' + self.KEY,)):
            with self.subTest(args=args[:2]):
                result = self.run_key(server.url, *args)
                self.assertEqual(result.code, 2, result.both)
                self.assert_clean(result)
        self.assertEqual(server.seen, [])

    def test_a_key_that_cannot_be_a_header_value_is_a_configuration_error(self):
        server = FakeServer(self, lambda handler: reply(handler, 200, b'{}'))
        for key in ('wbk_has space0123456789', 'wbk_line\nbreak0123456789', 'wbk_tab\t0123456789abcdef',
                    'wbk_\ud55c\uae00\ud55c\uae00\ud55c\uae00\ud55c\uae00', 'wbk_cr\r0123456789abcdef'):
            with self.subTest(key=key[:8]):
                result = self.run_key(server.url, 'projects', key=key)
                self.assertEqual(result.code, 3, result.both)
                self.assertIn('WB_API_KEY', result.err)
                for part in [key, *re.split(r'\s', key)]:
                    if len(part) >= 8:
                        self.assertNotIn(part, result.both)
        self.assertEqual(server.seen, [])

    def test_a_pathologically_nested_answer_is_a_clean_connection_error(self):
        server = FakeServer(self, lambda handler: reply(handler, 200, b'[' * 200000))
        result = self.run_key(server.url, 'projects')
        self.assertEqual(result.code, 4, result.both)
        self.assert_clean(result)

    def test_help_and_config_do_not_print_the_key(self):
        for args in (('--help',), ('submit', '--help'), ('config',)):
            with self.subTest(args=args):
                result = self.run_key(self.url, *args)
                self.assertEqual(result.code, 0, result.both)
                self.assert_clean(result)

    def test_the_env_file_key_is_never_printed_either(self):
        self.write_env(self.skill / '.env', f'WB_HUB_URL=http://127.0.0.1:9\nWB_API_KEY={self.KEY}\n', 0o644)
        for args in (('config',), ('projects',), ('list',)):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertNotIn(self.KEY, result.both)
        self.assertIn('is accessible by other users', result.err)


class ErrorLineTests(CliCase):
    """The stderr refusal line is one bounded line even when the server message is huge or has control characters."""

    def test_a_huge_server_message_is_capped_on_one_line(self):
        message = 'x' * 5_000_000
        body = json.dumps({'error': {'code': 'internal_error', 'message': message}}).encode()
        server = FakeServer(self, lambda handler: reply(handler, 500, body))
        result = self.run_cli('projects', env={'WB_HUB_URL': server.url, 'WB_API_KEY': 'wbk_cap0123456789'})
        self.assertEqual(result.code, 1)
        self.assertEqual(len(result.err.splitlines()), 1)
        self.assertLessEqual(len(result.err), 1100)
        self.assertTrue(result.err.startswith('wb_inbox: error 500 internal_error: '))
        self.assertTrue(result.err.rstrip().endswith('...'), result.err[-20:])

    def test_control_characters_become_spaces(self):
        body = json.dumps({'error': {'code': 'x\ny', 'message': 'a\nb\r\x1b[31mc\x00d\u2028e'}}).encode()
        server = FakeServer(self, lambda handler: reply(handler, 400, body))
        result = self.run_cli('projects', env={'WB_HUB_URL': server.url, 'WB_API_KEY': 'wbk_cap0123456789'})
        self.assertEqual(result.err, 'wb_inbox: error 400 x y: a b [31mc d e\n')


if __name__ == '__main__':
    unittest.main()
