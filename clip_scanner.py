#!/usr/bin/env python3
"""Twitch VOD product mention scanner."""

import os
import sys
import json
import tempfile
import argparse
import time
import datetime
import subprocess
from pathlib import Path

import requests
import whisper
from rapidfuzz import fuzz
from dotenv import load_dotenv

load_dotenv()

TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")

FUZZY_THRESHOLD = 80
WINDOW_SECONDS = 30

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config():
    if not CONFIG_PATH.exists():
        return {"streamers": [], "keywords": [], "fuzzy_threshold": FUZZY_THRESHOLD, "processed_vods": {}}
    return json.loads(CONFIG_PATH.read_text())


def save_config(config):
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


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


def get_live_stream(token, user_id):
    resp = requests.get(
        "https://api.twitch.tv/helix/streams",
        headers={
            "Client-Id": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}",
        },
        params={"user_id": user_id},
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return data[0] if data else None


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
            "--progress",
            "-o", str(output_path),
            url,
        ],
        stderr=subprocess.PIPE,
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


def find_mentions(words, keywords, threshold):
    raw = []
    n = len(words)

    for i in range(n):
        for product in keywords:
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


def format_vod_url(vod_id, seconds):
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    t = f"{hours}h{minutes}m{secs}s" if hours else f"{minutes}m{secs}s"
    return f"https://www.twitch.tv/videos/{vod_id}?t={t}"


def post_discord_notification(webhook_url, streamer, vod_id, vod_title, mention, context):
    vod_url = format_vod_url(vod_id, mention["timestamp_seconds"])
    payload = {
        "embeds": [{
            "title": f"Product mention: {mention['product']}",
            "url": vod_url,
            "description": f'"{context}"',
            "color": 0x9146FF,
            "fields": [
                {"name": "Streamer", "value": streamer or "unknown", "inline": True},
                {"name": "Timestamp", "value": f"[{mention['timestamp_formatted']}]({vod_url})", "inline": True},
                {"name": "Match", "value": f"`{mention['matched_text']}` ({mention['fuzzy_score']}%)", "inline": True},
                {"name": "VOD", "value": f"[{vod_title}]({vod_url})", "inline": False},
            ],
        }]
    }
    while True:
        resp = requests.post(webhook_url, json=payload)
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1)
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        break


def scan_once(args, token, whisper_model):
    config = load_config()
    keywords = config.get("keywords", [])
    threshold = args.threshold if args.threshold is not None else config.get("fuzzy_threshold", FUZZY_THRESHOLD)
    processed_vods = config.get("processed_vods", {})
    discord_webhook_url = config.get("discord_webhook_url")

    if not keywords:
        print("Warning: no keywords configured in config.json.", file=sys.stderr)

    if args.vod_id:
        usernames_to_scan = [None]
    elif args.username:
        usernames_to_scan = [args.username]
    else:
        usernames_to_scan = config.get("streamers", [])
        if not usernames_to_scan:
            print(
                "Error: no username given and no streamers configured in config.json.",
                file=sys.stderr,
            )
            return [], 0

    all_results = []
    total_vods_scanned = 0

    audio_dir_ctx = (
        _keep_audio_dir() if args.keep_audio else tempfile.TemporaryDirectory()
    )

    with audio_dir_ctx as audio_dir:
        for username in usernames_to_scan:
            if args.vod_id:
                print(f"\nFetching metadata for VOD {args.vod_id}...")
                vods = get_vod_by_id(token, args.vod_id)
            else:
                print(f"\nLooking up Twitch user: {username}")
                user_id = get_user_id(token, username)

                live_stream = get_live_stream(token, user_id)
                fetch_limit = args.vods + 1 if live_stream else args.vods
                print(f"Fetching {args.vods} most recent VOD(s)...")
                vods = get_vods(token, user_id, limit=fetch_limit)

                if live_stream and vods:
                    started_at = live_stream["started_at"]
                    in_progress = next(
                        (v for v in vods if v["created_at"] == started_at), vods[0]
                    )
                    print(
                        f"  {username} is currently live (since {started_at}); "
                        f"skipping in-progress VOD {in_progress['id']}."
                    )
                    vods = [v for v in vods if v["id"] != in_progress["id"]][:args.vods]

            if not vods:
                print(f"  No VODs found.")
                continue

            unprocessed = [v for v in vods if v["id"] not in processed_vods]
            skipped = len(vods) - len(unprocessed)
            if skipped:
                print(f"  Skipping {skipped} already-processed VOD(s).")
            if not unprocessed:
                print("  All VODs already processed.")
                continue
            print(f"  {len(unprocessed)} VOD(s) to scan.")

            for idx, vod in enumerate(unprocessed, 1):
                vod_id = vod["id"]
                vod_title = vod["title"]
                created_at = vod["created_at"]
                streamer_name = username or vod.get("user_login") or vod.get("user_name") or "unknown"
                print(f"\n  [{idx}/{len(unprocessed)}] {vod_title}")
                print(f"    VOD ID: {vod_id}  |  Created: {created_at}")

                try:
                    print("    Downloading audio...")
                    audio_path = download_audio(vod_id, audio_dir)

                    print("    Transcribing with Whisper (this may take a while)...")
                    words = transcribe_audio(audio_path, whisper_model)
                    print(f"    Transcribed {len(words)} words.")

                    print("    Searching for product mentions...")
                    mentions = find_mentions(words, keywords, threshold=threshold)

                    if mentions:
                        print(f"    Found {len(mentions)} mention(s).")
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
                            if discord_webhook_url:
                                try:
                                    post_discord_notification(
                                        discord_webhook_url, streamer_name, vod_id, vod_title, mention, context
                                    )
                                except Exception as exc:
                                    print(f"    Discord notification failed: {exc}", file=sys.stderr)
                    else:
                        print("    No mentions found.")

                    processed_vods[vod_id] = {
                        "streamer": streamer_name,
                        "title": vod_title,
                        "processed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    }
                    config["processed_vods"] = processed_vods
                    save_config(config)
                    total_vods_scanned += 1

                    if not args.keep_audio:
                        audio_path.unlink(missing_ok=True)

                except Exception as exc:
                    print(f"    Error: {exc}", file=sys.stderr)
                    continue

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(all_results, indent=2))

    print(f"\nDone. {len(all_results)} mention(s) found across {total_vods_scanned} VOD(s).")
    print(f"Results saved to: {output_path}")

    return all_results, total_vods_scanned


