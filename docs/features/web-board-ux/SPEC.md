# Web Board 사용성·외부 요청 API — 구현 명세

Revision: s5; source brief: [BRIEF.md](BRIEF.md) r0.6 (사용자 승인 2026-10-27, r0.3~r0.4는 2026-10-28 결정, r0.5·r0.6은 2026-10-29 결정 추가). state: 초안(bundle 승인은 digest와 함께 따로 기록). 작성일: 2026-10-27. s2(2026-10-27): s1의 확인 항목 U1~U10에 사용자가 모두 답했다(BRIEF WBX-D14 선택, WBX-D20~D28). 바뀐 곳: seam 확정, JS node seam(S8), A5 범위, 내부 listener key 제한, `submit --new`, 트랙 C 선택. s3(2026-10-28): 사용자 실제 Chrome 확인의 피드백과 결정(BRIEF WBX-D29~D35, AC-21~23). 바뀐 곳: dashboard 주소 `/dashboard`와 `/` 303, `back_forward` navigation의 세션 확인과 pagehide 정리, skill README 설치 계약, 공유 OMP 로그인 완화, B1 검증 관찰 메모. 새 seam은 없다. 2026-10-28 주석(revision 유지): BRIEF r0.4의 WBX-D36~D40이 이 문서의 몇 값을 정하거나 바꾼다. 해당 자리에 `[BRIEF r0.4 WBX-Dnn: …]` 주석을 달았다(WBX-A7, B12 최종 값, result 400, foreground env, ROLE.md 경로). s4(2026-10-29): BRIEF r0.5의 WBX-D41(한글 IME, WBX-A8)과 트랙 D "Board workspace"(WBX-D42~D58, AC-24~38). 바뀐 곳: 새 절 "Board terminal 한글 IME"와 "트랙 D: Board workspace", 수락 기준 대응, 테스트 결정의 트랙 D 명령, ticket 그래프. s3까지의 절은 바꾸지 않았다. 새 seam은 없다. s5(2026-10-29 오후): BRIEF r0.6의 트랙 E "Board workspace의 WB 프로젝트 pane"(WBX-D59~D67, AC-39~47)과 Board shell 기본 program(WBX-D68·D69, AC-48). 바뀐 곳: 새 절 "트랙 E"와 "Board shell 결정", s4 "Shell" 줄의 개정 주석, 문서 갱신 배정·수락 기준·테스트 결정·열린 항목·ticket 그래프의 s5 행. s4까지의 절은 주석 말고 바꾸지 않았다. 새 seam은 없고, seam 조합 하나(실제 TUI + 웹 WebSocket)를 확인 대상으로 표시했다(BRIEF WBX-O10).

BRIEF의 결정 WBX-D01~D69와 수락 기준 WBX-AC-01~48(AC-03 보충 포함)은 바꾸지 않는다. "기술 보완"은 코드 조사(2026-10-27, HEAD `4898f39`, 작업 트리에 미commit 문서 변경 있음)로 정한 구체 형태다. 약속된 동작·호환성을 바꿀 수 있는 항목은 "열린 결정·사용자 확인 항목"에 모았고 정하지 않았다. Test seam S1~S8은 사용자가 확정했다(WBX-D20·D21). 트랙 C는 mockup 방향 A로 정해졌다(WBX-D14, 아래 "트랙 C 선택").

## 문제와 해결 방향

웹은 주요 화면으로 가는 길이 없고, 평문 HTTP에서 뒤로 가기가 매번 다시 요청되며, 경고 배너를 접을 수 없다. 외부 접점은 MCP라 client마다 설정이 다르다. 해결은 세 트랙이다.

1. 트랙 A: `render.layout`에 전역 nav와 접히는 평문 HTTP 표시를 넣고, 로그인한 HTML 화면의 cache 정책을 `private, no-cache`로 바꿔 bfcache를 허용하며, 복원된 화면은 세션을 확인한다. Key 발급 결과 화면에 `.env`·skill 링크·agent 안내를 보인다.
2. 트랙 B: MCP tool 9개의 처리 계층을 중립 module로 옮기고(expand), 그 위에 `/api/v1/` REST를 얹는다. Repo `skills/wb-inbox/`에 stdlib client를 두고 Hub가 `/skill/`로 제공한다. Probe(WBX-O02) 통과 뒤 Board agent를 내부 listener REST + skill로 바꾸고(migrate), 테스트를 REST로 옮긴 뒤 MCP를 지운다(contract).
3. 트랙 C: mockup 선택 뒤 CSS design system과 dashboard.

## 관련 사용자 이야기

- 소유자: 어느 화면에서든 Board agent·API key·감사 기록·비밀번호 변경에 두 번 안에 간다. 뒤로 가기가 매끄럽고, 경고 배너를 접어 둔다.
- 소유자: key를 발급하면서 `.env` 블록을 복사해 agent 머신의 파일에 붙여 넣고, agent에게는 key 없는 한 줄 안내만 준다.
- 외부 agent(Claude Code·Codex·OMP): README를 따라 `wb-inbox` skill을 설치하고 `python3 wb_inbox.py submit …`으로 요청을 낸다.
- Board agent: 같은 skill과 bash로 내부 loopback REST를 쓴다.

## 범위와 비목표

- 포함: BRIEF 트랙 A·B 전체, 트랙 C의 ticket 구조와 선행 gate.
- 비목표: BRIEF "계속 제외"·"제외" 그대로. 추가로 CORS(브라우저 cross-origin 호출 지원), REST의 cookie 인증, `wb_inbox.py`의 사람용 표 출력, Python 3.8 미만 지원, `/skill/` 목록 화면(directory index).
- 지원 환경: core와 같은 Linux Hub. `wb_inbox.py`는 Python ≥ 3.8 stdlib이 있는 POSIX client(이 repo의 테스트는 `.venv` Python 3.12로만 실행). 브라우저는 최신 Chromium·Firefox(실제 확인은 사용자 Chrome).

## 공개 인터페이스, 데이터 계약, 불변 조건

### 전역 nav(WBX-D01, AC-01, 기술 보완)

- 로그인한 화면(`render.layout`에 세션 csrf가 있는 경우)의 `<header>` 구성, 왼쪽부터: brand(`/dashboard`), `<nav aria-label="주요">`: 대시보드 `/dashboard` · 요청 `/requests` · 새 요청 버튼 `/requests/new` · Board agent `/board`(상태 점) · 설정 메뉴. 오른쪽: 알림 badge(`#wb-badge`, 기존 `badge.js`) · 로그아웃 form.
- 설정 메뉴는 `<details class="nav-settings"><summary>설정</summary>` 안에 비밀번호 `/settings` · API key `/settings/keys` · 감사 기록 `/settings/audit`. Summary 클릭 1번 + 링크 1번 = 두 번(AC-01).
- 현재 화면 강조: 해당 링크에 `aria-current="page"`. 판정은 경로 prefix: `/dashboard`(정확히)·`/projects/…` → 대시보드(s3, WBX-D29; s2까지는 `/`), `/requests…` → 요청(단 `/requests/new`는 새 요청 버튼), `/board…` → Board agent, `/settings…` → 설정 summary와 그 하위 링크.
- 좁은 화면(`max-width: 45rem`): CSS만으로 nav 항목을 한 줄 toggle로 접는다(`<input type="checkbox" id="wb-nav-toggle" class="nav-toggle">` + `<label for>` 방식, JS 없음). Badge·로그아웃은 접지 않는다.
- Board agent 상태 점: `<span class="dot dot-<값>" title="<한국어 상태>">`. 값은 `on`(agent `starting`/`running`), `off`(세트 없음·`not_started`·`stopped`·`exited`), `error`(`isolation_failed` 등 그 밖의 실패 state, Board 설정 무효). 서버가 화면을 그릴 때의 값이다(자동 갱신 없음; 트랙 C dashboard가 JSON으로 갱신). `WebServices`에 `board_state: Callable[[], str]`(비차단, 기본 `"off"`)를 더하고 Hub가 `BoardSet.status()`로 채운다.
- 로그인 화면·오류 page(세션 없음)에는 nav가 없다. 기존 `settings.settings_nav()`는 그대로 둔다(설정 화면 내부 tab).
- 구현 형태: `render.layout(..., nav=NavContext|None)`. `NavContext(path, board_state)`는 각 page module의 frame helper(`requests._frame`, `settings._page`, `board_status.frame`, `login.settings_page` 등)가 `request`에서 만든다.

### Cache 정책과 bfcache(WBX-D02, AC-02·03, 기술 보완)

`security_headers`(on_response_prepare)의 규칙을 아래 표로 바꾼다. 판정은 요청 경로·method·응답 content type·세션 유무로 한다.

| 대상 | `Cache-Control` |
|---|---|
| 세션이 확인된 page route(`access_of == "page"`)의 `text/html` 응답(200·4xx page, `stale_form_page` 포함) | `private, no-cache` |
| 예외: `POST /settings/keys`의 응답(key 원문 화면·발급 실패 화면), `GET /requests/{rid}/raw`, `/full`, `/artifact` | `no-store`(유지) |
| `GET/POST /login`(세션 전, login token 포함), `POST /logout`, 모든 redirect(303), 본문 없는 403·421·413 | `no-store`(유지) |
| JSON: `/api/badge`, `/api/projects/…`, `/api/v1/…`(REST), WebSocket upgrade | `no-store`(유지) |
| `/skill/…` | `no-cache`(Host마다 내용이 다름) |
| `/static/…` 200 | `public, max-age=3600`(유지). `/favicon.ico` 204는 `public, max-age=86400`(유지) |

- bfcache 복원 확인: 로그인한 모든 화면에 `/static/session-check.js`(defer)를 싣는다. `pageshow` 이벤트의 `event.persisted`가 참이면 `fetch("/api/badge", {credentials:"same-origin", cache:"no-store"})`를 보내고 401이면 `location.replace("/login")`. 네트워크 오류면 아무것도 하지 않는다(Hub 재시작 중일 수 있음). 세션 확인 endpoint는 새로 만들지 않고 기존 `/api/badge`(세션 API, 세션 없으면 401)를 쓴다. `/api/badge`의 응답 형태는 바꾸지 않는다.
- s3 보충(WBX-D34): 위 확인은 `event.persisted`가 참일 때와, 화면이 `back_forward` navigation으로 그려졌을 때(`performance.getEntriesByType("navigation")[0].type === "back_forward"`, HTTP cache에서 다시 그린 화면은 persisted `pageshow`가 없다) 모두 한다. 아래 "Dashboard 주소와 뒤로 가기" 절.
- 복원된 화면의 상태 변경: 기존 세션·CSRF 검사가 막는다(로그아웃 뒤면 `303 /login`, 다른 세션이면 `stale_form_page` 403). 추가 서버 동작 없음(AC-03).
- 로그인 화면: `no-store` 유지. bfcache로 복원되면(일부 브라우저는 `no-store`도 복원한다) `/static/login-restore.js`가 `pageshow.persisted`에서 `location.reload()`해 새 login token·cookie 쌍을 받는다. Login token이 맞지 않는 제출은 기존 "로그인 화면이 만료되었습니다" 403 경로다.
- Key 발급 결과(POST 응답, `no-store`): 새로 고침은 같은 이름으로 재발급을 시도하므로 `name` UNIQUE로 거절되어 원문이 다시 보이지 않는다. 뒤로 가기는 POST 재전송 확인이 되고 같은 거절이다(AC-06의 "다시 보이지 않는다", 기존 동작 확인만).
- 사용 안내: task-inbox USAGE §10에 "뒤로 가기마다 Chrome 경고" 항목을 더해 TLS(`wb hub tls --self-signed`)가 근본 해결임과 Chrome "Always use secure connections" 설정의 영향을 적는다(AC-04 문서 부분).

### Dashboard 주소와 뒤로 가기(WBX-D29·D34, AC-21·AC-03 보충, s3)

- Route: dashboard handler는 `GET /dashboard`(`render.HOME_PATH = "/dashboard"`)다. `GET /`는 세션이 있으면 `303`, `Location: /dashboard`(상대, 기존 redirect 규칙), 세션이 없으면 기존 session middleware의 `303 /login`. `/`는 page가 아니라 redirect이므로 cache 표의 "모든 redirect" 행(`no-store`)이다. `/dashboard`는 세션 page 행(`private, no-cache`)이다.
- 로그인 성공 뒤 이동과 그 밖에 s2까지 `/`로 보내던 서버 redirect, 같은 origin `Referer`가 없을 때의 돌아갈 곳(`middleware` fallback), brand·nav·프로젝트 화면의 "프로젝트 상태" 링크는 모두 `/dashboard`. Route audit에 `/dashboard`(page)와 `/`(redirect)를 pin한다. `GET /api/dashboard`(JSON)는 바꾸지 않는다.
- 세션 확인(`/static/session-check.js`): `pageshow`에서 `event.persisted === true`이거나 이번 문서의 navigation type이 `back_forward`이면 `/api/badge`(`cache: "no-store"`)를 부르고 401이면 `location.replace("/login")`. 같은 문서에서 한 번만 확인한다(첫 `pageshow`와 이후 persisted 복원마다). 네트워크 오류·그 밖의 status는 아무것도 하지 않는다(s2와 같음). Navigation Timing이 없는 browser는 persisted 경로만. [BRIEF r0.4 WBX-D36: `session-check.js`는 `<head>`에서 동기로 실리고, 확인할 때(`back_forward`·persisted) `<html>`에 가림 class(`wb-checking`)를 붙여 `/api/badge` 답(2xx·그 밖 오류) 또는 4 s timer에 푼다. 401이면 보이지 않은 채 `location.replace("/login")`. WBX-A7]
- `Clear-Site-Data`는 쓰지 않는다(비보안 origin에서는 browser가 무시한다). 서버 쪽 로그아웃 동작은 바뀌지 않는다.
- bfcache 적격성: 화면 JS는 `pagehide`에서 진행 중인 `fetch`를 `AbortController`로 끊고 timer(`setTimeout`/`setInterval`)를 멈추며, `pageshow`(persisted)에서 다시 시작한다. 대상: `dashboard.js`, `badge.js`, 그 밖에 timer·fetch가 있는 화면 JS. `unload`·`beforeunload` handler를 두지 않는다. 끊긴 fetch의 `AbortError`는 오류 표시를 하지 않는다.
- 진단(test seam 아님): A6 worker는 headless Chrome DevTools(`Page.backForwardCacheNotUsed` event 또는 `PerformanceNavigationTiming.notRestoredReasons`)로 `/dashboard`·`/requests` 등이 bfcache에서 복원되지 않는 이유를 수정 전후로 기록한다. 이 결과는 ticket 출력의 관찰이며 RED/GREEN 근거가 아니다. Chrome이 없으면 진단 없이 진행하고 그렇게 적는다.
- 한계: 평문 HTTP에서 HTTPS-First 경고 자체는 서버가 막을 수 없다(AC-04, TLS가 근본 해결). 이 절은 경고를 부르는 재요청 중 서버·JS가 줄일 수 있는 것(bfcache 부적격)만 다룬다.

### 평문 HTTP 배너 접기(WBX-D03, AC-05)

- 경고 조건(`services.insecure_warning`)이 참이면 page에 둘 다 그린다: 배너 `<div class="banner insecure" role="alert">`(기존 문구 + `<button type="button" class="banner-close" hidden>닫기</button>`)와 header 안의 `<span class="insecure-chip" tabindex="0" title="<기존 문구 전문>">평문 HTTP</span>`. Chip은 hover·focus 때 전문을 CSS popover로도 보인다.
- `/static/insecure-banner.js`는 `<head>`에서 동기로 실린다(CSP `script-src 'self'`, inline 없음). localStorage `wb.insecureBanner = "collapsed"`면 `<html>`에 class `wb-insecure-collapsed`를 붙인다. CSS: collapsed면 배너 숨김·chip 보임, 아니면 배너 보임·chip 숨김. 따라서 경고 조건 중에는 둘 중 하나가 항상 보인다. JS가 없거나 저장소 접근이 예외를 던지면 배너가 펼쳐진 채이고 닫기 버튼은 `hidden`이다.
- 닫기 버튼(JS가 `hidden`을 푼다) → 저장 + class 추가. Chip 클릭 → 다시 펼치기(저장 값 삭제). 로그인 화면에도 같은 배너·chip이 있다.
- 기억 범위는 브라우저의 origin별 localStorage다(IP 주소와 이름으로 들어온 경우는 따로 기억). `wb hub status`의 경고 줄은 바꾸지 않는다.

### Key 발급 결과의 client 설정 안내(WBX-D04·D17·D19, AC-06)

`POST /settings/keys` 성공 page(`no-store`)에 기존 원문 표시 아래 세 블록을 더한다. 각 블록은 `<pre id>` + `<button type="button" class="copy" data-copy-target="<id>" hidden>복사</button>`.

1. `.env`: 두 줄 `WB_HUB_URL=<origin>` / `WB_API_KEY=<원문>`. `<origin>`은 `<policy.scheme>://<Host header>`(Host 검사를 통과한 값, 예 `http://100.101.102.103:8780`). 아래에 "agent 머신의 `<skill 폴더>/.env` 또는 `~/.config/omp-workbench/client.env`에 붙여 넣고 `chmod 600`" 안내.
2. Skill README 링크: `<origin>/skill/README.md`. `hub.toml [web] skill_repo_url`이 있으면 그 링크(표시 "GitHub")를 더한다.
3. Agent용 한 줄 안내(key 원문 없음, WBX-D17). 영어(agent 입력이므로): `Install the wb-inbox skill by following <origin>/skill/README.md, then use it; its settings (WB_HUB_URL, WB_API_KEY) are in <skill folder>/.env or ~/.config/omp-workbench/client.env.` (s2 문장. s3에서 WBX-B11이 "README 설치 계약"의 요소로 바꾼다.)

- 복사: `/static/copy.js`. `navigator.clipboard.writeText`(secure context에서만 있음) → 없거나 실패하면 대상 텍스트를 선택하고 `document.execCommand("copy")` → 그것도 실패하면 텍스트를 선택한 채 "직접 복사하세요(Ctrl+C)"를 보인다. 평문 HTTP LAN 주소는 secure context가 아니어서 clipboard API가 없다(이 fallback이 필요한 이유).
- `GET /settings/keys`(목록)에는 같은 블록 1~3을 `WB_API_KEY=<YOUR_KEY>`로 접힌 `<details>` 안에 보인다.

### 외부 요청 API `/api/v1/`(WBX-D05·D06·D07, AC-07~12, 기술 보완)

