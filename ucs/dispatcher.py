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
import re
import tarfile
import time
from asyncio.subprocess import PIPE

import docker as docker_sdk
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .config import load_config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DOCKER_IMAGE = "ucs_agent_claude"
CONTAINER_MEMORY = "4g"
IDLE_TIMEOUT_SECS = 300
CREDENTIALS_SRC = os.path.expanduser("~/.claude/.credentials.json")

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

_docker_client = None


def docker_client():
    global _docker_client
    if _docker_client is None:
        _docker_client = docker_sdk.from_env()
    return _docker_client

# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def container_name(thread_ts: str) -> str:
    safe_ts = thread_ts.replace(".", "_")
    return f"ucs_sess_slack_{safe_ts}"


def _get_container(name: str):
    try:
        return docker_client().containers.get(name)
    except docker_sdk.errors.NotFound:
        return None


def ensure_container(name: str) -> bool:
    """
    Ensure the container exists and is running.
    Returns True if the container was just created (use --name root),
    False if it already existed (use --resume root).
    """
    container = _get_container(name)

    if container is None:
        log.info("Creating container %s", name)
        docker_client().containers.run(
            DOCKER_IMAGE,
            name=name,
            detach=True,
            mem_limit=CONTAINER_MEMORY,
            command="sleep infinity",
        )
        _copy_credentials(name)
        return True

    if container.status != "running":
        log.info("Starting existing container %s", name)
        container.start()

    return False


def _copy_credentials(name: str) -> None:
    if not os.path.exists(CREDENTIALS_SRC):
        log.warning("Credentials file not found at %s", CREDENTIALS_SRC)
        return
    container = _get_container(name)
    if container is None:
        return
    container.exec_run("mkdir -p /home/agent/.claude")
    with open(CREDENTIALS_SRC, "rb") as f:
        data = f.read()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=".credentials.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    container.put_archive("/home/agent/.claude", buf.read())
    log.info("Credentials copied into %s", name)


def stop_container(name: str) -> None:
    container = _get_container(name)
    if container and container.status == "running":
        log.info("Stopping idle container %s", name)
        container.stop()

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
) -> None:
    session_flag = "--name" if is_new_container else "--resume"
    cmd = [
        "docker", "exec", cname,
        *CLAUDE_BASE_ARGS,
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
                await client.chat_update(channel=channel, ts=placeholder_ts, text=final_text)
                break

    except asyncio.CancelledError:
        pass
    finally:
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

def _strip_mention(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


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
        prompt = _strip_mention(event.get("text", ""))

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

        is_new = ensure_container(cname)

        async def _run():
            try:
                await run_agent(cname, prompt, is_new, channel, placeholder_ts, client)
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
