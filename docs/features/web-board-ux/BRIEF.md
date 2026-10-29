# Web Board 사용성·외부 요청 API — 요구사항 합의

Revision: r0.6; state: 승인(사용자 승인 2026-10-27; r0.2는 같은 날 SPEC s1 확인 항목 U1~U10의 사용자 답을 WBX-D20~D28과 WBX-D14에 더함; r0.3(2026-10-28)은 사용자 실제 Chrome 확인(WBX-A5 일부)의 피드백과 그에 따른 결정 WBX-D29~D35, 수락 기준 WBX-AC-21~23, 2026-10-27 OMP 로그인 유실 기록을 더함; r0.4(2026-10-28 오후)는 WBX-A6·B12·B4 재실행·B5·B8 뒤의 결정 WBX-D36~D40, WBX-AC-03 보충 2, WBX-A5 중간 결과와 B4 재실행 결과를 더함; r0.5(2026-10-29)는 WBX-A5 사용자 확인 결과, 한글 IME 수정 ticket WBX-A8, 트랙 D "Board workspace"의 결정 WBX-D41~D58과 수락 기준 WBX-AC-24~38을 더함; r0.6(2026-10-29 오후)은 트랙 E "Board workspace의 WB 프로젝트 pane"의 결정 WBX-D59~D67(Q56~Q61)과 수락 기준 WBX-AC-39~47, Board shell 기본 program 결정 WBX-D68·D69와 WBX-AC-48, 그리고 (A) D-08·D-09·P2-AC-21의 개정을 더함). 작성일: 2026-10-27.
WBX-D01~WBX-D16은 2026-10-27 요구사항 인터뷰(grilling)의 사용자 결정이며 사용자가 같은 날 각 결정을 확인했다. WBX-D17~D19는 같은 날 초안의 남은 확인(구 WBX-O03~O05)에 대한 사용자 답이다. WBX-D20~D28과 WBX-D14의 선택은 같은 날 SPEC s1의 확인 항목(U1~U10)에 대한 사용자 답이다. WBX-D29~D40은 2026-10-28 결정, WBX-D41~D69는 2026-10-29 결정이며 행마다 [사용자 결정] / [orchestrator 결정]을 표시했다. 문서 승인은 구현 권한과 별개다.
ID 접두사는 `WBX-`다. `WU-`는 [workbench-update](../workbench-update/BRIEF.md)(WU-AC-01~08, ticket WU-01~03), `UX-`는 dev-validation-log-retention의 cli-ux가 이미 쓴다.

이 문서는 [Task Inbox & Web Board BRIEF](../task-inbox/BRIEF.md)(이하 (A), D-05~D-36)와 [Hub BRIEF](../workbench-hub/BRIEF.md)(이하 (B))가 만든 Web Board 위에서, 사용성 개선과 외부 접점 교체를 다룬다. 여기서 바뀌는 기존 결정은 "개정되는 기존 결정"에 모으고, 기존 문서에는 이 문서를 가리키는 짧은 개정 주석만 단다. 관련 ADR: [ADR-0006](../../adr/0006-rest-skill-single-external-contact.md).

## 출처

- 사용자 PRD: [USER-PRD-20261027.md](USER-PRD-20261027.md)(원문 보존, 수정하지 않는다).
- PRD 항목과 결정의 대응: §1 디자인 → WBX-D12~D14. §2 진입 버튼 → WBX-D01. §3 뒤로 가기 경고 → WBX-D02, 평문 HTTP 배너 → WBX-D03. §4 외부 작업 요청 → WBX-D04~D11. PRD의 "MCP 설정 복붙"은 MCP 제거(WBX-D05)에 따라 WBX-D04로 대체되었다. PRD "주의점"의 "필요한 확장 기능은 필요시 구현"은 WBX-D16(제안으로만 기록, 승인 뒤 구현)으로 좁혔다.

## 문제, 사용자와 목표

- [사용자 PRD] 웹이 정돈되지 않았고 첫 화면이 dashboard처럼 보이지 않는다. Board agent·설정·API key·감사 기록으로 가는 버튼이 없어 주소를 직접 쳐야 한다. 평문 HTTP에서 뒤로 가기마다 Chrome 경고가 뜨고, 평문 HTTP 경고 배너를 닫을 수 없다.
- [사용자 PRD] 외부 agent가 API key로 보호된 경로로, 특별한 프로그램 없이 skill 하나로 요청을 등록할 수 있어야 한다. Skill 설치는 README 링크 하나로 끝나고 key는 env 파일로 관리한다. API 접근은 웹과 같은 규칙으로 허용·차단한다.
- [목표] (1) 어느 화면에서든 한 번에 주요 기능으로 간다. (2) 외부 접점을 REST API와 `wb-inbox` skill 하나로 줄인다(MCP 제거). (3) 손으로 쓴 CSS design system과 dashboard로 첫 화면을 바꾼다.
- [2026-10-29 사용자 요구, 트랙 D] `/board`에서 Board agent와 shell을 여러 개 띄워 원하는 자리·크기로 배치하고, 다른 기기에서도 같은 terminal을 함께 보고 입력한다.
- [2026-10-29 사용자 요구, 트랙 E] 같은 workspace에서 WB 프로젝트의 manager·worker·host pane도 widget으로 열어, TUI가 붙어 있든 없든 그 pane을 보고 입력하고 TUI의 `Ctrl-]` 명령(인수·넘기기·명령 중지·재시작·강제 종료)을 버튼으로 쓴다.

## 공통 용어

[CONTEXT.md](../../../CONTEXT.md)를 따른다. 특히 요청, 요청 출처, 요청 상태, API key, API key 등급, Board agent, Board agent 세트, Hub, 소유자, 대기 사유, 결과 요약 / 결과 전체 보기. 이 문서가 더하는 용어: 외부 요청 API, Workbench 사용자 skill, `wb-inbox` skill(CONTEXT.md에 추가).

- 전역 nav: 모든 로그인 화면의 header에 고정된 주요 화면 링크 묶음(WBX-D01).
- 평문 HTTP 표시: 경고 배너를 닫은 뒤 header에 남는 작은 "평문 HTTP" 표시(WBX-D03).
- (2026-10-29) Board workspace: 격자 widget으로 Board agent와 shell을 배치하는 `/board` 화면(WBX-D42).
- (2026-10-29) Terminal: Board workspace의 agent 하나 또는 shell 하나. Hub가 id·종류·이름·상태를 관리한다(SPEC s4 terminal registry).
- (2026-10-29) 공유 연결: 한 terminal에 여러 브라우저·기기의 연결이 함께 붙어 같은 출력을 보고 입력하는 방식. 이전의 한 연결 lease·인수를 대신한다(WBX-D54).
- (2026-10-29, 트랙 E) 프로젝트 pane widget: Board workspace에서 WB 프로젝트 하나의 manager·worker·host pane 하나를 보이는 widget. 제목은 "<프로젝트 이름> · manager"처럼 쓴다(WBX-D64). Hub가 만드는 terminal이 아니라 backend가 가진 pane을 Hub가 중계한다.
- (2026-10-29, 트랙 E) 공유 attach: backend ui_v1에 더하는 Hub 전용 attach. TUI가 쓰는 기존 attach 자리(하나)와 따로 있어 TUI와 함께 붙는다(WBX-D65).

## 범위와 비목표

- [포함, 트랙 A] 전역 nav(WBX-D01), cache 정책과 bfcache 복원 시 세션 확인(WBX-D02), 평문 HTTP 배너 닫기(WBX-D03), API key 발급 화면의 client 설정 안내(WBX-D04).
- [포함, 트랙 B] MCP 제거와 `/api/v1/` REST API(WBX-D05~D07), repo `skills/`의 `wb-inbox` skill과 Hub의 skill 제공(WBX-D08~D10), Board agent의 skill 전환(WBX-D11).
- [포함, 트랙 C] CSS design system(WBX-D12), dashboard `/`(WBX-D13; 2026-10-28부터 주소는 `/dashboard`, WBX-D29), 구현 전 mockup 선택(WBX-D14).
- [포함, 2026-10-28 추가] `/dashboard` 이동과 로그아웃 뒤 뒤로 가기 수정(WBX-D29·D34), agent별 skill 설치 README(WBX-D30·D35), 공유 OMP 로그인 완화(WBX-D32)와 probe 재실행(WBX-D33).
- [포함, 2026-10-29 트랙 A 보충] Board terminal의 한글 IME 입력 오류 수정(WBX-D41, ticket WBX-A8).
- [포함, 2026-10-29 트랙 D "Board workspace"] `/board`를 격자 widget workspace로 바꾼다(WBX-D42·D43·D50·D52·D57). Board agent 여러 개(WBX-D44)와 shell 여러 개(WBX-D45·D46), 기본 이름과 이름 바꾸기(WBX-D47·D48), 왼쪽 관리 panel(WBX-D49), 좁은 화면(WBX-D51), focus(WBX-D53), 한 연결 lease를 대신하는 공유 연결(WBX-D54·D55), 수명 세부(WBX-D56), agent별 신원(WBX-D58)을 다룬다.
- [트랙 D 제외] 단축키(WBX-D53), 끌기 library·CDN(WBX-D52), agent·shell 사이의 OS 권한 분리(WBX-D58, ADR-0003), Hub 재시작 뒤 shell 복원, 여러 브라우저 사이의 배치 공유(WBX-D50: 배치는 브라우저별), manager·worker·host terminal의 widget화((A) D-08). [2026-10-29 r0.6 개정: manager·worker·host pane의 widget화는 트랙 E가 다룬다(WBX-D59~D67). 트랙 D의 범위는 그대로다.]
- [계속 제외] (A) D-08의 제외 목록(웹의 manager·worker·host pane 조작, 파일 첨부, 외부 알림, OAuth 등), rate limit, CDN·CSS/JS framework, 새 runtime 의존성. API key로 terminal·승인 명세 연결·backend 제어(P2-AC-13·20). [2026-10-29 r0.6 개정: "웹의 manager·worker·host pane 조작"은 제외 목록에서 빠지고 트랙 E(WBX-D59~D67)가 된다. 나머지 제외는 그대로이고, API key로는 pane에도 닿지 않는다(WBX-AC-47).]
- [포함, 2026-10-29 트랙 E "Board workspace의 WB 프로젝트 pane"] 등록된 WB 프로젝트의 manager·worker·host pane을 Board workspace widget으로 보고 입력한다(WBX-D59·D61). TUI가 붙어 있어도 함께 쓰도록 backend ui_v1에 Hub 전용 공유 attach를 더한다(WBX-D59·D65, 기존 TUI 동작은 그대로). 크기 주인(WBX-D60), pane 동작 버튼(WBX-D62), 감사(WBX-D63), panel의 "프로젝트" 묶음과 프로젝트 화면의 "작업 공간에서 열기"(WBX-D64), Hub 중계(WBX-D66), 실패 동작(WBX-D67)을 다룬다.
- [트랙 E 제외] 웹에서 프로젝트 켜기·끄기·일시정지·재개(기존 프로젝트 화면 그대로, D-09), 웹의 `Ctrl-]` prefix와 TUI 단축키, backend focus 바꾸기(웹 focus는 브라우저 안에서만), keystroke 감사, TUI 화면에 웹 연결 표시, pane 사이 OS 권한 분리, 실행 중 backend의 자동 재시작(이전 판 backend는 소유자가 재시작한다, WBX-D67).
- [포함, 2026-10-29 Board shell] Board shell의 기본 program을 bash로(없으면 sh) 바꾼다(WBX-D68·D69, ticket WBX-D4).
- [제외] MCP와 REST의 병행 기간(WBX-D07), 별도 API 허용 목록(WBX-D06), GitHub 게시(사용자가 한다, WBX-D10), 이 목록 밖 기능의 구현(WBX-D16).

## 제약과 호환성

- [유지, (A)] API key 등급·scope(D-12, D-34), idempotency(P2-AC-02·03), 두 단계 취소(D-33), 입력 한도(D-36), 결과 공개 범위(D-22), key 원문 1회 표시(D-12), 웹 기본 비활성과 `[web] enabled`(D-10, 2026-10-26), `allow_from`(2026-10-27), CSP `script-src 'self'`(inline JS 없음), CDN 없음(P2-AC-37).
- [유지, (A)] Manager의 검토 결과 도구는 MCP가 아니라 bridge `workbench_inbox` → backend → hub_v1 Unix socket이다. 이 문서는 그 경로를 바꾸지 않는다.
- [유지, ADR-0003] Board agent는 sandbox 없이 사용자 권한으로 실행되고 bash가 있다. 보호는 로그인·bind·감사 기록에 의존한다.
- [기존 규칙, AGENTS.md] skill 관련 문서는 영어가 기본이다. `skills/README.md`, `SKILL.md`, `.env.example` 주석은 영어로 쓴다. Commit·게시는 별도 요청이 있어야 하고 push는 사용자가 한다.
- [기존 계약, D-29] 새 의존성을 더하지 않는다. `wb_inbox.py`는 Python 3 표준 라이브러리만 쓴다.
- [유지, 트랙 D] 모든 Board agent와 shell은 sandbox 없이 같은 OS 사용자로 돈다(ADR-0003). Terminal은 로그인한 소유자 세션만 열고 API key로는 열 수 없다(P2-AC-13·20, WBX-AC-08). OMP 로그인은 사용자 `agent.db`로의 symlink로만 공유하고 복사하지 않는다(C-D64, WBX-D32). `/board`의 CSP는 2026-10-26 결정(style nonce, `style-src-attr 'unsafe-inline'`) 그대로이고 `script-src 'self'`는 바뀌지 않는다.
- [호환, 트랙 D] 이전 판으로 되돌려도(`wb update` rollback) 기존 Board agent의 세션·메모리(`<hub state>/board-agent/`)는 그대로 쓰인다. `hub.sqlite3`의 schema version은 올리지 않는다(SPEC s4 "트랙 D").
- [유지, 트랙 E] 기존 TUI(`workbench-ui`)의 attach·화면·입력·`Ctrl-]` 명령·detach는 바뀌지 않는다. 두 번째 primary attach는 지금처럼 `attached_elsewhere`다. ui_v1 `VERSION`은 1 그대로이고 바뀜은 덧붙이기(새 request type·field·reason)뿐이다(WBX-D65). Backend의 입력 판정(`admit`)·host shell 입력 주인·`takeover_*`/`handoff`·lone Ctrl+C 규칙(ADR-0002, command-stop)·manager 입력창 대기는 그대로 쓴다(WBX-D61).
- [유지, 트랙 E] 보호는 Board와 같다: 로그인한 소유자 세션만, CSRF, `allow_from`·bind, 감사. API key로는 닿지 않는다(P2-AC-13·20, WBX-AC-08). Hub ↔ backend는 기존 `<runtime>/ui.sock`(0600, `SO_PEERCRED` 같은 uid)이고, 공유 attach는 확인된 실행 중 Hub process만 쓴다(WBX-D65).
- [호환, 트랙 E] `wb update` 뒤에도 이미 돌던 backend는 이전 판 code다. 그런 backend는 공유 attach를 모르므로 widget이 "backend 재시작 필요"를 보인다(WBX-D67). 새 판 Hub와 이전 판 backend, 이전 판 Hub와 새 판 backend 모두 기존 기능은 그대로 동작한다.

