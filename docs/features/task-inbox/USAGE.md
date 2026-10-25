# Task Inbox & Web Board 사용 안내

Hub가 제공하는 웹(Web Board)과 MCP endpoint로 요청을 접수·검토·실행·조회하는 방법이다. 합의는 [BRIEF](BRIEF.md), 구현 명세는 [SPEC](SPEC.md), Hub 자체의 명령은 [Hub 사용 안내](../workbench-hub/USAGE.md)를 따른다. 이 문서는 사용자가 쓰는 명령·설정·화면과 보안·한계만 다룬다.

핵심 한 줄: **웹은 기본으로 닫혀 있다.** 비밀번호를 정해야 열리고, 기본 주소는 `127.0.0.1:8780`(자기 컴퓨터만)이다. 요청은 접수만으로 실행되지 않고 항상 manager 검토를 거친다.

## 1. 처음 설정

### 1.1 비밀번호

```sh
wb hub passwd            # terminal에서 두 번 입력 (10~1024자)
printf '%s\n' "$PW" | wb hub passwd --stdin   # 스크립트용. terminal에서는 거절
```

- 비밀번호가 없으면 웹 listener를 열지 않는다. 정하는 순간 Hub 재시작 없이 열린다(Hub는 약 2초마다 확인).
- 비밀번호를 정하면 기존 웹 로그인 세션이 모두 무효가 된다. 잊었으면 같은 명령으로 다시 정한다.
- 이 명령은 Hub를 시작하지도, 자동 시작하지도 않는다. 값은 인자나 환경 변수가 아니라 terminal 또는 stdin 첫 줄로만 받는다.

### 1.2 `hub.toml`

설정 파일은 `$XDG_CONFIG_HOME/omp-workbench/hub.toml`(절대 경로일 때), 아니면 `~/.config/omp-workbench/hub.toml`이다. 없으면 기본값을 쓴다. Hub는 이 파일을 **시작할 때 한 번 읽고 쓰지 않는다**. 바꾼 뒤에는 `wb hub stop` 다음 `wb hub start`(또는 다음 자동 시작)로 반영한다.

| 표 | key | 기본값 | 의미 |
|---|---|---|---|
| `[web]` | `bind` | `"127.0.0.1"` | 열 주소. IP literal만 된다(호스트 이름 불가). `"0.0.0.0"`·`"::"`은 직접 쓸 때만 |
| `[web]` | `port` | `8780` | 0~65535. 0이면 임의 port(실제 값은 `wb hub status`에 보임) |
| `[web]` | `tls_cert`, `tls_key` | `""` | 인증서·key 절대 경로. 빈 값이면 기본 경로 `~/.config/omp-workbench/tls/hub-cert.pem`, `hub-key.pem` |
| `[web]` | `allow_insecure_http` | `true` | TLS 파일이 없을 때 평문 HTTP를 허용할지. `false`면 TLS가 없으면 열지 않음 |
| `[web]` | `server_names` | `[]` | bind IP 외에 `Host`로 받을 DNS 이름 목록(예: `"box.vpn"`). DNS rebinding 방어용이며 인증서 SAN에도 들어감 |
| `[board_agent]` | `model` | `""` | Board agent의 OMP model selector. 빈 값이면 `omp-models.yml`의 manager 항목 |
| `[board_agent]` | `role_prompt_file` | `""` | 추가 role prompt 절대 경로. 기본 prompt 뒤에 붙고 대체하지 않는다 |
| `[board_agent]` | `shell` | `""` | 웹 shell 프로그램 절대 경로. 빈 값이면 `$SHELL`, 없으면 `/bin/sh` |

모르는 key, 모르는 표, 잘못된 형식, 호스트 이름인 `bind`는 파일 전체가 `config_invalid:<항목>`이 된다. 이때 웹과 Board agent만 닫히고 Hub의 나머지(등록·목록·켜기/끄기)는 그대로 동작한다. 파일은 64 KiB까지다.

LAN/VPN 예(VPN 주소 `10.8.0.2`, 이름 `box.vpn`):

```toml
[web]
bind = "10.8.0.2"
port = 8780
server_names = ["box.vpn"]
```

`bind = "0.0.0.0"`은 이 컴퓨터의 모든 네트워크 인터페이스에서 받는다. 공용망에 닿는 인터페이스가 있으면 로그인 화면이 인터넷에 노출될 수 있으니 VPN 인터페이스 주소를 직접 쓰는 편이 안전하다.

**평문 HTTP 경고.** 인증서가 없고 `bind`가 loopback이 아니면(`0.0.0.0` 포함) 평문 HTTP로 열리며, 모든 웹 page 상단과 `wb hub status`에 "평문 HTTP로 LAN에 열려 있습니다. VPN 등 네트워크 보호가 필요합니다."가 보인다. 평문에서는 비밀번호·세션 cookie·API key가 네트워크에 그대로 오간다. VPN처럼 믿을 수 있는 네트워크 안에서만 쓰거나 아래 TLS를 켠다.

### 1.3 자체 서명 TLS

```sh
wb hub tls --self-signed            # hub.toml의 TLS 경로에 cert·key 생성
wb hub tls --self-signed --force    # 이미 있는 쌍을 새것으로 교체
```

