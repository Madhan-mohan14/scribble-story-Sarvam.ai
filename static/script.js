const canvas = document.getElementById("drawCanvas");
const ctx = canvas.getContext("2d");
const placeholder = document.getElementById("canvasPlaceholder");
const colorPalette = document.getElementById("colorPalette");
const brushSizeInput = document.getElementById("brushSize");

let currentColor = "#1a1a1a";
let currentBrushSize = Number(brushSizeInput.value);
let hasDrawn = false;
let isDrawing = false;
let lastPoint = null;

function canvasPoint(event) {
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  return {
    x: (event.clientX - rect.left) * scaleX,
    y: (event.clientY - rect.top) * scaleY,
  };
}

function startStroke(event) {
  isDrawing = true;
  lastPoint = canvasPoint(event);
  ctx.fillStyle = currentColor;
  ctx.beginPath();
  ctx.arc(lastPoint.x, lastPoint.y, currentBrushSize / 2, 0, Math.PI * 2);
  ctx.fill();
  if (!hasDrawn) {
    hasDrawn = true;
    placeholder.style.display = "none";
    onCanvasChanged();
  }
}

function continueStroke(event) {
  if (!isDrawing) return;
  const point = canvasPoint(event);
  ctx.strokeStyle = currentColor;
  ctx.lineWidth = currentBrushSize;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.moveTo(lastPoint.x, lastPoint.y);
  ctx.lineTo(point.x, point.y);
  ctx.stroke();
  lastPoint = point;
}

function endStroke() {
  if (!isDrawing) return;
  isDrawing = false;
  lastPoint = null;
  pushUndoSnapshot();
}

canvas.addEventListener("pointerdown", startStroke);
canvas.addEventListener("pointermove", continueStroke);
canvas.addEventListener("pointerup", endStroke);
canvas.addEventListener("pointerleave", endStroke);

colorPalette.addEventListener("click", (event) => {
  const swatch = event.target.closest(".swatch");
  if (!swatch) return;
  currentColor = swatch.dataset.color;
  colorPalette.querySelectorAll(".swatch").forEach((el) => el.classList.remove("active"));
  swatch.classList.add("active");
});

brushSizeInput.addEventListener("input", () => {
  currentBrushSize = Number(brushSizeInput.value);
});

// --- PCM Audio Playback Queue ---
let audioCtx = null;
let nextPlayTime = 0;
let currentAudioSources = [];

function stopAudioPlayback() {
  currentAudioSources.forEach((source) => {
    try {
      source.stop();
    } catch (e) {}
  });
  currentAudioSources = [];
  nextPlayTime = 0;
  updateStopVisibility();
}

function playPcmChunk(base64Data, sampleRate = 24000) {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    nextPlayTime = audioCtx.currentTime;
  }

  if (audioCtx.state === "suspended") {
    audioCtx.resume();
  }

  const binaryString = atob(base64Data);
  const len = binaryString.length;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    bytes[i] = binaryString.charCodeAt(i);
  }

  const int16Array = new Int16Array(bytes.buffer);
  const float32Array = new Float32Array(int16Array.length);
  for (let i = 0; i < int16Array.length; i++) {
    float32Array[i] = int16Array[i] / 32768.0;
  }

  const audioBuffer = audioCtx.createBuffer(1, float32Array.length, sampleRate);
  audioBuffer.copyToChannel(float32Array, 0);

  const source = audioCtx.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(audioCtx.destination);

  currentAudioSources.push(source);
  source.onended = () => {
    const index = currentAudioSources.indexOf(source);
    if (index > -1) {
      currentAudioSources.splice(index, 1);
    }
    // Audio outlives the request: the last chunk arrives long before it
    // finishes playing, so the Stop button has to survive past the stream.
    updateStopVisibility();
  };

  const startTime = Math.max(nextPlayTime, audioCtx.currentTime);
  source.start(startTime);
  nextPlayTime = startTime + audioBuffer.duration;
}

