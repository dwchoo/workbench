# Task Inbox & Web Board — 구현 명세

Revision: s1; source brief: [BRIEF.md](BRIEF.md) r0.4 (approved 2026-10-25). state: approved (2026-10-25, 사용자가 SPEC·seam 확정을 orchestrator에 사전 위임했고 orchestrator가 검토해 승인했다). 선행: [Hub SPEC](../workbench-hub/SPEC.md) s1과 그 구현(HEAD `3c36901`: HB-01~06·09·10 통합, **HB-07 `workbench.hub.projects`·HB-08·HB-11·HB-12는 아직 없음**).

2026-10-28 개정 주석(web-board-ux WBX-B9): 외부 접점 MCP(`/mcp`, JSON-RPC)는 없어졌다(web-board-ux BRIEF WBX-D05·D07, WBX-B8). 외부 agent와 Board agent는 `/api/v1/` REST(`Authorization: Bearer <API key>`)와 `wb-inbox` skill을 쓴다. 현행 계약은 [web-board-ux SPEC](../web-board-ux/SPEC.md) s3의 "외부 요청 API `/api/v1/`", "`wb-inbox` skill과 `wb_inbox.py`", "`/skill/` 제공", "Board agent 전환", "공유 OMP 로그인 완화", "Cache 정책과 bfcache", "Dashboard 주소와 뒤로 가기" 절과 [ADR-0006](../../adr/0006-rest-skill-single-external-contact.md)이다. 이 문서의 MCP 절·표는 이력으로 두고 주요 자리에만 `[2026-10-28 개정: …]` 주석을 달았다. 그 밖의 "MCP"(웹·MCP, MCP 응답 등)는 REST로 읽는다. 등급·scope·한도·idempotency·두 단계 취소·결과 범위 규칙은 그대로다.

BRIEF의 사용자 결정(D-05~D-30), orchestrator 결정(D-31~D-36), 수락 예시(P2-AC-01~45)와 이번 orchestrator 지시(다른 editor API key는 Board agent와 같은 요청 권한, Board agent 세트가 돌 때 Hub 정지는 확인 뒤 세트 종료, REVIEWING 중 편집은 바로 반영하고 검토를 다시 시작)는 바꾸지 않는다. "기술 보완"은 코드 조사로 정한 구체 형태다. 사용자 노출 동작을 바꾸거나 BRIEF에 없는 예외를 더하는 항목은 "열린 결정"에 모았다. 실행 주체는 Claude orchestrator가 배정하는 Opus/Sonnet subagent다.

## 문제와 해결 방향

Hub(B)는 WB 프로젝트를 등록·관측·켜고 끄지만, 요청을 받아 manager 검토와 실행 결과로 잇는 수단과 브라우저 화면이 없다. 해결은 일곱 부분이다.

1. Hub 프로세스의 asyncio loop에 aiohttp 웹 서버를 붙인다. 비밀번호가 정해지기 전에는 listener를 열지 않는다. 로그인 세션·CSRF·감사 기록·API key는 Hub 상태 디렉터리의 `hub.sqlite3`와 `auth.json`에 둔다.
2. 요청 저장소는 WB 프로젝트마다 `<root>/.wb/inbox.sqlite3`이고 Hub만 쓴다(ADR-0005). 상태 전이·권한·순서는 Hub의 `InboxService` 한 곳에서 정한다.
3. 외부 agent와 Board agent는 Hub의 MCP Streamable HTTP endpoint(`POST /mcp`, 직접 구현)로 접근한다. 웹 양식과 MCP는 같은 `InboxService`를 부른다. [2026-10-28 개정: MCP 대신 `/api/v1/` REST. Board agent는 내부 loopback listener의 REST를 `wb-inbox` skill로 쓴다. [web-board-ux SPEC](../web-board-ux/SPEC.md) "외부 요청 API"·"Board agent 전환"]
4. Hub → backend: 기존 ui_v1 UDS의 attach 없는 연결에 `inbox_notice`·`task_status`·`cancel_task`를 더한다(HB-06의 `workbench-hub` 한정 규칙 확장). Backend → Hub: backend가 hub_v1 socket에 `backend` principal로 접속해 manager 도구의 결과를 기록한다.
5. Manager에게는 새 bridge 도구 `workbench_inbox`(조회·검토 결과 기록)와 `to_worker`의 `request_id` 필드(승인 명세 연결)를 준다. 연결은 worker 전달 **전에** Hub가 허락해야 한다.
6. Board agent 세트(Board agent OMP + 일반 shell)의 PTY는 Hub가 소유하고, 웹은 WebSocket으로 붙는다(epoch·heartbeat·즉시 인수).
7. 웹 제어(켜기/끄기·일시정지·재개·`confirm-boot`)는 HB-07의 `workbench.hub.projects`를 Hub 프로세스에서 부른다.

## 관련 사용자 이야기

- 소유자: VPN 너머 브라우저로 로그인해 모든 WB 프로젝트의 상태·대기 사유·요청 수를 보고, 요청을 쓰고, 질문에 답하고, 실행 결과 요약과 전체 로그를 본다.
- 소유자: 웹의 Board agent에게 "프로젝트 A·B의 미해결 요청 정리해 줘"라고 시키고, 브라우저를 닫았다가 휴대폰에서 다시 열어 이어서 본다.
- 소유자의 외부 agent(Codex 등): API key로 요청을 내고, 질문에 답하고, 결과를 가져간다. 실행 중 취소는 token 확인을 거친다.
- Manager: 쉬고 있을 때 새 요청 알림을 받아 `workbench_inbox`로 읽고, 질문·거절하거나 `to_worker`에 `request_id`를 붙여 Task로 보낸다. 끝나면 요약과 함께 요청을 닫는다.

## 범위와 비목표

- 포함: BRIEF "범위"의 [포함] 전부. 구체적으로 `hub.toml`·listener 수명·TLS·`wb hub passwd`·`wb hub tls --self-signed`, 로그인·세션·CSRF·감사, API key, inbox 저장소·상태 기계, MCP endpoint, 웹 화면 전체, Hub ↔ backend 요청 통로, manager `workbench_inbox`·`to_worker.request_id`, 사용자 Task 취소 경로, 결과 요약·전체 보기, Board agent 세트, WebSocket terminal, `wb hub review-mode`, Hub 목록의 `pending_requests`, 의존성 고정·xterm.js 포함.
- 비목표(BRIEF와 같음): 웹의 manager·worker·host pane 조작, 다중 worker, 범용 scheduler, 파일 첨부, 외부 알림, OAuth·외부 identity, rate limit, Docker, callback/webhook, 전체 대화 동기화, 다중 사용자. `mcp` SDK. Board agent의 sandbox(ADR-0003). 자원 lock·중재(D-27).
- 지원 환경: core와 같은 Linux. 브라우저는 최신 Firefox/Chromium 계열(대상 host 확인).

## 공개 인터페이스, 데이터 계약, 불변 조건

### 위치(기술 보완)

| 대상 | 위치 | 비고 |
|---|---|---|
| 설정 | `$XDG_CONFIG_HOME/omp-workbench/hub.toml`(절대 경로일 때), 아니면 `~/.config/omp-workbench/hub.toml` | Hub 시작 때와 `wb hub reload` 때 읽는다(`tomllib`, 2026-10-27 사용자 결정). 없으면 Hub 시작 때와 `wb hub passwd`가 주석 템플릿을 만든다(아래 "초기 템플릿"). 그 밖에는 쓰지 않는다. 읽을 파일이 없으면 기본값 |
| TLS 기본 경로 | `<config dir>/omp-workbench/tls/hub-cert.pem`, `hub-key.pem` | `wb hub tls --self-signed`가 만든다. key 0600, 디렉터리 0700 |
| 비밀번호 | `<hub state>/auth.json` + `<hub state>/auth.lock` | 0600. `wb hub passwd`와 Hub(웹 비밀번호 변경)가 lock을 잡고 원자적으로 쓴다 |
| Hub DB | `<hub state>/hub.sqlite3` | 0600, WAL. 세션·API key·로그인 실패·감사. Hub만 쓴다 |
| 요청 저장소 | `<root>/.wb/inbox.sqlite3`, `<root>/.wb/inbox.lock` | 0600. Hub만 열고 쓴다(ADR-0005). lock은 Hub가 그 DB를 여는 동안 잡는다 |
| Board agent home | `<hub state>/board-agent/` (`omp-root/`, `omp-root/agent/agent.db` → 사용자 `agent.db` symlink) | 0700. 세션·메모리 영속(D-25) |
| Web 자산 | `src/workbench/hub/web/static/`(설치본 package data), `…/static/vendor/xterm/`(+`LICENSE`) | 외부 CDN 없음(P2-AC-37) |
| Board agent 기본 role prompt | `omp_bridge/board/ROLE.md` | English(agent prompt). 설치 자산 |
| Manager inbox skill | `omp_bridge/skills/workbench-inbox/SKILL.md` | English. manager role allowlist에 더한다 |

`<hub state>`는 Hub SPEC의 상태 디렉터리(`~/.local/state/omp-workbench/hub/`)다.

### `hub.toml` schema(D-10, 기술 보완)

```toml
[web]
enabled = false             # TOML bool. 기본 꺼짐(2026-10-26 사용자 결정). true일 때만 아래 조건으로 연다
bind = "127.0.0.1"          # IP literal 하나 또는 IP literal 목록(2026-10-27). "0.0.0.0"/"::"는 명시할 때만
port = 8780                 # 1..65535. 0은 임의 port(테스트·고급). 실제 port는 status에 보인다
tls_cert = ""               # "" = TLS 기본 경로
tls_key = ""
allow_insecure_http = true  # 기본 허용(D-10)
server_names = []           # bind IP 외에 받을 Host 이름(예: "box.vpn"). DNS rebinding 방어용
allow_from = []             # 접속을 받을 client IP·CIDR 목록(2026-10-27). [] = 제한 없음

[board_agent]
model = ""                  # OMP model selector. "" = omp-models.yml의 manager 항목
role_prompt_file = ""       # 추가 role prompt 파일. 기본 prompt 뒤에 붙는다(대체하지 않는다)
shell = ""                  # "" = $SHELL, 없으면 /bin/sh
```

- 2026-10-27 사용자 결정: bind 여러 주소, allow_from, wb hub reload.
  - `bind`는 IP 문자열 하나(지금과 같음) 또는 비어 있지 않은 IP 문자열 목록(최대 16개)이다. 빈 목록, 같은 주소의 중복(`"::1"`과 `"0:0:0:0:0:0:0:1"`도 중복), 문자열 아닌 값, hostname, scope가 붙은 IPv6는 `config_invalid:web.bind`. `WebConfig.bind`는 첫 주소, `WebConfig.addresses`는 전체다.
  - Listener는 모든 주소에서 같은 `port`로 연다. `port = 0`이면 첫 주소가 받은 port를 나머지 주소에도 쓰고, 그 port가 다른 주소에서 이미 쓰이고 있으면 전체를 새 port로 최대 5번 다시 시도한다. 한 주소라도 열지 못하면 이미 연 주소도 모두 닫고 `bind_failed`(`detail` errno 이름, `address` `<주소>:<port>`)다. 일부만 여는 일은 없다.
  - Host 검사·평문 경고·`wb hub tls` SAN은 모든 bind 주소를 쓴다: Host는 어느 bind IP든 받고, loopback bind가 하나라도 있으면 `localhost`를 받고, wildcard가 하나라도 있으면 IP literal 전부를 받는다. 평문 HTTP이고 loopback이 아닌 주소가 하나라도 있으면 경고한다.
  - `allow_from`은 IPv4·IPv6 주소나 network(`ipaddress.ip_network(strict=True)`)의 목록(최대 256개)이다. Host bit가 있는 값(`"192.168.10.5/24"`)은 정규화하지 않고 `config_invalid:web.allow_from`이다(한 주소인지 /24 전체인지 모호하고, 조용히 넓히면 허용 범위가 256배가 되기 때문). IPv4-mapped IPv6 항목(`::ffff:0:0/96` 안)도 거절한다(peer는 IPv4로 비교하므로 맞을 수 없다). 중복은 하나로 합친다. 없거나 `[]`면 지금과 같이 거르지 않는다. Loopback도 자동 허용하지 않는다. 이 컴퓨터에서 쓰려면 `"127.0.0.1"`, `"::1"`을 넣는다.
- 2026-10-26 사용자 결정: web 기본 비활성, `[web] enabled = true`로 켬. 웹이 가장 많이 노출되는 부분이기 때문이다. `enabled`가 없거나 `false`면 같은 listener가 내는 웹 화면, 외부 `/mcp`, `/ws/board`를 모두 열지 않고, 웹에서만 쓰는 Board agent 세트도 만들지 않는다(그 세트만 쓰는 내부 MCP listener도 시작되지 않는다). registry·목록·hub_v1·inbox writer·검토 전달·ui_v1은 그대로 동작한다. `enabled`가 bool이 아니면 `config_invalid:web.enabled`. [2026-10-28 개정: "외부 `/mcp`"는 `/api/v1/`·`/skill/`, "내부 MCP listener"는 Board agent 내부 REST listener로 읽는다(WBX-D06·D11)]
- 초기 템플릿(2026-10-26 사용자 요청: hub.toml 자동 생성). Hub는 시작할 때마다(autostart·`wb hub start`·systemd·foreground 모두 `wb _hub`의 같은 경로) instance lock을 잡은 뒤, `hub.json`을 내기 전에 `config_path()`에 아무것도 없으면 `config.CONFIG_TEMPLATE`을 쓴다. `wb hub passwd`도 비밀번호를 정한 뒤 같은 함수로 만든다(안내 줄이 가리키는 파일이 실제로 있게). 규칙:
  - 내용: 한국어 주석으로 모든 key와 기본값, 웹 켜는 순서(`enabled = false`를 `true`로 → `wb hub passwd` → `wb hub reload`, 2026-10-27), LAN·평문 HTTP 보안 한 줄, `bind` 목록 예와 `allow_from` 예(loopback 안내, Tailscale `100.64.0.0/10`)를 적는다. 예는 `#   예: ...` 꼴이라 주석을 모두 풀어도 기본값 그대로다. 주석이 아닌 줄은 `[web]`·`enabled = false`·`[board_agent]`뿐이고, 나머지 key는 기본값을 적은 주석이다. 엄격 parser로 읽으면 정확히 `HubConfig()`(모든 기본값)이므로 웹은 사용자가 고치기 전까지 꺼져 있다.
  - 이미 있는 것은 절대 바꾸지 않는다. 빈 파일, `config_invalid`인 파일, 사용자가 고친 파일, symlink(대상이 없는 것 포함)도 그대로 둔다. 같은 디렉터리의 0600 임시 파일(`O_CREAT|O_EXCL|O_NOFOLLOW`)을 쓰고 fsync한 뒤 `link()`로 넣는다(이름이 있으면 `EEXIST`로 실패, symlink를 따라가지 않음). hard link가 안 되는 file system에서는 `O_CREAT|O_EXCL|O_NOFOLLOW`로 직접 쓴다. 동시에 만드는 쪽(다른 `wb hub passwd` 등)에 진 경우는 조용히 넘어간다.
  - 권한: 파일 0600. `omp-workbench` 설정 디렉터리가 없으면 0700으로 만든다. 있는 디렉터리의 권한은 바꾸지 않는다. 디렉터리가 symlink·디렉터리 아님·다른 소유자·group/other 쓰기 가능이면 unsafe로 보고 만들지 않는다.
  - 만들지 못하면(unsafe 디렉터리, `EACCES`·`EROFS` 등) Hub는 `hub.log`에 `[workbench] hub.toml template not created (<이유>): <경로>; the Hub uses the defaults` 한 줄만 남기고 기본값으로 계속 동작한다. `wb hub passwd`는 아무것도 출력하지 않는다.
  - symlink인 `hub.toml`은 읽을 때는 지금처럼 따라간다(대상 파일의 내용을 쓴다). 대상이 없으면 파일 없음과 같아 기본값(`disabled`)이고, 템플릿은 그 symlink나 대상에 쓰지 않는다.
- 모르는 key·잘못된 형식·hostname인 `bind`는 `config_invalid:<항목>`이다. Hub 시작 때면 웹 listener와 Board agent는 열지 않고 Hub의 나머지 기능(registry·목록·제어 socket)은 동작한다. `wb hub reload` 때면 실행 중인 설정을 그대로 둔다(아래 "`wb hub reload`").
- TLS 판정: cert·key 파일이 **둘 다** 있으면 HTTPS(`ssl.create_default_context(Purpose.CLIENT_AUTH)`, TLS ≥ 1.2). 하나만 있거나 읽기·load가 실패하면 `tls_invalid`로 listener를 열지 않는다(평문으로 내려가지 않는다). 둘 다 없으면 `allow_insecure_http=true`일 때 평문 HTTP, `false`면 `insecure_http_disallowed`로 열지 않는다(BRIEF P2-AC-16 "SPEC에서 정함").
- 평문 경고: 평문 HTTP이고 bind 주소 중 하나라도 loopback(127.0.0.0/8, ::1)이 아니면(`0.0.0.0` 포함) `insecure_warning: true`. 웹 모든 page 상단 banner와 `wb hub status`에 `평문 HTTP로 LAN에 열려 있습니다. VPN 등 네트워크 보호가 필요합니다.`를 보인다.

