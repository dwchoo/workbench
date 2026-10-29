# Task Inbox & Web Board — 요구사항 합의

Revision: r0.4; state: approved (2026-10-25, 사용자가 인터뷰 결정 D1~D42를 확정한 뒤 orchestrator 검토·수정을 거친 승인을 사전 위임).
작성일: 2026-09-23(r0.1~r0.2), 개정일: 2026-10-25(r0.3~r0.4).
D-05~D-30은 2026-10-25 요구사항 인터뷰(grilling)의 사용자 결정이며 사용자가 같은 날 결정 목록 전체를 확인했다. D-31~D-36은 남은 항목에 대한 [orchestrator 결정, 사용자 사전 위임]이며 사용자가 나중에 바꿀 수 있다. r0.4는 orchestrator가 결정 목록과 대조해 고치고, 대체된 r0.2 이력을 끝의 "이력"으로 옮긴 판이다. 모든 ID는 보존한다. 문서 승인은 구현 권한과 별개다. 승인 reference·digest 파일은 orchestrator가 따로 남긴다.
2026-10-27 개정(사용자 결정, 구현 전): 외부 접점 MCP를 없애기로 했다. 이 문서의 MCP client·Hub MCP·MCP 응답·MCP tool은 `/api/v1/` REST API와 `wb-inbox` skill로 읽는다(등급·한도·두 단계 취소·결과 범위는 그대로). 원문은 보존한다. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D05~D11, [ADR-0006](../../adr/0006-rest-skill-single-external-contact.md).

이 문서는 (A) Web Board다. 선행 문서는 [Hub·다중 WB 프로젝트 BRIEF](../workbench-hub/BRIEF.md)(이하 (B), HB-D01~HB-D15)이며, (A)는 (B)의 Hub 위에서 동작한다.

## 문제, 사용자와 목표

- [PRD 기준] 외부 agent의 작업 요청을 보관하고 manager의 검토·토의를 거쳐 worker 실행 및 결과 조회까지 연결한다.
- [사용자 결정 D-05] Core가 완성되었으므로 Phase 2를 지금 시작한다. 요구사항은 (B) Hub를 먼저, (A) Web Board를 그 위에 둔다(HB-D01).
- [사용자 결정 D-06] 소유자는 한 사람, 한 OS 사용자다(HB-D02). 외부 MCP client도 소유자 자신의 agent이며, "요청자"는 API key 이름으로 식별되는 소유자의 agent를 말한다.
- [목표] 소유자가 LAN의 브라우저에서 등록된 모든 WB 프로젝트의 상태와 요청을 보고, 요청을 작성하고, 소유자 전용 Board agent와 shell로 요청을 찾고 정리할 수 있게 한다. 요청은 manager 검토를 거쳐 상시 위임 아래 실행되고 결과가 같은 요청 기록에 남는다.

## 공통 용어

[CONTEXT.md](../../../CONTEXT.md)를 따른다. 특히 요청, 요청 출처, 요청 상태, 요청 revision, 검토 방식, 검토 후 자동 실행, API key 등급, Board agent, Board agent 세트, Hub, 소유자, 대기 사유, 승인 명세, Task, 명세 revision, 명령 stop을 따른다. Workbench의 제품 정의와 이번 인터뷰의 작업 범위는 구분한다.

## 범위와 비목표

- [PRD 기준] 요청 접수·조회·댓글, 검토 기록, 승인 명세 연결, 내부 전달, 실행 추적, 웹 조회, 결과 회수 및 영속 저장.
- [포함] Hub가 제공하는 웹: 등록된 WB 프로젝트 목록·상태(HB-D09), 요청 목록·상세·작성·편집·댓글·취소, 수동 검토 방식의 "검토 시작"(D-31), WB 프로젝트별 검토 방식 설정(D-32), 제한된 backend 제어(켜기/끄기·일시정지·재개·`confirm-boot`), 로그인·API key 관리, Board agent 세트(Board agent OMP tab + 일반 shell tab). Hub MCP endpoint(외부 agent·Board agent용). Manager의 검토 결과 기록 도구.
- [사용자 결정 D-08, 해제된 비목표] PRD §2 "제외"와 r0.2의 "원격 terminal 조작", PRD의 "임의 shell 실행 API", "범용 chatbot 웹 UI", 그리고 OMP MCP 비활성 정책은 다음 범위에 한해 해제한다. (1) 웹 terminal과 shell은 Board agent 세트에만 둔다. (2) Board agent의 OMP MCP 연결은 Hub MCP 하나만 허용한다. (3) 웹의 backend 제어는 켜기/끄기·일시정지·재개·`confirm-boot`로 한정한다. 이 해제는 PRD 문구를 고치지 않고 이 문서의 사용자 결정으로 기록한다. [2026-10-27 개정: Board agent는 MCP 없이 `wb-inbox` skill만 쓴다. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D11, [ADR-0006](../../adr/0006-rest-skill-single-external-contact.md)]
- [사용자 결정 D-08, 계속 제외] 웹에서 manager·worker·host pane 조작, 다중 worker, 범용 scheduler, 파일 첨부, 외부 알림(메일·메신저), OAuth·외부 identity, rate limit, Docker, callback/webhook delivery, 전체 대화 동기화.

## 제약과 호환성

- [사용자 결정 D-07] 네트워크는 LAN 공개다. HTTPS를 지원하지만 선택이고, HTTP를 허용한다(VPN 사용 전제).
- [사용자 결정 D-03, D-36] Codex CLI는 첫 사용 후보지만 client 선택의 우선순위는 낮다. 개발 중 client 호환성은 표준 라이브러리로 만든 MCP 테스트 client로만 검증하고, Codex CLI 호환성은 대상 host에서 확인한다. 개발 테스트 통과를 Codex CLI 호환 완료로 보지 않는다.
- [PRD 기준, r0.3 한정] Board 없이도 core가 동작해야 하며 Board가 host terminal이나 PTY를 소유하지 않는다. Board agent 세트의 PTY는 Hub가 소유하지만 WB 프로젝트의 host terminal과 별개이며, 웹은 WB 프로젝트의 host terminal을 조작하지 않는다.
- [PRD 기준] 요청 저장과 mailbox 통지를 분리하고, 저장 후 통지 실패로 접수 성공을 뒤집지 않는다.
- [PRD 제안, D-14·D-29로 결정] 서비스는 Hub 하나, 저장소는 WB 프로젝트별 SQLite(Hub 단일 writer), 외부 접점은 MCP Streamable HTTP의 tool 부분집합이다. [2026-10-27 개정: MCP를 없애고 외부 접점은 `/api/v1/` REST API와 `wb-inbox` skill이다. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D05, [ADR-0006](../../adr/0006-rest-skill-single-external-contact.md)]
- [기존 계약 C-D66] Manager의 `to_worker`는 UI 승인 없이 상시 위임으로 실행된다. D-17은 이 위임을 요청 출처와 무관하게 적용한다.
- [기존 계약 C-D70] Backend → manager 알림은 manager가 쉬고 입력창이 비었을 때만 전달된다(`editor_not_empty`면 대기). 요청 전달(D-15, D-31)도 이 경로를 쓰며 그 대기 사유를 웹에 보인다.
- [기존 계약 C-D71 (3)] 재부팅 뒤 `confirm-boot` 전에는 Workbench가 스스로 시작하는 동작을 보류한다. 웹의 `confirm-boot`는 같은 확인이며 보류 규칙을 바꾸지 않는다.
- [기존 계약, 설치 SPEC] 새 의존성 프레임워크보다 표준 라이브러리를 우선하고(`docs/features/workbench-installation/SPEC.md:117`), lockfile 변경 시 dependency audit을 한다(`:183`). D-29는 `aiohttp`와 그 전이 의존성을 예외로 둔다.

