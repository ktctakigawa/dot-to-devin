"""Durable Slack thread ownership and event deduplication."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path


class Registry:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        # Keep this database in a private, bridge-owned directory.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path)
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS bindings (
                channel TEXT NOT NULL, root TEXT NOT NULL, session TEXT NOT NULL UNIQUE,
                workdir TEXT NOT NULL, PRIMARY KEY (channel, root))""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                channel TEXT NOT NULL, stamp TEXT NOT NULL, PRIMARY KEY (channel, stamp))""")
        return db

    def reserve(self, channel, stamp):
        # Fail closed after a crash: an event already accepted is not replayed.
        with closing(self.connect()) as db, db:
            return db.execute("INSERT OR IGNORE INTO events VALUES (?, ?)",
                              (channel, stamp)).rowcount == 1

    def get(self, channel, root):
        with closing(self.connect()) as db:
            row = db.execute("SELECT session, workdir FROM bindings WHERE channel=? AND root=?",
                             (channel, root)).fetchone()
        return row

    def bind(self, channel, root, session, workdir):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO bindings VALUES (?, ?, ?, ?)",
                       (channel, root, session, str(workdir)))
