# WBX-B7: MCP client를 쓰는 다른 테스트를 REST로 이전

명세: [../SPEC.md](../SPEC.md) s2. 의존성·자원·gate: [../PLAN.json](../PLAN.json)의 `WBX-B7`. 상태·실제 assignment는 orchestrator 운영 기록에서만 관리한다.
권장 executor: Sonnet(`model: "sonnet"`). 기존 테스트의 기계적 이전이다.

## 제공할 동작

`McpHub`·`McpClient`·`mcp` import를 쓰는 테스트를 REST로 옮긴다: `test_inbox_results.py`, `test_inbox_results_independent.py`, `test_inbox_cancel_fingerprint_independent.py`, `test_inbox_cancel_independent.py`, `test_inbox_flow_cancel.py`, `test_inbox_untrusted_independent.py`, `test_web_hangul.py`, `test_hub_web_allow_from.py`(`_RecordingMcp`), `tests/integration/live_inbox_final_independent.py`. Board 관련(`test_board_*`, `test_web_pages_board`, `test_hub_web_reload`, `test_hub_web_enabled_independent`)은 WBX-B5 몫이다.

## 수락 기준

- WBX-AC-12(테스트 부분): 위 파일에 `mcp` import·`/mcp` 호출이 없고 같은 동작을 REST로 검증해 GREEN.

## 계약과 맥락

선행: WBX-B1. 자원: `tests/hub/support/**` 공유(읽기), 파일 쓰기 범위는 위 목록.

## 검증

S2·S3(확정, WBX-D20). `live_inbox_final_independent.py`는 실제 Hub·backend live라 통합 단계에서 한 번 실행.
명령: `python3 tools/shm_tests.py -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/hub -p 'test_inbox_*.py'`, `-p 'test_web_hangul.py'`, `-p 'test_hub_web_allow_from.py'`.
격리 실행 주의: 실제 Hub·OMP·isolation home을 띄운다. CLAUDE.md "테스트 실행"의 sandbox 규칙(짧은 private `XDG_RUNTIME_DIR`, temp HOME/XDG_STATE_HOME, `WORKBENCH_HUB_AUTOSTART=0`, 실행 전후 `ls /tmp/omp-workbench-<uid> | wc -l` 동일)을 지킨다. Checkpoint·hash manifest exclude: `graphify-out/`, `.workflow/`, `.worktrees/`, `.venv/`, 격리 home·캐시(`*/.omp/natives/`, Board home). 전후 `du -sh .workflow graphify-out .worktrees`를 보고한다.

## 범위 밖과 중단 조건

Board 테스트, MCP 테스트 삭제. 테스트 약화 금지.

## 출력

파일별 변경 요약, 명령·결과.