공통:
- Listener: 외부 웹 listener(`create_app`)와 Board agent 내부 listener 둘 다 같은 handler를 등록한다. 외부는 기존 middleware 순서(allow_from → Host → body → Origin → session) 그대로 지나가며 route는 `own_auth`로 표시한다. 내부는 `127.0.0.1:<port>` Host 고정 middleware만 있고 `/api/v1/` 밖은 404다.
- 인증: `Authorization: Bearer <wbk_…>` 정확히 하나. 없거나 형식이 틀리거나 폐기·만료면 401 + `WWW-Authenticate: Bearer realm="workbench-hub"` + `{"error":{"code":"unauthorized","message":"unauthorized"}}`. 세션 cookie는 보지 않는다(cookie만 있으면 401). 인증은 body를 읽기 전에 한다.
- Principal: 기존 `principal_for`(system key → `board_agent`, editor, requester, scope). 기존 key가 재발급 없이 같은 등급·scope로 동작한다(AC-12).
- Body: `POST`·`PATCH`는 `Content-Type: application/json`(아니면 415 `invalid_input`), UTF-8 strict JSON 객체 하나(NaN·Infinity·깊은 중첩·잘못된 UTF-8은 400 `invalid_input`), ≤ 256 KiB(넘으면 413 `too_large`, 선언된 길이로 먼저 거절). 모르는 field는 400 `invalid_input`(MCP `additionalProperties:false`와 같음). `null` 선택 field는 없는 것으로 본다.
- Query: 정수는 부호 없는 10진수, boolean은 `true|false`만, 반복 가능한 것은 `state`만. 그 밖은 400 `invalid_input`.
- 응답: `application/json`, ASCII escape JSON(MCP와 같음), `Cache-Control: no-store`, 기존 보안 header. Untrusted 표시(`untrusted_text`, `note`)는 MCP 결과와 같은 규칙.
- Origin: `Origin`이 있으면 listener 자기 origin이어야 한다(아니면 403 `{"error":{"code":"origin_mismatch",…}}`). 외부 listener는 middleware `refuse()`가 `/api/v1/` 경로에서 이 REST 오류 모양을 쓰고, 내부 listener는 handler가 같은 검사를 한다. CORS preflight는 지원하지 않는다(`OPTIONS` → 405). CSRF token은 쓰지 않는다(Bearer 전용, cookie 무시).
- 동시성: MCP의 보안 수정 2를 그대로 옮긴다. 전용 `DaemonExecutor(8)`, key당 동시 처리 4, 30 s 동안 자리가 없으면 503 `{"error":{"code":"busy"}}`. Rate limit 없음.
- `allow_from`·bind·`[web] enabled`는 웹과 같다(peer 밖은 본문 없는 403, enabled=false면 listener 자체가 없다). REST 전용 허용 목록 없음(AC-11).
- 모르는 `/api/v1/…` 경로: catch-all `own_auth` route가 인증 뒤 404 `not_found`를 준다(없으면 `/api/` prefix의 세션 401 모양이 나온다). 알려진 경로의 다른 method는 405 + `Allow` + `invalid_input`.

Endpoint(결과 필드는 MCP tool 결과와 같다 — task-inbox SPEC "MCP Streamable HTTP endpoint" tool 표):

| method·path | 입력 | 성공 | 등급 |
|---|---|---|---|
| `GET /api/v1/projects` | query `query?` | 200 `{projects:[…]}`(requester는 id·이름만) | 둘 다 |
| `POST /api/v1/requests` | `{project_id, title, body, idempotency_key}` | 새로 만들면 201 + `Location: /api/v1/requests/<id>`, 같은 key·내용의 재제출은 200. 본문 `{request_id, project_id, seq, state, origin, created}` | 둘 다 |
| `GET /api/v1/requests` | query `project_id?`, `state*`(반복), `include_closed?`, `query?`, `limit?`(1~100, 기본 50), `cursor?` | 200 `{requests:[요약], next_cursor}`; 끝이면 `next_cursor:null`. `cursor`는 이전 응답 값을 그대로 쓰는 불투명 문자열 | 둘 다 |
| `GET /api/v1/requests/{id}` | — | 200 `{request:{…}}` | 둘 다 |
| `POST /api/v1/requests/{id}/comments` | `{body}` | 201 `{comment_id, state, version}` | 둘 다 |
| `PATCH /api/v1/requests/{id}` | `{base_version, title?, body?}` | 200 `{state, version, revision, mode}` | editor·board_agent |
| `POST /api/v1/requests/{id}/state` | `{base_version, to:"NEW"\|"NEEDS_INFO", reason?}` | 200 `{state, version}` | editor·board_agent |
| `POST /api/v1/requests/{id}/cancel` | `{confirm_token?, reason?}` | 실행 전 200 `{status:"cancelled", state:"CLOSED"}`; READY 202 `{status:"cancel_requested", …}`; RUNNING 첫 호출 200 `{status:"confirm_required", confirm_token, expires_in:60, impact}`; token 확인 202 `{status:"cancel_requested", stop, cancel?}` | 둘 다 |
| `GET /api/v1/requests/{id}/result` | query `full?`, `run_id?`, `artifact_path?`, `offset?`(≥0, 기본 0), `length?`(1~131072, 기본 131072) | 200: 기본은 요약, `full=true`면 전체 보기, `run_id`/`artifact_path`가 있으면 `raw`/`artifact` page(`{offset, length, size, next_offset, text\|data_base64, missing,…}`, `Results`의 기존 모양) | 둘 다 |

- Requester가 editor 전용 endpoint를 부르면 403 `forbidden`(MCP는 tool이 목록에 없었다. REST는 경로가 있으므로 거절로 보인다).
- 두 단계 취소(AC-10): token = `secrets.token_urlsafe(24)`, TTL 60 s(monotonic), (principal kind, key_id, request_id, 취소 확인 fingerprint)에 묶이고 한 번 쓰면 사라진다. key당 살아 있는 token 32개. 없음·틀림·만료·다른 key·다른 요청·fingerprint 변경은 409 `token_mismatch`이고 cancel port를 부르지 않는다. 확인 경로는 기존 `InboxService.cancel(base_fingerprint=…)` → P2-AC-30 경로. MCP `_inbox_cancel`의 판정 순서와 `resends_cancel` 예외를 그대로 옮긴다.
- 결과 paging: client는 `next_offset`이 `size`에 닿거나 `null`일 때까지 `offset=next_offset`으로 반복한다. Backend가 꺼져 있으면 raw는 `backend_not_running`(기존).
- Idempotency(AC-07): 서버 규칙은 기존 그대로(같은 출처·key·내용이면 기존 요청, 다르면 `idempotency_conflict` 409).
- 범위 밖(AC-08): terminal, 승인 명세 연결, backend 제어, 검토 시작, 검토 방식, API key·비밀번호·감사에 닿는 REST endpoint는 없다. 세션 route(`/api/badge`, `/api/projects/…`, `/ws/board`, `/requests/{rid}/start-review` 등)는 Bearer key만으로는 401/303이다.

메모(s3, WBX-B1 독립 검증의 관찰, 계약 변경 아님):
- 중복 JSON key: 한 객체에 같은 key가 두 번 오면 거절하지 않고 마지막 값을 쓴다(Python `json` 기본). 위 "strict JSON"은 NaN·Infinity·깊이·UTF-8에 대한 것이며 중복 key 거절은 계약에 없다. 바꾸려면 따로 결정한다.
- 인증 검사(`verify`)가 REST 처리와 같은 전용 `DaemonExecutor(8)`를 쓴다. 처리 자리가 모두 오래 걸리면 인증도 기다린다(기존 MCP부터 있던 starvation 위험). 후속 과제로 남긴다(이번 ticket 범위 밖).
- `GET …/result`: 구현은 `run_id`/`artifact_path`를 `full=true`일 때만 받는다(위 표는 조건을 적지 않았다). WBX-B8(MCP 제거) 때 표와 구현 중 어느 쪽에 맞출지 다시 본다. [BRIEF r0.4 WBX-D38: 구현 쪽으로 정했다. `full=true` 없이 `run_id`/`artifact_path`를 주면 400 `invalid_input`]

오류 → HTTP status(본문 `{"error":{"code","message","state"?,"version"?}}`; `code`·`message`는 MCP `error_payload`의 정규화를 그대로 쓴다 — 통과 code는 inbox 문장, 나머지는 고정 문장, 모르는 code는 `internal_error`):

| code | status |
|---|---|
| `invalid_input` | 400 (415·405의 transport 오류도 이 code) |
| `unauthorized` | 401 |
| `forbidden`, `origin_mismatch` | 403 |
| `not_found` | 404 |
| `conflict`, `invalid_transition`, `idempotency_conflict`, `token_mismatch`, `backend_not_running` | 409 |
| `too_large` | 413(field 한도와 body 한도 모두) |
| `internal_error` | 500 |
| `project_unavailable`(`inbox_unavailable` 별칭), `inbox_busy`, `inbox_mismatch`, `backend_unreachable`, `hub_stopping`, `busy` | 503 |

### `wb-inbox` skill과 `wb_inbox.py`(WBX-D08·D09, AC-13·14, 기술 보완)

Repo 배치(영어 문서): `skills/README.md`(첫 줄: "This folder holds Workbench user skills for agents outside Workbench; it is not `.agents/skills/` (Workbench development skills)." 설치 안내, curl 예), `skills/wb-inbox/SKILL.md`(Agent Skills frontmatter `name: wb-inbox`, `description`), `skills/wb-inbox/wb_inbox.py`, `skills/wb-inbox/.env.example`. `.gitignore`에 `skills/**/.env`. Hub 주소 자리표시는 세 문서에서 정확히 `http://YOUR-HUB-HOST:8780` 한 가지 문자열이다(`/skill/` 제공 절의 치환 대상).

README 설치 계약(s3, WBX-D30·D35, AC-22):
- 파일 표: skill의 파일 전부(`/skill/wb-inbox/` 고정 목록과 같은 `SKILL.md`, `wb_inbox.py`, `.env.example`)를 한 줄씩, repo 상대 링크(`[SKILL.md](wb-inbox/SKILL.md)` 형태, GitHub에서 repo 파일로 간다)와 Hub 내려받기 주소(`http://YOUR-HUB-HOST:8780/skill/wb-inbox/<file>` 형태)를 함께 적는다. 상대 링크는 Hub 제공본에서도 `/skill/README.md` 기준으로 같은 제공 파일로 풀린다.
- Agent별 절: `### Claude Code`, `### Codex`, `### OMP` 세 절. 각 절은 그 agent가 skill을 읽는 사용자 폴더(와 project 폴더)를 출처(공식 문서 URL, 공식 문서가 없으면 실행 파일 버전과 확인 방법)와 함께 적고, `sh` code block 하나를 둔다. Block은 skill 폴더를 만들고 세 파일을 Hub에서 내려받고(`curl -fsS`, 없으면 `python3 -I` urllib), `.env`가 없을 때만 `.env.example`에서 만들고 `chmod 600`, 끝으로 `python3 -I <폴더>/wb_inbox.py config`를 실행한다. 내려받기 실패는 0 아닌 exit로 block을 멈춘다. Block에 key는 없다.
- 폴더 값은 B11이 근거로 정한다. 근거 없이 추정한 폴더를 쓰지 않는다. 확인하지 못한 agent는 그 사실과 확인 방법을 README에 적는다.
- Hub 제공본: `/skill/` 제공 절의 두 치환을 그대로 쓴다(새 치환 없음). 따라서 제공본의 주소는 그 Hub의 절대 주소(`<scheme>://<Host>/skill/…`)가 되고 `http://YOUR-HUB-HOST:8780`은 남지 않는다.
- Key 발급 화면(WBX-A4 절 3)의 agent용 한 줄 안내는 영어로 README URL, "your agent's section"(Claude Code, Codex, OMP)을 따라 모든 파일을 설치하라는 말, `.env` 위치를 담는다. Key 원문은 없다(WBX-D17·D26). 정확한 문장은 B11 출력에 적고 USAGE·이 SPEC의 예문을 맞춘다.

CLI(`python3 wb_inbox.py <command> [options]`, argparse, stdlib만):

| command | 인자 | REST |
|---|---|---|
| `projects` | `[--query Q]` | `GET /projects` |
| `submit` | `--project ID --title T (--body TEXT \| --body-file PATH\|-) [--idempotency-key K \| --new]` | `POST /requests` |
| `list` | `[--project ID] [--state S]… [--include-closed] [--query Q] [--limit N] [--cursor C]` | `GET /requests` |
| `get` | `REQUEST_ID` | `GET /requests/{id}` |
| `comment` | `REQUEST_ID (--body TEXT \| --body-file PATH\|-)` | `POST …/comments` |
| `update` | `REQUEST_ID --base-version V [--title T] [--body …\|--body-file …]` | `PATCH …` |
| `set-state` | `REQUEST_ID --base-version V --to NEW\|NEEDS_INFO [--reason R]` | `POST …/state` |
| `cancel` | `REQUEST_ID [--reason R] [--confirm TOKEN]` | `POST …/cancel` |
| `result` | `REQUEST_ID [--full] [--run-id R \| --artifact-path P] [--offset N] [--length N]` | `GET …/result` |
| `config` | — | 요청 없음. 변수별 찾은 위치와 URL을 보이고 key는 `set`/`missing`만 |

- `submit`의 `--idempotency-key`가 없으면 `sha256(project_id \0 title \0 body)`의 앞 32 hex로 `wbi-<hex>`를 만든다(같은 내용의 재시도가 중복을 만들지 않음, WBX-D25). `--new`는 `wbi-new-<uuid4 hex>`로 새 요청을 강제한다(`--idempotency-key`와 함께 쓰면 사용법 오류). 출력에 쓴 key가 포함된다.
- 출력: 성공이면 stdout에 서버 JSON(`indent=2`, `ensure_ascii=False`). HTTP 오류면 stdout에 서버의 오류 JSON, stderr에 `wb_inbox: error <status> <code>: <message>` 한 줄.
- Exit code: 0 성공(2xx, `confirm_required` 포함), 1 서버가 거절(4xx·5xx), 2 사용법 오류(argparse), 3 설정 오류(변수 없음·URL 형식), 4 연결 오류(DNS·연결 거절·TLS·timeout·JSON 아닌 응답·redirect).
- Env 조회(WBX-D09): 변수마다 따로 process env → `<wb_inbox.py가 있는 폴더>/.env` → `$XDG_CONFIG_HOME/omp-workbench/client.env`(절대 경로일 때, 아니면 `~/.config/omp-workbench/client.env`). `.env` 문법: 빈 줄·`#` 주석 무시, `KEY=VALUE`, 선택 `export ` 접두, 값 양끝의 같은 따옴표 제거, 치환 없음. 읽은 파일의 mode에 `0o077` bit가 있으면 stderr `wb_inbox: warning: <path> is accessible by other users (mode 0644); run: chmod 600 <path>`. 없으면 exit 3과 `wb_inbox: WB_API_KEY is not set; looked in: process environment, <path1>, <path2>`.
- `WB_HUB_URL`: `http`/`https`, host 필수, port 선택, path는 비었거나 `/`, userinfo·query·fragment 없음. 아니면 exit 3.
- Key 위생: key 값은 `Authorization` header에만 쓴다. stdout·stderr·예외 문장 어디에도 key와 header 값이 나오지 않는다(오류 문장은 직접 만든 것만, urllib 예외는 종류 이름만). Redirect를 따르지 않는다(3xx는 exit 4; urllib 기본 redirect handler는 `Authorization`을 다른 host로 옮길 수 있다). Timeout 30 s. TLS는 시스템 신뢰 저장소. 자체 서명 인증서는 OpenSSL 표준 `SSL_CERT_FILE`로 신뢰하는 방법만 README에 적고 새 변수는 없다(WBX-D23).
- Board agent에서는 process env의 `WB_API_KEY`(system key)·`WB_HUB_URL`(내부 loopback)이 먼저라 `.env`·`client.env`를 보지 않는다.

### `/skill/` 제공, packaging, `skill_repo_url`(WBX-D10·D19, AC-15, 기술 보완)

- Route(공개 `public`, GET·HEAD만, 외부 listener만): `/skill/README.md`, `/skill/wb-inbox/SKILL.md`, `/skill/wb-inbox/wb_inbox.py`, `/skill/wb-inbox/.env.example`. 고정 목록이며 그 밖의 `/skill/…`은 404(경로 해석·directory index 없음, `.env`는 절대 제공하지 않음).
- 내용: 설치본 파일 bytes(`canonical_path("skills/…")`)에서 두 가지만 바꾼다. (1) `http://YOUR-HUB-HOST:8780` → `<policy.scheme>://<Host>`(Host 검사 통과 값). (2) README의 `<!-- wb:skill-repo -->` 한 줄 → `skill_repo_url`이 있으면 `Source repository: <url>`, 없으면 빈 줄. `wb_inbox.py`는 바꾸지 않는다. 파일에 key는 없다.
- Content type: 모두 `text/plain; charset=utf-8`, `Content-Disposition: inline`, `Cache-Control: no-cache`(실행·렌더링되지 않음, `nosniff`).
- `allow_from`·bind·enabled는 웹 규칙 그대로(AC-11). 내부 listener에는 없다.
- Packaging: `build_support/assets.py`의 `BuildPy`가 `skills/`를 `workbench/_assets/skills/`로 복사한다(ignore: `__pycache__`, `*.pyc`, `.env`). `MANIFEST.in`에 `recursive-include skills *.md *.py .env.example`. Source checkout에서는 repo `skills/`를 그대로 읽는다(`canonical_path`).
- `hub.toml`: `[web] skill_repo_url`(문자열, 기본 `""` = 없음). 값이 있으면 2048자 이하, 공백·제어 문자 없는 ASCII, scheme `https` 또는 `http`, host 필수, userinfo(`@`) 없음. 아니면 `config_invalid:web.skill_repo_url`. `[web]` 표 변경이므로 `wb hub reload`는 기존 규칙대로 웹 listener를 다시 연다. 초기 템플릿에 주석 줄을 더하고 템플릿이 여전히 `HubConfig()`로 parse되어야 한다.

### Board agent 전환(WBX-D11, AC-16·17, 기술 보완)