### Web listener 수명(D-10, P2-AC-15·16)

- Listener는 (1) `hub.toml`이 유효하고 (1a) `[web] enabled = true`이고(2026-10-26 사용자 결정) (2) `auth.json`에 유효한 비밀번호가 있고 (3) TLS 판정이 `tls`/`insecure_ok`일 때만 연다. 열기 전과 실패 상태는 hub_v1 `status.web.state`에 남는다: `disabled`(`config`: Hub가 읽은 `hub.toml` 경로. `config_invalid` 다음, 나머지 조건보다 먼저 판정), `password_not_set`, `config_invalid`, `tls_invalid`, `insecure_http_disallowed`, `bind_failed`(errno 이름 detail, 실패한 `address`, 30 s마다 재시도), `listening`(`url`: 첫 주소, `urls`: 모든 주소, `bind`: 첫 주소, `binds`, `port`, `allow_from`, `tls`, `insecure_warning`), `stopping`.
- Hub watchdog(2 s)이 `auth.json`의 (dev, ino, mtime_ns, size)를 본다. 비밀번호가 새로 생기면 Hub 재시작 없이 listener를 연다. `hub.toml`·TLS 파일 변경은 `wb hub reload`(또는 Hub 재시작) 때 반영한다(2026-10-27).

### `wb hub reload`(2026-10-27 사용자 결정)

- hub_v1 local op `reload{}`. 같은 uid의 local principal만 쓴다(backend principal은 `unsupported_type`). Hub는 `hub.toml`을 엄격 parser로 다시 읽고 TLS를 다시 판정한다(cert·key 파일의 stat이 바뀌어도 `web.tls_files` 변경).
- 무효: 실행 중인 설정을 그대로 둔다. 답 `ok:false`, `reason:"config_invalid"`, `detail:"config_invalid:<항목>"`, `item`. `status.config.reload_error = {reason, at}`. CLI는 항목을 적은 한국어 줄을 stderr에 내고 exit 1.
- 유효하고 웹 설정(`enabled`·`bind`·`port`·TLS·`allow_insecure_http`·`server_names`·`allow_from`)이 바뀜: 웹 listener만 다시 만든다. 기존 site를 닫고(열린 HTTP·WebSocket 연결은 끊길 수 있고 Board client는 다시 붙는다) 새 설정으로 연다. `hub.sqlite3`·WebServices(로그인 세션은 `hub.sqlite3`에 있다)·Board terminal은 유지한다. 새 listener를 열지 못하면(port 사용 중 등) 이전 설정으로 되돌리지 않는다: 사용자가 일부러 바꾼 파일과 실제 상태가 어긋나지 않게 하고, 보안 때문에 뺀 주소를 다시 열지 않기 위해서다. `status.web`은 `bind_failed`(원인·주소)이고 30 s마다 새 설정으로 다시 시도한다. CLI는 원인을 보이고 exit 1. Hub는 reload 때문에 죽거나 멈추지 않는다(`RELOAD_TIMEOUT` 20 s, 실패는 `reload_failed`/`reload_timeout`).
- 바뀐 것이 없으면 listener는 그대로이고 답의 `changed`가 `[]`이다.
- 꺼짐→켜짐: listener를 열고 Hub 시작 때처럼 Board agent 세트를 만든다(이미 있으면 그것을 쓴다). 켜짐→꺼짐: listener를 닫는다(외부 `/mcp`, `/ws/board`도 닫힘). 실행 중인 Board agent 세트는 죽이지 않는다(작업 중일 수 있다). 그 세트와 내부 MCP listener는 `wb hub stop`까지 계속 돌고 status에 보이며 답의 `board_agent_note`가 그렇게 말한다. 다시 켜면 같은 세트를 쓴다(중복 생성 없음). [2026-10-28 개정: `/mcp` → `/api/v1/`·`/skill/`, 내부 MCP listener → 내부 REST listener]
- `[board_agent]` 변경은 다음 Board agent 시작 때 적용된다(답의 `board_agent_note`).
- WB 프로젝트 backend·manager·worker를 시작·정지·재시작하거나 signal하지 않는다. registry, inbox writer, 검토 전달도 건드리지 않는다.
- 답: `{changed: [<"web.bind"|"web.port"|...|"web.tls_files"|"board_agent.<key>">], web: <status.web>, board_agent: <status.board_agent>, board_agent_note: <text>|null, config: <status.config>}`. 시작 때 `config_invalid`였으면 `changed`는 `["web", "board_agent"]`.
- `status.config = {path, loaded_at, state:"ok"|"config_invalid", detail?, reload_error: {reason, at}|null}`. `wb hub status`는 `config: loaded <시각> (<경로>)`와, 마지막 reload가 거절되었으면 `config: 마지막 'wb hub reload' 실패 <시각>: <reason> — 지금은 이전 설정으로 동작합니다`를 보인다. 성공한 reload는 오류를 지운다. TUI Hub 목록 header의 web 줄 끝에 `· config HH:MM`, `· reload 실패: <reason>`을 붙인다.
- 같은 loop에 붙고, blocking 작업(scrypt, SQLite, `/proc`, ui_v1 연결)은 executor에서 돈다.
- 정상 정지 순서(Hub SPEC 정지 순서의 (A) 확장 지점): 새 HTTP 연결 거절(site 정지) → WebSocket을 1001로 닫음 → Board agent 세트 종료 → API key 마지막 사용 시각 flush → 열린 inbox DB 닫기·`inbox.lock` 해제 → 기존 정지.
- `wb hub status`: `web: listening https://10.8.0.2:8780`(여러 주소면 URL을 공백으로 이어서, `allow_from`이 있으면 다음 줄에 `allow_from: <목록> (목록 밖 주소는 403)`) / `web: disabled — 웹은 꺼져 있습니다. <hub.toml 경로>에서 '[web] enabled = true'로 설정하고 'wb hub reload'로 반영하세요.`(2026-10-27) / `web: not_listening (bind_failed:EADDRINUSE 127.0.0.2:8780)` / `web: not_listening (password_not_set) — 'wb hub passwd'로 비밀번호를 정하세요` / 평문 경고 줄. 웹이 꺼져 있을 때 `wb hub passwd`는 비밀번호를 정한 뒤 같은 안내를 한 줄 더 낸다. `board_agent:` 줄(아래).

### 인증·세션·CSRF(D-11, P2-AC-17, ADR-0003)

비밀번호:
- `auth.json` = `{schema_version:1, kdf:"scrypt", n:32768, r:8, p:1, dklen:32, salt:<b64 16B>, hash:<b64>, generation:<int>, changed_at}`. `hashlib.scrypt(..., maxmem=64 MiB)`, 비교는 `hmac.compare_digest`. 길이 10~1024자.
- `wb hub passwd`: TTY에서 `getpass` 두 번(같아야 함). `--stdin`이면 stdin 한 줄(테스트·스크립트용, echo 없음). Hub가 없어도 동작하고 Hub를 자동 시작하지 않는다. 쓸 때마다 `generation += 1` → 기존 세션 전부 무효(P2-AC-17 "잊으면 `wb hub passwd`").
- 웹 비밀번호 변경(`POST /settings/password`): 현재 비밀번호·새 비밀번호 두 번. 현재가 틀리면 로그인 실패와 같은 지연·감사. 성공하면 `generation += 1`, 현재 세션만 새 generation으로 다시 발급하고 나머지 세션을 지운다.

세션:
- `hub.sqlite3.sessions(token_hash PK, created_at, expires_at, password_generation, csrf_token, client_addr, user_agent)`. Token은 32 B random(base64url), DB에는 SHA-256만 둔다.
- Cookie `wb_hub_session`: `HttpOnly; SameSite=Strict; Path=/; Max-Age=86400`, TLS면 `Secure`. 만료는 로그인 시각 + 24 h 절대 시각(연장 없음). 요청마다 `expires_at`·`password_generation == auth.generation`을 확인한다. 실패면 page는 `303 /login`, API는 401.
- 로그인한 세션은 Board agent 세트에 재인증 없이 붙는다(P2-AC-17).

CSRF·출처:
- 세션마다 `csrf_token`(32 B). 모든 상태 변경(POST/PUT/PATCH/DELETE)은 form field `csrf` 또는 header `X-CSRF-Token`이 세션 값과 같아야 한다(아니면 403 `csrf_failed`, 아무것도 바꾸지 않음). WebSocket은 hello의 `csrf`로 확인한다.
- `Origin`이 있으면 기대 origin(`<scheme>://<Host>`)과 같아야 한다. `Origin`이 없고 `Referer`가 있으면 그 origin이 같아야 한다. 아니면 403.
- Client IP 검사(`allow_from`, 2026-10-27, 모든 route의 가장 바깥 middleware): `allow_from`이 있으면 TCP peer 주소(transport `peername`)만 본다. `X-Forwarded-For`·`Forwarded`·`X-Real-IP` 등 header는 믿지 않는다. IPv4-mapped IPv6 peer(`::ffff:a.b.c.d`)는 IPv4로 바꿔 비교한다. 읽을 수 없는 peer는 거절한다. 거절은 본문 없는 403이고 Host·Origin·세션 조회·CSRF·로그인 throttle·handler보다 먼저 일어나며 page, `/static`, 외부 `/mcp`, `/ws/board` upgrade, 없는 경로 모두 같다(`Set-Cookie` 없음). Board agent의 내부 MCP listener(127.0.0.1)는 이 검사 대상이 아니다. [2026-10-28 개정: 외부 `/mcp`는 없고 같은 검사가 `/api/v1/`·`/skill/`에 적용된다. Board agent 내부 listener는 REST만 제공하며 이 검사 대상이 아니다(web-board-ux WBX-D06·D11)]
- Host 검사(DNS rebinding 방어, 모든 route): `Host`는 `<bind IP>:<port>`(bind 주소 중 아무것), `server_names`의 `<name>:<port>`, bind 주소 중 loopback이 있으면 `localhost:<port>`만 받는다. bind 주소 중 `0.0.0.0`/`::`가 있으면 IP literal Host 전부와 `server_names`를 받는다. 아니면 421.
- 거절 기록(2026-10-27 사용자 요구): Host 거절(421), `allow_from` 거절(403), 로그인 form token 불일치, 세션 CSRF 불일치는 `hub.log`(stderr)에 한 줄을 남긴다. 예: `[workbench] web: refused Host 'dgx-spark:8780' on 100.116.175.100:8780 (421 misdirected_request); allowed: bind IP, server_names, localhost on loopback bind`, `[workbench] web: refused client 10.1.2.3 on 127.0.0.1:8780 (403 allow_from); not in [web] allow_from`. Host 값은 출력 가능한 ASCII만(나머지는 `?`), 100자까지, 따옴표로 감싼다. token·비밀번호 값은 남기지 않는다. 같은 (이유, 값)은 60 s에 한 줄, 전체는 60 s에 20줄까지이고 나머지는 세기만 해서 다음 줄에 `(+N more refusals not logged: rate limit)`로 붙인다. 기억하는 key는 그 window에 쓴 줄 수(최대 20개)를 넘지 않는다(`web/refusals.py`).
- 오래된 form(2026-10-27 사용자 요구): 로그인 form의 double-submit token이 맞지 않으면(cookie 없음·불일치) 맨 `csrf_failed` 대신 로그인 화면을 새 token·cookie로 다시 보이고 403과 `로그인 화면이 만료되었습니다. 다시 로그인하세요.`를 보인다. 비밀번호는 확인하지 않고 로그인 실패로 세지 않는다(throttle·감사 없음). 로그인한 page form의 세션 CSRF가 맞지 않으면 403 작은 한국어 page(`화면이 오래되었습니다. 새로 고친 뒤 다시 시도하세요.`, 같은 출처 `Referer`의 path로 가는 링크, 없으면 `/`, reason code `csrf_failed` 표시)를 보인다. API route는 지금처럼 JSON `{"ok":false,"reason":"csrf_failed"}`다.
- 로그인 token 유지(2026-10-27 hotfix): `GET /login`은 형식이 맞는 `wb_hub_login` cookie(`token_urlsafe(32)`, 43자)가 있으면 그 값을 다시 쓰고 cookie를 같은 값으로 다시 내린다(Max-Age 갱신). 없거나 형식이 틀릴 때만 새 token을 만든다. `/favicon.ico`는 공개 route로 204(`Cache-Control: public, max-age=86400`)이고 redirect·cookie가 없다. 이전에는 browser의 favicon 요청이 `303 /login`으로 가서 새 cookie가 열린 form의 token을 무효로 만들었다.
- 로그인 form은 세션 전 double-submit token(`wb_hub_login` cookie, HttpOnly, SameSite=Strict + hidden field)을 쓴다.

로그인 실패 지연:
- 로그인 검증은 Hub 전체에서 한 번에 하나(asyncio lock)다. 연속 실패 수를 client 주소별·전체로 센다(`hub.sqlite3.login_state`, Hub 재시작에도 유지). 다음 시도의 검증 전 지연 = `max(f(ip), f(global/3))`, `f(n) = 0 (n ≤ 3), min(2^(n-4), 30) s (n > 3)`. 성공하면 그 주소와 전체 수를 0으로. 실패 응답은 `비밀번호가 맞지 않습니다`만 보인다. (2026-10-25 orchestrator 결정, 소유자 lockout 방지) 전체 수에 따른 지연 `f(global/3)`은 loopback client와 최근 30일 안에 로그인에 성공한 주소에는 적용하지 않고 주소별 지연만 적용한다. 지연은 lock 안에서 잠들지 않고 429 + `Retry-After`로 즉시 답한다.

보안 header(모든 응답): `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`(2026-10-25 orchestrator 정정: `no-referrer`면 Fetch 규칙상 같은 출처 form POST도 `Origin: null`이 되어 Origin 검사에 모두 막힌다. Origin `null`은 계속 403), `X-Frame-Options: DENY`, 인증 page·API는 `Cache-Control: no-store`. Inline script·style 없음(예외: `/board` 200 응답의 nonce `<style>`·style 속성, "WebSocket terminal 프로토콜" 절 2026-10-26 사용자 결정). 요청 본문·댓글·제목·key 이름 등 사용자 텍스트는 모두 `html.escape` 뒤 `<pre>`/text node로만 출력한다(Markdown·HTML 해석 없음, P2-AC-31).

### 감사 기록(D-11, P2-AC-18)

`hub.sqlite3.audit(id, at, event, actor, client_addr, detail_json)`. 최근 50,000건을 유지하고 넘으면 오래된 것부터 지운다(요청은 자동 삭제하지 않는다, D-23과 별개).

| event | 남기는 때 | detail |
|---|---|---|
| `login_success`, `login_failure`, `logout` | 로그인 | 지연 초 |
| `password_changed` | 웹·CLI(`source`) | 무효화한 세션 수 |
| `api_key_issued`, `api_key_revoked` | key 관리 | key_id, name, grade, scope, expires_at |
| `terminal_attach`, `terminal_takeover`, `terminal_release`, `terminal_timeout`, `terminal_closed` | WebSocket | epoch, 이전 epoch |
| `terminal_denied` (2026-10-25 추가) | WebSocket hello 실패·`taken_elsewhere`·세션 무효 4401 | epoch, reason(내용 없음) |
| `board_restart`, `board_start`, `board_stop` | Board agent 세트 | channel, pid, 이유 |
| `web_control` | 켜기/끄기·pause·resume·confirm-boot·검토 방식 변경·검토 시작 | project_id, action, 결과 |

Terminal 입력·출력, 비밀번호, key secret, 요청 본문은 남기지 않는다.

### API key(D-12, P2-AC-19·20)

