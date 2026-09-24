"""Bounded G1 sentry MCP card; default warning-only. No model in frame loop."""
import asyncio
import contextlib
import json
import math
import os
import time
from collections import deque
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from sentry_rules import SentryRules


class Sentry:
    def __init__(self, call, stream, permit_path):
        self.call, self.stream = call, stream
        self.permit_path = Path(permit_path)
        self.task = self.action_task = None
        self.rules = SentryRules()
        self.events = deque(maxlen=100)
        self.latest = None
        self.samples = 0
        self.moving = False
        self.lock = asyncio.Lock()

    def record(self, kind, **details):
        self.events.append({'time': time.time(), 'kind': kind, **details})

    def info(self):
        return {'state': 'running' if self.task and not self.task.done() else 'idle',
                'samples': self.samples, 'latest': self.latest, 'events': list(self.events),
                'motion_policy': 'disabled unless operator supplies a short-lived single-use permit',
                'source_limit': 'center-point depth, not person detection; reception time is not exposure time'}

    async def start(self):
        async with self.lock:
            if not self.task or self.task.done():
                self.rules = SentryRules()
                self.latest = None
                self.samples = 0
                self.task = asyncio.create_task(self.run())
        return self.info()

    async def stop(self):
        async with self.lock:
            if self.task and not self.task.done():
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
        return self.info()

    def sample_matches(self, kind):
        if not self.latest or time.monotonic() - self.latest['received_monotonic'] > .5:
            return False
        d = self.latest['distance_m']
        return (0 < d < .15) if kind == 'occlusion_suspected' else (.15 <= d < .8)

    async def move_if_permitted(self, kind):
        # No permit-creation MCP tool. The on-site operator supplies it out-of-band.
        try:
            permit = json.loads(self.permit_path.read_text())
        except (OSError, ValueError):
            return
        expires = permit.get('expires_at')
        if (not isinstance(expires, (int, float)) or isinstance(expires, bool)
                or not math.isfinite(expires) or not 0 < expires - time.time() <= 90
                or permit.get('branch') != kind or permit.get('supervised') is not True
                or permit.get('space_clear') is not True or not self.sample_matches(kind)):
            self.record('motion_blocked', reason='permit or fresh distance missing')
            return
        mode = await self.call('switch_mode', {'action': 'get_current_mode'})
        before = mode.get('posture_detail', {}).get('ts')
        await asyncio.sleep(.15)
        posture = await self.call('posture', {})
        after = posture.get('ts')
        if (mode.get('fsm_id') != 801 or posture.get('posture') != 'standing'
                or not all(isinstance(t, (int, float)) and math.isfinite(t) for t in (before, after))
                or not 0 < after - before < 2 or time.time() >= expires
                or not self.sample_matches(kind)):
            self.record('motion_blocked', reason='mode, posture or freshness failed')
            return
        # Consume permission BEFORE dispatch, including when dispatch subsequently fails.
        self.permit_path.unlink()
        args = ({'vx': -.2, 'vy': 0, 'vyaw': 0, 'duration': 1.0} if kind == 'near'
                else {'vx': 0, 'vy': 0, 'vyaw': .3, 'duration': .8})
        self.moving = True
        try:
            result = await self.call('loco', {'action': 'move', **args})
            self.record('motion_requested', branch=kind, arguments=args, response=result,
                        physical_success='unverified')
            until = time.monotonic() + args['duration']
            while time.monotonic() < until:
                if not self.latest or time.monotonic() - self.latest['received_monotonic'] > .5:
                    break
                await asyncio.sleep(.05)
        finally:
            await self.call('loco', {'action': 'stop_move'})
            self.moving = False
            self.record('stop_requested')

    async def alert(self, kind):
        try:
            if kind == 'clear':
                await self.call('led', {'action': 'set', 'r': 255, 'g': 200, 'b': 0})
                return
            # LED flashes before speech; camera receive loop continues throughout.
            for _ in range(2):
                await self.call('led', {'action': 'set', 'r': 255, 'g': 0, 'b': 0})
                await asyncio.sleep(.15)
                await self.call('led', {'action': 'set', 'r': 0, 'g': 0, 'b': 0})
                await asyncio.sleep(.15)
            await self.call('led', {'action': 'set', 'r': 255, 'g': 0, 'b': 0})
            message = '镜头前方过近，请勿遮挡' if kind == 'occlusion_suspected' else '前方距离较近，请保持距离'
            await self.call('tts', {'action': 'speak', 'text': message})
            self.record('warning_requested', branch=kind)
            await self.move_if_permitted(kind)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.record('action_error', error=type(exc).__name__)

    def feed(self, data):
        now = time.monotonic()
        self.samples += 1
        d = data.get('distance_m') if isinstance(data, dict) else None
        self.latest = ({'distance_m': d, 'received_monotonic': now}
                       if isinstance(d, (int, float)) and not isinstance(d, bool)
                       and math.isfinite(d) and d > 0 else None)
        event = self.rules.feed(data, received_at=now, now=now)
        if event:
            self.record(event.kind, distance_m=event.distance_m)
            if event.kind in ('near', 'occlusion_suspected', 'clear'):
                if self.action_task and not self.action_task.done():
                    # Never queue stale movement behind a blocking voice request.
                    self.record('action_skipped_busy', branch=event.kind)
                else:
                    self.action_task = asyncio.create_task(self.alert(event.kind))

    async def consume(self):
        async for sample in self.stream():
            self.feed(sample)

    async def run(self):
        self.record('started')
        try:
            await self.call('led', {'action': 'set', 'r': 255, 'g': 200, 'b': 0})
            # A lost connection or two seconds without a frame ends this session.
            await asyncio.wait_for(self.consume(), timeout=180)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.record('session_ended', error=type(exc).__name__)
        finally:
            if self.action_task and not self.action_task.done():
                self.action_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.action_task
            if self.moving:
                try:
                    await self.call('loco', {'action': 'stop_move'})
                    self.moving = False
                except Exception as exc:
                    self.record('stop_failed', error=type(exc).__name__)
            try:
                await self.call('led', {'action': 'state', 'state': 'idle'})
            except Exception as exc:
                self.record('cleanup_failed', error=type(exc).__name__)
            self.record('stopped')


