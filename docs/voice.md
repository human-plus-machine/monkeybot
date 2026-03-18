# Voice

monkey-bot supports voice input and output via the `/voice` endpoint, powered by Google Cloud Speech-to-Text (STT) and Text-to-Speech (TTS).

---

## Overview

```
Client sends audio file (POST /voice)
          │
          ▼
VoiceHandler: Speech-to-Text
    GCP STT → transcript text
          │
          ▼
Agent processes text as normal message
    Returns text response
          │
          ▼
VoiceHandler: Text-to-Speech
    GCP TTS → audio bytes
          │
          ▼
Client receives:
    - Audio response (OGG Opus)
    - X-Transcript-In header (what the user said)
    - X-Transcript-Out header (what the agent said)
    - X-Trace-Id header
```

---

## Setup

### 1. Enable GCP APIs

```bash
gcloud services enable \
    speech.googleapis.com \
    texttospeech.googleapis.com \
    --project=your-gcp-project
```

### 2. Install voice dependencies

```bash
pip install "emonk[voice]"
# or explicitly:
pip install google-cloud-speech google-cloud-texttospeech
```

### 3. Configure

```bash
# .env or GCP Secret Manager
VOICE_ENABLED=true
VOICE_STT_LANGUAGE_CODE=en-US
VOICE_STT_MODEL=latest_long
VOICE_TTS_VOICE_NAME=en-US-Journey-F
VOICE_TTS_AUDIO_ENCODING=OGG_OPUS
```

### 4. Verify service account permissions

The service account needs access to both speech APIs:

```bash
gcloud projects add-iam-policy-binding your-project \
    --member="serviceAccount:your-sa@your-project.iam.gserviceaccount.com" \
    --role="roles/speech.client"
```

---

## The /voice Endpoint

```
POST /voice
Content-Type: audio/ogg; codecs=opus (or audio/webm, audio/wav, etc.)
Authorization: Bearer <user-email>   (optional — uses ALLOWED_USERS check)
```

**Validation:**
- Supported MIME types: `audio/ogg`, `audio/webm`, `audio/wav`, `audio/flac`, `audio/mp4`, `audio/mpeg`
- Max file size: configurable (default 10MB)
- User validation via `ALLOWED_USERS` if configured

**Response:**
```
HTTP/2 200
Content-Type: audio/ogg; codecs=opus
X-Transcript-In: <what the user said>
X-Transcript-Out: <what the agent said>
X-Trace-Id: <request trace ID>

<binary audio data>
```

---

## Testing Voice Locally

```bash
# Record a voice message (requires sox or ffmpeg)
rec -r 16000 -c 1 -b 16 message.wav trim 0 5

# Send to the voice endpoint
curl -X POST http://localhost:8080/voice \
    -H "Content-Type: audio/wav" \
    --data-binary @message.wav \
    --output response.ogg \
    -D -

# Play the response
play response.ogg
```

Or use Python:

```python
import httpx

with open("message.wav", "rb") as f:
    audio_data = f.read()

response = httpx.post(
    "http://localhost:8080/voice",
    content=audio_data,
    headers={"Content-Type": "audio/wav"},
)

print("You said:", response.headers["X-Transcript-In"])
print("Bot said:", response.headers["X-Transcript-Out"])

with open("response.ogg", "wb") as f:
    f.write(response.content)
```

---

## Configuration Reference

### Speech-to-Text Options

| Variable | Default | Options | Description |
|---|---|---|---|
| `VOICE_STT_LANGUAGE_CODE` | `en-US` | Any BCP-47 code | Recognition language |
| `VOICE_STT_MODEL` | `latest_long` | See below | STT model to use |

**STT Models:**

| Model | Best For |
|---|---|
| `latest_long` | Long-form dictation, general use |
| `latest_short` | Short commands, queries |
| `telephony` | Phone call audio |
| `medical_dictation` | Medical transcription |
| `medical_conversation` | Multi-speaker medical |

**Supported languages (examples):**

| Code | Language |
|---|---|
| `en-US` | English (United States) |
| `en-GB` | English (United Kingdom) |
| `es-US` | Spanish (United States) |
| `fr-FR` | French (France) |
| `de-DE` | German (Germany) |
| `ja-JP` | Japanese |
| `zh-CN` | Chinese (Simplified) |
| `pt-BR` | Portuguese (Brazil) |

