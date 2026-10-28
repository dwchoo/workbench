# Web Board 사용성·외부 요청 API — 요구사항 합의

Revision: r0.4; state: 승인(사용자 승인 2026-10-27; r0.2는 같은 날 SPEC s1 확인 항목 U1~U10의 사용자 답을 WBX-D20~D28과 WBX-D14에 더함; r0.3(2026-10-28)은 사용자 실제 Chrome 확인(WBX-A5 일부)의 피드백과 그에 따른 결정 WBX-D29~D35, 수락 기준 WBX-AC-21~23, 2026-10-27 OMP 로그인 유실 기록을 더함; r0.4(2026-10-28 오후)는 WBX-A6·B12·B4 재실행·B5·B8 뒤의 결정 WBX-D36~D40, WBX-AC-03 보충 2, WBX-A5 중간 결과와 B4 재실행 결과를 더함). 작성일: 2026-10-27.
WBX-D01~WBX-D16은 2026-10-27 요구사항 인터뷰(grilling)의 사용자 결정이며 사용자가 같은 날 각 결정을 확인했다. WBX-D17~D19는 같은 날 초안의 남은 확인(구 WBX-O03~O05)에 대한 사용자 답이다. WBX-D20~D28과 WBX-D14의 선택은 같은 날 SPEC s1의 확인 항목(U1~U10)에 대한 사용자 답이다. WBX-D29~D40은 2026-10-28 결정이며 행마다 [사용자 결정] / [orchestrator 결정]을 표시했다. 문서 승인은 구현 권한과 별개다.
ID 접두사는 `WBX-`다. `WU-`는 [workbench-update](../workbench-update/BRIEF.md)(WU-AC-01~08, ticket WU-01~03), `UX-`는 dev-validation-log-retention의 cli-ux가 이미 쓴다.

이 문서는 [Task Inbox & Web Board BRIEF](../task-inbox/BRIEF.md)(이하 (A), D-05~D-36)와 [Hub BRIEF](../workbench-hub/BRIEF.md)(이하 (B))가 만든 Web Board 위에서, 사용성 개선과 외부 접점 교체를 다룬다. 여기서 바뀌는 기존 결정은 "개정되는 기존 결정"에 모으고, 기존 문서에는 이 문서를 가리키는 짧은 개정 주석만 단다. 관련 ADR: [ADR-0006](../../adr/0006-rest-skill-single-external-contact.md).

## 출처

- 사용자 PRD: [USER-PRD-20261027.md](USER-PRD-20261027.md)(원문 보존, 수정하지 않는다).
- PRD 항목과 결정의 대응: §1 디자인 → WBX-D12~D14. §2 진입 버튼 → WBX-D01. §3 뒤로 가기 경고 → WBX-D02, 평문 HTTP 배너 → WBX-D03. §4 외부 작업 요청 → WBX-D04~D11. PRD의 "MCP 설정 복붙"은 MCP 제거(WBX-D05)에 따라 WBX-D04로 대체되었다. PRD "주의점"의 "필요한 확장 기능은 필요시 구현"은 WBX-D16(제안으로만 기록, 승인 뒤 구현)으로 좁혔다.

## 문제, 사용자와 목표

- [사용자 PRD] 웹이 정돈되지 않았고 첫 화면이 dashboard처럼 보이지 않는다. Board agent·설정·API key·감사 기록으로 가는 버튼이 없어 주소를 직접 쳐야 한다. 평문 HTTP에서 뒤로 가기마다 Chrome 경고가 뜨고, 평문 HTTP 경고 배너를 닫을 수 없다.
- [사용자 PRD] 외부 agent가 API key로 보호된 경로로, 특별한 프로그램 없이 skill 하나로 요청을 등록할 수 있어야 한다. Skill 설치는 README 링크 하나로 끝나고 key는 env 파일로 관리한다. API 접근은 웹과 같은 규칙으로 허용·차단한다.
- [목표] (1) 어느 화면에서든 한 번에 주요 기능으로 간다. (2) 외부 접점을 REST API와 `wb-inbox` skill 하나로 줄인다(MCP 제거). (3) 손으로 쓴 CSS design system과 dashboard로 첫 화면을 바꾼다.

## 공통 용어

[CONTEXT.md](../../../CONTEXT.md)를 따른다. 특히 요청, 요청 출처, 요청 상태, API key, API key 등급, Board agent, Board agent 세트, Hub, 소유자, 대기 사유, 결과 요약 / 결과 전체 보기. 이 문서가 더하는 용어: 외부 요청 API, Workbench 사용자 skill, `wb-inbox` skill(CONTEXT.md에 추가).

- 전역 nav: 모든 로그인 화면의 header에 고정된 주요 화면 링크 묶음(WBX-D01).
- 평문 HTTP 표시: 경고 배너를 닫은 뒤 header에 남는 작은 "평문 HTTP" 표시(WBX-D03).

## 범위와 비목표

