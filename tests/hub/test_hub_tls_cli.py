"""``wb hub tls --self-signed [--force]`` (TI-07, seam T8): real CLI subprocesses and the host ``openssl``.

Temp HOME/XDG_* (``WebSandbox``), ``WORKBENCH_HUB_AUTOSTART=0``. Expected values are what ``openssl
x509 -text`` and ``os.lstat`` report about the written files and the SPEC command line. Run with
``discover -s tests/hub``.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from fixtures.web_sandbox import PASSWORD, WebSandbox  # noqa: E402

OPENSSL = shutil.which('openssl') or '/usr/bin/openssl'
RESTART_TEXT = 'Hub를 다시 시작하면 HTTPS로 열립니다'


def openssl(*args: str, data: bytes | None = None) -> str:
    return subprocess.run([OPENSSL, *args], input=data, capture_output=True, check=True, timeout=30).stdout.decode()


def ossl_ip(address: str) -> str:
    """How ``openssl x509 -text`` prints an IP SAN: IPv4 as is, IPv6 as 8 uppercase groups without zeros."""
    import ipaddress
    parsed = ipaddress.ip_address(address)
    if parsed.version == 4:
        return f'IP Address:{address}'
    return 'IP Address:' + ':'.join(f'{int(group, 16):X}' for group in parsed.exploded.split(':'))


def san_set(text: str) -> set:
    san = re.search(r'Subject Alternative Name:[^\n]*\n\s*([^\n]+)', text).group(1)
    return {item.strip() for item in san.split(',')}


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


class TlsCase(unittest.TestCase):
    def setUp(self):
        self.box = WebSandbox(self)
        self.cert, self.key = self.box.tls_dir / 'hub-cert.pem', self.box.tls_dir / 'hub-key.pem'

    def tls(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        return self.box.cli('hub', 'tls', *args, **kwargs)

    def text(self, cert: Path | None = None) -> str:
        return openssl('x509', '-noout', '-text', '-in', str(cert or self.cert))

    def fingerprint(self, cert: Path | None = None) -> str:
        return openssl('x509', '-noout', '-fingerprint', '-sha256', '-in', str(cert or self.cert)).strip().split('=', 1)[1]


class GenerateTests(TlsCase):
    def test_default_creates_a_loopback_cert_and_a_private_key_and_starts_no_hub(self):
        result = self.tls('--self-signed')
        self.assertEqual(result.returncode, 0, result)
        self.assertEqual((mode(self.box.tls_dir), mode(self.key), mode(self.cert)), (0o700, 0o600, 0o644))
        text = self.text()
        self.assertIn('Subject: CN = 127.0.0.1', text)
        self.assertIn('ecdsa-with-SHA256', text)
        self.assertIn('prime256v1', text)
        self.assertEqual(san_set(text), {ossl_ip('127.0.0.1'), ossl_ip('::1'), 'DNS:localhost'})
        self.assertEqual(openssl('x509', '-noout', '-pubkey', '-in', str(self.cert)),
                         openssl('pkey', '-pubout', '-in', str(self.key)))
        self.assertFalse(self.box.state.exists(), 'tls must not start or touch the Hub')

    def test_validity_is_825_days(self):
        self.assertEqual(self.tls('--self-signed').returncode, 0)
        end = openssl('x509', '-noout', '-enddate', '-in', str(self.cert)).strip()
        self.assertEqual(subprocess.run([OPENSSL, 'x509', '-checkend', str(824 * 86400), '-noout', '-in', str(self.cert)],
                                        capture_output=True).returncode, 0, end)
        self.assertEqual(subprocess.run([OPENSSL, 'x509', '-checkend', str(826 * 86400), '-noout', '-in', str(self.cert)],
                                        capture_output=True).returncode, 1, end)

    def test_output_shows_paths_the_sha256_fingerprint_and_the_restart_hint_but_never_the_key(self):
        result = self.tls('--self-signed')
        self.assertIn(self.fingerprint(), result.stdout)
        self.assertIn(RESTART_TEXT, result.stdout)
        self.assertIn(str(self.cert), result.stdout)
        self.assertIn(str(self.key), result.stdout)
        self.assertNotIn('PRIVATE KEY', result.stdout + result.stderr)

    def test_bind_and_server_names_come_from_hub_toml(self):
        self.box.write_config('127.0.0.2', 0, 'server_names = ["Box.VPN", "nas.lan"]')
        self.assertEqual(self.tls('--self-signed').returncode, 0)
        text = self.text()
        self.assertIn('Subject: CN = 127.0.0.2', text)
        self.assertEqual(san_set(text), {ossl_ip('127.0.0.2'), ossl_ip('127.0.0.1'), ossl_ip('::1'), 'DNS:localhost',
                                         'DNS:box.vpn', 'DNS:nas.lan'})

    def test_configured_tls_paths_are_used(self):
        cert, key = self.box.base / 'certs' / 'c.pem', self.box.base / 'certs' / 'k.pem'
        self.box.write_config(raw=f'[web]\nport = 0\ntls_cert = "{cert}"\ntls_key = "{key}"\n')
        self.assertEqual(self.tls('--self-signed').returncode, 0)
        self.assertEqual((mode(cert.parent), mode(key), mode(cert)), (0o700, 0o600, 0o644))
        self.assertFalse(self.box.tls_dir.exists())

    def test_invalid_hub_toml_writes_nothing(self):
        self.box.write_config(raw='[web]\nbind = "not-an-ip"\n')
        result = self.tls('--self-signed')
        self.assertEqual(result.returncode, 1, result)
        self.assertIn('config_invalid:web.bind', result.stdout + result.stderr)
        self.assertFalse(self.box.tls_dir.exists())


class RefusalTests(TlsCase):
    def test_without_self_signed_nothing_runs(self):
        result = self.tls()
        self.assertEqual(result.returncode, 2, result)
        self.assertIn('--self-signed', result.stdout + result.stderr)
        self.assertFalse(self.box.tls_dir.exists())
        self.assertFalse(self.box.state.exists())

    def test_an_existing_cert_or_key_is_never_overwritten_without_force(self):
        for existing in ('cert', 'key', 'both'):
            with self.subTest(existing):
                shutil.rmtree(self.box.tls_dir, ignore_errors=True)
                self.box.tls_dir.mkdir(mode=0o700, parents=True)
                kept = {}
                for name, path in (('cert', self.cert), ('key', self.key)):
                    if existing in (name, 'both'):
                        path.write_text(f'user {name}\n')
                        kept[path] = path.read_bytes()
                result = self.tls('--self-signed')
                self.assertEqual(result.returncode, 1, result)
                self.assertIn('--force', result.stdout + result.stderr)
                self.assertEqual({path: path.read_bytes() for path in kept}, kept)
                self.assertEqual(sorted(path.name for path in self.box.tls_dir.iterdir()),
                                 sorted(path.name for path in kept))

    def test_a_symlink_at_the_target_is_not_followed_or_replaced_without_force(self):
        self.box.tls_dir.mkdir(mode=0o700, parents=True)
        victim = self.box.base / 'victim.pem'
        victim.write_text('mine\n')
        self.cert.symlink_to(victim)
        self.assertEqual(self.tls('--self-signed').returncode, 1)
        self.assertEqual((victim.read_text(), os.path.islink(self.cert)), ('mine\n', True))

    def test_force_replaces_both_files_with_a_new_pair(self):
        self.assertEqual(self.tls('--self-signed').returncode, 0)
        before = self.fingerprint()
        result = self.tls('--self-signed', '--force')
        self.assertEqual(result.returncode, 0, result)
        self.assertNotEqual(self.fingerprint(), before)
        self.assertEqual((mode(self.box.tls_dir), mode(self.key), mode(self.cert)), (0o700, 0o600, 0o644))
        self.assertEqual(openssl('x509', '-noout', '-pubkey', '-in', str(self.cert)),
                         openssl('pkey', '-pubout', '-in', str(self.key)))
        self.assertEqual(sorted(path.name for path in self.box.tls_dir.iterdir()), ['hub-cert.pem', 'hub-key.pem'])

    def test_force_does_not_follow_a_symlink_it_replaces(self):
        self.box.tls_dir.mkdir(mode=0o700, parents=True)
        victim = self.box.base / 'victim.pem'
        victim.write_text('mine\n')
        self.key.symlink_to(victim)
        self.assertEqual(self.tls('--self-signed', '--force').returncode, 0)
        self.assertEqual(victim.read_text(), 'mine\n')
        self.assertFalse(os.path.islink(self.key))

    def test_missing_openssl_is_exit_2_and_creates_nothing(self):
        empty = self.box.base / 'empty-bin'
        empty.mkdir()
        self.box.env['PATH'] = str(empty)
        result = self.tls('--self-signed')
        self.assertEqual(result.returncode, 2, result)
        self.assertIn('openssl', result.stdout + result.stderr)
        self.assertFalse(self.box.tls_dir.exists())


class OpensslInvocationTests(TlsCase):
    def test_openssl_gets_the_spec_argument_vector_without_a_shell(self):
        bin_dir = self.box.base / 'bin'
        bin_dir.mkdir()
        log = self.box.base / 'openssl-argv.log'
        wrapper = bin_dir / 'openssl'
        wrapper.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" >> {log}\nprintf -- \'--\\n\' >> {log}\nexec {OPENSSL} "$@"\n')
        wrapper.chmod(0o755)
        self.box.env['PATH'] = f'{bin_dir}:/usr/bin:/bin'
        self.box.write_config('127.0.0.1', 0, 'server_names = ["box.vpn"]')
        self.assertEqual(self.tls('--self-signed').returncode, 0)
        calls = [call.split('\n') for call in log.read_text().split('--\n') if call]
        generate = calls[0]
        self.assertEqual(generate[:9],
                         ['req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
                          '-days', '825'])
        self.assertEqual(generate[9:19], ['-subj', '/CN=127.0.0.1', '-addext',
                                          'subjectAltName=IP:127.0.0.1,IP:::1,DNS:localhost,DNS:box.vpn',
                                          '-addext', 'basicConstraints=critical,CA:FALSE', '-addext',
                                          'extendedKeyUsage=serverAuth', '-addext',
                                          'keyUsage=digitalSignature,keyEncipherment'])
        self.assertEqual(generate[19], '-keyout')
        self.assertEqual(generate[21], '-out')
        for staged, name in ((generate[20], 'key.pem'), (generate[22], 'cert.pem')):
            # staged in a private directory beside the targets, never over them
            self.assertEqual((Path(staged).name, Path(staged).parent.parent), (name, self.box.tls_dir))


class HttpsAfterRestartTests(TlsCase):
    def test_a_restarted_hub_serves_https_with_the_generated_cert(self):
        self.box.write_config('127.0.0.1', 0)
        self.assertEqual(self.tls('--self-signed').returncode, 0)
        self.box.set_password(PASSWORD)
        hub = self.box.start_hub()
        _host, port = self.box.wait_listening(hub.pid)
        web = self.box.status()['web']
        self.assertEqual((web['state'], web['tls']), ('listening', True))
        self.assertTrue(web['url'].startswith('https://'), web)
        context = ssl.create_default_context(cafile=str(self.cert))
        with socket.create_connection(('127.0.0.1', port), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname='127.0.0.1') as secured:
                self.assertEqual(secured.version() in ('TLSv1.2', 'TLSv1.3'), True)
        status = self.box.cli('hub', 'status')
        self.assertNotIn('평문', status.stdout)
        self.box.stop_hub(hub)


class CertificateExtensionTests(TlsCase):
    def test_the_cert_is_a_server_leaf_not_a_ca(self):
        self.assertEqual(self.tls('--self-signed').returncode, 0)
        text = self.text()
        self.assertRegex(text, r'X509v3 Basic Constraints: critical\s+CA:FALSE')
        self.assertRegex(text, r'X509v3 Extended Key Usage:\s+TLS Web Server Authentication')
        self.assertRegex(text, r'X509v3 Key Usage:[^\n]*\n\s+Digital Signature, Key Encipherment')
        self.assertNotIn('CA:TRUE', text)


class WildcardSanTests(TlsCase):
    def test_a_wildcard_bind_never_puts_the_wildcard_address_in_the_cert(self):
        for bind in ('0.0.0.0', '::'):
            with self.subTest(bind):
                shutil.rmtree(self.box.tls_dir, ignore_errors=True)
                self.box.write_config(bind, 0, 'server_names = ["box.vpn"]')
                result = self.tls('--self-signed')
                self.assertEqual(result.returncode, 0, result)
                san = san_set(self.text())
                self.assertTrue({ossl_ip('127.0.0.1'), ossl_ip('::1'), 'DNS:localhost', 'DNS:box.vpn'} <= san, san)
                self.assertNotIn(ossl_ip(bind), san)
                self.assertNotIn('IP Address:0.0.0.0', san)
                self.assertNotIn('IP Address:0:0:0:0:0:0:0:0', san)
                self.assertIn('SAN:', result.stdout)
                self.assertIn('IP:127.0.0.1', result.stdout)
                self.assertNotIn('IP:0.0.0.0', result.stdout)

    def test_san_entries_for_each_bind_kind(self):
        from workbench.hub import tls
        from workbench.hub.config import WebConfig
        host = lambda: (['10.8.0.2', 'fd00::5', '10.8.0.2'], ['box.lan'])
        self.assertEqual(tls.san_entries(WebConfig(bind='0.0.0.0', server_names=('a.vpn',)), discover=host),
                         ('IP:127.0.0.1', 'IP:::1', 'IP:10.8.0.2', 'IP:fd00::5', 'DNS:localhost', 'DNS:box.lan',
                          'DNS:a.vpn'))
        self.assertEqual(tls.san_entries(WebConfig(bind='::'), discover=host),
                         ('IP:127.0.0.1', 'IP:::1', 'IP:10.8.0.2', 'IP:fd00::5', 'DNS:localhost', 'DNS:box.lan'))
        self.assertEqual(tls.san_entries(WebConfig(bind='::1'), discover=host),
                         ('IP:127.0.0.1', 'IP:::1', 'DNS:localhost'))
        self.assertEqual(tls.san_entries(WebConfig(bind='10.8.0.2'), discover=host),
                         ('IP:10.8.0.2', 'DNS:localhost'))

    def test_interface_addresses_come_from_proc_without_external_tools(self):
        from workbench.hub import tls
        proc = self.box.base / 'proc'
        (proc / 'net').mkdir(parents=True)
        (proc / 'net' / 'fib_trie').write_text(
            'Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 0.0.0.0\n        /0 universe UNICAST\n'
            '     +-- 10.8.0.0/24 2 0 2\n        |-- 10.8.0.0\n           /32 link BROADCAST\n'
            '        |-- 10.8.0.2\n           /32 host LOCAL\n        |-- 10.8.0.255\n           /32 link BROADCAST\n'
            'Local:\n     +-- 127.0.0.0/8 2 0 2\n        |-- 127.0.0.1\n           /32 host LOCAL\n')
        (proc / 'net' / 'if_inet6').write_text(
            '00000000000000000000000000000001 01 80 10 80 lo\n'
            'fd000000000000000000000000000005 02 40 00 80 eth0\n'
            'fe800000000000000000000000000009 02 40 20 80 eth0\n')
        self.assertEqual(tls.proc_addresses(proc), ['10.8.0.2', '127.0.0.1', '::1', 'fd00::5'])
        self.assertEqual(tls.proc_addresses(self.box.base / 'missing'), [])


class ForceReplacesAsPairTests(TlsCase):
    def web(self):
        from workbench.hub.config import WebConfig
        return WebConfig()

    def flaky(self, tls):
        real_install, calls = tls._install, []

        def flaky(source, destination, mode_bits):
            calls.append(destination.name)
            if len(calls) == 2:
                raise OSError(28, 'No space left on device')
            return real_install(source, destination, mode_bits)
        return flaky, calls

    def test_a_failure_between_the_two_installs_restores_the_old_pair(self):
        from workbench.hub import tls
        self.assertEqual(self.tls('--self-signed').returncode, 0)
        before = (self.cert.read_bytes(), self.key.read_bytes())
        flaky, calls = self.flaky(tls)
        with mock.patch.object(tls, '_install', flaky):
            with self.assertRaises(tls.TlsError):
                tls.self_signed(self.web(), self.cert, self.key, force=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual((self.cert.read_bytes(), self.key.read_bytes()), before)
        self.assertEqual((mode(self.key), mode(self.cert)), (0o600, 0o644))
        self.assertEqual(sorted(path.name for path in self.box.tls_dir.iterdir()), ['hub-cert.pem', 'hub-key.pem'])

    def test_a_failure_leaves_no_half_pair_when_nothing_existed(self):
        from workbench.hub import tls
        flaky, _calls = self.flaky(tls)
        with mock.patch.object(tls, '_install', flaky):
            with self.assertRaises(tls.TlsError):
                tls.self_signed(self.web(), self.cert, self.key, force=False)
        self.assertEqual(list(self.box.tls_dir.iterdir()), [])

    def test_a_success_leaves_no_backup_files(self):
        self.assertEqual(self.tls('--self-signed').returncode, 0)
        self.assertEqual(self.tls('--self-signed', '--force').returncode, 0)
        self.assertEqual(sorted(path.name for path in self.box.tls_dir.iterdir()), ['hub-cert.pem', 'hub-key.pem'])


class LinkFallbackTests(TlsCase):
    def test_a_filesystem_without_hard_links_still_installs_without_overwriting(self):
        import errno
        from workbench.hub import tls
        from workbench.hub.config import WebConfig
        for code in (errno.EPERM, errno.EXDEV):
            with self.subTest(errno.errorcode[code]):
                shutil.rmtree(self.box.tls_dir, ignore_errors=True)
                with mock.patch.object(tls.os, 'link', side_effect=OSError(code, 'no links')):
                    made = tls.self_signed(WebConfig(), self.cert, self.key, force=False)
                self.assertEqual((mode(self.key), mode(self.cert)), (0o600, 0o644))
                self.assertEqual(openssl('x509', '-noout', '-pubkey', '-in', str(self.cert)),
                                 openssl('pkey', '-pubout', '-in', str(self.key)))
                self.assertEqual(made.fingerprint, self.fingerprint())
                self.assertEqual(sorted(path.name for path in self.box.tls_dir.iterdir()),
                                 ['hub-cert.pem', 'hub-key.pem'])

    def test_the_fallback_refuses_an_existing_name(self):
        import errno
        from workbench.hub import tls
        self.box.tls_dir.mkdir(mode=0o700, parents=True)
        mine = self.box.tls_dir / 'x.pem'
        mine.write_text('mine\n')
        staged = self.box.base / 'staged.pem'
        staged.write_text('new\n')
        with mock.patch.object(tls.os, 'link', side_effect=OSError(errno.EPERM, 'no links')):
            with self.assertRaises(FileExistsError):
                tls._install(staged, mine, 0o644)
        self.assertEqual(mine.read_text(), 'mine\n')


class StatusLinesTests(TlsCase):
    def test_plain_http_warning_line_follows_the_injected_decision(self):
        from workbench.hub.cli import web_status_lines
        from workbench.hub.config import INSECURE_WARNING_TEXT
        listening = {'state': 'listening', 'url': 'http://10.8.0.2:8780', 'tls': False}
        self.assertEqual(web_status_lines({**listening, 'insecure_warning': True}),
                         ['web: listening http://10.8.0.2:8780', f'경고: {INSECURE_WARNING_TEXT}'])
        self.assertEqual(web_status_lines({**listening, 'insecure_warning': False}),
                         ['web: listening http://10.8.0.2:8780'])

    def test_status_says_there_is_no_board_agent_until_one_exists(self):
        self.box.write_config('127.0.0.1', 0)
        hub = self.box.start_hub()
        result = self.box.cli('hub', 'status')
        self.assertEqual(result.returncode, 0, result)
        self.assertIn('  board_agent: 없음', result.stdout.splitlines())
        as_json = self.box.status()
        self.assertIsNone(as_json['board_agent'])
        self.assertNotIn('board_agent: 없음', self.box.cli('hub', 'status', '--json').stdout)
        self.box.stop_hub(hub)


if __name__ == '__main__':
    unittest.main()