## 공개 동작과 수락 예시

다음 P2-AC-01~14는 [원본 PRD §14](../../OMP_Workbench_Phase2_Task_Inbox_PRD_v0.1.md)의 요구사항 입력이다. 기존 ID를 보존한다. r0.3에서 바뀐 항목은 "r0.3 한정"으로 표시한다.

- P2-AC-01, 04: Board 없이 core를 사용하고, manager가 offline이어도 Board가 살아 있으면 접수·조회한다. r0.3 한정: "Board"는 Hub다. Hub가 꺼지면 Inbox는 쓸 수 없고 core는 영향이 없다. WB 프로젝트가 꺼져 있어도 Hub가 살아 있으면 접수·조회한다(D-14).
- P2-AC-02, 03: 접수는 완료를 기다리지 않고 ID를 반환한다. 같은 요청자·키·내용은 중복 생성하지 않으며 내용이 다르면 충돌로 처리한다.
- P2-AC-05, 06: 웹과 MCP에서 같은 기록을 조회한다. 질문·답변·재검토가 이어지며 조회는 모델을 호출하지 않는다.
- P2-AC-07, 08: 외부 요청은 실행 승인이 아니다. 승인 명세 연결, 전달 ACK와 실제 시작을 구분한다. r0.3 한정: 접수·client 호출만으로는 실행되지 않고 manager 검토가 반드시 앞선다. 검토로 승인 명세가 연결되면 출처와 무관하게 상시 위임으로 실행된다(D-17, ADR-0004).
- P2-AC-09, 10: Command 성공만으로 요청을 성공 종료하지 않고, 새 요청이 실행 중 작업을 자동 선점하지 않는다.
- P2-AC-11, 12: 알림 병합에도 요청을 보존한다. 불명확한 실행은 자동 재전송하지 않으며 재시작·연결 상실·동기화 지연을 구분한다.
- P2-AC-13, 14: 요청자 권한과 안전한 웹 표시를 검증하고, 결과 조회로 비밀 정보나 임의 host 파일이 노출되지 않도록 한다. r0.3 한정: P2-AC-13은 유지한다(어떤 API key도 승인 명세 연결·terminal·backend 제어 불가). P2-AC-14는 의도적으로 완화한다. 모든 client가 소유자의 것이므로 명시적 요청 시 requester key를 포함한 모든 client에 원문 로그·명령 출력·실행 산출물 전체 조회를 허용한다(D-22). Board agent와 웹 shell은 sandbox 없이 사용자 권한으로 host 파일에 접근한다(D-25, ADR-0003).

r0.3~r0.4에서 더하는 수락 예시다. "관측"은 웹 화면, MCP 응답, `wb hub status`, 감사 기록, 프로세스 존재 여부다.

