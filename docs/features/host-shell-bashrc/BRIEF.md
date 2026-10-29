# Host shell에서 사용자 `~/.bashrc` 적용 — 요구사항 합의

Revision: r0.1. 상태: 구현 완료(미커밋). 사용자 요청(2026-10-29): "~/.bashrc가 적용이 안 돼서 불편. 명령 완료 감지가 유지되면서 ~/.bashrc도 되면 좋겠다. 개발·테스트해서 먼저 push." 아래 HSB-D01~D07은 같은 날 orchestrator가 정한 설계 결정이고, HSB-D03은 테스트로 확정했다.

## 문제와 목표

- WB 프로젝트 host shell(Bash)은 `bash --noprofile --rcfile <tmp>/shell-init -i`로 시작한다(`src/workbench/terminal/shell_g2/prototype.py`). `--rcfile`이 `~/.bashrc`를 대신하므로 사용자의 alias·function·PATH·env·conda/nvm/pyenv 초기화가 하나도 적용되지 않았다.
- 명령 완료 감지는 Workbench가 init에서 정한 `PROMPT_COMMAND='__b_emit READY'`, `PS1='$ '`, CHLD trap 없음에 의존한다(`shell_g2/lifecycle.py` `__b_hook_compatible`). 이 값이 바뀌면 HOOK_REJECTED/HOOK_LOST로 자동 실행이 보류된다.
- 목표: 완료 감지·handoff·자동 실행을 그대로 두면서 host shell이 사용자 `~/.bashrc`를 적용한다.
- 원래 명세 `docs/OMP_Workbench_Development_Spec_v0.3.md` §5.2는 "사용자의 초기화 파일에서 로드한 상태는 유지한다"였다. 지금까지의 `--rcfile` 대체는 이 문장과 어긋났고, 이 기능으로 다시 맞춘다. C-D41("사용자 기본 shell 설정은 변경하지 않는다", 전용 persistent Bash)은 그대로다. 사용자 파일은 읽기만 하고 고치지 않는다.

## 범위와 비목표

- 포함: 제품 host shell(`ShellPane`, 시작과 `restart_pane` 재시작), Bash만.
- 비목표: sh(dash) host shell(지금처럼 `ENV`만 다룸), `~/.bash_profile`·`~/.profile`(login 파일, `--noprofile` 유지), 사용자 prompt 테마 유지, 시작 뒤 사용자가 직접 바꾼 PS1/PROMPT_COMMAND(예: 수동 `conda activate`)의 처리(C-D55 범위 밖 그대로), workflow 격리 실행용 별도 `PersistentShell`(기본값 `load_user_rc=False`라 변화 없음).

## 결정

