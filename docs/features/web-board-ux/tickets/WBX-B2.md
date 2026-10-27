# WBX-B2: `wb-inbox` skill과 `wb_inbox.py`

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-B2`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). stdlib CLI와 문서다. Key 위생은 reviewer 초점.

## 제공할 동작

`skills/README.md`, `skills/wb-inbox/{SKILL.md,wb_inbox.py,.env.example}`(문서·주석 영어), `.gitignore`의 `skills/**/.env`. CLI는 SPEC "`wb-inbox` skill" 표(command, 출력, exit code, env 조회, `.env` 문법·권한 경고, URL 검사, redirect 거부, 기본 idempotency key, key 위생). README: 첫 줄(개발용 `.agents/skills/`와 다름), 설치(`/skill/` 링크 또는 repo에서 복사), `.env` 위치·`chmod 600`, curl 예(`curl -H "Authorization: Bearer $WB_API_KEY" "$WB_HUB_URL/api/v1/projects"`), 자체 서명 TLS(`SSL_CERT_FILE` 안내만, WBX-D23), `<!-- wb:skill-repo -->` 표지 줄, 자리표시 `http://YOUR-HUB-HOST:8780`.

## 수락 기준

- WBX-AC-13: 파일 4개와 README 첫 줄; `python3 -I wb_inbox.py`가 pip 없이 실행; requester key로 projects·submit·list·get·comment·cancel·result, editor key로 + update·set-state가 S2 Hub에서 성공.
- WBX-AC-14: 조회 순서(변수별), 0600보다 넓은 `.env` 경고, 값 없음 → exit 3과 찾은 위치·변수 이름, 모든 경로(성공·4xx·5xx·연결 오류·사용법 오류)의 stdout·stderr에 key 문자열 없음.

## 계약과 맥락

선행: WBX-B1(REST와 `support/rest_hub.py`). 영어 문서 규칙(AGENTS.md). 새 의존성 없음.

## 검증

Seam은 SPEC s2 "테스트 결정"에서 사용자가 확정한 것이다(2026-10-27, WBX-D20·D21).
Seam: S4(주), S5(파일·첫 줄·stdlib import만). 독립 기대값: SPEC 표.
Behavior 순서: (1) `config`의 조회 순서 (2) 값 없음 exit 3 (3) `.env` 권한 경고 (4) URL 검사 (5) projects (6) submit + 기본 idempotency key·`--new`(새 요청 강제, `--idempotency-key`와 함께면 exit 2, WBX-D25) (7) list·get (8) comment (9) update·set-state(editor, requester 403 → exit 1) (10) cancel 2단계 (11) result page (12) 연결 오류 exit 4·redirect 거부 (13) key 비노출 전수.
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_wb_inbox*.py'`.
문서(README·SKILL.md·.env.example)는 TDD 예외, S5 정적 확인.
독립 test_designer 필수. 초점: 예외 경로의 key 노출, `.env` parser의 이상 입력.
격리 실행 주의: 실제 Hub·OMP·isolation home을 띄운다. CLAUDE.md "테스트 실행"의 sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일)을 지킨다. Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시(`*/.omp/natives/`, Board home). 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

`/skill/` 제공·packaging(WBX-B3). 사람용 표 출력. 새 env 변수(TLS는 `SSL_CERT_FILE` 안내만, WBX-D23).

## 출력

파일 목록, CLI 도움말 출력, behavior별 RED/GREEN, 명령·결과.
