# WB 작업 기록 — 구현 명세

Revision: s1; source brief: [BRIEF.md](BRIEF.md) r0.2.
상태: planning bundle 확인 대기. BRIEF 승인과 이 SPEC·PLAN·ticket 묶음의 승인을 구분하며 실제 권한과 digest는 [approval.json](approval.json)을 따른다. 제품 구현·테스트 작성은 시작하지 않았다.

## 문제와 해결 방향

WB에서 “이 내용을 기록해줘”라고 요청할 때 프로젝트 문서 파일이 기본 산출물이 되지 않도록 한다. 작업 메모·결정·진행 상황·실험 정리·인계의 원본은 프로젝트 WB 상태의 SQLite에 저장한다. Manager·Worker가 같은 저장·조회 도구를 사용하고, 같은 프로젝트의 새 세션에는 최근 기록의 제목·미리보기를 제공한다. 수정은 이전 내용을 보존하는 새 revision이며 Markdown은 요청한 시점의 사본이다.

BRIEF의 WR-D01~07·WR-P01~05가 사용자 합의다. 아래의 모듈·schema·명령·수치·test seam은 해당 합의 안에서 정한 기술 구체화이며 이 planning bundle 승인 대상이다. BRIEF의 bytes나 승인된 제품 동작은 변경하지 않는다.

## 주요 사용자 흐름

1. Manager 또는 Worker에게 메모를 남겨 달라고 한다. 기록 도구가 DB 저장 성공과 ID·revision을 반환한 뒤 저장 완료를 알린다. Task가 없어도 프로젝트 메모를 저장할 수 있다.
2. 새 세션은 최근 기록의 제목·짧은 미리보기를 받고 필요한 본문을 ID로 조회한다. 목록·문자열 검색으로 오래된 기록도 찾는다.
3. 기록을 고치면 같은 ID의 다음 revision이 생긴다. 기본 조회는 최신본이고 지정한 이전 revision도 읽을 수 있다.
4. 사용자가 기록과 출력 경로를 지정해 Markdown을 요청한다. 최신 또는 지정 revision을 ID·revision·시각과 함께 출력하고 DB 원본을 유지한다.

## 범위와 비목표

- BRIEF의 WR-AC-01~12 전체를 구현한다. Linux·Python 중심, 기존 OMP 두 개·bridge·프로젝트 상태 해석을 활용한다.
- BRIEF·SPEC·ADR은 파일로 유지한다. 사용자 직접 호출용 WB 문제 보고서도 기존 Markdown 계약을 유지한다. 일반 메모와 구분하는 짧은 지침을 두 역할의 상시 prompt에 제공한다.
- Task 실행·지시·질문·응답은 기존 전달 경로를 유지한다. 기록의 본문·미리보기는 참고 데이터이며 실행 승인·Task 명세·완료 판단을 대체하지 않는다.
- 삭제·자동 정리·기존 Markdown 수입·양방향 동기화·의미 검색·embedding·별도 agent·상주 서비스·외부 DB·신규 dependency는 제외한다.
- `wb migrate`의 기존 중단 상태 단일 저장소 이관에서 신규 기록 DB를 보존하는 호환 확장만 포함한다. live 이관·병합·폴더 이동 자동 복구 등 새로운 이관 방식은 추가하지 않는다.
- 1회 export는 기록 ID 하나와 최신 또는 지정 revision 하나를 선택한다. 여러 기록을 합쳐 보고서를 새로 작성하는 기능은 포함하지 않는다.

## 현재 저장소와의 접점

조사 기준: HEAD `f3eac23ec7ce7d806c2975139a55c5faf54a1528`, 2026-10-20의 실제 파일. 기존 workflow·skill·CONTEXT 변경과 이 기능의 untracked 문서를 포함해 조사했다. 기준 manifest는 `.workflow/workbench-records/runs/plan-p1-20261020/before.json`이다. 현재 구현의 통과 상태를 조사한 것은 아니다.

