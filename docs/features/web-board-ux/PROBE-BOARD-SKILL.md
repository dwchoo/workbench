# PROBE-BOARD-SKILL: Board overlay 아래 `wb-inbox` skill probe(WBX-B4, WBX-O02)

판정: **passed**. OMP `omp/18.13.2`, 모델 selector `openai-codex/gpt-6.1-sol:high`, 실행 2026-10-28T14:23:15+09:00, 15.5 s. 이 파일의 표·관측 절은 `tests/hub/probes/board_skill_probe.py --report …`가 생성한다.

## 판정 기준과 결과

| 확인 | 결과 |
|---|---|
| login at start: providers listed | ok |
| every OMP start: logged-in providers never shrank | ok |
| auth store: same inode before/after (never replaced) | ok |
| stage1 board_skill: isolation check ok, 0 leaks | ok |
| stage1 board_skill: skills == {wb-inbox} | ok |
| stage1 board_skill: no MCP tool | ok |
| stage1 board_skill: bash tool present | ok |
| stage1 board_skill: no decoy skill / prompt marker | ok |
| stage1 control_no_skill: no skill visible (skill_missing detectable) | ok |
| processes: none left | ok |
| omp tmp dir: unchanged | ok |
| stage2: the prompt was accepted | ok |
| stage2: model ran wb_inbox.py through bash | ok |
| stage2: hub served GET /api/v1/requests 200 with the system key | ok |
| stage2: every hub request carried the system key | ok |
| stage2: system key last_used_at recorded | ok |
| stage2: answer names every seeded title | ok |
| stage2: no MCP tool called | ok |
| stage2: key not in RPC stream | ok |

## 결과 해석(사람이 쓴 절, 재실행 2026-10-28)

선행 조건: WBX-B12(d3d4fa9) 통합, 사용자가 2026-10-28 12:52경 다시 로그인, `~/wb-demo` 정지와 그 인증 사본 삭제. 실행 직전 OMP 프로세스는 사용자 Hub의 Board agent 하나뿐이었고 그 `agent.db`는 사용자 store로의 symlink였다(`readlink`·`/proc/<pid>/fd`만 확인, 사본을 쓰는 OMP 없음). 사용자 Hub와 그 상태 디렉터리는 건드리지 않았다.

### 판정: passed

- 1단계: 후보 구성으로 `check_isolation(..., role="board", allowed_skills=("wb-inbox",))`가 `ok`, leak 0건. skill은 `wb-inbox` 하나(목록과 `/skill:wb-inbox` command), MCP tool 0개(agent dir에 이전 버전 `mcp.json`을 남겨 둔 상태), `bash` 있음, 미끼 skill·MCP·SYSTEM.md·AGENTS.md 0건. 음성 대조(빈 skill 디렉터리)는 skill 0개.
- 2단계(모델 `openai-codex/gpt-6.1-sol:high`, 한 turn, 9.6 s): 모델은 `read skill://wb-inbox` 다음 `bash`로 `python3 -I <hub state>/board-agent/skills/wb-inbox/wb_inbox.py list`를 실행했다. 절대 경로는 prompt에 없지만 `skill://` 읽기 결과로 알아냈다(1차 실행의 미확인 우려 해소). 테스트 Hub(S2, loopback)는 `GET /api/v1/requests` 1건을 200으로 받았고(system key, User-Agent `wb-inbox/1`), system key `last_used_at`이 `null` → 기록됨. 답은 seed한 3건의 제목과 개수를 모두 말했다. MCP tool 호출 0, RPC stream에 key 없음.
- 로그인: 시작 전·각 OMP 시작 뒤·끝의 authenticated는 모두 `openai-codex`, `openai-codex-device`로 같았다(1차 실행 첫 확인에 있던 `anthropic`은 이번 실행 전부터 없었다; 이번 실행 중 줄어든 것이 아니다).
- Auth store(`stat`만): inode 12477383과 크기 200704는 그대로. mtime은 OMP 시작마다 바뀌었다(아래 표). 모델 호출이 없는 시작(격리 확인, `get_login_providers`만)도 store에 쓴다는 뜻이다. `-wal`은 끝에 0 byte로 checkpoint되었다.