- 형식 `wbk_<key_id 10자 base32>_<secret 43자 base64url>`. `api_keys(key_id PK, name UNIQUE, grade, scope_json, secret_hash, created_at, expires_at, revoked_at, last_used_at, system)`. `secret_hash`는 SHA-256(secret이 256 bit random이라 KDF 불필요). 비교는 상수 시간.
- 등급: `requester`, `editor`. Scope: `{"all": true}` 또는 `{"projects": [project_id…]}`(1개 이상, 등록된 id).
- 발급(웹만): 이름(1~64자, `[A-Za-z0-9 ._-]`), 등급, scope, 만료(없음 또는 1~3650일). 원문은 발급 결과 page에 한 번만 보인다(`no-store`). 이후 어디에도 원문이 없다.
- 확인: `revoked_at`·`expires_at`(만료 시각 이후) 확인 → 실패는 401. `last_used_at`은 메모리에서 모아 10 s마다·정지 때 기록한다(표시가 최대 10 s 늦을 수 있다).
- 폐기: 확인 page 뒤 즉시. 다음 호출부터 401.
- 시스템 key: Board agent용 editor·전체 scope key(`system=1`, 이름 `board-agent`). Board agent를 시작할 때마다 새로 발급하고 이전 것을 폐기한다. 목록에는 "Board agent(시스템)"로 보이고 폐기 버튼이 없다. 이 key의 요청 출처는 `board_agent`다.
- [2026-10-28 개정: key는 외부 요청 API(`/api/v1/`, Bearer)에서 쓴다. 기존 key는 재발급 없이 같은 등급·scope로 동작한다. 시스템 key는 Board agent env `WB_API_KEY`로 전달되고 내부 listener는 이번 시작의 시스템 key만 받는다(WBX-D11·D24). 발급 결과 page는 원문과 함께 `.env` 두 줄, skill README 링크, key 없는 agent용 영어 한 줄 안내를 복사 버튼과 함께 보인다(WBX-D04·D17·D26). [web-board-ux SPEC](../web-board-ux/SPEC.md) "Key 발급 결과의 client 설정 안내"]
- Rate limit 없음.

### Principal과 권한(D-09, D-12, D-31, D-34, P2-AC-13·20·42, orchestrator 지시)

Principal: `owner`(웹 세션), `board_agent`(시스템 key), `editor`(일반 editor key), `requester`(requester key), `manager`(검증된 backend 연결, 그 프로젝트만), `local`(같은 uid의 hub_v1 CLI 연결). Board agent와 다른 editor key의 요청 권한은 같다(출처 표시만 다르다).

| 동작 | owner | board_agent / editor | requester | manager | local |
|---|---|---|---|---|---|
| 프로젝트 목록(상태 필드 포함) | 예 | scope 안 | scope 안 id·이름만 | — | `list`(기존) |
| 요청 제출 | 예 | scope 안 | scope 안 | — | — |
| 조회·목록·검색 | 전부 | scope 안 전부 | 자기 요청만 | 그 프로젝트 | — |
| 편집 | 예 | scope 안 | 아니오 | 아니오 | — |
| 댓글(답변 포함) | 예 | scope 안 | 자기 요청 | `progress` 댓글 | — |
| NEW↔NEEDS_INFO, 닫힌 요청 다시 열기 | 예 | scope 안 | 아니오 | — | — |
| 취소(실행 전) | 예 | scope 안 | 자기 요청 | — | — |
| 취소(READY·RUNNING) | 확인 page | 2단계 token | 자기 요청, 2단계 token | — | — |
| 검토 시작(수동) | 예 | 아니오 | 아니오 | `begin_review` | — |
| 검토 방식 변경 | 예 | 아니오 | 아니오 | — | 예(`wb hub review-mode`) |
| 질문(NEEDS_INFO)·거절·승인 명세 연결·READY/RUNNING·CLOSED(success/failed) | 아니오 | 아니오 | 아니오 | 예 | — |
| 결과 요약·전체 보기 | 예 | scope 안 | 자기 요청 | — | — |
| Board agent 세트·terminal | 예 | 아니오 | 아니오 | — | — |
| backend 켜기/끄기·pause·resume·`confirm-boot` | 확인 page | 아니오 | 아니오 | — | (TUI·CLI 기존) |
| API key·비밀번호·감사 조회 | 예 | 아니오 | 아니오 | — | `passwd` |

볼 수 없는 요청은 `not_found`로 답한다(존재를 드러내지 않는다). 볼 수 있지만 허용되지 않은 동작은 `forbidden`이다. Owner도 manager 전용 전이는 할 수 없다(불변식 1).

### 요청 저장소 `.wb/inbox.sqlite3`(D-13·14, ADR-0005, 기술 보완)

Schema v1(모든 시각은 epoch 초 REAL, JSON은 TEXT):

```
meta(key PRIMARY KEY, value)            -- schema_version=1, project_id, review_mode('auto'|'manual'), created_at
requests(request_id TEXT PRIMARY KEY,   -- canonical UUID4
  seq INTEGER UNIQUE NOT NULL,          -- 프로젝트 안 접수 순서, 표시 "#<seq>"
  created_at, updated_at, version INTEGER NOT NULL,      -- 모든 변경마다 +1(편집 base_version. 취소 확인 token은 version이 아니라 fingerprint, "취소")
  origin_kind TEXT,                     -- owner | board_agent | api_key
  origin_key_id TEXT, origin_name TEXT, -- api_key일 때 key_id·이름(이름은 접수 시점 값)
  idem_key TEXT, content_hash TEXT,     -- UNIQUE(origin_kind, origin_key_id, idem_key)
  title TEXT, body TEXT, revision INTEGER NOT NULL,      -- 현재 요청 revision(1부터)
  state TEXT,                           -- NEW|REVIEWING|NEEDS_INFO|READY|RUNNING|CLOSED
  outcome TEXT,                         -- CLOSED일 때 success|failed|rejected|cancelled
  review_round INTEGER, delivery_state TEXT, delivery_id TEXT, delivery_backend TEXT, delivery_at,
  task_id TEXT, spec_revision INTEGER, task_view TEXT,   -- 마지막 동기화한 Task 요약(≤ 256 KiB)
  cancel_state TEXT,                    -- null | requested | stop_requested
  result_summary TEXT)
request_revisions(request_id, revision, title, body, created_at, author_kind, author_name, PRIMARY KEY(request_id, revision))
comments(comment_id PRIMARY KEY, request_id, seq, created_at, author_kind, author_name, kind, body)
  -- kind: comment | answer | question | progress | decision | system
links(request_id, task_id, spec_revision, linked_at, state)    -- 연결 이력(void 포함)
events(event_id INTEGER PRIMARY KEY, request_id, at, actor_kind, actor_name, type, from_state, to_state, detail)
```

- Hub만 연다. Backend·Board agent·CLI는 이 파일을 열지 않는다(ADR-0005, TI-T03). Hub는 첫 쓰기 때 파일을 만든다(읽기만 할 때 없으면 빈 결과, 파일을 만들지 않는다). `.wb`가 사용자 소유·symlink 아님을 확인한다. `PRAGMA journal_mode=WAL; synchronous=NORMAL; foreign_keys=ON; busy_timeout=2000`.
- `inbox.lock`(flock): Hub가 그 프로젝트 DB를 여는 동안 잡는다. 다른 프로세스(다른 HOME의 Hub 등)가 잡고 있으면 그 프로젝트 inbox는 `inbox_busy`로 거절한다. 60 s 동안 쓰지 않은 DB는 닫고 lock을 푼다.
- `meta.project_id`가 `.wb/project.json`의 id와 다르면 `inbox_mismatch`로 거절하고 파일을 고치지 않는다. Registry 항목이 `mismatch`면 그 프로젝트 inbox 동작은 `project_unavailable`이다.
- Backend가 실행 중이어도 Hub가 쓴다: backend는 이 파일을 열지 않으므로 SQLite 경합은 Hub 안의 직렬화뿐이다.
- **직렬화(D-35, P2-AC-43)**: 프로젝트마다 단일 thread executor 하나로 모든 변경을 처리한다. 웹·MCP·hub_v1(manager) 호출은 loop에서 도착한 순서대로 그 executor에 넣는다. 각 변경은 `BEGIN IMMEDIATE` → 현재 상태 읽기 → 전이 검사 → 쓰기 → `COMMIT`이다. 앞선 변경으로 무효가 된 변경은 `invalid_transition`(현재 state·version과 이유)으로 거절하고 아무것도 쓰지 않는다. 부수 효과(backend 통지)는 commit 뒤에만 하고, 통지 실패는 commit을 되돌리지 않는다(BRIEF "저장 후 통지 실패로 접수 성공을 뒤집지 않는다").
- 요청 ID로 프로젝트를 찾는 index는 Hub 메모리에 둔다(시작 때와 등록 변경 때 각 inbox를 읽어 만든다).
- 자동 삭제 없음(D-23). `wb update` backup은 `.wb` 전체를 복사하므로 inbox도 포함된다(Hub를 먼저 멈춰 lock이 풀린 뒤라 committed image가 일관된다, Q-11).

### 요청 상태 기계(D-13·15·16·18·19·20·34·35, orchestrator 지시)

| 현재 | 다음 | 주체 | 계기 |
|---|---|---|---|
| — | NEW | owner·editor·board_agent·requester | 제출 |
| NEW | REVIEWING | Hub | manager 통지 `delivered` 확인(자동 또는 웹 "검토 시작") |
| NEW | REVIEWING | manager | `workbench_inbox begin_review`(TUI에서 사용자 지시, D-31). Manager의 다른 검토 동작도 NEW면 먼저 이 전이를 한다 |
| NEW ↔ NEEDS_INFO | | owner·editor·board_agent | 상태 지정(D-34) |
| REVIEWING, RUNNING | NEEDS_INFO | manager | `ask`(질문 댓글). RUNNING에서 오면 Task 연결을 유지한다(P2-AC-27) |
| NEEDS_INFO | REVIEWING | owner·editor·board_agent·원 요청 requester | manager 아닌 주체의 댓글(=답변, kind `answer`). 새 review round |
| REVIEWING, NEEDS_INFO | READY / RUNNING | manager(backend) | `to_worker`의 `request_id` 연결 commit. Task가 아직 시작 전이면 READY, 이미 실행 중(후속 명세)이면 RUNNING |
| READY | RUNNING | Hub | Task 동기화에서 시작 관측 |
| REVIEWING, NEEDS_INFO | CLOSED(rejected) | manager | `reject` |
| READY, RUNNING | CLOSED(success/failed) | manager | `close`(최종 보고 요약 필수) |
| READY | REVIEWING | Hub | 연결 무효(`inbox_link_unconfirmed`로 닫힌 Task, 아래) |
| NEW, REVIEWING, NEEDS_INFO | CLOSED(cancelled) | owner·editor·board_agent·원 requester | 취소(즉시) |
| READY, RUNNING | (cancel_state=requested) → CLOSED(cancelled) | 같은 주체, 확인 뒤 | backend Task 취소 수락 → Hub가 Task `closed`(사용자 취소) 관측 |
| CLOSED(*) | NEW | owner·editor·board_agent | 다시 열기. 현재 Task 연결은 `links` 이력으로 옮기고 비운다 |

- READY·RUNNING에서 연결 Task가 사용자 취소가 아닌 이유로 닫히면(manager 취소, 새 Task로 대체) 요청 state는 그대로 두고 `task_view.closed`를 보이며 manager에게 `linked_task_closed` 통지를 한 번 보낸다. 닫기·재연결은 manager가 정한다(P2-AC-09: command·Task 종료만으로 성공 종료하지 않는다).
- 편집: NEW·NEEDS_INFO는 그 자리에서 바꾼다(이전 내용은 `events.detail`에 보존). **REVIEWING은 그 자리에서 바꾸고 새 review round를 시작한다**(자동이면 `updated` 통지, 수동이면 "검토 시작" 대기). READY·RUNNING은 새 `request_revisions` 행(revision+1)을 만들고 검토 방식과 무관하게 manager에게 `revised` 통지를 한다. 실행 중 Task·명세는 바꾸지 않는다(불변식 5). CLOSED는 편집 거절(다시 열기 먼저). 편집은 `base_version`이 현재 `version`과 달라야 하지 않는다(다르면 `conflict`).
- Review round와 전달: `delivery_state ∈ {none, pending, offered, delivered, unknown, rejected}`. 새 round(NEW 접수, 답변, REVIEWING 편집)는 `pending`이다. 자동이면 Hub dispatcher가 보내고, 수동이면 "검토 시작"이 보낸다.
- 출처는 항상 `origin_kind`(+key 이름)로 보인다(D-17, P2-AC-28): `소유자` / `Board agent` / `key:<이름>`.

### 입력 한도(D-36, 기술 보완)

| 항목 | 한도 | 넘으면 |
|---|---|---|
| 제목 | 1~200 Unicode code point, 제어 문자 없음 | `too_large`/`invalid_input`, 아무것도 저장 안 함 |
| 본문 | ≤ 64 KiB(UTF-8 byte), 빈 문자열 허용 안 함 | 같음 |
| 댓글 | 1 B ~ 16 KiB | 같음 |
| `result_summary`(manager `close`) | ≤ 16 KiB | 같음 |
| `idempotency_key` | 1~128자 `[A-Za-z0-9._:-]` | `invalid_input` |
| 검색어 | ≤ 200자 | `invalid_input` |
| API key 이름 | 1~64자 | `invalid_input` |
| HTTP 요청 body(`/mcp`, form) [2026-10-28 개정: `/mcp` → `/api/v1/`] | ≤ 256 KiB | 413 |
| WebSocket 입력 frame | ≤ 64 KiB | 그 frame 버림 + `error` frame |
| 목록 `limit` | 1~100(기본 50) | `invalid_input` |
| 전체 보기 page | `length` 1~131072 B | `invalid_input` |

### MCP Streamable HTTP endpoint(D-29, D-36, P2-AC-45, 기술 보완)

[2026-10-28 개정: 이 절은 이력이다. `/mcp`와 `mcp.py`는 WBX-B8에서 지웠다. 현행은 [web-board-ux SPEC](../web-board-ux/SPEC.md) "외부 요청 API `/api/v1/`"(endpoint 표, 오류 → HTTP status, 두 단계 취소 token)이다. 아래 tool 표의 결과 field는 REST 응답에 그대로 쓰인다. 다른 점: `GET …/result`의 `run_id`·`artifact_path`는 `full=true`일 때만 받고 없으면 400 `invalid_input`이다(web-board-ux BRIEF WBX-D38).]

Transport(`mcp` SDK 없이 직접 구현, `src/workbench/hub/mcp.py`):
- `POST /mcp`: body는 JSON-RPC 2.0 객체 하나(배열 batch는 `-32600`). `Accept`에 `application/json`이 있어야 한다. 응답은 `application/json` 단일 응답(SSE stream 없음). Notification(`id` 없음)은 `202` 빈 body. `GET /mcp`는 `405`(server 시작 stream 없음), `DELETE /mcp`는 session 종료 `204`.
- 인증: 모든 요청(`initialize` 포함)에 `Authorization: Bearer wbk_…`. 없거나 틀리거나 폐기·만료면 HTTP 401 + `WWW-Authenticate: Bearer realm="workbench-hub"` + JSON-RPC error `-32001 unauthorized`. 세션 cookie는 `/mcp`에서 인정하지 않는다(브라우저 CSRF 차단).
- `Origin` header가 있으면 listener 자기 origin이어야 한다(아니면 403). Host 검사는 웹과 같다.
- `initialize` → `{protocolVersion, capabilities:{tools:{listChanged:false}}, serverInfo:{name:"workbench-hub", version:<release>}}` + response header `Mcp-Session-Id`(random). 지원 version: `2025-06-18`, `2025-03-26`. Client가 다른 값을 주면 `2025-06-18`을 답한다. 이후 `Mcp-Session-Id`가 있으면 Hub가 발급한 살아 있는 값이어야 한다(아니면 404). 없어도 받는다(client 호환성 관대). `MCP-Protocol-Version` header가 있으면 지원 값이어야 한다(아니면 400).
- Method: `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`. 그 밖은 `-32601`.
- `tools/list`는 key 등급에 맞는 tool만 보인다(requester는 `inbox_update`·`inbox_set_state` 없음). 각 tool은 `inputSchema`(JSON Schema, `additionalProperties:false`)를 가진다.
- `tools/call` 성공: `{content:[{type:"text", text:<JSON 문자열>}], structuredContent:<객체>, isError:false}`. Tool 오류: `isError:true`, `structuredContent:{error:{code, detail, state?, version?}}`. 모르는 tool 이름은 JSON-RPC `-32602`.
- 오류 code: `invalid_input`, `too_large`, `not_found`, `forbidden`, `invalid_transition`, `conflict`, `idempotency_conflict`, `confirm_required`, `token_mismatch`, `project_unavailable`, `inbox_busy`, `inbox_mismatch`, `backend_not_running`, `backend_unreachable`, `hub_stopping`, `internal_error`.

Tool(이름·인자·결과):