- P2-AC-15 (D-10): 비밀번호가 설정되지 않으면 웹 listener를 열지 않고, `wb hub status`가 그 이유를 보인다. Hub의 registry·목록·켜기/끄기는 그대로 동작한다. `wb hub passwd`로 비밀번호를 정하면 listener가 열린다. (2026-10-26 사용자 결정: web 기본 비활성, `[web] enabled = true`로 켬. 켜지 않았으면 비밀번호를 정해도 열리지 않고, `wb hub status`와 `wb hub passwd`가 켜는 방법을 안내한다.)
- P2-AC-16 (D-10): Listener는 `hub.toml`에 설정한 주소·port에만 bind한다. `0.0.0.0`은 명시 설정이 있을 때만 쓴다. `allow_insecure_http`의 기본값은 허용이며, 기본 설정에서 TLS 경로가 없으면 평문 HTTP로 제공한다. Loopback이 아닌 주소에서 평문 HTTP로 제공하면 웹 화면과 `wb hub status`에 경고가 보인다. `wb hub tls --self-signed`는 host의 openssl로 인증서를 만들고 설정 경로에 둔다. `allow_insecure_http`를 끈 경우의 정확한 동작은 SPEC에서 정한다.
- P2-AC-17 (D-11): 로그인 실패가 반복되면 다음 시도에 지연이 붙는다. 세션 cookie는 HttpOnly이고 CSRF 방어가 없는 상태 변경 요청은 거절된다. 세션은 24시간 뒤 만료된다. 웹에서 비밀번호를 바꾸려면 현재 비밀번호가 필요하고, 바꾸면 다른 모든 세션이 무효가 된다. 비밀번호를 잊으면 `wb hub passwd`로 다시 정한다. 로그인한 세션은 Board agent 세트를 열 때 다시 인증하지 않는다. OAuth 등 외부 로그인 수단은 없다.
- P2-AC-18 (D-11): 감사 기록에 로그인, terminal 열기·닫기·인수, API key 발급·폐기가 남는다. Terminal 입력 내용은 기록하지 않는다.
- P2-AC-19 (D-12): API key는 발급할 때 한 번만 보이고, 저장소에는 hash만 남는다. 폐기한 key나 만료한 key의 다음 호출은 거절된다. 웹에 key별 이름·scope·등급·마지막 사용 시각·만료가 보인다. 한 key로 짧은 시간에 많은 요청을 내도 rate limit으로 거절되지 않는다.
- P2-AC-20 (D-12, D-34): requester key는 자기가 낸 요청만 제출·조회·댓글·취소한다. 다른 요청의 조회는 거절된다. editor key는 scope 안 모든 요청을 P2-AC-42의 범위에서 다루고 WB 프로젝트 검색·상태 조회를 한다. 두 등급 모두 scope 밖 WB 프로젝트에는 접근할 수 없다. 어떤 key로도 terminal 열기·인수, 승인 명세 연결, backend 켜기/끄기·일시정지·재개·`confirm-boot`, 수동 검토 시작, 검토 방식 변경을 할 수 없다.
- P2-AC-21 (D-09): 웹의 켜기/끄기·일시정지·재개·`confirm-boot`는 로그인한 소유자만 할 수 있고, 각각 확인 창을 거친다. 확인 창에서 취소하면 아무것도 바뀌지 않는다. 끄기는 HB-AC-09의 절차를 따른다. 웹에는 manager·worker·host pane 입력 수단이 없다.
- P2-AC-22 (D-09): 대기 사유가 있으면 웹에 보인다. 예: manager 입력창에 글이 있어 요청 전달이 기다리는 동안 "manager 입력창에 작성 중인 글이 있어 알림 대기"가 표시된다. (2026-10-25 주석: 제품 label의 원문은 `hub/presentation.py` `WAIT_LABELS`의 "manager 입력창에 글이 있어 알림 대기"이며 이 예시 문구는 설명용이다.)
- P2-AC-23 (D-14): WB 프로젝트가 꺼져 있을 때 낸 요청은 NEW로 저장되고 목록에 보인다. WB 프로젝트를 켜면 접수 순서대로 처리된다. Hub가 꺼져 있으면 웹·MCP 접수는 실패로 보이고, WB 프로젝트의 core 동작은 영향이 없다. 요청은 그 WB 프로젝트의 `.wb/inbox.sqlite3`에만 저장되고 다른 WB 프로젝트와 섞이지 않는다.
- P2-AC-24 (D-15): 검토 방식이 자동(기본)이면 WB 프로젝트가 켜져 있고 manager가 쉬고 있을 때 Workbench가 NEW 요청을 manager에게 전달하고 상태가 REVIEWING이 된다. Worker가 실행 중이어도 그 작업을 선점·중단하지 않는다. 수동이면 자동으로 전달하지 않고 "검토 시작"(P2-AC-39) 전까지 NEW로 남는다.
- P2-AC-25 (D-16, D-18): Manager가 검토 결과 도구로 질문하면 요청이 NEEDS_INFO가 되고 질문이 댓글로 웹·MCP에 보인다. 소유자(웹), Board agent, 원 요청 client 중 누가 답해도 요청은 REVIEWING으로 돌아가고 검토 방식에 따라 다시 검토된다. 거절하면 CLOSED(rejected)가 된다.
- P2-AC-26 (D-13, D-16): Manager가 승인 명세를 연결하면 요청에 Task ID와 명세 revision이 남고 READY가 되며, Task가 시작되면 RUNNING이 된다. 이후 요청 화면에서 Task 진행과 run 보고가 보인다. 연결된 command가 성공해도 manager의 완료 판단 전에는 CLOSED(success)가 되지 않는다(P2-AC-09).
- P2-AC-27 (D-16): 재시도 한도(3회) 소진 뒤 manager가 기다리는 사용자 결정은 요청 댓글로 묻고 답할 수 있다.
- P2-AC-28 (D-17): 소유자·Board agent·외부 key 어느 출처의 요청이든 manager 검토로 승인 명세가 연결되면 소유자의 별도 승인 없이 실행된다. 웹 목록·상세와 MCP 응답에 출처(소유자 / Board agent / key 이름)가 항상 보인다.
- P2-AC-29 (D-19): NEW·NEEDS_INFO 요청의 편집은 바로 반영된다. READY·RUNNING 이후의 편집은 새 요청 revision으로 남고 manager에게 알림이 간다. 실행 중 Task는 manager가 새 명세 revision을 만들기 전까지 바뀌지 않는다.
- P2-AC-30 (D-20): 실행 전 요청의 취소는 바로 CLOSED(cancelled)가 된다. RUNNING 요청의 취소는 확인을 거친 뒤 기존 명령 stop(사용자 stop)과 Task 취소 경로를 탄다. 취소 요청 수락만으로 명령이 끝났다고 보고하지 않는다. 취소는 소유자, Board agent(editor key), 원 요청 client만 할 수 있다. MCP의 확인 형태는 P2-AC-41이다.
- P2-AC-31 (D-22): 결과 조회의 기본은 manager의 최종 보고 요약이다. MCP tool의 옵션이나 웹 "전체 보기"를 명시하면 원문 로그·명령 출력·실행 산출물 전체를 볼 수 있다. requester key도 자기 요청에 대해 전체 보기를 할 수 있다. 요청 본문의 script·HTML은 웹에서 실행되지 않는다(P2-AC-13 유지).
- P2-AC-32 (D-23, D-24): 요청은 자동 삭제되지 않는다. CLOSED 요청은 기본 목록에서 숨고 filter로 보인다. 파일 첨부 수단은 없고 본문의 경로 참조만 있다. 브라우저 tab 제목과 badge에 처리 대기·NEEDS_INFO 수가 보이고, 그 밖의 알림(메일·메신저·OS 알림)은 없다.
- P2-AC-33 (D-25): Board agent는 Hub가 실행하며 어느 WB 프로젝트의 manager와도 별개다. Board agent는 Hub MCP에 editor 등급으로 연결되어 등록된 모든 WB 프로젝트를 검색하고 요청을 등록·편집한다. Board agent는 승인 명세를 연결하거나 worker에게 전달할 수 없고, Board agent가 낸 요청도 manager 검토를 거친다. 다른 MCP, 프로젝트 설정, 사용자 `~/.omp` 설정은 Board agent에게 보이지 않는다. WB 프로젝트의 manager·worker MCP는 계속 비활성이며 기존 누출 검사가 통과한다. Board agent는 manager와 같은 OMP 로그인을 쓰고, 재시작해도 세션·메모리가 유지된다. [2026-10-27 개정: Hub MCP 대신 내부 listener의 REST를 `wb-inbox` skill로 쓰고 MCP는 없다. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D11, WBX-AC-16]
- P2-AC-34 (D-26): 소유자는 Board agent tab 하나와 일반 shell tab 하나를 연다. 브라우저를 닫아도 둘 다 계속 실행되고, 다시 열거나 다른 브라우저에서 열면 이어서 연결된다. 웹의 재시작 버튼으로 Board agent를 다시 띄울 수 있다. [2026-10-29 개정: 여러 agent·shell의 widget. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D42~D46, WBX-AC-25~31]
- P2-AC-35 (D-28): 연결 수명. (1) 인증된 새 tab은 항상 즉시 인수한다. (2) 인수 뒤 이전 연결(지난 epoch)의 입력은 버려진다. (3) Heartbeat가 10초 간격으로 두 번 오지 않으면 서버가 연결을 끊고 "연결 없음"으로 표시한다. (4) Tab을 닫으면 가능한 한 바로 해제한다. (5) 다시 살아난 이전 tab에는 "다른 곳에서 연결됨 — 다시 가져오기"가 보인다. 검증 사례: tab 닫기, 브라우저 강제 종료, 네트워크 단절과 복구, 두 tab 동시 연결, 인수 직후 이전 tab 입력. 어느 경우에도 남은 "연결됨" 표시가 새 연결을 막지 않는다. [2026-10-29 개정: 인수 대신 공유 연결. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D54, WBX-AC-32·33]
- P2-AC-36 (D-27): (1) 다른 곳에서 GPU를 쓰고 있으면(nvidia-smi 사용량, "프로젝트 X 실험 실행 중") Board agent는 GPU를 쓰기 전에 "계속할까요?"라고 묻는다. (2) 어떤 WB 프로젝트의 manager·worker가 작업 중일 때 Board agent는 그 WB 프로젝트의 코드를 수정하기 전에 "계속할까요?"라고 묻는다. 어느 경우에도 lock이나 자원 중재는 하지 않는다.
- P2-AC-37 (D-29): 웹은 외부 CDN 없이 동작한다(오프라인 LAN). `xterm.js` 자산은 설치본에 포함되고 license 고지가 함께 있다. 설치 의존성에 `aiohttp`와 그 전이 의존성이 고정 버전으로 들어가며 dependency audit 결과가 남는다. MCP는 `mcp` SDK 없이 동작한다.
- P2-AC-38 (D-21): 로그인한 소유자는 웹 양식에서 WB 프로젝트를 골라 요청을 작성하고, 편집하고, 댓글을 단다. 작성한 요청은 출처 "소유자"로 NEW에 저장된다.
- P2-AC-39 (D-31): 검토 방식이 수동인 WB 프로젝트의 NEW 요청은 웹 "검토 시작" 버튼(로그인한 소유자만) 또는 TUI에서 시작할 때 manager에게 전달되고 REVIEWING이 된다. 전달은 C-D70의 대기 규칙을 따른다. API key로는 검토를 시작할 수 없다.
- P2-AC-40 (D-32): 검토 방식은 웹의 WB 프로젝트 설정 화면(소유자)과 CLI에서 바꾼다. 값은 그 WB 프로젝트의 `.wb/` 아래 저장되어, Hub 재시작이나 등록 해제·재등록 뒤에도 유지된다.
- P2-AC-41 (D-33): MCP client가 RUNNING 요청을 취소하면 첫 호출은 취소하지 않고 token과 영향 요약(연결된 Task·실행 중 명령)을 돌려준다. 같은 token으로 확인을 호출해야 P2-AC-30의 취소 경로를 탄다. token이 없거나 맞지 않거나, 그 사이 요청·Task 상태가 바뀌었으면 확인은 거절되고 아무것도 멈추지 않는다. [2026-10-27 개정: 같은 두 단계를 REST `POST /api/v1/requests/{id}/cancel`로 한다. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D05, WBX-AC-10]
- P2-AC-42 (D-34): editor key는 요청 작성·편집·댓글·취소, NEW↔NEEDS_INFO 전환, 닫힌 요청 다시 열기를 할 수 있다. 승인 명세 연결, READY·RUNNING 지정, CLOSED(success/failed) 종료는 거절되며 이 전이는 manager(검토 결과 도구)와 backend만 한다.
- P2-AC-43 (D-35): 같은 요청의 상태 변경이 겹치면 Hub가 도착 순서대로 하나씩 적용하고, 앞선 변경으로 더는 유효하지 않은 변경은 이유와 함께 거절한다. 예: worker 전달 전에 도착한 소유자 취소가 이기면 요청은 CLOSED(cancelled)가 되고, 뒤이은 승인 명세 연결은 거절되며 worker에게 아무것도 전달되지 않는다. Worker 전달 뒤의 취소는 P2-AC-30을 따른다.
- P2-AC-44 (D-36): 본문 64 KiB, 댓글 16 KiB, 제목 200자를 넘는 입력은 웹·MCP 모두 이유와 함께 거절되고 아무것도 저장되지 않는다(한도 값은 SPEC에서 조정할 수 있다).
- P2-AC-45 (D-36): 표준 라이브러리로 만든 MCP 테스트 client가 Streamable HTTP로 Hub MCP의 tool 부분집합(제출·조회·댓글·취소·결과 조회)을 호출하는 검증이 테스트에 있다. Codex CLI 호환성은 대상 host 확인 항목으로 따로 남는다. [2026-10-27 개정: MCP 테스트 client 대신 REST 테스트 client와 `wb_inbox.py`로 검증한다. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D07, WBX-AC-12]

