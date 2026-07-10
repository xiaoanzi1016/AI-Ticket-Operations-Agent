# -*- coding: utf-8 -*-
import sqlite3
con = sqlite3.connect('data/agent_operations.db')
cur = con.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
rows = [r[0] for r in cur.fetchall()]
print('Tables:', rows)
# 检查 FTS5 相关表
fts = [t for t in rows if 'fts' in t.lower() or 'case' in t.lower()]
print('FTS/case tables:', fts)
con.close()
