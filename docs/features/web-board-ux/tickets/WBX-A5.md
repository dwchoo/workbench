# WBX-A5: 사용자 Chrome 확인(WBX-O01)

명세: [../SPEC.md](../SPEC.md) s3. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-A5`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: 사용자(관찰) + orchestrator 기록. Agent가 대신할 수 없는 실제 browser 확인이다.

## 제공할 동작

사용자가 실제 Chrome(평문 HTTP, LAN 주소)에서 Workbench의 자동 갱신 없는 화면 사이를 오가며 뒤로 가기 경고가 매번 뜨는지 확인한다. Orchestrator가 결과를 `docs/features/web-board-ux/CHECK-CHROME.md`(새 파일)에 기록한다: 날짜, Chrome 버전, "Always use secure connections" 설정, 주소 형태, 오간 화면 순서, 경고 횟수, `/`(meta refresh가 남아 있으면) 따로 표시. 같은 기회에 배너 접힘 기억(WBX-AC-05), 복사 버튼(WBX-AC-06), 좁은 화면 nav(WBX-AC-01), 로그아웃 뒤 뒤로 가기(WBX-AC-03)를 함께 본다.

## 부분 결과(2026-10-28, 사용자 관찰, 평문 HTTP LAN 주소의 Hub)

| 항목 | 결과 |
|---|---|
| 배너 닫기 → "평문 HTTP" chip(WBX-AC-05) | 동작 |
| 좁은 화면 nav 접힘(WBX-AC-01) | 동작 |
| 디자인(트랙 C, mockup A) | "나쁘지 않아" — 수락 |
| Dashboard `/`로 뒤로 가기(WBX-AC-04) | 매번 Chrome 비보안 연결 경고 → WBX-D29(`/dashboard`)·WBX-A6 |
| 로그아웃 뒤 뒤로 가기(WBX-AC-03) | 이전 로그인 화면이 보이고 남음(실패) → WBX-D34·WBX-A6 |
| `/skill/README.md`로 agent 설치 | `SKILL.md`·`wb_inbox.py`를 가져오지 못함 → WBX-D30·D35·WBX-B11(대상 host 확인은 I1 `pending`) |
| Chrome "Always use secure connections" 설정 | 보고 안 됨 |
| 복사 버튼(WBX-AC-06) | 보고 안 됨 |

남은 확인: (1) WBX-A6 통합 뒤 `/dashboard`·자동 갱신 없는 화면 사이 뒤로 가기 경고와 로그아웃 → 뒤로 가기 재확인, (2) Chrome 설정 상태, (3) 복사 버튼, (4) WBX-B12·B4 재실행 뒤 `/board`(Board agent 화면) 확인. 이 부분 결과는 `CHECK-CHROME.md`를 만들 때 그대로 옮긴다.

## 결과(2026-10-29, 사용자 확인, 평문 HTTP LAN 주소의 Hub)

| 항목 | 결과 |
|---|---|
| Dashboard 표가 화면 폭에 맞고 좁은 화면에서 card로 바뀜(WBX-AC-20, commit `cf67e15` 뒤) | 문제없음 |
| 로그아웃 → 뒤로 가기에서 이전 화면이 보이지 않음(WBX-AC-03 보충 2, WBX-A7) | 문제없음 |
| `/board`의 Board agent가 REST + `wb-inbox` skill로 동작(WBX-AC-16 실제 OMP·모델) | 문제없음 |
| Chrome "Always use secure connections" 설정(WBX-AC-04) | 사용자 답 "문제없음" |
| 실제 agent의 skill 설치(WBX-AC-22 대상 host) | 사용자가 미룸(BRIEF WBX-O07) |
| 같은 기회의 새 관찰: 한글 IME "안녕하세요" → "녕녕녕녕녕안." | WBX-D41·WBX-A8 |

남은 것: 복사 버튼(WBX-AC-06) 보고 없음, 자동 갱신 없는 화면 사이 뒤로 가기의 명시 기록, skill 설치(미룸). `CHECK-CHROME.md`는 아직 만들지 않았다(만들 때 2026-10-28·29 결과를 그대로 옮긴다).

## 수락 기준

- WBX-AC-04: 효과가 있으면 그 기록, 없으면 알려진 한계로 기록하고 USAGE(WBX-A2가 쓴 항목)가 TLS를 근본 해결로 안내함을 확인.

## 계약과 맥락

선행: WBX-A1·A2·A3 통합, 재확인은 WBX-A6과 WBX-B4(재실행, ← WBX-B12) 뒤. 시점은 트랙 A 뒤(WBX-D22). 트랙 C(WBX-C2)로 `/`의 meta refresh는 없어졌고 dashboard는 `/dashboard`다(WBX-D29). 뒤로 가기 확인은 자동 갱신이 없는 화면(요청 목록 `/requests`, 요청 상세, 설정·key 목록, 프로젝트 상세 등)과 `/dashboard` 사이에서 하고 `/dashboard`는 따로 표시한다.

## 검증

증거 수준 runtime(사용자 관찰). 자동 테스트 없음(TDD 대상 아님).

## 범위 밖과 중단 조건

결과에 따라 새 기능을 더하지 않는다(WBX-D16). 효과가 없으면 사용자에게 다음 선택을 묻는다.

## 출력

`CHECK-CHROME.md`, 관찰 요약.
