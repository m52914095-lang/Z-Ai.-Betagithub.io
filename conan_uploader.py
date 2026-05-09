#!/usr/bin/env python3
"""
Detective Conan Auto-Uploader
==============================
Automated script that:
  1. Calculates the latest episode number (base: ep 1200 on May 2, 2026)
  2. Searches nyaa.si RSS for Erai-raws release at 1080p
  3. Downloads the MKV file via torrent (aria2c)
  4. Extracts embedded subtitles from the MKV
  5. Creates a Hard-Sub version (subtitles burned into video)
  6. Creates a Soft-Sub version (subtitles as separate track)
  7. Uploads both versions to Doodstream and StreamP2P

Usage:
  python3 conan_uploader.py                    # Auto-detect latest episode
  python3 conan_uploader.py --episode 1200     # Process a specific episode
  python3 conan_uploader.py --range 1195 1200  # Process a range of episodes
  python3 conan_uploader.py --latest           # Same as auto-detect (explicit)

Designed to run as a GitHub Actions cron job every Saturday at 7:30 AM EST.
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
import base64
import xml.etree.ElementTree as ET
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

# ──────────────────────────────────────────────
# CONFIGURATION — API Keys & Folder IDs
# ──────────────────────────────────────────────

# Doodstream
DOODSTREAM_API_KEY = "554366xrjxeza9m7e4m02v"
DOODSTREAM_BASE_URL = "https://doodapi.com/api"
DOODSTREAM_FOLDER_HARDSUB = "1729147"   # Hard Sub > Original Episodes (fld_id)
DOODSTREAM_FOLDER_SOFTSUB = "1748072"   # Soft Sub > Episodes (fld_id)

# StreamP2P
STREAMP2P_API_KEY = "a7165e18e69dc32127258688"
STREAMP2P_BASE_URL = "https://streamp2p.com/api"
STREAMP2P_V1_BASE_URL = "https://streamp2p.com/api/v1"
STREAMP2P_FOLDER_HARDSUB = "3eyk"   # Hard Sub > Original Episodes (folder ID)
STREAMP2P_FOLDER_SOFTSUB = "5mfe"   # Soft Sub > Original Episodes (folder ID)
STREAMP2P_TUS_CHUNK_SIZE = 52_428_800  # 50 MB chunks per TUS spec

# Nyaa RSS configuration
NYAA_BASE_URL = "https://nyaa.si"
NYAA_USER = "Erai-raws"

# Episode tracking
# Episode 1200 was released on May 2, 2026 (Saturday)
# Conan airs weekly on Saturdays
BASE_EPISODE = 1200
BASE_DATE = datetime(2026, 5, 2, tzinfo=timezone.utc)

# Working directories
WORK_DIR = Path("/tmp/conan_uploader")
DOWNLOAD_DIR = WORK_DIR / "downloads"
OUTPUT_DIR = WORK_DIR / "output"

# FFmpeg encoding settings for hard sub
HARDSUB_CRF = "23"           # Quality (lower = better, higher file size)
HARDSUB_PRESET = "medium"    # Speed: ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow
HARDSUB_AUDIO_CODEC = "copy" # Copy audio without re-encoding

# ──────────────────────────────────────────────
# LOGGING SETUP
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("conan-uploader")


# ──────────────────────────────────────────────
# EPISODE SELECTION — argparse
# ──────────────────────────────────────────────

def parse_args():
    """
    Parse command-line arguments for episode selection.

    Modes:
      --latest    : Auto-calculate the latest episode (default)
      --episode N : Process a specific episode number
      --range A B : Process episodes from A to B inclusive
      (no args)   : Same as --latest
    """
    parser = argparse.ArgumentParser(
        description="Detective Conan Auto-Uploader — Downloads, encodes, and uploads episodes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 conan_uploader.py                     Auto-detect latest episode
  python3 conan_uploader.py --latest            Same as above (explicit)
  python3 conan_uploader.py --episode 1200      Process episode 1200 only
  python3 conan_uploader.py --range 1195 1200   Process episodes 1195 through 1200
  python3 conan_uploader.py --range 1200 1210   Process episodes 1200 through 1210
        """,
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--latest",
        action="store_true",
        default=True,
        help="Auto-calculate and process the latest episode (default)",
    )
    mode.add_argument(
        "--episode",
        type=int,
        metavar="N",
        help="Process a specific episode number (e.g. --episode 1200)",
    )
    mode.add_argument(
        "--range",
        type=int,
        nargs=2,
        metavar=("START", "END"),
        help="Process a range of episodes (e.g. --range 1195 1200)",
    )

    args = parser.parse_args()
    return args


def get_episode_list(args) -> list[int]:
    """
    Convert parsed args into a list of episode numbers to process.

    Args:
        args: Parsed argparse namespace

    Returns:
        List of episode numbers to process.
    """
    if args.episode:
        if args.episode < 1:
            log.error(f"Invalid episode number: {args.episode}")
            sys.exit(1)
        log.info(f"Mode: Single episode — {args.episode}")
        return [args.episode]

    if args.range:
        start, end = args.range
        if start < 1 or end < 1:
            log.error(f"Invalid range: {start}-{end}")
            sys.exit(1)
        if start > end:
            log.error(f"Range start ({start}) is greater than end ({end})")
            sys.exit(1)
        if end - start > 50:
            log.error(f"Range too large: {end - start + 1} episodes. Max is 50.")
            sys.exit(1)
        episodes = list(range(start, end + 1))
        log.info(f"Mode: Range — episodes {start} to {end} ({len(episodes)} total)")
        return episodes

    # Default: auto-calculate latest
    ep = calculate_latest_episode()
    log.info(f"Mode: Auto — latest episode is {ep}")
    return [ep]


# ──────────────────────────────────────────────
# STEP 1: Calculate expected episode number
# ──────────────────────────────────────────────

