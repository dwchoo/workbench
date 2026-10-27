# WBX-B3: `/skill/` 제공, packaging, `skill_repo_url`

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-B3`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). 고정 목록 정적 제공과 config key 추가다. 공개 route라 reviewer 보안 초점.

## 제공할 동작

SPEC "`/skill/` 제공": 공개 GET route 4개(고정 목록, 그 밖 404, `.env` 없음), 두 가지 치환, `text/plain; charset=utf-8`·`no-cache`. `build_support/assets.py` `BuildPy`가 `skills/`를 `_assets/skills/`로 복사(`.env` 제외), `MANIFEST.in`. `hub.toml [web] skill_repo_url` parse·검사·템플릿 주석. Route audit EXPECTED에 `/skill/…` public pin.

## 수락 기준

- WBX-AC-15: 로그인 없이 4개 파일, `WB_HUB_URL` 자리가 요청 origin(HTTP/HTTPS, IP·server_name)으로 치환, 나머지 bytes = 설치본, `wb_inbox.py`는 무치환, README의 skill-repo 줄 조건, 응답에 key 없음.
- WBX-AC-11(skill 부분): `allow_from` 밖 403 본문 없음, `[web] enabled=false`면 없음(S3), 내부 listener에는 없음.
- `skill_repo_url` 잘못된 값 → `config_invalid:web.skill_repo_url`, 템플릿은 여전히 `HubConfig()`로 parse.
- Wheel에 `workbench/_assets/skills/{README.md,wb-inbox/SKILL.md,wb-inbox/wb_inbox.py,wb-inbox/.env.example}`가 있고 `.env`는 없다(repo에 `.env`를 둔 상태로 build).

## 계약과 맥락

선행: WBX-B2(파일). `hub/config.py`, `web/pages/skill.py`(새), `web/app.py`, `build_support/assets.py`, `MANIFEST.in`, `tests/hub/test_web_route_audit.py`, `tests/backend/installation/test_package_assets*.py`.

## 검증

Seam은 SPEC s2 "테스트 결정"에서 사용자가 확정한 것이다(2026-10-27, WBX-D20·D21).
Seam: S1(route·치환·allow_from), S3(enabled=false), S5(wheel·bytes). 독립 기대값: 설치본 파일 bytes, SPEC 절.
Behavior 순서: (1) README 제공·치환 (2) 나머지 3개 (3) 그 밖 경로 404·`.env` 404 (4) content type·cache (5) skill-repo 줄 (6) config 검사 (7) allow_from (8) enabled=false (9) wheel 포함·`.env` 제외.
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_skill*.py'`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_hub_config*.py'`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/backend/installation -p 'test_package_assets*.py'`, route audit.
격리 실행 주의: 실제 Hub·OMP·isolation home을 띄운다. CLAUDE.md "테스트 실행"의 sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일)을 지킨다. Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시(`*/.omp/natives/`, Board home). 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

Key 발급 화면(WBX-A4). 경로 해석으로 임의 파일을 여는 구현 금지.

## 출력

변경 경로, 치환 전후 예, behavior별 RED/GREEN, 명령·결과.