- [포함, 트랙 A] 전역 nav(WBX-D01), cache 정책과 bfcache 복원 시 세션 확인(WBX-D02), 평문 HTTP 배너 닫기(WBX-D03), API key 발급 화면의 client 설정 안내(WBX-D04).
- [포함, 트랙 B] MCP 제거와 `/api/v1/` REST API(WBX-D05~D07), repo `skills/`의 `wb-inbox` skill과 Hub의 skill 제공(WBX-D08~D10), Board agent의 skill 전환(WBX-D11).
- [포함, 트랙 C] CSS design system(WBX-D12), dashboard `/`(WBX-D13; 2026-10-28부터 주소는 `/dashboard`, WBX-D29), 구현 전 mockup 선택(WBX-D14).
- [포함, 2026-10-28 추가] `/dashboard` 이동과 로그아웃 뒤 뒤로 가기 수정(WBX-D29·D34), agent별 skill 설치 README(WBX-D30·D35), 공유 OMP 로그인 완화(WBX-D32)와 probe 재실행(WBX-D33).
- [계속 제외] (A) D-08의 제외 목록(웹의 manager·worker·host pane 조작, 파일 첨부, 외부 알림, OAuth 등), rate limit, CDN·CSS/JS framework, 새 runtime 의존성. API key로 terminal·승인 명세 연결·backend 제어(P2-AC-13·20).
- [제외] MCP와 REST의 병행 기간(WBX-D07), 별도 API 허용 목록(WBX-D06), GitHub 게시(사용자가 한다, WBX-D10), 이 목록 밖 기능의 구현(WBX-D16).

## 제약과 호환성

- [유지, (A)] API key 등급·scope(D-12, D-34), idempotency(P2-AC-02·03), 두 단계 취소(D-33), 입력 한도(D-36), 결과 공개 범위(D-22), key 원문 1회 표시(D-12), 웹 기본 비활성과 `[web] enabled`(D-10, 2026-10-26), `allow_from`(2026-10-27), CSP `script-src 'self'`(inline JS 없음), CDN 없음(P2-AC-37).
- [유지, (A)] Manager의 검토 결과 도구는 MCP가 아니라 bridge `workbench_inbox` → backend → hub_v1 Unix socket이다. 이 문서는 그 경로를 바꾸지 않는다.
- [유지, ADR-0003] Board agent는 sandbox 없이 사용자 권한으로 실행되고 bash가 있다. 보호는 로그인·bind·감사 기록에 의존한다.
- [기존 규칙, AGENTS.md] skill 관련 문서는 영어가 기본이다. `skills/README.md`, `SKILL.md`, `.env.example` 주석은 영어로 쓴다. Commit·게시는 별도 요청이 있어야 하고 push는 사용자가 한다.
- [기존 계약, D-29] 새 의존성을 더하지 않는다. `wb_inbox.py`는 Python 3 표준 라이브러리만 쓴다.

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

## 결정과 근거

WBX-D01~D28은 [사용자 결정 2026-10-27]이다. WBX-D29~D40은 2026-10-28 결정이며 각 행 앞에 출처를 표시했다(r0.4: D36~D40). "개정"은 이 결정이 바꾸는 기존 결정이다.

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

## 남은 확인

| ID | 내용 | 시점 |
|---|---|---|
| WBX-O01 | WBX-D02의 효과를 사용자의 실제 Chrome에서 확인한다. "Always use secure connections" 설정 상태를 같이 기록한다(WBX-AC-04). 2026-10-28 일부 확인(위 관측 사실). `/dashboard` 뒤로 가기 경고는 해소. 남은 것: WBX-A7 뒤 로그아웃 → 뒤로 가기 재확인, 자동 갱신 없는 화면 사이 뒤로 가기, Chrome 설정 상태, 복사 버튼, `/board`, 실제 agent의 skill 설치 | 트랙 A 구현 뒤, 사용자 |
| WBX-O02 | Board overlay 아래 OMP의 `wb-inbox` skill 로드와 bash REST 호출 probe(WBX-AC-17). 실패 시 대안 결정. 2026-10-27 1차는 inconclusive(로그인 사용 불가). 해소: WBX-B12 뒤 재실행(WBX-D33) 2026-10-28 passed, WBX-B5 진행 | 트랙 B 전환 전 |
| WBX-O03 | 해소: WBX-D17 | — |
| WBX-O04 | 해소: WBX-D19 | — |
| WBX-O05 | 해소: WBX-D18 | — |

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

## 다음 단계

사용자가 이 초안을 승인하면 to-tickets 절차로 SPEC과 트랙 A/B/C ticket을 만든다. 남은 확인은 WBX-O01(Chrome)과 WBX-O02(probe)다. 동작 변경은 TDD로 하고 seam은 계획 단계에서 사용자 확인을 받는다.

2026-10-28(r0.3): 새 ticket WBX-A6(`/dashboard`, 뒤로 가기), WBX-B11(agent별 skill 설치 README), WBX-B12(공유 OMP 로그인 완화)는 SPEC s3·PLAN p3에 있다. 새 seam은 없다(S1·S3·S4·S5·S6·S8과 기존 backend 격리 seam을 쓴다).

2026-10-28(r0.4): 새 ticket WBX-A7(뒤로 가기로 다시 보인 화면을 세션 확인까지 가림, WBX-D36)은 PLAN p4에 있다(완료). WBX-D37~D40은 이미 통합된 구현(B12·B8·B5)과 사용 안내(B9)를 기록한 것이다. 새 seam은 없다(A7은 S1·S8).