// --- Streaming API Consumer ---
async function consumeStream(url, body, onChunk, onText, onTranslatedText, onMetrics, onError, signal) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok) {
    const errorData = await response.json();
    throw new Error(errorData.detail || "Request failed");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n\n");
    buffer = lines.pop(); // keep partial line

    for (const line of lines) {
      if (line.startsWith("data: ")) {
        try {
          const payload = JSON.parse(line.substring(6));
          if (payload.type === "text") {
            onText(payload.text);
          } else if (payload.type === "audio") {
            onChunk(payload.audio);
          } else if (payload.type === "translated_text") {
            onTranslatedText(payload.text);
          } else if (payload.type === "metrics") {
            onMetrics(payload);
          } else if (payload.type === "error") {
            onError(payload.error);
          }
        } catch (e) {
          console.error("Error parsing SSE JSON line:", e, line);
        }
      }
    }
  }
}

// --- Language selection + Make Story button state ---
const languageSelect = document.getElementById("languageSelect");
const makeStoryBtn = document.getElementById("makeStoryBtn");
let selectedLanguage = languageSelect.value;

function onCanvasChanged() {
  makeStoryBtn.disabled = !hasDrawn;
}

languageSelect.addEventListener("change", () => {
  selectedLanguage = languageSelect.value;
});

const storyCard = document.getElementById("storyCard");
const storyContent = document.getElementById("storyContent");
const languageChips = document.getElementById("languageChips");
const stackPanel = document.getElementById("stackPanel");
const stackStatus = document.getElementById("stackStatus");
const metricVlm = document.getElementById("metricVlm");
const metricTtfa = document.getElementById("metricTtfa");
const metricTotal = document.getElementById("metricTotal");
const stopBtn = document.getElementById("stopBtn");

let currentStoryText = "";
let storyLanguage = "en-IN"; // whatever language the story was ACTUALLY generated in
let activeAbortController = null;
let activeRequestId = null;

// Prevents overlapping /generate + /narrate streams writing into the same
// audio queue: while any request is in flight, the language dropdown and
// chips (and, in the makeStoryBtn handler below, the button itself) are
// disabled, so a real user click can't start a second stream mid-request.
// The stop button is the mirror image - shown only while something is
// actually running, so there's always exactly one way to change state.
// Stop stays available while EITHER a request is in flight or audio is
// still playing. Sarvam delivers the narration far faster than realtime
// (a ~40s story arrives in ~20s), so tying Stop to the request alone left
// the last half of the audio unstoppable.
let isRequestActive = false;

function updateStopVisibility() {
  stopBtn.hidden = !isRequestActive && currentAudioSources.length === 0;
}

function setControlsLocked(locked) {
  languageSelect.disabled = locked;
  languageChips.querySelectorAll(".chip").forEach((el) => {
    el.disabled = locked;
  });
  makeStoryBtn.disabled = locked || !hasDrawn;
  isRequestActive = locked;
  updateStopVisibility();
}

// Tells the backend to actually cancel the in-flight Gemini/Sarvam pipeline
// (not just abandon the connection) via its request_id, aborts the fetch for
// immediate UI responsiveness, and kills any audio already queued/playing.
stopBtn.addEventListener("click", () => {
  if (activeRequestId) {
    fetch("/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: activeRequestId }),
    }).catch(() => {});
    activeRequestId = null;
  }
  if (activeAbortController) {
    activeAbortController.abort();
    activeAbortController = null;
  }
  stopAudioPlayback();
  setControlsLocked(false);
  stackStatus.innerText = "Stopped.";
});

// Wire up language chips clicks
languageChips.addEventListener("click", async (event) => {
  const chip = event.target.closest(".chip");
  if (!chip || !currentStoryText) return;

  const targetLang = chip.dataset.lang;

  // Update active chip UI
  languageChips.querySelectorAll(".chip").forEach((el) => el.classList.remove("active"));
  chip.classList.add("active");

  setControlsLocked(true);
  stopAudioPlayback();
  stackStatus.innerText = `Translating and narrating in ${chip.innerText}...`;
  metricTtfa.innerText = "—";
  metricTotal.innerText = "—";

  const controller = new AbortController();
  activeAbortController = controller;
  const requestId = crypto.randomUUID();
  activeRequestId = requestId;

  let hasError = false;
  try {
    await consumeStream(
      "/narrate",
      {
        text: currentStoryText,
        language_code: targetLang,
        source_language_code: storyLanguage,
        request_id: requestId,
      },
      (audioChunk) => {
        playPcmChunk(audioChunk);
      },
      () => {},
      (translatedText) => {
        storyContent.innerText = translatedText;
      },
      (metrics) => {
        if (metrics.tts_ms) {
          metricTtfa.innerText = `${metrics.tts_ms}ms`;
        }
        if (metrics.total_ms) {
          metricTotal.innerText = `${metrics.total_ms}ms`;
        }
      },
      (err) => {
        hasError = true;
        stackStatus.innerText = `Error: ${err}`;
      },
      controller.signal
    );
    if (!hasError) {
      stackStatus.innerText = `Narration complete!`;
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      // AbortError means the Stop button was clicked - it already set its
      // own "Stopped." status, so don't overwrite it with an error message.
      console.error("Narration failed:", err);
      stackStatus.innerText = `Error: ${err.message}`;
    }
  } finally {
    if (activeAbortController === controller) {
      activeAbortController = null;
    }
    if (activeRequestId === requestId) {
      activeRequestId = null;
    }
    setControlsLocked(false);
  }
});