| ID | 결정 |
|---|---|
| HSB-D01 | Host Bash init은 먼저 `export WORKBENCH_SHELL=1`을 하고, `$HOME/.bashrc`가 읽을 수 있는 일반 파일이면 source한다(bash의 interactive non-login 동작과 같다). HOME은 shell에 넘기는 환경의 값이다. 파일이 없거나 읽을 수 없거나 디렉터리면 그냥 건너뛴다(상태 `absent`). 사용자는 `[ -n "$WORKBENCH_SHELL" ]`로 rc 일부를 Workbench에서 건너뛸 수 있다. |
| HSB-D02 | rc 뒤에 Workbench가 필요한 것을 마지막에 다시 정한다. 모든 명령은 `builtin`으로 부르므로(첫 줄은 `\builtin`) 사용자 alias·함수(`trap() { :; }`, `alias set=:` 등)가 이 reset을 막지 못한다: `trap - DEBUG RETURN ERR CHLD`(EXIT 등 다른 trap은 유지), `set +aeuxvETC +o pipefail`, `shopt -u extdebug`, `unset PROMPT_COMMAND`(배열 포함) 뒤 Workbench의 `PROMPT_COMMAND='__b_emit READY'`와 `PS1='$ '`. noclobber(`-C`)는 끄고, controller가 자기 파일에 쓰는 곳은 모두 `>|`이라 사용자가 나중에 `set -C`해도 handoff trap 목록이 갱신된다. allexport(`-a`)는 **꺼진 채로 둔다**: 켜 두면 controller가 매 handoff·실행에서 대입하는 `BOUNDARY_*`·`__b_*` 변수가 export되기 때문이다. Persistent Bash controller 함수도 `builtin printf/trap/read/jobs/compgen/eval/[/return/continue/alias`, 파일은 `$(<file)`, `builtin command sleep`을 써서 같은 이름의 사용자 함수(예: `printf() { :; }`)가 있어도 동작한다(그 함수는 사용자 prompt에서 그대로 남는다). Workbench 함수는 `expand_aliases`를 끈 채 정의하고 정의 뒤 다시 켠다. 사용자 alias·function·env·PATH·conda 등 초기화 결과는 그대로 남는다. 사용자 prompt 테마는 의도적으로 버린다(완료 감지가 `PS1='$ '`를 요구한다). 사용자 `PROMPT_COMMAND`는 실행하지 않는다: 기존 `PRIOR_HOOK`은 고정 문자열 `__b_emit PRIOR_HOOK; __b_emit READY`를 허용하는 probe용 형태일 뿐 임의 hook을 실행하는 장치가 아니어서 쓰지 않는다. |
| HSB-D03 | rc의 `set -o vi`와 readline 설정은 유지한다. vi 명령 모드는 ESC로만 들어가고, 경계 추적(`InputBoundary.observe_manual_bytes`)은 ESC를 이미 알 수 없는 편집으로 보아 다음 READY까지 자동 실행만 보류한다. vi로 고친 줄의 완료, 이후 `wb-handoff`와 자동 실행이 emacs 모드와 같게 동작함을 테스트로 확인했다(`ReadlineTests`). |
| HSB-D04 | rc는 fd 7·8·9를 닫은 채 실행한다(`. ~/.bashrc 7<&- 8<&- 9>&-`). bash가 끝난 뒤 원래 fd를 되돌리므로 rc가 이 번호를 닫거나 다른 파일로 열어도(예: `exec 9>lock`) 제어 채널은 그대로이고, rc가 남긴 background 프로그램도 제어 fd를 갖지 않는다. rc가 같은 번호로 연 fd는 rc가 끝날 때 닫힌다. |
| HSB-D05 | 안전 fallback: rc가 10초(`USER_RC_SECONDS`) 안에 첫 prompt까지 가지 못하거나(입력 대기 `read`, tmux 자동 실행, `sleep`, 느린 init), 셸이 끝나거나(`exit`), 다른 프로그램으로 바뀌거나(`exec zsh`/`exec sh`), 되돌릴 수 없는 상태를 남기면 그 셸과 session을 끝내고 rc 없이 다시 시작한다. 되돌릴 수 없는 상태: readonly `PS1`/`PROMPT_COMMAND`, `builtin`이라는 이름의 함수(이유: `Workbench가 쓰는 명령을 함수로 재정의함(builtin)`), controller가 쓰는 builtin을 `enable -n`으로 끈 것(이유: `Workbench가 쓰는 builtin을 끔(enable -n trap)`; 관계없는 builtin을 끈 것은 그대로 둔다). 두 경우 모두 reset이 먼저 기록한 뒤 init을 끝낼 수 있게 되돌려 두므로 10초를 기다리지 않고 원인이 이유에 나온다. pane 첫 줄에 `[Workbench] ~/.bashrc 적용 실패: <이유> — 기본 설정으로 시작`을 보이고 shell 상태 `user_rc`에 `{"state": "fallback", "reason": ...}`를 남긴다. 판단은 init이 첫 prompt 전에 쓰는 보고 파일(`rc-status`, `builtin`만 사용), READY, `/proc`(종료·exe 변경)로 하며 아무것도 입력하지 않는다. rc의 시작 출력은 pane에 그대로 보인다(성공 시). |
| HSB-D07 | rc가 `cd`해도 host shell은 띄운 디렉터리(프로젝트 dir)에서 시작한다. init이 rc 전 `$PWD`·`OLDPWD`를 저장하고, rc 뒤 `$PWD`가 다르면 `builtin cd -- <시작 dir>`로 돌아가며 `OLDPWD`는 rc 전 값(없으면 unset)으로 되돌린다. 시작 디렉터리로 돌아갈 수 없으면(삭제 등) 셸의 현재 디렉터리를 유지하고 `user_rc.reason`에 적는다(상태는 `applied`). |
| HSB-D06 | `restart_pane{host_shell}`은 같은 `ShellPane` 생성 경로를 쓰므로 rc 적용·fallback도 같다. sh(dash) host shell은 바뀌지 않는다(`WORKBENCH_SHELL`도 넣지 않음). |

