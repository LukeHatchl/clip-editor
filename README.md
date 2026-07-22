# clip-scanner

Scans Twitch VODs for mentions of specific product names using Whisper transcription and fuzzy matching.

## How it works

1. Fetches recent VODs for a Twitch username via the Helix API
2. Downloads audio-only (MP3) with yt-dlp
3. Transcribes with OpenAI Whisper (word-level timestamps)
4. Fuzzy-matches the transcript against target product names
5. Outputs a JSON file with timestamps and 30-second context windows around each mention

Keywords, streamers, and scan history are managed in `config.json` (see [Configuration](#configuration) below).

## Setup

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Configure Twitch credentials**

Create a Twitch app at [dev.twitch.tv/console](https://dev.twitch.tv/console), then copy your Client ID and generate a Client Secret.

```bash
cp .env.example .env
# edit .env and fill in your credentials
```

```env
TWITCH_CLIENT_ID=your_client_id_here
TWITCH_CLIENT_SECRET=your_client_secret_here
```

Alternatively, set them directly as environment variables.

## Configuration

`config.json` sits alongside the script and controls everything:

```json
{
  "discord_webhook_url": "https://discordapp.com/api/webhooks/...",
  "streamers": ["shroud", "xqc"],
  "keywords": ["flick fire", "flick edge", "new mousepad"],
  "fuzzy_threshold": 80,
  "processed_vods": {}
}
```

- **`discord_webhook_url`** — Discord webhook to notify when a mention is found. Remove the key entirely to disable notifications.
- **`streamers`** — Twitch usernames scanned when no username is passed on the CLI
- **`keywords`** — product names and phrases to match against transcripts
- **`fuzzy_threshold`** — default match score (0–100); overridden by `--threshold`
- **`processed_vods`** — populated automatically after each scan; VODs listed here are skipped on future runs to avoid re-processing. Remove an entry by hand to force a re-scan.

## Testing Discord notifications

The quickest way to confirm notifications are working is to scan a VOD you already know contains a keyword match, using a low threshold so something is guaranteed to fire.

**1. Find a VOD ID that will produce a hit**

Pick a VOD from a streamer you know has mentioned one of your keywords. Copy the VOD ID from the URL — e.g. `https://www.twitch.tv/videos/2803949530` → `2803949530`.

**2. Make sure the VOD isn't in `processed_vods` yet**

If it is, remove its entry from `config.json` so the script doesn't skip it:

```json
"processed_vods": {}
```

**3. Run with a low threshold to force matches**

```bash
python clip_scanner.py --vod-id 2803949530 --threshold 50
```

Dropping the threshold to `50` makes fuzzy matching much more permissive, so you're likely to get at least one hit even on a VOD that doesn't contain an exact keyword match. If you get a hit, a Discord embed will be posted immediately.

**4. What the notification looks like**

Each mention produces one embed:

- **Title**: `Product mention: flick fire` — links directly to the VOD at the exact timestamp
- **Streamer / Timestamp / Match** fields inline
- **Quote**: the ~30-second transcript window surrounding the mention

**Troubleshooting**

| Symptom | Fix |
|---|---|
| No notification, no error | No mentions found at the current threshold — lower `--threshold` |
| `Discord notification failed: 404` | Webhook URL is invalid or has been deleted — regenerate in Discord server settings |
| `Discord notification failed: 429` | Rate limited; Discord allows ~30 requests/minute per webhook. Unlikely unless a single VOD has dozens of hits. |
| Notification fires but VOD link doesn't jump to timestamp | The `?t=` param only works when you're logged in to Twitch |

## Usage

```bash
# Scan all streamers listed in config.json (5 most recent VODs each)
python clip_scanner.py

# Scan a specific streamer
python clip_scanner.py shroud

# Scan 20 VODs with a more accurate Whisper model
python clip_scanner.py shroud --vods 20 --model small

# Scan a specific VOD by ID
python clip_scanner.py --vod-id 2803949530

# Custom output path, lower match threshold, keep audio files
python clip_scanner.py xqc --vods 10 --output results/xqc.json --threshold 75 --keep-audio

# Run continuously, rescanning all configured streamers every 24 hours (default)
python clip_scanner.py --schedule

# Run continuously on a custom interval, e.g. every 30 minutes
python clip_scanner.py --schedule 0.5
```

## Scheduling

Pass `--schedule` to keep the script running and rescan the streamers in `config.json` on a loop instead of exiting after one pass:

```bash
# Every 24 hours (the default)
python clip_scanner.py --schedule

# Every 0.5 hours (30 minutes)
python clip_scanner.py --schedule 0.5
```

Notes:
- The interval is in hours and accepts decimals (`--schedule 0.5` = 30 minutes, `--schedule 2` = 2 hours).
- Each cycle re-reads `config.json`, so edits to `streamers`, `keywords`, or `discord_webhook_url` take effect on the next run without restarting the script.
- Already-processed VODs (tracked in `processed_vods`) are still skipped each cycle, so only new VODs get scanned.
- The output file (`--output`, default `mentions.json`) is overwritten with just that cycle's findings each run — Discord notifications are the persistent record across cycles.
- `--schedule` can't be combined with `--vod-id` (scanning one fixed VOD repeatedly doesn't make sense).
- Stop the loop with Ctrl+C.

## CLI flags

| Flag | Default | Description |
|---|---|---|
| `username` | *(optional)* | Twitch username to scan; omit to scan all streamers in `config.json` |
| `--vod-id VOD_ID` | — | Scan a specific VOD ID (skips recent VOD fetch) |
| `--vods N` | `5` | Number of most recent VODs to scan per streamer |
| `--model` | `base` | Whisper model size: `tiny`, `base`, `small`, `medium`, `large` |
| `--output PATH` | `mentions.json` | Output JSON file path |
| `--threshold 0-100` | from config | Fuzzy match score threshold |
| `--keep-audio` | off | Keep downloaded MP3s in `./audio/` |
| `--schedule [HOURS]` | off (single run) | Loop forever, rescanning every `HOURS` hours; defaults to `24` if the flag is passed with no value |

**Choosing a Whisper model:**

| Model | Speed | Accuracy |
|---|---|---|
| `tiny` | Fastest | Lowest |
| `base` | Fast | Good (recommended starting point) |
| `small` | Moderate | Better |
| `medium` | Slow | High |
| `large` | Slowest | Highest |

Expect roughly 5–15 minutes per VOD on CPU with `base`. GPU will be significantly faster.

## Output format

Results are written as a JSON array. Each entry represents one product mention:

```json
[
  {
    "vod_id": "2345678901",
    "vod_title": "!new mouse day 7 hours",
    "vod_created_at": "2025-11-03T18:00:00Z",
    "timestamp_seconds": 4823.5,
    "timestamp_formatted": "01:20:23",
    "keyword_matched": "flick fire",
    "matched_text": "flick fire",
    "fuzzy_score": 100,
    "context_window": "... i switched to the flick fire last week and my aim has been ..."
  }
]
```

| Field | Description |
|---|---|
| `vod_id` | Twitch VOD ID |
| `vod_title` | Title of the VOD |
| `vod_created_at` | VOD creation timestamp (ISO 8601) |
| `timestamp_seconds` | Time of mention in seconds |
| `timestamp_formatted` | Time of mention as `HH:MM:SS` or `MM:SS` |
| `keyword_matched` | The matched keyword or phrase |
| `matched_text` | The exact words from the transcript that matched |
| `fuzzy_score` | Match confidence score (0–100) |
| `context_window` | ~30 seconds of transcript surrounding the mention |

## Notes

- **Fuzzy matching** catches variations like "flickfire", "flick fiyah", or mumbled speech. Lower `--threshold` to catch more (at the cost of false positives).
- **Deduplication:** Multiple hits for the same product within 5 seconds are collapsed into the highest-scoring match.
- **Skipped VODs:** Subscriber-only or deleted VODs will be skipped with an error message; scanning continues for the rest.
- **Current Streamer List:** vergofn_, Sommerset, venofn, Higgs, shxrkfnbr, acorn, fnpaper, GMoney, noahreyli, Crackly, bushszn