## 수락 기준

ID는 WBX-AC-nn이다. "관측"은 웹 화면, HTTP 응답(status·header·본문), `wb hub status`, 프로세스·파일, 테스트 결과다.

트랙 A

- WBX-AC-01 (WBX-D01): 로그인한 모든 화면의 header에 대시보드(`/`) · 요청(`/requests`, 새 요청 버튼) · Board agent(`/board`, 상태 점) · 설정(비밀번호 / API key / 감사 기록)이 있고 현재 화면이 강조된다. 오른쪽에 알림 badge와 로그아웃이 있다. `/board`, `/settings/keys`, `/settings/audit`, 비밀번호 변경은 어느 화면에서든 클릭 두 번 안에 닿는다. 좁은 화면에서는 JS 없이 한 줄로 접힌다. 로그인 화면에는 nav가 없다.
- WBX-AC-02 (WBX-D02): 로그인한 화면의 응답은 `Cache-Control: private, no-cache`다. API key 발급 결과 화면과 `/requests/{id}/raw`·`/full`·`/artifact`는 `no-store`를 유지한다.
- WBX-AC-03 (WBX-D02): 로그아웃·세션 만료 뒤 뒤로 가기로 bfcache에서 복원된 화면은 세션을 확인하고 로그인 화면으로 간다. 복원된 화면에서 상태 변경이 성공하지 않는다(기존 CSRF·세션 검사).
- WBX-AC-04 (WBX-D02): 사용자의 실제 Chrome(평문 HTTP, LAN 주소)에서 Workbench 화면 사이의 뒤로 가기가 매번 HTTPS-First 경고를 띄우지 않는다. 확인 때 Chrome "Always use secure connections" 설정 상태를 함께 기록한다. 효과가 없으면 알려진 한계로 남기고, 사용 안내는 TLS(`wb hub tls --self-signed`)를 근본 해결로 적는다.
- WBX-AC-05 (WBX-D03): 평문 HTTP 경고 배너의 닫기 버튼을 누르면 배너가 header의 "평문 HTTP" 표시로 접히고, hover하면 원래 경고 전문이 보인다. 같은 브라우저에서 새로 고침·다른 화면으로 가도 접힌 상태가 유지되고, 저장소를 지운 브라우저에서는 배너가 다시 펼쳐 보인다. 경고 조건이 있는 동안 배너나 표시 중 하나는 항상 보인다. `wb hub status`의 경고 줄은 바뀌지 않는다.
- WBX-AC-06 (WBX-D04): API key를 발급하면 결과 화면에 (1) `WB_HUB_URL`, `WB_API_KEY`가 든 `.env` 내용, (2) skill README 링크(Hub 제공 `/skill/README.md`, 그리고 `hub.toml`에 skill repo 주소가 설정된 경우에만 GitHub 링크, WBX-D19), (3) agent에게 붙여 넣을 한 줄 안내가 각각 복사 버튼과 함께 보인다. 한 줄 안내에는 key 원문이 없고 `wb-inbox` skill 설치(README 링크)와 `.env` 파일 위치만 있다(WBX-D17). `WB_HUB_URL` 기본값은 브라우저가 지금 쓰는 주소다. 새로 고침·뒤로 가기로 key 원문이 다시 보이지 않는다. Key 목록 화면은 같은 안내를 `<YOUR_KEY>` 자리표시로 보일 수 있다.
- WBX-AC-21 (WBX-D29): Dashboard의 주소는 `/dashboard`다. 로그인한 `GET /`는 `303 /dashboard`이고 세션 없는 `/`는 로그인 화면으로 간다. 로그인 성공, brand 링크, nav의 대시보드 링크, 돌아갈 곳이 없을 때의 기본 이동은 모두 `/dashboard`다. WBX-AC-01·AC-20의 "대시보드(`/`)"·"`/`에"는 `/dashboard`로 읽는다.
- WBX-AC-03 보충(WBX-D34, 2026-10-28): "뒤로 가기로 복원된 화면"에는 bfcache 복원(`pageshow.persisted`)과 HTTP cache에서 다시 그린 `back_forward` navigation이 모두 들어간다. 사용자 Chrome에서 로그아웃 뒤 뒤로 가기는 로그인 화면으로 끝난다.
- WBX-AC-03 보충 2(WBX-D36, 2026-10-28): 뒤로·앞으로 가기로 다시 보인 로그인 화면은 세션 확인이 답하기 전까지 내용을 보이지 않는다. 세션이 없으면 이전 내용을 한 번도 보이지 않고 로그인 화면으로 간다. 세션이 있거나 그 밖의 오류·시간 초과면 화면을 보인다. 일반 이동·새로 고침·로그인 화면·JS가 없는 browser는 바뀌지 않는다.
- WBX-AC-24 (WBX-D41, 2026-10-29): Board terminal(agent·shell)에서 한글 IME로 "안녕하세요"를 입력하면 그 terminal의 program에 "안녕하세요"가 그대로 도착한다(글자 순서·반복 오류 없음). 영문·붙여넣기 입력은 바뀌지 않는다. 마지막 확인은 사용자의 실제 Chrome과 실제 IME다.

트랙 B

- WBX-AC-07 (WBX-D05): `/api/v1/`의 endpoint로 MCP tool 9개가 하던 일(프로젝트 목록, 제출, 목록, 조회, 댓글, 편집, 상태 전환, 취소, 결과)을 모두 할 수 있고, 같은 입력에 같은 기록·권한 판정·출처 표시가 나온다. 같은 `idempotency_key`의 재제출은 같은 요청 ID를, 내용이 다르면 충돌을 돌려준다.
- WBX-AC-08 (WBX-D05): 인증은 `Authorization: Bearer <key>`뿐이다. Header가 없거나 틀리거나 폐기·만료된 key는 거절되고, 로그인 세션 cookie만 있는 요청도 거절된다. requester key의 편집·상태 전환은 거절된다. 어떤 key로도 terminal·승인 명세 연결·backend 제어·검토 시작에 닿는 endpoint가 없다.
- WBX-AC-09 (WBX-D05): 오류는 HTTP status와 `{"error":{"code","message"}}`이고 `code`는 기존 오류 코드다. 한도를 넘는 입력은 이유와 함께 거절되고 아무것도 저장되지 않는다(P2-AC-44).
- WBX-AC-10 (WBX-D05): RUNNING 요청의 `POST /api/v1/requests/{id}/cancel` 첫 호출은 취소하지 않고 token과 영향 요약을 돌려준다. 같은 token의 확인만 P2-AC-30의 취소 경로를 탄다. Token이 없거나 틀리거나 만료됐거나 그 사이 상태가 바뀌었으면 거절되고 아무것도 멈추지 않는다(P2-AC-41).
- WBX-AC-11 (WBX-D06): `allow_from` 밖 주소의 `/api/v1/`·`/skill/` 요청은 화면과 같은 본문 없는 403이고, bind하지 않은 주소에서는 열리지 않는다. `[web] enabled = false`면 REST와 skill 제공도 열리지 않는다. REST 전용 허용 목록 설정은 없다.
- WBX-AC-12 (WBX-D07): 외부 listener와 Board agent 내부 listener 모두 `/mcp`를 제공하지 않는다. 기존에 발급한 API key는 다시 발급하지 않아도 REST에서 같은 등급·scope로 동작한다. 테스트 suite에 MCP client가 남지 않고, 기존 MCP 테스트가 검증하던 동작은 REST 테스트로 옮겨져 통과한다.
- WBX-AC-13 (WBX-D08): repo에 `skills/README.md`, `skills/wb-inbox/SKILL.md`, `skills/wb-inbox/wb_inbox.py`, `skills/wb-inbox/.env.example`이 있다. README 첫 줄은 이 폴더가 사용자용 skill이며 개발용 `.agents/skills/`와 다르다고 밝히고, 설치 안내와 curl 예를 담는다. `wb_inbox.py`는 pip 설치 없이 Python 3 표준 라이브러리로 실행되며, key 등급이 허용하는 모든 동작(requester: projects·submit·list·get·comment·cancel·result, editor: + update·set_state)을 한다.
- WBX-AC-14 (WBX-D09): `wb_inbox.py`는 `WB_HUB_URL`·`WB_API_KEY`를 process env → skill 폴더 `.env` → `~/.config/omp-workbench/client.env` 순서로 찾는다. 읽은 `.env`의 권한이 0600보다 넓으면 경고한다. Key 값은 stdout·stderr·log·오류 메시지 어디에도 나오지 않는다. 값이 없으면 찾은 위치와 변수 이름을 알려 주고 실패한다.
- WBX-AC-15 (WBX-D10): Hub는 로그인 없이 `/skill/README.md`와 `/skill/wb-inbox/...`로 설치본에 포함된 같은 skill 파일을 제공한다. `WB_HUB_URL`은 요청에 쓰인 주소로 채워지고 나머지 내용은 설치본 파일과 같다. 제공되는 파일에 key는 없다. WBX-AC-11의 규칙을 따른다.
- WBX-AC-16 (WBX-D11): Board agent는 Hub 내부 loopback listener의 REST를 `wb-inbox` skill과 bash로 쓴다. 시작마다 새 system editor key를 env `WB_API_KEY`로 받는다. Board agent의 tool 목록에 MCP tool이 하나도 없고, skill은 `wb-inbox` 하나뿐이다. 다른 skill이나 MCP가 보이면 isolation 검사가 시작을 거절한다. `allow_from`에서 loopback을 빼도 Board agent의 요청 작업은 끊기지 않는다. Manager·worker의 기존 MCP 누출 검사는 계속 통과한다.
- WBX-AC-17 (WBX-D11): 전환 전 probe 기록이 있다: Board overlay 아래 OMP가 `wb-inbox` skill을 읽고 bash로 REST를 호출해 요청 목록을 받는다. Probe가 실패하면 전환하지 않고 사용자에게 묻는다. 2026-10-28 보충(WBX-D33): 이 기록은 WBX-B12 뒤 사용자의 실제 로그인으로 다시 실행한 결과이며, 그 실행의 OMP log가 남아 있다.
- WBX-AC-22 (WBX-D30·D35): Hub가 제공하는 `/skill/README.md`만 받은 agent가 그 안의 명령으로 `wb-inbox` skill의 모든 파일(`SKILL.md`, `wb_inbox.py`, `.env.example`)을 내려받아 설치할 수 있다. README에는 Claude Code·Codex·OMP 각각의 절(skill 폴더, 내려받기 명령, 근거 문서)이 있다. Hub 제공본의 파일 주소는 그 Hub의 절대 `/skill/` 주소이고, repo(GitHub) 판은 repo 파일로 가는 상대 링크다. Key 발급 화면의 agent용 한 줄 안내는 README의 agent별 절을 따르라고 말하고 key 원문은 담지 않는다(WBX-D17·D26 유지).
- WBX-AC-23 (WBX-D32): (1) 격리 확인용 OMP는 바로 SIGTERM·SIGKILL되지 않고 상한이 있는 유예 안에서 스스로 끝날 기회를 받는다. (2) Board agent와 그 격리 확인 OMP의 log가 Hub 상태 디렉터리에 크기·개수 상한을 두고 다음 시작 뒤에도 남는다(system key 값은 없다). (3) Repo의 test helper·도구 어디에도 사용자 `agent.db`를 복사하는 코드가 없다(공유가 필요하면 C-D64의 symlink).

트랙 C

