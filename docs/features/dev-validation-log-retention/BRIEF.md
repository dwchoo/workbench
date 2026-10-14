# 개발용 검증과 로그 보관 — 요구사항

Revision: r0.9 + 2026-10-10 범위 합의·B안; state: scoped_implementation_authorized.
현재 권한은 [s5/p5 승인](../../../.workflow/dev-validation-log-retention/runs/approval-s5-p5-20261009/approval.json)과
[이번 두 결정·재개 승인](../../../.workflow/dev-validation-log-retention/runs/implement-p5-20261009/resume-decisions-20261010.json)을 함께 따른다. 아래 2026-10-09 승인 전 설명은 당시 이력이며 현재 권한을 취소하지 않는다.
기록일: 2026-10-09. r0.8 전체 승인 “너가 말한대로 승인”은 기존 계획 기준의 승인이다.
이번 “1번 A / 2번 A / 3번은 … 기본은 그냥 새 버전에 대해서 동작해보고 문제가 생기거나 우리 프로그램의 업데이트가 필요하면 검증 + 수정” 및 “반영해줘.”를 운영 원칙·문서 수정 근거로 기록한다.
r0.9 전체 bundle의 별도 승인이나 구현 승인을 받은 것으로 확대 해석하지 않는다.
기존 [r0.8 승인 기록](../../../.workflow/dev-validation-log-retention/runs/approval-r0.8/approval.json)과
[승인본 원문](../../../.workflow/dev-validation-log-retention/runs/plan-p3-s3-20261009/source/BRIEF-r0.8.md)을 보존한다.
추가 합의는 [운영 결정 기록](../../../.workflow/dev-validation-log-retention/runs/plan-p3-s3-20261009/decisions.json)을 따른다.
제품·테스트 구현, 기존 로그 삭제, 모델 실행, commit·공개를 이 문서로 허가하지 않는다.

## 문제, 사용자와 목표

개발 호스트가 이전 호스트에서 바뀌었다. OMP의 빈번한 업데이트에 대응해 이 개발 저장소에서
빠르게 반복할 수 있는 호환성 검증이 필요하다. 기존의 긴 시험 시간을 줄이고 확인 범위와 미확인을 명시한다.
제품 로그는 한도에서 추가 저장이 멈추는 현행 방식에서 순환 저장으로 전환한다.
중요 정보는 기존 OMP agent의 정기 요약으로 별도 보관한다.
중요 로그의 목적은 지시받은 작업을 이해하고 이어갈 수 있도록 목적·범위, 결정과 이유,
수정·시도, 검증 결과, 미완료와 다음 행동을 남기는 것이다. 전체 시스템 활동이나 사용자 발화와
개별 command의 일대일 권한 이력을 수집하는 기능으로 확대하지 않는다(LG-D24).

## 공통 용어

[CONTEXT.md](../../../CONTEXT.md)의 fast 검증, 정밀 검증, raw log rotation, 중요 로그,
조건 충족 근거를 따른다. 검증의 fast와 OMP 모델 service tier의 fast는 다른 의미다.

## 범위와 비목표

- 포함: 저장소 전용 fast·정밀 검증, 테스트 대기·중복 비용 단축, 후속 TODO 구체화,
  프로젝트 공용 raw rotation, Task별 중요 로그 SQLite, 기존 agent·내부 skill을 이용한 자동 요약.
- 검증 runner는 이 개발 프로젝트에서 사용한다. 일반 제품 CLI 기능이나 전역 설치 도구로 만들지 않는다.
- 로그 정책과 자동 요약은 Workbench 제품 기능이다. 검증 runner의 개발 전용 범위와 구분한다.
- 간헐 실패 추적과 실모델 확신 시험은 이번에는 TODO 계획으로 기록한다. 자동 실행하거나 수정 완료로 표시하지 않는다.
- 제외: 새 agent 정의·상주 OMP 프로세스 추가, Phase 2, truecolor, 기존 사용자 로그 자동 이관·삭제,
  사용자 host 재부팅, 전체 대화 수집, 원문 완전 보존 보장.

## 제약과 호환성 약속

- 기존 core의 승인·입력 owner·pause·no replay·프로세스 수명 계약을 유지한다.
- 기존 raw 정책(64 MiB/run, 512 MiB/project에서 추가 저장 중단)을 신규 로그에 대해서만 대체한다.
  LG-D12에 따라 Q-01 통과 후 사용자가 rotation 활성화를 별도로 결정하기 전에는 기존 한도·저장 중단
  정책을 유지한다. 문서 승인이나 기술 검증 통과만으로 삭제를 활성화하지 않는다.
  활성화 이후 신규 raw에 한해 core C-AC-24와 OPERATING-CONTRACT §6의 raw 상한·저장 중단·
  자동 삭제 제외 부분을 대체하고 종전 per-run 64 MiB 상한을 폐지한다.
  기존 core의 작업·명세·승인·결과 요약·실행 이력과 실행용 worktree의 보존 계약은 유지한다.
  중요 로그는 별도의 보조 기록이며 기존 결과 요약을 대체하거나 그 삭제를 허가하지 않는다.
- 모델 호출·인증은 OMP를 활용한다. Python이 독립 판단 agent를 만들지 않는다.
- 현재 호스트에서 도구·버전을 다시 확인한다. 이전 호스트의 venv·VM·절대 경로를 가정하지 않는다.
- 최초 Q-01 통과·경로 채택 이후에는 OMP 버전 변경만으로 기존 권한 아래 자동 요약을 중단하지 않는다.
  Raw는 별도 활성화 결정이 있었을 때만 신규 rotation 정책을 유지한다(DV-D03).
  기존 검증 버전과 현재 실행 버전은 구분해 기록하고
  미검증 새 버전을 통과로 표시하지 않는다. 업데이트마다 verify 요구·반복 알림·자동 검증을 하지 않는다.
  실제 호환성 문제가 확인되거나 제품·skill·호출 경로 등 관련 경계가 바뀌어 재검증이 필요한 경우에는
  새 요약을 보류하고 문제 알림 1회·상태 표시로 수동 verify를 안내한다. 신규 source만 legacy-stop으로 받고
  진행 중 source는 기존 cohort 정책을 유지한다(LG-D13). 원인 확인·수정은 요청 범위 안에서 별도 진행한다.
  최초 검증/활성화, 승인·pause·no replay·요청별 도구 차단·입출력 preflight는 이 예외로 생략하지 않는다.
  정상 모델 지연·일시적 통신/요금 오류·raw eviction만으로 OMP 비호환을 확정하지 않는다.
- 실험·Worker terminal 로그 두 경로를 같은 프로젝트 raw 예산으로 관리한다.
- 개발 검증은 전용 data dir·OMP home·socket·multiplexer 설정/세션으로 격리한다.
  사용자의 실제 Workbench·로그·DB·tmux/herdr 세션을 변경·정리하거나 공용 rotation 예산을 소모하지 않는다.

## 합의된 동작과 수락 예시

수락 조건은 사용자 결정과 기존 계약에서 도출한 검증 조건을 포함한다. 아래 추적표에서 근거를 구분한다.
Q-07~09의 LG-D11 합의 중 보관 종료·호출 권한은 후속 리뷰와 LG-D12로 수정했다.

### 개발용 검증과 테스트 시간

- DV-AC-01: `fast`와 `precise` 두 모드를 명시적으로 선택한다. 목표 시간은 각각 약 2분·10분이다.
  전체 경과 시간이 목표를 넘었다는 이유로 중단하거나 미완료 판정하지 않는다.
- DV-AC-02: 고정 sleep 대신 준비·응답 조건이 충족되면 즉시 진행한다. 개별 점검의 응답 정체는
  watchdog으로 판별한다. 필수 실시간 의미를 가상 clock 결과로 대체했다고 표시하지 않는다.
- DV-AC-03: fast는 핵심 smoke, 정밀은 더 넓은 runtime·장애·호환 경계를 검사한다.
  정밀은 plain과 설치된 tmux·herdr 조합을 사용하며 없는 조합은 미검증으로 보고한다.
  VM 재부팅·실모델 시험은 별도 TODO/출시 검증이다. 전체 출시 검증을 두 모드가 대체하지 않는다.
- DV-AC-04: 코드·테스트 입력, 호스트·OMP·주요 도구 버전, 실행 모드·항목·소요시간·실패·skip을 기록한다.
  다른 호스트나 버전의 이전 통과를 현재 통과로 취급하지 않는다. 재실행 통과로 첫 실패를 지우지 않는다.
