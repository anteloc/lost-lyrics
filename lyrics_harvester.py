#!/usr/bin/env python3
"""
lyrics_harvester.py - download as many lyrics files as possible for a music genre.

How it works
------------
1. DISCOVER songs that belong to the genre, from one of two catalogues:
     * Last.fm   (best quality / popularity-ordered; needs a free API key)
         - tag.getTopTracks           -> the most-tagged tracks for the genre
         - tag.getTopArtists          -> then artist.getTopTracks for each artist
     * MusicBrainz (no key needed; open data, 1 request/second)
         - artist search  tag:"<genre>"  (most relevant artists first, only those whose
           community tags vote for the genre) -> tracks of their official albums/EPs/singles
           (live, demo, interview… releases are skipped; duplicates are merged)
2. FETCH lyrics for each song from LRCLIB (https://lrclib.net), a free and open
   lyrics database (exact lookup first, fuzzy search as fallback).
3. SAVE one file per song:   <out>/<genre>/<Artist>/<Title>.txt   (+ .lrc with --synced)
   plus an index.csv and a SQLite state file so you can stop (Ctrl+C) and resume at any time.

Quick start
-----------
    pip install requests
    python lyrics_harvester.py rock                                  # MusicBrainz, unlimited
    python lyrics_harvester.py rock --lastfm-key YOUR_KEY -n 2000    # Last.fm, stop at 2000 files
    python lyrics_harvester.py "hard rock" --synced --out ./lyrics

Get a free Last.fm API key at https://www.last.fm/api/account/create
(or set the LASTFM_API_KEY environment variable).
A registered LRCLIB app id (higher rate limits) can be given with --lrclib-app-id
or the LRCLIB_APP_ID environment variable.

Lyrics are copyrighted by their authors/publishers. Use the downloaded files for
personal use or research where that is permitted, and respect the services' terms.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import re
import signal
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("This tool needs the 'requests' package:  pip install requests")

__version__ = "1.0.0"
HOMEPAGE = "no contact given"

LRCLIB_URL = "https://lrclib.net/api"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
MB_URL = "https://musicbrainz.org/ws/2"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

class Stop(Exception):
    """Raised to unwind cleanly (Ctrl+C or --max-songs reached)."""


STOP_REQUESTED = False


def _on_sigint(signum, frame):  # first Ctrl+C: finish current song and exit; second: hard exit
    global STOP_REQUESTED
    if STOP_REQUESTED:
        raise KeyboardInterrupt
    STOP_REQUESTED = True
    log("\n⏹  Stopping after the current song (press Ctrl+C again to force)…")


def log(msg: str, *, verbose_only: bool = False) -> None:
    if verbose_only and not ARGS_VERBOSE:
        return
    print(msg, flush=True)


ARGS_VERBOSE = False

# Suffixes that mark alternate versions of the same song.
_VERSION_WORDS = (
    r"live|remaster(?:ed)?|demo|version|mix|remix|edit|mono|stereo|acoustic|"
    r"instrumental|karaoke|take|session|bonus|deluxe|single|radio|explicit|clean|"
    r"rehearsal|outtake|alternate|alt|unplugged|re-?recorded|bbc|feat\.?|ft\.?|with"
)
_PAREN_VERSION = re.compile(r"\s*[\(\[][^\)\]]*\b(?:" + _VERSION_WORDS + r")\b[^\)\]]*[\)\]]", re.I)
_DASH_VERSION = re.compile(r"\s+[-–—]\s+[^-–—]*\b(?:" + _VERSION_WORDS + r")\b.*$", re.I)
_JUNK_TITLE = re.compile(r"\b(karaoke|instrumental|interview|commentary|backing track|8-bit|tribute to)\b", re.I)
_DATE_TITLE = re.compile(r"\b\d{4}\W\d{2}\W\d{2}\b")  # bootleg-style "1976-10-28: Palladium …"
_JUNK_DISAMBIG = re.compile(r"\b(live|demo|instrumental|karaoke|remix|rehearsal|interview|backing track)\b", re.I)


def clean_title(title: str) -> str:
    """'Paint It Black (Remastered 2011)' -> 'Paint It Black'."""
    t = _PAREN_VERSION.sub("", title)
    t = _DASH_VERSION.sub("", t)
    return t.strip() or title.strip()


_PUNCT_MAP = str.maketrans({"’": "'", "‘": "'", "´": "'", "`": "'", "“": '"', "”": '"',
                            "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "…": "...", " ": " "})


def asciify_punct(s: str) -> str:
    """Typographic punctuation -> plain ASCII, so search queries match how most lyrics sites store titles."""
    return s.translate(_PUNCT_MAP)


def norm(s: str) -> str:
    """Aggressive normalisation for de-duplication / matching (keeps non-Latin scripts)."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold().replace("&", " and ")
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r"[\W_]+", " ", s)
    return s.strip()


