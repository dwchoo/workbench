# Fixed briefing output template

Read this reference on every invocation. Use the five numbered headings, field labels,
column names and order below exactly for Korean output; translate labels only when the
user requests another language. Preserve the same structure in main and side chats.
Do not replace tables with prose, rename/reorder sections, or omit empty sections.
The content and row count vary with evidence; the structure does not.

Write for a reader who has never seen the project or its documents. Keep table cells short.
Explain consequences rather than implementation details. Start with what is being built,
for whom and why; introduce unfamiliar terms in ordinary language. Source links support
an explanation and never substitute for it. No requirement to read linked documents.

## Template

Render the following Markdown as the answer, not as a fenced code block. Replace the
bracketed slots with supported facts. Do not print unfilled slots in the actual report.

```markdown
## 1. 프로젝트와 현재 위치

**만드는 것:** [사용자·목적·개발 내용을 한두 문장으로 설명]
**현재 위치:** [현재 단계·실제로 진행 중인 작업·남은 주요 개발]
**결정 현황:** [필수 결정 수와 핵심 영향 / 지금 필요한 결정 없음 / 미확인 사유]
**확인 기준:** [확인 시각·대상·기록 revision, 중요한 접근·최신성 한계]

## 2. 전체 티켓 현황

| 티켓 · 개발 내용 | 현재 단계 | 남은 작업 | 이슈 |
|---|---|---|---|
| [ID · 사용자에게 제공할 기능] | [알기 쉬운 단계] | [미완료 개발·검증] | [없음 / 있음: ID·원인 / 미확인: 이유] |

## 3. 이슈 분류

| 구분 | 이슈 · 무엇이 문제인가 | 처리 방향 | 영향 |
|---|---|---|---|
| 에이전트 해결 가능 | [ID · 문제 / 해당 없음] | [수정 방법·확인할 검사 / —] | [영향받는 작업 / —] |
| 사용자 의견 권장 | [ID · 선택할 내용 / 해당 없음] | [추천·기존 기준 / —] | [답변 없이 가능한 작업 / —] |
| 사용자 결정 필수 | [ID · 필요한 결정 / 해당 없음] | [결정할 내용 / —] | [막히는 작업 / —] |
| 미확인 | [빠진 근거·분류할 수 없는 문제 / 해당 없음] | [필요한 확인 / —] | [알려진 영향·미확인 범위 / —] |

## 4. 결정 설명과 추천

**[이슈 ID — 일상적인 말로 쓴 결정 질문] · [필수 / 의견 권장]**

**상황:** [무슨 상황인가. 필요하면 짧은 예시 하나]
**결정이 필요한 이유:** [기존 합의로 판단할 수 없는 부분 또는 사용자 의견이 도움이 되는 이유]

| 선택지 | 선택하면 달라지는 점 | 장점 | 단점·부담 |
|---|---|---|---|
| [A · 의미가 드러나는 이름] | [실제 사용자가 겪을 동작] | [핵심 이점] | [핵심 부담] |
| [B · 의미 있는 다른 안] | [실제 사용자가 겪을 동작] | [핵심 이점] | [핵심 부담] |

**추천과 이유:** [추천안·프로젝트 목적에 맞는 이유·중요한 전제 / 추천 근거 부족: 필요한 정보]
**결정을 미루면:** [막히는 작업과 독립적으로 가능한 작업. 선택 사항이면 기존의 승인된 기준]
**답변할 내용:** [간단히 선택·응답할 수 있는 문장]

## 5. 다음 행동

| 담당 | 다음 행동 | 조건 |
|---|---|---|
| 개발 담당 에이전트 | [승인된 범위에서 가능한 다음 작업 / 없음 / 미확인] | [진행 조건] |
| 사용자 | [지금 필요한 결정·입력 / 지금 필요한 결정 없음] | [필요 시점·영향 / —] |
| 개발 담당 에이전트 | [답변 후 이어질 작업 / 추가 작업 없음 / 미확인] | [현재 요구·작업 영향 대조 등 필요한 조건 / —] |

**메인 채팅 전달문:** [사이드 채팅에서 전달할 초안 또는 해당 없음]
**근거:** [관련 기록의 짧은 링크·revision / 확인 가능한 기록 없음]
```

## Filling rules

- Section 1: two to four short explanatory sentences plus the observation fields. Never
  open with unexplained IDs or a document inventory. Verified ticket counts may be added
  to `현재 위치`; do not present them as effort, time or overall completion percentages.
- Section 2: include all canonical tickets once, including completed and final verification
  work; emphasize all currently active rows. With many tickets, repeat the same columns
  under milestone labels, without omission. Mention orphan files after the table as not
  reconciled with PLAN. With no plan, keep one row: `티켓 미생성` when established, or
  `티켓 목록 미확인` when inaccessible, followed by supported stage/remaining work/gaps.
  Missing status is unknown. Distinguish predecessor waits from defects, and implementation
  from verified integration. `없음` means no issue in inspected evidence; use `기록상 없음`
  when that is the limit. Preserve `미확인` when evidence is insufficient.
- Section 3: keep all four categories in the shown order. Repeat a category for each issue;
  otherwise use its `해당 없음` row. Missing evidence is not no issue. Use existing IDs;
  if absent, label new IDs as report-local. Do not create tickets or persist new records.
  Technical difficulty alone does not make a problem a required user choice. If an external
  fact or action is needed, name that instead of inventing decision options.
- Section 4: repeat the exact card fields for each required decision, then useful preference,
  with the same IDs as section 3. Usually two or three meaningful options; never invent a
  second option for a factual input. For that case put one row `필요한 입력·조치` describing
  what is needed, and retain the closing fields. If there are no decisions/input requests,
  retain the heading and write `지금 설명하거나 요청할 사용자 결정·입력은 없습니다.`
  If decision presence is unknown, state the missing evidence instead of that sentence.
  Keep each background field to one or two sentences, each trade-off to one main point,
  and recommendation to one or two sentences. Expand only for material consequences.
  Follow the host's allowed question channel; `답변할 내용` can state the expected answer
  format without asking a prohibited question or resubmitting an already pending request.
- Section 5: retain all three rows and both footer fields. Proposals are not executed actions.
  Recorded ongoing work is not confirmed live work. For the main-chat message use
  `해당 없음` if none is needed. In a side chat, an unanswered draft must say `미선택 초안`
  and include plain feature name / observed revision / issue ID / a choice placeholder,
  followed by `현재 작업과 결정의 영향을 확인한 뒤 반영해주세요.` Do not prefill a
  recommendation as a user choice. State that the draft has not been sent or applied.
  If the user has actually chosen, quote that choice accurately without implying delivery.

## Wording example, not project evidence

Do not write only "upsert vs skip; skip recommended". Explain that an imported customer
email may already exist: updating replaces old information; skipping preserves it and
reports the duplicate. Recommend based on whether the agreed feature only registers new
customers or also updates existing information. The reader should understand the choice
without knowing a database term or reading a specification. Never import this illustrative
issue into a real project's briefing.
