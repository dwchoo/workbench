# PROBE-OMP-MCP: OMP MCP 연결 feasibility probe(TI-15)

판정: **passed**. OMP `omp/18.12.6`, 실행 2026-10-25T07:26:10+09:00, 12.7 s. 이 파일은 `tests/hub/probes/omp_mcp_probe.py --report …`가 생성한다.

> 참고: `tests/hub/probes/omp_mcp_probe.py`는 WBX-B8(2026-10-28)에서 제거됐다. git history를 본다.

모델·provider 호출 없음(RPC `get_state`·`get_available_commands`·`get_login_providers`·`set_session_name`·`bash echo`만. HTTP(S)/ALL_PROXY는 닫힌 port, loopback만 NO_PROXY). temp HOME과 placeholder `agent.db`(빈 파일, symlink만 만들고 열지 않음)를 썼다. 사용자 `~/.omp`는 읽지도 쓰지도 않았다.

## 판정 기준과 결과

| 확인 | 결과 |
|---|---|
| control_manager: existing isolation check ok (0 leaks) | ok |
| control_manager: no MCP tool, no stub request | ok |
| control_worker: existing isolation check ok (0 leaks) | ok |
| control_worker: no MCP tool, no stub request | ok |
| board_env: exactly one MCP tool, from workbench-hub | ok |
| board_env: no leak besides that tool | ok |
| board_env: stub saw initialize + tools/list with the Bearer key | ok |
| board_env: every hub request carried the key | ok |
| board_env: no decoy server contacted | ok |
| board_env: no decoy prompt marker | ok |
| board_literal: exactly one MCP tool, from workbench-hub | ok |
| board_literal: no leak besides that tool | ok |
| board_literal: stub saw initialize + tools/list with the Bearer key | ok |
| board_literal: every hub request carried the key | ok |
| board_literal: no decoy server contacted | ok |
| board_literal: no decoy prompt marker | ok |
| decoys: every MCP decoy planted | ok |
| processes: none left | ok |

## TI-16이 쓸 구성

Board agent home: `omp_home.prepare_omp_home(<hub state>/board-agent, …, provider_ids=<isolation 목록 − native>)`(`skills_dir`·`agents_dir`는 빈 Workbench 디렉터리). 그 agent dir에 `mcp.json`(0600)을 둔다.

argv(cwd = `$HOME`):

```
omp --config omp_bridge/omp-isolation.yml --config <board overlay> --no-extensions --append-system-prompt <role prompt> --no-title --allow-home [--continue] [--model …]
```

Board overlay(두 번째 `--config`. 나중 overlay의 배열이 앞의 것을 대체한다는 것을 이 probe가 확인했다):

```json
{
  "disabledProviders": [
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
    "customDirectories": [],
    "includeSkills": [],
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

`<agent dir>/mcp.json`(header는 환경 변수 참조, key는 파일에 들어가지 않는다):

```json
{
  "mcpServers": {
    "workbench-hub": {
      "type": "http",
      "url": "http://127.0.0.1:34603/mcp",
      "headers": {
        "Authorization": "Bearer ${WB_BOARD_MCP_KEY}"
      }
    }
  }
}
```

환경: `omp_home.home_environment(base, home.environment())` + `WB_BOARD_MCP_KEY=<시스템 editor key>`. `board_literal`처럼 key 값을 파일에 직접 적어도 동작한다(그 경우 파일 0600, 시작마다 다시 쓴다). 어느 쪽이든 같은 uid로 도는 Board agent의 bash는 key를 읽을 수 있다(ADR-0003의 범위).

Board home(관측):

```json
{
  "root": "<root>/hub-state/board-agent/omp-root",
  "agent_dir": "<root>/hub-state/board-agent/omp-root/agent",
  "config_dir": "../hub-state/board-agent/omp-root",
  "linked": true,
  "environment": {
    "PI_CONFIG_DIR": "../hub-state/board-agent/omp-root",
    "PI_CODING_AGENT_DIR": "<root>/hub-state/board-agent/omp-root/agent",
    "BUN_RUNTIME_TRANSPILER_CACHE_PATH": "<root>/hub-state/board-agent/omp-root/bun-transpiler-cache"
  }
}
```

Tool 이름: `mcp__<서버 이름, `-`→`_`>_<tool 이름>`. 서버 `workbench-hub`의 tool `probe_echo`는 `mcp__workbench_hub_probe_echo`다. OMP 18.12에서 MCP tool은 RPC `dumpTools`에 나오지 않고 system prompt의 discovery 목록(`xd://mcp__…`)으로만 보인다. `launcher.observe_isolation`은 둘 다 보므로 그대로 잡는다.

