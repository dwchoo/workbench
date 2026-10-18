# 로그 용량 관리 운영 마무리 — 구현 명세

Revision: s1. 입력: [후속 BRIEF b1](BRIEF.md), [상위 BRIEF](../BRIEF.md), [상위 SPEC](../SPEC.md).
상태: 계획 초안. bundle 승인은 [approval.json](approval.json)에 별도 기록하며 계획 검증 통과가 승인은 아니다.

## 문제와 해결

화면 메모리 제한과 디스크 보관 제한이 혼동되고, 현재 legacy 저장 중단·비활성 순환·중요 DB의
부분 자동 정리가 하나의 전체 용량 상한처럼 보일 수 있다. 기존 두 CLI의 사람용 표시와 사용 절차를
보완한다. 새 통합 명령·삭제 엔진·설정 항목 없이 보관 정책을 정확히 설명한다.

사용자는 현재 얼마나 저장됐는지 확인하고, 신규 원본 순환으로 관리될 부분과 수동 정리가 필요한
부분을 구분하며, 작업 기록을 유지한 채 나중에 명시적으로 운영 정책을 적용할 수 있어야 한다.

## 범위와 기술 상세화

**합의 조건:** 원본 공용 100MB×5, 활성 중요 DB 250MB/120일 기존 정리, 보존 DB 수동 선택,
legacy 불변, 실용적 다섯 안전 조건, C2 hold, gate 보존, 실제 활성화/삭제 별도 승인.

**계획상 기술 상세화:** 기존 CLI의 기본 출력만 한국어 안내로 보완한다. `--json`의 key/type/구조,
기존 exit code와 query/mutation 분리, transport·권한 모델은 바꾸지 않는다. 기본 출력은 사람이 읽는
표시로 취급하며 기존 raw-status JSON 소비자는 명시적 `--json`을 사용하도록 안내한다.
새 TUI 화면이나 상태 프로토콜, 보관 설정은 만들지 않는다.

## 기존 연결점과 관측 사실

| 파일/접점 | 계획 당시 관측과 사용 범위 |
|---|---|
| `src/workbench/backend/cli.py:cmd_raw_read` | raw-status의 기본/JSON 출력이 둘 다 JSON. 기본 출력 formatter의 최소 변경 후보 |
| `src/workbench/backend/cli.py:_important_print` | status에 DB 수·물리 합계·active UUID·복수 DB 경고. 기존 databases 항목을 풀어 안내 |
| `storage/log_raw/port.py:raw_usage` | legacy run/terminal bytes 및 상이한 한도, segmented raw/metadata/limit·부분 상태. 계산을 새로 만들지 않음 |
| `backend/service.py:raw_status` | 현재 singleton과 기존 leaf lock을 통해 usage 조회. activation과 신규 source 정책을 구분 |
| `storage/log_raw/segments.py` | 100,000,000 bytes×5; storage_usage는 raw_bytes/metadata_bytes/raw_limit_bytes. 전체 data dir 상한 아님 |
| `storage/important_log/reader.py:status` | databases별 role·physicalBytes와 개수·합계·cleanupNeeded. 기존 readonly 경계 유지 |
| `storage/important_log/maintenance.py` | TARGET_BYTES=250,000,000, LOW_WATER_BYTES=225,000,000, RETENTION_DAYS=120. 정책 변경 없음 |
| `backend/raw_rotation.py`, `cli.py:cmd_raw_rotation` | 로컬 record와 live admission, 현재 install 자산 관측, --yes가 필요한 최초 활성화. 우회 금지 |

이 경로들은 현 checkout의 source 관측이다. runtime 성공으로 기록하지 않는다. 인용된 상위 문서의
`tests/backend/test_raw_policy.py`·`test_logs_cli.py`는 현재 존재하지 않아 신규 검증 명령으로 사용하지 않는다.

## 공개 인터페이스·데이터와 불변 조건

