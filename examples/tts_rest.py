"""Bulbul v3 via REST: one request in, one complete audio file out.

Simplest of the three TTS paths. You wait for the whole clip to be
synthesised, then save it. Good for short, fixed strings.
"""

import os

from dotenv import load_dotenv
from sarvamai import SarvamAI
from sarvamai.play import save

load_dotenv()
# The app supports several comma-separated keys for failover; a single
# key works fine here, so just take the first one.
api_key = os.environ["SARVAM_API_KEY"].split(",")[0].strip()

client = SarvamAI(api_subscription_key=api_key)

response = client.text_to_speech.convert(
    text="नमस्ते! सर्वम् की दुनिया में आपका स्वागत है।",
    language_code="hi-IN",
    model="bulbul:v3",
    speaker="priya",
)

save(response, "rest_output.wav")
print("Saved rest_output.wav")
