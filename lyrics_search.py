#!/usr/bin/env python3
"""
lyrics_search.py - semantic search over the lyrics database via jev-rerank.

Runs the globally installed `jev-rerank` tool (BM25 shortlist + TypeSafe Jev
re-ranking) over LYRICS.FULL_LYRICS, then fetches the matched rows back from
the same database by their LRCLIB primary key and prints LRCLIB / GENRE /
ARTIST / ALBUM / SONG_NAME plus the relevance score in percent. FULL_LYRICS
is included only with --full-lyrics. Results scored below --min-score
(percent units, default 50) are dropped. Output is text by default, or JSON
with --json. Without -V/--verbose only the results are printed; progress and
filtering info appear on stderr when it is set (warnings and errors always
show).

Quick start
-----------
    uv run lyrics_search.py lost-lyrics.db 'a song about eating something sweet'
    uv run lyrics_search.py lost-lyrics.db 'a song about heartbreak' -n 5 --json
    uv run lyrics_search.py lost-lyrics.db 'a song about the rain' --full-lyrics --min-score 40

Needs `jev-rerank` on PATH and, for its default hosted backend, TYPESAFE_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

__version__ = "1.0.0"

FIELDS = ("LRCLIB", "GENRE", "ARTIST", "ALBUM", "SONG_NAME", "FULL_LYRICS")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def rerank(db: Path, query: str, top: int, verbose: bool) -> list[dict]:
    cmd = ["jev-rerank", "--db", str(db), "--table-field", "LYRICS.FULL_LYRICS",
           "--top", str(top), "--query", query, "--show", "--json"]
    if not verbose:
        cmd.append("--quiet")  # hide progress; jev-rerank still reports warnings on stderr
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)  # stderr passes through
    except FileNotFoundError:
        sys.exit("jev-rerank not found on PATH; install it first, e.g.  uv tool install jev-rerank")
    if proc.returncode != 0:
        sys.exit(f"jev-rerank exited with status {proc.returncode} (see its output above).")
    try:
        return json.loads(proc.stdout)["results"]
    except (ValueError, KeyError, TypeError) as e:
        sys.exit(f"Could not parse jev-rerank JSON output: {e}")


def fetch_rows(db: Path, results: list[dict]) -> list[dict]:
    """Re-query LYRICS for each ranked result, best rank first, one row per song."""
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    rows, seen = [], set()
    try:
        for res in results:
            key = (res.get("source") or {}).get("key")
            if not key:
                log(f"   ⚠ rank {res.get('rank')}: result has no database key, skipping")
                continue
            ident = json.dumps(key, sort_keys=True)
            if ident in seen:  # several ranked passages can belong to the same row
                continue
            seen.add(ident)
            where = " AND ".join('"' + col.replace('"', '""') + '" = ?' for col in key)
            row = con.execute(f"SELECT {', '.join(FIELDS)} FROM LYRICS WHERE {where}",
                              list(key.values())).fetchone()
            if row is None:
                log(f"   ⚠ rank {res.get('rank')}: row {key} no longer in LYRICS, skipping")
                continue
            rows.append(dict(zip(FIELDS, row), SCORE=res["score"]))
    finally:
        con.close()
    return rows


def record(row: dict, full_lyrics: bool) -> dict:
    out = {"lrclib": row["LRCLIB"], "genre": row["GENRE"], "artist": row["ARTIST"],
           "album": row["ALBUM"], "song_name": row["SONG_NAME"],
           "score": round(row["SCORE"] * 100, 2)}  # percent, same units as --min-score
    if full_lyrics:
        out["full_lyrics"] = row["FULL_LYRICS"]
    return out


def print_text(rows: list[dict], full_lyrics: bool) -> None:
    for i, row in enumerate(rows, 1):
        print(f"{i}. [{row['GENRE']}] {row['ARTIST']} – {row['SONG_NAME']}")
        print(f"   Album: {row['ALBUM'] or '-'}")
        print(f"   LRCLIB: {row['LRCLIB']}")
        print(f"   Score: {round(row['SCORE'] * 100, 2):g}")
        if full_lyrics:
            print()
            print(row["FULL_LYRICS"].rstrip("\n"))
        print()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="lyrics_search",
        description="Semantic search over the lyrics database (jev-rerank + TypeSafe), printing the matched songs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("db", help="lyrics SQLite database file (as built by lyrics_loader.py)")
    p.add_argument("query", help="what the best-matching songs should be about, in plain text")
    p.add_argument("-n", "--top", type=int, default=10, help="number of results to return")
    p.add_argument("--min-score", type=float, default=50, metavar="PERCENT",
                   help="drop results whose relevance score is below this value in percent (0-100)")
    p.add_argument("--full-lyrics", action="store_true", help="include FULL_LYRICS in the output")
    p.add_argument("--json", action="store_true", help="output JSON instead of text")
    p.add_argument("-V", "--verbose", action="store_true",
                   help="show progress and filtering info on stderr (default: results only)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        p.error(f"database not found: {db}")
    if args.top < 1:
        p.error("--top must be at least 1")
    if not 0 <= args.min_score <= 100:
        p.error("--min-score must be between 0 and 100 (percent units, e.g. 50)")

    results = rerank(db, args.query, args.top, args.verbose)
    threshold = args.min_score / 100  # jev-rerank scores are probabilities in [0, 1]
    kept = [r for r in results if r["score"] >= threshold]
    if args.verbose and len(kept) < len(results):
        log(f"Filtered out {len(results) - len(kept)} of {len(results)} results below min score {args.min_score:g}")
    rows = fetch_rows(db, kept)
    if args.json:
        print(json.dumps([record(r, args.full_lyrics) for r in rows], ensure_ascii=False, indent=2))
    else:
        if args.verbose and not rows:
            log("No results.")
        print_text(rows, args.full_lyrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