def calculate_latest_episode() -> int:
    """
    Calculate the latest Detective Conan episode number based on the
    known base reference: Episode 1200 was released on May 2, 2026.

    Detective Conan airs one episode per week on Saturdays.
    We calculate weeks elapsed since the base date and add to the base number.

    Returns:
        Expected latest episode number.
    """
    now = datetime.now(timezone.utc)
    weeks_elapsed = (now - BASE_DATE).days // 7
    latest_ep = BASE_EPISODE + weeks_elapsed
    log.info(f"Base episode: {BASE_EPISODE} (May 2, 2026)")
    log.info(f"Weeks elapsed: {weeks_elapsed}")
    log.info(f"Expected latest episode: {latest_ep}")
    return latest_ep


# ──────────────────────────────────────────────
# STEP 2: Search nyaa.si RSS for the episode
# ──────────────────────────────────────────────

def search_nyaa_rss(episode_num: int) -> dict | None:
    """
    Search nyaa.si RSS feed for Erai-raws Detective Conan at 1080p.

    Uses the nyaa.si RSS endpoint which is more reliable than the JSON API
    (the JSON API is often behind DDoS protection and returns 404).

    The search URL format is:
      https://nyaa.si/?page=rss&u=Erai-raws&q=Detective+Conan+{EP}&c=0_0&f=0

    We then filter the results for the 1080p version.

    Args:
        episode_num: The episode number to search for

    Returns:
        Dict with keys: title, torrent_url, info_hash, size
        or None if nothing found.
    """
    query = f"Detective Conan {episode_num}"
    log.info(f"Searching nyaa.si RSS for: {query} (user: {NYAA_USER}, 1080p)")

    params = {
        "page": "rss",
        "u": NYAA_USER,
        "q": query,
        "c": "0_0",    # All categories
        "f": "0",       # No filter
    }

    try:
        response = requests.get(
            NYAA_BASE_URL,
            params=params,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (ConanUploader/1.0)"},
        )
        response.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Failed to search nyaa.si RSS: {e}")
        return None

    # Parse the RSS XML
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as e:
        log.error(f"Failed to parse RSS XML: {e}")
        return None

    # RSS namespace
    ns = {"nyaa": "https://nyaa.si/xmlns/nyaa"}

    items = root.findall(".//item")
    if not items:
        log.warning(f"No results found on nyaa.si for: {query}")
        return None

    log.info(f"Found {len(items)} result(s) on nyaa.si")

    # Collect all results and pick the best 1080p one
    results = []
    for item in items:
        title_el = item.find("title")
        link_el = item.find("link")
        info_hash_el = item.find("nyaa:infoHash", ns)
        size_el = item.find("nyaa:size", ns)

        if title_el is None or link_el is None:
            continue

        title = title_el.text or ""
        torrent_url = link_el.text or ""
        info_hash = info_hash_el.text if info_hash_el is not None else ""
        size = size_el.text if size_el is not None else ""

        results.append({
            "title": title,
            "torrent_url": torrent_url,
            "info_hash": info_hash,
            "size": size,
        })

        log.info(f"  Found: {title} ({size})")

    # Filter for 1080p
    p1080_results = [r for r in results if "1080p" in r["title"]]
    if not p1080_results:
        log.warning("No 1080p version found, trying 720p fallback...")
        p720_results = [r for r in results if "720p" in r["title"]]
        if p720_results:
            chosen = p720_results[0]
            log.info(f"Using 720p fallback: {chosen['title']}")
            return chosen
        # Last resort: use first result
        if results:
            chosen = results[0]
            log.info(f"Using first available result: {chosen['title']}")
            return chosen
        return None

    # Among 1080p results, prefer HEVC (smaller, more efficient) over AVC
    hevc_results = [r for r in p1080_results if "HEVC" in r["title"]]
    if hevc_results:
        chosen = hevc_results[0]
        log.info(f"Selected 1080p HEVC: {chosen['title']}")
    else:
        chosen = p1080_results[0]
        log.info(f"Selected 1080p: {chosen['title']}")

    return chosen


def build_magnet_uri(info_hash: str, title: str) -> str:
    """
    Build a magnet URI from the info hash and title.

    Includes common public trackers for better connectivity.
    """
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


def extract_episode_number(title: str) -> str:
    """
    Extract the episode number from an Erai-raws release title.

    Handles formats like:
      - [Erai-raws] Detective Conan - 1200 [1080p CR WEBRip HEVC AAC][MultiSub][A3B09911]
      - [Erai-raws] Detective Conan - 1200 [1080p CR WEB-DL AVC AAC][72D96468]

    Returns:
        Episode number as string, or "unknown" if not found.
    """
    # Erai-raws format: "Detective Conan - 1200"
    m = re.search(r"Conan\s*-\s*(\d{3,4})", title, re.IGNORECASE)
    if m:
        return m.group(1)

    # Generic: "Episode XXX"
    m = re.search(r"[Ee]pisode\s*\.?(\d{3,4})", title)
    if m:
        return m.group(1)

    # Generic: "- XXX" before bracket
    m = re.search(r"-\s*(\d{3,4})(?:v\d+)?\s*[\[\(]", title)
    if m:
        return m.group(1)

    log.warning(f"Could not extract episode number from: {title}")
    return "unknown"


# ──────────────────────────────────────────────
# STEP 3: Download the episode
# ──────────────────────────────────────────────