### stdin EOF 종료 측정(WBX-B12 지연)

OMP 18.13.2는 격리 확인 모드(`--mode rpc --no-session --no-title`)에서 stdin EOF만으로 스스로 끝났다. 다섯 번 모두 종료 단계 `eof`, returncode 0, EOF 뒤 0.13~0.31 s(모델 turn 뒤도 0.18 s). SIGTERM·SIGKILL은 한 번도 필요 없었다. 따라서 B12의 10 s 유예는 실제로는 시작 지연을 약 0.2 s만 더한다.

### WBX-B5가 쓸 구성

- Home: `omp_home.prepare_omp_home(<hub state>/board-agent, env, skills_dir=<hub state>/board-agent/skills, provider_ids=launcher.ISOLATION_PROVIDER_IDS, agents_dir=<빈 디렉터리>)`. `native` 포함 전체 목록이다(MCP 파일을 읽을 이유가 없다). Skill 복사: 시작마다 `<hub state>/board-agent/skills/wb-inbox/{SKILL.md, wb_inbox.py}`(디렉터리 0700, 파일 0600, `.env`·`.env.example` 없음).
- Overlay(두 번째 `--config`): 아래 "Overlay" JSON. `disabledProviders` = isolation 목록 전체, `skills.customDirectories=[<board skills>]`, `skills.includeSkills=["wb-inbox"]`, `ignoredSkills=[]`, `mcp.enableProjectConfig=false`, `task.disabledAgents=["seance"]`, `memory.backend="local"`, `memories.enabled=true`.
- argv는 지금과 같다(`--config omp-isolation.yml --config <overlay> --no-extensions --append-system-prompt <role> --no-title --allow-home [--continue] [--model …]`), cwd = `$HOME`. 이 probe는 role prompt 없이(`""`) 통과했다. ROLE.md에 skill 사용 안내를 넣는 것은 SPEC대로이고 절대 경로는 필수가 아니다.
- Env: `board_environment` + `WB_API_KEY=<이번 시작의 system key>` + `WB_HUB_URL=http://127.0.0.1:<내부 port>`(경로 없는 origin; `wb_inbox.py`가 `/api/v1/…`를 붙인다). 지금 `InternalListener.url`은 `…/mcp`를 돌려주므로 B5는 origin만 넘긴다. `NO_PROXY`/`no_proxy`에 `127.0.0.1`을 넣는다(`wb_inbox.py`는 urllib이라 `*_PROXY`를 따른다; 이 probe는 그렇게 실행했다).
- 격리 검사: 기대 = leak 0, `observed.skills == ["wb-inbox"]`(`skill_commands`의 `wb-inbox`는 허용 목록 안). `skills`에 `wb-inbox`가 없으면 `skill_missing:wb-inbox`. 이 판정은 `isolation_leaks`에 없으므로 B5가 `isolation_failure`에 더한다. 이전 버전 `<agent dir>/mcp.json`은 `native`가 꺼져 있어 읽히지 않지만 SPEC대로 시작 때 지운다.

### 그 밖의 관측

- OMP는 시작할 때 loopback의 로컬 모델 서버를 찾는다(OMP log: ollama `127.0.0.1:11434`, lm-studio `:1234`, llama.cpp `:8080`에 ECONNREFUSED). Hub 기본 port(8780)와는 겹치지 않는다.
- `get_login_providers`는 격리 확인에서 optional이라 1차 실행에서는 응답이 비는 일이 있었다. 이번에는 모든 확인에서 응답이 왔다. Probe는 응답이 없으면 따로 질의하도록 했다.
- OMP log(redacted)·stderr(모두 빈 파일)·시작별 기록은 `/tmp/wbx-b4-rerun/`에 남겼다(`logs/omp/<시작>/`, `starts.json`, `run.json`). 지우지 않는다.

## 1차 실행(2026-10-27)