| tool | 인자 | 결과 | 등급 |
|---|---|---|---|
| `inbox_projects` | `query?`(이름·경로 부분 일치) | `{projects:[{project_id, name, root, power, power_label, task, command, waits, pending_requests, review_mode}]}`. requester는 `{project_id, name}`만 | 둘 다 |
| `inbox_submit` | `project_id`, `title`, `body`, `idempotency_key` | `{request_id, seq, state:"NEW", origin, created:true\|false}` 즉시(완료를 기다리지 않음, P2-AC-02). 같은 출처·key·내용이면 기존 것(`created:false`), 내용이 다르면 `idempotency_conflict`(P2-AC-03) | 둘 다 |
| `inbox_list` | `project_id?`, `states?`, `include_closed`(기본 false), `query?`, `limit?`, `cursor?` | `{requests:[요약], next_cursor}`. 요약 = `{request_id, project_id, seq, title, state, outcome, origin, created_at, updated_at, task_id}` | 둘 다(requester는 자기 것) |
| `inbox_get` | `request_id` | `{request:{…요약, body, revision, revisions:[…], comments:[…], task:{task_id, spec_revision, view}, result_summary, cancel_state, delivery_state, version}}` | 둘 다 |
| `inbox_comment` | `request_id`, `body` | `{comment_id, state, version}`. NEEDS_INFO면 답변이 되어 REVIEWING | 둘 다 |
| `inbox_update` | `request_id`, `base_version`, `title?`, `body?` | `{state, version, revision, mode:"in_place"\|"new_revision"}` | editor |
| `inbox_set_state` | `request_id`, `base_version`, `to:"NEW"\|"NEEDS_INFO"`, `reason?` | `{state, version}`. CLOSED → NEW는 다시 열기 | editor |
| `inbox_cancel` | `request_id`, `confirm_token?`, `reason?` | 실행 전: `{status:"cancelled", state:"CLOSED"}`. READY: `{status:"cancel_requested"}`(token 없음). 웹은 READY·RUNNING 모두 확인 page와 token을 거친다(2026-10-25 orchestrator 결정: 웹은 사람이 누르는 경로라 확인을 두고, MCP READY는 바로 요청한다). RUNNING 첫 호출: `{status:"confirm_required", confirm_token, expires_in:60, impact:{task_id, task_status, command:{command_id, label, state}\|null}}`. Token 확인: `{status:"cancel_requested", stop:{status:"stopping"}\|null}`. 취소 수락은 종료가 아니다(P2-AC-30) | 둘 다 |
| `inbox_result` | `request_id`, `full`(기본 false), `run_id?`, `offset?`, `length?`, `artifact_path?` | 기본: `{summary, state, outcome, task:{status, runs:[{run_id, status, exit_code}]}}`. `full`: 아래 "결과 조회" | 둘 다 |

Board agent는 별도 내부 listener(아래)의 같은 `/mcp`를 쓴다. 외부 listener가 없어도(TLS 문제 등) Board agent의 MCP는 동작한다. [2026-10-28 개정: 내부 listener는 `/api/v1/` REST만 제공한다. 외부 listener가 없어도 Board agent의 REST는 동작한다(WBX-D11)]

### 웹 route와 화면(D-09, D-21, D-23, D-24, 기술 보완)

모든 page는 서버가 그린 HTML이다. 상태 변경은 POST → `303` 재조회(PRG). "확인 창"은 **서버가 그린 확인 page**다: 영향(끌 때의 진행 중 작업 목록 등)과 `확인`(POST, token 포함)·`취소`(돌아가기 link, 아무것도 바꾸지 않음)를 보인다(P2-AC-21). JavaScript는 board page와 badge 갱신에만 쓴다(`/static/*.js`, inline 없음).

| route | 내용 |
|---|---|
| `GET/POST /login`, `POST /logout` | 로그인(비밀번호만) |
| `GET /` | 상태 board: 등록된 프로젝트 표(이름, 경로, 상태, Task·단계, 명령, 대기 사유, 요청 수, 마지막 활동, 검토 방식). label은 `workbench.hub.presentation` 공용. 10 s meta refresh. 평문 경고 banner | [2026-10-28 개정: `GET /`는 세션이 있으면 `303 /dashboard`. Dashboard는 `GET /dashboard`(요약 card·프로젝트 표·최근 요청·Board card, meta refresh 없이 `GET /api/dashboard` JSON으로 10 s 갱신). 전역 nav, 접히는 평문 HTTP 배너. web-board-ux WBX-D01·D03·D13·D29]
| `GET /projects/<project_id>` | 프로젝트 상태, 제어 버튼(상태에 맞는 것만), 검토 방식 설정 form, 그 프로젝트 요청 목록 link |
| `GET /projects/<pid>/<action>` → `POST …/<action>` | `power-on`, `power-off`, `pause`, `resume`, `confirm-boot`의 확인 page와 실행. `power-off` 확인 page는 `shutdown_request`의 `active`를 보이고 token을 hidden으로 싣는다. `shutdown_token_mismatch`면 "작업이 바뀌었습니다"와 새 목록의 확인 page를 다시 보인다(HB-AC-09) |
| `POST /projects/<pid>/review-mode` | `auto`/`manual`(D-32) |
| `GET /requests` | 목록: filter(project, state, 보관함=CLOSED 포함, 검색어). 기본은 CLOSED 숨김(D-23). 열: #seq, 프로젝트, 제목, 상태, 출처, 갱신 |
| `GET /requests/new`, `POST /requests` | 작성 form(프로젝트 선택, 제목, 본문). 출처 "소유자", NEW(P2-AC-38). Hidden nonce가 idempotency key(이중 제출 방지) |
| `GET /requests/<rid>` | 상세: 출처, 상태, revision 이력, 댓글 thread(질문·답변·진행·결정 구분), 연결 Task(id·명세 revision·진행·run 보고 요약), 결과 요약, 버튼(편집, 상태 지정, 다시 열기, 취소, 검토 시작(수동·`pending`일 때), 전체 보기) |
| `GET/POST /requests/<rid>/edit` | 편집(`base_version` hidden) |
| `POST /requests/<rid>/comments` | 댓글/답변 |
| `POST /requests/<rid>/state` | NEW↔NEEDS_INFO, 다시 열기 |
| `GET/POST /requests/<rid>/cancel` | 확인 page(RUNNING이면 영향 요약과 token) → 실행. 연결된 Task가 있으면 token에 묶인 취소 확인 fingerprint로 비교하고("취소", 2026-10-26 사용자 결정), 없으면 hidden `base_version`으로 비교한다 |
| `POST /requests/<rid>/start-review` | 수동 검토 시작(D-31, P2-AC-39) |
| `GET /requests/<rid>/full` | 전체 보기(run 목록, 보고 원문, 명령 목록, raw page link, 산출물 경로) |
| `GET /requests/<rid>/raw?run_id=&offset=&length=` / `…/artifact?path=&offset=&length=` | 원문 page(`text/plain; charset=utf-8`, `Content-Disposition: inline`) |
| `GET /board`, `POST /board/restart`, `GET /ws/board` | Board agent 세트 화면, 재시작(확인 page), WebSocket |
| `GET /settings`, `POST /settings/password` | 비밀번호 변경(현재 비밀번호 필요) |
| `GET/POST /settings/keys`, `GET/POST /settings/keys/<key_id>/revoke` | key 목록(이름·scope·등급·마지막 사용·만료·상태)·발급·폐기 |
| `GET /settings/audit` | 감사 기록(최근 순, page) |
| `GET /api/badge` | `{pending:<NEW 수>, needs_info:<NEEDS_INFO 수>}`(scope 전체). `badge.js`가 30 s마다 받아 tab 제목 `(<pending>·<needs_info>) WB Hub`와 header badge를 바꾼다(D-24) |
| `/static/…` | 인증 없이 제공(비밀 없음). `Cache-Control: public, max-age=3600` |
| `POST/GET/DELETE /mcp` | 위 MCP. [2026-10-28 개정: 제거(WBX-B8). 대신 `/api/v1/…`(REST, Bearer 전용, 세션 cookie 무시)와 `GET /skill/README.md`, `/skill/wb-inbox/{SKILL.md,wb_inbox.py,.env.example}`(로그인 없음, `allow_from`·bind 적용). Cache 정책은 로그인한 page `private, no-cache`(key 발급 결과·raw/full/artifact는 `no-store`). 정본은 web-board-ux SPEC s3] |

로그인하지 않은 page 요청은 `303 /login`, API·`/ws`는 401. 웹에는 manager·worker·host pane 입력 수단이 없다(D-09).

### 프로젝트 제어 실행(Hub 쪽, D-09, Hub SPEC (A) 확장 6)

- `POST /projects/<pid>/<action>`은 HB-07의 `workbench.hub.projects.{power_on(attach=False), power_off, pause, resume, confirm_boot}`를 executor에서 부른다. `power_off`의 `ask` callback은 웹 확인 page의 token 흐름(첫 요청은 목록을 돌려주고 멈춤, 확인 POST가 `shutdown_confirm`)으로 바꾼다. 결과 `verified`가 아니면 성공으로 보이지 않는다(C-AC-22).
- Hub에서 켠 backend는 Hub 환경(`hub_environment`)을 물려받는다. **systemd 모드 문제**: unit에 `KillMode`가 없어 기본 `control-group`이면, Hub(main process)가 끝날 때 systemd가 같은 cgroup의 backend·Board agent까지 끝낸다(HB-D04 위반). `wb hub enable`의 unit에 `KillMode=process`를 더하고, `enable` 시점의 `PATH`, `SHELL`, `LANG`, `LC_ALL`(있으면)을 `Environment=`로 싣는다. 이미 만든 unit은 다음 `enable`이 다시 쓴다. `wb hub status`는 unit이 옛 형식이면 `wb hub enable`을 다시 실행하라고 안내한다.
- API key principal은 이 경로에 닿지 않는다(route가 세션만 받는다, P2-AC-20).

### Hub ↔ backend 요청 통로(D-15·16·31, HB-O01 확장, 기술 보완)

**Backend → Hub (hub_v1, backend principal)**:
- Backend는 hub_v1 socket에 hello `{"v":1,"type":"hello","client":"workbench-backend","project_id":<id>}`로 접속한다. Hub는 `SO_PEERCRED` pid·uid를 읽고, registry에서 그 `project_id` 항목을 찾아 `inspect_backend` 신원(pid·start_ticks)이 peer pid·`start_ticks(peer pid)`와 같을 때만 principal `manager(project_id)`를 준다. 아니면 `reject`/`principal_mismatch`(TI-T04). 이 확인은 같은 uid 안의 혼동 방지이며 보안 경계는 0600 socket과 uid다.
- Backend는 연결을 하나 유지하고 끊기면 5 s마다 다시 붙는다. Hub가 없어도 backend·core는 영향이 없다(P2-AC-01). Backend는 Hub를 자동 시작하지 않는다.
- 새 op(backend principal 전용): `inbox_list{states?, limit?}`, `inbox_show{request_id}`, `inbox_review{request_id, action:"begin_review"|"ask"|"comment"|"reject"|"close", body?, outcome?, summary?}`, `inbox_link_check{request_id}` → `{link_token, expires_in:30}`, `inbox_link_commit{link_token, task_id, spec_revision, task_status}` → `{state:"READY"|"RUNNING"}`, `inbox_delivery{request_id, delivery_id, outcome}`. 모든 결과는 hub_v1 `result`/`failure`이고 실패 reason은 MCP 오류 code와 같은 이름을 쓴다.
- Local principal 새 op: `review_mode{root, set?}`(`wb hub review-mode`), `reload{}`(`wb hub reload`, 2026-10-27, 위 "`wb hub reload`"). 예약 op `power_on`·`power_off_*`·`pause`·`resume`·`confirm_boot`는 hub_v1 socket에서 계속 `unsupported_type`이다(웹은 같은 process 안에서 라이브러리를 부른다. 이 socket으로 열 소비자가 없다).

**Hub → backend (ui_v1, attach 없음, client `workbench-hub`)**: HB-06의 `NON_ATTACHED_CONTROL_CLIENTS` 규칙(이름 + server 쪽 peer uid == backend euid)을 다음 새 request type에도 적용한다. 다른 client나 다른 uid는 `not_attached`. ui_v1 `VERSION`은 그대로(추가). 옛 backend는 `unsupported_type`/`not_attached` → Hub는 `backend_too_old`로 보인다.
- `inbox_notice{request_id, delivery_id, event, fields}` → `{queued:true, notice_id}`. Backend는 Watchdog notice queue에 `inbox_request` type으로 넣는다(bridge `NOTICE_TYPES.manager`에 추가. recovery skill 문서 대상인 `MANAGER_NOTICE_TYPES`에는 넣지 않고 별도 상수 `INBOX_NOTICE_TYPE`로 둔다, 2026-10-25 TI-10 리뷰 판단). `event ∈ {new, answered, updated, revised, cancelled, linked_task_closed}`. `fields` = `{request_id, seq, title, origin, state, excerpt(≤1000자), hint}`. 본문 전체는 넣지 않는다(manager는 `workbench_inbox show`로 읽는다).
- `task_status{task_ids:[…], detail:false|true}` → 각 `{task_id, exists, status, closed_reason, revision, cancel_requested_by, runs:[{run_id, status, exit_code, started_at, ended_at, stopped_by, log_sources:[{source_kind, source_id}]}], commands:[{command_id, owner, exit_code, log_path}], reports:[{message_id, kind, at, text}](detail일 때만, 보고당 ≤ 64 KiB)}`. 읽기 전용, 모델 호출 없음(P2-AC-06).
- `cancel_task{task_id, request_id, expect:{status, command_id|null}, reason}` → 아래 "취소".
- Raw 원문은 기존 `raw_query`·`raw_status`(이미 attach 없이 받는다)를 쓴다.

**Notice 전달 결과**: Watchdog `_Queued`에 최종 결과 callback(`delivered`·`unknown`·`rejected`·`dropped`)을 더한다. Backend는 그 결과를 hub_v1 `inbox_delivery`로 Hub에 알린다(Hub가 없으면 메모리에 두고 다시 붙을 때 보낸다). 전달을 미루는 동안 backend snapshot에 additive `inbox:{notices:[{request_id, event, queued_at, attempts, wait}]}`를 싣는다. `wait`: 미룬 시도 직후 manager probe가 `editorKnown && !editorEmpty`면 `manager_editor_not_empty`, manager 세션 없음이면 `manager_session_not_connected`, pause면 `paused`, 그 밖의 바쁨은 `manager_busy`.

**Hub dispatcher**: 2 s마다(그리고 관련 commit 직후) 켜진(`on`) 프로젝트마다 실행한다.
- 프로젝트당 `offered`(결과 미확인) notice는 최대 1개다. 없으면 `delivery_state == pending`이고 보낼 수 있는 요청 중 `seq`가 가장 작은 것을 보낸다(접수 순서, P2-AC-23). 자동 검토 방식이면 NEW·REVIEWING의 pending round를, 수동이면 "검토 시작"으로 표시된 round만 보낸다. `revised`·`cancelled`·`linked_task_closed`는 검토 방식과 무관하게 보낸다. 거절 뒤 backoff 중인 요청은 "보낼 수 있는" 요청에서 빠지므로 뒤 `seq`가 먼저 갈 수 있다(head-of-line blocking 없음, 2026-10-25 orchestrator 결정). 첫 제공은 항상 `seq` 순서다.
- `inbox_notice`가 `queued`면 `delivery_state=offered`, `delivery_backend=<pid:start_ticks>`. `inbox_delivery`가 `delivered`면 NEW → REVIEWING(P2-AC-24), round `delivered`. `unknown`이면 REVIEWING으로 두되 다시 보내지 않고 system 댓글 `manager 전달 여부 확인 불명`(P2-AC-11, 웹 "다시 전달" = 검토 시작 버튼). `rejected`·`dropped`면 `pending`으로 되돌리고 system 댓글.
- Backend 신원(pid·start_ticks)이 바뀌었는데 `offered`면(backend 재시작, 큐는 메모리였다) `pending`으로 되돌려 다시 보낸다. 재시작·연결 상실·지연을 `delivery_state`와 system 댓글로 구분한다(P2-AC-12). 이 경우 같은 요청 통지가 manager에게 두 번 갈 수 있다. 통지는 실행이 아니고, 실행 연결은 link 단계의 상태 검사가 막는다.
- Worker 실행 중에도 통지는 manager가 쉴 때만 간다(bridge 규칙). 실행 중 작업을 선점·중단하지 않는다(P2-AC-10).

**Task 동기화**: 2 s마다 READY·RUNNING 요청이 있는 켜진 프로젝트에 `task_status`를 묻고 `task_view`를 갱신한다. `dispatched`·`starting` → READY, `running`·`waiting_report`·`held`·`cancelling`·`finished`·`blocked` → RUNNING(세부는 `task_view.status`), `closed`: `closed_reason`이 사용자 취소면 CLOSED(cancelled), `inbox_link_unconfirmed`면 READY → REVIEWING(link `void`, system 댓글), 그 밖이면 `linked_task_closed` 통지. 꺼진 프로젝트는 마지막 `task_view`를 "backend 꺼짐"과 함께 보인다.