- DV-AC-05: 기존 시험의 비용을 측정해 중복 시작·대기·중복 실행을 줄인다. 격리된 자원만 안전하게
  병렬화한다. 시간 단축을 이유로 실패 assertion이나 수락 조건을 약화하지 않는다.

### Raw rotation과 결과 판단

- LG-AC-01: 신규 raw log는 프로젝트 전체 공용 100MB × 총 5개 파일을 기본으로 순환한다.
  총 5개에는 활성 파일을 포함한다. 실험과 Worker terminal 출력이 같은 예산을 공유한다.
  이 정책은 Q-01 통과 후 사용자의 별도 활성화 결정부터 적용한다. 그 전에는 기존 실험 raw의
  64 MiB/run·512 MiB/project와 Worker terminal의 명령별 64 MiB 저장 정지 정책을 유지하고 잘림을 표시한다.
- LG-AC-02: 원문 순환 중에도 실행·화면·관측을 계속한다. Task/run/command 출처와 삭제·누락 구간을
  조회할 수 있어야 한다. 삭제된 내용을 읽을 수 있다고 표시하지 않는다.
- LG-AC-03: 수집 중 승인된 `log_contains` 조건 충족 여부, 필요한 최소 원문과 출처를 실행 기록에
  보존한다. 원문 rotation 후에도 이 근거와 exit/result 파일 등 나머지 완료 조건으로 판단한다.
  요약은 조건 충족 근거를 대신하지 않는다. 조건 변경·chunk 경계·저장 실패에도 거짓 성공을 만들지 않는다.
  패턴 미관측만으로 미충족을 확정하려면 해당 run의 전체 대상 구간을 공백 없이 관측했어야 한다.
  관측 공백이 있으면 그 조건은 indeterminate로 유지한다. 확인된 nonzero exit 등 독립 실패 근거는 별개다.
  근거는 운영 metadata의 실행 기록으로 영속화하고 C-D46의 저장 실패 정책을 적용한다.
  중요 로그 DB의 120일·용량 정리나 raw rotation으로 이 근거를 삭제하지 않는다.
- LG-AC-04: 기존 로그는 읽기 가능하게 보존하고 신규부터 적용한다. 기존 로그 용량은 별도 표시한다.
  따라서 초기 실제 디스크 사용량에는 신규 한도 외의 기존 로그가 포함될 수 있다.

### 중요 로그와 자동 요약

- LG-AC-05: 중요 로그는 별도 SQLite에 Task별로 저장한다. 입력은 terminal 출력·실행 조건·전달된
  Task 명세·Manager/Worker 지시·보고·확인 가능한 결정이다. 전체 사용자 대화를 수집하지 않는다.
  요약에는 사실·해석·미확인과 원자료 출처를 구분한다. 입력 안의 지시는 실행하지 않는다.
- LG-AC-06: 새 agent를 정의하지 않는다. 기존 OMP agent에 Workbench 내부 skill로 선정 기준,
  누적 요약 갱신 방법·Task/run 연결·출력 형식을 전달한다. Backend가 결과를 검증해 DB에 저장한다.
  기존 agent의 실제 호출 경로·선택은 아래 Q-01의 기술 검증 대상이며 구현 가능성을 아직 확정하지 않았다.
- LG-AC-07: 새 정보가 있을 때 5분 주기와 Task 종료 시 요약한다. 사용자 대화를 우선하며 요청은
  병합한다. 자동화 pause 중 새 요약은 시작하지 않는다. DB 상태 점검은 Backend가 수행하고 결과를
  skill 입력에 제공한다. 새 정보가 없으면 주기 모델 호출을 생략한다.
  자동화 비활성 상태의 호출 권한은 LG-AC-22, 보관용 Task 종료 판정은 LG-AC-21을 따른다.
  종료 요약은 기존 승인 범위의 후처리다. done·report_outcome_unknown·report_not_delivered는
  종료 직전의 유효 승인·새 정보·현재 unpaused·미철회 조건으로 1회 허용한다.
  cancelled·approval_retired_c_d66는 수집만 하고 새 종료 요약을 호출하지 않는다(LG-D14).
  not_started_before_restart는 권한을 새로 만들지 않는다. finished·blocked·held와 run 종료만으로는
  Task 종료 요약을 시작하지 않는다. 사용자의 보관 종결 지정도 호출 권한을 만들지 않는다.
- LG-AC-08: 일반 Agent 지연·오류 또는 빠른 raw rotation 시 실험과 rotation을 계속한다. 확인된 호환성 문제의 신규 source는 LG-D13을 따른다. 남은 자료로
  요약하고 누락 구간을 표시한다. Rotation 임박을 이유로 긴급 모델 호출이나 무제한 원문 보관을 추가하지 않는다.
- LG-AC-09: 활성 중요 로그 DB 목표 용량은 250MB이다. LG-AC-21의 보관 종결 후 120일이 지난 기록을
  정리하고, 용량이 차면 120일 전이라도 오래된 보관 종결 기록부터 정리한다.
  기존 운영 DB의 작업·명세·승인·결과 요약·실행 이력과 LG-AC-03의 근거는 이 정리 대상이 아니다.
  이 자동 정리는 활성 DB에만 적용한다. 전환 뒤의 보존 DB는 LG-AC-17을 따른다.
- LG-AC-10: 보관 종결 기록을 정리해도 진행 중 기록 때문에 용량이 차면 오래된 중간 요약부터 정리한다.
  시작 정보와 최신 누적 요약을 우선 보존하고 삭제 구간을 표시한다.
- LG-AC-11: 저장 전후 DB 본체·WAL·SHM 크기와 합계, 목표 대비 사용량, Task별 기록·저장량,
  오래된 기록·정리 후보, 쓰기 결과·lock 경합·디스크 여유·재사용 가능 공간을 점검한다.
  무거운 집계는 캐시·증분 방식 등으로 구현해 매 저장 시 전체 스캔을 강제하지 않는다.
- LG-AC-12: `quick_check`는 최초 사용·오류·주기 점검 때 실행하고 매 저장마다 전체 검사를 하지 않는다.
  Checkpoint와 파일 축소는 필요 시 작은 유지보수 단위로 수행한다. 마지막 검사·정리 결과와 미확인을 표시한다.
  저장 실패를 성공으로 보고하지 않고 기존 실험을 유지한다.
- LG-AC-13: CLI와 기존 Agent의 읽기 전용 도구로 Task별 목록·중요 로그·누적 요약·DB 상태를 조회한다.
  조회 자체는 모델 호출이나 요약 생성·삭제를 시작하지 않는다. TUI에는 요약 오류·지연 상태를 표시한다.
  과거 Task를 탐색하는 별도 TUI 화면은 이번 범위에 포함하지 않는다.
- LG-AC-14: Task 없는 Worker terminal 명령도 중요 로그에 포함한다. 인위적으로 Task를 만들지 않고
  command ID로 기록·조회한다. 새 정보가 있을 때 5분 주기와 명령 종료 시 요약하며,
  LG-AC-22의 command별 허용 조건과 LG-AC-21의 보관 종결 후 120일·공용 DB 용량 기준을 따른다.
  실행 중에는 LG-AC-10의 보존 우선순위를 적용한다.
  Task가 있는 명령은 기존 Task 요약에 연결하고 같은 자료를 명령별·Task별로 중복 요약하지 않는다.
- LG-AC-15: 정리 가능한 보관 종결 기록과 오래된 중간 요약을 정리해도 활성 DB가 가득 차면,
  기존 DB를 보존하고 새 중요 로그 DB를 만들어 신규 저장 대상으로 전환한다. 전환 사실과
  남아 있는 DB 수를 사용자에게 알린다. 이전에 제안된 '용량 때문에 새 요약 호출·저장 보류' 대신 이 동작을 적용한다.
  이때 250MB는 활성 DB의 목표이며, 보존 DB를 포함한 전체 용량은 이를 넘을 수 있다.
  정상 용량 도달과 물리적 저장 불능을 구분한다. 전환 불가 시 모델 호출·복구 정책은 LG-AC-23을 따른다.
- LG-AC-16: 중요 로그 DB가 여러 개 남아 있는 동안 정리 필요 상태를 유지한다. 단순 알림 확인이나
  Backend 재시작만으로 상태를 해제하지 않는다. DB가 1개인 것을 실제 확인한 뒤에만 해제한다.
  DB 수가 2개면 2개, 더 많으면 실제 수와 전체 용량을 표시해 사용자에게 정리를 요구한다.
  운영 metadata DB나 SQLite의 WAL·SHM 파일을 별도의 중요 로그 DB로 세지 않는다.
  정리 필요 알림은 TUI에 상시 표시하며 시작·재접속 때 다시 안내한다.