- `backend/project.py::resolve_project_context`와 CLI `_layout`은 가까운 `.wb/`, 명시적 data-dir, 기존 backend identity를 해석한다. `paths.py::resolve_data_dir`만 보고 구형 XDG 기본값을 새 기능에 복제하지 않는다.
- `DataLayout`은 `tasks.sqlite3`와 `important-logs/`를 분리한다. 기존 중요 로그 본문은 `facts`·`interpretations`·`unknowns` 계약이므로 일반 메모를 여기에 억지로 넣지 않는다.
- `service.py::_tool_request`와 G3 `tool_request/tool_result`는 현재 역할·session ID·generation을 연결한다. `workbench_logs`는 읽기 전용이므로 새 기록 저장을 그 이름으로 확대하지 않는다.
- `bridge.ts`에는 `session_start`, `session_switch`, subagent 도구 차단, at-most-once 전송·결과 불명 처리가 있다. `launcher.py`는 역할별 도구와 상시 append prompt를 관리한다.
- `tests/bridge/handoff_tools.test.ts`는 실제 UDS와 대체 OMP API로 extension을 확인한다. 이는 실제 OMP lifecycle·context 전달을 입증하지 않는다.
- `tests/backend/q01_sdk_context_probe/context_extension.ts`에 `pi.sendMessage(..., {triggerTurn:false})` 사용 사례가 있다. startup·session switch에서 UI와 다음 provider context에 미리보기를 넣는 전체 조건은 아직 미검증이다.
- `backend/migrate.py`는 알려진 파일·SQLite schema를 검사한다. 신규 `records.sqlite3`를 무조건 복사하는 것으로 이관이 끝난다고 가정하지 않는다.
- `build_support/assets.py`는 canonical `omp_bridge/`를 배포 자산에 수집한다. 자산 경로·역할 도구 allowlist 변경은 설치본에서도 확인해야 한다.

## 공개 인터페이스와 데이터 계약

### 저장 단위와 모듈

- 기본 파일: 검증된 `DataLayout.root / "records.sqlite3"`. `.wb/` 프로젝트와 기존 명시 data-dir 모두 현행 context/identity 해석을 사용한다. Model은 DB 경로나 프로젝트 identity를 지정하지 않는다.
- `workbench.contracts.records_v1`: 요청·응답·한도·오류 코드의 Python 계약. `omp_bridge/contract/records_v1.ts`와 독립된 JSON fixture에 같은 wire 의미를 고정한다. Python이 저장·조회·export를 소유한다.
- `workbench.storage.work_records.store.RecordStore`: create/revise/get/list/search/history. 같은 데이터 root 안의 기록만 다룬다. 실제 SQLite와 파일시스템을 사용하는 공개 module seam이다.
- `workbench.storage.work_records.exporter.export_record`: `RecordStore.get`이 반환한 특정 revision snapshot을 지정 경로에 출력한다. 본문을 요약·재작성하지 않는다.
- `backend.records_service.RecordService.handle`: host가 바인딩한 layout·역할과 검증된 G3 request를 받아 저장소 호출을 수행한다. DB root·actor·session identity는 본문 필드에서 받지 않는다.
- `backend.records_cli`: `wb records list|search|show|history|export`를 기존 CLI에 연결한다. 사용자 조회·export는 backend/model을 새로 시작하지 않는다. Agent 저장·수정은 `workbench_records` 도구를 사용한다.

### 저장 schema와 revision

SQLite `user_version=1`의 별도 저장소로 시작한다. 새 테이블은 schema metadata, 기록 header, append-only revision으로 나눈다. Header는 UUID record ID와 생성 시각을 보관하고, 각 revision은 양의 정수 revision·제목·본문·선택적 Task ID·저장 시각·실제 기록 역할을 보관한다. 조회 순서를 안정화하는 DB 내부 sequence는 구현 상세다.