1. `wb logs raw-status [--json]` 기본 표시:
   - 활성화 결정 유무와 실제 **신규 source 정책**(`legacy-stop`/`segmented-v1`)·기존 사유를 구분.
     활성화돼 있어도 보호 상태라 신규 source가 legacy인 경우 그대로 표시한다.
   - legacy 실험 bytes/512MiB 합계 제한·64MiB/run, terminal bytes/64MiB/command를 따로 표시.
     terminal 전체 합계의 상한을 발명하지 않는다.
   - segmented raw bytes/500,000,000 bytes 예산과 metadata bytes를 따로 표시한다.
     missing store는 미생성, 읽기 실패는 unknown/partial이다. 미확인 bytes를 0으로 추정하지 않는다.
   - 기존 legacy·중요 DB·기타 파일까지 합친 전체 용량 상한이 아니라는 짧은 설명과
     `logs important status` 안내를 포함한다. 디렉터리 전체 재귀 스캔은 추가하지 않는다.
2. `wb logs important status [--json]` 기본 표시:
   - 현재 응답의 DB 개수·물리 합계, databases별 UUID·role·physicalBytes와 정리 필요를 표시.
     `orphan`/불명은 삭제 가능한 archive로 안내하지 않는다.
   - 활성 DB 250MB는 **논리 용량 목표**이며 보존 DB를 합친 전체 물리 상한이 아님을 설명한다.
     120일은 보관 종결부터 계산하고 용량 압박 시 더 이른 정리가 가능함을 간단히 안내한다.
   - 현재 응답에 active 논리 사용량이 없으면 물리 크기에서 역산하지 않는다. 이 값을 얻으려고
     새 query나 DB 전체 스캔을 추가하지 않는다. 상세 저장 상태의 기존 소비자는 그대로 유지한다.
   - 복수 DB의 기존 지속 경고 및 UUID 선택 delete-db 안내를 유지한다. 조회만으로 삭제/정리하지 않는다.
3. 기존 JSON 응답을 그대로 보존한다. 원본 body·SQL·환경·인증정보를 status에 넣지 않는다.
   기존 조회에서 허용하는 active SQLite SHM/read mark 부작용은 상위 LG-D18 범위이며,
   본체/WAL 내용 변경·checkpoint·prune·새 catalog 할당을 조회의 일부로 추가하지 않는다.
4. 기존 제한과 권한을 상수 복제나 별도 새 정책으로 구현하지 않는다. 제품이 제공하는 실제 한도를
   우선 표시하고, 중요 DB 보관 기본값은 기존 정의를 재사용한다. formatter 테스트도 정책을 독자 계산하지 않는다.
5. 현재 코드에서 추가 port/schema/잠금/삭제 API 없이는 달성할 수 없는 사항이 나오면 해당 항목만
   원인과 영향 범위를 보고한다. 기존 미확인·실패를 숨기거나 넓은 구조 변경으로 우회하지 않는다.

## 상태·실패와 운영 경계

- 이번 개발 중 실제 `~/wb-demo`는 명시된 data root의 metadata/상태 조회만 허용한다. 모델·prompt·
  로그 본문·인증파일을 읽지 않는다. 실행 중이라 전후 관측이 달라지면 시점 차이/부분 결과로 표시한다.
- legacy 정리 후보는 확정 종료된 작업과 정확한 경로·용량을 연결하고, 실행 중/종료 불명은 보류한다.
  별도 dry-run 제품 기능을 만들지 않는다. 삭제용 glob·일괄 rm 스크립트를 생성하거나 실행하지 않는다.
- `logs important delete-db --db UUID`는 기존 보호 이월·active 거부·삭제 후 재관측 계약 그대로다.
  실제 legacy 수동 삭제는 대상 선택·별도 승인·종료 확인 후의 별도 운영 단계로 남긴다.