def main():
    parser = argparse.ArgumentParser(
        description="Scan Twitch VODs for product mentions using Whisper + fuzzy matching."
    )
    parser.add_argument(
        "username", nargs="?",
        help="Twitch username to scan (omit to scan all streamers in config.json)",
    )
    parser.add_argument(
        "--vods", type=int, default=5, metavar="N",
        help="Number of recent VODs to scan per streamer (default: 5)",
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
        "--threshold", type=int, default=None, metavar="0-100",
        help="Fuzzy match threshold (default: fuzzy_threshold in config.json)",
    )
    parser.add_argument(
        "--vod-id", metavar="VOD_ID",
        help="Scan a specific VOD ID instead of fetching recent VODs",
    )
    parser.add_argument(
        "--keep-audio", action="store_true",
        help="Keep downloaded audio files in ./audio/",
    )
    parser.add_argument(
        "--schedule", type=float, nargs="?", const=24.0, default=None, metavar="HOURS",
        help="Run continuously, rescanning the configured streamers every HOURS hours "
             "(default: 24). Pass a value like '--schedule 0.5' for 30 minutes.",
    )
    args = parser.parse_args()

    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        print(
            "Error: TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET environment variables must be set.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.schedule is not None and args.vod_id:
        print("Error: --schedule cannot be combined with --vod-id.", file=sys.stderr)
        sys.exit(1)

    if args.schedule is not None and args.schedule <= 0:
        print("Error: --schedule must be a positive number of hours.", file=sys.stderr)
        sys.exit(1)

    print("Authenticating with Twitch...")
    token = get_twitch_token()

    print(f"Loading Whisper model '{args.model}'...")
    whisper_model = whisper.load_model(args.model)

    if args.schedule is None:
        scan_once(args, token, whisper_model)
        return

    interval_seconds = args.schedule * 3600
    print(f"Schedule mode enabled: scanning every {args.schedule} hour(s). Press Ctrl+C to stop.")
    try:
        while True:
            print(f"\n=== Scan started at {datetime.datetime.now().isoformat(timespec='seconds')} ===")
            try:
                token = get_twitch_token()
            except Exception as exc:
                print(f"Warning: failed to refresh Twitch token, reusing previous one: {exc}", file=sys.stderr)
            scan_once(args, token, whisper_model)
            next_run = datetime.datetime.now() + datetime.timedelta(seconds=interval_seconds)
            print(f"Sleeping {args.schedule} hour(s) until next scan at {next_run.isoformat(timespec='seconds')}...")
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\nSchedule stopped.")


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
