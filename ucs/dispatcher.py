"""
UCS Dispatcher — maps Slack threads to Docker containers running Claude Code.

Each @pal mention in a thread gets routed to a persistent container
ucs_sess_slack_<thread_ts>. The agent streams its response back as
live Slack message edits (reasoning bullets → final response).

Config is read from ~/.config/ucs/config.toml.
"""

import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import tarfile
import time
from asyncio.subprocess import PIPE
from pathlib import Path

import docker as docker_sdk
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .config import load_config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTAINER_MEMORY = "4g"
IDLE_TIMEOUT_SECS = 300
CREDENTIALS_SRC = os.path.expanduser("~/.claude/.credentials.json")
AGENT_USER = "ucs-agent"

CLAUDE_BASE_ARGS = [
    "claude",
    "--dangerously-skip-permissions",
    "--output-format", "stream-json",
    "--print",
    "--verbose",
]

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# container_name -> asyncio.subprocess.Process
active_processes: dict[str, asyncio.subprocess.Process] = {}

# container_name -> last activity timestamp (monotonic)
last_active: dict[str, float] = {}

# container_name -> exec user (AGENT_USER if we created one, "" to use image default)
agent_users: dict[str, str] = {}

_docker_client = None


def docker_client():
    global _docker_client
    if _docker_client is None:
        _docker_client = docker_sdk.from_env()
    return _docker_client

# ---------------------------------------------------------------------------
# CC binary helpers
# ---------------------------------------------------------------------------

def find_cc_binary() -> str | None:
    """Return path to the local Claude Code standalone binary, or None."""
    path = shutil.which("claude")
    if path:
        real = os.path.realpath(path)
        if os.path.isfile(real) and os.access(real, os.X_OK):
            return real
    # Fallback: newest version in the default install dir
    versions_dir = Path.home() / ".local" / "share" / "claude" / "versions"
    if versions_dir.exists():
        versions = sorted(v for v in versions_dir.iterdir() if v.is_file())
        if versions:
            return str(versions[-1])
    return None


def _copy_file_into_container(container, src_path: str, dest_dir: str, dest_name: str) -> None:
    """Copy a host file into a container directory via put_archive."""
    with open(src_path, "rb") as f:
        data = f.read()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=dest_name)
        info.size = len(data)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    container.put_archive(dest_dir, buf.read())

# ---------------------------------------------------------------------------
# Container setup
# ---------------------------------------------------------------------------

def container_name(thread_ts: str) -> str:
    safe_ts = thread_ts.replace(".", "_")
    return f"ucs_sess_slack_{safe_ts}"


def _get_container(name: str):
    try:
        return docker_client().containers.get(name)
    except docker_sdk.errors.NotFound:
        return None


def setup_container(name: str, image: str, log_fn=None) -> bool:
    """
    Ensure container exists, is running, has Claude Code installed, and has
    a non-root exec user. Populates agent_users[name].

    Returns True if the container was newly created (use --name root),
    False if it already existed (use --resume root).

    log_fn: optional callable(msg) for verbose output (used by config test).
    """
    emit = log_fn or (lambda msg: log.info(msg))

    container = _get_container(name)
    is_new = container is None

    if is_new:
        emit(f"Creating container {name} from {image}")
        container = docker_client().containers.run(
            image,
            name=name,
            detach=True,
            mem_limit=CONTAINER_MEMORY,
            command="sleep infinity",
        )
        time.sleep(1)  # let container settle
    elif container.status != "running":
        emit(f"Starting existing container {name}")
        container.start()
        time.sleep(0.5)

    if is_new:
        _install_agent(name, container, emit)

    # Re-derive exec user on every setup call (handles dispatcher restarts)
    agent_users[name] = _resolve_exec_user(name, container, emit)

    return is_new