**Hub 목록 연결**: `list` 행의 `pending_requests`를 `{new, reviewing, needs_info, ready, running}`(inbox를 읽지 못하면 `null`)로 채우고 `review_mode`를 더한다. `waits`에 snapshot `inbox.notices[].wait`의 `manager_editor_not_empty`·`manager_session_not_connected`(이미 있으면 중복 없이)와 새 `manager_busy`(label `manager 작업 중이라 요청 전달 대기`)를 더한다. Presentation `요청` 열은 `대기 N · 질문 M`(0이면 `0`). TUI와 `wb hub list`도 같은 label이다.

### Manager 도구(D-16, D-31, ADR-0004)

`workbench_inbox`(manager 전용 bridge 도구, `omp_bridge/g3/bridge.ts`의 `MANAGER_TOOLS`에 추가, backend `_tool_request`가 처리):
- 인자(strict, 쓰지 않는 필드는 null): `action ∈ {list, show, begin_review, ask, comment, reject, close}`, `request_id`, `body`(ask·comment·reject 이유), `outcome ∈ {success, failed}`(close), `summary`(close, 최종 보고 요약, 필수).
- `list`는 이 프로젝트의 NEW·REVIEWING·NEEDS_INFO·READY·RUNNING 요약, `show`는 요청 전체(본문·댓글·revision·연결)다. 읽기는 모델을 부르지 않는다.
- 결과: Hub 결과 그대로. Hub가 없으면 `{status:"rejected", reason:"inbox_unavailable", detail:"Hub가 실행 중이 아니라 기록하지 않았습니다. 나중에 다시 호출하세요."}`(아무것도 기록 안 됨). 응답이 없으면 `{status:"unknown", detail:"기록 여부 불명. show로 확인한 뒤 필요하면 다시 호출하세요."}`. 자동 재전송은 없다(P2-AC-11, ADR-0005 결과).

`to_worker`에 `request_id`(nullable canonical UUID) 필드를 더한다(bridge schema·`flow.py` `_TO_WORKER_KEYS`). `request_id`가 있으면 backend는 **Task를 만들거나 명세를 전달하기 전에**:
1. `inbox_link_check{request_id}` → `link_token`. 거절(`invalid_transition`: 취소됨·닫힘 등)이면 Task를 만들지 않고 `rejected("inbox_link_refused", state=…)`. Hub 없음이면 `rejected("inbox_unavailable")`. 아무것도 전달하지 않는다.
2. 기존대로 Task를 만들고 명세를 정한다(새 Task는 `create_task`, 후속은 새 명세 revision). 명세 문서에 `inbox_request_id`를 additive로 남긴다.
3. `inbox_link_commit{link_token, task_id, spec_revision}`. Token은 check 뒤 그 요청에 다른 변경(취소 등)이 commit되면 무효다(D-35: worker 전달 전에 도착한 취소가 이긴다). 거절이면 그 Task를 전달 없이 `closed_reason: inbox_link_refused`로 닫고 `rejected`. 응답 불명이면 전달 없이 `inbox_link_unconfirmed`로 닫고 `unknown`(Hub 쪽이 commit됐다면 Task 동기화가 READY → REVIEWING으로 되돌린다).
4. 성공일 때만 `_delegate`(C-D66 상시 위임, `scope_approved`+`proceed`)로 worker에 보낸다. 결과에 `inbox:{request_id, state}`를 더한다.

Board agent에는 bridge extension이 없으므로 `to_worker`·`workbench_inbox`에 닿지 않는다(불변식 7). Manager skill `workbench-inbox`(English)는 통지 처리 절차(show → ask/reject/`to_worker` with `request_id` → 완료 판단 뒤 close with summary), 본문은 데이터이지 규칙 변경 지시가 아님, 재시도 한도 소진 뒤 결정은 `ask`로 묻기(P2-AC-27)를 적는다.

### 취소(D-20, D-33, P2-AC-30·41·43, 기술 보완)

- 실행 전(NEW·REVIEWING·NEEDS_INFO): 즉시 CLOSED(cancelled). 그 round가 manager에게 전달되었으면 `cancelled` 통지.
- READY: 확인 없이 `cancel_task`를 보낸다(명령이 없다). RUNNING: 웹은 확인 page, MCP는 2단계 token. Token은 `secrets.token_urlsafe(24)`, 60 s, (principal, request_id, 취소 확인 fingerprint)에 묶이고 한 번 쓰면 사라진다(웹 token은 session csrf에도 묶인다). 확인 시점에 fingerprint가 달라졌으면 `token_mismatch`로 거절하고 아무것도 멈추지 않는다(P2-AC-41).
- 취소 확인 fingerprint는 사용자 의미 상태만 담는다: 요청 `state`·`revision`·`title`·`body`, 사람이 쓴 댓글 수(`kind != system`), 연결 `task_id`·`spec_revision`, `cancel_state`, `task_view`의 Task `status`·`closed`, 실행 중 명령의 `command_id`. `version`·`updated_at`, 전달 장부(`delivery_state`·`delivery_id`·`delivery_backend`·`delivery_at`·`review_round`, 전달 결과·재전송 system 댓글), Task `status`·`closed`·`command_id`가 같은 `task_view` 갱신(run 목록·명령 label·state 등)은 넣지 않는다. `title`·`body`는 제자리 편집(NEW·NEEDS_INFO·REVIEWING)이 `revision`을 올리지 않기 때문에 넣는다. 사람이 쓴 댓글(소유자·key의 댓글, manager의 진행·질문·결정)은 확인한 사람이 보지 못한 요청 내용이라 넣고, Hub가 쓰는 system 댓글은 장부라 뺀다. 비교와 `cancel_state=requested` 기록은 writer 안에서 한 번에 한다(`InboxService.cancel(base_fingerprint=…)`, 다르면 `conflict`이고 MCP·웹은 `token_mismatch`로 답한다). 연결된 Task가 없는 웹 실행 전 취소(멈추는 것이 없는 상태 편집)는 지금처럼 `base_version`으로 비교한다.
- 2026-10-26 사용자 결정: confirm token은 사용자 의미 상태에 묶는다. 전에는 request `version`에 묶어서 Task 종료 통지의 전달 round(`reserve_delivery`·`mark_offered`·`record_delivery`)나 Task 상태가 같은 view 갱신처럼 장부만 바뀌어도 정직한 확인이 `token_mismatch`로 거절되었다. BRIEF P2-AC-41은 "요청·Task 상태가 바뀌었으면" 거절이라고 적는다. `version`은 편집의 일반 optimistic concurrency 값으로 그대로 둔다.
- Hub는 요청에 `cancel_state=requested`를 commit한 뒤 backend에 `cancel_task{task_id, request_id, expect, reason}`을 보낸다. Backend가 꺼져 있으면 commit 전에 `backend_not_running`으로 거절한다(아무것도 바꾸지 않음. 켠 뒤 다시).
- Backend `cancel_task`(새 `TaskFlow.cancel_by_user`): 현재 Task 상태·실행 중 명령이 `expect`와 다르면 `task_changed`로 거절. 같으면 (1) 그 Task의 Workbench 실행 명령이 있으면 기존 사용자 stop(CS-O10, `requested_by: user`, SIGINT→SIGTERM→SIGKILL, 잔존 확인)을 background로 시작하고, (2) 기존 `_cancel`과 같은 절차(withdraw, `cancelling`, host exit 대기, worker 알림)를 `by: "user"`로 실행한다. 결과 `{cancel:"cancel_requested"|"cancelled", stop:{status:"stopping"}|null}`. Manager에게 `cancelled` 통지가 간다.
- 요청은 Task 동기화가 Task `closed`(사용자 취소)를 관측해야 CLOSED(cancelled)가 된다. 명령 stop이 `unknown`이면 그 사실을 system 댓글로 남기고 성공으로 보이지 않는다(P2-AC-30).
- D-35 경합: 취소 commit이 `inbox_link_commit`보다 먼저 직렬화되면 link token이 무효가 되어 Task는 전달 없이 닫힌다(P2-AC-43). Link commit이 먼저면 요청은 READY이고 이후 취소는 READY 취소 경로다.

### 결과 조회(D-22, P2-AC-31)

- 기본(요약): manager `close`의 `summary`(CLOSED 전이면 `task_view`의 상태와 run 결과 줄).
- 전체 보기(`inbox_result{full:true}`, 웹 `/full`): `task_status{detail:true}`의 run 목록·worker 보고 원문·명령 목록, 각 run의 raw log를 `raw_query`로 `offset`/`length` page 단위 제공. Backend가 꺼져 있으면 마지막 `task_view`만 보이고 raw는 `backend_not_running`.
- 산출물(`artifact_path`): WB 프로젝트 root 아래의 일반 파일만, `resolve()` 뒤에도 root 안(symlink 탈출 거절), `<root>/.wb/**`·`<root>/.git/**` 제외, page ≤ 128 KiB. 요청을 볼 수 있는 principal이면 requester도 된다(D-22의 의도적 완화). 그 밖의 host 파일은 이 경로로 열지 않는다.
- 웹 원문 page는 `text/plain`로만 보낸다(실행되지 않음).

### Board agent 세트(D-25·26·27, ADR-0003, 기술 보완)

- Hub의 `BoardSet`이 두 channel을 소유한다: `agent`(OMP), `shell`(`board_agent.shell` / `$SHELL` / `/bin/sh`, cwd `$HOME`). 각각 `pty.fork`로 새 session leader가 되는 Hub의 자식이다(backend `OmpPane`·`_OwnedSession` 방식 재사용, 어느 backend의 후손도 아님). 환경 = `hub_environment()` + `TERM=xterm-256color`. 출력은 channel마다 1 MiB ring buffer에 두고 attach 때 replay한다.
- 시작: 첫 `/board` 연결 때 시작한다(Hub 시작 때 자동으로 띄우지 않는다. HB-O09: 다음 Hub 시작에서도 복원하지 않고 첫 연결에서 `--continue`로 이전 세션을 잇는다).
- Board agent OMP: home `<hub state>/board-agent/omp-root`(`PI_CONFIG_DIR`는 HOME 기준 상대, agent dir `…/agent`, `agent.db`는 사용자 것을 symlink, `omp_home`의 규칙 재사용, D-25 "같은 OMP 로그인"). argv = `omp --config <omp-isolation.yml> --config <board overlay> --no-extensions --append-system-prompt <role prompt> [--continue] [--model <selector>]`. Workbench bridge extension(`--extension`)은 싣지 않는다.
- Board overlay: isolation의 provider·skill·project 설정 차단을 유지하고, **Hub MCP 서버 하나만** 켠다. 접속 대상은 Hub가 Board agent용으로 여는 내부 listener `http://127.0.0.1:<임의 port>/mcp`(MCP route만, Host `127.0.0.1:<port>`만, 시스템 editor key 필요)다. 메모리·세션은 영속(D-25)이므로 isolation의 `memory`/`memories` 끄기를 overlay가 되돌린다. 정확한 OMP 설정 key·key 전달 방식(header 값 또는 환경 변수 참조)은 TI-15 probe 결과로 정한다. Key가 파일에 들어가야 하면 overlay는 0600, Board agent 시작마다 새 key로 다시 쓴다. [2026-10-28 개정: Board overlay는 MCP를 켜지 않고 provider·skill 차단 위에 `wb-inbox` skill 하나(시작마다 `<hub state>/board-agent/skills/wb-inbox/`에 새로 복사)만 허용한다. Key는 파일이 아니라 env `WB_API_KEY`, 주소는 `WB_HUB_URL=http://127.0.0.1:<내부 port>`. 이전 `mcp.json`은 시작 때 지운다. {WBX} "Board agent 전환"]
- 격리 확인: 시작 직후 launcher의 isolation 관측(`observe_isolation`)과 같은 방법으로 tool 목록을 보고, `mcp__` tool이 Hub 서버 것뿐이 아니면 Board agent를 끝내고 `board_agent.agent.state=isolation_failed`(사유)로 보인다. Manager·worker의 기존 MCP 누출 검사는 바뀌지 않는다(P2-AC-33). [2026-10-28 개정: MCP tool이 하나라도, `wb-inbox` 밖의 skill이 보이거나 `wb-inbox`가 없으면(`skill_missing:wb-inbox`) `isolation_failed`. 격리 확인 OMP는 stdin EOF 뒤 최대 10 s, SIGTERM 3 s, SIGKILL 3 s 순서로 끝낸다. Board OMP log는 `<hub state>/board-agent/omp-logs/`(최근 20개, 파일당 ≤1 MiB, key 가림)와 `board-events.log`에 남는다(web-board-ux BRIEF WBX-D32·D37)]
- 모델·role prompt: `board_agent.model`(빈 값이면 `omp-models.yml`의 manager selector). Role prompt = 기본 `omp_bridge/board/ROLE.md` + `role_prompt_file`(있으면 뒤에 붙임). 기본 prompt 내용(English): Board agent의 역할(요청 찾기·작성·정리, 승인·worker 전달 불가, 모든 요청은 manager 검토를 거침), D-27 안내 두 가지 — (1) GPU를 쓰기 전에 `nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader`와 `inbox_projects`의 실행 중 명령을 확인하고 사용 중이면 "계속할까요?"라고 묻기, (2) `inbox_projects`에서 Task·명령이 진행 중인 프로젝트 root 아래 파일을 고치기 전에 "계속할까요?"라고 묻기. Lock·중재는 하지 않는다.
- 재시작(`POST /board/restart{channel}`, 확인 page): 그 channel의 session을 SIGHUP → SIGTERM → 5 s → SIGKILL로 끝내고 새로 띄운다(agent는 `--continue`). Channel이 스스로 끝나면 `exited`(exit status)로 보이고 다시 시작 버튼을 보인다.
- 정지 연동(orchestrator 결정, Hub SPEC 정지 2단계): `Hub.active()`가 살아 있는 channel이 있으면 `[{"kind":"board_agent_set","channels":{"agent":{"pid":…,"state":"running"},"shell":{…}},"lease":"connected"|"none"}]`를 돌려준다. `wb hub stop`·`wb update`는 이 목록을 보여 확인을 받고, 확인하면 세트를 끝낸 뒤 Hub가 정지한다. 거절하면 아무것도 멈추지 않는다. Channel 상태가 바뀌면 `active`가 달라져 기존 `stop_token_mismatch`가 된다. `SIGTERM` 경로는 확인 없이 세트를 끝낸다. Hub `SIGKILL` 때는 PTY master가 닫혀 SIGHUP을 받는다(그 뒤 남는 프로세스는 보장하지 않음, 문서화).
- 상태: hub_v1 `status.board_agent = {agent:{state, pid, started_at, detail, message?}, shell:{…}, lease:{state:"connected"|"none", epoch, since}} | null`. `wb hub status`: `board_agent: agent running(pid N) · shell running(pid M) · 연결 없음`.
  - `message`(선택, 2026-10-26 보완)는 channel 시작 실패를 사람이 읽을 설명(선택된 경로·원인·고치는 방법)이다. 값이 있을 때만 key가 있고, 다음 시작에서 지워지며, 1024자로 자른다. `detail`은 그대로 안정된 code다. 지금은 Board agent OMP 선택 실패(`detail=omp_unavailable`)만 채운다. `SetupError`의 `explain()`·`fix`로 만들어 문제 디렉터리와 chmod 같은 고치는 방법을 담는다. `wb hub status`는 `board_agent:` 줄 아래에 `    <channel>: <message>`로 보인다. 웹 Board page는 아직 보이지 않는다.
- Board agent 안의 `wb`는 조상이 Hub이므로 관리 tree가 아니다(Hub SPEC 확장 3). `wb init`의 확인은 PTY에서 답한다.

### WebSocket terminal 프로토콜(D-26, D-28, P2-AC-34·35)

`GET /ws/board`(세션 cookie 필수, `Origin`은 기대 origin과 정확히 같아야 함, 아니면 upgrade 전 403. API key로는 열 수 없다). 세트 하나에 lease 하나(현재 연결 하나)다.

