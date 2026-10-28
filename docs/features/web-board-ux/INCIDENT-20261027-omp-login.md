# INCIDENT-20261027: WBX-B4 probe 중 사용자 OMP 로그인 유실

작성 2026-10-28. 관련: [BRIEF.md](BRIEF.md) r0.3(WBX-D31~D33, WBX-AC-23), [PROBE-BOARD-SKILL.md](PROBE-BOARD-SKILL.md), [tickets/WBX-B12.md](tickets/WBX-B12.md), [core-workbench C-D64](../core-workbench/DECISIONS.md#C-D64). 인증 저장소의 내용은 열지 않았다(C-D64, 자격 증명 비노출). 아래 원인은 추정이며 확인되지 않았다.

## 증상

- 2026-10-27 15:40:33 WBX-B4 probe 1단계 첫 실행. 첫 격리 확인(`board_skill`)의 `get_login_providers`는 authenticated에 `anthropic`, `openai-codex`, `openai-codex-device`를 보였다.
- 사용자 인증 저장소 `~/.omp/agent/agent.db`의 mtime이 15:40:35.347로 바뀌었다. 그 첫 확인 OMP가 실행 중이던 시각이다(제품 설계대로 `agent.db`는 사용자 저장소로의 symlink).
- 몇 초 뒤 같은 실행의 `control_no_skill` 확인과 이후 모든 실행은 authenticated가 비었다. 2단계 실행(15:40:55, 15:49:13)은 `No API key found for openai-codex.`로 prompt가 거절되었다. 모델·tool·Hub 요청은 0건이다.
- 사용자의 openai-codex 로그인이 그 뒤 쓸 수 없게 되었다.

## 가장 유력한 원인

- 예전 `~/wb-demo` backend의 OMP는 사용자 저장소의 symlink가 아니라 `agent.db` **사본**을 쓰고 있었다(2026-10-28 확인). 사본이 생긴 경위는 이 기록에서 확인하지 않았다.
- OAuth refresh token은 갱신할 때마다 회전한다. 사본과 원본이 같은 refresh token을 들고 있다가 한쪽(사본 쪽으로 추정)이 갱신하면 다른 쪽의 refresh token은 무효가 된다(갈라진 refresh token). C-D64가 내용 복사·동기화를 금지한 이유가 이것이다.
- Probe의 OMP가 시작 preflight에서 원본 저장소의 무효 refresh token으로 갱신을 시도했고, `invalid_grant`를 받은 OMP가 그 자격 증명을 비활성화했다(저장소 쓰기 = 위 mtime). OMP 실행 파일에 `auth_credential_blocks`·`DELETE FROM auth_credentials` 경로가 있다는 것은 확인했지만, 이번에 그 경로를 탔는지는 확인하지 않았다.

## 바로잡는 서술

PROBE-BOARD-SKILL.md(2026-10-27)의 "같은 store를 쓰는 … 실행 중인 Workbench(manager/worker)의 openai-codex 로그인도 영향을 받았을 수 있다"는 틀렸다. 실행 중이던 Workbench(`~/wb-demo`)는 같은 저장소가 아니라 사본을 썼다.

## 남은 위험

- 격리 확인 OMP는 확인이 끝나면 process group에 SIGTERM(3 s 유예) → SIGKILL(3 s)을 받는다(`backend/launcher.py:_stop_group`). OMP가 시작 preflight에서 token을 갱신하는 도중에 죽으면, 서버가 token을 회전한 뒤 새 token을 저장소에 쓰기 전에 끊길 수 있다. 그러면 사본이 없어도 로그인을 잃는다. 이번 사고에서 이 경로가 원인이라는 근거는 없지만 막혀 있지도 않다.
- Board agent OMP와 그 격리 확인 OMP의 log가 남지 않아(격리 확인 stderr는 임시 파일에서 64 KiB만 읽고 버린다) 이런 사고의 원인을 사후에 가릴 수 없다.
- Repo에 실제 `agent.db`를 복사하는 test helper가 있다(`tests/integration/live_real_summary.py`의 `_copy_auth`, `--auth-source`). 사용하면 같은 갈라짐이 다시 생긴다.

## 조치

- [사용자 결정 2026-10-28, WBX-D31] `~/wb-demo` backend를 멈췄다(`wb down` 뒤 멈춤 확인). 그 backend가 쓰던 인증 사본을 지웠다.
- [사용자 결정 2026-10-28, WBX-D32] 완화 ticket [WBX-B12](tickets/WBX-B12.md): 격리 확인 OMP의 상한 있는 graceful 종료, Board OMP log 보존(크기·개수 상한), test helper의 `agent.db` 복사 금지(필요하면 C-D64의 symlink).
- [사용자 결정 2026-10-28, WBX-D33] WBX-B12 뒤 WBX-B4 probe를 사용자의 실제 로그인으로 다시 실행하고 OMP log를 남긴다.

## 증거 위치

`/tmp/wbx-b4-out/`(`s1.json`, `full.json`, `full2.json`, `notes.md`; 2026-10-27 probe 출력과 해석 메모). 사용자가 정할 때까지 지우지 않는다(CLAUDE.md 삭제 확인 범위: 보고서·실패 근거는 사용자 확인 뒤 삭제).
