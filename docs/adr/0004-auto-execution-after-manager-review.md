# ADR-0004: 출처와 무관하게 manager 검토 뒤 상시 위임으로 자동 실행한다

상태: 사용자 결정 수용, 2026-10-25. 문서 전체 승인과 구현은 별개다.
근거: [Task Inbox & Web Board BRIEF](../features/task-inbox/BRIEF.md) r0.3의 D-06, D-12, D-15, D-16, D-17. [Core 결정 이력](../features/core-workbench/DECISIONS.md) C-D66.

## 배경

요청은 세 출처에서 온다: 웹의 소유자, Hub의 Board agent, API key로 식별되는 외부 agent. 세 출처 모두 소유자 한 사람의 것이다(D-06, HB-D02).

[관측 사실] Core는 C-D66에 따라 manager의 `to_worker` 지시를 UI 승인 없이 상시 위임으로 실행한다. Backend가 `scope_approved`와 `proceed`를 기록하고 바로 전달한다(`src/workbench/backend/flow_tasks.py:967-986`). AGENTS.md와 PRD는 "외부 요청 접수는 실행 승인이 아니다"를 유지한다.

외부 key 요청을 실행하기 전에 소유자가 웹에서 승인해야 하는지가 결정할 점이었다.

## 결정

- 요청의 접수는 실행이 아니다. 모든 요청은 먼저 manager가 검토한다. 검토 결과는 manager 전용 검토 결과 도구로만 기록되며, 승인 명세 연결만 요청을 READY로 만든다.
- 승인 명세가 연결된 요청은 출처(소유자 웹, Board agent, 외부 key)와 무관하게 C-D66 상시 위임으로 소유자의 별도 승인 없이 실행된다.
- 출처(소유자 / Board agent / key 이름)는 웹 목록·상세와 MCP 응답에 항상 표시한다. [2026-10-28 개정: MCP는 없어졌고 "MCP 응답"은 외부 요청 API(`/api/v1/` REST) 응답으로 읽는다. [web-board-ux](../features/web-board-ux/BRIEF.md) WBX-D05, [ADR-0006](0006-rest-skill-single-external-contact.md)]
- Rate limit은 두지 않는다.
- 어떤 API key도 승인 명세 연결, terminal 열기, backend 제어를 할 수 없다(P2-AC-13 유지).
- 사용자는 외부 key 요청에 소유자 승인을 요구하는 안 대신 이 안을 명시적으로 골랐다.

## 검토한 대안

| 대안 | 판단 |
|---|---|
| 외부 key 요청만 소유자 웹 승인 | 채택하지 않는다. 모든 client가 소유자의 agent이므로 승인 대기가 소유자 자신의 위임을 막는다. 사용자가 명시적으로 거절했다. |
| 모든 요청에 소유자 승인 | 채택하지 않는다. C-D66이 없앤 건별 UI 승인을 다시 들이고, 부재 중 자동 진행이라는 목적에 맞지 않는다. |
| Manager 검토 뒤 상시 위임 자동 실행 + 출처 표시 | 채택한다. Core의 승인 모델과 같고, 판단 주체는 manager로 유지된다. |

## 결과

- P2-AC-07("외부 요청은 실행 승인이 아니다")은 유지되지만 의미가 좁아진다. 접수·client 호출만으로는 실행되지 않으며, manager 검토가 승인 명세를 만든 뒤에는 소유자 확인 없이 실행된다.
- Manager가 요청 내용을 잘못 판단하면 그대로 실행된다. 안전장치는 C-D66의 기존 장치(worker 동시 한 작업, 깨끗한 host prompt에서만 실험 시작, pause/resume, 재시도 3회, 기록)와 출처 표시, 요청 취소(D-20)다.
- Rate limit이 없으므로 한 key가 많은 요청을 내면 요청은 순서대로 쌓인다. 선점은 없다(P2-AC-10). 검토 비용(manager turn·token)은 요청 수에 비례한다.
- API key가 새면 그 key의 scope 안에서 실행 가능한 요청을 낼 수 있다. 폐기·만료·마지막 사용 시각 표시가 대응 수단이다.
- 다중 사용자 계정을 도입하면 이 결정을 다시 검토해야 한다.

## 검증 요구

- 접수 직후 요청이 manager 검토 전에 worker 전달·terminal 실행·승인 상태가 되지 않는지 확인한다.
- editor·requester key로 승인 명세 연결을 시도하면 거절되는지 확인한다.
- 세 출처의 요청이 검토 뒤 같은 경로로 실행되고 출처가 표시되는지 확인한다.
