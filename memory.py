import os
import re
import sqlite3
import threading
import time

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS laps(
    id INTEGER PRIMARY KEY, ts REAL, car TEXT, track TEXT, compound TEXT,
    lap INTEGER, lap_time REAL, fuel_used REAL, wear_lost REAL, valid INTEGER);
CREATE TABLE IF NOT EXISTS profiles(
    car TEXT, track TEXT, compound TEXT,
    avg_fuel REAL, avg_wear REAL, fuel_margin REAL, tyre_cliff REAL,
    PRIMARY KEY (car, track, compound));
CREATE TABLE IF NOT EXISTS tyre_wear(
    car TEXT, track TEXT, compound TEXT, fl REAL, fr REAL, rl REAL, rr REAL,
    PRIMARY KEY (car, track, compound));
CREATE TABLE IF NOT EXISTS knowledge(
    question TEXT PRIMARY KEY, answer TEXT, source TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS unanswered(question TEXT, ts REAL, done INTEGER DEFAULT 0);
"""


def _norm(text):
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


class Memory:
    """The engineer's long-term memory (one local file, no internet)."""

    def __init__(self):
        os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
        self.db = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            try:   # upgrade a database created by the previous version
                self.db.execute("ALTER TABLE unanswered ADD COLUMN done INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            self.db.commit()

    def _run(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def add_lap(self, car, track, compound, lap, lap_time, fuel_used, wear_lost, valid):
        self._run("INSERT INTO laps(ts,car,track,compound,lap,lap_time,fuel_used,wear_lost,valid) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (time.time(), car, track, compound, lap, lap_time, fuel_used, wear_lost, int(valid)))

    def get_profile(self, car, track, compound):
        row = self._run("SELECT avg_fuel, avg_wear, fuel_margin, tyre_cliff FROM profiles "
                        "WHERE car=? AND track=? AND compound=?", (car, track, compound)).fetchone()
        if not row:
            return None
        return dict(zip(("avg_fuel", "avg_wear", "fuel_margin", "tyre_cliff"), row))

    def save_profile(self, car, track, compound, avg_fuel, avg_wear, fuel_margin, tyre_cliff):
        self._run("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?,?,?)",
                  (car, track, compound, avg_fuel, avg_wear, fuel_margin, tyre_cliff))

    def get_tyre_wear(self, car, track, compound):
        row = self._run("SELECT fl, fr, rl, rr FROM tyre_wear WHERE car=? AND track=? AND compound=?",
                        (car, track, compound)).fetchone()
        return list(row) if row else None

    def save_tyre_wear(self, car, track, compound, wear4):
        self._run("INSERT OR REPLACE INTO tyre_wear VALUES (?,?,?,?,?,?,?)", (car, track, compound, *wear4))

    def get_knowledge(self, question):
        words = set(_norm(question).split())
        best, best_score = None, 0.0
        for q, answer, source in self._run("SELECT question, answer, source FROM knowledge").fetchall():
            other = set(q.split())
            score = len(words & other) / max(1, len(words | other))
            if score > best_score:
                best, best_score = (answer, source), score
        return best if best_score >= 0.6 else None

    def save_knowledge(self, question, answer, source):
        self._run("INSERT OR REPLACE INTO knowledge VALUES (?,?,?,?)",
                  (_norm(question), answer, source, time.time()))

    def log_unknown(self, question):
        self._run("INSERT INTO unanswered(question, ts) VALUES (?,?)", (question, time.time()))

    def next_unanswered(self):
        row = self._run("SELECT question FROM unanswered WHERE done=0 ORDER BY ts LIMIT 1").fetchone()
        return row[0] if row else None

    def mark_answered(self, question):
        self._run("UPDATE unanswered SET done=1 WHERE question=?", (question,))