def download_episode(result: dict, ep_num: int = 0) -> Path | None:
    """
    Download the episode using aria2c via torrent URL or magnet link.

    Strategy:
      1. Download the .torrent file from nyaa.si
      2. Use aria2c with the .torrent file
      3. Fall back to magnet link if torrent file download fails

    Args:
        result: nyaa search result dict with keys: title, torrent_url, info_hash, size
        ep_num: Episode number (used for logging/cleanup)

    Returns:
        Path to the downloaded MKV file, or None on failure.
    """
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    torrent_url = result.get("torrent_url", "")
    info_hash = result.get("info_hash", "")
    title = result.get("title", "unknown")

    # --- Try downloading via .torrent file first ---
    if torrent_url:
        full_url = torrent_url if torrent_url.startswith("http") else f"{NYAA_BASE_URL}{torrent_url}"
        log.info(f"Downloading torrent file from: {full_url}")

        try:
            resp = requests.get(full_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()

            safe_name = re.sub(r'[^\w\s\-\.\[\]]', '', title)
            torrent_file = DOWNLOAD_DIR / f"{safe_name}.torrent"
            torrent_file.write_bytes(resp.content)
            log.info(f"Saved torrent file: {torrent_file} ({len(resp.content)} bytes)")

            # Use aria2c with the .torrent file
            mkv_path = _aria2_download(str(torrent_file), is_torrent_file=True)
            if mkv_path:
                return mkv_path
        except Exception as e:
            log.warning(f"Torrent file download failed: {e}")

    # --- Fall back to magnet link ---
    if info_hash:
        magnet_uri = build_magnet_uri(info_hash, title)
        log.info(f"Falling back to magnet link (hash: {info_hash})")
        mkv_path = _aria2_download(magnet_uri, is_torrent_file=False)
        if mkv_path:
            return mkv_path

    log.error("All download methods failed")
    return None


def _aria2_download(source: str, is_torrent_file: bool = False) -> Path | None:
    """
    Download using aria2c with optimized settings for GitHub Actions.

    Args:
        source: Path to .torrent file or magnet URI
        is_torrent_file: True if source is a .torrent file path

    Returns:
        Path to the downloaded MKV file, or None on failure.
    """
    cmd = [
        "aria2c",
        "--dir", str(DOWNLOAD_DIR),
        "--seed-time=0",              # Don't seed after download
        "--max-tries=5",
        "--retry-wait=10",
        "--timeout=60",
        "--connect-timeout=30",
        "--max-download-limit=0",     # No speed limit
        "--split=5",                  # Multiple connections
        "--max-concurrent-downloads=1",
        "--file-allocation=none",     # Faster start, no pre-allocation
        "--summary-interval=30",
        "--bt-max-peers=55",
        "--bt-request-peer-speed-limit=0",
        "--continue=true",
    ]

    if is_torrent_file:
        cmd.extend(["--follow-torrent=mem", source])
    else:
        cmd.extend(["--follow-magnet=true", source])

    log.info(f"Running aria2c...")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour max for torrent download
        )
        log.info(f"aria2c exit code: {result.returncode}")

        if result.returncode != 0:
            log.error(f"aria2c stderr: {result.stderr[-500:]}")

    except subprocess.TimeoutExpired:
        log.error("aria2c timed out after 1 hour")
        return None
    except Exception as e:
        log.error(f"aria2c failed: {e}")
        return None

    # Find the downloaded MKV file
    mkv_files = list(DOWNLOAD_DIR.rglob("*.mkv"))
    if mkv_files:
        # Return the largest MKV (in case there are extras)
        mkv_files.sort(key=lambda f: f.stat().st_size, reverse=True)
        chosen = mkv_files[0]
        log.info(f"Downloaded MKV: {chosen} ({chosen.stat().st_size / 1024 / 1024:.1f} MB)")
        return chosen

    log.error("No MKV file found after download")
    return None


# ──────────────────────────────────────────────
# STEP 4: Extract subtitles from MKV
# ──────────────────────────────────────────────