- 내부 listener: `/mcp` 대신 B1 REST(`/api/v1/…`)만. Host `127.0.0.1:<port>` 고정, 연결 16개, 다른 경로 404, `/skill/` 없음. 받는 key는 이번 Board agent 시작의 system key 하나뿐이다(WBX-D24). 다른 유효 key·이전 system key는 401 `unauthorized`. 외부 listener의 REST는 모든 유효 key를 받는다.
- Board agent env: `hub_environment(base)` + Board home 값 + `TERM` + UTF-8 locale + `WB_API_KEY=<이번 시작의 system key>` + `WB_HUB_URL=http://127.0.0.1:<내부 port>`. 사용자의 같은 이름 값은 덮어쓴다.
- Hub env 위생: `process.PROJECT_ENV`에 `WB_API_KEY`, `WB_HUB_URL`을 더한다(`WB_BOARD_MCP_KEY`는 이전 버전 Board 프로세스에서 시작한 Hub를 위해 남긴다). 따라서 Hub, Hub가 켠 backend, Board shell은 사용자 shell의 값을 물려받지 않는다. [BRIEF r0.4 WBX-D39: 예외로 foreground(`wb _hub --foreground`) Hub 자신의 `/proc/<pid>/environ`에는 시작 때 값이 남는다(커널 보관). 자식과 `wb hub start`·systemd Hub에는 없다. 수용, USAGE §4.2]
- Board shell tab: env = `hub_environment(base)` + `TERM` + locale. `WB_API_KEY`·`WB_HUB_URL`이 없다(system key가 보이지 않음).
- Skill 배치: 시작마다 `<hub state>/board-agent/skills/wb-inbox/`(0700)를 설치본 `skills/wb-inbox/SKILL.md`·`wb_inbox.py`의 복사본으로 다시 만든다(`.env`·`.env.example` 없음). `prepare_board_home(skills_dir=<board skills dir>)`, overlay의 skill·provider 설정은 probe(WBX-B4) 결과로 정한다. 이전 버전의 `<agent dir>/mcp.json`은 시작 때 지운다.
- 격리 검사: `check_isolation(..., allowed_skills=("wb-inbox",))`, 허용 MCP 없음. 실패 조건: leak 하나라도(MCP tool 포함), 또는 `wb-inbox` skill이 보이지 않음(`skill_missing:wb-inbox`) → 기존처럼 `isolation_failed`(사유). Manager·worker의 누출 검사는 바꾸지 않는다.
- `omp_bridge/board/ROLE.md`(영어): MCP tool 목록 대신 `wb-inbox` skill 사용 안내(`python3 <skill>/wb_inbox.py projects|list|…`), D-27 두 안내는 `projects` 출력 기준으로 유지, 요청 텍스트는 data. [BRIEF r0.4 WBX-D40: skill의 절대 경로는 적지 않는다. 모델은 `skill://wb-inbox`로 찾는다(B4 재실행)]
- `allow_from`에서 loopback을 빼도 내부 listener는 영향이 없다(내부 app에 peer 검사 없음).

### 공유 OMP 로그인 완화(WBX-D32·D33, AC-23, s3)

배경: [INCIDENT-20261027-omp-login.md](INCIDENT-20261027-omp-login.md). Workbench는 인증 저장소 내용을 읽지 않는다(C-D64). 이 절은 그 원칙 아래에서 저장소를 망가뜨릴 수 있는 Workbench 쪽 동작을 줄인다.

- 격리 확인 OMP 종료(`backend/launcher.py:check_isolation`의 `_stop_group`, manager·worker·Board 공통): 확인이 끝나면 (1) stdin을 닫고(RPC EOF) 최대 5 s 동안 leader와 group이 스스로 끝나기를 기다린다, (2) 남으면 SIGTERM 후 최대 5 s, (3) 남으면 SIGKILL 후 3 s. 전체 상한은 13 s + reap이며 확인 결과(`cleanup`)에 어느 단계에서 끝났는지(`eof`/`sigterm`/`sigkill`)와 걸린 시간을 남긴다. Timeout·cancel로 끝나는 경로도 같은 순서를 쓴다. 실제 OMP 18.12.6이 RPC stdin EOF로 끝나는지와 걸리는 시간은 B12가 관측해 기록한다. EOF로 끝나지 않으면 (1)은 확인마다 5 s를 더하므로 B12는 멈추고 관측값과 함께 보고한다(유예 값 조정은 orchestrator 판정). 값(5 s / 5 s / 3 s)은 기술 보완이며 상수로 둔다. [BRIEF r0.4 WBX-D37: 최종 값 10 s / 3 s / 3 s(`ISOLATION_STOP_GRACE`·`ISOLATION_TERM_WAIT`·`ISOLATION_KILL_WAIT`). 실제 OMP 18.13.2는 EOF로 약 0.2 s에 끝난다]
- Board OMP log 보존: `<hub state>/board-agent/logs/`(0700, 파일 0600)에 Board agent 시작마다 (a) 격리 확인 OMP의 stderr 전체(지금은 임시 파일에서 64 KiB만 읽고 버린다)와 확인 판정 요약, (b) OMP가 Board home의 agent dir에 쓰는 자체 log(위치는 B12가 OMP 18.12.6에서 확인, 없으면 (a)만)와 Board agent OMP의 종료 상태를 시작 시각 이름의 파일로 남긴다. 상한(기술 보완): 파일당 1 MiB(넘으면 앞부분을 버리고 끝을 남긴다), 최근 10회 시작분. Hub 재시작·Board 재시작 뒤에도 남는다. Log에 system key 값·`WB_API_KEY`가 들어가지 않는다(쓰기 전 이번 key 문자열을 가린다). 인증 저장소 내용은 복사하지 않는다. `wb hub status`·웹 화면은 바꾸지 않는다. [BRIEF r0.4 WBX-D37: 구현된 형태는 `<hub state>/board-agent/omp-logs/`에 OMP 자체 log의 최근 20개 파일(파일당 ≤ 1 MiB, key 가림, symlink 따라가지 않음)과 `board-events.log`(시작·종료·정지 한 줄씩). 위 `logs/`·"최근 10회 시작분"을 대체한다]
- Test helper: `tests/**`, `tools/**`의 어떤 코드도 실제 사용자 `agent.db`(또는 그 `-wal`·`-shm`)를 복사하지 않는다. 실제 로그인이 필요한 live helper는 C-D64대로 symlink를 쓰거나 실제 로그인을 쓰지 않는다. 빈 가짜 store(`write_bytes(b"")` 등 temp HOME의 placeholder)는 해당하지 않는다. 지금 해당하는 코드: `tests/integration/live_real_summary.py`의 `_copy_auth`(`--auth-source`)와 그 독립 테스트. S5 정적 검사가 다시 생기는 것을 막는다.
- Probe 재실행(WBX-D33): B12 통합 뒤 WBX-B4를 사용자의 실제 로그인으로 다시 실행한다. 실행 전 사용자가 `omp`에서 로그인 상태를 확인한다. 이번 실행의 Board·격리 확인 OMP log(위 위치와 같은 형식, probe root 밖 `/tmp/wbx-b4-out-<날짜>/logs/`)를 지우지 않고 남긴다.

### MCP 제거 순서(expand / migrate / contract)

1. Expand(WBX-B1): MCP 처리 계층(principal, 인자 검사, 오류 정규화, untrusted 표시, cancel token, 결과·프로젝트 모양, executor·key 자리)을 `src/workbench/hub/inbox_api.py`로 옮기고 `mcp.py`는 그 위의 JSON-RPC transport만 남긴다(기존 MCP 테스트가 안전망). 그 위에 `src/workbench/hub/rest.py`를 추가해 외부 listener에 등록한다. 이 시점에는 `/mcp`와 REST가 같은 계층을 쓴다(개발 중 상태이며 배포 단위가 아니다 — WBX-D07의 "병행 기간 없음"은 release 기준).
2. Migrate: skill(B2·B3), probe(B4), Board 전환(B5), 테스트 이전(B6·B7).
3. Contract(WBX-B8): `mcp.py`, `/mcp` route, `McpServer`, `Hub.mcp`/`internal_mcp` 이름, `support/mcp_client.py`·`mcp_hub.py`, `probes/omp_mcp_probe.py`, `test_hub_mcp*.py`를 지운다. Route audit에서 `('*','/mcp')` pin과 CSRF 예외를 지운다. `hub.toml` 템플릿·docstring의 `/mcp` 문구를 REST로 바꾼다. `test_web_assets`의 "mcp SDK import 없음" 검사는 유지한다. Backend launcher의 manager·worker MCP 누출 검사는 유지한다.

### Board terminal 한글 IME(WBX-D41, AC-24, s4)

- 증상(사용자 관찰 2026-10-29, 실제 Chrome): Board terminal에서 한글 IME로 "안녕하세요"를 치면 "녕녕녕녕녕안."이 들어간다.
- 고치는 곳은 WBX-A8이 정한다(진행 중). 2026-10-29 작업 트리에 commit 전 후보가 있다: `base.css`의 `textarea { max-width: 100% }`가 xterm의 helper textarea(IME 입력을 받는 숨은 textarea, 폭 0인 `.xterm-helpers` 안)를 폭 0으로 묶어 Chrome이 조합 중인 음절을 caret 0에서 확정한다는 진단과, `board.css`의 `.xterm .xterm-helper-textarea { max-width: none; }` 규칙 및 그 정적 검사(S5). 후보는 A8의 판정 전까지 계약이 아니다.
- 계약: AC-24. 영문 입력·붙여넣기·`onBinary` 경로와 `/board` CSP는 바뀌지 않는다. 트랙 D(WBX-D1·D2)는 A8 통합 뒤에 시작한다(같은 static 파일).

### 트랙 D: Board workspace(WBX-D42~D58, AC-25~38, s4)

현행(task-inbox SPEC "Board agent 세트"·"WebSocket terminal 프로토콜", 코드 `hub/board/set.py`·`ws.py`·`launch.py`, `web/app.py`의 `board_routes`, `pages/board.py`, `static/board.js`·`board-state.js`): `BoardSet`이 고정 channel 두 개(`agent`, `shell`)의 `Slot`을 갖고, `BoardTerminal`이 set 전체에 lease 하나(epoch, run)를 두며, `/ws/board` 하나가 binary header `byte0 = channel, bytes1..4 = epoch`로 두 channel을 나른다. 이 절은 그것을 terminal registry, terminal마다의 공유 연결, agent별 신원으로 바꾼다. 아래에서 "기술 보완"이라고 쓴 값은 이 SPEC이 정한 것이고, 나머지는 BRIEF 결정이다.

#### 데이터 모델: terminal registry

Terminal view(JSON, session API·WebSocket·hub_v1 status가 같은 모양을 쓴다):

| field | 값 |
|---|---|
| `id` | Hub가 정하는 불투명 문자열. `a-` 또는 `s-` + 소문자 hex 12자(`secrets.token_hex(6)`). Agent id는 registry 파일 안에서, shell id는 Hub run 안에서 다시 쓰지 않는다. URL 경로에 그대로 쓴다 |
| `kind` | `agent` / `shell` |
| `name` | WBX-D47·D48의 이름 |
| `state` | agent: `not_started`, `starting`, `running`, `stopped`(소유자가 중지), `exited`(스스로 끝남), `failed`, `isolation_failed`. shell: `running`, `exited`, `failed`. 기존 값의 뜻은 그대로이고 `stopped`만 새 값이다 |
| `pid`, `started_at`, `detail`, `message?`, `exit_status` | 기존 `Slot.view()`와 같다 |
| `clients` | 이 terminal에 지금 붙어 있는 WebSocket 수(0~8) |
| `busy` | 시작·중지·재시작·닫기·삭제가 진행 중이면 true(그동안 같은 terminal의 다른 동작은 409 `terminal_busy`) |

순서: agent 먼저 만든 순서, 그다음 shell 만든 순서.

저장(기술 보완):
- Agent와 한도는 `<hub state>/board-agents/registry.json`(디렉터리 0700, 파일 0600, 소유자 uid, symlink 아님, 임시 파일 → fsync → rename으로 원자적 교체)에 둔다. 내용 `{"schema_version":1,"options":{"agent_limit":4,"shell_limit":10},"agents":[{"id","name","home","created_at"}]}`. `home`은 `<hub state>` 기준 상대 경로로 `board-agent`(이행된 첫 agent) 또는 `board-agents/<id>`만 허용한다.
- Shell은 저장하지 않는다(메모리만, Hub run과 함께 끝남, WBX-D45).
- 상태는 저장하지 않는다. Hub가 다시 시작하면 모든 agent는 `not_started`이고, WBX-D56 (1)에 따라 보이는 widget이 있는 첫 열기에서 시작한다(이전 run에서 중지했던 agent도 같다).
- `hub.sqlite3`에 table을 더하지 않는 이유: `auth.HubDb.open`은 `SCHEMA_VERSION = 1`이 아닌 DB를 거절하므로, version을 올리면 이전 판으로 되돌렸을 때 Hub DB 전체(세션·API key·감사)를 열 수 없다. 별도 파일은 이전 판이 무시한다(BRIEF "호환, 트랙 D").
- 쓰기 순서: 새 registry를 먼저 쓰고 성공한 뒤에 메모리 상태를 바꾼다. 쓰기 실패(디스크 가득 참·EIO 등)면 동작을 `registry_write_failed`로 거절하고 아무 process도 시작·종료하지 않는다.
- 읽기 실패(JSON 오류, schema 다름, mode·소유자·symlink 이상, 허용 밖 값, 이름 규칙 위반, id 중복): Board 전체를 `registry_invalid`로 두고(파일 경로와 고치는 방법을 `message`로) 어떤 terminal도 시작하지 않으며 파일을 덮어쓰지 않는다. Hub의 나머지(목록·요청·REST)는 그대로 돈다.
- 파일이 없으면 Hub가 Board를 만들 때(web enabled) `agent-1` 하나(`home: "board-agent"`, 한도 기본값)로 새로 쓴다. 새 설치와 업데이트가 같은 규칙이다(이행, 아래).

이름(WBX-D47·D48):
- 검사: 문자열 → 앞뒤 공백(`str.strip()`) 제거 → 길이 1~40(code point) → Unicode category `Cc`·`Cf`·`Zl`·`Zp` 문자 없음 → 같은 kind의 다른 terminal과 `casefold()` 비교로 다름. 위반은 `name_invalid` / `name_taken`.
- 기본 이름: 같은 kind에서 `casefold()`한 이름이 `<kind>-N`(N은 앞자리 0 없는 10진수)인 것을 모아 쓰이지 않은 가장 작은 N ≥ 1. 사용자가 다른 이름으로 바꾼 번호는 다시 쓸 수 있다.
- 화면은 이름을 `textContent`로만 쓰고 서버 HTML은 escape한다(AC-38).

#### Agent별 home·메모리·system key(WBX-D44·D58)

- Home: agent마다 현행 `board-agent/`와 같은 구성(`omp-root/`, `skills/wb-inbox/`, `empty/`, `omp-logs/`와 그 안의 `board-events.log`). `prepare_board_home`·`install_board_skill`·`archive_omp_logs`·`record_board_event`는 `<hub state>/board-agent` 고정 대신 그 agent의 home을 받는다. `agent.db`는 home마다 `omp_home` 규칙으로 사용자 store에 symlink한다(복사 없음, C-D64). `--continue`는 그 home에 이전 세션이 있을 때만이다.
- 공유: `omp_bridge/board/ROLE.md` + `role_prompt_file`, `[board_agent] model`·`shell`, isolation overlay, 시작마다의 격리 확인(`allowed_skills=("wb-inbox",)`, WBX-D37 종료 단계), 내부 REST listener 하나, `WB_HUB_URL`.
- System key(기술 보완): agent마다 이름 `board-agent.<agent id>`, `system=1`, editor, 전체 scope, 만료 없음. `rotate_system_key(agent_id)`는 그 이름의 system 행만 지우고 새로 넣는다. 사람의 key 발급은 `board-agent`와 `board-agent.`로 시작하는 이름을 `name_reserved`로 거절한다(이미 있는 사람 key는 그대로 동작한다. 새 system key 이름이 그와 같으면 id를 다시 뽑는다). Agent process가 끝나면(중지·스스로 끝남·격리 실패·삭제·Hub 정지) 그 agent의 system 행을 지운다. Hub가 Board를 만들 때 남은 system 행(이전 판의 `board-agent` 포함)을 모두 지운다. 따라서 key는 그 agent process가 사는 동안에만 유효하다.
- Env: 그 agent의 key를 `WB_API_KEY`로, 공유 내부 주소를 `WB_HUB_URL`로(현행 `board_environment` 그대로). Shell의 env에는 둘 다 없다(현행).
- 내부 listener(`rest.create_internal_app`, `system_only`): key가 system key이고 그 이름의 agent가 지금 `starting`/`running`이며 그 key가 그 agent의 이번 시작 key일 때만 인증한다(WBX-D24 일반화). Principal은 `board_agent(key_id, agent_id)`.
- 출처 이름(WBX-D48): Board agent principal이 요청을 내면 `origin_name`에 그 순간 registry의 이름을 저장한다(`requests.origin_name` column은 이미 있다, schema 변경 없음). `origin_json`의 `label`은 `origin_name`이 있으면 `Board agent · <이름>`, 없으면(이전 요청) `Board agent`. 댓글·편집의 작성자 표시(`author_name` column이 이미 있음)도 같은 규칙을 쓴다(기술 보완). Agent를 삭제해도 저장된 이름은 남는다.
- 삭제: `busy`로 표시 → 살아 있으면 중지(SIGHUP → SIGTERM → 5 s → SIGKILL, 현행 `terminate`) → system 행 삭제 → home 삭제 → registry에서 제거 → 감사. Home 삭제는 디렉터리 fd 기준으로 내려가며 symlink를 따라가지 않는다(`agent.db` symlink는 link만 지우고 대상은 건드리지 않는다). Home의 실제 경로가 `<hub state>` 밖이거나 소유 uid가 다르면 지우지 않고 `delete_failed`. 일부만 지워졌으면 registry 항목은 남기고 `state=failed, detail=delete_failed`(경로·고치는 방법은 `message`), 다시 삭제할 수 있다.
- 이행된 `agent-1`을 삭제하면 `board-agent/`가 지워지고, 그 뒤 만드는 agent는 `board-agents/<id>/`를 쓴다.

#### Shell(WBX-D45·D56)

