This folder holds Workbench user skills for agents outside Workbench; it is not `.agents/skills/` (Workbench development skills).
<!-- wb:skill-repo -->

# Workbench user skills

A skill here lets an AI agent (Claude Code, Codex, OMP, or any agent that reads the Agent Skills format) talk to a
Workbench Hub: send work requests to a project's inbox, follow them, and read their results.

| Skill | What it does |
|---|---|
| [`wb-inbox`](wb-inbox/SKILL.md) | Submit, list, read, comment on, cancel and read results of Workbench requests through the Hub's `/api/v1/` API. Editor keys can also edit a request and move it between `NEW` and `NEEDS_INFO`. |

## Install

The Hub serves this folder, so the files can be fetched from the Hub itself:

```sh
mkdir -p ~/.claude/skills/wb-inbox          # or the skills folder of your agent
cd ~/.claude/skills/wb-inbox
for file in SKILL.md wb_inbox.py .env.example; do
  curl -fsSO "http://YOUR-HUB-HOST:8780/skill/wb-inbox/$file"
done
```

Or copy the `wb-inbox/` folder from a checkout of this repository into your agent's skills folder
(`skills/wb-inbox/` here). `wb_inbox.py` needs only Python 3 and its standard library; there is nothing to `pip install`.

## Configure

Ask the Hub owner for an API key (Hub web page, `Settings`, `API keys`). The page shows the two lines to use:

```
WB_HUB_URL=http://YOUR-HUB-HOST:8780
WB_API_KEY=wbk_...
```

Put them in `.env` next to `wb_inbox.py` (copy `.env.example` to `.env` and edit it), or in
`~/.config/omp-workbench/client.env` to share them between agents, and restrict the file to yourself:

```sh
chmod 600 ~/.claude/skills/wb-inbox/.env
```

`wb_inbox.py` looks each variable up in this order: the process environment, `.env` next to the script, then
`$XDG_CONFIG_HOME/omp-workbench/client.env` (`~/.config/omp-workbench/client.env` when `XDG_CONFIG_HOME` is not set).
It warns when a file it reads is readable by other users. The key is only sent in the `Authorization` header and is never
printed. Do not paste the key into a chat with an agent; put it in the file yourself. `.env` files are ignored by git.

Check the setup (no request is sent; the key is shown only as `set` or `missing`):

```sh
python3 -I ~/.claude/skills/wb-inbox/wb_inbox.py config
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
