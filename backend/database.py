import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "stories.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            aspect_ratio REAL,
            theme TEXT,
            theme_confidence REAL,
            tags TEXT,
            exif_date TEXT,
            source_folder TEXT,
            indexed_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS weekly_selections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start TEXT NOT NULL,
            day_of_week INTEGER NOT NULL,
            photo_id INTEGER NOT NULL REFERENCES photos(id),
            cropped_path TEXT,
            text_overlay TEXT,
            text_style TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(week_start, day_of_week)
        );

        CREATE TABLE IF NOT EXISTS used_photos (
            photo_id INTEGER PRIMARY KEY REFERENCES photos(id),
            last_used TEXT DEFAULT (datetime('now'))
        );
    """)

    # Migration: add source_folder if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE photos ADD COLUMN source_folder TEXT")
        conn.commit()
    except Exception:
        pass  # column already exists

    conn.commit()
    conn.close()