_BAD_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_name(s: str, maxlen: int = 120) -> str:
    s = _BAD_FS.sub("_", s).strip().rstrip(". ")
    s = re.sub(r"\s+", " ", s)[:maxlen].rstrip(". ") or "_"
    if s.lower() in _WIN_RESERVED:
        s = "_" + s
    return s


_LRC_TS = re.compile(r"^\s*(\[[0-9:.]+\]\s*)+")


def synced_to_plain(synced: str) -> str:
    lines = []
    for line in synced.splitlines():
        if re.match(r"^\s*\[(ar|ti|al|by|length|offset|re|ve|au):", line, re.I):
            continue
        lines.append(_LRC_TS.sub("", line))
    return "\n".join(lines).strip()


@dataclass
class Track:
    artist: str
    title: str
    album: str = ""
    duration: int = 0  # seconds, 0 = unknown
    source: str = ""

    @property
    def query_title(self) -> str:
        return clean_title(self.title)

    @property
    def key(self) -> str:
        return f"{norm(self.artist)}|{norm(self.query_title)}"


# --------------------------------------------------------------------------- #
# HTTP with per-host rate limiting and polite retries
# --------------------------------------------------------------------------- #

class Http:
    def __init__(self, user_agent: str, delays: dict[str, float], extra_headers: Optional[dict] = None,
                 timeout: float = 30.0, max_retries: int = 6):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        if extra_headers:
            self.s.headers.update(extra_headers)
        self.delays = delays
        self.last: dict[str, float] = {}
        self.timeout = timeout
        self.max_retries = max_retries
        self.calls = 0

    def _wait_turn(self, host: str) -> None:
        delay = self.delays.get(host, 0.25)
        elapsed = time.monotonic() - self.last.get(host, 0.0)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self.last[host] = time.monotonic()

    def get_json(self, host: str, url: str, params: dict) -> tuple[int, object]:
        """Returns (status_code, parsed_json_or_None). 404 is returned, not raised."""
        backoff = 2.0
        for attempt in range(1, self.max_retries + 1):
            if STOP_REQUESTED and attempt > 1:
                raise Stop
            self._wait_turn(host)
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
                self.calls += 1
            except requests.RequestException as e:
                log(f"   ⚠ network error on {host} ({e.__class__.__name__}), retry {attempt}/{self.max_retries}")
                time.sleep(backoff + random.random())
                backoff = min(backoff * 2, 120)
                continue

            if r.status_code in (429, 503) or r.status_code >= 500:
                ra = r.headers.get("Retry-After")
                try:
                    wait = float(ra) if ra else backoff
                except ValueError:
                    wait = backoff
                wait *= 1 + random.random() * 0.3  # jitter, as LRCLIB recommends
                log(f"   ⏳ {host} answered {r.status_code}; waiting {wait:.0f}s (retry {attempt}/{self.max_retries})")
                time.sleep(wait)
                backoff = min(backoff * 2, 300)
                continue

            try:
                data = r.json()
            except ValueError:
                data = None
            return r.status_code, data
        return 0, None


# --------------------------------------------------------------------------- #
# Persistent state (resume support)
# --------------------------------------------------------------------------- #

