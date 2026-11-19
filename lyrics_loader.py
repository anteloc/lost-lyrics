#!/usr/bin/env python3
"""
lyrics_loader.py - load harvested lyrics .txt files into a SQLite database.

Recursively reads every *.txt under a lyrics directory (as produced by
lyrics_harvester.py: <root>/<genre>/<Artist>/<Title>.txt) and inserts one row
per file into a LYRICS table:

    LRCLIB (INTEGER PRIMARY KEY), GENRE, ARTIST, ALBUM, SONG_NAME, FULL_LYRICS

LRCLIB is the song's LRCLIB id, taken from the file's header:

    # Ariana Gaga - no applause left to cry
    # Album: a superkill bye for now (swt studio)
    # Source: LRCLIB #7887516

Files without that id line are skipped. The other fields come from the header
when present, with fallbacks to the directory layout (genre = first folder
under the root, artist = second folder) and the file name (song name).
FULL_LYRICS stores the complete file contents, header included.

Rows are upserted by LRCLIB id, so re-running refreshes instead of duplicating.
An existing database with the old schema (no LRCLIB column) is migrated in
place, backfilling ids from the stored lyrics headers.

Quick start
-----------
    uv run lyrics_loader.py output/lyrics                     # -> ./lost-lyrics.db
    uv run lyrics_loader.py output/lyrics --db data/lyrics.db
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

__version__ = "1.0.0"

DEFAULT_DB = "lost-lyrics.db"

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS LYRICS(
    LRCLIB INTEGER PRIMARY KEY,
    GENRE TEXT,
    ARTIST TEXT,
    ALBUM TEXT,
    SONG_NAME TEXT,
    FULL_LYRICS TEXT)
"""
INSERT_ROW = ("INSERT OR REPLACE INTO LYRICS (LRCLIB, GENRE, ARTIST, ALBUM, SONG_NAME, FULL_LYRICS) "
              "VALUES (?,?,?,?,?,?)")

_LRCLIB_ID = re.compile(r"lrclib\s*#\s*(\d+)", re.I)


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_header(text: str) -> tuple[str, str, str, int | None]:
    """(artist, album, song, lrclib_id) from the leading '# …' header lines; ''/None when absent."""
    artist = album = song = ""
    lrclib = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#"):
            break
        body = line.lstrip("#").strip()
        low = body.lower()
        if low.startswith("album:"):
            album = body[len("album:"):].strip()
        elif low.startswith("source:"):
            m = _LRCLIB_ID.search(body)
            if m:
                lrclib = int(m.group(1))
        elif not artist and " - " in body:
            artist, song = (part.strip() for part in body.split(" - ", 1))
    if album == "-":  # harvester writes '-' when the album is unknown
        album = ""
    return artist, album, song, lrclib


def row_for(path: Path, root: Path, text: str) -> tuple[int | None, str, str, str, str, str]:
    parts = path.relative_to(root).parts
    genre = parts[0] if len(parts) > 1 else ""
    dir_artist = parts[1] if len(parts) > 2 else ""
    artist, album, song, lrclib = parse_header(text)
    return (
        lrclib,
        genre,
        artist or dir_artist,
        album,
        song or path.stem,
        text,
    )


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create LYRICS, or migrate an old table (no LRCLIB column) to the LRCLIB-primary-key schema."""
    if not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='LYRICS'").fetchone():
        db.execute(CREATE_TABLE)
        db.commit()
        return
    if any(col[1] == "LRCLIB" for col in db.execute("PRAGMA table_info(LYRICS)")):
        return
    old = db.execute("SELECT GENRE, ARTIST, ALBUM, SONG_NAME, FULL_LYRICS FROM LYRICS").fetchall()
    with db:  # one transaction: either the whole rebuild lands, or nothing changes
        db.execute("ALTER TABLE LYRICS RENAME TO LYRICS_old")
        db.execute(CREATE_TABLE)
        kept = 0
        for genre, artist, album, song, text in old:
            lrclib = parse_header(text)[3]
            if lrclib is None:
                continue
            db.execute(INSERT_ROW, (lrclib, genre, artist, album, song, text))
            kept += 1
        db.execute("DROP TABLE LYRICS_old")
    log(f"🔧 Migrated LYRICS to the LRCLIB primary key   ({kept}/{len(old)} rows kept)")


def load(lyrics_dir: Path, db_path: Path) -> int:
    files = sorted(p for p in lyrics_dir.rglob("*.txt") if p.is_file())
    if not files:
        log(f"⚠ No .txt files found under {lyrics_dir}")
        return 0

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    ensure_schema(db)

    rows, skipped = [], 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log(f"   ⚠ skipping {path}: {e}")
            skipped += 1
            continue
        row = row_for(path, lyrics_dir, text)
        lrclib, genre, artist, album, song, _ = row
        if lrclib is None:
            log(f"   ⚠ skipping {path}: no '# Source: LRCLIB #<id>' header")
            skipped += 1
            continue
        rows.append(row)
        log(f"✅ {genre} / {artist} – {song}   (LRCLIB #{lrclib})")

    db.executemany(INSERT_ROW, rows)
    db.commit()
    total = db.execute("SELECT COUNT(*) FROM LYRICS").fetchone()[0]
    db.close()

    log(f"\n🏁 Loaded {len(rows)} lyrics into {db_path.resolve()}   (skipped: {skipped}, table total: {total})")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="lyrics_loader",
        description="Load harvested lyrics .txt files into a SQLite database (LYRICS table).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("lyrics_dir", help="root folder to scan recursively for .txt lyrics files")
    p.add_argument("-d", "--db", default=DEFAULT_DB, metavar="PATH",
                   help="SQLite database file (created, with parent folders, if missing)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)

    lyrics_dir = Path(args.lyrics_dir)
    if not lyrics_dir.is_dir():
        p.error(f"not a directory: {lyrics_dir}")
    return load(lyrics_dir, Path(args.db))


if __name__ == "__main__":
    sys.exit(main())
