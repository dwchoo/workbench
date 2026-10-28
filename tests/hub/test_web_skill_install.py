"""WBX-B11 end to end (seams S1 + S4): the install block of the served README, run by a shell in a temp HOME.

The Hub is the real web app in-process on a loopback port (``test_web_skill.SkillCase``). The shell block is taken out
of the README the Hub serves, run with ``bash`` and a controlled environment, and then ``wb_inbox.py config`` is run
from the installed folder. The block must work with ``curl`` and, when ``curl`` is missing, with ``python3`` alone.
Run with ``discover -s tests/hub -p 'test_web_skill*.py'``.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from test_web_skill import SKILLS, SkillCase  # noqa: E402

BASH = shutil.which('bash')
PYTHON = shutil.which('python3')
AGENTS = {'Claude Code': '.claude/skills/wb-inbox', 'Codex': '.agents/skills/wb-inbox',
          'OMP': '.omp/agent/skills/wb-inbox'}
TEST_ENV = 'WB_HUB_URL={origin}\nWB_API_KEY=wbk_test_only_not_a_real_key\n'


def install_block(readme: str, agent: str) -> str:
    sections = re.split(r'^### (.+)$', readme, flags=re.M)
    section = {sections[i]: sections[i + 1] for i in range(1, len(sections), 2)}[agent]
    return re.search(r'```sh\n(.*?)```', section, flags=re.S).group(1)


class InstallCase(SkillCase):
    async def asyncSetUp(self):
        await self.serve()
        self.origin = f'http://{self.host}'
        self.readme = (await self.get('/skill/README.md')).body.decode()
        self.home = self.base_dir / 'home'
        self.home.mkdir()

    async def run_block(self, block: str, path: str):
        env = {'HOME': str(self.home), 'PATH': path, 'LANG': 'C.UTF-8'}
        process = await asyncio.create_subprocess_exec(
            BASH, '-c', block, cwd=str(self.base_dir), env=env, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(process.communicate(), 60)
        return process.returncode, out.decode(), err.decode()

    async def config(self, folder: Path):
        process = await asyncio.create_subprocess_exec(
            PYTHON, '-I', str(folder / 'wb_inbox.py'), 'config', cwd=str(self.base_dir),
            env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'}, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(process.communicate(), 60)
        return process.returncode, out.decode(), err.decode()

    def check_installed(self, folder: Path):
        for name in ('SKILL.md', 'wb_inbox.py', '.env.example'):
            self.assertTrue((folder / name).is_file(), name)
        self.assertEqual((folder / 'wb_inbox.py').read_bytes(), (SKILLS / 'wb-inbox' / 'wb_inbox.py').read_bytes())
        self.assertIn(f'WB_HUB_URL={self.origin}', (folder / '.env.example').read_text())
        self.assertEqual(stat.S_IMODE((folder / '.env').stat().st_mode), 0o600)


class InstallTests(InstallCase):
    async def test_each_agent_block_installs_the_skill_and_the_client_then_runs(self):
        for agent, relative in AGENTS.items():
            with self.subTest(agent=agent):
                folder = self.home / relative
                code, out, err = await self.run_block(install_block(self.readme, agent), '/usr/bin:/bin')
                self.assertEqual(code, 0, (out, err))
                self.check_installed(folder)
                self.assertIn('"WB_HUB_URL"', out)
                # the owner puts the key in; the key never appeared in what the block printed
                (folder / '.env').write_text(TEST_ENV.format(origin=self.origin))
                os.chmod(folder / '.env', 0o600)
                code, out, err = await self.config(folder)
                self.assertEqual(code, 0, (out, err))
                self.assertIn('"status": "set"', out)
                self.assertNotIn('wbk_test_only', out + err)

    async def test_without_curl_the_same_block_downloads_with_python_alone(self):
        bin_dir = self.base_dir / 'bin'
        bin_dir.mkdir()
        for tool in ('python3', 'mkdir', 'cp', 'chmod'):
            (bin_dir / tool).symlink_to(shutil.which(tool))
        self.assertIsNone(shutil.which('curl', path=str(bin_dir)))
        for agent, relative in AGENTS.items():
            with self.subTest(agent=agent):
                code, out, err = await self.run_block(install_block(self.readme, agent), str(bin_dir))
                self.assertEqual(code, 0, (out, err))
                self.check_installed(self.home / relative)

    async def test_running_the_block_again_keeps_an_existing_env(self):
        block = install_block(self.readme, 'Claude Code')
        folder = self.home / AGENTS['Claude Code']
        self.assertEqual((await self.run_block(block, '/usr/bin:/bin'))[0], 0)
        (folder / '.env').write_text(TEST_ENV.format(origin=self.origin))
        self.assertEqual((await self.run_block(block, '/usr/bin:/bin'))[0], 0)
        self.assertIn('wbk_test_only_not_a_real_key', (folder / '.env').read_text())

    async def test_a_download_failure_is_reported_not_swallowed(self):
        block = install_block(self.readme, 'Codex').replace('/skill/wb-inbox/', '/skill/missing/')
        code, out, err = await self.run_block(block, '/usr/bin:/bin')
        self.assertNotEqual(code, 0, (out, err))


if __name__ == '__main__':
    unittest.main()
