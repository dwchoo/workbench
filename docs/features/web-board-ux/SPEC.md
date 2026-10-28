# Web Board 사용성·외부 요청 API — 구현 명세

Revision: s3; source brief: [BRIEF.md](BRIEF.md) r0.3 (사용자 승인 2026-10-27, r0.3은 2026-10-28 결정 추가). state: 초안(bundle 승인은 digest와 함께 따로 기록). 작성일: 2026-10-27. s2(2026-10-27): s1의 확인 항목 U1~U10에 사용자가 모두 답했다(BRIEF WBX-D14 선택, WBX-D20~D28). 바뀐 곳: seam 확정, JS node seam(S8), A5 범위, 내부 listener key 제한, `submit --new`, 트랙 C 선택. s3(2026-10-28): 사용자 실제 Chrome 확인의 피드백과 결정(BRIEF WBX-D29~D35, AC-21~23). 바뀐 곳: dashboard 주소 `/dashboard`와 `/` 303, `back_forward` navigation의 세션 확인과 pagehide 정리, skill README 설치 계약, 공유 OMP 로그인 완화, B1 검증 관찰 메모. 새 seam은 없다.

BRIEF의 결정 WBX-D01~D35와 수락 기준 WBX-AC-01~23(AC-03 보충 포함)은 바꾸지 않는다. "기술 보완"은 코드 조사(2026-10-27, HEAD `4898f39`, 작업 트리에 미commit 문서 변경 있음)로 정한 구체 형태다. 약속된 동작·호환성을 바꿀 수 있는 항목은 "열린 결정·사용자 확인 항목"에 모았고 정하지 않았다. Test seam S1~S8은 사용자가 확정했다(WBX-D20·D21). 트랙 C는 mockup 방향 A로 정해졌다(WBX-D14, 아래 "트랙 C 선택").

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
- 세션 확인(`/static/session-check.js`): `pageshow`에서 `event.persisted === true`이거나 이번 문서의 navigation type이 `back_forward`이면 `/api/badge`(`cache: "no-store"`)를 부르고 401이면 `location.replace("/login")`. 같은 문서에서 한 번만 확인한다(첫 `pageshow`와 이후 persisted 복원마다). 네트워크 오류·그 밖의 status는 아무것도 하지 않는다(s2와 같음). Navigation Timing이 없는 browser는 persisted 경로만.
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
- `GET …/result`: 구현은 `run_id`/`artifact_path`를 `full=true`일 때만 받는다(위 표는 조건을 적지 않았다). WBX-B8(MCP 제거) 때 표와 구현 중 어느 쪽에 맞출지 다시 본다.

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
- Hub env 위생: `process.PROJECT_ENV`에 `WB_API_KEY`, `WB_HUB_URL`을 더한다(`WB_BOARD_MCP_KEY`는 이전 버전 Board 프로세스에서 시작한 Hub를 위해 남긴다). 따라서 Hub, Hub가 켠 backend, Board shell은 사용자 shell의 값을 물려받지 않는다.
- Board shell tab: env = `hub_environment(base)` + `TERM` + locale. `WB_API_KEY`·`WB_HUB_URL`이 없다(system key가 보이지 않음).
- Skill 배치: 시작마다 `<hub state>/board-agent/skills/wb-inbox/`(0700)를 설치본 `skills/wb-inbox/SKILL.md`·`wb_inbox.py`의 복사본으로 다시 만든다(`.env`·`.env.example` 없음). `prepare_board_home(skills_dir=<board skills dir>)`, overlay의 skill·provider 설정은 probe(WBX-B4) 결과로 정한다. 이전 버전의 `<agent dir>/mcp.json`은 시작 때 지운다.
- 격리 검사: `check_isolation(..., allowed_skills=("wb-inbox",))`, 허용 MCP 없음. 실패 조건: leak 하나라도(MCP tool 포함), 또는 `wb-inbox` skill이 보이지 않음(`skill_missing:wb-inbox`) → 기존처럼 `isolation_failed`(사유). Manager·worker의 누출 검사는 바꾸지 않는다.
- `omp_bridge/board/ROLE.md`(영어): MCP tool 목록 대신 `wb-inbox` skill 사용 안내(`python3 <skill>/wb_inbox.py projects|list|…`), D-27 두 안내는 `projects` 출력 기준으로 유지, 요청 텍스트는 data.
- `allow_from`에서 loopback을 빼도 내부 listener는 영향이 없다(내부 app에 peer 검사 없음).