## 결정과 근거

D-05~D-30은 2026-10-25 인터뷰의 사용자 결정이다(같은 날 목록 전체 확인). D-31~D-36은 [orchestrator 결정, 사용자 사전 위임]이다. D-01~D-04는 "이력"에 있다. 관련 ADR: [ADR-0003](../../adr/0003-board-agent-without-sandbox.md), [ADR-0004](../../adr/0004-auto-execution-after-manager-review.md), [ADR-0005](../../adr/0005-per-project-inbox-store-hub-single-writer.md).

인터뷰 결정 번호와의 대응: D1→D-05, D2→D-06, D3→D-07, D6·D17·D18·D26→D-25, D7→D-08, D12·D31→D-09, D21·D32→D-10, D13·D23·D32→D-11, D14·D41→D-12, D15→D-13, D16→D-29, D19→D-15, D20→D-14, D24·D34→D-27, D25·D33→D-26, D27→D-21, D28→D-18, D29→D-17, D30→D-16, D35→D-28, D36→D-19, D37→D-20, D39→D-23, D40→D-24, D42→D-22. Hub 쪽(D4·D5·D8~D11·D22·D38)은 (B)에 있다.

| ID | 상태·근거 | 결정 |
|---|---|---|
| D-05 | 사용자 결정 | Core가 완성되었으므로 Phase 2를 지금 시작한다. (B) Hub BRIEF를 먼저, 이 문서 (A)를 그 위에 진행한다. 기존 r0.2는 (A)의 입력이다. D-04의 보류를 해제한다. |
| D-06 | 사용자 결정 | 소유자는 한 사람, 한 OS 사용자다. 외부 MCP client도 소유자의 agent이며 요청자는 API key 이름으로 식별한다. 계정 구조는 나중에 확장할 수 있게 둔다. |
| D-07 | 사용자 결정 | 네트워크는 LAN 공개다. HTTPS는 지원하되 선택이고 HTTP를 허용한다(VPN 전제). D-02를 대체한다. |
| D-08 | 사용자 결정 | 해제하는 비목표: 웹 terminal과 shell(Board agent 세트에만), Board agent의 OMP MCP(Hub MCP에만), 웹 backend 제어(켜기/끄기·일시정지·재개·`confirm-boot`만). 계속 제외: 웹의 manager·worker·host pane 조작, 다중 worker, 범용 scheduler, 파일 첨부, 외부 알림, OAuth·외부 identity. [2026-10-27 개정: Board agent의 MCP는 없고 `wb-inbox` skill만 허용. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D11] |
| D-09 | 사용자 결정 | 웹은 상태 보기다. 웹에서 일을 시키는 통로는 Board agent이고(요청 양식은 D-21), manager·worker·host pane 조작은 없다. 웹 제어 버튼은 켜기/끄기·일시정지·재개·`confirm-boot`이며 확인 창을 거치고 로그인한 소유자만 쓸 수 있다. API key로는 할 수 없다. 대기 사유(예: "manager 입력창에 작성 중인 글이 있어 알림 대기")를 보인다. [2026-10-25 주석: 실제 label은 `WAIT_LABELS`의 "manager 입력창에 글이 있어 알림 대기"가 기준이다.] |
| D-10 | 사용자 결정 | Listener는 설정한 주소(예: VPN IP)에만 bind하고 `0.0.0.0`은 명시 설정이 필요하다. 비밀번호가 설정되기 전에는 웹 listener를 열지 않는다(Hub의 다른 기능은 동작). 설정 파일은 `~/.config/omp-workbench/hub.toml`이며 bind, port, TLS 인증서·key 경로, `allow_insecure_http`(설정은 있고 기본값은 허용)를 둔다. TLS는 선택이고 `wb hub tls --self-signed`가 host openssl을 쓴다. Loopback이 아닌 주소에서 평문 HTTP면 웹과 `wb hub status`에 경고한다. 2026-10-26 사용자 결정: web 기본 비활성, `[web] enabled = true`로 켬. 웹이 가장 많이 노출되는 부분이라 Hub 시작(자동 시작 포함)만으로는 웹(화면·외부 `/mcp`·`/ws/board`)과 Board agent 세트를 열지 않는다. 켜려면 `hub.toml`을 고치고 Hub를 재시작한다. |
| D-11 | 사용자 결정 | 초기 비밀번호는 `wb hub passwd`로 정하고 hash로만 저장한다(방식은 SPEC, 초안 후보는 표준 라이브러리 scrypt). 웹에서 바꾸려면 현재 비밀번호가 필요하고, 바꾸면 다른 모든 세션을 무효로 한다. 잊으면 `wb hub passwd`. OAuth는 없다. HttpOnly 세션 cookie, CSRF 방어, 반복 로그인 실패 시 지연, 세션 만료 24시간. Terminal 재인증은 없다. 감사 기록: 로그인, terminal 열기·닫기·인수, API key 발급·폐기(terminal 입력 내용은 기록하지 않음). |
| D-12 | 사용자 결정 | API key는 이름, 프로젝트 scope(전체 또는 선택), 등급을 가진다. requester: 자기 요청의 제출·조회·댓글·취소. editor: scope 안 모든 요청을 다루고(세부 전이는 D-34) WB 프로젝트 검색·상태 조회를 한다(Board agent용). Hash만 저장하고 한 번만 보이며 폐기할 수 있고 마지막 사용 시각과 선택적 만료가 있다. Rate limit은 없다. 어떤 key도 terminal 열기, 승인 명세 연결, backend 제어를 할 수 없다. |
| D-13 | 사용자 결정 | 게시판 항목은 core Task와 별개인 요청이다. 상태는 NEW → REVIEWING → NEEDS_INFO → READY → RUNNING → CLOSED(success/failed/rejected/cancelled)다. Manager의 승인 명세가 생기면 core Task(task ID + revision)에 연결되고, 이후 게시판은 Task 진행과 run 보고를 보인다. |
| D-14 | 사용자 결정 | 저장소는 WB 프로젝트별 `<root>/.wb/inbox.sqlite3`이고 Hub가 유일한 writer다. Backend와 Board agent는 Hub를 거친다. Hub가 꺼지면 Inbox를 쓸 수 없고 core는 영향이 없다(P2-AC-01). WB 프로젝트가 꺼져 있어도 Hub는 접수·조회하고(P2-AC-04), 요청은 NEW로 쌓였다가 WB 프로젝트가 켜지면 순서대로 처리된다. ADR-0005. |
| D-15 | 사용자 결정 | 검토 방식은 WB 프로젝트별 설정으로 자동(기본) 또는 수동이다. 자동이면 WB 프로젝트가 켜져 있고 manager가 쉴 때 backend가 기존 메시지 경로로 요청을 manager에게 전달한다. 실행 중 작업을 선점하지 않는다(P2-AC-10). |
| D-16 | 사용자 결정 | Manager는 새 전용 bridge 도구로 검토 결과를 기록한다: 질문 → NEEDS_INFO, 승인 명세 연결 → READY/RUNNING, 거절 → CLOSED(rejected), 진행 댓글. Hub를 거쳐 inbox에 저장되고 웹 댓글 thread에 보인다. 재시도 한도 소진 뒤의 "사용자 결정"은 요청 댓글로 답한다. |
| D-17 | 사용자 결정 | 모든 요청(소유자 웹, Board agent, 외부 key)은 manager 검토 뒤 상시 위임(C-D66)으로 자동 실행된다. 소유자 승인 단계는 없다. 출처(소유자 / Board agent / key 이름)는 항상 표시한다. 사용자는 외부 key 요청에 소유자 승인을 요구하는 안 대신 이것을 명시적으로 골랐다. ADR-0004. |
| D-18 | 사용자 결정 | NEEDS_INFO 질문은 댓글로 올라간다. 소유자(웹), Board agent, 원 요청 client가 답할 수 있다. 답하면 REVIEWING으로 돌아가고 검토 방식에 따라 다시 검토된다. |
| D-19 | 사용자 결정 | NEW·NEEDS_INFO의 편집은 바로 반영한다. READY·RUNNING 이후의 편집은 새 요청 revision으로 기록하고 manager에게 알린다. 새 명세 revision을 만들지는 manager가 정한다. 실행 중 Task를 조용히 바꾸지 않는다. |
| D-20 | 사용자 결정 | 취소는 소유자, Board agent, 원 요청 client가 할 수 있다. RUNNING의 취소는 확인을 거쳐 기존 사용자 stop([CS-O10](../command-stop/FOLLOWUPS-BRIEF.md) FU-D06~08)과 Task 취소 경로로 CLOSED(cancelled)가 된다. 실행 전 취소는 바로 닫는다. MCP의 확인 형태는 D-33. |
| D-21 | 사용자 결정 | 소유자는 웹 양식으로 요청을 작성·편집하고 댓글을 단다. |
| D-22 | 사용자 결정 | 결과는 기본으로 요약(manager 최종 보고)이다. 원문 로그·명령 출력·실행 산출물 전체는 명시 요청(MCP tool 옵션 또는 웹 "전체 보기") 시 제공하며 requester key에도 허용한다(자기 요청). 모든 client가 소유자의 것이기 때문이다. 이는 P2-AC-14(비밀 정보·임의 host 파일 노출 금지)의 의도적 완화다. [2026-10-27 개정: "MCP tool 옵션"은 REST 결과 조회의 옵션이다. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D05] |
| D-23 | 사용자 결정 | 요청은 자동 삭제하지 않는다. 닫힌 요청은 기본 보기에서 숨기고 filter(보관함)로 보인다. 파일 첨부는 없고 텍스트와 경로 참조만 둔다. |
| D-24 | 사용자 결정 | 알림은 웹 tab 제목과 badge 수(처리 대기, NEEDS_INFO)뿐이다. 외부 알림은 나중이다. |
| D-25 | 사용자 결정 | Board agent는 Hub가 실행·소유하는 OMP이며 어느 WB 프로젝트의 manager와도 별개다. 승인 명세를 연결하거나 worker에게 전달하지 않는다. Sandbox가 없다. 사용자의 일반 shell과 같은 권한이다(사용자 gh/git 설정의 GitHub 인증, 프로젝트 코드 수정, `wb init`으로 새 WB 프로젝트 생성 → 자동 등록, GPU 사용). 보안은 웹 로그인 + listener 주소 제한 + 감사 기록에 의존한다. Hub MCP에 editor 등급으로 연결해 등록된 모든 WB 프로젝트를 검색하고 각 프로젝트에 요청을 등록·편집한다. 다른 MCP, 프로젝트 설정, 사용자 `~/.omp` 설정은 막고, WB 프로젝트의 manager·worker MCP는 계속 비활성이다. Manager와 같은 OMP 로그인(`agent.db`)을 쓰고, 전용 영속 설정·home(세션·메모리)과 Hub 설정의 자체 모델·역할 prompt를 가진다. ADR-0003. [2026-10-27 개정: Hub MCP 연결 대신 내부 loopback listener의 REST를 `wb-inbox` skill로 쓰고, system key는 env `WB_API_KEY`, 허용 skill은 `wb-inbox` 하나, MCP는 없다. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D11, [ADR-0006](../../adr/0006-rest-skill-single-external-contact.md)] [2026-10-29 개정: Board agent는 여러 개이고 agent마다 home·세션·메모리와 시작마다의 system key를 가진다. 로그인·role prompt·model·격리 확인은 공유. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D44·D58] |
| D-26 | 사용자 결정 | 웹 terminal 세트는 소유자당 한 벌이다: Board agent OMP tab 하나 + 일반 shell tab 하나. 둘 다 브라우저를 닫아도 유지되며(detach 의미) 새 브라우저가 인수한다(D-28). 웹에 Board agent 재시작 버튼이 있다. [2026-10-29 개정: agent 여러 개(기본 한도 4, 1~8)와 shell 여러 개(기본 한도 10, 1~32)를 격자 widget workspace로 쓴다. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D42~D46] |
| D-27 | 사용자 결정 | 안내만 하고 자원 관리는 하지 않는다. (1) 다른 곳에서 GPU를 쓰고 있으면(nvidia-smi 사용량 / "프로젝트 X 실험 실행 중" 표시) Board agent는 GPU를 쓰기 전에 "계속할까요?"라고 묻는다. (2) 어떤 WB 프로젝트의 manager·worker가 작업 중이면 Board agent는 그 프로젝트의 코드를 수정하기 전에 "계속할까요?"라고 묻는다. Lock·중재는 없다. |
| D-28 | 사용자 결정 | 연결 수명: 남은 "연결됨"이 새 연결을 막지 않는다. (1) 인증된 새 tab은 항상 즉시 인수한다. (2) attach마다 lease/epoch 번호를 주고 지난 epoch의 입력은 버린다. (3) WebSocket heartbeat 10초, 두 번 놓치면 서버가 끊고 "연결 없음"으로 표시한다. (4) Tab을 닫으면 best-effort로 해제한다. (5) 다시 살아난 이전 tab은 "다른 곳에서 연결됨 — 다시 가져오기"를 보인다. (6) tab 닫기, 브라우저 강제 종료, 네트워크 단절·복구, 두 tab 동시, 인수 직후 이전 tab 입력을 테스트로 검증한다. [2026-10-29 개정: 한 연결 lease·즉시 인수·epoch 입력 버림·"다시 가져오기"를 공유 연결(모든 연결이 보고 입력, 도착 순서, 크기는 최근 활동 연결, terminal당 8 연결)로 바꾼다. (3)·(4)와 "남은 연결됨이 막지 않음"은 유지. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D54·D55] |
| D-29 | 사용자 결정 | 고정 버전 runtime 의존성 `aiohttp`(HTTP+WebSocket+TLS)를 추가한다. 고정 버전: `aiohttp==3.14.5`와 전이 의존성 9개 `aiohappyeyeballs==2.7.1`, `aiosignal==1.4.0`, `attrs==26.1.0`, `frozenlist==1.8.0`, `idna==3.20`, `multidict==7.2.0`, `propcache==0.5.4`, `typing-extensions==4.16.0`, `yarl==1.25.1`. Dependency audit을 한다. 설치 SPEC:117의 표준 라이브러리 우선에 대한 예외이며, 이유는 LAN에 공개되는 endpoint에서 HTTP·WebSocket을 직접 구현하는 것이 보안 위험이기 때문이다. `xterm.js`는 license 고지와 함께 wheel에 포함한다(CDN 없음, LAN이 오프라인일 수 있음). MCP Streamable HTTP의 tool 부분집합은 직접 구현한다(`mcp` SDK 미사용). Docker는 쓰지 않는다. [2026-10-27 개정: MCP 구현은 없앤다. `aiohttp` 예외의 근거는 REST에도 그대로다. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D05] |
| D-30 | 사용자 결정(알려진 한계로 기록) | GPU 경로는 이 개발 host에서 검증할 수 없다. 웹 로그인이 유일한 장벽이다. HTTP는 VPN에 의존한다. |
| D-31 | orchestrator 결정, 사용자 사전 위임 (Q-02) | 수동 검토 방식에서 검토는 웹 "검토 시작" 버튼(로그인한 소유자만) 또는 TUI에서 시작한다. API key로는 시작하지 않는다. |
| D-32 | orchestrator 결정, 사용자 사전 위임 (Q-09) | 검토 방식은 웹의 WB 프로젝트 설정 화면과 CLI에서 바꾸고, 값은 그 WB 프로젝트의 `.wb/` 아래 저장한다. |
| D-33 | orchestrator 결정, 사용자 사전 위임 (Q-03) | MCP client의 RUNNING 요청 취소는 두 단계다. 취소 요청이 token과 영향 요약을 돌려주고, 그 token으로 확인해야 취소한다(종료의 `shutdown_request`/`shutdown_confirm`과 같은 형태). [2026-10-27 개정: 같은 두 단계를 REST cancel endpoint로 한다. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D05] |
| D-34 | orchestrator 결정, 사용자 사전 위임 (Q-04) | editor 등급은 요청 작성·편집·댓글·취소와 NEW↔NEEDS_INFO 전환, 다시 열기를 할 수 있다. 승인 명세 연결, READY·RUNNING 지정, CLOSED(success/failed) 종료는 할 수 없으며 manager·backend 전용이다. |
| D-35 | orchestrator 결정, 사용자 사전 위임 (Q-08) | 겹치는 상태 변경은 Hub(단일 writer)가 도착 순서대로 직렬화한다. Worker 전달 전에 도착한 취소가 이긴다. 전달 뒤의 취소는 D-20을 따른다. |
| D-36 | orchestrator 결정, 사용자 사전 위임 (Q-06) | 입력 한도: 요청 본문 ≤ 64 KiB, 댓글 ≤ 16 KiB, 제목 ≤ 200자(SPEC에서 조정 가능). Client 호환성은 표준 라이브러리 MCP 테스트 client로만 검증하고, Codex CLI 호환성은 대상 host 확인 항목이다. [2026-10-27 개정: 검증은 REST 테스트 client와 `wb_inbox.py`로, Codex CLI 확인은 skill 사용 확인으로 한다. [web-board-ux](../web-board-ux/BRIEF.md) WBX-D05·D07] |

