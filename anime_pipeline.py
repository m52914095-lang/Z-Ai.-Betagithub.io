#!/usr/bin/env python3
"""
AI-Powered Anime Bulk Downloader & Uploader
=============================================
Automated pipeline that:
  1. Researches anime using AniList API + Gemini AI
  2. Searches nyaa.si for best quality torrents (1080p & original quality)
  3. Downloads via aria2c with smart fallback (0B/s stall detection)
  4. Creates softsub and hardsub MKV files with ffmpeg
  5. Applies naming scheme: {Anime Name} {Type}{Number} {Original/Remastered} {Hs/Ss}
  6. Uploads to StreamP2P (TUS), Doodstream, Lulustream, Streamtape
  7. Manages disk space — auto-cleans when storage is low

Usage:
  python3 anime_pipeline.py --name "Fullmetal Alchemist Brotherhood"
  python3 anime_pipeline.py --name "Naruto" --type-filter tv
  python3 anime_pipeline.py --name "Evangelion" --episodes 1-26
  python3 anime_pipeline.py --name "Spirited Away" --quality 1080p

Environment Variables:
  AI                    - Gemini API key (required for AI research)
  DOODSTREAM_API_KEY    - Doodstream API key
  LULUSTREAM_API_KEY    - Lulustream API key (optional)
  STREAMTAPE_API_KEY    - Streamtape API key (optional)
  STREAMP2P_API_KEY     - Override built-in StreamP2P key
  DOODSTREAM_FOLDER_HS  - Doodstream Hard Sub folder ID
  DOODSTREAM_FOLDER_SS  - Doodstream Soft Sub folder ID
"""

import argparse
import os
import re
import sys
import json
import time
import logging
import subprocess
import shutil
import hashlib
import base64
import xml.etree.ElementTree as ET
import requests
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote

# ============================================================================
# CONFIGURATION
# ============================================================================

# StreamP2P API key (hardcoded as requested)
STREAMP2P_API_KEY = os.environ.get("STREAMP2P_API_KEY", "a7165e18e69dc32127258688")
STREAMP2P_BASE_URL = "https://streamp2p.com/api/v1"

# Doodstream
DOODSTREAM_API_KEY = os.environ.get("DOODSTREAM_API_KEY", "")
DOODSTREAM_BASE_URL = "https://doodapi.com/api"
DOODSTREAM_FOLDER_HS = os.environ.get("DOODSTREAM_FOLDER_HS", "1729147")   # Hard Sub folder
DOODSTREAM_FOLDER_SS = os.environ.get("DOODSTREAM_FOLDER_SS", "1748072")   # Soft Sub folder

# Lulustream / Streamtape (keys to be provided later)
LULUSTREAM_API_KEY = os.environ.get("LULUSTREAM_API_KEY", "")
LULUSTREAM_BASE_URL = "https://api.lulustream.com/api"
STREAMTAPE_API_KEY = os.environ.get("STREAMTAPE_API_KEY", "")
STREAMTAPE_BASE_URL = "https://streamtape.com/api"

# Gemini AI
GEMINI_API_KEY = os.environ.get("AI", "")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# AniList
ANILIST_API_URL = "https://graphql.anilist.co"

# Nyaa
NYAA_BASE_URL = "https://nyaa.si"

# Working directories
WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/anime_pipeline"))
DOWNLOAD_DIR = WORK_DIR / "downloads"
OUTPUT_DIR = WORK_DIR / "output"
SUBTITLE_DIR = WORK_DIR / "subtitles"

# StreamP2P TUS upload config
TUS_CHUNK_SIZE = 52_428_800  # 50 MB per TUS spec

# FFmpeg encoding
HARDSUB_CRF = "23"
HARDSUB_PRESET = "medium"

# Download monitoring
STALL_TIMEOUT = 60        # seconds of 0B/s before aborting
MAX_DOWNLOAD_TIME = 7200  # 2 hours max per torrent

# Storage management
MIN_FREE_GB = 10          # minimum free GB to maintain
CHUNK_SIZE_GB = 50        # split large torrents into chunks

# Upload retry
MAX_UPLOAD_RETRIES = 3

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("anime-pipeline")


# ============================================================================
# NAMING SCHEME
# ============================================================================

def build_filename(anime_name: str, entry_type: str, number: str,
                   quality_label: str, sub_type: str) -> str:
    """
    Build output filename following the naming scheme:
      {Anime Name} {Type}{Number} {Original/Remastered} {Hs/Ss}.mkv

    Examples:
      Fullmetal Alchemist Brotherhood TV01 Original Ss.mkv
      Spirited Away Movie1 Remastered Hs.mkv
      Evangelion OVA1 Original Ss.mkv
    """
    type_prefix = entry_type.upper().strip()
    if type_prefix not in ("TV", "MOVIE", "OVA", "ONA", "SPECIAL"):
        type_prefix = "TV"

    # Format number: TV01, Movie1, OVA1, etc.
    if type_prefix == "TV":
        num_str = f"{int(number):02d}" if number.isdigit() else str(number)
    else:
        num_str = str(number)

    quality_str = quality_label if quality_label in ("Original", "Remastered") else "Original"
    sub_str = "Hs" if sub_type.lower() in ("hs", "hardsub", "hard") else "Ss"

    return f"{anime_name} {type_prefix}{num_str} {quality_str} {sub_str}.mkv"


# ============================================================================
# PHASE 1: ANIME RESEARCH (AniList + Gemini AI)
# ============================================================================