- LG-AC-17: 전환된 이전 중요 로그 DB는 사용자가 정리할 때까지 보존한다. 보존 DB에는
  120일 경과나 용량을 이유로 한 자동 기록 삭제·DB 삭제를 적용하지 않는다.
  경고를 확인하거나 닫는 행위는 삭제 요청이 아니다. 실제 사용자 정리가 끝나 DB가 1개가 되면
  LG-AC-16의 경고를 해제한다.
- LG-AC-18: 요약 실패 시 마지막 정상 요약을 보존한다. 다음 5분 주기의 재시도는 아래 경계를 따른다.
  Backend 재시작 시 미완료 요청을 실제 저장 상태와 대조하고 새 요청과 병합한다.
  같은 입력의 요약을 중복 저장하거나 실패 결과로 정상 요약을 덮어쓰지 않는다.
  미실행 확인·실행 후 결과 유실/저장 실패·전달 불명을 구분한다. 미실행이 확인된 입력만 다음
  누적 요청에 다시 포함한다. 이미 실행했거나 실행 여부가 불명확한 입력은 자동 재호출하지 않고
  누락을 표시한다. 새 요청 ID·주기/종료 누적 요약으로 이 경계를 우회하지 않는다.
  늦게 도착한 결과는 출처·입력 범위와 중복을 검사해 유효한 첫 결과만 저장하고 중복 폐기를 기록한다.
  재시도는 기존 승인·pause 경계를 따른다.
- LG-AC-19: 자동화 pause 중 새 요약 호출은 시작하지 않는다. Pause 전에 이미 요청한 결과가
  도착하면 출처·입력 범위·출력 형식을 검증한 뒤 저장할 수 있다. 결과 저장은 자동화 재개가 아니다.
  Pause 중 결과 도착을 보장하거나 기존 manager turn 중단 요청을 생략한다는 뜻은 아니다.
- LG-AC-20: 중요 로그 조회는 활성 DB와 보존 DB를 함께 대상으로 삼고 각 결과의 출처 DB를 표시한다.
  사용자 정리는 CLI에서 명시적으로 선택한 보존 DB의 삭제로 제공한다. 활성 DB 삭제는 거부한다.
  삭제 완료 후 실제 DB 수와 용량을 다시 확인해 정리 필요 상태를 갱신한다.
- LG-AC-21: Task 보관 기간은 기존 core의 `closed` 기록 시각부터 120일을 계산하고 종결 사유를 보존한다.
  `closed`가 아닌 Task(`finished`·`blocked`·`held` 등)는 run 종료만으로 보관 종결하지 않는다.
  Task 없는 command는 실제 종료 확인 시각을 쓴다. 닫히지 않은 Task와 종료 불명 command는
  사용자 CLI로 명시적 보관 종결을 지정할 수 있으며, 이때 지정 시각을 기산점으로 기록한다.
  이 도구는 Agent에 제공하지 않는다. 보관 종결은 성공 판정·core 상태 변경·실행 중단이 아니며,
  120일 또는 용량에 따른 정리 대상이 됨을 사용자에게 안내한다. 그 전에는 미종결로 보호한다.
  같은 대상이 다시 활동하면 미종결 보호로 돌아가고 다음 보관 종결부터 기간을 다시 계산한다.
  기존 core가 금지하는 재개를 허용한다는 뜻은 아니다. 미종결에도 LG-AC-10의 중간 요약 정리는 적용한다.
- LG-AC-22: Task가 있는 요약은 해당 Task의 자동화가 승인·활성이고 unpaused일 때만 호출한다.
  Task 없는 command는 Workbench가 기존 실행 권한 아래 접수한 command의 작업 맥락·범위와
  관련 source/ranges를 command ID에 연결해 요약한다. 특정 사용자 발화와 command의 일대일
  연결 증명은 요구하지 않는다(LG-D24). 작업 연결이 불명인 자료는 다른 작업에 추정 귀속하지 않는다.
  허용 범위는 새 정보의 5분 주기 요약과 종료 요약까지이며 종료 요약 처리 후 끝난다.
  이는 프로젝트 자동화 재개나 다른 command의 허가가 아니다. Pause 중에는 새 요약을 호출하지 않는다.
  관련 없는 Task의 승인·자동 notice·unpaused 상태만으로 새 작업 권한을 만들지 않고
  별도 요약 활성화 설정은 도입하지 않는다. 기존 core 실행 승인과 작업 범위는 유지한다.
  허용되지 않는 동안에는 가능한 수집만 계속한다. 호출 가능 시 남은 입력을 병합하되 LG-AC-18을
  따르고 이미 사라진 raw는 누락으로 표시한다. 허용 중 요청한 유효한 늦은 결과는 LG-AC-19처럼 저장할 수 있다.
- LG-AC-23: 정상 용량 도달에는 LG-AC-15대로 DB를 전환한다. 디스크 부족·권한 오류·손상 등으로
  안전한 저장/전환이 불가능할 때만 저장 장애를 이유로 새 요약 모델 호출을 보류한다.
  기존 실험·가능한 관측은 유지하고 장애·요약 지연/누락을 표시한다. 이미 도착한 결과의 저장 실패도
  숨기거나 무제한 버퍼링하지 않는다. 가벼운 저장 상태 점검으로 복구를 확인한 뒤 다음 5분 주기에
  재시도하되 승인·pause·no replay 경계를 유지한다.

## 결정과 근거

아래는 현재 대화의 사용자 직접 답변이다. 외부 승인 ID는 제공되지 않았으며 문서 전체 승인과 구분한다.

| ID | 사용자 결정/근거 |
|---|---|
| DV-D01 | 다른 개발 host, 잦은 OMP 업데이트에 대응하는 프로젝트 전용 두 검증 모드 요청 |
| DV-D02 | “2분, 10분을 목표로 … 이 시간이 지났다고해서 미완료는 좋지 않은 … 각 점검에서 대기가 많지 않도록” |
| DV-D03 | “3번은 omp의 업데이트 주기가 너무 짧아 … 기본은 그냥 새 버전에 대해서 동작해보고 문제가 생기거나 우리 프로그램의 업데이트가 필요하면 검증 + 수정” 및 “반영해줘.”. OMP 버전 변경만으로 차단/반복 재검증 요구 없이 기존 경로를 사용하되 미검증 상태를 구분하고, 실제 문제·관련 제품 변경 때 수동 verify·필요 수정으로 전환한다. 업데이트마다 자동 검증하지 않음 |
| LG-D01 | raw 100MB×5개 제안 뒤 “프로젝트 전체 공용” 선택; 활성 파일 포함 안내에 동의 |
| LG-D02 | 중요 로그를 Task별 SQLite로 보관·오래된 기록 정리 요청; 새 agent 반대, 기존 agent+내부 skill 지정 |
| LG-D03 | SQLite 저장 시 총 크기·기본 상태 점검 요청에 제안된 검사 방식으로 “응 그렇게 하자” |
| LG-D04 | “120일+용량 기준 … 90일 보다는 120일”; “5분 + Task 종료” 선택 |
| LG-D05 | “실행 + 결정·보고”, “조건 충족 근거 보존”, “오래된 중간 요약 정리” 선택 |
| LG-D06 | “실행 우선·누락 명시”, “설치된 terminal 조합”, “기존 보존·신규부터 적용” 선택 |
| LG-D07 | 조회 경로와 Task 없는 명령 포함 여부의 추천안에 “너가 추천하는대로 해줘.”라고 답변. CLI·Agent 읽기 전용 조회, TUI 오류·지연 표시, command ID별 요약·보관을 채택 |
| LG-D08 | “중간 요약을 지워도 DB가 가득 차면 새로 DB를 만들고 사용자에게 알림을 줘. … DB가 1개로 될 때 까지 계속 … 정리를 해야한다고 요구해.” 활성 DB 전환과 복수 DB 정리 알림 유지로 변경 |
| LG-D09 | 이전 DB를 자동 삭제하지 않고 사용자 정리까지 보존하며 TUI 상시 표시·시작/재접속 재안내한다는 추천에 “응 그렇게해”라고 답변. LG-AC-09의 자동 정리는 활성 DB에 한정 |
| LG-D10 | 실패 시 정상 요약 보존·다음 주기 재시도·재시작 pending 병합, pause 전 요청 결과의 검증 후 저장, 활성·보존 DB 통합 조회와 CLI의 보존 DB 선택 삭제 추천에 “그렇게 해.”라고 답변 |
| LG-D11 | Q-07~09의 보관 종료·자동화 활성 중 요약·저장 불능 시 호출 보류와 복구 추천에 “3가지 모두 그렇게 하자.”라고 답변. 세 항목의 합의이며 전체 BRIEF 승인은 아님 |
| LG-D12 | Claude 후속 리뷰 `brief-r07-options-review-02` 뒤 제시한 다섯 추천에 “응 그렇게 해줘.”라고 답변. 기존 closed 시각과 사용자 CLI 보관 종결, 직접 지시 command의 종료 요약까지 허용, Q-01 통과와 별도 활성화 전 기존 raw 정책 유지, 미실행 확인만 재호출, 전체 승인 때 core·AGENTS 포인터 추가를 채택. LG-D07/D10/D11의 해당 경계와 LG-D01/D06의 적용 시점을 수정하며 전체 BRIEF 승인은 아님 |
| LG-D13 | “1번 A” 및 DV-D03과 결합한 설명 뒤 “반영해줘.”. 보호/재검증 상태에서는 신규 source만 legacy-stop, 진행 중 cohort는 유지. OMP 버전만 변경된 상태는 이 보호 상태에서 제외. 재검증 통과는 기존 활성화 아래 신규 admission 회복이며 최초 raw 활성화 승인이 아님 |
| LG-D14 | “2번 A” 및 “반영해줘.”. 취소·승인 철회 후 사실 수집/기존 정상 요약 보존만 하고 신규 종료 요약 호출 없음. 기존에 보낸 유효한 늦은 결과의 검증 후 저장은 새 호출과 구분 |