- WBX-AC-18 (WBX-D14): Dashboard와 로그인 화면의 정적 HTML mockup 2~3개가 있고, 사용자의 선택이 기록된 뒤에 트랙 C 구현이 시작된다.
- WBX-AC-19 (WBX-D12): 로그인 화면을 포함한 모든 화면이 같은 색·간격·글자 token과 table·card·badge·form·button 구성 요소를 쓴다. Light·dark가 모두 있다. 외부 CDN·framework 요청이 없고 CSP는 그대로다.
- WBX-AC-20 (WBX-D13): `/`에 요약 card(실행 중·꺼진 프로젝트 수, NEW·REVIEWING·NEEDS_INFO·RUNNING별 요청 수, "내 답이 필요한 요청"(WBX-D18의 정의)), 프로젝트 표(상태, 대기 사유, 검토 방식, 바로가기), 최근 요청 10개, Board agent 상태 card와 "열기" 버튼이 있다. `<meta http-equiv=refresh>`가 없고, 숫자와 표는 JS가 JSON을 받아 화면 전체를 다시 읽지 않고 갱신한다. 세션이 없으면 로그인 화면으로 간다.

트랙 D (2026-10-29, Board workspace)

- WBX-AC-25 (WBX-D42·D43·D50·D52·D57): `/board`에서 Board agent와 shell이 모두 widget으로 보인다. 제목 막대를 끌면 옮겨지고 모서리를 끌면 크기가 바뀌며, 놓은 위치·크기는 격자 칸에 맞춰진다. 다른 widget과 겹치는 자리에 놓으면 놓이지 않고 마지막 유효 자리로 돌아간다. 위치·크기·보이기는 같은 브라우저의 새로 고침·재방문 뒤에도 그대로이고, 저장소를 지웠거나 쓸 수 없는 브라우저에서는 기본 배치로 보인다. 새 library·외부 URL·inline script가 없고 CSP는 바뀌지 않는다.
- WBX-AC-26 (WBX-D49): 왼쪽 관리 panel을 접고 펼 수 있고 그 상태가 브라우저별로 기억된다. Panel에는 agent마다 상태 점·보이기/숨기기·시작/중지·이름 바꾸기·삭제, shell마다 이름 바꾸기·보이기/숨기기·닫기, "새 Board agent"·"새 shell", 배치 초기화, 한도 설정이 있다. 중지·삭제·닫기·재시작은 확인을 거친 뒤에만 일어난다.
- WBX-AC-27 (WBX-D44·D56): 한도(기본 4) 안에서 Board agent를 더 만들 수 있고, 한도에 닿으면 더 만들 수 없다는 이유가 보인다. Agent마다 따로 OMP process가 돌고 세션·메모리 디렉터리가 따로이며, 모두 같은 OMP 로그인(사용자 `agent.db`로의 symlink)·role prompt·`[board_agent]` model·격리 확인을 쓴다. 한 agent의 시작 실패·격리 실패는 다른 agent와 shell에 영향이 없다. 중지한 agent는 메모리가 남고 다시 열면 이전 세션을 잇는다(`--continue`). 삭제한 agent의 home은 없어지고 사용자 `agent.db`와 다른 agent의 home은 그대로다. 같은 Hub run 안에서 중지한 agent는 page를 다시 열어도 저절로 시작하지 않는다.
- WBX-AC-28 (WBX-D44·D48·D58): Agent마다 시작마다 새 system key를 env `WB_API_KEY`로 받고, 다른 agent·shell의 env에는 그 key가 없다. 내부 listener는 실행 중인 agent 각자의 이번 시작 key만 받는다(멈춘·삭제한 agent의 key, 이전 시작의 key, 사람이 발급한 key는 401). Agent가 낸 요청의 출처는 "Board agent · <제출 시점의 이름>"으로 보이고, 이름을 나중에 바꿔도 이미 낸 요청의 출처는 바뀌지 않는다.
- WBX-AC-29 (WBX-D45·D56): 한도(기본 10) 안에서 shell을 더 만들 수 있다. Shell마다 따로 process(sandbox 없음)이고, 닫으면 확인 뒤 그 shell의 process group이 끝나고 목록에서 빠진다. 브라우저를 닫았다 열거나 네트워크가 끊겼다 이어져도 shell은 그대로 돈다. Hub가 다시 시작하면 shell은 모두 끝나고, 그 run에서 처음 `/board`를 열면 `shell-1` 하나가 생긴다.
- WBX-AC-30 (WBX-D47·D48): 새 agent·shell은 이름을 묻지 않고 `agent-N`·`shell-N`(같은 종류에서 쓰이지 않은 가장 작은 N)으로 생긴다. Panel에서 이름을 바꾸면(예: `전처리`) 모든 브라우저의 panel과 widget 제목에 같은 이름이 보인다. 앞뒤 공백을 뺀 1~40자가 아니거나 제어 문자가 있거나 같은 종류에 대소문자만 다른 같은 이름이 있으면 거절되고 이유가 보인다.
- WBX-AC-31 (WBX-D44·D45·D46): Agent 한도는 1~8, shell 한도는 1~32 안에서 Board 화면에서 바꿀 수 있고, 바꾼 값은 Hub에 저장되어 다른 브라우저와 Hub 재시작 뒤에도 같다. 범위 밖 값과 지금 개수보다 작은 값은 거절된다.
- WBX-AC-32 (WBX-D54·D55): 두 브라우저(또는 기기·tab)가 같은 terminal을 열면 둘 다 같은 출력(이전 출력 replay와 이후 출력)을 보고 둘 다 입력할 수 있다. 입력은 Hub에 도착한 순서대로 program에 간다. 어느 쪽도 인수당하지 않으며 4001·"다시 가져오기"가 없다. Widget에 "다른 화면 N개 연결됨"(N = 같은 terminal에 붙은 다른 연결 수)이 보인다. 한 terminal에 9번째 연결은 거절되고 한도 안내가 보이며 자동으로 다시 시도하지 않는다. 네트워크로 끊긴 연결은 저절로 다시 붙는다.
- WBX-AC-33 (WBX-D54): Terminal 크기는 가장 최근에 입력하거나 화면 크기를 바꾼 연결의 크기를 따른다. 다른 연결은 그 크기로 그리며 남는 자리는 비우고(letterbox) 모자라면 scroll한다. 새로 붙는 연결은 혼자일 때만 크기를 정한다. 두 연결이 서로 크기를 계속 바꾸는 반복이 없다.
- WBX-AC-34 (WBX-D44·D45·D54): 감사 기록에 연결별 terminal 연결·해제(terminal id·종류·이름, 그 terminal의 연결 수), terminal 열기·닫기(shell 생성·닫기, agent 생성·삭제), 시작·중지·재시작, 이름 바꾸기, 한도 변경이 남는다. Terminal 입출력과 key 값은 남지 않는다.
- WBX-AC-35 (WBX-D51): 화면 폭이 720 px 미만이면 격자가 없고, panel에서 고른 terminal 하나가 화면 전체로 보이며 그 terminal만 연결된다. 폭이 다시 넓어지면 저장된 배치로 돌아간다.
- WBX-AC-36 (WBX-D53): Widget을 클릭하면 그 widget이 focus를 얻고 강조 테두리가 보이며 입력이 그 terminal로 간다. 새 단축키는 없다.
- WBX-AC-37 (WBX-D44·D48): 업데이트 뒤 기존 Board agent는 `agent-1`로 보이고 이전 세션·메모리를 잇는다(home 위치는 그대로 `<hub state>/board-agent/`). 첫 shell은 `shell-1`이다. 이전 판으로 되돌려도 `agent-1`의 세션·메모리가 그대로 쓰인다.
- WBX-AC-38 (WBX-D54·D58, ADR-0003): 모든 Board 화면·동작·terminal 연결은 로그인한 세션만 쓸 수 있고, 상태를 바꾸는 동작은 CSRF 검사를 거친다. API key(Bearer)로는 어떤 Board 동작·terminal 연결에도 닿지 않는다(401). Terminal 이름은 HTML·script로 해석되지 않는다. `wb hub status`와 `wb hub stop`·`wb update`의 확인 목록에 실행 중인 모든 agent·shell이 이름과 함께 보이고, 확인하면 모두 끝난 뒤 Hub가 멈춘다.

트랙 E (2026-10-29, Board workspace의 WB 프로젝트 pane)

- WBX-AC-39 (WBX-D59·D61·D65·D66): 켜진 프로젝트 P의 manager widget을 열면 그 pane의 이전 출력(replay)과 이후 출력이 보이고, widget에서 친 글자가 manager OMP에 도착한다. P에 TUI가 붙어 있는 동안과 없는 동안 모두 같고, worker·host widget도 같다. 같은 pane widget을 두 브라우저에서 열면 둘 다 같은 출력을 보고 입력할 수 있다(입력은 Hub에 도착한 순서). TUI 쪽은 웹이 붙어 있는 동안에도 화면·입력·`Ctrl-]` 명령·detach·다시 attach가 이전과 같고, 두 번째 TUI attach는 지금처럼 `attached_elsewhere`다.
- WBX-AC-40 (WBX-D65): 공유 attach는 hello 이름 `workbench-hub`, backend와 같은 peer uid, 확인된 실행 중 Hub process 세 조건이 모두 맞을 때만 받아들여지고, 하나라도 틀리면 거절되며 attach 자리·화면 전송은 바뀌지 않는다. 공유 attach가 읽지 않아 송신 한도를 넘으면 그 연결만 `slow_client`로 닫히고 TUI의 연결·화면과 pane process는 영향이 없다. 공유 attach를 모르는 이전 판 backend는 `unsupported_type`으로 답하고 TUI attach 자리를 빼앗지 않는다.
- WBX-AC-41 (WBX-D60): TUI가 붙어 있는 동안 웹은 pane 크기를 바꾸지 않는다(웹이 보낸 크기 변경은 적용되지 않음). 웹 widget은 그 pane의 크기로 그리고 남으면 비우고(letterbox) 모자라면 scroll한다. TUI가 없으면 pane마다 가장 최근에 입력하거나 화면 크기를 바꾼 웹 연결의 크기를 따른다(WBX-AC-33과 같은 규칙). TUI가 다시 붙으면 TUI 크기로 돌아간다. 웹과 TUI, 두 웹 연결 사이에 크기를 서로 바꾸는 반복이 없다.
- WBX-AC-42 (WBX-D61): Manager·worker widget에서는 어떤 글자든 그대로 간다. 단 Ctrl-d는 TUI처럼 첫 번째를 보내지 않고 "2초 안에 한 번 더" 안내를 보이며, 2초 안의 두 번째 Ctrl-d만 보내고, 다른 키가 오면 첫 Ctrl-d는 버린다. Host widget의 입력이 backend 규칙으로 거절되면(`host_shell_automation`, `input_owner_manager`, `input_target_unknown` 등) 그 widget에 이유 문장이 보이고 아무것도 shell에 가지 않는다. 확인된 foreground Workbench 명령으로 가는 lone Ctrl+C는 TUI와 같이 전달되고 backend journal에 남는다. 웹에서 manager 입력창에 글을 쓰면 자동 전달이 TUI 때와 같이 기다린다(대기 사유 표시). 웹에는 `Ctrl-]` prefix가 없고 그 byte는 친 그대로 간다.
- WBX-AC-43 (WBX-D62): Host widget에 인수 요청·인수 확인·넘기기 버튼, 실행 중인 Workbench 명령이 있을 때 "명령 중지"(확인 dialog에 명령 이름, 확인하면 그 명령 id만 중지), 끝난 pane에 "재시작", host widget에 "강제 종료"(확인 dialog)가 있다. 결과(성공·거절 이유, 명령 중지의 최종 결과)가 widget에 보인다. Dialog에서 취소하면 아무것도 바뀌지 않는다. 지금 상태에서 할 수 없는 동작의 버튼은 비활성이다. 인수·넘기기 결과(입력 주인)는 TUI와 모든 웹 widget에 같게 보인다.
- WBX-AC-44 (WBX-D63): 감사 기록에 웹 pane 연결·해제(연결별, 프로젝트 id·이름·pane·연결 수), 인수 요청·확인·넘기기, 명령 중지(명령 id, 최종 결과), 강제 종료, 재시작이 결과(성공·거절 이유)와 함께 남는다. 입력한 글자·붙여넣기 내용·byte 수는 남지 않는다.
- WBX-AC-45 (WBX-D64): Board 왼쪽 panel에 "프로젝트" 묶음이 있고 등록된 프로젝트마다 이름·켜짐 상태와 manager·worker·host widget 추가 버튼이 있다. 추가한 widget의 제목은 "<프로젝트 이름> · manager"(worker, host)다. Widget의 ×와 숨기기는 이 브라우저에서 감출 뿐 pane과 process에 아무것도 하지 않는다. 프로젝트 화면의 "작업 공간에서 열기"를 누르면 `/board`에서 그 프로젝트의 widget이 보인다. 배치 기억(WBX-AC-25)·좁은 화면(WBX-AC-35)·focus(WBX-AC-36)는 pane widget에도 같다.
- WBX-AC-46 (WBX-D66·D67): 프로젝트가 꺼져 있으면 widget은 "꺼짐"과 프로젝트 화면 링크를 보이고 입력을 받지 않는다. Backend가 다시 시작되면 widget은 화면을 지우고 새 backend의 replay로 저절로 다시 붙는다. Hub가 다시 시작돼도 다시 붙는다. 이전 판 backend면 "backend 재시작 필요"를 보이고 되풀이해 시도하지 않는다. 등록이 지워진 프로젝트의 widget은 사라진다. 한 pane에 9번째 웹 연결은 거절되고, 한 웹 연결이 느려 끊겨도 같은 pane의 다른 연결과 TUI는 영향이 없다.
- WBX-AC-47 (WBX-D59·D65·D66): Pane widget 연결과 pane 동작은 로그인한 세션만 쓸 수 있고 CSRF 검사를 거친다. API key(Bearer)로는 401이고 `/api/v1/`·내부 listener에 pane 동작이 없다. 모르는 프로젝트·잘못된 pane 이름은 연결 전에 거절된다. `[web] enabled = false`나 `allow_from` 밖에서는 열리지 않는다. 프로젝트 이름은 HTML·script로 해석되지 않는다.