Frame:
- Control은 text JSON, 데이터는 binary: `byte0 = channel(0 agent, 1 shell)`, `bytes1..4 = epoch(uint32 BE)`, 나머지 = payload. Server → client 출력도 같은 형식(epoch = 현재).
- Client hello(연결 후 5 s 안, 아니면 4400으로 닫음): `{"t":"hello","v":1,"csrf":…,"resume":{"run":…,"epoch":N}|null,"sizes":{"agent":[rows,cols],"shell":[rows,cols]}}`.
- `resume`이 없거나 `resume.run`이 현재 Hub run이고 `resume.epoch`가 현재 epoch면: epoch += 1, 이전 연결이 있으면 그 연결에 `{"t":"taken_over","epoch":<new>}`을 보내고 4001로 닫는다(즉시 인수, D-28 (1)). 새 연결에는 `{"t":"attached","v":1,"run":…,"epoch":…,"channels":{agent:{state,…},shell:{…}},"heartbeat":10}` 뒤 replay.
- `resume.epoch`가 현재보다 작으면(그 사이 다른 곳이 인수) 인수하지 않고 `{"t":"taken_elsewhere","epoch":<current>}` 뒤 4001. Client는 "다른 곳에서 연결됨 — 다시 가져오기"를 보이고 버튼을 누르면 `resume:null` hello로 강제 인수한다(D-28 (5)).
- 입력: binary frame의 epoch가 현재 epoch이고 그 연결이 현재 lease일 때만 PTY에 쓴다. 아니면 버리고 셈만 한다(D-28 (2)).
- `{"t":"resize","epoch":E,"channel":"agent","rows":r,"cols":c}`(1~1000), `{"t":"ping","epoch":E,"n":k}` → `{"t":"pong","n":k}`, `{"t":"release","epoch":E}` → lease 해제·1000 close(D-28 (4), tab 닫기 `pagehide`에서 best-effort).
- Heartbeat: client는 10 s마다 `ping`. Server는 현재 연결에서 어떤 frame도 20 s(+1 s 여유) 동안 오지 않으면 4002로 닫고 lease를 `none`("연결 없음")으로 둔다(D-28 (3)). TCP 끊김(EOF·RST)은 즉시 해제다. Lease 해제는 epoch를 올리지 않는다(같은 tab의 resume이 된다).
- (2026-10-25 orchestrator 보완) 세션이 무효가 되면(로그아웃·비밀번호 변경·CLI `wb hub passwd`·24 h 만료) 열린 terminal 연결도 4401로 닫는다(약 5 s 안 재확인, 로그아웃·변경 시 즉시). `/ws/board` 연결은 전체 8개, 세션당 hello 전 2개로 제한한다. 다른 Hub run의 `resume`은 이 run에 아직 attach가 없을 때만 attach한다. Client는 4400~4499를 자동 재연결하지 않는다. `/board` 응답만 xterm DOM renderer 때문에 style 예외를 둔다(아래 항목). `script-src`는 그대로다.
- (2026-10-26 사용자 결정) `/board`의 200 응답 CSP는 전역 CSP에서 `style-src`만 `style-src 'self' 'nonce-<N>'; style-src-attr 'unsafe-inline'`로 바꾼다. 나머지 directive는 그대로다. 이것은 TI-18의 `style-src 'self' 'unsafe-inline'`를 대체한다.
  - `N`은 응답마다 새로 만드는 `secrets.token_urlsafe(16)`다. Page는 같은 값을 `<meta name="style-nonce">`(HTML escape)로 넘긴다.
  - `static/style-nonce.js`의 `installStyleNonce(document, N)`는 `board.js`가 `term.open()` 전에 한 번 호출한다. 이 함수는 `document.createElement`를 감싸 `style` element에만 `.nonce = N`을 넣는다. 다른 tag와 인자는 그대로 둔다.
  - xterm 5.5 DOM renderer는 `<style>`을 두 곳(`_dimensionsStyleElement`, `_themeStyleElement`)에서 `createElement("style")` 뒤 `textContent`로 채운다. 두 element 모두 nonce로 허용되고, nonce 없이 주입된 `<style>` markup은 계속 막힌다.
  - `style-src-attr 'unsafe-inline'`이 남는 이유: xterm은 24-bit truecolor 전경·배경색과 `minimumContrastRatio`가 1이 아닐 때의 색을 `setAttribute("style", …)`로 쓴다. Nonce는 style 속성에 적용되지 않는다. 이 directive가 없으면 headless Chrome 측정에서 truecolor 칸이 기본색으로 그려졌다. Style 속성만으로는 script를 실행할 수 없고, page에는 사용자가 제어하는 HTML이 없다(terminal 출력은 xterm으로 쓴다).
  - 다른 page, `/board`의 200이 아닌 응답(303 login, 421, 403, 503 등), static file은 전역 CSP 그대로다(nonce·`style-src-attr` 없음).
  - xterm을 올릴 때 주입 방식이 바뀌면 `tests/hub/test_web_assets.py`의 tripwire가 실패한다. 이 test는 `createElement("style")` 2회, `setAttribute("style"` 1회, `innerHTML`·`insertAdjacentHTML`·`adoptedStyleSheets`·`cssText` 0회를 확인한다.
- 남은 "연결됨"은 아무것도 막지 않는다: 새 hello는 항상 인수한다(불변식 6).
- Client 재연결 규칙(`board-state.js`): 네트워크로 끊기면 지수 backoff(1→2→4…≤15 s)로 `resume` hello. `taken_over`·`taken_elsewhere`를 받은 뒤에는 자동 재연결하지 않는다(두 tab 사이 핑퐁 방지). Pong이 20 s 없으면 "연결 없음"을 보이고 재연결.
- 송신 queue가 연결당 4 MiB를 넘으면 그 연결을 4003으로 닫는다(client는 resume).
- 감사: attach·takeover·release·timeout·close(D-11).

### CLI 추가(기술 보완)

| 명령 | 동작 | Hub 자동 시작 |
|---|---|---|
| `wb hub passwd [--stdin]` | 위 비밀번호. 예약 거절을 대체. 비밀번호를 정한 뒤 `hub.toml`이 없으면 초기 템플릿을 만든다(2026-10-26 사용자 요청: hub.toml 자동 생성) | 아니오 |
| `wb hub tls --self-signed [--force]` | `openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 825 -subj /CN=<bind 또는 hostname> -addext subjectAltName=IP:<bind>,DNS:localhost[,DNS:<server_names>…]`로 TLS 경로에 만든다. (2026-10-25 orchestrator 정정) wildcard bind(`0.0.0.0`/`::`)면 그 IP 대신 `IP:127.0.0.1`·`IP:::1`과 host 주소(hostname 해석·interface 주소, best effort)를 넣고, loopback bind는 두 loopback IP를 넣는다. leaf 확장(`CA:FALSE`, `serverAuth`)을 붙이고 `--force` 교체는 key·cert 쌍을 함께 바꾼다(실패 시 이전 쌍 복원). 출력에 SAN 목록을 보인다. 이미 있으면 `--force` 없이 거절. openssl이 없으면 exit 2. 끝나면 지문(SHA-256)과 `Hub를 다시 시작하면 HTTPS로 열립니다. 실행 중인 Hub에는 'wb hub reload'로 반영하세요.`를 보인다. `bind`가 목록이면 모든 주소를 같은 규칙으로 SAN에 넣는다(2026-10-27) | 아니오 |
| `wb hub reload` | 실행 중인 Hub가 `hub.toml`을 다시 읽는다(위 "`wb hub reload`"). 바뀐 항목, `web:` 줄, Board agent 안내를 보인다. 무효면 exit 1, 새 listener를 열지 못하면 exit 1, Hub가 없으면 exit 3 | 아니오 |
| `wb hub review-mode <PATH> [auto\|manual]` | 값 없으면 현재 값. Hub `review_mode` op(D-32). 등록되지 않은 PATH는 exit 1 | 예 |
| `wb hub status` | `web:`, 평문 경고, `config:`(2026-10-27), `board_agent:` 줄 추가. `--json`은 hub_v1 `status` 그대로 | 아니오 |

## 상태 전이, 실패와 경계 동작

- Hub 정지 중: 웹·MCP 연결 거절(client는 실패로 본다, P2-AC-23). Backend의 `workbench_inbox`·`to_worker.request_id`는 `inbox_unavailable`. Core 동작은 그대로(P2-AC-01).
- 프로젝트 꺼짐: 제출·조회·댓글·편집·실행 전 취소는 된다(P2-AC-04). 통지·Task 동기화·raw 조회·READY/RUNNING 취소는 `backend_not_running`. 켜지면 dispatcher가 `seq` 순서로 보낸다.
- `inbox.sqlite3` 손상·읽기 실패: 그 프로젝트만 `inbox_unavailable:<reason>`, 덮어쓰지 않는다. 다른 프로젝트는 영향 없음.
- 디스크 가득: 그 변경만 실패(`internal_error`), 아무것도 저장되지 않는다.
- 비밀번호 hash 계산이 executor에서 막혀도 loop는 다른 요청을 계속 처리한다.
- Board agent OMP를 띄울 수 없음(omp 없음, 격리 실패): channel `failed`/`isolation_failed`와 이유. Shell channel은 따로 동작한다.
- Hub 재시작: 세션·API key·감사·요청은 유지된다. Board agent 세트·WebSocket·cancel token·link token·MCP session id는 사라진다(다시 연결).
- 같은 요청에 겹친 변경: 프로젝트 executor의 도착 순서로 하나씩 적용하고 무효가 된 것은 이유와 함께 거절(P2-AC-43).

## 구현 결정

### 합의된 제약(BRIEF)

D-05~D-36 전부와 BRIEF 불변식 (1)~(7). ADR-0003(sandbox 없음, 보호는 로그인·bind·감사), ADR-0004(검토 뒤 상시 위임), ADR-0005(프로젝트별 inbox, Hub 단일 writer). Hub SPEC의 불변식(Hub는 backend 부모·감독자가 아님, 관측 실패는 꺼짐이 아님).

### 기술 보완(이 문서가 정함)

1. 웹 framework는 aiohttp 하나(D-29). Template engine 없이 `html.escape`를 거치는 작은 렌더 함수로 page를 만든다(새 의존성 없음).
2. 비밀번호 KDF는 `hashlib.scrypt`(n=2^15, r=8, p=1). API key는 SHA-256(고엔트로피 secret).
3. 세션·key·감사는 Hub 상태의 `hub.sqlite3`, 비밀번호는 CLI도 쓰므로 별도 `auth.json`(+lock). Hub는 `auth.json`을 감시해 listener를 연다.
4. 확인 창 = 서버가 그린 확인 page(JS 없이 HTTP로 검증 가능).
5. 기본 bind `127.0.0.1:8780`. LAN은 사용자가 `hub.toml`에 명시한다(D-10 "설정한 주소에만").
6. Host·Origin 검사로 DNS rebinding을 막는다(`server_names`).
7. 요청 순서·전이는 프로젝트별 단일 thread executor + `BEGIN IMMEDIATE`.
8. 검토 방식은 `inbox.sqlite3`의 `meta.review_mode`에 둔다(`.wb` 아래, 프로젝트와 함께 이동·재등록 뒤 유지, 단일 writer 유지. D-32·Q-09).
9. Hub → backend는 ui_v1 비attach 요청 3종 추가, backend → Hub는 hub_v1 backend principal. 새 socket·channel 없음.
10. 승인 명세 연결은 `to_worker.request_id` + link check/commit 두 단계로, 연결이 확인되기 전에는 worker에 전달하지 않는다.
11. 수동 검토의 "TUI에서 시작"(D-31)은 사용자가 TUI에서 manager에게 말하면 manager가 `workbench_inbox begin_review`를 부르는 것이다(TUI 전용 key·화면을 새로 두지 않는다).
12. REVIEWING 중 편집은 그 자리에서 반영하고 새 review round(orchestrator 지시, Q-10). READY·RUNNING 편집의 `revised` 통지는 검토 방식과 무관하게 보낸다.
13. 사용자 Task 취소는 backend의 새 `TaskFlow.cancel_by_user`(기존 `_cancel` 절차 재사용, `by: user`)와 기존 사용자 명령 stop의 조합이다.
14. Board agent의 MCP는 Board agent 전용 loopback 내부 listener로 붙는다(외부 listener의 bind 주소·자체 서명 TLS와 무관). [2026-10-28 개정: MCP → 같은 내부 listener의 REST]
15. Board agent 세트는 첫 연결 때 시작하고 Hub 재시작 뒤 자동 복원하지 않는다(HB-O09). OMP 세션은 `--continue`로 잇는다.
16. systemd unit에 `KillMode=process`와 사용자 환경 일부(`Environment=`)를 더한다.
17. MCP 2단계 취소 token TTL 60 s(Q-03), link token 30 s. [2026-10-28 개정: REST `POST …/cancel`의 같은 token·TTL]
18. 감사 기록은 최근 50,000건 유지.
19. Board agent 기본 모델은 manager 모델(Q-07).

### 모듈 배치

| 모듈 | 내용 |
|---|---|
| `src/workbench/hub/config.py` | `hub.toml` 읽기·검증, TLS 판정 |
| `src/workbench/hub/auth.py` | `auth.json`(scrypt), `hub.sqlite3`(세션·로그인 실패·API key·감사) store, 로그인 지연 정책 |
| `src/workbench/hub/web/app.py`, `middleware.py`, `render.py`, `pages/*.py`, `static/` | aiohttp app, Host/Origin/CSRF/세션 middleware, 보안 header, page |
| `src/workbench/hub/web/listener.py` | listener 수명(auth 감시, bind, TLS, 재시도, 정지) |
| `src/workbench/hub/inbox/store.py`, `service.py`, `limits.py` | inbox schema·lock·직렬화 executor, 상태 기계·권한, 한도 |
| `src/workbench/hub/inbox/dispatch.py` | dispatcher, Task 동기화, backend ui_v1 호출 |
| `src/workbench/hub/mcp.py` | MCP JSON-RPC transport·tool [2026-10-28 개정: 삭제. 처리 계층은 `inbox_api.py`, REST·내부 listener는 `rest.py`, `/skill/`은 `web/pages/skill.py`] |
| `src/workbench/hub/board/set.py`, `launch.py`, `ws.py` | BoardSet PTY, Board agent home·overlay·격리 확인, WebSocket lease |
| `src/workbench/hub/web_control.py` | Hub 쪽 `projects.*` 실행(executor, 웹 token 흐름) |
| 변경: `hub/server.py`, `hub/status.py`, `hub/presentation.py`, `hub/cli.py`, `hub/service_unit.py`, `contracts/hub_v1.py` | 웹·dispatcher·Board 결합, principal, 새 op, status 필드, CLI, unit |
| 변경: `contracts/ui_v1.py`, `backend/ui_server.py` | 비attach `inbox_notice`·`task_status`·`cancel_task` |
| 변경: `backend/service.py`, `backend/flow.py`, `backend/flow_tasks.py`, `backend/flow_recovery.py`, `backend/inbox_link.py`(새) | backend 쪽 hub client, `workbench_inbox`, `to_worker.request_id`, `cancel_by_user`, notice 결과 callback·snapshot `inbox` |
| 변경: `omp_bridge/g3/bridge.ts`, `omp_bridge/skills/workbench-inbox/`, `omp_bridge/board/` | 도구·notice type, manager skill, Board agent 기본 prompt·overlay template |
| 변경: `pyproject.toml`, `build_support/assets.py`(필요 시), `MANIFEST.in` | 의존성 고정, web 자산 package data |

## 수락 기준

BRIEF의 P2-AC-01~45를 그대로 쓰고 기술 보완 기준 TI-T01~T10을 더한다. 증거: `runtime`(실제 Hub·backend 프로세스·loopback socket), `inproc`(aiohttp test client의 in-process app), `fixture`(fake OMP·가짜 controller·가짜 backend), `static`(파일·문구·asset 검사), `pending`(이 host에서 실행 불가, `not_run`).