class State:
    FINAL = ("saved", "not_found", "instrumental", "duplicate")

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracks(
                key TEXT PRIMARY KEY, artist TEXT, title TEXT, album TEXT,
                status TEXT, file TEXT, lrclib_id INTEGER, source TEXT,
                updated REAL);
            CREATE INDEX IF NOT EXISTS tracks_lrclib ON tracks(lrclib_id);
            CREATE TABLE IF NOT EXISTS crawled(name TEXT PRIMARY KEY, updated REAL);
            """
        )
        self.db.commit()

    def status(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT status FROM tracks WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def has_lrclib_id(self, lid: int) -> bool:
        return self.db.execute("SELECT 1 FROM tracks WHERE lrclib_id=? AND status='saved'", (lid,)).fetchone() is not None

    def mark(self, t: Track, status: str, file: str = "", lrclib_id: Optional[int] = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO tracks VALUES (?,?,?,?,?,?,?,?,?)",
            (t.key, t.artist, t.query_title, t.album, status, file, lrclib_id, t.source, time.time()),
        )
        self.db.commit()

    def crawled(self, name: str) -> bool:
        return self.db.execute("SELECT 1 FROM crawled WHERE name=?", (name,)).fetchone() is not None

    def mark_crawled(self, name: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO crawled VALUES (?,?)", (name, time.time()))
        self.db.commit()

    def count(self, status: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM tracks WHERE status=?", (status,)).fetchone()[0]

    def export_csv(self, path: Path) -> None:
        rows = self.db.execute(
            "SELECT artist,title,album,file,lrclib_id,source FROM tracks WHERE status='saved' ORDER BY artist,title"
        ).fetchall()
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["artist", "title", "album", "file", "lrclib_id", "source"])
            w.writerows(rows)


# --------------------------------------------------------------------------- #
# Song discovery
# --------------------------------------------------------------------------- #

def _as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


class LastFm:
    def __init__(self, http: Http, api_key: str, per_artist: int):
        self.http, self.key, self.per_artist = http, api_key, per_artist

    def _call(self, method: str, **params) -> dict:
        params.update(method=method, api_key=self.key, format="json")
        code, data = self.http.get_json("lastfm", LASTFM_URL, params)
        if not isinstance(data, dict):
            return {}
        if "error" in data:
            if data.get("error") in (10, 26):  # invalid / suspended key
                sys.exit(f"Last.fm error: {data.get('message')}")
            log(f"   ⚠ Last.fm {method}: {data.get('message')}", verbose_only=True)
            return {}
        return data

    def _paged(self, method: str, root: str, item: str, limit: int, max_items: int = 0, **params) -> Iterator[dict]:
        page, total_pages, n = 1, 1, 0
        while page <= total_pages:
            data = self._call(method, page=page, limit=limit, **params).get(root, {})
            items = _as_list(data.get(item))
            if not items:
                return
            try:
                total_pages = int(data.get("@attr", {}).get("totalPages", 1))
            except (TypeError, ValueError):
                total_pages = page
            for it in items:
                yield it
                n += 1
                if max_items and n >= max_items:
                    return
            page += 1

    def tracks(self, genre: str, state: State) -> Iterator[Track]:
        log(f"🔎 Last.fm: top tracks tagged '{genre}'")
        for t in self._paged("tag.getTopTracks", "tracks", "track", 1000, tag=genre):
            yield Track(t["artist"]["name"], t["name"], duration=int(t.get("duration") or 0), source="lastfm:tag")

        log(f"🔎 Last.fm: top artists tagged '{genre}' → their top tracks")
        for a in self._paged("tag.getTopArtists", "topartists", "artist", 1000, tag=genre):
            name = a["name"]
            ck = f"lastfm:{norm(name)}"
            if state.crawled(ck):
                continue
            log(f"🎸 {name}", verbose_only=True)
            for t in self._paged("artist.getTopTracks", "toptracks", "track", 200,
                                 max_items=self.per_artist, artist=name, autocorrect=1):
                yield Track(t.get("artist", {}).get("name", name), t["name"], source="lastfm:artist")
            state.mark_crawled(ck)


# MusicBrainz "special purpose" artists: Various Artists, [unknown], [anonymous], [traditional], [no artist], …
MB_SPECIAL_ARTISTS = {
    "89ad4ac3-39f7-470e-963a-56509c546377", "125ec42a-7229-4250-afc5-e057484327fe",
    "f731ccc4-e22a-43af-a747-64213329e088", "9be7f096-97ec-4615-8957-8d40b5dcbc41",
    "eec63d3c-3b81-4ad4-b1e4-7c147d4d2b61", "33cf029c-63b0-41a0-9855-be2a3665fb3b",
    "314e1c25-dde7-4e4d-b2f4-0a7b9f7c56dc", "66ea0139-149f-4a0c-8fbf-5ea9ec4a6e49",
}


class MusicBrainz:
    def __init__(self, http: Http, per_artist: int, loose: bool = False):
        self.http, self.per_artist, self.loose = http, per_artist, loose

    def _is_genre_artist(self, a: dict, genre: str) -> bool:
        """Keep artists whose community tags actually vote for the genre (e.g. 'rock' or 'hard rock')."""
        if a.get("id") in MB_SPECIAL_ARTISTS or (a.get("name") or "").startswith("["):
            return False
        if self.loose:
            return True
        g = f" {norm(genre)} "
        return any(t.get("count", 0) > 0 and g in f" {norm(t.get('name', ''))} " for t in a.get("tags", []))

    def _artists(self, genre: str) -> Iterator[dict]:
        offset, total = 0, None
        query = f'tag:"{genre}"'
        while total is None or offset < total:
            code, data = self.http.get_json("musicbrainz", f"{MB_URL}/artist",
                                            {"query": query, "limit": 100, "offset": offset, "fmt": "json"})
            if not isinstance(data, dict) or not data.get("artists"):
                return
            total = int(data.get("count", 0))
            yield from data["artists"]
            offset += len(data["artists"])

    SKIP_SECONDARY = {"live", "interview", "spokenword", "audiobook", "audio drama", "demo",
                      "dj-mix", "remix", "field recording", "mixtape/street"}

    def _recordings(self, artist_id: str) -> Iterator[dict]:
        """Tracks of the artist's *official* albums / EPs / singles (skips live, demo, interview … releases)."""
        offset, total = 0, None
        while total is None or offset < total:
            code, data = self.http.get_json("musicbrainz", f"{MB_URL}/release", {
                "artist": artist_id, "status": "official", "type": "album|ep|single",
                "inc": "recordings+release-groups", "limit": 50, "offset": offset, "fmt": "json"})
            if not isinstance(data, dict) or not data.get("releases"):
                return
            total = int(data.get("release-count", 0))
            for rel in data["releases"]:
                sec = {s.lower() for s in (rel.get("release-group") or {}).get("secondary-types", [])}
                if sec & self.SKIP_SECONDARY:
                    continue
                for medium in rel.get("media", []):
                    for tr in medium.get("tracks", []):
                        rec = dict(tr.get("recording") or {})
                        rec.setdefault("title", tr.get("title"))
                        rec.setdefault("length", tr.get("length"))
                        rec["album"] = rel.get("title", "")
                        yield rec
            offset += len(data["releases"])

    def tracks(self, genre: str, state: State) -> Iterator[Track]:
        log(f"🔎 MusicBrainz: artists tagged '{genre}' → their official albums, EPs and singles")
        for a in self._artists(genre):
            ck = f"mb:{a['id']}"
            if state.crawled(ck) or not self._is_genre_artist(a, genre):
                continue
            log(f"🎸 {a['name']}", verbose_only=True)
            seen: set[str] = set()
            for rec in self._recordings(a["id"]):
                title = rec.get("title") or ""
                if not title or rec.get("video") or _JUNK_TITLE.search(title) or title.startswith("[") \
                        or _DATE_TITLE.search(title):
                    continue
                if _JUNK_DISAMBIG.search(rec.get("disambiguation") or ""):
                    continue
                k = norm(clean_title(title))
                if not k or k in seen:
                    continue
                seen.add(k)
                length = rec.get("length") or 0
                yield Track(a["name"], title, album=rec.get("album", ""),
                            duration=round(length / 1000) if length else 0, source="musicbrainz")
                if self.per_artist and len(seen) >= self.per_artist:
                    break
            state.mark_crawled(ck)