판정 **inconclusive**(로그인 사용 불가). OMP `omp/18.12.6`, 실행 2026-10-27T15:49:13+09:00(첫 시도 15:40:55). 1단계 검사는 모두 통과했고, 2단계 prompt는 `No API key found for openai-codex.`로 거절되었다(모델·tool·Hub 요청 0건). 아래는 그때 사람이 쓴 해석과 2026-10-28 정정이다. 증거: `/tmp/wbx-b4-out/`.


#### 1단계(모델 호출 없음): 통과

- 후보 구성(아래 "WBX-B5가 쓸 구성")으로 `launcher.check_isolation(..., role="board", allowed_skills=("wb-inbox",))`가 `ok`, leak 0건이었다. 보이는 skill은 `wb-inbox` 하나(system prompt `<skills>` 목록과 `/skill:wb-inbox` command), MCP tool 0개, `bash` tool 있음.
- 미끼는 보이지 않았다: temp HOME의 `~/.omp/skills`, `~/.omp/agent/skills`, `~/.claude/skills`, `~/.codex/skills`, `~/.agents/skills`, Board agent dir의 `skills/`, `~/.omp/agent/mcp.json`·`~/.omp/mcp.json`·`~/.mcp.json`·`~/.claude.json`, `~/.omp/SYSTEM.md`·`~/.omp/agent/AGENTS.md`.
- Board agent dir에 이전 버전의 `mcp.json`(workbench-hub, `${WB_BOARD_MCP_KEY}`)을 남겨 두어도 MCP tool이 없었다: `native` provider를 다시 끄면(isolation 목록 전체) 그 파일은 읽히지 않는다. SPEC대로 시작 때 지우는 것은 위생으로 유지하면 된다.
- 음성 대조(`control_no_skill`, 같은 overlay에 빈 skill 디렉터리): skill 0개, 상태 `ok`. 즉 `wb-inbox`가 빠지면 `observed.skills`가 비어 `skill_missing:wb-inbox`를 판정할 수 있다(이 판정은 `isolation_leaks`에 없으므로 B5가 `isolation_failure`에 더해야 한다).
- OMP가 skill을 모델에 보이는 방식(18.12.6): `- wb-inbox: <description>` 목록과 "Matching skill → MUST read `skill://<name>` first", "`skill://<name>`: instructions; append `/<path>` for a file". 절대 경로는 prompt에 없다. SKILL.md는 `python3 -I <this folder>/wb_inbox.py`라고만 하므로 모델이 폴더 경로를 알아내야 한다(아래 B5 권고).

#### 2단계(실제 모델 한 turn): inconclusive — 로그인 사용 불가

- 모델 selector `openai-codex/gpt-6.1-sol:high`(Board 기본값 = manager `default`). OMP가 prompt를 즉시 거절했다: `No API key found for openai-codex.` 모델 호출·tool 호출·Hub 요청은 0건이고 system key의 `last_used_at`도 그대로(`null`)다. Skill 로드·bash·REST 경로는 **미확인**이다.
- 첫 full 실행(15:40:55)은 probe 결함으로 거절 응답 뒤에도 agent_end를 300 s 기다렸다. 결함을 고치고(get_state 응답 뒤 prompt, 거절 시 즉시 종료) 15:49:13에 다시 실행했고 같은 거절이 나왔다. 아래 관측은 두 번째 실행이다.
- **주의(사용자 확인 필요)**: 첫 1단계 실행(15:40:33 시작, 2.7 s)에서 `board_skill` 확인은 `get_login_providers`의 authenticated에 `anthropic`, `openai-codex`, `openai-codex-device`를 보였다. 몇 초 뒤 같은 실행의 `control_no_skill` 확인과 이후 모든 실행은 authenticated가 비어 있었다. 사용자 auth store `~/.omp/agent/agent.db`의 mtime이 15:40:35.347로, 그 첫 확인 OMP가 실행 중인 시각이다. 즉 probe가 띄운 OMP(제품 설계대로 `agent.db`가 사용자 store로의 symlink)가 사용자 store에 무언가를 썼고, 그 뒤 openai-codex 자격 증명이 보이지 않는다. 원인은 확인하지 않았다(store를 열지 않았다: `omp_home` 원칙과 자격 증명 비노출 규칙). 가능성: OAuth refresh 실패 뒤 차단/삭제(`auth_credential_blocks`·`DELETE FROM auth_credentials` 경로가 바이너리에 있음), 또는 다른 HOME(temp, 새 `install-id`)에 묶인 동작. `get_login_providers`는 check에서 optional이라 응답이 늦으면 비는 경우도 있으나, prompt 거절은 그것과 무관하다.
- 사용자 영향: 같은 store를 쓰는 사용자 자신의 `omp`와 실행 중인 Workbench(manager/worker)의 openai-codex 로그인도 영향을 받았을 수 있다. 사용자가 `omp`에서 로그인 상태를 확인하고 필요하면 다시 로그인해야 한다. 그 전에는 이 probe의 2단계를 다시 돌리지 않는다.
- 정정(2026-10-28): 위 "실행 중인 Workbench(manager/worker)"가 같은 store를 쓴다는 서술은 틀렸다. 그 Workbench(`~/wb-demo`)는 `agent.db` 사본을 썼다. 원인 추정·완화·재실행 조건은 [INCIDENT-20261027-omp-login.md](INCIDENT-20261027-omp-login.md)와 BRIEF WBX-D31~D33.

