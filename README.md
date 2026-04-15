# UCS

UCS maps conversational threads to persistent Docker containers running [Claude Code](https://claude.ai/code). Each thread gets its own container; the container is the session.

Currently supported: **Slack** (`@pal` mentions → agent responses in-thread).

## How it works

1. A user mentions `@pal` in a Slack thread
2. The dispatcher creates (or resumes) a Docker container for that thread
3. Claude Code is invoked inside the container via `docker exec`
4. The agent's reasoning steps appear as live bullet updates in the Slack placeholder message
5. The final response replaces the placeholder
6. The container stays alive for 5 minutes of inactivity, then stops (and can be resumed)

The container is a real Linux environment — the agent can write files, run programs, install tools, and accumulate state across messages in the same thread.

## Requirements

- Python 3.12+
- Docker (rootless or standard)
- [Claude Code CLI](https://claude.ai/code) installed on the host (`claude install`)
- A Slack app with Socket Mode enabled

## Installation

```bash
pip install -e .
```

## Configuration

On first run, a template is written to `~/.config/ucs/config.toml`:

```toml
[slack]
bot_token = "xoxb-..."   # OAuth & Permissions page
app_token = "xapp-..."   # Basic Information → App-Level Tokens (Socket Mode)

[auth]
# Slack user IDs allowed to trigger agent activity
# Find yours: Slack profile → ⋮ → Copy member ID
authorized_user_ids = ["UXXXXXXXXXX"]

[docker]
# Any Linux x86-64 image. Defaults to debian:bookworm-slim if not set.
# image = "your-org/your-image:tag"
```

Secrets live in `~/.config/ucs/config.toml`, not in the repo.

### Slack app setup

Your Slack app needs:
- **Bot Token Scopes**: `app_mentions:read`, `chat:write`
- **Event Subscriptions** (Socket Mode): `app_mention`, `message.channels` or `message.groups`
- **Socket Mode** enabled with an App-Level Token (`connections:write` scope)

## Usage

```bash
# Validate config and test agent installation on the configured image
ucs config test

# Start the dispatcher in a tmux session (logs to ~/.local/share/ucs/logs/)
ucs stack up

# Restart
ucs stack up -r

# Check status
ucs stack status

# Stop
ucs stack down

# Shell into a running session container
ucs shell ucs_sess_slack_<thread_ts>
```

## Docker images

By default UCS uses `debian:bookworm-slim`. You can bring any Linux x86-64 image — the dispatcher installs Claude Code into it at container creation time (no Node.js required, uses the CC standalone binary from the host).

If the container's default user is root, UCS automatically creates a `ucs-agent` user and runs the agent as that user (`--dangerously-skip-permissions` refuses to run as root).

```toml
[docker]
image = "your-org/your-dev-environment:latest"
```

`ucs config test` will validate the image works before you bring up the stack.

## Session lifecycle

| Event | Behavior |
|---|---|
| First `@pal` in a thread | New container created, `claude --name root` |
| Subsequent messages | `docker exec` into running container, `claude --resume root` |
| New message while agent is thinking | SIGTERM to active process, restart with new prompt |
| 5 min idle | Container stopped (state preserved, resumes on next message) |

## Project layout

```
ucs/
  config.py      # Config loading from ~/.config/ucs/config.toml
  dispatcher.py  # Slack bot + Docker container management + stream parser
  cli.py         # `ucs` CLI (stack up/down/status, shell, config test)
pyproject.toml
```