# --------------------------------------------------------------------------- #
# Lyrics lookup (LRCLIB)
# --------------------------------------------------------------------------- #

class Lrclib:
    def __init__(self, http: Http):
        self.http = http

    @staticmethod
    def _artist_ok(found: str, wanted: str) -> bool:
        f, w = norm(found), norm(wanted)
        if not f or not w:
            return False
        # exact, "Artist & The Band" / "Artist feat. X", or "Springsteen Bruce" (same words, other order)
        return f == w or f.startswith(w + " ") or set(f.split()) == set(w.split())

    @classmethod
    def _score(cls, rec: dict, t: Track) -> float:
        if not cls._artist_ok(rec.get("artistName") or "", t.artist):
            return -1
        found_title = rec.get("trackName") or ""
        if norm(clean_title(found_title)) != norm(t.query_title):
            return -1
        if not (rec.get("plainLyrics") or rec.get("syncedLyrics") or rec.get("instrumental")):
            return -1
        score = 10.0
        if rec.get("syncedLyrics"):
            score += 1
        if norm(found_title) == norm(t.title):  # prefer the plain studio title over "(Live)" etc.
            score += 2
        if t.duration and rec.get("duration"):
            score -= min(abs(float(rec["duration"]) - t.duration), 60) / 10
        if rec.get("instrumental"):
            score -= 5
        return score

    def _search(self, params: dict, t: Track) -> Optional[dict]:
        code, data = self.http.get_json("lrclib", f"{LRCLIB_URL}/search", params)
        if code != 200 or not isinstance(data, list):
            return None
        scored = [(self._score(r, t), r) for r in data]
        scored = [x for x in scored if x[0] >= 0]
        return max(scored, key=lambda x: x[0])[1] if scored else None

    def find(self, t: Track) -> Optional[dict]:
        title, artist = asciify_punct(t.query_title), asciify_punct(t.artist)
        # 1) structured search (title + artist)
        rec = self._search({"track_name": title, "artist_name": artist}, t)
        if rec:
            return rec
        # 2) fuzzy keyword search over title/artist/album (catches "feat." credits, odd spellings…)
        return self._search({"q": f"{artist} {title}"}, t)