- 최초 revision은 1이다. `revise`는 전체 제목·본문·Task 연결 snapshot과 `expected_revision`을 받는다. 최신본과 다르면 `revision_conflict`이며 어느 기존 revision도 수정하지 않는다.
- 새 revision과 최신본 조회에 필요한 상태는 한 transaction에서 확정한다. 동시 수정 시 한 요청만 해당 expected revision에서 성공한다. 과거 revision의 UPDATE·DELETE·REPLACE로 보존 조건을 우회하지 않는다.
- Task ID가 null이면 프로젝트 메모다. null이 아니면 현재 저장소의 `TaskRepository.get_task_spec`에 해당 Task가 있는지 확인한다. 확인 불가를 프로젝트 메모로 조용히 바꾸지 않는다. 기록 저장 자체가 Task 승인 상태를 바꾸지 않는다.
- 같은 project/data-root를 다른 역할·새 process에서 열어도 같은 ID·revision을 읽는다. 다른 프로젝트의 data-root를 요청 인자로 선택하는 기능은 없다. 명시적 legacy data-dir 선택은 기존 호환 계약을 따른다.
- 새 DB는 첫 쓰기 때만 만든다. DB가 없는 정상 저장소의 목록·검색·history/show는 각각 빈 목록 또는 `not_found`이며 조회만으로 DB를 생성하지 않는다. 기존 DB의 권한·schema·읽기 오류는 `unavailable`이다.
- 초기화되지 않은 프로젝트는 `project_not_initialized`로 안내하며 조회/export 명령이 `.wb/`를 자동 생성하지 않는다. 위 DB 부재의 정상 빈 결과는 이미 유효한 프로젝트 상태 또는 기존 명시 data-dir 안에서 records DB만 없는 경우다.
- 검증된 private data-root, 사용자 소유의 일반 DB 파일과 0600 권한을 사용한다. Symlink·지원하지 않는 schema·변조된 저장소를 새 빈 DB로 덮어쓰지 않는다. 초기 journal은 DELETE, synchronous는 FULL, busy 대기는 유한하게 둔다. SQLite 연결은 요청 thread마다 소유하며 cross-thread connection을 공유하지 않는다.
- 중요 로그의 요약·rollover·정리 코드가 이 DB나 revision을 수정·삭제하지 않는다. 기존 DB schema와 metadata 장애 hold 정책을 변경하지 않는다.

### 내용과 조회 한도

| 항목 | s1 기술 기본값과 의미 |
|---|---|
| 제목 | 비어 있지 않은 문자열, 최대 256 Unicode code point. 저장 내용을 임의 정규화하지 않는다. |
| 본문 | 비어 있지 않은 UTF-8 문자열, 최대 64 KiB. 초과는 거절하고 저장 완료로 알리지 않는다. |
| 검색어 | 1~256 Unicode code point. 최신 revision의 제목·본문에 대한 대소문자를 구분하는 literal substring 검색. SQL wildcard·정규식 의미를 부여하지 않는다. |
| 일반 목록·검색·history | 기본 20건, 최대 100건. 최신 변경순의 안정적인 tie-break와 불투명 cursor를 사용한다. Cursor는 페이지 위치일 뿐 DB 경로·SQL이 아니다. |
| 새 세션 recent | 최신 변경 기록 최대 5건, 본문 미리보기 최대 160 Unicode code point/건, 직렬화된 전체 context 최대 16 KiB. 초과·생략 여부를 표시한다. |
| 본문 조회 | ID와 선택적 revision으로 전체 저장 본문을 반환한다. 미리보기의 잘림은 본문의 유실이 아니다. |
| Bridge 응답 | 직렬화된 응답은 기존 1 MiB frame 안에 들어가도록 900,000 bytes 이내로 확인한다. 본문을 몰래 잘라 성공 응답을 만들지 않는다. |

목록·검색은 기본적으로 기록당 최신 revision만 보여준다. `history`는 revision metadata 목록이며 지정한 본문은 `show`로 읽는다. Record ID·revision·Task 연결·기록 시각·미리보기·더 있는지와 다음 cursor를 반환한다. 정상 빈 결과와 저장소 접근 불가는 다른 결과다.

### Agent 도구

새 essential 도구 `workbench_records`를 Manager·Worker의 주 세션에 제공한다. 별도 shell·파일 경로 추측 없이 기록을 남기고 읽을 수 있어야 한다. 기존 `workbench_logs`·handoff·terminal 기능은 그대로 둔다.

Action은 `create`, `revise`, `list`, `search`, `show`, `history`, `recent`, `export`다. Strict object schema에서 action별로 쓰지 않는 nullable 필드는 null로 전달한다. 공유 계약에 `record_id`, `revision`, `expected_revision`, `title`, `body`, `task_id`, `query`, `limit`, `after`, `output_path`의 타입·필수 조합을 고정한다.