| ID | 관측 | 책임 ticket | seam | 증거 | 최종 통합 |
|---|---|---|---|---|---|
| P2-AC-01, 04 | Hub 정지 중 core 동작, 프로젝트 꺼짐 중 제출·조회 | TI-04(꺼짐 접수), TI-19 | T1, T3 | runtime | TI-19(Hub 정지 중 backend·Task 진행) |
| P2-AC-02, 03 | 제출 즉시 id, 같은 key·내용 중복 없음, 다른 내용 `idempotency_conflict` | TI-04, TI-05 | T1, T4 | runtime | TI-19 |
| P2-AC-05, 06 | 웹·MCP 같은 기록, 질문·답·재검토, 조회가 모델·backend 통지를 만들지 않음 | TI-05, TI-06, TI-11 | T2, T4, T6 | inproc, runtime | TI-19 |
| P2-AC-07, 08 | 접수만으로 REVIEWING·READY 아님, 전달 ACK(`delivered`)와 REVIEWING, link와 Task 시작(READY/RUNNING) 구분 | TI-04, TI-10, TI-11 | T1, T6 | fixture | TI-19 |
| P2-AC-09, 10 | Task·명령 성공만으로 CLOSED 아님, worker 실행 중 통지가 선점하지 않음 | TI-10, TI-11 | T6 | fixture | TI-19 |
| P2-AC-11, 12 | 통지 `unknown` 재전송 없음, link 불명이면 전달 없음, 재시작·연결 상실·지연 구분 | TI-10, TI-11 | T6 | fixture | TI-19 |
| P2-AC-13, 14 | key로 연결·terminal·제어 불가, 웹 escape, 전체 보기 경로 제한 | TI-05, TI-06, TI-08, TI-14, TI-17 | T2, T4, T5 | inproc, runtime | TI-19 security |
| P2-AC-15 | 비밀번호 없으면 listener 없음·이유, passwd 뒤 재시작 없이 열림, registry·목록 정상 | TI-03 | T3 | runtime | TI-19 |
| P2-AC-16 | bind 주소만, 기본 HTTP, 비loopback 평문 경고, `allow_insecure_http=false`, `wb hub tls` | TI-03, TI-07 | T3, T8 | runtime | TI-19 |
| P2-AC-17 | 실패 지연, HttpOnly·SameSite, CSRF 거절, 24 h 만료, 비밀번호 변경 시 다른 세션 무효, passwd 재설정, terminal 재인증 없음 | TI-02, TI-03, TI-07 | T1a, T2, T3 | inproc, runtime | TI-19 |
| P2-AC-18 | 감사 사건 기록, 입력 내용 없음 | TI-02, TI-03, TI-07, TI-17 | T1a, T2, T5 | inproc, runtime | TI-19 |
| P2-AC-19 | key 한 번 표시·hash만, 폐기·만료 거절, 목록 필드, rate limit 없음 | TI-02, TI-07, TI-05 | T1a, T2, T4 | runtime | TI-19 |
| P2-AC-20 | requester 자기 것만, editor scope, scope 밖 거절, key로 금지 동작 거절 | TI-04, TI-05, TI-08, TI-17 | T1, T4, T5 | runtime | TI-19 |
| P2-AC-21 | 제어는 세션만·확인 page·취소면 불변, 끄기 HB-AC-09, pane 입력 수단 없음 | TI-08, TI-12 | T2, T6 | inproc, fixture | TI-19 |
| P2-AC-22 | `manager_editor_not_empty` 대기 표시 | TI-10, TI-12 | T6, T2 | fixture | TI-19 |
| P2-AC-23 | 꺼짐 접수 NEW·켜면 순서대로, Hub 꺼짐이면 실패·core 무관, 프로젝트 간 분리 | TI-04, TI-10 | T1, T6 | runtime | TI-19 |
| P2-AC-24 | 자동: 쉬는 manager에 전달·REVIEWING, 선점 없음. 수동: NEW 유지 | TI-10 | T6 | fixture | TI-19 |
| P2-AC-25 | ask → NEEDS_INFO·질문 댓글, 세 주체 답변 → REVIEWING·재검토, reject → CLOSED(rejected) | TI-11 | T6 | fixture | TI-19 |
| P2-AC-26 | link → Task id·revision·READY, 시작 → RUNNING, 진행·보고 표시, 완료 판단 전 CLOSED 없음 | TI-11 | T6 | fixture | TI-19 |
| P2-AC-27 | 재시도 한도 뒤 결정을 댓글로 묻고 답함 | TI-11 | T6 | fixture | TI-19 |
| P2-AC-28 | 세 출처 같은 경로로 실행, 출처 표시 | TI-11, TI-05, TI-06 | T6, T4, T2 | fixture | TI-19 |
| P2-AC-29 | NEW·NEEDS_INFO 바로 반영, READY·RUNNING 새 revision·통지, Task 불변 | TI-04, TI-10, TI-06 | T1, T6, T2 | fixture | TI-19 |
| P2-AC-30 | 실행 전 즉시, RUNNING 확인 → 사용자 stop + Task 취소, 수락만으로 종료 보고 없음, 취소 주체 제한 | TI-13 | T6, T7 | fixture | TI-19 |
| P2-AC-31 | 기본 요약, 전체 보기(requester 자기 것), script·HTML 비실행 | TI-14, TI-06 | T2, T4, T6 | inproc, fixture | TI-19 |
| P2-AC-32 | 자동 삭제 없음, CLOSED 숨김·filter, 첨부 없음, tab 제목·badge | TI-04, TI-06 | T1, T2 | inproc | TI-19, 실제 browser `pending` |
| P2-AC-33 | Board agent는 Hub 자식·manager와 별개, Hub MCP editor만, 다른 MCP·설정 안 보임, manager·worker 누출 검사 통과, 같은 로그인·세션 유지 | TI-15, TI-16 | T6b, T9 | fixture, runtime | TI-19, 실제 OMP·모델 `pending` |
| P2-AC-34 | 두 tab, 브라우저 닫아도 유지, 다른 브라우저 이어짐, 재시작 버튼 | TI-16, TI-17, TI-18 | T5, T2, T10 | runtime | TI-19, 실제 browser `pending` |
| P2-AC-35 | 연결 수명 5사례 + 남은 연결 표시가 막지 않음 | TI-17 | T5 | runtime | TI-19 |
| P2-AC-36 | GPU·작업 중 프로젝트 "계속할까요?" 안내 | TI-16 | T9(prompt 문구) | static | `pending`(GPU·실제 모델) |
| P2-AC-37 | CDN 없음, xterm.js+license 포함, aiohttp 고정·audit 기록, `mcp` SDK 없음 | TI-01 | T9 | static | TI-19(wheel·page scan) |
| P2-AC-38 | 웹 양식 작성·편집·댓글, 출처 소유자 NEW | TI-06 | T2 | inproc | TI-19 |
| P2-AC-39 | 수동: 웹 "검토 시작"(세션만)·TUI(manager `begin_review`), C-D70 대기 규칙, key 불가 | TI-10, TI-11, TI-12 | T6, T2 | fixture, inproc | TI-19 |
| P2-AC-40 | 웹·CLI 변경, `.wb` 아래 저장, Hub 재시작·재등록 뒤 유지 | TI-04, TI-10, TI-12 | T1, T8, T2 | runtime | TI-19 |
| P2-AC-41 | MCP RUNNING 취소 2단계, token 없음·불일치·상태 변경이면 거절·불변 | TI-05(token), TI-13(실물) | T4, T6 | runtime, fixture | TI-19 |
| P2-AC-42 | editor 허용 전이와 거절 전이 | TI-04, TI-05 | T1, T4 | runtime | — |
| P2-AC-43 | 겹친 변경 도착 순서, 전달 전 취소가 link를 이김·worker 전달 없음 | TI-04, TI-11, TI-13 | T1, T6 | fixture | TI-19 |
| P2-AC-44 | 한도 초과 거절·저장 없음(웹·MCP) | TI-04, TI-05, TI-06 | T1, T4, T2 | runtime, inproc | — |
| P2-AC-45 | stdlib MCP client로 제출·조회·댓글·취소·결과 | TI-05 | T4 | runtime | TI-19, Codex CLI `pending` |
| TI-T01 | Host·Origin 검사(DNS rebinding): 다른 Host 421, 다른 Origin 403(웹·`/mcp`·`/ws`) | TI-03, TI-05, TI-17 | T2, T3 | inproc, runtime | TI-19 security |
| TI-T02 | 보안 header·CSP, inline script 없음, 원문 page `text/plain` | TI-03, TI-06 | T2, T9 | inproc, static | TI-19 |
| TI-T03 | Backend·Board agent·CLI가 `inbox.sqlite3`를 열지 않음(`/proc/<pid>/fd`), 다른 Hub(다른 HOME)는 `inbox.lock`에서 `inbox_busy` | TI-04, TI-19 | T1, T3 | runtime | TI-19 |
| TI-T04 | hub_v1 backend principal: peer pid가 그 프로젝트 backend가 아니면 `principal_mismatch` | TI-09 | T3b | runtime | — |
| TI-T05 | ui_v1 새 비attach 요청은 `workbench-hub`+같은 uid만, attach 자리 불변 | TI-09 | T7 | fixture | — |
| TI-T06 | unit에 `KillMode=process`·`Environment=`, Hub에서 켠 backend가 Hub 정지·`SIGKILL` 뒤 생존 | TI-08 | T3, T8(fake systemctl) | runtime, fixture | TI-19: 실제 systemd `pending` |
| TI-T07 | Board agent 세트가 돌 때 `stop_request.active`에 보임, 확인 → 세트 종료 뒤 정지, 거절 → 불변, `SIGTERM` → 확인 없이 종료, 세트 없으면 기존처럼 묻지 않음 | TI-16 | T3, T8 | runtime | TI-19(`wb update`) |
| TI-T08 | `wb update` backup에 `inbox.sqlite3` 포함(committed image), Hub 정지로 lock 해제 | TI-04, TI-19 | T1 | runtime | TI-19 |
| TI-T09 | `to_worker.request_id` link 실패·불명이면 worker 전달 없음(mailbox·worker frame 없음) | TI-11 | T6 | fixture | TI-19 |
| TI-T10 | 산출물 경로: root 밖·symlink 탈출·`.wb`·`.git` 거절 | TI-14 | T2, T4 | inproc, runtime | TI-19 security |

## 테스트 결정

**아래 seam은 제안이다. CLAUDE.md TDD 규칙에 따라 orchestrator가 사용자 확인(또는 사전 위임 판단)을 받은 뒤 테스트를 쓴다.** 기존 seam(Hub S1~S9)을 그대로 쓰는 곳은 그 이름을 쓴다.

| seam | 대상 | 잡는 것 | 놓치는 것 | 독립 기대값 출처 |
|---|---|---|---|---|
| T1 | `workbench.hub.inbox.store/service` API + 테스트가 stdlib `sqlite3`로 직접 여는 `inbox.sqlite3`(여러 thread·process 동시) | schema, 전이표, 권한 matrix, revision·댓글, idempotency, 한도, 도착 순서, lock, mismatch, backup 포함 | HTTP·인증·backend 연동 | 이 SPEC의 전이표·권한표·한도표, 테스트가 쓴 입력, 파일 내용 |
| T1a | `workbench.hub.auth` API + `auth.json`·`hub.sqlite3` 파일 | scrypt 형식, generation 무효화, 세션 만료(시계 주입), key 형식·hash만·폐기·만료·마지막 사용, 지연 함수, 감사 행 | HTTP cookie·middleware | 이 SPEC 수치(n,r,p, 24 h, f(n)), `hashlib.scrypt` 직접 계산 |
| T2 | aiohttp `TestServer`/`TestClient`로 in-process `create_app(services)`(가짜 projects·backend port, 실제 auth·inbox store, temp 상태) | route·redirect·cookie flag·CSRF·Host/Origin·보안 header·escape·확인 page 흐름·권한·badge·form 한도 | 실제 bind·TLS·프로세스 수명, browser 렌더링·JS | SPEC route표, HTML에서 읽은 값, header 문자열 |
| T3 | 실제 Hub(`wb _hub --foreground`) + loopback HTTP(stdlib `http.client`/`ssl`) + CLI subprocess(S1 환경) | listener 개폐·bind 주소·port 0·TLS·평문 경고·passwd 뒤 열림·status 필드·정지 순서·세트 정지 | browser | `ss`/`/proc/net/tcp` bind 주소, 응답 bytes, BRIEF 문구 |
| T3b | 실제 Hub + 테스트가 직접 hub_v1 socket으로 backend hello(가짜 peer·실제 backend) | backend principal 검증, 새 op framing | manager 도구 경로 | `/proc`의 pid·start_ticks, `backend.json` |
| T4 [2026-10-28 개정: web-board-ux S2(`support/rest_hub.py` + `support/rest_client.py`)로 대체, MCP client 삭제] | 실제 Hub + **stdlib MCP 테스트 client**(`tests/hub/support/mcp_client.py`, `http.client` + `json`) | JSON-RPC·initialize·tools/list(등급별)·tools/call·401·Origin·session id·각 tool·2단계 cancel token(가짜 cancel port 또는 실제)·한도 | Codex CLI·다른 client 호환 | MCP 문서의 message 형태(이 SPEC에 요약), SPEC tool표 |
| T5 | 실제 Hub(또는 in-process app의 실제 server) + Python WebSocket client(aiohttp `ws_connect`) + 테스트 asyncio TCP proxy(멈춤·끊기·RST) + 가짜 PTY 명령(stdin을 파일에 기록하는 echo) | epoch·인수·이전 epoch 입력 버림·heartbeat 끊김(축소 interval + 한 번은 실제 10 s)·release·RST·resume·taken_elsewhere·두 tab 동시·감사 | browser JS·xterm 렌더링 | D-28 6사례, 가짜 PTY가 기록한 stdin bytes, 감사 행 |
| T6 | 실제 backend + fake OMP(`tests/recovery_boot/fake_omp.py` 확장: `editor <text>` 입력으로 composer 비지 않음 흉내, notice 기록, `tool workbench_inbox …`·`tool to_worker …`) + 실제 Hub(foreground) | 통지·REVIEWING·대기 사유·수동·답변 재검토·link check/commit·READY/RUNNING·close·cancel(사용자 stop·Task 취소)·재시작 구분 | 실제 OMP·모델 판단 | `$FAKE_NOTICES`·`$FAKE_FRAMES`, `tasks.sqlite3`(stdlib 읽기), inbox 파일, `/proc` |
| T6b | 가짜 `omp`(PATH 앞 temp bin: argv·env·overlay 파일 내용 기록, `--version`, isolation dump에 테스트가 정한 tool 목록 답) + 실제 BoardSet | Board agent argv(`--no-extensions`, bridge 없음, overlay·prompt·`--continue`·model), home·symlink, 시스템 key 회전, 격리 실패 시 종료, 재시작 | 실제 OMP의 MCP 연결(→ TI-15 probe, 대상 host) | SPEC Board agent 절, 기록된 argv·파일 |
| T7 | 실제 `UiServer` + 가짜 controller(Hub S5 패턴) | `inbox_notice`·`task_status`·`cancel_task` 허용 조건, attach 자리 불변, 결과 형태 | 실제 TaskFlow | ui_v1 계약 문구 |
| T8 | CLI subprocess(Hub S1) + fake `systemctl`·실제 `openssl` | `passwd --stdin`·TTY, `tls --self-signed` 결과 파일·권한·SAN, `review-mode`, unit 내용(KillMode·Environment), status 줄 | 실제 systemd | 파일 bytes, `openssl x509 -text` 출력 |
| T9 | 정적 검사: wheel build 결과의 자산·`LICENSE`, `pyproject.toml` pin, 제공 page·JS에 외부 URL 없음, `mcp` import 없음, ROLE.md 문구 | P2-AC-36·37, TI-T02 일부 | runtime 동작 | BRIEF D-29 버전 목록, SPEC 문구 |
| T10 | `node --test`로 `static/board-state.js`(순수 상태 기계 모듈) | 재연결 규칙(taken_over 뒤 자동 재연결 없음, backoff, banner 상태), 입력 epoch 부착 | 실제 browser DOM·xterm | SPEC WebSocket 절 |

원칙: 각 ticket은 해당 seam에서 behavior 하나씩 RED(의도한 behavior 부재로 실패) → 최소 GREEN → 다음 순서로 진행한다. 인증·세션·CSRF·API key(TI-02·03·05), inbox 단일 writer·순서(TI-04), backend 통로·주입·도구(TI-09~11·13), 보안 경계(TI-08·14), Board agent·WebSocket(TI-15~17)은 독립 test_designer가 요구에서 시나리오를 먼저 도출해 확인·보강한다(`*_independent*` 파일은 test_designer 소유). 최종 통합 전 ticket마다 단위 review, 최종 통합은 Standards/Spec 두 fresh reviewer와 별도 security review(LAN listener, 인증, API key, MCP, 웹 terminal, 비attach 제어 확장, 산출물 경로가 trigger)와 dependency audit을 받는다.

테스트 위생(관측된 교훈):
- Hub·CLI를 띄우는 모든 테스트는 temp `HOME`·`XDG_STATE_HOME`·`XDG_CONFIG_HOME`, **짧은 private `XDG_RUNTIME_DIR`**(0700, 아니면 runtime이 실제 `/tmp/omp-workbench-<uid>`로 내려간다), `WORKBENCH_HUB_AUTOSTART=0`을 쓴다.
- 웹·MCP·WebSocket은 `127.0.0.1`·port 0에만 bind한다. LAN 주소 bind 테스트는 하지 않는다(`bind`가 설정 값 그대로 쓰이는지는 loopback의 다른 주소 `127.0.0.2`로 확인).
- 유료 provider·실제 모델·Docker·외부 network 없음. Board agent는 가짜 `omp`, manager는 fake OMP.
- Hub·backend·PTY 자식은 띄운 테스트가 끝에 정리하고 `/proc`로 부재를 확인한다.
- `tests/hub/fixtures/`의 공용 sandbox helper 하나를 TI-03에서 만들고 이후 ticket이 쓴다.