### 공유 OMP 로그인 완화(WBX-D32·D33, AC-23, s3)

배경: [INCIDENT-20261027-omp-login.md](INCIDENT-20261027-omp-login.md). Workbench는 인증 저장소 내용을 읽지 않는다(C-D64). 이 절은 그 원칙 아래에서 저장소를 망가뜨릴 수 있는 Workbench 쪽 동작을 줄인다.

- 격리 확인 OMP 종료(`backend/launcher.py:check_isolation`의 `_stop_group`, manager·worker·Board 공통): 확인이 끝나면 (1) stdin을 닫고(RPC EOF) 최대 5 s 동안 leader와 group이 스스로 끝나기를 기다린다, (2) 남으면 SIGTERM 후 최대 5 s, (3) 남으면 SIGKILL 후 3 s. 전체 상한은 13 s + reap이며 확인 결과(`cleanup`)에 어느 단계에서 끝났는지(`eof`/`sigterm`/`sigkill`)와 걸린 시간을 남긴다. Timeout·cancel로 끝나는 경로도 같은 순서를 쓴다. 실제 OMP 18.12.6이 RPC stdin EOF로 끝나는지와 걸리는 시간은 B12가 관측해 기록한다. EOF로 끝나지 않으면 (1)은 확인마다 5 s를 더하므로 B12는 멈추고 관측값과 함께 보고한다(유예 값 조정은 orchestrator 판정). 값(5 s / 5 s / 3 s)은 기술 보완이며 상수로 둔다.
- Board OMP log 보존: `<hub state>/board-agent/logs/`(0700, 파일 0600)에 Board agent 시작마다 (a) 격리 확인 OMP의 stderr 전체(지금은 임시 파일에서 64 KiB만 읽고 버린다)와 확인 판정 요약, (b) OMP가 Board home의 agent dir에 쓰는 자체 log(위치는 B12가 OMP 18.12.6에서 확인, 없으면 (a)만)와 Board agent OMP의 종료 상태를 시작 시각 이름의 파일로 남긴다. 상한(기술 보완): 파일당 1 MiB(넘으면 앞부분을 버리고 끝을 남긴다), 최근 10회 시작분. Hub 재시작·Board 재시작 뒤에도 남는다. Log에 system key 값·`WB_API_KEY`가 들어가지 않는다(쓰기 전 이번 key 문자열을 가린다). 인증 저장소 내용은 복사하지 않는다. `wb hub status`·웹 화면은 바꾸지 않는다.
- Test helper: `tests/**`, `tools/**`의 어떤 코드도 실제 사용자 `agent.db`(또는 그 `-wal`·`-shm`)를 복사하지 않는다. 실제 로그인이 필요한 live helper는 C-D64대로 symlink를 쓰거나 실제 로그인을 쓰지 않는다. 빈 가짜 store(`write_bytes(b"")` 등 temp HOME의 placeholder)는 해당하지 않는다. 지금 해당하는 코드: `tests/integration/live_real_summary.py`의 `_copy_auth`(`--auth-source`)와 그 독립 테스트. S5 정적 검사가 다시 생기는 것을 막는다.
- Probe 재실행(WBX-D33): B12 통합 뒤 WBX-B4를 사용자의 실제 로그인으로 다시 실행한다. 실행 전 사용자가 `omp`에서 로그인 상태를 확인한다. 이번 실행의 Board·격리 확인 OMP log(위 위치와 같은 형식, probe root 밖 `/tmp/wbx-b4-out-<날짜>/logs/`)를 지우지 않고 남긴다.

### MCP 제거 순서(expand / migrate / contract)

1. Expand(WBX-B1): MCP 처리 계층(principal, 인자 검사, 오류 정규화, untrusted 표시, cancel token, 결과·프로젝트 모양, executor·key 자리)을 `src/workbench/hub/inbox_api.py`로 옮기고 `mcp.py`는 그 위의 JSON-RPC transport만 남긴다(기존 MCP 테스트가 안전망). 그 위에 `src/workbench/hub/rest.py`를 추가해 외부 listener에 등록한다. 이 시점에는 `/mcp`와 REST가 같은 계층을 쓴다(개발 중 상태이며 배포 단위가 아니다 — WBX-D07의 "병행 기간 없음"은 release 기준).
2. Migrate: skill(B2·B3), probe(B4), Board 전환(B5), 테스트 이전(B6·B7).
3. Contract(WBX-B8): `mcp.py`, `/mcp` route, `McpServer`, `Hub.mcp`/`internal_mcp` 이름, `support/mcp_client.py`·`mcp_hub.py`, `probes/omp_mcp_probe.py`, `test_hub_mcp*.py`를 지운다. Route audit에서 `('*','/mcp')` pin과 CSRF 예외를 지운다. `hub.toml` 템플릿·docstring의 `/mcp` 문구를 REST로 바꾼다. `test_web_assets`의 "mcp SDK import 없음" 검사는 유지한다. Backend launcher의 manager·worker MCP 누출 검사는 유지한다.

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