- host의 `openssl`을 쓴다(EC P-256, 유효 825일). 없으면 exit 2. 이미 있으면 `--force` 없이는 거절한다. `--force` 교체는 cert와 key를 함께 바꾸고 실패하면 이전 쌍을 되돌린다.
- key는 본인만 읽는 0600, 디렉터리는 0700이다. 다른 권한이면 `wb hub status`가 경고한다.
- SAN(접속 주소 목록)은 `hub.toml`에서 정해진다. `bind`가 특정 IP면 그 IP, loopback이면 `127.0.0.1`과 `::1`, `0.0.0.0`/`::`이면 두 loopback 주소와 host의 interface 주소·이름(best effort)이다. `localhost`와 `server_names`는 항상 들어간다. 출력의 `SAN:` 줄에 실제 목록이 나오니, **브라우저에서 접속할 주소가 거기 없으면** `bind`·`server_names`를 고친 뒤 `--force`로 다시 만든다.
- 만든 뒤 출력되는 SHA-256 지문을 기억해 두고, 브라우저에서 처음 열 때 인증서 경고 화면의 지문과 같은지 확인한 뒤 예외를 수락한다.
- cert·key 둘 다 있어야 HTTPS로 열린다. **하나만 있거나 읽지 못하면 평문으로 내려가지 않고 열지 않는다**(`tls_invalid`).
- 이 명령은 Hub를 건드리지 않는다. **Hub를 다시 시작해야** HTTPS로 열린다(`wb hub stop` 후 `wb hub start`).

### 1.4 시작과 확인

```sh
wb hub start
wb hub status            # web: listening https://10.8.0.2:8780 / web: not_listening (…)
```

