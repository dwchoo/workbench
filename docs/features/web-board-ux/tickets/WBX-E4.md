# WBX-E4: 트랙 E 문서와 통합 확인

명세: [../SPEC.md](../SPEC.md) s5 "트랙 E"와 "문서 갱신 배정"의 s5 행. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-E4`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`) 문서 + orchestrator 통합 확인. 통합된 구현을 사용자 안내로 옮기고 트랙 E의 수락 기준별 증거를 모은다.

## 제공할 동작

1. task-inbox USAGE를 트랙 E 현재 동작으로 고친다: §6에 "작업 공간에서 열기", §7에 프로젝트 pane widget 절(panel "프로젝트" 묶음, 보기·입력, TUI와 함께 쓰기, 크기 따름, Ctrl-d 두 번, host 입력 주인과 거절 이유 문장, 버튼과 확인, 실패 표시, 이전 판 backend는 끄고 켜기), §8 감사 `pane_*`(keystroke는 남지 않음), §10.3 pane id의 4404·4429.
2. core-workbench SPEC:211(`ui_v1` 설명)에 공유 attach 개정 주석, CONTEXT.md에 "프로젝트 pane widget" 용어. 2026-10-29 r0.6 개정 주석(task-inbox BRIEF D-08·D-09·P2-AC-21, task-inbox SPEC:318·:515, 이 feature BRIEF "범위와 비목표")이 구현과 맞는지 확인한다.
3. 통합 확인: WBX-AC-39~47별 증거 표(ticket 검증 결과, 회귀, 사용자 확인 `pending`), 디렉터리별 회귀, `graphify update .`(repo 루트).

## 수락 기준

WBX-AC-39~47의 최종 통합(실제 TUI·browser·IME·두 기기 확인은 `pending`으로 명시, WBX-O09).

## 계약과 맥락

선행: WBX-E1·E2·E3 통합. 문서는 한국어, 기존 USAGE 문체. Skill 문서(영어)는 바꾸지 않는다.

## 검증

문서는 TDD 대상이 아니다(S5 정적 확인: USAGE에 "웹에는 pane 입력 수단이 없다" 같은 옛 문구가 현재 동작으로 남지 않음, 감사 event 이름·close code가 SPEC s5와 같음).
회귀(공통 접두 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest`): `discover -s tests/hub`, `discover -s tests/contracts`, `discover -s tests/backend -p 'test_ui_server*.py'`, `discover -s tests/backend -p 'test_panes.py'`, `discover -s tests/backend -p 'test_command_stop*.py'`, `discover -s tests/ui -p 'test_product_*.py'`.
격리 실행 주의: 회귀가 실제 Hub·backend를 띄우므로 CLAUDE.md sandbox 규칙과 전후 `ls /tmp/omp-workbench-<uid> | wc -l`·`du -sh .workflow graphify-out .worktrees` 보고. 실제 curses TUI와 웹 WebSocket을 함께 쓰는 시험 조합은 사용자 확인(WBX-O10) 전에는 쓰지 않는다.

## 범위 밖과 중단 조건

구현 변경(차이를 찾으면 E1~E3 계약 차이로 보고), 사용자 확인 대행.

## 출력

변경 문서 경로, 수락 기준별 증거 표, 회귀 명령·결과, 사용자 확인 목록(WBX-O09).
