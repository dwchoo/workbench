This folder holds Workbench user skills for agents outside Workbench; it is not `.agents/skills/` (Workbench development skills).
<!-- wb:skill-repo -->

# Workbench user skills

A skill here lets an AI agent (Claude Code, Codex, OMP, or any agent that reads the Agent Skills format) talk to a
Workbench Hub: send work requests to a project's inbox, follow them, and read their results.

| Skill | What it does |
|---|---|
| [`wb-inbox`](wb-inbox/SKILL.md) | Submit, list, read, comment on, cancel and read results of Workbench requests through the Hub's `/api/v1/` API. Editor keys can also edit a request and move it between `NEW` and `NEEDS_INFO`. |

## Install

An agent that was given only this README has everything it needs here: the files of the skill, the commands that
download them from the Hub, and the folder each agent type loads skills from. Follow the section for your agent type
(Claude Code, Codex or OMP), then do not stop at the key: the owner of the Hub puts the API key into `.env` (see
"Configure"). Never ask for the key in chat.

### The files

The skill is a folder of three files. Nothing else is needed; `wb_inbox.py` uses only Python 3 and its standard
library, so there is nothing to `pip install`.

| File | Purpose | On GitHub | Download from the Hub |
|---|---|---|---|
| `SKILL.md` | The skill itself: when to use it and how | [SKILL.md](wb-inbox/SKILL.md) | `http://YOUR-HUB-HOST:8780/skill/wb-inbox/SKILL.md` |
| `wb_inbox.py` | The client the skill runs (`python3 -I wb_inbox.py ...`) | [wb_inbox.py](wb-inbox/wb_inbox.py) | `http://YOUR-HUB-HOST:8780/skill/wb-inbox/wb_inbox.py` |
| `.env.example` | Template for the Hub address and API key settings | [.env.example](wb-inbox/.env.example) | `http://YOUR-HUB-HOST:8780/skill/wb-inbox/.env.example` |

When this README is served by a Hub, the address above is that Hub's own address. On GitHub it is only a
placeholder: use the address of your Hub instead.

Every block below does the same: it creates the skill folder, downloads the three files (with `curl`, or with
`python3` alone when `curl` is not installed), creates `.env` from `.env.example` when there is no `.env` yet, restricts
it with `chmod 600`, and finishes with `wb_inbox.py config`. That check sends nothing; it exits 0 when both settings
are present (the key is shown only as `set` or `missing`) and 3 when one is missing. A failed download stops the block
with a non-zero exit. The `.env` it creates already holds this Hub's address; the key is still the placeholder until
the owner replaces it.

Each agent reads skills from a user folder (all projects) and a project folder (one repository). The blocks install
into the user folder. For a project install set `SKILL_DIR` as shown in the comment, and keep the real `.env` out of git
(or put the settings in `~/.config/omp-workbench/client.env` instead). Start a new session of the agent afterwards if
the skill is not listed yet.

### Claude Code

Claude Code loads `~/.claude/skills/<name>/SKILL.md` (personal) and `.claude/skills/<name>/SKILL.md` (project); see
https://code.claude.com/docs/en/skills.

```sh
(
  set -eu
  SKILL_DIR="$HOME/.claude/skills/wb-inbox"   # project level instead: SKILL_DIR=".claude/skills/wb-inbox" (run from the repository root)
  mkdir -p "$SKILL_DIR"
  for f in SKILL.md wb_inbox.py .env.example; do
    url="http://YOUR-HUB-HOST:8780/skill/wb-inbox/$f"
    if command -v curl >/dev/null 2>&1; then
      curl -fsS "$url" -o "$SKILL_DIR/$f"
    else
      python3 -I -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$url" "$SKILL_DIR/$f"
    fi
  done
  [ -e "$SKILL_DIR/.env" ] || cp "$SKILL_DIR/.env.example" "$SKILL_DIR/.env"
  chmod 600 "$SKILL_DIR/.env"
  python3 -I "$SKILL_DIR/wb_inbox.py" config
)
```

### Codex

Codex CLI loads `~/.agents/skills/<name>/SKILL.md` (user) and `.agents/skills/<name>/SKILL.md` (every directory from the
working directory up to the repository root); see https://developers.openai.com/codex/skills. Codex also reads
`$CODEX_HOME/skills` (`~/.codex/skills`), which its own skill installer uses; the documentation names `~/.agents/skills`.

```sh
(
  set -eu
  SKILL_DIR="$HOME/.agents/skills/wb-inbox"   # project level instead: SKILL_DIR=".agents/skills/wb-inbox" (run from the repository root)
  mkdir -p "$SKILL_DIR"
  for f in SKILL.md wb_inbox.py .env.example; do
    url="http://YOUR-HUB-HOST:8780/skill/wb-inbox/$f"
    if command -v curl >/dev/null 2>&1; then
      curl -fsS "$url" -o "$SKILL_DIR/$f"
    else
      python3 -I -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$url" "$SKILL_DIR/$f"
    fi
  done
  [ -e "$SKILL_DIR/.env" ] || cp "$SKILL_DIR/.env.example" "$SKILL_DIR/.env"
  chmod 600 "$SKILL_DIR/.env"
  python3 -I "$SKILL_DIR/wb_inbox.py" config
)
```

