# WB 업데이트와 명시적 작업 재개

Linux에서 현재 실행 중인 Python·distribution metadata·uv tool receipt가 일치하는 비editable Git 설치를 지원한다. `uv`와 Git은 이미 설치되어 있어야 한다. 개발 checkout, editable, wheel/sdist 직접 설치, 출처 불일치는 설명하고 거절한다. 기본 명령 `wb`와 alias `omp-workbench`는 같은 update parser를 사용한다.

```sh
wb update
wb update --ref release-tag
wb update --ref COMMIT
```

이 명령은 WB가 관리하지 않는 외부 terminal에서 실행한다. WB의 host shell이나 terminal 작업 안에서 실행하면 updater 자신도 종료 대상이 될 수 있으므로, backend·shell의 process identity와 caller의 ancestry/session을 대조해 작업 종료 전에 거절한다. caller identity를 확인하지 못한 경우도 외부 terminal에서 다시 실행해야 한다. updater를 자동으로 분리 실행하거나 자신의 작업을 종료 대상에서 제외하지 않는다.

기본 ref는 `main`이다. branch/tag/commit을 하나의 commit으로 확정한 뒤 별도 temporary uv tool 환경에서 build·dependency 준비를 확인한다. 목표 WB의 공개 `start`가 현재 update fence를 지키는지도 확인한다. 이 기능이 없는 과거 ref는 작업 종료 전에 거절한다. REF는 shell 명령이나 URL로 해석하지 않는다. 설치 출처의 인증 정보를 출력하지 않는다.

업데이트는 현재 사용자 runtime binding, 명시/현재 data root, 현재 사용자 WB process 후보를 대조한다. 실행 cwd에 프로젝트가 없어도 다른 프로젝트를 발견할 수 있다. 시작 중인 backend, source/process identity 불명, 남은 survivor·다른 session의 process, 종료 결과 불명, writer 잔존은 설치 진행을 허용하지 않는다. writer 확인은 백업 전후에 현재 사용자 process의 열린 fd 경로·access flags를 본다. capability를 가졌거나 non-dumpable인 같은 uid session process(`systemd --user`·`(sd-pam)`·`sshd`·`ssh-agent` 등)는 커널이 fd 목록을 보여 주지 않으므로 쓰기 여부를 확인하지 못한 채 건너뛰고, `확인하지 못한 process(쓰기 여부 미확인, 건너뜀): <pid> <comm>, …` 한 줄로 알린다. 읽을 수 있는 fd가 WB 원본을 가리키는데 쓰기 모드이거나 access flags를 확인할 수 없으면 pid·comm·경로를 표시하고 거절한다. 오래되거나 불일치하는 runtime binding이 있으면 소유자와 원본 위치를 확인한 뒤 다시 판단한다. 자동 정리하거나 작업 없음으로 간주하지 않는다.

진행 중인 작업은 프로젝트와 종료 영향을 먼저 표시하고 `[y/N]`으로 묻는다. 수동 foreground 명령과 같은 shell session의 실행·중지된 background job도 실제 PID/start ticks·process group으로 표시하고 동의에 결합한다. 표시 후 명령/job이 교체되면 이전 동의로 종료하지 않으며, shell 작업 identity가 불명이면 idle로 간주하지 않는다. `y` 외의 답변, EOF, Ctrl-C, 비대화형 입력은 작업을 종료하거나 pause·백업을 만들지 않는다. `--yes`나 `--force` 옵션은 제공하지 않는다. 작업이 없는 idle backend에는 별도 확인을 묻지 않는다. 사용자 범위 flock으로 동시 update를 막고, update 중 새 `wb start`도 거절한다.

동의한 작업은 기존 shutdown request/confirm 경로로 종료한다. 표시한 작업에 종료 동의를 결합하지 못하는 구버전 backend는 작업 종료 전에 거절한다. 해당 backend의 작업을 직접 확인·종료한 뒤 다시 업데이트한다. 현재 backend에서도 표시한 Task·run·process가 변경되거나 token이 만료되면 새 작업을 임의 종료하지 않는다. 종료 준비 hold 때문에 같은 experiment의 표시 상태만 `active`에서 `held`로 바뀐 경우에는 같은 run·process인지 비교해 진행한다. 새 자동 작업을 억제하고 원래 작업/Task/run/revision·프로젝트/worktree 경로·session·마지막 확인 상태·종료 결과를 유지보수 metadata로 보존한다. 일반 작업 기록의 manager/worker actor를 위조하거나 모델로 대화를 요약하지 않는다.

백업 기본 위치는 `XDG_STATE_HOME`(기본 `~/.local/state`) 아래 `omp-workbench/updates/<고유 run>/`이다. run/directory는 `0700`, 파일은 `0600`이며 기존 백업을 덮어쓰지 않는다. source와 겹치는 백업 위치는 거절한다. `manifest.json`에는 이전/목표 commit, 프로젝트 identity, 포함 파일의 hash, 제외 이유, 종료 결과, 프로젝트별 writer 확인(`writer_checks`의 백업 전후 `unverified_processes: [{pid, comm}]`), 백업 완료 여부, 설치 검증 결과를 남긴다.

백업에는 stopped WB root의 SQLite family, 모든 작업 기록 revision, 작업/실행 이력, OMP session/대화 이력이 포함된다. 원본 SQLite를 열어 schema나 sidecar를 만들지 않고, 복사한 main/WAL bytes와 in-memory SQLite로 검증한다. 프로젝트 소스·실험 산출물·외부 worktree·공유 `agent.db`와 credential 사본·재생성 cache는 제외한다. symlink를 따라 외부 파일을 수집하지 않는다. 원래 프로젝트와 `.wb/`는 제자리에 남으며, 미완성 백업으로 설치하지 않는다.

업데이트 후 원래 프로젝트에서 다음 명령을 실행한다.

```sh
wb start
# 또는 명시적인 기존 store
wb start --data-dir /path/to/project/.wb
```

이전 작업·양 역할 session·마지막 확인 상태·종료 결과·백업 위치가 표시된다. 원래 파일과 기록을 검토한 뒤 기존 작업 재개 절차로 명시적으로 계속한다. 프로세스 메모리나 중단된 terminal 명령의 연속성을 복원하지 않으며, 명령을 자동 재전송하거나 실험을 자동 재실행하지 않는다. 기존 취소·일시정지·실행 승인 상태를 임의로 해제하지 않는다. 일반 재개를 위해 backup을 원본에 덮어쓸 필요는 없다.

설치 실패는 성공으로 표시하지 않는다. 실패 출력과 manifest의 실제 `uv` exit, 이전/목표 commit, 백업 위치, 이전 ref 수동 재설치 명령을 확인한다. 실패가 원래 설치의 보존을 보장하지는 않는다. 인증 정보가 필요한 출처는 원래 인증 설정을 직접 사용해야 한다. 자동 rollback/restore·원격 백업·백업 자동 삭제 명령은 제공하지 않는다. OMP·연구 환경 패키지는 `wb update`의 대상이 아니다.