- 자동 raw 순환은 요약 성공을 기다리지 않는다. 요약 장애는 마지막 정상 기록을 보존하고 상태를 남긴다.
  raw gap·작업 귀속·sent/unknown no replay를 기존 collector/journal/writer에서 유지한다.
- 실제 source의 최초 활성화는 별도 승인과 현재 로컬 product evidence 검증이 필요하다.
  기존 gate가 불충족이면 **후속 개발 완료/운영 활성화 보류**를 분리해 보고하고 조건을 완화하지 않는다.
- C2 hold는 유지한다. 이번에는 새로운 실패 trace 없이 shell/terminal 제품 수정을 하지 않는다.

## 수락 조건과 책임

| ID | 조건 | 담당 ticket / 공개 관측 | 최종 확인 |
|---|---|---|---|
| LSO-AC-01 | 원본 용량·실제 적용 정책·한도 적용 범위를 혼동 없이 표시 | DVL-OPS-01/02, raw-status 기본/JSON | DVL-OPS-03 mixed/readonly CLI |
| LSO-AC-02 | 활성/보존 DB의 개수·물리량·목표·정리 필요·unknown을 정확히 표시 | DVL-OPS-01/02, important status | 기존 retention/rollover와 연결 |
| LSO-AC-03 | 조회가 모델·verify·활성화·정리·로그/설정 쓰기를 유발하지 않고 JSON 계약 유지 | DVL-OPS-02, 기존 CLI 호출 경로 | foreground/중지 조회를 임시 환경에서 대조 |
| LSO-AC-04 | 순환 중 결과·필수 근거·정상 요약을 보존, gap·실패/no replay 유지 | DVL-OPS-03, 실제 producer/store/reader·journal | 작은 임시 mixed runtime + 유효 근거 재사용 |
| LSO-AC-05 | legacy와 보존 DB 정리 대상/유지 대상을 구분, 실제 정리 미실행 | DVL-OPS-01/03, 후보 보고·운영 안내 | source/gate/승인 경계 검토 |
| LSO-AC-06 | 상위 28 AC/46 gate·완료/실패 이력을 보존, 영향별 증거 재사용과 한계를 명시 | DVL-OPS-01/03, 현재 diff 대조·근거 대응표 | 기존 goal과 분리한 후속 결과 |
| LSO-AC-07 | 최초 활성화 조건·별도 승인 유지, 차단을 정상 적용으로 표시하지 않음 | DVL-OPS-02/03, activation status/기존 거부 seam | activate 명령 미실행·운영 준비 결과 |
| LSO-AC-08 | 관련 변경만 독립 검증·리뷰하고 임시 소유 자원 정리, 기존 사용자 변경 보존 | DVL-OPS-03, 격리 테스트/cleanup/scope audit | 성공·실패 잔여물·미검증 보고 |

조건과 gate 정의의 기계 입력은 [requirements.json](requirements.json), owner/supplier는 PLAN만을 따른다.
상위 required list는 원본을 참조하며 로컬 gate 7개가 기존 46개를 대체하지 않는다.

## 검증과 기존 근거 재사용

- DVL-OPS-01은 원 기능의 최종 outcome/gate 및 각 영향 gate의 실제 check·candidate·입력 hash를
  대조한다. `done_verified` flag만으로 재사용하지 않는다. stale/unknown은 원인을 보존한다.
- CLI 단위는 실제 parser/main을 통해 기존 status 응답에 기반한 사람용 문구와 JSON 동등을 검사한다.
  모델·verify·mutation 호출 0, 없는/부분 저장소를 확인한다. 임의 내부 고장 matrix는 추가하지 않는다.
- 기존 시험 후보: `tests/backend/test_raw_rotation.py`, `test_raw_rotation_independent.py`,
  `test_summary_activation_identity_independent.py`; `tests/storage/test_raw_segments.py`,
  `test_raw_reader.py`, `tests/storage/important_log/test_retention.py`, `test_rollover.py`,
  `test_dvl07_delete_independent.py`; `tests/integration/test_raw_rotation_independent.py`,
  `test_raw_rotation_volume_independent.py`, `test_validation_retention.py`.
  전부 실행하라는 목록이 아니다. 기존 record와 diff 영향으로 정확한 case를 선정한다.
