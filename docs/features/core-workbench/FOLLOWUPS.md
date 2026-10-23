# Core Workbench 후속 과제

상태 기준: 2026-10-09, 최종 후보 C18(`c47456a`) 위에 최종 검증 문서 커밋 `9a70f36`. CW-16은 `done_verified`로 판정했다([VERIFICATION.md](VERIFICATION.md) §1). 아래 항목은 완료를 막지 않지만, 다음 작업에서 다룰 후보다. 착수 전에 사용자 결정이 필요한 항목은 따로 표시했다.

## 1. 기능 후속(사용자 결정 필요)

| 항목 | 내용 | 근거·출발점 |
|---|---|---|
| 24-bit 트루컬러 표시 | 지금은 C-D73에 따라 256색으로 근사한다. 원본 RGB를 그대로 보이게 하려면 렌더링 방식을 바꿔야 한다. | [FOLLOWUP-TRUECOLOR.md](FOLLOWUP-TRUECOLOR.md), [C-D73](DECISIONS.md#C-D73) |
| Phase 2: Task Inbox & Web Board | AGENTS.md와 BRIEF가 1차 core 이후로 미룬 기능이다. core가 완료됐으므로 계획 단계부터 시작할 수 있다. | `AGENTS.md`, [BRIEF.md](BRIEF.md) |
| 실험 raw log 프로젝트 한도 순환 | 512 MiB 프로젝트 한도가 차면 그 뒤 run의 로그를 저장하지 않는다. 오래된 로그를 지우는 순환 규칙이 없다. | `.workflow/core-workbench/runs/implement-p2.6-20260927/integration-p27-cw19.json` carried |

2026-10-09 후속 요구사항 구체화: 개발용 fast·정밀 검증, 테스트 시간 단축, raw rotation·중요 로그는
[BRIEF r0.8](../dev-validation-log-retention/BRIEF.md) 전체를 계획 기준으로 승인받았다. Q-01은 선행 기술 검증으로 유지하며 구현 전이다. Raw rotation 활성화는 Q-01 통과 후 별도 사용자 결정이다.

## 2. 간헐 실패 조사 결과

2026-10-23 네 건 모두 조사·수정을 마쳤다. 제품 결함은 없고 전부 시험 쪽 결함이다.
조사 로그는 세션 scratch에 있어 보존되지 않았다. 아래 수치가 기록이다.

| 항목 | 관측(9월) | 기록 | 결과 |
|---|---|---|---|
| manager OMP slash menu가 Esc로 닫히지 않음 | C18 compat 첫 실행에서 6셀 중 3셀(plain/bash, plain/dash, tmux/dash)이 실패했다. 같은 argv의 r2는 6/6 통과했다. | `run/result-p27-cw16-c18-formal-01.md`, `run/check-p27-cw16-compat-c18.log` | 시험 판정 결함. 7e67fe2 + 97bdd24 |
| `test_product_pty` RestartExitedOmp 순서 race | 전체 실행에서 가끔 실패하고, 모듈만 다시 돌리면 통과한다. | `run/result-p27-cw16-fix-05.md`, `run/result-p27-cw16-b4b-01.md` | 시험 결함(assertion race). 1086334 |
| backend `connection_cap` BlockingIOError | 단독 실행 9회 중 2회 실패했다. listen backlog와 테스트의 비차단 connect 20개가 겹치는 테스트 쪽 타이밍으로 추정한다. | `run/result-p27-cw16-rerun-01.md` | 시험 결함(9월 추정이 맞음). 8307a73 |
| `test_concurrent_kill_and_restart` | 전체 실행에서 1회 실패했고 단독 3/3 통과했다. | `run/result-p27-cw16-rerun-01.md` | 시험 결함(결과 순서 가정). 5c2f43b |

(`run/` = `.workflow/core-workbench/runs/implement-p2.6-20260927/`)

### 조사 결과 (2026-10-23)

- [x] FL-01 Esc/slash menu — **시험 판정 결함**, 수정 7e67fe2(2026-10-16) + 회귀 시험 97bdd24.
  - 원인: OMP 시작 화면의 무작위 Tip(`... see /hotkeys for your exit key`)을 옛 판정식
    (`"/hot" not in text or "hotkeys" not in text`)이 열린 메뉴로 오인했다. 메뉴는 실제로 닫혔고 Esc도 전달됐다.
    DVL13 2026-10-15 plain/dash OMP 18.8.6 실패 화면의 raw tail을 pyte로 재생해 확인했다.
  - 수정: 판정을 메뉴 행 기준으로 바꿨다(7e67fe2). 97bdd24는 실제 Tip 문구와 캡처한 raw tail로
    옛 판정은 속고 현재 판정은 닫힘으로 보는 것을 고정한다.
  - 검증: 현재 코드 재현 alone 35회(plain/bash 10, plain/dash 10, tmux/dash 10, tmux/bash 5)
    + CPU hog 24개 10회 → 0 실패. Esc 후 메뉴가 사라지기까지 plain 144~408ms, tmux 629~769ms(escape-time 500ms).
  - 한계: 1코어 고정+hog 6에서는 8회 중 6회 실패했으나 Esc 단계 전 timeout(과부하 환경)이라 별개다.
    9월 3/6 실패는 화면 미보존·OMP 18.8.0이라 같은 원인이라는 것은 정황 추정이다.
- [x] FL-02 RestartExitedOmp 순서 — **시험 결함(assertion race)**, 수정 1086334.
  - 원인: fake OMP 기록은 프로세스 시작 순서인데 시험은 첫 줄이 manager라고 가정했다.
    제품은 항상 manager를 먼저 fork한다(`service.py:563`). tmpfs(10-21 efeb755 도입)에서 fork 간격이
    약 3ms로 줄어 기동 편차에 순서가 뒤집혔다. `/tmp` 디스크에서는 fsync로 45~116ms라 가려졌다.
  - 수정: manager 기록을 role로 고른다. manager 기록 지연 변형 시험을 추가했고 수정 전 결정적 RED였다.
  - 검증: 수정 전 재현 tmpfs 단독 4/100, hog 24에서 26/100, 디스크 0/40.
    수정 후 tmpfs 단독 0/100, 부하 0/50.
- [x] FL-03 connection_cap — **시험 결함**, 수정 8307a73. 제품 상한(16) 위반 없음.
  - 원인: 시험이 연결 20개를 accept를 기다리지 않고 연달아 열었다. CPU 경합으로 backend 루프가
    몇 ms accept하지 못하면 AF_UNIX listen backlog(`listen(8)`, 대기열 9)가 차서 10번째 connect가 EAGAIN이 났다.
  - 수정: 연결마다 hello 응답을 읽고 다음 연결을 연다. 정확히 14 welcome 후 6 reject를 단언한다.
  - 검증: 수정 전 tmpfs 단독 3/40(load 3 이상일 때), stressor 8개 20/30.
    수정 후 단독 40/40, 부하 30/30. 상한 15/17·reject 비활성 변형은 모두 검출한다.
- [x] FL-04 concurrent kill/restart — **시험 결함**, 수정 5c2f43b. 제품은 계약대로 동작.
  - 원인: 제품은 `_shell_lock`을 비차단으로 잡고 다른 쪽이 잡고 있으면 Held(재시도)로 답한다.
    시험은 kill이 항상 이긴다고 가정했다. 부하에서 restart가 먼저 잡으면(거절 경로가 lock 안에서 /proc을 읽음)
    둘 다 Held가 된다. 이중 backend·잘못된 종료·잔여 process는 없었다.
  - 수정: 합법적 결과 3가지(kill ok+restart held / kill held+restart PANE_ALIVE / kill 후 restart 직렬 ok)를
    허용하고, lock을 잡은 결정적 시험을 추가했다.
  - 검증: 수정 전 단독 0/40, stressor 8개 1/100. 수정 후 단독 40/40, 부하 100/100, 순서 강제 러너 통과.

## 3. 실제 모델로 다시 볼 것(확신용)

- 210초 이상 걸리는 worker 명령: terminal_check, terminal_done, 감시 억제를 실제 모델로 확인한다. C16의 RM2는 요청 예산 때문에 실행하지 못했다. 이전 코드에서는 150초 명령으로 확인했다(`run/result-p27-cd70-smoke-04.md`).
- 자동화 run이 진행 중일 때 일시정지하면 manager turn 중단 요청·확인이 표시되는지 확인한다. C18에서는 scripted provider로만 관측했다.
- `shutdown --yes`를 활성 turn과 함께 실행했을 때의 실제 소요 시간. fix-03 수정은 fake OMP로 확인했다.

### 실모델 확신 시험 TODO (계획만 기록, 미착수)

실행 전 현재 host의 격리 data dir·사용할 기존 OMP 인증·모델·호출/시간 예산을 확인한다.
유료 모델을 쓰는 별도 시험이며 fast·정밀 자동 검증에는 넣지 않는다. 요청·완료·실패·미실행을 구분한다.

- [ ] RM-01 210초 이상 Worker 명령: 최초 120초 반환 뒤 명령이 계속되고,
  60초 점검·busy 시 병합·완료 알림·중복 명령 방지가 실제 모델에서 성립하는지 확인한다.
- [ ] RM-02 활성 run 중 pause: Manager turn 중단 요청과 확인을 구분하고,
  host 실험·일반 관측 유지, 새 자동 모델 작업 보류, 명시적 재개와 no replay를 확인한다.
- [ ] RM-03 활성 turn 중 shutdown: 경과 시간·응답·실제 소유 process 종료와 잔존 여부를 기록한다.
  요청 수락만으로 종료 성공을 선언하지 않고, 사용자 종료 뒤 자동 재시작이 없는지 확인한다.

완료 기준: 각 시나리오의 모델/OMP/후보·관측 timeline·판정·미확인·실제 사용량을 연결한다.
이 결과를 scripted 최종 gate의 대체 근거로 소급 사용하지 않는다.

## 4. 작은 개선 후보

- `to-worker` skill이 크기 한도(11000자)에 거의 찼다(약 10994자). 문구를 더하려면 먼저 줄여야 한다.
- `held:retry_limit`에서 실제 모델이 원인·근거·시도·남은 문제를 빠짐없이 보고하는지 확인하지 않았다(VERIFICATION §7 review P3-1).
- SH5: 사용자 PROMPT_COMMAND/PS1 hook이 원인일 때 사유를 단정하지 못한다(review P3-5).
- worker가 가끔 다른 언어로 답한다. 사용자 판단으로 지금은 무시한다.
- manager가 재시작 복구 전후로 두 번 답하는 경우가 있었다(모델 행동).

## 5. 환경 메모

아래는 C18 검증을 수행한 이전 host의 환경 기록이며 현재 host의 설치·경로 보장이 아니다.
2026-10-09 새 host 조사에서는 OMP 18.8.6·Python 3.12.3을 관측했다. 아래 VM 경로와 도구 조합은
새 host에서 사용 전에 재확인해야 하며, 이전 통과를 현 host 호환 통과로 취급하지 않는다.

- 이전 검증 host의 로컬 OMP는 18.8.0이었다. OMP 업데이트 뒤에는 격리 점검(시작 시 isolation)과 `tests/backend` live 테스트(sandbox, 버전 기록)를 다시 돌린다.
- 격리 재부팅 VM: `~/.local/share/wb-vm/wb-reboot`(start.sh/stop.sh/ssh.sh). guest OMP는 18.2.10이고, VM 시험은 fake OMP로 모델 요청 없이 돌린다.
- 이 기록을 남긴 Claude 세션은 사용자 tmux와 herdr 안에서 돌았다. 검증용 tmux와 herdr는 항상 별도 socket과 설정 디렉터리로 격리했다.