브라우저에서 `status`가 보여 준 주소를 연다. 부팅 때 자동 시작하려면 `wb hub enable`([Hub 사용 안내](../workbench-hub/USAGE.md#부팅-자동-시작과-linger)). `wb hub status --json`은 hub_v1 `status`를 그대로 보인다.

## 2. 로그인과 세션

- 로그인은 비밀번호 하나다. 세션은 로그인 시각부터 **24시간** 뒤 절대 만료되며 연장되지 않는다. 만료·로그아웃·비밀번호 변경이 일어나면 다시 로그인한다.
- 로그인에 반복 실패하면 다음 시도에 지연이 붙는다(최대 30초). 지연 중에는 서버가 잠들지 않고 "로그인 시도가 너무 잦습니다. N초 뒤 다시 시도하세요."와 함께 즉시 거절한다. 지연은 Hub를 재시작해도 유지된다. 같은 컴퓨터(loopback)와 최근 30일 안에 로그인에 성공한 주소는 다른 사람의 실패 때문에 전체 지연을 받지 않는다.
- 비밀번호 변경: **설정 > 비밀번호**(`/settings`). 현재 비밀번호가 필요하고, 바꾸면 현재 세션만 남고 다른 세션은 모두 끊긴다. 현재 비밀번호를 틀리면 로그인 실패와 같은 지연을 받는다. 잊었으면 `wb hub passwd`.
- 웹은 CSRF 토큰·`Origin` 확인·`Host` 확인을 한다. 접속 주소(`Host`)는 bind IP, `server_names`, loopback bind의 `localhost`만 받고 그 밖은 421이다. 이름으로 접속하려면 `server_names`에 적어야 한다.

## 3. API key (외부 agent용)

**설정 > API key**(`/settings/keys`)에서 소유자만 발급·폐기한다. API key로는 웹 로그인, terminal, Board agent 세트, 프로젝트 제어, 비밀번호·key·감사 조회를 할 수 없다.

- 발급 항목: 이름(1~64자, 영문·숫자·공백·`.`·`_`·`-`), 등급, scope, 만료(없음 또는 1~3650일).
- 형식: `wbk_<10자>_<43자>`. **원문은 발급 직후 한 번만** 보인다. 이후 어디에도 없고 저장소에는 hash만 남는다. 잃어버리면 폐기하고 새로 발급한다.
- 등급:

| 등급 | 할 수 있는 것 |
|---|---|
| `requester` | scope 안 프로젝트에 요청 제출. **자기가 낸 요청만** 조회·댓글·취소·결과 조회. 프로젝트 목록은 id·이름만 |
| `editor` | scope 안 모든 요청 조회·편집·댓글·상태 지정(NEW↔NEEDS_INFO, 다시 열기)·취소·결과 조회. 프로젝트 상태 조회 |

- scope: 전체(`all`) 또는 선택한 프로젝트 목록. 발급 form의 기본값은 **선택한 프로젝트**이며 전체는 직접 골라야 한다. scope 밖 프로젝트는 존재하지 않는 것처럼 `not_found`로 답한다.
- 목록에는 이름·등급·scope·마지막 사용(최대 약 10초 늦을 수 있음)·만료·상태가 보인다. `board-agent` 시스템 key(Board agent용, Board agent를 시작할 때마다 새로 발급)는 "Board agent(시스템)"로 보이고 폐기 버튼이 없다.
- **폐기**: 확인 page 뒤 즉시 효력이 있고 다음 호출부터 401이다. rate limit은 없다.
- **key가 유출됐다면**: 즉시 폐기하고 새로 발급한다. 그 key로 낸 요청은 출처가 `key:<이름>`으로 남아 있으니 요청 목록에서 출처로 훑어 의심되는 것을 취소한다. 실행 전에 걸러지지 않은 요청은 manager 검토를 거치지만, **검토를 통과하면 출처와 상관없이 실행된다**([ADR-0004](../../adr/0004-auto-execution-after-manager-review.md)). 평문 HTTP에서 유출됐다면 비밀번호도 바꾼다.
- 모든 key는 소유자의 것이다. 요청 출처는 항상 `소유자` / `Board agent` / `key:<이름>`으로 보인다.

## 4. MCP 연결

외부 agent는 Hub의 MCP Streamable HTTP endpoint에 붙는다.

| 항목 | 값 |
|---|---|
| URL | `https://<host>:<port>/mcp` (평문이면 `http://`) |
| 인증 | `Authorization: Bearer wbk_…`. `initialize`를 포함해 **모든 요청**에 필요. 웹 cookie는 인정하지 않는다 |
| 요청 | `POST`, JSON-RPC 2.0 객체 하나(batch 불가), body ≤ 256 KiB |
| 필수 header | `Accept: application/json`, `Content-Type: application/json`. 없으면 각각 406·415 |
| 응답 | `application/json` 단일 응답(SSE stream 없음). notification은 `202` |
| 그 밖 | `GET`은 405, `DELETE`는 세션 종료 204. 지원 version은 `2025-06-18`, `2025-03-26`. `Mcp-Session-Id`는 `initialize`가 돌려준다 |

수동 확인 예(`curl`, 자체 서명 인증서는 `--cacert`로 신뢰하거나 시험 때만 `-k`):

```sh
curl -sS https://box.vpn:8780/mcp \
  -H "Authorization: Bearer $WB_KEY" \
  -H "Accept: application/json" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

**Codex CLI 예 (대상 host에서 확인되지 않음).** Codex CLI의 MCP Streamable HTTP 호환성은 개발 host에서 검증하지 못했다. 아래 설정 형식은 Codex 문서의 일반 형식을 따른 예이며 동작을 보장하지 않는다. 개발 중 호환성은 표준 라이브러리 MCP 시험 client로만 확인했다.

```toml
# ~/.codex/config.toml  (미검증 예)
[mcp_servers.workbench-hub]
url = "https://box.vpn:8780/mcp"
bearer_token_env_var = "WB_HUB_KEY"
```

자체 서명 인증서를 Codex가 신뢰하는지도 확인되지 않았다. 안 되면 VPN 안에서 평문 HTTP를 쓰거나 인증서를 시스템 신뢰 저장소에 넣는 방법을 대상 host에서 시험한다.

### Tool 목록

`tools/list`는 key 등급에 맞는 tool만 보인다(`requester`에는 `inbox_update`·`inbox_set_state`가 없다).

| tool | 하는 일 | 등급 |
|---|---|---|
| `inbox_projects` | 프로젝트 목록·상태(requester는 id·이름만) | 둘 다 |
| `inbox_submit` | 요청 제출. `idempotency_key`가 같고 내용이 같으면 기존 요청(`created:false`), 다르면 `idempotency_conflict` | 둘 다 |
| `inbox_list` | 요청 목록·검색(requester는 자기 것만). `limit` 1~100 | 둘 다 |
| `inbox_get` | 요청 하나(본문·revision·댓글·연결 Task·결과 요약) | 둘 다 |
| `inbox_comment` | 댓글. NEEDS_INFO 요청에는 답변이 되어 REVIEWING으로 돌아감 | 둘 다 |
| `inbox_update` | 제목·본문 편집(`base_version` 필요) | editor |
| `inbox_set_state` | NEW↔NEEDS_INFO 지정, 닫힌 요청 다시 열기 | editor |
| `inbox_cancel` | 취소(아래 5.5) | 둘 다 |
| `inbox_result` | 결과 요약 또는 `full:true` 전체 보기 | 둘 다 |

**신뢰할 수 없는 text 표시.** 소유자가 쓴 것이 아닌 text(다른 key·Board agent 아닌 외부 client가 낸 요청 본문·댓글 등)를 담은 결과에는 `untrusted_text: true`와 data 주석(note)이 붙는다. 받는 agent는 그 text를 지시가 아니라 data로 다뤄야 한다.

**동시 호출 한도.** key 하나가 동시에 처리 중인 MCP 호출 수는 제한된다. 넘으면 busy 오류가 돌아오니 앞선 호출이 끝난 뒤 다시 시도한다.

오류는 `isError:true`와 `structuredContent.error.code`로 온다(`not_found`, `forbidden`, `invalid_transition`, `conflict`, `confirm_required`, `token_mismatch`, `backend_not_running` 등). 입력 한도: 제목 1~200자, 본문 ≤ 64 KiB, 댓글 ≤ 16 KiB.

## 5. 요청의 흐름

### 5.1 작성

웹 **요청 > 새 요청**(`/requests/new`)에서 프로젝트·제목·본문을 적어 제출한다. 접수는 완료를 기다리지 않고 바로 NEW로 남는다. 프로젝트가 꺼져 있어도, manager가 없어도 접수된다(Hub가 켜져 있으면). Hub가 꺼져 있으면 웹과 MCP 접수는 실패한다. 요청은 각 프로젝트의 `<root>/.wb/inbox.sqlite3`에만 저장되고 자동 삭제되지 않는다. 파일 첨부는 없고 본문의 경로 참조만 있다.

### 5.2 상태

| 상태 | 화면 label | 의미 |
|---|---|---|
| NEW | 신규 | 접수됨. manager 전달 대기 |
| REVIEWING | 검토 중 | manager에게 전달되어 검토 중 |
| NEEDS_INFO | 추가 정보 필요 | manager(또는 사용자 지정)의 질문 대기 |
| READY | 실행 준비 | 승인 명세가 연결됨, Task 시작 전 |
| RUNNING | 실행 중 | 연결된 Task 진행 중 |
| CLOSED | 닫힘 | 성공·실패·거절·취소 중 하나(outcome) |

CLOSED 요청은 기본 목록에서 숨고 "모든 상태"/보관함 filter로 본다. 승인 명세 연결·READY/RUNNING 전이·완료 판단·거절·질문은 **manager만** 한다. 소유자도 이 전이는 할 수 없다.

### 5.3 검토 방식: 자동과 수동

검토 방식은 프로젝트별 설정이다. 기본은 **자동**이다.

```sh
wb hub review-mode /path/to/project            # 현재 값 보기
wb hub review-mode /path/to/project manual     # auto | manual
```

프로젝트 페이지(`/projects/<id>`)의 검토 방식 form으로도 바꾼다. 등록되지 않은 PATH는 exit 1이고 Hub가 필요하면 자동으로 띄운다.

- 자동: 프로젝트가 켜져 있고 manager가 쉬고 있을 때 Workbench가 접수 순서대로 NEW 요청을 manager에게 알린다. 알림이 manager에게 전달된 것을 확인하면 REVIEWING이 된다.
- 수동: 자동으로 알리지 않는다. 요청 상세의 **검토 시작** 버튼(또는 manager TUI에서 사용자가 지시)을 누를 때까지 NEW로 남는다. 전달 여부가 불명이면 같은 자리에 **다시 전달** 버튼이 나온다.
- 어느 쪽도 실행 중인 작업을 선점·중단하지 않는다. 알림은 manager가 쉬고 입력창이 빌 때만 간다.

**대기 사유.** 알림이 기다리는 이유는 상태 board와 프로젝트 표의 "대기 사유" 열에 아래 label로 보인다.

| label | 뜻 |
|---|---|
| 일시정지 | 자동화 일시정지 중 |
| 재부팅 확인 전 보류 | `confirm-boot` 전 |
| manager 입력창에 글이 있어 알림 대기 | manager 입력창에 글이 있음 |
| manager 세션 연결 없음(알림 대기) | manager OMP 세션이 없음 |
| manager 작업 중이라 요청 전달 대기 | manager가 다른 일을 하는 중 |
| metadata 저장 장애로 보류 | 저장 장애 |
| 모델 오류로 보류 | 모델 오류 |
| Task 보류 | Task 보류 |

"요청" 열은 `대기 N · 질문 M`(NEW N건, NEEDS_INFO M건), 모두 0이면 `0`, 요청 저장소를 읽지 못하면 `—`다. 브라우저 tab 제목 `(대기·질문) WB Hub`와 header badge도 같은 수이며 30초마다 갱신한다. 그 밖의 알림(메일·메신저·OS 알림)은 없다.

### 5.4 질문과 답변, 편집, 검토 중 변경

- Manager가 질문하면 NEEDS_INFO가 되고 질문이 댓글 thread에 나온다. **누가 답해도**(웹의 소유자, Board agent, 원 요청 client) 댓글이 답변이 되어 REVIEWING으로 돌아가고 새 검토가 시작된다.
- 사용자도 NEW↔NEEDS_INFO를 직접 지정할 수 있고 CLOSED 요청을 NEW로 다시 열 수 있다(이전 Task 연결은 이력으로 옮겨진다).
- 편집:
  - NEW·NEEDS_INFO: 그 자리에서 바뀐다(이전 내용은 기록에 남는다).
  - REVIEWING: 그 자리에서 바뀌고 검토가 다시 시작된다.
  - READY·RUNNING: **새 요청 revision**이 만들어지고 manager에게 알림이 간다. 실행 중 Task와 명세는 manager가 새 명세 revision을 만들기 전까지 바뀌지 않는다.
  - CLOSED: 편집할 수 없다. 먼저 다시 연다.
  - 같은 요청을 동시에 편집하면 늦게 저장한 쪽이 `conflict`를 받는다.
- 재시도 한도를 다 쓴 뒤 manager가 기다리는 사용자 결정도 요청 댓글로 묻고 답한다.

### 5.5 취소

| 요청 상태 | 웹 | MCP (`inbox_cancel`) |
|---|---|---|
| NEW·REVIEWING·NEEDS_INFO | 확인 page 없이 즉시 CLOSED(취소) | 즉시 `cancelled` |
| READY | **확인 page** 뒤 Task 취소 요청 | 토큰 없이 바로 `cancel_requested` |
| RUNNING | **확인 page**(영향 요약: Task·실행 중 명령)와 토큰 | **2단계**: 첫 호출이 `confirm_required`와 `confirm_token`(60초)·영향을 돌려주고, 같은 token으로 다시 호출하면 실행 |

- 확인 사이에 Task나 명령이 바뀌었으면 `token_mismatch`로 거절하고 아무것도 멈추지 않는다.
- 취소 수락은 종료가 아니다. 요청은 Task가 사용자 취소로 닫힌 것이 관측되어야 CLOSED(취소)가 된다. 실행 중 명령의 stop 결과가 불명이면 그 사실이 댓글로 남고 성공처럼 보이지 않는다.
- 프로젝트가 꺼져 있으면 READY/RUNNING 취소는 `backend_not_running`으로 거절된다. 켠 뒤 다시 한다.
- 취소할 수 있는 주체: 소유자, editor key(Board agent 포함), 원 요청의 requester key.

### 5.6 결과: 요약과 전체 보기

- **요약**(기본): manager가 닫으면서 남긴 최종 보고 요약. 닫기 전에는 연결 Task의 상태와 run 결과 줄. 요청 상세와 `inbox_result`가 같다.
- **전체 보기**(상세의 "전체 보기", `/requests/<id>/full`, MCP `inbox_result` `full:true`): run 목록, worker 보고 원문, 명령 목록, 각 run의 raw 로그(offset/length page, 1~131072 B), 산출물 경로. 요청을 볼 수 있으면 requester key도 자기 요청에 대해 쓸 수 있다.
- **산출물**(`artifact_path`)의 한계: 요청에 **연결된 Task**가 있어야 읽을 수 있다. 프로젝트 root 아래 일반 파일만, symlink로 root 밖으로 나가는 경로는 거절, `.wb/`와 `.git/` 아래는 제외, 한 page ≤ 128 KiB. 비밀로 보이는 파일(`.env`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `*.p12`, `*.pfx`, `.netrc`, `.npmrc`, `.pypirc`, `credentials*`, `*.kdbx`)은 거절한다. 그 밖의 host 파일은 이 경로로 열 수 없다.
- 프로젝트가 꺼져 있으면 마지막으로 본 Task 상태만 "backend 꺼짐"과 함께 보이고 raw 로그는 열 수 없다.
- 원문은 `text/plain`으로만 보내며 요청 본문·댓글의 HTML·script는 실행되지 않는다. 로그·산출물에 비밀이 섞여 있을 수 있으므로 **requester key에도 전체 보기가 열려 있다는 점**(모든 key가 소유자의 것이라는 전제의 의도적 완화, D-22)을 알고 쓴다.

## 6. 웹의 프로젝트 제어

상태 board(`/`)는 등록된 프로젝트를 10초마다 새로 고쳐 보여 준다(이름·경로·상태·Task·단계·명령·대기 사유·요청 수·마지막 활동·검토 방식). 상태를 확인하지 못한 프로젝트는 꺼짐이 아니라 "연결 없음/불명"이다. 프로젝트 페이지에서 **상태에 맞는 것만** 버튼이 나온다.

| 동작 | 비고 |
|---|---|
| 켜기 / 끄기 | 끄기 확인 page가 진행 중인 작업 목록을 보여 준다. 그 사이 작업이 바뀌면 새 목록으로 확인 page를 다시 보인다 |
| 일시정지 / 재개 | 자동화 일시정지 |
| 재부팅 확인(`confirm-boot`) | 재부팅 뒤 보류를 푸는 확인. 보류 규칙은 바꾸지 않는다 |

- 모든 제어는 **확인 page**를 거친다. 확인 page에서 "취소" 링크를 따르면 아무것도 바뀌지 않는다. 결과가 검증(verified)되지 않으면 성공으로 보이지 않는다.
- 로그인한 소유자 세션만 된다. API key로는 이 경로에 닿을 수 없다.
- **웹에는 manager·worker·host pane 입력 수단이 없다.** 웹으로 그 pane을 조작하지 못하고, 요청 흐름 밖에서 일을 시키는 통로는 Board agent뿐이다.

## 7. Board agent 세트

`/board`의 **Board agent**(OMP) tab과 **shell** tab이다. Hub가 소유하는 소유자 전용 화면이며 어느 프로젝트의 manager·worker와도 별개다. Hub가 켜진 동안 유지되고 브라우저를 닫아도 계속 돈다. 첫 `/board` 연결 때 시작한다(Hub가 시작될 때 자동으로 띄우지 않는다).

### 7.1 위험 (읽고 쓰기)

**Board agent와 웹 shell은 sandbox 없이 당신의 일반 shell과 같은 OS 권한으로 실행된다.** 코드 수정, `gh`/`git` 인증, `wb init`, GPU 사용을 하게 하려는 의도적 선택이다([ADR-0003](../../adr/0003-board-agent-without-sandbox.md)). 그 결과:

- **웹 로그인이 유일한 장벽이다.** 비밀번호나 세션이 새면 공격자는 당신의 OS 사용자 권한으로 이 컴퓨터에서 무엇이든 할 수 있다(코드·자격 증명·다른 프로젝트 자료 포함).
- 평문 HTTP는 네트워크 보호(VPN 등)에 전적으로 의존한다.
- Board agent와 shell은 요청 흐름 밖에서 명령을 실행할 수 있다. "검토 전에는 실행되지 않는다"는 보장은 MCP·요청 경로에 대한 것이다.
- 완화: 비밀번호를 길게, `bind`를 필요한 주소 하나로, 가능하면 TLS와 VPN을 쓴다. Board agent의 MCP 연결은 Hub 하나로 제한되고 프로젝트·사용자 `~/.omp` 설정은 막히지만 이것은 **설정 노출을 줄일 뿐 OS 권한 경계가 아니다.**

### 7.2 연결, 인수, 재연결

- 세트는 한 번에 **하나의 연결**만 쓴다(lease). 다른 브라우저·기기에서 `/board`를 열면 **새로 연결한 곳이 즉시 인수**하고, 이전 연결에는 인수당했다는 안내가 나온 뒤 닫힌다.
- 인수당한 쪽 화면에는 **"다른 곳에서 연결됨 — 다시 가져오기"**가 보인다. 버튼을 누르면 다시 가져온다. 두 tab 사이에 자동으로 뺏고 뺏기지 않도록 인수당한 뒤에는 자동 재연결을 하지 않는다.
- 같은 tab에서 네트워크가 잠깐 끊기면 자동으로 다시 붙는다(1→2→4…최대 15초 간격). 안정적으로 10초 동안 붙어 있었으면 간격은 처음(1초)부터 다시 시작한다. 남은 "연결됨"이 새 연결을 막지는 않는다.
- close 4400~4499(hello 오류 4400, hello CSRF 불일치 4403 등 서버가 이 page 자체를 거절한 경우)는 같은 hello를 다시 보내도 소용없으므로 **자동 재연결하지 않는다**. "연결 없음"으로 두고 소유자가 "다시 연결"을 누를 때까지 기다린다.
- close 4401은 "연결 없음 — 다시 로그인"으로 보이고 로그인 동작이 나온다. 자동 재연결은 없다.
- 연결 수 한도: 열린 `/ws/board` 소켓이 모두 8개를 넘으면 upgrade가 **503**으로, 한 로그인 세션에서 hello 전 소켓이 2개를 넘으면 **429**로 거절된다. client는 거절당하면 backoff를 두고 다시 시도한다.
- 크기 한도: 한 message가 1 MiB를 넘으면 Hub가 연결을 닫는다(1009). 입력 frame은 64 KiB까지이고 넘으면 그 입력은 버려지며 `error`(`too_large`)가 돌아온다.
- 화면 쪽 감시: 20초 동안 pong이 없으면 화면이 스스로 연결을 닫고(4000, 서버 코드가 아님) 다시 붙는다.
- 브라우저 back/forward cache로 page가 복원되면(`pageshow`) 떠날 때(`pagehide`) 해제했던 연결을 다시 이어 받는다.
- Heartbeat: 화면이 10초마다 신호를 보내고, Hub는 20초(+1초)간 아무 신호도 없으면 연결을 끊고 상태를 "연결 없음"으로 둔다. 연결 없음일 때 입력한 키는 어디에도 전달되지 않는다.
- 로그아웃·비밀번호 변경(`wb hub passwd` 포함)·24시간 만료로 세션이 무효가 되면 열린 연결도 닫힌다(close 4401).
- 연결은 로그인한 세션만 열 수 있고 API key로는 열 수 없다.

### 7.3 재시작

channel(agent 또는 shell) 하나만 재시작할 수 있다(`/board`의 재시작 버튼, 확인 page). 그 channel의 session을 SIGHUP → SIGTERM → 5초 → SIGKILL로 끝내고 새로 띄운다. Board agent는 이전 세션을 `--continue`로 잇는다. channel이 스스로 끝나면 `exited`(종료 코드)로 보이고 다시 시작 버튼이 나온다. Board agent OMP를 띄우지 못하거나(omp 없음, 격리 확인 실패 `isolation_failed`) 해도 shell은 따로 동작한다.

### 7.4 Board agent의 home과 역할 안내

- Board agent의 home은 `~/.local/state/omp-workbench/hub/board-agent/`(0700)다. 세션과 메모리가 여기에 남는다. OMP 로그인은 사용자 `agent.db`를 따라간다. 이 위치를 지우면 Board agent의 기억이 사라진다.
- 시작 직후 tool 목록을 확인해 Hub MCP 외의 MCP tool이 보이면 Board agent를 끝내고 `isolation_failed`로 표시한다.
- 기본 role prompt에는 두 가지 안내가 있다. (1) GPU를 쓰기 전에 `nvidia-smi`와 실행 중 명령을 확인해 사용 중이면 **"계속할까요?"**라고 묻는다. (2) 진행 중인 Task·명령이 있는 프로젝트 root 아래 파일을 고치기 전에 **"계속할까요?"**라고 묻는다. 잠금이나 중재는 하지 않으며 어디까지나 모델이 묻는 안내다. 직접 만든 prompt는 `board_agent.role_prompt_file`로 뒤에 붙인다.
- Board agent는 Workbench bridge가 없어서 승인·worker 전달을 할 수 없다. 요청을 찾고 쓰고 정리하는 데까지다.
- Board agent 안의 `wb`는 Hub의 후손이므로 WB 관리 terminal이 아니다. `wb init`의 확인은 PTY에서 답한다.

### 7.5 Hub 정지와 업데이트

세트의 channel이 살아 있으면 `wb hub stop`과 `wb update`가 확인 목록에 "Board agent 세트: agent running(pid N) · shell running(pid M) · 연결됨"을 보이고 `계속할까요? [y/N]`을 묻는다. 확인하면 세트를 끝낸 뒤 Hub가 멈춘다. 거절하면 아무것도 멈추지 않는다. 확인하는 사이 channel 상태가 바뀌면 다시 확인한다. `--yes`는 확인 질문만 건너뛰며 terminal이 없으면 `--yes` 없이는 멈추지 않는다. Hub가 `SIGTERM`을 받으면 확인 없이 세트를 끝낸다. Hub가 `SIGKILL`로 죽으면 PTY가 닫혀 SIGHUP을 받지만 그 뒤 남는 프로세스는 보장하지 않는다. Hub 재시작 뒤에는 세트·WebSocket·MCP 세션이 사라지지만, 열려 있던 `/board` tab은 로그인 세션이 아직 유효하면 자동으로 다시 붙는다(세션이 무효면 4401로 "다시 로그인"). 요청·API key·세션·감사 기록은 유지된다.

## 8. 감사 기록

**설정 > 감사 기록**(`/settings/audit`)에 최근 순으로 최대 50,000건이 남고 넘으면 오래된 것부터 지워진다. 위치는 Hub 상태 디렉터리의 `hub.sqlite3`(`~/.local/state/omp-workbench/hub/`)다.

남는 사건: 로그인 성공·실패·로그아웃, 비밀번호 변경(웹·CLI), API key 발급·폐기, terminal 연결·인수·해제·시간 초과·닫힘, Board agent 시작·정지·재시작, 웹 프로젝트 제어(켜기/끄기·일시정지·재개·`confirm-boot`·검토 방식 변경·검토 시작). **terminal 입력·출력, 비밀번호, key 원문, 요청 본문은 남기지 않는다.**

## 9. `wb hub enable` 사용자는 다시 실행하세요

Web Board 이전에 `wb hub enable`을 한 사용자는 **`wb hub enable`을 한 번 더 실행**해야 한다. 새 unit은 `KillMode=process`를 담는다. 없으면 systemd가 Hub를 멈출 때(`systemctl --user stop`, 로그아웃·linger 변화 포함) 같은 cgroup의 backend·Board agent까지 끝낸다. 옛 형식이면 `wb hub status`가 "systemd unit이 옛 형식입니다(KillMode 없음). 'wb hub enable'을 다시 실행하세요."라고 알려 준다.

또한 `enable`을 실행하는 시점의 `PATH`, `SHELL`, `LANG`, `LC_ALL`(있는 것)이 unit의 `Environment=`로 **고정**된다. systemd 서비스는 로그인 shell의 환경을 받지 않기 때문이다. 이 값은 Hub가 켜는 backend와 Board agent shell이 물려받는다. 나중에 `PATH`에 도구를 추가했다면(예: `nvm`, `uv tool` 경로 변경) `wb hub enable`을 다시 실행해야 반영된다.

## 10. 문제 해결

### 10.1 `web: not_listening (…)`

`wb hub status`의 `web:` 줄이 이유를 보인다. `wb hub status --json`에는 `web.state`/`web.detail`로 나온다.

| 이유 | 뜻과 조치 |
|---|---|
| `password_not_set` | 비밀번호가 없다 → `wb hub passwd`. `password_not_set:auth_invalid`면 `auth.json`이 손상된 것이니 `wb hub passwd`로 다시 정한다 |
| `config_invalid:<항목>` | `hub.toml`의 해당 key/표가 모르는 것이거나 형식이 틀리다(`web.bind` 등, `toml`은 문법, `file`은 읽기 실패·64 KiB 초과) → 고친 뒤 Hub 재시작 |
| `tls_invalid:<cert_without_key\|key_without_cert\|load_failed>` | cert·key 쌍이 불완전하거나 읽을 수 없다. 평문으로 내려가지 않는다 → 두 파일을 맞추거나 지우고(`wb hub tls --self-signed --force`) Hub 재시작 |
| `insecure_http_disallowed` | `allow_insecure_http=false`인데 TLS 파일이 없다 → `wb hub tls --self-signed` 또는 설정 변경 |
| `bind_failed:<errno 이름>` | 주소가 이 컴퓨터에 없거나 port가 쓰이는 중(예: `EADDRINUSE`, `EADDRNOTAVAIL`, `EACCES`). 30초마다 다시 시도한다 → `bind`·`port`를 고치고 Hub 재시작 |
| `stopping` | Hub가 멈추는 중 |

`wb hub status`의 `board_agent:` 줄은 `agent running(pid N) · shell running(pid M) · 연결됨|연결 없음`이거나 `없음`이다.

### 10.2 WebSocket close 코드

| 코드 | 의미 |
|---|---|
| 4001 | 다른 연결이 인수했다(다른 곳에서 연결됨). 자동 재연결 안 함 → "다시 가져오기" |
| 4000 | 화면이 스스로 닫음: 20초 동안 pong 없음. 같은 tab이 다시 붙는다(서버 코드 아님) |
| 4002 | heartbeat 시간 초과(20초 무신호). 연결 없음으로 바뀌며 같은 tab이 다시 붙는다 |
| 4003 | 보내야 할 출력이 너무 쌓여(연결당 4 MiB) 끊었다. 다시 붙는다 |
| 4400 | hello를 5초 안에 보내지 않았거나 형식이 틀렸다. 자동 재연결 안 함 |
| 4401 | 세션이 무효가 됐다(로그아웃·비밀번호 변경·만료). "연결 없음 — 다시 로그인"과 로그인 동작이 나온다. 자동 재연결 안 함 |
| 4403 | hello의 CSRF 토큰이 맞지 않는다. page를 새로 고친다. 자동 재연결 안 함 |
| 4400~4499 | 서버가 이 page를 거절한 코드 전체. 모두 자동 재연결 안 함 → "다시 연결" |
| 1009 | message가 1 MiB 초과 |
| (upgrade 거절) | 503: 열린 소켓 8개 초과. 429: 한 세션의 hello 전 소켓 2개 초과. client는 backoff 후 재시도 |
| 1000 | 정상 해제(탭 닫기·release) |
| 1001 | Hub가 멈추는 중 |

### 10.3 그 밖

- **`proc_unreadable`**: `/proc`을 읽을 수 없는 host(`hidepid` 등)라 WB 관리 terminal 여부를 확인할 수 없어 Hub를 자동으로 시작하지 않았다는 뜻이다. 외부 terminal에서도 `/proc` 접근을 고치기 전에는 시작되지 않는다(Hub 사용 안내 참고).
- **421**: 접속 주소(Host)가 bind IP/`server_names`/loopback의 `localhost`가 아니다 → `server_names`에 이름을 넣고 Hub 재시작.
- **403 csrf_failed / Origin**: 오래 열어 둔 page이거나 프록시가 `Origin`을 바꾸었다. page를 새로 고친다.
- **`backend_not_running`**: 그 프로젝트가 꺼져 있다. READY/RUNNING 취소와 raw 로그는 켠 뒤 된다.
- **`project_unavailable`**(MCP; manager 쪽 도구에서는 `inbox_unavailable:<이유>`): 그 프로젝트의 `.wb/inbox.sqlite3`를 읽지 못한다. 덮어쓰지 않으며 다른 프로젝트는 영향이 없다.
- **옛 backend**: `backend_too_old`가 보이면 `wb update`로 backend를 새 버전으로 다시 시작한다.
- 웹 로그는 `hub.log`(Hub 사용 안내의 위치 표)에 있다.

## 11. 알려진 한계: 대상 host에서 확인할 것

개발 host에서는 검증하지 못한 항목이다. 처음 쓰는 컴퓨터에서 한 번 확인한다.

1. 실제 브라우저(Firefox·Chromium·휴대폰)에서 page·확인 page·badge·xterm.js 렌더링, tab 닫기·강제 종료·다른 브라우저 인수의 체감 동작. `/board` 응답만 xterm DOM renderer 때문에 `style-src 'self' 'unsafe-inline'`을 쓴다(`script-src`는 `'self'` 그대로). 다른 page는 inline style을 허용하지 않는다.
2. LAN/VPN 주소 bind와 다른 기기 접근, 자체 서명 인증서의 브라우저 신뢰 흐름.
3. GPU 안내: 실제 `nvidia-smi`와 실제 모델의 "계속할까요?" 응답. 개발 host에는 NVIDIA GPU가 없다.
4. 실제 OMP·실제 모델로 Board agent가 Hub MCP tool을 쓰고 manager가 검토하는 전체 흐름(유료 provider).
5. Codex CLI의 MCP Streamable HTTP 호환성. 위 예시는 미검증이다.
6. 실제 `systemd --user`에서 `KillMode=process` unit과 재부팅 뒤 시작, linger.
7. 온라인 dependency advisory 확인은 **실행하지 않았다**(`not_run`). 고정 버전과 license 기록은 [DEPENDENCY-AUDIT](DEPENDENCY-AUDIT.md)에 있고, 알려진 취약점이 없다는 증명이 아니다.

그 밖의 한계: 다중 사용자·OAuth·외부 identity 없음, 파일 첨부 없음, rate limit 없음, 웹에서의 manager·worker·host pane 조작 없음, 외부 알림 없음, 하나의 Board agent 세트에 한 번에 한 연결.

관련 문서: [BRIEF](BRIEF.md) · [SPEC](SPEC.md) · [OMP MCP probe](PROBE-OMP-MCP.md) · [ADR-0003](../../adr/0003-board-agent-without-sandbox.md) · [ADR-0004](../../adr/0004-auto-execution-after-manager-review.md) · [ADR-0005](../../adr/0005-per-project-inbox-store-hub-single-writer.md) · [Hub 사용 안내](../workbench-hub/USAGE.md) · [README](../../../README.md)
