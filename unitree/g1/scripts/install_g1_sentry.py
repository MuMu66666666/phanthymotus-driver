#!/usr/bin/env python3
"""G1 站岗哨兵 skill 安装脚本（在机器人上运行）

用法（机器人上执行）:
    cd /opt/phanthy-motus
    sudo python3 /tmp/install_g1_sentry.py

作用: 向 /opt/phanthy-motus/data/data.db 的 config(key='skills') 字典
     追加 g1-sentry skill，随后需重启容器:
    sudo docker compose restart agent-core
"""
import sqlite3
import json

new_skill = {
    "slug": "g1-sentry",
    "name": "G1 站岗哨兵",
    "description": "G1 站岗哨兵：进入站岗模式后持续监测前方距离。有人逼近（距离小于0.8米）时面灯变红、语音警告并自动后退一步拉开距离；有人遮挡相机（距离突然小于0.15米）时面灯变红、语音警告并原地转身重新观察。用户要求停止时恢复安静站立。",
    "oneLiner": "G1站岗哨兵 - 有人逼近警告后撤，有人捂镜头警告转身",
    "instruction": (
        "你是 G1 站岗哨兵。用户要求你站岗、放哨或进入哨兵模式时，按以下流程操作：\n\n"
        "【进入站岗】\n"
        "1. 停止当前动作：调用工具 `loco` action=\"stop_move\"\n"
        "2. 启动距离监测：调用工具 `camera_distance` action=\"start\"\n"
        "3. 面灯变黄表示警戒中：调用工具 `led` action=\"set\" r=255 g=200 b=0\n"
        "4. 语音确认：调用工具 `tts` action=\"speak\" text=\"哨兵模式已启动，我将保持警戒\"\n"
        "5. 定期（每2秒左右）读取 camera_distance 输出的前方距离值 distance（单位米）\n\n"
        "【触发分支一：有人逼近】\n"
        "当 distance < 0.8 且 >= 0.15，判定有人逼近：\n"
        "a. 面灯变红：调用工具 `led` action=\"set\" r=255 g=0 b=0\n"
        "b. 语音警告：调用工具 `tts` action=\"speak\" text=\"请勿靠近，警戒区域！\"\n"
        "c. 自动后退拉开距离：调用工具 `loco` action=\"move\" vx=-0.2 duration=1.5\n"
        "d. 后退完成后恢复黄色警戒灯：调用工具 `led` action=\"set\" r=255 g=200 b=0\n"
        "e. 若对方继续逼近则重复警告与后撤；若对方离开（distance 恢复大于1.0）则保持静默警戒\n\n"
        "【触发分支二：相机被遮挡】\n"
        "当 distance 突然小于 0.15（正常站岗时不该这么近），判定有人遮挡相机：\n"
        "a. 面灯变红：调用工具 `led` action=\"set\" r=255 g=0 b=0\n"
        "b. 语音警告：调用工具 `tts` action=\"speak\" text=\"请勿遮挡我的镜头\"\n"
        "c. 原地转身重新观察：调用工具 `loco` action=\"move\" vx=0 vy=0 vyaw=1.0 duration=2.0\n"
        "d. 转身完成后恢复黄色警戒灯，继续按距离监测流程执行\n\n"
        "【退出站岗】\n"
        "当用户要求停止站岗、退出哨兵模式时：\n"
        "1. 停止移动：调用工具 `loco` action=\"stop_move\"\n"
        "2. 停止距离监测：调用工具 `camera_distance` action=\"stop\"\n"
        "3. 面灯恢复默认：调用工具 `led` action=\"state\" state=\"idle\"\n"
        "4. 语音确认：调用工具 `tts` action=\"speak\" text=\"哨兵模式已退出\"\n\n"
        "【安全注意事项】\n"
        "- 每次执行新动作前，确保之前的移动已停止（必要时先调 `loco` action=\"stop_move\"）\n"
        "- 后退速度固定 0.2m/s，转身速度固定 1.0rad/s，不得加大\n"
        "- 若 distance 读数长时间无变化或异常（如恒为0），先调用 `camera_distance` action=\"info\" 检查状态，不要盲目执行动作\n"
        "- 若 loco 返回错误或机器人未处于平衡状态，停止所有移动并告知用户"
    ),
    "category": "robot",
    "version": "1.0.0",
    "author": "MuMu",
    "installedAt": "2026-09-23T12:00:00",
    "active": True,
    "requiredTools": ["loco", "tts", "led", "camera_distance"],
    "configSchema": {},
    "icon": "🛡️",
}

db_path = "/opt/phanthy-motus/data/data.db"
conn = sqlite3.connect(db_path)
row = conn.execute("SELECT value FROM config WHERE key='skills'").fetchone()

if row:
    skills = json.loads(row[0])
else:
    skills = {"installed": []}

# 幂等：已存在同名 slug 则替换，不重复追加
skills["installed"] = [s for s in skills.get("installed", []) if s.get("slug") != new_skill["slug"]]
skills["installed"].append(new_skill)

conn.execute("UPDATE config SET value=? WHERE key='skills'", (json.dumps(skills, ensure_ascii=False),))
conn.commit()
conn.close()

print(f'OK: skill "{new_skill["slug"]}" 已安装到 {db_path}')
print('记得重启容器: sudo docker compose restart agent-core')