- `create`: 제목·본문·Task ID를 받는다. Host가 새 record ID와 시각·역할을 정하고 성공 시 ID·revision 1을 반환한다.
- `revise`: ID·expected_revision·전체 새 snapshot을 받는다. 최신본 확인 없이 덮어쓰는 patch·replace 동작은 없다.
- `show/history`: ID와 선택적 revision·pagination을 사용한다. 알려지지 않은 ID/revision은 `not_found`다.
- `list/search/recent`: 같은 프로젝트의 기록만 조회한다. 검색·본문·revision은 새 모델 호출을 시작하지 않는다.
- `export`: ID·선택적 revision·출력 경로를 받는다. 경로가 빠졌으면 `needs_output_path`로 사용자에게 경로를 확인하게 하고 파일을 만들지 않는다.
- Main Manager·Worker만 사용한다. 기존 `isSubagentContext`와 backend peer 결속을 유지하고 `BRIDGE_TOOL_NAMES`·Worker custom tool allowlist에 필요한 이름만 추가한다. 요약 SDK child의 DB-only 도구 정책과 역할·session/generation 검증은 확대하지 않는다.
- 사용자가 명시한 기록/수정/내보내기 의도와 기존 위임 범위 안에서 사용하도록 도구 설명·상시 prompt에 명시한다. 기록 내용에서 발견한 명령·승인 문구만으로 실행하거나 자동으로 export하지 않는다.

응답은 `saved`(ID·revision·시각), `ok`(조회 결과), `exported`(경로·ID·revision), `not_found`, `rejected`(인자·한도·revision conflict 등), `unavailable`(DB·파일 I/O 등), `needs_output_path`를 구분한다. Transport의 기존 `outcome_unknown`은 그대로 유지한다. 알려진 실패는 reason code를 제공하며 경로·본문·인증 내용을 진단 로그에 불필요하게 복사하지 않는다.

### 사용자 CLI와 export

```text
wb records list [--task TASK_ID] [--limit N] [--page CURSOR] [--json] [--data-dir DIR]
wb records search QUERY [--task TASK_ID] [--limit N] [--page CURSOR] [--json] [--data-dir DIR]
wb records show RECORD_ID [--revision N] [--json] [--data-dir DIR]
wb records history RECORD_ID [--limit N] [--page CURSOR] [--json] [--data-dir DIR]
wb records export RECORD_ID [--revision N] --output PATH [--json] [--data-dir DIR]
```

`omp-workbench` alias도 같은 parser를 사용한다. CLI는 입력 누락/잘못된 인자에 기존 argparse 오류를, 저장소·파일 오류에는 실패 exit를, 성공과 정상 빈 목록에는 성공 exit를 사용한다. JSON 결과는 도구와 같은 domain status를 담는다. 경로 미지정 export는 CLI usage 오류 또는 도구의 `needs_output_path`로 처리하며 자동 경로를 발명하지 않는다.

Markdown은 선택한 snapshot의 제목·record ID·revision·생성/해당 revision 저장 시각·Task ID(있을 때)와 저장 본문을 담는다. 본문을 해석·redact·줄여 재저장하지 않는다. DB는 읽기만 한다. Relative path는 agent 도구에서는 backend의 검증된 프로젝트 root, CLI에서는 호출 cwd를 기준으로 해석하고 실제 출력 경로를 결과에 표시한다.

부모 디렉터리는 존재해야 한다. Leaf 파일은 배타 생성하며 일반 기존 파일·symlink 모두 덮어쓰지 않고 `output_exists`를 반환한다. 새 파일은 0600으로 만들고 쓰기 완료를 확인한 뒤 성공을 반환한다. 실패한 부분 파일을 정리할 때는 이 호출이 만든 파일인지 확인한다. 정리/출력 결과가 불명확하면 그 사실과 경로를 알린다. DB를 fallback 파일이나 export 결과로 대체하지 않는다. 기존 문제 보고서 helper의 자동 대체 파일명 정책을 그대로 재사용하지 않는다.

### 새 세션 미리보기

공개 OMP `session_start/session_switch`에서 주 세션의 현재 session ID·generation·연결을 결속한다. Bridge 연결 후 `recent`를 한 번 조회하고, `pi.sendMessage`의 custom context message와 `triggerTurn:false` 또는 동등하게 검증된 공개 hook으로 제목·미리보기를 표시하고 다음 자연스러운 turn의 context에 제공한다.