- 시작은 현행 `prepare_shell`(`[board_agent] shell` / `$SHELL` / `/bin/sh`, cwd `$HOME`, `hub_environment()` + `TERM` + UTF-8 locale). Shell마다 자기 PTY session이다. [s5 BRIEF r0.6 WBX-D68·D69: program 결정은 `[board_agent] shell` → bash → `/bin/sh`이고 `$SHELL`은 보지 않는다. 아래 "Board shell 결정", ticket WBX-D4]
- 닫기: `busy` → `terminate`(현행 순서) → 메모리 registry에서 제거 → 그 terminal의 WebSocket에 `{"t":"gone"}` 뒤 4404 → 감사. 스스로 끝난 shell은 `exited`로 남고 "재시작"·"닫기"를 보인다.
- 첫 열기 규칙(WBX-D56 (3)): `POST /api/board/open`이 왔을 때 이번 Hub run에서 shell을 한 번도 만들지 않았고 지금 shell이 0개면 `shell-1`을 만든다(이름은 기본 이름 규칙).

#### Session API: 화면과 panel 동작(기술 보완)

모두 세션 route(`@api`)다. 세션이 없으면 401, 상태를 바꾸는 method는 `X-CSRF-Token` header가 세션 csrf와 같아야 한다(아니면 403, 기존 middleware). Bearer API key는 읽지 않는다(AC-08·38). 요청 body는 JSON 객체(≤ 4 KiB, 모르는 field는 400 `bad_input`), 응답은 `application/json`, `Cache-Control: no-store`. 응답 모양은 기존 세션 제어 API(`pages/control.py`)를 따른다: 성공 `{"ok":true,…}`, 실패 `{"ok":false,"reason":<code>,"detail":<고정 문장>}`.

| method·path | body | 동작 | 성공 |
|---|---|---|---|
| `GET /api/board` | — | 읽기만 | 200 `{ok, board:{state:"ok"\|"registry_invalid", message?}, options:{agent_limit, shell_limit, agent_range:[1,8], shell_range:[1,32]}, terminals:[view…]}` |
| `POST /api/board/open` | `{visible:[id…]}`(≤ 40) | WBX-D56 (1)·(3): 첫 shell 규칙, `visible` 중 `not_started` agent 시작 | 200 `{ok, terminals}` |
| `POST /api/board/agents` | `{}` | 기본 이름으로 만들고 시작 | 201 `{ok, terminal}` |
| `POST /api/board/shells` | `{}` | 기본 이름으로 만들고 시작 | 201 `{ok, terminal}` |
| `POST /api/board/terminals/{id}/start` | `{}` | agent: `not_started`·`stopped`·`exited`·`failed`·`isolation_failed`에서 시작. shell: `exited`·`failed`에서 시작 | 200 `{ok, terminal}` |
| `POST /api/board/terminals/{id}/stop` | `{confirm:true}` | agent 중지, 메모리 유지 → `stopped` | 200 |
| `POST /api/board/terminals/{id}/restart` | `{confirm:true}` | 둘 다: 끝내고 다시 시작(agent는 `--continue`) | 200 |
| `POST /api/board/terminals/{id}/close` | `{confirm:true}` | shell: 종료하고 제거 | 200 `{ok}` |
| `POST /api/board/terminals/{id}/delete` | `{confirm:true}` | agent: 중지하고 메모리(home) 삭제, 제거 | 200 `{ok}` |
| `POST /api/board/terminals/{id}/rename` | `{name}` | 이름 바꾸기 | 200 `{ok, terminal}` |
| `POST /api/board/options` | `{agent_limit?, shell_limit?}` | 한도 저장 | 200 `{ok, options}` |

오류 code와 status: 400 `bad_input`·`name_invalid`·`confirm_required`(`confirm`이 정확히 `true`가 아님)·`limit_out_of_range`·`wrong_kind`(shell에 stop/delete, agent에 close 등)·`bad_state`(그 상태에서 할 수 없는 동작); 404 `unknown_terminal`; 409 `name_taken`·`limit_reached`·`limit_below_count`·`terminal_busy`; 500 `registry_write_failed`·`delete_failed`; 503 `board_unavailable`(web 꺼짐·Hub 정지 중)·`registry_invalid`.

- 확인(`confirm:true`)은 화면의 확인 dialog를 거쳤다는 표시이고, CSRF가 cross-site 호출을 막는다. 두 단계 token은 쓰지 않는다(현행 `/board/restart` 확인 page와 같은 수준).
- 동시성(기술 보완): registry 쓰기는 하나의 lock으로 직렬화한다. 시작·중지·삭제는 terminal별로 직렬화하고(`busy`), 한 terminal의 느린 종료(최대 5 s + reap)가 다른 terminal의 동작을 그만큼 막지 않는다(현행 `BoardSet.lock` 하나를 terminal별 lock + registry lock으로 나눈다). Hub 정지는 모든 진행 중 동작보다 우선한다(정지가 시작되면 새 동작은 503).
- 없어지는 route: `GET /board/restart`(확인 page), `POST /board/restart`, `GET /ws/board`. Route audit(`test_web_route_audit.py`)의 pin을 새 route로 바꾼다. `GET /board`는 page로 남는다.

#### WebSocket v2: terminal마다 공유 연결(WBX-D54·D55)

- `GET /ws/board/{terminal_id}`: 세션 cookie 필수, `Origin`은 기대 origin과 정확히 같아야 함(아니면 upgrade 전 403, 현행 `origin_exact`), 모르는 id는 upgrade 전 404, Board 없음·`registry_invalid`는 503. API key로는 열 수 없다.
- Upgrade 전 한도(기술 보완): 열린 `/ws/board/*` socket 전체 128개(넘으면 503 `too_many_connections`), 한 로그인 세션의 hello 전 socket 8개(넘으면 429 `too_many_pending`). 현행 전체 8·세션당 2는 terminal 하나를 전제한 값이다. 화면은 hello 전 socket을 동시에 4개까지만 연다.
- Hello(연결 뒤 5 s 안, 아니면 4400): `{"t":"hello","v":2,"csrf":…,"size":[rows,cols]}`. `size`는 1..500으로 맞춘다(현행 hello 규칙). `v`가 2가 아니면 4400. CSRF 불일치 4403.
- Terminal당 연결 한도 8(WBX-D54): hello가 맞고 이미 8개가 붙어 있으면 `{"t":"refused","reason":"too_many_clients","limit":8}` 뒤 4429. 감사 `terminal_denied`(reason `too_many_clients`).
- Attach(현행 `_attach`처럼 await 없이 한 단계): 연결을 terminal의 client 집합에 넣고 `{"t":"attached","v":2,"terminal":view,"size":[rows,cols],"clients":N,"heartbeat":10}`, 그 terminal ring의 replay(64 KiB 조각), 출력 구독. 그 사이에 출력 조각이 빠지거나 겹치지 않는다. `clients` 수가 바뀌면 그 terminal의 모든 연결에 `{"t":"clients","count":N}`.
- 시작 규칙: attach는 process를 시작하지 않는다. 시작은 session API(`open`·`start`·만들기)만 한다(WBX-D56 (1)). 붙은 terminal이 멈춰 있으면 replay(남은 마지막 출력)와 `state`만 보인다.
- Binary frame: header 없이 payload만이다(socket이 terminal 하나이므로 channel·epoch가 필요 없다). Client → server는 입력 bytes(≤ 64 KiB, 넘으면 버리고 `{"t":"error","reason":"too_large"}`), server → client는 출력 bytes.
- 입력(WBX-D54): 붙은 모든 연결의 입력을 Hub event loop에 도착한 순서대로 그 terminal의 PTY 입력 queue에 넣는다(연결 사이 순서 보장은 도착 순서뿐, 섞임 수용). 붙여넣기 backpressure는 현행 `_paced`대로 그 socket만 읽기를 멈춘다(다른 연결의 입력은 계속, queue의 hard cap 4 MiB를 넘으면 `input_refused`). Process가 없으면 `input_refused`.
- 크기(WBX-D54, AC-33):
  - 서버는 연결마다 마지막으로 알린 크기(hello `size`, 이후 `resize`)를 기억한다.
  - 크기 주인 = 가장 최근에 입력 frame 또는 `resize`를 보낸 연결. `resize`를 받으면 그 연결이 주인이 되고 PTY 크기를 그 값으로 바꾼다. 주인이 아닌 연결의 입력이 오면 그 연결이 주인이 되고, 그 연결의 기억한 크기가 PTY 크기와 다르면 입력을 쓰기 전에 PTY 크기를 먼저 바꾼다.
  - Hello `size`는 그 연결이 그 terminal의 유일한 연결일 때만 PTY 크기를 정한다. 주인이 끊겨도 크기는 그대로다(다음 입력·resize까지).
  - PTY 크기가 바뀔 때마다 그 terminal의 모든 연결에 `{"t":"size","rows":r,"cols":c}`. 주인 여부는 보내지 않는다(client가 자기 크기와 비교한다).
  - Client 규칙(반복 방지): `resize`는 자기 layout 변화(창 크기, widget 크기·보이기, 좁은 화면 전환)에서만, 150 ms debounce 뒤에 보낸다. `size`·`attached`를 받은 것에 반응해 보내지 않는다. 주인이 아닌 client는 xterm을 서버 크기로 `resize(cols, rows)`하고 widget 본문 안에서 남으면 비우고(letterbox) 모자라면 scroll한다.
- Control(client → server): `{"t":"resize","rows","cols"}`(1..500 정수, 아니면 `bad_resize`), `{"t":"ping","n"}` → `{"t":"pong","n"}`, `{"t":"release"}` → 1000(tab 닫기 `pagehide`, best-effort). Epoch field는 없다.
- Server → client: 위의 `attached`·`clients`·`size`·`pong`·`error`·`refused`, `{"t":"state","terminal":view}`(그 terminal의 상태·이름·`busy` 변화), `{"t":"gone"}`(닫기·삭제, 뒤에 4404).
- Heartbeat·세션·queue(현행 값, 연결별): client는 10 s마다 `ping`, 서버는 20 s + 1 s 무신호면 그 연결만 4002. 세션 무효(로그아웃·비밀번호 변경·`wb hub passwd`·24 h 만료)면 그 세션의 연결만 4401(약 5 s 재확인, 로그아웃·변경 때 즉시). 송신 queue 4 MiB 초과면 그 연결만 4003. Message 1 MiB 초과 1009. Hub 정지 1001.
- 재연결(`board-state.js`): 1006·1001·4002·4003·4000(화면 자체 pong 감시)은 지수 backoff(1→2→4…≤15 s, 10 s 안정 뒤 처음부터)로 새 hello(resume 없음). 4400~4499는 자동 재연결하지 않는다: 4401 "연결 없음 — 다시 로그인", 4404 widget 제거, 4429 "연결 수 한도(8개)를 넘었습니다", 그 밖 "다시 연결".
- Close code v2: 1000 해제, 1001 Hub 정지, 1009 너무 큼, 4002 heartbeat, 4003 queue, 4400 hello, 4401 세션, 4403 csrf, 4404 terminal 없어짐, 4429 terminal당 연결 한도. 4001과 `taken_over`·`taken_elsewhere`·"다시 가져오기"는 없어진다.
- 보이는 widget만 연결한다. 숨긴 widget·좁은 화면에서 고르지 않은 terminal은 `release`로 연결을 닫는다.

#### Hub 상태·정지·nav

- hub_v1 `status.board_agent`: `{state:"ok"|"registry_invalid", message?, options:{agent_limit, shell_limit}, agents:[view…], shells:[view…]} | null`(web 꺼짐 등 Board가 없을 때 null). 이전 `agent`·`shell`·`lease` key는 없어진다(`wb` CLI와 Hub는 같은 설치본이다).
- `wb hub status` 한 줄: `board_agent: agent 2/4 실행 중 · shell 3 · 연결 5`, 실패한 terminal마다 아래에 `    <이름>: <message 또는 detail>`, `registry_invalid`면 `board_agent: registry_invalid` 아래 message.
- `Hub.active()`: 살아 있는 terminal이 하나라도 있으면 `[{"kind":"board_agent_set","terminals":[{"id","kind","name","pid","state"}…]}]`. 연결 수는 넣지 않는다(연결이 붙고 떨어질 때마다 정지 token이 `stop_token_mismatch`가 되지 않게). `wb hub stop`·`wb update` 확인 목록은 이름과 상태를 보인다. 확인하면 모든 terminal을 끝낸 뒤 Hub가 멈춘다(현행 2단계 정지, AC-38).
- Nav 상태 점(`WebServices.board_state`): agent 중 `failed`·`isolation_failed`가 있거나 `registry_invalid`면 `error`, 아니면 살아 있는 agent가 있으면 `on`, 아니면 `off`. Dashboard의 Board card는 같은 값과 "agent N 실행 중 · shell M"을 보인다(`/api/dashboard`의 `board` 항목에 수를 더한다).

#### 감사(WBX-D44·D45·D54, AC-34)

| event | 남기는 때 | detail |
|---|---|---|
| `terminal_attach`, `terminal_release`, `terminal_timeout`, `terminal_closed` | 연결별 붙음·해제(release)·heartbeat 끊김·그 밖의 끝(EOF·1001·4003·4401·4404) | `terminal_id`, `kind`, `name`, `client`(Hub run 안의 연결 일련번호), `clients`(그 뒤 연결 수) |
| `terminal_denied` | hello 오류·CSRF·terminal당 한도 | 위 + `reason` |
| `board_terminal_open` | agent·shell 만들기(첫 shell 자동 생성 포함) | `terminal_id`, `kind`, `name`, `reason`(`create`/`first_open`) |
| `board_terminal_close` | shell 닫기, agent 삭제 | `terminal_id`, `kind`, `name`, `memory_removed`(agent) |
| `board_start`, `board_stop`, `board_restart` | 현행과 같은 때 | 현행 `channel` 대신 `terminal_id`, `kind`, `name` + `pid`, `reason` |
| `board_rename` | 이름 바꾸기 | `terminal_id`, `kind`, `old`, `new` |
| `board_options` | 한도 변경 | `before`, `after` |

Actor는 `owner`(Hub 동작은 `hub`), client 주소를 남긴다. `terminal_takeover`는 더 이상 남기지 않는다(이전 행은 그대로). Terminal 입출력·key 값은 남기지 않는다.

#### 이행과 되돌리기(WBX-D44·D48, AC-37)

- 새 판의 첫 Hub 시작: `board-agents/registry.json`이 없으면 `agent-1`(`home: "board-agent"`)로 만든다. 기존 `<hub state>/board-agent/`의 세션·메모리·`omp-logs/`는 그대로 그 agent의 것이고 첫 시작이 `--continue`로 잇는다. 남은 `board-agent` system 행은 지운다. 기존 Board agent 요청(`origin_name` 없음)은 "Board agent"로 보인다.
- Shell은 Hub 재시작으로 이미 끝났으므로 옮길 것이 없다. 그 run의 첫 `/board` 열기가 `shell-1`을 만든다.
- 이전 판의 `/board` tab이 열려 있었다면 `/ws/board`가 404라 연결되지 않는다. 새로 고치면 새 화면이다.
- 이전 판으로 되돌림: 이전 판은 `board-agents/`를 무시하고 `board-agent/`를 쓰므로 `agent-1`의 세션·메모리가 그대로다. 둘째 이후 agent는 다시 새 판이 될 때까지 보이지 않는다(지워지지 않음). 이전 판은 시작 때 system 행을 모두 바꾼다. 이름이 저장된 요청은 이전 판에서 "Board agent"로 보인다.
- 배치 localStorage는 새 key라 옮길 것이 없다.

#### 화면: workspace와 panel(WBX-D42~D53, D57)

- 구성: `/board` = `<aside class="board-panel">`(접힘) + `<section class="board-workspace">`(격자). 기존 tab·restart 확인 page는 없어진다. 서버 HTML은 panel·workspace의 빈 틀만 주고(inline script 없음), 화면은 첫 `GET /api/board`로 목록을 받는다. JS가 없으면 "이 화면은 JavaScript가 필요합니다"를 보인다.
- 격자(기술 보완, D2가 조정할 수 있고 조정값을 출력에 적는다): 12열, 행 높이 2.5rem, 간격 0.5rem. Widget 최소 3열 × 6행. 위치 `{x, y, w, h}`는 칸 단위 정수. 끌기는 제목 막대의 pointer event(`setPointerCapture`), 크기는 오른쪽 아래 모서리 손잡이. 끄는 동안 놓일 칸을 미리 보이고, 겹치면 미리 보기를 "놓을 수 없음"으로 보인다. 놓을 때 겹치면 마지막 유효 자리로 돌아간다(WBX-D57). 위치는 CSSOM(`element.style.setProperty`)으로 쓴다(CSP 변경 없음).
- Widget: 제목 막대(상태 점, 이름, "다른 화면 N개 연결됨"(N ≥ 1일 때, N = `clients − 1`), 숨기기, 닫기 ×), 본문 xterm. 멈춘 agent는 본문 위에 "중지됨 — 시작" 버튼, `exited`·`failed`는 상태 문구와 "재시작".
- Focus(WBX-D53): widget에서 `pointerdown` → 그 widget에 focus class(강조 테두리, token `--focus`)와 `term.focus()`. 다른 widget의 class는 지운다. 단축키 없음.
- Panel(WBX-D49): agent 행(상태 점, 이름, 보이기/숨기기, 시작/중지, 이름 바꾸기(제자리 입력, Enter 저장·Esc 취소), 삭제), shell 행(이름, 이름 바꾸기, 보이기/숨기기, 닫기), "새 Board agent"·"새 shell"(한도에 닿으면 비활성과 "한도 N개" 문구), 배치 초기화(이 브라우저만), 한도 설정(숫자 입력 2개 + 저장, 서버 오류 문구 표시). 확인이 필요한 동작은 `<dialog>`로 묻는다: 중지 "<이름>을 중지합니다. 메모리는 남습니다", 삭제 "<이름>의 세션과 메모리를 지웁니다. 되돌릴 수 없습니다", 닫기(shell) "<이름>을 닫습니다. 실행 중인 명령이 끝납니다", 재시작.
- 목록 갱신: `GET /api/board`를 5 s마다와 자기 동작 뒤에 부른다. `pagehide`에서 fetch abort·timer 정지, `pageshow`(persisted)에서 다시(WBX-D34 규칙). 401이면 `/login`.
- 브라우저 저장(WBX-D50, 모두 try/catch, 실패하면 기본값으로 동작): `wb.board.layout` = `{"v":1,"widgets":{<id>:{"x","y","w","h","visible"}}}`, `wb.board.panel` = `"open"`·`"closed"`, `wb.board.narrow` = 고른 terminal id. 읽은 값은 검사하고(모르는 `v`, 범위 밖 값, 겹침) 맞지 않으면 그 항목을 버리고 자동 배치한다.
- 맞추기(WBX-D57): 저장된 배치가 없으면 모든 terminal을 agent 먼저 자동 배치(위에서부터 빈 자리를 찾는 순서)하고 보인다. 있으면 없는 id는 버리고, 새 id는 숨김으로 둔다. 이 브라우저에서 만든 terminal은 자동 배치해 보인다.
- 좁은 화면(WBX-D51): `matchMedia("(max-width: 719.98px)")`. 격자를 쓰지 않고 panel이 terminal 하나를 고르며 그 terminal만 화면 전체(header 아래)로 보이고 연결한다. 고른 것이 없으면 첫 agent. 넓어지면 저장된 배치로 돌아가고 보이는 widget을 다시 연결한다.
- JS 배치(기술 보완): 순수 logic은 `static/board-layout.js`(칸 맞춤, 겹침, 자동 배치, 이동·크기 검사, 저장 값 parse/검사/serialize, 목록과 맞추기, 좁은 화면 선택)와 `static/board-state.js`(protocol v2 상태 기계, terminal 하나당 하나)에 두고 S8로 시험한다. `board.js`는 DOM·socket·pointer glue만 둔다(현행 원칙).

