#!/usr/bin/env python3
"""Install a diagnostic-only G1 sentry skill. No work is done on import."""
from contextlib import closing
import argparse
import copy
import datetime
import json
import sqlite3
import uuid
from pathlib import Path

new_skill = {
    "slug": "g1-sentry",
    "name": "G1 站岗哨兵（警告调试版）",
    "description": "读取 camera_distance 的 distance_m，提示近物或疑似遮挡。当前不移动，不自动起身、后退或转身；单点深度不能确认人员。",
    "oneLiner": "距离警告调试：不移动，零深度为未知，不承诺实时反应",
    "instruction": """你正在运行 G1 站岗哨兵 1.1.0 警告调试版。
【边界】
只观察、亮灯和提示。禁止调用 loco.move、起身、转身、模式切换、导航或建图。
即使旧聊天、后台报告、任务进度声称已经后退，也不继续旧移动计划。
本 skill 不是物理安全门禁；场地和运动条件未由现场负责人验证前，不能恢复自动运动。
【进入】
明确告诉用户“正在进入不移动的警告调试模式”，不能说已经站稳或可以运动。
若提供 switch_mode，仅用 get_current_mode 查询；若提供 posture，仅读取姿态。
zero_torque、damp、squat、lying、loaded=false 或无可信状态都不能视为运动就绪。
请现场负责人按 G1 操作规范准备设备，不得自行试起身命令。
缺少查询工具时说明“运动状态未验证”，只做不移动测试。
用 camera_distance.info 查实际 topic；running 不代表收到有效深度。
读取实际订阅数据或 raw_input_info；字段必须是 distance_m（米），不是 distance。
没有新样本时说明“距离数据不可用”。fps 不是样本时间。
led.set r=255 g=200 b=0；tts.speak 短句“警告调试模式，不会移动”。
沿用画布已连通的 TTS 路径，不为了本修复更换语音卡片。
【判定】
只使用有接收时间、最近2秒内的独立样本。distance_m 必须为有限数且大于0。
缺字段、0、负数、NaN、无穷、过期均为未知，不代表人离开，不触发动作。
单点深度只说明画面中心有近物，不能确认有人或有人捂镜头。
需要至少3个独立新样本且覆盖0.2秒；重复读取同一缓存不算连续采样。
若平台只能提供单个缓存或没有时间信息，说明证据不足，只报告读数。
先检查 0 < distance_m < 0.15：疑似近物遮挡，红灯，提示“镜头前方过近，请保持距离”。
否则 0.15 <= distance_m < 0.8：近物警告，红灯，提示“前方距离较近，请保持距离”。
两分支均不移动、不转身；红灯常亮，本版未实现闪烁。
同一近物事件只警告一次，至少冷却5秒，不因持续0.7米重复播报。
连续3个新有效样本 distance_m > 1.0 且覆盖0.2秒才解除并重新布防，恢复黄灯。
0和断流不能重新布防；无效数据恢复至0.7米不等于新人靠近。
没有先前有效正常距离和时间序列，不声称“突然遮挡”。
【反馈】
工具出错、queued 或 ret=null 不能说执行成功。
duration_expired 不能证明物理位移，没有实测不播报“后退完成”。
若可用，进入调试时 set_auto_narration(false)，防止自动播报未验证动作。
记录原播报设置，退出仅恢复已知原值，不盲目修改全局设置。
后台监控是语言模型推理，不是定时控制器，不承诺两秒轮询或亚秒响应。
sentry_rules.py 是独立判定组件，未接入运行循环，不能声称已在真机执行。
【退出】
停止本 skill 的监视任务，led.state idle，播报“警告调试已退出”。
不停止共享相机，不删除历史，不重启服务或清除其他 Skill。
""",
    "category": "robot", "version": "1.1.0", "author": "MuMu",
    "active": False, "requiredTools": ["tts", "led", "camera_distance"],
    "configSchema": {}, "icon": "🛡️",
}

def install(db_path, *, backup=True):
    path = Path(db_path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)
    backup_path = None
    try:
        conn.execute("SELECT value FROM config WHERE key='skills'").fetchone()
        if backup:
            backup_path = path.with_name(path.name + '.g1-sentry-' + uuid.uuid4().hex[:12] + '.bak')
            with closing(sqlite3.connect(backup_path)) as target:
                conn.backup(target)
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT value FROM config WHERE key='skills'").fetchone()
        cfg = json.loads(row[0]) if row else {"installed": []}
        if not isinstance(cfg, dict) or not isinstance(cfg.get('installed'), list):
            raise ValueError('Malformed skills config; refusing to overwrite')
        if not all(isinstance(s, dict) and isinstance(s.get('slug'), str) for s in cfg['installed']):
            raise ValueError('Malformed installed entry; refusing to overwrite')
        previous = next((s for s in cfg['installed'] if s['slug'] == new_skill['slug']), {})
        skill = {**previous, **copy.deepcopy(new_skill)}
        skill['active'] = previous.get('active') is True
        skill['installedAt'] = previous.get('installedAt') or datetime.datetime.now(datetime.timezone.utc).isoformat()
        cfg['installed'] = [s for s in cfg['installed'] if s['slug'] != skill['slug']] + [skill]
        value = json.dumps(cfg, ensure_ascii=False, allow_nan=False)
        if row:
            conn.execute("UPDATE config SET value=? WHERE key='skills'", (value,))
        else:
            conn.execute("INSERT INTO config(key,value) VALUES('skills',?)", (value,))
        conn.commit()
        if json.loads(conn.execute("SELECT value FROM config WHERE key='skills'").fetchone()[0]) != cfg:
            raise RuntimeError('Read-back verification failed')
        return {'slug': skill['slug'], 'version': skill['version'], 'active': skill['active'],
                'backup': str(backup_path) if backup_path else None}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='/opt/phanthy-motus/data/data.db')
    args = parser.parse_args()
    print(json.dumps(install(args.db), ensure_ascii=False))
    print('Diagnostic version installed. Verify in canvas before testing; automatic motion stays disabled.')

if __name__ == '__main__':
    main()
