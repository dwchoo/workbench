# 기존 subagent 기반 중요 로그 — 개발 보완 명세

Revision: sls-2, 2026-10-09. 상태: **Claude 리뷰 반영 개발 초안·bundle 미승인·미구현**.
기준 HEAD: `74d5e0301c03820aed7262d833e248b1c784d0f6`.
상위 기준: [BRIEF r0.9](BRIEF.md), [SPEC s5](SPEC.md), [PLAN p5](PLAN.json).
승인 bundle digest: `78bc55919e57bbf2132d6653780c5c164a2ee1d618422e5232d4cf0d1d667c3e`.
이 문서는 [전략 proposal-1](SUBAGENT-LOGGING-STRATEGY.md)과 Claude 피드백
`subagent-logging-strategy-feedback-01`을 개발 가능한 경계로 구체화한 **별도 보완 초안**이다.
기존 SPEC·PLAN·ticket·동결 판정 도구를 변경하지 않는다. 본문에서 제안한 wire 이름·route·gate 대응도 아직 채택되지 않았다.
이번 사용자 요청은 문서 저장·Claude 리뷰·보고 및 회신 뒤 “좋아 이렇게 하자.”에 따른 문서 보완이다.
이는 해당 기술 보완을 문서에 반영하는 근거이며 정확한 변경 bundle 승인이나 route 채택은 아니다.
제품 구현, probe, paid model, raw 삭제 활성화, commit을 시작하지 않는다.

## 1. 목적과 가장 작은 구성

`Backend가 자료와 DB 상태를 준비 → 기존 OMP 안의 제한된 subagent + 내부 skill이 요약 → Backend가 검증·SQLite 저장`

Agent는 중요한 내용을 고르는 역할만 한다. 실행 시점·권한·pause·저장·정리·rotation·DB 전환은 Python Backend가 책임진다.
별도 logger agent 정의, 상주 OMP, 독립 Python 모델 클라이언트, 별도 OS 요약 프로세스는 추가하지 않는다.
부모 모델이 매번 task 위임을 결정하는 turn을 만들지 않는다. 수동으로 “기록해”라고 지시하는 사용성과 정기 자동 요약은 별개다.
요약 연결 부분의 신규 구성은 전용 bridge 처리기, 내부 skill, Backend 검증·저장 연결이다.
이는 DB 유지보수·raw reader·scheduler·제품 admission 등의 나머지 ticket이 필요 없다는 뜻은 아니다.

### 1.1 사실·제안·미검증의 구분

| 구분 | 현재 내용 |
| --- | --- |
| 승인된 동작 | 기존 agent/skill·Backend 저장, 주기/권한/pause/no replay, raw·DB 보관 정책. 상위 문서가 우선 |
| 코드에서 확인 | DVL-03 JSON v1과 별도 store 기반 존재. bridge의 notice는 `sendUserMessage`로 부모 turn을 만듦. 요약 전용 frame 처리기는 없음 |
| 이전 관측 | runtime08의 tested `runSubprocess` 경로는 missing/inert guard에서도 POST가 발생. 모든 공개 API의 불가능을 입증하지 않음 |
| 우선 기술 후보 | 기존 OMP extension에서 caller-owned `createAgentSession`으로 요청별 in-memory child를 준비하고 확인 뒤 prompt 호출 |
| 미검증 | 실제 도구 제한/전송 장벽, typed 회수, model/thinking/tier, skill 전달, 부모와의 간섭, origin·priority·cleanup |

## 2. 구현 책임과 경계

| 경계 | 구현 책임 | 금지 |
| --- | --- | --- |
| Backend scheduler/adapter | 새 정보·승인·pause·우선순위 대조, 범위/health 준비, 요청 journal, 결과 validation/commit, 장애 상태 | 직접 모델 호출, 모델에게 삭제/정리 판단 위임 |
| 기존 OMP bridge | 전용 frame 검증, 기존 role 해석, child 소유/guard/preflight/skill 전달, typed 회수, usage·cleanup | notice/deliver/sendUserMessage 재사용, 부모 transcript에 원문 삽입, child의 Workbench bridge 재연결 |
| 제한된 child + skill | 제공 자료에서 facts/interpretations/unknowns·누적 요약 생성 | SQL·파일 저장, shell, 임의 read/grep/glob/web_search, 인증 조회, 새 Task 생성 |
| SQLite store/운영 metadata | 실제 저장·health·보존/전환·중복/no replay 근거 | 모델 자기보고를 저장 성공·실행 성공으로 취급 |

운영 요청은 기존 실행 중 OMP 하나에만 배정한다. **최소 probe는 Worker와 기존 허용 explorer를 우선 후보로 한다**.
이는 제품 route 채택이 아니다. 최종 host role/session 선택은 Q01 결과에 기록하고 Manager/Worker 간 자동 fallback은 두지 않는다.
Host OMP가 없거나 사용자 직접 입력·승인 UI·composer 활성·기존 종료 처리/60초 점검이 진행 중이면 신규 전송을 보류한다.
Host의 일반 turn이 진행 중이라는 이유만으로는 보류하지 않는다. 부모 turn과 child의 실제 간섭은 Q01-priority에서 측정하고,
공유 경로 차단이 확인되면 관측값을 Q-S2-03으로 보고한다. 다른 OMP로 재전송하지 않는다.
Child는 Workbench bridge를 로드하거나 `hello`를 보내지 않는다. 부모 connection의 role/generation을 대체하면 실패다.
요약 전용 handler와 pause/resume/종료 handler의 실행·정리 책임을 DVL-09에서 연결한다.

