import base64
import io
import os
import time
import json
import asyncio
import websockets
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from PIL import Image
from dotenv import load_dotenv
from google import genai
from google.genai import types
from sarvamai import AsyncSarvamAI, AudioOutput, EventResponse, ErrorResponse
from sarvamai.text_to_speech_streaming.socket_client import AsyncTextToSpeechStreamingSocketClient

load_dotenv()

app = FastAPI(title="Scribble Story")

active_pipelines: dict[str, asyncio.Task] = {}


class GenerateRequest(BaseModel):
    image: str  # Base64 string or data URL
    language_code: str = "en-IN"
    stream: bool = False
    request_id: str | None = None


class NarrateRequest(BaseModel):
    text: str
    language_code: str
    source_language_code: str = "en-IN"
    request_id: str | None = None


class CancelRequest(BaseModel):
    request_id: str


@app.post("/cancel")
async def cancel_generation(req: CancelRequest):
    task = active_pipelines.get(req.request_id)
    if task is not None and not task.done():
        task.cancel()
        return {"cancelled": True}
    return {"cancelled": False}


LANGUAGE_NAMES = {
    "hi-IN": "Hindi", "bn-IN": "Bengali", "ta-IN": "Tamil", "te-IN": "Telugu",
    "gu-IN": "Gujarati", "kn-IN": "Kannada", "ml-IN": "Malayalam", "mr-IN": "Marathi",
    "pa-IN": "Punjabi", "od-IN": "Odia",
}


def get_speaker(language_code: str) -> str:
    return "shubh" if language_code == "en-IN" else "ritu"


STORY_PROMPT = """You are a cheerful, enthusiastic storyteller for young children (ages 3-7).

Look carefully at this child's drawing — notice the colours, shapes, characters, and objects in it.

Write a SHORT, CHEERFUL, ENGAGING narration story of exactly 100-150 words based on what you see. Rules:
- Mention the actual colours and shapes you see (e.g. 'a bright orange house', 'a sunny yellow sun').
- Use a warm, upbeat narrator voice — like reading aloud to a child at bedtime.
- Include the characters or objects from the drawing as the heroes of the story.
- Make it exciting with a tiny adventure or magical moment.
- End with something cosy and happy.
- NO bullet points. Plain flowing paragraphs only.
- NO markdown of any kind: no asterisks, no bold, no italics, no headings. The text is read aloud by a speech model, so any stray symbol gets spoken.
- Strictly 100-150 words. Count carefully."""


def build_story_prompt(language_code: str) -> str:
    if language_code == "en-IN":
        return STORY_PROMPT
    return f"""{STORY_PROMPT}
- Write the story directly in {LANGUAGE_NAMES.get(language_code, 'English')}."""


def strip_markdown(text: str) -> str:
    # Removes every '*' rather than matching '**' pairs on purpose: Gemini
    # streams text in arbitrary chunks, so a pair can straddle a chunk
    # boundary and never match. A children's story has no legitimate use for
    # an asterisk, and anything left in gets spoken aloud by Bulbul.
    return text.replace("*", "")


def composite_canvas_image(base64_str: str) -> bytes:
    """Decodes base64 image and composites onto an opaque cream (#fbf7f2) background."""
    if "," in base64_str:
        base64_str = base64_str.split(",", 1)[1]

    image_bytes = base64.b64decode(base64_str)
    canvas_img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

    background = Image.new("RGBA", canvas_img.size, (251, 247, 242, 255))
    composited = Image.alpha_composite(background, canvas_img).convert("RGB")

    buffer = io.BytesIO()
    composited.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


# Cached so tests can patch genai.Client before the first real call.
_genai_client = None

def get_genai_client():
    global _genai_client
    if _genai_client is None:
        # GOOGLE_API_KEY (Gemini Developer API) wins when present because it
        # needs no GCP project and no gcloud login - that is what makes this
        # repo runnable straight after a clone. Vertex AI is the fallback.
        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        if api_key:
            _genai_client = genai.Client(api_key=api_key)
        else:
            _genai_client = genai.Client(
                vertexai=True,
                project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
            )
    return _genai_client


# SARVAM_API_KEY may hold several comma-separated keys. Sarvam trial keys run
# out of credits mid-demo, so the app walks to the next one on a quota error
# instead of needing a restart or redeploy.
_sarvam_key_index = 0
_sarvam_client = None


def get_sarvam_keys() -> list[str]:
    return [k.strip() for k in os.environ.get("SARVAM_API_KEY", "").split(",") if k.strip()]


