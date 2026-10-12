"""Joint trajectory data capture and guarded physical replay contracts."""
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agilex.piper.trajectory_stream import TrajectoryStream
from agilex.piper.cards import build_tools


class Node:
    def __init__(self, *args, **kwargs):
        self.messages = []
        self.destroyed = False

    def create_publisher(self, *_args):
        return SimpleNamespace(publish=lambda msg: self.messages.append(json.loads(msg.data)))

    def create_timer(self, period, callback):
        self.period, self.callback = period, callback
        return SimpleNamespace(cancel=Mock())

    def destroy_node(self):
        self.destroyed = True

    def get_logger(self):
        return SimpleNamespace(error=Mock())


class TrajectoryStreamTests(unittest.TestCase):
    def test_card_marks_physical_replay_as_guarded_arm_motion(self):
        card = next(tool for tool in build_tools('/arm/piper/rgb')
                    if tool['name'] == 'piper_trajectory')
        self.assertEqual(card['type'], 'actuator')
        self.assertEqual(card['topic_out'], [{'topic': '/arm/piper/trajectory', 'format': 'data/json'}])
        self.assertTrue(card['inputSchema']['x-is-dangerous'])
        self.assertIn('replay', card['inputSchema']['properties']['action']['enum'])
        self.assertIn('preview', card['inputSchema']['properties']['action']['enum'])
        self.assertIn('replay_data', card['inputSchema']['properties']['action']['enum'])

    def setUp(self):
        modules = {'rclpy.node': SimpleNamespace(Node=Node),
                   'rclpy.qos': SimpleNamespace(QoSProfile=lambda **kw: kw,
                       ReliabilityPolicy=SimpleNamespace(RELIABLE=1),
                       DurabilityPolicy=SimpleNamespace(VOLATILE=0)),
                   'std_msgs.msg': SimpleNamespace(String=SimpleNamespace)}
        stub = patch.dict(sys.modules, modules)
        stub.start()
        self.addCleanup(stub.stop)
        self.ros = SimpleNamespace(ctx_core=object(), executor_core=Mock())
        self.angle = 0
        self.stream = TrajectoryStream(self.ros, '/arm/piper/trajectory', self.read)
        self.addCleanup(self.stream.close)

    def read(self):
        self.angle += 1
        return {'timestamp_ms': self.angle * 200, 'feedback_time': self.angle,
                'joints_deg': [float(self.angle)] * 6, 'available': True, 'fresh': True}

    def test_record_and_replay_publish_feedback_without_arm_commands(self):
        self.assertEqual(self.stream.record()['state'], 'recording')
        self.assertEqual(self.stream.node.period, .2)
        self.stream.tick()
        self.stream.tick()
        self.assertEqual(self.stream.finish()['sample_count'], 2)
        self.assertEqual(self.stream.replay()['state'], 'replaying_data')
        self.stream.tick()
        self.stream.tick()
        self.stream.tick()
        self.assertEqual(self.stream.info()['state'], 'recorded')
        events = self.stream.node.messages
        self.assertEqual([m['event'] for m in events],
                         ['sample', 'sample', 'record_complete', 'replay_sample', 'replay_sample', 'replay_complete'])
        self.assertEqual(events[3]['joints_deg'], [1.] * 6)
        self.assertFalse(events[3]['arm_motion'])
        self.assertEqual(self.ros.executor_core.add_node.call_count, 1)

    def test_read_error_ends_capture_and_card_stop_releases_ros_node(self):
        self.stream.read = Mock(side_effect=RuntimeError('feedback stale'))
        self.stream.record()
        node = self.stream.node
        self.stream.tick()
        self.assertEqual(self.stream.info()['state'], 'error')
        self.assertEqual(self.stream.info()['last_error'], 'feedback stale')
        self.stream.stop()
        self.assertTrue(node.destroyed)
        self.ros.executor_core.remove_node.assert_called_once_with(node)
        self.assertEqual(self.stream.info()['topic_out'], [])

    def test_replay_requires_a_recording_and_clear_retains_no_samples(self):
        with self.assertRaisesRegex(RuntimeError, '没有可回放'):
            self.stream.replay()
        self.stream.record()
        self.stream.tick()
        self.stream.finish()
        self.assertEqual(self.stream.clear()['sample_count'], 0)


if __name__ == '__main__':
    unittest.main()