## 3. 전용 제어 채널: 신규 구현 제안

현재 존재하지 않는 요약 control plane을 기존 Backend↔bridge socket 위에 추가한다.
다음 frame 이름은 가칭이다. 실제 버전/필드와 양언어 fixture는 DVL-03 계약을 기준으로 확정하며 일반 frame parser를 우회하지 않는다.

| 방향·가칭 | 의미 | 완료라고 뜻하지 않는 것 |
| --- | --- | --- |
| Backend→bridge `summary_run` | 검증된 SummaryRequest + 제한된 material 전달 | 모델 전송/실행/저장 완료 |
| bridge→Backend `summary_state` | 인증된 연결에서 request/host session/child epoch에 귀속된 not_sent·sending·executed 관측 및 사유 | hook 등록·API 수락만으로 실행 확정 |
| bridge→Backend `summary_result` | typed SummaryResult + 신뢰 가능한 전송/usage provenance | SQLite 저장 성공 |
| Backend→bridge `summary_cancel` 또는 기존 pause 연결 | 해당 child abort/dispose, 신규 전송 금지 | 원래 요청이 미실행이었다는 증명 |

Transport의 연결 generation, request ID와 child/session epoch는 adapter가 관리한다.
Model result 안의 임의 필드로 연결 신뢰를 대체하지 않는다. Frame ack와 durable 저장 receipt를 구분한다.
모든 frame은 크기 상한·버전·허용 role·현재 connection generation으로 발신 연결을 인증한다.
폐기된 연결에서 온 frame은 거부한다. 다만 요청의 시작 generation과 결과를 전달하는 현재 generation은 별개다.
같은 host OMP session/incarnation이 재연결해 이전 generation에서 시작한 child 결과를 현재 인증된 연결로 전달하면,
durable journal의 requestId/host session/child epoch/inputKey를 대조해 유효한 late result로 받을 수 있다.
연결 재수립은 새로운 모델 호출·요청 재생을 허가하지 않는다. Host 재시작은 이전 in-memory child를 잃으므로 미확정 요청은 unknown으로 둔다.
이전 host identity/잘못된 요청 결속·중복을 거부하고 이유를 기록한다. 전체 원문을 operational journal에 복제하지 않는다.

### 3.1 기존 계약을 그대로 재사용할 부분

Python `src/workbench/contracts/logs_v1.py`와 TS `omp_bridge/contract/logs_v1.ts`가 기준이다.

- SummaryRequest: `requestId`, `subject`, `revision`, `authorizationRef/Epoch`, `inputKey`,
  `skillName/Digest`, `sources`, `sourceGaps`, `priorCumulativeEntryId`, `dbHealth`, `requestedAt`, `schemaVersion`.
- SummaryResult: `requestId`, `inputKey`, `skillDigest`, `sources`, `facts`, `interpretations`, `unknowns`, `usage`, `schemaVersion`.
- 현재 Result에는 subject/revision/nonce를 독립 필드로 추가하지 않는다. Request ID→영속 Request의 대조와 inputKey/range 일치로 묶는다.
  Probe nonce/GuardReceipt는 별도 adapter evidence이며 현재 Result schema가 이미 이를 받는다고 쓰지 않는다.
- inputKey는 subject/revision/source ranges+digests/gaps/skill digest에 대한 기존 계산을 사용한다.
  skill 변경·새 ID로 이전 executed/unknown 범위의 no replay를 우회하지 않는다.
- Strict parser의 unknown field/중복 key/잘못된 UTF-8/크기 거부를 유지하고 free text heuristic 추출은 금지한다.

기존 Python SummaryAdapter Protocol은 동기 `dispatch_summary(request) -> SummaryResult`, TS는 단일 Promise 반환이다.
이를 그대로 사용해 상태 event/cancel/unknown/late까지 표현한다고 가정하지 않는다.
제품 연결에는 별도의 비동기·상태 보고형 port 계약이 필요하다: submit(request, material), 상태/결과 event 구독,
cancel(requestId, childEpoch), 소유 context 정리의 의미를 함께 정의한다. 메서드명은 아직 가칭이다.
공통 port/interface의 버전 추가·변경, Python/TS fixture 및 scheduler/bridge 소비자 재검증 범위를 bundle 수정 때 기록한다.
현재 v1 Request/Result 데이터 필드는 유지하며 동기 port를 제자리에서 몰래 바꾸지 않는다.
Port/material 변경이 동결 judge seam에 영향을 주면08 bounded follow-up의 독립 재동결 후09 소비자를 연결한다.

