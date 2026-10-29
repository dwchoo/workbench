"""WBX-D2 independent adversarial verification of the Board workspace page (seams S1/S6 markup and a real headless Chrome).

Expected values come from the contract, not from the worker's tests: web-board-ux SPEC s4 "트랙 D" ("화면: workspace와
panel", WebSocket v2 client rules, 보안), BRIEF WBX-D42..D58 and AC-25/26/30/32/33/35/36/38, ticket WBX-D2.

Part 1 (``BoardFrameTests``): the served frame and the static files against each other (every id ``board.js`` asks for
exists, every import is served, CSS grid numbers equal the JS numbers, no HTML-from-text in any module).

Part 2 (``BoardWorkspaceChromeTests``): a real ``wb _hub --foreground`` with the fake Board OMP (``Hub3`` of
test_board_workspace_independent; temp HOME/XDG, short private ``XDG_RUNTIME_DIR``, ``WORKBENCH_HUB_AUTOSTART=0``) and
one headless Google Chrome driven over the DevTools protocol (aiohttp websocket; no selenium/puppeteer). Every
scenario uses its own browser context(s) with their own login session, own cookies and own localStorage. Chrome, the
Hub and every temp dir are owned and removed by identity (pid + start time, unique profile path). Skipped only when
``google-chrome`` is not installed.
Run with ``discover -s tests/hub -p 'test_web_pages_board_independent.py'``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import aiohttp

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_board_workspace_independent as ws_tests  # noqa: E402  (module import: its test classes stay uncollected)
from fixtures.web_sandbox import PASSWORD  # noqa: E402
from test_web_pages_board import BoardPageCase, Dom  # noqa: E402
from test_web_requests_hub import Browser  # noqa: E402

STATIC = REPO / 'src/workbench/hub/web/static'
BOARD_FILES = ('board.js', 'board-layout.js', 'board-panel.js', 'board-state.js', 'style-nonce.js')
CHROME = shutil.which('google-chrome')


# ======================================================================================================
# Part 1: the frame against the static files
# ======================================================================================================

class BoardFrameTests(BoardPageCase):
    async def test_every_id_board_js_asks_for_exists_exactly_once_in_the_served_page(self):
        response, dom = await self.page()
        wanted = set(re.findall(r'byId\("([^"]+)"\)', (STATIC / 'board.js').read_text()))
        self.assertGreaterEqual(len(wanted), 15)
        ids = [attrs['id'] for _, attrs in dom.elements if attrs.get('id')]
        for name in sorted(wanted):
            self.assertEqual(ids.count(name), 1, f'#{name} must exist exactly once')
        self.assertEqual(len(ids), len(set(ids)), 'no duplicate id in the page')

    async def test_frame_order_noscript_viewport_and_hidden_starts(self):
        response, dom = await self.page()
        text = response.body_text
        self.assertLess(text.index('id="board-panel"'), text.index('id="board-workspace"'), 'panel, then workspace')
        self.assertLess(text.index('id="board-workspace"'), text.index('<dialog'), 'the dialog is outside the grid')
        self.assertEqual(text.count('<dialog'), 1)
        self.assertIn('JavaScript가 필요합니다', text)
        self.assertRegex(text, r'<noscript>.*JavaScript가 필요합니다.*</noscript>')
        viewports = [attrs for attrs in dom.tags('meta') if attrs.get('name') == 'viewport']
        self.assertEqual(len(viewports), 1)
        self.assertIn('width=device-width', viewports[0]['content'], 'the 720 px rule needs a device-width viewport on phones')
        for ident in ('board-notice', 'board-options-error'):
            self.assertIn('hidden', dom.by_id(ident), f'#{ident} is hidden until there is something to say')
        toggle = dom.by_id('board-panel-toggle')
        self.assertEqual((toggle['aria-controls'], toggle['aria-expanded']), ('board-panel', 'true'))
        self.assertEqual(dom.by_id('board-confirm')['aria-labelledby'], 'board-confirm-text')
        self.assertEqual(dom.by_id('board-agent-limit')['min'], '1')
        self.assertEqual(dom.by_id('board-agent-limit')['max'], '8')
        self.assertEqual(dom.by_id('board-shell-limit')['min'], '1')
        self.assertEqual(dom.by_id('board-shell-limit')['max'], '32')

    async def test_hostile_text_that_reaches_the_frame_is_escaped_and_no_terminal_name_is_ever_rendered_by_the_server(self):
        hostile = '<script>alert(1)</script><img src=x onerror=alert(2)>'
        self.board.terminals[0]['name'] = hostile
        response, dom = await self.page()
        self.assertNotIn('alert(1)', response.body_text)
        self.assertNotIn('alert(2)', response.body_text)
        self.assertEqual(dom.tags('img'), [])
        self.board.invalid = hostile  # the registry_invalid message is the one server-rendered free text
        response, dom = await self.page()
        self.assertEqual(response.status, 503)
        self.assertNotIn('<script>alert(1)', response.body_text)
        self.assertNotIn('<img src=x', response.body_text)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', response.body_text)
        self.assertEqual(dom.tags('img'), [])
        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])
        self.assertNotIn('nonce-', response.headers['Content-Security-Policy'])
        self.assertIsNone(dom.by_id('board-workspace'), 'the 503 page has no workspace')

    async def test_every_import_of_the_page_modules_is_served_and_the_css_is_served_without_external_urls(self):
        seen = set()
        todo = ['board.js']
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            response = await self.get(f'/static/{name}')
            self.assertEqual(response.status, 200, name)
            self.assertIn('javascript', response.headers['Content-Type'], name)
            self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'], name)
            for target in re.findall(r'from\s+"\./([A-Za-z0-9_.-]+)"', response.body_text):
                todo.append(target)
        self.assertEqual(seen, set(BOARD_FILES) - set() | {'board.js'})
        css = await self.get('/static/board.css')
        self.assertEqual(css.status, 200)
        self.assertNotRegex(css.body_text, r'url\(\s*["\']?(?:https?:)?//')
        self.assertNotRegex(css.body_text, r'@import')

    def test_css_grid_numbers_equal_the_layout_module_numbers_and_the_narrow_rule_is_the_same_text(self):
        css = (STATIC / 'board.css').read_text()
        js = (STATIC / 'board-layout.js').read_text()
        cols = int(re.search(r'export const COLS = (\d+)', js).group(1))
        self.assertIn(f'grid-template-columns: repeat({cols}, minmax(0, 1fr))', css)
        self.assertIn('grid-auto-rows: 2.5rem', css, 'SPEC: row height 2.5rem')
        workspace = re.search(r'\.board-workspace \{([^}]*)\}', css).group(1)
        self.assertIn('gap: 0.5rem', workspace, 'SPEC: gap 0.5rem')
        self.assertEqual((int(re.search(r'MIN_W = (\d+)', js).group(1)), int(re.search(r'MIN_H = (\d+)', js).group(1))), (3, 6))
        query = re.search(r'export const NARROW_QUERY = "(\(max-width: [0-9.]+px\))"', js).group(1)
        self.assertEqual(query, '(max-width: 719.98px)')
        self.assertIn(f'@media {query}', css, 'CSS and JS switch at the same width (720 px, SPEC)')

    def test_no_module_builds_html_from_text_runs_eval_or_writes_style_markup_and_storage_is_only_the_three_keys(self):
        forbidden = (r'\.innerHTML\b', r'\.outerHTML\b', r'insertAdjacentHTML', r'document\.write', r'\beval\s*\(', r'new\s+Function',
                     r'setAttribute\(\s*["\']style["\']', r'\.cssText\b', r'createContextualFragment', r'DOMParser', r'srcdoc', r'\.on\w+\s*=\s*["\'`]')
        for name in BOARD_FILES:
            text = re.sub(r'(?m)^\s*//.*$|/\*.*?\*/', '', (STATIC / name).read_text(), flags=re.S)
            for pattern in forbidden:
                self.assertIsNone(re.search(pattern, text), f'{name}: {pattern}')
            self.assertEqual(re.findall(r'https?://|wss?://', text), [], name)
            self.assertNotIn('document.cookie', text, name)
            self.assertNotIn('sessionStorage', text, name)
            self.assertNotIn('indexedDB', text, name)
        used = set()
        for name in BOARD_FILES:  # (``wb.styleNonce.installed`` is a Symbol.for registry key, not a storage key)
            used |= set(re.findall(r'["\'](wb\.board\.[A-Za-z.]+)["\']', (STATIC / name).read_text()))
        self.assertEqual(used, {'wb.board.layout', 'wb.board.panel', 'wb.board.narrow'}, 'the three keys of the SPEC and nothing else')
        for name in ('board.js', 'board-panel.js', 'board-state.js'):
            self.assertNotIn('localStorage', (STATIC / name).read_text(), f'{name}: storage goes through board-layout.js only')

    def test_the_panel_fallback_sentences_are_the_hubs_own_sentences(self):
        """The page shows ``detail`` when the Hub sends it and a fixed sentence when only a reason is known: both are the
        same text (so a refusal reads the same whichever side noticed it)."""
        from workbench.hub.web.pages.board_api import DETAIL
        script = ('import {errorText} from "%s";const out={};for (const r of %s) out[r]=errorText({reason:r});'
                  'console.log(JSON.stringify(out));' % ((STATIC / 'board-panel.js').as_uri(), json.dumps(sorted(DETAIL))))
        node = shutil.which('node')
        if node is None:
            self.skipTest('node is not installed')
        result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        shown = json.loads(result.stdout)
        # only the reasons the page can raise on its own (no round trip) need a fixed sentence
        for reason in ('name_invalid', 'name_taken', 'limit_out_of_range', 'limit_below_count', 'limit_reached', 'terminal_busy',
                       'bad_input', 'confirm_required', 'wrong_kind', 'bad_state', 'unknown_terminal', 'registry_write_failed',
                       'delete_failed', 'board_unavailable', 'registry_invalid'):
            self.assertEqual(shown[reason], DETAIL[reason], reason)


# ======================================================================================================
# Part 2: real headless Chrome
# ======================================================================================================

def proc_start(pid: int):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


class Owned:
    """A process we started, signalled only while its pid and start time still match."""

    def __init__(self, process: subprocess.Popen):
        self.process, self.pid, self.start = process, process.pid, proc_start(process.pid)

    def stop(self, timeout: float = 8.0):
        if self.process.poll() is not None:
            return
        for sig, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5)):
            if proc_start(self.pid) == self.start:
                try:
                    os.killpg(self.pid, sig)
                except ProcessLookupError:
                    pass
            try:
                self.process.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue


def command_users(marker: str) -> list[int]:
    found = []
    for name in os.listdir('/proc'):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            raw = Path(f'/proc/{name}/cmdline').read_bytes()
        except OSError:
            continue
        if marker.encode() in raw:
            found.append(int(name))
    return found


INIT_SCRIPT = r"""
window.__csp = [];
document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective + '|' + e.blockedURI), true);
window.__samples = [];
window.addEventListener('pageshow', e => window.__samples.push([performance.now(), 'SHOW', e.persisted, location.pathname]));
(function sample() {
  const root = document.documentElement, body = document.body;
  window.__samples.push([performance.now(), root ? root.className : null, body ? getComputedStyle(body).visibility : null, location.pathname]);
  if (window.__samples.length < 4000) requestAnimationFrame(sample);
})();
"""


class Page:
    """One tab in its own browser context (own cookies and localStorage)."""

    def __init__(self, chrome, context_id, target_id, session_id):
        self.chrome, self.context_id, self.target_id, self.session_id = chrome, context_id, target_id, session_id
        self.errors: list[str] = []
        self.frames: list[tuple] = []
        self.ws_urls: dict[str, str] = {}
        self.ws_open: dict[str, str] = {}
        self.ws_created: list[str] = []
        self.requests: list[dict] = []
        self._load = asyncio.Event()
        chrome.listeners[session_id] = self._on_event

    async def send(self, method, **params):
        return await self.chrome.send(method, params, self.session_id)

    def _on_event(self, method, params):
        if method == 'Runtime.exceptionThrown':
            details = params['exceptionDetails']
            self.errors.append('exception: ' + str((details.get('exception') or {}).get('description') or details.get('text')))
        elif method == 'Runtime.consoleAPICalled' and params.get('type') in ('error', 'assert'):
            self.errors.append('console.error: ' + ' '.join(str(a.get('value', a.get('description'))) for a in params['args']))
        elif method == 'Log.entryAdded' and params['entry'].get('level') == 'error':
            entry = params['entry']
            self.errors.append(f"log: {entry.get('text')} {entry.get('url', '')}")
        elif method == 'Network.webSocketCreated':
            self.ws_urls[params['requestId']] = params['url']
            self.ws_open[params['requestId']] = params['url']
            self.ws_created.append(params['url'])
        elif method == 'Network.webSocketClosed':
            self.ws_open.pop(params['requestId'], None)
        elif method in ('Network.webSocketFrameSent', 'Network.webSocketFrameReceived'):
            response = params['response']
            text = response['payloadData'] if response.get('opcode') == 1 else None
            self.frames.append((self.ws_urls.get(params['requestId']), 'sent' if method.endswith('Sent') else 'recv', text,
                                response.get('payloadData') if response.get('opcode') == 2 else None))
        elif method == 'Network.requestWillBeSent':
            req = params['request']
            self.requests.append({'method': req['method'], 'url': req['url'], 'body': req.get('postData')})
        elif method == 'Page.loadEventFired':
            self._load.set()

    def sent_json(self, tid=None, kind=None):
        out = []
        for url, direction, text, _ in self.frames:
            if direction == 'sent' and text and (tid is None or (url or '').endswith('/' + tid)):
                try:
                    value = json.loads(text)
                except ValueError:
                    continue
                if kind is None or value.get('t') == kind:
                    out.append(value)
        return out

    def recv_json(self, tid=None, kind=None):
        out = []
        for url, direction, text, _ in self.frames:
            if direction == 'recv' and text and (tid is None or (url or '').endswith('/' + tid)):
                try:
                    value = json.loads(text)
                except ValueError:
                    continue
                if kind is None or value.get('t') == kind:
                    out.append(value)
        return out

    def api_posts(self, suffix=None):
        return [r for r in self.requests if r['method'] == 'POST' and '/api/board' in r['url'] and (suffix is None or r['url'].endswith(suffix))]

    async def enable(self):
        for domain in ('Page', 'Runtime', 'Log', 'Network'):
            await self.send(f'{domain}.enable')
        await self.send('Page.addScriptToEvaluateOnNewDocument', source=INIT_SCRIPT)

    async def goto(self, url, timeout=15):
        self._load.clear()
        self.ws_open.clear()  # the old document's sockets are gone with it
        await self.send('Page.navigate', url=url)
        await asyncio.wait_for(self._load.wait(), timeout)

    async def reload(self):
        self._load.clear()
        self.ws_open.clear()
        await self.send('Page.reload')
        await asyncio.wait_for(self._load.wait(), 15)

    async def eval(self, expression):
        result = await self.send('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True)
        if 'exceptionDetails' in result:
            raise AssertionError(f"eval failed: {result['exceptionDetails'].get('exception', {}).get('description')} :: {expression[:240]}")
        return result['result'].get('value')

    async def wait_for(self, expression, timeout=10, message=''):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = await self.eval(expression)
            if last:
                return last
            await asyncio.sleep(0.05)
        raise AssertionError(f'timeout waiting for {expression[:200]} (last={last!r}) {message}')

    async def viewport(self, width, height):
        await self.send('Emulation.setDeviceMetricsOverride', width=width, height=height, deviceScaleFactor=1, mobile=False)

    async def mouse(self, kind, x, y, buttons=0, button='none'):
        await self.send('Input.dispatchMouseEvent', type=kind, x=x, y=y, button=button, buttons=buttons,
                        clickCount=1 if kind != 'mouseMoved' else 0)

    async def drag(self, x, y, tx, ty, steps=6):
        await self.mouse('mouseMoved', x, y)
        await self.mouse('mousePressed', x, y, buttons=1, button='left')
        for i in range(1, steps + 1):
            await self.mouse('mouseMoved', x + (tx - x) * i / steps, y + (ty - y) * i / steps, buttons=1, button='left')
        await self.mouse('mouseReleased', tx, ty, buttons=0, button='left')
        await asyncio.sleep(0.05)

    async def click(self, x, y):
        await self.mouse('mouseMoved', x, y)
        await self.mouse('mousePressed', x, y, buttons=1, button='left')
        await self.mouse('mouseReleased', x, y, buttons=0, button='left')

    async def key(self, key, code='', vk=0):
        params = dict(key=key, code=code or key, windowsVirtualKeyCode=vk)
        await self.send('Input.dispatchKeyEvent', type='rawKeyDown', **params)
        await self.send('Input.dispatchKeyEvent', type='keyUp', **params)

    async def type_text(self, text):
        for char in text:
            await self.send('Input.dispatchKeyEvent', type='keyDown', key=char, text=char)
            await self.send('Input.dispatchKeyEvent', type='keyUp', key=char)

    async def screenshot(self, path):
        data = await self.send('Page.captureScreenshot', format='png')
        Path(path).write_bytes(base64.b64decode(data['data']))

    async def close(self):
        self.chrome.listeners.pop(self.session_id, None)
        for method, params in (('Target.closeTarget', {'targetId': self.target_id}),
                               ('Target.disposeBrowserContext', {'browserContextId': self.context_id})):
            try:
                await self.chrome.send(method, params)
            except Exception:
                pass


class Chrome:
    def __init__(self):
        self.profile = tempfile.mkdtemp(prefix='d2chrome')
        self.process = None
        self.owned = None
        self.session = None
        self.ws = None
        self.reader = None
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.listeners: dict = {}
        self.pages: list[Page] = []

    async def start(self):
        self.process = subprocess.Popen(
            [CHROME, '--headless=new', '--remote-debugging-port=0', f'--user-data-dir={self.profile}', '--no-first-run',
             '--disable-gpu', '--no-sandbox', '--disable-extensions', '--disable-background-networking',
             '--no-default-browser-check', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.owned = Owned(self.process)
        active = Path(self.profile) / 'DevToolsActivePort'
        for _ in range(200):
            if active.exists() and len(active.read_text().split()) >= 2:
                break
            await asyncio.sleep(0.1)
        port, path = active.read_text().split()[:2]
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(f'ws://127.0.0.1:{port}{path}', max_msg_size=0)
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for message in self.ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(message.data)
            if 'id' in data:
                future = self.pending.pop(data['id'], None)
                if future and not future.done():
                    if 'error' in data:
                        future.set_exception(RuntimeError(str(data['error'])))
                    else:
                        future.set_result(data.get('result', {}))
            elif 'method' in data:
                listener = self.listeners.get(data.get('sessionId'))
                if listener:
                    listener(data['method'], data.get('params', {}))

    async def send(self, method, params=None, session_id=None):
        self.next_id += 1
        future = asyncio.get_event_loop().create_future()
        self.pending[self.next_id] = future
        message = {'id': self.next_id, 'method': method, 'params': params or {}}
        if session_id:
            message['sessionId'] = session_id
        await self.ws.send_str(json.dumps(message))
        return await asyncio.wait_for(future, 30)

    async def new_page(self, base, cookie, viewport=(1400, 1000), init=None):
        context = (await self.send('Target.createBrowserContext', {'disposeOnDetach': False}))['browserContextId']
        target = (await self.send('Target.createTarget', {'url': 'about:blank', 'browserContextId': context}))['targetId']
        session = (await self.send('Target.attachToTarget', {'targetId': target, 'flatten': True}))['sessionId']
        page = Page(self, context, target, session)
        await page.enable()
        if init:
            await page.send('Page.addScriptToEvaluateOnNewDocument', source=init)
        if cookie:
            await page.send('Network.setCookie', name='wb_hub_session', value=cookie, url=base + '/', httpOnly=True)
        await page.viewport(*viewport)
        self.pages.append(page)
        return page

    async def close(self):
        for page in list(self.pages):
            await page.close()
        if self.reader:
            self.reader.cancel()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self.session is not None:
            await self.session.close()
        if self.owned is not None:
            self.owned.stop()
        left = command_users(self.profile)
        for pid in left:  # renderer or gpu helpers that outlived the group kill: they carry our unique profile path
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(self.profile, True)
        return left


WIDGETS_JS = """(() => {
  const ws = document.getElementById('board-workspace').getBoundingClientRect();
  const widgets = [...document.querySelectorAll('.board-widget')].map(e => {
    const r = e.getBoundingClientRect();
    const col = e.style.getPropertyValue('grid-column').match(/(\\d+) \\/ span (\\d+)/), row = e.style.getPropertyValue('grid-row').match(/(\\d+) \\/ span (\\d+)/);
    return {id: e.dataset.terminal, name: e.querySelector('.widget-name').textContent, x: r.x, y: r.y, w: r.width, h: r.height,
      cx: col ? col[1] - 1 : null, cw: col ? +col[2] : null, cy: row ? row[1] - 1 : null, ch: row ? +row[2] : null,
      others: e.querySelector('.widget-others').textContent, status: e.querySelector('.widget-status').textContent,
      focused: e.classList.contains('is-focused'), transform: e.style.getPropertyValue('transform'),
      cover: e.querySelector('.widget-cover').hidden ? '' : e.querySelector('.cover-text').textContent};
  });
  const rows = [...document.querySelectorAll('li.panel-row')].map(r => ({id: r.dataset.terminal, name: r.querySelector('.panel-name') ? r.querySelector('.panel-name').textContent : null,
    toggle: [...r.querySelectorAll('button')].map(b => b.textContent)}));
  let ls = null; try { ls = localStorage.getItem('wb.board.layout'); } catch (e) { ls = 'THROWS'; }
  return {ws: {x: ws.x, y: ws.y, w: ws.width, h: ws.height}, widgets, rows, ls, narrow: document.getElementById('board-shell').classList.contains('board-narrow'),
    scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth,
    preview: (() => { const p = document.querySelector('.drop-preview'); return p ? {hidden: p.hidden, bad: p.classList.contains('is-bad'), text: p.textContent} : null; })(),
    panelHidden: document.getElementById('board-panel').hidden};
})()"""


def pairwise_overlap(rects):
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            if a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]:
                return a, b
    return None


@unittest.skipUnless(CHROME, 'google-chrome is not installed')
class BoardWorkspaceChromeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        uid = os.getuid()
        cls.omp_dir = Path(f'/tmp/omp-workbench-{uid}')
        cls.omp_before = len(os.listdir(cls.omp_dir)) if cls.omp_dir.exists() else None
        cls.loop = asyncio.new_event_loop()
        cls.shim = unittest.TestCase()
        cls.hub = ws_tests.Hub3(cls.shim, shell_script='exec /bin/sh\n')
        cls.base = f'http://{cls.hub.host}:{cls.hub.port}'
        cls.chrome = Chrome()
        cls.sessions: list[Browser] = []
        try:
            cls.loop.run_until_complete(cls.chrome.start())
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        left = []
        try:
            left = cls.loop.run_until_complete(cls.chrome.close())
        except Exception:
            pass
        cls.loop.close()
        cls.shim.doCleanups()
        cls.chrome_left = left
        if cls.omp_before is not None:
            after = len(os.listdir(cls.omp_dir)) if cls.omp_dir.exists() else 0
            assert after == cls.omp_before, f'/tmp/omp-workbench-{os.getuid()} entries {cls.omp_before} -> {after}'

    # ---- helpers ---------------------------------------------------------------------------------------------
    def run_async(self, coro):
        return self.loop.run_until_complete(coro)

    def login_cookie(self) -> str:
        """A fresh login session (own session id, as another device would have)."""
        browser = Browser(self.hub.host, self.hub.port)
        _, _, page = browser.request('GET', '/login')
        token = re.search(r'name="login_token" value="([^"]+)"', page).group(1)
        status, _, _ = browser.request('POST', '/login', {'login_token': token, 'password': PASSWORD})
        self.assertEqual(status, 303)
        self.sessions.append(browser)
        return browser.cookies['wb_hub_session']

    async def page(self, viewport=(1400, 1000), init=None, cookie=None) -> Page:
        page = await self.chrome.new_page(self.base, cookie or self.login_cookie(), viewport, init)
        return page

    async def open_board(self, **kwargs) -> Page:
        page = await self.page(**kwargs)
        await page.goto(self.base + '/board')
        await page.wait_for("document.querySelectorAll('li.panel-row').length > 0", 10)
        await asyncio.sleep(0.4)
        return page

    def api(self, method, path, body=None):
        return self.hub.api(method, path, body)

    def reset_board(self):
        """Back to one agent (agent-1) and one shell (shell-1), default limits, everything running."""
        self.api('POST', '/api/board/options', {'agent_limit': 4, 'shell_limit': 10})
        terminals = self.hub.terminals()
        agents = [t for t in terminals if t['kind'] == 'agent']
        self.api('POST', '/api/board/open', {'visible': [agents[0]['id']]})  # this run's first open makes shell-1
        terminals = self.hub.terminals()
        shells = [t for t in terminals if t['kind'] == 'shell']
        if not shells:
            shells = [self.create('shell')]
        for agent in agents[1:]:
            self.api('POST', f"/api/board/terminals/{agent['id']}/delete", {'confirm': True})
        for shell in shells[1:]:
            self.api('POST', f"/api/board/terminals/{shell['id']}/close", {'confirm': True})
        self.api('POST', f"/api/board/terminals/{agents[0]['id']}/rename", {'name': 'agent-1'})
        self.api('POST', f"/api/board/terminals/{shells[0]['id']}/rename", {'name': 'shell-1'})
        for terminal in (agents[0], shells[0]):
            if self.named(terminal['id'])['state'] not in ('running', 'starting'):
                action = 'start' if terminal['kind'] == 'agent' and self.named(terminal['id'])['state'] in ('stopped', 'not_started') else 'restart'
                self.api('POST', f"/api/board/terminals/{terminal['id']}/{action}", {'confirm': True})
        ws_tests.wait_until(lambda: all(t['state'] == 'running' for t in self.hub.terminals()), 15)

    def named(self, terminal_id):
        return next(t for t in self.hub.terminals() if t['id'] == terminal_id)

    def create(self, kind):
        status, reply = self.api('POST', f'/api/board/{kind}s', {})
        self.assertEqual(status, 201, reply)
        return reply['terminal']

    def setUp(self):
        self.reset_board()
        self.opened: list[Page] = []

    def tearDown(self):
        async def close_all():
            for page in self.opened:
                await page.close()
                if page in self.chrome.pages:
                    self.chrome.pages.remove(page)
        self.run_async(close_all())

    async def board(self, **kwargs) -> Page:
        page = await self.open_board(**kwargs)
        self.opened.append(page)
        return page

    async def snap(self, page):
        return await page.eval(WIDGETS_JS)

    async def clean(self, page, label='', refused_api_calls=False, session_ended=False):
        """No CSP violation, no uncaught exception, no console error. ``refused_api_calls``: the scenario made the Hub answer a
        4xx to the page on purpose; Chrome logs those as 'Failed to load resource' (not an app error) and only those are skipped."""
        csp = await page.eval('window.__csp')
        self.assertEqual(csp, [], f'{label}: CSP violations')
        errors = page.errors
        if refused_api_calls:
            errors = [e for e in errors if not re.match(r'log: Failed to load resource: the server responded with a status of 4\d\d .*/api/board/', e)]
        if session_ended:  # Back/Forward scenario: Chrome logs the refused requests, the refused handshake and a socket still open (release sent, the Hub closes it) when the page enters the bfcache
            errors = [e for e in errors if not re.match(r'log: Failed to load resource: the server responded with a status of 401 ', e)
                      and not re.match(r"log: WebSocket connection to 'ws://127\.0\.0\.1:\d+/ws/board/[a-z0-9-]+' failed: (HTTP Authentication failed|Page entered Back-Forward Cache)", e)]
        self.assertEqual(errors, [], f'{label}: uncaught errors / console errors')

    def assert_geometry(self, snapshot, label, stored_valid=True):
        """No two widgets overlap on screen or in cells, all inside the workspace, the stored layout (if any) is valid."""
        widgets = snapshot['widgets']
        ws = snapshot['ws']
        self.assertIsNone(pairwise_overlap([(w['x'] + 0.6, w['y'] + 0.6, w['w'] - 1.2, w['h'] - 1.2) for w in widgets]), f'{label}: screen overlap {widgets}')
        for w in widgets:
            self.assertGreaterEqual(w['x'], ws['x'] - 1, f'{label}: left of the grid')
            self.assertLessEqual(w['x'] + w['w'], ws['x'] + ws['w'] + 1, f'{label}: right of the grid')
            if not snapshot['narrow']:
                self.assertTrue(w['cw'] >= 3 and w['ch'] >= 6 and w['cx'] >= 0 and w['cy'] >= 0 and w['cx'] + w['cw'] <= 12, f'{label}: cells {w}')
        if not snapshot['narrow']:
            self.assertIsNone(pairwise_overlap([(w['cx'], w['cy'], w['cw'], w['ch']) for w in widgets]), f'{label}: cell overlap')
            if stored_valid and snapshot['ls'] not in (None, 'THROWS'):
                stored = json.loads(snapshot['ls'])
                visible = [e for e in stored['widgets'].values() if e.get('visible')]
                self.assertIsNone(pairwise_overlap([(e['x'], e['y'], e['w'], e['h']) for e in visible]), f'{label}: stored overlap')
                for e in visible:
                    self.assertTrue(e['w'] >= 3 and e['h'] >= 6 and e['x'] >= 0 and e['y'] >= 0 and e['x'] + e['w'] <= 12, f'{label}: stored {e}')

    @staticmethod
    def since_restore(samples):
        """The frame samples of the document as shown by the last bfcache restore (all of them for a freshly loaded document)."""
        marks = [i for i, item in enumerate(samples) if item[1] == 'SHOW' and item[2] is True]
        return samples[marks[-1] + 1:] if marks else [item for item in samples if item[1] != 'SHOW']

    @staticmethod
    def pitch(snapshot):
        """The true column pitch (cell + gap) and row pitch in px, from the rendered widgets."""
        for w in snapshot['widgets']:
            if w['cw']:
                col = (w['w'] - (w['cw'] - 1) * 8) / w['cw']
                row = (w['h'] - (w['ch'] - 1) * 8) / w['ch']
                return col + 8, row + 8
        raise AssertionError('no widget')

    async def press(self, page, tid, part):
        """Centre of a widget's title bar name or resize handle, in page coordinates."""
        point = await page.eval(f"""(() => {{ const e = document.querySelector('.board-widget[data-terminal="{tid}"]');
          const t = e.querySelector({json.dumps('.widget-name' if part == 'bar' else '.widget-handle')}).getBoundingClientRect();
          return [t.x + t.width / 2, t.y + t.height / 2]; }})()""")
        return point

    async def panel_click(self, page, tid, label):
        found = await page.eval(f"""(() => {{ const r = document.querySelector('li.panel-row[data-terminal="{tid}"]');
          const b = r && [...r.querySelectorAll('button')].find(b => b.textContent === {json.dumps(label)}); if (b) b.click(); return !!b; }})()""")
        self.assertTrue(found, f'panel button {label} of {tid}')

    # ---- scenarios -------------------------------------------------------------------------------------------
    def test_01_boot_shows_every_terminal_as_a_widget_with_no_errors_csp_violations_or_overlap(self):
        async def go():
            page = await self.board()
            snap = await self.snap(page)
            self.assertEqual([w['name'] for w in snap['widgets']], ['agent-1', 'shell-1'])
            self.assert_geometry(snap, 'boot')
            self.assertEqual(sorted(snap['rows'][i]['name'] for i in range(2)), ['agent-1', 'shell-1'])
            await page.wait_for("document.querySelectorAll('.xterm').length === 2")
            # one socket each, v2 hello with the widget's own size, and the Hub counts one connection each
            self.assertEqual(sorted(u.rsplit('/', 1)[1] for u in page.ws_created), sorted(t['id'] for t in self.hub.terminals()))
            for hello in page.sent_json(kind='hello'):
                self.assertEqual(hello['v'], 2)
                self.assertTrue(all(isinstance(n, int) and 1 <= n <= 500 for n in hello['size']))
            self.assertTrue(ws_tests.wait_until(lambda: all(t['clients'] == 1 for t in self.hub.terminals()), 8))
            for w in snap['widgets']:  # the body fills the widget: the terminal is drawn, not collapsed
                self.assertGreater(w['w'], 200)
                self.assertGreater(w['h'], 200)
            await self.clean(page, 'boot')
        self.run_async(go())

    def test_02_fuzz_200_random_drags_resizes_and_visibility_changes_never_overlap_or_leave_the_grid(self):
        async def go():
            self.create('agent')
            for _ in range(3):
                self.create('shell')
            page = await self.board(viewport=(1400, 2600))
            snap = await self.snap(page)
            self.assertEqual(len(snap['widgets']), 6)
            rnd = random.Random(20261029)
            outcomes = {'moved': 0, 'resized': 0, 'reverted': 0, 'noop': 0, 'toggled': 0}
            for step in range(200):
                snap = await self.snap(page)
                visible = snap['widgets']
                kind = rnd.choices(['move', 'resize', 'toggle'], [10, 8, 2])[0]
                label = f'step {step} {kind}'
                if kind == 'toggle' or not visible:
                    row = rnd.choice(snap['rows'])
                    await self.panel_click(page, row['id'], '숨기기' if '숨기기' in row['toggle'] else '보이기')
                    outcomes['toggled'] += 1
                else:
                    w = rnd.choice(visible)
                    px, py = await self.pitch_of(page, snap)
                    before = (w['cx'], w['cy'], w['cw'], w['ch'])
                    x, y = await self.press(page, w['id'], 'bar' if kind == 'move' else 'handle')
                    dx, dy = rnd.randint(-5, 5), rnd.randint(-8, 14)
                    dx = max(min(dx, int((1385 - x) / px)), -int((x - 15) / px))  # the pointer stays inside the page
                    dy = max(min(dy, int((2570 - y) / py)), -int((y - 80) / py))
                    tx = x + dx * px + rnd.uniform(-0.3, 0.3) * px
                    ty = y + dy * py + rnd.uniform(-0.3, 0.3) * py
                    await page.drag(x, y, tx, ty, steps=rnd.randint(2, 7))
                    after_snap = await self.snap(page)
                    after_w = next(a for a in after_snap['widgets'] if a['id'] == w['id'])
                    after = (after_w['cx'], after_w['cy'], after_w['cw'], after_w['ch'])
                    self.assertEqual(after_w['transform'], '', f'{label}: drag transform left behind')
                    self.assertEqual(await page.eval("document.querySelector('.drop-preview').hidden"), True, f'{label}: preview left behind')
                    self.assertEqual(await page.eval("document.querySelectorAll('.is-dragging, .is-resizing').length"), 0, label)
                    if after == before:  # a refused (overlapping), clamped-to-nothing or zero-cell drop: back at the last valid place
                        outcomes['noop'] += 1
                    else:
                        # contract: whole cells; a move keeps the size, a resize keeps the place; the pointer's distance in nearest
                        # cells, clamped to the 12 columns, rows >= 0 and the 3 x 6 minimum
                        cx, cy, cw, ch = before
                        if kind == 'move':
                            expected = (min(12 - cw, max(0, cx + dx)), max(0, cy + dy), cw, ch)
                            outcomes['moved'] += 1
                        else:
                            expected = (cx, cy, min(12 - cx, max(3, cw + dx)), max(6, ch + dy))
                            outcomes['resized'] += 1
                        self.assertEqual(after, expected, f'{label}: {before} by ({dx},{dy}) cells')
                snap = await self.snap(page)
                self.assert_geometry(snap, label)
                # the connections follow the visible widgets
                visible_ids = sorted(w['id'] for w in snap['widgets'])
                ok = await self.wait_open_sockets(page, visible_ids, 4)
                self.assertTrue(ok, f'{label}: open sockets {sorted(u.rsplit("/", 1)[1] for u in page.ws_open.values())} vs visible {visible_ids}')
            self.assertGreater(outcomes['moved'], 15, outcomes)
            self.assertGreater(outcomes['resized'], 10, outcomes)
            await self.clean(page, 'fuzz')
        self.run_async(go())

    async def pitch_of(self, page, snap):
        for w in snap['widgets']:
            if w['cw']:
                return (w['w'] - (w['cw'] - 1) * 8) / w['cw'] + 8, (w['h'] - (w['ch'] - 1) * 8) / w['ch'] + 8
        return 80.0, 48.0

    async def wait_open_sockets(self, page, ids, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if sorted(u.rsplit('/', 1)[1] for u in page.ws_open.values()) == ids:
                return True
            await asyncio.sleep(0.05)
        return False

    def test_03_deterministic_drag_resize_touching_overlap_revert_minimum_and_preview(self):
        async def go():
            self.create('agent')
            self.create('shell')
            page = await self.board(viewport=(1400, 2000))
            await self.panel_click(page, self.hub.terminals()[0]['id'], '숨기기')  # make sure a stored layout exists later
            await self.panel_click(page, self.hub.terminals()[0]['id'], '보이기')
            await page.eval("document.getElementById('board-reset-layout').click()")
            snap = await self.snap(page)
            order = [t['id'] for t in self.hub.terminals()]  # agents first, as the Hub lists them
            cells = {w['id']: (w['cx'], w['cy'], w['cw'], w['ch']) for w in snap['widgets']}
            self.assertEqual([cells[i] for i in order], [(0, 0, 6, 10), (6, 0, 6, 10), (0, 10, 6, 10), (6, 10, 6, 10)], 'reset: agents first, then shells, two per row')
            px, py = await self.pitch_of(page, snap)
            a, b, c, d = order

            async def cells_of(tid):
                s = await self.snap(page)
                w = next(x for x in s['widgets'] if x['id'] == tid)
                return (w['cx'], w['cy'], w['cw'], w['ch'])

            before_ls = (await self.snap(page))['ls']
            # (1) a drop that shares cells with b goes back to the exact last valid place and stores nothing new
            x, y = await self.press(page, a, 'bar')
            await page.drag(x, y, x + 2 * px, y)
            self.assertEqual(await cells_of(a), (0, 0, 6, 10), 'overlap: back where it was')
            self.assertEqual((await self.snap(page))['ls'], before_ls)
            # (2) preview while dragging: "놓을 수 없음" and bad class over b, clean again after the drop
            await page.mouse('mouseMoved', x, y)
            await page.mouse('mousePressed', x, y, buttons=1, button='left')
            await page.mouse('mouseMoved', x + 2 * px, y, buttons=1, button='left')
            mid = await self.snap(page)
            self.assertEqual(mid['preview'], {'hidden': False, 'bad': True, 'text': '놓을 수 없음'})
            await page.mouse('mouseMoved', x, y + 4 * py + 0, buttons=1, button='left')  # still over c (rows 4..13 vs c at 10): overlap
            await page.mouse('mouseMoved', x, y, buttons=1, button='left')
            await page.mouse('mouseReleased', x, y, buttons=0, button='left')
            await asyncio.sleep(0.05)
            self.assertEqual((await self.snap(page))['preview']['hidden'], True)
            # (3) the nearest cell: 0.4 cell is no move, 0.6 cell is one (d is the only widget with a free left neighbour? no: use a column step on a free row)
            xd, yd = await self.press(page, d, 'bar')
            await page.drag(xd, yd, xd, yd + 12 * py)  # d to row 22: free
            self.assertEqual(await cells_of(d), (6, 22, 6, 10))
            xd, yd = await self.press(page, d, 'bar')
            await page.drag(xd, yd, xd - 0.4 * px, yd)
            self.assertEqual(await cells_of(d), (6, 22, 6, 10), '0.4 cell rounds to nothing')
            xd, yd = await self.press(page, d, 'bar')
            await page.drag(xd, yd, xd - 0.6 * px, yd)
            self.assertEqual(await cells_of(d), (5, 22, 6, 10), '0.6 cell rounds to one cell')
            # (4) touching is not overlapping: b moved to sit exactly under a? a is rows 0-9, c rows 10-19 at x 0: put d at (6, 20) next to c's bottom
            xd, yd = await self.press(page, d, 'bar')
            await page.drag(xd, yd, xd + px, yd - 2 * py)  # (6, 20): touches b's bottom? b is rows 0-9, d at row 20: free; touches c? c ends at row 20, x 0-5; d x 6-11: free
            self.assertEqual(await cells_of(d), (6, 20, 6, 10))
            xd, yd = await self.press(page, d, 'bar')
            await page.drag(xd, yd, xd, yd - 10 * py)  # (6, 10): directly under b (b ends at row 10) and right of c (c ends at col 6): touching both
            self.assertEqual(await cells_of(d), (6, 10, 6, 10), 'touching edges are not an overlap')
            # (5) resize: the minimum is 3 x 6, the right edge is column 12, an overlapping size goes back
            xh, yh = await self.press(page, d, 'handle')
            await page.drag(xh, yh, xh - 20 * px, yh - 20 * py)
            self.assertEqual(await cells_of(d), (6, 10, 3, 6), 'minimum 3 x 6')
            xh, yh = await self.press(page, d, 'handle')
            await page.drag(xh, yh, xh + 20 * px, yh + 20 * py)
            got = await cells_of(d)
            self.assertEqual(got[:3], (6, 10, 6), 'the right edge stops at column 12')
            self.assertGreater(got[3], 6)
            xh, yh = await self.press(page, a, 'handle')
            await page.drag(xh, yh, xh + 3 * px, yh)  # a: 6 -> 9 columns: into b
            self.assertEqual(await cells_of(a), (0, 0, 6, 10), 'a resize into a neighbour goes back')
            # (6) the layout is stored in this browser and survives a reload exactly
            stored = (await self.snap(page))['ls']
            self.assertIsNotNone(stored)
            parsed = json.loads(stored)
            self.assertEqual(parsed['v'], 1)
            self.assertEqual(set(parsed['widgets'][a]), {'x', 'y', 'w', 'h', 'visible'})
            current = {w['id']: (w['cx'], w['cy'], w['cw'], w['ch']) for w in (await self.snap(page))['widgets']}
            await page.reload()
            await page.wait_for("document.querySelectorAll('.board-widget').length === 4")
            after = {w['id']: (w['cx'], w['cy'], w['cw'], w['ch']) for w in (await self.snap(page))['widgets']}
            self.assertEqual(after, current, 'reload: the same cells')
            # (7) focus: a click on a widget highlights it and gives its terminal the keyboard; only one widget is highlighted
            xb, yb = await self.press(page, b, 'bar')
            await page.click(xb, yb + 60)
            focused = [w['id'] for w in (await self.snap(page))['widgets'] if w['focused']]
            self.assertEqual(focused, [b])
            self.assertTrue(await page.eval(f"document.activeElement && document.activeElement.closest('.board-widget') && document.activeElement.closest('.board-widget').dataset.terminal === '{b}'"))
            xc, yc = await self.press(page, c, 'bar')
            await page.click(xc, yc + 60)
            self.assertEqual([w['id'] for w in (await self.snap(page))['widgets'] if w['focused']], [c])
            await self.clean(page, 'deterministic')
        self.run_async(go())

    def test_04_stored_layouts_corrupt_foreign_oversized_and_broken_storage(self):
        async def go():
            self.create('shell')
            ids = [t['id'] for t in self.hub.terminals()]
            page = await self.board()
            default = {w['id']: (w['cx'], w['cy'], w['cw'], w['ch']) for w in (await self.snap(page))['widgets']}
            self.assertEqual(len(default), 3)
            overlapping = json.dumps({'v': 1, 'widgets': {ids[0]: {'x': 0, 'y': 0, 'w': 6, 'h': 10, 'visible': True}, ids[1]: {'x': 2, 'y': 2, 'w': 6, 'h': 10, 'visible': True}, ids[2]: {'x': 50, 'y': 0, 'w': 6, 'h': 10, 'visible': True}}})
            junk = [('{', 'not json'), ('x' * 3_000_000, 'oversized text'), (json.dumps({'v': 2, 'widgets': {ids[0]: {'x': 0, 'y': 0, 'w': 6, 'h': 10, 'visible': False}}}), 'foreign version'),
                    ('null', 'null'), ('[]', 'array'), (overlapping, 'overlap and out of grid'),
                    (json.dumps({'v': 1, 'widgets': {i: {'x': 'a', 'y': None, 'w': 1e9, 'h': -4, 'visible': 'yes'} for i in ids}}), 'nonsense numbers')]
            for text, label in junk:
                await page.eval(f"localStorage.setItem('wb.board.layout', {json.dumps(text)})")
                page.errors.clear()
                await page.reload()
                await page.wait_for("document.querySelectorAll('li.panel-row').length === 3")
                await asyncio.sleep(0.3)
                snap = await self.snap(page)
                self.assertEqual(len(snap['widgets']), 3, f'{label}: every terminal is visible again')
                self.assert_geometry(snap, label, stored_valid=False)  # the junk text is left alone until the owner changes something
                await self.clean(page, label)
            # stored hidden entries stay hidden; a hidden one is listed in the panel with 보이기
            hidden = json.dumps({'v': 1, 'widgets': {ids[0]: {'x': 0, 'y': 0, 'w': 6, 'h': 10, 'visible': True}, ids[1]: {'x': 6, 'y': 0, 'w': 6, 'h': 10, 'visible': False}, ids[2]: {'x': 0, 'y': 10, 'w': 6, 'h': 10, 'visible': True}}})
            await page.eval(f"localStorage.setItem('wb.board.layout', {json.dumps(hidden)})")
            await page.reload()
            await page.wait_for("document.querySelectorAll('li.panel-row').length === 3")
            snap = await self.snap(page)
            self.assertEqual(sorted(w['id'] for w in snap['widgets']), sorted([ids[0], ids[2]]))
            self.assertIn('보이기', next(r for r in snap['rows'] if r['id'] == ids[1])['toggle'])
            self.assertTrue(await self.wait_open_sockets(page, sorted([ids[0], ids[2]]), 5), 'a hidden widget has no connection')
            # the Hub saw the hidden terminal's connection count at 0
            self.assertEqual(self.named(ids[1])['clients'], 0)
            # panel closed state and narrow choice are remembered per browser too
            await page.eval("document.getElementById('board-panel-toggle').click()")
            self.assertEqual(await page.eval("localStorage.getItem('wb.board.panel')"), 'closed')
            await page.reload()
            await page.wait_for("document.querySelectorAll('li.panel-row').length === 3")
            self.assertTrue((await self.snap(page))['panelHidden'])
            await page.eval("localStorage.setItem('wb.board.panel', 'banana')")
            await page.reload()
            await page.wait_for("document.querySelectorAll('li.panel-row').length === 3")
            self.assertFalse((await self.snap(page))['panelHidden'], 'a corrupt panel value means open')
            await self.clean(page, 'panel state')
        self.run_async(go())

    def test_05_storage_that_throws_or_is_full_still_gives_a_working_default_workspace(self):
        async def go():
            for label, init in (
                    ('property throws', "Object.defineProperty(window, 'localStorage', {get() { throw new DOMException('denied', 'SecurityError'); }});"),
                    ('getItem and setItem throw', "Storage.prototype.getItem = function () { throw new DOMException('denied', 'SecurityError'); }; Storage.prototype.setItem = function () { throw new DOMException('full', 'QuotaExceededError'); };"),
                    ('getItem returns garbage', "Storage.prototype.getItem = function () { return {a: 1}; }; Storage.prototype.setItem = function () { throw new DOMException('full', 'QuotaExceededError'); };")):
                page = await self.board(init=init)
                snap = await self.snap(page) if label != 'property throws' else await page.eval(WIDGETS_JS.replace("let ls = null; try { ls = localStorage.getItem('wb.board.layout'); } catch (e) { ls = 'THROWS'; }", "let ls = 'THROWS';"))
                self.assertEqual(len(snap['widgets']), 2, f'{label}: the default layout shows every terminal')
                self.assertFalse(snap['panelHidden'], label)
                # drag, hide and show work for the session even though nothing can be stored
                a = snap['widgets'][0]
                x, y = await self.press(page, a['id'], 'bar')
                await page.drag(x, y, x, y + 800)
                moved = next(w for w in (await page.eval(WIDGETS_JS.replace("let ls = null; try { ls = localStorage.getItem('wb.board.layout'); } catch (e) { ls = 'THROWS'; }", "let ls = 'THROWS';")))['widgets'] if w['id'] == a['id'])
                self.assertGreater(moved['cy'], 0, f'{label}: the drag still works')
                await page.eval("document.getElementById('board-panel-toggle').click()")
                self.assertTrue(await page.eval("document.getElementById('board-panel').hidden"), label)
                await self.panel_click(page, snap['widgets'][1]['id'], '숨기기') if not await page.eval("document.getElementById('board-panel').hidden") else None
                await page.eval("document.getElementById('board-panel-toggle').click()")
                await self.panel_click(page, snap['widgets'][1]['id'], '숨기기')
                await page.wait_for("document.querySelectorAll('.board-widget').length === 1")
                await self.panel_click(page, snap['widgets'][1]['id'], '보이기')
                await page.wait_for("document.querySelectorAll('.board-widget').length === 2")
                await self.clean(page, label)
        self.run_async(go())

    def test_06_terminals_added_and_removed_by_another_browser_are_reconciled(self):
        async def go():
            page = await self.board()
            first = await self.snap(page)
            # no stored layout in this browser: a terminal made elsewhere appears, visible, within the 5 s refresh
            other = self.create('shell')
            await page.wait_for(f"!!document.querySelector('.board-widget[data-terminal=\"{other['id']}\"]')", 9, 'the 5 s poll')
            self.assertEqual(len((await self.snap(page))['widgets']), 3)
            # the owner changes the layout (so this browser now has its own); then another terminal appears: hidden
            await self.panel_click(page, first['widgets'][0]['id'], '숨기기')
            await self.panel_click(page, first['widgets'][0]['id'], '보이기')
            before = {w['id']: (w['cx'], w['cy'], w['cw'], w['ch']) for w in (await self.snap(page))['widgets']}
            stored_before = (await self.snap(page))['ls']
            newest = self.create('shell')
            await page.wait_for(f"!!document.querySelector('li.panel-row[data-terminal=\"{newest['id']}\"]')", 9)
            snap = await self.snap(page)
            self.assertNotIn(newest['id'], [w['id'] for w in snap['widgets']], 'new elsewhere + stored layout: hidden')
            self.assertIn('보이기', next(r for r in snap['rows'] if r['id'] == newest['id'])['toggle'])
            self.assertEqual({w['id']: (w['cx'], w['cy'], w['cw'], w['ch']) for w in snap['widgets']}, before, 'the layout is not disturbed')
            self.assertEqual(snap['ls'], stored_before, 'merely seeing a new terminal rewrites nothing')
            self.assertEqual(self.named(newest['id'])['clients'], 0)
            # it can be shown from the panel: placed in a free spot, no overlap, one more connection
            await self.panel_click(page, newest['id'], '보이기')
            snap = await self.snap(page)
            self.assertIn(newest['id'], [w['id'] for w in snap['widgets']])
            self.assert_geometry(snap, 'shown from the panel')
            # another browser closes a terminal: its widget, row and connection go; the stored entry is dropped on the next save
            self.api('POST', f"/api/board/terminals/{other['id']}/close", {'confirm': True})
            await page.wait_for(f"!document.querySelector('.board-widget[data-terminal=\"{other['id']}\"]')", 9)
            snap = await self.snap(page)
            self.assertNotIn(other['id'], [r['id'] for r in snap['rows']])
            self.assertTrue(await self.wait_open_sockets(page, sorted(w['id'] for w in snap['widgets']), 5))
            await self.panel_click(page, first['widgets'][0]['id'], '숨기기')
            await self.panel_click(page, first['widgets'][0]['id'], '보이기')
            self.assertNotIn(other['id'], json.loads((await self.snap(page))['ls'])['widgets'], 'a vanished terminal is dropped from the stored layout')
            # the other browser's own agent delete (a stopped, then deleted agent): same
            agent = self.create('agent')
            await page.wait_for(f"!!document.querySelector('li.panel-row[data-terminal=\"{agent['id']}\"]')", 9)
            self.api('POST', f"/api/board/terminals/{agent['id']}/delete", {'confirm': True})
            await page.wait_for(f"!document.querySelector('li.panel-row[data-terminal=\"{agent['id']}\"]')", 9)
            await self.clean(page, 'reconcile')
        self.run_async(go())

    def test_07_rename_korean_longest_duplicate_reserved_and_the_text_of_every_refusal(self):
        async def go():
            second = self.create('agent')
            page = await self.board()
            a1 = next(t for t in self.hub.terminals() if t['name'] == 'agent-1')
            shown_error = "(() => { const e = document.querySelector('li.panel-row .row-error'); return e && !e.hidden ? e.textContent : null; })()"

            async def rename(tid, text, expect_open=False):
                await self.panel_click(page, tid, '이름 바꾸기')
                await page.wait_for(f"!!document.querySelector('li.panel-row[data-terminal=\"{tid}\"] .rename-input')")
                await page.eval(f"(() => {{ const i = document.querySelector('li.panel-row[data-terminal=\"{tid}\"] .rename-input'); i.focus(); i.select(); }})()")
                await page.send('Input.insertText', text=text)
                await page.key('Enter', 'Enter', 13)
                await asyncio.sleep(0.4)

            def server_detail(tid, name):
                status, reply = self.api('POST', f'/api/board/terminals/{tid}/rename', {'name': name})
                return status, reply

            # Korean name: panel row and widget title show it; the Hub has it; another browser sees it after its refresh
            other = await self.board()
            await rename(a1['id'], '전처리')
            self.assertEqual(self.named(a1['id'])['name'], '전처리')
            await page.wait_for("document.querySelector('.board-widget .widget-name') && [...document.querySelectorAll('.widget-name')].some(e => e.textContent === '전처리')")
            self.assertEqual((await self.snap(page))['rows'][0]['name'], '전처리')
            await other.wait_for("[...document.querySelectorAll('.widget-name')].some(e => e.textContent === '전처리') && [...document.querySelectorAll('.panel-name')].some(e => e.textContent === '전처리')", 10, 'the other browser follows')
            # the longest name (40) and a padded name (trimmed)
            await rename(a1['id'], '가' * 40)
            self.assertEqual(self.named(a1['id'])['name'], '가' * 40)
            await rename(a1['id'], '  패딩  ')
            self.assertEqual(self.named(a1['id'])['name'], '패딩')
            # refusals: the row shows a sentence and stays open; the Hub's own sentence for the same input is the shown text
            cases = [('41 chars', '가' * 41), ('whitespace only', '   '), ('duplicate ignoring case', 'AGENT-2'.replace('2', '-2') if False else second['name'].upper()), ('reserved', 'board-agent'),
                     ('reserved mixed case', 'Board-Agent')]
            for label, text in cases:
                await rename(a1['id'], text)
                error = await page.eval(shown_error)
                before = self.named(a1['id'])['name']
                status, reply = server_detail(a1['id'], text.strip() if label != 'whitespace only' else text)
                self.assertNotEqual(status, 200, f'{label}: the Hub refuses it too')
                self.assertEqual(self.named(a1['id'])['name'], before, f'{label}: nothing changed')
                self.assertIsNotNone(error, f'{label}: a reason is shown')
                self.assertEqual(error, reply['detail'], f'{label}: the page shows the Hub\'s sentence ({reply["reason"]})')
                self.assertTrue(await page.eval(f"!!document.querySelector('li.panel-row[data-terminal=\"{a1['id']}\"] .rename-input')"), f'{label}: the input stays for another try')
                await page.key('Escape', 'Escape', 27)
                await asyncio.sleep(0.15)
                self.assertFalse(await page.eval(f"!!document.querySelector('li.panel-row[data-terminal=\"{a1['id']}\"] .rename-input')"), f'{label}: Esc cancels')
            # a refusal that only the Hub can make (a name the page's own check lets through but the Hub's rules refuse)
            status, reply = server_detail(a1['id'], 'ok\u200bname')
            self.assertEqual(status, 400)
            await self.clean(page, 'rename', refused_api_calls=True)
            await self.clean(other, 'rename other')
        self.run_async(go())

    def test_08_a_rename_in_progress_survives_the_five_second_refresh(self):
        async def go():
            page = await self.board()
            a1 = next(t for t in self.hub.terminals() if t['kind'] == 'agent')
            await self.panel_click(page, a1['id'], '이름 바꾸기')
            await page.wait_for(f"!!document.querySelector('li.panel-row[data-terminal=\"{a1['id']}\"] .rename-input')")
            await page.eval(f"(() => {{ const i = document.querySelector('li.panel-row[data-terminal=\"{a1['id']}\"] .rename-input'); i.focus(); i.select(); }})()")
            await page.send('Input.insertText', text='작성 중인 이름')
            await asyncio.sleep(6.5)  # one 5 s refresh (and any state message) in between
            value = await page.eval(f"(() => {{ const i = document.querySelector('li.panel-row[data-terminal=\"{a1['id']}\"] .rename-input'); return i ? i.value : null; }})()")
            self.assertEqual(value, '작성 중인 이름', 'the typed name is still in the input after a refresh (the input must not be rebuilt from the old name)')
        self.run_async(go())

    def test_09_delete_stop_close_and_restart_need_the_dialog_and_cancel_or_escape_changes_nothing(self):
        async def go():
            second = self.create('agent')
            extra = self.create('shell')
            page = await self.board()
            a1 = next(t for t in self.hub.terminals() if t['name'] == 'agent-1')
            s1 = next(t for t in self.hub.terminals() if t['name'] == 'shell-1')
            dialog_text = "(() => { const d = document.getElementById('board-confirm'); return d.open ? document.getElementById('board-confirm-text').textContent : null; })()"
            state = lambda tid: self.named(tid)['state'] if any(t['id'] == tid for t in self.hub.terminals()) else None
            posts_before = len(page.api_posts())
            cases = [(a1['id'], '중지', 'agent-1을 중지합니다. 메모리는 남습니다.'), (a1['id'], '삭제', 'agent-1의 세션과 메모리를 지웁니다. 되돌릴 수 없습니다.'),
                     (s1['id'], '닫기', 'shell-1을 닫습니다. 실행 중인 명령이 끝납니다.')]
            for tid, label, text in cases:
                await self.panel_click(page, tid, label)
                await page.wait_for(f"({dialog_text}) !== null")
                self.assertEqual(await page.eval(dialog_text), text)
                await page.eval("document.getElementById('board-confirm-cancel').click()")
                await asyncio.sleep(0.2)
                self.assertEqual(await page.eval(dialog_text), None)
                await self.panel_click(page, tid, label)
                await page.wait_for(f"({dialog_text}) !== null")
                await page.key('Escape', 'Escape', 27)  # the dialog's own Escape
                await asyncio.sleep(0.2)
                self.assertEqual(await page.eval(dialog_text), None, f'{label}: Escape closes the dialog')
            await asyncio.sleep(0.5)
            self.assertEqual(len(page.api_posts()), posts_before, 'cancelled and escaped dialogs sent no request')
            self.assertEqual((state(a1['id']), state(s1['id'])), ('running', 'running'))
            # the widget's x: agent -> stop dialog, shell -> close dialog; "yes" acts
            await page.eval(f"document.querySelector('.board-widget[data-terminal=\"{a1['id']}\"] .widget-close').click()")
            await page.wait_for(f"({dialog_text}) !== null")
            self.assertEqual(await page.eval(dialog_text), 'agent-1을 중지합니다. 메모리는 남습니다.')
            await page.eval("document.getElementById('board-confirm-ok').click()")
            await page.wait_for(f"document.querySelector('.board-widget[data-terminal=\"{a1['id']}\"] .cover-text') && document.querySelector('.board-widget[data-terminal=\"{a1['id']}\"] .widget-cover').hidden === false", 10)
            self.assertEqual(state(a1['id']), 'stopped')
            self.assertEqual((await self.snap(page))['widgets'][0]['cover'], '중지됨 — 시작')
            stops = [r for r in page.api_posts('/stop')]
            self.assertEqual(len(stops), 1)
            self.assertEqual(json.loads(stops[0]['body']), {'confirm': True})
            # 중지됨 — 시작 starts it again without a dialog
            await page.eval(f"document.querySelector('.board-widget[data-terminal=\"{a1['id']}\"] .widget-cover button').click()")
            await page.wait_for(f"document.querySelector('.board-widget[data-terminal=\"{a1['id']}\"] .widget-cover').hidden === true", 12)
            self.assertEqual(state(a1['id']), 'running')
            self.assertEqual(await page.eval(dialog_text), None)
            # a shell that exits shows 재시작 behind a dialog
            sid = extra['id']
            await page.wait_for(f"!!document.querySelector('.board-widget[data-terminal=\"{sid}\"]')", 9)
            await page.eval(f"document.querySelector('.board-widget[data-terminal=\"{sid}\"] .xterm-helper-textarea').focus()")
            await page.type_text('exit\r')
            await page.wait_for(f"document.querySelector('.board-widget[data-terminal=\"{sid}\"] .widget-cover').hidden === false", 10)
            cover = (await self.snap(page))['widgets']
            exited = next(w for w in cover if w['id'] == sid)
            self.assertIn('종료됨', exited['cover'])
            await page.eval(f"document.querySelector('.board-widget[data-terminal=\"{sid}\"] .widget-cover button').click()")
            await page.wait_for(f"({dialog_text}) !== null")
            self.assertIn('다시 시작합니다', await page.eval(dialog_text))
            await page.eval("document.getElementById('board-confirm-cancel').click()")
            await asyncio.sleep(0.3)
            self.assertEqual(len(page.api_posts('/restart')), 0)
            await page.eval(f"document.querySelector('.board-widget[data-terminal=\"{sid}\"] .widget-cover button').click()")
            await page.wait_for(f"({dialog_text}) !== null")
            await page.eval("document.getElementById('board-confirm-ok').click()")
            await page.wait_for(f"document.querySelector('.board-widget[data-terminal=\"{sid}\"] .widget-cover').hidden === true", 12)
            self.assertEqual(state(sid), 'running')
            # delete an agent and close a shell for real
            await self.panel_click(page, second['id'], '삭제')
            await page.wait_for(f"({dialog_text}) !== null")
            await page.eval("document.getElementById('board-confirm-ok').click()")
            await page.wait_for(f"!document.querySelector('li.panel-row[data-terminal=\"{second['id']}\"]')", 12)
            self.assertIsNone(state(second['id']))
            await self.panel_click(page, sid, '닫기')
            await page.wait_for(f"({dialog_text}) !== null")
            await page.eval("document.getElementById('board-confirm-ok').click()")
            await page.wait_for(f"!document.querySelector('.board-widget[data-terminal=\"{sid}\"]')", 12)
            self.assertIsNone(state(sid))
            await self.clean(page, 'confirmations')
        self.run_async(go())

    def test_10_limits_form_range_below_count_saving_and_the_disabled_create_buttons(self):
        async def go():
            self.create('agent')
            page = await self.board()
            other = await self.board()
            err = "(() => { const e = document.getElementById('board-options-error'); return e.hidden ? null : e.textContent; })()"

            async def submit(agent, shell):
                await page.eval(f"document.getElementById('board-agent-limit').value = {json.dumps(agent)}; document.getElementById('board-shell-limit').value = {json.dumps(shell)};"
                                " document.getElementById('board-options').requestSubmit()")
                await asyncio.sleep(0.4)

            posts = len(page.api_posts('/options'))
            for agent, shell, label in (('1', '10', 'below the 2 agents now'), ('9', '10', 'agent range'), ('0', '10', 'zero'), ('4', '33', 'shell range'), ('', '10', 'empty'),
                                        ('abc', '10', 'text'), ('1.5', '10', 'fraction'), ('-1', '10', 'negative')):
                await submit(agent, shell)
                text = await page.eval(err)
                self.assertTrue(text, f'{label}: a reason is shown')
            self.assertEqual(len(page.api_posts('/options')), posts, 'the page never sends a limit it can see is wrong')
            self.assertEqual(self.api('GET', '/api/board')[1]['options']['agent_limit'], 4, 'nothing was saved')
            # the Hub's sentence for the below-count case is the shown one
            await submit('1', '10')
            status, reply = self.api('POST', '/api/board/options', {'agent_limit': 1, 'shell_limit': 10})
            self.assertEqual((status, reply['reason']), (409, 'limit_below_count'))
            self.assertEqual(await page.eval(err), reply['detail'])
            # a valid change is saved in the Hub, hides the error, disables creating at the limit and shows 한도 N개
            await submit('2', '10')
            self.assertIsNone(await page.eval(err))
            self.assertEqual(self.api('GET', '/api/board')[1]['options']['agent_limit'], 2)
            self.assertTrue(await page.eval("document.getElementById('board-new-agent').disabled"))
            self.assertEqual(await page.eval("document.getElementById('board-agent-note').textContent"), '한도 2개')
            self.assertFalse(await page.eval("document.getElementById('board-new-shell').disabled"))
            # another browser sees the new limit after its refresh; a click on the disabled button creates nothing
            await other.wait_for("document.getElementById('board-new-agent').disabled", 9)
            agents_before = len([t for t in self.hub.terminals() if t['kind'] == 'agent'])
            await page.eval("document.getElementById('board-new-agent').click()")
            await asyncio.sleep(0.4)
            self.assertEqual(len([t for t in self.hub.terminals() if t['kind'] == 'agent']), agents_before)
            # raise it and create from the panel: the new agent is a visible widget at once, named agent-N
            await submit('3', '10')
            await page.eval("document.getElementById('board-new-agent').click()")
            await page.wait_for("document.querySelectorAll('.board-widget').length === 4", 10)
            self.assertEqual(sorted(t['name'] for t in self.hub.terminals() if t['kind'] == 'agent'), ['agent-1', 'agent-2', 'agent-3'])
            self.assertTrue(await page.eval("document.getElementById('board-new-agent').disabled"))
            await self.clean(page, 'limits')
            await self.clean(other, 'limits other')
        self.run_async(go())

    def test_11_narrow_375_shows_one_terminal_and_returns_to_the_stored_grid(self):
        async def go():
            self.create('agent')
            self.create('shell')
            page = await self.board(viewport=(1400, 2400))
            ids = [t['id'] for t in self.hub.terminals()]
            self.assertEqual(len((await self.snap(page))['widgets']), 4)
            x, y = await self.press(page, ids[1], 'bar')
            await page.drag(x, y, x, y + 24 * 48)  # a free place far below: a stored layout of its own
            wide = {w['id']: (w['cx'], w['cy'], w['cw'], w['ch']) for w in (await self.snap(page))['widgets']}
            stored = (await self.snap(page))['ls']
            self.assertIsNotNone(stored)
            await page.viewport(720, 900)
            await asyncio.sleep(0.5)
            self.assertFalse((await self.snap(page))['narrow'], '720 px is still the grid')
            await page.viewport(719, 900)
            await page.wait_for("document.getElementById('board-shell').classList.contains('board-narrow')")
            await asyncio.sleep(0.5)
            await page.viewport(375, 800)
            await page.wait_for("document.querySelectorAll('.board-widget').length === 1")
            snap = await self.snap(page)
            self.assertTrue(snap['narrow'])
            self.assertEqual(len(snap['widgets']), 1)
            first_agent = next(t for t in self.hub.terminals() if t['kind'] == 'agent')
            self.assertEqual(snap['widgets'][0]['id'], first_agent['id'], 'nothing chosen: the first agent')
            self.assertLessEqual(snap['scrollW'], snap['innerW'], 'no horizontal page scroll at 375 px')
            self.assertLessEqual(snap['widgets'][0]['w'], 375)
            self.assertGreater(snap['widgets'][0]['w'], 300, 'the terminal fills the screen')
            self.assertTrue(snap['panelHidden'], 'the panel folds away')
            self.assertEqual(snap['ls'], stored, 'the grid layout is untouched by narrowing')
            self.assertTrue(await self.wait_open_sockets(page, [first_agent['id']], 6), 'only that terminal is connected')
            self.assertTrue(ws_tests.wait_until(lambda: sorted(t['id'] for t in self.hub.terminals() if t['clients'] == 1) == [first_agent['id']], 8))
            # choose another one from the panel
            await page.eval("document.getElementById('board-panel-toggle').click()")
            self.assertFalse((await self.snap(page))['panelHidden'])
            shells = [t for t in self.hub.terminals() if t['kind'] == 'shell']
            await self.panel_click(page, shells[1]['id'], '보기')
            await page.wait_for(f"document.querySelector('.board-widget').dataset.terminal === '{shells[1]['id']}' && document.querySelectorAll('.board-widget').length === 1")
            self.assertTrue(await self.wait_open_sockets(page, [shells[1]['id']], 6))
            self.assertTrue(ws_tests.wait_until(lambda: sorted(t['id'] for t in self.hub.terminals() if t['clients'] == 1) == [shells[1]['id']], 8))
            self.assertEqual(await page.eval("localStorage.getItem('wb.board.narrow')"), shells[1]['id'])
            # a reload at 375 px comes back to the same terminal
            await page.reload()
            await page.wait_for("document.querySelectorAll('.board-widget').length === 1")
            self.assertEqual((await self.snap(page))['widgets'][0]['id'], shells[1]['id'])
            # back to a wide screen: the stored grid, every visible widget reconnected, one connection each
            await page.viewport(1400, 1400)
            await page.wait_for(f"document.querySelectorAll('.board-widget').length === {len(ids)}", 10)
            await asyncio.sleep(0.5)
            snap = await self.snap(page)
            self.assertFalse(snap['narrow'])
            self.assertEqual({w['id']: (w['cx'], w['cy'], w['cw'], w['ch']) for w in snap['widgets']}, wide, 'the stored layout is back')
            self.assert_geometry(snap, 'back wide')
            self.assertTrue(await self.wait_open_sockets(page, sorted(ids), 8))
            self.assertTrue(ws_tests.wait_until(lambda: all(t['clients'] == 1 for t in self.hub.terminals()), 8))
            await self.clean(page, 'narrow')
        self.run_async(go())

    def test_12_two_browsers_on_one_terminal_share_output_count_each_other_and_the_size_follows_the_typist_without_ping_pong(self):
        async def go():
            shell = next(t for t in self.hub.terminals() if t['kind'] == 'shell')
            a = await self.board(viewport=(1500, 900))
            b = await self.board(viewport=(1000, 700))
            sid = shell['id']
            sel = f'.board-widget[data-terminal="{sid}"]'
            await a.wait_for(f"document.querySelector('{sel} .widget-others').textContent === '다른 화면 1개 연결됨'", 10)
            await b.wait_for(f"document.querySelector('{sel} .widget-others').textContent === '다른 화면 1개 연결됨'", 10)
            self.assertTrue(ws_tests.wait_until(lambda: self.named(sid)['clients'] == 2, 5))
            # the agent has the same two connections but each is its own terminal's count
            # typing in A shows in both (the same output)
            await a.eval(f"document.querySelector('{sel} .xterm-helper-textarea').focus()")
            await a.type_text('echo SHARED-ONE\r')
            for page in (a, b):
                await page.wait_for(f"document.querySelector('{sel} .xterm-rows').textContent.includes('SHARED-ONE')", 8)
            # B types: both see it too, in arrival order
            await b.eval(f"document.querySelector('{sel} .xterm-helper-textarea').focus()")
            await b.type_text('echo SHARED-TWO\r')
            for page in (a, b):
                text = await page.wait_for(f"(() => {{ const t = document.querySelector('{sel} .xterm-rows').textContent; return t.includes('SHARED-TWO') ? t : ''; }})()", 8)
                self.assertLess(text.index('SHARED-ONE'), text.index('SHARED-TWO'))

            async def pty_size(page):
                before = len(page.frames)
                await page.eval(f"document.querySelector('{sel} .xterm-helper-textarea').focus()")
                await page.type_text('stty size\r')
                await page.wait_for(f"/\\n\\d+ \\d+/.test(document.querySelector('{sel} .xterm-rows').innerText)", 8)
                await asyncio.sleep(0.3)
                text = await page.eval(f"document.querySelector('{sel} .xterm-rows').innerText")
                matches = re.findall(r'(?m)^(\d+) (\d+)\s*$', text)
                return tuple(map(int, matches[-1]))

            box = lambda page: page.eval(f"(() => {{ const sc = document.querySelector('{sel} .widget-scroll'), sr = document.querySelector('{sel} .xterm-screen').getBoundingClientRect(); return {{box: [sc.clientWidth, sc.clientHeight], scroll: [sc.scrollWidth, sc.scrollHeight], screen: [sr.width, sr.height]}}; }})()")
            size_a = await pty_size(a)
            await asyncio.sleep(0.5)
            at_b = await box(b)  # B draws at A's wider size: it scrolls (contract: scroll when it does not fit)
            self.assertGreater(at_b['scroll'][0], at_b['box'][0], f'B must scroll sideways: {at_b}')
            size_b = await pty_size(b)
            await asyncio.sleep(0.5)
            at_a = await box(a)  # A draws at B's smaller size: the rest is left empty (letterbox), nothing scrolls
            self.assertLess(at_a['screen'][0], at_a['box'][0] - 20, f'A letterboxes: {at_a}')
            self.assertLessEqual(at_a['scroll'][0], at_a['box'][0] + 1, f'A does not scroll: {at_a}')
            self.assertNotEqual(size_a, size_b, 'two differently sized windows')
            self.assertGreater(size_a[1], size_b[1], 'the terminal follows whoever typed last: A is wider than B')
            hello_a = a.sent_json(sid, 'hello')[0]['size']
            hello_b = b.sent_json(sid, 'hello')[0]['size']
            self.assertEqual(size_a, tuple(hello_a), 'A typed: the PTY has A\'s own size')
            self.assertEqual(size_b, tuple(hello_b), 'B typed: the PTY has B\'s own size')
            # the non-owner draws at the owner's size (rows of the DOM renderer), letterbox or scroll in its own widget
            rows_a = await a.eval(f"document.querySelectorAll('{sel} .xterm-rows > div').length")
            self.assertEqual(rows_a, size_b[0], 'A now draws at B\'s row count')
            # no ping-pong: idle, then count the resize frames and size frames over several seconds
            sent_a, sent_b = len(a.sent_json(sid, 'resize')), len(b.sent_json(sid, 'resize'))
            recv_a, recv_b = len(a.recv_json(sid, 'size')), len(b.recv_json(sid, 'size'))
            await asyncio.sleep(4)
            self.assertEqual((len(a.sent_json(sid, 'resize')), len(b.sent_json(sid, 'resize'))), (sent_a, sent_b), 'idle windows send no resize')
            self.assertEqual((len(a.recv_json(sid, 'size')), len(b.recv_json(sid, 'size'))), (recv_a, recv_b), 'and the Hub sends no size')
            self.assertEqual((sent_a, sent_b), (0, 0), 'a size message never answered with a resize (no resize without a layout change of that window)')
            # hiding in B releases its connection: A no longer sees "다른 화면"
            await b.eval(f"document.querySelector('{sel} .widget-hide').click()")
            await a.wait_for(f"document.querySelector('{sel} .widget-others').textContent === ''", 8)
            self.assertTrue(ws_tests.wait_until(lambda: self.named(sid)['clients'] == 1, 5))
            await self.panel_click(b, sid, '보이기')
            await a.wait_for(f"document.querySelector('{sel} .widget-others').textContent === '다른 화면 1개 연결됨'", 8)
            # a window that resizes (layout change of its own widget) takes the size: B shrinks its window
            await b.viewport(800, 650)
            await asyncio.sleep(1.0)
            self.assertEqual(len(b.sent_json(sid, 'resize')), 1, 'one resize after the 150 ms debounce, from the window that changed')
            self.assertEqual(len(a.sent_json(sid, 'resize')), 0)
            await self.clean(a, 'share a')
            await self.clean(b, 'share b')
        self.run_async(go())

    def test_13_the_ninth_connection_to_one_terminal_is_refused_with_the_limit_text_and_is_not_retried(self):
        async def go():
            shell = next(t for t in self.hub.terminals() if t['kind'] == 'shell')
            only_shell = json.dumps({'v': 1, 'widgets': {shell['id']: {'x': 0, 'y': 0, 'w': 12, 'h': 10, 'visible': True}}})
            init = f"try {{ localStorage.setItem('wb.board.layout', {json.dumps(only_shell)}); }} catch (e) {{}}"
            pages = []
            for _ in range(9):
                pages.append(await self.board(init=init))
            counts = [len(p.ws_created) for p in pages]
            self.assertEqual(self.named(shell['id'])['clients'], 8)
            texts = []
            for p in pages:
                texts.append(await p.eval("(() => { const c = document.querySelector('.widget-cover'); return c && !c.hidden ? c.querySelector('.cover-text').textContent : ''; })()"))
            self.assertEqual(sorted(t for t in texts if t), ['연결 수 한도(8개)를 넘었습니다'], texts)
            await asyncio.sleep(3.5)
            self.assertEqual([len(p.ws_created) for p in pages], counts, 'no automatic retry of the refused connection')
            for p in pages:
                await self.clean(p, 'ninth')
        self.run_async(go())

    def test_14_hostile_names_are_text_in_the_panel_the_title_the_dialogs_and_the_notice(self):
        async def go():
            payloads = ['<img src=x onerror="window.__pwn=1">', '<script>window.__pwn=1</script>', '"><svg onload=window.__pwn=1>', '&lt;b&gt;x&lt;/b&gt;']
            a1 = next(t for t in self.hub.terminals() if t['kind'] == 'agent')
            s1 = next(t for t in self.hub.terminals() if t['kind'] == 'shell')
            second = self.create('agent')
            third = self.create('shell')
            for terminal, name in zip((a1, s1, second, third), payloads):
                status, reply = self.api('POST', f"/api/board/terminals/{terminal['id']}/rename", {'name': name})
                self.assertEqual(status, 200, reply)
            page = await self.board()
            snap = await self.snap(page)
            shown = sorted(w['name'] for w in snap['widgets'])
            self.assertEqual(shown, sorted(payloads), 'the widget titles are the exact text')
            self.assertEqual(sorted(r['name'] for r in snap['rows']), sorted(payloads))
            self.assertEqual(await page.eval("document.querySelectorAll('.board-widget img, .board-widget svg, #board-panel img, #board-panel svg, #board-panel script, .board-widget script, main script:not([src])').length"), 0)
            self.assertEqual(await page.eval("window.__pwn"), None)
            html = await page.eval("document.getElementById('board-panel').innerHTML")
            self.assertIn('&lt;img src=x', html, 'the markup is in the DOM as text')
            self.assertNotIn('<img', html)
            # the dialog texts and a rename input hold it as text too
            for tid, label in ((a1['id'], '삭제'), (s1['id'], '닫기')):
                await self.panel_click(page, tid, label)
                await page.wait_for("document.getElementById('board-confirm').open")
                text = await page.eval("document.getElementById('board-confirm-text').textContent")
                self.assertIn(payloads[0 if tid == a1['id'] else 1], text)
                self.assertEqual(await page.eval("document.querySelectorAll('#board-confirm img, #board-confirm svg, #board-confirm script').length"), 0)
                await page.eval("document.getElementById('board-confirm-cancel').click()")
                await asyncio.sleep(0.15)
            await self.panel_click(page, a1['id'], '이름 바꾸기')
            await page.wait_for("!!document.querySelector('.rename-input')")
            self.assertEqual(await page.eval("document.querySelector('.rename-input').value"), payloads[0])
            await page.key('Escape', 'Escape', 27)
            self.assertEqual(await page.eval("window.__pwn"), None)
            await self.clean(page, 'xss')
        self.run_async(go())

    def test_15_back_navigation_hides_the_page_until_the_session_check_answers(self):
        async def go():
            cookie = self.login_cookie()
            browser = self.sessions[-1]
            page = await self.board(cookie=cookie)
            await page.goto(self.base + '/dashboard')
            await page.wait_for("document.readyState === 'complete'")
            # Back to /board with a live session: hidden while the check runs, then revealed, and the terminals reconnect
            page._load.clear()
            # leaving the page releases its connections (pagehide -> release), well before the Hub's heartbeat would notice
            await page.goto(self.base + '/dashboard')
            self.assertTrue(ws_tests.wait_until(lambda: all(t['clients'] == 0 for t in self.hub.terminals()), 4), 'navigating away released every connection')
            await page.eval("history.back()")
            await page.wait_for("location.pathname === '/board' && document.querySelectorAll('.board-widget').length === 2", 12)
            await page.wait_for("!document.documentElement.classList.contains('wb-checking')", 8)
            samples = self.since_restore(await page.eval("window.__samples"))
            self.assertTrue(samples, 'frames were sampled after Back')
            # a hidden-until-checked page: whenever the class was set, the body was hidden
            for _, cls, visibility, path in samples:
                if cls and 'wb-checking' in cls:
                    self.assertEqual(visibility, 'hidden', f'{path}: hidden while checking')
            # now end the session elsewhere and go Back: the page never shows, and ends on /login
            await page.goto(self.base + '/dashboard')
            status, headers, body = browser.request('GET', '/settings')
            csrf = re.search(r'name="csrf" value="([^"]+)"', body).group(1)
            status, headers, _ = browser.request('POST', '/logout', {'csrf': csrf})
            self.assertEqual(status, 303)
            await page.eval("history.back()")
            await page.wait_for("location.pathname === '/login'", 12)
            samples = self.since_restore(await page.eval("window.__samples"))
            visible_board = [s for s in samples if s[3] == '/board' and s[1] != 'SHOW' and not (s[1] and 'wb-checking' in s[1]) and s[2] != 'hidden']
            self.assertEqual(visible_board, [], 'the stale /board content was never visible')
            await self.clean(page, 'back navigation', session_ended=True)
        self.run_async(go())

    def test_16_korean_ime_input_reaches_the_program_once_and_in_order(self):
        """Emulated IME through the DevTools protocol (``Input.imeSetComposition`` then ``Input.insertText`` per syllable, paced
        like fingers: the unpaced version races xterm's own composition timer and garbles even a correct page). A negative
        control puts back the rule the A8 fix removed (``textarea { max-width: 100% }`` on xterm's helper textarea) and must
        produce the reported garbage, so this check can fail."""
        jamo = {'안': ['ㅇ', '아', '안'], '녕': ['ㄴ', '녀', '녕'], '하': ['ㅎ', '하'], '세': ['ㅅ', '세'], '요': ['ㅇ', '요']}

        async def typed(page, sid):
            sel = f'.board-widget[data-terminal="{sid}"]'
            await page.eval(f"document.querySelector('{sel} .xterm-helper-textarea').focus()")
            await page.type_text('echo ')
            for syllable in '안녕하세요':
                for step in jamo[syllable]:
                    await page.send('Input.imeSetComposition', text=step, selectionStart=len(step), selectionEnd=len(step))
                    await asyncio.sleep(0.05)
                await page.send('Input.insertText', text=syllable)
                await asyncio.sleep(0.08)
            await page.type_text('\r')
            await asyncio.sleep(1.0)
            return await page.eval(f"document.querySelector('{sel} .xterm-rows').innerText")

        async def go():
            s1 = next(t for t in self.hub.terminals() if t['kind'] == 'shell')
            page = await self.board()
            text = await typed(page, s1['id'])
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            self.assertIn('$ echo 안녕하세요', lines, text)
            self.assertEqual(lines.count('안녕하세요'), 1, 'the program printed it once, in order')
            for junk in ('안안', '녕녕', '하하', '세세', '요요'):
                self.assertNotIn(junk, text)
            await self.clean(page, 'ime')
            # negative control: with the old clamp the same keystrokes give the reported repeated first syllable
            broken = await self.board()
            await broken.eval("(() => { const s = document.createElement('style'); s.textContent = '.xterm .xterm-helper-textarea { max-width: 100% !important; }'; document.head.append(s); })()")
            text = await typed(broken, s1['id'])
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            commands = [line for line in lines if line.startswith('$ echo') and '안' in line]  # (the shell keeps earlier tests' lines)
            self.assertEqual(len(commands), 2, lines)  # the replay of the first page's command, then the new one
            self.assertEqual(commands[0], '$ echo 안녕하세요')
            self.assertNotEqual(commands[1], '$ echo 안녕하세요', 'the clamp reproduces the garbling (A8), so the check above can detect it')

        self.run_async(go())

    def test_17_panel_rows_widget_title_status_and_connection_text_for_a_stopped_agent(self):
        async def go():
            a1 = next(t for t in self.hub.terminals() if t['kind'] == 'agent')
            page = await self.board()
            await self.panel_click(page, a1['id'], '중지')
            await page.eval("document.getElementById('board-confirm-ok').click()")
            await page.wait_for(f"document.querySelector('li.panel-row[data-terminal=\"{a1['id']}\"] .panel-state').textContent === '중지됨'", 10)
            row = await page.eval(f"[...document.querySelector('li.panel-row[data-terminal=\"{a1['id']}\"]').querySelectorAll('button')].map(b => b.textContent)")
            self.assertEqual(row, ['숨기기', '시작', '이름 바꾸기', '삭제'])
            # reload while stopped: a stopped agent is NOT started by opening the page (WBX-D56), and shows 중지됨 — 시작
            await page.reload()
            await page.wait_for(f"!!document.querySelector('.board-widget[data-terminal=\"{a1['id']}\"] .cover-text')")
            await asyncio.sleep(1.0)
            self.assertEqual(self.named(a1['id'])['state'], 'stopped')
            cover = next(w for w in (await self.snap(page))['widgets'] if w['id'] == a1['id'])['cover']
            self.assertEqual(cover, '중지됨 — 시작')
            await self.clean(page, 'stopped')
        self.run_async(go())


if __name__ == '__main__':
    unittest.main()
