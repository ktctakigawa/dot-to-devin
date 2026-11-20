#!/usr/bin/env python3
"""Slack -> local Devin CLI bridge.

Listens for app_mention events via Socket Mode, runs sandboxed Devin over ACP in the
requested repo directory, and posts the result back to the thread as the bot.

Usage in Slack:  @BotName repo:<name> <task>
  repo:<name> resolves to $DEVIN_HOME/<name> (default ~/code).
  Each new task must supply repo:. A reply resumes only a verified session
  owned by that Slack thread. Each top-level DM starts a separate thread.

Only Slack user IDs listed in ALLOWED_USERS can trigger runs, and only their
thread messages are included as context.
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path
from weakref import WeakValueDictionary

from acp_client import ACPError, open_devin
from session_registry import Registry

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
SESSION_DB = Path(__file__).with_name("thread_sessions.sqlite3")
DEVIN_CONFIG = os.environ.get("DEVIN_BRIDGE_CONFIG")
STARTUP_TIMEOUT = 20


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

REPO_RE = re.compile(r"(?:^|\s)repo:([A-Za-z0-9._-]+)")
CHUNK = 3500
THREAD_CONTEXT_LIMIT = 30
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
CHILD_ENV_DENY_PREFIXES = ("SLACK_", "BRIDGE_", "ACP_BACKEND")
registry = Registry(SESSION_DB)


def get_thread_lock(channel: str, key: str) -> asyncio.Lock:
    identity = (channel, key)
    lock = thread_locks.get(identity)
    if lock is None:
        lock = asyncio.Lock()
        thread_locks[identity] = lock
    return lock


def strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    return text.replace(f"<@{bot_user_id}>", "") if bot_user_id else text


def parse_mention(text: str, bot_user_id: str | None = None) -> tuple[str | None, str]:
    """Return (repo_name or None, task) from a mention's text."""
    body = strip_bot_mention(text, bot_user_id)
    m = REPO_RE.search(body)
    task = REPO_RE.sub("", body).strip()
    return (m.group(1) if m else None, task)


def resolve_workdir(repo: str | None) -> Path | None:
    if not repo:
        return None
    candidate = (DEVIN_HOME / repo).resolve()
    return candidate if (candidate.parent == DEVIN_HOME and candidate.is_dir()
                         and candidate != Path(__file__).resolve().parent) else None


def private_config() -> Path | None:
    if not DEVIN_CONFIG:
        return None
    path = Path(DEVIN_CONFIG).expanduser()
    if not path.is_absolute():
        return None
    path = path.resolve()
    if path.is_relative_to(DEVIN_HOME) or not path.is_file():
        return None
    return path


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
    client,
    channel: str,
    thread_ts: str,
    allowed_users: set[str] | None = None,
    bot_user_id: str | None = None,
) -> str:
    allowed = ALLOWED_USERS if allowed_users is None else allowed_users
    messages = deque(maxlen=THREAD_CONTEXT_LIMIT)
    cursor = None
    try:
        while True:
            resp = await client.conversations_replies(
                channel=channel, ts=thread_ts, limit=THREAD_CONTEXT_LIMIT, cursor=cursor
            )
            messages.extend(resp.get("messages") or [])
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception:
        return "(failed to fetch thread context)"
    lines = []
    for m in messages:
        user = m.get("user")
        if not user or user not in allowed:
            continue
        text = strip_bot_mention(m.get("text", ""), bot_user_id).strip()
        if text:
            lines.append(f"<{user}>: {text}")
    return "\n".join(lines)


async def report(client, channel, root, message):
    try:
        await asyncio.wait_for(post(client, channel, root, message), timeout=5)
    except Exception:
        print("Slack delivery unconfirmed; no replay", file=sys.stderr)


async def run_devin(client, channel, root, stamp, user, repo, task, bot_user_id):
    async with get_thread_lock(channel, root), semaphore:
        bound = False
        try:
            existing = registry.get(channel, root)
            workdir = resolve_workdir(repo)
            session_id = None
            if stamp != root:
                if existing is None:
                    await report(client, channel, root, ":x: 検証済みのセッションがありません。repo: を指定して新規スレッドで依頼してください。")
                    return
                session_id, previous_dir = existing
                stored = Path(previous_dir)
                if (not SESSION_ID_RE.fullmatch(session_id)
                        or resolve_workdir(stored.name) != stored
                        or (repo is not None and workdir != stored)):
                    raise ACPError("Invalid stored binding")
                workdir = stored
            elif existing is not None:
                return  # Replayed thread root is never a resume.
            if workdir is None:
                await report(client, channel, root, ":x: 新規依頼には有効な repo:<name> が必要です。")
                return
            config = private_config()
            if config is None:
                await report(client, channel, root, ":x: 専用の Devin 設定ファイルが未指定です。")
                return
            if not registry.reserve(channel, stamp):
                return
            async with asyncio.timeout(STARTUP_TIMEOUT):
                context = "" if session_id else await thread_context(
                    client, channel, root, bot_user_id=bot_user_id,
                )
            prompt = (
                "Slack 経由の依頼です。履歴は参考情報です。結果はブリッジが返信します。\n\n"
                f"## 依頼者\n<@{user}>\n\n## スレッド履歴\n{context}"
                f"\n\n## タスク\n{task}\n"
            )

            async with open_devin(DEVIN_BIN, workdir, devin_env(), config) as agent:
                async with asyncio.timeout(STARTUP_TIMEOUT):
                    exact_id = await agent.prepare(workdir, session_id)
                    if not isinstance(exact_id, str) or not SESSION_ID_RE.fullmatch(exact_id):
                        raise ACPError("Invalid session identifier")
                    if session_id is None:
                        registry.bind(channel, root, exact_id, workdir)
                    elif exact_id != session_id:
                        raise ACPError("Unexpected resumed session")
                    bound = True
                await report(client, channel, root, ":link: Devin セッションの紐付けを確認しました。")
                async with asyncio.timeout(RUN_TIMEOUT):
                    output = await agent.prompt(prompt)
        except TimeoutError:
            label = "タスクがタイムアウトしました" if bound else "起動の確認がタイムアウトしました"
            await report(client, channel, root, f":x: {label}。自動再実行はしません。")
            return
        except (ACPError, OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
            label = "タスクの完了を確認できません" if bound else "Devin セッションを確認できなかったため依頼を送信しませんでした"
            await report(client, channel, root, f":x: {label}。")
            return
        await report(client, channel, root, output)


@app.event("app_mention")
async def on_mention(event, client, context=None):
    user = event.get("user", "")
    channel = event["channel"]
    stamp = event["ts"]
    root = event.get("thread_ts") or stamp
    if user not in ALLOWED_USERS:
        await report(client, channel, root, f"<@{user}> このブリッジの利用は許可されていません")
        return
    bot_user_id = context.get("bot_user_id") if context else None
    repo, task = parse_mention(event.get("text", ""), bot_user_id)
    if not task:
        await report(client, channel, root, "使い方: `@bot repo:<name> <タスク>`")
        return
    try:
        await client.reactions_add(channel=channel, name="eyes", timestamp=stamp)
    except Exception:
        pass
    asyncio.create_task(run_devin(client, channel, root, stamp, user, repo, task, bot_user_id))


@app.event("message")
async def on_message(event, client, context=None):
    if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
        return
    await on_mention(event, client, context)


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