Board shell (2026-10-29)

- WBX-AC-48 (WBX-D68·D69): `[board_agent] shell`이 비어 있으면 새 Board shell은 bash(`/bin/bash`, `/usr/bin/bash`, Hub `PATH`의 `bash` 순서)이고, bash가 없으면 `/bin/sh`다. `$SHELL`이 무엇이든(없음, zsh, fish) 결과는 같다. `shell`이 설정되어 있으면 그 절대 경로만 쓴다. 이미 돌던 shell은 바뀌지 않는다.

## 결정과 근거

WBX-D01~D28은 [사용자 결정 2026-10-27]이다. WBX-D29~D40은 2026-10-28 결정, WBX-D41~D69는 2026-10-29 결정이며 각 행 앞에 출처를 표시했다(r0.4: D36~D40, r0.5: D41~D58, r0.6: D59~D69). 2026-10-29의 Q 번호는 그날 사용자에게 물은 질문 번호다. "개정"은 이 결정이 바꾸는 기존 결정이다.

| ID | 결정 | 개정 |
|---|---|---|
| WBX-D01 | 모든 화면 header에 전역 nav를 고정하고 현재 화면을 강조한다: 대시보드(`/`) · 요청(`/requests`, 새 요청 버튼) · Board agent(`/board`, 상태 점) · 설정(비밀번호 / API key / 감사 기록). 오른쪽에 알림 badge와 로그아웃. 프로젝트는 dashboard에서 들어가고 별도 메뉴가 없다. 좁은 화면은 CSS만으로 한 줄로 접는다. | — (D-24 badge의 위치만 정함) |
| WBX-D02 | 로그인한 화면은 `Cache-Control: private, no-cache`로 바꾸고, key 발급 결과와 요청 raw/full/artifact는 `no-store`를 유지한다. bfcache에서 복원되면 JS가 세션을 확인하고 없으면 로그인 화면으로 간다. 효과는 사용자의 실제 Chrome에서 확인하고("Always use secure connections" 설정도 확인), 사용 안내에 TLS가 근본 해결임을 적는다. | 현행 구현(모든 비정적 응답 `no-store`)의 정책 변경 |
| WBX-D03 | 평문 HTTP 배너에 닫기 버튼을 두고, 닫으면 header의 작은 "평문 HTTP" 표시로 접는다(hover로 전문). 닫은 상태는 브라우저별로 기억한다(localStorage). `wb hub status` 경고는 그대로다. 경고를 완전히 숨기지 않는다. | D-10·P2-AC-16의 웹 경고 표시 방식 보완(경고는 계속 보임) |
| WBX-D04 | Key 발급 화면은 발급 때만(원문 1회 표시, D-12) `.env`(`WB_HUB_URL`, `WB_API_KEY`), skill README 링크(Hub 제공, GitHub는 WBX-D19 조건), agent에게 줄 한 줄 안내(key 원문 없음, WBX-D17)를 복사 버튼과 함께 보인다. `WB_HUB_URL` 기본값은 브라우저가 쓰는 주소다. Key 목록은 `<YOUR_KEY>`로 같은 template을 보일 수 있다. | PRD의 "MCP 설정 복붙"을 대체 |
| WBX-D05 | MCP를 모두 없앤다(외부 `/mcp`와 Board agent용). 외부 접점은 `/api/v1/` REST JSON API 하나이며, MCP tool 9개의 기능을 같은 내부 service로 제공한다. Resource: `GET /projects`; `POST·GET /requests`, `GET /requests/{id}`; `POST /requests/{id}/comments`; `PATCH /requests/{id}`(editor); `POST /requests/{id}/state`(editor); `POST /requests/{id}/cancel`(두 단계 token); `GET /requests/{id}/result`. 인증은 `Authorization: Bearer <key>`뿐이고 세션 cookie는 받지 않는다. 오류는 기존 코드를 `{"error":{"code","message"}}`와 HTTP status로 돌려준다. 등급·한도·idempotency·두 단계 취소·scope 규칙은 그대로다. 기존 세션 route `/api/badge`, `/api/projects/...`는 바꾸지 않는다. Field 세부는 SPEC. | (A) D-06, "제약과 호환성"의 외부 접점 줄(D-14·D-29), D-22, D-29, D-33, D-36, P2-AC-05·06·31·37·41·44·45; ADR-0004:18, ADR-0005:12의 MCP 문구 |
| WBX-D06 | REST와 skill 내려받기는 웹과 같은 `allow_from`·bind 규칙을 따른다. 별도 API 허용 목록은 없다. `[web] enabled = false`면 REST도 닫힌다. | — (D-10, `allow_from`을 REST에 그대로 적용) |
| WBX-D07 | 이행 기간 없이 MCP를 바로 없앤다. 기존 API key는 REST에서 그대로 동작한다. MCP 테스트는 REST로 옮긴다. | P2-AC-45 |
| WBX-D08 | Repo의 skill 배치: `skills/README.md`(Workbench 사용자 skill 전체 소개와 설치 안내, 첫 줄에 개발용 `.agents/skills/`와 다름을 밝힘, curl 예 포함), `skills/wb-inbox/SKILL.md`(Agent Skills 표준, Claude Code·Codex·OMP가 읽음), `skills/wb-inbox/wb_inbox.py`(Python 3 표준 라이브러리 단일 파일), `skills/wb-inbox/.env.example`. Skill 범위는 key 등급이 허용하는 전부(requester: submit/list/get/comment/cancel/result/projects, editor: + update/set_state). | — |
| WBX-D09 | Env는 `WB_HUB_URL`, `WB_API_KEY`. 찾는 순서는 process env → skill 폴더 `.env` → `~/.config/omp-workbench/client.env`. `.env` 권한이 0600보다 넓으면 경고하고, key를 출력·기록하지 않는다. `.env.example`과 .gitignore 안내를 둔다. | — |
| WBX-D10 | Hub는 설치본에 포함된 같은 skill 파일을 `/skill/README.md`, `/skill/wb-inbox/...`로 로그인 없이(agent에게는 세션이 없음) 제공하되 `allow_from`·bind를 따른다. 제공할 때 `WB_HUB_URL`을 채운다. GitHub 게시는 사용자가 한다(push 규칙). | — |
| WBX-D11 | Board agent는 계속 Hub 내부 loopback listener에 붙고, 그 listener는 `/mcp` 대신 같은 REST를 제공한다(`allow_from` 변경이 Board agent를 끊지 않음). 시작마다 system key를 주고 env 이름은 `WB_API_KEY`가 된다. Board agent에 허용하는 skill은 `wb-inbox` 하나이고 MCP는 전혀 없다. isolation 검사를 그에 맞게 바꾼다. 전환 전에 OMP가 Board overlay 아래에서 skill을 읽고 bash로 REST를 호출하는지 짧은 probe로 확인한다. 실패하면 멈추고 사용자에게 묻는다(대안인 "MCP를 Board 전용 내부 통로로 유지"는 결정하지 않았다). | (A) D-08(Board agent의 Hub MCP), D-25, P2-AC-33; ADR-0003 결정 4번째 항목 |
| WBX-D12 | 손으로 쓴 CSS design system: 색·간격·글자 token, light·dark, table·card·badge·form·button 구성 요소. CDN·framework 없음. 로그인 화면을 포함한 모든 화면에 적용한다. | — |
| WBX-D13 | Dashboard `/`: 요약 card(프로젝트 실행 중·꺼짐; 요청 NEW/REVIEWING/NEEDS_INFO/RUNNING 수; "내 답이 필요한 요청"(WBX-D18)), 프로젝트 표(상태, 대기 사유, 검토 방식, 바로가기), 최근 요청 10개, Board agent 상태 card와 "열기" 버튼. 10초 meta refresh 대신 JS가 JSON을 받아 숫자와 표를 갱신한다. | 현행 `/` 프로젝트 표와 10초 meta refresh 대체(HB-D09 항목은 표에 유지) |
| WBX-D14 | 구현 전에 dashboard와 로그인 화면만의 정적 HTML mockup 2~3개를 만들고 사용자가 고른다. **선택(2026-10-27): 방향 A(차분한 중립 admin console, [mockups/a/](mockups/a/)). WBX-C1은 `mockups/a/style.css`에서 시작한다(구 SPEC U10).** | — |
| WBX-D15 | 문서는 BRIEF 하나이고 ticket은 트랙 A/B/C로 나눈다. A와 B는 병행하고, C의 mockup 선택은 A 진행 중에 받는다(A와 C가 모두 `render.py`를 고친다). | — |
| WBX-D16 | 이 목록 밖 기능은 "제안"으로만 기록하고 사용자 승인 뒤 구현한다. 현재 제안: "내 답이 필요한 요청"의 dashboard 알림. | — |
| WBX-D17 | Key 발급 화면의 agent용 한 줄 안내에는 key 원문을 넣지 않는다. `wb-inbox` skill을 설치하라는 말(README 링크)과 `.env` 파일이 놓일 곳만 담는다. Key가 든 `.env` 블록은 소유자가 복사 버튼으로 그 파일에 직접 붙여 넣는다. Key가 agent 대화 기록에 남지 않게 하기 위해서다(구 WBX-O03). | WBX-D04 보완 |
| WBX-D18 | "내 답이 필요한 요청"은 NEEDS_INFO 요청과, 재시도 한도 소진 뒤 소유자의 결정을 기다리는 요청(P2-AC-27)을 합친 것이다(구 WBX-O05). | WBX-D13 보완 |
| WBX-D19 | GitHub skill 링크는 `hub.toml`의 선택 설정(예: `skill_repo_url`, 정확한 key 이름은 SPEC)이 있을 때만 key 발급 화면과 Hub 제공 README에 보인다. 없으면 Hub가 제공하는 `/skill/` 링크만 보인다(구 WBX-O04). | WBX-D04·D10 보완 |
| WBX-D20 | SPEC s1 "테스트 결정"의 test seam S1~S7을 쓰인 그대로 확정한다(구 SPEC U1). | — |
| WBX-D21 | 화면 JS의 logic은 기존 node seam(`tests/hub/test_board_state_js.py`가 `tests/hub/js/*.test.mjs`를 `node --test`로 실행, node 없으면 skip)으로 시험하고, 실제 browser 동작은 사용자 Chrome(S7)으로 확인한다. 새 도구는 없다(구 SPEC U2). | — |
| WBX-D22 | `/`의 10초 refresh는 트랙 C(WBX-D13)까지 둔다. WBX-O01 Chrome 확인은 트랙 A 뒤에 하고, 자동 갱신이 없는 화면(요청 목록·상세 등) 사이의 뒤로 가기로 시험한다(구 SPEC U3). | WBX-AC-04 확인 범위 |
| WBX-D23 | 자체 서명 TLS Hub에 대한 `wb_inbox.py` 접속은 OpenSSL 표준 `SSL_CERT_FILE` 안내만 하고 새 변수를 두지 않는다(구 SPEC U4). | WBX-D09 보완 |
| WBX-D24 | Board agent 내부 listener는 이번 시작의 system key만 받는다(다른 유효 API key는 401)(구 SPEC U5). | 현행 내부 `/mcp`(모든 유효 key) 변경 |
| WBX-D25 | `wb_inbox.py submit`에 idempotency key가 없으면 내용에서 만든다(같은 내용의 재시도는 같은 요청). `--new`는 새 요청을 강제한다(구 SPEC U6). | — |
| WBX-D26 | Key 발급 화면의 agent용 한 줄 안내는 영어로 쓴다(구 SPEC U7). | WBX-D17 보완 |
| WBX-D27 | WBX-O02 probe는 이 host의 기존 OMP 로그인으로 실제 모델을 한 번 호출한다(작은 비용 수용)(구 SPEC U8). | — |
| WBX-D28 | Hub는 물려받은 `WB_API_KEY`·`WB_HUB_URL`을 지우므로 Hub가 켠 manager·worker에도 그 값이 없다. 이를 수용한다(구 SPEC U9). | — |
| WBX-D29 | [사용자 결정 2026-10-28] `/`는 `303 /dashboard`로 보내고 dashboard는 `/dashboard`에 둔다. 로그인 뒤 도착, brand·nav의 대시보드 링크, 기본 돌아갈 곳이 `/dashboard`다. 계기: 실제 Chrome에서 dashboard `/`로 뒤로 가기마다 HTTPS-First 경고가 떴다. | WBX-D01·D13의 dashboard 주소 `/`(AC-01·20) |
| WBX-D30 | [사용자 결정 2026-10-28] Hub가 제공하는 skill README만으로 agent가 `SKILL.md`·`wb_inbox.py`를 포함한 skill의 모든 파일을 내려받아 설치할 수 있어야 하고, README는 Claude Code·Codex·OMP 각각의 설치를 설명한다. GitHub에서 읽을 때는 repo의 파일로 링크된다. 계기: 2026-10-28 확인에서 `/skill/README.md`를 받은 agent가 `SKILL.md`·`wb_inbox.py`를 가져오지 못했다. | WBX-D08(README 내용), WBX-D10 |
| WBX-D31 | [사용자 결정 2026-10-28, 구 Q36] 예전 `~/wb-demo` backend를 멈춘다. 2026-10-28 수행: `wb down` 뒤 멈춤 확인, 그 backend가 쓰던 인증 사본 삭제. | — |
| WBX-D32 | [사용자 결정 2026-10-28, 구 Q37] 공유 OMP 로그인을 지키는 완화 ticket(WBX-B12)을 더한다: 격리 확인 OMP의 graceful 종료, Board OMP log 보존, test helper가 `agent.db`를 복사하지 않음. 근거: [INCIDENT-20261027-omp-login.md](INCIDENT-20261027-omp-login.md). | C-D64 보강(symlink 공유 원칙의 test 쪽 적용) |
| WBX-D33 | [사용자 결정 2026-10-28, 구 Q38] WBX-B12 뒤 WBX-B4 probe를 사용자의 실제 로그인으로 다시 실행하고 OMP log를 남긴다. Passed면 WBX-B5 → B8 → B9를 계속한다. Failed·inconclusive면 기존 규칙대로 멈추고 묻는다. | WBX-D27 보완(재실행 조건), PROBE의 "다시 돌리지 않는다" 주의를 해제하는 조건 |
| WBX-D34 | [orchestrator 결정 2026-10-28] 로그아웃 뒤 뒤로 가기 수정: 세션 확인을 `pageshow.persisted`뿐 아니라 `back_forward` navigation(HTTP cache에서 다시 그린 화면은 persisted `pageshow`가 없다)에서도 한다. `Clear-Site-Data`에는 기대지 않는다(비보안 origin에서는 무시된다). Dashboard의 bfcache 적격성은 headless Chrome DevTools로 진단하되 진단일 뿐 test seam이 아니다(seam은 S1·S6·S8, 실제 확인은 S7). | WBX-D02 보완(AC-03 보충) |
| WBX-D35 | [orchestrator 결정 2026-10-28] Skill README는 agent별(Claude Code, Codex, OMP) 설치 절과 내려받기 명령을 둔다. Hub 제공본의 파일 주소는 절대 `/skill/` 주소(기존 `http://YOUR-HUB-HOST:8780` 치환으로 만든다), repo 판은 상대 링크다. Agent별 skill 폴더는 공식 문서나 실행 파일 근거로 정하고 README에 출처를 적는다. | WBX-D30의 구현 방식 |
| WBX-D36 | [사용자 결정 2026-10-28, 구 Q39 a] 로그아웃 → 뒤로 가기에서 이전 화면이 잠깐 보이던 문제(WBX-A6 뒤 사용자 관찰)는 화면을 세션 확인이 답할 때까지 가리는 방식으로 고친다. `session-check.js`를 `<head>`에서 동기로 싣고, `back_forward` navigation과 bfcache 복원에서 `<html>`에 가림 class를 붙여 `/api/badge` 답(2xx·그 밖 오류) 또는 4 s timer에 푼다. 401이면 보이지 않은 채 `/login`. 새 ticket WBX-A7. | WBX-D34 보완(AC-03 보충 2) |
| WBX-D37 | [orchestrator 결정 2026-10-28] WBX-B12의 최종 값: 격리 확인 OMP 종료는 stdin EOF 뒤 최대 10 s, SIGTERM 뒤 3 s, SIGKILL 뒤 3 s(SPEC s3의 5 s / 5 s / 3 s 대체). 근거: 실제 OMP 18.13.2가 EOF로 약 0.2 s 만에 끝나므로(WBX-B4 재실행) 긴 EOF 유예가 시작을 늦추지 않고, 저장소에 쓰는 중인 OMP에 signal을 보낼 가능성을 줄인다. Board OMP log는 `<hub state>/board-agent/omp-logs/`에 OMP 자체 log의 최근 20개 파일(파일당 ≤ 1 MiB, key 가림, symlink 따라가지 않음)과 시작·종료·정지 한 줄씩의 `board-events.log`(SPEC s3의 `board-agent/logs/`·"최근 10회 시작분" 대체). | SPEC s3 "공유 OMP 로그인 완화"의 기술 보완 값 |
| WBX-D38 | [orchestrator 결정 2026-10-28] REST `GET /api/v1/requests/{id}/result`는 `run_id`·`artifact_path`를 `full=true`일 때만 받는다. `full=true` 없이 주면 400 `invalid_input`이다(SPEC s3 B1 메모의 미정 항목을 구현 쪽으로 정함, WBX-B8). `wb_inbox.py result --run-id/--artifact-path`는 `--full`과 함께 쓴다. | SPEC s3 "외부 요청 API" endpoint 표의 result 행 |
| WBX-D39 | [orchestrator 결정 2026-10-28] `WB_API_KEY`를 export한 shell에서 Hub를 foreground(`wb _hub --foreground`)로 띄우면 Hub 프로세스 자신의 `/proc/<pid>/environ`에는 시작 때 값이 남는다(커널이 보관하는 최초 환경). Hub가 띄우는 자식(backend, Board shell 등)에는 없고, `wb hub start`·자동 시작·systemd로 띄운 Hub에도 없다. 같은 OS 사용자만 읽는 값이므로 수용하고 USAGE에 적는다. | WBX-D28 보완 |
| WBX-D40 | [orchestrator 결정 2026-10-28] Board agent의 `omp_bridge/board/ROLE.md`에는 skill의 절대 경로를 적지 않는다. WBX-B4 재실행에서 모델이 `skill://wb-inbox`로 skill을 찾아 `wb_inbox.py`를 실행했다. | SPEC s3 "Board agent 전환"의 ROLE.md 내용 |
| WBX-D41 | [orchestrator 결정 2026-10-29] Board terminal의 한글 IME 입력 오류(사용자 관찰: 실제 Chrome에서 "안녕하세요"를 치면 "녕녕녕녕녕안."이 들어감)는 트랙 D와 따로 ticket WBX-A8에서 고친다. A8은 트랙 D(WBX-D1·D2)보다 먼저 끝낸다. 둘 다 `/board`의 static 파일(`board.js`·`board.css` 등)을 고치기 때문이다. 마지막 검증은 사용자의 실제 IME 확인이다. | — (TI-18 화면의 결함 수정) |
| WBX-D42 | [사용자 결정 2026-10-29, Q44 a] `/board`를 widget workspace로 바꾼다. Widget은 격자에 맞춰 놓이고(snap), 제목 막대를 끌어 옮기고 모서리를 끌어 크기를 바꾸며, 서로 겹치지 않는다. | (A) D-26의 화면 구성(agent tab 하나 + shell tab 하나), P2-AC-34의 "tab" |
| WBX-D43 | [사용자 결정 2026-10-29, Q45] Board agent도 widget이다(옮기기·크기 바꾸기). 고정된 열이 아니다. | — |
| WBX-D44 | [사용자 결정 2026-10-29, Q50·Q51 기본안 수락] Board agent를 여러 개 둔다. 기본 최대 4개이고 Board 화면에서 1~8로 바꾼다. 이름은 사용자가 바꿀 수 있다(기본 이름 WBX-D47). 기존 Board agent가 첫 번째가 되고 세션·메모리를 그대로 가진다. Agent마다 자기 OMP process, 영속 세션·메모리 디렉터리, 시작마다 새 system key를 가진다. 공유하는 것: OMP 로그인(symlink, 복사하지 않음), role prompt, `[board_agent]` model, 격리 확인. 요청 출처는 "Board agent · <이름>"이다. Agent는 열 때(widget·panel) 시작하고, 닫으면 확인 뒤 process를 끝내되 메모리는 남긴다(다시 열면 이어 감). Panel의 "삭제"는 확인 뒤 그 agent의 메모리를 지운다. | (A) D-25(전용 home 하나 → agent별 home), D-26(한 벌 → 여러 개), P2-AC-33·34; WBX-D11·D24(시작마다 system key 하나 → agent마다); task-inbox SPEC 기술 보완 15·HB-O09(첫 연결 때 시작 → WBX-D56) |
| WBX-D45 | [사용자 결정 2026-10-29, Q46] Shell을 여러 개 둔다. 이름은 바꿀 수 있다. 기본 한도는 10이고 Board 화면에서 바꾼다. Shell마다 자기 process이고 sandbox가 없다(ADR-0003). 닫으면 확인 뒤 그 process를 끝낸다. 브라우저 재연결에는 살아남고 Hub 재시작에서 끝난다(현행과 같음). 감사 기록에 terminal 열기·닫기가 남는다. Q46 답의 "만들 때 이름을 정함"은 같은 날 WBX-D47(기본 이름, 묻지 않음)로 바뀌었다. | (A) D-26, P2-AC-34 |
| WBX-D46 | [orchestrator 결정 2026-10-29] Shell 한도의 허용 범위는 1~32다. Agent·shell 한도는 Hub가 저장해 모든 브라우저가 같은 값을 쓴다(브라우저별 값이 아님). 근거: 한도는 Hub가 띄우는 process 수를 정하는 Hub 자원 설정이다. 32는 terminal당 출력 ring buffer 1 MiB와 terminal당 연결 8개(WBX-D54)를 곱한 메모리·socket 상한을 작게 두기 위한 값이다. | — |
| WBX-D47 | [사용자 결정 2026-10-29] 새 terminal의 기본 이름은 `agent-1`, `agent-2`, … / `shell-1`, `shell-2`, …(다음 빈 번호)다. 만들 때 이름을 묻지 않는다. 사용자는 왼쪽 panel에서 이름을 바꾸고(예: `전처리`) widget 제목에 그 이름이 보인다. | WBX-D45의 "만들 때 이름" |
| WBX-D48 | [orchestrator 결정 2026-10-29] 이름은 앞뒤 공백을 뺀 Unicode 1~40자이고 제어 문자가 없으며, 같은 종류(agent / shell) 안에서 대소문자를 무시하고 유일하다. Hub가 저장해 모든 브라우저가 같은 이름을 본다. "다음 빈 번호"는 같은 종류에서 `<종류>-N` 이름으로 쓰이지 않은 가장 작은 양의 정수다. 이행: 기존 agent는 `agent-1`, 기존 shell은 `shell-1`이 된다. 요청 출처는 제출 시점의 현재 이름으로 "Board agent · <이름>"을 기록한다(이후 이름을 바꿔도 이미 낸 요청은 그대로). | Board agent 요청의 `origin_name`(지금은 비어 있음) |
| WBX-D49 | [사용자 결정 2026-10-29, Q47] 왼쪽에 접히는 관리 panel을 둔다. Agent: 상태 점, 보이기/숨기기, 시작/중지, 이름 바꾸기, 삭제. Shell: 새 shell, 이름 바꾸기, 보이기/숨기기, 닫기. 그 밖에 배치 초기화, 한도 설정. Panel의 열림/닫힘은 브라우저별로 기억한다. | — |
| WBX-D50 | [사용자 결정 2026-10-29, Q42] 배치(위치·크기·보이기)는 브라우저별로 기억한다(localStorage). | — |
| WBX-D51 | [사용자 결정 2026-10-29, Q48] 화면 폭 720 px 미만에서는 격자를 쓰지 않고, panel에서 고른 terminal 하나를 화면 전체로 보인다. | — |
| WBX-D52 | [사용자 결정 2026-10-29, Q49] 끌기·크기 조절은 직접 작성하고 새 library를 쓰지 않는다(CDN 없음, CSP `script-src 'self'`). | — (P2-AC-37·WBX-D12의 CDN·framework 제외 유지) |
| WBX-D53 | [사용자 결정 2026-10-29, Q43] 단축키는 지금 두지 않는다. Focus는 클릭한 widget이 가지며 강조 테두리로 보인다. | — |
| WBX-D54 | [사용자 결정 2026-10-29, Q52 → Q53·Q54] 한 연결 lease를 공유 연결로 바꾼다. Terminal을 연 모든 브라우저·기기가 같은 출력을 보고 입력할 수 있다. 입력은 도착 순서대로 적용한다(서로 섞이는 것을 수용). Terminal 크기는 가장 최근에 활동한(마지막 입력 또는 resize) 연결을 따르고, 다른 연결은 letterbox/scroll로 보인다. Terminal당 최대 8 연결. Widget에 "다른 화면 N개 연결됨"을 보인다. 감사 기록은 연결별 연결·해제를 남긴다. | (A) D-28 (1)·(2)·(5)(즉시 인수·epoch 입력 버림·"다시 가져오기"), P2-AC-35, task-inbox SPEC "WebSocket terminal 프로토콜"의 lease·epoch·4001, 감사 `terminal_takeover`, USAGE §7.2·§10.3, ADR-0003 감사 항목의 "인수" |
| WBX-D55 | [orchestrator 결정 2026-10-29] WebSocket은 terminal마다 하나다(`GET /ws/board/{terminal_id}`, protocol v2). 근거: 현행 `board/ws.py`에서 연결 하나가 송신 queue 한도(4 MiB, 넘으면 4003), 붙여넣기 backpressure(socket 읽기를 멈춤), heartbeat, attach replay를 한 단위로 가진다. 여러 terminal을 한 socket에 묶으면 한 terminal의 큰 출력·붙여넣기가 다른 terminal의 입출력과 heartbeat를 막거나 함께 끊는다. Terminal당 연결 8개 한도도 socket 수와 바로 대응한다. 보이는 widget만 연결한다. 세부는 SPEC s4. | task-inbox SPEC "WebSocket terminal 프로토콜"(`/ws/board` 하나에 channel 두 개) |
| WBX-D56 | [orchestrator 결정 2026-10-29] 수명 세부. (1) "열기"는 panel의 보이기·시작, widget의 시작 버튼, 그리고 이번 Hub run에서 한 번도 시작하지 않은 agent의 widget이 보이는 채로 `/board`를 여는 것이다(현행 "첫 연결 때 시작"을 잇는다). 소유자가 중지한 agent는 page를 다시 열어도 저절로 시작하지 않는다. (2) Widget의 "숨기기"는 이 브라우저에서만 감추고 process는 그대로 둔다. "닫기"(×)는 agent면 중지(확인), shell이면 종료·목록에서 제거(확인)다. (3) 이번 Hub run에 shell이 하나도 없고 이 run에서 shell을 만든 적이 없으면 첫 `/board` 열기에서 `shell-1`을 만든다(현행 "첫 연결에 shell이 있음"을 잇는다). (4) 한도를 지금 개수보다 작게 바꾸는 것은 거절한다(먼저 닫거나 삭제). (5) 새로 만들기는 곧 열기다(만들고, 시작하고, 이 브라우저에 보인다). | WBX-D44·D45 보완 |
| WBX-D57 | [orchestrator 결정 2026-10-29] 배치 세부. 겹치는 자리에 놓으면 놓지 않고 마지막 유효 자리로 돌아간다(다른 widget을 밀어내지 않음). 저장된 배치가 없는 브라우저는 모든 terminal을 agent 먼저 자동 배치해 보인다. 저장된 배치가 있는 브라우저에는 다른 곳에서 만든 terminal이 panel에 "숨김"으로 나온다(배치를 흐트러뜨리지 않음). 없어진 terminal의 배치 항목은 버린다. | WBX-D42·D50 보완 |
| WBX-D58 | [orchestrator 결정 2026-10-29] Agent별 system key와 출처 이름은 표시·감사용 구분이며 보안 경계가 아니다. 모든 agent·shell은 같은 OS 사용자로 sandbox 없이 돌아 서로의 env(`/proc/<pid>/environ`)·home을 읽을 수 있다(ADR-0003). 내부 listener는 실행 중인 agent 각자의 이번 시작 key만 받는다. | WBX-D24("이번 시작의 system key 하나" → 실행 중인 agent마다 하나) |
| WBX-D59 | [사용자 결정 2026-10-29, Q56] 웹이 TUI가 붙어 있는 동안에도 같은 pane을 보고 입력할 수 있도록 backend를 바꾼다. 바꾸는 방식은 ui_v1에 덧붙이는 것(추가 attach와 화면 fan-out)이고, 기존 TUI는 바뀌지 않고 그대로 동작한다. | (A) D-08 "계속 제외"의 "웹의 manager·worker·host pane 조작", D-09 "manager·worker·host pane 조작은 없다", P2-AC-21 "웹에는 manager·worker·host pane 입력 수단이 없다"; 이 문서 "계속 제외"·"트랙 D 제외"의 같은 문구 |
| WBX-D60 | [사용자 결정 2026-10-29, Q57] TUI가 붙어 있는 동안은 TUI가 pane 크기를 정하고 웹 연결은 그 크기를 따른다(letterbox/scroll). TUI가 없으면 pane 크기는 마지막으로 활동한 웹 연결을 따른다(Board terminal과 같은 규칙, WBX-D54). | — |
| WBX-D61 | [사용자 결정 2026-10-29, Q58] Manager·worker pane은 TUI처럼 웹에서 자유롭게 입력하되 TUI의 Ctrl-d 두 번 누르기 규칙을 따른다. Host shell 입력은 backend의 기존 주인 규칙을 따르고, 웹은 거절 이유를 보인다. | (A) D-09·P2-AC-21(위와 같음) |
| WBX-D62 | [사용자 결정 2026-10-29, Q59] Pane widget 버튼: host 인수 요청·인수 확인·넘기기, 실행 중 명령 중지(확인 dialog), 끝난 pane 재시작, pane 강제 종료(확인 dialog). TUI의 `Ctrl-]` 명령을 버튼으로 옮긴 것이다. | (A) D-09의 "웹 제어 버튼은 켜기/끄기·일시정지·재개·`confirm-boot`"(pane 동작 버튼이 더해짐; 프로젝트 제어 버튼은 그대로) |
| WBX-D63 | [사용자 결정 2026-10-29, Q60] Hub 감사 기록에 웹 pane 연결·해제, 인수·넘기기, 명령 중지, 강제 종료, 재시작을 남긴다. Keystroke 내용은 절대 남기지 않는다. | — |
| WBX-D64 | [사용자 결정 2026-10-29, Q61] Workspace 왼쪽 panel에 "프로젝트" 묶음을 두고, 프로젝트마다 manager / worker / host widget을 더한다. Widget 제목은 "<프로젝트 이름> · manager". 프로젝트 상세 화면에 "작업 공간에서 열기" 링크를 둔다. | WBX-D49(panel 내용에 프로젝트 묶음 추가) |
| WBX-D65 | [orchestrator 결정 2026-10-29] ui_v1 덧붙이기의 모양. (1) 기존 `attach`는 TUI의 primary 자리 하나로 그대로 두고, 새 request `share_attach`·`share_detach`로 primary와 따로 있는 공유 attach를 더한다. 기존 `attach`에 field를 더하지 않는 이유: 이전 판 backend가 모르는 field를 무시하면 웹이 TUI 자리를 차지하게 된다. 새 type은 이전 판에서 `unsupported_type`이라 안전하게 구분된다. (2) 공유 attach는 Hub 전용이다: hello 이름 `workbench-hub` + 같은 peer uid + 확인된 실행 중 Hub process(현행 `inbox_notice`·`cancel_task`와 같은 문). (3) 화면(display)·상태(state)는 primary와 모든 공유 attach에 연결별 송신 buffer로 보내고, 한도를 넘긴 연결만 `slow_client`로 닫는다. (4) 공유 attach는 입력·붙여넣기·크기·인수·넘기기·명령 중지·재시작·강제 종료를 보낼 수 있고, primary와 같은 `admit` 경로와 거절 이유를 쓴다. Backend focus(`focus`)와 primary `detach`는 보낼 수 없다(TUI focus를 움직이지 않음). (5) Host shell 입력 주인은 backend에 하나(`manager`/`user`)이고 연결별이 아니다. "user"는 TUI와 웹 모두의 소유자 한 사람이다. 누가 인수·넘기기를 하든 모두에게 같게 적용되고, user가 주인일 때 TUI와 웹의 입력은 backend에 도착한 순서로 섞인다. (6) TUI가 붙어 있으면 공유 attach의 `resize`는 적용하지 않고 새 reason `resize_owned_by_ui`로 답한다. 세부는 SPEC s5 "트랙 E". | ui_v1 계약(덧붙이기만), (A) D-09 |
| WBX-D66 | [orchestrator 결정 2026-10-29] Hub 중계는 프로젝트마다 ui_v1 연결 하나(공유 attach 하나)를 그 프로젝트의 모든 웹 연결이 함께 쓴다. 근거: backend는 select loop 하나에 연결 16개 한도와 연결당 송신 buffer 16 MiB를 둔다. 웹 연결마다 backend 연결을 두면 backend의 fan-out·replay·메모리가 웹 연결 수(pane당 8 × 3 × 프로젝트 수)만큼 늘고, 크기 주인 판정과 감사가 backend와 Hub로 나뉜다. 하나로 두면 backend는 연결 하나만 더 보고, 느린 웹 연결은 Hub의 연결별 queue에서 그 연결만 끊긴다(트랙 D WebSocket과 같은 방식). 웹 쪽 endpoint는 트랙 D의 `GET /ws/board/{id}` v2를 그대로 쓰고 id는 `<project_id>:manager`·`:worker`·`:host`다. 버튼 동작은 세션 API `POST /api/board/panes/{id}/{action}`(CSRF, 확인이 필요한 동작은 `confirm:true`)이다. Pane당 웹 연결 한도는 8(WBX-D54와 같음). | WBX-D55(id 공간에 pane id 추가) |
| WBX-D67 | [orchestrator 결정 2026-10-29] 실패 동작. Backend가 꺼져 있으면 widget은 "꺼짐"이고 Hub는 backend에 연결하지 않는다(켜기는 프로젝트 화면에서). Backend가 끝나거나 다시 시작하면 Hub는 연결된 웹 widget이 있는 동안 backoff로 다시 붙고, 새 backend면 widget 화면을 지운 뒤 replay를 보낸다. 이전 판 backend(`share_attach`가 `unsupported_type`)는 "backend 재시작 필요"로 보이고 Hub가 backend를 재시작하지 않는다(실행 중인 실험을 끊지 않기 위해). | — |
| WBX-D68 | [사용자 결정 2026-10-29] Board shell의 기본 program은 bash다. bash를 찾지 못하면 sh다. | (A) task-inbox SPEC "Board agent 세트"·이 문서 SPEC s4 "Shell"의 `$SHELL` 기본값, `hub.toml` 템플릿 주석, task-inbox USAGE §1.2 표의 `shell` 줄 |
| WBX-D69 | [orchestrator 결정 2026-10-29] Shell 결정 순서: `hub.toml` `[board_agent] shell`(설정되어 있으면 그 절대 경로만, 실행할 수 없으면 지금처럼 `shell_unavailable`이고 다른 것으로 넘어가지 않음) → bash(`/bin/bash`, `/usr/bin/bash`, Hub 환경 `PATH`의 `bash`의 절대 경로 순서) → `/bin/sh`. `$SHELL`은 더 이상 보지 않는다. 근거: Hub는 자동 시작·systemd로 떠 `$SHELL`이 없거나 사용자의 대화형 shell과 다를 때가 많다. 새로 시작하는 shell(만들기·재시작)에만 적용하고 돌던 shell은 그대로다. Ticket WBX-D4(Sonnet, 작음). | WBX-D68의 구현 방식 |

