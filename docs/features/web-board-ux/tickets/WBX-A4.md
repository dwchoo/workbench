# WBX-A4: Key 발급 화면의 client 설정 안내와 복사 버튼

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-A4`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). 기존 settings page 패턴의 UI와 정적 JS다. Key 원문 노출 범위는 reviewer 초점.

## 제공할 동작

SPEC "Key 발급 결과의 client 설정 안내": 발급 결과 page에 `.env` 블록(`WB_HUB_URL=<origin>`, `WB_API_KEY=<원문>`), README 링크(`<origin>/skill/README.md`, `skill_repo_url`이 있으면 GitHub 링크), agent용 한 줄 안내(영어, key 없음), 각 복사 버튼(`/static/copy.js`, clipboard → execCommand → 수동 안내 fallback). Key 목록 page에 `<YOUR_KEY>` 판 `<details>`. Task-inbox USAGE §3에 발급 화면 안내.

## 수락 기준

- WBX-AC-06: 발급 결과 HTML에 세 블록·복사 버튼, `.env`의 URL이 요청 Host 기준 origin(HTTP/HTTPS 모두), 한 줄 안내에 key 원문 없음, `skill_repo_url` 있음/없음 두 경우의 링크, 응답 `no-store`, 같은 form 재전송은 원문을 다시 보이지 않음(이름 중복 거절). 목록 page의 자리표시 판에 원문 없음.

## 계약과 맥락

선행: WBX-B3(`[web] skill_repo_url` config와 `/skill/README.md` route). `web/pages/settings.py`, `static/copy.js`, `docs/features/task-inbox/USAGE.md` §3.

## 검증

Seam은 SPEC s2 "테스트 결정"에서 사용자가 확정한 것이다(2026-10-27, WBX-D20·D21).
Seam: S1+S6, S8(`tests/hub/js/copy.test.mjs`: clipboard → execCommand → 수동 안내 순서, stub global). 독립 기대값: SPEC 절, BRIEF AC-06·WBX-D17·D19.
Behavior 순서: (1) `.env` 블록 (2) origin = Host (3) README·GitHub 링크 조건 (4) 한 줄 안내에 key 없음 (5) 재전송 거절 (6) 목록 자리표시 판 (7) copy 버튼 markup·script.
명령: node seam: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_board_state_js.py'`(새 `tests/hub/js/<name>.test.mjs`는 자동으로 포함, node 없으면 skip); `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_pages_settings*.py'`; 회귀 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_*.py'`.
독립 test_designer 필수. 초점: key 원문이 발급 결과 밖(목록·감사·log·오류 page)에 나오지 않음.

## 범위 밖과 중단 조건

Key 발급 규칙·형식 변경. 안내 문구는 영어(WBX-D26).

## 출력

변경 경로, 화면 예(원문은 가림), behavior별 RED/GREEN, 명령·결과.
