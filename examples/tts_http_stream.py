"""Bulbul v3 via HTTP stream: still one request, but bytes arrive as they
are synthesised instead of all at the end.

You can start writing (or playing) audio before the sentence is finished,
without taking on a WebSocket. The text is still fixed up front.
"""

import os
import time

from dotenv import load_dotenv
from sarvamai import SarvamAI

load_dotenv()
api_key = os.environ["SARVAM_API_KEY"].split(",")[0].strip()

client = SarvamAI(api_subscription_key=api_key)

start = time.time()
first_chunk_ms = None
chunks = 0

with open("stream_output.mp3", "wb") as f:
    for chunk in client.text_to_speech.convert_stream(
        text="भारत की संस्कृति विश्व की सबसे प्राचीन और समृद्ध संस्कृतियों में से एक है।",
        language_code="hi-IN",
        model="bulbul:v3",
        speaker="priya",
        output_audio_codec="mp3",
    ):
        if first_chunk_ms is None:
            first_chunk_ms = int((time.time() - start) * 1000)
        chunks += 1
        f.write(chunk)

print(f"First chunk after {first_chunk_ms} ms")
print(f"Saved stream_output.mp3 ({chunks} chunks)")