def get_subtitle_info(mkv_path: Path) -> list[dict]:
    """
    Get information about subtitle tracks in the MKV file using mkvmerge.

    Returns:
        List of dicts with track info: {'id': 0, 'codec': 'S_TEXT/ASS', 'lang': 'eng', ...}
    """
    cmd = ["mkvmerge", "--identify", "--json", str(mkv_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.error(f"mkvmerge failed: {result.stderr}")
            return []

        data = json.loads(result.stdout)
        tracks = data.get("tracks", [])

        subtitle_tracks = []
        for track in tracks:
            if track.get("type") == "subtitles":
                props = track.get("properties", {})
                subtitle_tracks.append({
                    "id": track.get("id", 0),
                    "codec": props.get("codec_id", "unknown"),
                    "language": props.get("language", "und"),
                    "track_name": props.get("track_name", ""),
                    "default": props.get("default_track", False),
                })

        return subtitle_tracks

    except FileNotFoundError:
        log.warning("mkvmerge not found, will use ffmpeg fallback for subtitle info")
        return []
    except Exception as e:
        log.error(f"Failed to get subtitle info: {e}")
        return []


def extract_subtitles(mkv_path: Path, output_dir: Path) -> list[Path]:
    """
    Extract all subtitle tracks from the MKV file.

    Uses mkvextract to pull out each subtitle track as a separate file.
    Falls back to ffmpeg if mkvextract is not available.

    Args:
        mkv_path: Path to the source MKV file
        output_dir: Directory to save extracted subtitle files

    Returns:
        List of paths to extracted subtitle files.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Try mkvextract first
    if shutil.which("mkvextract"):
        sub_tracks = get_subtitle_info(mkv_path)
        if sub_tracks:
            return _extract_subtitles_mkvextract(mkv_path, sub_tracks, output_dir)

    # Fallback to ffmpeg
    log.info("Using ffmpeg fallback for subtitle extraction")
    return _extract_subtitles_ffmpeg(mkv_path, output_dir)


def _extract_subtitles_mkvextract(mkv_path: Path, sub_tracks: list[dict], output_dir: Path) -> list[Path]:
    """Extract subtitles using mkvextract."""
    log.info(f"Found {len(sub_tracks)} subtitle track(s):")
    for t in sub_tracks:
        log.info(f"  Track {t['id']}: {t['codec']} [{t['language']}] - {t.get('track_name', 'unnamed')}")

    extracted_files = []

    for track in sub_tracks:
        track_id = track["id"]
        codec = track["codec"].lower()
        lang = track["language"]

        # Determine file extension based on codec
        if "srt" in codec:
            ext = "srt"
        elif "ass" in codec or "ssa" in codec:
            ext = "ass"
        elif "vobsub" in codec or "dvd" in codec:
            ext = "sub"
        elif "pgs" in codec or "hdmv" in codec:
            ext = "sup"
        else:
            ext = "srt"

        output_file = output_dir / f"subtitle_t{track_id}_{lang}.{ext}"

        cmd = [
            "mkvextract",
            str(mkv_path),
            "tracks",
            f"{track_id}:{str(output_file)}",
        ]

        log.info(f"Extracting track {track_id} to {output_file}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
                extracted_files.append(output_file)
                log.info(f"  Successfully extracted: {output_file}")
            else:
                log.warning(f"  mkvextract failed for track {track_id}: {result.stderr}")
        except Exception as e:
            log.warning(f"  Failed to extract track {track_id}: {e}")

    if not extracted_files:
        log.warning("mkvextract failed for all tracks, trying ffmpeg fallback")
        return _extract_subtitles_ffmpeg(mkv_path, output_dir)

    return extracted_files


def _extract_subtitles_ffmpeg(mkv_path: Path, output_dir: Path) -> list[Path]:
    """
    Fallback: Extract subtitle tracks using ffmpeg.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    extracted_files = []

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "s",
        str(mkv_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
    except Exception as e:
        log.error(f"ffprobe failed: {e}")
        return []

    if not streams:
        log.info("No subtitle streams found by ffprobe")
        return []

    for stream in streams:
        codec = stream.get("codec_name", "srt")
        lang = stream.get("tags", {}).get("language", "und")
        index = stream.get("index", 0)

        ext_map = {"ass": "ass", "ssa": "ass", "srt": "srt", "sub": "sub", "dvd_subtitle": "sub"}
        ext = ext_map.get(codec, "srt")

        output_file = output_dir / f"subtitle_ffmpeg_{index}_{lang}.{ext}"

        cmd = [
            "ffmpeg", "-y",
            "-i", str(mkv_path),
            "-map", f"0:{index}",
            "-f", ext if ext in ("srt", "ass") else "srt",
            str(output_file),
        ]

        log.info(f"Extracting subtitle stream {index} with ffmpeg")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
                extracted_files.append(output_file)
                log.info(f"  Successfully extracted: {output_file}")
            else:
                log.warning(f"  ffmpeg extract failed for stream {index}")
        except Exception as e:
            log.warning(f"  ffmpeg extract error for stream {index}: {e}")

    return extracted_files


# ──────────────────────────────────────────────
# STEP 5: Create Hard-Sub and Soft-Sub versions
# ──────────────────────────────────────────────

def create_softsub(mkv_path: Path, episode_num: str, output_dir: Path) -> Path | None:
    """
    Create a Soft-Sub version of the episode.

    Soft-Sub keeps subtitles as a separate, toggleable track inside
    the MKV container. This is a clean remux with no re-encoding.

    The output file is named: {episode_num}ss.mkv

    Args:
        mkv_path: Path to the source MKV file
        episode_num: Episode number string
        output_dir: Directory to save the output

    Returns:
        Path to the soft-sub MKV, or None on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{episode_num}ss.mkv"

    if output_file.exists():
        log.info(f"Soft-sub file already exists: {output_file}")
        return output_file

    log.info(f"Creating Soft-Sub version: {output_file}")

    # Remux the MKV — copy all streams without re-encoding
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(mkv_path),
        "-map", "0",          # Include all streams
        "-c", "copy",         # Copy all codecs (no re-encoding)
        "-movflags", "+faststart",
        str(output_file),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and output_file.exists():
            size_mb = output_file.stat().st_size / 1024 / 1024
            log.info(f"Soft-Sub created: {output_file} ({size_mb:.1f} MB)")
            return output_file
        else:
            log.error(f"ffmpeg soft-sub failed: {result.stderr[-500:]}")
            return None
    except Exception as e:
        log.error(f"Soft-sub creation error: {e}")
        return None


def create_hardsub(mkv_path: Path, subtitle_files: list[Path], episode_num: str, output_dir: Path) -> Path | None:
    """
    Create a Hard-Sub version of the episode.

    Hard-Sub permanently burns the subtitles into the video frames,
    making them always visible and part of the video itself.

    The output file is named: {episode_num}hs.mkv

    Args:
        mkv_path: Path to the source MKV file
        subtitle_files: List of extracted subtitle file paths
        episode_num: Episode number string
        output_dir: Directory to save the output

    Returns:
        Path to the hard-sub MKV, or None on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{episode_num}hs.mkv"

    if output_file.exists():
        log.info(f"Hard-sub file already exists: {output_file}")
        return output_file

    if not subtitle_files:
        log.error("No subtitle files available for hard-subbing")
        return None

    # Pick the best subtitle file for hard-subbing
    sub_file = _pick_best_subtitle(subtitle_files)
    log.info(f"Using subtitle file for hard-sub: {sub_file}")

    log.info(f"Creating Hard-Sub version: {output_file}")
    log.info("This may take a while — re-encoding video with burned-in subtitles...")

    # Escape the path for ffmpeg's subtitle filter
    sub_path_escaped = (
        str(sub_file)
        .replace("\\", "/")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )

    subtitle_filter = f"subtitles='{sub_path_escaped}'"

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(mkv_path),
        "-vf", subtitle_filter,
        "-c:v", "libx264",
        "-preset", HARDSUB_PRESET,
        "-crf", HARDSUB_CRF,
        "-c:a", HARDSUB_AUDIO_CODEC,
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
            log.info(f"Hard-Sub created: {output_file} ({size_mb:.1f} MB)")
            return output_file
        else:
            log.error(f"ffmpeg hard-sub failed (extracted subtitle): {result.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        log.error("Hard-sub encoding timed out after 2 hours")
        return None
    except Exception as e:
        log.error(f"Hard-sub encoding error: {e}")
        return None

    # Fallback: Try using the embedded subtitle track directly from the MKV
    log.info("Trying fallback: burning subtitles directly from MKV embedded stream...")
    mkv_path_escaped = (
        str(mkv_path)
        .replace("\\", "/")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )

    cmd_fallback = [
        "ffmpeg",
        "-y",
        "-i", str(mkv_path),
        "-vf", f"subtitles='{mkv_path_escaped}'",
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
        result = subprocess.run(cmd_fallback, capture_output=True, text=True, timeout=7200)
        if result.returncode == 0 and output_file.exists():
            size_mb = output_file.stat().st_size / 1024 / 1024
            log.info(f"Hard-Sub (fallback) created: {output_file} ({size_mb:.1f} MB)")
            return output_file
        else:
            log.error(f"ffmpeg hard-sub fallback also failed: {result.stderr[-500:]}")
            return None
    except Exception as e:
        log.error(f"Hard-sub fallback error: {e}")
        return None


def _pick_best_subtitle(subtitle_files: list[Path]) -> Path:
    """
    Pick the best subtitle file for hard-subbing.

    Priority:
      1. ASS format (preserves styling) — English preferred
      2. SRT format — English preferred
      3. First available file
    """
    ass_files = [f for f in subtitle_files if f.suffix == ".ass"]
    srt_files = [f for f in subtitle_files if f.suffix == ".srt"]

    eng_ass = [f for f in ass_files if "eng" in f.name.lower()]
    if eng_ass:
        return eng_ass[0]
    if ass_files:
        return ass_files[0]

    eng_srt = [f for f in srt_files if "eng" in f.name.lower()]
    if eng_srt:
        return eng_srt[0]
    if srt_files:
        return srt_files[0]

    return subtitle_files[0]


# ──────────────────────────────────────────────
# STEP 6: Upload to Doodstream
# ──────────────────────────────────────────────

def upload_to_doodstream(file_path: Path, folder_id: str, title: str = "") -> str | None:
    """
    Upload a video file to Doodstream into the specified folder.

    Doodstream API flow (tested and verified):
      1. GET /upload/server?key=API_KEY — retrieve the upload server URL
      2. POST file to the upload server with:
         - field name: 'file'
         - form data: api_key=API_KEY, fld_id=FOLDER_ID

    Args:
        file_path: Path to the video file to upload
        folder_id: Doodstream folder fld_id to upload into
        title: Optional title for the uploaded file

    Returns:
        Doodstream file code on success, or None on failure.
    """
    log.info(f"Uploading to Doodstream: {file_path.name} → folder {folder_id}")

    # Step 1: Get upload server URL (uses 'key' param for GET requests)
    try:
        resp = requests.get(
            f"{DOODSTREAM_BASE_URL}/upload/server",
            params={"key": DOODSTREAM_API_KEY},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != 200:
            log.error(f"Doodstream get-server failed: {data}")
            return None

        upload_url = data.get("result")
        if not upload_url:
            log.error(f"No upload URL in Doodstream response: {data}")
            return None

        log.info(f"Doodstream upload server: {upload_url}")

    except Exception as e:
        log.error(f"Failed to get Doodstream upload server: {e}")
        return None

    # Step 2: Upload the file (uses 'api_key' param for POST requests)
    try:
        file_size_mb = file_path.stat().st_size / 1024 / 1024
        log.info(f"Uploading {file_size_mb:.1f} MB to Doodstream...")

        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "video/x-matroska")}
            data = {
                "api_key": DOODSTREAM_API_KEY,
                "fld_id": folder_id,
            }
            if title:
                data["title"] = title

            resp = requests.post(
                upload_url,
                files=files,
                data=data,
                timeout=1800,
            )
            resp.raise_for_status()

            try:
                result = resp.json()
            except ValueError:
                log.warning("Doodstream returned non-JSON response, parsing HTML...")
                match = re.search(r'"filecode"\s*:\s*"([^"]+)"', resp.text)
                if match:
                    file_code = match.group(1)
                    log.info(f"Doodstream upload successful! File code: {file_code}")
                    return file_code
                log.error(f"Doodstream upload HTML response: {resp.text[:500]}")
                return None

        if result.get("status") == 200:
            result_data = result.get("result", [])
            if isinstance(result_data, list) and result_data:
                file_code = result_data[0].get("filecode", "")
            elif isinstance(result_data, dict):
                file_code = result_data.get("filecode", "")
            else:
                file_code = str(result_data) if result_data else ""

            if file_code:
                log.info(f"Doodstream upload successful! File code: {file_code}")
                return file_code
            else:
                log.error(f"Doodstream upload returned no file code: {result}")
                return None
        else:
            log.error(f"Doodstream upload failed: {result}")
            return None

    except requests.Timeout:
        log.error("Doodstream upload timed out (30 min)")
        return None
    except Exception as e:
        log.error(f"Doodstream upload error: {e}")
        return None


# ──────────────────────────────────────────────
# STEP 7: Upload to StreamP2P (TUS protocol)
# ──────────────────────────────────────────────

def _streamp2p_v1_headers() -> dict:
    """Return common headers for StreamP2P v1 API requests."""
    return {"api-token": STREAMP2P_API_KEY, "Content-Type": "application/json"}


def _streamp2p_ensure_folders():
    """
    Ensure the folder structure exists on StreamP2P.

    Creates if missing:
      - Soft Sub / Original Episodes
      - Hard Sub / Original Episodes

    Returns updated folder IDs from the server.
    """
    headers = _streamp2p_v1_headers()

    try:
        resp = requests.get(
            f"{STREAMP2P_V1_BASE_URL}/video/folder",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        folders = resp.json()
    except Exception as e:
        log.warning(f"Failed to list StreamP2P folders: {e}")
        return

    # Build a map of folder name -> folder for root-level folders
    root_folders = {f["name"]: f for f in folders if not f.get("parentId")}

    softsub_root = root_folders.get("Soft Sub")
    hardsub_root = root_folders.get("Hard Sub")

    # Create Soft Sub root if missing
    if not softsub_root:
        log.info("Creating StreamP2P folder: Soft Sub")
        try:
            resp = requests.post(
                f"{STREAMP2P_V1_BASE_URL}/video/folder",
                headers=headers,
                json={"name": "Soft Sub"},
                timeout=15,
            )
            if resp.status_code == 201:
                softsub_root = {"id": resp.json().get("id"), "name": "Soft Sub"}
                log.info(f"  Created Soft Sub folder: {softsub_root['id']}")
        except Exception as e:
            log.error(f"Failed to create Soft Sub folder: {e}")

    # Create Hard Sub root if missing
    if not hardsub_root:
        log.info("Creating StreamP2P folder: Hard Sub")
        try:
            resp = requests.post(
                f"{STREAMP2P_V1_BASE_URL}/video/folder",
                headers=headers,
                json={"name": "Hard Sub"},
                timeout=15,
            )
            if resp.status_code == 201:
                hardsub_root = {"id": resp.json().get("id"), "name": "Hard Sub"}
                log.info(f"  Created Hard Sub folder: {hardsub_root['id']}")
        except Exception as e:
            log.error(f"Failed to create Hard Sub folder: {e}")

    # Create Original Episodes subfolder inside each root
    for parent, label in [(softsub_root, "Soft Sub"), (hardsub_root, "Hard Sub")]:
        if not parent:
            continue
        parent_id = parent["id"]

        # Check if "Original Episodes" subfolder already exists
        subfolder_exists = any(
            f.get("parentId") == parent_id and f.get("name") == "Original Episodes"
            for f in folders
        )

        if not subfolder_exists:
            log.info(f"Creating StreamP2P subfolder: {label} / Original Episodes")
            try:
                resp = requests.post(
                    f"{STREAMP2P_V1_BASE_URL}/video/folder",
                    headers=headers,
                    json={"name": "Original Episodes", "folderId": parent_id},
                    timeout=15,
                )
                if resp.status_code == 201:
                    log.info(f"  Created subfolder: {resp.json().get('id')}")
            except Exception as e:
                log.error(f"Failed to create Original Episodes subfolder: {e}")


def upload_to_streamp2p(file_path: Path, folder_id: str, title: str = "") -> str | None:
    """
    Upload a video file to StreamP2P using the TUS protocol.

    StreamP2P uses TUS (tus.io) for resumable uploads. The flow is:
      1. GET /api/v1/video/upload — get TUS endpoint + access token
      2. POST to TUS URL — create upload session with metadata
      3. PATCH to TUS URL — upload file data in chunks (50 MB each)
      4. POST /api/v1/video/folder/{id}/link — link video to folder

    The API key is passed as the 'api-token' header for v1 endpoints.

    Args:
        file_path: Path to the video file to upload
        folder_id: StreamP2P folder ID to place the video in
        title: Optional title for the uploaded file

    Returns:
        StreamP2P video ID on success, or None on failure.
    """
    log.info(f"Uploading to StreamP2P: {file_path.name} → folder {folder_id}")

    file_size = file_path.stat().st_size
    file_size_mb = file_size / 1024 / 1024
    v1_headers = _streamp2p_v1_headers()

    # ── Step 1: Get TUS upload endpoint and access token ──
    try:
        resp = requests.get(
            f"{STREAMP2P_V1_BASE_URL}/video/upload",
            headers=v1_headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        tus_url = data.get("tusUrl")
        access_token = data.get("accessToken")

        if not tus_url or not access_token:
            log.error(f"Missing TUS URL or access token: {data}")
            return None

        log.info(f"StreamP2P TUS endpoint: {tus_url}")

    except Exception as e:
        log.error(f"Failed to get StreamP2P upload endpoint: {e}")
        return None

    # ── Step 2: Create TUS upload session ──
    filename = title if title else file_path.name
    filename_b64 = base64.b64encode(filename.encode()).decode()
    # StreamP2P requires "video/mp4" as filetype in metadata even for MKV files
    # The server detects and handles the actual container format during transcoding
    filetype_b64 = base64.b64encode(b"video/mp4").decode()
    token_b64 = base64.b64encode(access_token.encode()).decode()
    folder_b64 = base64.b64encode(folder_id.encode()).decode()

    metadata = (
        f"filename {filename_b64},"
        f"filetype {filetype_b64},"
        f"accessToken {token_b64},"
        f"folderId {folder_b64}"
    )

    tus_create_headers = {
        "Tus-Resumable": "1.0.0",
        "Upload-Length": str(file_size),
        "Upload-Metadata": metadata,
    }

    try:
        resp = requests.post(tus_url, headers=tus_create_headers, timeout=30)
        if resp.status_code != 201:
            log.error(f"TUS create failed (status {resp.status_code}): {resp.text[:300]}")
            return None

        upload_url = resp.headers.get("Location")
        if not upload_url:
            log.error("No Location header in TUS create response")
            return None

        log.info(f"TUS upload session created: {upload_url}")

    except Exception as e:
        log.error(f"TUS create request failed: {e}")
        return None

    # ── Step 3: Upload file data in chunks via PATCH ──
    video_id = None  # Will be set from PATCH response or search
    try:
        log.info(f"Uploading {file_size_mb:.1f} MB to StreamP2P (50 MB chunks)...")

        offset = 0
        with open(file_path, "rb") as f:
            while offset < file_size:
                chunk = f.read(STREAMP2P_TUS_CHUNK_SIZE)
                chunk_size = len(chunk)

                patch_headers = {
                    "Tus-Resumable": "1.0.0",
                    "Upload-Offset": str(offset),
                    "Content-Type": "application/offset+octet-stream",
                }

                resp = requests.patch(
                    upload_url,
                    headers=patch_headers,
                    data=chunk,
                    timeout=1800,
                )

                if resp.status_code not in (200, 204):
                    log.error(
                        f"TUS PATCH failed at offset {offset} (status {resp.status_code}): "
                        f"{resp.text[:300]}"
                    )
                    return None

                # StreamP2P returns videoId in the final PATCH response body
                if resp.text and "videoId" in resp.text:
                    try:
                        patch_data = resp.json()
                        video_id = patch_data.get("videoId")
                        if video_id:
                            log.info(f"Video ID received from upload: {video_id}")
                    except ValueError:
                        pass

                offset += chunk_size
                progress = (offset / file_size) * 100
                log.info(
                    f"  Uploaded {offset / 1024 / 1024:.1f}/{file_size_mb:.1f} MB "
                    f"({progress:.1f}%)"
                )

        log.info(f"TUS upload complete — {file_size_mb:.1f} MB transferred")

    except requests.Timeout:
        log.error("StreamP2P upload timed out (30 min)")
        return None
    except Exception as e:
        log.error(f"StreamP2P TUS upload error: {e}")
        return None

    # ── Step 4: Link the uploaded video to the folder ──
    # The videoId may have been returned in the PATCH response, or we
    # search for it via the v1 API. Then we link it to the correct folder.
    # (The folderId in TUS metadata should auto-assign, but we link
    # explicitly as a safety measure.)

    # If we didn't get videoId from PATCH, try searching
    if not video_id:
        # Wait a moment for processing
        time.sleep(3)

        try:
            resp = requests.get(
                f"{STREAMP2P_V1_BASE_URL}/video/manage",
                headers=v1_headers,
                params={"perPage": 5, "search": filename},
                timeout=15,
            )
            if resp.status_code == 200:
                videos = resp.json().get("data", [])
                for v in videos:
                    if v.get("name") == filename:
                        video_id = v.get("id")
                        break

        except Exception as e:
            log.warning(f"Failed to search for uploaded video: {e}")

    # If we found the video, link it to the folder
    if video_id:
        log.info(f"Found uploaded video: {video_id}")
        try:
            resp = requests.post(
                f"{STREAMP2P_V1_BASE_URL}/video/folder/{folder_id}/link",
                headers=v1_headers,
                json={"videoId": video_id},
                timeout=15,
            )
            if resp.status_code == 204:
                log.info(f"Linked video {video_id} to folder {folder_id}")
            else:
                log.warning(f"Failed to link video to folder (status {resp.status_code})")
        except Exception as e:
            log.warning(f"Failed to link video to folder: {e}")

        log.info(f"StreamP2P upload successful! Video ID: {video_id}")
        return video_id
    else:
        log.info("StreamP2P upload completed (video processing, ID not yet available)")
        return "uploaded_processing"


# ──────────────────────────────────────────────
# UTILITY FUNCTIONS
# ──────────────────────────────────────────────

def setup_work_dirs():
    """Create and clean working directories."""
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"Working directory: {WORK_DIR}")


def cleanup_work_dirs():
    """Remove working directories to free space."""
    try:
        shutil.rmtree(WORK_DIR)
        log.info("Cleaned up working directories")
    except Exception as e:
        log.warning(f"Cleanup failed: {e}")


def check_dependencies():
    """Verify that all required external tools are available."""
    required = ["ffmpeg", "ffprobe", "aria2c"]
    optional = ["mkvmerge", "mkvextract"]
    missing = []
    missing_optional = []

    for tool in required:
        if not shutil.which(tool):
            missing.append(tool)

    for tool in optional:
        if not shutil.which(tool):
            missing_optional.append(tool)

    if missing:
        log.error(f"Missing required tools: {', '.join(missing)}")
        log.error("Install them with: sudo apt-get install ffmpeg aria2")
        sys.exit(1)

    if missing_optional:
        log.warning(f"Missing optional tools: {', '.join(missing_optional)}")
        log.warning("Subtitle extraction will use ffmpeg fallback (may be less precise)")
        log.warning("Install with: sudo apt-get install mkvtoolnix")

    log.info("All required dependencies satisfied")


# ──────────────────────────────────────────────
# PER-EPISODE PROCESSING
# ──────────────────────────────────────────────

def process_episode(episode_num: int) -> dict:
    """
    Process a single episode: search, download, extract, encode, upload.

    Args:
        episode_num: The episode number to process

    Returns:
        Dict with upload results and status for this episode.
    """
    log.info(f"\n{'=' * 50}")
    log.info(f"PROCESSING EPISODE {episode_num}")
    log.info(f"{'=' * 50}")

    results = {
        "episode": episode_num,
        "status": "pending",
        "doodstream_softsub": None,
        "doodstream_hardsub": None,
        "streamp2p_softsub": None,
        "streamp2p_hardsub": None,
        "error": None,
    }

    # ── Search nyaa.si ──
    log.info(f"[EP {episode_num}] Searching nyaa.si...")
    result = search_nyaa_rss(episode_num)

    # Fallback: try neighboring episodes
    if not result:
        log.warning(f"[EP {episode_num}] Not found, trying {episode_num - 1}...")
        result = search_nyaa_rss(episode_num - 1)
    if not result:
        log.warning(f"[EP {episode_num}] Not found, trying {episode_num + 1}...")
        result = search_nyaa_rss(episode_num + 1)

    if not result:
        results["status"] = "not_found"
        results["error"] = f"Episode {episode_num} not found on nyaa.si"
        log.error(results["error"])
        return results

    title = result.get("title", "unknown")
    ep_num_str = extract_episode_number(title)
    log.info(f"[EP {episode_num}] Found: {title}")
    log.info(f"[EP {episode_num}] Size: {result.get('size', 'unknown')}")

    # ── Download ──
    log.info(f"[EP {episode_num}] Downloading...")
    mkv_path = download_episode(result, episode_num)
    if not mkv_path:
        results["status"] = "download_failed"
        results["error"] = f"Download failed for episode {episode_num}"
        log.error(results["error"])
        return results

    # ── Extract subtitles ──
    log.info(f"[EP {episode_num}] Extracting subtitles...")
    subtitle_dir = WORK_DIR / f"subtitles_{episode_num}"
    subtitle_files = extract_subtitles(mkv_path, subtitle_dir)
    if not subtitle_files:
        log.warning(f"[EP {episode_num}] No subtitles extracted — hard-sub will be skipped")
    else:
        log.info(f"[EP {episode_num}] Extracted {len(subtitle_files)} subtitle track(s)")

    # ── Create Soft-Sub ──
    log.info(f"[EP {episode_num}] Creating Soft-Sub...")
    ep_output_dir = OUTPUT_DIR / f"ep{episode_num}"
    softsub_path = create_softsub(mkv_path, ep_num_str, ep_output_dir)
    if not softsub_path:
        results["status"] = "softsub_failed"
        results["error"] = f"Soft-Sub creation failed for episode {episode_num}"
        log.error(results["error"])
        return results

    # ── Create Hard-Sub ──
    log.info(f"[EP {episode_num}] Creating Hard-Sub...")
    hardsub_path = None
    if subtitle_files:
        hardsub_path = create_hardsub(mkv_path, subtitle_files, ep_num_str, ep_output_dir)
        if not hardsub_path:
            log.warning(f"[EP {episode_num}] Hard-Sub creation failed — skipping hard-sub uploads")

    # ── Upload to Doodstream ──
    ss_title = f"Detective Conan - {ep_num_str} SS"
    hs_title = f"Detective Conan - {ep_num_str} HS"

    if softsub_path:
        log.info(f"[EP {episode_num}] Uploading Soft-Sub to Doodstream...")
        results["doodstream_softsub"] = upload_to_doodstream(
            softsub_path, DOODSTREAM_FOLDER_SOFTSUB, title=ss_title
        )

    if hardsub_path:
        log.info(f"[EP {episode_num}] Uploading Hard-Sub to Doodstream...")
        results["doodstream_hardsub"] = upload_to_doodstream(
            hardsub_path, DOODSTREAM_FOLDER_HARDSUB, title=hs_title
        )

    # ── Upload to StreamP2P ──
    if softsub_path:
        log.info(f"[EP {episode_num}] Uploading Soft-Sub to StreamP2P...")
        results["streamp2p_softsub"] = upload_to_streamp2p(
            softsub_path, folder_id=STREAMP2P_FOLDER_SOFTSUB, title=ss_title
        )

    if hardsub_path:
        log.info(f"[EP {episode_num}] Uploading Hard-Sub to StreamP2P...")
        results["streamp2p_hardsub"] = upload_to_streamp2p(
            hardsub_path, folder_id=STREAMP2P_FOLDER_HARDSUB, title=hs_title
        )

    # ── Determine overall status ──
    upload_fields = ["doodstream_softsub", "doodstream_hardsub", "streamp2p_softsub", "streamp2p_hardsub"]
    if hardsub_path is None:
        # Only count soft-sub uploads if no hard-sub was created
        upload_fields = ["doodstream_softsub", "streamp2p_softsub"]

    all_ok = all(results[f] is not None for f in upload_fields)
    results["status"] = "success" if all_ok else "partial"

    # Clean up downloaded MKV to free disk space for next episode
    try:
        if mkv_path.exists():
            mkv_path.unlink()
            log.info(f"[EP {episode_num}] Cleaned up download")
    except Exception:
        pass

    return results


# ──────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ──────────────────────────────────────────────

def main():
    """
    Main entry point — orchestrates the entire workflow.

    Supports three modes via command-line arguments:
      - Auto (default): Calculate and process the latest episode
      - Single: Process a specific episode (--episode N)
      - Range: Process a range of episodes (--range START END)
    """
    log.info("=" * 60)
    log.info("Detective Conan Auto-Uploader — Starting")
    log.info("=" * 60)

    # Parse arguments
    args = parse_args()

    # Pre-flight checks
    check_dependencies()

    # Get list of episodes to process
    episodes = get_episode_list(args)
    log.info(f"Will process {len(episodes)} episode(s): {episodes}")

    # Setup working directories
    setup_work_dirs()

    # Ensure StreamP2P folder structure exists
    _streamp2p_ensure_folders()

    # Process each episode
    all_results = []
    for i, ep in enumerate(episodes, 1):
        if len(episodes) > 1:
            log.info(f"\n{'#' * 60}")
            log.info(f"EPISODE {i}/{len(episodes)}: {ep}")
            log.info(f"{'#' * 60}")

        result = process_episode(ep)
        all_results.append(result)

        # Re-setup work dirs between episodes to clean up
        if i < len(episodes):
            setup_work_dirs()

    # ── Final Summary ──
    log.info("\n" + "=" * 60)
    log.info("FINAL SUMMARY")
    log.info("=" * 60)

    success_count = 0
    for r in all_results:
        ep = r["episode"]
        status = r["status"]
        log.info(f"  Episode {ep}: {status}")

        if status == "success":
            success_count += 1
            for field in ["doodstream_softsub", "doodstream_hardsub", "streamp2p_softsub", "streamp2p_hardsub"]:
                val = r.get(field)
                if val:
                    log.info(f"    {field}: {val}")
        elif status == "partial":
            success_count += 1
            for field in ["doodstream_softsub", "doodstream_hardsub", "streamp2p_softsub", "streamp2p_hardsub"]:
                val = r.get(field)
                marker = "✓" if val else "✗"
                log.info(f"    {marker} {field}: {val if val else 'FAILED'}")
        else:
            log.info(f"    Error: {r.get('error', 'unknown')}")

    log.info("")
    log.info(f"Result: {success_count}/{len(all_results)} episode(s) processed successfully")
    log.info("=" * 60)

    # Cleanup
    cleanup_work_dirs()

    # Exit code
    if success_count < len(all_results):
        log.warning("Some episodes failed — check logs above")
        sys.exit(1)

    log.info("All episodes completed successfully!")


if __name__ == "__main__":
    main()