def current_sarvam_key() -> str:
    keys = get_sarvam_keys()
    if not keys:
        return ""
    return keys[min(_sarvam_key_index, len(keys) - 1)]


def rotate_sarvam_key() -> bool:
    """Move to the next key. False when the last one is already in use."""
    global _sarvam_key_index, _sarvam_client
    keys = get_sarvam_keys()
    if _sarvam_key_index + 1 >= len(keys):
        return False
    _sarvam_key_index += 1
    # The cached client captured the old key at construction time.
    _sarvam_client = None
    # flush=True: without it this sits in Python's stdout buffer and never
    # reaches container logs (Cloud Run) when you most need to see it.
    print(f"[sarvam] key {_sarvam_key_index} out of credits, "
          f"switching to key {_sarvam_key_index + 1} of {len(keys)}", flush=True)
    return True


def is_quota_error(message: str) -> bool:
    m = (message or "").lower()
    return "credit" in m or "quota" in m or "insufficient" in m


def get_sarvam_client():
    global _sarvam_client
    if _sarvam_client is None:
        _sarvam_client = AsyncSarvamAI(api_subscription_key=current_sarvam_key())
    return _sarvam_client


async def keepalive_ping(ws):
    # Sarvam's own docs list this as a best practice for long-running
    # connections. Confirmed live: without it, Sarvam's server closes the
    # socket server-side with 1011 "keepalive ping timeout" around the 40s
    # mark, independent of our own ping_interval=None on the client side.
    try:
        while True:
            await asyncio.sleep(15)
            await ws.ping()
    except (asyncio.CancelledError, websockets.ConnectionClosed):
        pass


