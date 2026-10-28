# PROBE-BOARD-SKILL: Board overlay 아래 `wb-inbox` skill probe(WBX-B4, WBX-O02)

판정: **inconclusive**. OMP `omp/18.12.6`, 모델 selector `openai-codex/gpt-6.1-sol:high`, 실행 2026-10-27T15:49:13+09:00, 4.7 s. 이 파일의 표·관측 절은 `tests/hub/probes/board_skill_probe.py --report …`가 생성한다.

Inconclusive 사유: login unavailable: OMP refused the prompt (No API key found for openai-codex.)

## 판정 기준과 결과

| 확인 | 결과 |
|---|---|
| stage1 board_skill: isolation check ok, 0 leaks | ok |
| stage1 board_skill: skills == {wb-inbox} | ok |
| stage1 board_skill: no MCP tool | ok |
| stage1 board_skill: bash tool present | ok |
| stage1 board_skill: no decoy skill / prompt marker | ok |
| stage1 control_no_skill: no skill visible (skill_missing detectable) | ok |
| processes: none left | ok |
| omp tmp dir: unchanged | ok |

## 결과 해석(사람이 쓴 절)

### 1단계(모델 호출 없음): 통과

- 후보 구성(아래 "WBX-B5가 쓸 구성")으로 `launcher.check_isolation(..., role="board", allowed_skills=("wb-inbox",))`가 `ok`, leak 0건이었다. 보이는 skill은 `wb-inbox` 하나(system prompt `<skills>` 목록과 `/skill:wb-inbox` command), MCP tool 0개, `bash` tool 있음.
- 미끼는 보이지 않았다: temp HOME의 `~/.omp/skills`, `~/.omp/agent/skills`, `~/.claude/skills`, `~/.codex/skills`, `~/.agents/skills`, Board agent dir의 `skills/`, `~/.omp/agent/mcp.json`·`~/.omp/mcp.json`·`~/.mcp.json`·`~/.claude.json`, `~/.omp/SYSTEM.md`·`~/.omp/agent/AGENTS.md`.
- Board agent dir에 이전 버전의 `mcp.json`(workbench-hub, `${WB_BOARD_MCP_KEY}`)을 남겨 두어도 MCP tool이 없었다: `native` provider를 다시 끄면(isolation 목록 전체) 그 파일은 읽히지 않는다. SPEC대로 시작 때 지우는 것은 위생으로 유지하면 된다.
- 음성 대조(`control_no_skill`, 같은 overlay에 빈 skill 디렉터리): skill 0개, 상태 `ok`. 즉 `wb-inbox`가 빠지면 `observed.skills`가 비어 `skill_missing:wb-inbox`를 판정할 수 있다(이 판정은 `isolation_leaks`에 없으므로 B5가 `isolation_failure`에 더해야 한다).
- OMP가 skill을 모델에 보이는 방식(18.12.6): `- wb-inbox: <description>` 목록과 "Matching skill → MUST read `skill://<name>` first", "`skill://<name>`: instructions; append `/<path>` for a file". 절대 경로는 prompt에 없다. SKILL.md는 `python3 -I <this folder>/wb_inbox.py`라고만 하므로 모델이 폴더 경로를 알아내야 한다(아래 B5 권고).

### 2단계(실제 모델 한 turn): inconclusive — 로그인 사용 불가

