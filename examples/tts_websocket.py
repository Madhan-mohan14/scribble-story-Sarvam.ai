"""Bulbul v3 over WebSocket: one connection, text pushed in as it becomes
available, audio streaming back the whole time.

This is the only one of the three where you do NOT need the full text up
front - which is what makes it the right fit for an LLM that is still
generating.

Sending and receiving run concurrently, which is the entire point: audio
for sentence 1 comes back while sentence 3 is still being written.
"""

import asyncio
import base64
import os
import time

from dotenv import load_dotenv
from sarvamai import AsyncSarvamAI, AudioOutput, ErrorResponse, EventResponse

load_dotenv()
api_key = os.environ["SARVAM_API_KEY"].split(",")[0].strip()

SENTENCES = [
    "भारत की संस्कृति विश्व की सबसे प्राचीन संस्कृतियों में से एक है। ",
    "यह विविधता और परंपराओं का अद्भुत संगम है। ",
    "इसमें विभिन्न धर्म, भाषाएं, त्योहार और संगीत शामिल हैं।",
]


async def main():
    client = AsyncSarvamAI(api_subscription_key=api_key)

    async with client.text_to_speech_streaming.connect(
        model="bulbul:v3", send_completion_event=True
    ) as ws:
        # Config must be the first message. Note the Python keyword is
        # target_language_code here, while the REST client takes
        # language_code - same concept, different layer.
        await ws.configure(
            target_language_code="hi-IN",
            speaker="priya",
            output_audio_codec="mp3",
            min_buffer_size=30,
            max_chunk_length=150,
        )

        start = time.time()
        stats = {"first_ms": None, "chunks": 0}

        async def send_text():
            # Feed sentences in slowly, the way an LLM produces them.
            for sentence in SENTENCES:
                await ws.convert(sentence)
                await asyncio.sleep(0.6)
            # Without this, any tail shorter than min_buffer_size is
            # never spoken - the last words just go missing.
            await ws.flush()

        async def read_audio():
            with open("ws_output.mp3", "wb") as f:
                async for message in ws:
                    if isinstance(message, AudioOutput):
                        if stats["first_ms"] is None:
                            stats["first_ms"] = int((time.time() - start) * 1000)
                            print(f"First audio at {stats['first_ms']} ms "
                                  f"- still sending text")
                        stats["chunks"] += 1
                        f.write(base64.b64decode(message.data.audio))
                    elif isinstance(message, EventResponse):
                        if message.data.event_type == "final":
                            break
                    elif isinstance(message, ErrorResponse):
                        print(f"Error: {message.data.message}")
                        break

        await asyncio.gather(send_text(), read_audio())

    total_ms = int((time.time() - start) * 1000)
    print(f"Saved ws_output.mp3 ({stats['chunks']} chunks in {total_ms} ms)")


if __name__ == "__main__":
    asyncio.run(main())
