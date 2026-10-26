# WB 읽기 전용 진단

```sh
wb doctor
wb doctor --json
wb doctor --data-dir /path/to/project/.wb --json
omp-workbench doctor --json
```

현재 cwd의 가장 가까운 프로젝트 또는 명시한 `--data-dir`를 읽는다. 프로젝트가 없으면 그 항목을 warning으로 표시하고 전역 설치 검사는 계속한다. `.wb/`, DB, 진단 파일을 만들지 않는다. 기존 환경변수 `WORKBENCH_DATA_DIR` 선택도 같은 프로젝트 해석을 따른다.

각 항목은 `id`, `status`, `summary`, `hint`를 제공한다. 텍스트와 JSON의 의미·exit는 같다. JSON stdout에는 진행 로그나 prompt를 섞지 않는다.

| 항목 ID | 검사와 한계 |
|---|---|
| `installation` | 실행 distribution·Python·entrypoint 결속과 설치 방법·Git commit 또는 일반/editable 출처. 출처 URL·인증 정보는 출력하지 않는다. 확인되지 않은 결속/버전 형식은 unknown이다. |
| `python`, `uv`, `omp` | Python 3.12 이상과 로컬 도구 version. uv stdout/stderr 총 4KiB·3초, OMP는 기존 inspector의 총 64KiB·3초 제한을 사용한다. 허용된 version만 출력하며 raw stderr를 버린다. OMP SDK 호환성·로그인·모델/provider 검사는 하지 않는다. | 사용자 OMP 명령 선택 자체가 거절되면(예: `~/.local/bin` 쓰기 권한) `omp`는 error이며 summary에 reason code와 문제 디렉터리, hint에 수정 명령을 표시한다.
| `auth` | 선택된 공유 auth store의 존재·소유자·종류·0600 metadata만 확인한다. 내용을 열거나 key/token 유효성을 검사하지 않는다. 부재는 warning이다. |
| `project`, `permissions` | 초기화 없는 context 해석과 사용자 소유 0700 WB root. 권한을 바꾸지 않는다. |
| `db.tasks`, `db.records` | 원본 main/WAL의 고정 bytes를 기존 SQLite committed-image 경계로 메모리에 옮겨 quick_check·지원 user_version·core table metadata를 확인한다. 각 파일은 최대 64MiB이며 원본을 SQLite로 열지 않는다. 부재는 미생성 warning, 손상/schema 오류는 error, 변경 중인 family·rollback journal·관측 불가는 unknown이다. |
| `backend`, `bridge` | 기존 UID/boot/PID/start ticks·lock·endpoint binding 검증 뒤 read-only UI snapshot을 조회한다. bridge는 그 snapshot의 연결·process binding만 판단하며 새 SDK probe를 요청하지 않는다. stopped backend는 warning, identity 불일치나 도달 불가는 error/unknown이다. |
| `update` | 기존 사용자 범위 fence와 `/proc/locks`의 읽기 관측. 진행 중이면 warning으로 표시하고 중단하지 않는다. fence가 없어도 새 파일을 만들지 않는다. |
| `backup` | update manifest의 완료/실패/부분 상태와 포함 파일의 존재·크기·private metadata. 최대 128개 manifest·각 4096개 파일을 확인하며 backup 내용/hash를 재검증하거나 복원하지 않는다. |
| `resume` | 기존 update 재개 marker의 존재. warning으로 안내하며 `wb start`의 명시적 재개 안내를 확인하도록 한다. 명령 replay·자동 재개·pause 해제는 하지 않는다. |

메모리 snapshot이 정상이어도 살아 있는 writer의 이후 상태까지 보장하지 않는다. source family가 관측 중 바뀌거나 안전하게 확인할 수 없으면 unknown을 정상으로 바꾸지 않는다. DB 원문·작업 기록·raw 대화·환경변수 값·credential URL은 출력하지 않는다.

JSON 형식은 다음과 같다. 실제 check 목록은 프로젝트 유무에 따라 달라질 수 있다.

```json
{"schema_version":1,"overall":"warning","checks":[{"id":"project","status":"warning","summary":"현재 프로젝트 없음","hint":"프로젝트 안에서 또는 --data-dir로 다시 확인하세요."}]}
```

`status`는 `ok|warning|error|unknown`, `overall`은 `ok|warning|error`다. 필수 검사에 error/unknown이 있으면 overall error·exit 1, warning만 있으면 exit 0, 인자 오류는 exit 2다. `--fix`는 제공하지 않는다. hint는 다음 확인 행동의 안내이며 doctor가 자동 설치·업데이트·초기화·schema 이관·복구·권한 변경·프로세스 종료를 수행하지 않는다.