실행 명령(repo 루트, 공통 접두사 `P='python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python'`, `-t .` 없음):
- auth: `$P -m unittest discover -s tests/hub -p 'test_hub_auth*.py'`
- listener·web: `$P -m unittest discover -s tests/hub -p 'test_hub_web*.py'`
- inbox store: `$P -m unittest discover -s tests/hub -p 'test_inbox_store*.py'`
- MCP: `$P -m unittest discover -s tests/hub -p 'test_hub_mcp*.py'` [2026-10-28 개정: 삭제. REST는 `-p 'test_hub_rest*.py'`]
- 웹 page: `$P -m unittest discover -s tests/hub -p 'test_web_pages*.py'`
- 제어: `$P -m unittest discover -s tests/hub -p 'test_web_control*.py'`
- ui_v1: `$P -m unittest discover -s tests/backend -p 'test_ui_server_inbox*.py'`, `$P -m unittest tests.contracts.test_ui_v1`
- backend 통로·검토·취소(T6): `$P -m unittest discover -s tests/hub -p 'test_inbox_flow*.py'`
- Board agent·WS: `$P -m unittest discover -s tests/hub -p 'test_board*.py'`
- JS: `node --test tests/hub/js/`
- 정적: `$P -m unittest discover -s tests/hub -p 'test_web_assets*.py'`
- 회귀(관련 모듈만, 넓은 실행은 TI-19 한 번): `tests/hub`, `tests/backend`(`-p 'test_ui_server*.py'`, `-p 'test_handoff*.py'`, `-p 'test_flow*.py'`), `tests/recovery_boot`, `tests/contracts`, `tests/backend/maintenance`(`-p 'test_update_*.py'`), `tests/backend/installation`.
- 통합(TI-19): `$P -m unittest discover -s tests/integration -p 'live_inbox*.py'`. `tests/gates`는 쓰지 않는다.

TDD 예외: `ROLE.md`·manager skill·README·USAGE 등 문서(TI-16 일부, TI-11 일부, TI-20)는 RED 없이 T9 정적 확인. xterm.js vendoring(TI-01)은 외부 자산 복사라 RED 대신 T9 정적 확인과 wheel 검사. CSS·page 배치는 T2의 필수 요소 확인만 하고 시각 검증은 대상 host.

## 호환성, 통합, 롤아웃 제약

- 추가 위주다: 새 파일·새 route·새 op. ui_v1·hub_v1 `VERSION` 그대로(추가 허용). 옛 backend는 Hub의 새 요청을 거절하고 Hub는 `backend_too_old`로 보인다(`wb update`가 backend를 재시작하므로 정상 경로에서는 없다).
- `.wb`에 `inbox.sqlite3`·`inbox.lock`이 생긴다. `wb update` backup·`wb migrate`가 Hub 정지 뒤 이를 포함하는지 TI-19가 확인한다.
- `tasks.sqlite3`·`backend.json`·설치 manifest 구조는 바꾸지 않는다(Task 명세 문서의 `inbox_request_id`는 additive JSON 필드).
- `pyproject.toml` runtime 의존성에 D-29의 10개 고정 버전이 들어간다. 설치 SPEC의 dependency audit 요구를 따른다(아래 열린 결정 2).
- `wb hub enable`로 만든 unit은 형식이 바뀐다(`KillMode=process`). 기존 사용자는 `enable`을 다시 실행해야 한다(status가 안내).
- Hub 목록의 `요청` 열이 `—`에서 수로 바뀐다(HB-AC-19는 (B) 범위의 기준이었다).
- 실제 사용자 데이터·실제 연구 process·유료 provider·Docker·새 도구 설치 없음. 운영 배포·commit/push는 별도 권한이다.

## 대상 host 확인 항목(이 개발 host에서 검증 불가, `pending`)

1. 실제 browser(Firefox·Chromium·휴대폰)에서 page·확인 page·badge·xterm.js 렌더링, tab 닫기·강제 종료·다른 browser 인수의 체감 동작.
2. LAN/VPN 주소 bind와 다른 기기 접근, 자체 서명 인증서의 browser 신뢰 흐름.
3. GPU 안내(D-27 (1)): 실제 `nvidia-smi`와 실제 모델의 "계속할까요?" 응답(D-30).
4. 실제 OMP·실제 모델로 Board agent가 Hub MCP tool을 쓰고 manager가 `workbench_inbox`로 검토하는 흐름(유료 provider). [2026-10-28 개정: Board agent는 `wb-inbox` skill과 REST. Skill 로드와 REST 호출은 web-board-ux WBX-B4 재실행(2026-10-28, passed)에서 확인]
5. Codex CLI의 MCP Streamable HTTP 호환성(D-36). [2026-10-28 개정: MCP 없음. 대신 Claude Code·Codex·OMP의 `wb-inbox` skill 설치·사용(web-board-ux WBX-AC-22, `pending`)]
6. 실제 systemd --user에서 `KillMode=process` unit과 재부팅 뒤 시작.
7. Dependency audit의 온라인 advisory 확인(열린 결정 2).

## 열린 결정·위임된 선택

**2026-10-25 orchestrator 확정(사용자 사전 위임)**: test seam T1~T10을 이 문서의 안대로 확정한다. dependency audit는 새 도구 설치 금지에 따라 `pip-audit`·`uvx`를 쓰지 않고 수동 audit 기록(고정 버전·출처·알려진 advisory 확인 범위)을 남기며 온라인 advisory 점검은 `not_run`으로 표시한다(최종 보고에서 사용자에게 다시 묻는다). TI-15 probe가 실패하면 D-25 대안은 사용자 승인 대상이므로 TI-16 이후 Board agent 관련 ticket만 보류하고 나머지는 진행한다.

사용자 확인이 필요한 항목:
1. **TI-15 probe가 실패할 때**: OMP 18.12가 config overlay로 Streamable HTTP MCP 서버 하나(인증 header 포함)만 켤 수 없으면, D-25의 "Hub MCP에 editor 등급으로 연결"을 그대로 지킬 수 없다. 대안(Workbench 전용 OMP extension이 Hub에 HTTP로 붙는 Board 전용 도구 묶음, 또는 stdio MCP proxy)은 그때 근거와 함께 묻는다. Probe가 통과하면 이 항목은 닫힌다.
2. **Dependency audit 방법**: 개발 `.venv`에 `pip-audit`이 없다. `uvx pip-audit`(network로 도구를 내려받음)을 허용할지, 아니면 고정 버전·hash·출처를 기록한 수동 audit(`DEPENDENCY-AUDIT.md`)과 대상 host 확인으로 둘지. 기본안은 후자이며 온라인 확인은 `not_run`으로 남긴다.

위임된 선택(사용자 확인 불필요, 위 "기술 보완"에 기록): 기본 bind·port, 확인 창 형식, scrypt 수치, 지연 함수, 세션 저장 위치, 감사 보존 건수, 상태 이름·reason·op·route 이름, polling 주기(2 s)·TTL(60 s/30 s)·heartbeat 여유(1 s), ring buffer 크기, 검토 방식 저장 위치, Board agent 기본 모델, HB-O09 복원 방식, systemd unit 변경, TUI 검토 시작 해석.

## BRIEF·Hub SPEC과의 차이(발견 사항)

- Hub SPEC의 systemd unit에는 `KillMode`가 없어 Hub가 끝날 때 Hub가 켠 backend·Board agent가 같이 끝난다. TI-08이 `KillMode=process`로 고친다(HB-D04 유지).
- BRIEF는 RUNNING 취소에 "기존 Task 취소 경로"를 쓰라고 하지만 core에는 manager `to_worker cancel:true`만 있고 사용자 Task 취소는 없다. ui_v1 `stop_command`도 attach가 필요하다. 새 `cancel_task`(비attach)와 `TaskFlow.cancel_by_user`를 둔다.
- BRIEF는 "manager에게 전달하면 REVIEWING"이지만 Watchdog notice queue는 메모리에만 있고 결과 callback이 없으며, `report_wait`은 worker 보고만 센다. 결과 callback·snapshot `inbox`·Hub 쪽 전달 상태 영속을 더한다.
- `to_worker`는 Task를 만들자마자 worker에 보낸다. D-35의 "전달 전 취소가 이김"을 지키려면 link check/commit을 전달 앞에 둬야 한다.
- Board agent가 외부 listener(LAN IP·자체 서명 TLS)로 MCP에 붙으면 bind·인증서 문제가 생긴다. 내부 loopback listener를 둔다.
- HB-07(`workbench.hub.projects`)이 아직 없다. TI-08은 HB-07 완료가 선행 조건이다.

## Ticket 그래프

이번 판은 PLAN.json을 만들지 않았다. 아래 표가 의존성의 정본이며 ticket Markdown은 이 표를 참조한다. 모델은 CLAUDE.md 기준(Opus: 인증·세션·CSRF·API key, WebSocket, inbox 단일 writer·순서, manager 주입·도구, 보안 경계, Board agent 프로세스. Sonnet: page 렌더링, CLI, vendoring, 문서, 검증).

| ID | 제목 | 선행 | 모델 | 주요 쓰기 범위(추정) |
|---|---|---|---|---|
| [TI-01](tickets/TI-01.md) | 의존성 고정·web 자산 packaging·xterm.js vendoring | — | Sonnet | `pyproject.toml`, `MANIFEST.in`, `src/workbench/hub/web/static/vendor/`, `build_support/` |
| [TI-02](tickets/TI-02.md) | 인증 core: `auth.json`·`hub.sqlite3`·세션·API key·감사·`wb hub passwd` | — | Opus | `hub/auth.py`, `hub/cli.py`, `backend/cli.py`(parser) |
| [TI-03](tickets/TI-03.md) | `hub.toml`·web listener 수명·middleware(Host/Origin/CSRF/세션/header)·로그인 | TI-02 | Opus | `hub/config.py`, `hub/web/{app,middleware,listener,render}.py`, `hub/server.py` |
| [TI-04](tickets/TI-04.md) | inbox 저장소·상태 기계·권한·직렬화·한도 | — | Opus | `hub/inbox/{store,service,limits}.py` |
| [TI-05](tickets/TI-05.md) | MCP Streamable HTTP endpoint·tool·2단계 cancel token | TI-03, TI-04 | Opus | `hub/mcp.py`, `hub/web/app.py`, `tests/hub/support/mcp_client.py` |
| [TI-06](tickets/TI-06.md) | 웹 요청 page(목록·상세·작성·편집·댓글·상태·취소 확인·badge) | TI-03, TI-04 | Sonnet | `hub/web/pages/requests.py`, `static/badge.js`, css |
| [TI-07](tickets/TI-07.md) | 설정 page(비밀번호·API key·감사), `wb hub tls`, status web 줄 | TI-03 | Sonnet | `hub/web/pages/settings.py`, `hub/cli.py` |
| [TI-08](tickets/TI-08.md) | Hub 쪽 프로젝트 제어 실행·systemd `KillMode`/환경 | TI-03, HB-07 | Opus | `hub/web_control.py`, `hub/service_unit.py` |
| [TI-09](tickets/TI-09.md) | Hub ↔ backend 통로 계약(hub_v1 backend principal, ui_v1 비attach 3종) | TI-04 | Opus | `contracts/{hub_v1,ui_v1}.py`, `hub/server.py`, `backend/ui_server.py`, `backend/inbox_link.py` |
| [TI-10](tickets/TI-10.md) | 검토 전달: dispatcher·notice 결과·REVIEWING·대기 사유·검토 방식·`review-mode`·목록 요청 수 | TI-09 | Opus | `hub/inbox/dispatch.py`, `backend/flow_recovery.py`, `backend/service.py`, `hub/status.py`, `hub/presentation.py`, `bridge.ts`(notice type) |
| [TI-11](tickets/TI-11.md) | Manager `workbench_inbox`·`to_worker.request_id` link·Task 동기화·close·skill | TI-10 | Opus | `bridge.ts`, `backend/{flow,flow_tasks,service,inbox_link}.py`, `hub/inbox/dispatch.py`, `omp_bridge/skills/workbench-inbox/` |
| [TI-12](tickets/TI-12.md) | 상태 board·프로젝트 page·제어 확인 page·검토 방식·검토 시작 버튼 | TI-06, TI-08, TI-10 | Sonnet | `hub/web/pages/{board_status,projects}.py` |
| [TI-13](tickets/TI-13.md) | 취소 경로(사용자 Task 취소·사용자 stop·READY/RUNNING·경합) | TI-05, TI-06, TI-11 | Opus | `backend/{flow_tasks,service,ui_server}.py`, `hub/inbox/{service,dispatch}.py` |
| [TI-14](tickets/TI-14.md) | 결과 요약·전체 보기·raw page·산출물 경로 제한 | TI-05, TI-06, TI-11 | Opus | `hub/inbox/results.py`, `hub/web/pages/requests.py`, `hub/mcp.py` |
| [TI-15](tickets/TI-15.md) | OMP MCP 연결 feasibility probe | — | Opus | `tests/hub/probes/`, `docs/features/task-inbox/PROBE-OMP-MCP.md` |
| [TI-16](tickets/TI-16.md) | Board agent 세트 프로세스·home·overlay·격리·정지 연동 | TI-15, TI-05, TI-02 | Opus | `hub/board/{set,launch}.py`, `omp_bridge/board/`, `hub/server.py` |
| [TI-17](tickets/TI-17.md) | WebSocket terminal 프로토콜(lease·epoch·heartbeat·인수) | TI-03, TI-16 | Opus | `hub/board/ws.py`, `hub/web/app.py` |
| [TI-18](tickets/TI-18.md) | Board page UI(xterm.js·tab·재연결·banner·재시작) | TI-17, TI-01 | Sonnet | `hub/web/pages/board.py`, `static/board*.js`, `tests/hub/js/` |
| [TI-19](tickets/TI-19.md) | 최종 통합·live·독립 검증·security review·dependency audit | TI-01~18 | Sonnet(검증), 결함 수정은 해당 ticket 모델 | `tests/integration/live_inbox*.py`, `docs/features/task-inbox/VERIFICATION.md` |
| [TI-20](tickets/TI-20.md) | 문서(README·CONTEXT·USAGE·보안 안내) | TI-19 | Sonnet | `README.md`, `CONTEXT.md`, `docs/features/task-inbox/USAGE.md`, `docs/features/workbench-hub/USAGE.md` |

의존 이유: TI-03은 TI-02의 세션·감사 store를 middleware에 쓴다. TI-05·06은 listener·middleware(TI-03)와 `InboxService`(TI-04)를 쓴다. TI-07은 TI-03의 page 틀과 TI-02의 key·감사 store를 쓴다. TI-08은 HB-07 라이브러리와 TI-03의 세션 전용 route를 쓴다. TI-09는 `InboxService`(TI-04)에 manager principal을 연결한다. TI-10은 TI-09의 `inbox_notice`·`inbox_delivery`를 쓴다. TI-11은 TI-10의 dispatcher(통지 event)와 TI-09의 link op를 쓴다. TI-12는 TI-06의 page 틀, TI-08의 제어, TI-10의 검토 방식·대기 사유를 쓴다. TI-13은 TI-05의 token, TI-06의 확인 page, TI-11의 Task 연결을 쓴다. TI-14는 TI-11의 Task 동기화와 TI-05·06의 출력 경로를 쓴다. TI-16은 TI-15의 OMP 설정 방식, TI-05의 MCP(내부 listener), TI-02의 시스템 key를 쓴다. TI-17은 TI-16의 BoardSet과 TI-03의 세션·Origin을 쓴다. TI-18은 TI-17 프로토콜과 TI-01의 xterm 자산을 쓴다.

초기 frontier: TI-01, TI-02, TI-04, TI-15. 그 뒤 병렬 후보: {TI-03(TI-02 뒤), TI-09(TI-04 뒤)}, {TI-05, TI-06, TI-07}(TI-03·04 뒤), TI-08(TI-03·HB-07 뒤). 배타 자원: `src/workbench/hub/server.py`(TI-03·09·10·16·17), `src/workbench/hub/web/app.py`(TI-03·05·06·07·12·14·17·18), `src/workbench/hub/cli.py`·`backend/cli.py`(TI-02·07·10), `src/workbench/backend/{service,flow_tasks,flow}.py`(TI-09·10·11·13), `omp_bridge/g3/bridge.ts`(TI-10·11), `hub/inbox/service.py`(TI-04·05·10·11·13), `hub/status.py`·`presentation.py`(TI-10). 같은 파일을 쓰는 ticket은 동시에 쓰지 않는다.