@app.post("/generate")
async def generate_story(req: GenerateRequest):
    if not req.image:
        raise HTTPException(status_code=400, detail="No image provided")

    try:
        jpeg_bytes = composite_canvas_image(req.image)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image format: {e}")

    language_code = req.language_code
    speaker = get_speaker(language_code)

    if not req.stream:
        start_time = time.time()
        client = get_genai_client()
        prompt = build_story_prompt(language_code)

        part = types.Part.from_bytes(
            data=jpeg_bytes,
            mime_type="image/jpeg",
        )

        try:
            vlm_start = time.time()
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[part, prompt],
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                    max_output_tokens=400,
                ),
            )
            vlm_ms = int((time.time() - vlm_start) * 1000)

            story_text = (
                strip_markdown(response.text).strip()
                if response.text
                else "Once upon a time, a magical drawing came to life!"
            )
            total_ms = int((time.time() - start_time) * 1000)

            return {
                "story_text": story_text,
                "metrics": {"vlm_ms": vlm_ms, "total_ms": total_ms},
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"VLM generation failed: {str(e)}")

    async def sse_generator():
        start_time = time.time()
        vlm_start = None
        vlm_ms = 0
        tts_start = None
        tts_first_chunk_ms = None

        queue = asyncio.Queue()
        uri = "wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true"

        async def connect_and_configure_tts():
            # Key is read here, not captured earlier, so a rotation triggered
            # by a quota error takes effect on the very next connection. It is
            # returned too, so the retry can tell an already-rotated key from
            # one that still needs rotating.
            key_used = current_sarvam_key()
            headers = {"Api-Subscription-Key": key_used}
            # ping_interval=None: the SDK's own connect() has no passthrough
            # for this, and our own wait_for(..., timeout=90.0) below is the
            # single source of truth for how long to wait. The library's
            # default keepalive ping/timeout would otherwise independently
            # force-close a connection that's just waiting on a slow response.
            # We open the raw socket ourselves and hand it to the SDK's own
            # socket client class so the rest of the pipeline still gets
            # typed messages (AudioOutput/EventResponse/ErrorResponse)
            # instead of hand-parsed JSON.
            raw_ws = await websockets.connect(uri, additional_headers=headers, ping_interval=None)
            ws = AsyncTextToSpeechStreamingSocketClient(websocket=raw_ws)
            await ws.configure(
                target_language_code=language_code,
                speaker=speaker,
                output_audio_codec="linear16",  # raw 16-bit PCM
                speech_sample_rate=24000,
                # Low values so Sarvam starts synthesizing on the first
                # small piece of text instead of waiting to accumulate
                # more - trades prosody smoothness for lower TTFA.
                min_buffer_size=30,
                max_chunk_length=150,
            )
            return raw_ws, ws, key_used

        async def run_pipeline():
            # tts_first_chunk_ms is deliberately absent: only read_audio()
            # assigns it, and it declares its own nonlocal for that.
            nonlocal vlm_start, vlm_ms, tts_start
            raw_ws = None
            ping_task = None
            # Sarvam emits one "final" event per flush(), and we flush twice:
            # once right after the first text chunk (to start synthesis
            # immediately) and once when Gemini is done. Breaking on the
            # first "final" would truncate the story to its opening words,
            # so stop only once all text is sent AND every flush we issued
            # has been acknowledged.
            # "failed" covers every way a dead key shows up: an explicit
            # quota ErrorResponse, or Sarvam simply closing the socket
            # (observed both). Classifying the cause is unreliable, so any
            # TTS failure before a single audio chunk triggers the retry.
            tts = {"flushes": 0, "finals": 0, "done_sending": False, "failed": False}
            story_so_far = ""
            try:
                # Opened concurrently with the Gemini call below, not before it -
                # TTS setup only needs language_code/speaker, already known
                # upfront, so its connect time is hidden inside Gemini's latency
                # instead of adding to it.
                connect_task = asyncio.create_task(connect_and_configure_tts())

                client = get_genai_client()
                prompt = build_story_prompt(language_code)

                part = types.Part.from_bytes(
                    data=jpeg_bytes,
                    mime_type="image/jpeg",
                )

                vlm_start = time.time()
                # .aio (async), not the sync client - a sync
                # generate_content_stream() would block the event loop while
                # iterating, stalling read_audio() below from processing
                # Sarvam's chunks concurrently.
                response_stream = await client.aio.models.generate_content_stream(
                    model="gemini-2.5-flash",
                    contents=[part, prompt],
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                        max_output_tokens=400,
                    ),
                )

                raw_ws, ws, key_used = await connect_task
                ping_task = asyncio.create_task(keepalive_ping(ws))

                async def read_audio():
                    nonlocal tts_first_chunk_ms
                    try:
                        async for message in ws:
                            if isinstance(message, AudioOutput):
                                if tts_first_chunk_ms is None and tts_start is not None:
                                    tts_first_chunk_ms = int((time.time() - tts_start) * 1000)
                                    # Sent immediately, not bundled into the
                                    # final metrics event, so the UI reflects
                                    # it the instant it's true.
                                    await queue.put({"type": "metrics", "tts_ms": tts_first_chunk_ms})
                                await queue.put({"type": "audio", "audio": message.data.audio})
                            elif isinstance(message, EventResponse) and message.data.event_type == "final":
                                tts["finals"] += 1
                                if tts["done_sending"] and tts["finals"] >= tts["flushes"]:
                                    break
                            elif isinstance(message, ErrorResponse):
                                if is_quota_error(message.data.message):
                                    # Handled by the retry below rather than
                                    # surfaced - the next key may well work.
                                    tts["failed"] = True
                                    break
                                await queue.put({"type": "error", "error": f"Sarvam TTS error: {message.data.message}"})
                    except websockets.ConnectionClosed:
                        # A closed socket after audio has started is a natural
                        # end of stream. Closing before any audio is a failure
                        # (a dead key drops the connection this way).
                        if tts_first_chunk_ms is None:
                            tts["failed"] = True

                audio_task = asyncio.create_task(read_audio())

                # tts_start is set on the first chunk actually sent to Sarvam
                # (not before this loop starts), so TTS TTFA measures only
                # Sarvam's own text -> first-audio latency, not Gemini's.
                async for chunk in response_stream:
                    if chunk.text:
                        text_chunk = strip_markdown(chunk.text)
                        if not text_chunk:
                            continue
                        await queue.put({"type": "text", "text": text_chunk})
                        story_so_far += text_chunk
                        if tts_start is None:
                            tts_start = time.time()
                        if tts["failed"]:
                            # Socket is dead; the retry below replays
                            # story_so_far once a working key is in hand.
                            continue
                        try:
                            await ws.convert(text_chunk)
                        except websockets.ConnectionClosed:
                            tts["failed"] = True
                            continue
                        if tts["flushes"] == 0:
                            # Gemini's first chunk is only a few characters,
                            # well under min_buffer_size, so without this
                            # Sarvam would idle until enough of the story has
                            # streamed in. Flushing here starts synthesis on
                            # the opening words and cuts time-to-first-audio
                            # from ~950ms to ~250ms.
                            tts["flushes"] += 1
                            await ws.flush()

                vlm_ms = int((time.time() - vlm_start) * 1000)
                await queue.put({"type": "metrics", "vlm_ms": vlm_ms})

                if not tts["failed"]:
                    # Set before the flush, not after: a "final" racing in from
                    # the early flush must not be mistaken for this one.
                    tts["done_sending"] = True
                    tts["flushes"] += 1
                    try:
                        await ws.flush()
                        # Synthesis-to-completion time scales with story length,
                        # not just first-byte latency, so this timeout is a
                        # safety net for a hung connection - not a point where
                        # real audio gets cut off.
                        await asyncio.wait_for(audio_task, timeout=90.0)
                    except asyncio.TimeoutError:
                        audio_task.cancel()
                    except websockets.ConnectionClosed:
                        tts["failed"] = True

                # Retry on a fresh key when the first one ran out of credits.
                # Safe to replay from the top: no audio reached the client, and
                # by now Gemini has finished so story_so_far is the whole story.
                while tts["failed"] and tts_first_chunk_ms is None and (
                        current_sarvam_key() != key_used or rotate_sarvam_key()):
                    tts.update({"flushes": 0, "finals": 0, "done_sending": False,
                                "failed": False})
                    audio_task.cancel()
                    if ping_task is not None:
                        ping_task.cancel()
                    await raw_ws.close()

                    raw_ws, ws, key_used = await connect_and_configure_tts()
                    ping_task = asyncio.create_task(keepalive_ping(ws))
                    audio_task = asyncio.create_task(read_audio())
                    tts_start = time.time()
                    tts["done_sending"] = True
                    tts["flushes"] += 1
                    try:
                        await ws.convert(story_so_far)
                        await ws.flush()
                        await asyncio.wait_for(audio_task, timeout=90.0)
                    except asyncio.TimeoutError:
                        audio_task.cancel()
                    except websockets.ConnectionClosed:
                        tts["failed"] = True

                if tts["failed"]:
                    await queue.put({"type": "error", "error":
                                     "Narration unavailable: every Sarvam API key "
                                     "in SARVAM_API_KEY failed (most likely out of credits)."})

            except Exception as e:
                await queue.put({"type": "error", "error": f"Pipeline error: {str(e)}"})
            finally:
                if ping_task is not None:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
                if raw_ws is not None:
                    await raw_ws.close()
                await queue.put({"type": "done"})

        pipeline_task = asyncio.create_task(run_pipeline())
        if req.request_id:
            active_pipelines[req.request_id] = pipeline_task
            pipeline_task.add_done_callback(
                lambda t: active_pipelines.pop(req.request_id, None)
            )

        while True:
            item = await queue.get()
            if item["type"] == "done":
                break
            yield f"data: {json.dumps(item)}\n\n"

        total_ms = int((time.time() - start_time) * 1000)
        metrics_event = {
            "type": "metrics",
            "vlm_ms": vlm_ms,
            "tts_ms": tts_first_chunk_ms or 0,
            "total_ms": total_ms
        }
        yield f"data: {json.dumps(metrics_event)}\n\n"
        # pipeline_task is already finished by now (that's how "done" reached
        # the queue). If it finished via /cancel, awaiting it again re-raises
        # CancelledError, which would abort this response instead of closing
        # it cleanly.
        try:
            await pipeline_task
        except asyncio.CancelledError:
            pass

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