격리 확인(TI-16): `check_isolation` 결과의 leak 중 `mcp:mcp__workbench_hub_*`만 기대값이다. 다른 leak이나 다른 `mcp__` 이름이 있으면 `isolation_failed`.

## 관측

### control_manager

```json
{
  "cwd": "<root>/home/work/proj",
  "argv": [
    "/home/dwchoo/.local/bin/omp",
    "--config",
    "<repo>/omp_bridge/omp-isolation.yml",
    "--config",
    "<root>/data/omp-isolation-manager.yml",
    "--no-extensions",
    "--append-system-prompt",
    "",
    "--no-title",
    "--mode",
    "rpc",
    "--no-session",
    "--no-title"
  ],
  "state": "ok",
  "error": null,
  "leaks": [],
  "warnings": [],
  "mcp_tools": [],
  "prompt_mcp_lines": [],
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
  "skills": [
    "to-worker",
    "workbench-recovery"
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
  "decoy_markers_in_prompt": [],
  "system_prompt_default": true,
  "omp_home": {
    "config_root_in_use": true,
    "agent_dir_in_use": true,
    "user_paths": [],
    "authenticated_providers": [
      "anthropic"
    ]
  },
  "cleanup": {
    "pid": 4112635,
    "start_ticks": 75138834,
    "state": "dead",
    "returncode": 143,
    "group_left": []
  },
  "stub_requests": []
}
```

### control_worker

```json
{
  "cwd": "<root>/home/work/proj",
  "argv": [
    "/home/dwchoo/.local/bin/omp",
    "--config",
    "<repo>/omp_bridge/omp-isolation.yml",
    "--config",
    "<root>/data/omp-isolation-worker.yml",
    "--no-extensions",
    "--append-system-prompt",
    "",
    "--no-title",
    "--tools",
    "read,grep,glob,edit,write,web_search,todo,task",
    "--mode",
    "rpc",
    "--no-session",
    "--no-title"
  ],
  "state": "ok",
  "error": null,
  "leaks": [],
  "warnings": [],
  "mcp_tools": [],
  "prompt_mcp_lines": [],
  "tools": [
    "read",
    "grep",
    "glob",
    "edit",
    "write",
    "web_search",
    "todo",
    "task"
  ],
  "skills": [
    "to-manager"
  ],
  "task_agents": [
    "explorer"
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
  "decoy_markers_in_prompt": [],
  "system_prompt_default": true,
  "omp_home": {
    "config_root_in_use": true,
    "agent_dir_in_use": true,
    "user_paths": [],
    "authenticated_providers": [
      "anthropic"
    ]
  },
  "cleanup": {
    "pid": 4112801,
    "start_ticks": 75139140,
    "state": "dead",
    "returncode": 143,
    "group_left": []
  },
  "stub_requests": []
}
```

### board_env

