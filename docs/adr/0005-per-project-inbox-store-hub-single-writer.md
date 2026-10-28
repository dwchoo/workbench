# ADR-0005: 요청 저장소는 WB 프로젝트별로 두고 Hub가 유일한 writer다

상태: 사용자 결정 수용, 2026-10-25. 문서 전체 승인과 구현은 별개다.
근거: [Task Inbox & Web Board BRIEF](../features/task-inbox/BRIEF.md) r0.3의 D-13, D-14. [Hub BRIEF](../features/workbench-hub/BRIEF.md) HB-D04, HB-D06. [설치 BRIEF](../features/workbench-installation/BRIEF.md) WI-AC-03, WI-D03.

## 배경

Hub 하나가 여러 WB 프로젝트의 요청을 받는다. 요청은 WB 프로젝트가 꺼져 있어도 접수되어야 하고(P2-AC-04), Hub가 꺼져도 core는 영향이 없어야 한다(P2-AC-01).

[기존 계약] 프로젝트별 작업 이력·DB·로그는 각 `.wb/`에 두고 프로젝트 A와 B의 이력을 합치지 않는다(WI-AC-03). Hub registry는 위치와 project_id만 기억하고 등록 해제는 `.wb/`를 지우지 않는다(HB-D06).

요청에 쓰는 주체는 여럿이다: 웹의 소유자, MCP client, Board agent, 그리고 검토 결과를 기록하는 각 WB 프로젝트의 backend(manager 도구). [2026-10-28 개정: MCP는 없어졌고 "MCP client"는 외부 요청 API(`/api/v1/` REST)를 쓰는 외부 agent(`wb-inbox` skill)로 읽는다. Hub가 유일한 writer라는 결정은 그대로다. [web-board-ux](../features/web-board-ux/BRIEF.md) WBX-D05, [ADR-0006](0006-rest-skill-single-external-contact.md)]

## 결정

- 요청 저장소는 WB 프로젝트마다 `<root>/.wb/inbox.sqlite3` 하나다.
- Hub가 모든 inbox 저장소의 유일한 writer다. Backend와 Board agent는 직접 쓰지 않고 Hub를 거친다.
- Hub가 꺼지면 Inbox(접수·조회·검토 결과 기록)는 쓸 수 없다. WB 프로젝트의 core 동작은 영향을 받지 않는다.
- WB 프로젝트가 꺼져 있어도 Hub는 그 프로젝트의 inbox에 접수하고 조회한다. 요청은 NEW로 쌓이고 프로젝트가 켜지면 순서대로 처리된다.

## 검토한 대안

| 대안 | 판단 |
|---|---|
| Hub 중앙 DB 하나 | 채택하지 않는다. 프로젝트별 이력 분리(WI-AC-03)와 WB 프로젝트의 이동·보관 가능성을 깨뜨린다. 등록 해제·재등록 시 요청 이력이 프로젝트와 따로 논다. |
| 프로젝트별 저장소 + 여러 writer(backend도 직접 기록) | 채택하지 않는다. Hub와 backend가 같은 SQLite에 동시에 쓰면 쓰기 순서·상태 전이 판정이 두 곳에 흩어진다. |
| 프로젝트별 저장소 + Hub 단일 writer | 채택한다. 이력은 프로젝트와 함께 있고, 상태 전이와 순서는 한 곳에서 정한다. |

## 결과

- 요청 이력은 WB 프로젝트의 `.wb/`와 함께 보관·이동된다. 등록 해제 뒤 다시 등록하면 요청 이력도 이어진다.
- Manager의 검토 결과 기록은 Hub가 살아 있어야 한다. Hub가 없을 때 manager 도구가 어떻게 실패를 보이고 다시 시도하는지는 SPEC에서 정한다. 불명확한 실행을 자동 재전송하지 않는 원칙(P2-AC-11)은 유지한다.
- 여러 프로젝트를 가로지르는 검색(Board agent, 웹 목록)은 Hub가 각 inbox를 읽어 모은다. 큰 규모에서의 비용은 미확인이다.
- `.wb/` 안에 새 파일이 생기므로 WB 상태 이관·백업 범위에 inbox가 포함되는지 SPEC에서 확인한다.
- 같은 inbox를 두 Hub가 쓰지 않도록 OS 사용자당 Hub 하나(HB-D05)에 의존한다.

## 검증 요구

- 프로젝트 A·B의 요청이 각 `.wb/inbox.sqlite3`에만 저장되고 섞이지 않는지 확인한다.
- Backend·Board agent가 inbox 파일에 직접 쓰지 않는지 확인한다.
- 프로젝트가 꺼진 상태의 접수, Hub 정지 중 core 동작, Hub 재시작 뒤 요청 보존을 확인한다.