## 개정되는 기존 결정

CONTEXT.md에서 MCP 없는 상태로 바꾼 용어에는 "(WBX 트랙 B 완료 전에는 MCP가 현행)" 표시를 달았다. 트랙 B가 끝나면 이 표시를 지운다(2026-10-28 WBX-B9에서 지움). 기존 문서에는 원문을 고치지 않고 2026-10-27 개정 주석만 달았다. 주석을 단 곳에는 ✓를 붙였다. 나머지 MCP 문구는 task-inbox BRIEF 머리의 일괄 주석("MCP"는 REST API·`wb-inbox` skill로 읽는다)이 덮는다.

| 기존 | 문서 | 개정 내용 | 근거 |
|---|---|---|---|
| 일괄 ✓ | task-inbox BRIEF 머리 | 이 문서의 MCP client·Hub MCP·MCP 응답·MCP tool은 REST API와 `wb-inbox` skill로 읽는다 | WBX-D05 |
| D-08 ✓ | task-inbox BRIEF | Board agent의 OMP MCP 해제 → Board agent는 MCP가 없고 `wb-inbox` skill만 쓴다 | WBX-D11 |
| 외부 접점 줄(제약과 호환성) ✓ | task-inbox BRIEF | 외부 접점: MCP Streamable HTTP tool 부분집합 → `/api/v1/` REST + skill | WBX-D05 |
| D-22 ✓ | task-inbox BRIEF | "MCP tool 옵션" → REST 결과 조회의 옵션 | WBX-D05 |
| D-25 ✓ | task-inbox BRIEF | Hub MCP editor 연결 → 내부 listener REST, system key `WB_API_KEY`, skill `wb-inbox`만 | WBX-D11 |
| D-29 ✓ | task-inbox BRIEF | MCP 직접 구현 삭제. `aiohttp` 예외의 근거는 REST에도 그대로 | WBX-D05 |
| D-33 ✓ | task-inbox BRIEF | 두 단계 취소의 형태를 REST `POST .../cancel`로 | WBX-D05 |
| D-36 ✓ | task-inbox BRIEF | MCP 테스트 client → REST 테스트 client와 `wb_inbox.py`. Codex CLI 호환 확인은 skill 사용 확인으로 | WBX-D05, D07 |
| P2-AC-33 ✓, 41 ✓, 45 ✓ | task-inbox BRIEF | 위 결정과 같음 | WBX-D05, D07, D11 |
| P2-AC-05·06·23·25·28·31·37·44 | task-inbox BRIEF | MCP 문구만 REST로(일괄 주석) | WBX-D05 |
| HB-D02 ✓ | workbench-hub BRIEF | "외부 MCP client" → 외부 agent(REST·skill) | WBX-D05 |
| 결정 4번째 항목 ✓ | ADR-0003 | Board agent의 Hub MCP 허용 → MCP 없음, `wb-inbox` skill만 | WBX-D11, ADR-0006 |
| :18 ✓, :12 ✓ | ADR-0004, ADR-0005 | "MCP 응답", "MCP client" 문구 → REST 응답, REST를 쓰는 외부 agent(2026-10-28 WBX-B9에서 주석 부착) | WBX-D05 |
| D-10, P2-AC-16 | task-inbox BRIEF | 충돌 없음: 경고 배너를 접을 수 있게 할 뿐 경고는 항상 보인다 | WBX-D03 |

