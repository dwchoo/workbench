# Workbench Hub 사용 안내

Hub는 OS 사용자당 하나만 실행되는 background 프로세스로, 등록된 WB 프로젝트를 한곳에서 보고 켜고 끄고 진입시킨다. 명세는 [SPEC](SPEC.md), 합의는 [BRIEF](BRIEF.md)를 따른다. 이 문서는 사용자가 쓰는 명령과 화면만 다룬다.

Hub는 어느 backend의 부모도 감독자도 아니다. Hub를 멈추거나 Hub가 죽어도 실행 중인 WB 프로젝트와 실험은 계속되고, 프로젝트 안에서 `wb`로 직접 들어갈 수 있다.

## bare `wb`: 프로젝트 안과 밖

| 위치 | `wb`(인자 없음) |
|---|---|
| 프로젝트 안(가장 가까운 `.wb/`가 있는 폴더 또는 하위 폴더, 또는 `WORKBENCH_DATA_DIR` 지정) | `wb start`처럼 backend를 시작하거나 재사용해 연결 |
| 프로젝트 밖 | TUI Hub 목록. 초기화를 묻지 않는다 |
| 프로젝트 밖 + terminal 아님 | exit 2, `wb hub list` 사용 안내 |

이전에는 프로젝트 밖의 `wb`가 현재 폴더 초기화를 물었다. 이제 새 프로젝트는 다음 중 하나로 만든다.

```sh
cd /path/to/research
wb init            # 확인 후 .wb/ 생성 + Hub 등록. backend는 시작하지 않음
wb start           # 명시한 start는 예전처럼 초기화 확인 후 시작
```

`wb init [PATH]`는 `[y/N]` 확인(기본 아니요)이 필요하고 `--yes`는 없다. terminal이 아니면 아무것도 만들지 않고 exit 2다. 이미 WB 프로젝트인 폴더는 등록만 하고, 다른 WB 프로젝트의 하위 폴더에는 중첩 초기화를 하지 않는다.

## Hub 목록(TUI)

열은 이름, 경로, 상태, Task·단계, 명령, 대기 사유, 요청, 마지막 활동이다. 3초마다 갱신한다. 값이 없으면 `—`이다. 요청 열은 `대기 N · 질문 M`(신규 N건, 추가 정보 필요 M건)이고 둘 다 0이면 `0`, 그 프로젝트의 요청 저장소를 읽지 못하면 `—`다(0이 아니다). 요청 기능은 [Task Inbox & Web Board 사용 안내](../task-inbox/USAGE.md)를 본다.

| 키 | 동작 |
|---|---|
| `↑` `↓` / `k` `j` | 선택 이동 |
| `Enter` | 켜짐: 연결. 꺼짐: "켤까요? [y/N]" 뒤 `y`면 켜고 연결, 다른 키는 목록에 머문다. 시작 중·끄는 중: 안내만. 연결 없음/불명·불일치: 이유만 표시 |
| `o` | 꺼짐 프로젝트를 연결 없이 켜기(같은 확인) |
| `x` | 켜짐 프로젝트 끄기. 진행 중 작업을 보여 주고 `[y/N]` 확인(기존 `wb shutdown`과 같은 종료) |
| `r` | 즉시 새로 고침 |
| `q` | 목록 종료 |

연결에서 detach하면 목록으로 돌아온다. 상태 label은 켜짐, 꺼짐, 시작 중, 끄는 중, 연결 없음/불명, 불일치다.

"연결 없음/불명"은 Hub가 그 프로젝트의 backend를 확인하지 못했다는 뜻이다. 꺼짐이 아니다. 이 상태에서는 켜기·연결을 하지 않으니 `wb status`나 `wb doctor`로 직접 확인한다. Hub 자체와 연결이 끊기면 상단에 "Hub 연결 없음"을 보이고 모든 행을 "연결 없음/불명"으로 바꾼다. "불일치"는 등록된 폴더가 없거나, `.wb/`가 없거나 손상됐거나, 프로젝트 ID가 다른 경우다.

## 명령

```sh
wb hub start               # Hub가 없으면 background로 시작
wb hub stop [--yes]        # Hub 정지. 켜진 WB 프로젝트는 계속 실행
wb hub status [--json]     # pid, version, 위치, 등록 수, 부팅 자동 시작 여부
wb hub list [--json]       # 목록을 TUI 없이 출력
wb hub add PATH            # WB 프로젝트 root 등록 (.wb 없으면 거절, wb init 안내)
wb hub remove PATH         # 등록 해제 (.wb 보존. 켜짐·불명 프로젝트는 먼저 끄라며 거절)
wb hub enable | disable    # 로그인 뒤 자동 시작하는 systemd 사용자 서비스
wb --version               # wb <release> (<commit>|editable|unknown)
```

```sh
wb hub passwd [--stdin]              # Web Board 로그인 비밀번호. Hub를 시작하지 않는다
wb hub tls --self-signed [--force]   # 자체 서명 TLS cert·key. Hub 재시작 뒤 HTTPS
wb hub review-mode PATH [auto|manual]  # 프로젝트의 요청 검토 방식 보기·바꾸기
```

세 명령의 설정·동작은 [Task Inbox & Web Board 사용 안내](../task-inbox/USAGE.md)에 있다. `wb hub status`는 `web:`(listening 주소 또는 `not_listening` 이유)와 평문 HTTP 경고, `board_agent:` 줄도 보인다. Exit code는 0 성공, 1 실패·거절, 2 요건 없음, 3 Hub 실행 중 아님이다.