SummaryRequest는 참조 계약이며 원문 body 필드가 없다. 실제 material 전달은 별도 제한된 transport payload가 필요하다.
그 payload는 Request와 같은 sources/digests에 연결된 원문 조각, 시작/명세·이전 cumulative의 필요한 본문,
omission/gap과 health 해석에 필요한 수치만 담는다. Child에게 경로를 주고 임의로 읽게 하지 않는다.
prior entry는 생성 시점의 ID·본문 digest를 고정하고, 요청 동안 바뀐 최신 요약으로 몰래 교체하지 않는다.
본문·고정 instructions·skill·schema를 포함한 모델 전송량은 §6 preflight를 만족해야 한다.
신규 material envelope의 canonical digest/fixture는 DVL-09 착수 전 계약 대조에서 확정한다. 참조 계약 변경이 필요하면 버전/동결/소비자 재검증을 기록한다.

Terminal source offset·range digest는 원본 byte를 기준으로 유지한다. 모델에 전달할 text만 결정적으로 UTF-8 decode하며
유효하지 않은 byte sequence는 U+FFFD replacement로 치환하고 각 fragment에 lossy decoding 여부를 표시한다.
원본 digest를 치환 text digest로 바꾸거나 source offset을 text 문자 위치로 다시 계산하지 않는다.
Lossy decoding은 표현 변환이며 collector/storage gap과 구분한다. 중요한 byte 정보가 복원됐다고 주장하지 않는다.
1MiB 수집 cap은 원본 후보 bytes,32KiB body cap은 치환·escaping·구조화 후 실제 UTF-8 전송 body에 적용한다.
변환으로 커지면 범위 단위로 input selection을 줄이고 omission을 표시한다. 임의 raw byte를 strict JSON 문자열에 넣지 않는다.
Raw digest/offset·lossy flag·전송 크기를 대조하는 양언어 fixture를 계약 확인에 포함한다.

## 4. Child 생성·기존 agent 재사용

`createAgentSession`은 AgentDefinition을 그대로 받는 조합이라고 가정하지 않는다.
Workbench가 기존 definition의 system prompt 원문 bytes, @role model 선택, thinking, service tier와 skill 전달을 연결해야 한다.
새 role/default를 몰래 만들거나 해석 실패를 임의 모델로 대체하지 않는다.
현재 explorer의 `@smol`과 model config는 초기 출처일 뿐, child가 실제 쓸 model/thinking/tier는 생성 후 전송 전에 read-back한다.
실제 fast tier 적용 여부도 측정한다. Task subagent 전용 tier override가 직접 생성 session에도 적용된다고 추정하지 않는다.
Metadata 관측은 로컬 공개 API/기보유 검증 정보로만 하며 네트워크0·auth store open0·설정 쓰기0를 확인한다.
기존 운영 OMP의 정상 모델 호출 인증을 금지하거나 다른 프로세스에 복사하는 설계가 아니다.

요청 하나가 새 in-memory child 하나를 소유한다. 부모의 일반 도구/모델/registry/설정을 변경하지 않는다.
Ambient extension/MCP/IRC/자동 discovery·의도하지 않은 context/session persistence를 차단할 공개 옵션을 확인한다.
Process-wide binding과 child lifecycle을 공개 API로 분리할 수 있는지도 관측한다. 격리 session은 OS 보안 sandbox가 아니다.
Session 준비가 자체 모델 전송을 일으키면 전송 전 guard/preflight를 만족하지 못한 것으로 판정한다.

### 4.1 프롬프트 적합성: probe 이후 판단

Explorer는 프로젝트 탐색·path:line·read/grep/glob/web_search 사용을 지시한다. yield-only 요약 구성과 모순이 남는다.
Skill로 “이번 입력만 요약”을 전달해도 실모델이 불필요한 도구를 요청하거나 결과를 거부할 위험이 있다.
Scripted provider는 이 프롬프트 적합성·품질·비용을 입증하지 않는다.

최소 barrier probe는 승인 해석에 가까운 **explorer 원문 + 내부 skill**을 사용한다.
OMP 기본 prompt + 내부 skill + 기존 smol role 방식은 더 명료할 수 있지만 “기존 agent 활용”의 해석이 달라져 아직 채택하지 않는다.
Probe 후 DVL-09 전에 실제 차이·위험·사용자 pin을 보여 주고 필요한 경우 사용자에게 선택을 요청한다.

## 5. 결과 채널과 도구 차단

### 5.1 결과 제출은 저장이 아니다

