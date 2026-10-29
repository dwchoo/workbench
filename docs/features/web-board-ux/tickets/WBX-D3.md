# WBX-D3: 트랙 D 문서와 통합 확인

명세: [../SPEC.md](../SPEC.md) s4 "트랙 D"와 "문서 갱신 배정"의 s4 행. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-D3`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`) 문서 + orchestrator 통합 확인. 통합된 구현을 사용자 안내로 옮기고 트랙 D·A8의 수락 기준별 증거를 모은다.

## 제공할 동작

1. task-inbox USAGE를 트랙 D 현재 동작으로 다시 쓴다: §7 머리(여러 agent·shell, workspace), §7.2(공유 연결, 크기 따름, terminal당 8 연결, 재연결, close code; lease·인수·"다시 가져오기" 삭제), §7.3(재시작·중지·삭제·닫기와 확인), §7.4(agent별 home `board-agent/`·`board-agents/<id>/`, agent별 system key, 출처 "Board agent · <이름>", 메모리 삭제의 범위), §7.5(정지 확인 목록), §8(감사 event), §10.3(close code v2). 한도·이름 규칙·좁은 화면·브라우저별 배치 기억을 넣는다.
2. 2026-10-29에 단 개정 주석(task-inbox BRIEF D-25·D-26·D-28·P2-AC-34·35, task-inbox SPEC 해당 절, ADR-0003, CONTEXT.md)이 구현과 맞는지 확인하고, CONTEXT.md의 "(트랙 D 완료 전에는 현행)" 표시를 지운다.
3. 통합 확인: 트랙 D와 A8의 수락 기준(WBX-AC-24~38)별 증거 표(ticket 검증 결과, 회귀, 사용자 확인 `pending`), 디렉터리별 회귀, `graphify update .`(repo 루트).

## 수락 기준

WBX-AC-24~38의 최종 통합(사용자 browser·IME 확인은 `pending`으로 명시, WBX-O06).

## 계약과 맥락

선행: WBX-D1·D2(그리고 그 앞의 A8) 통합. 문서는 한국어, 기존 USAGE 문체. Skill 문서(영어)는 바꾸지 않는다.

## 검증

문서는 TDD 대상이 아니다(S5 정적 확인: 없어진 4001·"다시 가져오기"·`/ws/board`·`/board/restart` 문구가 USAGE에 남지 않음, 새 close code 표가 SPEC s4와 같음).
회귀(공통 접두 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest`): `discover -s tests/hub`, `discover -s tests/backend -p 'test_omp_isolation*.py'`, `discover -s tests/backend/installation`.
격리 실행 주의: 회귀가 실제 Hub를 띄우므로 CLAUDE.md sandbox 규칙과 전후 `ls /tmp/omp-workbench-<uid> | wc -l`·`du -sh .workflow graphify-out .worktrees` 보고.

## 범위 밖과 중단 조건

구현 변경(차이를 찾으면 D1·D2 계약 차이로 보고), 사용자 확인 대행.

## 출력

변경 문서 경로, 수락 기준별 증거 표, 회귀 명령·결과, 사용자 확인 목록(WBX-O06).

## D3 결과 (2026-10-29, 문서 + 통합 확인)

변경 문서: `docs/features/task-inbox/USAGE.md`(§1.2 `shell` 줄·웹 꺼짐 문구, §7 전체, §8 감사, §10.1 `board_agent:` 줄, §10.3 close code v2, §11 한계), `docs/features/workbench-hub/USAGE.md`(Board 상태 줄·정지 확인 목록), `README.md`(Web Board 문단), `CONTEXT.md`("트랙 D 완료 전" 표시 제거, 세트 용어를 공유 연결로). USAGE §7 구성: 머리·화면(panel·widget·이름·배치 기억·좁은 화면)·7.1 위험·7.2 공유 연결·7.3 시작/중지/재시작/닫기/삭제·7.4 home·key·출처·shell program·7.5 정지.
S5 정적 확인: USAGE에 `4001`·"다시 가져오기"·`/ws/board`(id 없는)·`/board/restart`·lease·"Board agent 세트" 문구가 남지 않음. close code 표는 SPEC s4 "Close code v2"(1000·1001·1009·4002·4003·4400·4401·4403·4404·4429)와 같고 화면 자체 코드 4000을 따로 적음.
회귀: 이 문서의 보고(오케스트레이터 최종 보고)에 둔다. 사용자 확인(WBX-O06)은 `pending`.