def _resolve_exec_user(name: str, container, emit) -> str:
    """
    Determine the user to exec as. If the container's default user is root,
    ensure ucs-agent exists and return it. Otherwise return "" (use default).
    """
    result = container.exec_run("id -u")
    uid = result.output.decode().strip()

    if uid == "0":
        # Running as root — ensure ucs-agent exists
        r = container.exec_run(f"id {AGENT_USER}")
        if r.exit_code != 0:
            emit(f"Creating non-root user '{AGENT_USER}'")
            container.exec_run(
                f"useradd -m -s /bin/bash {AGENT_USER}",
                user="root",
            )
        return AGENT_USER
    else:
        emit(f"Container default user is non-root (uid={uid}), using as-is")
        return ""


def _install_agent(name: str, container, emit) -> None:
    """Install Claude Code binary and credentials into a fresh container."""
    cc_binary = find_cc_binary()
    if cc_binary is None:
        raise RuntimeError(
            "Claude Code binary not found on host. "
            "Run: claude install"
        )

    emit(f"Installing Claude Code from {cc_binary}")
    _copy_file_into_container(container, cc_binary, "/usr/local/bin", "claude")
    emit("Claude Code installed")

    _copy_credentials(name, container, emit)


def _copy_credentials(name: str, container, emit) -> None:
    if not os.path.exists(CREDENTIALS_SRC):
        log.warning("Credentials file not found at %s", CREDENTIALS_SRC)
        return

    # Determine credential destination based on exec user
    # We might not have agent_users populated yet during setup, so check directly
    result = container.exec_run("id -u")
    uid = result.output.decode().strip()
    if uid == "0":
        cred_home = f"/home/{AGENT_USER}"
        container.exec_run(f"mkdir -p {cred_home}/.claude", user="root")
    else:
        result2 = container.exec_run("sh -c 'echo $HOME'")
        cred_home = result2.output.decode().strip() or "/root"
        container.exec_run(f"mkdir -p {cred_home}/.claude")

    _copy_file_into_container(container, CREDENTIALS_SRC, f"{cred_home}/.claude", ".credentials.json")
    emit(f"Credentials copied to {cred_home}/.claude")


def stop_container(name: str) -> None:
    container = _get_container(name)
    if container and container.status == "running":
        log.info("Stopping idle container %s", name)
        container.stop()

# ---------------------------------------------------------------------------
# Integration context / system prompt
# ---------------------------------------------------------------------------

def build_system_prompt(ctx: dict) -> str:
    source = ctx.get("source")
    if source == "slack":
        return (
            "You are @pal, an AI agent operating inside a Slack workspace.\n\n"
            "Integration context:\n"
            f"  Source:  Slack\n"
            f"  Team:    {ctx['team']}\n"
            f"  Channel: {ctx['channel']}\n"
            f"  Thread:  {ctx['thread_ts']}\n"
            f"  User:    {ctx['user']}\n\n"
            "Each Slack thread maps to a persistent session — messages in this thread "
            "are continuations of the same conversation.\n\n"
            "Formatting: use Slack mrkdwn in responses.\n"
            "  Bold: *text*   Italic: _text_   Code: `code`   Block: ```code```\n"
            "  Lists: use • or - bullets. Do not use # headers (not rendered in Slack).\n\n"
            "You do not currently have Slack API tools — you cannot read channel history, "
            "look up users, or send messages outside this thread. If asked to do something "
            "requiring Slack API access, say so clearly."
        )
    return ""


# ---------------------------------------------------------------------------
# Stream parser
# ---------------------------------------------------------------------------