### 트랙 D가 개정하는 기존 결정(2026-10-29)

원문은 고치지 않고 각 자리에 한 줄 개정 주석(이 문서 WBX-D42~D58을 가리킴)만 달았다. 주석을 단 곳에 ✓를 붙였다. USAGE는 현재 동작 설명이므로 지금은 "트랙 D에서 바뀜" 한 줄만 달고, 구현 뒤 WBX-D3가 현재 동작으로 다시 쓴다.

| 기존 | 문서 | 개정 내용 | 근거 |
|---|---|---|---|
| D-25 ✓ | task-inbox BRIEF | 전용 home 하나 → agent마다 home·세션·메모리, 시작마다 agent별 system key. 로그인·role prompt·model·격리 확인은 공유 | WBX-D44·D58 |
| D-26 ✓ | task-inbox BRIEF | 소유자당 agent tab 하나 + shell tab 하나 → agent 여러 개(기본 한도 4, 1~8)와 shell 여러 개(기본 한도 10, 1~32), 격자 widget workspace | WBX-D42~D46 |
| D-28 ✓ | task-inbox BRIEF | 한 연결 lease·즉시 인수·epoch 입력 버림·"다시 가져오기" → 공유 연결(모든 연결이 보고 입력, 도착 순서, 크기는 최근 활동 연결, terminal당 8 연결). Heartbeat·tab 닫기 해제·"남은 연결됨이 새 연결을 막지 않음"은 유지 | WBX-D54·D55 |
| P2-AC-34 ✓, P2-AC-35 ✓ | task-inbox BRIEF | 위와 같음. 검증 사례는 WBX-AC-32·33 | WBX-D54 |
| "Board agent 세트" ✓, "WebSocket terminal 프로토콜" ✓, 감사 표의 `terminal_*` 행 ✓, 기술 보완 15 ✓ | task-inbox SPEC | 이 문서 SPEC s4 "트랙 D"로 대체(terminal registry, `/ws/board/{id}` v2, `terminal_takeover` 폐지, 열 때 시작) | WBX-D44~D56 |
| §7 머리 ✓, §7.2 ✓, §7.3 ✓, §10.3 ✓ | task-inbox USAGE | 트랙 D 구현 뒤 WBX-D3가 다시 씀 | WBX-D42~D56 |
| 결정 2번째 항목 (3) 감사의 "인수" ✓ | ADR-0003 | 인수가 없어짐(공유 연결). 결정 자체(sandbox 없음·로그인·bind·감사)는 그대로이고 여러 agent·shell에도 같다 | WBX-D54·D58 |
| "Board agent", "Board agent 세트 / 웹 터미널" ✓ | CONTEXT.md | 여러 agent·shell과 공유 연결로 읽는다(트랙 D 완료 전에는 현행) | WBX-D44·D45·D54 |
| WBX-D24 | 이 문서 | "이번 시작의 system key만" → 실행 중인 agent 각자의 이번 시작 key만 | WBX-D58 |

