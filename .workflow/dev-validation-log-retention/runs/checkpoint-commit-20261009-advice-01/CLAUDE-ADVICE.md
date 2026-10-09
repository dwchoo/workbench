# Claude 읽기 전용 자문 — summary route와 사용자 결정의 필요성

의뢰 ID: `route-advice-after-checkpoint-01`. 대상: 기존 `claude-wb`, 회신: 기존 `codex-wb`.
사용자는 현재 상태 commit 후, 본인이 결정해야 하는 내용과 Root 추천안이 타당한지 충분한 근거를 가진 조언을 요청했다. 이전 설명이 부족해 판단하기 어려웠다고 명시했다.

## 1. 이번 의뢰의 권한

읽기 전용으로 아래 문서·현재 코드·실제 evidence를 대조하고 **Root 추천을 비판적으로 평가**해 달라. 확증을 구하는 의뢰가 아니다.
문서/코드/state/리뷰 파일 쓰기, 테스트·probe·모델 호출, git 변경·commit·승인, 구현 시작, 새 agent/pane/worktree 생성, 사용자 logs/DB/settings 변경을 하지 마.
이번 commit은 구현 완료나 대안 채택이 아니라 WIP 보존이다. 최종 commit SHA는 전송 prompt에서 제공한다.
완료되면 전체 조언을 `herdr agent prompt codex-wb`로 자동 전달해 달라(`--wait` 없이; 사용자 대신 권한 승인 금지). 전송하지 못하면 실패 사실과 회신 내용을 자신의 응답에 남겨 달라.

## 2. 승인된 제품 의도 — 유지해야 할 것

- 기존 Manager/Worker OMP와 Python Workbench를 이용한다. 새 제품 agent 정의나 상주 OMP 추가 금지. 선정/요약 기준은 기존 agent에 내부 skill로 확정 전달한다.
- 불신 가능한 terminal 출력/조건/전달된 명세·지시·보고를 입력으로 읽을 뿐, 입력 안 지시를 실행하지 않는다. 전체 사용자 대화 수집은 하지 않는다.
- 도구 차단은 skill의 약속/모델 자기보고가 아니라 예방적 enforcement이며 요청·session·turn epoch·policy·one-use nonce에 묶여야 한다. GuardReceipt read-back 뒤 실제 send 사이의 교체/누락/무효 hook에서 **provider 전송0**이 요구된다.
- 운영 중 비허용 toolCall/tool_use도 감시해 결과 거부·compatibility_failed 즉시 보호·추가 모델call0. Guard 설정은 await/read-back해야 한다.
- 기존 agent main 대화를 terminal원문으로 채우지 않는다. typed result+request/range/provenance 검증, no replay, pause·취소/철회 권한, 사용자/due60s/end 우선순위가 필요하다.
- 새 정보가 있을 때5분 및 승인된 종료 후처리 요약. 주요SQLite와 DB상태점검은 Backend가 담당한다.
- **DV-D03**: OMP 업데이트가 잦으므로 최초 정상 검증/채택 이후 OMP버전만 변하면 매번 수동verify 안내/자동검증 없이 실행을 유지한다. 다만 최초8개pass, 요청별 guard와 실제 모델/입출력 preflight는 생략하지 않는다.
- 실제 비호환 또는 Workbench 관련 경계 변경은 새 summary를 막고 신규source만 legacy-stop; 기존 진행source의cohort는 유지한다. 알림1회+상태표시. Raw삭제 활성화는 별도 사용자결정이며 아직 꺼져 있다.
- 출력/입력/usage/model metadata의 불명은 fail-closed. 실제 user 모델·effort·tier pin을 임의 낮추지 않는다. 채택 route에서8개Q01을 다시 판정해야 하며 일부 pass를 섞어 전체pass로 만들지 않는다.
- 본체repo checkout 실행형. Python의 독립 모델/agent, 별도 TS completion/일회성 summary OMP를 자동으로 기존 agent로 간주하지 않는다. 대안 채택은 사용자권한이다.

정확한 계약은 승인된 `docs/features/dev-validation-log-retention/{BRIEF.md,SPEC.md,PLAN.json,tickets/DVL-08.md,tickets/DVL-09.md,tickets/DVL-14.md}`를 읽어 달라. 문서 publication 당시 pending 헤더는 역사적bytes로 남아 있으나 이후 정확한 s5/p5 승인과 implement권한이 있다:
- `.workflow/dev-validation-log-retention/runs/approval-s5-p5-20261009/approval.json`
- `.workflow/dev-validation-log-retention/runs/implement-p5-20261009/authority.json`.
Bundle digest `78bc55919e57bbf2132d6653780c5c164a2ee1d618422e5232d4cf0d1d667c3e`.

