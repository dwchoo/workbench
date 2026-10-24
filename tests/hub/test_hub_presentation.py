"""``workbench.hub.presentation``: list labels and columns shared by the TUI, ``wb hub list`` and (A) web.

Expected strings are the SPEC "상태 집계" 표시 label table and BRIEF HB-AC-12/19. Pure unit
tests (seam S7's presentation half). Run with ``discover -s tests/hub``.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from workbench.hub import presentation  # noqa: E402

NOW = 1_800_000_000.0


def row(**fields) -> dict:
    base = {'root': '/work/alpha', 'name': 'alpha', 'project_id': 'p', 'power': 'off', 'power_detail': None,
            'task': None, 'command': None, 'waits': [], 'pending_requests': None, 'last_activity': None,
            'backend': None, 'observed_at': NOW}
    base.update(fields)
    return base


class PendingRequestsTests(unittest.TestCase):
    def test_unknown_pending_requests_are_a_dash_not_zero(self):
        cells = presentation.row_cells(row(), NOW)
        self.assertEqual(cells[presentation.COLUMNS.index('요청')], '—')
        self.assertNotIn('0', cells[presentation.COLUMNS.index('요청')])


class LabelTests(unittest.TestCase):
    def cell(self, value: dict, column: str) -> str:
        return presentation.row_cells(value, NOW)[presentation.COLUMNS.index(column)]

    def test_columns_are_in_brief_order(self):
        self.assertEqual(presentation.COLUMNS,
                         ('이름', '경로', '상태', 'Task·단계', '명령', '대기 사유', '요청', '마지막 활동'))

    def test_power_labels(self):
        expected = {('on', None): '켜짐', ('off', None): '꺼짐', ('starting', None): '시작 중',
                    ('closing', None): '끄는 중', ('unknown', 'timeout'): '연결 없음/불명',
                    ('unknown', 'identity:record_unreadable'): '연결 없음/불명',
                    ('mismatch', 'project_id_mismatch'): '불일치(project_id_mismatch)',
                    ('mismatch', 'root_missing'): '불일치(root_missing)'}
        for (power, detail), label in expected.items():
            self.assertEqual(self.cell(row(power=power, power_detail=detail), '상태'), label, power)

    def test_wait_labels_keep_the_row_order(self):
        codes = ['paused', 'boot_confirmation_required', 'manager_editor_not_empty',
                 'manager_session_not_connected', 'metadata_unavailable', 'model_hold', 'task_held']
        waits = [{'code': code, 'since': None, 'detail': None} for code in codes]
        self.assertEqual(self.cell(row(power='on', waits=waits), '대기 사유'),
                         '일시정지, 재부팅 확인 전 보류, manager 입력창에 글이 있어 알림 대기, '
                         'manager 세션 연결 없음(알림 대기), metadata 저장 장애로 보류, 모델 오류로 보류, Task 보류')
        self.assertEqual(presentation.WAIT_LABELS['task_held'], 'Task 보류')

    def test_command_state_labels(self):
        for state, label in (('running', '실행 중'), ('stopping', 'stop 중'), ('unknown', '불명')):
            command = {'command_id': 'c-1', 'owner': 'worker', 'label': 'make test', 'state': state}
            self.assertEqual(self.cell(row(power='on', command=command), '명령'), f'make test ({label})')

    def test_task_and_stage(self):
        task = {'task_id': 't-7', 'kind': 'experiment', 'status': 'running', 'summary': 'sweep lr'}
        self.assertEqual(self.cell(row(power='on', task=task), 'Task·단계'), 'sweep lr · running')
        self.assertEqual(self.cell(row(power='on', task={**task, 'summary': None}), 'Task·단계'), 't-7 · running')

    def test_missing_values_are_a_dash(self):
        cells = presentation.row_cells(row(power='unknown', power_detail='timeout'), NOW)
        for column in ('Task·단계', '명령', '대기 사유', '요청', '마지막 활동'):
            self.assertEqual(cells[presentation.COLUMNS.index(column)], '—', column)
        self.assertEqual(cells[:2], ['alpha', '/work/alpha'])

    def test_last_activity_is_relative(self):
        for seconds, text in ((5, '5초 전'), (180, '3분 전'), (3 * 3600 + 5, '3시간 전'), (2 * 86400, '2일 전'),
                              (-30, '방금')):
            self.assertEqual(self.cell(row(power='on', last_activity=NOW - seconds), '마지막 활동'), text, seconds)


if __name__ == '__main__':
    unittest.main()