우선 `outputSchema`/`requireYieldTool`과 공개 event/session API로 typed 결과를 회수한다.
모델 출력 schema와 wire의 SummaryResult를 구분한다. 모델이 생성하는 객체에는 facts/interpretations/unknowns와 인용 sources만 둔다.
Request ID/inputKey/skillDigest/usage는 모델에게 생성시키지 않는다. Bridge/adapter가 원 Request와 신뢰 가능한 OMP usage로
SummaryResult envelope을 조립한다. Tokens/cost를 얻지 못하면 기존 schema의 null(unknown)을 사용하며 모델 자기보고로 채우지 않는다.
Backend는 모델 sources의 각 source identity/start/end/digest가 원 Request sources에 포함되는지 검사한다.
기본은 Request에 있는 전체 SourceRange와 정확히 일치하는 항목만 인용하며 새/더 넓은/변조 범위·허위 digest는 거부한다.
부분 byte-range 인용이 필요하면 실제 material에서 digest를 검증하는 별도 계약 없이는 허용하지 않는다.
현재 parser는 형식 검증만 하므로 위 귀속 검사는 명시적인 adapter/Backend cross-check다.
8KiB 한도는 최종 envelope 전체다. 요청별 고정 identity·선정 sources·usage 최대 표현·JSON overhead를 먼저 예약하고
남은 visible byte 예산을 모델 출력에 전달한다. Envelope 자체가 안 맞으면 전송 전에 차단한다.
Executor의 private helper가 자동 retry/final yield를 제공하는 것과 공개 session에서 동일 결과를 회수할 수 있는 것은 다른 사실이다.
Typed 회수가 이번 최소 probe의 가장 큰 미확인이다. 결과를 얻으려고 부모 모델 turn·자유 텍스트 파싱·강제 재호출을 추가하지 않는다.

Yield 회수가 안 될 때만 **저장하지 않는 caller-owned in-memory result capture tool**을 한 번 검토한다.
가칭 `submit_summary_result`는 해당 request의 크기 제한 typed 객체를 메모리에 전달하고, DB/파일/Backend RPC/네트워크에 쓰지 않는다.
현재 allowlist와 custom restricted tool 효력·중복/늦은 제출을 새로 검증해야 하며 yield route의 부분 pass를 섞지 않는다.
직접 DB 저장 도구 `save_important_log`, 일반 write/shell 권한형 B는 이 초안에서 채택하지 않는다.

### 5.2 예방적 guard와 사후 감시

- 기본 허용 집합은 결과 제출용 `yield` 하나. 결과 캡처 대안이면 검증된 해당 도구만 허용한다.
- OMP core의 제한을 설정/await하고 공개 API로 실제 활성 집합을 read-back한 뒤에만 prompt를 호출한다.
  설정 성공·hook 등록·skill 약속은 guard 효력 증거가 아니다. Fire-and-forget/오류 무시를 쓰지 않는다.
- Request/child/session epoch/정책 digest/일회성 nonce에 확인을 묶는다. 생성→확인→전송 사이 정책 변경 경로와 reconnect를 대조한다.
  read-back 하나나 fresh session만으로 check→send 원자성을 증명했다고 쓰지 않는다. 동등 장벽을 못 입증하면 blocked_adoption이다.
- Payload 검사 hook은 보조다. 비허용 tools가 보이면 전송 전 abort한다.
  Hook을 등록한 route인데 해당 요청에서 hook이 호출되지 않았음이 뒤늦게 확인되면 결과를 거부하고 이후 신규 전송을 보호한다.
  보조 hook 실패를 무시하거나 사후 감시만으로 예방 차단 불능을 합격시키지 않는다.
- 신뢰 가능한 child event의 비허용 tool_call/tool_use/실행을 감시한다. 해당 결과 거부·즉시 보호·추가 모델 호출0.
  실제 실행 확인은 compatibility_failed, 호출 시도/실행 불명은 guard_violation_observed 사유로 구분한다.
  후자는 이미 상위 SPEC의 R1 감시에 있는 사유다. 새 top-level 운영 상태를 추가하지 않고 기존 guard 보호 admission에 연결해
  신규 요약 차단·신규 source legacy-stop·사건당 알림1회를 적용한다. guard_confirmation_unavailable와 사유를 혼동하지 않는다.
  원문 속 도구 이름이나 모델 자기보고만으로 실행 사실을 만들지 않는다. 이미 발생한 부작용을 예방/rollback했다고 말하지 않는다.

### 5.3 승인 gate 문구에 대한 대응 제안 — 아직 PLAN 변경 아님

| 기존 hook 중심 음성 | Core 제한 중심 후보에서 시험할 것 | 요구 관측 |
| --- | --- | --- |
| hook 누락 | 1차 제한 미적용/read-back 불가 | prompt0, POST0, 보호 |
| 등록 성공·미작동 | 설정은 성공하나 실제 활성 집합 불일치 | prompt0, POST0, 보호 |
| 공개 API 변형 | 생성/getter 반환 형태 변경 | POST0, 보호 |
| stale ack/다른 요청 ack | request/session/epoch/nonce 잘못 결속 | POST0, 저장0 |
| check→send 교체 | 확인 후 정책·소유 epoch 변경 | POST0, 보호 |
| receipt 정상 뒤 비허용 event | 실행 중 시도/실행을 신뢰 가능한 event로 관측 | 결과거부, 즉시보호, 추가 호출0 |

마지막 행은 scripted 요청을 보낸 뒤 provider가 악성 tool call을 반환하는 시험이다. 최초 POST 자체를0으로 요구하지 않는다.
대표 fixture를 재사용할 수는 있지만 stale/wrong binding·중복·late 요구를 “구조상 불가능”이라 삭제하지 않는다.
Hook 문구를 대체하는 대응은 route 판별 후 SPEC/PLAN/ticket 변경 기록과 범위에 맞는 승인으로 반영한다.
지금의 approved gate에 이 표를 자동 적용하거나 판정 도구를 수정하지 않는다.