@app.post("/narrate")
async def narrate_story(req: NarrateRequest):
    if not req.text:
        raise HTTPException(status_code=400, detail="No text provided")

    speaker = get_speaker(req.language_code)
    uri = "wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true"

    async def translate_if_needed() -> str:
        if req.language_code == req.source_language_code:
            return req.text
        while True:
            try:
                response = await get_sarvam_client().text.translate(
                    input=req.text,
                    source_language_code=req.source_language_code,
                    target_language_code=req.language_code,
                    model="sarvam-translate:v1",
                )
                return response.translated_text
            except Exception as e:
                if is_quota_error(str(e)) and rotate_sarvam_key():
                    continue
                raise

    async def connect_and_configure_tts():
        # The key in use is returned so the retry below can tell "this socket
        # used a key that has since been rotated away" (translate runs
        # concurrently and may rotate underneath us) from "we need to rotate".
        key_used = current_sarvam_key()
        headers = {"Api-Subscription-Key": key_used}
        raw_ws = await websockets.connect(uri, additional_headers=headers, ping_interval=None)
        ws = AsyncTextToSpeechStreamingSocketClient(websocket=raw_ws)
        await ws.configure(
            target_language_code=req.language_code,
            speaker=speaker,
            output_audio_codec="linear16",
            speech_sample_rate=24000,
            min_buffer_size=30,
            max_chunk_length=150,
        )
        return raw_ws, ws, key_used

    async def sse_generator():
        start_time = time.time()
        tts_start = None
        tts_first_chunk_ms = None
        queue = asyncio.Queue()

        async def run_pipeline():
            nonlocal tts_start
            raw_ws = None
            ping_task = None
            quota = {"failed": False}
            try:
                # Translate (if needed) and open+configure the TTS socket
                # concurrently - neither depends on the other's result.
                text_to_speak, (raw_ws, ws, key_used) = await asyncio.gather(
                    translate_if_needed(), connect_and_configure_tts()
                )
                ping_task = asyncio.create_task(keepalive_ping(ws))

                async def read_audio():
                    nonlocal tts_first_chunk_ms
                    try:
                        async for message in ws:
                            if isinstance(message, AudioOutput):
                                if tts_first_chunk_ms is None and tts_start is not None:
                                    tts_first_chunk_ms = int((time.time() - tts_start) * 1000)
                                    await queue.put({"type": "metrics", "tts_ms": tts_first_chunk_ms})
                                await queue.put({"type": "audio", "audio": message.data.audio})
                            elif isinstance(message, EventResponse) and message.data.event_type == "final":
                                break
                            elif isinstance(message, ErrorResponse):
                                if is_quota_error(message.data.message):
                                    quota["failed"] = True
                                    break
                                await queue.put({"type": "error", "error": f"Sarvam TTS error: {message.data.message}"})
                    except websockets.ConnectionClosed:
                        # Closing before any audio means the key failed; a dead
                        # key drops the socket instead of always erroring.
                        if tts_first_chunk_ms is None:
                            quota["failed"] = True

                audio_task = asyncio.create_task(read_audio())

                await queue.put({"type": "translated_text", "text": text_to_speak})

                # See /generate's matching comment: tts_start begins here so
                # TTS TTFA measures only Sarvam's own latency, not translation.
                tts_start = time.time()
                try:
                    await ws.convert(text_to_speak)
                    await ws.flush()
                    await asyncio.wait_for(audio_task, timeout=90.0)
                except asyncio.TimeoutError:
                    audio_task.cancel()
                except websockets.ConnectionClosed:
                    quota["failed"] = True

                # Same retry as /generate, but simpler: the text was known
                # upfront, so replaying it on the next key is a clean redo.
                # A key that changed while we were connected (translate rotates
                # concurrently) is reason enough to retry without rotating again.
                while quota["failed"] and tts_first_chunk_ms is None and (
                        current_sarvam_key() != key_used or rotate_sarvam_key()):
                    quota["failed"] = False
                    audio_task.cancel()
                    if ping_task is not None:
                        ping_task.cancel()
                    await raw_ws.close()

                    raw_ws, ws, key_used = await connect_and_configure_tts()
                    ping_task = asyncio.create_task(keepalive_ping(ws))
                    audio_task = asyncio.create_task(read_audio())
                    tts_start = time.time()
                    try:
                        await ws.convert(text_to_speak)
                        await ws.flush()
                        await asyncio.wait_for(audio_task, timeout=90.0)
                    except asyncio.TimeoutError:
                        audio_task.cancel()
                    except websockets.ConnectionClosed:
                        quota["failed"] = True

                if quota["failed"]:
                    await queue.put({"type": "error", "error":
                                     "Narration unavailable: every Sarvam API key "
                                     "in SARVAM_API_KEY failed (most likely out of credits)."})
            except Exception as e:
                await queue.put({"type": "error", "error": f"TTS Pipeline error: {str(e)}"})
            finally:
                if ping_task is not None:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
                if raw_ws is not None:
                    await raw_ws.close()
                await queue.put({"type": "done"})

        pipeline_task = asyncio.create_task(run_pipeline())
        if req.request_id:
            active_pipelines[req.request_id] = pipeline_task
            pipeline_task.add_done_callback(
                lambda t: active_pipelines.pop(req.request_id, None)
            )

        while True:
            item = await queue.get()
            if item["type"] == "done":
                break
            yield f"data: {json.dumps(item)}\n\n"

        total_ms = int((time.time() - start_time) * 1000)
        metrics_event = {
            "type": "metrics",
            "vlm_ms": 0,
            "tts_ms": tts_first_chunk_ms or 0,
            "total_ms": total_ms
        }
        yield f"data: {json.dumps(metrics_event)}\n\n"
        try:
            await pipeline_task
        except asyncio.CancelledError:
            pass

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


@app.get("/")
async def read_root():
    return FileResponse("static/index.html")

@app.get("/style.css")
async def read_css():
    return FileResponse("static/style.css", media_type="text/css")

@app.get("/script.js")
async def read_js():
    return FileResponse("static/script.js", media_type="application/javascript")
