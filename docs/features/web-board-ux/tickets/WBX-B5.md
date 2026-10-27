# WBX-B5: Board agent를 내부 REST와 `wb-inbox` skill로 전환

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-B5`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Opus(`model: "opus"`). Board agent 프로세스 env·격리·system key 경계다.

## 제공할 동작

SPEC "Board agent 전환": 내부 listener가 `/mcp` 대신 REST(`rest.py`의 internal app·`InternalListener`), Board agent env `WB_API_KEY`(system key)·`WB_HUB_URL`(내부 `http://127.0.0.1:<port>`), `process.PROJECT_ENV`에 `WB_API_KEY`·`WB_HUB_URL` 추가, Board shell env에 둘 다 없음, Board skill 복사 디렉터리, overlay(WBX-B4의 구성), 이전 `mcp.json` 삭제, 격리 검사(`allowed_skills=("wb-inbox",)`, MCP 허용 없음, `skill_missing:wb-inbox`), `omp_bridge/board/ROLE.md` 재작성(영어), `fixtures/fake_omp_board.py`가 skill 목록을 보고하도록 확장, 기존 Board 테스트(`test_board_launch`, `test_board_set`, `test_board_independent`, `test_web_pages_board`, `test_hub_web_reload`, `test_hub_web_enabled_independent`)의 MCP 기대를 새 계약으로.

## 수락 기준

- WBX-AC-16: Board agent `/proc/<pid>/environ`에 이번 system key와 내부 URL; shell에는 둘 다 없음; 사용자 shell에서 물려받은 `WB_API_KEY`·`WB_HUB_URL`은 Hub·Board 어디에도 없음; 내부 listener `/api/v1/requests`가 이번 system key로 동작·다른 유효 key와 이전 system key는 401·`/mcp` 404; fake OMP가 다른 skill이나 MCP tool을 보이면 `isolation_failed`, `wb-inbox`가 없으면 `skill_missing`; `allow_from = ["10.0.0.0/8"]`(loopback 제외)에서도 Board REST 성공; manager·worker 누출 검사 테스트 통과(`tests/backend/test_omp_isolation*.py`).
- WBX-AC-12(내부 부분): 내부 listener에 `/mcp` 없음.

## 계약과 맥락

선행: WBX-B4 passed(구성), WBX-B1, WBX-B3(packaging: 설치본에서 `canonical_path("skills/wb-inbox/…")`). `hub/board/launch.py`, `hub/process.py`, `hub/server.py`, `hub/rest.py`, `hub/mcp.py`(internal 부분 이동), `omp_bridge/board/ROLE.md`. 내부 listener는 이번 시작의 system key만 받는다(WBX-D24). Hub가 켠 manager·worker에도 두 변수가 없음을 수용(WBX-D28).

## 검증

Seam은 SPEC s2 "테스트 결정"에서 사용자가 확정한 것이다(2026-10-27, WBX-D20·D21).
Seam: S3(주, 기존 T3·T6b 재사용), S1(내부 app in-process). 독립 기대값: SPEC 절, `/proc` 값, BRIEF AC-16.
Behavior 순서: (1) Hub env 위생 (2) shell env (3) agent env (4) 내부 listener REST·`/mcp` 404·Host 고정 (5) skill 복사 디렉터리·`mcp.json` 삭제 (6) 격리 통과 (7) MCP leak 거절 (8) 다른 skill 거절 (9) `skill_missing` (10) loopback 뺀 allow_from (11) ROLE.md 정적(TDD 예외).
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_board_*.py'`, `-p 'test_web_pages_board.py'`, `-p 'test_hub_web_*.py'`; `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/backend -p 'test_omp_isolation*.py'`.
독립 test_designer 필수. 초점: system key가 shell·감사·log·status·`/proc/<hub>/environ`에 없음, Board 재시작마다 key 교체, 이전 key 401.
격리 실행 주의: 실제 Hub·OMP·isolation home을 띄운다. CLAUDE.md "테스트 실행"의 sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일)을 지킨다. Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시(`*/.omp/natives/`, Board home). 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

`mcp.py` 전체 삭제(WBX-B8). WBX-B4가 passed가 아니면 시작하지 않는다. 실제 OMP·모델 동작은 대상 host `pending`.

## 출력

변경 경로, env 관측 표(값은 가림), behavior별 RED/GREEN, 명령·결과.
