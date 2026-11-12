"""Optional native transport test. No model request, account, or real token."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from websockets.asyncio.client import connect, unix_connect


@unittest.skipUnless(os.environ.get('CODEX_DESKTOP_TEST_CLI'), 'set CODEX_DESKTOP_TEST_CLI to an app-bundled CLI')
class NativeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_stdio_and_secondary_client_share_one_server(self):
        with tempfile.TemporaryDirectory(prefix='codex-bridge-test-') as tmp:
            root = Path(tmp)
            home = root / 'home'; home.mkdir()
            settings = root / 'desktop.json'
            settings.write_text(json.dumps(dict(cli=os.environ['CODEX_DESKTOP_TEST_CLI'], instances=str(root / 'instances'))))
            env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'CODAMA_'))}
            env.update(CODEX_HOME=str(home), CODEX_DESKTOP_SETTINGS=str(settings))
            with (root / 'stderr').open('wb') as errors:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, '-m', 'codex_desktop.backend', 'app-server',
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=errors, env=env)
                async def rpc(ident, method, params):
                    process.stdin.write(json.dumps(dict(id=ident, method=method, params=params)).encode() + b'\n')
                    await process.stdin.drain()
                    while True:
                        line = await asyncio.wait_for(process.stdout.readline(), 30)
                        if not line:
                            self.fail((root / 'stderr').read_text())
                        msg = json.loads(line)
                        if msg.get('id') == ident:
                            self.assertNotIn('error', msg)
                            return msg['result']
                try:
                    await rpc(1, 'initialize', dict(clientInfo=dict(name='bridge_test', version='1'), capabilities=dict(experimentalApi=True)))
                    process.stdin.write(b'{"method":"initialized"}\n'); await process.stdin.drain()
                    started = await rpc(2, 'thread/start', dict(cwd=tmp, ephemeral=False))
                    thread_id = started['thread']['id']
                    await rpc(3, 'thread/name/set', dict(threadId=thread_id, name='bridge transport probe'))
                    record = json.loads(next((root / 'instances').glob('*.json')).read_text())
                    if 'url' in record:
                        token = Path(record['token']).read_text().strip()
                        transport = connect(record['url'], additional_headers={'Authorization': 'Bearer ' + token})
                    else:
                        transport = unix_connect(record['socket'])
                    async with transport as ws:
                        async def secondary(ident, method, params):
                            await ws.send(json.dumps(dict(id=ident, method=method, params=params)))
                            while True:
                                msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
                                if msg.get('id') == ident:
                                    self.assertNotIn('error', msg)
                                    return msg['result']
                        await secondary(1, 'initialize', dict(clientInfo=dict(name='bridge_terminal_test', version='1'), capabilities=dict(experimentalApi=True)))
                        await ws.send('{"method":"initialized"}')
                        loaded = await secondary(2, 'thread/loaded/list', {})
                        self.assertIn(thread_id, loaded['data'])
                        resumed = await secondary(3, 'thread/resume', dict(threadId=thread_id, excludeTurns=True))
                        self.assertEqual(resumed['thread']['id'], thread_id)
                    process.stdin.close()
                    await asyncio.wait_for(process.wait(), 10)
                    self.assertEqual(process.returncode, 0, (root / 'stderr').read_text())
                    self.assertFalse(list((root / 'instances').glob('*.json')))
                    self.assertFalse(list((root / 'instances').glob('*.token')))
                    if 'socket' in record:
                        self.assertFalse(Path(record['socket']).exists())
                finally:
                    if process.returncode is None:
                        process.terminate(); await process.wait()