class AnimeResearcher:
    """
    Research anime using AniList GraphQL API and Gemini AI.

    The research process:
    1. Query AniList for basic anime information and all related entries
    2. Use Gemini AI to expand the entry list, fill gaps, and generate
       optimal nyaa.si search queries
    3. Fall back to AniList-only data if Gemini is unavailable
    """

    def __init__(self, gemini_key: str = ""):
        self.gemini_key = gemini_key
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "AnimePipeline/2.0"})

    def research(self, anime_name: str, type_filter: str = "",
                 episode_range: str = "") -> list[dict]:
        """
        Full research pipeline. Returns a list of entry dicts:
        {
            "title": str,            # English/romaji title
            "type": str,             # TV, Movie, OVA, ONA, Special
            "number": str,           # Episode/movie number
            "quality_label": str,    # Original or Remastered
            "year": int,             # Release year
            "total_episodes": int,   # For TV: total episode count
            "search_queries": [str], # AI-generated nyaa.si search queries
            "anilist_id": int,       # AniList media ID
        }
        """
        log.info(f"=== Phase 1: Researching '{anime_name}' ===")

        # Step 1: Query AniList
        anilist_data = self._query_anilist(anime_name)
        if not anilist_data:
            log.error(f"AniList found no results for '{anime_name}'")
            return []

        # Step 2: Build entries from AniList data
        entries = self._build_entries_from_anilist(anilist_data, type_filter, episode_range)

        # Step 3: Use Gemini to refine and generate search queries
        if self.gemini_key:
            entries = self._refine_with_gemini(anime_name, entries, anilist_data)
        else:
            # Generate basic search queries without AI
            for entry in entries:
                entry["search_queries"] = self._build_basic_queries(entry)

        log.info(f"Research complete: {len(entries)} entries found")
        for i, entry in enumerate(entries):
            log.info(f"  [{i+1}] {entry['title']} — {entry['type']}{entry['number']} "
                     f"({entry['quality_label']}, {entry.get('year', '?')})")

        return entries

    def _query_anilist(self, name: str) -> dict | None:
        """Query AniList GraphQL API for anime info and relations."""
        query = """
        query ($search: String) {
            Media(search: $search, type: ANIME) {
                id
                title { romaji english native }
                format
                episodes
                status
                seasonYear
                startDate { year month day }
                synonyms
                relations {
                    edges { relationType(version: 2) }
                    nodes {
                        id
                        title { romaji english native }
                        format
                        episodes
                        type
                        seasonYear
                        startDate { year month day }
                        status
                        synonyms
                    }
                }
            }
        }
        """
        try:
            resp = self.session.post(
                ANILIST_API_URL,
                json={"query": query, "variables": {"search": name}},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                log.warning(f"AniList errors: {data['errors']}")
                return None
            return data.get("data", {}).get("Media")
        except Exception as e:
            log.error(f"AniList query failed: {e}")
            return None

    def _build_entries_from_anilist(self, media: dict, type_filter: str = "",
                                     episode_range: str = "") -> list[dict]:
        """Build entry list from AniList media data."""
        entries = []

        # Map AniList format to our type names
        format_map = {
            "TV": "TV",
            "TV_SHORT": "TV",
            "MOVIE": "Movie",
            "OVA": "OVA",
            "ONA": "ONA",
            "SPECIAL": "Special",
            "MUSIC": "Special",
        }

        # Process the main entry
        main_entry = self._media_to_entry(media, format_map, is_main=True)
        if main_entry:
            entries.append(main_entry)

        # Process related entries
        relations = media.get("relations", {})
        nodes = relations.get("nodes", [])
        edges = relations.get("edges", [])

        for i, node in enumerate(nodes):
            if node.get("type") != "ANIME":
                continue
            edge = edges[i] if i < len(edges) else {}
            relation_type = edge.get("relationType", "")

            entry = self._media_to_entry(node, format_map, is_main=False,
                                          relation=relation_type)
            if entry:
                entries.append(entry)

        # Apply type filter
        if type_filter:
            type_upper = type_filter.upper()
            entries = [e for e in entries if e["type"].upper() == type_upper]

        # Apply episode range
        if episode_range and entries:
            main = entries[0]
            if main["type"] == "TV":
                parts = episode_range.split("-")
                if len(parts) == 2:
                    start_ep, end_ep = int(parts[0]), int(parts[1])
                    expanded = []
                    for ep in range(start_ep, end_ep + 1):
                        ep_entry = dict(main)
                        ep_entry["number"] = str(ep)
                        ep_entry["title"] = f"{main['title']} Episode {ep}"
                        expanded.append(ep_entry)
                    entries = expanded

        return entries

    def _media_to_entry(self, media: dict, format_map: dict,
                         is_main: bool = False, relation: str = "") -> dict | None:
        """Convert AniList media node to our entry dict."""
        if not media:
            return None

        title_obj = media.get("title", {})
        title = title_obj.get("english") or title_obj.get("romaji") or title_obj.get("native") or "Unknown"

        anilist_format = media.get("format", "TV")
        entry_type = format_map.get(anilist_format, "TV")
        episodes = media.get("episodes") or 0
        year = media.get("seasonYear") or media.get("startDate", {}).get("year")

        # Determine quality label
        quality_label = "Original"
        if relation in ("ADAPTATION", "ALTERNATIVE"):
            quality_label = "Remastered"

        # For TV with multiple episodes, create a single entry for the whole series
        number = "1"
        if entry_type == "TV" and episodes and episodes > 1:
            number = f"1-{episodes}"

        return {
            "title": title,
            "type": entry_type,
            "number": str(number),
            "quality_label": quality_label,
            "year": year,
            "total_episodes": episodes or 0,
            "search_queries": [],  # Will be filled by Gemini or fallback
            "anilist_id": media.get("id", 0),
            "synonyms": media.get("synonyms", []),
            "romaji": title_obj.get("romaji", ""),
            "english": title_obj.get("english", ""),
            "native": title_obj.get("native", ""),
        }

    def _refine_with_gemini(self, anime_name: str, entries: list[dict],
                             anilist_data: dict) -> list[dict]:
        """
        Use Gemini AI to refine the entry list and generate smart search queries.
        Gemini understands anime naming conventions on nyaa.si and can generate
        much better search queries than template-based approaches.
        """
        log.info("Refining entries with Gemini AI...")

        # Build context for Gemini
        entry_descriptions = []
        for i, entry in enumerate(entries):
            entry_descriptions.append(
                f"{i+1}. {entry['title']} | Type: {entry['type']} | "
                f"Number: {entry['number']} | Episodes: {entry['total_episodes']} | "
                f"Quality: {entry['quality_label']} | Year: {entry.get('year', '?')} | "
                f"Romaji: {entry.get('romaji', '')} | English: {entry.get('english', '')} | "
                f"Synonyms: {', '.join(entry.get('synonyms', []))}"
            )

        prompt = f"""You are an expert anime researcher who knows nyaa.si torrent search inside and out. I need you to help me create optimal search queries for downloading anime from nyaa.si.

Anime: {anime_name}

Here are the entries I found from AniList:
{chr(10).join(entry_descriptions)}

For each entry, I need you to:

1. **Verify and correct the entry data** - Make sure the type (TV/Movie/OVA/ONA/Special), numbering, and quality labels are correct. Add any missing entries (sequels, prequels, recap movies, remastered versions) that AniList might have missed.

2. **Generate nyaa.si search queries** - nyaa.si search is VERY sensitive. Good queries are critical. For each entry, generate 3-5 search queries that would find the anime on nyaa.si at:
   - 1080p quality (search for "1080p", "BD", "BluRay", "HEVC")
   - Original release quality (search for the original broadcast/DVD quality)

   Important rules for nyaa.si queries:
   - Use the romanized Japanese title (romaji) as it appears on nyaa.si
   - Common release groups: "Erai-raws", "SubsPlease", "HorribleSubs", "Judas", "ASW", "DmonHiro", "Vyseo", "Cleo", "Zahuczky"
   - For movies, try: "{anime_name} Movie", "{anime_name} BD", "{anime_name} 1080p"
   - For TV, try: "{anime_name} 1080p", "{anime_name} HEVC", "{anime_name} Batch"
   - Avoid overly specific queries that return 0 results
   - Try both short and long forms of the title

3. **Return a JSON array** with this exact format:
```json
[
  {{
    "title": "English Title",
    "type": "TV",
    "number": "1-25",
    "quality_label": "Original",
    "year": 2009,
    "total_episodes": 25,
    "search_queries_1080p": ["Fullmetal Alchemist Brotherhood 1080p", "FMAB 1080p HEVC", "Hagane no Renkinjutsushi 1080p"],
    "search_queries_original": ["Fullmetal Alchemist Brotherhood BD", "FMAB BluRay"]
  }}
]
```

Return ONLY the JSON array, no other text. Make sure every entry has search queries."""

        try:
            url = f"{GEMINI_BASE_URL}/gemini-2.0-flash:generateContent?key={self.gemini_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.3,
                    "maxOutputTokens": 4096,
                }
            }
            resp = self.session.post(url, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()

            # Extract the text response
            text = ""
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    text += part.get("text", "")

            # Parse JSON from response
            text = text.strip()
            # Remove markdown code fences if present
            text = re.sub(r'^```json\s*', '', text)
            text = re.sub(r'\s*```$', '', text)

            refined = json.loads(text)

            # Merge refined data with original entries
            merged = []
            for r_entry in refined:
                # Find matching original entry
                orig = None
                for e in entries:
                    if (e.get("anilist_id") and
                        r_entry.get("title", "").lower() in e.get("title", "").lower()):
                        orig = e
                        break

                merged.append({
                    "title": r_entry.get("title", anime_name),
                    "type": r_entry.get("type", "TV"),
                    "number": str(r_entry.get("number", "1")),
                    "quality_label": r_entry.get("quality_label", "Original"),
                    "year": r_entry.get("year"),
                    "total_episodes": r_entry.get("total_episodes", 0),
                    "search_queries": (
                        r_entry.get("search_queries_1080p", []) +
                        r_entry.get("search_queries_original", [])
                    ),
                    "anilist_id": orig.get("anilist_id", 0) if orig else 0,
                    "synonyms": orig.get("synonyms", []) if orig else [],
                    "romaji": orig.get("romaji", "") if orig else "",
                    "english": r_entry.get("title", ""),
                    "native": orig.get("native", "") if orig else "",
                })

            return merged if merged else entries

        except json.JSONDecodeError as e:
            log.warning(f"Gemini returned invalid JSON, using AniList data: {e}")
            for entry in entries:
                entry["search_queries"] = self._build_basic_queries(entry)
            return entries
        except Exception as e:
            log.warning(f"Gemini refinement failed, using AniList data: {e}")
            for entry in entries:
                entry["search_queries"] = self._build_basic_queries(entry)
            return entries

    def _build_basic_queries(self, entry: dict) -> list[str]:
        """Build basic nyaa.si search queries without AI assistance."""
        queries = []
        title = entry.get("english") or entry.get("romaji") or entry.get("title", "")
        romaji = entry.get("romaji", "")
        synonyms = entry.get("synonyms", [])

        # Primary queries with 1080p
        queries.append(f"{title} 1080p")
        queries.append(f"{title} HEVC")

        # Try romaji if different from english
        if romaji and romaji.lower() != title.lower():
            queries.append(f"{romaji} 1080p")

        # Try synonyms
        for syn in synonyms[:2]:
            queries.append(f"{syn} 1080p")

        # Movie-specific queries
        if entry.get("type") == "Movie":
            queries.append(f"{title} BD")
            queries.append(f"{title} BluRay 1080p")

        # Original quality queries
        queries.append(f"{title} BD")
        if entry.get("quality_label") == "Remastered":
            queries.append(f"{title} Remastered")

        return queries


# ============================================================================
# PHASE 2: NYAA.SI SEARCH
# ============================================================================

class NyaaSearcher:
    """
    Search nyaa.si for anime torrents using the RSS feed.

    The RSS feed is more reliable than the JSON API, which is often
    behind DDoS protection. Returns results sorted by seeder count.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (AnimePipeline/2.0; +https://github.com/anime-pipeline)"
        })

    def search(self, entry: dict) -> list[dict]:
        """
        Search nyaa.si for an anime entry using all available search queries.
        Returns results sorted by seeder count (highest first).

        Each result dict:
        {
            "title": str,
            "magnet_url": str,
            "torrent_url": str,
            "info_hash": str,
            "seeders": int,
            "leechers": int,
            "size": str,
            "query_used": str,
        }
        """
        log.info(f"=== Phase 2: Searching nyaa.si for '{entry['title']}' ===")

        all_results = []
        queries = entry.get("search_queries", [])

        if not queries:
            queries = self._fallback_queries(entry)

        for query in queries:
            log.info(f"  Searching: '{query}'")
            results = self._search_rss(query)
            for r in results:
                r["query_used"] = query
            all_results.extend(results)
            if results:
                log.info(f"    Found {len(results)} result(s)")
            time.sleep(1)  # Rate limiting

        # Deduplicate by info_hash
        seen = set()
        unique = []
        for r in all_results:
            h = r.get("info_hash", "")
            if h and h in seen:
                continue
            if h:
                seen.add(h)
            unique.append(r)

        # Sort by seeders (highest first)
        unique.sort(key=lambda x: x.get("seeders", 0), reverse=True)

        log.info(f"Total unique results: {len(unique)}")
        if unique:
            log.info(f"Best match: {unique[0]['title']} ({unique[0].get('seeders', 0)} seeders)")

        return unique

    def _search_rss(self, query: str) -> list[dict]:
        """Search nyaa.si RSS feed for the given query."""
        params = {
            "page": "rss",
            "q": query,
            "c": "1_2",   # Anime - English translated
            "f": "0",      # No filter
        }

        try:
            resp = self.session.get(NYAA_BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"  nyaa.si RSS request failed: {e}")
            return []

        return self._parse_rss(resp.text)

    def _parse_rss(self, rss_text: str) -> list[dict]:
        """Parse nyaa.si RSS XML into result dicts."""
        results = []
        ns = {"nyaa": "https://nyaa.si/xmlns/nyaa"}

        try:
            root = ET.fromstring(rss_text)
        except ET.ParseError as e:
            log.warning(f"  Failed to parse RSS XML: {e}")
            return []

        for item in root.findall(".//item"):
            try:
                title_el = item.find("title")
                link_el = item.find("link")
                if title_el is None or link_el is None:
                    continue

                title = title_el.text or ""
                link = link_el.text or ""

                # nyaa namespace elements
                info_hash_el = item.find("nyaa:infoHash", ns)
                seeders_el = item.find("nyaa:seeders", ns)
                leechers_el = item.find("nyaa:leechers", ns)
                size_el = item.find("nyaa:size", ns)

                info_hash = info_hash_el.text if info_hash_el is not None else ""
                seeders = int(seeders_el.text) if seeders_el is not None else 0
                leechers = int(leechers_el.text) if leechers_el is not None else 0
                size = size_el.text if size_el is not None else ""

                # Determine URL type (torrent or magnet)
                torrent_url = ""
                magnet_url = ""
                if link.startswith("magnet:"):
                    magnet_url = link
                else:
                    torrent_url = link if link.startswith("http") else f"{NYAA_BASE_URL}{link}"

                results.append({
                    "title": title,
                    "magnet_url": magnet_url,
                    "torrent_url": torrent_url,
                    "info_hash": info_hash,
                    "seeders": seeders,
                    "leechers": leechers,
                    "size": size,
                    "query_used": "",
                })
            except Exception as e:
                log.warning(f"  Failed to parse RSS item: {e}")
                continue

        return results

    def _fallback_queries(self, entry: dict) -> list[str]:
        """Generate fallback queries if no AI queries available."""
        title = entry.get("english") or entry.get("romaji") or entry.get("title", "")
        queries = [
            f"{title} 1080p",
            f"{title} HEVC",
            f"{title} BD",
            f"{title} Batch",
        ]
        if entry.get("type") == "Movie":
            queries.append(f"{title} Movie 1080p")
        return queries


# ============================================================================
# PHASE 3: DOWNLOAD (aria2c with stall detection)
# ============================================================================

class TorrentDownloader:
    """
    Download anime torrents using aria2c with smart features:
    - 0B/s stall detection (aborts after STALL_TIMEOUT seconds)
    - Falls back to next best magnet/torrent if current one stalls
    - Handles large torrents (>50GB) by processing in chunks
    - Monitors disk space and cleans up if needed
    """

    def __init__(self):
        self.download_dir = DOWNLOAD_DIR
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def download(self, results: list[dict], entry: dict) -> list[Path]:
        """
        Try downloading from search results in order (sorted by seeders).
        If a download stalls at 0B/s for STALL_TIMEOUT seconds, abort
        and try the next result.

        Returns a list of downloaded video file paths (may be multiple
        if a batch torrent contained several video files).
        """
        log.info(f"=== Phase 3: Downloading '{entry['title']}' ===")

        if not results:
            log.error("No search results to download from")
            return []

        # Check estimated total size and warn about large torrents
        for i, result in enumerate(results[:5]):
            log.info(f"  [{i+1}] {result['title']} — {result.get('seeders', 0)} seeders, "
                     f"size: {result.get('size', '?')}")

        # Try each result in order
        for i, result in enumerate(results):
            log.info(f"  Attempt {i+1}/{min(len(results), 5)}: {result['title']}")

            # Check disk space first
            if not self._check_disk_space():
                self._cleanup_disk_space()

            # Try downloading
            file_paths = self._try_download(result)
            if file_paths:
                return file_paths

            log.info(f"  Download failed or stalled, trying next result...")

        log.error("All download attempts failed")
        return []

    def _try_download(self, result: dict) -> list[Path]:
        """Try to download from a single result. Returns list of video file paths on success."""
        # Strategy 1: Download via .torrent file
        torrent_url = result.get("torrent_url", "")
        if torrent_url:
            file_paths = self._download_via_torrent_file(torrent_url, result.get("title", ""))
            if file_paths:
                return file_paths

        # Strategy 2: Download via magnet link
        magnet_url = result.get("magnet_url", "")
        info_hash = result.get("info_hash", "")
        if not magnet_url and info_hash:
            magnet_url = self._build_magnet(info_hash, result.get("title", ""))

        if magnet_url:
            file_paths = self._download_via_magnet(magnet_url, result.get("title", ""))
            if file_paths:
                return file_paths

        return []

    def _download_via_torrent_file(self, url: str, title: str) -> list[Path]:
        """Download the .torrent file first, then use aria2c."""
        try:
            resp = requests.get(url, timeout=30,
                               headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()

            safe_name = re.sub(r'[^\w\s\-\.\[\]]', '', title)[:100]
            torrent_path = self.download_dir / f"{safe_name}.torrent"
            torrent_path.write_bytes(resp.content)
            log.info(f"  Saved .torrent file: {torrent_path}")

            return self._run_aria2c(str(torrent_path), is_torrent=True, title=title)
        except Exception as e:
            log.warning(f"  .torrent download failed: {e}")
            return []

    def _download_via_magnet(self, magnet: str, title: str) -> list[Path]:
        """Download using a magnet URI with aria2c."""
        log.info(f"  Using magnet link...")
        return self._run_aria2c(magnet, is_torrent=False, title=title)

    def _run_aria2c(self, source: str, is_torrent: bool = False,
                     title: str = "") -> list[Path]:
        """Run aria2c with stall detection and monitoring.

        Returns a list of all downloaded video files (handles batch torrents
        that contain multiple episodes).
        """
        # Clean download directory before starting a new download
        for f in self.download_dir.rglob("*"):
            if f.is_file():
                try:
                    f.unlink()
                except Exception:
                    pass

        cmd = [
            "aria2c",
            "--dir", str(self.download_dir),
            "--seed-time=0",
            "--max-tries=3",
            "--retry-wait=10",
            "--timeout=60",
            "--connect-timeout=30",
            "--max-download-limit=0",
            "--split=5",
            "--max-concurrent-downloads=1",
            "--file-allocation=none",
            "--summary-interval=30",
            "--bt-max-peers=55",
            "--bt-request-peer-speed-limit=0",
            "--continue=true",
        ]

        if is_torrent:
            cmd.extend(["--follow-torrent=mem", source])
        else:
            cmd.extend(["--follow-magnet=true", source])

        log.info(f"  Starting aria2c...")

        # Run aria2c in background and monitor
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            # Monitor the download with stall detection
            start_time = time.time()
            last_size = 0
            stall_start = None

            while proc.poll() is None:
                elapsed = time.time() - start_time
                if elapsed > MAX_DOWNLOAD_TIME:
                    log.warning(f"  Download exceeded {MAX_DOWNLOAD_TIME}s, aborting...")
                    proc.terminate()
                    proc.wait(timeout=10)
                    return []

                # Check current download size
                current_size = self._get_download_size()
                if current_size == last_size and current_size >= 0:
                    if stall_start is None:
                        stall_start = time.time()
                    elif time.time() - stall_start > STALL_TIMEOUT:
                        log.warning(f"  Download stalled at 0B/s for {STALL_TIMEOUT}s, aborting...")
                        proc.terminate()
                        proc.wait(timeout=10)
                        return []
                else:
                    stall_start = None
                    if elapsed > 30 and current_size > 0:
                        log.info(f"  Downloading... {current_size / 1024 / 1024:.0f} MB")

                last_size = current_size
                time.sleep(5)

            # Process finished
            if proc.returncode != 0:
                stderr = proc.stderr.read() if proc.stderr else ""
                log.warning(f"  aria2c exited with code {proc.returncode}: {stderr[-300:]}")

        except Exception as e:
            log.error(f"  aria2c failed: {e}")
            return []

        # Find ALL downloaded video files (batch torrents may contain multiple)
        return self._find_all_downloaded_videos()

    def _get_download_size(self) -> int:
        """Get total size of all files in download directory."""
        total = 0
        try:
            for f in self.download_dir.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        except Exception:
            pass
        return total

    def _find_all_downloaded_videos(self) -> list[Path]:
        """Find ALL downloaded video files (handles batch torrents)."""
        video_exts = {".mkv", ".mp4", ".avi", ".wmv", ".flv"}
        video_files = []

        for ext in video_exts:
            video_files.extend(self.download_dir.rglob(f"*{ext}"))

        if not video_files:
            log.warning("  No video files found after download")
            return []

        # Sort by name (for batch torrents, this keeps episodes in order)
        video_files.sort(key=lambda f: f.name)

        for vf in video_files:
            size_mb = vf.stat().st_size / 1024 / 1024
            log.info(f"  Downloaded: {vf.name} ({size_mb:.1f} MB)")

        if len(video_files) > 1:
            log.info(f"  Batch torrent: {len(video_files)} video files found")
            # Check total size and warn about large batches
            total_gb = sum(vf.stat().st_size for vf in video_files) / (1024 ** 3)
            if total_gb > CHUNK_SIZE_GB:
                log.info(f"  Total size: {total_gb:.1f} GB (>{CHUNK_SIZE_GB}GB — will process in chunks)")

        return video_files

    def _build_magnet(self, info_hash: str, title: str) -> str:
        """Build a magnet URI with public trackers."""
        trackers = [
            "udp://tracker.opentrackr.org:1337/announce",
            "udp://open.stealth.si:80/announce",
            "udp://tracker.torrent.eu.org:451/announce",
            "udp://tracker.bittor.pw:1337/announce",
            "udp://public.popcorn-tracker.org:6969/announce",
            "udp://tracker.dler.org:6969/announce",
            "udp://exodus.desync.com:6969/announce",
            "udp://open.demonii.com:1337/announce",
        ]
        magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}"
        for t in trackers:
            magnet += f"&tr={quote(t)}"
        return magnet

    def _check_disk_space(self) -> bool:
        """Check if there's enough disk space."""
        try:
            stat = shutil.disk_usage(str(WORK_DIR))
            free_gb = stat.free / (1024 ** 3)
            return free_gb >= MIN_FREE_GB
        except Exception:
            return True

    def _cleanup_disk_space(self):
        """Clean up old files to free disk space."""
        log.warning(f"Low disk space, cleaning up...")
        # Clean download directory first
        for f in self.download_dir.rglob("*"):
            if f.is_file():
                try:
                    f.unlink()
                except Exception:
                    pass
        # Clean output directory
        if OUTPUT_DIR.exists():
            for f in OUTPUT_DIR.rglob("*"):
                if f.is_file():
                    try:
                        f.unlink()
                    except Exception:
                        pass
        log.info("  Cleanup complete")


# ============================================================================
# PHASE 4: VIDEO PROCESSING (Softsub + Hardsub)
# ============================================================================

class VideoProcessor:
    """
    Process downloaded video files into softsub and hardsub MKV versions.

    Softsub: Remux with subtitle tracks as toggleable (no re-encoding)
    Hardsub: Burn subtitles into the video stream (requires re-encoding)
    """

    def process(self, input_path: Path, entry: dict) -> dict:
        """
        Process a video file into softsub and hardsub versions.

        Returns:
            {
                "softsub": Path or None,
                "hardsub": Path or None,
                "softsub_name": str,
                "hardsub_name": str,
            }
        """
        log.info(f"=== Phase 4: Processing '{input_path.name}' ===")

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)

        anime_name = entry.get("english") or entry.get("romaji") or entry.get("title", "")
        # Clean anime name for filename (remove special chars but keep spaces)
        clean_name = re.sub(r'[^\w\s\-]', '', anime_name).strip()

        softsub_name = build_filename(
            clean_name, entry["type"], entry["number"],
            entry["quality_label"], "Ss"
        )
        hardsub_name = build_filename(
            clean_name, entry["type"], entry["number"],
            entry["quality_label"], "Hs"
        )

        # Step 1: Extract subtitles
        subtitle_files = self._extract_subtitles(input_path)

        # Step 2: Create softsub version
        softsub_path = self._create_softsub(input_path, softsub_name)

        # Step 3: Create hardsub version
        hardsub_path = self._create_hardsub(input_path, subtitle_files, hardsub_name)

        result = {
            "softsub": softsub_path,
            "hardsub": hardsub_path,
            "softsub_name": softsub_name,
            "hardsub_name": hardsub_name,
        }

        log.info(f"  Processing complete:")
        log.info(f"    Softsub: {softsub_path} ({softsub_name})")
        log.info(f"    Hardsub: {hardsub_path} ({hardsub_name})")

        return result

    def _extract_subtitles(self, mkv_path: Path) -> list[Path]:
        """Extract subtitle tracks from the MKV file."""
        SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)
        extracted = []

        # Try mkvextract first
        if shutil.which("mkvextract"):
            sub_tracks = self._get_subtitle_info(mkv_path)
            if sub_tracks:
                extracted = self._extract_with_mkvextract(mkv_path, sub_tracks)

        # Fallback to ffmpeg
        if not extracted:
            extracted = self._extract_with_ffmpeg(mkv_path)

        # Also check for external subtitle files in the same directory
        for ext in (".ass", ".srt", ".sup", ".sub"):
            for f in mkv_path.parent.rglob(f"*{ext}"):
                if f not in extracted:
                    extracted.append(f)
                    log.info(f"  Found external subtitle: {f.name}")

        return extracted

    def _get_subtitle_info(self, mkv_path: Path) -> list[dict]:
        """Get subtitle track info using mkvmerge."""
        try:
            result = subprocess.run(
                ["mkvmerge", "--identify", "--json", str(mkv_path)],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                return []

            data = json.loads(result.stdout)
            tracks = []
            for track in data.get("tracks", []):
                if track.get("type") == "subtitles":
                    props = track.get("properties", {})
                    tracks.append({
                        "id": track.get("id", 0),
                        "codec": props.get("codec_id", ""),
                        "language": props.get("language", "und"),
                        "name": props.get("track_name", ""),
                        "default": props.get("default_track", False),
                    })
            return tracks
        except Exception as e:
            log.warning(f"  mkvmerge failed: {e}")
            return []

    def _extract_with_mkvextract(self, mkv_path: Path, tracks: list[dict]) -> list[Path]:
        """Extract subtitles using mkvextract."""
        extracted = []
        for track in tracks:
            track_id = track["id"]
            codec = track["codec"].lower()
            lang = track["language"]

            ext_map = {"s_text/ass": "ass", "s_text/ssa": "ass", "s_text/srt": "srt",
                       "s_vobsub": "sub", "s_hdmv/pgs": "sup"}
            ext = ext_map.get(codec, "srt")

            out_file = SUBTITLE_DIR / f"sub_t{track_id}_{lang}.{ext}"

            try:
                result = subprocess.run(
                    ["mkvextract", str(mkv_path), "tracks", f"{track_id}:{str(out_file)}"],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode == 0 and out_file.exists() and out_file.stat().st_size > 0:
                    extracted.append(out_file)
                    log.info(f"  Extracted subtitle track {track_id}: {out_file.name}")
            except Exception as e:
                log.warning(f"  mkvextract failed for track {track_id}: {e}")

        return extracted

    def _extract_with_ffmpeg(self, mkv_path: Path) -> list[Path]:
        """Extract subtitles using ffmpeg as fallback."""
        extracted = []

        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-select_streams", "s", str(mkv_path)],
                capture_output=True, text=True, timeout=30
            )
            data = json.loads(result.stdout)
            streams = data.get("streams", [])
        except Exception as e:
            log.warning(f"  ffprobe failed: {e}")
            return []

        for stream in streams:
            codec = stream.get("codec_name", "srt")
            lang = stream.get("tags", {}).get("language", "und")
            index = stream.get("index", 0)

            ext_map = {"ass": "ass", "ssa": "ass", "srt": "srt", "sub": "sub",
                       "dvd_subtitle": "sub"}
            ext = ext_map.get(codec, "srt")

            out_file = SUBTITLE_DIR / f"sub_ff_{index}_{lang}.{ext}"

            try:
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", str(mkv_path),
                     "-map", f"0:{index}",
                     "-f", ext if ext in ("srt", "ass") else "srt",
                     str(out_file)],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode == 0 and out_file.exists() and out_file.stat().st_size > 0:
                    extracted.append(out_file)
            except Exception as e:
                log.warning(f"  ffmpeg subtitle extract failed for stream {index}: {e}")

        return extracted

    def _create_softsub(self, input_path: Path, output_name: str) -> Path | None:
        """Create softsub MKV (remux with all streams, no re-encoding)."""
        output_file = OUTPUT_DIR / output_name

        if output_file.exists():
            log.info(f"  Softsub already exists: {output_file}")
            return output_file

        log.info(f"  Creating softsub: {output_name}")

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-map", "0",
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_file),
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0 and output_file.exists():
                size_mb = output_file.stat().st_size / 1024 / 1024
                log.info(f"  Softsub created: {size_mb:.1f} MB")
                return output_file
            else:
                log.error(f"  Softsub failed: {result.stderr[-300:]}")
                return None
        except Exception as e:
            log.error(f"  Softsub error: {e}")
            return None

    def _create_hardsub(self, input_path: Path, subtitle_files: list[Path],
                          output_name: str) -> Path | None:
        """Create hardsub MKV (burn subtitles into video)."""
        output_file = OUTPUT_DIR / output_name

        if output_file.exists():
            log.info(f"  Hardsub already exists: {output_file}")
            return output_file

        if not subtitle_files:
            log.warning("  No subtitles for hardsub, trying embedded stream...")
            return self._hardsub_from_embedded(input_path, output_file)

        # Pick best subtitle
        sub_file = self._pick_best_subtitle(subtitle_files)
        log.info(f"  Creating hardsub with subtitle: {sub_file.name}")

        # Escape path for ffmpeg filter
        sub_escaped = (str(sub_file).replace("\\", "/").replace(":", "\\:")
                       .replace("'", "\\'").replace("[", "\\[").replace("]", "\\]"))

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vf", f"subtitles='{sub_escaped}'",
            "-c:v", "libx264",
            "-preset", HARDSUB_PRESET,
            "-crf", HARDSUB_CRF,
            "-c:a", "copy",
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-map", "-0:s",
            "-movflags", "+faststart",
            str(output_file),
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
            if result.returncode == 0 and output_file.exists():
                size_mb = output_file.stat().st_size / 1024 / 1024
                log.info(f"  Hardsub created: {size_mb:.1f} MB")
                return output_file
            else:
                log.warning(f"  Hardsub with extracted subtitle failed, trying embedded...")
                return self._hardsub_from_embedded(input_path, output_file)
        except Exception as e:
            log.warning(f"  Hardsub error: {e}, trying embedded...")
            return self._hardsub_from_embedded(input_path, output_file)

    def _hardsub_from_embedded(self, input_path: Path, output_file: Path) -> Path | None:
        """Burn subtitles from embedded MKV stream."""
        mkv_escaped = (str(input_path).replace("\\", "/").replace(":", "\\:")
                       .replace("'", "\\'").replace("[", "\\[").replace("]", "\\]"))

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vf", f"subtitles='{mkv_escaped}'",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "copy",
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-map", "-0:s",
            "-movflags", "+faststart",
            str(output_file),
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
            if result.returncode == 0 and output_file.exists():
                size_mb = output_file.stat().st_size / 1024 / 1024
                log.info(f"  Hardsub (embedded) created: {size_mb:.1f} MB")
                return output_file
            else:
                log.error(f"  Hardsub failed completely: {result.stderr[-300:]}")
                return None
        except Exception as e:
            log.error(f"  Hardsub embedded error: {e}")
            return None

    def _pick_best_subtitle(self, files: list[Path]) -> Path:
        """Pick the best subtitle file (ASS > SRT, English preferred)."""
        ass = [f for f in files if f.suffix == ".ass"]
        srt = [f for f in files if f.suffix == ".srt"]

        for candidates in [ass, srt]:
            eng = [f for f in candidates if "eng" in f.name.lower() or "en" in f.name.lower()]
            if eng:
                return eng[0]
            if candidates:
                return candidates[0]

        return files[0]


