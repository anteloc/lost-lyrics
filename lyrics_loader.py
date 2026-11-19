#!/usr/bin/env python3
"""
lyrics_loader.py - load harvested lyrics .txt files into a SQLite database.

Recursively reads every *.txt under a lyrics directory (as produced by
lyrics_harvester.py: <root>/<genre>/<Artist>/<Title>.txt) and inserts one row
per file into a LYRICS table:

    GENRE, ARTIST, ALBUM, SONG_NAME, FULL_LYRICS   (all TEXT)

Fields are taken from the file's header when present:

    # Ariana Gaga - no applause left to cry
    # Album: a superkill bye for now (swt studio)

with fallbacks to the directory layout (genre = first folder under the root,
artist = second folder) and the file name (song name). FULL_LYRICS stores the
complete file contents, header included.

Quick start
-----------
    uv run lyrics_loader.py output/lyrics                     # -> ./lost-lyrics.db
    uv run lyrics_loader.py output/lyrics --db data/lyrics.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

__version__ = "1.0.0"

DEFAULT_DB = "lost-lyrics.db"


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_header(text: str) -> tuple[str, str, str]:
    """(artist, album, song) from the leading '# …' header lines; '' when absent."""
    artist = album = song = ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#"):
            break
        body = line.lstrip("#").strip()
        low = body.lower()
        if low.startswith("album:"):
            album = body[len("album:"):].strip()
        elif low.startswith("source:"):
            continue
        elif not artist and " - " in body:
            artist, song = (part.strip() for part in body.split(" - ", 1))
    if album == "-":  # harvester writes '-' when the album is unknown
        album = ""
    return artist, album, song


def row_for(path: Path, root: Path, text: str) -> tuple[str, str, str, str, str]:
    parts = path.relative_to(root).parts
    genre = parts[0] if len(parts) > 1 else ""
    dir_artist = parts[1] if len(parts) > 2 else ""
    artist, album, song = parse_header(text)
    return (
        genre,
        artist or dir_artist,
        album,
        song or path.stem,
        text,
    )


def load(lyrics_dir: Path, db_path: Path) -> int:
    files = sorted(p for p in lyrics_dir.rglob("*.txt") if p.is_file())
    if not files:
        log(f"⚠ No .txt files found under {lyrics_dir}")
        return 0

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS LYRICS(
            GENRE TEXT,
            ARTIST TEXT,
            ALBUM TEXT,
            SONG_NAME TEXT,
            FULL_LYRICS TEXT)
        """
    )

    rows, skipped = [], 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log(f"   ⚠ skipping {path}: {e}")
            skipped += 1
            continue
        row = row_for(path, lyrics_dir, text)
        rows.append(row)
        log(f"✅ {row[0]} / {row[1]} – {row[3]}")

    db.executemany("INSERT INTO LYRICS VALUES (?,?,?,?,?)", rows)
    db.commit()
    total = db.execute("SELECT COUNT(*) FROM LYRICS").fetchone()[0]
    db.close()

    log(f"\n🏁 Inserted {len(rows)} lyrics into {db_path.resolve()}   (skipped: {skipped}, table total: {total})")
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
