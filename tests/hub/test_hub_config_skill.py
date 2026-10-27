"""WBX-B3 ``hub.toml [web] skill_repo_url`` (seam: the ``workbench.hub.config`` parse API and template).

Expected values come from the web-board-ux SPEC "`/skill/` 제공, packaging, `skill_repo_url`": default ``""`` (none);
a value is at most 2048 characters of ASCII without whitespace or control characters, scheme ``https`` or ``http``, a
host, no userinfo; anything else is ``config_invalid:web.skill_repo_url``. Run with
``discover -s tests/hub -p 'test_hub_config*.py'``.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))

from workbench.hub import config  # noqa: E402


def parse(value: str, *, quote: bool = True) -> config.HubConfig:
    literal = json.dumps(value) if quote else value  # a JSON string is a valid TOML basic string
    return config.parse_config(f'[web]\nskill_repo_url = {literal}\n'.encode('utf-8'))


class SkillRepoUrlTests(unittest.TestCase):
    def test_the_default_is_none(self):
        self.assertEqual(config.HubConfig().web.skill_repo_url, '')
        self.assertEqual(config.parse_config(b'').web.skill_repo_url, '')
        self.assertEqual(parse('').web.skill_repo_url, '')

    def test_https_and_http_urls_with_a_host_are_accepted_as_written(self):
        for value in ('https://github.com/example/workbench-skills', 'http://git.internal:3000/a/b.git',
                      'https://example.com', 'https://example.com/path?x=1#frag', 'http://[::1]:8000/x',
                      'HTTPS://Example.com/Repo', 'https://' + 'a' * 63 + '.example/' + 'p' * 1900):
            with self.subTest(value=value[:60]):
                self.assertEqual(parse(value).web.skill_repo_url, value)

    def test_other_values_are_config_invalid_naming_the_key(self):
        bad = ['ftp://example.com/x', 'file:///etc/passwd', 'javascript:alert(1)', 'example.com/x', '//example.com/x',
               'https://', 'https:///path', 'https://user@example.com/x', 'https://user:pw@example.com/x',
               'https://exa mple.com/x', 'https://example.com/a b', ' https://example.com', 'https://example.com ',
               'https://example.com/\n', 'https://example.com/\t', 'https://example.com/\x7f', 'https://example.com/\x00',
               'https://example.com/é', 'https://한글.example/x', 'https://example.com:99999/x',
               'https://example.com:abc/x', 'https://example.com/' + 'p' * 2040, 'http:', 'https:example.com']
        for value in bad:
            with self.subTest(value=value[:60]):
                with self.assertRaises(config.ConfigInvalid) as caught:
                    parse(value)
                self.assertEqual(caught.exception.reason, 'config_invalid:web.skill_repo_url')

    def test_the_limit_is_2048_characters(self):
        head = 'https://example.com/'
        self.assertEqual(len(parse(head + 'p' * (2048 - len(head))).web.skill_repo_url), 2048)
        with self.assertRaises(config.ConfigInvalid):
            parse(head + 'p' * (2049 - len(head)))

    def test_a_non_string_is_config_invalid(self):
        for literal in ('true', '1', '[]', '["https://example.com"]', '{}'):
            with self.subTest(literal=literal):
                with self.assertRaises(config.ConfigInvalid) as caught:
                    parse(literal, quote=False)
                self.assertEqual(caught.exception.reason, 'config_invalid:web.skill_repo_url')

    def test_other_web_keys_are_unaffected(self):
        parsed = config.parse_config(b'[web]\nenabled = true\nport = 9000\nskill_repo_url = "https://example.com/r"\n')
        self.assertEqual((parsed.web.enabled, parsed.web.port, parsed.web.skill_repo_url),
                         (True, 9000, 'https://example.com/r'))


class TemplateTests(unittest.TestCase):
    def test_the_template_documents_the_key_in_korean_and_still_parses_to_the_defaults(self):
        self.assertEqual(config.parse_config(config.CONFIG_TEMPLATE.encode('utf-8')), config.HubConfig())
        lines = [line for line in config.CONFIG_TEMPLATE.splitlines() if 'skill_repo_url' in line]
        self.assertIn('# skill_repo_url = ""', lines)
        self.assertTrue([line for line in lines if line.startswith('#') and '기본' in line], lines)

    def test_uncommenting_the_key_keeps_the_defaults(self):
        text = config.CONFIG_TEMPLATE.replace('# skill_repo_url = ""', 'skill_repo_url = ""')
        self.assertNotEqual(text, config.CONFIG_TEMPLATE)
        self.assertEqual(config.parse_config(text.encode('utf-8')), config.HubConfig())


if __name__ == '__main__':
    unittest.main()
