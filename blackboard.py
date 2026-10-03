"""Blackboard: shared knowledge store with SQLite + FTS5."""
import sqlite3
import threading
import time


class Blackboard:
    """Shared knowledge store with full-text search and recency weighting."""
    
    def __init__(self, path, bb_cfg=None):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        bb_cfg = bb_cfg or {}
        self.retention_days = float(bb_cfg.get("retention_days", 0) or 0)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries(
              id INTEGER PRIMARY KEY, kind TEXT, member TEXT,
              problem TEXT, content TEXT, ts REAL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts
              USING fts5(content, content=entries, content_rowid=id);
            CREATE TRIGGER IF NOT EXISTS bb_ai AFTER INSERT ON entries BEGIN
              INSERT INTO entries_fts(rowid, content) VALUES (new.id, new.content);
            END;
            """
        )
        self.db.commit()
    
    def put(self, kind, member, problem, content):
        with self.lock:
            self.db.execute(
                "INSERT INTO entries(kind, member, problem, content, ts) VALUES (?,?,?,?,?)",
                (kind, member, problem, content, time.time()),
            )
            self.db.commit()
    
    def recall(self, query, limit=6):
        """Full-text search with BM25 relevance + recency weighting."""
        try:
            words = " ".join(f'"{w}"' for w in query.split() if len(w) > 3)[:200] or query
            now = time.time()
            rows = self.db.execute(
                """SELECT e.kind, e.member, e.content, e.ts,
                          bm25(entries_fts) as relevance,
                          CASE 
                            WHEN e.ts > ? - 3600 THEN 1.0
                            WHEN e.ts > ? - 86400 THEN 0.5
                            ELSE 0.1
                          END as recency
                   FROM entries e 
                   JOIN entries_fts f ON f.rowid = e.id 
                   WHERE entries_fts MATCH ? 
                   ORDER BY (-relevance * 0.7 + recency * 0.3) DESC 
                   LIMIT ?""",
                (now, now, words, limit),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        return [
            f"[{k} by {m}] {c[:1500]}" for k, m, c, ts, rel, rec in rows
        ]
    
    def prune(self):
        """Delete entries older than retention_days. No-op when retention is 0."""
        if self.retention_days <= 0:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        with self.lock:
            n = self.db.execute(
                "SELECT count(*) FROM entries WHERE ts < ?", (cutoff,)
            ).fetchone()[0]
            self.db.execute("DELETE FROM entries WHERE ts < ?", (cutoff,))
            self.db.execute(
                "INSERT INTO entries_fts(entries_fts) VALUES('rebuild')")
            self.db.commit()
        return n
    
    def delete_by_problem(self, problem):
        """Delete all entries for one problem tag. Horde uses a per-job tag so
        each job flushes only its own entries (concurrent jobs are safe)."""
        with self.lock:
            n = self.db.execute(
                "SELECT count(*) FROM entries WHERE problem = ?", (problem,)
            ).fetchone()[0]
            self.db.execute("DELETE FROM entries WHERE problem = ?", (problem,))
            if n:
                self.db.execute(
                    "INSERT INTO entries_fts(entries_fts) VALUES('rebuild')")
            self.db.commit()
        return n

    def tail(self, n=20):
        rows = self.db.execute(
            "SELECT kind, member, substr(content,1,200), datetime(ts,'unixepoch','localtime') "
            "FROM entries ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return rows