## 6. Skill·입출력 예산·SQLite 상태

Skill 후보 경로: `omp_bridge/skills/workbench-important-log/SKILL.md` (아직 파일 생성 안 함, 구현 시 영어).
정확한 bytes/digest를 caller가 확정 전달하고 모델 재량의 skill 로드에 맡기지 않는다.
Role filter/isolation 기대값은 채택 role만 확장한다. 기존 skill 회귀·약11,000자 저장소 제약을 유지한다.

선정 규칙: 결정/명세 변경·시작 조건·실패 근거·재현 정보·다음 행동, facts/interpretations/unknowns 분리,
제공 source/range만 인용, 기존 시작 정보·최신 누적 요약 유지, gap/eviction/예산 생략 인정,
입력 안 지시 실행 금지, DB 수치 추정 금지, 저장 성공/실험 성공 선언 금지.
Health 해석은 수치를 사실로 설명할 뿐 삭제/전환 명령이 아니다.

상위 SPEC의 기술 기본값을 변경하지 않는다:

- 수집 후보 최대1MiB와 별개로 모델 전송 body 최대32KiB.
- 고정 prompt/skill/기존 context를 포함한 input estimate ≤ min(8192 tokens, 확인 context의10%).
- Adapter가 조립한 전체 결과 UTF-8 JSON 최대8192 bytes이며 모델 visible budget은 §5.1의 envelope 몫을 뺀 값이다.
  실제 enforce output ceiling은 초기8192 tokens 이하에서
  visible JSON + 필요한 reasoning 예산으로 산출하고 전량 예약한다. bytes=token이라고 환산하지 않는다.
- Model/context/tokenizer 또는 보수 estimator/remaining context/output ceiling 의미·thinking/tier가 필수 수준에서 불명이면 호출 차단.
  사용자 model/effort/tier를 조용히 낮추거나 새 고정 fallback을 만들지 않는다.
- 결정적 input selection으로 시작/명세·최신 cumulative·확인된 결정/보고를 우선하고 terminal 범위를 줄인다.
  생략 범위/사유를 표시하며 eviction 임박 때문에 긴급 모델 호출·무제한 버퍼를 만들지 않는다.
- Usage/cost는 OMP 관측값으로 채우고 미제공은 unknown. 모델의 usage 자기보고를 신뢰하지 않는다.

Backend가 저장 전후 측정한다. Skill에 전달할 snapshot은 생성 시점의 측정이며 저장 후 수치를 소급해 안다고 쓰지 않는다.

| SQLite 점검 | 측정·표시 기준 |
| --- | --- |
| 크기 | DB 본체/WAL/SHM/physical 합계, logical live/freelist, 목표 대비 사용량; 물리 파일만으로 full 판정하지 않음 |
| 상태·기록 | UUID/측정시각/stale, Task/command별 entry 수·저장량, 정리 후보, 마지막 write/lock/maintenance/quick_check |
| 자원 | disk free, 저장/lock 경합/손상/권한 오류; 미확인을 정상이라고 표시하지 않음 |
| 전체 store | 활성/보존 DB 수·전체 물리량·정리 필요 상태. 기존 HealthSnapshot에 없는 집계는 별도 Backend status로 표시 |
| 별도 metadata | proof/request journal/운영 DB 사용량은250MB·120일 정책 밖임을 명시, 자동 정리 추가 없음 |

현재 HealthSnapshot v1은 기존 필드 그대로 사용한다. 복수 DB/catalog aggregate를 몰래 새 필드로 넣지 않는다.
가벼운 pre/post 측정과 증분/cached 통계를 사용하며 매 요약 full scan/quick_check/VACUUM을 하지 않는다.
Quick_check/유지보수 주기·100rows/256pages 등은 기존 SPEC §7을 따른다. 읽기 전용 조회는 모델/삭제/유지보수를 시작하지 않는다.

## 7. 호출·pause·저장 실패와 no replay

Backend 한 scheduler, 한 요청 in-flight, oldest-due, candidate page 최대32.
Source byte/event high-water의 새 정보만 dirty이며 첫 dirty 이후 최소300초 + 허용 종료 event를 coalesce한다.
Health 값만 변했다고 모델을 호출하지 않는다. Task 있는 command는 Task에만 포함하고 중복 요약하지 않는다.
우선순위는 사용자/보호 → 기존 종료·60초 점검 → 요약/유지보수다. §2의 사용자·보호/점검 조건에서만 신규 전송을 양보하고,
일반 host busy를 일괄 차단 조건으로 쓰지 않는다. Child 실행 중 실제 경합도 측정하며 영구적인 busy starvation을 숨기지 않는다.

Task는 유효한 승인·활성 또는 허용 종료 후처리 참조 + 현재 unpaused + 미철회가 필요하다.
Task 없는 command는 직접 사용자 실행 지시의 provenance가 command 하나에 연결되어야 한다. unpaused·무관 Task 승인만으로 허용하지 않는다.
done/report_*는 조건부 종료1회, cancelled/approval_retired/not_started_before_restart는 신규 종료 호출 없음.
finished/blocked/held·run 종료·사용자 archival-close는 Task 종료 trigger가 아니다. 상세 표는 상위 SPEC §8.3을 따른다.