```json
{
  "cwd": "<root>/home",
  "argv": [
    "/home/dwchoo/.local/bin/omp",
    "--config",
    "<repo>/omp_bridge/omp-isolation.yml",
    "--config",
    "<root>/hub-state/board-agent/omp-board.yml",
    "--no-extensions",
    "--append-system-prompt",
    "",
    "--no-title",
    "--allow-home",
    "--mode",
    "rpc",
    "--no-session",
    "--no-title"
  ],
  "state": "leak",
  "error": null,
  "leaks": [
    "mcp:mcp__workbench_hub_probe_echo"
  ],
  "warnings": [],
  "mcp_tools": [
    "mcp__workbench_hub_probe_echo"
  ],
  "prompt_mcp_lines": [
    "- \"probe_echo\" → `xd://mcp__workbench_hub_probe_echo` — TI-15 probe tool: echoes its text argument."
  ],
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
  "skills": [],
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
  "decoy_markers_in_prompt": [],
  "system_prompt_default": true,
  "omp_home": {
    "config_root_in_use": true,
    "agent_dir_in_use": true,
    "user_paths": [],
    "authenticated_providers": [
      "anthropic"
    ]
  },
  "cleanup": {
    "pid": 4112897,
    "start_ticks": 75139271,
    "state": "dead",
    "returncode": 143,
    "group_left": []
  },
  "stub_requests": [
    {
      "http": "POST",
      "path": "/mcp",
      "rpc": "initialize",
      "authorization": "Bearer <key matched>",
      "auth_ok": true,
      "session": false,
      "protocol_header": null,
      "accept": "application/json, text/event-stream",
      "user_agent": "omp/18.12.6"
    },
    {
      "http": "POST",
      "path": "/mcp",
      "rpc": "notifications/initialized",
      "authorization": "Bearer <key matched>",
      "auth_ok": true,
      "session": true,
      "protocol_header": "2025-06-18",
      "accept": "application/json, text/event-stream",
      "user_agent": "omp/18.12.6"
    },
    {
      "http": "GET",
      "path": "/mcp",
      "rpc": null,
      "authorization": "Bearer <key matched>",
      "auth_ok": true,
      "session": true,
      "protocol_header": "2025-06-18",
      "accept": "text/event-stream",
      "user_agent": "omp/18.12.6"
    },
    {
      "http": "POST",
      "path": "/mcp",
      "rpc": "tools/list",
      "authorization": "Bearer <key matched>",
      "auth_ok": true,
      "session": true,
      "protocol_header": "2025-06-18",
      "accept": "application/json, text/event-stream",
      "user_agent": "omp/18.12.6"
    }
  ]
}
```

### board_project_cwd

```json
{
  "cwd": "<root>/home/work/proj",
  "argv": [
    "/home/dwchoo/.local/bin/omp",
    "--config",
    "<repo>/omp_bridge/omp-isolation.yml",
    "--config",
    "<root>/hub-state/board-agent/omp-board.yml",
    "--no-extensions",
    "--append-system-prompt",
    "",
    "--no-title",
    "--mode",
    "rpc",
    "--no-session",
    "--no-title"
  ],
  "state": "leak",
  "error": null,
  "leaks": [
    "system_prompt:replaced",
    "context_file:<root>/home/work/proj/.omp/AGENTS.md",
    "mcp:mcp__workbench_hub_probe_echo",
    "command:decoy-cmd(file)"
  ],
  "warnings": [],
  "mcp_tools": [
    "mcp__workbench_hub_probe_echo"
  ],
  "prompt_mcp_lines": [
    "- \"probe_echo\" → `xd://mcp__workbench_hub_probe_echo` — TI-15 probe tool: echoes its text argument."
  ],
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
  "skills": [],
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
      "decoy-cmd",
      "file"
    ],
    [
      "init",
      "file"
    ]
  ],
  "context_files": [
    "<root>/home/work/proj/.omp/AGENTS.md"
  ],
  "decoy_markers_in_prompt": [
    "DECOY-PROJECT-SYSTEM",
    "DECOY-PROJECT-AGENTS",
    "DECOY-PROJECT-RULE"
  ],
  "system_prompt_default": false,
  "omp_home": {
    "config_root_in_use": true,
    "agent_dir_in_use": true,
    "user_paths": [],
    "authenticated_providers": [
      "anthropic"
    ]
  },
  "cleanup": {
    "pid": 4113005,
    "start_ticks": 75139514,
    "state": "dead",
    "returncode": 143,
    "group_left": []
  },
  "stub_requests": [
    {
      "http": "POST",
      "path": "/mcp",
      "rpc": "initialize",
      "authorization": "Bearer <key matched>",
      "auth_ok": true,
      "session": false,
      "protocol_header": null,
      "accept": "application/json, text/event-stream",
      "user_agent": "omp/18.12.6"
    },
    {
      "http": "POST",
      "path": "/mcp",
      "rpc": "notifications/initialized",
      "authorization": "Bearer <key matched>",
      "auth_ok": true,
      "session": true,
      "protocol_header": "2025-06-18",
      "accept": "application/json, text/event-stream",
      "user_agent": "omp/18.12.6"
    },
    {
      "http": "GET",
      "path": "/mcp",
      "rpc": null,
      "authorization": "Bearer <key matched>",
      "auth_ok": true,
      "session": true,
      "protocol_header": "2025-06-18",
      "accept": "text/event-stream",
      "user_agent": "omp/18.12.6"
    },
    {
      "http": "POST",
      "path": "/mcp",
      "rpc": "tools/list",
      "authorization": "Bearer <key matched>",
      "auth_ok": true,
      "session": true,
      "protocol_header": "2025-06-18",
      "accept": "application/json, text/event-stream",
      "user_agent": "omp/18.12.6"
    }
  ]
}
```

### board_literal

```json
{
  "cwd": "<root>/home",
  "argv": [
    "/home/dwchoo/.local/bin/omp",
    "--config",
    "<repo>/omp_bridge/omp-isolation.yml",
    "--config",
    "<root>/hub-state/board-agent/omp-board.yml",
    "--no-extensions",
    "--append-system-prompt",
    "",
    "--no-title",
    "--allow-home",
    "--mode",
    "rpc",
    "--no-session",
    "--no-title"
  ],
  "state": "leak",
  "error": null,
  "leaks": [
    "mcp:mcp__workbench_hub_probe_echo"
  ],
  "warnings": [],
  "mcp_tools": [
    "mcp__workbench_hub_probe_echo"
  ],
  "prompt_mcp_lines": [
    "- \"probe_echo\" → `xd://mcp__workbench_hub_probe_echo` — TI-15 probe tool: echoes its text argument."
  ],
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
  "skills": [],
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
  "decoy_markers_in_prompt": [],
  "system_prompt_default": true,
  "omp_home": {
    "config_root_in_use": true,
    "agent_dir_in_use": true,
    "user_paths": [],
    "authenticated_providers": [
      "anthropic"
    ]
  },
  "cleanup": {
    "pid": 4113113,
    "start_ticks": 75139607,
    "state": "dead",
    "returncode": 143,
    "group_left": []
  },
  "stub_requests": [
    {
      "http": "POST",
      "path": "/mcp",
      "rpc": "initialize",
      "authorization": "Bearer <key matched>",
      "auth_ok": true,
      "session": false,
      "protocol_header": null,
      "accept": "application/json, text/event-stream",
      "user_agent": "omp/18.12.6"
    },
    {
      "http": "POST",
      "path": "/mcp",
      "rpc": "notifications/initialized",
      "authorization": "Bearer <key matched>",
      "auth_ok": true,
      "session": true,
      "protocol_header": "2025-06-18",
      "accept": "application/json, text/event-stream",
      "user_agent": "omp/18.12.6"
    },
    {
      "http": "GET",
      "path": "/mcp",
      "rpc": null,
      "authorization": "Bearer <key matched>",
      "auth_ok": true,
      "session": true,
      "protocol_header": "2025-06-18",
      "accept": "text/event-stream",
      "user_agent": "omp/18.12.6"
    },
    {
      "http": "POST",
      "path": "/mcp",
      "rpc": "tools/list",
      "authorization": "Bearer <key matched>",
      "auth_ok": true,
      "session": true,
      "protocol_header": "2025-06-18",
      "accept": "application/json, text/event-stream",
      "user_agent": "omp/18.12.6"
    }
  ]
}
```

### board_continue

`--continue`(session 유지)로 세 번 시작했다. 두 번째에 `set_session_name`과 RPC `bash echo`를 보냈다.

```json
{
  "argv": [
    "/home/dwchoo/.local/bin/omp",
    "--config",
    "<repo>/omp_bridge/omp-isolation.yml",
    "--config",
    "<root>/hub-state/board-agent/omp-board.yml",
    "--no-extensions",
    "--append-system-prompt",
    "",
    "--no-title",
    "--allow-home",
    "--continue",
    "--mode",
    "rpc"
  ],
  "runs": [
    {
      "start": 1,
      "requests": [
        "get_state"
      ],
      "session": {
        "sessionId": "01a17005-a386-7079-ae45-f7fba6f8795b",
        "sessionFile": "<root>/hub-state/board-agent/omp-root/agent/sessions/-tmp-ti15-fblj7qgc-home/2026-10-24T22-26-20-166Z_01a17005-a386-7079-ae45-f7fba6f8795b.jsonl",
        "sessionName": null,
        "messageCount": 0
      },
      "exit": 0
    },
    {
      "start": 2,
      "requests": [
        "get_state",
        "set_session_name",
        "bash",
        "get_state"
      ],
      "session": {
        "sessionId": "01a17005-a74e-7523-b893-41b8b26f60b7",
        "sessionFile": "<root>/hub-state/board-agent/omp-root/agent/sessions/-tmp-ti15-fblj7qgc-home/2026-10-24T22-26-21-134Z_01a17005-a74e-7523-b893-41b8b26f60b7.jsonl",
        "sessionName": null,
        "messageCount": 0
      },
      "rename_ok": true,
      "bash_ok": true,
      "session_after_rename": {
        "sessionId": "01a17005-a74e-7523-b893-41b8b26f60b7",
        "sessionFile": "<root>/hub-state/board-agent/omp-root/agent/sessions/-tmp-ti15-fblj7qgc-home/2026-10-24T22-26-21-134Z_01a17005-a74e-7523-b893-41b8b26f60b7.jsonl",
        "sessionName": "ti15-probe",
        "messageCount": 1
      },
      "exit": 0
    },
    {
      "start": 3,
      "requests": [
        "get_state"
      ],
      "session": {
        "sessionId": "01a17005-acbb-720e-a255-5c74475f403d",
        "sessionFile": "<root>/hub-state/board-agent/omp-root/agent/sessions/-tmp-ti15-fblj7qgc-home/2026-10-24T22-26-22-523Z_01a17005-acbb-720e-a255-5c74475f403d.jsonl",
        "sessionName": null,
        "messageCount": 0
      },
      "exit": 0
    }
  ],
  "session_files": [],
  "hub_requests": 15,
  "hub_requests_all_with_key": true
}
```

## 미끼(decoy)

모든 미끼 MCP 서버는 같은 stub의 `/decoy/<이름>`을 가리킨다. 접속하면 stub 기록에 남는다.

```json
{
  "mcp": {
    "<root>/home/.omp/agent/mcp.json": "user_agent_dir",
    "<root>/home/.omp/mcp.json": "user_omp_root",
    "<root>/home/.mcp.json": "home_dotmcp",
    "<root>/home/work/proj/.mcp.json": "project_dotmcp",
    "<root>/home/work/proj/mcp.json": "project_mcp",
    "<root>/home/work/proj/.omp/mcp.json": "project_omp",
    "<root>/home/work/proj/.omp/.mcp.json": "project_omp_dot",
    "<root>/home/.claude.json": "claude_user",
    "<root>/home/work/proj/.claude/mcp.json": "claude_project",
    "<root>/home/.cursor/mcp.json": "cursor_user",
    "<root>/home/work/proj/.cursor/mcp.json": "cursor_project",
    "<root>/home/.codex/config.toml": "codex_user",
    "<root>/home/.gemini/settings.json": "gemini_user",
    "<root>/home/work/proj/.vscode/mcp.json": "vscode_project"
  },
  "skills": [
    "decoy-user-real-skill",
    "decoy-home-omp-skill",
    "decoy-project-skill"
  ],
  "prompt_markers": [
    "DECOY-USER-REAL-SYSTEM",
    "DECOY-USER-REAL-AGENTS",
    "DECOY-HOME-OMP-SYSTEM",
    "DECOY-HOME-OMP-AGENTS",
    "DECOY-PROJECT-SYSTEM",
    "DECOY-PROJECT-AGENTS",
    "DECOY-PROJECT-RULE"
  ]
}
```

## OMP 소스에서 확인한 설정 경로

설치된 OMP 18.12.6 바이너리에 포함된 번들 소스(읽기 전용 검색, `packages/coding-agent/src/…` 주석 기준)에서 확인한 사실:

- MCP 서버 목록은 capability `mcps`를 제공하는 capability provider가 모은다(`mcp/config.ts`, `capability/mcp.ts`).
  설정 overlay(`config.yml`/`--config`)에는 서버를 직접 적는 key가 없다. `mcp.*` 설정은 `enableProjectConfig`,
  `startupTimeoutMs`(기본 250), `notifications`, `notificationDebounceMs`, `renderMarkdownResults`뿐이다
  (`mcp/settings.ts`).
- `disabledProviders`는 provider 단위로 모든 capability에 함께 적용된다(`capability/index.ts`). MCP만 따로 켜는
  capability별 스위치는 없다.
- `native` provider(`discovery/builtin.ts`)가 읽는 MCP 파일: `<cwd>/.omp/mcp.json`, `<cwd>/.omp/.mcp.json`
  (level project), `<agent dir>/mcp.json`, `<agent dir>/.mcp.json`(level user). agent dir은 `PI_CODING_AGENT_DIR`
  (Workbench home에서는 `<omp-root>/agent`)이다. Project level 항목은 `mcp.enableProjectConfig:false`이면 걸러진다
  (`mcp/config.ts`의 filter).
- `mcp-json` provider(`discovery/mcp-json.ts`)는 `<cwd>/mcp.json`·`<cwd>/.mcp.json`(level project)만 읽는다.
  지정 파일을 줄 방법이 없어 Board agent에는 쓸 수 없고 계속 비활성으로 둔다.
- MCP 설정의 문자열 값(url, headers, env, args 등)은 `${VAR}`·`${VAR:-default}`를 OMP 프로세스 환경 변수로
  치환한다(`discovery/helpers.ts`). 따라서 header는 값을 직접 적거나 환경 변수를 참조할 수 있다.
- user `mcp.json`의 `disabledServers`/`enabledServers` 배열과 서버별 `enabled:false`로 서버를 끌 수 있다
  (`mcp/config.ts`).
- `native`를 켜면 그 provider의 다른 capability(SYSTEM.md, AGENTS.md, skills, rules, commands, prompts, hooks,
  tools, settings)도 같이 켜진다. Project level은 `<cwd>/.omp/`(cwd가 HOME이면 건너뜀)와 cwd에서 repo root/HOME까지의
  상위 디렉터리(HOME 자체는 제외)를 본다(`Ka`, `NOe`). cwd가 HOME이면 OMP는 `~/tmp`·`/tmp`로 cwd를 옮기므로
  (`cli/startup-cwd.ts`) `--allow-home`이 필요하다.
- HTTP transport(`mcp/transports/http.ts`)는 설정 header를 모든 POST/GET/DELETE에 싣고, `initialize` 응답의
  `Mcp-Session-Id`를 이후 요청에 붙이며, `GET`(SSE listener)에 405가 오면 조용히 넘어간다.

## 결과 해석

- 단일 서버: isolation overlay를 그대로 두고 두 번째 overlay가 `disabledProviders`에서 `native`만 빼면, OMP는 Workbench agent dir의 `mcp.json`에 적은 Streamable HTTP 서버 하나에 연결하고 그 tool만 보인다. 사용자 `~/.omp`(agent dir·root), 프로젝트 `.mcp.json`/`mcp.json`/`.omp/mcp.json`, Claude·Cursor·Codex·Gemini·VS Code 미끼에는 접속하지 않았다(`board_env`, `board_literal`).
- Header: stub은 `initialize`, `notifications/initialized`, `GET`(SSE, 405), `tools/list` 모두에서 `Authorization: Bearer <key>`를 받았다. `${VAR}` 참조와 값 직접 기입 둘 다 동작한다.
- Manager·worker: 기존 isolation(`native` 비활성)은 그 agent dir에 `mcp.json`을 두어도 MCP tool·접속이 없고 누출 검사가 0건이다(`control_manager`, `control_worker`). 이 probe는 제품 코드를 바꾸지 않았다.
- 주의(cwd): `native`를 켜면 project `.omp/`의 SYSTEM.md·AGENTS.md·commands·rules도 돌아온다(`board_project_cwd`: `system_prompt:replaced`, `context_file`, `command:decoy-cmd`). Board agent는 cwd를 `$HOME`으로 하고 `--allow-home`을 준다(HOME에서는 project lookup이 없다). 다른 cwd를 쓰려면 그 디렉터리와 상위(HOME 전까지)에 `.omp/`가 없어야 하고, 시작 격리 확인이 그것을 잡는다.
- Project skill 미끼(`.omp/skills`)는 isolation의 `skills.enablePiProject:false`로 이미 막혀 보이지 않았다.
- `--continue`: session은 `<agent dir>/sessions/<cwd slug>/`에 놓인다. 모델 응답 없이(`set_session_name`, RPC `bash`)는 session 파일이 써지지 않아 다음 `--continue`가 새 session을 열었다. 이전 대화를 잇는지는 모델 turn이 있어야 확인되므로 이 probe에서는 **미확인**이다(실제 OMP·모델 확인으로 남김, P2-AC-33 `pending`).
- `memory.backend:"local"`, `memories.enabled:true`는 overlay에 넣었고 시작·격리 확인이 통과했다. 실제 기억 생성은 모델 turn이 필요해 **미확인**이다.
- `tools/call`: stub은 구현했지만 OMP가 tool을 부르려면 모델 turn이 필요해 이 probe에서는 부르지 않았다.
- `mcp.startupTimeoutMs`는 기본값(250 ms)으로도 loopback stub의 tool이 첫 `get_state`에 보였다. Hub 내부 listener가 느리면 tool이 늦게 보일 수 있으므로 TI-16의 격리 확인은 Hub listener가 열린 뒤 시작한다.

## 정리

```json
{
  "leftover_processes_final": [],
  "root_removed": true,
  "omp_tmp": {
    "dir": "/tmp/omp-workbench-1000",
    "before": 261,
    "after": 261,
    "new": []
  },
  "natives": {
    "dir": "<root>/home/.omp/natives",
    "bytes": 116389144,
    "entries": [
      "18.12.6"
    ]
  }
}
```

natives는 temp HOME의 `~/.omp/natives/<version>`에 추출되고 temp root와 함께 지워진다(크기는 위 bytes). 남은 OMP 프로세스는 `/proc/*/cmdline`에서 temp root 경로로 찾아 없음을 확인했다.