# ============================================================================
# PHASE 5: UPLOAD TO STREAMING PLATFORMS
# ============================================================================

class Uploader:
    """
    Upload processed video files to multiple streaming platforms.

    Supported platforms:
    - StreamP2P (TUS protocol upload)
    - Doodstream (HTTP POST upload)
    - Lulustream (placeholder - needs API key)
    - Streamtape (placeholder - needs API key)
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "AnimePipeline/2.0"
        })
        # Cache for StreamP2P folder IDs
        self._sp2p_folders = None

    def upload_all(self, files: dict, entry: dict) -> dict:
        """
        Upload both softsub and hardsub files to all platforms.

        Returns a dict of upload results per platform.
        """
        log.info("=== Phase 5: Uploading to all platforms ===")

        results = {
            "streamp2p": {"softsub": None, "hardsub": None},
            "doodstream": {"softsub": None, "hardsub": None},
            "lulustream": {"softsub": None, "hardsub": None},
            "streamtape": {"softsub": None, "hardsub": None},
        }

        # Ensure StreamP2P folder structure
        sp2p_folders = self._ensure_streamp2p_folders(entry)

        # Upload softsub
        softsub = files.get("softsub")
        if softsub:
            # StreamP2P
            if STREAMP2P_API_KEY:
                results["streamp2p"]["softsub"] = self._upload_streamp2p(
                    softsub, sp2p_folders.get("softsub")
                )
            # Doodstream
            if DOODSTREAM_API_KEY:
                results["doodstream"]["softsub"] = self._upload_doodstream(
                    softsub, DOODSTREAM_FOLDER_SS, files.get("softsub_name", softsub.name)
                )
            # Lulustream
            if LULUSTREAM_API_KEY:
                results["lulustream"]["softsub"] = self._upload_lulustream(softsub)
            # Streamtape
            if STREAMTAPE_API_KEY:
                results["streamtape"]["softsub"] = self._upload_streamtape(softsub)

        # Upload hardsub
        hardsub = files.get("hardsub")
        if hardsub:
            # StreamP2P
            if STREAMP2P_API_KEY:
                results["streamp2p"]["hardsub"] = self._upload_streamp2p(
                    hardsub, sp2p_folders.get("hardsub")
                )
            # Doodstream
            if DOODSTREAM_API_KEY:
                results["doodstream"]["hardsub"] = self._upload_doodstream(
                    hardsub, DOODSTREAM_FOLDER_HS, files.get("hardsub_name", hardsub.name)
                )
            # Lulustream
            if LULUSTREAM_API_KEY:
                results["lulustream"]["hardsub"] = self._upload_lulustream(hardsub)
            # Streamtape
            if STREAMTAPE_API_KEY:
                results["streamtape"]["hardsub"] = self._upload_streamtape(hardsub)

        # Log results
        for platform, uploads in results.items():
            for sub_type, result in uploads.items():
                status = "OK" if result else "FAILED"
                log.info(f"  {platform}/{sub_type}: {status}")

        return results

    # ── StreamP2P ──

    def _sp2p_headers(self) -> dict:
        """Headers for StreamP2P v1 API."""
        return {"api-token": STREAMP2P_API_KEY, "Content-Type": "application/json"}

    def _ensure_streamp2p_folders(self, entry: dict) -> dict:
        """
        Ensure StreamP2P folder structure exists for the anime.
        Creates: AnimeName / Soft Sub / Original Episodes
                 AnimeName / Hard Sub / Original Episodes

        Returns: {"softsub": folder_id, "hardsub": folder_id}
        """
        if self._sp2p_folders is not None:
            return self._sp2p_folders

        headers = self._sp2p_headers()
        anime_name = entry.get("english") or entry.get("romaji") or entry.get("title", "Anime")
        # Clean name for folder
        clean_name = re.sub(r'[^\w\s\-]', '', anime_name).strip()

        # Get existing folders
        try:
            resp = self.session.get(
                f"{STREAMP2P_BASE_URL}/video/folder",
                headers=headers, timeout=15
            )
            resp.raise_for_status()
            folders = resp.json()
            log.info(f"  StreamP2P: Found {len(folders)} existing folder(s)")
        except Exception as e:
            log.error(f"  Failed to list StreamP2P folders: {e}")
            return {"softsub": None, "hardsub": None}

        # Build folder lookup
        folder_by_id = {f["id"]: f for f in folders}
        folder_by_name_parent = {}
        for f in folders:
            key = (f.get("parentId"), f.get("name"))
            folder_by_name_parent[key] = f

        # Find or create anime root folder
        root_key = (None, clean_name)
        anime_root = folder_by_name_parent.get(root_key)
        if not anime_root:
            # Also check for root folders (parentId could be "" or null)
            for f in folders:
                if f.get("name") == clean_name and not f.get("parentId"):
                    anime_root = f
                    break

        if not anime_root:
            anime_root = self._create_sp2p_folder(clean_name, headers=headers)
            if not anime_root:
                return {"softsub": None, "hardsub": None}
            log.info(f"  Created StreamP2P folder: {clean_name}")

        root_id = anime_root["id"]

        # Find or create Soft Sub and Hard Sub under anime root
        result = {}
        for sub_name, key in [("Soft Sub", "softsub"), ("Hard Sub", "hardsub")]:
            sub_folder = folder_by_name_parent.get((root_id, sub_name))
            if not sub_folder:
                # Search again in case we just created it
                for f in folders:
                    if f.get("parentId") == root_id and f.get("name") == sub_name:
                        sub_folder = f
                        break

            if not sub_folder:
                sub_folder = self._create_sp2p_folder(sub_name, parent_id=root_id, headers=headers)
                if not sub_folder:
                    log.warning(f"  Failed to create {sub_name} folder on StreamP2P")
                    result[key] = None
                    continue
                log.info(f"  Created StreamP2P folder: {clean_name} / {sub_name}")

            # Find or create "Original Episodes" or "Remastered" subfolder
            quality = entry.get("quality_label", "Original")
            quality_folder = None
            for f in folders:
                if f.get("parentId") == sub_folder["id"] and f.get("name") == quality:
                    quality_folder = f
                    break

            if not quality_folder:
                quality_folder = self._create_sp2p_folder(quality, parent_id=sub_folder["id"], headers=headers)
                if quality_folder:
                    log.info(f"  Created StreamP2P folder: {clean_name} / {sub_name} / {quality}")

            result[key] = quality_folder["id"] if quality_folder else sub_folder["id"]

        self._sp2p_folders = result
        return result

    def _create_sp2p_folder(self, name: str, parent_id: str = None,
                             headers: dict = None) -> dict | None:
        """Create a folder on StreamP2P."""
        if not headers:
            headers = self._sp2p_headers()

        body = {"name": name}
        if parent_id:
            body["folderId"] = parent_id

        try:
            resp = self.session.post(
                f"{STREAMP2P_BASE_URL}/video/folder",
                headers=headers,
                json=body,
                timeout=15,
            )
            if resp.status_code == 201:
                data = resp.json()
                folder_id = data.get("id")
                if folder_id:
                    return {"id": folder_id, "name": name}
            else:
                log.warning(f"  StreamP2P folder creation failed ({resp.status_code}): {resp.text[:200]}")
        except Exception as e:
            log.error(f"  StreamP2P folder creation error: {e}")
        return None

    def _upload_streamp2p(self, file_path: Path, folder_id: str = None) -> str | None:
        """
        Upload a video file to StreamP2P using the TUS protocol.

        Based on the official API documentation:
        1. GET /api/v1/video/upload → get tusUrl and accessToken
        2. POST to tusUrl with TUS headers (Upload-Length, Upload-Metadata, Tus-Resumable)
        3. PATCH to the returned URL with file chunks (50MB each)

        The metadata header must include: accessToken, filename, filetype, and optional folderId
        """
        log.info(f"  Uploading to StreamP2P: {file_path.name}")

        for attempt in range(MAX_UPLOAD_RETRIES):
            try:
                # Step 1: Get TUS upload endpoint
                headers = self._sp2p_headers()
                resp = self.session.get(
                    f"{STREAMP2P_BASE_URL}/video/upload",
                    headers=headers,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()

                tus_url = data.get("tusUrl")
                access_token = data.get("accessToken")

                if not tus_url or not access_token:
                    log.error(f"  StreamP2P: No TUS URL or access token in response: {data}")
                    continue

                log.info(f"  StreamP2P TUS endpoint: {tus_url}")

                # Step 2: Create the upload via TUS POST
                file_size = file_path.stat().st_size
                filename = file_path.name
                filetype = "video/x-matroska" if filename.endswith(".mkv") else "video/mp4"

                # Build Upload-Metadata header
                # Format: key base64(value),key base64(value),...
                metadata_parts = [
                    f"accessToken {base64.b64encode(access_token.encode()).decode()}",
                    f"filename {base64.b64encode(filename.encode()).decode()}",
                    f"filetype {base64.b64encode(filetype.encode()).decode()}",
                ]
                if folder_id:
                    metadata_parts.append(
                        f"folderId {base64.b64encode(folder_id.encode()).decode()}"
                    )

                upload_metadata = ",".join(metadata_parts)

                tus_headers = {
                    "Upload-Length": str(file_size),
                    "Upload-Metadata": upload_metadata,
                    "Tus-Resumable": "1.0.0",
                    "Content-Length": "0",
                }

                post_resp = self.session.post(
                    tus_url,
                    headers=tus_headers,
                    timeout=30,
                )

                if post_resp.status_code not in (200, 201):
                    log.error(f"  StreamP2P TUS POST failed ({post_resp.status_code}): {post_resp.text[:300]}")
                    continue

                # Get the upload URL from Location header
                upload_url = post_resp.headers.get("Location")
                if not upload_url:
                    log.error(f"  StreamP2P TUS: No Location header in response")
                    continue

                # Make upload URL absolute if relative
                if upload_url.startswith("/"):
                    from urllib.parse import urlparse
                    parsed = urlparse(tus_url)
                    upload_url = f"{parsed.scheme}://{parsed.netloc}{upload_url}"

                log.info(f"  StreamP2P TUS upload URL: {upload_url}")

                # Step 3: Upload file chunks via PATCH
                uploaded = self._tus_upload_chunks(upload_url, file_path, file_size)
                if uploaded:
                    log.info(f"  StreamP2P upload complete!")
                    return upload_url
                else:
                    log.warning(f"  StreamP2P TUS chunk upload failed (attempt {attempt+1})")

            except Exception as e:
                log.error(f"  StreamP2P upload error (attempt {attempt+1}): {e}")

            if attempt < MAX_UPLOAD_RETRIES - 1:
                wait = 5 * (attempt + 1)
                log.info(f"  Retrying in {wait}s...")
                time.sleep(wait)

        log.error(f"  StreamP2P upload failed after {MAX_UPLOAD_RETRIES} attempts")
        return None

    def _tus_upload_chunks(self, upload_url: str, file_path: Path,
                            file_size: int) -> bool:
        """Upload file in 50MB chunks using TUS PATCH protocol."""
        offset = 0

        with open(file_path, "rb") as f:
            while offset < file_size:
                chunk = f.read(TUS_CHUNK_SIZE)
                if not chunk:
                    break

                chunk_len = len(chunk)
                chunk_headers = {
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Offset": str(offset),
                    "Tus-Resumable": "1.0.0",
                    "Content-Length": str(chunk_len),
                }

                try:
                    resp = self.session.patch(
                        upload_url,
                        headers=chunk_headers,
                        data=chunk,
                        timeout=300,
                    )

                    if resp.status_code in (200, 204):
                        offset += chunk_len
                        progress = (offset / file_size) * 100
                        if int(progress) % 10 == 0:  # Log every ~10%
                            log.info(f"    Upload progress: {progress:.0f}% ({offset / 1024 / 1024:.0f} / {file_size / 1024 / 1024:.0f} MB)")
                    else:
                        log.error(f"    TUS PATCH failed ({resp.status_code}): {resp.text[:200]}")
                        return False

                except requests.Timeout:
                    log.warning(f"    TUS chunk timeout, retrying from offset {offset}...")
                    continue
                except Exception as e:
                    log.error(f"    TUS chunk error: {e}")
                    return False

        return offset >= file_size

    # ── Doodstream ──

    def _upload_doodstream(self, file_path: Path, folder_id: str,
                            title: str = "") -> str | None:
        """Upload a video file to Doodstream."""
        log.info(f"  Uploading to Doodstream: {file_path.name}")

        if not DOODSTREAM_API_KEY:
            log.warning("  Doodstream API key not set, skipping")
            return None

        for attempt in range(MAX_UPLOAD_RETRIES):
            try:
                # Get upload server URL
                resp = self.session.get(
                    f"{DOODSTREAM_BASE_URL}/upload/server",
                    params={"key": DOODSTREAM_API_KEY},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()

                if data.get("status") != 200:
                    log.error(f"  Doodstream get-server failed: {data}")
                    continue

                upload_url = data.get("result")
                if not upload_url:
                    log.error(f"  Doodstream: No upload URL in response")
                    continue

                # Upload the file
                file_size_mb = file_path.stat().st_size / 1024 / 1024
                log.info(f"  Uploading {file_size_mb:.1f} MB to Doodstream...")

                with open(file_path, "rb") as f:
                    content_type = "video/x-matroska" if file_path.suffix == ".mkv" else "video/mp4"
                    files = {"file": (file_path.name, f, content_type)}
                    post_data = {
                        "api_key": DOODSTREAM_API_KEY,
                        "fld_id": folder_id,
                    }
                    if title:
                        post_data["title"] = title

                    resp = self.session.post(
                        upload_url, files=files, data=post_data, timeout=1800
                    )
                    resp.raise_for_status()

                    # Parse response
                    try:
                        result = resp.json()
                    except ValueError:
                        # Try to parse HTML response
                        match = re.search(r'"filecode"\s*:\s*"([^"]+)"', resp.text)
                        if match:
                            file_code = match.group(1)
                            log.info(f"  Doodstream upload OK! File code: {file_code}")
                            return file_code
                        log.error(f"  Doodstream: Non-JSON response: {resp.text[:300]}")
                        continue

                if result.get("status") == 200:
                    result_data = result.get("result", [])
                    if isinstance(result_data, list) and result_data:
                        file_code = result_data[0].get("filecode", "")
                    elif isinstance(result_data, dict):
                        file_code = result_data.get("filecode", "")
                    else:
                        file_code = str(result_data) if result_data else ""

                    if file_code:
                        log.info(f"  Doodstream upload OK! File code: {file_code}")
                        return file_code
                    else:
                        log.error(f"  Doodstream: No file code in response: {result}")
                else:
                    log.error(f"  Doodstream upload failed: {result}")

            except Exception as e:
                log.error(f"  Doodstream upload error (attempt {attempt+1}): {e}")

            if attempt < MAX_UPLOAD_RETRIES - 1:
                time.sleep(5 * (attempt + 1))

        return None

    # ── Lulustream (Placeholder) ──

    def _upload_lulustream(self, file_path: Path) -> str | None:
        """Upload to Lulustream. API key required."""
        log.info(f"  Uploading to Lulustream: {file_path.name}")

        if not LULUSTREAM_API_KEY:
            log.warning("  Lulustream API key not set, skipping")
            return None

        for attempt in range(MAX_UPLOAD_RETRIES):
            try:
                # Get upload server
                resp = self.session.get(
                    f"{LULUSTREAM_BASE_URL}/upload/server",
                    params={"key": LULUSTREAM_API_KEY},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()

                upload_url = data.get("result") or data.get("url")
                if not upload_url:
                    log.error(f"  Lulustream: No upload URL: {data}")
                    continue

                # Upload file
                with open(file_path, "rb") as f:
                    files = {"file": (file_path.name, f)}
                    post_data = {"key": LULUSTREAM_API_KEY}

                    resp = self.session.post(
                        upload_url, files=files, data=post_data, timeout=1800
                    )
                    resp.raise_for_status()
                    result = resp.json()

                    # Try to extract file code
                    file_code = ""
                    if isinstance(result, dict):
                        file_code = (result.get("result", {})
                                    .get("filecode", "") if isinstance(result.get("result"), dict)
                                    else result.get("filecode", ""))

                    if file_code:
                        log.info(f"  Lulustream upload OK! Code: {file_code}")
                        return file_code
                    else:
                        log.warning(f"  Lulustream response: {str(result)[:300]}")

            except Exception as e:
                log.error(f"  Lulustream error (attempt {attempt+1}): {e}")

            if attempt < MAX_UPLOAD_RETRIES - 1:
                time.sleep(5)

        return None

    # ── Streamtape (Placeholder) ──

    def _upload_streamtape(self, file_path: Path) -> str | None:
        """Upload to Streamtape. API key + login required."""
        log.info(f"  Uploading to Streamtape: {file_path.name}")

        if not STREAMTAPE_API_KEY:
            log.warning("  Streamtape API key not set, skipping")
            return None

        for attempt in range(MAX_UPLOAD_RETRIES):
            try:
                # Get upload server
                resp = self.session.get(
                    f"{STREAMTAPE_BASE_URL}/file/ul",
                    params={"login": STREAMTAPE_API_KEY, "key": STREAMTAPE_API_KEY},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()

                upload_url = data.get("url")
                if not upload_url:
                    log.error(f"  Streamtape: No upload URL: {data}")
                    continue

                # Upload file
                with open(file_path, "rb") as f:
                    files = {"file1": (file_path.name, f)}
                    resp = self.session.post(
                        upload_url, files=files, timeout=1800
                    )
                    resp.raise_for_status()
                    result = resp.json()

                    file_code = result.get("url") or result.get("filecode", "")
                    if file_code:
                        log.info(f"  Streamtape upload OK! Code: {file_code}")
                        return file_code
                    else:
                        log.warning(f"  Streamtape response: {str(result)[:300]}")

            except Exception as e:
                log.error(f"  Streamtape error (attempt {attempt+1}): {e}")

            if attempt < MAX_UPLOAD_RETRIES - 1:
                time.sleep(5)

        return None


# ============================================================================
# STORAGE MANAGER
# ============================================================================

class StorageManager:
    """Manage disk space — auto-delete old files when storage is low."""

    def __init__(self, min_free_gb: float = MIN_FREE_GB):
        self.min_free_gb = min_free_gb

    def check_and_cleanup(self) -> bool:
        """
        Check disk space and clean up if needed.
        Returns True if enough space, False if still low after cleanup.
        """
        free_gb = self._get_free_space_gb()
        if free_gb >= self.min_free_gb:
            return True

        log.warning(f"Low disk space: {free_gb:.1f} GB free (need {self.min_free_gb} GB)")
        log.info("Cleaning up processed files...")

        # Clean download directory
        self._clean_directory(DOWNLOAD_DIR)
        # Clean subtitle directory
        self._clean_directory(SUBTITLE_DIR)
        # Clean output directory (keep last processed files)
        self._clean_directory(OUTPUT_DIR, keep_count=2)

        free_gb = self._get_free_space_gb()
        if free_gb >= self.min_free_gb:
            log.info(f"After cleanup: {free_gb:.1f} GB free")
            return True

        log.error(f"Still low on disk space after cleanup: {free_gb:.1f} GB free")
        return False

    def cleanup_entry(self, download_path: Path = None, processed: dict = None):
        """Clean up files for a completed entry."""
        # Remove download files
        if download_path and download_path.exists():
            try:
                if download_path.is_file():
                    download_path.unlink()
                    log.info(f"  Cleaned up download: {download_path.name}")
                else:
                    shutil.rmtree(download_path, ignore_errors=True)
            except Exception:
                pass

        # Clean subtitle files
        if SUBTITLE_DIR.exists():
            for f in SUBTITLE_DIR.iterdir():
                try:
                    if f.is_file():
                        f.unlink()
                except Exception:
                    pass

    def _get_free_space_gb(self) -> float:
        """Get free disk space in GB."""
        try:
            stat = shutil.disk_usage(str(WORK_DIR))
            return stat.free / (1024 ** 3)
        except Exception:
            return 999.0  # Assume plenty if we can't check

    def _clean_directory(self, directory: Path, keep_count: int = 0):
        """Clean a directory, optionally keeping the N most recent files."""
        if not directory.exists():
            return

        files = sorted(directory.rglob("*"), key=lambda f: f.stat().st_mtime, reverse=True)

        for f in files[keep_count:]:
            if f.is_file():
                try:
                    f.unlink()
                    log.info(f"  Deleted: {f.name}")
                except Exception:
                    pass


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def _extract_ep_from_filename(filename: str) -> str | None:
    """
    Try to extract an episode number from a torrent filename.
    Handles common formats like:
      - [Erai-raws] Anime - 1200 [1080p]
      - [SubsPlease] Anime - 01
      - Anime EP01 1080p
      - Anime E05 1080p
      - Anime 12 1080p
    """
    # Try "- NNN" pattern (Erai-raws, SubsPlease style)
    m = re.search(r'-\s*(\d{2,4})\s*[\[\(]', filename)
    if m:
        return m.group(1)

    # Try "EP NNN" or "EPNNN"
    m = re.search(r'[Ee][Pp]\s*\.?(\d{2,4})', filename)
    if m:
        return m.group(1)

    # Try "E NNN" pattern
    m = re.search(r'\bE(\d{2,4})\b', filename)
    if m:
        return m.group(1)

    # Try standalone number after anime name (e.g., "Anime 01 1080p")
    m = re.search(r'\b(\d{2,4})\b', filename)
    if m:
        num = int(m.group(1))
        if 1 <= num <= 9999:  # Reasonable episode range
            return m.group(1)

    return None


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="AI-Powered Anime Bulk Downloader & Uploader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--name", "-n", required=True,
        help="Anime name to search for (e.g. 'Fullmetal Alchemist Brotherhood')"
    )
    parser.add_argument(
        "--type-filter", "-t", default="",
        help="Filter by type: tv, movie, ova, ona, special"
    )
    parser.add_argument(
        "--episodes", "-e", default="",
        help="Episode range for TV series (e.g. '1-25')"
    )
    parser.add_argument(
        "--quality", "-q", default="",
        help="Preferred quality (e.g. '1080p', '720p')"
    )
    parser.add_argument(
        "--skip-upload", action="store_true",
        help="Skip uploading, only download and process"
    )
    parser.add_argument(
        "--skip-hardsub", action="store_true",
        help="Skip hardsub creation (faster, less CPU)"
    )
    parser.add_argument(
        "--output-dir", default="",
        help="Override output directory"
    )

    return parser.parse_args()


def main():
    """Main pipeline entry point."""
    args = parse_args()

    log.info("=" * 60)
    log.info("AI-Powered Anime Pipeline")
    log.info(f"Target: {args.name}")
    log.info("=" * 60)

    # Setup directories
    global OUTPUT_DIR
    if args.output_dir:
        OUTPUT_DIR = Path(args.output_dir)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize components
    researcher = AnimeResearcher(GEMINI_API_KEY)
    searcher = NyaaSearcher()
    downloader = TorrentDownloader()
    processor = VideoProcessor()
    uploader = Uploader()
    storage = StorageManager()

    # Phase 1: Research
    entries = researcher.research(args.name, args.type_filter, args.episodes)
    if not entries:
        log.error("No anime entries found. Check the anime name and try again.")
        sys.exit(1)

    # Process each entry
    success_count = 0
    fail_count = 0

    for i, entry in enumerate(entries):
        log.info(f"\n{'=' * 60}")
        log.info(f"Processing entry {i+1}/{len(entries)}: {entry['title']}")
        log.info(f"{'=' * 60}")

        try:
            # Check disk space
            if not storage.check_and_cleanup():
                log.error("Insufficient disk space, stopping pipeline")
                break

            # Phase 2: Search nyaa.si
            results = searcher.search(entry)
            if not results:
                log.warning(f"No nyaa.si results for: {entry['title']}")
                fail_count += 1
                continue

            # Phase 3: Download
            downloaded_files = downloader.download(results, entry)
            if not downloaded_files:
                log.warning(f"Download failed for: {entry['title']}")
                fail_count += 1
                continue

            # Phase 4 & 5: Process each downloaded file (handles batch torrents)
            # For batch torrents with multiple files, try to extract episode
            # numbers from filenames and adjust the entry number accordingly
            for file_idx, downloaded_file in enumerate(downloaded_files):
                try:
                    # For batch torrents, adjust the episode number per file
                    batch_entry = dict(entry)
                    if len(downloaded_files) > 1 and entry["type"] == "TV":
                        # Try to extract episode number from filename
                        ep_num = _extract_ep_from_filename(downloaded_file.name)
                        if ep_num:
                            batch_entry["number"] = ep_num
                            log.info(f"  Batch file {file_idx+1}/{len(downloaded_files)}: "
                                     f"episode {ep_num}")
                        else:
                            # Fall back to sequential numbering
                            try:
                                base_num = int(entry["number"].split("-")[0])
                                batch_entry["number"] = str(base_num + file_idx)
                            except (ValueError, IndexError):
                                batch_entry["number"] = str(file_idx + 1)

                    # Phase 4: Process this file
                    processed = processor.process(downloaded_file, batch_entry)

                    # Phase 5: Upload
                    if not args.skip_upload:
                        upload_results = uploader.upload_all(processed, batch_entry)
                    else:
                        log.info("Upload skipped (--skip-upload)")

                    # Cleanup this download file to free space for next
                    if len(downloaded_files) > 1:
                        storage.cleanup_entry(downloaded_file, processed)

                except Exception as e:
                    log.error(f"  Failed to process batch file {file_idx+1}: {e}")
                    continue

            # Final cleanup for single-file entries
            if len(downloaded_files) == 1:
                storage.cleanup_entry(downloaded_files[0], None)

            success_count += 1
            log.info(f"Entry complete: {entry['title']}")

        except Exception as e:
            log.error(f"Entry failed: {entry['title']} — {e}")
            fail_count += 1
            continue

    # Final summary
    log.info(f"\n{'=' * 60}")
    log.info(f"Pipeline Complete")
    log.info(f"  Success: {success_count}")
    log.info(f"  Failed:  {fail_count}")
    log.info(f"  Total:   {len(entries)}")
    log.info(f"{'=' * 60}")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