Journal 상태는 상위 `prepared → sending → executed_observed → stored` 및 확정 not_sent/unknown/gap 규칙을 재사용한다.
전송 전에 durable reserve/전송 상태를 남길 수 없으면 보내지 않는다. API 수락만으로 executed나 stored로 올리지 않는다.
Prompt를 caller가 호출하지 않았고 session 준비 과정에도 자동 provider 전송이 없음을 확인한 경우에만 확정 not_sent의 근거로 쓴다.
실행 관측은 request/child에 결속된 공개 after_provider_response 또는 실제 assistant message_start를 후보로 확인한다.
합성·로컬 message_start/provider error를 실행 증거로 오인하지 않고, 실제 모델 응답에 대응하는 event인지 probe에서 검증·기록한다.
이 이벤트의 공개 가용성/의미는 아직 미검증이다. Prompt는 호출했지만 실제 응답 관측을 확정할 수 없으면 unknown이다.
POST 관측은 시험 provider가 전송을 확인하는 별도 근거이며 모델 처리/결과 정상/DB 저장 완료의 동의어가 아니다.
Bridge disconnect/host restart/pause abort로 전송 여부를 확정할 수 없으면 unknown이다.
미실행이 확정된 입력만 이후 새 정보와 병합한다. executed/unknown/결과거부/저장 실패 범위는 새 ID로도 자동 재호출하지 않는다.

Pause는 신규 전송을 즉시 막고 소유 child의 abort/dispose를 처리한다. **모든 pause 결과를 일괄 unknown/폐기하지 않는다**.
Pause 전에 이미 보낸 유효한 late result는 출처·범위·중복·저장 안전 검증 후 저장할 수 있다.
첫 유효 결과 하나만 받고 이후 중복을 기록한다. 이 저장은 자동화 재개/새 모델 호출이 아니다.

Bridge/adapter가 §5.1에 따라 조립한 strict Result를 Backend가 원 Request의 ID/inputKey/skill 및 인용 sources의 부분집합과 대조한 뒤 ImportantEntry를 만든다.
Usage의 관측 provenance도 검증하며 모델의 identity/usage 자기보고를 사용하지 않는다.
중요 DB transaction commit 성공 전에는 저장 완료로 보고하지 않는다. 이전 정상 cumulative는 실패 결과로 덮어쓰지 않는다.
중요 DB와 운영 journal은 별개 DB이므로 단일 SQLite transaction이라고 주장하지 않는다.
DB commit 뒤 journal 반영 전 crash는 안정 entry ID/inputKey/receipt로 실제 저장을 대조한다.
entry 중복 생성이나 모델 재호출 없이 reconciled stored/unknown을 판정하고 journal이 불능이면 신규 전송을 보호한다.
모델 Result 전달 성공·in-memory capture 성공은 DB commit 성공이 아니다.

정상250MB 용량 압력은 기존 정리→새 UUID DB 전환으로 해결한다. 디스크/권한/손상으로 안전한 저장·전환이 불가능할 때만 storage 장애로 신규 호출을 보류한다.
이미 실행 후 저장 실패한 범위는 no-replay gap이며 복구 뒤 재실행하지 않는다. 가능한 실험·수집·기존 정상 요약은 유지한다.
연속 계약 거부3회 보호/명시 scripted verify 뒤 복구 시도 중 거부1회 재보호 규칙도 상위 SPEC을 유지한다.

## 8. 기존 보존·업데이트 정책: 변경 없음

Raw 기본은 기존 legacy-stop. 최초 Q01/필수 metadata/명시 raw 활성화 후 **신규 source**만100MB×총5파일(활성 포함) rotation.
조건 충족 proof는 raw 삭제·중요 DB 정리와 별개 운영 metadata에 보존한다. 요약으로 성공 조건을 대신하지 않는다.
중요 DB는 논리 live 목표250,000,000 bytes/정리 low-water225,000,000 bytes, 종결 후120일 및 용량 압력 정리.
시작 정보·최신 cumulative를 우선 보호하고 오래된 중간 요약 정리 뒤에도 가득 차면 새 DB로 전환한다.
이전 DB는 자동 삭제하지 않는다. 실제 DB가1개가 될 때까지 복수 DB 수·전체량·사용자 정리 필요 표시를 유지한다.
보존 DB 명시 CLI 삭제·활성 DB 삭제 거부·UUID/sidecar/reader/crash 안전 경계는 기존 SPEC §7 그대로다.

OMP version-only 변경이면 기존 채택 경로는 요청별 guard/preflight 아래 실행을 유지한다(DV-D03).
미검증 새 버전을 pass로 기록하거나 매번 verify banner/자동 verify를 만들지 않는다.
실제 안전/호환 경계 불능·관련 closure/skill/route 변경이면 보호·사건당 안내1회·수동 verify.
신규 source만 legacy-stop, 진행 중 cohort 유지(LG-D13). Model delay/provider 일시 오류/eviction을 곧바로 비호환으로 분류하지 않는다.
canonical policy bytes/digest는 기존 동결본을 재사용하고, 제품은 tools/tests/.workflow를 runtime import하지 않는다.

