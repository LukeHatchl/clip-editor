# clip-scanner

Scans Twitch VODs for mentions of specific product names using Whisper transcription and fuzzy matching.

## How it works

1. Fetches recent VODs for a Twitch username via the Helix API
2. Downloads audio-only (MP3) with yt-dlp
3. Transcribes with OpenAI Whisper (word-level timestamps)
4. Fuzzy-matches the transcript against target product names
5. Outputs a JSON file with timestamps and 30-second context windows around each mention

Keywords scanned for: product names (`flick fire`, `flick edge`, `flick stratus`, `npen`), mousepad/mouse references (`new mousepad`, `new mouse`, `mouse glide`, `good glide`, `nice glide`, `glides smooth`), and aim/feel phrases (`feels smooth`, `so smooth`, `smooth aim`, `aim feels`, `tracking feels`, `control feels`)

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

## Usage

```bash
# Scan the 5 most recent VODs (default)
python clip_scanner.py shroud

# Scan 20 VODs with a more accurate Whisper model
python clip_scanner.py shroud --vods 20 --model small

# Scan a specific VOD by ID
python clip_scanner.py shroud --vod-id 2803949530

# Custom output path, lower match threshold, keep audio files
python clip_scanner.py xqc --vods 10 --output results/xqc.json --threshold 75 --keep-audio
```

## CLI flags

| Flag | Default | Description |
|---|---|---|
| `username` | *(required)* | Twitch username to scan |
| `--vod-id VOD_ID` | — | Scan a specific VOD ID (skips recent VOD fetch) |
| `--vods N` | `5` | Number of most recent VODs to scan |
| `--model` | `base` | Whisper model size: `tiny`, `base`, `small`, `medium`, `large` |
| `--output PATH` | `mentions.json` | Output JSON file path |
| `--threshold 0-100` | `80` | Fuzzy match score threshold |
| `--keep-audio` | off | Keep downloaded MP3s in `./audio/` |

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