- 이 조회·표시 때문에 모델 turn·Task·worker 보고·terminal 명령을 시작하지 않는다. Pause 중에도 새 자동 모델 작업을 시작하지 않는다.
- 동일 연결의 재접속만으로 미리보기를 반복 주입하지 않는다. Session switch 전의 지연 결과는 새 session에 넣지 않는다. Subagent에는 주 세션 preview를 보내지 않는다.
- 첫 사용자 turn과 startup 조회가 경합하면 공개 lifecycle hook의 유한한 대기로 context 준비를 조정한다. 실패하면 저장소 불가를 표시하고 일반 대화·기존 실행을 불필요하게 중단하지 않는다. `empty`와 `unavailable`을 구분한다.
- 실제 API·표시·provider context·첫 turn·switch 동작은 WR-01의 선행 runtime 판별 대상이다. 함수가 존재한다는 사실이나 mocked handler 성공만으로 이 경로를 통과 처리하지 않는다.

## 상태 전이·실패·보존

Create는 없는 기록에서 revision 1로, revise는 일치하는 최신 revision에서 다음 revision으로만 진행한다. DB commit이 확인된 뒤 `saved`를 반환한다. Write 실패·busy·잘못된 Task·revision conflict에서는 기존 정상 기록을 유지한다. Transport가 결과를 잃은 경우 `outcome_unknown`이며 자동 재전송하지 않는다. 사용자는 목록·조회로 현재 저장 상태를 확인한 뒤 다음 행동을 정한다. 새로운 영속 retry queue나 exactly-once 보장은 추가하지 않는다.

조회와 export는 DB를 수정하지 않는다. 잘못된 cursor·revision·한도는 명시적으로 거절한다. 파일 충돌과 DB 접근 불가는 빈 결과·저장 성공·export 성공으로 둔갑하지 않는다. 기록 DB 장애를 이유로 기존 메인 작업을 자동 중단하는 새 정책은 만들지 않으며, 기존 Task metadata 영속 장애의 hold는 그대로 적용한다.

## 수락 추적

PLAN의 `acceptance_trace`와 `gate_items`가 canonical 기계 추적이다. 아래는 사용자 동작을 읽기 위한 요약이다. Ticket의 완료 상태는 근거를 대신하지 않는다.

| BRIEF ID | 담당 ticket | 관측할 공개 동작 | 최종 결합 확인 |
|---|---|---|---|
| WR-AC-01 | WR-02, WR-05 | 도구 create → 실제 DB 재열기에서 같은 본문, 프로젝트 docs 파일 변화 없음 | WR-07 실제 두 역할 경로 |
| WR-AC-02 | WR-04, WR-06 | exporter·CLI·도구의 요청한 Markdown 출력 | WR-07 이동한 파일 단독 열람 |
| WR-AC-03 | WR-06 | 상시 지침의 일반 기록/정식 문서/문제 보고서 구분, 기존 자산 계약 유지 | WR-07 role prompt·기존 회귀 |
| WR-AC-04 | WR-05 | Manager create → Worker show, 반대 방향도 같은 ID | WR-07 process 재시작·프로젝트 분리 |
| WR-AC-05 | WR-05 | 기존 handoff/terminal/logs 요청·응답과 lifecycle 유지 | WR-07 기존 bridge·flow 회귀 |
| WR-AC-06 | WR-01, WR-06 | 실제 OMP startup/switch에서 preview 표시·다음 context·추가 turn 없음 | WR-07 실제 WB 연결 |
| WR-AC-07 | WR-02 | public revise·get/history에서 최신/과거 본문과 conflict 보존 | WR-07 다른 역할·재시작 조회 |
| WR-AC-08 | WR-02 | Task 없는 메모·연결된 Task, 저장 본문과 자동 요약의 독립성 | WR-07 중요 로그 입력 후 원본 대조 |
| WR-AC-09 | WR-03 | CLI·RecordStore의 literal 검색·목록·ID/revision 조회 | WR-07 같은 도구 경로 |
| WR-AC-10 | WR-04, WR-06 | 최신/지정 revision·경로 누락·충돌·출처 metadata·DB 원본 유지 | WR-07 CLI/도구 파일 내용 대조 |
| WR-AC-11 | WR-02 | 재열기·revision 추가 후 과거 보존, 삭제 surface 없음 | WR-07 로그 maintenance·중단 상태 이관 호환 |
| WR-AC-12 | WR-02~06 | 실제 module/CLI/bridge에 가한 저장·읽기·경로·transport 실패의 구분 | WR-07 기존 metadata hold와 분리 |