### 트랙 E와 Board shell이 개정하는 기존 결정(2026-10-29, r0.6)

원문은 고치지 않고 각 자리에 한 줄 개정 주석(이 문서 WBX-D59~D69를 가리킴)만 달았다. 주석을 단 곳에 ✓를 붙였다. 사용 안내(USAGE)·`hub.toml` 템플릿의 현재 동작 설명은 구현 뒤 WBX-E4·WBX-D4가 고친다.

| 기존 | 문서 | 개정 내용 | 근거 |
|---|---|---|---|
| D-08 "계속 제외" ✓(범위 목록 줄과 결정 표 행) | task-inbox BRIEF | "웹에서 manager·worker·host pane 조작"을 제외 목록에서 뺀다. Board workspace의 프로젝트 pane widget으로 보고 입력하고 pane 동작 버튼을 쓴다. 나머지 제외는 그대로 | WBX-D59·D61·D62 |
| D-09 ✓ | task-inbox BRIEF | "웹은 상태 보기 … manager·worker·host pane 조작은 없다" → pane widget으로 보고 입력하고 pane 동작 버튼(인수·넘기기·명령 중지·재시작·강제 종료)을 쓴다. 프로젝트 제어 버튼(켜기/끄기·일시정지·재개·`confirm-boot`)과 "API key로는 할 수 없다"는 그대로 | WBX-D59~D62 |
| P2-AC-21 ✓ | task-inbox BRIEF | "웹에는 manager·worker·host pane 입력 수단이 없다" → 웹 pane 입력은 WBX-AC-39~43의 규칙을 따른다. 프로젝트 제어의 세션·확인 창 규칙은 그대로 | WBX-D59·D61 |
| "웹에는 pane 입력 수단이 없다(D-09)" 줄 ✓, 수락 기준 표 P2-AC-21 행 ✓ | task-inbox SPEC(:318, :515) | 위와 같음. 정본은 이 문서 SPEC s5 "트랙 E" | WBX-D59 |
| "계속 제외", "트랙 D 제외" ✓ | 이 문서 "범위와 비목표" | 위와 같음 | WBX-D59 |
| `shell` 기본값 `$SHELL` | task-inbox SPEC(:68, "Board agent 세트" :389), task-inbox USAGE §1.2, `hub.toml` 템플릿, 이 문서 SPEC s4 "Shell" ✓ | `$SHELL` 대신 bash → `/bin/sh`(WBX-D69 순서). task-inbox 쪽과 템플릿은 WBX-D4가 고친다 | WBX-D68·D69 |

### 문서 갱신 대상(트랙 B 중)

2026-10-28: 아래 항목은 WBX-B5(ROLE.md)와 WBX-B9(나머지)에서 고쳤다. task-inbox SPEC은 원문을 두고 개정 주석을 달았고, USAGE는 현재 동작으로 다시 썼다.

- task-inbox SPEC: "API key"(:148~), "MCP Streamable HTTP endpoint"(:256~, REST 절로 교체), "Board agent 세트"(:380~), `allow_from` 설명의 `/mcp`·내부 MCP listener 문구(:120).
- task-inbox USAGE: §3 API key, §4 MCP 연결(skill 설치로 교체), §1.2 표의 `allow_from` 줄, 머리의 "외부 `/mcp`", §7.4 Board agent 역할 안내, §10.2.
- workbench-hub SPEC·USAGE의 MCP 문구, `omp_bridge/board/ROLE.md`(MCP tool 9개 목록 → skill 사용 안내), ADR-0004:18, ADR-0005:12.
- task-inbox `PROBE-OMP-MCP.md`와 TI ticket은 이력이므로 고치지 않는다.

## 관측 사실

2026-10-27 정적 읽기(조사 subagent 결과, 일부 줄 재확인). Chrome 재현은 하지 않았다. 경로는 `src/workbench/hub/` 기준이다.

| 사실 | 출처 |
|---|---|
| `/`는 프로젝트 상태 표이고 10초 `<meta http-equiv=refresh>`가 있다 | `render.py:78`, `board_status.py:106` |
| Header에는 brand 링크·badge·logout뿐이다. `/board`, `/settings`, `/settings/keys`, `/settings/audit`으로 가는 링크가 어디에도 없다(화면은 있다: 비밀번호 변경, key 목록·발급·폐기, 감사 기록) | `render.py:63-87`, `pages/login.py:207-278`, `pages/settings.py:88-250` |
| Key 발급 결과는 key 원문만 보인다 | `pages/settings.py:180-186` |
| 평문 HTTP 배너: 문구 `config.py:30`, 조건 `config.py:461-465`, 닫기 없이 렌더링 `render.py:72`. CSP `script-src 'self'` | `config.py`, `render.py`, `web/middleware.py:24` |
| 모든 비정적 응답에 `Cache-Control: no-store`. HSTS 없음, redirect는 모두 303·상대 Location, Secure cookie는 TLS일 때만 | `web/middleware.py:109` |
| [진단, 코드로만, 재현 안 함] `no-store`가 bfcache를 막아 뒤로 가기마다 HTTP로 다시 요청하고 Chrome HTTPS-First 검사가 다시 뜬다. 10초 meta refresh가 history 항목을 늘린다. Chrome "Always use secure connections" 설정도 원인일 수 있으며 서버가 제어하지 못한다 | 위 두 줄 |
| `allow_from` peer 검사는 app 전체의 가장 바깥 middleware(화면, static, `/mcp`, `/ws/board`) | `web/middleware.py:196-207` |
| HTML은 Python 문자열 helper로 만든다. CSS 합계 4.5KB(`base.css`, `app.css`, `board.css`). xterm은 `static/vendor`에 vendored, CDN 없음 | `render.py`, `static/` |
| MCP tool 9개: `inbox_projects`, `inbox_submit`(idempotency_key), `inbox_list`, `inbox_get`, `inbox_comment`, `inbox_update`(editor), `inbox_set_state`(editor), `inbox_cancel`(두 단계 token, TTL 60s), `inbox_result`(summary / full / raw page ≤131072B, artifact_path 규칙). 한도 제목 200자·본문 64KiB·댓글 16KiB·목록 ≤100. Bearer `wbk_…`, principal requester / editor / board_agent(system key) | `mcp.py:155-194`, `inbox/limits.py:33-39` |
| Board agent: Hub가 띄운 OMP가 내부 loopback listener `/mcp`에 시작마다 만든 system editor key(env `WB_BOARD_MCP_KEY`)로 붙는다. `board/launch.py`가 mcp.json을 쓰고 overlay가 skill을 비우며(`customDirectories: []`), isolation 검사는 `mcp__workbench_hub_*`만 허용한다. `omp_bridge/board/ROLE.md`가 MCP tool 9개를 나열한다 | `mcp.py:736-774`, `board/launch.py:37`, `:98` |
| OMP는 SKILL.md skill을 지원한다(manager·worker는 `skills.customDirectories`로 `omp_bridge/skills`를 읽고, workbench-report skill은 bash로 번들 script를 실행). Board agent에는 bash가 있다 | `omp_bridge/skills`, ADR-0003 |
| `WB_BOARD_MCP_KEY`는 Hub가 물려받지 않는 env 목록에 있다(HB-T04) | `process.py:267-270` |
| Manager 검토 결과 도구는 MCP가 아니다(bridge `workbench_inbox` → backend → hub_v1 Unix socket) | 조사 결과 |
| MCP 테스트 약 190개 | `tests/hub/test_hub_mcp*.py`, `tests/hub/support/mcp_client.py`, `support/mcp_hub.py`, `probes/omp_mcp_probe.py` |

2026-10-28 사용자 실제 Chrome 확인(WBX-A5 일부, 평문 HTTP LAN 주소의 Hub)과 같은 날 orchestrator 확인:

| 사실 | 출처 |
|---|---|
| 배너 닫기 → "평문 HTTP" chip 접힘, 좁은 화면 nav 접힘이 동작한다. 디자인은 "나쁘지 않아"(수락) | 사용자 관찰 |
| Dashboard `/`로 뒤로 가기 하면 매번 Chrome의 비보안 연결 경고(interstitial)가 뜬다 | 사용자 관찰 |
| 로그아웃 뒤 뒤로 가기 하면 이전 로그인 화면이 보이고 그대로 남는다(로그인 화면으로 가지 않는다) | 사용자 관찰 |
| Hub 제공 `/skill/README.md`를 받은 agent가 `SKILL.md`·`wb_inbox.py`를 가져오지 못했다 | 사용자 관찰 |
| Chrome "Always use secure connections" 설정 상태는 아직 보고되지 않았다 | 사용자 보고 없음 |
| 2026-10-27 WBX-B4 probe 중 사용자 OMP의 openai-codex 로그인이 사라졌다. 가장 유력한 원인은 `~/wb-demo` backend가 쓰던 인증 사본과 갈라진 refresh token이다. 이전의 "wb-demo도 같은 store를 쓴다"는 서술은 틀렸다(사본이었다) | [INCIDENT-20261027-omp-login.md](INCIDENT-20261027-omp-login.md) |

2026-10-28 오후(r0.4), WBX-A6·B12 통합 뒤:

| 사실 | 출처 |
|---|---|
| Dashboard를 `/dashboard`로 옮긴 뒤(WBX-A6) dashboard로 뒤로 가기 할 때의 Chrome 경고가 사라졌다 | 사용자 확인 2026-10-28 |
| 로그아웃 뒤 뒤로 가기는 로그인 화면으로 가지만 그 전에 이전 화면이 잠깐 보였다 → WBX-D36·WBX-A7(재확인 필요) | 사용자 관찰 2026-10-28 |
| 배너 접기·nav·디자인은 이전 확인대로 문제없다. Skill 설치(실제 agent), `/board`, Chrome "Always use secure connections" 설정은 아직 확인 전이다 | 사용자 관찰·보고 없음 |
| WBX-B4 재실행(B12 뒤, 사용자의 실제 로그인)은 passed: OMP 18.13.2가 Board overlay 아래에서 `wb-inbox` skill만 보고 MCP tool이 없었고, 한 turn에서 `skill://wb-inbox`를 읽고 bash로 `wb_inbox.py list`를 내부 REST에 실행했다. 로그인 provider는 전후로 줄지 않았고 OMP는 stdin EOF에서 약 0.2 s에 끝났다 | [PROBE-BOARD-SKILL.md](PROBE-BOARD-SKILL.md) |
| 재실행 전부터 사용자 OMP의 authenticated provider에 `anthropic`이 없었다(1차 실행 첫 확인에는 있었다). 이번 실행 중 줄어든 것은 아니다. Anthropic provider가 필요하면 사용자가 `omp`에서 다시 로그인한다 | PROBE-BOARD-SKILL.md "결과 해석" |

2026-10-29, WBX-A5 사용자 확인(평문 HTTP LAN 주소의 Hub, 사용자 실제 Chrome):

| 사실 | 출처 |
|---|---|
| Dashboard 표가 화면 폭에 맞고 좁은 화면에서는 card로 바뀐다(commit `cf67e15` 뒤) — 문제없음 | 사용자 확인 2026-10-29 |
| 로그아웃 → 뒤로 가기에서 이전 화면이 전혀 보이지 않는다 — 문제없음(WBX-A7 확인, WBX-AC-03 보충 2) | 사용자 확인 2026-10-29 |
| `/board`의 Board agent가 REST + `wb-inbox` skill로 동작한다 — 문제없음(WBX-AC-16의 실제 OMP·모델 확인) | 사용자 확인 2026-10-29 |
| Chrome "Always use secure connections" 설정: 사용자 답 "문제없음"(WBX-AC-04 기록) | 사용자 답 2026-10-29 |
| 실제 agent의 skill 설치 확인(WBX-AC-22 대상 host)은 사용자가 뒤로 미뤘다 | 사용자 결정 2026-10-29 |
| Board terminal에서 한글 IME로 "안녕하세요"를 치면 "녕녕녕녕녕안."이 들어간다 → WBX-D41·WBX-A8 | 사용자 관찰 2026-10-29 |

