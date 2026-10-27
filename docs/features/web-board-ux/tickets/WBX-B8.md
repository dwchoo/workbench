# WBX-B8: MCP 제거(contract)

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-B8`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Opus(`model: "opus"`). 여러 module·테스트·설정 문구에 걸친 제거와 route audit 경계다.

## 제공할 동작

SPEC "MCP 제거 순서" 3단계: `src/workbench/hub/mcp.py` 삭제(남은 transport), `Hub.mcp`·`internal_mcp`·`_mcp_projects` 등 이름 정리, `server.web_routes()`의 `/mcp`, `middleware`의 `/mcp` 언급·`ACCESS_OWN` 설명, `config.py` 템플릿의 `/mcp` 문구, `web_control.py`·`inbox/backend_port.py`·`contracts/hub_v1.py` 주석, `tests/hub/support/mcp_client.py`·`mcp_hub.py`, `tests/hub/probes/omp_mcp_probe.py`, `test_hub_mcp*.py`. Route audit: `('*','/mcp')`·CSRF 예외 삭제. 유지: `test_web_assets`의 mcp SDK import 금지 검사, backend launcher의 manager·worker MCP 누출 검사.

## 수락 기준

- WBX-AC-12: 외부·내부 listener 모두 `/mcp` 404(S1·S3); `grep -rn "mcp" src/workbench/hub tests/hub`에 남는 것은 누출 검사·SDK 금지 검사·이력 주석뿐(목록을 출력에 첨부); 기존 key가 REST에서 동작(B1 테스트 재실행).
- 전체 `tests/hub` GREEN.

## 계약과 맥락

선행: WBX-B5, WBX-B6, WBX-B7.

## 검증

Seam은 SPEC s2 "테스트 결정"에서 사용자가 확정한 것이다(2026-10-27, WBX-D20·D21).
`/mcp` 404 behavior 한 개는 RED → 삭제 → GREEN. 나머지는 삭제라 기존 suite GREEN 유지로 증명.
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub`(디렉터리 전체, 통합 단계 1회), `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/backend -p 'test_omp_isolation*.py'`, `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/backend -p 'test_workbench_skills.py'`.
격리 실행 주의: 실제 Hub·OMP·isolation home을 띄운다. CLAUDE.md "테스트 실행"의 sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일)을 지킨다. Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시(`*/.omp/natives/`, Board home). 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

문서(WBX-B9). Backend의 manager·worker MCP 차단 설정을 건드리지 않는다.

## 출력

삭제 목록, 남은 `mcp` 문자열 목록과 이유, 명령·결과.
