#!/usr/bin/env python3
"""Twitch VOD product mention scanner."""

import os
import sys
import json
import tempfile
import argparse
import subprocess
from pathlib import Path

import requests
import whisper
from rapidfuzz import fuzz
from dotenv import load_dotenv

load_dotenv()

TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")

KEYWORDS = [
    # Specific products
    "flick fire",
    "flick edge",
    "flick stratus",
    "npen",
    # Mousepad / mouse references
    "new mousepad",
    "new mouse",
    "mouse glide",
    "good glide",
    "nice glide",
    "glides smooth",
    # Aim / feel phrases
    "feels smooth",
    "so smooth",
    "smooth aim",
    "aim feels",
    "tracking feels",
    "control feels",
]

FUZZY_THRESHOLD = 80
WINDOW_SECONDS = 30
DEDUP_WINDOW_SECONDS = 5


def get_twitch_token():
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_user_id(token, username):
    resp = requests.get(
        "https://api.twitch.tv/helix/users",
        headers={
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}",
        },
        params={"login": username},
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    if not data:
        raise ValueError(f"User '{username}' not found on Twitch")
    return data[0]["id"]


def get_vod_by_id(token, vod_id):
    resp = requests.get(
        "https://api.twitch.tv/helix/videos",
        headers={
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}",
        },
        params={"id": vod_id},
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    if not data:
        raise ValueError(f"VOD '{vod_id}' not found")
    return data


def get_vods(token, user_id, limit):
    vods = []
    cursor = None
    while len(vods) < limit:
        params = {
            "user_id": user_id,
            "type": "archive",
            "first": min(100, limit - len(vods)),
        }
        if cursor:
            params["after"] = cursor
        resp = requests.get(
            "https://api.twitch.tv/helix/videos",
            headers={
                "Client-Id": TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {token}",
            },
            params=params,
        )
        resp.raise_for_status()
        body = resp.json()
        vods.extend(body["data"])
        cursor = body.get("pagination", {}).get("cursor")
        if not cursor or not body["data"]:
            break
    return vods[:limit]


