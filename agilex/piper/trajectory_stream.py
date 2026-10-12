"""Read-only joint trajectory capture and data replay over the canvas DDS bus."""
import json
import threading
import time


class TrajectoryStream:
    PERIOD = .2
    MAX_SAMPLES = 1500
    MAX_DURATION_S = 300

    def __init__(self, ros2, topic, read):
        self.ros2, self.topic, self.read = ros2, topic, read
        self.lock = threading.RLock()
        self.node = self.publisher = self.timer = None
        self.phase = "idle"
        self.samples = []
        self.replay_index = 0
        self.last_error = None
        self.record_deadline = None

    def _ensure_node(self):
        if self.ros2 is None:
            raise RuntimeError("轨迹数据流需要 ROS2 通道；当前环境无法录制或回放数据")
        if self.node is not None:
            return
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        from std_msgs.msg import String

        node = Node("piper_joint_trajectory", context=self.ros2.ctx_core)
        try:
            qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.VOLATILE)
            self.publisher = node.create_publisher(String, self.topic, qos)
            self.timer = node.create_timer(self.PERIOD, self.tick)
            self.ros2.executor_core.add_node(node)
            self.node = node
        except Exception:
            self.ros2.executor_core.remove_node(node)
            node.destroy_node()
            self.publisher = self.timer = None
            raise

    def _publish(self, data):
        from std_msgs.msg import String
        message = String()
        message.data = json.dumps(data, ensure_ascii=False, allow_nan=False)
        self.publisher.publish(message)

    def record(self):
        with self.lock:
            self._ensure_node()
            if self.phase != "recording":
                self.samples.clear()
                self.replay_index = 0
                self.record_deadline = time.monotonic() + self.MAX_DURATION_S
            if len(self.samples) >= self.MAX_SAMPLES:
                raise RuntimeError("本次轨迹已达到 300 秒上限；先结束录制")
            self.phase = "recording"
            self.last_error = None
            return self.info()

    def finish(self):
        with self.lock:
            if self.phase == "recording":
                self.phase = "recorded"
                if self.node is not None:
                    self._publish({"event": "record_complete", "sample_count": len(self.samples),
                                   "arm_motion": False})
            return self.info()

    def replay(self):
        with self.lock:
            if not self.samples:
                raise RuntimeError("没有可回放的轨迹数据；请先录制")
            self._ensure_node()
            self.replay_index = 0
            self.phase = "replaying_data"
            self.last_error = None
            return self.info()

    def clear(self):
        with self.lock:
            if self.phase in {"recording", "replaying_data"}:
                raise RuntimeError("请先结束录制或数据回放，再清除轨迹")
            self.samples.clear()
            self.replay_index = 0
            self.phase = "idle"
            self.last_error = None
            return self.info()

    def tick(self):
        with self.lock:
            if self.node is None:
                return
            try:
                if self.phase == "recording":
                    if time.monotonic() >= self.record_deadline:
                        self.phase = "recorded"
                        self._publish({"event": "record_complete", "sample_count": len(self.samples),
                                       "limit_reached": True, "arm_motion": False})
                        return
                    sample = self.read()
                    if len(self.samples) >= self.MAX_SAMPLES:
                        self.phase = "recorded"
                        raise RuntimeError("已达到 300 秒上限，录制自动结束")
                    self.samples.append(sample)
                    self._publish({"event": "sample", **sample, "index": len(self.samples) - 1})
                    if len(self.samples) == self.MAX_SAMPLES:
                        self.phase = "recorded"
                        self._publish({"event": "record_complete", "sample_count": len(self.samples),
                                       "limit_reached": True, "arm_motion": False})
                elif self.phase == "replaying_data":
                    if self.replay_index >= len(self.samples):
                        self.phase = "recorded"
                        self._publish({"event": "replay_complete", "sample_count": len(self.samples),
                                       "arm_motion": False})
                        return
                    sample = self.samples[self.replay_index]
                    self._publish({"event": "replay_sample", **sample,
                                   "index": self.replay_index, "arm_motion": False})
                    self.replay_index += 1
            except Exception as exc:
                self.last_error = str(exc)
                self.phase = "error"
                self.node.get_logger().error(self.last_error)

    def info(self):
        with self.lock:
            return {"state": self.phase, "sample_count": len(self.samples),
                    "sample_limit": self.MAX_SAMPLES, "refresh_hz": 1 / self.PERIOD,
                    "replay_index": self.replay_index, "last_error": self.last_error,
                    "arm_motion": self.phase == "replaying_arm",
                    "topic_out": [{"topic": self.topic, "format": "data/json"}] if self.node else []}

    def stop(self):
        with self.lock:
            if self.phase == "recording":
                self.phase = "recorded"
            elif self.phase == "replaying_data":
                self.phase = "recorded" if self.samples else "idle"
            if self.node is not None:
                try:
                    self._publish({"event": "stopped", "sample_count": len(self.samples),
                                   "arm_motion": False})
                except Exception as exc:
                    self.last_error = str(exc)
                finally:
                    if self.timer is not None:
                        self.timer.cancel()
                    self.ros2.executor_core.remove_node(self.node)
                    self.node.destroy_node()
                    self.node = self.publisher = self.timer = None
            return self.info()

    def close(self):
        with self.lock:
            self.stop()