## 9. 최소 기술 확인과 구현 순서 — 실행은 별도

### 9.1 DVL-08의 bounded feasibility probe 계획

실제 설치 OMP + isolated scripted local provider, private home/project/data/socket, 사용자 auth/log 접근0·paid model0.
Probe는 위 route 하나를 판별하는 작은 묶음이다. 결과 캡처 보정이 필요하면 한 번의 별도 변경/증거로 기록한다.
반복해서 같은 결론만 확인하는 runSubprocess/hook-only 실험을 추가하지 않는다.
Pause/disconnect/late는 DVL-08 collector의 시험 전용 control·local 포트로 유발한다.
실제 제품 summary frame·비동기 port는09에서 구현한다.08 최소 probe가 후행 제품 frame 구현을 선행 조건으로 요구하지 않는다.

| ID | 시나리오 | 최소 판정 근거 |
| --- | --- | --- |
| SL-P01 | 정상1요청 | 실제 POST1, payload tools=[yield], 모델 내용→adapter envelope/request 결속, 실제 response/실행 event 출처, skill bytes, resolved model/thinking/tier local 관측 |
| SL-P02 | read-back 불가/불일치·API shape 변형·read-back 정상 뒤 payload tools 변조 | 사전 차단은 prompt0/POST0, 보호. Payload 변조는 prompt 뒤 hook abort/POST0. 서로 다른 실패 이유를 fixture로 분리 |
| SL-P03 | stale/다른 request receipt·check-send 정책 교체 | provider 전송0·거부. 변경 경로 없는 항목도 구조 근거+대표 잘못 결속 fixture 필요 |
| SL-P04 | scripted provider가 read/web_search/write/bash 요청 | 도구 실행0/ToolNotFound 등 실제 관측, 결과거부·보호, sentinel 불변, 추가 모델 호출0 |
| SL-P05 | 실행 중 pause·disconnect·late | 신규 전송0, 소유 child 정리, prompt/response 증거별 not_sent/executed/unknown, 같은 host 재연결 뒤 valid late 첫 결과 수용·host restart unknown/no replay 경계 관측 |
| SL-P06 | 공통 isolation/cleanup | 부모 turn/transcript/일반 설정 변화0, child bridge hello0, 소유 자원 잔존0, 실제 사용자 home/auth/log 접근0 |

SL-P04는 POST 후 응답 음성으로 최초 POST0 조건과 구분한다. 종료/abort 뒤 자동 모델 retry가 생기면 실패다.
최소 probe에서 DB 저장의 후속 제품 동작을 요구하지 않는다. Late result 저장 가능성은 local 계약/adapter 포트로 확인하고 실제 DB 통합은 후속 ticket에 둔다.
중단: 예방 guard 음성 POST발생, 허용 밖 tools 광고/실행, sentinel 변경, 부모 상태/bridge connection 변경,
cleanup 실패, 실제 home/auth/log 접근. 관측 부족/probe 자체 timeout은 inconclusive이며 원인을 OMP로 단정하지 않는다.

통과는 **최소 호출 구조 타당성**만 뜻한다. 같은 route에서 Q01-route/tools/skill/result/context/priority/recovery/origin 8개를 전부 다시 판정한다.
Fixture 또는 정상1회는 priority/context3회/recovery/origin/실모델 예산의 대체 근거가 아니다.
새 route 결과를 이전 runSubprocess partial pass와 섞지 않는다. 동결 judge 변경이 필요하면08 bounded follow-up·독립 재동결 후 판정한다.

### 9.2 제품 연결 경계와 owner

| 단계 | 기존 ticket | 완료 기준/확대 경계 |
| --- | --- | --- |
| 최소 확인·route/8개 판정 | DVL-08 | 위 관측·제한 기록. 필요한 prompt 선택/gate 수정 승인 후09 착수 |
| 전용 frame·child·skill·typed adapter | DVL-09 | 양언어 계약 대조, parent turn0, guard/usage/cleanup. frozen judge 쓰기 없음 |
| 허용 scheduler·journal·결과/저장 연결 | DVL-10 | actual DB commit/중복·cross-DB crash 대조·pause/no replay; 기존 local 포트 경계 유지 |
| 제품 admission/verify·보호 연결 | DVL-14 | 관련 closure/policy 및 실제 adapter로 재판정, runtime checkout 독립, 필수 metadata, 최초 활성화 별도 |
| precise registry·최종 통합 | DVL-13 | 기존 required46gate/28AC final frozen candidate, 독립 test/review 및 실제 시간 근거 |

이 표는 PLAN dependencies/status를 새로 정하는 병렬 스케줄러가 아니다. 기존03→저장 기반,07→DB catalog 등 실제 선행을 모두 유지한다.
01→02,04/05→06→07,04→11→12 등 독립 기능을 요약 route와 불필요하게 묶지 않는다.
Fast2분/precise10분은 목표이고 cutoff가 아니다. 반복 fixtures는 재사용하되 실제60초/65초 의미를 fake clock으로 대체하지 않는다.