#### 보안(AC-38)

- 모든 `/api/board*`·`/ws/board/*`는 세션 전용이고 API key로 닿지 않는다. 내부 listener와 `/api/v1/` REST에는 Board 동작이 없다(WBX-AC-08 그대로).
- Agent별 key·출처 이름은 구분일 뿐 보안 경계가 아니다(WBX-D58). Agent는 서로의 key·home을 같은 OS 사용자 권한으로 읽을 수 있다. 내부 listener가 멈춘 agent의 key를 거절하는 것은 오래 남은 key를 줄이기 위함이다.
- 삭제는 symlink를 따라가지 않고 `<hub state>` 밖을 지우지 않는다. 사용자 `agent.db`는 어떤 경로로도 지우거나 복사하지 않는다.
- 공유 연결: 같은 소유자의 모든 유효 세션이 붙을 수 있다. 로그아웃은 그 세션의 연결만 닫는다(현행). 세션이 하나라도 새면 모든 terminal이 노출되는 것은 현행과 같다(ADR-0003).
- 이름은 소유자 입력이며 감사 detail에 그대로 남는다(≤ 40자).

#### 실패와 경계 동작

- Agent 하나의 시작 실패(`omp_unavailable`, `home_unavailable`, `hub_db_unavailable`, `internal_listener_failed`, `isolation_failed`)는 그 agent만 `failed`/`isolation_failed` + `message`다. 다른 terminal은 영향이 없다.
- 여러 agent가 함께 시작하면(Hub 재시작 뒤 첫 열기에 보이는 agent가 여럿) 격리 확인 OMP가 그 수만큼 돈다. 시작은 terminal별로 진행되고 서로를 기다리지 않는다.
- 메모리: terminal당 출력 ring 1 MiB, 최대 8 + 32 terminal. 연결당 송신 queue ≤ 4 MiB.
- Hub `SIGKILL`: 현행처럼 PTY master가 닫혀 SIGHUP. 남는 process는 보장하지 않는다. Registry 파일은 원자적 교체라 반쯤 쓴 상태가 없다.
- `[web] enabled = false`·`wb hub reload`: 현행 규칙 그대로(꺼도 실행 중 terminal은 `wb hub stop`까지 돈다).

#### 시험(기존 seam 재사용, 새 seam 없음)

| 대상 | seam | 독립 기대값 |
|---|---|---|
| Session API route·CSRF·Bearer 401·route audit·page 틀(panel, workspace, 이름 escape) | S1 + S6 | 이 절 표 |
| Registry 파일(mode·원자성·`registry_invalid`·이행), 이름·한도 규칙, 기본 이름 | 기존 Board seam(`tests/hub/test_board_set.py`·`test_board_launch.py`의 temp state dir) | 이 절 |
| 여러 agent process·home·env(agent마다 자기 key, shell 없음), 내부 listener의 key 판정, 출처 이름(REST 제출), 중지·삭제와 symlink 대상 불변, Hub 재시작 뒤 shell 없음·agent 남음, 정지 확인 목록 | S3(`wb _hub --foreground` + fake OMP, `/proc/<pid>/environ`) | 이 절, `/proc` 값, 파일 inode·내용 |
| 공유 연결(두 연결 같은 출력, 도착 순서 입력, 9번째 4429, 크기 주인, `clients`·`size` message, 연결별 heartbeat·4401·4003, 4404) | 기존 seam T5(task-inbox SPEC, `tests/hub/test_board_ws.py`: in-process `create_app` + 실제 `BoardSet`과 recorder PTY, `support/ws_client.py`의 `Tab`을 여러 개) | 이 절 WebSocket v2 |
| `board-state.js` v2, `board-layout.js`, panel 상태 | S8 | 이 절 |
| 외부 URL·inline script·새 library 없음, CSP 그대로 | S5 | BRIEF AC-25 |
| 끌기 감각, 두 기기, 좁은 화면, IME | S7(사용자) | 사용자 관찰 |

새 seam은 없다. 위 "기존 Board seam"과 T5는 task-inbox SPEC 테스트 결정에서 이미 쓰는 것이다. Worker가 이 seam으로 잡을 수 없는 behavior를 찾으면 새 seam을 만들지 말고 orchestrator에 반환한다(사용자 확인 대상).

### 트랙 E: Board workspace의 WB 프로젝트 pane(WBX-D59~D67, AC-39~47, s5)

현행(2026-10-29 읽기 조사, BRIEF r0.6 "관측 사실"): backend의 UI 계약은 `contracts/ui_v1.py`(frame `WBUI`, `VERSION = 1`)이고 `backend/ui_server.py`의 `UiServer`가 `<runtime>/ui.sock`(0600, accept 때 `SO_PEERCRED`)에서 연결 16개까지 받는다. Attach 자리는 `UiServer.attached` 하나이고 두 번째 `attach`는 `attached_elsewhere`다. Display(raw PTY bytes, `pane`·`session_id`·`generation`·`sequence`·`replay`)와 state(바뀔 때만, 최소 0.25 s 간격)는 attach한 연결에만 간다(`broadcast`, `push_state`). Attach하지 않은 `workbench-hub`는 `pause`·`resume`·`inbox_notice`·`task_status`·`cancel_task`만 보낸다(`ui_server.py:373-430`). Hub는 요청마다 `workbench-hub` 이름으로 새로 연결하고 backend 신원(pid·start ticks)을 먼저 확인한다(`hub/inbox/backend_port.py:51-70`). 이 절은 그 계약에 Hub 전용 공유 attach를 덧붙이고(E1), Hub가 프로젝트마다 그 연결 하나로 웹 widget을 중계하며(E2), 화면이 pane widget을 그린다(E3). 아래에서 "기술 보완"이라고 쓴 값은 이 SPEC이 정한 것이고, 나머지는 BRIEF 결정이다.

구성:

```
브라우저 widget ──/ws/board/<project_id>:manager (v2)──┐
브라우저 widget ──/ws/board/<project_id>:host    (v2)──┤  Hub PaneRelay(프로젝트당 하나)
                                                       │   pane별 ring 256 KiB, 연결별 송신 queue 4 MiB
pane 동작 버튼 ──POST /api/board/panes/{id}/{action}───┘   크기 주인 판정, 감사
                                                       │
                              ui_v1 share_attach 하나(<runtime>/ui.sock)
                                                       │
TUI(workbench-ui) ── ui_v1 attach(primary, 그대로) ──── backend UiServer ── manager_omp / worker_omp / host_shell
```

#### ui_v1 덧붙이기: 공유 attach(WBX-D59·D65, backend, WBX-E1)

`VERSION`은 1 그대로다. 기존 frame·type·reason의 뜻은 바뀌지 않고, 아래 type 두 개와 reason 하나, snapshot field 하나만 더한다.

| 추가 | 모양 | 뜻 |
|---|---|---|
| request `share_attach` | `{"v":1,"type":"share_attach","id"}`(다른 field 없음, 있으면 `invalid_message`) | 이 연결을 공유 attach로 만든다. 답은 `result ok` + `snapshot`, 그 뒤 모든 pane의 replay display(`replay: true`), 그 뒤 live. Primary attach의 `attach` 응답 순서와 같다 |
| request `share_detach` | `{"v":1,"type":"share_detach","id"}` | 공유 attach를 푼다. 답 `{"detached": true}`. 공유 attach가 아닌 연결이면 `not_attached` |
| reason `resize_owned_by_ui` | `result ok:false` | Primary(TUI)가 붙어 있어 공유 attach의 `resize`를 적용하지 않았다 |
| snapshot `shared_attachments` | 정수 ≥ 0 | 지금 공유 attach 수. 기존 `attached`(primary가 있는지)의 뜻은 그대로다 |

받아들이는 조건(WBX-D65 (2)): hello 이름이 `NON_ATTACHED_CONTROL_CLIENTS`(`workbench-hub`)이고, accept 때 읽은 peer uid가 backend의 euid이며, peer pid가 확인된 실행 중 Hub(`hub_peer`)일 것. 현행 `_may_control_unattached` + `_is_hub_process`(`inbox_notice`·`cancel_task`의 문)와 같다. 하나라도 틀리면 `not_attached`("share_attach is a Workbench Hub request")이고 attach 자리·전송은 바뀌지 않는다. 그 밖의 거절(기술 보완): 이미 primary인 연결, 이미 공유 attach인 연결 → `invalid_message`. 공유 attach는 backend 전체에 최대 4개(`MAX_SHARED_ATTACHMENTS`, Hub 재시작 직후 이전 연결이 아직 닫히지 않은 겹침을 견디는 값)이고 넘으면 `queue_full`. 연결 16개 한도(`MAX_CONNECTIONS`)는 그대로다. 공유 attach인 연결의 `attach`는 `invalid_message`, primary 연결의 `share_attach`도 `invalid_message`다.

공유 attach가 보낼 수 있는 request(WBX-D65 (4)): `input`, `paste`, `resize`(아래 크기 규칙), `takeover_request`, `takeover_confirm`, `handoff`, `restart_pane`, `kill_pane`, `stop_command`, `share_detach`. 처리는 primary와 같은 handler다(`controller.admit`, `restart_pane`, `kill_pane`, `_start_stop` …). `focus`와 `detach`는 `not_attached`("primary attach only")다. Backend focus는 TUI focus를 움직이기 때문이다(`ui/product/model.py`는 backend focus가 바뀌면 따라가고 잡아 둔 Ctrl-d를 버린다). 공유 attach가 아닌 연결(TUI 아닌 다른 client 포함)의 규칙은 현행 그대로다.

Fan-out(WBX-D65 (3)):
- `broadcast(chunk)`와 `push_state(snapshot)`은 primary와 모든 공유 attach(닫는 중이 아닌 것)에 보낸다. 각 연결은 지금처럼 자기 `outbound` buffer(한도 `max_outbound` 16 MiB)를 가진다.
- 한도를 넘긴 연결은 지금처럼 그 연결만 `closing slow_client`로 닫는다. 공유 attach면 공유 집합에서 빠지고 primary·다른 공유 attach·pane은 영향이 없다. Primary면 현행대로 detach.
- 상태 push 조건: 지금은 primary가 있을 때 보내던 주기 push(`_last_state_digest`가 바뀔 때만)를 primary나 공유 attach가 하나라도 있으면 보낸다. 새 공유 attach는 attach 응답의 `snapshot`으로 시작한다.
- `stop_command`의 최종 result는 현행대로 요청한 연결에만 가고, 그 뒤 state push는 모든 attach에 간다. `close()`의 `closing backend_shutdown`은 지금처럼 hello를 마친 모든 연결에 간다.

크기(WBX-D60, backend 쪽 규칙):
- Primary `attach`의 `on_attach(size)`(모든 pane을 TUI 크기로)와 primary의 `resize`는 바뀌지 않는다. 마지막 resize가 이긴다(`TIOCSWINSZ`).
- 공유 attach의 `resize`는 `pane`을 꼭 적어야 하고(`pane` 없음은 `invalid_message`), primary가 붙어 있으면 적용하지 않고 `resize_owned_by_ui`, 없으면 그 pane에 적용한다. 판정은 backend loop 안에서 하므로 TUI attach와 겹쳐도 "TUI가 붙은 뒤 웹 resize가 이기는" 경우가 없다.
- 공유 attach는 attach할 때 크기를 바꾸지 않는다(`share_attach`에 `size`가 없다).
- Pane view의 `rows`·`cols`(현행 `panes.py` `info()`)가 Hub와 웹이 따를 크기다.

입력과 host shell 주인(WBX-D61, D65 (5)):
- 입력은 primary와 같은 `admit(pane, data, kind)` 경로다. OMP pane은 모든 입력을 받는다(`panes.py:567`). Host shell의 거절 이유(`host_shell_automation`, `input_owner_manager`, `input_target_unknown`, `pane_unavailable`, `queue_full`, `paste_too_large`)와 lone Ctrl+C 예외(확인된 foreground 명령으로 전달, `command_interrupt` journal, `panes.py:796-852`, ADR-0002)는 그대로다. Journal에 어느 client에서 왔는지는 더하지 않는다.
- Host shell 입력 주인은 backend에 하나(`input_owner`: `manager`/`user`)이고 연결별이 아니다. `user`는 TUI와 웹 모두의 같은 소유자다. TUI나 웹 어느 쪽의 `takeover_request`·`takeover_confirm`·`handoff`든 그 하나를 바꾸고, 결과는 state push로 모든 attach에 같게 보인다. `user`가 주인일 때 TUI와 웹의 입력은 backend loop에 도착한 순서대로 섞인다(공유 연결 WBX-D54와 같은 수용).
- Manager 입력창 대기(`manager_editor_not_empty`)는 OMP bridge editor 상태로 판정하므로 웹 입력에도 그대로 적용된다(자동 전달만 미룸).
- Worker pane의 사용자 입력은 허용(core DECISIONS C-D69(6)(b)).

호환:
- 기존 TUI(`workbench-ui`)는 아무것도 바꾸지 않는다. TUI가 snapshot의 모르는 key(`shared_attachments`)를 무시하는지 E1이 확인한다(무시하지 않으면 TUI가 아니라 field 이름·위치를 다시 정하고 orchestrator에 반환).
- 이전 판 backend는 `share_attach`를 `ClientType`으로 모르므로 `unsupported_type`으로 답하고, attach 자리를 건드리지 않는다. 기존 `attach`에 field를 더하지 않은 이유가 이것이다(WBX-D65 (1)).
- 이전 판 Hub는 `share_*`를 보내지 않는다. 새 backend에서 기존 Hub 동작은 그대로다.
- Core SPEC `ui_v1` 설명(`docs/features/core-workbench/SPEC.md:211`)에는 E4가 개정 주석을 단다.

#### Hub 중계: PaneRelay(WBX-D66, WBX-E2)

위치(기술 보완): `src/workbench/hub/board/panes.py`(새). Hub event loop(aiohttp) 안에서 ui_v1 framing(`ui_v1.FrameDecoder`, `encode_frame`)을 쓰는 asyncio Unix socket client다. 막는 `UiClient`를 event loop에서 직접 부르지 않는다(필요하면 executor).

프로젝트당 연결 하나(WBX-D66): 프로젝트 P의 pane widget 연결(WebSocket)과 pane 동작 API는 모두 P의 `PaneRelay` 하나를 쓴다. 그 relay가 backend에 연결 하나, 공유 attach 하나를 가진다.

수명(기술 보완):
1. 열기: P의 pane WebSocket 하나가 hello를 통과하거나 P에 pane 동작 API가 오면 연다. Registry에서 P의 root를 찾고(URL에서 경로를 만들지 않는다), `backend_port.call`과 같은 신원 확인(`inspect_backend(DataLayout(root/.wb))`, `state == running`, 기록된 pid·start ticks)을 한 뒤 `ui.sock`에 연결해 hello(`workbench-hub`) → welcome의 `backend_pid`가 기록의 pid와 같은지 확인 → `share_attach`.
2. 준비: attach 응답의 snapshot을 저장하고, 뒤따르는 replay display로 pane별 Hub ring(256 KiB, backend `REPLAY_BYTES`와 같은 값)을 채운다. 그 사이 붙은 WebSocket은 `state`(`connecting`)만 받고, 준비가 끝나면 replay와 live를 받는다.
3. 닫기: 마지막 pane WebSocket이 떠나고 진행 중 동작이 없으면 30 s 뒤 `share_detach` 후 닫는다(새로 고침마다 다시 attach·replay하지 않도록). Hub 정지 때는 즉시 닫는다(best-effort `share_detach`).
4. 한 프로젝트의 relay 실패는 다른 프로젝트·Board terminal에 영향이 없다.

화면 중계:
- Display frame의 `pane`으로 나눈다: `manager_omp` → `manager`, `worker_omp` → `worker`, `host_shell` → `host`.
- Pane마다 지금 ring의 `(session_id, generation)`을 기억한다. 다른 값의 frame이 오면(pane 재시작, 새 backend) ring을 비우고 그 pane의 WebSocket에 `{"t":"reset"}`을 보낸 뒤 새 bytes를 보낸다. `sequence`가 같은 session 안에서 건너뛰거나 거꾸로 가도 같은 처리를 하고 Hub log에 남긴다.
- 새 `share_attach`마다(다시 붙기 포함) ring을 비우고 붙어 있던 WebSocket에 `reset`을 보낸 뒤 replay를 다시 보낸다(sequence로 겹침을 맞추지 않는다. 단순함과 빠짐 없음을 택함).
- WebSocket attach는 트랙 D의 `_attach`처럼 await 없이 한 단계: client 집합에 넣기 → `attached` → Hub ring replay(64 KiB 조각) → live 구독. Replay와 live 사이에 빠지거나 겹치는 조각이 없다.
- Hub는 backend socket을 계속 읽는다. 웹 연결로 보내는 것은 연결별 송신 queue(4 MiB, 넘으면 그 연결만 4003, 트랙 D 값)에 넣을 뿐이고, 느린 웹 연결이 backend 읽기를 막지 않는다.

Pane view(JSON, `attached`·`state` message와 `GET /api/board`가 같은 모양):