## 구현 방향과 검증 의도

- [PRD 기준] 요청 저장소, manager의 판단, worker의 실행을 구분한다. 명세·task ID·revision·실행 보고는 1차의 계약(C-D66, C-D70, C-D71, 명령 stop)을 재사용한다. 구체 API·tool signature, schema, MCP tool 이름, 웹 화면 배치, ticket 분할·일정은 SPEC에서 정한다(Q-07).
- [불변식] (1) 접수는 실행이 아니다. Manager 검토 결과 도구만 요청을 READY로 만든다. (2) Inbox 저장소의 writer는 Hub 하나다. (3) API key 경로로는 terminal·승인·backend 제어에 닿을 수 없다. (4) 웹 제어는 기존 backend 동작(종료 확인, pause/resume, `confirm-boot`)을 호출할 뿐 새 의미를 만들지 않는다. (5) 실행 중 Task는 요청 편집으로 바뀌지 않는다. (6) 남은 연결 상태가 인수를 막지 않는다. (7) Board agent는 승인·worker 전달 경로에 닿지 않는다.
- [검증 의도] 접수 중복, 권한 경계(등급·scope·key로 불가능한 동작, editor 전이 범위), 겹치는 상태 변경, 입력 한도, 알림 실패, 재시작, 실행 시작·종료 판정, 모델 호출 없는 조회, 로그인·CSRF·세션 무효화, 연결 수명 6사례(D-28), Board agent 격리 범위(Hub MCP만, manager·worker MCP 비활성 유지), 표준 라이브러리 MCP 테스트 client 호출.
- [검증 한계] 현재 구현이나 실행 테스트 결과는 없다. 원본에 인용된 외부 MCP/OMP 자료도 이번 인터뷰에서 재검증하지 않았다. GPU 안내 경로와 Codex CLI 호환성은 대상 host에서만 확인할 수 있다(D-30, D-36).