### 수락 조건의 근거 추적

| 수락 조건 | 사용자 결정 및 도출 근거 |
|---|---|
| DV-AC-01~02 | DV-D01~02; watchdog·실시간 근거 구분은 목표를 검증하기 위한 상세화 |
| DV-AC-03 | DV-D01, LG-D06의 terminal 조합 결정; LG 접두어라도 개발 검증에도 적용 |
| DV-AC-04~05 | DV-D01~02의 호환 검증·시간 단축 요청 + workflow contract §6~7의 후보/실패 근거·수락 조건 보존 |
| LG-AC-01~04 | LG-D01/D05/D06/D12/D13 + DV-D03 + core C-AC-24/28, C-D46의 관측·저장 실패 계약 |
| LG-AC-05~06 | LG-D02/D05; 사실/해석·출처 구분과 입력 지시 미실행은 보조 요약 역할의 안전 제약 |
| LG-AC-07~10 | LG-D04/D05/D06/D13/D14 + DV-D03; 종료·호출 권한 경계는 LG-D11/D12, LG-AC-21~22 |
| LG-AC-11~12 | LG-D03; 제안된 점검 방식의 내용은 이 두 조건에 명시. 구체 주기·집계/축소 알고리즘은 계획 상세 |
| LG-AC-13~14 | LG-D07/D12 |
| LG-AC-15~17 | LG-D08/D09; 전환 불가 경계는 LG-D11/LG-AC-23 |
| LG-AC-18~20 | LG-D10/D12 + 기존 no replay·pause 계약 |
| LG-AC-21~23 | LG-D11/D12/D14; Q-07~09와 후속 리뷰 결정 |

리뷰 반영 근거: 사용자가 전달한 Claude의 r0.5 읽기 전용 리뷰와 저장소 대조 후
“보완이 필요한 부분은 보완해서 다시 정리해봐.” 요청으로 r0.6을 보완했다.
이후 LG-D11로 Q-07~09를 합의해 r0.7에 반영했으며 전체 승인은 별도다.
Claude의 r0.7 및 후속 추천안 리뷰 뒤 LG-D12를 합의해 r0.8에 반영했다. 리뷰 자체는 승인이 아니다.

## 구현 방향과 검증 의도

- 별도 개발 runner가 현재 설치 OMP와 scripted local provider를 사용하도록 설계한다.
  정확한 모드별 항목·CLI·자원 격리·성능 기준은 실행계획에서 구체화한다.
- 일반 SQLite와 긴 본문의 application-level zlib 압축은 제안이다. 특별한 SQLite 확장 도입은
  합의되지 않았다. 압축은 조회·삭제·크기 계산·복구 시험과 함께 결정한다.
- 기존 agent를 요약에 사용하는 공개 호출·skill 전달·출력 회수·사용자 대화와의 경합을
  작은 scripted 실험으로 먼저 판별한다. 불가능하면 새 agent로 우회하지 않고 제한을 보고한다.
- 작은 시험 한도로 rotation/압축/정리 경계, 120일 경계, 활성 Task 보호, crash·lock·disk-full,
  중복 요약·Task 혼동·pause·raw 누락·승인 조건 증거·이전 로그 보존을 검증한다.
- 조회는 Task/command 분리·없는 ID·삭제된 구간·요약 지연/실패·DB 장애를 확인한다.
  조회만으로 모델 요청·요약 생성·정리가 시작되지 않는지, Task 없는 명령이 임의 Task로 연결되지 않는지 검증한다.
- DB 전환 도중 crash·생성 실패·동시 저장 시 기존 DB 보존과 활성 DB 식별을 검증한다.
  복수 DB 알림은 재시작·확인 조작 후에도 유지되고 실제 DB 1개가 되면 해제되는지 확인한다.
  보존 DB는 120일이 지나거나 활성 DB 용량이 차도 자동 삭제되지 않으며,
  시작·재접속 안내와 TUI 상시 표시가 유지되는지 확인한다.
  디스크 부족·권한 오류·DB 손상을 단순 목표 용량 도달과 혼동해 DB를 반복 생성하지 않는다.
- 요약 실패 후 정상 기록 보존·다음 주기 재시도, 재시작 후 pending 병합·중복 방지,
  pause 중 신규 호출 차단·기존 결과의 검증 후 저장을 확인한다.
  복수 DB 조회의 출처 표시, 명시적 보존 DB 삭제, 활성 DB 삭제 거부와 삭제 실패 시 알림 유지를 검증한다.
- 불필요한 실시간 대기는 축소하되 실제 60초 점검·detach 생존처럼 시간 자체가 조건인 근거는
  대표 runtime 시나리오에 남긴다. 동일 조건의 이전/이후 시간과 실행 항목을 비교한다.
- [후속 TODO](../core-workbench/FOLLOWUPS.md)의 간헐 실패와 실모델 확신 시험은 별도 작업이다.

### Q-01 선행 기술 검증의 경계

- 기존 Manager/Worker OMP와 허용된 기존 agent·내부 skill을 우선 조사한다. 일회성 OMP 프로세스는
  상주 프로세스와 다르지만 기존 isolation probe가 요약 실행까지 허가하지는 않는다.
  별도 일회성 프로세스나 TS 직접 completion만 가능하면 기존 agent 활용 조건을 충족한다고 추정하지 않고,
  필요한 대안과 영향을 보고해 채택 결정을 받은 뒤 해당 방식의 구현을 진행한다.
- 작은 격리 실험에서 요청 단위의 수정·terminal 등 부작용 도구 차단, skill의 확정적 전달과 버전 기록,
  request ID·Task/command·revision·입력 범위에 연결된 구조화 결과 검증을 확인한다.
  대화 텍스트의 휴리스틱 추출이나 skill 지시만으로 도구 차단 성공을 주장하지 않는다.
- Role skill filter·isolation 기대값과 기존 구조화 응답 회수 경로를 함께 검토한다.
  기존 skill의 11,000자 제한은 저장소 시험 제약이며 OMP 전체 runtime 한도라고 일반화하지 않는다.
- 사용자 대화·승인·입력 보호, transcript/context 증가와 입력 크기 상한, 60초 점검·종료 처리와의 경합,
  pause/재시작/late result 경계를 관측한다. 요약을 기존 점검·종료 처리보다 낮게 두는 방향을 계획에서 검증하고,
  기존 계약에 없는 분석 완료 hard deadline을 추가하지 않는다.
- 계획은 항목별 통과·실패·inconclusive 기준을 먼저 정한다. 안전 차단·출처 연결 실패는 실패,
  관측 부족·불명 timeout은 inconclusive이며 해당 요약 구현 확대를 막는다.
  결과는 관측한 OMP 버전·후보·호스트에만 유효하다. 개발 gate·최초 활성화는 현재 입력의 통과를 요구한다.
  이후 제품 사용에서 OMP 업데이트만으로 기존 통과를 현재 통과로 바꾸지 않되, DV-D03에 따라 실행은 유지한다.
  실제 호환성 문제·관련 제품/skill/경로 변경 때 사용자가 verify를 명시 실행하고 필요한 수정을 검증한다.
