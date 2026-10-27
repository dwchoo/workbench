# ADR-0006: 외부 접점은 REST API와 `wb-inbox` skill 하나로 하고 MCP를 없앤다

상태: 사용자 결정 수용, 2026-10-27. 문서 전체 승인과 구현은 별개다. [ADR-0003](0003-board-agent-without-sandbox.md)의 Board agent MCP 항목을 대체한다.
근거: [Web Board 사용성·외부 요청 API BRIEF](../features/web-board-ux/BRIEF.md) WBX-D05~D11. [사용자 PRD](../features/web-board-ux/USER-PRD-20261027.md) §4. 개정 대상: [Task Inbox & Web Board BRIEF](../features/task-inbox/BRIEF.md) D-08, D-14 제약 줄, D-22, D-25, D-29, D-33, D-36, P2-AC-33·41·45.

## 배경

Hub는 외부 agent와 Board agent에게 MCP Streamable HTTP의 tool 9개(`inbox_*`)를 제공한다. 외부는 웹 listener의 `/mcp`, Board agent는 Hub 내부 loopback listener의 `/mcp`를 쓴다(`src/workbench/hub/mcp.py`). 인증은 Bearer API key이고 등급(requester / editor / board_agent system key)·scope·한도·두 단계 취소가 적용된다.

사용자는 외부 agent가 "특별한 프로그램 없이" skill 하나로 요청을 등록하고, README 링크 하나로 설치하며, key를 env 파일로 관리하기를 원한다. MCP는 client마다 서버 등록 설정이 다르고, client 호환성은 개발 중 표준 라이브러리 테스트 client로만 검증했다(D-36). Codex CLI 호환은 대상 host 확인 항목으로 남아 있다.

[관측 사실] OMP는 SKILL.md skill을 지원하고, manager·worker는 이미 bash로 번들 script를 실행하는 skill을 쓴다. Board agent에는 bash가 있다(ADR-0003). Manager의 검토 결과 도구는 MCP가 아니라 bridge → backend → hub_v1 Unix socket이다.

## 결정

- MCP를 모두 없앤다. 외부 `/mcp`도, Board agent용 내부 `/mcp`도 두지 않는다. 이행 기간은 없다.
- 외부 접점은 `/api/v1/` REST JSON API 하나다. MCP tool 9개가 하던 일을 같은 내부 service로 제공하고, 등급·scope·한도·idempotency·두 단계 취소·결과 공개 범위(D-22)는 바꾸지 않는다. 인증은 `Authorization: Bearer <key>`뿐이며 기존 API key는 그대로 쓴다.
- Client는 `wb-inbox` skill이다: `SKILL.md`(Agent Skills 표준) + Python 3 표준 라이브러리 단일 script + `.env`(`WB_HUB_URL`, `WB_API_KEY`). Repo의 `skills/`에 두고, Hub가 같은 파일을 로그인 없이 `/skill/`로 제공한다.
- REST와 skill 제공은 웹과 같은 listener·bind·`allow_from`·`[web] enabled`를 따른다. 별도 허용 목록은 없다.
- Board agent도 같은 REST를 `wb-inbox` skill과 bash로 쓴다. Hub 내부 loopback listener가 `/mcp` 대신 REST를 제공하고, 시작마다 만든 system editor key를 env `WB_API_KEY`로 준다. Board agent에 허용하는 skill은 `wb-inbox` 하나이고 MCP tool은 하나도 없다.
- 전환 전에 OMP가 Board overlay 아래에서 skill을 읽고 bash로 REST를 호출하는지 probe로 확인한다. 실패하면 전환하지 않고 사용자에게 묻는다.

## 검토한 대안

| 대안 | 판단 |
|---|---|
| MCP 유지 + REST 추가 | 채택하지 않는다. 같은 기능의 외부 접점이 둘이 되어 권한·한도·오류 대응과 테스트를 두 벌 유지한다. 사용자의 목표(설치가 간단한 skill 하나)는 REST만으로 충족된다. |
| 외부는 REST, Board agent에만 MCP를 내부 통로로 유지 | 지금은 채택하지 않는다. Board agent와 외부 agent가 다른 경로를 쓰면 Board agent가 실제 사용자 경로를 검증하지 못하고 MCP 구현·테스트가 남는다. Probe(WBX-AC-17)가 실패할 때의 후보로만 남기며 결정하지 않았다. |
| MCP 제거, REST + skill 단일 접점(Board agent 포함) | 채택한다. 외부 agent·Board agent가 같은 경로와 같은 client를 쓰고, MCP client 호환 문제가 사라진다. |

## 결과

- (A)의 MCP 관련 결정·수락 기준은 문구가 REST로 바뀌고 의미(등급, 두 단계 취소, 한도, 결과 범위)는 유지된다. 기존 문서에는 개정 주석만 단다.
- Board agent의 설정 노출 범위는 더 좁아진다: MCP가 없고 skill은 `wb-inbox` 하나다. 이 제한은 여전히 OS 권한 경계가 아니다(ADR-0003). Board agent의 system key는 env에 있으므로 같은 사용자 권한의 bash로 읽을 수 있다. 이는 ADR-0003이 이미 받아들인 위험 범위 안이다.
- Board agent는 내부 loopback listener를 쓰므로 `allow_from`에서 loopback을 빼도 끊기지 않는다.
- `/skill/`은 로그인 없이 열린다. 내용은 공개 가능한 skill 파일과 Hub 주소뿐이고 key는 없다. 노출 범위는 `allow_from`·bind·`[web] enabled`가 정한다.
- MCP 테스트 약 190개를 REST 테스트로 옮겨야 한다. `mcp.py`, MCP 테스트 client, OMP MCP probe는 제거 대상이다.
- Codex CLI 등 client 호환 확인은 MCP 설정 대신 skill 사용 확인이 된다. 여전히 대상 host 확인 항목이다.
- Skill의 env 이름 `WB_API_KEY`·`WB_HUB_URL`을 Board agent도 쓰므로, Hub가 사용자 shell에서 같은 이름의 값을 물려받지 않게 하고 Board 세트의 일반 shell에 system key가 보이지 않게 하는 규칙을 SPEC에서 정한다.

## 검증 요구

- 외부·내부 listener 모두 `/mcp`가 없고, REST로 MCP tool 9개의 동작(등급별 허용·거절, idempotency, 두 단계 취소, 한도, 결과 범위)이 같게 나오는지 확인한다.
- `allow_from` 밖·bind 밖·`[web] enabled = false`에서 REST와 `/skill/`이 열리지 않는지 확인한다.
- Board agent의 tool 목록에 MCP tool이 없고 skill이 `wb-inbox` 하나뿐인지, 다른 skill이나 MCP가 보이면 시작이 거절되는지, manager·worker의 기존 MCP 누출 검사가 계속 통과하는지 확인한다.
- `wb_inbox.py`가 key를 출력·기록하지 않는지 확인한다.
