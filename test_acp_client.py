import asyncio
import json
import unittest
from pathlib import Path
from unittest import mock

from acp_client import ACPClient, ACPError, open_devin


class FakePipe:
    def __init__(self):
        self.frames = []
        self.closed = False

    def write(self, data):
        self.frames.append(json.loads(data))

    def close(self):
        self.closed = True

    async def drain(self):
        pass


class FakeProcess:
    def __init__(self, frames):
        self.stdin = FakePipe()
        self.stdout = asyncio.StreamReader()
        for frame in frames:
            self.stdout.feed_data(json.dumps({"jsonrpc": "2.0", **frame}).encode() + b"\n")
        self.stdout.feed_eof()
        self.pid = 123
        self.wait = mock.AsyncMock()


def result(request_id, **payload):
    return {"id": request_id, "result": payload}


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_sandbox_argv_exact_id_and_no_permission_escalation(self):
        def message(session, chunk):
            return {"method": "session/update", "params": {
                "sessionId": session, "update": {"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": chunk}},
            }}

        proc = FakeProcess([
            result(1, protocolVersion=1, agentCapabilities={"loadSession": True}),
            result(2, sessionId="sess-example"),
            {"id": 100, "method": "session/request_permission"},
            message("unrelated", "do not forward"),
            message("sess-example", "example output"),
            result(3, stopReason="end_turn"),
        ])
        cfg = Path("/example/private-settings.json")
        repo = Path("/example/repo")
        with mock.patch("acp_client.asyncio.create_subprocess_exec", return_value=proc) as spawn, \
                mock.patch("acp_client.os.killpg") as kill:
            async with open_devin("devin", repo, {"PATH": "/bin"}, cfg) as client:
                self.assertEqual(await client.prepare(repo), "sess-example")
                self.assertEqual(await client.prompt("sample task"), "example output")
            kill.assert_not_called()
        self.assertTrue(proc.stdin.closed)
        self.assertEqual(spawn.call_args.args, (
            "devin", "--sandbox", "--permission-mode", "autonomous", "--config", str(cfg), "acp",
        ))
        self.assertEqual(spawn.call_args.kwargs["cwd"], str(repo))
        self.assertEqual(proc.stdin.frames[1]["params"], {"cwd": str(repo), "mcpServers": []})
        self.assertEqual(proc.stdin.frames[2]["params"]["sessionId"], "sess-example")
        self.assertEqual(proc.stdin.frames[3]["result"]["outcome"]["outcome"], "cancelled")
        proc.wait.assert_awaited_once()

    async def test_failed_request_kills_launched_process_group(self):
        proc = FakeProcess([])
        repo = Path("/example/repo")
        with mock.patch("acp_client.asyncio.create_subprocess_exec", return_value=proc), \
                mock.patch("acp_client.os.killpg") as kill:
            with self.assertRaises(ACPError):
                async with open_devin("devin", repo, {}, Path("/example/config.json")) as client:
                    await client.prepare(repo)
        kill.assert_called_once()
        proc.wait.assert_awaited_once()

    async def test_mismatched_response_fails_before_prompt(self):
        proc = FakeProcess([result(1, protocolVersion=1), result(81, sessionId="other")])
        with self.assertRaises(ACPError):
            await ACPClient(proc).prepare(Path("/example/repo"))
        self.assertEqual([msg["method"] for msg in proc.stdin.frames], ["initialize", "session/new"])

    async def test_resume_rejects_unsupported_capability_and_wrong_id(self):
        for frames in [
            [result(1, protocolVersion=1, agentCapabilities={})],
            [result(1, protocolVersion=1, agentCapabilities={"loadSession": True}),
             result(2, sessionId="other")],
        ]:
            with self.subTest(frames=frames):
                proc = FakeProcess(frames)
                with self.assertRaises(ACPError):
                    await ACPClient(proc).prepare(Path("/example/repo"), "sess-example")
                self.assertFalse(any(m["method"] == "session/prompt" for m in proc.stdin.frames))
