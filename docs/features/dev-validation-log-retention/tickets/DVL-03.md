# DVL-03 — 로그 공통 계약과 private 중요 store 기반

계획 revision p5 / SPEC s5 / BRIEF r0.9 (r0.8 승인 + 운영 원칙 개별 합의).
상태·의존성·resources·scope의 canonical source는 [PLAN.json](../PLAN.json)의 DVL-03이다.
이 ticket은 계획안이며 제품 구현/시험을 시작할 권한은 아직 없다.
기본 계약: [SPEC](../SPEC.md), [BRIEF](../BRIEF.md); source decision은 BRIEF 추적표를 따른다.

## 제공할 행동

Source/Subject/Range/Health/Summary JSON v1 및 안전한 SQLite 기본 저장·조회와 영속 제어 계약을 통합한다.

공통 SourceRef/Range, ImportantEntry, HealthSnapshot, SummaryRequest/Result의 v1 bytes/validation contract를 구현한다. 문서·코드 comment 언어는 repo 규칙을 따른다.
별도 중요 DB의 UUID/schema/private path, WAL/FULL/INCREMENTAL 생성과 bounded codec 저장·readonly 조회를 구현한다.
운영 metadata에는 source/proof 및 summary request/no-replay 제어를 위한 additive versioned schema/API를 만든다. 중요 body와 원문 사용자 대화는 넣지 않는다.
소유 path/mode/alias/symlink/regular-file 검사와 migration의 rollback을 기존 패턴으로 처리한다.
소비 ticket들이 fixture에서 사용할 typed ports: RawReader, SubjectEventSink, ImportantStore/Health, SummaryAdapter/RequestJournal을 제공한다.
현재는 자동 요약·retention·rotation·Agent 호출을 연결하지 않는다. 계약의 immutable 필드와 오류 shape를 먼저 고정한다.

## 수락·검증 계약

수락 ID: LG-AC-05, LG-AC-06, LG-AC-11, LG-AC-18.
STORE-contract만 이 ticket에서 완료; LG-AC-05/06/11/18 전체 수락은 뒤 owner/INT-runtime.
Gate supplier/required evidence level 및 체크 ID는 PLAN의 gate_items/required_checks를 따른다.
전체 trace는 SPEC §11이며 local 부분과 뒤 combined coverage를 바꾸거나 삭제하지 않는다.

검증: Python/TS roundtrip/strict schema, wrong ID/range/version, compressed/decompression bound·checksum, private path/alias/fifo, DB reopen/transaction rollback/migration; 실제 tools/model 없이 positive+negative contract fixtures.

후속 implement는 coherent candidate의 독립 test_designer 평가/시험과 frozen reviewer 검토를 포함한다.
다른 ticket 또는 실제 OMP 없이 관측 불가능한 항목을 fixture/문구 시험만으로 pass하지 않는다.
Required item unknown이면 실패 원인/한계와 evidence를 남기고 affected gate를 닫는다.

## 구현 경계·안전

새 외부 SQLite extension/agent 정의를 필요로 하거나 기존 metadata 보존/append-only를 깨면 중단. 후속 구현 없이는 검증 불가능한 gate를 이 ticket pass로 넣지 않는다.
기존 dirty/new 사용자 파일과 legacy log/DB/core 승인 증거를 보존한다.
사용자/실제 Workbench·DB·socket·tmux/herdr를 대상으로 시험하거나 정리하지 않는다.
새 제품 agent 정의/상주 OMP/독립 Python 모델, Phase2, commit·게시·배포는 범위 밖이다.
권한이 필요한 대안은 Root에 사실/대안/영향을 전달하고 임의 확장하지 않는다.

## 작업·인계

추천 worker: worker_senior; 근거: SPEC.md §5/7.1; schema v2・RawLogStore path safety.
추천은 실제 dispatch pin이 아니며 Root가 당시 code/host/권한을 재확인한다.
물리 작업 공간은 PLAN의 policy를 따르고 parallel writer는 별도 검증된 workspace/service namespace가 필요하다.
write_scope_hint는 계획 추정이며 dispatch 때 baseline·실제 경로를 검증한다. 이 Markdown에 별도 graph/status를 만들지 않는다.
중앙 PLAN/state/승인은 Root만 변경한다. Writer는 candidate delta/검사 evidence/남은 unknown을 반환한다.
현재 미검증: 실제 agent safety/skill behavior는 DVL-08/09/10 pending.

## p2 리뷰 보강

계약 bytes/digest와 golden SummaryRequest/Result fixture를 제공한다.08의 probe-local envelope는 독립이며09가 두 계약/adapter의 같은 fixture를 비교한다. 이후 contract 수정자는 PLAN의 exclusive claim과 version/digest·소비자 영향 재검증을 따른다.
