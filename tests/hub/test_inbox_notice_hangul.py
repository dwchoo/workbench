"""Korean (Hangul) text in the manager's ``inbox_request`` notice (``inbox_link.manager_notice_content``).

The notice cleans the requester's text for display: control, format (bidi, zero-width) and separator characters
become spaces. Expected values: every Hangul block is letters (Unicode ``Lo``) and must pass unchanged —
syllables U+AC00-D7A3, compatibility jamo U+3131-318E (incl. U+3164 HANGUL FILLER), Hangul Jamo U+1100-11FF
(conjoining vowels/finals, U+115F/U+1160 fillers) and halfwidth jamo U+FFA0-FFDC.
Run with ``discover -s tests/hub -p 'test_inbox_notice_hangul.py'``.
"""
from __future__ import annotations

import unicodedata
import unittest

from workbench.backend.inbox_link import manager_notice_content

SYLLABLES = ''.join(map(chr, range(0xAC00, 0xD7A4)))
COMPATIBILITY = ''.join(chr(c) for c in range(0x3131, 0x318F))
JAMO = ''.join(chr(c) for c in range(0x1100, 0x1200))
HALFWIDTH = ''.join(chr(c) for c in range(0xFFA0, 0xFFDD) if unicodedata.category(chr(c)) == 'Lo')


def notice(title: str = 'T', excerpt: str = 'E') -> dict:
    return manager_notice_content('submitted', {'request_id': 'r', 'seq': 1, 'title': title, 'excerpt': excerpt,
                                                'origin': {'kind': 'owner'}, 'state': 'NEW'})


class NoticeHangulTests(unittest.TestCase):
    def test_every_hangul_letter_block_passes_unchanged(self):
        for name, text in (('compatibility', COMPATIBILITY), ('jamo', JAMO), ('halfwidth', HALFWIDTH)):
            with self.subTest(name):
                self.assertEqual(notice(excerpt=text)['excerpt'], text[:1000])
        for start in range(0, len(SYLLABLES), 1000):
            chunk = SYLLABLES[start:start + 1000]
            self.assertEqual(notice(excerpt=chunk)['excerpt'], chunk)

    def test_composed_decomposed_and_filler_titles_stay_exact(self):
        for title in ('한글 제목', unicodedata.normalize('NFD', '한글 제목'), '\u3164채움', '\u115f\u1160\u11a8',
                      '가' * 200):
            with self.subTest(title=ascii(title)):
                self.assertEqual(notice(title=title)['title'], title)

    def test_only_controls_format_and_separators_around_hangul_become_spaces(self):
        dirty = '한\x1b[31m글\u202e거꾸로\u200b붙임\u2028줄\x00끝\n둘째'
        self.assertEqual(notice(excerpt=dirty)['excerpt'], '한 [31m글 거꾸로 붙임 줄 끝\n둘째')
        self.assertEqual(notice(title=dirty)['title'], '한 [31m글 거꾸로 붙임 줄 끝 둘째')


if __name__ == '__main__':
    unittest.main()