async def run_agent(
    cname: str,
    prompt: str,
    is_new_container: bool,
    channel: str,
    placeholder_ts: str,
    client,
    ctx: dict,
) -> None:
    session_flag = "--name" if is_new_container else "--resume"
    system_prompt = build_system_prompt(ctx)
    exec_user = agent_users.get(cname, "")
    user_args = ["--user", exec_user] if exec_user else []

    cmd = [
        "docker", "exec", *user_args, cname,
        *CLAUDE_BASE_ARGS,
        *(["--append-system-prompt", system_prompt] if system_prompt else []),
        session_flag, "root",
        prompt,
    ]

    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    active_processes[cname] = proc
    last_active[cname] = time.monotonic()

    bullets: list[str] = []

    async def _drain_stderr():
        async for line in proc.stderr:
            text = line.decode().strip()
            if text:
                log.warning("[%s stderr] %s", cname, text)

    asyncio.create_task(_drain_stderr())

    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode().strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Non-JSON line: %s", line[:120])
                continue

            ev_type = ev.get("type")

            if ev_type == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    btype = block.get("type")
                    if btype == "thinking":
                        snippet = block.get("thinking", "")[:120].replace("\n", " ")
                        ellipsis = "…" if len(block.get("thinking", "")) > 120 else ""
                        bullet = f"• _{snippet}{ellipsis}_"
                        if bullet not in bullets:
                            bullets.append(bullet)
                            await _update_placeholder(client, channel, placeholder_ts, bullets)
                    elif btype == "tool_use":
                        bullet = f"• calling `{block.get('name', '?')}`…"
                        bullets.append(bullet)
                        await _update_placeholder(client, channel, placeholder_ts, bullets)

            elif ev_type == "result":
                final_text = ev.get("result", "").strip()
                if ev.get("is_error") or not final_text:
                    final_text = f"_(error)_ {ev.get('result', 'unknown error')}"
                log.info("[%s] response: %s", cname, final_text[:300])
                await client.chat_update(channel=channel, ts=placeholder_ts, text=final_text)
                break

    except asyncio.CancelledError:
        pass
    finally:
        await proc.wait()
        log.info("docker exec exited with code %s for %s", proc.returncode, cname)
        active_processes.pop(cname, None)
        last_active[cname] = time.monotonic()
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass


async def _update_placeholder(client, channel: str, ts: str, bullets: list[str]) -> None:
    body = "thinking…\n" + "\n".join(bullets)
    try:
        await client.chat_update(channel=channel, ts=ts, text=body)
    except Exception as e:
        log.warning("Failed to update placeholder: %s", e)

# ---------------------------------------------------------------------------
# Slack app + event handlers
# ---------------------------------------------------------------------------

def build_app(config) -> AsyncApp:
    app = AsyncApp(token=config.slack.bot_token)
    authorized_ids = set(config.auth.authorized_user_ids)

    @app.event("message")
    async def handle_message():
        # Slack sends message events alongside app_mention — acknowledged and ignored.
        pass

    @app.event("app_mention")
    async def handle_mention(event, client):
        user = event.get("user", "")
        if user not in authorized_ids:
            log.info("Ignoring message from unauthorized user %s", user)
            return

        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        prompt = event.get("text", "").strip()

        if not prompt:
            return

        cname = container_name(thread_ts)

        resp = await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="thinking…",
        )
        placeholder_ts = resp["ts"]

        if cname in active_processes:
            proc = active_processes[cname]
            if proc.returncode is None:
                log.info("Interrupting active agent in %s", cname)
                await client.chat_update(channel=channel, ts=placeholder_ts, text="interrupting…")
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    proc.kill()
                active_processes.pop(cname, None)
            await client.chat_update(channel=channel, ts=placeholder_ts, text="thinking…")

        ctx = {
            "source": "slack",
            "team": event.get("team", ""),
            "channel": channel,
            "thread_ts": thread_ts,
            "user": user,
        }

        is_new = setup_container(cname, config.docker.image)

        async def _run():
            try:
                await run_agent(cname, prompt, is_new, channel, placeholder_ts, client, ctx)
            except Exception:
                log.exception("run_agent crashed for %s", cname)

        asyncio.create_task(_run())

    return app

# ---------------------------------------------------------------------------
# Idle container reaper
# ---------------------------------------------------------------------------

async def idle_reaper():
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        for cname, last in list(last_active.items()):
            if cname in active_processes:
                continue
            if now - last >= IDLE_TIMEOUT_SECS:
                stop_container(cname)
                last_active.pop(cname, None)
                agent_users.pop(cname, None)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    config = load_config()
    app = build_app(config)
    asyncio.create_task(idle_reaper())
    handler = AsyncSocketModeHandler(app, config.slack.app_token)
    await handler.start_async()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