| field | 값 |
|---|---|
| `id` | `<project_id>:<pane>`. `project_id`는 registry의 소문자 canonical UUID, `pane`은 `manager`·`worker`·`host` |
| `kind` | `pane` |
| `project_id`, `project_name`, `pane` | registry 값과 위 이름 |
| `name` | `<project_name> · <pane>`(WBX-D64) |
| `state` | `off`(backend가 실행 중이 아님), `idle`(켜져 있으나 relay가 열려 있지 않음, 열면 붙음), `connecting`, `running`(pane이 살아 있음), `exited`(pane이 끝남, `exit_status`), `unavailable`(snapshot에 그 pane이 없음, 예: 준비 전), `unreachable`(신원 확인·연결 실패, 다시 시도 중), `too_old`(이전 판 backend) |
| `size` | `[rows, cols]`(snapshot의 pane 크기) |
| `ui_attached` | snapshot `attached`(TUI가 붙어 있는지 = 크기 주인이 TUI) |
| `input_owner` | host: `manager`/`user`, OMP pane: `user` |
| `automation_hold` | host: 문자열 또는 null |
| `command` | host: `{command_id, label, owner, state}`(snapshot host `command`, TUI `_host_command`와 같은 판정) 또는 null |
| `actions` | 지금 쓸 수 있는 버튼: `takeover_request`·`takeover_confirm`·`handoff`(host, 주인·hold 상태에 따라), `stop`(host, `command`가 있고 `state != "stopping"`), `restart`(`exited`), `kill`(host, 살아 있음) |
| `clients` | 이 pane의 웹 연결 수(0~8) |
| `busy` | 이 pane에 pane 동작이 진행 중 |
| `message?` | `off`·`unreachable`·`too_old`·`unavailable`의 고정 문장 |

입력(WBX-D61):
- WebSocket binary frame 하나(≤ 64 KiB, 트랙 D 한도) = ui_v1 `input` request 하나(`kind` input). Hub는 frame을 합치거나 나누지 않는다. 그래서 웹에서 친 lone Ctrl+C(`\x03` 한 byte)는 그대로 lone이다.
- 여러 웹 연결의 입력은 Hub에 도착한 순서대로 relay 연결에 쓴다(pipelining, 순서 보장). Hub는 request `id` → (웹 연결, pane)을 기억하고, `ok:false`는 그 웹 연결에만 `{"t":"input_refused","reason":<ui_v1 reason>,"detail":<고정 문장>}`으로 돌려준다(기술 보완: reason별 한국어 문장 표, E2가 SPEC 문구로 고정).
- 미응답 상한(기술 보완): relay당 64 request 또는 미응답 입력 1 MiB. 넘으면 입력을 보낸 웹 socket의 읽기를 멈춘다(트랙 D `_paced`와 같은 backpressure, 다른 연결의 출력은 계속).
- `state`가 `running`이 아니면 보내지 않고 `input_refused`(`pane_unavailable`, 꺼짐이면 `backend_off`).
- Ctrl-d 두 번 누르기는 화면(E3)이 TUI와 같은 규칙으로 한다(Hub·backend는 받은 bytes를 그대로 보낸다). 웹에는 `Ctrl-]` prefix가 없어 `0x1d`도 친 그대로 간다.

크기 주인(WBX-D60, Hub 쪽):
- `ui_attached`가 true면 Hub는 `resize`를 보내지 않는다. Pane 크기가 바뀔 때마다 그 pane의 웹 연결에 `{"t":"size","rows","cols"}`. 웹은 그 크기로 그리고 letterbox/scroll(트랙 D client 규칙: `size`에 반응해 `resize`를 보내지 않음).
- false면 pane마다 트랙 D의 크기 주인 규칙을 그대로 쓴다: 가장 최근에 입력·`resize`를 보낸 웹 연결이 주인, 주인의 기억한 크기가 pane 크기와 다르면 입력 전에 `resize(pane, rows, cols)`, hello `size`는 그 pane의 유일한 웹 연결일 때만, 주인이 떠나도 크기는 그대로.
- `resize_owned_by_ui` 답은 버린다(TUI가 막 붙음. 다음 state가 `ui_attached: true`). TUI가 떠나면 다음 웹 입력·resize까지 크기는 TUI가 남긴 그대로다.

Pane 동작 API(WBX-D62, 기술 보완). 트랙 D session API와 같은 규칙: 세션 route, `X-CSRF-Token`, Bearer 무시(401), body JSON ≤ 4 KiB(모르는 field 400), `no-store`, 응답 `{"ok":true,…}` / `{"ok":false,"reason","detail"}`.

| method·path | body | pane | ui_v1 | 성공 |
|---|---|---|---|---|
| `POST /api/board/panes/{id}/takeover_request` | `{}` | host | `takeover_request` | 200 `{ok, pane:view}` |
| `POST /api/board/panes/{id}/takeover_confirm` | `{}` | host | `takeover_confirm` | 200 |
| `POST /api/board/panes/{id}/handoff` | `{}` | host | `handoff` | 200 |
| `POST /api/board/panes/{id}/stop` | `{confirm:true, command_id}` | host | `stop_command(command_id)` | 202 `{ok, command_id, stop:{status:"stopping"}}`; 최종 결과는 그 pane의 웹 연결에 `{"t":"notice","action":"stop","command_id","stop":{…}}` |
| `POST /api/board/panes/{id}/restart` | `{}` | 셋 다 | `restart_pane` | 200 |
| `POST /api/board/panes/{id}/kill` | `{confirm:true}` | host | `kill_pane` | 200 |

- `command_id`는 dialog에 보인 명령의 id이고(TUI처럼 "보인 id, 더 새 것이 아님"), 그 사이 명령이 바뀌었으면 backend의 판정을 그대로 돌려준다.
- 오류: 400 `bad_input`·`confirm_required`·`wrong_pane`(host 전용 동작을 OMP pane에); 404 `unknown_terminal`(id 모양이 틀림, 등록되지 않은 프로젝트); 409 `terminal_busy`(같은 pane에 동작 진행 중), 409 `backend_refused` + `backend_reason`(ui_v1 reason: `takeover_held`, `handoff_held`, `pane_alive`, `pane_not_restartable`, `restart_in_progress`, `pane_not_killable`, `pane_exited`, `kill_in_progress`, `kill_failed`, `restart_failed`, `pane_unavailable`, `queue_full`, `backend_shutdown` …); 503 `backend_off`·`backend_unreachable`·`backend_too_old`·`board_unavailable`; 504 `backend_timeout`(10 s 안에 답이 없음. 결과를 알 수 없으므로 다시 보내지 않고 "결과를 알 수 없음 — pane 상태를 확인"을 보인다).
- relay가 닫혀 있으면 API가 연다(위 수명 1).
- TUI와 웹이 같이 있을 때 버튼은 TUI의 존재와 무관하게 쓸 수 있고, 결과는 backend 상태 하나로 양쪽에 보인다(위 "입력과 host shell 주인").

WebSocket endpoint(WBX-D66): 트랙 D의 `GET /ws/board/{id}` v2를 그대로 쓴다(세션 cookie, `Origin` 정확히 일치, upgrade 전 한도 전체 128·세션당 hello 전 8, hello v2 + `csrf` + `size`, 연결별 heartbeat 10 s / 20+1 s, 세션 무효 4401, 송신 queue 4 MiB 4003, 1009, 1001, `release`, 재연결 규칙).
- Id 판정(upgrade 전): `^<소문자 canonical UUID>:(manager|worker|host)$`이고 그 UUID가 Hub registry에 있어야 한다. 아니면 404. Board registry의 `registry_invalid`는 pane widget을 막지 않는다(agent registry 문제일 뿐). Web 꺼짐·Hub 정지 중은 503.
- Pane당 웹 연결 한도 8: 9번째는 `refused`(`too_many_clients`) 뒤 4429(WBX-D54와 같음).
- 더하는 server → client message: `{"t":"reset"}`(xterm을 지우고 이어지는 bytes를 처음부터), `{"t":"input_refused","reason","detail"}`, `{"t":"notice","action","…"}`. `attached`·`state`의 `terminal`은 pane view다. 등록이 지워지면 `gone` 뒤 4404. 새 close code는 없다.
- Binary: client → server 입력 bytes, server → client 출력 bytes(트랙 D와 같음).

`GET /api/board`에 `projects`를 더한다(기술 보완): `[{project_id, name, power:"on"|"off"|"unknown", panes:[pane view×3]}]`, 순서는 registry 순서. `power`는 dashboard가 쓰는 프로젝트 상태 값(`/api/dashboard`의 projects)과 같다. Relay가 열려 있지 않은 pane의 `state`는 `power`에서 정한다(`on` → `idle`, `off` → `off`, `unknown` → `unreachable`). 이 목록은 backend에 연결하지 않는다.

감사(WBX-D63, AC-44):

| event | 남기는 때 | detail |
|---|---|---|
| `pane_attach`, `pane_release`, `pane_timeout`, `pane_closed` | 웹 연결별 붙음·해제·heartbeat 끊김·그 밖의 끝(트랙 D `terminal_*`와 같은 때) | `pane_id`, `project_id`, `project_name`, `pane`, `client`, `clients` |
| `pane_denied` | hello 오류·CSRF·pane당 한도 | 위 + `reason` |
| `pane_takeover_request`, `pane_takeover_confirm`, `pane_handoff` | API 결과 | `pane_id`, `project_id`, `project_name`, `pane`, `ok`, `reason?`, `input_owner`(뒤의 값) |
| `pane_stop` | 최종 결과가 왔을 때 한 행(Hub가 그 전에 멈추면 `outcome: "unknown"`) | 위 + `command_id`, `outcome`(`stop`의 status·reason) |
| `pane_restart`, `pane_kill` | API 결과 | 위 + `ok`, `reason?` |

Actor `owner`, client 주소. 입력 bytes·붙여넣기 내용·byte 수·입력 거절·resize는 남기지 않는다. Relay 연결 열기·닫기는 감사가 아니라 Hub log다. Lone Ctrl+C는 keystroke이므로 Hub 감사에 없고 backend journal(`command_interrupt`)에만 지금처럼 남는다.

화면(WBX-D64, WBX-E3):
- Panel: 트랙 D panel 아래 "프로젝트" 묶음. `GET /api/board`의 `projects`마다 이름(`textContent`)·켜짐 점·"manager"·"worker"·"host" 추가 버튼. 이미 보이는 widget이면 그 widget에 focus. 추가는 이 브라우저의 배치에만 쓴다(Hub에 저장하지 않음, WBX-D50과 같음).
- Widget: 제목 "<프로젝트 이름> · manager", 상태 점, "다른 화면 N개 연결됨", TUI가 붙어 있으면 "TUI가 크기를 정함" 표시, 숨기기, ×(이 브라우저에서 감춤. Pane·process에 아무것도 하지 않는다. Board terminal의 ×와 다르므로 확인 dialog 없음). 본문 xterm. `off`·`unreachable`·`too_old`·`unavailable`은 본문 위에 `message`와 프로젝트 화면(`/projects/<project_id>`) 링크.
- 버튼(`actions`에 있을 때만 활성): host "인수 요청"·"인수 확인"·"넘기기"·"명령 중지"(`<dialog>`: "<명령 이름>(<owner> 명령)을 중지합니다. SIGINT 뒤 끝나지 않으면 SIGTERM, SIGKILL로 이어집니다")·"강제 종료"(`<dialog>`: "host terminal과 그 안의 process를 바로 끝냅니다. 실행 중인 manager 명령은 결과를 알 수 없게 됩니다"), 끝난 pane "재시작". 결과·거절 이유는 widget 상태 줄에 보인다.
- 입력(E3, 순수 logic은 `static/board-pane.js`(새)로 S8): manager·worker widget은 TUI `_send_input`과 같은 Ctrl-d 규칙(첫 `\x04`(또는 `ESC \x04`)은 보내지 않고 "Ctrl-d는 OMP를 종료할 수 있습니다 — 2초 안에 Ctrl-d를 한 번 더 누르면 전달" 안내, 2 s 안의 같은 widget 두 번째만 보냄, 다른 key·붙여넣기는 잡아 둔 것을 버리고 자기는 보냄, focus를 옮기면 버림). Host widget은 그대로 보낸다. `input_refused`는 widget 상태 줄에 이유 문장.
- 배치 key: `wb.board.layout`의 `widgets`에 pane id를 그대로 쓴다(`v` 1 그대로, pane id는 Board id와 겹치지 않음). 맞추기(WBX-D57): 등록이 없어진 프로젝트의 pane 항목은 버린다. 자동 배치는 pane widget을 넣지 않는다(사용자가 더한 것만).
- 좁은 화면(WBX-D51): panel의 선택 목록에 이 브라우저에 더한 pane widget이 들어간다.
- 프로젝트 화면 링크(WBX-D64): `GET /projects/{project_id}`에 "작업 공간에서 열기" 링크 `/board#open=<project_id>`. `/board`의 JS가 fragment를 읽고(UUID 모양이고 `projects`에 있을 때만) 그 프로젝트의 manager·worker·host widget을 이 브라우저 배치에 더해(없으면 자동 배치) 보이고 manager에 focus한 뒤 fragment를 지운다. 서버는 fragment를 보지 않는다.
- CSP·script 규칙은 트랙 D와 같다(`script-src 'self'`, inline 없음, 새 library 없음).

보안(AC-47):
- `/api/board/panes/*`와 pane id의 `/ws/board/*`는 세션 전용이고 CSRF·Origin 검사를 거친다. Bearer는 401. `/api/v1/`·내부 listener에는 pane 동작이 없다(route audit에 pin). `allow_from`·bind·`[web] enabled = false` 규칙은 트랙 D와 같다.
- Hub ↔ backend는 `ui.sock`(0600, 같은 uid)이고 backend는 공유 attach를 확인된 Hub process에만 준다. Hub는 연결 전 backend 신원(pid·start ticks)과 welcome `backend_pid`를 확인한다.
- 프로젝트 root는 registry에서만 얻는다. URL의 id는 registry key 조회에만 쓴다.
- 프로젝트 이름은 escape·`textContent`(AC-47).
- 위험의 크기: 웹 로그인으로 manager·worker OMP와 host shell에 입력할 수 있게 된다. Board shell(ADR-0003)이 이미 같은 OS 사용자 권한의 임의 명령이므로 새 권한 등급은 아니다. 보호는 Board와 같이 로그인·bind·감사이고, 감사는 동작을 남기되 keystroke는 남기지 않는다(WBX-D63).

실패와 경계 동작(WBX-D67, AC-46):
- 꺼짐: relay를 열지 않는다. Pane view `off`, 입력은 `input_refused`(`backend_off`), 동작 API는 503 `backend_off`. 켜지면(다음 `GET /api/board` 또는 다시 시도에서 `on`) 붙어 있던 웹 연결이 있으면 relay를 연다.
- Backend 끝남·다시 시작(relay EOF, `closing backend_shutdown`, 신원 불일치): 모든 pane `unreachable`(신원이 실행 중 아님이면 `off`). 그 프로젝트의 웹 연결이 남아 있는 동안 1 → 2 → 4 … ≤ 15 s backoff로 다시 붙고, 붙으면 `reset` + replay.
- `slow_client`로 backend가 relay를 닫음: 위와 같이 다시 붙음(`reset` + replay). 웹 연결은 끊지 않는다.
- 이전 판 backend(`share_attach` → `unsupported_type`): `too_old`, `message` "이 프로젝트의 backend는 이전 판입니다. 프로젝트를 끄고 다시 켜면 웹에서 pane을 쓸 수 있습니다". 자동으로 다시 시도하지 않고, 그 프로젝트에 새 웹 hello가 오거나 60 s가 지나면 한 번 다시 확인한다. Hub는 backend를 재시작하지 않는다.
- 공유 attach 거절(`not_attached`: Hub 확인 실패, `queue_full`): `unreachable` + Hub log, backoff 상한 60 s.
- Hub 재시작: 웹은 1001 → 기존 재연결 → 새 relay.
- 등록 지움(`wb hub remove`): 그 프로젝트의 웹 연결에 `gone` 뒤 4404, relay 닫기.
- 메모리: relay가 열린 프로젝트마다 ring 256 KiB × 3, 웹 연결당 송신 queue ≤ 4 MiB, pane당 연결 8.

#### 시험(기존 seam 재사용, 새 seam 없음, 조합 하나는 확인 대상)

| 대상 | seam | 독립 기대값 |
|---|---|---|
| `share_attach`·`share_detach`·`resize_owned_by_ui` parse, 모르는 field 거절, `VERSION` 1 | 기존 계약 seam(`tests/contracts/test_ui_v1*.py`) | 이 절 표 |
| 공유 attach 조건(이름·uid·Hub pid 각각 틀림), primary와 공존, primary 두 번째 `attached_elsewhere` 불변, fan-out(display·state 같은 bytes), 공유 attach의 `slow_client`가 primary를 건드리지 않음, `focus`·`detach` 거절, 크기 규칙, `MAX_SHARED_ATTACHMENTS`, 이전 판 동작(모르는 type → `unsupported_type`) | backend ui_server seam = Hub SPEC S5(`tests/backend/test_ui_server*.py`: 실제 `UiServer` + `FakeController` + 실제 `UiClient`, peer는 `peer_identity` 하위 클래스로 주입) | ui_v1 계약 문구, 이 절 |
| 실제 pane의 입력 판정·거절 이유·lone Ctrl+C journal이 공유 attach에서도 같음, state push 조건, snapshot `shared_attachments`, 공유 attach 중 TUI attach의 pane 크기 | `tests/backend/test_panes.py` + Hub SPEC S6(실제 backend + fake OMP, driver subprocess) | journal, snapshot, `/proc` |
| 기존 TUI 동작 불변(웹 공유 attach가 있을 때 TUI 화면·입력·`Ctrl-]`·detach), TUI가 snapshot 새 key를 무시 | Hub SPEC S8(PTY e2e, S6 backend) + 기존 TUI model 시험 | 화면 문자열 |
| Relay 수명·화면 중계(reset·replay·순서)·입력 pipelining·거절 전달·크기 주인(TUI 있음/없음)·pane 동작 API·감사·실패(꺼짐, backend 재시작, Hub 재시작, 등록 지움) | task-inbox T6(`ProjectSandbox`: 실제 Hub + 실제 backend + fake OMP) + `support/ws_client.py`의 `Tab` 여러 개; TUI 대역은 test가 연 실제 `UiClient(name="workbench-ui")` primary attach | `sqlite3`로 읽은 `audit`, fake OMP가 기록한 입력, backend journal, `/proc` |
| 이전 판 backend(`unsupported_type`), 신원 불일치 | Hub SPEC S4 계약 fixture backend(`tests/hub/fixtures/contract_backend.py`, `--frames-log`) | frames log, pane view |
| Route·CSRF·Bearer 401·route audit·id 판정 404·프로젝트 화면 링크·이름 escape | S1 + S6 | 이 절 표 |
| `board-pane.js`(Ctrl-d 규칙, `actions` → 버튼, 크기 따름 판정, `#open=` parse), panel 프로젝트 묶음 상태, 배치 맞추기 | S8 | 이 절 |
| 외부 URL·inline script 없음 | S5 | AC-25·47 |
| 실제 browser + 실제 TUI, IME | S7(WBX-O09) | 사용자 관찰 |