2026-10-29 오후(r0.6), 트랙 E 전 읽기 조사(정적, 실행 없음). 경로는 `src/workbench/` 기준이다.

| 사실 | 출처 |
|---|---|
| Backend UI 계약은 ui_v1(frame magic `WBUI`, `VERSION = 1`). 화면은 raw PTY bytes의 display frame(pane, session_id, generation, sequence, replay 표시), 상태는 바뀔 때만 JSON state frame(최소 0.25 s 간격) | `contracts/ui_v1.py`, `backend/service.py` state push |
| Attach 자리는 하나다. 두 번째 `attach`는 `attached_elsewhere`. Attach한 연결만 display·state를 받고 input·paste·resize·focus·takeover_*·handoff·restart_pane·kill_pane·stop_command를 보낼 수 있다. Attach하지 않은 `workbench-hub`는 pause·resume·inbox_notice·task_status·cancel_task만 | `backend/ui_server.py:373-430` |
| Socket은 `<runtime>/ui.sock`(0600), accept 때 `SO_PEERCRED`로 peer pid·uid를 읽는다. 연결 16개 한도, 연결당 송신 buffer 16 MiB(넘으면 `slow_client`로 그 연결을 닫고 primary였으면 detach) | `backend/ui_server.py` |
| Hub는 이미 `workbench-hub` 이름으로 backend 신원(pid·start ticks)을 확인한 뒤 요청마다 연결한다 | `hub/inbox/backend_port.py:51-70` |
| Pane: manager_omp·worker_omp는 `OmpPane`(`pty.fork`), host_shell은 `PersistentShell` 위의 `ShellPane`. Replay ring은 pane당 256 KiB. Resize는 `TIOCSWINSZ`이고 마지막 resize가 이긴다. Pane 크기는 TUI가 계산해 pane마다 보낸다. Pane view에 `rows`·`cols`가 있다 | `backend/panes.py` |
| OMP pane은 모든 입력을 받는다. Manager 입력창 대기는 화면이 아니라 OMP bridge의 editor 상태로 판정하고 자동 전달만 미룬다. 사용자의 worker 입력은 허용된다 | `backend/panes.py:567`, core DECISIONS C-D69(6)(b) |
| Host shell 수동 입력은 `host_shell_automation`·`input_owner_manager`·`input_target_unknown`으로 거절될 수 있고, `takeover_request` → `takeover_confirm` → `handoff`로 주인이 바뀐다. 입력 주인은 backend에 하나다. Lone Ctrl+C는 확인된 foreground 명령으로 가고 journal에 남는다 | `backend/panes.py:796-852`, ADR-0002, command-stop SPEC |
| `Ctrl-]` prefix와 OMP pane의 Ctrl-d 두 번 누르기(2 s)는 TUI 안에만 있다. Backend focus가 바뀌면 TUI focus도 따라간다 | `ui/product/model.py`(`CTRL_D_CONFIRM_SECONDS`, `_send_input`), `ui/product/input.py`(`PREFIX`) |
| Keystroke는 감사하지 않는다. 남는 것은 interrupt·stop·kill·restart의 journal뿐이다 | `backend/service.py`(`_command_interrupted`), command-stop |
| 웹의 pane 조작을 막는 기존 결정: (A) D-08 "계속 제외", D-09, P2-AC-21, task-inbox SPEC:318·:515, 이 문서 "계속 제외"(:40) | 각 문서 |
| Board shell은 `[board_agent] shell` → `$SHELL` → `/bin/sh` 순서로 정한다 | `hub/board/launch.py` `prepare_shell` |

## 남은 확인

| ID | 내용 | 시점 |
|---|---|---|
| WBX-O01 | WBX-D02의 효과를 사용자의 실제 Chrome에서 확인한다. "Always use secure connections" 설정 상태를 같이 기록한다(WBX-AC-04). 2026-10-28 일부 확인(위 관측 사실). `/dashboard` 뒤로 가기 경고는 해소. 2026-10-29: WBX-A7 뒤 로그아웃 → 뒤로 가기, `/board`(REST + skill), dashboard 좁은 화면, Chrome 설정("문제없음") 확인. 남은 것: 복사 버튼, 자동 갱신 없는 화면 사이 뒤로 가기의 명시 기록, 실제 agent의 skill 설치(사용자가 미룸 → WBX-O07) | 트랙 A 구현 뒤, 사용자 |
| WBX-O02 | Board overlay 아래 OMP의 `wb-inbox` skill 로드와 bash REST 호출 probe(WBX-AC-17). 실패 시 대안 결정. 2026-10-27 1차는 inconclusive(로그인 사용 불가). 해소: WBX-B12 뒤 재실행(WBX-D33) 2026-10-28 passed, WBX-B5 진행 | 트랙 B 전환 전 |
| WBX-O03 | 해소: WBX-D17 | — |
| WBX-O04 | 해소: WBX-D19 | — |
| WBX-O05 | 해소: WBX-D18 | — |
| WBX-O06 | 트랙 D의 실제 browser 확인: 끌기·크기 조절의 감각, 두 기기 공유 연결과 크기 따름(letterbox/scroll), 720 px 미만 화면, focus 테두리, 한글 IME(WBX-A8) | WBX-A8·D2 뒤, 사용자(S7) |
| WBX-O07 | 실제 agent(Claude Code·Codex·OMP)의 `wb-inbox` skill 설치(WBX-AC-22 대상 host). 사용자가 2026-10-29에 미뤘다 | 사용자가 정하는 때 |
| WBX-O08 | 사용자가 바꿀 수 있는 트랙 D orchestrator 결정: 중지한 agent는 page 열기로 시작하지 않음, run마다 첫 열기의 `shell-1` 자동 생성(WBX-D56), 겹치는 자리는 놓지 않음(WBX-D57), 가장 작은 빈 번호(WBX-D48), shell 한도 상한 32(WBX-D46). 구현을 막지 않는다 | WBX-D1·D2 전, 사용자 확인 권장 |
| WBX-O09 | 트랙 E의 실제 확인: 실제 프로젝트에서 TUI와 웹 widget을 함께 쓸 때의 화면·입력·크기 따름, host 인수·넘기기, 명령 중지, 한글 IME(pane widget), 두 기기 | WBX-E4 뒤, 사용자(S7) |
| WBX-O10 | 사용자가 바꿀 수 있는 트랙 E orchestrator 결정과 seam 조합: 프로젝트당 Hub 연결 하나(WBX-D66), 이전 판 backend는 재시작 안내만(WBX-D67), 웹은 backend focus를 바꾸지 않음(WBX-D65 (4)), "작업 공간에서 열기"가 그 프로젝트의 세 widget을 모두 엶(SPEC s5), 실제 TUI(Hub SPEC S8 PTY e2e)와 웹 WebSocket(task-inbox T6 sandbox)을 한 시험에서 함께 쓰는 seam 조합(SPEC s5 "시험") | WBX-E1 전, 사용자 확인 권장(seam 조합은 E2 전 확인 필요) |

## SPEC으로 넘기는 것

- REST: endpoint별 field, 성공 status(201/200 등)와 기존 오류 코드 ↔ HTTP status 대응, 목록 paging·filter, 결과 조회 옵션(summary / full / raw page, artifact_path), 두 단계 취소 token 형식·TTL, REST 응답의 cache header, Origin·CSRF 처리(Bearer 전용), request body 크기 한도.
- `wb_inbox.py` CLI 모양: sub-command·인자, 출력 형식(사람용·JSON), exit code, `.env` 권한 경고 문구, 오류 시 key 가림.
- Cache 정책 화면 목록: `private, no-cache` / `no-store` 대상 route 표, 로그인 화면과 form token의 bfcache 복원 처리, 세션 확인용 endpoint.
- Board agent: 내부 listener의 REST 범위, Board agent env에 `WB_HUB_URL`(내부 loopback 주소)도 넣는지, Hub가 사용자 shell에서 물려받은 `WB_API_KEY`·`WB_HUB_URL`을 지우는 규칙(현 `WB_BOARD_MCP_KEY` 제외 규칙의 대체), Board agent 세트의 일반 shell tab에 system key가 보이지 않게 하는 규칙, ROLE.md 내용, isolation 검사 기준.
- `/skill/` 제공: route 목록, `WB_HUB_URL` 채우기 규칙(Host 검사 통과 주소), content type, 설치본 포함 경로, WBX-D19 설정의 key 이름·값 검사.
- Dashboard JSON: session JSON endpoint의 모양과 갱신 주기, Board agent 상태 점의 값.
- 전역 nav의 좁은 화면 기준 폭, design token 이름.

## 트랙 개요

| 트랙 | 결정 | 주로 닿는 곳 | 순서 |
|---|---|---|---|
| A 웹 빠른 수정 | WBX-D01~D04 | `render.py`, `web/middleware.py`, `pages/settings.py`, static JS·CSS | B와 병행. 진행 중 C의 mockup 선택을 받는다 |
| B 외부 요청 API | WBX-D05~D11 | `mcp.py` 대체 REST module, `board/launch.py`, `omp_bridge/board/ROLE.md`, `skills/`, packaging, MCP 테스트 이전, 문서 갱신 대상 | A와 병행. WBX-O02 probe가 Board 전환의 선행 조건 |
| C 디자인 | WBX-D12~D14 | CSS 전체, `render.py`, `board_status.py`, 로그인 화면, dashboard JS | Mockup 선택 뒤. A의 `render.py` 변경 뒤에 시작 |
| D Board workspace | WBX-D42~D58 | `hub/board/{set,ws,launch,registry}.py`, `hub/auth.py`(system key), `inbox/service.py`(출처 이름), `rest.py`(내부 listener), `web/app.py`·`pages/board.py`, `static/board*.js`·`board.css`, `server.py`·`cli.py`(status) | WBX-A8(IME) 뒤. D1(server) → D2(화면) → D3(문서·통합) |
| D 보충 Board shell | WBX-D68·D69 | `hub/board/launch.py`(`prepare_shell`), `hub/config.py`(템플릿 주석), USAGE·task-inbox SPEC의 `shell` 줄 | WBX-D4, 선행 없음 |
| E 프로젝트 pane | WBX-D59~D67 | `contracts/ui_v1.py`, `backend/ui_server.py`, `backend/service.py`(fan-out·크기), Hub 중계(`hub/board/` 새 module, `ws.py`), `web/pages/board_api.py`·`projects.py`, `static/board*.js`, 감사 | 트랙 D(D2·D3) 뒤. E1(backend) → E2(Hub 중계·endpoint·감사) → E3(화면) → E4(문서·통합) |

## 다음 단계

사용자가 이 초안을 승인하면 to-tickets 절차로 SPEC과 트랙 A/B/C ticket을 만든다. 남은 확인은 WBX-O01(Chrome)과 WBX-O02(probe)다. 동작 변경은 TDD로 하고 seam은 계획 단계에서 사용자 확인을 받는다.

2026-10-28(r0.3): 새 ticket WBX-A6(`/dashboard`, 뒤로 가기), WBX-B11(agent별 skill 설치 README), WBX-B12(공유 OMP 로그인 완화)는 SPEC s3·PLAN p3에 있다. 새 seam은 없다(S1·S3·S4·S5·S6·S8과 기존 backend 격리 seam을 쓴다).

2026-10-29(r0.5): 새 ticket WBX-A8(한글 IME, WBX-D41)과 트랙 D의 WBX-D1(server: registry·수명·신원·공유 연결), WBX-D2(workspace 화면), WBX-D3(문서·통합 확인)는 SPEC s4·PLAN p5에 있다. 새 seam은 없다(S1·S3·S5·S6·S7·S8과 기존 Board seam을 쓴다). 요구사항 정본은 requirements-p5.json(WBX-AC-01~38).

2026-10-29(r0.6): 트랙 E의 WBX-E1(backend ui_v1 공유 attach·fan-out·크기·입력 규칙, Opus, 독립 검증), WBX-E2(Hub 중계·WebSocket endpoint·pane 동작 API·감사, Opus), WBX-E3(pane widget·버튼·panel "프로젝트" 묶음·프로젝트 화면 링크, Sonnet), WBX-E4(USAGE·문서·통합, Sonnet)와 Board shell의 WBX-D4(Sonnet)는 SPEC s5·PLAN p6에 있다. 기존 seam(backend ui_server seam = Hub SPEC S5, Hub SPEC S6·S8, task-inbox T5·T6, S1·S3·S5·S6·S7·S8)을 쓴다. 실제 TUI와 웹 WebSocket을 한 시험에서 함께 쓰는 seam 조합은 사용자 확인 대상이다(WBX-O10). 요구사항 정본은 requirements-p6.json(WBX-AC-01~48).

2026-10-28(r0.4): 새 ticket WBX-A7(뒤로 가기로 다시 보인 화면을 세션 확인까지 가림, WBX-D36)은 PLAN p4에 있다(완료). WBX-D37~D40은 이미 통합된 구현(B12·B8·B5)과 사용 안내(B9)를 기록한 것이다. 새 seam은 없다(A7은 S1·S8).