## 공개 동작

- `ShellPane.shell_state()["user_rc"]`(backend snapshot의 `panes.host_shell.shell.user_rc`): Bash host shell이면 `{"state": "applied" | "absent" | "fallback", "path": "$HOME/.bashrc", "reason": None | "<한 줄 이유>"}`, sh면 `None`.
- `PersistentShell(..., load_user_rc=True)`가 이 동작을 켠다. `ShellPane`만 켠다.
- 남는 한계: rc가 `&`로 띄우고 `disown`하지 않은 job은 host shell job으로 보여 handoff의 jobs 확인에 걸린다. 시작 뒤 사용자가 PS1/PROMPT_COMMAND를 바꾸면 지금처럼 자동 실행이 보류된다.
- PS0와 `bind -x`: rc가 정한 `PS0`(명령 실행 직전 출력)와 `bind -x` 키 바인딩은 그대로 남아 그 출력이 pane에 보인다. 완료 감지에는 영향이 없다.
- 시작·재시작 대기: `ShellPane` 생성(backend 시작과 `restart_pane`)은 rc가 끝나거나 fallback할 때까지 최대 10초 동기로 기다린다. host shell은 첫 READY가 확인된 뒤에야 입력 경계·자동 실행 대상이 되므로 생성 단계에서 결과(applied/fallback)를 확정한다. backend 시작 한도(READY 90초, start 120초) 안이다.
- fallback 뒤 남는 프로세스: fallback은 rc를 실행한 셸의 PTY session만 끝낸다. rc가 session 밖으로 daemon화한 프로세스(ssh-agent, tmux server, `setsid`로 띄운 것)는 C-D45 규칙대로 신호를 보내지 않아 남는다.

## 테스트

- `tests/terminal/test_host_shell_bashrc.py`: 실제 `ShellPane`→`PersistentShell`→`/usr/bin/bash`, temp HOME의 rc. alias·function·PATH·env 적용, 상태 applied/absent(없음·읽기 불가·디렉터리), 시작 출력, PS1 테마·PROMPT_COMMAND 문자열·배열, `set -euxo pipefail -o errtrace -o functrace`·extdebug, CHLD/DEBUG/ERR/RETURN trap 제거와 EXIT 유지, fd 7/8/9 닫기·재열기, rc가 남긴 background 프로그램의 fd, fallback(`exec sh`, `exit`, `read`, `sleep 60`, readonly PS1·PROMPT_COMMAND, `builtin` 함수, `enable -n trap`·`enable -n echo`), noclobber에서 두 번째 handoff의 CHLD 거부, `trap`·`unset`·`printf` 등 함수와 `alias set=:`·`alias builtin=:`가 있어도 reset·자동 실행 동작(이전 기대 "`printf` 함수→fallback"은 "적용되고 동작"으로 바뀜), `set -a`에서 `BOUNDARY_*`·`__b_*` 비export, conda 형식 init, alias 가로채기, `set -o vi`, rc `cd` 되돌리기·삭제된 시작 디렉터리, sh 불변. 각 경우 수동 줄의 READY와 `wb-handoff`→자동 실행의 `RETURN:0`을 확인한다.
- `tests/backend/test_host_shell_bashrc_restart.py`: 실제 Backend에서 host shell 종료→`restart_pane` 뒤 rc 적용, 다시 fallback.