Full list: https://cloud.google.com/speech-to-text/docs/languages

---

### Text-to-Speech Options

| Variable | Default | Description |
|---|---|---|
| `VOICE_TTS_VOICE_NAME` | `en-US-Journey-F` | Voice name for TTS |
| `VOICE_TTS_AUDIO_ENCODING` | `OGG_OPUS` | Audio output format |

**Voice Names:**

Google offers three tiers of TTS voices:

| Tier | Example | Quality | Cost |
|---|---|---|---|
| Journey | `en-US-Journey-F` | Highest (neural, expressive) | Higher |
| Neural2 | `en-US-Neural2-A` | High | Medium |
| Standard | `en-US-Standard-A` | Good | Lower |

Browse all voices: https://cloud.google.com/text-to-speech/docs/voices

**Common journey voices:**

| Voice Name | Gender | Style |
|---|---|---|
| `en-US-Journey-F` | Female | Natural, warm |
| `en-US-Journey-M` | Male | Natural, professional |
| `en-US-Journey-D` | Male | Deep, authoritative |

**Audio Encodings:**

| Encoding | Format | Best For |
|---|---|---|
| `OGG_OPUS` | OGG with Opus codec | Web/app playback (default) |
| `MP3` | MPEG Layer III | Broad compatibility |
| `LINEAR16` | 16-bit PCM WAV | High quality, large files |
| `MULAW` | 8-bit PCM | Telephony |

---

## Building Voice-Enabled Applications

### Web App (JavaScript)

```javascript
// Record and send voice input
const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
const recorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
const chunks = [];

recorder.ondataavailable = (e) => chunks.push(e.data);
recorder.onstop = async () => {
    const blob = new Blob(chunks, { type: 'audio/webm' });
    
    const response = await fetch('/voice', {
        method: 'POST',
        headers: { 'Content-Type': 'audio/webm' },
        body: blob,
    });
    
    // Play audio response
    const audioBlob = await response.blob();
    const audioUrl = URL.createObjectURL(audioBlob);
    const audio = new Audio(audioUrl);
    audio.play();
    
    // Show transcripts
    console.log('You said:', response.headers.get('X-Transcript-In'));
    console.log('Bot said:', response.headers.get('X-Transcript-Out'));
};

recorder.start();
setTimeout(() => recorder.stop(), 5000); // 5 second recording
```

### Mobile App (Python/Kivy or React Native)

The endpoint accepts any standard audio format. Send the audio as the raw request body with the correct `Content-Type` header.

---

## Voice + Google Chat

Voice is separate from Google Chat — it's a standalone REST endpoint designed for:
- Custom mobile or web apps
- Voice assistants
- Phone systems (Twilio, etc.)
- IoT devices

Google Chat does not natively support audio responses, so voice is not wired into the `/webhook` endpoint.

---

## Troubleshooting

### "VOICE_ENABLED is false"

```bash
# Ensure it's set in your environment
VOICE_ENABLED=true

# Or check for typos — it must be exactly "true" (lowercase)
```

### "Unsupported audio format"

The endpoint validates MIME types. Ensure your `Content-Type` header matches the actual audio format:

```bash
# Correct
curl -H "Content-Type: audio/wav" --data-binary @file.wav ...

# Wrong (will be rejected)
curl -H "Content-Type: audio/mp3" --data-binary @file.wav ...
```

### STT returns empty transcript

Common causes:
- Audio file is silent or corrupted
- Language code doesn't match the spoken language
- Audio quality too low (background noise, low volume)

Try with a cleaner recording or switch to `latest_short` model for command-style input.

### "Permission denied" from GCP Speech API

```bash
# Verify the speech.client role
gcloud projects get-iam-policy your-project \
    --flatten="bindings[].members" \
    --filter="bindings.role=roles/speech.client"

# Add if missing
gcloud projects add-iam-policy-binding your-project \
    --member="serviceAccount:your-sa@your-project.iam.gserviceaccount.com" \
    --role="roles/speech.client"
```
