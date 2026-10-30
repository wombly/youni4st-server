"""PostgreSQL in production; SQLite for local development or a persistent disk."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time


class Store:
    def __init__(self, database_url="", sqlite_path="data/miniapp.sqlite3"):
        self.url = database_url
        self.path = sqlite_path
        if not self.url:
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.execute("CREATE TABLE IF NOT EXISTS tg_users (id TEXT PRIMARY KEY, identity TEXT NOT NULL, data TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS tg_sessions (hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires BIGINT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS tg_leads (id TEXT PRIMARY KEY, user_id TEXT NOT NULL)")

    @contextmanager
    def connection(self):
        if self.url:
            import psycopg
            con = psycopg.connect(self.url, connect_timeout=10)
        else:
            con = sqlite3.connect(self.path, timeout=10)
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def query(self, con, sql, params=()):
        return con.execute(sql.replace("?", "%s") if self.url else sql, params)

    def login(self, identity, token_hash, expires):
        uid = identity["id"]
        with self.connection() as con:
            self.query(con, "INSERT INTO tg_users (id, identity, data) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET identity=excluded.identity", (uid, json.dumps(identity), "{}"))
            self.query(con, "DELETE FROM tg_sessions WHERE expires < ?", (int(time.time()),))
            # Bound session count per user; supports several devices.
            rows = self.query(con, "SELECT hash FROM tg_sessions WHERE user_id=? ORDER BY expires DESC", (uid,)).fetchall()
            for row in rows[9:]:
                self.query(con, "DELETE FROM tg_sessions WHERE hash=?", (row[0],))
            self.query(con, "INSERT INTO tg_sessions VALUES (?, ?, ?)", (token_hash, uid, expires))
        return self.profile(uid)

    def session_user(self, token_hash):
        with self.connection() as con:
            row = self.query(con, "SELECT user_id FROM tg_sessions WHERE hash=? AND expires>?", (token_hash, int(time.time()))).fetchone()
            return row[0] if row else None

    def logout(self, token_hash):
        with self.connection() as con:
            self.query(con, "DELETE FROM tg_sessions WHERE hash=?", (token_hash,))

    def profile(self, uid):
        with self.connection() as con:
            row = self.query(con, "SELECT identity, data FROM tg_users WHERE id=?", (uid,)).fetchone()
            if not row:
                raise KeyError(uid)
            return {"user": json.loads(row[0]), "data": json.loads(row[1])}

    def update(self, uid, changes):
        with self.connection() as con:
            if not self.url:
                con.execute("BEGIN IMMEDIATE")
            suffix = " FOR UPDATE" if self.url else ""
            row = self.query(con, "SELECT data FROM tg_users WHERE id=?" + suffix, (uid,)).fetchone()
            data = json.loads(row[0])
            data.update(changes)
            self.query(con, "UPDATE tg_users SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), uid))
        return self.profile(uid)

    def save_lead(self, uid, lead_id):
        with self.connection() as con:
            self.query(con, "INSERT INTO tg_leads VALUES (?, ?) ON CONFLICT(id) DO NOTHING", (str(lead_id), uid))

    def owns_lead(self, uid, lead_id):
        with self.connection() as con:
            return self.query(con, "SELECT id FROM tg_leads WHERE id=? AND user_id=?", (str(lead_id), uid)).fetchone() is not None
