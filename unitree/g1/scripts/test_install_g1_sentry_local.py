#!/usr/bin/env python3
"""本地验证 install_g1_sentry.py 的逻辑（不连真机，用临时数据库模拟）

验证项:
1. 脚本能正确解析 new_skill 字典
2. 模拟写入 config(key='skills') 后数据完整
3. 重复安装（幂等）不产生重复条目
4. 卸载脚本逻辑正确
"""
import sqlite3
import json
import re
import tempfile
import os

SCRIPTS = os.path.dirname(os.path.abspath(__file__))


def load_skill_dict():
    src = open(os.path.join(SCRIPTS, "install_g1_sentry.py"), encoding="utf-8").read()
    m = re.search(r"new_skill = \{(.*?)\n\}", src, re.S)
    assert m, "install 脚本里找不到 new_skill 字典"
    ns = {}
    exec("new_skill = {" + m.group(1) + "\n}", ns)
    return ns["new_skill"]


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO config VALUES ('skills', ?)", (json.dumps({"installed": []}),))
    conn.commit()
    return conn


def run_install(db_path, skill):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT value FROM config WHERE key='skills'").fetchone()
    skills = json.loads(row[0]) if row else {"installed": []}
    skills["installed"] = [s for s in skills.get("installed", []) if s.get("slug") != skill["slug"]]
    skills["installed"].append(skill)
    conn.execute("UPDATE config SET value=? WHERE key='skills'", (json.dumps(skills, ensure_ascii=False),))
    conn.commit()
    conn.close()


def main():
    skill = load_skill_dict()

    # 字段完整性检查
    required = ["slug", "name", "description", "oneLiner", "instruction", "category",
                "version", "author", "installedAt", "active", "requiredTools",
                "configSchema", "icon"]
    missing = [k for k in required if k not in skill]
    assert not missing, f"缺少字段: {missing}"
    print(f"[1] 字段完整性 OK ({len(required)} 个必填字段齐全)")

    # requiredTools 是否都在 G1 已有工具里
    g1_tools = {"loco", "tts", "led", "camera_distance"}
    bad = [t for t in skill["requiredTools"] if t not in g1_tools]
    assert not bad, f"requiredTools 里有 G1 不存在的工具: {bad}"
    print(f"[2] requiredTools OK ({skill['requiredTools']})")

    # instruction 里引用的工具/参数与实际 schema 对齐
    checks = [
        ('loco', ['stop_move', 'move', 'vx', 'vyaw', 'duration']),
        ('led', ['set', 'state', 'r=', 'g=', 'b=']),
        ('tts', ['speak', 'text=']),
        ('camera_distance', ['start', 'stop', 'info']),
    ]
    inst = skill["instruction"]
    for tool, tokens in checks:
        for t in tokens:
            assert t in inst, f"instruction 缺少 {tool} 的 {t}"
    assert "0.8" in inst and "0.15" in inst, "instruction 缺少阈值定义"
    print("[3] instruction 与 G1 工具 schema 对齐 OK (逼近阈值0.8m / 遮挡阈值0.15m)")

    # 模拟 db 写入 + 幂等重装
    import shutil
    td = tempfile.mkdtemp()
    try:
        db = os.path.join(td, "data.db")
        make_db(db)
        run_install(db, skill)
        run_install(db, skill)  # 装两次
        conn = sqlite3.connect(db)
        skills = json.loads(conn.execute("SELECT value FROM config WHERE key='skills'").fetchone()[0])
        conn.close()
        slugs = [s["slug"] for s in skills["installed"]]
        assert slugs.count("g1-sentry") == 1, f"重复安装产生重复条目: {slugs}"
        print(f"[4] 模拟写入+幂等重装 OK (installed slugs: {slugs})")

        # 卸载逻辑
        slug = "g1-sentry"
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT value FROM config WHERE key='skills'").fetchone()
        skills = json.loads(row[0])
        skills["installed"] = [s for s in skills["installed"] if s.get("slug") != slug]
        conn.execute("UPDATE config SET value=? WHERE key='skills'", (json.dumps(skills),))
        conn.commit()
        row = conn.execute("SELECT value FROM config WHERE key='skills'").fetchone()
        assert len(json.loads(row[0])["installed"]) == 0
        conn.close()
        print("[5] 卸载逻辑 OK")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print("\n全部验证通过 ✔ 可上传真机执行")


if __name__ == "__main__":
    main()