### OMP

OMP loads `~/.omp/agent/skills/<name>/SKILL.md` (user) and `.omp/skills/<name>/SKILL.md` (project). It also reads the
`.claude/skills`, `.codex/skills` and `.agents/skills` folders of the other agents, so a skill installed for one of them
is found by OMP too. These paths come from OMP's own skill discovery (no public documentation page was found for them).

```sh
(
  set -eu
  SKILL_DIR="$HOME/.omp/agent/skills/wb-inbox"   # project level instead: SKILL_DIR=".omp/skills/wb-inbox" (run from the repository root)
  mkdir -p "$SKILL_DIR"
  for f in SKILL.md wb_inbox.py .env.example; do
    url="http://YOUR-HUB-HOST:8780/skill/wb-inbox/$f"
    if command -v curl >/dev/null 2>&1; then
      curl -fsS "$url" -o "$SKILL_DIR/$f"
    else
      python3 -I -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$url" "$SKILL_DIR/$f"
    fi
  done
  [ -e "$SKILL_DIR/.env" ] || cp "$SKILL_DIR/.env.example" "$SKILL_DIR/.env"
  chmod 600 "$SKILL_DIR/.env"
  python3 -I "$SKILL_DIR/wb_inbox.py" config
)
```

### From GitHub

When the Hub owner has configured a repository address, the second line of this README (when served by the Hub) names it.
Without a reachable Hub you can take the same files from a checkout of that repository (the `skills/wb-inbox/` folder
at its root); `<REPO_URL>` is that address:

```sh
(
  set -eu
  SKILL_DIR="$HOME/.claude/skills/wb-inbox"   # or the folder of your agent from the sections above
  SRC="$(mktemp -d)"
  git clone --depth 1 "<REPO_URL>" "$SRC"
  mkdir -p "$SKILL_DIR"
  cp "$SRC/skills/wb-inbox/SKILL.md" "$SRC/skills/wb-inbox/wb_inbox.py" "$SRC/skills/wb-inbox/.env.example" "$SKILL_DIR/"
  rm -rf "$SRC"
  [ -e "$SKILL_DIR/.env" ] || cp "$SKILL_DIR/.env.example" "$SKILL_DIR/.env"
  chmod 600 "$SKILL_DIR/.env"
)
```

Without `git`, GitHub serves single files at `<REPO_URL>/raw/HEAD/skills/wb-inbox/<file>`, so replace the Hub address in
the download loop of the blocks above with `<REPO_URL>/raw/HEAD/skills/wb-inbox/` and add `-L` to `curl` (GitHub
redirects). The `.env.example` from GitHub holds a placeholder Hub address, so edit `WB_HUB_URL` in `.env` yourself.

## Configure

Ask the Hub owner for an API key (Hub web page, `Settings`, `API keys`). The page shows the two lines to use:

```
WB_HUB_URL=http://YOUR-HUB-HOST:8780
WB_API_KEY=wbk_...
```

Put them in `.env` next to `wb_inbox.py` (the install blocks above already created it from `.env.example`; edit it), or in
`~/.config/omp-workbench/client.env` to share them between agents, and restrict the file to yourself:

```sh
chmod 600 "$SKILL_DIR/.env"
```

`wb_inbox.py` looks each variable up in this order: the process environment, `.env` next to the script, then
`$XDG_CONFIG_HOME/omp-workbench/client.env` (`~/.config/omp-workbench/client.env` when `XDG_CONFIG_HOME` is not set).
It warns when a file it reads is readable by other users. The key is only sent in the `Authorization` header and is never
printed. Do not paste the key into a chat with an agent; put it in the file yourself. `.env` files are ignored by git.

Check the setup (no request is sent; the key is shown only as `set` or `missing`):

```sh
python3 -I "$SKILL_DIR/wb_inbox.py" config
```

## Use

```sh
python3 -I wb_inbox.py projects
python3 -I wb_inbox.py submit --project <PROJECT_ID> --title "Add a retry" --body "Details..."
python3 -I wb_inbox.py list --state NEW
python3 -I wb_inbox.py get <REQUEST_ID>
```

Every command prints JSON; run `python3 -I wb_inbox.py --help` for the rest. The same API works with plain `curl`:

```sh
curl -H "Authorization: Bearer $WB_API_KEY" "$WB_HUB_URL/api/v1/projects"
```

## Hub with a self-signed TLS certificate

`wb_inbox.py` trusts the system certificate store. To trust a Hub's own certificate, point the standard OpenSSL
variable at it (there is no Workbench-specific setting):

```sh
SSL_CERT_FILE=/path/to/hub-certificate.pem python3 -I wb_inbox.py projects
```