## 10. 결정 시점과 리뷰 요청

지금 새로운 사용자 제품 결정을 요구하지 않는다. 이 초안/리뷰 수신은 route 채택·bundle 승인·구현 재개의 근거가 아니다.

- Probe 후: explorer prompt 충돌을 보고 필요한 기존 agent 해석 선택, route-specific gate/계약/owner 수정의 범위와 exact bundle 승인.
- 공유 route면 실제 최대 지연/lock 점유 관측 후 Q-S2-03. 비공유 입증 시 해당 수용 결정이 불필요할 수 있음.
- 실제 metadata가 안 나오면 어떤 항목·이유인지 보여 주고 Q-S3-05. 임의 fallback 없음.
- Thinking/tier 변경은 별도 paid RM 관측·비용·잘림·reasoning 의미 후 Q-S3-06. 현재 pin 유지.
- 공개 API로 typed 회수/child 격리/guard를 충족할 수 없다고 확인된 경우에만 대안 범위 결정을 요청한다.

Claude 리뷰 요청: 기존 합의와 충돌 여부, 구현자가 추정으로 채워야 할 누락, 제어/typed 회수·guard의 실현 가능성,
형식상 단순화가 아닌 실제 줄일 수 있는 검사, user 결정 시점의 적절성을 읽기 전용으로 평가한다.
기술 보완/관측 후 결정/실패 조건을 구분하고 구현·probe·테스트·승인·파일 변경은 하지 않는다.

## 11. 참고·관측 근거

- [승인 기록](../../../.workflow/dev-validation-log-retention/runs/approval-s5-p5-20261009/approval.json),
  [checkpoint 상태](../../../.workflow/dev-validation-log-retention/runs/checkpoint-commit-20261009-advice-01/CURRENT-STATE.md),
  [runtime08 관측](../../../.workflow/dev-validation-log-retention/runs/checkpoint-commit-20261009-advice-01/guard-barrier-runtime08.json).
- [Python 계약](../../../src/workbench/contracts/logs_v1.py), [TS 계약](../../../omp_bridge/contract/logs_v1.ts),
  [store 기반](../../../src/workbench/storage/important_log/store.py), [bridge](../../../omp_bridge/g3/bridge.ts),
  [explorer](../../../omp_bridge/agents/explorer.md), [launcher](../../../src/workbench/backend/launcher.py).
- Version-pinned [OMP SDK v18.8.6](https://raw.githubusercontent.com/can1357/oh-my-pi/v18.8.6/packages/coding-agent/src/sdk.ts)
  및 [executor](https://raw.githubusercontent.com/can1357/oh-my-pi/v18.8.6/packages/coding-agent/src/task/executor.ts).
  공개 조합 옵션·제한 custom tool 지원의 존재만 확인했으며 전체 요구사항 충족을 뜻하지 않는다.
- Claude 이전 조언: `route-advice-after-checkpoint-01`, `subagent-logging-strategy-feedback-01` (사용자 전달 회신).
  리뷰 의견은 runtime pass나 사용자 승인으로 승격하지 않는다.

## 12. sls-2 보완 추적

근거: 사용자 전달 `subagent-logging-spec-sls1-review-01`(stale 아님), 보고 뒤 사용자 “좋아 이렇게 하자.”.
이전 sls-1 원문은 별도 run source snapshot으로 보존한다. 수정된 sls-2는 아직 재리뷰/실행 검증되지 않았다.

| 리뷰 항목 | 보완 위치·내용 |
| --- | --- |
| M1 | §2/§7: 일반 host busy 차단 제거, 사용자·승인·composer·종료/60초 점검 우선 및 실제 간섭 관측 |
| M2 | §5.1/§6/§7: 모델 내용 schema와 adapter envelope 분리, 사용량 관측값, source 귀속 검사, envelope byte 예약 |
| M3 | §3/SL-P05: 현재 연결 인증과 요청 시작 generation 분리, 같은 host late 수용·restart unknown |
| D1 | §3.1: 비동기 상태/cancel port의 별도 버전 계약·소비자/동결 seam 재검증 명시 |
| D2 | §7/SL-P01/05: caller prompt 미호출의 제한된 not_sent 증거, 실제 response 이벤트 의미 확인, 미확정 unknown |
| D3 | §3.1: 원본 byte digest/offset 유지, UTF-8 replacement·lossy 표시, 변환 후 전송 cap |
| D4 | SL-P02: read-back 정상/payload tools 변조의 hook abort·POST0 음성 추가 |
| D5 | §5.2: 상위 SPEC에 이미 있는 guard_violation_observed 사유를 기존 보호 admission에 연결, 새 운영 상태 아님 |
| D6 | §9.1:08 시험 전용 control과09 제품 frame/port 구현 분리 |

제품 동작의 새 결정·API 성공을 이 수정으로 확정하지 않는다. 기존 승인 bundle과 기능 검증 상태는 그대로다.
