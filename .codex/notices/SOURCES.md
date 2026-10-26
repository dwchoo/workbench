# 출처와 라이선스 고지

이 workflow는 mattpocock/skills를 참고해 이미 우리 방식으로 조정된 v4.1을 계승합니다.
Astra의 BRIEF 합의, 새 Sol의 단일 to-tickets, Root의 배분·통합, 독립 Terra 테스트와
Sol 리뷰, Root가 중계하는 Oracle 및 안내 전용 ask-workflow를 유지합니다.
원문으로 전면 교체하거나 upstream 설치를 의존성으로 추가하지 않습니다.

- 원 repository: https://github.com/mattpocock/skills
- 확인한 source revision: c55ee46073ed923f86ce59a5eb3b6d895095d1b7
- 참고 범위: grilling, grill-with-docs, domain-modeling, to-spec, to-tickets, ask-matt.
- implement, tdd, code-review와 직접 참조는 2026-09-22 추가 조사 기준선이며 전체 채택 주장이 아닙니다.
- 원 저작권: Copyright (c) 2026 Matt Pocock
- 원 허가문: [MIT LICENSE](mattpocock-skills-LICENSE.txt)

기존 v4.1에 기록된 여섯 source blob을 보존하며 과거 전체 commit은 미확인입니다.
조사한 revision과 실제 항목별 채택 revision은 별개입니다.
OpenAI 공식 문서는 설계 참고이며 위 MIT 라이선스를 적용하지 않습니다.
프로젝트 자체의 라이선스를 이 고지로 정하지 않습니다.

## investigate-bug

M16 diagnosing-bugs at `c55ee46073ed923f86ce59a5eb3b6d895095d1b7` is adapted for explicit user invocation,
Root-managed experiments, optional requested repair and independent verification.
Source: https://github.com/mattpocock/skills/blob/c55ee46073ed923f86ce59a5eb3b6d895095d1b7/skills/engineering/diagnosing-bugs/SKILL.md
M17 HITL script was inspected and excluded; it is not installed or executed.
Astra Root/senior guidance is a local user decision, not upstream model policy.
The existing Matt Pocock MIT notice applies to the adapted material.

## Handoff and operational helpers

Purpose-specific handoff and canonical references adapt productivity/handoff and the Context
pointers section of productivity/writing-for-agents from mattpocock/skills at
`c55ee46073ed923f86ce59a5eb3b6d895095d1b7`. The existing Matt Pocock MIT notice applies.
Sources: https://github.com/mattpocock/skills/blob/c55ee46073ed923f86ce59a5eb3b6d895095d1b7/skills/productivity/handoff/SKILL.md
and https://github.com/mattpocock/skills/blob/c55ee46073ed923f86ce59a5eb3b6d895095d1b7/skills/productivity/writing-for-agents/SKILL.md
Project persistence, explicit authority reconciliation and all ledger/evidence/validator/PTY
algorithms are local design and implementation. OpenAI and Anthropic articles are design
references, not MIT-licensed source code or evidence of runtime effectiveness.

## 행동별 TDD와 architecture

2026-10-18 사용자가 선택한 개발 프로세스는 mattpocock/skills의 고정 commit
`49dd158d1076134a641b33efb035946536778336`에서 TDD·tests·mocking, grilling/domain-modeling,
code-review와 improve-codebase-architecture/HTML-REPORT, codebase-design/DEEPENING을 변형한다.
위 Matt Pocock MIT 저작권·허가문을 이 자료에도 유지한다.

- `skills/engineering/tdd/{SKILL.md,tests.md,mocking.md}`
- `skills/productivity/grilling/SKILL.md`, `skills/engineering/domain-modeling/SKILL.md`
- `skills/engineering/code-review/SKILL.md`
- `skills/engineering/improve-codebase-architecture/{SKILL.md,HTML-REPORT.md}`
- `skills/engineering/codebase-design/{SKILL.md,DEEPENING.md}`

각 원문은 위 commit의 [저장소](https://github.com/mattpocock/skills/tree/49dd158d1076134a641b33efb035946536778336)에서 확인한다.
Root dispatch는 같은 commit의 `skills/engineering/implement-spec/SKILL.md`를 참고한다.
실제 파일별 채택은 provenance에 별도로 기록하며 조사·등록만 된 항목을 채택으로 표시하지 않는다.
Codex metadata·Root-only 위임·사용자 승인·로컬 티켓·설치 호환성은 프로젝트의 변형이다.
DESIGN-IT-TWICE의 별도 병렬 설계 과정과 retro는 이번 설치에 포함하지 않는다.
