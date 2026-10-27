# WBX-B9: MCP → REST·skill 문서 갱신

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-B9`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). 문서 작업이다.

## 제공할 동작

SPEC "문서 갱신 배정"의 WBX-B9 행: task-inbox SPEC("API key", "MCP Streamable HTTP endpoint" → "외부 요청 API(REST)" 절과 이 SPEC 링크, "Board agent 세트", `allow_from` 설명, route 표의 `/mcp` 줄), task-inbox USAGE(머리, §1.2, §4 → skill 설치·`.env`, §7.4, §10.2), workbench-hub SPEC·USAGE의 MCP 문구, ADR-0004:18·ADR-0005:12 개정 주석, CONTEXT.md의 "(WBX 트랙 B 완료 전에는 MCP가 현행)" 표시 제거, `README.md`의 MCP 언급. BRIEF·PROBE-OMP-MCP·TI ticket은 고치지 않는다(이력, 개정 주석만 이미 있음).

## 수락 기준

- WBX-AC-12의 문서 부분: 위 문서에 현재 동작으로서의 MCP 안내가 없고 skill 설치 안내가 있다.

## 계약과 맥락

선행: WBX-B8. 한국어 문서(skill 문서는 B2에서 영어).

## 검증

TDD 예외(문서). 정적 확인: `grep -n "/mcp\|MCP" <대상 문서>`의 남은 줄이 이력·개정 주석뿐. 링크 확인.

## 범위 밖과 중단 조건

BRIEF 원문 수정. 새 결정 추가.

## 출력

문서별 변경 요약, 남은 MCP 언급 목록.
