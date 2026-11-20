"""Minimal subprocess client for the ACP operations used by the bridge."""

import asyncio
import json
import os
import signal
from contextlib import asynccontextmanager

MAX_FRAME = 2 ** 20
MAX_OUTPUT = 12000


class ACPError(Exception):
    """A protocol failure. Do not expose peer messages in Slack."""


class ACPClient:
    def __init__(self, process):
        self.process = process
        self.next_id = 0
        self.capabilities = {}
        self.session_id = None
        self.output = ""

    async def send(self, message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        await self.process.stdin.drain()

    async def call(self, method, params, capture=False):
        self.next_id += 1
        request_id = self.next_id
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            line = await self.process.stdout.readline()
            if not line:
                raise ACPError("ACP closed before replying")
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ACPError("Invalid ACP frame")
            if "method" in message:
                if "id" in message:
                    if message["method"] == "session/request_permission":
                        # Cancel rather than approve any permission expansion.
                        await self.send({"id": message["id"], "result": {
                            "outcome": {"outcome": "cancelled"},
                        }})
                    else:
                        await self.send({"id": message["id"], "error": {
                            "code": -32601, "message": "Client method not supported",
                        }})
                elif capture and message["method"] == "session/update":
                    p = message.get("params", {})
                    update = p.get("update", {})
                    content = update.get("content", {})
                    if (p.get("sessionId") == self.session_id
                            and update.get("sessionUpdate") == "agent_message_chunk"
                            and content.get("type") == "text"
                            and isinstance(content.get("text"), str)):
                        self.output = (self.output + content["text"])[-MAX_OUTPUT:]
                continue
            if type(message.get("id")) is not int or message["id"] != request_id:
                raise ACPError("Unmatched ACP response")
            result = message.get("result")
            if "error" in message or not isinstance(result, dict):
                raise ACPError("ACP request failed")
            return result

    async def prepare(self, workdir, session_id=None):
        hello = await self.call("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "dot-to-devin", "version": "1.0.0"},
        })
        if type(hello.get("protocolVersion")) is not int or hello["protocolVersion"] != 1:
            raise ACPError("Unsupported ACP version")
        self.capabilities = hello.get("agentCapabilities", {})
        params = {"cwd": str(workdir), "mcpServers": []}
        if session_id is None:
            result = await self.call("session/new", params)
            self.session_id = result.get("sessionId")
        else:
            if self.capabilities.get("loadSession") is not True:
                raise ACPError("Session loading not supported")
            result = await self.call("session/load", {**params, "sessionId": session_id})
            if result.get("sessionId", session_id) != session_id:
                raise ACPError("Loaded a different session")
            self.session_id = session_id
        return self.session_id

    async def prompt(self, text):
        result = await self.call("session/prompt", {
            "sessionId": self.session_id, "prompt": [{"type": "text", "text": text}],
        }, capture=True)
        if result.get("stopReason") != "end_turn":
            raise ACPError("Agent did not finish normally")
        return self.output


@asynccontextmanager
async def open_devin(binary, workdir, env, config):
    # No legacy fallback, no task in argv, no trust bypass.
    process = await asyncio.create_subprocess_exec(
        binary, "--sandbox", "--permission-mode", "autonomous",
        "--config", str(config), "acp",
        cwd=str(workdir), env=env, start_new_session=True,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=MAX_FRAME,
    )
    completed = False
    reaped = False
    try:
        yield ACPClient(process)
        completed = True
    finally:
        if completed:
            # EOF lets the CLI flush session state before a later session/load.
            process.stdin.close()
            try:
                async with asyncio.timeout(3):
                    while await process.stdout.read(65536):
                        pass
                    exit_code = await process.wait()
            except (TimeoutError, OSError):
                pass
            else:
                reaped = True
        if not reaped:
            # On timeout/protocol failure, stop the launched process group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            async with asyncio.timeout(3):
                while await process.stdout.read(65536):
                    pass
                await process.wait()
        if completed and (not reaped or exit_code != 0):
            raise ACPError("Devin did not shut down cleanly")