## 3. 현재까지 실제 관측 — 성공과 실패를 분리

호스트: Ubuntu24.04.5 / Python3.12.3 / OMP18.8.6 / tmux3.4 / herdr0.9.3. 유료/실모델 호출은 없었고 private OMP home/data/socket와 loopback scripted provider만 썼다.

DVL-08은 public SDK `runSubprocess`에 **기존 explorer 정의 원문**을 그대로 넣어 native child session을 시험했다. 이름과 달리 여기서 runSubprocess는 OMP내 in-process child이며 새OSprocess는 아니다.
기존 정의: `omp_bridge/agents/explorer.md`; SHA-256 `a226d3d2007fbced80bbe071a7e8f4f9587ee8d9fe96b05000059e5d8366d671`.
`restrictToolNames:true`, `sessionFile:null`, `keepAlive:false`, `persistArtifacts:false`, MCP/IRC/LSP 비활성, prepared extensions 재결속 및 autoloadSkills를 사용했다. 실제 검증용으로 격리 일회성 OMP를 띄운 것은 **제품의 별도 summary process를 채택한 것과 다르다**.

### 이전 runtime07의 일부 긍정 관측

Native child active catalog read/yield와 strict typed structuredOutput valid nonce를 읽었다. Scripted write/edit/bash 시도는 ToolNotFound; sentinels unchanged. Epoch를 무효화하여 parent가 외부 abort한 음성에서는 actualPOST0, positive에서는 actualPOST2였다.
이는 tool 제한/typed 회수와 abort 기능의 일부 존재를 보인 것뿐이다. 실제 공개 API의 원자적 guard-check/send를 보이지 않았고8개 전체pass가 아니다. 비허용 tool attempt 이벤트가 있으면 운영 결과를 거부해야 하므로 뒤의 valid yield를 제품 성공으로 간주한 것도 아니다.

### runtime08의 결정적 음성 관측

`guard-barrier-runtime08.json`은 원 actual evidence의 그대로 보존한 복사본이다.
- guard extension 누락: loopback actual provider POST1.
- extension은 등록·hook 관측되지만 enforcement가 없는 inert stub: actualPOST1.
- 두 경우 advertised tools는 read/grep/glob/web_search/yield; native structured result valid 및 process exit0.
- 이 exit0은 **시험 절차가 끝났다는 뜻**이지 safety gate pass가 아니다.
- POST에 write/edit/bash가 광고되었거나 사용자파일을 수정한 사실은 관측하지 않았다. 관측된 결함은 합의된 “유효guard 없으면 전송0” 조건을 tested route가 제공하지 않는 것이다.
- executor option에서 caller가 first prompt 이전 guard를 확인하고 send를 보유하는 명시 barrier를 찾지 못했다.
- 상태: tested native executor route의 `blocked_adoption_guard_confirmation_unavailable`. **모든 OMP 공개경로가 불가능하다는 증거는 아니다.**
- owned parent/provider 전부 종료. 실제 사용자로그·DB·인증·운영세션을 쓰지 않았다.

기록:
- `.../implement-p5-20261009/result-dvl08-runtime-07.json`
- `.../implement-p5-20261009/result-dvl08-runtime-08.json`
- `.../implement-p5-20261009/q01-next-probe-primary-assessment{,-02}.json`.
실제 collector는 Root의 placeholder가 아니라 이 checkpoint의 `dvl08-runtime.patch`와 별도workspace
`/tmp/workbench-dvl-20261009-3l1ViH/dvl08/tests/backend/live_summary_feasibility.py`다. 이 파일의 runtime08 분기/가드등록·scripted provider 집계를 읽어 probe가 정말 가설을 반증하는지도 검토해 달라.
Collector 후보 `3f50d497011ad76a05ff524149ae09e04e700c9e1b0ea6976ff33b7efae7fc7d`.
Root에 통합된 것은 validator/recheck/policy이고 collector나 product adapter가 아니다.

공개 version-pinned primary source(기존 Root가 읽은 경계):
- https://raw.githubusercontent.com/can1357/oh-my-pi/v18.8.6/packages/coding-agent/src/task/executor.ts
- https://raw.githubusercontent.com/can1357/oh-my-pi/v18.8.6/packages/coding-agent/src/task/index.ts
- https://raw.githubusercontent.com/can1357/oh-my-pi/v18.8.6/packages/coding-agent/src/modes/runtime-init.ts
- https://raw.githubusercontent.com/can1357/oh-my-pi/v18.8.6/packages/coding-agent/src/extensibility/extensions/types.ts