makeStoryBtn.addEventListener("click", async () => {
  if (!hasDrawn) return;

  setControlsLocked(true);
  makeStoryBtn.innerText = "Generating story...";

  stackPanel.classList.add("visible");
  stackStatus.innerText = "Analyzing drawing with Gemini 2.5 Flash...";
  
  storyContent.innerText = "";
  storyCard.style.display = "block";
  currentStoryText = "";
  storyLanguage = selectedLanguage; // Gemini writes the story directly in this language
  stopAudioPlayback();

  metricVlm.innerText = "—";
  metricTtfa.innerText = "—";
  metricTotal.innerText = "—";

  // Reset active chip to matches dropdown or default
  languageChips.querySelectorAll(".chip").forEach((el) => {
    if (el.dataset.lang === selectedLanguage) {
      el.classList.add("active");
    } else {
      el.classList.remove("active");
    }
  });

  const controller = new AbortController();
  activeAbortController = controller;
  const requestId = crypto.randomUUID();
  activeRequestId = requestId;

  let hasError = false;
  try {
    const dataUrl = canvas.toDataURL("image/png");

    await consumeStream(
      "/generate",
      { image: dataUrl, language_code: selectedLanguage, stream: true, request_id: requestId },
      (audioChunk) => {
        playPcmChunk(audioChunk);
      },
      (textChunk) => {
        currentStoryText += textChunk;
        storyContent.innerText = currentStoryText;
      },
      () => {},
      (metrics) => {
        if (metrics.vlm_ms) {
          metricVlm.innerText = `${metrics.vlm_ms}ms`;
        }
        if (metrics.tts_ms) {
          metricTtfa.innerText = `${metrics.tts_ms}ms`;
        }
        if (metrics.total_ms) {
          metricTotal.innerText = `${metrics.total_ms}ms`;
        }
      },
      (err) => {
        hasError = true;
        stackStatus.innerText = `Error: ${err}`;
      },
      controller.signal
    );

    if (!hasError) {
      stackStatus.innerText = "Story generated!";
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      console.error("Error generating story:", err);
      stackStatus.innerText = `Error: ${err.message}`;
      storyContent.innerText = `Could not generate story. ${err.message}`;
    }
  } finally {
    if (activeAbortController === controller) {
      activeAbortController = null;
    }
    if (activeRequestId === requestId) {
      activeRequestId = null;
    }
    setControlsLocked(false);
    makeStoryBtn.innerHTML = '<span class="make-story-icon">&#8645;</span> Make story';
  }
});

// --- Undo and clear ---
const undoBtn = document.getElementById("undoBtn");
const clearBtn = document.getElementById("clearBtn");
const MAX_UNDO_STEPS = 20;
let undoStack = [];

function pushUndoSnapshot() {
  undoStack.push(ctx.getImageData(0, 0, canvas.width, canvas.height));
  if (undoStack.length > MAX_UNDO_STEPS) {
    undoStack.shift();
  }
}

undoBtn.addEventListener("click", () => {
  if (undoStack.length === 0) return;
  undoStack.pop();
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (undoStack.length > 0) {
    ctx.putImageData(undoStack[undoStack.length - 1], 0, 0);
  } else {
    hasDrawn = false;
    placeholder.style.display = "block";
    onCanvasChanged();
  }
});

clearBtn.addEventListener("click", () => {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  undoStack = [];
  hasDrawn = false;
  placeholder.style.display = "block";
  onCanvasChanged();
});
