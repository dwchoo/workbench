# Workbench

Workbench는 독립 프로그램으로 설치하고, 연구 프로젝트마다 별도 `.wb/`에 이력과 로그를 보관합니다. 연구 프로젝트에 Workbench checkout, Python 의존성, 내장 skills를 설치할 필요가 없습니다.

## 설치

현재 검증 범위는 Linux x86_64 glibc입니다. `uv`, Git과 저장소 접근 권한, Python 3.12 이상이 필요합니다.

`uv`가 없다면 [공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)를 따르세요. Linux에서는 다음 명령을 사용할 수 있습니다. 설치 후 새 터미널을 열거나 설치기가 안내한 환경 파일을 적용합니다.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Workbench를 연구 프로젝트 밖의 uv tool 환경에 설치하고 OMP를 준비합니다.

```sh
uv tool install --python 3.12 'git+https://github.com/dwchoo/workbench.git@main'
wb setup
```

`wb` 명령을 찾지 못하면 `uv tool update-shell`을 실행한 뒤 새 터미널을 여세요. 같은 설치 버전을 재현하려면 `@main` 대신 실제 tag 또는 commit을 지정합니다. `main`은 이후 변경될 수 있습니다. uv tool 환경과 PATH 관리는 [uv tools 안내](https://docs.astral.sh/uv/guides/tools/)를 참고하세요.

이전 이름인 `omp-workbench`로 설치한 사용자는 먼저 사용 중인 모든 프로젝트의 backend를 `wb shutdown`으로 종료한 뒤 기존 tool을 제거하고 위 새 주소로 설치하세요. 기존 설치가 있는지는 `uv tool list`로 확인할 수 있습니다.

```sh
# 이전 omp-workbench tool이 설치된 경우에만 실행
uv tool uninstall omp-workbench
uv tool install --python 3.12 'git+https://github.com/dwchoo/workbench.git@main'
wb setup
```

배포 패키지 이름은 `workbench`이고 Python import 이름도 `workbench`입니다. 기본 명령 `wb`와 기존 `omp-workbench` 명령은 모두 유지합니다. 프로젝트 `.wb/`, 기존 setup 설정, 공유 OMP와 runtime 경로는 이름 변경 때문에 이동하거나 삭제하지 않습니다. 경로에 남아 있는 `omp-workbench`는 기존 설치와의 호환성을 위한 저장 위치입니다.

같은 ref의 설치를 다시 만들려면 다음처럼 실행합니다.

```sh
uv tool install --force --reinstall --python 3.12 'git+https://github.com/dwchoo/workbench.git@main'
```

`uv tool install`은 Workbench와 내장 skills·references·helpers를 uv 관리 환경에 둡니다. 연구 프로젝트에 이 저장소를 clone하거나 Workbench를 프로젝트 Python 의존성으로 추가할 필요가 없습니다.

설치 launcher는 `/bin/sh`와 `-f`를 지원하는 `/usr/bin/readlink` 또는 `/bin/readlink`를 사용합니다. launcher가 해당 utility나 tool interpreter를 찾지 못하면 고정 경로의 OS utility와 Python 환경을 확인한 뒤 같은 ref로 tool을 다시 설치하세요.

## OMP 준비와 로그인

`wb setup`은 기존 실행 파일을 선택해 재사용하거나, 선택할 OMP가 없을 때 공식 stable 배포본을 사용자 전용 위치에 준비합니다. 준비가 끝나면 실행 경로·버전과 SDK 로드 여부를 표시합니다. SDK 로드는 실제 모델 연결 검증을 뜻하지 않습니다. setup은 OMP 로그인이나 연구 작업을 시작하지 않습니다.

`wb setup`은 검증한 OMP를 가리키는 `~/.local/bin/omp` 절대 경로 symlink를 준비합니다. 공식 배포본과 기존 외부 실행 파일 모두 대상이며, 외부 실행 파일 자체는 변경하지 않습니다. 이전 설치도 `wb setup`을 다시 실행하면 저장된 설치를 재검사해 빠진 링크를 복구합니다. 이미 같은 실행 파일을 가리키거나 사용자 명령 자체가 실행 파일이면 그대로 재사용합니다. 다른 파일이나 링크는 덮어쓰지 않습니다. 출력에서 현재 `PATH`가 선택한 OMP를 가리키는지 확인하세요. `~/.local/bin`이 빠졌거나 다른 `omp`가 먼저 선택되면 출력된 `export PATH=...`를 현재 shell에서 실행할 수 있습니다. shell 설정 파일은 자동으로 바꾸지 않습니다. 링크를 준비하지 못해도 검증된 절대 실행 경로와 경고를 출력합니다.

setup의 설치 검사 순서는 `wb setup --omp` 인자, `WORKBENCH_OMP`, 현재 `~/.local/bin/omp`, 이전 setup의 설치 기록, `PATH`의 `omp`입니다. 선택한 파일이 없거나 실행할 수 없으면 다른 파일로 조용히 바꾸지 않고 실패를 안내합니다. 직접 선택하려면 다음처럼 실행합니다.

```sh
wb setup --omp /absolute/path/to/omp
```

일반 `wb` 실행은 `PATH`나 저장된 버전 경로와 관계없이 `~/.local/bin/omp`를 기본 명령으로 사용합니다. `--omp`와 `WORKBENCH_OMP`를 지정하면 그 선택이 우선합니다. 기본 명령이 없거나 고장 나면 저장된 설치 경로로 우회하지 않고 `wb setup`을 안내합니다. setup 기록의 버전 경로는 설치 검사·복구용이며, WB 실행 시에는 symlink 경로를 유지합니다.

일반 `omp`는 OMP 실행 파일로 직접 연결되며 WB wrapper가 아닙니다. WB 전용 인자와 bridge·격리 환경은 WB가 시작하는 OMP process에만 전달합니다. 사용자 OMP 설정·skills·명령이나 shell 환경을 전역으로 설치·변경하지 않습니다.

`wb setup` 이후 출력에서 PATH가 확인되면 `omp`를, 그렇지 않으면 출력된 절대 경로 명령을 실행하고 OMP의 `/login` 흐름에서 로그인한 뒤 `wb`를 실행하세요. Workbench는 기존 사용자 OMP의 인증 저장소를 프로젝트별 OMP 설정에 연결해 사용합니다. 인증 DB나 비밀 값을 `.wb/`로 복사하지 않고 새 WB 인증 저장소도 만들지 않습니다. 한 번 로그인한 사용자 OMP 인증은 프로젝트들에서 공유될 수 있습니다.

Workbench와 OMP의 사용자 위치는 연구 프로젝트 밖에 있습니다.

| 항목 | 기본 위치 또는 경계 |
|---|---|
| Workbench 프로그램·내장 asset | uv가 관리하는 tool 환경 |
| Workbench setup 선택 기록 | `~/.config/omp-workbench/setup.json` (`XDG_CONFIG_HOME` 사용 시 그 아래) |
| Workbench가 준비한 공유 OMP | `~/.local/share/omp-workbench/tools/` (`XDG_DATA_HOME` 사용 시 그 아래) |
| 사용자 OMP 인증 | 기존 OMP 인증 저장소. 프로젝트의 `.wb/omp-root/agent/agent.db`는 그 파일을 가리키는 link입니다. |
| socket·재생성 가능한 runtime/cache | 프로젝트 밖의 private runtime namespace. 유효하고 경로가 충분히 짧은 `XDG_RUNTIME_DIR`을 우선 사용하고, 그렇지 않으면 `/tmp/omp-workbench-<uid>/` 아래를 사용합니다. |

## 프로젝트 시작과 데이터 위치

연구 프로젝트 폴더로 이동한 뒤 실행합니다.

```sh
cd /path/to/research
wb
```

`wb`는 `wb start`처럼 동작합니다. OMP 준비가 되어 있고 초기화된 프로젝트가 없으면 Workbench가 현재 폴더를 프로젝트 root로 표시하고 `.wb/`를 만들지 확인합니다. 입력 프롬프트의 기본값은 아니요(`[y/N]`)입니다. 터미널 입력 없이 실행하면 확인을 받지 못해 프로젝트를 만들거나 backend를 시작하지 않습니다. Git 저장소는 필요하지 않고, Git root로 자동 이동하지도 않습니다. 처음 시작할 폴더를 먼저 선택하세요. 일반 `wb`/`wb start`는 OMP나 runtime을 내려받지 않습니다. 시작 전 검사에서 OMP 선택·실행 요건을 충족하지 못하면 `wb setup` 안내와 함께 멈추며, 새 `.wb/`나 backend를 만들지 않습니다. SDK 로드 검사는 `wb setup`에서 수행합니다.

현재 폴더에서 상위 폴더 방향으로 검사할 때 처음 발견한 `.wb/`만 확인합니다. 정상 marker면 해당 프로젝트를 사용하지만, symlink·invalid/incomplete marker·미완료 이관이면 진단과 함께 그 자리에서 멈추고 더 위의 `.wb/`로 건너뛰지 않습니다. 해당 경로를 자동으로 덮어쓰거나 복구하지 않습니다. `research/.wb/`가 정상이라면 `research/src/`에서도 같은 프로젝트에 연결됩니다. Git 저장소 안에서는 `.wb/`를 Git에서 제외합니다. Git 제외를 확인할 수 없거나 `.wb/`가 이미 추적 중이면 새 초기화와 backend 시작을 보류합니다. 기존 추적 파일과 ignore 설정을 보존하며 임의 untrack·삭제·자동 복구를 하지 않습니다.

프로젝트마다 다음 영속 자료를 `<project>/.wb/`에 보관합니다.

- `tasks.sqlite3`, `records.sqlite3`, `important-logs/`, `raw-logs/`, `backend.log`
- `workflow/`와 Workbench 전용 OMP 설정·대화 session
- 프로젝트 marker와 backend 기록

각 프로젝트의 `.wb/`는 서로 다른 이력과 OMP 대화를 유지합니다. socket과 재생성 가능한 cache는 별도 private runtime 위치에 있어 `.wb/`를 공유하지 않습니다. 프로그램 assets와 내장 skills도 설치본에서 읽으며 연구 프로젝트로 복사하지 않습니다. raw log 보관 설정은 별도 정책을 따르고, 이 안내가 rotation을 켜거나 기존 로그를 변경하지 않습니다.

## 자주 쓰는 명령

| 명령 | 동작 |
|---|---|
| `wb` 또는 `wb start` | backend를 시작하거나 실행 중인 backend를 재사용해 Workbench 화면에 연결 |
| `wb start --no-attach` | backend를 시작하거나 재사용한 뒤 화면 연결 없이 반환 |
| `wb attach` | 현재 프로젝트의 실행 중 backend에 연결. `--plain`은 최소 터미널 client 사용 |
| `wb status` | 현재 프로젝트 backend 상태 표시. `--json`은 JSON 출력 |
| `wb shutdown` | active work를 표시하고 확인을 받은 뒤 backend, 두 OMP session, host shell 전체 종료 |
| `wb confirm-boot` | 상태가 boot 확인을 요구할 때 환경 조건을 확인하고 진행 여부를 명시적으로 확인 |
| `wb setup` | 사용자 범위 OMP 준비·선택 |
| `wb migrate --from <source> --project <project>` | 중단된 기존 data-dir을 새 프로젝트로 명시 이관 |

선택된 저장소의 project marker·기록된 canonical root·실제 backend process identity가 서로 일치하지 않거나 확인되지 않으면 연결·재사용·새 시작을 보류합니다. 하위 폴더나 다른 cwd에서 `--data-dir`로 선택하는 것 자체는 불일치가 아닙니다. 진단에 표시된 경로와 identity를 확인하세요. 알 수 없는 backend를 정상 종료 상태로 간주하거나 기록을 자동 수정·복구하지 않습니다.

옵션은 해당 하위 명령 뒤에 둡니다. 예를 들어 `wb start --data-dir ...`, `wb status --json`, `wb setup --omp ...`와 같이 씁니다. `wb start`의 `--plain`은 최소 client를 사용하고, `--omp-arg`는 두 OMP process에 전달할 추가 인자(반복 가능), `--bridge-extension`은 G3 bridge extension 경로를 지정합니다. `--json`은 `setup`, `status`, `confirm-boot`, `shutdown` 등의 help에 표시된 명령에서 사용할 수 있습니다. 각 세부 옵션은 `wb <command> --help`로 확인하세요.

기존 명령 `omp-workbench`도 같은 CLI 진입점으로 유지됩니다. 저장소 옵션은 `--data-dir` 명시값, `WORKBENCH_DATA_DIR`, 프로젝트 탐색 순으로 적용됩니다. `--data-dir`이 있는 명령에서는 옵션을 하위 명령 뒤에 둡니다.

```sh
wb status --data-dir /path/to/old-data-dir
WORKBENCH_DATA_DIR=/path/to/old-data-dir wb status
```

명시 경로가 있으면 해당 저장소를 직접 사용합니다. 예전 global 기본 경로를 자동으로 찾아 이관하거나 새 프로젝트에 연결하지 않습니다. 기존 저장소를 계속 쓸지 프로젝트로 이관할지는 사용자가 명시적으로 선택해야 합니다. `wb logs --help`는 현재 제공되는 로그 조회·관리 명령을 보여줍니다.

## 작업 기록

Manager·Worker에게 “이 내용을 기록해줘”라고 요청하면 일반 작업 메모·결정·진행 상황·실험 정리·인계는 프로젝트 `.wb/records.sqlite3`에 저장합니다. 새 세션에는 최근 기록의 제목·짧은 미리보기를 제공하며 본문은 필요할 때 조회합니다. 수정은 같은 기록의 새 revision으로 저장하고 이전 revision도 보존합니다. BRIEF·SPEC·ADR과 사용자 직접 호출용 WB 문제 보고서는 기존 문서 방식을 따릅니다.

```sh
wb records list
wb records search '찾을 문자열'
wb records show RECORD_ID --revision 1
wb records history RECORD_ID
wb records export RECORD_ID --revision 1 --output /path/to/note.md
```

조회·내보내기는 backend나 모델을 새로 시작하지 않습니다. Markdown 출력은 사용자가 요청한 사본이며 기존 파일을 덮어쓰거나 DB 원본을 바꾸지 않습니다. 저장·조회 실패는 빈 결과와 구분하며, 결과가 불명확한 기록 요청은 자동으로 다시 보내지 않습니다. 한도·페이지·실패 의미는 [작업 기록 사용법](docs/features/workbench-records/USAGE.md)을 참고하세요.

## 연결 해제, 일시정지, 종료

- **연결 해제(detach):** 화면에서 `Ctrl-]`를 누른 뒤 `q`를 누릅니다. 현재 화면 연결만 닫고 backend, OMP session, host shell은 계속 실행합니다. `q`는 확인이 아니라 연결 해제입니다.
- **자동화 일시정지·재개:** 화면에서 `Ctrl-]` 다음 `p`를 누르면 확인 화면이 열립니다. 그 화면에서 `p`를 눌러 확인해야 자동화를 일시정지합니다. 재개도 `p` 확인 전 상태 대조를 요구합니다. 이는 backend 전체 종료가 아니며 새 자동 작업을 보류하고 진행 중 Manager turn의 중단을 요청합니다.
- **전체 종료:** `wb shutdown`은 실행 중인 작업을 표시하고 확인을 받은 뒤 backend와 그 OMP session, host shell을 종료합니다. 프로젝트의 `.wb/` 데이터는 보존됩니다. 자동 외부 요청은 실행 승인으로 취급하지 않으며, 종료나 재연결이 이전 명령·실험의 자동 재실행을 뜻하지 않습니다.

재부팅 후에는 먼저 `wb status`의 boot 상태를 확인하세요. 확인이 필요한 상태로 표시될 때만 `wb confirm-boot`를 실행해 환경 조건을 확인하고 승인하세요. 기존의 확인 기록을 재사용하지 않으며, 재부팅 전에 중단된 실험을 자동으로 다시 실행하지 않습니다.

## 기존 data-dir 이관

이관은 명시적으로 실행하며 source 원본을 보존합니다. 먼저 source backend와 관련 writer를 모두 중단하세요. 실행 중인지 확인할 때는 다음처럼 기존 경로를 지정합니다.

```sh
wb status --data-dir /path/to/old-data-dir
```

실행 중이면 작업 상태를 확인하고, 종료가 적절한 경우 `wb shutdown --data-dir /path/to/old-data-dir`에서 표시되는 active work와 확인 요청을 검토하세요. 새 프로젝트 root는 이미 존재해야 하고 `.wb/`는 미리 만들지 않아야 합니다.

```sh
mkdir -p /path/to/new-research
wb migrate --from /path/to/old-data-dir --project /path/to/new-research
```

`--project`에는 새 프로젝트 폴더를 지정하고 `.wb/` 하위 경로를 넣지 않습니다. source와 project는 존재하는 canonical 경로여야 하며 symlink 경로, 서로 같거나 중첩된 source/target, 이미 존재하는 대상 `.wb/`는 거절됩니다. 이관은 병합·덮어쓰기·자동 재시도를 하지 않습니다.

이관은 지원하는 DB/WAL 자료를 복구해 새 target에서 다시 확인합니다. target filesystem은 migration snapshot에 Linux `O_TMPFILE`을 지원해야 합니다. 지원하지 않으면 안내와 함께 이관이 거절되고 source는 보존됩니다. 이 제한은 migration snapshot에만 적용되며 일반 setup/start 조건은 아닙니다. SQLite의 non-empty rollback journal과 지원하지 않는 DB/WAL 형식은 자동 복구하지 않고 거절합니다. 복구에 필요한 임시 메모리는 DB image 크기에 따라 늘 수 있습니다.

custom `models.yml`은 지원되는 credential-free configuration subset만 이관할 수 있습니다. credential이 들어 있거나 알 수 없는 형식·field가 있는 설정은 target을 만들거나 설정을 복사하기 전에 거절됩니다. `config.yml`은 Workbench가 생성한 정확한 형식만 인식합니다. 이 검사는 임의 YAML 변환이나 모든 파일의 비밀값 검사를 뜻하지 않습니다. 프로젝트별 custom models 정의는 설치본의 기본 role-model table과 별개입니다. 거절되면 source를 보존하고 OMP 설정을 수동으로 정리한 뒤, target `.wb/`가 없는 상태에서 다시 판단하세요.

이관은 외부 worktree나 실험 결과를 이동하지 않고 기존 경로 참조만 보존합니다. 경로가 더 이상 접근 가능하지 않으면 이관만으로 결과를 찾거나 복구하지 않습니다. 인증 DB 내용은 복사하지 않습니다. source에 기존 사용자 OMP 인증 link가 있고 같은 사용자 인증 저장소가 사용 가능하면 새 프로젝트의 link가 그 저장소를 다시 가리킵니다. backend socket/PID와 과거 실행 승인은 활성 상태로 가져오거나 재사용하지 않으며, 비활성 제어 기록이 있더라도 실행되지 않습니다. 성공한 이관도 실험이나 worker 실행의 승인이 아닙니다. 새 프로젝트에서 일반적인 start/recovery 조건을 적용하며, 현재 상태가 boot 확인을 요구할 때만 `wb confirm-boot`가 자동 동작을 다시 허용합니다.

실패 시 명령은 중단 phase와 불완전 target 경로를 출력할 수 있습니다. source는 보존되지만 target `.wb/`가 남아 있을 수 있습니다. 실패 target을 자동 삭제·합치거나 곧바로 재시도하지 말고 출력된 경로와 원인을 먼저 확인하세요. 기존 data-dir은 이관이 성공해도 자동 삭제되지 않습니다.

## 업데이트

Workbench를 바꿀 때는 먼저 사용 중인 모든 프로젝트의 backend를 `wb shutdown`으로 종료하고, 변경을 포함한 새 Git tag/commit을 명시해 uv tool을 다시 설치하세요. `wb start`와 `wb setup`은 자동으로 Workbench code를 업데이트하지 않습니다. 더 낮은 Workbench 버전으로 기존 `.wb/` DB를 열 수 있다는 보장은 없습니다.

Workbench는 `wb update` 명령을 제공하지 않습니다. 또한 이미 선택되어 정상 동작하는 OMP를 시작할 때나 `wb setup`을 다시 실행할 때 자동으로 최신 버전으로 바꾸지 않습니다. WB가 관리하는 OMP 복사본을 업데이트하는 별도 명령도 없습니다. 준비된 다른 OMP 실행 파일을 검사하려면 모든 backend를 멈춘 다음 실제 경로를 명시하세요.

```sh
wb setup --omp /absolute/path/to/updated/omp
```

이 명령은 해당 실행 파일을 검사하고 설치 기록에 저장하지만, 기존 `~/.local/bin/omp`가 다른 실행 파일을 가리키면 링크를 덮어쓰지 않고 경고합니다. 이 경우 일반 `wb`의 기본 실행 파일도 바뀌지 않습니다. 새 실행 파일로 WB를 시작하려면 명시적으로 지정하세요.

```sh
wb start --omp /absolute/path/to/updated/omp
```

여러 실행에 같은 선택을 적용하려면 `WORKBENCH_OMP`를 설정할 수 있습니다. 일반 터미널의 `omp`도 바꾸려면 기존 사용자 명령을 확인하고 원하는 OMP를 가리키도록 직접 관리하세요. OMP 자체의 다운로드·업데이트 방법은 선택한 OMP 배포의 안내를 따르세요.

## 설치본 skills

설치본이 제공하는 skill source와 역할별 노출 계약은 [Workbench skills 안내](omp_bridge/skills/README.md)를 참고하세요. 이 문서는 연구 프로젝트에 skill을 복사하거나 별도 의존성을 설치하라고 요구하지 않습니다.

## 프로그램 제거

Workbench만 제거하려면 사용 중인 모든 프로젝트의 backend를 종료한 뒤 실행합니다.

```sh
uv tool uninstall workbench
```

OMP 자체, 프로젝트 `.wb/`와 기존 setup 설정은 유지됩니다. Workbench 전용 `uninstall` 명령이나 자동 데이터 정리 기능은 제공하지 않습니다.
