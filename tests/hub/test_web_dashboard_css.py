"""WBX-C2 dashboard CSS (seam S5: static reads of ``src/workbench/hub/web/static/*.css``; plus the dashboard page's
class names from the real app). What it checks: every class the dashboard markup (server and ``dashboard.js``) uses has a
rule, the summary cards wrap and the two-column layout only starts on a wide screen, the power chips carry a glyph, and
every colour pair the rules declare reaches the WCAG 4.5:1 text contrast in light and dark. How it looks is the user's
Chrome check (S7). Run with ``discover -s tests/hub -p 'test_web_dashboard*.py'``.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

import test_web_dashboard as dashboard_tests  # noqa: E402  (module import: its test classes stay uncollected here)
import test_web_design as design  # noqa: E402

STATIC = REPO / 'src/workbench/hub/web/static'
# Classes that only mark a table/section for tests and scripts; they carry no rule of their own.
HOOK_ONLY = {'board', 'requests', 'dot-off'}  # dot-off: the plain .dot is the off colour
VAR = re.compile(r'^var\(--([a-z0-9-]+)\)$')


def selector_text() -> str:
    rules = [rule for text in design.own_css().values() for rule in design.all_rules(text)]
    return ' , '.join(selector for selector, _ in rules)


class PageClassTests(dashboard_tests.DashboardCase):
    async def test_every_class_of_the_dashboard_markup_has_a_rule(self):
        self.populate()
        self.rows = [dashboard_tests.hub_row('alpha', self.a, waits=[{'code': 'paused'}])]
        _, page = await self.page('/dashboard')
        used = {name for _, attrs in page.elements for name in (attrs.get('class') or '').split()}
        selectors = selector_text()
        missing = sorted(name for name in used - HOOK_ONLY if not re.search(rf'\.{re.escape(name)}(?![\w-])', selectors))
        self.assertEqual(missing, [])

    async def test_every_class_dashboard_js_builds_has_a_rule(self):
        source = (STATIC / 'dashboard.js').read_text(encoding='utf-8')
        built = re.findall(r'el\("[a-z]+", "([^"]+)"', source) + re.findall(r'(?:cell|link)\(.*?, "([a-z-]+)"\)', source)
        names = {part for text in built for part in text.split()}
        names -= {'p-', 's-'}  # prefixes of the power/state chip kinds, checked below
        self.assertGreaterEqual(names, {'chip', 'none', 'wait', 'flag', 'meta', 'links', 'proj-link', 'title', 'proj', 'src', 'path',
                                        'when'})
        selectors = selector_text()
        missing = sorted(name for name in names if not re.search(rf'\.{re.escape(name)}(?![\w-])', selectors))
        self.assertEqual(missing, [])
        # the dynamic suffixes: power and state chip kinds
        for prefix, kinds in (('p', ('on', 'off', 'busy', 'unknown')),
                              ('s', ('new', 'reviewing', 'needs', 'ready', 'running', 'closed', 'unknown'))):
            for kind in kinds:
                self.assertRegex(selectors, rf'\.{prefix}-{kind}(?![\w-])')


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.css = design.own_css()
        self.rules = [rule for text in self.css.values() for rule in design.all_rules(text)]

    def declared(self, selector: str) -> dict[str, str]:
        """The declarations of the top-level (not inside @media) rules that name ``selector``."""
        merged: dict[str, str] = {}
        for text in self.css.values():
            for head, body in design.blocks(text):
                if not head.startswith('@') and selector in [part.strip() for part in head.split(',')]:
                    merged.update(design.declarations(body))
        return merged

    def test_the_summary_cards_wrap_into_as_many_columns_as_fit(self):
        columns = self.declared('.summary').get('grid-template-columns', '')
        self.assertRegex(columns, r'repeat\(auto-fit,\s*minmax\(\d+(\.\d+)?rem,\s*1fr\)\)')

    def test_a_card_and_the_two_columns_can_shrink_so_the_page_never_scrolls_sideways(self):
        for selector in ('.stat', '.primary', '.side'):
            with self.subTest(selector=selector):
                self.assertEqual(self.declared(selector).get('min-width'), '0')

    def test_the_layout_is_one_column_and_two_only_on_a_wide_screen(self):
        self.assertRegex(self.declared('.layout').get('grid-template-columns', ''), r'^minmax\(0,\s*1fr\)$')
        wide = []  # (min-width in px, grid-template-columns) of every min-width media rule that sets .layout
        for text in self.css.values():
            for head, body in design.blocks(text):
                match = re.search(r'min-width:\s*(\d+)px', head)
                if head.startswith('@media') and match:
                    wide += [(int(match.group(1)), design.declarations(inner).get('grid-template-columns', ''))
                             for selector, inner in design.all_rules(body) if '.layout' in selector]
        self.assertEqual(len(wide), 1, wide)
        self.assertGreaterEqual(wide[0][0], 900, 'two columns only where there is room for the 20 rem side column')
        self.assertRegex(wide[0][1], r'^minmax\(0,\s*1fr\)\s+\d+(\.\d+)?rem$')  # the flexible tables | a fixed side column

    def test_the_dashboard_tables_have_no_minimum_width_so_they_fit_instead_of_scrolling(self):
        # 2026-10-28: base.css gives table.data min-width 38rem (a table that scrolls inside .table-wrap); the dashboard's
        # two tables shrink to their card instead, and the cells that hold long text (names, paths, titles) may break.
        self.assertEqual(self.declared('.dash-card table.data').get('min-width'), '0')
        for text in self.css.values():
            for selector, body in design.all_rules(text):
                if re.search(r'\.dash-card.*(\.proj-link|td\.proj|\.path|td\.title|\.sub)', selector):
                    with self.subTest(selector=selector):
                        self.assertNotEqual(design.declarations(body).get('white-space'), 'nowrap')

    def test_below_45rem_the_dashboard_rows_stack_as_labelled_cards(self):
        narrow = []
        for text in self.css.values():
            for head, body in design.blocks(text):
                match = re.search(r'max-width:\s*(\d+(?:\.\d+)?)rem', head)
                if head.startswith('@media') and match and 44 <= float(match.group(1)) <= 45:
                    narrow += design.all_rules(body)
        by_selector = {selector.strip(): design.declarations(body) for selector, body in narrow}
        stacked = [decl for selector, decl in by_selector.items() if 'table.data.tcards' in selector and decl.get('display') == 'block']
        self.assertTrue(stacked, sorted(by_selector))
        labels = [decl for selector, decl in by_selector.items() if 'td[data-label]::before' in selector]
        self.assertEqual([decl.get('content') for decl in labels], ['attr(data-label)'])
        self.assertTrue(any('thead' in selector and 'tcards' in selector for selector in by_selector), 'the header row is hidden')

    def test_the_power_chips_carry_a_glyph_so_colour_is_not_the_only_cue(self):
        for kind in ('on', 'off', 'busy', 'unknown'):
            with self.subTest(kind=kind):
                rule = self.declared(f'.p-{kind}')
                self.assertTrue(rule.get('--g'), rule)
                self.assertRegex(rule.get('--cb', ''), VAR)
                self.assertRegex(rule.get('--cf', ''), VAR)

    def test_the_owner_waiting_card_is_tinted_with_the_warning_pair(self):
        rule = self.declared('.stat-attn')
        self.assertEqual((rule.get('background'), rule.get('color')), ('var(--warn-bg)', 'var(--warn-fg)'))


class ContrastTests(unittest.TestCase):
    """Every text colour a rule sets on a background it sets (or a chip's ``--cf`` on ``--cb``) is >= 4.5:1."""

    def pairs(self) -> list[tuple[str, str, str]]:
        found = []
        for name, text in design.own_css().items():
            for selector, body in design.all_rules(text):
                rule = design.declarations(body)
                for fore, back in (('color', 'background'), ('color', 'background-color'), ('--cf', '--cb')):
                    first, second = VAR.match(rule.get(fore, '')), VAR.match(rule.get(back, ''))
                    if first and second:
                        found.append((f'{name}: {selector.strip()}', first.group(1), second.group(1)))
        return found

    def test_there_are_pairs_to_check_including_the_dashboard_ones(self):
        selectors = ' '.join(selector for selector, _, _ in self.pairs())
        for needle in ('.stat-attn', '.p-on', '.flag', '.count'):
            self.assertIn(needle, selectors)

    def test_every_declared_pair_reaches_4_5_to_1_in_light_and_dark(self):
        tokens = design.token_blocks(design.own_css()['base.css'])
        for label, values in (('light', tokens['light']), ('dark', {**tokens['light'], **tokens['media-dark']})):
            for where, fore, back in self.pairs():
                if f'--{fore}' not in values or f'--{back}' not in values:
                    continue  # the chip's own --cf/--cb: each s-*/p-* rule maps them to a token pair, checked as --cf/--cb
                with self.subTest(theme=label, rule=where):
                    self.assertGreaterEqual(design.contrast(values[f'--{fore}'], values[f'--{back}']), 4.5,
                                            f'{fore} on {back}')


if __name__ == '__main__':
    unittest.main()
