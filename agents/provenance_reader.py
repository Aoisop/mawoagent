"""Incremental, bounded local index of append-only evolution evidence."""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def load_recent_sources(path: Path, limit: int, *, persist: bool = True) -> list[dict]:
    if not path.exists():
        return []
    if not persist:
        groups = defaultdict(lambda: deque(maxlen=limit))
        with path.open() as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                name = row.get("skill") or (row.get("result") or {}).get("skill")
                if name:
                    groups[name].append(row)
        return [row for rows in groups.values() for row in rows]
    database = path.parent / "online-eval" / "provenance.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=10)) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS source (id INTEGER PRIMARY KEY, skill TEXT, payload TEXT)")
        connection.execute("CREATE INDEX IF NOT EXISTS source_skill ON source(skill, id)")
        connection.execute("CREATE TABLE IF NOT EXISTS cursor (key TEXT PRIMARY KEY, value TEXT)")
        # Serialize cursor advancement and inserts across concurrent evaluators.
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute("SELECT value FROM cursor WHERE key='position'").fetchone()
        state = json.loads(previous[0]) if previous else {}
        stat = path.stat()
        offset = state.get("offset", 0)
        if (state.get("inode") != stat.st_ino or stat.st_size < offset
                or (stat.st_size == offset and state.get("mtime") != stat.st_mtime_ns)):
            connection.execute("DELETE FROM source")
            offset = 0
        with path.open("rb") as stream:
            stream.seek(offset)
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                name = row.get("skill") or (row.get("result") or {}).get("skill")
                if name:
                    connection.execute("INSERT INTO source(skill,payload) VALUES(?,?)", (name, json.dumps(row, ensure_ascii=False)))
            offset = stream.tell()
        for (name,) in connection.execute("SELECT DISTINCT skill FROM source").fetchall():
            connection.execute("DELETE FROM source WHERE skill=? AND id NOT IN (SELECT id FROM source WHERE skill=? ORDER BY id DESC LIMIT ?)", (name, name, limit))
        connection.execute("INSERT OR REPLACE INTO cursor VALUES('position',?)", (json.dumps({"inode": stat.st_ino, "offset": offset, "mtime": stat.st_mtime_ns}),))
        return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM source ORDER BY id")]