def download_audio(vod_id, output_dir):
    url = f"https://www.twitch.tv/videos/{vod_id}"
    output_path = Path(output_dir) / f"{vod_id}.mp3"
    result = subprocess.run(
        [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "5",
            "--no-playlist",
            "-o", str(output_path),
            url,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed:\n{result.stderr.strip()}")
    return output_path


def transcribe_audio(audio_path, model):
    result = model.transcribe(str(audio_path), word_timestamps=True, verbose=False)
    words = []
    for segment in result["segments"]:
        for word_info in segment.get("words", []):
            words.append({
                "word": word_info["word"].strip().lower(),
                "start": word_info["start"],
                "end": word_info["end"],
            })
    return words


def format_timestamp(seconds):
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def find_mentions(words, threshold):
    raw = []
    n = len(words)

    for i in range(n):
        for product in KEYWORDS:
            product_parts = product.split()
            span = len(product_parts)
            if i + span > n:
                continue
            candidate_words = words[i : i + span]
            candidate = " ".join(w["word"] for w in candidate_words)
            score = fuzz.ratio(candidate, product)
            if score >= threshold:
                raw.append({
                    "product": product,
                    "timestamp_seconds": candidate_words[0]["start"],
                    "timestamp_formatted": format_timestamp(candidate_words[0]["start"]),
                    "fuzzy_score": score,
                    "matched_text": candidate,
                })

    # Deduplicate: for the same product within DEDUP_WINDOW_SECONDS, keep highest score.
    raw.sort(key=lambda m: (-m["fuzzy_score"], m["timestamp_seconds"]))
    seen = set()
    deduped = []
    for mention in raw:
        bucket = (mention["product"], int(mention["timestamp_seconds"] / DEDUP_WINDOW_SECONDS))
        if bucket not in seen:
            seen.add(bucket)
            deduped.append(mention)

    deduped.sort(key=lambda m: m["timestamp_seconds"])
    return deduped


def extract_window(words, timestamp_seconds, window_seconds=WINDOW_SECONDS):
    half = window_seconds / 2
    lo = timestamp_seconds - half
    hi = timestamp_seconds + half
    window_words = [w["word"] for w in words if lo <= w["start"] <= hi]
    return " ".join(window_words)


def main():
    parser = argparse.ArgumentParser(
        description="Scan Twitch VODs for product mentions using Whisper + fuzzy matching."
    )
    parser.add_argument("username", help="Twitch username to scan")
    parser.add_argument(
        "--vods", type=int, default=5, metavar="N",
        help="Number of recent VODs to scan (default: 5)",
    )
    parser.add_argument(
        "--model",
        default="base",
        choices=["tiny", "base", "small", "medium", "large"],
        help="Whisper model size (default: base)",
    )
    parser.add_argument(
        "--output", default="mentions.json",
        help="Output JSON file path (default: mentions.json)",
    )
    parser.add_argument(
        "--threshold", type=int, default=FUZZY_THRESHOLD, metavar="0-100",
        help=f"Fuzzy match threshold (default: {FUZZY_THRESHOLD})",
    )
    parser.add_argument(
        "--vod-id", metavar="VOD_ID",
        help="Scan a specific VOD ID instead of fetching recent VODs",
    )
    parser.add_argument(
        "--keep-audio", action="store_true",
        help="Keep downloaded audio files in ./audio/",
    )
    args = parser.parse_args()

    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        print(
            "Error: TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET environment variables must be set.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Authenticating with Twitch...")
    token = get_twitch_token()

    if args.vod_id:
        print(f"Fetching metadata for VOD {args.vod_id}...")
        vods = get_vod_by_id(token, args.vod_id)
    else:
        print(f"Looking up Twitch user: {args.username}")
        user_id = get_user_id(token, args.username)
        print(f"Fetching {args.vods} most recent VOD(s)...")
        vods = get_vods(token, user_id, limit=args.vods)

    if not vods:
        print("No VODs found.")
        sys.exit(0)
    print(f"Found {len(vods)} VOD(s).")

    print(f"Loading Whisper model '{args.model}'...")
    whisper_model = whisper.load_model(args.model)

    all_results = []

    audio_dir_ctx = (
        _keep_audio_dir() if args.keep_audio else tempfile.TemporaryDirectory()
    )

    with audio_dir_ctx as audio_dir:
        for idx, vod in enumerate(vods, 1):
            vod_id = vod["id"]
            vod_title = vod["title"]
            created_at = vod["created_at"]
            print(f"\n[{idx}/{len(vods)}] {vod_title}")
            print(f"  VOD ID: {vod_id}  |  Created: {created_at}")

            try:
                print("  Downloading audio...")
                audio_path = download_audio(vod_id, audio_dir)

                print("  Transcribing with Whisper (this may take a while)...")
                words = transcribe_audio(audio_path, whisper_model)
                print(f"  Transcribed {len(words)} words.")

                print("  Searching for product mentions...")
                mentions = find_mentions(words, threshold=args.threshold)

                if mentions:
                    print(f"  Found {len(mentions)} mention(s).")
                    for mention in mentions:
                        context = extract_window(words, mention["timestamp_seconds"])
                        all_results.append({
                            "vod_id": vod_id,
                            "vod_title": vod_title,
                            "vod_created_at": created_at,
                            "timestamp_seconds": round(mention["timestamp_seconds"], 2),
                            "timestamp_formatted": mention["timestamp_formatted"],
                            "keyword_matched": mention["product"],
                            "matched_text": mention["matched_text"],
                            "fuzzy_score": mention["fuzzy_score"],
                            "context_window": context,
                        })
                else:
                    print("  No mentions found.")

                if not args.keep_audio:
                    audio_path.unlink(missing_ok=True)

            except Exception as exc:
                print(f"  Error: {exc}", file=sys.stderr)
                continue

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(all_results, indent=2))

    print(f"\nDone. {len(all_results)} mention(s) found across {len(vods)} VOD(s).")
    print(f"Results saved to: {output_path}")


class _keep_audio_dir:
    """Context manager that yields a persistent ./audio/ directory."""

    def __enter__(self):
        self.path = Path("audio")
        self.path.mkdir(exist_ok=True)
        return str(self.path)

    def __exit__(self, *_):
        pass


if __name__ == "__main__":
    main()
