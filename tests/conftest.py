"""Keep process-wide provider cooldowns isolated between offline tests."""

import pytest


@pytest.fixture(autouse=True)
def isolated_provider_cooldowns(monkeypatch):
    from anghami_session import provider_recovery

    monkeypatch.setattr(provider_recovery, "_deadlines", {})


@pytest.fixture
def initialize_like_history_binding(monkeypatch):
    """Give older __new__ transport doubles a real disposable history index."""
    import hashlib
    import sqlite3
    from anghami_session import vault

    connections = []
    def initialize(selected, row=7):
        monkeypatch.setattr(vault, "_crypt", lambda value, decrypt=False: bytes(value))
        selected._index_key = b"synthetic-like-history-binding-key"
        selected._db = sqlite3.connect(selected.path)
        connections.append(selected._db)
        selected._db.executescript("""
            CREATE TABLE metadata (name TEXT PRIMARY KEY,value BLOB NOT NULL);
            CREATE TABLE accounts (source_row INTEGER PRIMARY KEY,email_key TEXT NOT NULL,
                record BLOB NOT NULL,session BLOB,state TEXT NOT NULL,checked_at_utc TEXT);
            CREATE INDEX account_email ON accounts(email_key);
        """)
        selected._db.executemany("INSERT INTO metadata VALUES (?,?)", {
            "format_version": "1", "index_key": selected._index_key,
            "source_sha256": hashlib.sha256(b"synthetic-binding-source").hexdigest(),
        }.items())
        record = {"source_row": row, "email": "synthetic-binding@example.invalid", "country": "EG"}
        selected._db.execute("INSERT INTO accounts VALUES (?,?,?,NULL,'ready',NULL)", (
            row, vault._email_key(selected._index_key, record["email"]), vault._pack(record)))
        selected._db.commit()
    yield initialize
    for connection in connections:
        connection.close()