- `test_raw_rotation_volume_independent.py`의 500MB 근거는 실제 bytes/store 검증이지만 shell drain과
  policy는 controlled이며 scheduler/model은 없다. 이 한계를 유지한다. 변경이 formatter뿐이면
  동일 source의 유효 대용량 근거를 재사용하며 500MB를 다시 쓰지 않는다.
- 부족한 mixed 통합 근거만 기존 producer/reader·local scripted provider를 재사용해 작은 임시 한도로
  검증한다. store만 직접 호출한 결과를 scheduler/실제 OMP 경로 성공으로 확대하지 않는다.
  exact bytes·source/range·결과/근거·실패 상태와 cleanup을 관측한다.
- 예시 명령은 승인 후 현재 Python에서 `PYTHONPATH=src <python> -m unittest <정확한 case ID>`.
  범위 외 테스트 자동 discovery를 피하고 모듈 import 시 output env가 필요한 기존 fixture는
  Python 시작 전에 새 전용 절대경로를 지정한다. 이전 evidence 디렉터리를 덮어쓰지 않는다.
- 작은 변경을 하나의 독립 test_designer assessment와 frozen reviewer 단위로 묶는다.
  보안 trigger(새 parsing/path/권한/네트워크/의존성)가 diff에 있으면 별도 security review·필수 검사를
  수행한다. 없으면 no security signal을 명시한다. final cross-ticket 확인은 생략하지 않는다.
- 새 격리 실행 전/종료 후 AGENTS의 용량 확인·10GiB 경계·캐시 정리·소유 process 정리를 적용한다.
  보고서/실패 trace/DB 결과는 보존하고 대형 private home/cache/byte checkpoint를 복제하지 않는다.

## 기준선·호환과 인계

HEAD `c15ae38b6ada6f2ae3bb0ca706ff90b564072fd7`. CLI down alias와 UI 색상 cache 및 관련 독립 시험,
사용자 skill/AGENTS/contract·기존 workflow 변경이 dirty/new다. 이 plan은 이들을 덮어쓰거나 commit하지 않는다.
구현 시작 때 현 입력 manifest·기존 agent/소유 process 상태를 다시 확인하고 한 workspace의 writer는 하나만 둔다.

상위 DVL-01~14 완료·실패 이력과 기존 final 46 판정은 별도 과거 candidate의 증거다. 이번 후속 PLAN의
상속 registry에 46 ID/증거 수준을 그대로 연결하고, 실제 변경의 영향 gate만 current evidence로 갱신한다.
기존 Q01, C2 hold, real-model 한계는 삭제하지 않는다. 새 paid model 실험·OS 격리·outer 전체 재검증은
formatter 변경만을 이유로 추가하지 않는다. 기존 실제 모델 시험도 replay하지 않는다.

구현·검증 완료 후에는 사용량 보고, 해당 변경, 실행/재사용 근거, 미검증 한계, 정리 후보, 운영 활성화
가능/보류 사유를 제시한다. 준비 문서에 실제 대상의 명령을 적더라도 실행 권한은 생기지 않는다.
실제 삭제/활성화·commit/게시/배포는 별도 요청이다. 새로운 차단 결정이 발견되지 않으면 재인터뷰하지 않는다.

## Ticket graph

[PLAN.json](PLAN.json)이 유일한 dependency/resource graph다. 흐름은
`DVL-OPS-01 → DVL-OPS-02 → DVL-OPS-03`이며, 초기 frontier는 OPS-01 하나다.
작은 공통 CLI 변경이므로 순차 실행하며 test writer·Root 통합도 같은 workspace에서 겹치지 않는다.
