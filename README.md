# Scribble Story

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Gemini 2.5 Flash](https://img.shields.io/badge/Gemini%202.5%20Flash-8E75B2?logo=googlegemini&logoColor=white)
![Sarvam Bulbul v3](https://img.shields.io/badge/Sarvam%20Bulbul%20v3-E8703A)
![Vanilla JS](https://img.shields.io/badge/Vanilla%20JS-F7DF1E?logo=javascript&logoColor=black)
![uv](https://img.shields.io/badge/uv-DE5FE9?logo=uv&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)
![Cloud Run](https://img.shields.io/badge/Cloud%20Run-4285F4?logo=googlecloud&logoColor=white)

A child draws a picture, picks a language, and hears a story about their drawing — read aloud in any of 11 Indian languages.

The story is written by **Gemini 2.5 Flash** looking at the drawing, and narrated by **Sarvam Bulbul v3** over a streaming WebSocket. Narration begins while the story is still being written, so there's no dead air after the button click.

**Live demo:** https://scribble-story-gicphaimgq-uc.a.run.app

> The service scales to zero, so the first request after an idle period takes a few seconds to wake.

---

## 🎧 Start here — the codelab

**[Text to Speech with Sarvam Bulbul v3 →](https://sarvam-bulbul-tts-gicphaimgq-uc.a.run.app)**

Bulbul v3 is one model with **three different APIs**, and choosing the wrong one is the most common reason a voice app feels sluggish. The codelab walks all three — REST, HTTP stream, and WebSocket — with short scripts you run yourself and hear the difference in.

By the end you'll know which one to reach for and why, and you'll have three working examples to keep. Nine steps, roughly 30 minutes, no prior voice-AI experience assumed. It closes with the app in this repo as the case that genuinely needs the third.

> This is my first codelab. If a step trips you up or something reads unclear, [open an issue](https://github.com/Madhan-mohan14/scribble-story-Sarvam.ai/issues) or email me at **madhanmohan1413@gmail.com** — I'd love to make it better.

---

## Disclaimer

**This is an independent, unofficial project. It is not affiliated with, endorsed by, or maintained by Sarvam AI.**

I built it to learn their text-to-speech APIs and to share what I worked out along the way. "Sarvam", "Bulbul", and "Saaras" are names belonging to Sarvam AI and are used here only to describe which APIs the project calls. Anything inaccurate here is mine, not theirs — please report it to me rather than to Sarvam.

For the official documentation, always go to **[docs.sarvam.ai](https://docs.sarvam.ai)**.

---

## Contents

- [Start here — the codelab](#-start-here--the-codelab)
- [Disclaimer](#disclaimer)
- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [API](#api)
- [TTS examples](#tts-examples)
- [Deploy to Cloud Run](#deploy-to-cloud-run)
- [Project structure](#project-structure)

---

## What it does

- **Draw anything** on an HTML canvas — no account, no upload dialog.
- **A vision model reads the drawing** and writes a 100–150 word story for a 5–7 year old.
- **Bulbul v3 narrates it**, streaming audio back as the story is generated.
- **Switch language after the fact** — the story is translated and re-narrated without calling the vision model again.
- **Stop any time** — playback and generation are cancellable mid-sentence.

Supported languages: English, Hindi, Bengali, Gujarati, Kannada, Malayalam, Marathi, Odia, Punjabi, Tamil, Telugu.

---

## How it works

```
canvas drawing
      │  base64 PNG, composited onto an opaque background
      ▼
Gemini 2.5 Flash  ──── story text, streamed a few words at a time
      │
      ▼
Sarvam Bulbul v3  ──── WebSocket: text in, PCM audio chunks out
      │
      ▼
browser  ──── Server-Sent Events → Web Audio API, gapless playback
```

Two things make this feel instant rather than slow:

1. **Text is forwarded to TTS as it arrives**, not after the story is finished. The WebSocket API is the only one of Sarvam's three TTS endpoints that accepts text you don't have yet.
2. **Audio is raw PCM (`linear16` @ 24 kHz)**, so each chunk drops straight into a Web Audio buffer with no per-chunk decode.

Language switching reuses the cached story text: it is translated with Sarvam Translate and re-narrated, skipping the vision model entirely.

---

## Quick start

**Prerequisites**

- Python 3.10 or newer
- A [Sarvam AI](https://dashboard.sarvam.ai/) API key
- Google Gemini access — either a `GOOGLE_API_KEY`, or Vertex AI credentials

**1. Clone**

```bash
git clone https://github.com/Madhan-mohan14/scribble-story-Sarvam.ai.git
cd scribble-story-Sarvam.ai
```

**2. Install**

Using [uv](https://docs.astral.sh/uv/) (recommended — installs the exact locked versions):

```bash
uv sync
```

Or with pip:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**3. Configure**

```bash
cp .env.example .env
```

Then edit `.env` and set at minimum `SARVAM_API_KEY` and `GOOGLE_API_KEY`.

**4. Run**

```bash
uv run uvicorn app:app --reload --port 8021
```

Open **http://localhost:8021** — a single process serves both the frontend and the API.

---

## Configuration

All configuration is via environment variables, read from `.env` at startup.

| Variable | Required | Description |
|---|---|---|
| `SARVAM_API_KEY` | Yes | Sarvam API key. Accepts a comma-separated list — see below. |
| `GOOGLE_API_KEY` | One of these | Gemini Developer API key. Takes precedence when set. |
| `GOOGLE_CLOUD_PROJECT` | One of these | GCP project ID, for Gemini via Vertex AI. |
| `GOOGLE_CLOUD_LOCATION` | No | Vertex region. Defaults to `us-central1`. |

**Gemini access:** if `GOOGLE_API_KEY` is set, the Developer API is used. Otherwise the app falls back to Vertex AI with Application Default Credentials — run `gcloud auth application-default login` first.

**Key rotation:** `SARVAM_API_KEY` accepts several keys separated by commas:

```
SARVAM_API_KEY=first-key,second-key
```

When a key is exhausted or rejected, the app rotates to the next one and retries the request rather than failing the user. Useful for demos, where running out of credits mid-sentence is otherwise fatal.

---

## API

The frontend is plain HTML and JavaScript talking to three endpoints.

### `POST /generate`

Turns a drawing into a story, and optionally streams the narration.

```json
{
  "image": "data:image/png;base64,...",
  "language_code": "hi-IN",
  "stream": true,
  "request_id": "optional-client-generated-id"
}
```

With `stream: false` you get a single JSON response. With `stream: true` the response is a `text/event-stream` interleaving story text and audio as they are produced.

### `POST /narrate`

Re-narrates existing story text in another language, without calling the vision model.

```json
{
  "text": "Once upon a time...",
  "language_code": "ta-IN",
  "source_language_code": "en-IN",
  "request_id": "optional-client-generated-id"
}
```

Translates only when `language_code` differs from `source_language_code`, then streams audio the same way as `/generate`.

### `POST /cancel`

```json
{ "request_id": "the-id-you-sent-earlier" }
```

Cancels an in-flight pipeline so a user pressing Stop halts generation server-side, not just playback in the browser.

### Server-Sent Event types

| Event | Payload |
|---|---|
| `text` | A chunk of story text |
| `audio` | A base64 PCM16 audio chunk |
| `translated_text` | Full translated text, when a translation happened |
| `metrics` | Timing values, emitted as soon as each is known |
| `error` | A human-readable failure message |

---

## TTS examples

`examples/` holds three standalone scripts, one per Sarvam TTS API. Each writes an audio file you can play, and together they show why this app uses the WebSocket.

```bash
uv run python examples/tts_rest.py           # one request, one complete .wav
uv run python examples/tts_http_stream.py    # bytes arrive while synthesising
uv run python examples/tts_websocket.py      # text in and audio out, concurrently
```

| API | Needs full text up front | Audio arrives |
|---|---|---|
| REST | Yes | All at the end |
| HTTP stream | Yes | While synthesising |
| WebSocket | **No** | While synthesising |

Only the third works when an LLM is still writing the text — which is exactly this app's situation.

---

## Deploy to Cloud Run

The included `Dockerfile` installs from `uv.lock`, so the image gets the versions that were tested locally, and listens on Cloud Run's injected `$PORT`.

```bash
gcloud run deploy scribble-story \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --timeout 600
```

`--timeout 600` matters: responses stream for tens of seconds and the default timeout can cut them off.

Set the runtime secrets:

```bash
gcloud run services update scribble-story --region us-central1 \
  --update-env-vars "SARVAM_API_KEY=your-sarvam-key,GOOGLE_API_KEY=your-gemini-key"
```

If you pass several comma-separated Sarvam keys, the comma collides with gcloud's parser — use an alternate delimiter:

```bash
gcloud run services update scribble-story --region us-central1 \
  --set-env-vars "^@^SARVAM_API_KEY=sk_one,sk_two"
```

**Using Vertex instead of an API key?** Grant the runtime service account `roles/aiplatform.user`. IAM changes take a few minutes to propagate — a 403 on `aiplatform.endpoints.predict` immediately after granting is usually just lag.

---

## Project structure

```
.
├── app.py                      FastAPI backend — the whole server
├── static/
│   ├── index.html              Canvas UI and language picker
│   ├── script.js               Drawing, SSE parsing, Web Audio playback
│   └── style.css
├── examples/
│   ├── tts_rest.py             Bulbul v3 over REST
│   ├── tts_http_stream.py      Bulbul v3 over HTTP streaming
│   └── tts_websocket.py        Bulbul v3 over WebSocket
├── Dockerfile                  Container image for Cloud Run
├── pyproject.toml              Project metadata and dependencies
├── uv.lock                     Locked dependency versions
├── requirements.txt            pip-installable export of the lock
└── .env.example                Template for local configuration
```
