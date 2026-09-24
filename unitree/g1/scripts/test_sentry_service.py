import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from sentry_service import Sentry


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.fail_move = False
        async def call(tool, args):
            self.calls.append((tool, args))
            if tool == 'switch_mode':
                return {'fsm_id': 801, 'posture_detail': {'ts': time.time()}}
            if tool == 'posture':
                return {'posture': 'standing', 'ts': time.time()}
            if tool == 'loco' and args['action'] == 'move' and self.fail_move:
                raise RuntimeError('connection failed')
            return {'ret': 0}
        async def stream():
            while True:
                yield {'distance_m': 2.0}
                await asyncio.sleep(.05)
        self.sentry = Sentry(call, stream, Path(self.tmp.name)/'permit.json')

    def near(self):
        self.sentry.latest = {'distance_m': .7, 'received_monotonic': time.monotonic()}

    def permit(self, **changes):
        data = {'expires_at': time.time()+30, 'branch': 'near', 'supervised': True, 'space_clear': True}
        data.update(changes)
        self.sentry.permit_path.write_text(json.dumps(data))

    async def test_no_permit_never_moves(self):
        self.near()
        await self.sentry.move_if_permitted('near')
        self.assertEqual(self.calls, [])

    async def test_invalid_expired_or_wrong_branch_permit_never_moves(self):
        for changes in ({'expires_at':time.time()-1},{'supervised':False},{'branch':'occlusion_suspected'}, {'space_clear':False}):
            self.permit(**changes); self.near()
            await self.sentry.move_if_permitted('near')
        self.assertEqual(self.calls, [])

    async def test_stale_distance_blocks_before_status_query(self):
        self.permit(); self.near()
        self.sentry.latest['received_monotonic'] -= 3
        await self.sentry.move_if_permitted('near')
        self.assertEqual(self.calls, [])

    async def test_failed_move_consumes_permit_and_still_stops(self):
        self.permit(); self.near(); self.fail_move = True
        with self.assertRaises(RuntimeError):
            await self.sentry.move_if_permitted('near')
        self.assertFalse(self.sentry.permit_path.exists())
        self.assertEqual(self.calls[-1], ('loco', {'action':'stop_move'}))

    async def test_permission_only_used_once(self):
        self.permit(); self.near()
        await self.sentry.move_if_permitted('near')
        self.near()
        await self.sentry.move_if_permitted('near')
        self.assertEqual(sum(t=='loco' and a['action']=='move' for t,a in self.calls),1)

    async def test_sensor_consumption_continues_during_slow_alert(self):
        self.sentry.action_task = asyncio.create_task(asyncio.sleep(5))
        for _ in range(5):
            self.sentry.feed({'distance_m':.7})
            await asyncio.sleep(.06)
        self.assertEqual(self.sentry.samples,5)
        self.assertTrue(any(e['kind']=='near' for e in self.sentry.events))
        self.sentry.action_task.cancel()
        with self.assertRaises(asyncio.CancelledError): await self.sentry.action_task

    async def test_stop_cancels_stream_and_restores_light(self):
        await self.sentry.start(); await asyncio.sleep(.15)
        before=self.sentry.samples
        await self.sentry.stop(); await asyncio.sleep(.1)
        self.assertEqual(self.sentry.samples,before)
        self.assertEqual(self.sentry.info()['state'],'idle')
        self.assertEqual(self.calls[-1],('led',{'action':'state','state':'idle'}))

    async def test_stream_failure_cleans_up(self):
        async def broken():
            yield {'distance_m':2}
            raise ConnectionError('lost')
        self.sentry.stream=broken
        await self.sentry.start(); await self.sentry.task
        self.assertEqual(self.sentry.info()['state'],'idle')
        self.assertTrue(any(e['kind']=='session_ended' for e in self.sentry.events))

    async def test_bad_sample_invalidates_movement_freshness(self):
        for value in (0,float('nan'),True,None):
            self.sentry.feed({'distance_m':value})
            self.assertIsNone(self.sentry.latest)

if __name__=='__main__':unittest.main()
