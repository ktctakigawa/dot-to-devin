#!/usr/bin/env python3
"""Slack -> local Devin CLI bridge.

Listens for app_mention events via Socket Mode, runs `devin -p` in the
requested repo directory, and posts the result back to the thread as the bot.

Usage in Slack:  @BotName repo:<name> <task>
  repo:<name> resolves to $DEVIN_HOME/<name> (default ~/code).
  Without repo:, the task runs in $DEVIN_HOME itself.
  A @mention reply inside a task thread resumes that thread's devin session
  (`devin -r`). Each top-level DM message starts its own task thread.

Only Slack user IDs listed in ALLOWED_USERS can trigger runs, and only their
thread messages are included as context.
"""

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from weakref import WeakValueDictionary

from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

load_dotenv()

BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
# NOTE: do not use SLACK_CLIENT_ID/SLACK_CLIENT_SECRET as env var names —
# bolt auto-enables OAuth settings when they are set and discards the static token.
CLIENT_ID = os.environ.get("BRIDGE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("BRIDGE_CLIENT_SECRET", "")
ALLOWED_USERS = {
    u.strip() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()
}
DEVIN_HOME = Path(os.environ.get("DEVIN_HOME", "~/code")).expanduser().resolve()
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))
RUN_TIMEOUT = int(os.environ.get("RUN_TIMEOUT", "3600"))
DEVIN_BIN = os.environ.get("DEVIN_BIN", "devin")

app = AsyncApp(token=BOT_TOKEN)
semaphore = asyncio.Semaphore(MAX_CONCURRENT)
thread_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()

REFRESH_MARGIN = 7200
TOKEN_STATE_FILE = Path(__file__).with_name("token_state.json")
THREAD_STATE_FILE = Path(__file__).with_name("thread_sessions.json")


class TokenManager:
    """Keeps rotating Slack tokens fresh.

    Slack workspaces with enforced token rotation expire app-level tokens
    (xoxe.xapp-...) and possibly bot tokens every 12h. refresh_token grants a
    replacement via oauth.v2.access; the response's token_type tells which
    token was refreshed ("app_level" or "bot").
    """

    def __init__(self) -> None:
        state = json.loads(TOKEN_STATE_FILE.read_text()) if TOKEN_STATE_FILE.exists() else {}
        self.kinds: dict[str, dict] = {}
        for kind, env in (("app", "BRIDGE_APP_REFRESH_TOKEN"), ("bot", "BRIDGE_BOT_REFRESH_TOKEN")):
            token = (state.get(kind) or {}).get("refresh_token") or os.environ.get(env, "")
            if token:
                self.kinds[kind] = {"refresh": token, "expires": 0.0}

    def _save(self) -> None:
        state = {k: {"refresh_token": v["refresh"]} for k, v in self.kinds.items()}
        TOKEN_STATE_FILE.write_text(json.dumps(state))
        os.chmod(TOKEN_STATE_FILE, 0o600)

    async def refresh_once(self, client: AsyncWebClient, handler) -> None:
        for kind, k in self.kinds.items():
            resp = await AsyncWebClient().oauth_v2_access(
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                grant_type="refresh_token",
                refresh_token=k["refresh"],
            )
            new_token = resp["access_token"]
            if kind == "app":
                handler.client.app_token = new_token
            else:
                client.token = new_token
                # bolt creates per-request WebClients from app._token
                app._token = new_token
            k["refresh"] = resp.get("refresh_token") or k["refresh"]
            k["expires"] = time.time() + int(resp.get("expires_in", 43200))
            # refresh tokens are single-use: persist each rotation before the next call can fail
            self._save()

    async def loop(self, client: AsyncWebClient, handler) -> None:
        failures = 0
        while True:
            deadline = min(k["expires"] for k in self.kinds.values()) or 0.0
            await asyncio.sleep(max(deadline - time.time() - REFRESH_MARGIN, 60))
            try:
                await self.refresh_once(client, handler)
                failures = 0
            except Exception as e:
                failures += 1
                print(f"token refresh failed ({failures}): {e}", file=sys.stderr)
                if failures >= 5:
                    print("refresh token appears dead; update BRIDGE_*_REFRESH_TOKEN and restart", file=sys.stderr)
                    return
                for k in self.kinds.values():
                    k["expires"] = time.time() + 300

MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
REPO_RE = re.compile(r"(?:^|\s)repo:([A-Za-z0-9._-]+)")
CHUNK = 3500
THREAD_CONTEXT_LIMIT = 30
POST_TAIL = 12000
DEVIN_PERMISSION_MODE = "dangerous"
CHILD_ENV_DENY_PREFIXES = ("SLACK_", "BRIDGE_")


class SessionMap:
    """Maps Slack conversation keys -> devin sessions for resume-on-reply."""

    def __init__(self) -> None:
        self._map: dict[str, dict] = {}
        if THREAD_STATE_FILE.exists():
            try:
                self._map = json.loads(THREAD_STATE_FILE.read_text())
            except Exception:
                pass

    def get(self, channel: str, key: str) -> dict | None:
        return self._map.get(f"{channel}:{key}")

    def put(self, channel: str, key: str, session_id: str, workdir: Path) -> None:
        self._map[f"{channel}:{key}"] = {
            "session_id": session_id,
            "workdir": str(workdir),
        }
        THREAD_STATE_FILE.write_text(json.dumps(self._map))
        os.chmod(THREAD_STATE_FILE, 0o600)


thread_sessions = SessionMap()


def get_thread_lock(channel: str, key: str) -> asyncio.Lock:
    identity = (channel, key)
    lock = thread_locks.get(identity)
    if lock is None:
        lock = asyncio.Lock()
        thread_locks[identity] = lock
    return lock