## 저장소 근거

HEAD `4ebaf9b`, 2026-10-25 정적 읽기(orchestrator 탐색 결과와 문서 작성 중 일부 줄 확인). 미커밋 변경이 있는 작업 트리다. 실행·재현은 하지 않았다. Hub 관련 근거는 [(B) BRIEF](../workbench-hub/BRIEF.md) "저장소 근거"에 있다. r0.2 근거는 "이력"에 있다.

| 사실 | 출처 |
|---|---|
| [관측 사실] UI transport는 0600 Unix socket뿐이다. HTTP·WebSocket·MCP·인증·API key 코드는 `src`에 없다 | `src/workbench/backend/ui_server.py`, `src/workbench/contracts/ui_v1.py` |
| [관측 사실] C-D66 상시 위임: manager `to_worker` → backend가 `scope_approved`+`proceed`를 기록하고 UI 승인 없이 전달한다 | `src/workbench/backend/flow_tasks.py:967-986`, `docs/features/core-workbench/DECISIONS.md` C-D66 |
| [관측 사실] 현재 TUI에서 사람이 판단하는 지점: 재부팅 뒤 `confirm-boot`(C-D71), pause/resume(UI 전용), 재시도 한도 3회 뒤 manager가 사용자 결정을 기다림 | `flow_tasks.py:127-133`, DECISIONS.md C-D71 |
| [관측 사실] Manager에게는 사용자에게 질문하는 전용 경로가 없고 일반 대화를 쓴다 | 탐색 결과(전용 도구 부재) |
| [관측 사실] Backend → manager 알림 경로가 있고, manager가 쉬며 입력창이 비었을 때만 전달된다(`editor_not_empty`면 대기, C-D70) | `src/workbench/backend/service.py:1793` (`_notice`), `src/workbench/backend/flow_recovery.py:590` (`Watchdog.enqueue_notice`) |
| [관측 사실] 종료는 `shutdown_request` → 확인 → `shutdown_confirm`(작업 변경 시 거절)이다 | `service.py:1427-1455`, `src/workbench/backend/cli.py:1010-1060` |
| [관측 사실] OMP MCP는 의도적으로 꺼져 있고(`mcp-json` provider 비활성, `mcp.enableProjectConfig: false`), launcher가 MCP tool 누출을 검사한다 | `omp_bridge/omp-isolation.yml:54`, `:68`, `src/workbench/backend/launcher.py:128`, `:822-857` |
| [관측 사실] OMP 인증 `agent.db`는 사용자 `~/.omp`의 것을 symlink로 공유한다 | `src/workbench/backend/omp_home.py` (module docstring) |
| [관측 사실] `pyproject.toml`의 runtime 의존성은 고정된 `pyte`, `wcwidth`뿐이다. 개발 `.venv`에는 D-29의 `aiohttp` 3.14.5와 전이 의존성 9개가 이미 설치되어 있다(2026-10-25 site-packages 확인) | `pyproject.toml:11`, `.venv/lib/python3.12/site-packages` |
| [관측 사실] 설치 SPEC은 새 프레임워크보다 표준 라이브러리를 우선하고 lockfile 변경 시 dependency audit을 요구한다 | `docs/features/workbench-installation/SPEC.md:117`, `:183` |
| [관측 사실, 개발 host 2026-10-25] bwrap 0.9.0은 비특권으로 동작하지만 `--tmp-overlay`가 없다. NVIDIA GPU가 없다(`/dev/nvidia*` 없음). openssl 3.0.13, node v24가 있다(node는 테스트에만 사용) | host 확인 |

