#!/usr/bin/env python3
"""G1 站岗哨兵 skill 卸载脚本（在机器人上运行）

用法:
    sudo python3 /tmp/uninstall_g1_sentry.py
"""
import sqlite3
import json

slug = "g1-sentry"
db_path = "/opt/phanthy-motus/data/data.db"

conn = sqlite3.connect(db_path)
row = conn.execute("SELECT value FROM config WHERE key='skills'").fetchone()

if not row:
    print(f"ERROR: config key='skills' not found in {db_path}")
    conn.close()
    exit(1)

skills = json.loads(row[0])
before = len(skills.get("installed", []))
skills["installed"] = [s for s in skills["installed"] if s.get("slug") != slug]
after = len(skills["installed"])

if before == after:
    print(f'WARNING: skill "{slug}" 未找到，无需卸载')
else:
    conn.execute("UPDATE config SET value=? WHERE key='skills'", (json.dumps(skills, ensure_ascii=False),))
    conn.commit()
    print(f'OK: skill "{slug}" 已卸载 ({before} → {after})')

conn.close()
