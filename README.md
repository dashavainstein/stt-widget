# STT Widget

Floating speech-to-text button for Windows. Click or press hotkey — speak — text gets pasted into any app.

Uses Groq Whisper API for fast transcription.

## Setup

```bash
pip install -r requirements.txt
```

Set your Groq API key:
```bash
set GROQ_API_KEY=your-key-here
```

## Run

```bash
python stt_widget.py
```

## How it works

- **Left-click** or **Ctrl+Shift+Space** — toggle recording
- **Right-click drag** — move the button
- Transcribed text is automatically pasted into the active window
- Sits in system tray when minimized
