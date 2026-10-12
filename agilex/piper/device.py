"""Eleven focused cards on one shared PiPER runtime and CAN connection."""
from dataclasses import asdict
import hashlib
import json
import math
import time

from agilex.piper.cards import TITLES, build_tools
from agilex.piper.control import healthy, mode_name, ready_to_move, teaching_stopped
from agilex.piper.runtime import PiperRuntime, confirm, number
from agilex.piper.trajectory_stream import TrajectoryStream


class PiperPlugin(PiperRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.trajectory_stream = TrajectoryStream(self.ros2, self.topic.rsplit('/', 1)[0] + '/trajectory',
                                                   self.read_trajectory_sample)

    def get_tools(self):
        return build_tools(self.topic)

    def read_trajectory_sample(self):
        with self.connection_lock:
            if self.closed or 'piper_trajectory' in self.stopped_cards:
                raise RuntimeError('轨迹卡片已停止，请先启动卡片')
            state = self.connected().get_state()
            now = time.time()
            if (not math.isfinite(state.feedback_time) or abs(now - state.feedback_time) > .5
                    or len(state.joints_deg) != 6
                    or not all(math.isfinite(v) for v in state.joints_deg)):
                raise RuntimeError('六轴反馈无效或超过 0.5 秒未更新')
            return {'timestamp_ms': int(now * 1000), 'feedback_time': state.feedback_time,
                    'joints_deg': list(state.joints_deg), 'available': True, 'fresh': True}

    def request_stop(self, name=None):
        if name is None:
            with self.trajectory_stream.lock:
                self.trajectory_stream.stop()
                return super().request_stop(name)
        if name == 'piper_trajectory':
            result = super().request_stop(name)
            with self.trajectory_stream.lock:
                stream_result = self.trajectory_stream.stop()
            return {**result, 'trajectory': stream_result}
        return super().request_stop(name)

    def stop(self):
        with self.trajectory_stream.lock:
            self.trajectory_stream.close()
            super().stop()

    def _validate(self, name, action, args):
        definition = next((t for t in self.get_tools() if t['name'] == name), None)
        if definition is None:
            raise ValueError('未知卡片；请刷新十卡片工具列表')
        properties = definition['inputSchema']['properties']
        if action not in properties['action']['enum']:
            raise ValueError(f'「{TITLES[name]}」不支持此操作；请使用对应功能卡片')
        for key, value in args.items():
            if key in {'_tool_name', 'action', 'instance_id'}:
                continue
            if key not in properties:
                raise ValueError(f'「{TITLES[name]}」不使用参数 {key}')
            prop = properties[key]
            if prop['type'] == 'boolean' and type(value) is not bool:
                raise ValueError(f'{prop["title"]}必须选择已确认或未确认')
            if prop['type'] in {'number', 'integer'}:
                number(args, key, None, prop.get('minimum', -math.inf), prop.get('maximum', math.inf),
                       integer=prop['type'] == 'integer')
            if prop['type'] == 'string' and not isinstance(value, str):
                raise ValueError(f'{prop["title"]}必须是文本')
            if 'enum' in prop and value not in prop['enum']:
                raise ValueError(f'{prop["title"]}的选项无效')

    def dispatch(self, action, args):
        name = args.get('_tool_name', 'piper_status')
        if name == 'piper_status':
            action = args.get('action', 'read')
        self._validate(name, action, args)
        long_action = (name == 'piper_photo' and action in {'capture', 'import_photo'}
                       or name in {'piper_enable', 'piper_joint'} and args.get('execute') is True
                       or name == 'piper_trajectory' and action == 'replay' and args.get('execute') is True
                       or name == 'piper_gripper' and action in {'open', 'close', 'set_width'} and args.get('execute') is True)
        if long_action and self.config.get('async_actions', True):
            previous = getattr(self.worker_context, 'operation_object', None)
            self.worker_context.operation_object = args.get('object', 'arm') if name in {'piper_enable', 'piper_disable'} else None
            try:
                return self.run_async(name, action, args)
            finally:
                self.worker_context.operation_object = previous
        return self.dispatch_sync(action, args)

    def dispatch_sync(self, action, args):
        name = args.get('_tool_name', 'piper_status')
        if name == 'piper_status':
            action = args.get('action', 'read')
        self._validate(name, action, args)
        if name == 'piper_trajectory':
            if action in {'start', 'stop', 'info'}:
                if action == 'stop':
                    return self.request_stop(name)
                if action == 'start':
                    self.start(name)
                    return {'state': 'ready', 'message': '轨迹卡片已就绪；尚未开始记录或运动'}
                return self.trajectory_stream.info()
            if action in {'record', 'finish', 'replay_data', 'clear'}:
                self.start(name)
                with self.operation(name, action), self.trajectory_stream.lock:
                    if action == 'record':
                        return self.trajectory_stream.record()
                    if action == 'finish':
                        return self.trajectory_stream.finish()
                    if action == 'replay_data':
                        return self.trajectory_stream.replay()
                    return self.trajectory_stream.clear()
            if action in {'preview', 'replay'}:
                speed = number(args, 'speed_deg_s', 1, .5, 2)
                speed_pct = number(args, 'speed_pct', 3, 1, 5, integer=True)
                execute = args.get('execute', False)
                if execute:
                    self.motion_allowed(args)
                    confirm(args, 'take_control')
                with self.operation(name, action):
                    driver = self.connected()
                    state = self.read_state(monitor=True)
                    if not math.isfinite(state.feedback_time) or abs(time.time() - state.feedback_time) > .5:
                        raise RuntimeError('六轴反馈超过 0.5 秒未更新，拒绝预览或执行')
                    with self.trajectory_stream.lock:
                        if self.trajectory_stream.phase != 'recorded':
                            raise RuntimeError('请先结束录制或数据回放；实体回放只使用完整轨迹')
                        samples = list(self.trajectory_stream.samples)
                    plan, points, digest = self._trajectory_plan(driver, state, samples, speed, speed_pct)
                    key = f'trajectory-replay:{digest}:{speed}:{speed_pct}'
                    if not execute:
                        return {**plan, 'executed': False, **self.previews.put(key, state, digest)}
                    self.previews.consume(args.get('preview_id') or None, key, state, digest)
                    with self.trajectory_stream.lock:
                        if self.trajectory_stream.phase != 'recorded':
                            raise RuntimeError('轨迹状态已变化，请重新预览')
                        self.trajectory_stream.phase = 'replaying_arm'
                    try:
                        result = driver.replay_joint_trajectory(points, speed, speed_pct)
                        return {'executed': True, 'sample_count': len(points), **result}
                    finally:
                        with self.trajectory_stream.lock:
                            self.trajectory_stream.phase = 'recorded'
            raise ValueError(f'Unknown trajectory action: {action}')
        if action == 'start':
            self.start(name)
            return {'state': 'ready', 'message': f'「{TITLES[name]}」已就绪；未使能或移动'}
        if action == 'stop':
            return self.request_stop(name)
        if action == 'info':
            return {'state': 'idle' if self.closed or name in self.stopped_cards else 'ready',
                    'title': TITLES[name], 'busy': self.gate.locked(), 'active_card': self.active_card,
                    'last_action': self.last_actions.get(name), 'motion_enabled': self.motion_enabled,
                    'camera_enabled': self.camera_enabled,
                    'topic_out': [{'topic': self.topic, 'format': 'image/jpeg'}] if name == 'piper_photo' else []}
        previous = getattr(self.worker_context, 'public_call', None)
        previous_object = getattr(self.worker_context, 'operation_object', None)
        self.worker_context.public_call = (name, action)
        self.worker_context.operation_object = args.get('object', 'arm') if name in {'piper_enable', 'piper_disable'} else None
        try:
            if name == 'piper_status':
                if self.closed or name in self.stopped_cards:
                    raise RuntimeError('机器人状态卡片已停止，请先启动卡片')
                if action in {'firmware', 'reconnect'}:
                    with self.operation(name, action):
                        if action == 'reconnect':
                            self.disconnect()
                        else:
                            self.firmware_version = self.connected().firmware(query=True)
                            self.previews.invalidate()
                return self.status()
            if name == 'piper_permission':
                return self.motion_permission('allow_motion' if action == 'allow' else 'block_motion')
            if name == 'piper_stop':
                return self.halt(args.get('target', 'all'))
            if self.closed or name in self.stopped_cards:
                raise RuntimeError(f'「{TITLES[name]}」已停止，请先启动卡片')
            if name in {'piper_standby', 'piper_can'}:
                return self.switch_card('standby' if name == 'piper_standby' else 'can', args)
            if name == 'piper_enable':
                return self.enable_card(args)
            if name == 'piper_disable':
                return self.disable_card(args)
            if name == 'piper_joint':
                if 'target_deg' not in args:
                    raise ValueError('请填写所选关节的绝对目标角度（°）')
                translated = {**args, '_tool_name': 'piper_arm'}
                return super().dispatch_sync('move_joint', translated)
            if name == 'piper_gripper':
                if action != 'read':
                    grip = self.connected().get_gripper()
                    if not grip.enabled:
                        raise RuntimeError('夹爪尚未使能；请到「电机使能」选择夹爪，再回来开合')
                return self.gripper_action(action, args)
            if name == 'piper_photo':
                with self.operation(name, action):
                    return self.capture(args) if action == 'capture' else self.import_photo(args)
            raise ValueError('未知卡片')
        finally:
            self.worker_context.public_call = previous
            self.worker_context.operation_object = previous_object

    def status(self):
        result = super().status()
        result['active_title'] = TITLES.get(result['active_card'])
        result['completed_actions'] = dict(self.last_actions)
        result['software_teaching_verified'] = self.verified_teaching()
        result['teaching_guidance'] = '软件示教流程尚未验收；请按对应固件手册使用机身按键。'
        state = self.driver.get_state()
        if result['busy']:
            next_step = '当前有动作执行中；请等待完成或使用「停止动作」'
        elif state.error_code or state.motor_faults or not state.arm_status.startswith('NORMAL'):
            next_step = '设备有故障或异常；请先检查状态，暂停控制操作'
        elif any(state.enabled) and not all(state.enabled):
            next_step = '六轴部分使能；先检查状态，可靠支撑后可到「电机失能」处理'
        elif mode_name(state) not in {'standby', 'can'} or not teaching_stopped(state):
            next_step = '先按对应固件流程停止示教/轨迹并退出其他控制模式，再读取状态'
        elif not result['motion_enabled']:
            next_step = '先到「操作许可」开启本次控制'
        elif mode_name(state) == 'standby':
            next_step = '到「切换电脑控制」进入 CAN，再单独使能电机'
        elif not any(state.enabled):
            next_step = '到「电机使能」使能六轴'
        else:
            next_step = '已可预览单轴运动；夹爪使能状态请单独检查'
        result['next_step'] = next_step
        return result

    def switch_card(self, target, args):
        state = self.read_state()
        healthy(state)
        if mode_name(state) == target and teaching_stopped(state):
            return {'confirmed': True, 'executed': False, 'state': asdict(state),
                    'message': '当前已是待机' if target == 'standby' else '当前已是电脑控制（CAN）',
                    'next_step': '模式和电机使能独立；按需使用「电机使能」或「电机失能」'}
        translated = {**args, 'target_mode': target}
        plan = self.mode_action('switch' if args.get('execute', False) else 'preview', translated)
        if not plan.get('allowed', True):
            plan['next_step'] = '当前驱动要求六轴全部失能；可靠支撑后到「电机失能」操作，再重新切换'
            plan['message'] = '切换条件未满足：' + '；'.join(plan['missing_conditions'])
        return plan

    def enable_card(self, args):
        obj = args.get('object', 'arm')
        with self.operation('piper_enable', 'enable'):
            driver = self.connected()
            state = self.read_state()
            healthy(state)
            if mode_name(state) != 'can':
                raise RuntimeError('当前不是电脑控制；请先到「切换电脑控制」进入 CAN，再使能')
            if not teaching_stopped(state):
                raise RuntimeError('请先停止示教录制或回放，再使能')
            if obj == 'arm':
                if all(state.enabled):
                    return {'confirmed': True, 'executed': False, 'state': asdict(state),
                            'message': '六轴已使能，当前已就绪；未重复发指令', 'next_step': '可预览单轴运动'}
                self.require_motion_permission()
                translated = {**args, '_tool_name': 'piper_arm'}
                return super().dispatch_sync('prepare', translated)
            ready_to_move(state)
            grip = driver.get_gripper()
            if grip.error_code:
                raise RuntimeError('夹爪有故障，请先查看机器人状态')
            if grip.enabled:
                return {'confirmed': True, 'executed': False, 'gripper': asdict(grip),
                        'message': '夹爪已使能；未重复发指令', 'next_step': '可预览夹爪开合'}
            self.require_motion_permission()
            translated = {**args, 'width_mm': grip.width_mm, 'torque_nm': .5}
            result = self.gripper_action('set_width', translated)
            if not args.get('execute', False):
                result['start_mm'] = grip.width_mm
            return result

    def disable_card(self, args):
        obj = args.get('object', 'arm')
        with self.operation('piper_disable', 'disable'):
            driver = self.connected()
            state = self.read_state()
            if obj == 'arm' and not any(state.enabled):
                return {'confirmed': True, 'executed': False, 'state': asdict(state), 'message': '六轴已经全部失能'}
            grip = driver.get_gripper() if obj == 'gripper' else None
            if grip is not None and not grip.enabled:
                return {'confirmed': True, 'executed': False, 'gripper': asdict(grip), 'message': '夹爪已经失能'}
            if args.get('supported') is not True:
                raise ValueError('失能前请确认机械臂已可靠支撑，或夹爪空载/夹持物已支撑')
            self.require_motion_permission()
            self.previews.invalidate()
            result = asdict(driver.disable(supported=True) if obj == 'arm' else driver.disable_gripper())
            return {'confirmed': True, 'executed': True, 'state' if obj == 'arm' else 'gripper': result,
                    'message': '已确认失能；相应电机保持力已释放'}

    def halt(self, target):
        affected_cards = {'piper_joint', 'piper_enable', 'piper_disable', 'piper_standby', 'piper_can', 'piper_trajectory'}
        if target == 'gripper':
            affected_cards = {'piper_gripper'}
        elif target == 'all':
            affected_cards |= {'piper_gripper', 'piper_photo'}
        with self.lifecycle:
            self.previews.invalidate()
            active = self.active_card
            in_scope = active in affected_cards
            if active in {'piper_enable', 'piper_disable'} and target != 'all':
                in_scope = self.active_object == ('arm' if target == 'arm' else 'gripper')
            affected = self.gate.locked() and in_scope
            if affected:
                self.cancel.set()
                if self.driver is not None and active != 'piper_photo':
                    self.driver.cancel_motion()
        return {'state': 'stopping' if affected else 'idle', 'operation_stopping': affected,
                'active_card': active, 'motor_torque_released': False,
                'message': '已请求取消并尽力保持；请读取反馈和完成结果' if affected else '本驱动当前没有所选范围内的动作',
                'next_step': '需要释放保持力时，到「电机失能」并确认支撑'}

    @staticmethod
    def _trajectory_plan(driver, state, samples, speed_deg_s, speed_pct):
        ready_to_move(state)
        if len(samples) < 2:
            raise RuntimeError('轨迹至少需要两个反馈点')
        points = []
        previous_ms = None
        for sample in samples:
            values = sample.get('joints_deg')
            stamp = sample.get('timestamp_ms')
            if (not isinstance(values, (list, tuple)) or len(values) != 6
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                           for v in values)
                    or isinstance(stamp, bool) or not isinstance(stamp, int)):
                raise RuntimeError('轨迹包含无效反馈，拒绝回放')
            if previous_ms is not None and not 0 < stamp-previous_ms <= 1000:
                raise RuntimeError('轨迹反馈存在倒序或超过 1 秒的间断，请重新录制')
            raw = tuple(round(v * 1000) for v in values)
            if raw != driver._bounded_target(raw):
                raise RuntimeError('轨迹中有点超出 SDK 或部署限位，拒绝回放')
            points.append(tuple(v / 1000 for v in raw))
            previous_ms = stamp
        if any(abs(a-b) > .5 for a, b in zip(state.joints_deg, points[0])):
            raise RuntimeError('当前位置与录制起点相差超过 0.5°；不会自动移动到起点')
        duration = sum(max(abs(b-a) for a, b in zip(left, right)) / speed_deg_s
                       for left, right in zip(points, points[1:]))
        if duration > 180:
            raise RuntimeError('按当前低速设置预计回放超过 180 秒；请缩短录制轨迹')
        canonical = json.dumps(samples, sort_keys=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        return ({'sample_count': len(points), 'duration_seconds': round(duration, 1),
                 'speed_deg_s': speed_deg_s, 'speed_pct': speed_pct,
                 'start_deg': points[0], 'end_deg': points[-1],
                 'reminder': '实体回放会驱动机械臂；预览不移动，执行前须确认现场空间与控制权。'},
                points, digest)


def build_plugins(config, namespace, ros2):
    return [PiperPlugin(config, namespace, ros2)]