class Backend:
    def __init__(self, session, base, token):
        self.session, self.base, self.token = session, base.rstrip('/'), token

    async def call(self, tool, arguments):
        if tool not in ('led', 'tts', 'loco', 'switch_mode', 'posture'):
            raise ValueError('Tool not allowed')
        async with self.session.post(self.base + '/api/mcp/driver-unitree-g1/call',
                json={'tool': tool, 'arguments': arguments}, allow_redirects=False) as response:
            response.raise_for_status()
            outer = await response.json()
        if outer.get('code') != 200:
            raise RuntimeError('Driver API error')
        data = outer.get('data')
        if isinstance(data, dict) and data.get('isError'):
            raise RuntimeError('MCP error')
        if isinstance(data, dict) and 'content' in data:
            data = data['content']
        if isinstance(data, list):
            data = json.loads(next(item['text'] for item in data if item.get('type') == 'text'))
        if not isinstance(data, dict) or data.get('error') or data.get('ret') not in (None, 0):
            raise RuntimeError('Driver rejected operation')
        return data

    async def stream(self):
        url = self.base.replace('https://', 'wss://').replace('http://', 'ws://')
        async with self.session.ws_connect(url + '/ws/bus/ubuntu/camera/distance',
                params={'token': self.token}, receive_timeout=2) as ws:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    raise RuntimeError('Distance stream disconnected')
                data = json.loads(message.data)
                if isinstance(data, dict) and data.get('type') in ('meta', 'ping'):
                    continue
                yield data


TOOL = {'name': 'g1_sentry', 'type': 'actuator',
        'description': 'G1 deterministic distance sentry. start=warning-only unless onsite operator supplied one-use permission; stop cancels session; info=status. Maximum 180 seconds. Never auto-start.',
        'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string',
                        'enum': ['start', 'stop', 'info']}}, 'required': ['action']}}


async def create_app():
    base = os.environ.get('G1_CORE_URL', 'https://127.0.0.1:15678')
    token = Path(os.environ['G1_TOKEN_FILE']).read_text().strip()
    # The local deployment uses a self-signed certificate. Only loopback is allowed.
    from urllib.parse import urlsplit
    if urlsplit(base).hostname not in ('127.0.0.1', 'localhost'):
        raise ValueError('Backend must be loopback')
    import aiohttp
    session = ClientSession(headers={'Authorization': 'Bearer ' + token},
                            timeout=ClientTimeout(total=5),
                            connector=aiohttp.TCPConnector(ssl=False))
    backend = Backend(session, base, token)
    sentry = Sentry(backend.call, backend.stream, Path(__file__).with_name('motion-permit.json'))

    async def rpc(request):
        body = await request.json()
        rid, method = body.get('id'), body.get('method')
        if rid is None:
            return web.Response(status=202)
        if method == 'initialize':
            result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'g1-sentry-controller', 'version': '1.2.0'}}
        elif method == 'tools/list':
            result = {'tools': [TOOL]}
        elif method == 'resources/list':
            result = {'resources': []}
        elif method == 'tools/call':
            params = body.get('params', {})
            action = params.get('arguments', {}).get('action')
            try:
                if params.get('name') != 'g1_sentry':
                    raise ValueError('Unknown tool')
                if action == 'start':
                    data = await sentry.start()
                elif action == 'stop':
                    data = await sentry.stop()
                elif action == 'info':
                    data = sentry.info()
                else:
                    raise ValueError('Unknown action')
                result = {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}]}
            except Exception as exc:
                result = {'isError': True, 'content': [{'type': 'text', 'text': type(exc).__name__}]}
        else:
            return web.json_response({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'Unknown method'}})
        return web.json_response({'jsonrpc': '2.0', 'id': rid, 'result': result})

    async def cleanup(app):
        await sentry.stop()
        await session.close()
    app = web.Application()
    app.router.add_post('/mcp', rpc)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == '__main__':
    web.run_app(create_app(), host='127.0.0.1', port=18791, print=None)