s3(2026-10-28)의 결정은 BRIEF r0.3 WBX-D29~D35에 있다. 사용자에게 물을 새 항목은 없다. 남은 관측: Chrome "Always use secure connections" 설정(A5), 실제 OMP의 RPC stdin EOF 종료(B12), B4 재실행 판정. 메모로 남긴 후속: REST 인증의 executor 공유(위 "외부 요청 API" 메모).

### 트랙 C 선택(WBX-D14, 2026-10-27)

- 방향 A: 차분한 중립 admin console. 자료: [mockups/a/](mockups/a/)(`dashboard.html`, `dashboard-collapsed.html`, `login.html`, `style.css`, `app.js`). 이 폴더는 참고 자료이며 제품 파일이 아니다.
- WBX-C1은 `mockups/a/style.css`를 출발점으로 `src/workbench/hub/web/static/`의 CSS를 다시 쓴다. Token 이름은 그 파일의 custom property(`--bg`, `--surface`, `--surface-2`, `--text`, `--muted`, `--border`, `--border-strong`, `--accent`, `--accent-fg`, `--accent-hover`, `--link`, `--focus`, `--ok`, `--danger`, `--warn-*`, `--err-*`, `--chip-<state>-bg/fg`, `--hdr-*`, `--badge-*`, `--font`, `--mono`, `--fs`, `--gap`, `--pad`, `--gutter`, `--radius`, `--radius-lg`, `--shadow`, `--card-shadow`, `--maxw` 등)를 따른다. 제품에 맞지 않는 것(mockup 전용 보조 값)은 C1에서 정리하고 차이를 출력에 적는다. CSP(`style-src 'self'`, inline 없음)와 외부 URL 없음은 그대로다.
- `mockups/a/app.js`는 mockup 시연용이다. 제품 JS는 CSP·S8 규칙에 맞게 C2가 새로 쓴다.
- Dashboard 배치(요약 card·프로젝트 표·최근 요청·Board card)는 `dashboard.html`, 배너가 접힌 상태는 `dashboard-collapsed.html`, 로그인 화면은 `login.html`을 따른다. Dashboard JSON은 세션 API `GET /api/dashboard`(`no-store`, 세션 없으면 401), 갱신 주기 10 s로 정한다(C2).

## Ticket 그래프

PLAN.json이 정본이다(의존·자원·gate·acceptance trace). 요약:

- 트랙 A: A1 nav, A2 cache·bfcache, A3 배너, A4 key 안내(← B3), A6 `/dashboard`·뒤로 가기(s3, 선행 없음), A5 사용자 Chrome(← A1·A2·A3·A6·B4; 2026-10-28 일부 확인).
- 트랙 B: B1 REST(expand) → B2 skill·client → B3 `/skill/`·packaging·config → B4 probe(← B1·B2·B12, s3: B12 뒤 재실행) → B5 Board 전환(← B3·B4) ; B6·B7 테스트 이전(← B1) → B8 MCP 제거(← B5·B6·B7) → B9 문서. s3: B11 skill 설치 README(← B3), B12 로그인 완화(선행 없음).
- 트랙 C: C0 mockup 선택 gate(충족: 방향 A) → C1 design system(← A1·A2·A3) → C2 dashboard.
- I1 최종 통합(← A4·A5·A6·B9·B11·B12·C2).
- 처음 시작 가능: A1, A2, A3, A6, B1, B12(C0은 충족). `render.py`를 쓰는 A1·A2·A3·A6·C1·C2는 자원 `file:src/workbench/hub/web/render.py`로 직렬화한다. `backend/launcher.py`를 쓰는 B12는 B4 재실행·B5보다 먼저 끝난다.
- 요구사항 정본: [requirements-p3.json](requirements-p3.json)(p1·p2 판은 이력으로 보존)(WBX-AC-01~23, gate WBX-G-*). PLAN p3 기준 `validate_plan.py` valid.
