# Workbench doctor 검증 기록

2026-10-20 WD-01/WD-02 후보의 구현 증거다. 독립 검증·최종 승인은 Root가 별도로 판정한다. 원시 증거 기준 경로는 `.workflow/workbench-doctor/runs/implement-p1-20261020/`다.

## 실제 설치본

`tests/integration/live_workbench_doctor.py`가 기존 InstalledRun/loopback harness와 uv 0.12.24·OMP 18.11.1·offline cache를 재사용했다. private local Git ref를 noneditable 설치하고 checkout 밖의 실제 `wb`/`omp-workbench`를 실행했다. 명령·설치 ref·namespace·OMP SHA·정리는 `WD02-live-a2/report.json`, packaging 입력 145개와 세 harness의 SHA는 `WD02-live-a2/input-hashes.json`에 있다. 최종 입력 불변은 `WD02-runtime-input-reuse.json`으로 대조했다.

실행 명령은 다음과 같다. private HOME/XDG/uv 환경은 저장된 harness가 구성한다.

```sh
unshare --user --map-root-user --pid --fork --mount-proc \
  sh -c 'mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /tmp && exec "$@"' sh \
  python3 tests/integration/live_workbench_doctor.py \
  --run-root .workflow/workbench-doctor/runs/implement-p1-20261020/WD02-live-a2 \
  --omp /home/dwchoo/.local/bin/omp --uv /home/dwchoo/.local/bin/uv \
  --cache /home/dwchoo/ai-exp-assistant/.workflow/WI-02-attempt-1/package-mv0ncw0_/cache \
  --host-pid-namespace 'pid:[4026531836]'
```

실제 namespace는 `pid:[4026533129]`, PID 1, UID map `0 → host 1000`, `/tmp`는 private tmpfs였다. 호스트 backend·auth·DB·설치는 접근하지 않았다.

| 실제 경계 | doctor exit / overall |
| --- | --- |
| 프로젝트 없음 | 0 / warning |
| 정상 비실행 프로젝트 | 0 / warning |
| text alias | 0 / text |
| 명시적으로 선택한 OMP 없음 | 1 / error |
| 손상 records DB | 1 / error |
| 진행 중 update lock·실패 backup·resume marker | 1 / error |
| 실제 backend·OMP bridge 실행 중 | 0 / ok |
| 실제 backend 응답 불가 | 1 / error, 해당 check unknown |
| 실제 backend identity 불일치 | 1 / error |

최종 harness exit는 0이다. 모든 9개 case에서 파일 hash·mode·UID·inventory 변경이 0이었다. task/spec/승인/이전 run, records 두 revision, raw bytes, OMP session, source/results, auth와 DB family를 포함한다. live case도 정상 writer 변경 허용 분기를 사용하지 않았다. backend·OMP의 PID/start ticks, 설치 ref가 유지됐고 자동 replay가 없었다. 전체 HTTP 요청·모델 턴은 0이다. 각 case의 stdout/stderr와 before/after inventory가 같은 디렉터리에 있으며, 원시 JSON overall의 별도 고정 기대값 대조는 `WD02-overall-check.json`이다. auth/record 원문은 출력·보고서에 복사하지 않았다.

응답 불가·identity mismatch·손상 DB·유지보수 상태의 구성/복원은 각 doctor 실행 바깥의 소유 fixture 동작이다. 호스트 사용자 상태는 사용하지 않았다.

## 관측된 수정과 검사

최초 실제 실행 `WD02-live-a1/report.json`은 shell launcher의 `python -I -m workbench` 경로를 doctor가 거절하는 결함을 검출했다. `WD02-entrypoint-red.log`로 먼저 재현하고, 현재 distribution의 canonical `workbench/__main__.py`도 허용하는 3줄 최소 수정 후 `WD02-entrypoint-green.log`가 통과했다. a1 원본을 보존했고 수정된 실제 설치본 a2만 최종 실행했다.

| 검사 | 원시 로그 | 결과 |
| --- | --- | --- |
| doctor 최종 공개 동작·비노출·읽기 전용 | `WD02-doctor-final.log` | 11개 통과, 18.652초 |
| update 유지보수 경계 | `WD02-update-boundary.log` | 29개 통과, 15.328초 |
| 기존 CLI 경계 | `WD01-affected-cli.log` | 3개 통과, CLI 입력 불변 이월 |

첫 두 명령은 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests/backend/maintenance -p 'test_doctor*.py'` 및 `'test_update*.py'`다. WD-01의 7개 RED→GREEN chronology와 WAL fixture 진단은 `WD01-result.json`에 연결되어 있다. 실패 로그를 성공으로 계산하지 않았다.

## 한계와 정리

최종 review의 manifest 동시 교체 결함은 실제 `os.replace`를 공개 CLI 읽기 중 발생시켜 재현했다(`Final-fix-red.log`). 기존 backup unknown handler에 `RuntimeError`만 추가한 뒤 schema 1 JSON·`backup: unknown`·exit 1·추가 쓰기 0으로 통과했다(`Final-fix-green.log`). 영향받는 자체 doctor CLI 12개 검사는 `Final-fix-doctor-final.log`에 있다. 기존 실제 9개 안정 경계의 doctor 원본 SHA와 145개 입력·세 harness를 보존하고 handler 1줄 차이만 대조해 이월했다. packaging·entrypoint·asset 변경이 없어 실제 설치 실행을 반복하지 않았다(`Final-fix-result.json`).

DB 검사는 복사한 committed image의 지원 schema version·핵심 table·quick_check 경계다. backup은 완료 metadata·파일 존재/크기/권한을 확인하며 restore나 payload 전체 hash를 검증하지 않는다. auth 유효성·provider 연결은 검사하지 않았다. 관측 시점 이후의 writer 동작도 보증하지 않는다.

최종 report는 `remaining_processes=[]`, `unrecorded_private_processes={}`, `private_processes_stopped=true`, `private_root_removed=true`다. 소유 cache를 정리한 뒤 private root를 제거했고 namespace 종료로 tmpfs도 제거됐다. Root official storage maintenance도 cache 0·saved 0·skipped 0으로 완료했다(`.workflow/storage-maintenance/20261020T120000.247013Z`). graph 갱신과 최종 판정은 Root 소유다.