#### WBX-B5가 쓸 구성(1단계로 확인된 부분)

- Home: `omp_home.prepare_omp_home(<hub state>/board-agent, env, skills_dir=<hub state>/board-agent/skills, provider_ids=ISOLATION_PROVIDER_IDS(native 포함 전체), agents_dir=<empty>)`. Skill 복사: `<hub state>/board-agent/skills/wb-inbox/{SKILL.md, wb_inbox.py}`(디렉터리 0700, 파일 0600, `.env`·`.env.example` 없음).
- Overlay(두 번째 `--config`): 아래 "Overlay" 절의 JSON에서 `customDirectories`를 위 skills 디렉터리로. 핵심: `disabledProviders` = isolation 목록 전체(`native` 다시 끔; MCP 파일을 읽을 이유가 없다), `skills.customDirectories=[<board skills>]`, `skills.includeSkills=["wb-inbox"]`, `ignoredSkills=[]`, `mcp.enableProjectConfig=false`, `task.disabledAgents=["seance"]`, `memory.backend="local"`, `memories.enabled=true`.
- argv: 기존과 같음(`--config omp-isolation.yml --config <overlay> --no-extensions --append-system-prompt <role> --no-title --allow-home [--continue] [--model …]`), cwd = `$HOME`.
- Env: `board_environment` + `WB_API_KEY=<이번 시작의 system key>` + `WB_HUB_URL=http://127.0.0.1:<내부 port>`(경로 없는 origin; `wb_inbox.py`가 `/api/v1/…`를 붙인다). 내부 listener의 `url`은 지금 `…/mcp`를 돌려주므로 B5에서 origin만 넘긴다. Loopback이므로 `NO_PROXY`에 `127.0.0.1`이 있어야 사용자 proxy 설정을 타지 않는다(이 probe는 그렇게 실행했다; `wb_inbox.py`는 urllib이라 `*_PROXY`를 따른다).
- 격리 검사: `check_isolation(..., allowed_skills=("wb-inbox",))`; 기대 = leak 0, `observed.skills == ["wb-inbox"]`(`skill_commands`에도 `wb-inbox`가 나온다, 허용 목록 안). `wb-inbox`가 `skills`에 없으면 `skill_missing:wb-inbox`.
- 권고(미검증, 2단계 필요): OMP prompt에 skill 절대 경로가 없으므로 ROLE.md 또는 launcher가 추가하는 한 줄에 `<hub state>/board-agent/skills/wb-inbox/wb_inbox.py` 절대 경로를 적는다(모델이 `skill://wb-inbox`만으로 경로를 찾는지는 확인하지 못했다).


## 환경

