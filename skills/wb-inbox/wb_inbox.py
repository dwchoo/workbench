#!/usr/bin/env python3
"""wb_inbox.py: command line client for the Workbench ``/api/v1/`` request API (Python 3 standard library only).

Settings (looked up per variable): process environment, then ``.env`` next to this file, then
``$XDG_CONFIG_HOME/omp-workbench/client.env`` (``~/.config/...`` when ``XDG_CONFIG_HOME`` is not absolute).
  WB_HUB_URL   Hub origin, for example http://192.0.2.10:8780
  WB_API_KEY   API key (wbk_...); only ever sent in the Authorization header, never printed

Output is JSON on stdout. Exit codes: 0 ok, 1 the server refused (4xx/5xx), 2 usage error,
3 configuration error, 4 connection error.
"""
import argparse
import hashlib
import http.client
import json
import os
import stat
import sys
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote, urlencode, urlsplit

PROCESS_ENVIRONMENT = 'process environment'
VARIABLES = ('WB_HUB_URL', 'WB_API_KEY')
MAX_ENV_BYTES = 65536
TIMEOUT = 30
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MIN_SCRUBBED_KEY = 6
MAX_MESSAGE_CHARS = 1000

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_CONFIG, EXIT_CONNECTION = 0, 1, 2, 3, 4


SECRETS = []  # the API key, once known: every byte this program prints goes through scrub()


def scrub(text):
    for secret in SECRETS:
        text = text.replace(secret, '[redacted]')
    return text


def write_stderr(text):
    sys.stderr.write(scrub(text))
    sys.stderr.flush()