## 테스트 결정과 근거 수준

아래 seam과 expected value는 이 계획의 승인 대상이다. 구현 요청이 이 exact bundle을 승인하면 TDD에서 재질문하지 않는다. 지금은 테스트 코드를 작성·실행하지 않았다.

| Seam | 검증하는 것 / 검증하지 못하는 것 | 독립 expected value |
|---|---|---|
| RecordStore + 실제 임시 SQLite | 재열기, 본문·revision·검색·동시 수정·실패 보존 / OMP 전달·UI | 직접 적은 한글·Markdown·인용부호·`%`·`_` 본문과 명시한 revision/검색 결과 |
| export_record + 실제 임시 디렉터리 | snapshot·파일 충돌·기존 파일·DB 보존 / 모델의 경로 의도 해석 | 손으로 정한 최신/과거 본문, sentinel 파일 bytes, ID·시각·revision |
| `python -m workbench records ...` subprocess | 실제 parser·프로젝트 선택·exit·JSON·출력 / agent 도구 | 두 개의 임시 프로젝트와 미리 적은 fixture, 예상 파일·status |
| 실제 G3 UDS + extension 대체 API | schema·역할·session/generation·한 번 전송·본문 전달 / 실제 OMP hook·provider | 독립 요청/응답 fixture와 고정 canary |
| 실제 OMP + localhost scripted provider | 실제 도구 roster·session context·추가 turn 없음·실제 tool execute / 유료 모델의 자연어 판단 품질 | DB와 무관하게 만든 startup/switch canary 및 provider 요청 관측 |
| 실제 WB 설치본/자산 + 두 프로젝트 | 역할 공유·재시작·격리·배포 자산·로그/이관 호환 / 전 provider와 OS | 임시 프로젝트별 서로 다른 본문·ID, 기존 DB/로그 sentinel |

Worker는 각 ticket에서 한 동작의 의미 있는 RED → 최소 GREEN 뒤 다음 동작으로 진행한다. 실제 resolved TDD reference는 `.agents/skills/tdd/SKILL.md`이며 Root가 구현 때 파일 identity와 seam·허용 경로·명령을 assignment에 전달한다. Runtime feasibility probe·문서/자산 안내는 예외 사유와 대체 근거를 기록하며 가짜 RED를 만들지 않는다.

기존 회귀 seam은 `tests/backend/installation/`, `tests/backend/workbench_report/`, `tests/bridge/handoff_tools.test.ts`, `tests/backend/test_workbench_skills.py`, `tests/backend/test_paths_launcher.py`다. 기본 명령은 다음과 같다. 새 파일은 ticket에서 작성할 예정이며 현재 존재/통과를 뜻하지 않는다.

```text
PYTHONPATH=src python3 -m unittest discover -s tests/storage/work_records -p 'test_*.py'
PYTHONPATH=src python3 -m unittest discover -s tests/backend/work_records -p 'test_*.py'
node --experimental-strip-types --no-warnings --test tests/bridge/work_records.test.ts
PYTHONPATH=src python3 -m unittest tests.backend.test_paths_launcher tests.backend.test_workbench_skills
PYTHONPATH=src python3 -m unittest discover -s tests/backend/workbench_report -p 'test_*.py'
PYTHONPATH=src python3 -m unittest discover -s tests/backend/installation -p 'test_*.py'
WB_LIVE_RECORDS=1 PYTHONPATH=src python3 -m unittest tests.integration.live_workbench_records
WB_LIVE_RECORDS=1 PYTHONPATH=src python3 -m unittest tests.integration.live_workbench_records.InstalledAssetsTests
```

WR-01은 `WB_LIVE_RECORDS=1 PYTHONPATH=src python3 -m unittest tests.backend.work_records.live_session_context_probe`로 실행할 작은 실험을 작성한다. 지정된 기존 OMP binary·임시 HOME/DB·synthetic 자료·localhost scripted provider만 사용하고 사용자 인증·실제 DB·유료 모델·Docker·설치/업데이트를 사용하지 않는다. 연결·한 turn·switch를 유한하게 제한하고 pipes/PTY를 계속 비우며 소유 process만 정리한다. 종료 후 소유 process와 재생성 cache 정리를 확인하고 용량 규칙을 따른다.