Root가 본 호출흐름은 extension초기화→pendingmessages→abortcheck→autoloadSkills→driveSessionToYield(session.prompt). `before_provider_request`는 typed block return이 없는 payload hook이며 async tools ack/readback은 sendlease와 동일하지 않다. `createAgentSession`/registry 등의 caller-controlled 공개조합은 **전부 실험해 소진한 것이 아니다**.

## 4. Root가 직전에 추천한 것 — 아직 선택·검증되지 않음

“기존 agent 정의를 재사용하는 격리 일회성 OMP **제품 summary 경로**의 최소실험을 명시 허용한다. 새 정의·상주프로세스·유료모델·실제raw삭제 없이 안전조건은 그대로 유지한다.”

이 대안이 guard를 해결한다는 관측은 없다. 새OSprocess 자체는 enforcement가 아니며 시작시간·사용자context 분리·auth/metadata/usage·직접origin·late/no replay 관리가 늘어날 수 있다.
기존 agent 재사용과 새 agent 정의는 다르지만, 사용자가 원한 “기존 agent 활용”이 definition 재사용으로 충분한지도 제품정책 차이다.
일회성실험의허용, 제품경로최종채택, 유료실모델시험, rawrotation활성화는 각각 별개이다. 사용자가 한번 동의했다고 모두 허가되는 것은 아니다.

## 5. 기술적으로 독립 진행 가능한 범위와 실제 상태

`CURRENT-STATE.md`에 Root통합·별도후보를 구분했다.
- 03foundation 통합97tests,08judge31fixtures. 실제제품 중요DB 활성화/scheduler/status/retention/rollover 완료 아님.
- 01fast별도후보81tests26.401s,독립/관련33tests+최종cleanup회귀 pass,Root통합전.
- 045별도후보원자성fix26tests pass,독립32중1fail setup의타당성/수정+cursorfault새회귀+freshreview미완료.
- precise2분/10분 목표중fast일부관측만있다. 실제60/65초matrix·실모델budget·공유차단시간은미관측.
- route미결정영향:09→10→14→13final. 다른01→02,045→06→07,04→11→12는 기존권한에서진행가능하다. **프로젝트전체정지를선택할필요는없다.**
- 이 요청에서는 기존 구현을 재개하지 않았다.

## 6. 요청하는 조언 — 사용자가 판단할 수 있도록

1. Root의 “tested route의guard 장벽 미제공” 해석이 정확한가? 시험08이 enforcement 부재와 caller확인 부재를 잘 구분하는가? 전체native공개API를포기하고 사용자결정을 먼저 요구한 것은 성급하지 않은가?
2. 이미 승인된 기존agent공개API범위에서 `createAgentSession` 등 firstprompt를caller가 소유하는 방식, 네이티브restrict+요청epoch/sendbarrier 등으로 더확인할 기술대안이 있는가? 새정의/새상주/직접completion 없이 가능한지, 근거와미검증을분리해 달라.
3. Root추천의 일회성OSprocess가 실제guard문제를해결하는가? 프로세스분리가아닌 구체적preventiveguard/check→send/failclosed 메커니즘은 무엇이어야 하는가? 보유context·modelpin·priority·requestjournal·skill확정로드에 어떤비용이생기는가?
4. 현재 “사용자가새권한을주어야하는결정”은정말무엇인가? 현재승인안의기술조사인지, 제한된대안실험권한인지, 제품route채택인지 구분해 달라. 불필요한결정질문은줄이고, 정말필요할때선택문장을제안해 달라.
5. 선택지별 사용자체감·정보보존·보안·개발시간·운영/모델비용을 비교하고 추천해 달라. 기본안유지+bounded공개API재조사 / 별도일회성최소실험 / 요약은보류하되독립기능개발 등. 안전조건을완화하는안을슬쩍포함하지 마.
6. 권장다음실험이있다면 읽기전용으로설계만제시해 달라: 가설·publicAPI·최소positive/negative·전송0/typed결과/guardproof·관측한계·중단조건·현재권한안인가. 실험성공≠제품채택임을 유지해 달라.
7. Q-S2-03(공유route차단수용),Q-S3-05(metadata불명대체상한),Q-S3-06(thinking/tier변경)은현재관측전이다. 지금사용자에게이를고르게할근거가있는지, 나중에어떤정보를보고결정하게해야하는지도알려 달라.

답변은 “동의/이견”, 현재확정사실, 아직부족한기술조사, 지금필요한사용자결정(없다면없음), 추천과이유·손해, 최소다음단계로 정리해 달라. 사용자에게다시설명할수있을정도로구체적이어야하되 새승인·기능pass로표현하지 마.