## 미결정·SPEC 결정 사항

| 항목 | 해소된 부분 | 남은 부분(SPEC에서 정함) |
|---|---|---|
| Q-01 | D-04 → D-05로 해소(Phase 2 시작) | 없음 |
| Q-02 검토 시작·위임·승인 범위 | D-15·D-16·D-17, D-31(수동 검토 시작) | 없음 |
| Q-03 재검토·revision·취소·재시도·재연결 흐름 | D-16, D-18~D-20, D-28, D-33(MCP 두 단계 취소) | Token 유효 시간 등 세부 |
| Q-04 인증·역할별 권한·결과 공개 범위 | D-10~D-12, D-22, D-34(editor 전이 범위) | 없음 |
| Q-05 1차 계약과의 통합 | 1차 계약(C-D66, C-D70, C-D71, 명령 stop)이 있다 | 정확한 형태는 Q-07 |
| Q-06 저장·보존·입력 제한·client 검증·기술 | D-14, D-23, D-29, D-36 | 한도 값 조정 |
| Q-07 구체 계약 | — | API·tool signature, inbox schema, MCP tool 이름, 웹 화면 배치, Board agent 기본 모델, 정확한 Hub ↔ backend IPC(HB-O01), `wb hub enable` 세부(HB-O02), `allow_insecure_http`를 끈 경우의 동작, 비밀번호 hash 방식, ticket 분할·일정 |
| Q-08 겹치는 상태 변경 | D-35 | 없음 |
| Q-09 검토 방식 변경 위치 | D-32 | `.wb/` 안 파일 이름·형식 |
| Q-10 REVIEWING 중 편집 | D-19는 NEW·NEEDS_INFO와 READY 이후만 정한다 | REVIEWING 중 편집을 바로 반영할지 요청 revision으로 남길지 |
| Q-11 Hub 부재·백업 범위 | ADR-0005 "결과" | Hub가 없을 때 검토 결과 도구의 실패 표시·재시도, `.wb/inbox.sqlite3`의 `wb update` 백업 범위(WU-AC-04) 포함 여부 |