# --------------------------------------------------------------------------- #
# Main harvest loop
# --------------------------------------------------------------------------- #

def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 2
    while True:
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
        i += 1


def harvest(args) -> int:
    genre_dir = Path(args.out) / safe_name(args.genre.lower())
    genre_dir.mkdir(parents=True, exist_ok=True)
    state = State(genre_dir / ".harvest_state.sqlite")

    contact = args.contact or HOMEPAGE
    if not args.contact:
        log("ℹ️  Tip: pass --contact you@example.com – MusicBrainz and LRCLIB ask clients to identify themselves.")
    ua = f"lyrics-harvester/{__version__} ({contact})"
    extra = {}
    app_id = args.lrclib_app_id or os.environ.get("LRCLIB_APP_ID", "")
    if app_id:
        extra["Lrclib-App-Id"] = app_id
    http = Http(ua, {"lrclib": args.lrclib_delay, "musicbrainz": 1.05, "lastfm": 0.25}, extra)
    lyrics = Lrclib(http)

    key = args.lastfm_key or os.environ.get("LASTFM_API_KEY", "")
    source = args.source if args.source != "auto" else ("lastfm" if key else "musicbrainz")
    if source == "lastfm" and not key:
        sys.exit("--source lastfm needs --lastfm-key or the LASTFM_API_KEY environment variable.")
    if source == "lastfm":
        disco = LastFm(http, key, args.per_artist if args.per_artist else 200)
    else:
        disco = MusicBrainz(http, args.per_artist, args.loose)

    already = state.count("saved")
    log(f"📁 Output: {genre_dir.resolve()}   (already saved: {already})")
    log(f"🌐 Discovery: {source}   Lyrics: LRCLIB   Target: {args.max_songs or 'as many as possible'}\n")

    saved_now = missing = skipped = 0
    seen_run: set[str] = set()
    t0 = time.monotonic()

    def target_reached() -> bool:
        return bool(args.max_songs) and (already + saved_now) >= args.max_songs

    try:
        if target_reached():
            raise Stop
        for t in disco.tracks(args.genre, state):
            if STOP_REQUESTED:
                raise Stop
            if not t.artist or not t.title or not norm(t.query_title):
                continue
            k = t.key
            if k in seen_run:
                continue
            seen_run.add(k)

            st = state.status(k)
            if st == "saved" or (st in State.FINAL and not args.retry_missing):
                skipped += 1
                continue

            rec = lyrics.find(t)
            if rec is None:
                state.mark(t, "not_found")
                missing += 1
                log(f"   · no lyrics: {t.artist} – {t.query_title}", verbose_only=True)
                continue
            if rec.get("instrumental"):
                state.mark(t, "instrumental", lrclib_id=rec.get("id"))
                continue
            lid = rec.get("id")
            if lid is not None and state.has_lrclib_id(lid):
                state.mark(t, "duplicate", lrclib_id=lid)  # same lyrics already saved under another title
                skipped += 1
                continue

            plain = (rec.get("plainLyrics") or "").strip()
            synced = (rec.get("syncedLyrics") or "").strip()
            if not plain and synced:
                plain = synced_to_plain(synced)
            if not plain:
                state.mark(t, "not_found", lrclib_id=lid)
                missing += 1
                continue

            artist_name = t.artist  # catalogue spelling keeps one folder per artist
            adir = genre_dir / safe_name(artist_name)
            adir.mkdir(exist_ok=True)
            path = unique_path(adir / f"{safe_name(t.query_title)}.txt")
            body = plain + "\n"
            if args.header:
                body = (f"# {artist_name} - {rec.get('trackName') or t.query_title}\n"
                        f"# Album: {rec.get('albumName') or '-'}\n# Source: LRCLIB #{lid}\n\n") + body
            path.write_text(body, encoding="utf-8")
            if args.synced and synced:
                path.with_suffix(".lrc").write_text(synced + "\n", encoding="utf-8")

            state.mark(t, "saved", str(path.relative_to(genre_dir)), lid)
            saved_now += 1
            total = already + saved_now
            rate = saved_now / max(time.monotonic() - t0, 1) * 3600
            log(f"✅ [{total:>6}] {artist_name} – {t.query_title}   ({rate:.0f}/h, misses {missing})")

            if target_reached():
                raise Stop
        log("\n🏁 Discovery exhausted – no more songs to try for this genre/source.")
    except Stop:
        pass
    except KeyboardInterrupt:
        log("\nForced stop.")
    finally:
        state.export_csv(genre_dir / "index.csv")
        mins = (time.monotonic() - t0) / 60
        log(f"\nSaved this run: {saved_now}   no lyrics: {missing}   skipped (known/dupes): {skipped}")
        log(f"Total lyrics files for '{args.genre}': {state.count('saved')}   ({mins:.1f} min, {http.calls} HTTP calls)")
        log(f"Index: {genre_dir / 'index.csv'}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="lyrics_harvester",
        description="Download as many lyrics files as possible for a music genre (Last.fm/MusicBrainz + LRCLIB).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("genre", help='genre / tag, e.g. rock, "hard rock", metal, flamenco')
    p.add_argument("-o", "--out", default="lyrics", help="output root folder")
    p.add_argument("-n", "--max-songs", type=int, default=0, help="stop after this many saved files in total (0 = no limit)")
    p.add_argument("-s", "--source", choices=["auto", "lastfm", "musicbrainz"], default="auto",
                   help="where to discover songs (auto = Last.fm if a key is available, else MusicBrainz)")
    p.add_argument("--lastfm-key", help="Last.fm API key (or env LASTFM_API_KEY)")
    p.add_argument("--per-artist", type=int, default=0,
                   help="max songs to try per artist (0 = all for MusicBrainz, 200 for Last.fm)")
    p.add_argument("--loose", action="store_true",
                   help="MusicBrainz: accept every artist the tag search returns, even without positive genre votes")
    p.add_argument("--synced", action="store_true", help="also save time-synced .lrc files when available")
    p.add_argument("--header", action="store_true", help="put artist/album/source lines at the top of each .txt")
    p.add_argument("--retry-missing", action="store_true", help="retry songs that had no lyrics in earlier runs")
    p.add_argument("--contact", help="your email or homepage, sent in the User-Agent (recommended by the APIs)")
    p.add_argument("--lrclib-delay", type=float, default=0.7,
                   help="seconds between LRCLIB requests (~90/min allowed for identified clients)")
    p.add_argument("--lrclib-app-id",
                   help="registered LRCLIB app id, allows higher rate limits (or env LRCLIB_APP_ID)")
    p.add_argument("-v", "--verbose", action="store_true", help="show artists being crawled and misses")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)

    global ARGS_VERBOSE
    ARGS_VERBOSE = args.verbose
    signal.signal(signal.SIGINT, _on_sigint)
    return harvest(args)


if __name__ == "__main__":
    sys.exit(main())
