# WBX-B11: Agent별 skill 설치 README

명세: [../SPEC.md](../SPEC.md) s3. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-B11`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). 문서와 기존 `/skill/` 치환 위의 설치 block, 기존 패턴의 test다.

## 제공할 동작

SPEC "README 설치 계약"(WBX-D30·D35):
1. `skills/README.md`(영어): 첫 줄과 `<!-- wb:skill-repo -->` 표지는 유지. 파일 표에 `SKILL.md`·`wb_inbox.py`·`.env.example` 각각의 repo 상대 링크와 Hub 내려받기 주소(`http://YOUR-HUB-HOST:8780/skill/wb-inbox/<file>`).
2. `### Claude Code`, `### Codex`, `### OMP` 절: 사용자 폴더(와 project 폴더), 출처(공식 문서 URL, 없으면 실행 파일 버전과 확인 방법), `sh` block 하나(폴더 생성 → 세 파일 내려받기 `curl -fsS`/`python3 -I` fallback → `.env`가 없을 때만 `.env.example`에서 생성 → `chmod 600` → `wb_inbox.py config`). 실패는 0 아닌 exit. Block에 key 없음.
3. Key 발급 화면(`web/pages/settings.py`)의 agent용 한 줄 안내를 "README의 자기 agent 절을 따라 모든 파일 설치"를 말하는 영어 문장으로(README URL, `.env` 위치, key 원문 없음, WBX-D17·D26). 목록 화면의 `<YOUR_KEY>` 판도 같은 문장. Task-inbox USAGE §3의 예문을 맞춘다.
4. `/skill/` 제공은 바꾸지 않는다(두 치환 그대로). 제공본에 `http://YOUR-HUB-HOST:8780`이 남지 않는다.

## 수락 기준

- WBX-AC-22: (a) repo README에 세 파일의 상대 링크가 있고 각 링크가 repo 파일을 가리킨다; (b) 제공 README에 세 파일의 절대 `<origin>/skill/wb-inbox/<file>` 주소가 있고 각각 200; (c) 상대 링크를 `<origin>/skill/README.md` 기준으로 풀면 같은 제공 파일(200); (d) 세 agent 절이 있고 각 절의 폴더에 출처가 적혀 있다; (e) 제공 README에서 꺼낸 각 절의 block을 temp HOME에서 `bash`로 실행하면 그 agent 폴더에 세 파일이 설치본과 같은 bytes로 생기고(`.env.example`은 치환본), `.env`가 0600으로 생기며 `config`가 exit 0(key 자리를 test key로 채운 경우)·3(자리표시 그대로인 경우)이다; (f) `curl`이 PATH에 없을 때도 (e)가 성립한다; (g) 내려받기 실패(404·연결 거절)는 0 아닌 exit이고 폴더에 부분 파일을 정상 설치로 남기지 않는다; (h) block 실행이 기존 `.env`를 덮지 않는다; (i) key 발급 화면의 한 줄 안내에 README URL과 agent 절 안내가 있고 key 원문이 없다.
- WBX-AC-13·15 회귀: README 첫 줄, 고정 목록, 치환 규칙, wheel 포함 그대로.

## 계약과 맥락

선행: WBX-B3(`/skill/` 제공·packaging). `skills/README.md`, `web/pages/settings.py`, `docs/features/task-inbox/USAGE.md` §3, 테스트 `tests/hub/test_web_skill*.py`, `tests/hub/test_wb_inbox_static.py`, `tests/hub/test_web_pages_settings*.py`. Agent별 폴더는 근거로 정한다(공식 문서 또는 실행 파일; 참고: WBX-B4 probe의 미끼 경로 `~/.claude/skills`, `~/.codex/skills`, `~/.agents/skills`, `~/.omp/agent/skills`는 후보일 뿐 근거가 아니다). 작업 트리에 이 ticket 범위의 미commit 변경이 있을 수 있다: 시작 전 `git status`로 baseline을 대조한다.

## 검증

Seam은 SPEC s2·s3 "테스트 결정"의 확정 seam이다(2026-10-27, WBX-D20·D21; s3에서 새 seam 없음).
Seam: S5(repo README 정적: 링크·절·출처·첫 줄), S1(in-process Hub의 `/skill/` 제공본과 key 발급 화면), S4 방식의 subprocess(제공 README에서 꺼낸 block을 `bash`로, temp HOME·통제한 `PATH`·env, 끝에 `python3 -I wb_inbox.py config`). 독립 기대값: SPEC "README 설치 계약", 설치본 파일 bytes.
Behavior 순서: (1) repo 상대 링크 (2) 제공본 절대 주소·200 (3) 상대 링크 해석 (4) 세 절과 출처 (5) Claude Code block 설치 (6) Codex block (7) OMP block (8) `curl` 없음 fallback (9) 실패 exit·부분 파일 (10) 기존 `.env` 보존 (11) key 발급 화면 문장 (12) 회귀.
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_skill*.py'`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_wb_inbox*.py'`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_pages_settings*.py'`; 회귀 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/backend/installation -p 'test_package_assets*.py'`.
README 문장 자체는 TDD 예외(S5 정적 확인). 설치 block의 동작과 화면 문장은 RED → GREEN.
독립 test_designer(Sonnet) 필수. 초점: block이 HOME 밖이나 사용자 실제 폴더에 쓰지 않음(temp HOME), 제공본에 key·`.env` 없음, 다른 Host로 받은 README의 주소가 그 Host.
Sandbox: in-process Hub와 subprocess block은 temp HOME·`XDG_CONFIG_HOME`, 짧은 private `XDG_RUNTIME_DIR`, `WORKBENCH_HUB_AUTOSTART=0`. 실제 사용자 `~/.claude`·`~/.codex`·`~/.agents`·`~/.omp`를 건드리지 않는다. 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일.

## 범위 밖과 중단 조건

`/skill/` 고정 목록·치환 규칙 변경, `wb_inbox.py` 동작 변경, 새 env 변수. 실제 agent(Claude Code·Codex·OMP) 실행은 대상 host `pending`. 어느 agent의 폴더도 근거를 찾지 못하면 추정하지 말고 README에 확인 방법을 적은 뒤 보고한다.

## 출력

변경 경로, agent별 폴더와 출처 표, 제공 README 발췌(주소 부분), behavior별 RED/GREEN, 명령·결과.