## 다음 단계

이 문서는 승인되었다. (B) Hub BRIEF 다음 순서로, to-tickets 절차에 따라 SPEC·ticket을 만든다(실행 주체는 정하지 않는다).

## 이력

r0.2(2026-09-23)의 결정과 근거다. ID를 보존하며 현재 요구사항은 위 절을 따른다.

- D-01: 사용자 답변 `1-b`. 당시 선행 core 부재를 확인했다. 최초의 Phase 2 우선 구체화 결정은 D-04로 대체되었다.
- D-02: 사용자 답변 `2-a`. 최초 환경은 Workbench·외부 agent·사용자 브라우저가 같은 컴퓨터에 있는 localhost 구성이었다. r0.3에서 D-07(LAN 공개)이 대체했다.
- D-03: 사용자 답변 `3-codex cli이지만 크게 중요하지 안을거 같아. 일단 workbench만 잘 만들면 좋겠어.` Client별 기능보다 제품 흐름을 우선한다. 현재도 유효하며 검증 범위는 D-36이 정한다.
- D-04: 후속 사용자 답변에서 `B. 1차 Core Workbench`를 명시적으로 선택했다. Manager 대화, worker 위임, terminal 실행·로그·결과 확인을 먼저 설계하고 Phase 2는 후속으로 두었다. r0.3에서 D-05가 보류를 해제했다.
- D-01~D-04는 개별 인터뷰 답변이며 당시 문서 전체 승인이나 구현 권한이 아니었다.
- r0.2의 범위 기록: "Phase 2 구현은 1차 core 완성과 실제 실험 검증 이후"(PRD 기준, D-05로 충족), "Phase 2 문서는 후속 요구사항 입력으로 보존하고 ticket을 만들지 않는다"(r0.3에서 대체), "1차 계약이 없으므로 API signature·이벤트 payload·파일 소유 범위·ticket 의존성을 확정하지 않는다"(1차 계약이 생겨 Q-07로 이어짐).
- Q-01(r0.2): D-04로 해결되었다(1차 core 우선).
- r0.2 저장소 근거: 조사일 2026-09-23, HEAD `fea46fd561f1da1cef32be191775b56c1cff0a13`. `git ls-tree -r --name-only HEAD`의 추적 파일은 README.md뿐이었다. `rg --files --hidden` 조사에서 제품 코드, 1차 PRD, 기존 glossary/ADR/기능 BRIEF를 찾지 못했고 사용자도 D-01에서 1차 부재를 확인했다. 원본 입력 [Phase 2 PRD v0.1](../../OMP_Workbench_Phase2_Task_Inbox_PRD_v0.1.md)은 조사 당시 untracked였고 SHA-256은 `7b612cad6913317eae33c09f5ec654dd691cb257fe9a617d1bf29a9e999be4e3`이다. 원본의 1차 wrapper·mailbox·명세 재사용은 당시 요구사항 전제였으며 구현이 있다는 관측 사실이 아니었다.
- r0.3(2026-10-25): 인터뷰 결정 D-05~D-30과 P2-AC-15~37을 더했다. r0.4(같은 날): 결정 목록과 대조해 고치고 D-31~D-36, P2-AC-38~45를 더했으며 승인 상태로 바꿨다.