class Failure(Exception):
    """A deliberate failure with an exit code and a message written by this program (never from an exception)."""

    def __init__(self, code, message, report=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.report = report


def parse_env_text(text):
    """``KEY=VALUE`` lines; blank lines and ``#`` comments are ignored, optional ``export ``, no substitution."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export ') or line.startswith('export\t'):
            line = line[7:].lstrip()
        name, separator, value = line.partition('=')
        if not separator:
            continue
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[name] = value
    return values


class Settings:
    """Per-variable lookup over the process environment and the two ``.env`` files (each read at most once)."""

    def __init__(self, environ, script_path):
        self.environ = environ
        folder = os.path.dirname(os.path.abspath(script_path))
        base = environ.get('XDG_CONFIG_HOME', '')
        if not os.path.isabs(base):
            base = os.path.join(os.path.expanduser('~'), '.config')
        self.files = [os.path.join(folder, '.env'), os.path.join(base, 'omp-workbench', 'client.env')]
        self.warnings = []
        self._cache = {}

    def locations(self):
        return [PROCESS_ENVIRONMENT] + self.files

    def _file(self, path):
        if path not in self._cache:
            self._cache[path] = self._read(path)
        return self._cache[path]

    def _read(self, path):
        try:
            # O_NONBLOCK: opening a FIFO named .env must not wait for a writer; it is rejected below.
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        except OSError:
            return {}
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                return {}
            mode = stat.S_IMODE(info.st_mode)
            if mode & 0o077:
                self.warnings.append('wb_inbox: warning: %s is accessible by other users (mode %04o); run: chmod 600 %s'
                                     % (path, mode, path))
            data = os.read(descriptor, MAX_ENV_BYTES)
        except OSError:
            return {}
        finally:
            os.close(descriptor)
        return parse_env_text(data.decode('utf-8', 'replace').lstrip('﻿'))

    def find(self, name):
        """``(value, source)`` of the first place with a non-empty value, or ``(None, None)``."""
        value = self.environ.get(name, '')
        if value:
            return value, PROCESS_ENVIRONMENT
        for path in self.files:
            value = self._file(path).get(name, '')
            if value:
                return value, path
        return None, None


def validate_hub_url(url):
    """Return the origin without a trailing slash, or raise a configuration ``Failure`` (the value is never echoed)."""
    problem = Failure(EXIT_CONFIG, 'wb_inbox: WB_HUB_URL must look like http://host[:port] or https://host[:port] '
                                   '(no user info, path, query or fragment)')
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or '?' in url or '#' in url:
        raise problem
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise problem
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname or '@' in parts.netloc:
        raise problem
    if parts.path not in ('', '/') or (port is None and parts.netloc.endswith(':')):
        raise problem
    return parts.scheme.lower() + '://' + parts.netloc


def missing_messages(settings, names):
    where = ', '.join(settings.locations())
    return ['wb_inbox: %s is not set; looked in: %s' % (name, where) for name in names]


def command_config(settings, args):
    report = {}
    for name in VARIABLES:
        value, source = settings.find(name)
        entry = {'status': 'set' if value else 'missing'}
        if source:
            entry['source'] = source
        if value and name == 'WB_HUB_URL':
            entry['value'] = value
        report[name] = entry
    missing = [name for name in VARIABLES if report[name]['status'] == 'missing']
    if missing:
        raise Failure(EXIT_CONFIG, '\n'.join(missing_messages(settings, missing)), report)
    try:
        validate_hub_url(report['WB_HUB_URL']['value'])
    except Failure as failure:
        del report['WB_HUB_URL']['value']  # a malformed URL may carry credentials
        failure.report = report
        raise
    return report


def one_line(value, limit=MAX_MESSAGE_CHARS):
    """A server-supplied string as a single bounded line: control characters and line breaks become one space."""
    text = ' '.join(''.join(char if char.isprintable() else ' ' for char in str(value)).split())
    return text if len(text) <= limit else text[:limit] + '...'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the Authorization header to wherever Location points."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def api_call(settings, method, path, query=None, body=None):
    """One request to ``<WB_HUB_URL>/api/v1<path>``; returns the parsed JSON of a 2xx answer."""
    missing = [name for name in VARIABLES if not settings.find(name)[0]]
    if missing:
        raise Failure(EXIT_CONFIG, '\n'.join(missing_messages(settings, missing)))
    origin = validate_hub_url(settings.find('WB_HUB_URL')[0])
    key = settings.find('WB_API_KEY')[0]
    if not all(33 <= ord(char) <= 126 for char in key):
        raise Failure(EXIT_CONFIG, 'wb_inbox: WB_API_KEY contains characters that cannot be sent in a header '
                                   '(use the wbk_... key exactly as issued)')
    url = origin + '/api/v1' + path
    pairs = []
    for name, value in (query or []):
        if value is None:
            continue
        pairs.append((name, 'true' if value is True else 'false' if value is False else str(value)))
    if pairs:
        url += '?' + urlencode(pairs)
    headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json', 'User-Agent': 'wb-inbox/1'}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with OPENER.open(request, timeout=TIMEOUT) as response:
            return parse_answer(response.read(MAX_RESPONSE_BYTES), response.status)
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise Failure(EXIT_CONNECTION, 'wb_inbox: the Hub answered with a redirect (HTTP %d); '
                                           'redirects are not followed' % error.code)
        payload = parse_answer(error.read(MAX_RESPONSE_BYTES), error.code)
        detail = payload.get('error') if isinstance(payload, dict) else None
        detail = detail if isinstance(detail, dict) else {}
        raise Failure(EXIT_REFUSED, 'wb_inbox: error %d %s: %s' % (
            error.code, one_line(detail.get('code', 'unknown'), 100), one_line(detail.get('message', 'unknown'))), payload)
    except (OSError, http.client.HTTPException) as error:
        kind = type(error.reason).__name__ if isinstance(error, urllib.error.URLError) else type(error).__name__
        raise Failure(EXIT_CONNECTION, 'wb_inbox: connection error (%s)' % kind)


def parse_answer(data, status):
    try:
        return json.loads(data.decode('utf-8'))
    except (ValueError, RecursionError):
        raise Failure(EXIT_CONNECTION, 'wb_inbox: the Hub answered HTTP %d with something that is not JSON' % status)


def command_projects(settings, args):
    return api_call(settings, 'GET', '/projects', [('query', args.query)])


def read_body(args, required=True):
    """The text of ``--body`` or ``--body-file PATH|-`` (strict UTF-8), or ``None`` when neither was given."""
    if args.body is not None:
        return args.body
    if args.body_file is None:
        return None
    try:
        if args.body_file == '-':
            data = sys.stdin.buffer.read()
        else:
            with open(args.body_file, 'rb') as stream:
                data = stream.read()
        return data.decode('utf-8')
    except (OSError, UnicodeDecodeError):
        raise Failure(EXIT_USAGE, 'wb_inbox: cannot read --body-file %s as UTF-8 text' % args.body_file)


def add_body_options(parser, required):
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument('--body', help='the text')
    group.add_argument('--body-file', metavar='PATH', help='read the text from PATH, or from stdin when PATH is -')


def command_submit(settings, args):
    if args.new and args.idempotency_key is not None:
        raise Failure(EXIT_USAGE, 'wb_inbox: --new cannot be combined with --idempotency-key')
    body = read_body(args)
    if args.new:
        key = 'wbi-new-' + uuid.uuid4().hex
    elif args.idempotency_key is not None:
        key = args.idempotency_key
    else:
        digest = hashlib.sha256('\0'.join((args.project, args.title, body)).encode('utf-8')).hexdigest()
        key = 'wbi-' + digest[:32]
    report = api_call(settings, 'POST', '/requests', body={'project_id': args.project, 'title': args.title,
                                                          'body': body, 'idempotency_key': key})
    report['idempotency_key'] = key
    return report


def request_path(request_id, suffix=''):
    if request_id in ('', '.', '..'):
        raise Failure(EXIT_USAGE, 'wb_inbox: REQUEST_ID is not a request id')
    return '/requests/' + quote(request_id, safe='') + suffix


def command_list(settings, args):
    query = [('project_id', args.project)] + [('state', state) for state in args.state or []]
    query += [('include_closed', True if args.include_closed else None), ('query', args.query),
              ('limit', args.limit), ('cursor', args.cursor)]
    return api_call(settings, 'GET', '/requests', query)


def command_get(settings, args):
    return api_call(settings, 'GET', request_path(args.request_id))


def command_comment(settings, args):
    return api_call(settings, 'POST', request_path(args.request_id, '/comments'), body={'body': read_body(args)})


def command_update(settings, args):
    body = read_body(args)
    if args.title is None and body is None:
        raise Failure(EXIT_USAGE, 'wb_inbox: update needs --title and/or --body/--body-file')
    fields = {'base_version': args.base_version}
    if args.title is not None:
        fields['title'] = args.title
    if body is not None:
        fields['body'] = body
    return api_call(settings, 'PATCH', request_path(args.request_id), body=fields)


def command_set_state(settings, args):
    fields = {'base_version': args.base_version, 'to': args.to}
    if args.reason is not None:
        fields['reason'] = args.reason
    return api_call(settings, 'POST', request_path(args.request_id, '/state'), body=fields)


def command_cancel(settings, args):
    fields = {}
    if args.confirm is not None:
        fields['confirm_token'] = args.confirm
    if args.reason is not None:
        fields['reason'] = args.reason
    return api_call(settings, 'POST', request_path(args.request_id, '/cancel'), body=fields)


def command_result(settings, args):
    # A run or artifact page only exists in the full view, so naming one implies --full.
    full = args.full or args.run_id is not None or args.artifact_path is not None
    query = [('full', True if full else None), ('run_id', args.run_id), ('artifact_path', args.artifact_path),
             ('offset', args.offset), ('length', args.length)]
    return api_call(settings, 'GET', request_path(args.request_id, '/result'), query)


def build_parser():
    parser = Parser(prog='wb_inbox.py', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', metavar='command', parser_class=Parser)
    commands.required = True
    commands.add_parser('config', help='show where each setting was found (the key is only set/missing)')
    projects = commands.add_parser('projects', help='list the projects this key can see')
    projects.add_argument('--query')
    submit = commands.add_parser('submit', help='create a request (a retry of the same content is the same request)')
    submit.add_argument('--project', required=True, metavar='ID')
    submit.add_argument('--title', required=True)
    add_body_options(submit, True)
    submit.add_argument('--idempotency-key', metavar='K', help='default: derived from project, title and body')
    submit.add_argument('--new', action='store_true', help='force a new request (random idempotency key)')
    listing = commands.add_parser('list', help='list requests')
    listing.add_argument('--project', metavar='ID')
    listing.add_argument('--state', action='append', metavar='S', help='repeatable')
    listing.add_argument('--include-closed', action='store_true')
    listing.add_argument('--query')
    listing.add_argument('--limit', type=int, metavar='N')
    listing.add_argument('--cursor', metavar='C', help='next_cursor of the previous answer')
    get = commands.add_parser('get', help='show one request')
    get.add_argument('request_id', metavar='REQUEST_ID')
    comment = commands.add_parser('comment', help='add a comment to a request')
    comment.add_argument('request_id', metavar='REQUEST_ID')
    add_body_options(comment, True)
    update = commands.add_parser('update', help='edit a request (editor key)')
    update.add_argument('request_id', metavar='REQUEST_ID')
    update.add_argument('--base-version', type=int, required=True, metavar='V', help='version you read it at')
    update.add_argument('--title')
    add_body_options(update, False)
    set_state = commands.add_parser('set-state', help='move a request to NEW or NEEDS_INFO (editor key)')
    set_state.add_argument('request_id', metavar='REQUEST_ID')
    set_state.add_argument('--base-version', type=int, required=True, metavar='V')
    set_state.add_argument('--to', required=True, choices=['NEW', 'NEEDS_INFO'])
    set_state.add_argument('--reason')
    cancel = commands.add_parser('cancel', help='cancel a request; a running one needs a second call with --confirm')
    cancel.add_argument('request_id', metavar='REQUEST_ID')
    cancel.add_argument('--reason')
    cancel.add_argument('--confirm', metavar='TOKEN', help='confirm_token from the first call')
    result = commands.add_parser('result', help='show the result: a summary, the full view, or a page of a run/artifact')
    result.add_argument('request_id', metavar='REQUEST_ID')
    result.add_argument('--full', action='store_true')
    pages = result.add_mutually_exclusive_group()
    pages.add_argument('--run-id', metavar='R', help='a page of this run log (implies --full)')
    pages.add_argument('--artifact-path', metavar='P', help='a page of this artifact (implies --full)')
    result.add_argument('--offset', type=int, metavar='N', help='start of the page (default 0)')
    result.add_argument('--length', type=int, metavar='N', help='page size, 1-131072 (default 131072)')
    return parser


def emit(report):
    text = scrub(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    sys.stdout.buffer.write(text.encode('utf-8', 'replace'))
    sys.stdout.flush()


class Parser(argparse.ArgumentParser):
    """argparse quotes bad values in its messages; route everything it prints through scrub()."""

    def _print_message(self, message, file=None):
        if message:
            (file or sys.stderr).write(scrub(message))


def flush_warnings(settings):
    while settings.warnings:
        write_stderr(settings.warnings.pop(0) + '\n')


def join_confirm_token(argv):
    """argparse reads `--confirm -abc` as a missing value and `-abc` as an option. Fold the token into
    `--confirm=-abc`, which argparse accepts for any value (Hub tokens are URL-safe and may start with '-')."""
    folded, index = [], 0
    while index < len(argv):
        if argv[index] == '--confirm' and index + 1 < len(argv):
            folded.append('--confirm=' + argv[index + 1])
            index += 2
        else:
            folded.append(argv[index])
            index += 1
    return folded


def main(argv=None):
    settings = Settings(os.environ, __file__)
    key = settings.find('WB_API_KEY')[0]
    if key and len(key) >= MIN_SCRUBBED_KEY:
        SECRETS.append(key)
    flush_warnings(settings)
    args = build_parser().parse_args(join_confirm_token(sys.argv[1:] if argv is None else list(argv)))
    handler = {'config': command_config, 'projects': command_projects, 'submit': command_submit,
               'list': command_list, 'get': command_get, 'comment': command_comment,
               'update': command_update, 'set-state': command_set_state,
               'cancel': command_cancel, 'result': command_result}[args.command]
    try:
        report = handler(settings, args)
        code = EXIT_OK
    except Failure as failure:
        report, code = failure.report, failure.code
        write_stderr(failure.message + '\n')
    except Exception as error:  # a bug here must not print an exception text that could quote a secret
        report, code = None, EXIT_REFUSED
        write_stderr('wb_inbox: unexpected error (%s)\n' % type(error).__name__)
    flush_warnings(settings)
    if report is not None:
        emit(report)
    return code


if __name__ == '__main__':
    sys.exit(main())