- Q-01 실패·inconclusive이면 자동 요약 확대와 신규 raw 자동 삭제 활성화를 막는다.
  fast·정밀 검증과 LG-AC-03 근거 보존은 독립 진행할 수 있다. 출처별 조회·DB 비요약 부분은
  미검증 호출 경로에 의존하지 않는 범위에서 계획한다. 통과 후에도 삭제 활성화는 별도 사용자 결정이다.
  호출 경로별 미실행/실행/불명 대조 가능성과 transcript 부재에 따른 한계를 기록한다.

### 계획에서 구체화할 안전·성능 조건

- DB 본체·WAL·SHM의 물리적 총량과 논리적 사용량·freelist를 구분하고 250MB 판정 기준을 정한다.
  삭제 후 파일이 그대로라는 이유만으로 곧바로 가득 찼다고 판단하지 않는다.
  Reader 수명·checkpoint 정체·유지보수 임시 공간과 bounded 작업 시간을 고려한다.
  auto_vacuum/incremental_vacuum 또는 VACUUM은 비용을 비교할 설계 선택이며 생성 시 설정만이 축소 수단은 아니다.
- DB 식별·활성 포인터의 원자적 전환·crash 복구·동시 조회/삭제를 설계한다.
  Checkpoint 성공을 확인하기 전 WAL 없이 본체만 완결됐다고 취급하지 않는다.
  DB 수에서는 WAL/SHM/journal·임시 파일을 제외하며 경로 별칭으로 활성 DB 삭제 방지를 우회하지 못하게 한다.
- 보존 DB 삭제 뒤 진행 중 Task의 시작 정보·최신 누적 요약 보존 우선순위가 깨지지 않도록 한다.
  필수 정보의 이월 또는 삭제 전 보호 등 구체 방식은 계획에서 정하고, 실패 시 삭제를 진행하지 않는다.
  열린 handle과 sidecar·삭제 중 crash를 처리하고 완료 후 실제 수·용량을 다시 확인한다.
- 요약 요청 중복 키에는 Task/command·revision·입력 범위·skill 버전 등을 검토한다.
  새 정보 판정·입출력 상한·usage/미확인 표시를 정하며, 5분마다 무조건 호출한다고 해석하지 않는다.
- core 종결 시각·사유와 사용자 보관 종결 기록을 중요 로그 DB에 보존해 flow ledger의 수명에만 의존하지 않는다.
  재활동 시 보호 복귀, command별 작업 연결·관련 자료·종료 요약 처리 상태를 영속 기록하고
  재시작·pause·늦은 결과·누락 처리에서 권한을 새로 만들거나 LG-AC-18을 우회하지 않게 한다.
- 공용 segment의 source framing과 reader 전환은 Worker의 DONE_INSTRUCTION/log_path 소비자도 포함한다.
  Matcher의 chunk 경계·관측 공백·최소 근거 크기 상한·revision 연결을 검증한다.
  중요 로그에도 기존 raw store 수준의 소유 경로 검사·0600 권한·fail-closed 처리를 적용한다.
- 현재 host에서 baseline과 목표 대비 소요시간을 기록한다. 실제 60초 점검·detach는 정밀의 대표 시험으로 남기고,
  fast에서 제외한 항목은 미검증으로 표시한다. 6셀×65초는 detach 대기만 약 6.5분이며
  이것만으로 병렬화 필수나 10분 달성을 확정하지 않는다. 전체 비용과 자원 격리를 측정한다.
  Watchdog 발동은 failed/inconclusive로 이유를 남기고 skip으로 숨기지 않는다.
  OMP 버전을 실행 전후 기록하고 scripted provider 장애와 제품 호환 실패를 구분한다.

## 저장소 근거

기준 HEAD: `2f21ec91990a409ea0d25f351343b50e34b48196` (2026-10-09 읽기 조사).

- [RawLogStore](../../../src/workbench/storage/log_raw/store.py): 64 MiB/run·512 MiB/project,
  append-only 저장 상한 방식. Rotation은 구현되어 있지 않다.
- [Worker terminal](../../../src/workbench/backend/flow_terminal.py): 명령별 별도 파일·64 MiB 상한.
- [결과 판정](../../../src/workbench/workflow/run.py): 저장된 원문을 종료 후 읽어 `log_contains`를 검사한다.
  LG-AC-03은 이 관측 동작에 대한 의도적인 변경 요구다.
- [Launcher](../../../src/workbench/backend/launcher.py): Manager는 bundled agents, Worker는 `explorer`만 허용.
  `memory`·`commit`은 model role이며 독립 호출 agent 이름이 아니다.
- [Bridge](../../../omp_bridge/g3/bridge.ts): 현재 notice는 `sendUserMessage`로 parent session에 전달하며
  busy·composer·pause를 검사한다. Subagent는 bridge 연결·도구 사용을 거절한다.
- [Metadata schema](../../../src/workbench/storage/metadata_sqlite/schema.py): Task에 종료 상태 필드가 없고,
  run 종료 이벤트와 task_decisions가 별도다. Run 종료는 Task 목표 완료의 충분조건이 아니다.
  [CommandState](../../../src/workbench/contracts/v1.py)의 unknown 역시 종료 확인과 다르다.
- [호환 시험](../../../tests/integration/live_cw16_compat.py): outer×shell 조합, 기본 65초 detach.
  [정책 시험](../../../tests/integration/live_cw16_flow_policy.py): 90~200초 실행과 FakeClock 시험이 공존한다.
- 이전 최종 근거는 [C18/OMP 18.8.0](../core-workbench/VERIFICATION.md)이다.
  이 대화의 현 host 관측은 OMP 18.8.6·Python 3.12.3이며 호환 통과를 뜻하지 않는다.