자연어 요청을 항상 완벽히 분류한다는 모델 품질 보장은 추가하지 않는다. 일반 기록을 도구로 보내는 상시 지침·도구 schema가 실제 OMP에 전달되는지, 그 도구 실행이 파일 생성 대신 DB 저장으로 이어지는지를 각각 관측한다. Scripted provider 근거를 실제 모델의 의도 해석 품질 시험으로 표시하지 않는다.

Independent test_designer는 승인된 동작에서 먼저 시나리오를 도출하고 worker TDD의 범위·expected value를 독립 평가한다. 중간 검증 단위 U1=WR-01, U2=WR-02, U3=WR-03+04, U4=WR-05+06은 각각 fresh general review를 받는다. Unit 결합 전 선행 계약은 필요한 독립 검사·review 후 통합한다. 최종 U5=WR-07은 동일 frozen code+tests에 독립 Standards/Spec 두 fresh context와 최종 검증을 적용한다. 문서만으로 runtime gate를 통과하지 않는다.

새 SQL 입력·출력 경로·custom tool 노출은 security review trigger다. 구현 중 실제 변경 경계를 정리하고 최종에는 별도 `review_kind: security` reviewer와 diff secret scan을 수행한다. 새 dependency를 추가하지 않으므로 lockfile 변경이 없다면 dependency audit는 해당하지 않는다. 모든 검증은 사용자 저장소·인증·실행 세션과 분리한다.

## 호환·통합·전환

- 기존 설치·실행 CLI와 alias, 프로젝트 탐색·명시 data-dir·runtime 분리·역할별 자산을 재사용한다. 새 기능 때문에 프로젝트를 자동 초기화하거나 backend를 새로 실행하지 않는다.
- 새 DB는 기존 중요 로그 정리 대상에 넣지 않는다. 기존 작업·명세·로그·raw·OMP session의 bytes/정책을 변경하는 migration은 만들지 않는다.
- 기존 `migrate.py`에 신규 records DB와 journal의 알려진 분류·schema/논리 내용 검증·복사 보존만 확장한다. 기존 stopped-owner·원본 보존·대상 충돌 거절은 그대로다. Source에 record가 없던 경우의 이관도 유지한다. 이관 대상의 실제 project binding과 기록 조회를 검증한다. 사용자 상태를 실제로 이관하는 실행 권한은 없다.
- Raw rotation 활성화·기존 로그 삭제·프로그램 게시/배포·commit은 별도 권한이다. `.workflow/`·`graphify-out/`를 통째로 source/checkpoint에 넣지 않는다. 구현 후 code graph update를 수행한다.

## 미검증 선행조건과 위임된 기술 상세

미결정 제품 정책은 없다. 공개 OMP lifecycle의 startup/switch context 전달·추가 turn 없는 표시가 미검증이며 WR-01이 판별한다. 통과한 API 경로를 WR-05·06이 소비한다. 실패/불명이면 도구·새 세션 연결과 그 후속만 보류하며 저장/조회/export의 독립 준비는 계속할 수 있다. 공개 hook의 동등한 대안을 같은 작은 실험으로 비교할 수 있지만 preview 제거·새 자동 모델 호출·사용자 workflow 변경이 필요하면 Root에 근거와 함께 제품 결정으로 돌린다.

구체 SQL index·내부 helper·cursor encoding·짧은 busy timeout·정확한 파일 배치와 dispatch scope는 의미를 보존하는 구현 상세다. Worker가 supported 본문 크기·revision 의미·보존·실패·Task 경계·역할 권한을 바꿔서는 안 된다. 실제 unavailable service나 도구는 pending/not_run으로 기록하고 통과로 가정하지 않는다.

## Ticket graph

[PLAN.json](PLAN.json) p1이 의존성·resource·write ownership·gate supplier의 정본이다. 초기 독립 frontier는 WR-01과 WR-02다. WR-02 통합 뒤 WR-03과 WR-04가 frozen get/revision 계약을 독립적으로 소비한다. WR-05는 WR-01·03·04를 결합하고 WR-06→WR-07로 이어진다. 격리가 검증되지 않으면 동시에 쓰지 않고 순차 진행한다. 이 문서의 경로는 dispatch 전 재확인할 제안이다.