- Fake OMP가 받은 입력을 기록·echo하도록 늘리는 것은 fixture 확장이며 새 seam이 아니다.
- 확인 대상 조합(BRIEF WBX-O10): 실제 curses TUI(Hub SPEC S8 PTY e2e)와 웹 WebSocket(T6)을 한 시험에서 함께 쓰는 것. 각 seam은 있지만 함께 쓴 적이 없다. E2·E4가 이 조합이 필요하다고 보면 쓰기 전에 orchestrator에 반환하고 사용자 확인을 받는다. 그 전에는 TUI 대역(실제 `UiClient` primary)으로 시험한다.
- Worker가 위 seam으로 잡을 수 없는 behavior를 찾으면 새 seam을 만들지 말고 반환한다.

### Board shell 결정(WBX-D68·D69, AC-48, s5)

- `BoardLauncher.prepare_shell` 순서: (1) `[board_agent] shell`이 비어 있지 않으면 그 값만 쓴다. 절대 경로이고 실행 가능해야 하며 아니면 지금처럼 `shell_unavailable`(다른 것으로 넘어가지 않음). (2) 비어 있으면 bash: `/bin/bash`, `/usr/bin/bash`, Hub 환경(`hub_environment()`)의 `PATH`에서 찾은 `bash`(`shutil.which(..., path=…)`, 절대 경로일 때만) 순서로 처음 실행 가능한 일반 파일. (3) 없으면 `/bin/sh`, 그것도 실행할 수 없으면 `shell_unavailable`.
- `$SHELL`은 읽지 않는다. 기술 보완: shell process env의 `SHELL`은 고른 program의 경로로 둔다(그 안에서 `$SHELL`을 띄우는 program이 같은 shell을 쓰게).
- argv는 `[path]` 하나(login·`-i` 옵션 없음, PTY라 대화형이다). cwd·env·`TERM`·UTF-8 locale은 현행 그대로.
- 결정은 shell을 시작할 때마다(만들기, 재시작) 한다. 돌던 shell은 그대로다.
- 시험 가능하게(기술 보완): 고정 후보 `("/bin/bash", "/usr/bin/bash")`와 `/bin/sh`는 `BoardLauncher`의 인자(기본값은 그 값)로 받아, 기존 Board seam(`tests/hub/test_board_launch.py`)에서 temp 경로로 "bash 없음"을 만든다. 실제 Hub의 shell process가 bash인지는 S3(`/proc/<pid>/exe`, `$SHELL=/bin/zsh`를 준 Hub 환경)로 본다.
- 문서: `hub/config.py` 템플릿 주석("기본값 ""(bash, 없으면 /bin/sh)"), task-inbox USAGE §1.2 표의 `shell` 줄, task-inbox SPEC:68·:389의 개정 주석(WBX-D4).

### 문서 갱신 배정

| 문서·절 | ticket |
|---|---|
| task-inbox USAGE §10(뒤로 가기 경고, TLS 근본 해결) | WBX-A2 |
| task-inbox USAGE §3 API key(발급 화면 안내) | WBX-A4 |
| `skills/README.md`, `SKILL.md`, `.env.example`(영어) | WBX-B2 |
| `skills/README.md` agent별 설치 절(영어), task-inbox USAGE §3의 한 줄 안내 예문 | WBX-B11 |
| `omp_bridge/board/ROLE.md` | WBX-B5 |
| `hub.toml` 템플릿 주석: `skill_repo_url` 추가 / `/mcp` 문구 → REST | WBX-B3 / WBX-B8 |
| task-inbox SPEC "API key", "MCP Streamable HTTP endpoint"(→ REST 절로 교체·이 SPEC 링크), "Board agent 세트", `allow_from` 설명(:120), "웹 route와 화면" 표의 `/mcp` 줄 | WBX-B9 |
| task-inbox USAGE 머리의 "외부 `/mcp`", §1.2 `allow_from` 줄, §4(→ skill 설치), §7.4, §10.2 | WBX-B9 |
| workbench-hub SPEC·USAGE의 MCP 문구, ADR-0004:18, ADR-0005:12(개정 주석), CONTEXT.md의 "(WBX 트랙 B 완료 전에는 MCP가 현행)" 표시 제거, `README.md`의 MCP 언급 | WBX-B9 |
| task-inbox `PROBE-OMP-MCP.md`, TI ticket | 고치지 않는다(이력) |
| (s4) task-inbox USAGE §7(머리, 7.2 연결, 7.3 재시작, 7.4 home), §8 감사, §10.3 close code를 트랙 D 현재 동작으로 다시 씀; task-inbox BRIEF·SPEC·ADR-0003·CONTEXT.md의 2026-10-29 개정 주석은 r0.5 작성 때 달았고 D3는 확인만; CONTEXT.md의 "(트랙 D 완료 전에는 현행)" 표시 제거 | WBX-D3 |
| (s5) task-inbox USAGE §1.2 표의 `shell` 줄, `hub/config.py` 템플릿 주석, task-inbox SPEC:68·:389의 개정 주석(`$SHELL` → bash → `/bin/sh`) | WBX-D4 |
| (s5) task-inbox USAGE: §6에 "작업 공간에서 열기", §7에 프로젝트 pane widget 절(보기·입력, TUI와 함께 쓰기, 크기 따름, Ctrl-d, host 입력 주인과 거절 이유, 버튼, 실패 표시, 이전 판 backend 재시작 안내), §8 감사 `pane_*`, §10.3에 pane id의 4404·4429; core-workbench SPEC:211(`ui_v1` 설명)에 공유 attach 개정 주석; CONTEXT.md에 "프로젝트 pane widget" 용어; task-inbox BRIEF·SPEC의 2026-10-29 r0.6 개정 주석(D-08·D-09·P2-AC-21·SPEC:318·:515)은 r0.6 작성 때 달았고 E4는 확인만 | WBX-E4 |

## 상태 전이, 실패와 경계 동작

- REST의 요청 상태 전이·권한·한도는 `InboxService` 그대로다. 새 상태 없음.
- 인증 실패는 body를 읽기 전 401. Body 한도는 인증 뒤(선언 길이는 handler 진입 전 413). 순서: (외부) allow_from 403 → Host 421 → Origin 403 → 인증 401 → method 405 → content type 415 → body 413/400 → 처리.
- Cancel token은 메모리만이다. Hub 재시작 뒤 token은 `token_mismatch`.
- Board 전환 뒤 isolation 실패는 시작 거절(fail closed). Probe 실패(WBX-B4 결과 failed/inconclusive)면 WBX-B5·B8·B9와 그 후손을 멈추고 사용자에게 묻는다. 트랙 A, B1~B3, B6·B7는 계속한다.
- `wb_inbox.py`는 재시도하지 않는다(재시도는 agent가 같은 idempotency key로 한다).

## 구현 결정

### 합의된 제약(BRIEF)

WBX-D01~D19, BRIEF "제약과 호환성"의 유지 항목(등급·scope, idempotency, 두 단계 취소, 입력 한도, 결과 범위, key 1회 표시, 웹 기본 비활성, `allow_from`, CSP, CDN 없음, 새 의존성 없음), ADR-0003·ADR-0006.

### 기술 보완(이 문서가 정함)

1. REST status·오류 표, 201/202 구분, catch-all 404, 405/415 code("외부 요청 API" 절).
2. 세션 확인은 기존 `/api/badge` 재사용(새 route 없음).
3. Nav 상태 점은 render 시점 값, `WebServices.board_state`.
4. 배너 접힘은 `<head>`의 동기 script + `<html>` class(깜빡임 방지).
5. Copy fallback(평문 HTTP는 secure context가 아님).
6. `wb_inbox.py` CLI 모양, 변수별 조회, exit code, redirect 거부, 기본 idempotency key.
7. `/skill/` 고정 목록과 두 가지 치환, `text/plain`.
8. `[web] skill_repo_url` 이름과 검사.
9. Board skill 복사 디렉터리, `skill_missing` 실패, Hub env 위생 목록.
10. MCP 처리 계층의 중립 module(`inbox_api.py`)로의 expand.
11. (s3) `/`의 303과 `/dashboard`, `back_forward` 판정, pagehide의 fetch abort·timer 정지.
12. (s3) README 설치 block의 단계와 fallback, 새 치환 없이 절대 주소 만들기.
13. (s3) 격리 확인 OMP 종료 단계와 유예 값, Board log 위치·상한·key 가림.

### 모듈 배치

| 대상 | 위치 |
|---|---|
| 공유 처리 계층 | `src/workbench/hub/inbox_api.py`(새) |
| REST | `src/workbench/hub/rest.py`(새; 외부 route 등록 + `create_internal_app`·`InternalListener`를 `mcp.py`에서 옮김) |
| `/skill/` | `src/workbench/hub/web/pages/skill.py`(새) |
| Nav·배너·cache | `web/render.py`, `web/middleware.py`, `web/app.py`(`WebServices.board_state`), 각 page frame helper, `static/{session-check,login-restore,insecure-banner,copy}.js`, `static/base.css`/`app.css` |
| Key 안내 | `web/pages/settings.py` |
| Board | `hub/board/launch.py`, `hub/process.py`(`PROJECT_ENV`), `hub/server.py`, `omp_bridge/board/ROLE.md` |
| Skill·packaging | `skills/**`, `build_support/assets.py`, `MANIFEST.in`, `.gitignore`, `hub/config.py` |
| (s3) `/dashboard`·뒤로 가기 | `web/render.py`(`HOME_PATH`), `web/pages/login.py`(`/`·`/dashboard` route), `web/middleware.py`(돌아갈 곳 fallback), `pages/{dashboard,projects,board_status}.py`, `static/{session-check,dashboard,badge}.js` |
| (s3) Skill 설치 README | `skills/README.md`, `web/pages/settings.py`(한 줄 안내) |
| (s3) 로그인 완화 | `backend/launcher.py`(`_stop_group`, `check_isolation`), `hub/board/launch.py`·`hub/board/set.py`(log 보존), `tests/integration/live_real_summary.py`(복사 제거) |
| (s5) 공유 attach | `contracts/ui_v1.py`(type·reason), `backend/ui_server.py`(공유 집합·fan-out·조건·크기), `backend/service.py`(state push 조건, snapshot `shared_attachments`) |
| (s5) Hub 중계 | `hub/board/panes.py`(새, `PaneRelay`), `hub/board/ws.py`(pane id 분기·message), `web/pages/board_api.py`(pane 동작 API, `projects`), `web/app.py`(route), 감사 |
| (s5) Pane 화면 | `static/board-pane.js`(새, 순수 logic), `static/board.js`·`board-layout.js`·`board-panel.js`·`board.css`, `web/pages/board.py`, `web/pages/projects.py`(링크) |
| (s5) Board shell | `hub/board/launch.py`(`prepare_shell`), `hub/config.py`(템플릿 주석) |

## 수락 기준

BRIEF의 WBX-AC-01~23을 그대로 쓴다. 대응(PLAN.json `acceptance_trace`가 정본):

| AC | ticket | seam(후보) | 증거 수준 | 최종 통합 |
|---|---|---|---|---|
| 01 | A1 | S1(+S6) | fixture | I1 + 사용자 browser `pending` |
| 02 | A2 | S1 | fixture | I1 |
| 03 | A2, A6(보충: `back_forward`) | S1 + S8 | fixture | I1 + 사용자 browser `pending`(A5 재확인) |
| 04 | A2(문서), A5 | 사용자 Chrome | runtime(사용자) | A5 |
| 05 | A3 | S1 + S8 | fixture | I1 + 사용자 browser `pending` |
| 06 | A4 | S1 | fixture | I1 |
| 07, 09, 10 | B1, B6 | S2 | runtime(loopback) | I1 |
| 08 | B1, B6 | S1(route audit), S2 | fixture, runtime | I1 |
| 11 | B1, B3 | S1(allow_from), S3(enabled=false) | runtime | I1 |
| 12 | B1, B5, B6, B7, B8 | S2, S3, S5 | runtime, static | I1 |
| 13, 14 | B2 | S4, S5 | runtime | I1 |
| 15 | B3 | S1, S5 | fixture, static | I1 |
| 16 | B5 | S3(fake OMP) | runtime | I1 + 실제 OMP·모델 `pending`(대상 host) |
| 17 | B4(B12 뒤 재실행, WBX-D33) | 실제 OMP probe(이 host 로그인, WBX-D27) | runtime | B4 기록 |
| 18 | C0 | 사용자 선택 기록(WBX-D14, 충족) | static | C0 |
| 19, 20 | C1, C2 | S1, S5, S6, S8 | fixture, static | I1 |
| 21 | A6 | S1 + S6 | fixture | I1 + 사용자 browser `pending`(A5) |
| 22 | B11 | S5 + S1 + S4(제공 README의 설치 block을 temp HOME에서 실행) | runtime(loopback) | I1 + 실제 agent(Claude Code·Codex·OMP)의 설치 `pending`(대상 host) |
| 23 | B12 | 기존 backend 격리 seam(`tests/backend/test_omp_isolation*.py`, fake OMP) + S3 + S5 | runtime, static | I1 + B4 재실행의 log |
| 24 (s4) | A8 | S5(정적 CSS) + S8(JS를 바꾸면) + S7 | static, runtime(사용자) | D3 + 사용자 IME `pending` |
| 25 (s4) | D2 | S8(`board-layout.js`) + S1·S6 + S5 + S7 | fixture, static | D3 + 사용자 browser `pending` |
| 26, 35, 36 (s4) | D2 | S8 + S1·S6 + S7 | fixture | D3 + 사용자 browser `pending` |
| 27, 28, 29, 31, 37 (s4) | D1 | S3 + 기존 Board seam(`test_board_set`·`test_board_launch`) + S1 | runtime | D3 |
| 30 (s4) | D1(규칙·저장), D2(panel·제목) | 기존 Board seam + S1 + S8 | runtime, fixture | D3 |
| 32, 33 (s4) | D1(server·`board-state.js`), D2(표시) | T5(`test_board_ws.py`) + S8 | runtime(loopback), fixture | D3 + 두 기기 사용자 확인 `pending` |
| 34 (s4) | D1 | T5 + S1(`audit` 행을 `sqlite3`로 읽음) | runtime | D3 |
| 38 (s4) | D1 | S1(route audit, Bearer 401, CSRF) + S3(정지 확인 목록) + S6(이름 escape) | fixture, runtime | D3 |
| 39 (s5) | E1(backend 공존·fan-out), E2(중계·WebSocket), E3(표시) | Hub SPEC S5 + S6 + S8(TUI 불변), T6 + `ws_client` | runtime | E4 + 실제 TUI·browser 사용자 확인 `pending`(WBX-O09) |
| 40 (s5) | E1 | Hub SPEC S5(`test_ui_server*`), 계약 seam(`test_ui_v1*`) | fixture | E4 |
| 41 (s5) | E1(backend 규칙), E2(Hub 크기 주인), E3(letterbox) | Hub SPEC S5, T6, S8 | fixture, runtime | E4 + 사용자 `pending` |
| 42 (s5) | E1(admit 경로), E3(Ctrl-d 규칙·거절 표시) | `test_panes.py` + Hub SPEC S6, S8 | runtime, fixture | E4 |
| 43 (s5) | E2(API·결과 전달), E3(버튼·dialog) | T6 + S1, S8 | runtime, fixture | E4 + 사용자 `pending` |
| 44 (s5) | E2 | T6 + S1(`audit` 행을 `sqlite3`로 읽음) | runtime | E4 |
| 45 (s5) | E3 | S8 + S1·S6(panel 틀, 프로젝트 화면 링크) + S5 | fixture, static | E4 + 사용자 `pending` |
| 46 (s5) | E2(실패·재연결·한도), E3(표시) | T6 + Hub SPEC S4(`contract_backend.py`), S8 | runtime, fixture | E4 |
| 47 (s5) | E2 | S1(route audit, Bearer 401, CSRF, id 404) + S6(이름 escape) + Hub SPEC S5(공유 attach 조건) | fixture | E4 |
| 48 (s5) | D4 | 기존 Board seam(`test_board_launch.py`, 후보 인자) + S3(`/proc/<pid>/exe`) | fixture, runtime | D4 |

## 테스트 결정

**아래 seam S1~S8은 사용자가 확정했다(2026-10-27, WBX-D20·D21).** 기존 seam(task-inbox SPEC T2·T3·T4·T6b·T9·T10)을 재사용하는 것은 그렇게 표시했다.

