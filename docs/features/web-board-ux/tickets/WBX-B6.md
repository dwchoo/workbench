# WBX-B6: MCP 핵심 테스트를 REST로 이전

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-B6`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). 기존 테스트의 기계적 이전이다.

## 제공할 동작

`tests/hub/test_hub_mcp.py`, `test_hub_mcp_independent.py`, `test_hub_mcp_transport.py`, `test_hub_mcp_hub.py`가 검증하던 동작을 `test_hub_rest*.py`(WBX-B1이 만든 것과 합치거나 별도 파일)로 옮긴다. JSON-RPC 고유 동작(initialize, session id, batch, notification, protocol version)은 REST 대응이 없으므로 대응표에 "REST 해당 없음"으로 적고 옮기지 않는다. 등급·scope·한도·idempotency·cancel·result·untrusted·오류 비노출 등 inbox 동작은 모두 옮긴다. MCP 테스트 파일은 이 ticket에서 지우지 않는다(WBX-B8).

## 수락 기준

- WBX-AC-12(테스트 부분): 대응표(새 문서 파일을 만들지 않고 ticket 출력에 둔다)의 모든 MCP test가 REST test로 대응되거나 "해당 없음" 이유가 있다. REST test 전부 GREEN.
- WBX-AC-07·08·09·10의 회귀 범위 확대.

## 계약과 맥락

선행: WBX-B1. `support/rest_client.py`, `support/rest_hub.py`. 테스트를 약화·skip하지 않는다.

## 검증

S2(확정, WBX-D20; 기존 T4의 후계). 이전은 기존 기대값을 그대로 쓴다(독립 기대값의 출처는 원 테스트의 SPEC 인용). 이전한 테스트가 실제로 실패할 수 있는지 표본 3개를 일시 변형으로 확인하고 되돌린다.
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_hub_rest*.py'`.

## 범위 밖과 중단 조건

다른 suite(WBX-B7). Product 코드 변경이 필요하면 멈추고 보고(WBX-B1 결함).

## 출력

대응표, 이전한 test 수, 명령·결과.
