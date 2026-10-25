"""Request listing with and without a search query over many large Hangul requests (``InboxService.list_requests``).

The search key (NFD of the case fold of NFD, so composed and decomposed Hangul match) is work over every title
and body; a list without a query must not pay it. Expected values: the test's own rows and a time bound far
above a plain listing of 1000 rows (and far below normalizing ~21 MB of Hangul per call).
Run with ``discover -s tests/hub -p 'test_inbox_search_hangul.py'``.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unicodedata
import unittest

import test_web_pages_requests as request_case  # module import: its test classes stay uncollected here

from workbench.hub.inbox import limits
from workbench.hub.inbox.service import InboxService, Principal, ProjectRef

OWNER = Principal.owner()
ROWS = 1000
BODY = '가나다라마바사아자차카타파하 ' * 500  # 21 KB of UTF-8 per body


class LargeHangulListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='wb-search-')
        root, cls.project = request_case.make_project(Path(cls.temp.name).resolve(), 'alpha')
        cls.inbox = InboxService()
        cls.inbox.set_projects([ProjectRef(cls.project, root, 'alpha')])
        for index in range(ROWS):
            title = f'요청 {index}' if index != 777 else unicodedata.normalize('NFD', '찾을 요청 777')
            cls.inbox.submit(OWNER, cls.project, title, BODY)

    @classmethod
    def tearDownClass(cls):
        cls.inbox.close()
        cls.temp.cleanup()

    def listing(self, **arguments) -> tuple[list[dict], float]:
        started = time.perf_counter()
        found, cursor = [], None
        while True:
            page = self.inbox.list_requests(OWNER, project_id=self.project, limit=100, cursor=cursor, **arguments)
            found += page['requests']
            cursor = page['next_cursor']
            if cursor is None:
                return found, time.perf_counter() - started

    def test_a_list_without_a_query_does_no_search_work(self):
        self.assertGreater(len(BODY.encode('utf-8')), 20_000)
        calls = []
        original = limits.search_key

        def counted(text):
            calls.append(len(text))
            return original(text)
        limits.search_key = counted
        try:
            found, elapsed = self.listing()
        finally:
            limits.search_key = original
        self.assertEqual(len(found), ROWS)
        self.assertEqual(calls, [])
        self.assertLess(elapsed, 5.0)

    def test_a_query_still_finds_composed_and_decomposed_hangul_and_keys_the_query_once_per_call(self):
        calls = []
        original = limits.search_key

        def counted(text):
            calls.append(text)
            return original(text)
        limits.search_key = counted
        try:
            found, _ = self.listing(query='찾을')
        finally:
            limits.search_key = original
        self.assertEqual([row['seq'] for row in found], [778])
        self.assertEqual(calls.count('찾을'), 1)  # once per call (one page here), never per row


if __name__ == '__main__':
    unittest.main()