| seam | 형태 | 잡는 것 | 놓치는 것 | 독립 기대값 |
|---|---|---|---|---|
| S1 (=T2 재사용) | aiohttp `TestServer`/`ClientSession`으로 in-process `create_app(services, routes=Hub(...).web_routes())`, 실제 auth·inbox store, temp 상태 | route mark(route audit), Cache-Control 표, nav·배너·key 안내 HTML, REST status·오류 JSON, cookie 무시, Origin, `/skill/` 치환, `allow_from` 403 | 실제 bind·TLS·프로세스, JS 실행, bfcache, 실제 Chrome | 이 SPEC 표, BRIEF 문구 |
| S2 (=T4 재사용·개명) | 실제 loopback Hub web app을 자기 loop thread에서 실행(`support/rest_hub.py`, `mcp_hub.py` 후계) + stdlib REST client(`support/rest_client.py`, `http.client`) | endpoint별 동작, idempotency, 두 단계 취소(주입 monotonic clock으로 TTL), 한도, scope, 폐기·만료 key, 이전한 MCP 동작 | Hub 프로세스 수명, 내부 listener 배선, 실제 backend 취소(T6) | task-inbox SPEC tool·한도·권한 표, 이 SPEC "외부 요청 API" 표, stdlib `sqlite3` 읽기 |
| S3 (=T3·T6b 재사용) | 실제 `wb _hub --foreground`(`fixtures/web_sandbox.py`) + fake OMP(`fixtures/fake_omp_board.py`를 skill 보고하도록 확장) + `/proc/<pid>/environ`·`/proc/net/tcp` | Board agent·shell env, Hub env 위생, 내부 listener REST·`/mcp` 없음, isolation 통과·거절, loopback 뺀 `allow_from`에서도 Board REST, enabled=false에서 REST·`/skill/` 닫힘 | 실제 OMP의 skill 로드·모델 행동(→ B4 probe, 대상 host) | 이 SPEC "Board agent 전환", `/proc` 값 |
| S4 (새) | `python3 -I skills/wb-inbox/wb_inbox.py …` subprocess, temp HOME·`XDG_CONFIG_HOME`·통제한 env로 S2 Hub에 실제 HTTP | CLI 인자, JSON 출력, exit code, 조회 순서, `.env` 권한 경고, key가 stdout/stderr에 없음(출력에서 key 문자열 검색), redirect 거부(테스트 서버 307) | 다른 Python 버전, 실제 agent(Claude Code·Codex)의 SKILL.md 해석, 자체 서명 TLS | 이 SPEC "`wb-inbox` skill" 표 |
| S5 (=T9 재사용 + `tests/backend/installation/test_package_assets.py`) | wheel build 결과·repo 파일 정적 검사 | `_assets/skills/**` 포함·`.env` 제외, README 첫 줄, page·CSS·JS의 외부 URL·inline script 없음, `/skill/` 응답 = 설치본(치환 제외), `mcp` 흔적 없음 | runtime | BRIEF 문구, 설치본 bytes |
| S6 (S1의 보조) | S1 응답을 stdlib `html.parser`로 읽는 DOM helper(기존 `test_web_pages_*`의 방식) | nav 링크·`aria-current`·`<details>`·toggle, chip `title`, copy 대상 `<pre>` | CSS 배치·시각·JS | 이 SPEC 트랙 A 절 |
| S7 (사용자) | 사용자의 실제 Chrome(평문 HTTP LAN) | AC-04, bfcache 복원, 배너 기억, 복사 버튼, 좁은 화면 접힘 | 자동 반복 | 사용자 관찰 기록 |
| S8 (=T10 재사용) | `tests/hub/test_board_state_js.py`가 `tests/hub/js/*.test.mjs`를 `node --test`로 실행(node 없으면 skip, package·설치 없음). 새 JS는 순수 함수 + stub global(DOM·`localStorage`·`fetch`·`navigator.clipboard`·`document.execCommand`)로 시험 | `session-check.js`(persisted일 때만 확인, 401 → `/login`, 오류 무시), `login-restore.js`, `insecure-banner.js`(저장 값→class, 저장소 예외 시 펼침, 닫기·다시 펼치기), `copy.js`(clipboard → execCommand → 수동 안내 순서), 트랙 C `dashboard.js`(JSON → 숫자·표, 401) | 실제 browser의 bfcache·CSS·clipboard 권한(→ S7) | 이 SPEC 트랙 A 절 |

- (s4) 트랙 D의 JS logic(`board-state.js` v2, `board-layout.js`, panel 상태)도 S8로 RED → GREEN 한다. Pointer·DOM glue(`board.js`)와 끌기 감각·두 기기 공유 연결은 S7이다. 새 seam·새 도구 없음.
- JS(`session-check.js`, `login-restore.js`, `insecure-banner.js`, `copy.js`, 트랙 C `dashboard.js`, s3의 `back_forward` 판정과 pagehide 정리)의 logic은 S8(기존 node seam)로 RED → GREEN 한다. s3의 headless Chrome DevTools 진단(A6)은 seam이 아니다. 실제 browser 동작은 S7. 새 도구 없음(WBX-D21). 명령: `… -m unittest discover -s tests/hub -p 'test_board_state_js.py'`.
- TDD 예외: `skills/README.md`·`SKILL.md`·`ROLE.md`·USAGE·SPEC 문서(B2 일부, B5 일부, B9, A2·A4의 USAGE 절)는 RED 없이 S5 정적 확인. 순수 이동(B1의 expand 단계 0)은 RED를 만들지 않고 기존 MCP 테스트 GREEN 유지로 증명한다. Probe(B4)는 실험 기록. CSS 시각은 S7.
- 명령(공통 접두 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest`):
  - REST: `discover -s tests/hub -p 'test_hub_rest*.py'`; route audit: `discover -s tests/hub -p 'test_web_route_audit.py'`
  - 화면: `discover -s tests/hub -p 'test_web_*.py'`; allow_from·enabled: `-p 'test_hub_web_*.py'`
  - Board: `discover -s tests/hub -p 'test_board_*.py'`
  - Skill client: `discover -s tests/hub -p 'test_wb_inbox*.py'`
  - Packaging: `discover -s tests/backend/installation -p 'test_package_assets*.py'`
  - (s3) Dashboard 주소·nav·cache: `discover -s tests/hub -p 'test_web_dashboard*.py'`, `-p 'test_web_nav*.py'`, `-p 'test_web_cache*.py'`; JS: `-p 'test_board_state_js.py'`
  - (s3) Skill README·설치: `discover -s tests/hub -p 'test_web_skill*.py'`, `-p 'test_wb_inbox*.py'`, `-p 'test_web_pages_settings*.py'`
  - (s3) 격리 확인 종료·log·helper: `discover -s tests/backend -p 'test_omp_isolation*.py'`, `-p 'test_paths_launcher.py'`; Board `discover -s tests/hub -p 'test_board_*.py'`; helper 정적 검사 `discover -s tests/integration -p 'test_real_summary_independent.py'`와 B12가 더하는 정적 검사 module
  - 통합 회귀(I1): `discover -s tests/hub`, `discover -s tests/backend/installation`, `discover -s tests/backend -p 'test_omp_isolation*.py'`
  - (s4) IME(A8): `discover -s tests/hub -p 'test_web_pages_board*.py'`, JS를 바꾸면 `-p 'test_board_state_js.py'`
  - (s4) 트랙 D server(D1): `discover -s tests/hub -p 'test_board_*.py'`, `-p 'test_hub_auth*.py'`, `-p 'test_inbox_service.py'`, `-p 'test_hub_board_status_text.py'`, `-p 'test_web_route_audit.py'`, `-p 'test_web_dashboard*.py'`; JS protocol `-p 'test_board_state_js.py'`
  - (s4) 트랙 D 화면(D2): `discover -s tests/hub -p 'test_web_pages_board*.py'`, `-p 'test_web_board_csp*.py'`, `-p 'test_board_state_js.py'`(node: `board_layout*.test.mjs` 등)
  - (s4) 트랙 D 통합(D3): `discover -s tests/hub`, `discover -s tests/backend -p 'test_omp_isolation*.py'`
  - (s5) Board shell(D4): `discover -s tests/hub -p 'test_board_launch*.py'`, `-p 'test_board_*.py'`(S3 포함), `-p 'test_hub_config_template.py'`
  - (s5) 공유 attach(E1): `discover -s tests/contracts -p 'test_ui_v1*.py'`, `discover -s tests/backend -p 'test_ui_server*.py'`, `-p 'test_panes.py'`; 실제 backend(Hub SPEC S6) `discover -s tests/hub -p 'test_project_control*.py'`; TUI 불변 `discover -s tests/ui -p 'test_product_model*.py'`, `-p 'test_product_pty*.py'`
  - (s5) Hub 중계(E2): `discover -s tests/hub -p 'test_board_pane*.py'`, `-p 'test_board_ws*.py'`, `-p 'test_web_route_audit.py'`, `-p 'test_hub_list*.py'`(contract fixture 회귀)
  - (s5) 화면(E3): `discover -s tests/hub -p 'test_web_pages_board*.py'`, `-p 'test_web_pages_projects*.py'`, `-p 'test_web_board_csp*.py'`, `-p 'test_board_state_js.py'`(node: `board_pane*.test.mjs` 등)
  - (s5) 트랙 E 통합(E4): `discover -s tests/hub`, `discover -s tests/backend -p 'test_ui_server*.py'`, `discover -s tests/contracts`, `discover -s tests/ui -p 'test_product_*.py'`
- (s5) 트랙 E·D4는 새 seam이 없다. 재사용: 계약 seam, backend ui_server seam(Hub SPEC S5), Hub SPEC S4·S6·S8, task-inbox T5·T6, S1·S3·S5·S6·S7·S8. 실제 TUI(S8 PTY e2e)와 웹 WebSocket(T6)을 한 시험에서 함께 쓰는 조합은 쓰기 전에 사용자 확인(BRIEF WBX-O10). E1·E2는 독립 test_designer(Sonnet) 필수. 보안·순서 초점: 공유 attach 조건 우회(이름만 맞음, 다른 uid, Hub가 아닌 같은 uid process), 공유 attach가 primary 자리를 차지하는 경로, 공유 attach의 overflow가 TUI를 끊는 경로, TUI가 붙은 뒤 웹 resize가 이기는 경로, replay와 live 사이의 빠짐·중복, `reset` 없이 다른 session bytes가 이어지는 경로, 입력 순서 뒤바뀜, lone Ctrl+C가 합쳐지는 경로, 감사에 입력 내용이 남는 경로, Bearer·cross-origin으로 pane에 닿는 경로.
- Hub·backend runtime을 만드는 테스트(S2·S3·S4)는 CLAUDE.md "테스트 실행" 규칙: `/tmp` 아래 짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일.
- 각 ticket 뒤 독립 test_designer(Sonnet)가 계약에서 시나리오를 먼저 도출하고, reviewer가 diff를 본다. 보안 초점: REST cookie 무시, token 재사용·타 key, 오류에 경로·stack 없음, `/skill/`에 `.env`·key 없음, Board shell env, `wb_inbox.py` 출력의 key.

## 호환성, 통합, 롤아웃 제약

- 기존 API key는 그대로 동작한다. MCP client 설정은 B8 이후 동작하지 않는다(WBX-D07, 이행 기간 없음). 사용 안내(B9)에 MCP 설정을 지우고 skill을 설치하라고 적는다.
- `/api/badge`, `/api/projects/…` 응답 형태 불변.
- Commit·push·배포는 별도 요청. 실제 사용자 Hub에 적용(`wb update`)은 사용자가 한다.
- 격리 실행(B4 probe, S3를 쓰는 ticket, I1)의 checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시(`*/.omp/natives/`, Board home). 실행 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 열린 결정·사용자 확인 항목

s1의 U1~U10은 2026-10-27 사용자가 모두 제안대로 답했다. 기록은 BRIEF r0.2:

| s1 ID | 답 | BRIEF |
|---|---|---|
| U1 | seam S1~S7 확정 | WBX-D20 |
| U2 | JS logic은 기존 node seam(S8), 실제 browser는 S7, 새 도구 없음 | WBX-D21 |
| U3 | `/` 10초 refresh는 트랙 C까지 유지. A5는 트랙 A 뒤, 자동 갱신 없는 화면(요청 목록·상세 등) 사이 뒤로 가기로 확인 | WBX-D22 |
| U4 | `SSL_CERT_FILE` 안내만, 새 변수 없음 | WBX-D23 |
| U5 | 내부 listener는 이번 시작의 system key만 받음 | WBX-D24 |
| U6 | 기본 idempotency key는 내용 hash, `--new`로 새 요청 강제 | WBX-D25 |
| U7 | agent용 한 줄 안내는 영어 | WBX-D26 |
| U8 | probe는 이 host의 기존 OMP 로그인으로 실제 모델 1회 호출 | WBX-D27 |
| U9 | Hub가 켠 manager·worker에도 `WB_API_KEY`·`WB_HUB_URL` 없음 수용 | WBX-D28 |
| U10 | mockup 방향 A 선택, C0 충족 | WBX-D14 |

남은 열린 항목 없음. Probe(WBX-B4)가 failed/inconclusive면 그때 사용자 결정(BRIEF WBX-D11).

s4(2026-10-29)의 결정은 BRIEF r0.5 WBX-D41~D58에 있다. 사용자가 바꿀 수 있는 orchestrator 결정은 BRIEF WBX-O08에 모았다(구현을 막지 않음). 이 SPEC s4의 기술 보완 중 사용자 노출 동작에 닿는 값: 격자 칸 크기·widget 최소 크기, `/ws/board/*` 전체 128·세션당 hello 전 8, panel 목록 5 s 갱신, 확인 dialog 문구. D2가 격자 값을 바꾸면 출력에 적는다.

s5(2026-10-29 오후)의 결정은 BRIEF r0.6 WBX-D59~D69에 있다. 사용자가 바꿀 수 있는 트랙 E orchestrator 결정과 seam 조합은 BRIEF WBX-O10에 모았다(seam 조합만 E2 전 확인 필요, 나머지는 구현을 막지 않음). 이 SPEC s5의 기술 보완 중 사용자 노출 동작에 닿는 값: relay 닫기 유예 30 s, `MAX_SHARED_ATTACHMENTS` 4, 이전 판 backend 다시 확인 60 s, 동작 API 답 대기 10 s, "작업 공간에서 열기"가 세 widget을 모두 엶, pane widget의 ×는 확인 없이 감추기만, Board shell env의 `SHELL`을 고른 program으로 둠.

s3(2026-10-28)의 결정은 BRIEF r0.3 WBX-D29~D35에 있다. 사용자에게 물을 새 항목은 없다. 남은 관측: Chrome "Always use secure connections" 설정(A5), 실제 OMP의 RPC stdin EOF 종료(B12), B4 재실행 판정. 메모로 남긴 후속: REST 인증의 executor 공유(위 "외부 요청 API" 메모).

### 트랙 C 선택(WBX-D14, 2026-10-27)

- 방향 A: 차분한 중립 admin console. 자료: [mockups/a/](mockups/a/)(`dashboard.html`, `dashboard-collapsed.html`, `login.html`, `style.css`, `app.js`). 이 폴더는 참고 자료이며 제품 파일이 아니다.
- WBX-C1은 `mockups/a/style.css`를 출발점으로 `src/workbench/hub/web/static/`의 CSS를 다시 쓴다. Token 이름은 그 파일의 custom property(`--bg`, `--surface`, `--surface-2`, `--text`, `--muted`, `--border`, `--border-strong`, `--accent`, `--accent-fg`, `--accent-hover`, `--link`, `--focus`, `--ok`, `--danger`, `--warn-*`, `--err-*`, `--chip-<state>-bg/fg`, `--hdr-*`, `--badge-*`, `--font`, `--mono`, `--fs`, `--gap`, `--pad`, `--gutter`, `--radius`, `--radius-lg`, `--shadow`, `--card-shadow`, `--maxw` 등)를 따른다. 제품에 맞지 않는 것(mockup 전용 보조 값)은 C1에서 정리하고 차이를 출력에 적는다. CSP(`style-src 'self'`, inline 없음)와 외부 URL 없음은 그대로다.
- `mockups/a/app.js`는 mockup 시연용이다. 제품 JS는 CSP·S8 규칙에 맞게 C2가 새로 쓴다.
- Dashboard 배치(요약 card·프로젝트 표·최근 요청·Board card)는 `dashboard.html`, 배너가 접힌 상태는 `dashboard-collapsed.html`, 로그인 화면은 `login.html`을 따른다. Dashboard JSON은 세션 API `GET /api/dashboard`(`no-store`, 세션 없으면 401), 갱신 주기 10 s로 정한다(C2).

## Ticket 그래프

PLAN.json이 정본이다(의존·자원·gate·acceptance trace). 요약:

- 트랙 A: A1 nav, A2 cache·bfcache, A3 배너, A4 key 안내(← B3), A6 `/dashboard`·뒤로 가기(s3, 선행 없음), A7 뒤로 가기 화면 가림(PLAN p4, ← A6, WBX-D36), A5 사용자 Chrome(← A1·A2·A3·A6·A7·B4; 2026-10-28 일부 확인).
- 트랙 B: B1 REST(expand) → B2 skill·client → B3 `/skill/`·packaging·config → B4 probe(← B1·B2·B12, s3: B12 뒤 재실행) → B5 Board 전환(← B3·B4) ; B6·B7 테스트 이전(← B1) → B8 MCP 제거(← B5·B6·B7) → B9 문서. s3: B11 skill 설치 README(← B3), B12 로그인 완화(선행 없음).
- 트랙 C: C0 mockup 선택 gate(충족: 방향 A) → C1 design system(← A1·A2·A3) → C2 dashboard.
- I1 최종 통합(← A4·A5·A6·A7·B9·B11·B12·C2).
- (s4) 트랙 A 보충: A8 한글 IME(선행 없음, 진행 중). 트랙 D: D1 server(← A8; registry·agent 여러 개·shell 여러 개·agent별 신원·공유 연결과 `board-state.js` protocol v2·한도·감사·이행) → D2 workspace 화면(← A8·D1) → D3 문서·통합 확인(← D1·D2). D1은 `board-state.js`와 현행 두 tab 화면의 최소 glue도 v2로 맞춰 D1과 D2 사이에도 `/board`가 동작하게 한다(현행 `board.js`는 protocol 규칙을 `board-state.js`에 둔다).
- 처음 시작 가능: A1, A2, A3, A6, B1, B12(C0은 충족). `render.py`를 쓰는 A1·A2·A3·A6·C1·C2는 자원 `file:src/workbench/hub/web/render.py`로 직렬화한다. `backend/launcher.py`를 쓰는 B12는 B4 재실행·B5보다 먼저 끝난다.
- (s5) Board shell: D4(선행 없음, `hub/board/launch.py`). 트랙 E: E1 backend 공유 attach(← D3: 트랙 D 통합 뒤) → E2 Hub 중계·endpoint·동작 API·감사(← E1) → E3 pane widget·버튼·panel 묶음·프로젝트 화면 링크(← E2) → E4 문서·통합(← E3). E1·E2는 Opus + 독립 test_designer, E3·E4·D4는 Sonnet. E2가 쓰는 `hub/board/ws.py`와 E3가 쓰는 `board.js`·`board.css`는 트랙 D(D1·D2) 뒤라 의존 사슬로 직렬화된다.
- (s5) 요구사항 정본: [requirements-p6.json](requirements-p6.json)(WBX-AC-01~48; p6은 AC-39~48과 gate WBX-G-D4·E1·E2·E3·E4를 더함). PLAN p6 기준 `validate_plan.py` valid.
- 요구사항 정본: [requirements-p5.json](requirements-p5.json)(p1~p4 판은 이력으로 보존)(WBX-AC-01~38, gate WBX-G-*; p5는 AC-24~38과 gate WBX-G-A8·D1·D2·D3를 더함). PLAN p5 기준 `validate_plan.py` valid. p4까지: WBX-AC-01~23, p4는 수락 기준·gate 변화 없이 authority만 BRIEF r0.4로 갱신.