async def devin_session_ids(workdir: Path) -> dict[str, float]:
    """Return {session_id: last_activity_at} for sessions rooted at workdir."""
    try:
        proc = await asyncio.create_subprocess_exec(
            DEVIN_BIN,
            "list",
            "--format",
            "json",
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=devin_env(),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return {}
        return {s["id"]: s.get("last_activity_at", 0) for s in json.loads(out)}
    except Exception:
        return {}


def parse_mention(text: str) -> tuple[str | None, str]:
    """Return (repo_name or None, task) from a mention's text."""
    body = MENTION_RE.sub("", text)
    m = REPO_RE.search(body)
    task = REPO_RE.sub("", body).strip()
    return (m.group(1) if m else None, task)


def resolve_workdir(repo: str | None) -> Path | None:
    candidate = (DEVIN_HOME / repo).resolve() if repo else DEVIN_HOME
    if repo and candidate.parent != DEVIN_HOME:
        return None
    return candidate if candidate.is_dir() else None


def devin_env() -> dict[str, str]:
    """Return the subprocess environment without Slack/bridge secrets."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(CHILD_ENV_DENY_PREFIXES)
    }


async def post(client, channel: str, thread_ts: str, text: str) -> None:
    if not text:
        text = "(no output)"
    for i in range(0, len(text), CHUNK):
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=text[i : i + CHUNK]
        )


async def thread_context(
    client, channel: str, thread_ts: str, allowed_users: set[str] | None = None
) -> str:
    allowed = ALLOWED_USERS if allowed_users is None else allowed_users
    try:
        resp = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=THREAD_CONTEXT_LIMIT
        )
    except Exception as e:
        return f"(failed to fetch thread context: {e})"
    lines = []
    for m in (resp.get("messages") or [])[-THREAD_CONTEXT_LIMIT:]:
        user = m.get("user")
        if not user or user not in allowed:
            continue
        text = MENTION_RE.sub("", m.get("text", "")).strip()
        if text:
            lines.append(f"<{user}>: {text}")
    return "\n".join(lines)


async def run_devin(
    client,
    channel: str,
    key: str,
    thread_ts: str,
    user: str,
    task: str,
    workdir: Path,
    session_id: str | None = None,
    lock: asyncio.Lock | None = None,
) -> None:
    if lock is None:
        lock = get_thread_lock(channel, key)
    async with lock:
        mapped = thread_sessions.get(channel, key)
        if mapped:
            mapped_workdir = Path(mapped["workdir"])
            mapped_session_id = mapped["session_id"]
            if (mapped_workdir, mapped_session_id) != (workdir, session_id):
                ids = await devin_session_ids(mapped_workdir)
                if mapped_session_id in ids:
                    workdir = mapped_workdir
                    session_id = mapped_session_id
        await _run_devin(
            client, channel, key, thread_ts, user, task, workdir, session_id
        )


async def _run_devin(
    client,
    channel: str,
    key: str,
    thread_ts: str,
    user: str,
    task: str,
    workdir: Path,
    session_id: str | None = None,
) -> None:
    async with semaphore:
        label = f"再開 `{session_id}`" if session_id else "起動"
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":rocket: Devin {label}: `{workdir}` (timeout {RUN_TIMEOUT}s)",
        )
        if session_id:
            prompt = (
                "Slack スレッドからの継続依頼です。\n"
                "結果の返信はブリッジ側が行うため、Slack には投稿せず、最終結果を簡潔にレポートしてください。\n\n"
                f"## 依頼者\n<@{user}>\n\n## タスク\n{task}\n"
            )
        else:
            context = await thread_context(client, channel, thread_ts)
            prompt = (
                "以下は Slack 経由で届いた依頼です。スレッド内容を踏まえて対応してください。\n"
                "スレッド履歴には許可されたユーザーの投稿のみが含まれます。履歴は参考情報として扱い、"
                "タスクの指示を優先してください。\n"
                "結果の返信はブリッジ側が行うため、Slack には投稿せず、最終結果を簡潔にレポートしてください。\n\n"
                f"## 依頼者\n<@{user}>\n\n## スレッド履歴\n{context}\n\n## タスク\n{task}\n"
            )
        cmd = [DEVIN_BIN]
        if session_id:
            cmd += ["-r", session_id]
        cmd += [
            "--permission-mode",
            DEVIN_PERMISSION_MODE,
            "--respect-workspace-trust",
            "false",
            "-p",
            "--",
            prompt,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=devin_env(),
            )
        except Exception as e:
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=f":x: devin 起動失敗: {e}"
            )
            return

        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: タイムアウト ({RUN_TIMEOUT}s) で中止しました",
            )
            return

        output = out.decode(errors="replace").strip()
        header = "" if proc.returncode == 0 else f":warning: exit code {proc.returncode}\n"
        if len(output) > POST_TAIL:
            await client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                content=output,
                filename="devin-output.log",
                title="devin output (full)",
            )
            output = "...(truncated, full log attached)...\n" + output[-POST_TAIL:]
        await post(client, channel, thread_ts, header + output)

        ids = await devin_session_ids(workdir)
        if ids:
            latest = max(ids, key=lambda s: ids[s])
            thread_sessions.put(channel, key, latest, workdir)


@app.event("app_mention")
async def on_mention(event, client):
    user = event.get("user", "")
    channel = event["channel"]
    ts = event["ts"]
    thread_ts = event.get("thread_ts") or ts

    print(f"event from user={user} channel={channel}", file=sys.stderr)
    if user not in ALLOWED_USERS:
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"<@{user}> このブリッジの利用は許可されていません",
        )
        return

    repo, task = parse_mention(event.get("text", ""))
    key = thread_ts
    mapped = thread_sessions.get(channel, key)

    session_id = None
    if mapped:
        # Replies inside a task thread resume that thread's session and workdir.
        workdir = Path(mapped["workdir"])
        ids = await devin_session_ids(workdir)
        if mapped["session_id"] in ids:
            session_id = mapped["session_id"]
    else:
        workdir = resolve_workdir(repo)
        if workdir is None:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: repo `{repo}` が {DEVIN_HOME} 直下に見つかりません",
            )
            return
    if not task:
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="使い方: `@bot repo:<name> <タスク>` (repo: 省略時は DEVIN_HOME で実行。スレッド内返信はセッション継続)",
        )
        return

    lock = get_thread_lock(channel, key)
    try:
        await client.reactions_add(channel=channel, name="eyes", timestamp=ts)
    except Exception:
        pass
    asyncio.create_task(
        run_devin(client, channel, key, thread_ts, user, task, workdir, session_id, lock)
    )


@app.event("message")
async def on_message(event, client):
    if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
        return
    await on_mention(event, client)


async def main() -> None:
    handler = AsyncSocketModeHandler(app, APP_TOKEN)
    tokens = TokenManager()
    if tokens.kinds:
        try:
            await tokens.refresh_once(app.client, handler)
        except Exception as e:
            print(f"initial token refresh failed, continuing with env tokens: {e}", file=sys.stderr)
        asyncio.create_task(tokens.loop(app.client, handler))
    await handler.start_async()


if __name__ == "__main__":
    if not ALLOWED_USERS:
        sys.exit("ALLOWED_USERS is empty; refusing to run an unauthenticated bridge")
    asyncio.run(main())