```json
{
  "omp": "<user home>/.local/bin/omp",
  "omp_version": "omp/18.13.2",
  "login": {
    "model": "openai-codex/gpt-6.1-sol:high",
    "provider": "openai-codex",
    "authenticated_providers": [
      "openai-codex",
      "openai-codex-device"
    ],
    "usable": true
  },
  "user_auth_store": {
    "state": "present",
    "ok": true,
    "path": "<user home>/.omp/agent/agent.db"
  },
  "board_home": {
    "root": "<root>/hub-state/board-agent/omp-root",
    "agent_dir": "<root>/hub-state/board-agent/omp-root/agent",
    "config_dir": "../hub-state/board-agent/omp-root",
    "linked": true,
    "notes": [],
    "environment": {
      "PI_CONFIG_DIR": "../hub-state/board-agent/omp-root",
      "PI_CODING_AGENT_DIR": "<root>/hub-state/board-agent/omp-root/agent",
      "BUN_RUNTIME_TRANSPILER_CACHE_PATH": "<root>/hub-state/board-agent/omp-root/bun-transpiler-cache"
    }
  },
  "skill_dir_listing": [
    "SKILL.md",
    "wb_inbox.py"
  ],
  "stale_mcp_json": "<root>/hub-state/board-agent/omp-root/agent/mcp.json",
  "decoy_mcp": [
    "<root>/home/.omp/agent/mcp.json",
    "<root>/home/.omp/mcp.json",
    "<root>/home/.mcp.json",
    "<root>/home/.claude.json"
  ],
  "decoy_skills": [
    "decoy-home-omp-skill",
    "decoy-home-agent-skill",
    "decoy-claude-skill",
    "decoy-codex-skill",
    "decoy-agents-skill",
    "decoy-board-agentdir-skill"
  ]
}
```

Overlay(두 번째 `--config`):

```json
{
  "disabledProviders": [
    "native",
    "omp-managed",
    "skillshare",
    "agents-md",
    "agents",
    "claude-md",
    "claude-plugins",
    "claude",
    "cline",
    "codex",
    "cursor",
    "gemini",
    "github",
    "mcp-json",
    "omp-plugins",
    "opencode",
    "ssh-json",
    "vscode",
    "windsurf",
    "agent-plugins",
    "builtin-defaults"
  ],
  "mcp": {
    "enableProjectConfig": false
  },
  "skills": {
    "customDirectories": [
      "<root>/hub-state/board-agent/skills"
    ],
    "includeSkills": [
      "wb-inbox"
    ],
    "ignoredSkills": []
  },
  "task": {
    "disabledAgents": [
      "seance"
    ]
  },
  "memory": {
    "backend": "local"
  },
  "memories": {
    "enabled": true
  }
}
```

## OMP 시작별 기록(로그인 provider, auth store stat, 종료 단계)

OMP log(stderr 전체·OMP 자체 log, 둘 다 `redact_log`)는 `/tmp/wbx-b4-rerun/logs/`에 남겼다(지우지 않음).

| 시작 | authenticated | 종료 단계 | EOF 대기 s | 전체 종료 s | agent.db mtime | size | inode |
|---|---|---|---|---|---|---|---|
| login-start | openai-codex, openai-codex-device | eof | 0.177 | 0.204 | 2026-10-28T14:23:17.175691+09:00 | 200704 | 12477383 |
| board_skill | openai-codex, openai-codex-device | eof | 0.132 | 0.159 | 2026-10-28T14:23:18.034713+09:00 | 200704 | 12477383 |
| control_no_skill | openai-codex, openai-codex-device | eof | 0.141 | 0.164 | 2026-10-28T14:23:19.116740+09:00 | 200704 | 12477383 |
| model_turn | (이 시작에서는 묻지 않음) | eof | 0.178 | 0.196 | 2026-10-28T14:23:29.334002+09:00 | 200704 | 12477383 |
| login-end | openai-codex, openai-codex-device | eof | 0.314 | 0.338 | 2026-10-28T14:23:31.037045+09:00 | 200704 | 12477383 |

Auth store(`stat`만, 열지 않음) 실행 전·후:

```json
{
  "before": {
    "agent.db": {
      "mtime": "2026-10-28T14:20:11.396937+09:00",
      "size": 200704,
      "inode": 12477383
    },
    "agent.db-wal": {
      "mtime": "2026-10-28T14:20:12.711971+09:00",
      "size": 24752,
      "inode": 12477389
    },
    "agent.db-shm": {
      "mtime": "2026-10-28T14:20:12.710971+09:00",
      "size": 32768,
      "inode": 12492848
    }
  },
  "after": {
    "agent.db": {
      "mtime": "2026-10-28T14:23:31.037045+09:00",
      "size": 200704,
      "inode": 12477383
    },
    "agent.db-wal": {
      "mtime": "2026-10-28T14:23:31.058046+09:00",
      "size": 0,
      "inode": 12477389
    },
    "agent.db-shm": {
      "mtime": "2026-10-28T14:23:30.729037+09:00",
      "size": 32768,
      "inode": 12492848
    }
  }
}
```

## 관측

### board_skill

```json
{
  "state": "ok",
  "error": null,
  "leaks": [],
  "warnings": [],
  "skills": [
    "wb-inbox"
  ],
  "skill_commands": [
    "wb-inbox"
  ],
  "mcp_tools": [],
  "tools": [
    "read",
    "bash",
    "edit",
    "eval",
    "glob",
    "grep",
    "task",
    "wait",
    "todo",
    "web_search",
    "write"
  ],
  "task_agents": [
    "scout",
    "reviewer",
    "security-reviewer",
    "task",
    "sonic"
  ],
  "other_commands": [
    [
      "autoresearch",
      "extension"
    ],
    [
      "init",
      "file"
    ]
  ],
  "context_files": [],
  "system_prompt_default": true,
  "model": "openai-codex/gpt-6.1-sol",
  "omp_home": {
    "config_root_in_use": true,
    "agent_dir_in_use": true,
    "user_paths": [],
    "authenticated_providers": [
      "openai-codex",
      "openai-codex-device"
    ]
  },
  "decoy_markers_in_prompt": [],
  "decoy_skills_in_prompt": [],
  "skill_prompt_lines": [
    "Matching skill → MUST read `skill://<name>` first.",
    "- wb-inbox: Send work requests to a Workbench project inbox and follow them with the Hub's /api/v1/ API.",
    "- `skill://<name>`: instructions; append `/<path>` for a file."
  ],
  "cleanup": {
    "pid": 3034497,
    "start_ticks": 103561454,
    "state": "dead",
    "returncode": 0,
    "group_left": [],
    "stage": "eof",
    "stage_seconds": {
      "eof": 0.132
    },
    "elapsed": 0.159
  },
  "argv": [
    "/tmp/wbx-b4-rerun/bin/omp-board_skill.sh",
    "--config",
    "<repo>/omp_bridge/omp-isolation.yml",
    "--config",
    "<root>/hub-state/board-agent/omp-board.yml",
    "--no-extensions",
    "--append-system-prompt",
    "",
    "--no-title",
    "--allow-home",
    "--model",
    "openai-codex/gpt-6.1-sol:high",
    "--mode",
    "rpc",
    "--no-session",
    "--no-title"
  ]
}
```

### control_no_skill

```json
{
  "state": "ok",
  "error": null,
  "leaks": [],
  "warnings": [],
  "skills": [],
  "skill_commands": [],
  "mcp_tools": [],
  "tools": [
    "read",
    "bash",
    "edit",
    "eval",
    "glob",
    "grep",
    "task",
    "wait",
    "todo",
    "web_search",
    "write"
  ],
  "task_agents": [
    "scout",
    "reviewer",
    "security-reviewer",
    "task",
    "sonic"
  ],
  "other_commands": [
    [
      "autoresearch",
      "extension"
    ],
    [
      "init",
      "file"
    ]
  ],
  "context_files": [],
  "system_prompt_default": true,
  "model": "openai-codex/gpt-6.1-sol",
  "omp_home": {
    "config_root_in_use": true,
    "agent_dir_in_use": true,
    "user_paths": [],
    "authenticated_providers": [
      "openai-codex",
      "openai-codex-device"
    ]
  },
  "decoy_markers_in_prompt": [],
  "decoy_skills_in_prompt": [],
  "skill_prompt_lines": [],
  "cleanup": {
    "pid": 3034553,
    "start_ticks": 103561536,
    "state": "dead",
    "returncode": 0,
    "group_left": [],
    "stage": "eof",
    "stage_seconds": {
      "eof": 0.141
    },
    "elapsed": 0.164
  },
  "argv": [
    "/tmp/wbx-b4-rerun/bin/omp-control_no_skill.sh",
    "--config",
    "<repo>/omp_bridge/omp-isolation.yml",
    "--config",
    "<root>/hub-state/board-agent/omp-board-empty.yml",
    "--no-extensions",
    "--append-system-prompt",
    "",
    "--no-title",
    "--allow-home",
    "--model",
    "openai-codex/gpt-6.1-sol:high",
    "--mode",
    "rpc",
    "--no-session",
    "--no-title"
  ]
}
```

### model_turn

```json
{
  "argv": [
    "/tmp/wbx-b4-rerun/bin/omp-model_turn.sh",
    "--config",
    "<repo>/omp_bridge/omp-isolation.yml",
    "--config",
    "<root>/hub-state/board-agent/omp-board.yml",
    "--no-extensions",
    "--append-system-prompt",
    "",
    "--no-title",
    "--allow-home",
    "--model",
    "openai-codex/gpt-6.1-sol:high",
    "--mode",
    "rpc",
    "--no-session"
  ],
  "prompt": "Use the wb-inbox skill to list the requests in the Workbench inbox. Reply with how many requests there are and their titles.",
  "duration": 9.6,
  "timed_out": false,
  "agent_end": true,
  "state_answered_after_s": 0.7,
  "ui_requests": [
    {
      "method": "setWidget",
      "title": ""
    },
    {
      "method": "setWidget",
      "title": ""
    }
  ],
  "prompt_response": {
    "success": true,
    "error": ""
  },
  "event_types": {
    "agent_end": 1,
    "agent_start": 1,
    "available_commands_update": 1,
    "extension_ui_request": 2,
    "message_end": 6,
    "message_start": 6,
    "message_update": 124,
    "ready": 1,
    "response": 2,
    "tool_execution_end": 2,
    "tool_execution_start": 2,
    "tool_execution_update": 2,
    "turn_end": 3,
    "turn_start": 3
  },
  "tool_calls": [
    {
      "tool": "read",
      "path": "skill://wb-inbox",
      "arg_keys": [
        "path"
      ]
    },
    {
      "tool": "bash",
      "command": "python3 -I <root>/hub-state/board-agent/skills/wb-inbox/wb_inbox.py list",
      "arg_keys": [
        "command"
      ]
    }
  ],
  "tool_results": [
    {
      "tool": "read",
      "isError": false,
      "result_chars": 7909,
      "mentions_seeded_title": false
    },
    {
      "tool": "bash",
      "isError": false,
      "result_chars": 1953,
      "mentions_seeded_title": true
    }
  ],
  "assistant_errors": [],
  "answer": {
    "chars": 120,
    "titles_named": {
      "B4 probe request one": true,
      "B4 probe request two": true,
      "B4 probe request three": true
    },
    "count_3_stated": true
  },
  "key_in_rpc_stream": false,
  "returncode": 0,
  "cleanup": {
    "pid": 3034698,
    "start_ticks": 103561701,
    "state": "dead",
    "returncode": 0,
    "group_left": [],
    "stage": "eof",
    "stage_seconds": {
      "eof": 0.178
    },
    "elapsed": 0.196
  },
  "hub_requests": [
    {
      "method": "GET",
      "path": "/api/v1/requests",
      "status": 200,
      "query_keys": [],
      "authorization": "Bearer <system key>",
      "user_agent": "wb-inbox/1"
    }
  ],
  "system_key_last_used": {
    "before": null,
    "after": 1000000.0
  },
  "seeded": 3,
  "hub_url": "http://127.0.0.1:<port>"
}
```

## 정리

```json
{
  "leftover_processes_final": [],
  "root_removed": true,
  "omp_tmp": {
    "dir": "/tmp/omp-workbench-1000",
    "before": 0,
    "after": 0,
    "new": []
  },
  "natives": {
    "bytes": 117046824,
    "entries": [
      "18.13.2"
    ]
  }
}
```