- OMP upstream [agent discovery](https://github.com/can1357/oh-my-pi/blob/main/docs/task-agent-discovery.md)와
  [extension API](https://github.com/can1357/oh-my-pi/blob/main/docs/extensions.md)는 참고 자료다.
  `main` 문서 내용을 현 설치 버전의 runtime 검증으로 대체하지 않는다.

## 미결정·보류 사항

- Q-01 (기술 판별): 기존 agent의 실제 자동 호출·skill 전달·출력 회수 경로. Python 모델 직접 호출이나
  새 agent 정의 없이 동작해야 한다. 확정 전 자동 요약 구현 확대는 차단한다.
- Q-02 해결 (LG-D07/LG-AC-13): CLI·Agent 읽기 전용 조회와 TUI 오류·지연 표시.
- Q-03 해결 (LG-D07/LG-AC-14): Task 없는 Worker 명령도 command ID로 포함.
- Q-04 (계획 상세): MB의 byte 환산, DB 목표 초과 시 정리 여유폭,
  검사 주기·모델 입력량·출력량. 재시도와 재시작의 제품 동작은 LG-AC-18을 따른다.
- Q-05 (조회 호환성): 프로젝트 공용 분할 로그를 기존 per-run `log_path` 소비자가 읽는 방법과
  Task/run/command별 원문 조회 계약. 구현계획에서 reader 전환·legacy 호환·증거 위치를 함께 확정한다.
- Q-06 해결 (LG-D09/LG-D10): 보존 DB 자동 삭제 금지, 지속 알림, CLI의 명시적 보존 DB 선택 삭제,
  활성·보존 DB 통합 조회를 채택한다. 동시 조회·삭제와 crash 안전성, 추가 전환의 자원 한계는 계획 상세다.
- 요약 실패·재시작·pause 중 도착 결과의 제품 동작은 LG-D10/LG-AC-18·19로 해결했다.
- Q-07 해결 (LG-D11/D12, LG-AC-21): 기존 closed 시각·사용자 CLI 보관 종결·재활동 보호를 채택했다.
- Q-08 해결 (LG-D11/D12/D24, LG-AC-22): Task별 승인·활성 조건과 작업에 연결된 command별 요약 범위를 구분한다. 개별 사용자 발화 연결 증명은 요구하지 않는다.
- Q-09 해결 (LG-D11/LG-AC-23): 정상 용량 전환과 저장 불능을 구분하고 호출 보류·복구를 확정했다.
- r0.8 전체 승인과 r0.9의 운영 원칙 개별 합의를 구분한다. 기존 core 완료·승인 bundle은 역사적 증거로 수정하지 않는다.
  LG-D12의 유일한 예외로 core BRIEF C-AC-24·OPERATING-CONTRACT §6에
  이 BRIEF 링크와 적용 조건을 주석으로 추가했고 AGENTS.md의 현재 요구사항에도 링크를 추가했다.
  기존 승인 문장·결정·검증 기록은 변경하지 않는다. Raw 대체는 문서 승인만이 아니라
  Q-01 통과 후 사용자의 별도 활성화 결정 이후 신규 raw에만 적용한다고 명시한다.
  이 연결 주석은 기능 구현이나 raw 자동 삭제 활성화의 근거가 아니다.

## 인계

현재 단계: r0.8 승인 기준과 r0.9 운영 원칙 합의를 반영한 SPEC s5·PLAN p5·14 ticket 검토안.
Q-01은 여전히 선행 기술 검증이다. 공유 경로의 차단 시간 수용과 실제 모델 metadata가 없을 때의
대체 상한은 관측 뒤 결정한다. 지금 임의의 상한·경로를 승인하지 않는다.
Claude s4/p4 마지막 리뷰 R1~R3의 기술 보완과 R4~R5 착수 검증은 SPEC·PLAN·ticket과 계획 보강 기록에 반영했다. 이 BRIEF의
제품 결정·수락 조건은 바꾸지 않고 인계 포인터만 갱신했다. 정확한 새 bundle의 검토/승인은 별도다.
이 문서 수정은 제품 구현·모델 probe 실행·raw 자동 삭제 활성화를 허가하지 않는다.


## 2026-10-10 격리 수정·검증 재개 합의

사용자는 “두 결정 모두 이 내용으로 반영”이라고 확인하고, 해당 범위의 문서·승인 기록 반영과 수정·격리 검증 재개를 지시했다.
원 s5/p5 승인 digest와 원문을 보존하며 AC·gate 기준, ticket dependency/resource graph는 유지한다.

- LG-D15 / DVL07-ephemeral-read-snapshot-scope: 조회의 “새 DB 없음”은 catalog/active DB의 신규 할당 금지다.
  원본 본체·WAL·SHM과 파일 목록을 변경하지 않는 private transient read snapshot은 허용한다.
  snapshot은 catalog count에 포함하지 않고 등록·유지보수·요약·삭제를 유발하지 않으며 사용 후 정리한다.
  우선 실제 copy bytes·scratch peak·I/O·lease 유지/경합 비용을 격리 합성 DB로 측정해 보고한다.
  크기/시간 상한이나 큰 정상 DB를 unavailable로 처리할 정책은 측정 후 별도 결정이며 지금 임의로 확정하지 않는다.
- LG-D16 / DVL11-preservation-fault-scope: public segment 이름이 다른 inode로 교체되는 fault를 시험한다.
  control/quarantine namespace는 transaction owner 전용이라는 전제 아래 격리 실험·disabled engine 수정을 허용한다.
  victim identity와 durable intent를 연결하고 quarantine capture 불일치·충돌·crash에서 unknown replacement를 삭제·채택하지 않는다.
  quarantine과 열린 raw inode도 총5개 한도에 포함한다. 동일 UID가 control/quarantine까지 임의 교체하는 보장은 별도 범위다.
- 제외: Q01 기준 변경, proxy 채택, FL-01 제품 수정, 실제 raw 활성화. 실제 사용자 DB/raw/서비스와 유료 모델을 사용하지 않는다.
  기존 Q01 차단·C2 실패·원 검증 기록을 보존하고 격리 fixture 통과를 최종 runtime pass로 해석하지 않는다.
  commit·게시·배포는 별도 요청 대상이다.

LG-D15는 LG-AC-13/20의 조회 구현 해석, LG-D16은 LG-AC-01~04의 preservation fault 범위다.

## 2026-10-10 쓰기 우선 결정과 첫 수정 경계

- LG-D17 / DVL07-writing-priority: 사용자는 “쓰기 우선으로 하자.”라고 결정했다. 조회는 대기하거나
  일시 보류될 수 있지만 조회 경합 때문에 중요 로그 저장이 실패하거나 누락되어서는 안 된다.
  이 결정은 모든 저장 장애에 대한 무손실 보장이나 수치 상한의 채택을 뜻하지 않는다.
- 사용자의 “적당한 바운더리를 정해서 계획을 빨리 마무리하자” 요청과 기존 claude-wb의 경계 리뷰를
  반영해 [DVL-07 첫 수정 계획](tickets/DVL-07.md#2026-10-10-쓰기-우선-첫-수정-계획)을 정리했다.
  기존 lock과 원본 hash 검증을 유지하는 최소 수정·격리 판별부터 진행하는 계획이다.
  추가 잠금 프로토콜·전역 멱등화·지속 cache 등은 실패 관측 전 첫 수정에 포함하지 않는다.
- LG-D15/16, 원 AC·gate·PLAN p5 의존성과 Q01/C2 차단은 유지한다. 첫 수정 통과는 측정한 조건의
  결과이며 DVL-07 전체 완료·일반적인 잠금 우선순위 보장·실제 raw 활성화로 해석하지 않는다.
  문서 정리와 외부 자문 기록은 이번 턴의 구현·시험 실행을 뜻하지 않는다.

## 2026-10-10 B안 승인: active page 직접 조회

- LG-D18 / DVL07-active-page-read: 사용자는 제시된 B안과 추천에 “b안과 네 추천대로 해줘”라고
  승인하고 해당 범위의 구현·격리 측정·독립 시험·리뷰를 재개했다.
  [승인 기록](../../../.workflow/dev-validation-log-retention/runs/dvl07-direct-20261010/authority.json)을 따른다.
- Active 조회에 SQLite 표준 reader의 SHM read mark·wal-index 갱신과 필요한 WAL/SHM sidecar 생성을
  허용한다. Reader의 로그 내용·main/WAL 내용 쓰기, checkpoint·정리·rotation·삭제,
  새 중요 DB·catalog 할당은 계속 금지한다. owner·private 권한·inode 안전성 검사는 유지한다.
- 이는 LG-D15의 active SHM bytes·파일 목록 불변 보장을 제한적으로 대체한다. Active sidecar의
  forensic bytes 보존은 약해지며, 같은 UID라는 이유로 보안 영향이 없다고 단정하지 않는다.
  Archive의 원본 본체/WAL/SHM·파일 목록 불변과 snapshot 정리 조건, LG-D16은 유지한다.
- LG-D17의 고정 쓰기 우선 목표는 짧은 조회·경합 최소화 목표로 조정한다. Catalog 배타 lease를
  ro open → page 조회 → close 전체에 유지해 기존 rollover·삭제 직렬화를 재사용한다.
  전체 DB 복사·재-hash를 active 조회에서 제거하며, WAL 동시 읽기나 공정성·무손실 일반 보장은 주장하지 않는다.
- Paging token·generation 부분 결과와 같은 250MB 격리 조건의 누락·중복·writer lease 실패,
  page lease·refresh·rollover·WAL 비용을 확인한다. 제품 수치 상한은 측정 후 보고하며 지금 채택하지 않는다.
  실패를 숨기거나 자동으로 새 프로토콜을 도입하지 않는다. Intent/abort·전역 멱등화·queue/cache·
  archive별 lock/immutable fast path·삭제 규약 변경은 보류한다.
- 원 AC·gate·PLAN p5 의존성과 나머지 DVL-07/DVL-12 범위는 유지한다. 이전 실패 증거를 보존하며,
  Q01 기준 변경·proxy 채택·FL-01 제품 수정·실제 raw 활성화·실제 사용자 DB·유료 모델은 제외한다.

## 2026-10-10 현실 호출 부하 판별 승인

- LG-D19 / DVL07-usage-contention-boundary: 사용자는 claude-wb 자문을 종합한 제안에
  “그렇게 진행해 줘.”라고 승인했다. 중요 로그 조회·쓰기 경합의 이번 국소 완료 판별은
  실제 호출 주체를 모사한 현실 부하에서 전체 쓰기 시도 기준 누락·중복 0, 경합에 따른
  capture_unknown 0으로 한다. 사람의 page 이동·초 단위 Agent 조회·경량 status·backend 점검을
  모사하되, 실제 제품 호출 빈도나 자동 Agent 제한을 측정·보장한 것으로 해석하지 않는다.
  [승인 기록](../../../.workflow/dev-validation-log-retention/runs/dvl07-usage-20261010/authority.json)을 따른다.
- Active만 있는 약250MB DB와 rollover 뒤 약250MB archive 조회를 모두 판별한다.
  단독 active page의 약4ms 관측을 archive나 다른 환경의 상한으로 일반화하지 않는다.
  Writer 대기 분포·page EX 보유 시간·backend 지연과 watchdog을 함께 기록한다.
  조회 실패는 busy/부분 결과 등으로 표시하고, 저장 실패를 busy/capture_unknown으로 드러낸다.
  무음 누락은 허용하지 않는다.
- 끊김 없는 연속 조회 stress의 기존100ms 실패와 실험2초 결과는 알려진 한계 기록으로 보존한다.
  이 기록을 현실 부하의 통과 근거로 바꾸거나 삭제하지 않는다. 이번 완료 판별에서 연속 stress의
  누락0을 요구하지 않는다는 범위 조정만 승인하며, 원 feature AC·Q01·PLAN 의존성은 유지한다.
- LG-D18의 조회 구현과 원본 보호는 유지한다. 현실 부하에서도 실패하면 원인·결과와 최소 대안을
  보고한다. 제품 writer 대기 수치·archive immutable page 읽기·새 잠금 프로토콜은 지금 채택하지 않는다.
  해당 판별 통과도 B1 경합 국소 검증이며 DVL-07 전체·DVL-12·feature 완료는 아니다.
  Q01 기준 변경·proxy 채택·FL-01 제품 수정·실제 raw 활성화·사용자 DB·유료 모델 제외를 유지한다.

## LG-D20 — sealed archive page 직접 읽기 최소 후속 (2026-10-10)

- 사용자 결정: “응 일단 이어가자.”로 [최소 후속 제안](../../../.workflow/dev-validation-log-retention/runs/dvl07-usage-20261010/NEXT-SCOPE.md)을 승인했다. [승인 기록](../../../.workflow/dev-validation-log-retention/runs/dvl07-sealed-20261010/authority.json)을 따른다.
- LG-D19 현실 부하에서 active40/40, 실제 rollover archive23/40(17 busy 누락)이 관측됐다. 해당 실패 이력을 유지한다.
- `archiveChecks=complete`이고 WAL 없음 또는 0바이트인 sealed archive만 ro immutable로 한 page를 직접 읽는다. 기존 catalog EX는 open→query→close 전체에 유지한다. SPEC §7.3 조건의 적용이며 archive별 lock·삭제 규약 변경은 하지 않는다.
- 미checkpoint archive는 기존 snapshot/불가 사유 경로, active는 LG-D18 경로를 유지한다. 원본 bytes·파일 목록 불변, 최신 checkpoint/WAL 내용, 기존 안전성 검사를 유지한다.
- 측정 harness monitor 지연에도 기존 실험 watchdog을 적용한다. 기존 fixed-plan fixture·호출 일정·시도 쓰기·writer100ms/10ms를 유지하며 obsolete copy-start 동기화만 실제 archive page lease 진입 관측으로 바꾼다. 원시 측정은 별도 새 파일로 보존한다. 독립 테스트·250MB 측정·fresh review·영향권 최종 검사를 수행한다.
- 수치 정책·새 잠금/queue/cache/intent-abort/전역 멱등화는 채택하지 않는다. 전체 DVL-07/DVL-12 완료와 구분하며 Q01/proxy/FL-01/실제 raw/사용자 DB 제외를 유지한다.

## LG-D21 — 전용 DB 기록 기능 채택과 개발 재개 (2026-10-11)

- 사용자 결정: “c2는 추천안대로 해주고 q01이 결정되었으니 goal을 재게해줘.”로 전용 DB 기록 script/tool 방향과 개발 재개를 승인했다. [승인 기록](../../../.workflow/dev-validation-log-retention/runs/script-only-adoption-20261011/authority.json)을 따른다.
- 요약 agent에는 일반 shell·파일 수정 기능 대신 고정 DB 기록 기능만 제공한다. Script는 검증된 요약 데이터를 고정 저장 경로로 전달하며 임의 command·SQL·경로를 받지 않는다. Skill 지시만으로 실행 제한을 보장하지 않는다.
- 격리 OMP의 단일 tool 시험은 통과했으며 독립 테스트·리뷰 근거를 보존한다. 운영 중인 기존 agent/Task 경로 적용과 Q01 나머지 판별은 후속 구현·검증 대상이다. 방향 채택을 Q01 전체 통과로 기록하지 않는다. 새 운영 agent/상주 프로세스나 전송 차단 기준 변경을 이 결정으로 추정하지 않는다.
- C2는 실패 trace 또는 구체적인 새 재현 근거가 생길 때까지 추측성 제품 수정을 보류한다. 원 실패와 DVL-02/최종 통합 gate의 미완료 상태를 유지한다.
- 기존 조회 결정·검증과 PLAN 의존성을 유지한다. Proxy·실제 raw 활성화·유료 모델·사용자 DB·commit/게시/배포는 제외한다.

## LG-D22 — 운영 요약의 요청별 제한 SDK session 경로 채택 (2026-10-11)

- 사용자 결정: “문서 요약도 sdk session 경로로 채택해.”로 제안된 운영 요약 경로를 승인했다. [승인 기록](../../../.workflow/dev-validation-log-retention/runs/q01-sdk-route-20261011/authority.json)을 따른다. 별도 문서 요약 제품 기능을 추가하는 결정은 아니다.
- 기존 Manager/Worker OMP 프로세스 안에서 요청마다 제한된 SDK session을 만들고 기존 agent 정의 prompt와 정확한 skill bytes를 재사용한다. 일반 shell·파일 수정 기능 없이 요청에 결속된 전용 DB 기록 tool 하나만 제공하고 종료·실패 뒤 session을 dispose한다.
- 기존 Task tool 생명주기 재사용으로 표시하지 않는다. 새 agent 정의·상주 프로세스·별도 운영 OMP·직접 HTTP completion 경로는 채택하지 않는다.
- 경로 채택과 Q01 검증 통과는 별개다. 기존 8개 항목, 확인 전 전송0, 실행 중 위반의 결과 거부·보호, provenance·budget·priority·recovery 기준을 유지하고 이 경로로 재판별한다. 기존 Task/callback 실패와 SDK 국소 시험 결과는 각각 보존한다.
- DVL-09/10/14의 진입은 DVL-08의 충분한 통합 검증 뒤다. C2 hold와 기존 조회 결정, Q01 기준 변경·proxy·FL-01 제품 수정·실제 raw 활성화 등의 제외 범위를 유지한다.

## LG-D23 — 요약 시작 전 검증과 제한된 자동 복구 (2026-10-11)

- 사용자 결정: “추천한 시작 전 검증 경계로 변경”으로 Q01-tools의 완료 경계를 조정했다. 이어서 설정·검사 검증 실패 시 복원을 요청했다. [경계 승인](../../../.workflow/dev-validation-log-retention/runs/q01-sdk-route-20261011/startup-boundary-decision-20261011.json)과 [복구 요청](../../../.workflow/dev-validation-log-retention/runs/q01-sdk-route-20261011/startup-recovery-authority.json)을 따른다. 이전의 Q01 기준 변경 제외는 이 항목에 한해 대체한다.
- Host가 요청별 private SDK session의 제한 설정·전용 DB tool 실제 목록·검사 동작을 모델 전송 전에 확인한다. 검사 누락·무효·API 불일치·오래된 요청 결속이면 시작하지 않는다. 함수 등록이나 모델 자기보고만으로 통과시키지 않는다.
- 시작 전 검증이 실패하면 host가 session을 폐기하고 신뢰하는 구성으로 한 번 새로 생성·재검증한다. 이는 유한한 구현 기본값이다. 다시 실패하면 요약만 보류하고 원인을 표시하며 메인 작업은 계속한다. 모델이 코드·권한 정책을 고치거나 전송된 요약을 재실행하는 복구는 하지 않는다.
- 실행 중 요청·tool·모델·정책 불일치의 예방 차단과 비허용 tool event의 결과 거부·즉시 보호는 유지한다. 정상 시작 후 검사 코드를 고의로 제거·무력화하는 시험까지 견디는 보장은 완료 필수 조건에서 제외한다.
- 기존 고장 시험과 실패는 당시 기준의 한계 기록으로 보존한다. 새 경계의 시작 거부·복구·재검증·실행 중 보호를 별도로 시험한다. LG-D22 채택, 나머지 Q01 7개 항목·C2 hold·조회 결정·PLAN 의존성과 proxy·FL-01·실제 raw·사용자 DB·유료 모델 제외는 유지한다.

## LG-D24 — 작업 내용 중심 중요 로그와 작업 연결 기준 (2026-10-11)

- 사용자는 중요한 로그가 전체 시스템 로깅이 아니라 지시받은 작업의 내용을 나중에 파악하기 위한 것이라고 명확히 했고, “이것에 맞게 작업 방향을 수정해줘”라고 요청했다. [방향 변경 승인 기록](../../../.workflow/dev-validation-log-retention/runs/work-log-boundary-20261011/decision.json)을 따른다.
- 작업 목적·범위, 결정과 이유, 수정·시도, 검증 결과, 미완료·다음 행동을 중심으로 남긴다. Task/command ID와 revision, 관련 자료·source ranges·결과를 연결해 작업 간 혼동과 중복 요약을 막는다.
- Q01-origin과 LG-AC-22의 개별 사용자 발화→각 command 일대일 증명은 완료 필수 조건에서 제거한다. 직접 발화 identity나 실행 의도 추적을 얻기 위한 새 OMP API·permission UI·전체 대화 수집은 도입하지 않는다. Task 없는 command의 기존 포함 범위는 유예하거나 삭제하지 않는다.
- Task의 기존 승인·활성·종료 후처리·철회, 현재 pause, 기존 core 실행 범위, DB 전용 도구와 요청 결속·source 검증·no replay는 유지한다. 자동 notice·무관 Task 승인·unpaused만으로 새 작업 권한을 만들거나 다른 작업 자료를 귀속하지 않는다.
- 이전 origin source gap과 pending 자문은 당시 기준의 조사 기록으로 보존하며 새 기준의 차단 선행조건으로 사용하지 않는다. 새 Q01-origin은 작업/자료 연결과 범위·종료 처리의 실제 검증을 요구한다. Frozen 판정 도구와 해당 구현은 독립 시험·리뷰·재동결로 정렬해야 하며, 이 결정 자체를 Q01 통과로 표시하지 않는다.
- 나머지 Q01 기준·gate ID/수준·PLAN 의존성, SDK 경로, 조회 결정, C2 hold 및 proxy·FL-01 제품 수정·실제 raw 활성화·사용자 DB·유료 모델·commit/게시/배포 제외는 유지한다. 기존 Q01 기준 변경 제외는 이번 작업 연결 조건에 한해 대체한다.

## LG-D25 — 원인 수정·복구는 기존 승인 Task 방식 사용 (2026-10-11)

- 사용자 결정: repair-scope 추천 A를 선택하고 이어서 개발 재개를 요청했다. [승인 기록](../../../.workflow/dev-validation-log-retention/runs/continuation-20261011/repair-scope-A-decision.json)을 따른다.
- 이미 승인된 Task 범위에서는 원인 분석 → 영구 수정 → 회귀 검증 → 재시도를 진행한다. 범위 밖 파일·설정은 필요한 대상만 포함한 작은 수정 Task로 승인받는다. Workbench 제품 전체에 대한 상시 자동 수정 권한은 추가하지 않는다.
- 현재 개발의 수정·독립 시험·리뷰 권한은 유지한다. 운영의 명시 verify 계약과 미전송 입력만 재시도하는 no replay 경계를 유지하며 자동 verify loop·새 복구 서비스·queue를 추가하지 않는다.
- 이 결정은 DVL-09/10/14 후속 연결의 복구 범위이며, Q01 통과나 실제 raw 활성화를 뜻하지 않는다. 기존 제외 범위와 C2 hold를 유지한다.

## LG-D26 — 최초 host 입력 quota와 후속 turn 예약 (2026-10-12)

- 사용자 결정: 입력 예산 적용 범위 추천에 “그럼 그렇게 하자.”로 동의했다. [승인 기록](../../../.workflow/dev-validation-log-retention/runs/dvl10-20261012/u1-host-input-20261012/decision.json)을 따른다.
- `min(8192 tokens, 확인된 model context의 10%)`는 host가 선정한 최초 입력에 적용한다. 모델의 tool 호출·고정 결과 확인문이 추가되는 후속 SDK turn에는 같은 quota를 반복 적용하지 않는다.
- 최초 전송 전에 필수 후속 turn의 증가분까지 수용 가능한지 검사한다. 후속 turn은 최대 output 전량 예약·기존 context/고정 instructions·전체 contextWindow와 전송 body 32KiB로 판별한다. 결과 8KiB와 output ceiling 초기8192 이하 등 숫자는 유지한다.
- bytes 보수 estimator에서 token ceiling을 임의 byte 비율로 변환하지 않는다. 확인된 결과 byte 상한과 실제 framing·고정 tool 결과를 사용한다. 필수 envelope·모델/tokenizer/output 의미를 확인하지 못하거나 예산이 부족하면 전송 전에 사유를 표시하고 보류한다.
- 이 결정은 U1 해석을 확정하며 실제 모델 적합성·운영 metadata·Q01 통과·DVL10 통합 완료·raw 활성화가 아니다. 기존 C2 hold·조회 결정·no replay·승인/pause·제외 범위를 유지한다.

## LG-D27 — 검증용 OMP의 준비·scripted 검증·observer I/O 경계 (2026-10-13)

- 사용자 결정: 검증 환경의 허용 동작을 구분하는 제안에 “좋아 그렇게 해줘.”로 동의했다. [승인 기록](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261013/io-boundary01/decision.json)을 따른다.
- 소유한 임시 환경 준비에서 빈 private 저장소 생성·열기를 허용한다. Scripted 검증에서는 소유 localhost provider 통신·임시 DB 기록을 허용한다. 사용자 인증정보·실제 로그 접근, 외부 통신, 유료 모델 호출은 전 구간 금지한다.
- Metadata observer의 모델 호출·대화 입력·네트워크·auth store open·설정 쓰기0는 유지한다. 검증용 SDK child의 새 auth store open·설정 쓰기는 금지한다. 준비 I/O와 observer/child 관측은 분리하며 금지 동작이나 격리/귀속 불명은 pass로 판정하지 않는다.
- 이 결정은 DVL-14 검증 경계의 후속 승인이다. 공개 capability를 먼저 확인하고 최소 구현·독립 테스트·리뷰를 진행한다. 기존 실패를 새 기준의 pass로 재표시하지 않고, 운영 MODEL unknown·Q01 나머지 기준/caps·LG-D22 route·DB-only·no replay·C2 hold·실제 raw 미활성화를 유지한다.

## LG-D28 — 검증용 OMP의 정상 시작·모델 목록 조회 허용 (2026-10-13)

- 사용자 결정: 외부 통신 전체 차단과 OS 격리안을 철회하고, 임시 HOME·설정·DB에서 OMP의 정상 시작·모델 목록 조회 통신을 허용하는 최소 검증안에 “그렇게 해줘.”로 동의했다. [승인 기록](../../../.workflow/dev-validation-log-retention/runs/dvl14-20261013/normal-startup01/decision.json)을 따른다.
- LG-D27의 전 구간 외부 통신 금지는 검증 환경 준비의 정상 시작·모델 목록 갱신에 한해 대체한다. 사용자 인증정보·실제 로그를 넘기지 않으며 유료 모델 호출은 금지한다. 검증 모델 요청은 소유 localhost scripted provider로 처리하고 결과는 임시 DB에만 기록한다.
- OS sandbox·provider blacklist·새 상주 서비스를 추가하지 않는다. 시작·목록 갱신 I/O는 준비 동작으로 분리해 기록하고, metadata observer의 기존 무부작용 조건 및 SDK child의 새 auth store open·설정 쓰기0는 유지한다. 귀속 불명은 pass로 판정하지 않는다.
- 기존 운영 route·DB-only·no replay·caps·C2 hold·실제 raw 미활성화와 gate ID/level·의존 graph는 유지한다. LG-D27과 최초 실패·과거 검증은 이력으로 보존하며 새 기준의 pass로 재표시하지 않는다. 이 결정만으로 제품 verifier·MODEL-metadata·DVL-14 전체 또는 DVL-13 최종 검증이 완료되지는 않는다.

## LG-D29 — Bash 중심의 외부 terminal 검증 (2026-10-14)

- 최초 결정은 “tmux는 검증이 중요하지만 dash는 안해도 될거 같아. 난 그걸 사용해본적이 없어. herdr는 몰라도.”에 따른 dash 셀 제외였다. 같은 날 사용자가 “미안 dash가 sh인줄 몰랐어. sh는 필요해.”라고 정정해 제외를 철회했다. 현재 필수 범위는 plain 및 설치된 tmux/Herdr의 Bash·sh 경로이며 tmux 검증을 우선한다. 이 호스트의 sh fallback은 dash이므로 기존 dash 셀을 그대로 사용한다.
- 기존 Bash 우선·`/bin/sh` fallback·shell 부재 진단과 외부 tmux/Herdr 지원 계약을 유지한다. C1~C7의 입력·복사·resize·실제65초 detach·소유 자원 정리와 실제60초 점검 등 시간 의미도 유지한다. 없는 outer 또는 격리 불가는 미검증 사유로 보고한다.
- 최초 제외 결정에 따른 후보·fixture 결과와 과거 tmux/dash C2 실패·보류 기록을 보존하며 현재 범위의 pass로 재표시하지 않는다. 기존 C2 hold는 해제하지 않는다. DVL-02·DVL-13의 적용 범위를 정렬하며 gate ID·evidence level·의존 graph, Q01/caps/no-replay·실제 raw 비활성은 유지한다.