## 자동 시작

Hub가 없으면 `wb`가 background로 띄운다. argparse 오류, `--help`, `--version`은 띄우지 않는다. 다음 명령도 띄우지 않는다.

- `wb setup`, `wb update`, `wb doctor`
- `wb hub start`(자기 자신), `stop`, `status`, `enable`, `disable`, `passwd`, `tls`

그 밖의 `wb`(`start`, `init`, `attach`, `status`, `shutdown`, `hub list` 등)는 Hub를 띄우려 시도한다. 띄우지 못해도 명령은 계속하며 stderr에 한 줄만 낸다. Hub가 꼭 필요한 bare 목록과 `wb hub list`만 실패로 끝난다. 업데이트가 진행 중이면 자동 시작을 건너뛴다.

### WB 관리 terminal 안의 보류

backend나 실험 shell 아래(WB가 관리하는 terminal)에서 Hub를 띄우면 Hub가 그 shell로 재부모화되어 실험 명령이 끝나지 않고 종료 때 잔존으로 표시되며 `wb update`가 거절된다. 그래서 이 안에서는 Hub를 자동으로 시작하지 않는다. stderr가 terminal이면 "WB가 관리하는 terminal 안에서는 Hub를 자동으로 시작하지 않습니다. 외부 terminal에서 'wb hub start'를 실행하세요."를 한 줄 보인다. `wb hub start`도 같은 이유로 거절(exit 2)한다. 이미 Hub가 실행 중이면 이 안에서도 목록·등록을 쓸 수 있다.

`/proc`을 읽을 수 없는 호스트(예: `hidepid`)에서는 관리 terminal인지 확인할 수 없어 같은 방식으로 보류한다. 이때는 "프로세스 정보를 읽을 수 없어 Hub 자동 시작을 보류했습니다"(`proc_unreadable`)로 안내하며, 외부 terminal에서도 `/proc` 접근을 고치기 전에는 시작되지 않는다.

`WORKBENCH_HUB_AUTOSTART=0`은 자동 시작만 끄는 개발·테스트용 설정이다. `wb hub start`는 이 값과 무관하게 동작한다.

## `wb update`와 Hub

`wb update`는 backend 종료 전에 Hub를 먼저 멈춘다. Hub가 실행 중이면 확인 목록에 "Hub(pid N, version V)를 멈춥니다"가 더해지고(Board agent 세트가 돌고 있으면 그 목록도 함께 보이며 확인하면 세트도 끝난다), Board agent 세트가 없으면 Hub 정지만으로는 새 질문이 생기지 않는다. Hub를 멈추지 못하거나 상태를 알 수 없으면 어떤 backend도 종료하지 않고 업데이트를 중단한다(`wb hub stop` 안내). 업데이트가 끝나도 Hub를 다시 띄우지 않고, 다음 자동 시작 대상 `wb`가 새 설치본으로 띄운다. `wb hub status`가 새 version을 보인다.

## 부팅 자동 시작과 linger

`wb hub enable`은 `systemd --user` 서비스(`omp-workbench-hub.service`)를 만들고 enable한다. unit에는 `KillMode=process`가 있어 서비스를 멈춰도 Hub가 켠 backend·Board agent는 계속 실행되며, `enable` 시점의 `PATH`·`SHELL`·`LANG`·`LC_ALL`(있는 것)이 `Environment=`로 고정된다. 이전에 `enable`한 사용자와 `PATH`를 바꾼 사용자는 `wb hub enable`을 다시 실행한다(옛 형식이면 `wb hub status`가 안내한다). 이미 실행 중인 Hub는 바꾸지 않는다. systemd 사용자 서비스 관리자를 쓸 수 없으면 아무것도 바꾸지 않고 exit 2다. `wb hub disable`은 서비스를 끄고 지우며 실행 중인 Hub는 멈추지 않는다. 설치나 `wb`만으로는 서비스를 만들지 않는다.

linger가 꺼져 있으면 서비스는 로그인한 뒤에 시작한다. 로그인 전(부팅 직후)에 시작하려면 직접 `loginctl enable-linger`를 실행한다. `wb hub enable`은 linger를 바꾸지 않고 현재 상태와 필요한 명령만 알려 준다. 관리 terminal 안이거나 `/proc`을 읽을 수 없어 Hub가 시작을 거절하면(exit 3) 서비스는 재시작을 반복하지 않는다.

## 위치

| 대상 | 위치 |
|---|---|
| 상태 디렉터리 | `$XDG_STATE_HOME/omp-workbench/hub/` (기본 `~/.local/state/omp-workbench/hub/`) |
| Hub log | 상태 디렉터리의 `hub.log`(시작 때 1 MiB를 넘으면 `hub.log.1`로 회전) |
| 등록 목록·Hub 기록 | 상태 디렉터리의 `registry.json`, `hub.json` |
| 제어 socket | `$XDG_RUNTIME_DIR/omp-workbench-hub/hub.sock`, 없으면 `/tmp/omp-workbench-hub-<uid>/hub.sock` |
| 서비스 unit | `$XDG_CONFIG_HOME/systemd/user/omp-workbench-hub.service` (기본 `~/.config/...`) |

`wb hub status`가 현재 socket과 log 경로를 표시한다. Hub를 시작하지 못했을 때는 `hub.log`를 먼저 본다.
