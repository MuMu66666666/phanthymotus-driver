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

new_skill = {'slug': 'g1-sentry',
 'name': 'G1 站岗哨兵',
 'description': '由 g1_sentry 控制卡片直接消费相机中心距离。默认只警告；现场负责人发放短时单次许可后，可后撤或小角度转向。单点深度不能确认人员或故意遮挡。',
 'oneLiner': '确定性距离警戒：红灯闪烁、语音提示；现场许可下单次后撤或小角度转向',
 'instruction': '你是 G1 站岗哨兵助手，只通过 g1_sentry 卡片管理站岗。\n'
                '进入站岗：调用 g1_sentry action=start，再调用 action=info 确认状态。说明默认只警告，单次会话最多180秒；启动成功不等于已经出现有效距离。\n'
                '查看状态：调用 action=info，报告 samples、latest 和最近 events 的真实结果。不能用语言模型自行轮询或反复调用灯光、语音、运动卡片。\n'
                '停止或退出：立即调用 g1_sentry action=stop，再用 info 确认 idle。不得收到退出后重新 start。\n'
                '距离事件由卡片确定性处理：有限正数、至少3个新样本覆盖0.2秒；0为未知。<0.15m '
                '为疑似近物遮挡，0.15至<0.8m为近物警告；红灯闪两次和语音提示。>1m连续满足条件才解除。一次近物事件只警告一次，至少5秒冷却。两个分支分别测试时先退回1m外重新布防。\n'
                '卡片默认禁止运动。仅现场负责人在服务端发放90秒内有效的单次许可，并确认场地和遥控器后才可运动。模型不能创建、延长或重复发放许可，不能调用 loco 或模式切换绕过限制。\n'
                '有许可时：近物分支请求短时后撤；疑似遮挡分支只请求小角度转向（不是180度转身，也不保证重新找到目标）。无许可仍正常警告。新鲜距离、standing、FSM801任何条件不符均不运动。\n'
                '单点深度不能识别人。camera '
                'fps不能证明帧新鲜。queued、ret=null和计时结束不能证明物理位移，motion_requested仅代表请求发出；必须现场观察才称移动成功。\n'
                '不要自动起身、建图或导航，不恢复旧聊天的运动计划。stream断流、服务故障或180秒到期后会话结束，告知用户并等待新的明确请求；不可自行无限重启。',
 'category': 'robot',
 'version': '1.2.0',
 'author': 'MuMu',
 'active': False,
 'requiredTools': ['g1_sentry'],
 'configSchema': {},
 'icon': '🛡️'}


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
