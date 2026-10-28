# WBX-B12: 공유 OMP 로그인 완화

명세: [../SPEC.md](../SPEC.md) s3. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-B12`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Opus(`model: "opus"`). Process 수명·signal 순서(manager·worker·Board 공통 격리 확인)와 인증 저장소 경계다.

## 제공할 동작

SPEC "공유 OMP 로그인 완화"(WBX-D32). 배경: [../INCIDENT-20261027-omp-login.md](../INCIDENT-20261027-omp-login.md).
1. 격리 확인 OMP의 graceful 종료: `backend/launcher.py:_stop_group`(과 `check_isolation`의 timeout·cancel 경로)을 stdin EOF → 최대 5 s → SIGTERM → 최대 5 s → SIGKILL → 3 s 순서로. 결과 `cleanup`에 끝난 단계(`eof`/`sigterm`/`sigkill`)와 걸린 시간. 전체 상한 13 s + reap.
2. Board OMP log 보존: `<hub state>/board-agent/logs/`(0700, 파일 0600)에 Board 시작마다 격리 확인 OMP의 stderr 전체와 판정 요약, OMP 자체 log(위치 확인, 없으면 생략), Board agent OMP의 종료 상태. 파일당 1 MiB(끝을 남김), 최근 10회분. System key 문자열은 쓰기 전에 가린다. 인증 저장소 내용 복사 없음.
3. Test helper의 `agent.db` 복사 제거: `tests/integration/live_real_summary.py`의 `_copy_auth`/`--auth-source`를 없애거나 C-D64의 symlink로 바꾸고(실제 로그인 공유가 필요하면), 그 독립 테스트(`tests/integration/test_real_summary_independent.py`)를 새 계약으로. `tests/**`·`tools/**`에 실제 `agent.db`(·`-wal`·`-shm`)를 복사하는 코드가 없음을 확인하는 정적 검사를 더한다(temp HOME의 빈 placeholder 쓰기는 허용).

## 수락 기준

- WBX-AC-23(1): fake OMP가 stdin EOF에서 끝나면 SIGTERM·SIGKILL을 받지 않는다(fake가 받은 signal 기록); EOF를 무시하고 SIGTERM에서 끝나면 SIGKILL을 받지 않고, 그 전 EOF 유예를 다 기다렸다; 둘 다 무시하면 SIGKILL로 끝나고 전체가 상한 안; `cleanup`의 단계·시간 값; 자식 process group이 남지 않음; manager·worker 격리 검사의 판정(`ok`·leak)은 바뀌지 않는다.
- WBX-AC-23(2): 실제 `wb _hub --foreground` + fake OMP Board 시작 뒤 log 디렉터리·파일 mode, 격리 확인 stderr 전체(64 KiB 넘는 출력도), Hub·Board 재시작 뒤에도 남음, 11번째 시작에서 가장 오래된 것 삭제, 1 MiB 넘는 출력은 끝 1 MiB, log 어디에도 이번 system key 문자열 없음.
- WBX-AC-23(3): 정적 검사가 현재 repo에서 통과하고, `agent.db`를 `shutil.copy*`·읽어서 쓰기로 복사하는 견본 코드를 넣으면 실패한다(검사 자체의 RED).
- 실제 OMP 18.12.6이 RPC stdin EOF로 끝나는지와 시간: 1회 관측 기록(격리 home, 모델 호출 없음, 사용자 인증 저장소 대신 빈 placeholder).

## 계약과 맥락

`backend/launcher.py`(`_stop_group`, `check_isolation`, `ISOLATION_CHECK_TIMEOUT`), `backend/service.py`(manager·worker의 `check_isolation` 호출), `hub/board/launch.py`·`hub/board/set.py`(Board 시작·격리 확인), `tests/integration/live_real_summary.py`. C-D64(인증은 symlink 공유, 내용을 읽지 않음). Workbench는 인증 저장소를 열지 않는다. 격리 확인 시간 상한이 늘어나는 것이 manager·worker 시작 timeout과 맞는지 확인한다.

## 검증

Seam은 SPEC s2·s3 "테스트 결정"의 확정 seam과 기존 backend 격리 seam이다(2026-10-27, WBX-D20·D21; s3에서 새 seam 없음).
Seam: 기존 backend 격리 seam(`tests/backend/test_omp_isolation*.py`, `tests/backend/test_paths_launcher.py`의 fake OMP로 `check_isolation` 호출, fake가 EOF·signal 수신을 파일에 기록), S3(`fixtures/web_sandbox.py`의 실제 Hub + `fixtures/fake_omp_board.py`, `/proc`·파일 관측), S5(repo 정적 검사). 독립 기대값: SPEC 절의 단계·상한·위치, fake의 signal 기록, 파일 mode.
Behavior 순서: (1) EOF에서 끝나는 fake → signal 없음 (2) EOF 무시·SIGTERM 종료 → SIGKILL 없음 (3) 모두 무시 → SIGKILL·상한 (4) timeout·cancel 경로도 같은 순서 (5) `cleanup` 단계·시간 (6) manager·worker 판정 불변 (7) Board log 디렉터리·mode (8) stderr 전체 보존 (9) 재시작 뒤 남음 (10) 개수·크기 상한 (11) key 가림 (12) 정적 검사 RED(견본) → GREEN (13) `live_real_summary` 복사 제거·독립 테스트 갱신 (14) 실제 OMP EOF 관측(기록, TDD 아님).
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/backend -p 'test_omp_isolation*.py'`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/backend -p 'test_paths_launcher.py'`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_board_*.py'`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/integration -p 'test_real_summary_independent.py'`, 정적 검사 module(B12가 정하는 이름, 예 `tests/backend/test_no_auth_copy.py`)은 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/backend -p 'test_no_auth_copy*.py'`; 회귀 `-p 'test_launcher*.py'`(tests/backend), `-p 'test_board_*.py'`·`-p 'test_hub_web_*.py'`(tests/hub).
독립 test_designer(Sonnet) 필수. 초점: SIGKILL이 graceful 단계보다 먼저 가는 경로(예외·cancel·timeout), PID 재사용 없이 자기 group만 signal, log에 key·인증 내용 없음, 상한을 넘는 log가 디스크를 키우지 않음.
격리 실행 주의: 실제 Hub·OMP·isolation home을 띄운다. CLAUDE.md "테스트 실행"의 sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일)을 지킨다. 실제 OMP 관측은 빈 placeholder `agent.db`의 temp HOME으로 하고 사용자 `~/.omp/agent/agent.db`를 symlink하지 않는다. Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시(`*/.omp/natives/`, Board home). 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

인증 저장소 열기·복구·동기화, OMP auth-broker, `wb hub status`·웹 화면 변경, Board 전환(WBX-B5), 모델 호출. 실제 OMP가 stdin EOF로 끝나지 않아 확인마다 5 s가 늘어나면 멈추고 관측값과 함께 보고한다(유예 조정은 orchestrator 판정). `live_real_summary`의 사용처가 symlink로는 성립하지 않으면(사본이 꼭 필요하면) 지우기 전에 보고한다.

## 출력

변경 경로, 종료 단계 관측 표(fake·실제 OMP), log 디렉터리 예(값 가림), 정적 검사 결과, behavior별 RED/GREEN, 명령·결과.
