# OMP Workbench

Workbench는 독립 프로그램으로 설치하고, 연구 프로젝트마다 별도 `.wb/`에 이력과 로그를 보관합니다. 연구 프로젝트에 Workbench checkout, Python 의존성, 내장 skills를 설치할 필요가 없습니다.

## 설치

사전 준비물은 `uv`, 저장소에 접근할 수 있는 Git 인증, Python 3.12 이상입니다. 설치 launcher는 `/bin/sh`와 `-f` 옵션을 지원하는 실행 파일 `/usr/bin/readlink` 또는 `/bin/readlink`를 고정 경로에서 찾습니다. 사용자의 `PATH`에서 다른 `readlink`를 찾는 fallback은 없습니다.

현재 검증 범위는 Linux x86_64 glibc입니다. 이 범위를 모든 Linux 배포판·아키텍처의 지원 보장으로 확대하지 않습니다. `readlink -f` 또는 Workbench tool 환경의 Python interpreter가 없다는 launcher 진단이 나오면, 먼저 위 고정 경로의 OS utility와 Python 3.12 이상을 확인하세요. utility를 설치하거나 고친 뒤에도 tool interpreter가 없으면, 설치에 사용한 것과 같은 실제 Git ref로 uv tool 환경을 다시 만듭니다.

```sh
uv tool install 'git+https://github.com/dwchoo/ai-exp-assistant.git@<implemented-tag-or-commit>'
wb setup
```

`<implemented-tag-or-commit>`은 이 구현이 포함된 저장소의 실제 tag 또는 commit으로 바꾸세요. 현재 uncommitted `main`은 아직 원격에서 설치할 수 있는 ref가 아닙니다. 해당 tag/commit이 원격에서 접근 가능해진 뒤 설치하세요. 재설치가 필요하면 같은 ref를 사용합니다.

```sh
uv tool install --force --reinstall 'git+https://github.com/dwchoo/ai-exp-assistant.git@<implemented-tag-or-commit>'
```

`uv tool install`은 Workbench와 패키지에 포함된 skills·references·helpers를 uv 관리 환경에 둡니다. 아래 작업 폴더에는 이 저장소를 clone하거나 Workbench를 프로젝트 Python 환경에 추가하지 않아도 됩니다.

## OMP 준비와 로그인

`wb setup`은 기존 실행 파일을 선택해 재사용하거나, 선택할 OMP가 없을 때 공식 stable 배포본을 사용자 전용 위치에 준비합니다. 준비가 끝나면 실행 경로·버전과 SDK 로드 여부를 표시합니다. SDK 로드는 실제 모델 연결 검증을 뜻하지 않습니다. setup은 OMP 로그인이나 연구 작업을 시작하지 않습니다.

OMP 선택 순서는 `wb setup --omp` 인자, `WORKBENCH_OMP`, 이전 setup에서 저장한 경로, `PATH`의 `omp`입니다. 선택한 파일이 없거나 실행할 수 없으면 다른 파일로 조용히 바꾸지 않고 실패를 안내합니다. 직접 선택하려면 다음처럼 실행합니다.

```sh
wb setup --omp /absolute/path/to/omp
```

setup 출력에 표시된 OMP 실행 파일을 실행하고 OMP의 `/login` 흐름에서 로그인하세요. Workbench는 기존 사용자 OMP의 인증 저장소를 프로젝트별 OMP 설정에 연결해 사용합니다. 인증 DB나 비밀 값을 `.wb/`로 복사하지 않고 새 WB 인증 저장소도 만들지 않습니다. 한 번 로그인한 사용자 OMP 인증은 프로젝트들에서 공유될 수 있습니다.

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

- `tasks.sqlite3`, `important-logs/`, `raw-logs/`, `backend.log`
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

Workbench는 `wb update` 명령을 제공하지 않습니다. 또한 이미 선택되어 정상 동작하는 OMP를 시작할 때나 `wb setup`을 다시 실행할 때 자동으로 최신 버전으로 바꾸지 않습니다. WB가 관리하는 OMP 복사본을 업데이트하는 별도 명령도 없습니다. 준비된 다른 OMP 실행 파일을 선택하려면 모든 backend를 멈춘 다음 실제 경로를 명시하세요.

```sh
wb setup --omp /absolute/path/to/updated/omp
```

이 명령은 해당 실행 파일을 검사하고 setup 선택으로 기록합니다. OMP 자체의 다운로드·업데이트 방법은 선택한 OMP 배포의 안내를 따르세요.

## 설치본 skills

설치본이 제공하는 skill source와 역할별 노출 계약은 [Workbench skills 안내](omp_bridge/skills/README.md)를 참고하세요. 이 문서는 연구 프로젝트에 skill을 복사하거나 별도 의존성을 설치하라고 요구하지 않습니다.