- 모델 selector `openai-codex/gpt-6.1-sol:high`(Board 기본값 = manager `default`). OMP가 prompt를 즉시 거절했다: `No API key found for openai-codex.` 모델 호출·tool 호출·Hub 요청은 0건이고 system key의 `last_used_at`도 그대로(`null`)다. Skill 로드·bash·REST 경로는 **미확인**이다.
- 첫 full 실행(15:40:55)은 probe 결함으로 거절 응답 뒤에도 agent_end를 300 s 기다렸다. 결함을 고치고(get_state 응답 뒤 prompt, 거절 시 즉시 종료) 15:49:13에 다시 실행했고 같은 거절이 나왔다. 아래 관측은 두 번째 실행이다.
- **주의(사용자 확인 필요)**: 첫 1단계 실행(15:40:33 시작, 2.7 s)에서 `board_skill` 확인은 `get_login_providers`의 authenticated에 `anthropic`, `openai-codex`, `openai-codex-device`를 보였다. 몇 초 뒤 같은 실행의 `control_no_skill` 확인과 이후 모든 실행은 authenticated가 비어 있었다. 사용자 auth store `~/.omp/agent/agent.db`의 mtime이 15:40:35.347로, 그 첫 확인 OMP가 실행 중인 시각이다. 즉 probe가 띄운 OMP(제품 설계대로 `agent.db`가 사용자 store로의 symlink)가 사용자 store에 무언가를 썼고, 그 뒤 openai-codex 자격 증명이 보이지 않는다. 원인은 확인하지 않았다(store를 열지 않았다: `omp_home` 원칙과 자격 증명 비노출 규칙). 가능성: OAuth refresh 실패 뒤 차단/삭제(`auth_credential_blocks`·`DELETE FROM auth_credentials` 경로가 바이너리에 있음), 또는 다른 HOME(temp, 새 `install-id`)에 묶인 동작. `get_login_providers`는 check에서 optional이라 응답이 늦으면 비는 경우도 있으나, prompt 거절은 그것과 무관하다.
- 사용자 영향: 같은 store를 쓰는 사용자 자신의 `omp`와 실행 중인 Workbench(manager/worker)의 openai-codex 로그인도 영향을 받았을 수 있다. 사용자가 `omp`에서 로그인 상태를 확인하고 필요하면 다시 로그인해야 한다. 그 전에는 이 probe의 2단계를 다시 돌리지 않는다.
- 정정(2026-10-28): 위 "실행 중인 Workbench(manager/worker)"가 같은 store를 쓴다는 서술은 틀렸다. 그 Workbench(`~/wb-demo`)는 `agent.db` 사본을 썼다. 원인 추정·완화·재실행 조건은 [INCIDENT-20261027-omp-login.md](INCIDENT-20261027-omp-login.md)와 BRIEF WBX-D31~D33.

### WBX-B5가 쓸 구성(1단계로 확인된 부분)

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
  "omp_version": "omp/18.12.6",
  "login": {
    "model": "openai-codex/gpt-6.1-sol:high",
    "provider": "openai-codex",
    "authenticated_providers": [],
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
    "authenticated_providers": []
  },
  "decoy_markers_in_prompt": [],
  "decoy_skills_in_prompt": [],
  "skill_prompt_lines": [
    "Matching skill → MUST read `skill://<name>` first.",
    "- wb-inbox: Send work requests to a Workbench project inbox and follow them with the Hub's /api/v1/ API.",
    "- `skill://<name>`: instructions; append `/<path>` for a file."
  ],
  "cleanup": {
    "pid": 1272519,
    "start_ticks": 95437123,
    "state": "dead",
    "returncode": 143,
    "group_left": []
  },
  "argv": [
    "<user home>/.local/bin/omp",
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
    "authenticated_providers": []
  },
  "decoy_markers_in_prompt": [],
  "decoy_skills_in_prompt": [],
  "skill_prompt_lines": [],
  "cleanup": {
    "pid": 1272588,
    "start_ticks": 95437251,
    "state": "dead",
    "returncode": 143,
    "group_left": []
  },
  "argv": [
    "<user home>/.local/bin/omp",
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
    "<user home>/.local/bin/omp",
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
  "duration": 0.7,
  "timed_out": false,
  "agent_end": false,
  "state_answered_after_s": 0.6,
  "ui_requests": [
    {
      "method": "setWidget",
      "title": ""
    }
  ],
  "prompt_response": {
    "success": false,
    "error": "No API key found for openai-codex.\n\nUse /login, set an API key environment variable, or create <root>/hub-state/board-agent/omp-root/agent/agent.db"
  },
  "event_types": {
    "available_commands_update": 1,
    "extension_ui_request": 1,
    "ready": 1,
    "response": 3
  },
  "tool_calls": [],
  "tool_results": [],
  "assistant_errors": [],
  "answer": {
    "chars": 0,
    "titles_named": {
      "B4 probe request one": false,
      "B4 probe request two": false,
      "B4 probe request three": false
    },
    "count_3_stated": false
  },
  "key_in_rpc_stream": false,
  "returncode": 0,
  "hub_requests": [],
  "system_key_last_used": {
    "before": null,
    "after": null
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
    "bytes": 116389144,
    "entries": [
      "18.12.6"
    ]
  }
}
```
