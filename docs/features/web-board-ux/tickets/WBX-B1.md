# WBX-B1: 외부 요청 API `/api/v1/`(expand)

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-B1`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Opus(`model: "opus"`). API key 인증 경계, 두 단계 취소 token, 여러 module에 걸친 처리 계층 이동이다.

## 제공할 동작

0단계(순수 이동, RED 없음): `mcp.py`의 처리 계층(principal_for, bearer_key, 인자 검사, `error_payload`, untrusted 표시, cancel token store, 결과·프로젝트 모양, 전용 executor·key당 자리)을 `src/workbench/hub/inbox_api.py`로 옮기고 `mcp.py`는 JSON-RPC transport만 남긴다. 기존 `test_hub_mcp*.py`가 그대로 GREEN이어야 한다.
1단계 이후(TDD): `src/workbench/hub/rest.py` — SPEC "외부 요청 API"의 endpoint 9개, 인증, body·query 규칙, 오류 → status 표, Origin, 동시성, catch-all 404·405, `/api/v1/` 경로의 middleware `refuse()` REST 오류 모양. `Hub.web_routes()`에 등록(이 시점에는 `/mcp`도 남는다). 내부 listener용 app factory는 아직 바꾸지 않는다(WBX-B5). 테스트 지원: `tests/hub/support/rest_client.py`(stdlib `http.client`), `tests/hub/support/rest_hub.py`(`mcp_hub.McpHub`의 구성을 REST로; 두 프로젝트, 주입 clock, 가짜 cancel port).

## 수락 기준

- WBX-AC-07: endpoint 9개가 MCP tool과 같은 기록·권한 판정·출처(`origin`)를 낸다(같은 입력을 REST와 MCP로 보내 store 행·응답 field 비교, 이 ticket 동안만 가능). Idempotency 재제출 200·같은 id, 다른 내용 409 `idempotency_conflict`, 새로 만들면 201+`Location`.
- WBX-AC-08: Bearer만; 없음·틀림·폐기·만료 401+`WWW-Authenticate`; cookie만 있으면 401; requester의 PATCH·state 403. Route audit에 REST route가 `own`으로 pin되고 terminal·link·backend 제어·검토 시작 endpoint가 없다.
- WBX-AC-09: 오류 모양·code·status 표 전부, 한도 초과 거절·저장 없음(제목·본문·댓글·body 256 KiB·limit·length).
- WBX-AC-10: RUNNING 첫 호출 token+impact(취소 안 함), 맞는 token → cancel port 1회, 없음·틀림·만료(주입 clock)·다른 key·다른 요청·fingerprint 변경 → 409 `token_mismatch`·port 호출 없음.
- WBX-AC-11(REST 부분): `allow_from` 밖 peer 403 본문 없음(S1, 기존 `test_hub_web_allow_from` 방식), `[web] enabled=false`면 REST 없음(S3, `WebSandbox`).
- WBX-AC-12(key 부분): 이 ticket 전에 발급된 key(store에 직접 만든 행)가 REST에서 같은 등급·scope.

## 계약과 맥락

SPEC "외부 요청 API", task-inbox SPEC "Principal과 권한"·"입력 한도"·"취소"·"결과 조회". 기존 `mcp.py`, `inbox/service.py`, `inbox/results.py`, `web/middleware.py`, `web/app.py`, `hub/server.py`, `tests/hub/test_web_route_audit.py`(EXPECTED에 REST route 추가). 새 의존성 없음.

## 검증

Seam은 SPEC s2 "테스트 결정"에서 사용자가 확정한 것이다(2026-10-27, WBX-D20·D21).
Seam: S2(주), S1(transport·route audit·allow_from), S3(enabled=false 한 사례). S2·S1은 기존 T4·T2 재사용.
Behavior 순서: (0) 이동 뒤 `test_hub_mcp*.py` GREEN (1) 401 4종·cookie 무시 (2) Origin 403·Host 421 (3) method 405·415·413·400 strict JSON·모르는 field (4) `GET /projects` 등급별 (5) `POST /requests` 201/200/409·한도 (6) list·get(scope·requester 범위·cursor) (7) comment(NEEDS_INFO → REVIEWING) (8) PATCH·state(editor만) (9) cancel 실행 전·READY·RUNNING 2단계·불일치 (10) result 요약·full·raw/artifact page (11) 오류 정규화(내부 문장 비노출) (12) busy 503 (13) catch-all 404 (14) route audit pin (15) allow_from·enabled=false.
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_hub_rest*.py'`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_web_route_audit.py'`; 0단계 안전망 `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_hub_mcp*.py'`; 회귀 `-p 'test_hub_web_*.py'`, `-p 'test_inbox_*.py'`.
독립 test_designer(Sonnet) 필수: 시나리오를 SPEC에서 먼저 도출. 초점: cookie+Bearer 혼합, token 재사용·다른 key, 오류에 경로·stack 없음, JSON 깊이·큰 숫자·잘못된 UTF-8, Content-Length 거짓.
격리 실행 주의: 실제 Hub·OMP·isolation home을 띄운다. CLAUDE.md "테스트 실행"의 sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일)을 지킨다. Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시(`*/.omp/natives/`, Board home). 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

`/mcp` 삭제(WBX-B8), 내부 listener(WBX-B5), skill(WBX-B2). 처리 계층 이동 중 MCP 동작이 하나라도 바뀌면 멈추고 보고한다.

## 출력

변경 경로, 0단계 GREEN 근거, behavior별 RED/GREEN, endpoint·status 관측 표, 명령·결과, `rest_client` 사용법.
