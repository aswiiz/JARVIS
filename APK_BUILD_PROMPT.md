# Prompt: Build the JARVIS Android App (APK)

Copy everything below the line into Claude, Lovable, Bolt, Cursor, or any AI app builder.

---

Build a mobile-first chat app that I can package as a free Android APK with **Capacitor** (React + Vite + TypeScript, or plain HTML/JS if simpler). It is a front end only, for an existing backend called "JARVIS Backend" that I host on Render.

## Core screens

1. **Settings** (first launch, reachable from a gear icon)
   - "Backend URL" (e.g. `https://jarvis-backend.onrender.com`, no trailing slash)
   - "App token" (password field)
   - "Test connection" button that calls `GET {url}/health` and shows OK or the error
   - Save both with `@capacitor/preferences`. Never hardcode URLs or keys. The app must NEVER contain an OpenAI or Hugging Face key.
2. **Chat**
   - Message bubbles (user right, assistant left), auto-scroll, multi-line input, Send button
   - Attach button: pick an image (gallery or camera) or an audio file; show a small preview chip before sending; up to 4 files per message
   - Assistant replies can include generated images or media: render each `file_url` returned by the backend inline (images tap-to-zoom, audio with a player)
   - "Show steps" expandable section under each assistant reply listing the `tasks` array (task name, model, status, error if any)
   - "New chat" button that creates a new `session_id`
   - Typing/loading indicator while waiting
3. **History** (optional drawer): load past messages for the current session via `GET /history/{session_id}`

## Backend API contract (exact)

All requests except `/health` and `/files/...` need header `X-API-Token: <app token>`.

- `GET /health` -> `{"status":"ok","storage":"mongodb"|"memory","llm_providers":["groq","gemini"],"huggingface":true}`
- `POST /chat` (JSON)
  ```json
  {
    "message": "How many zebras are in this picture?",
    "session_id": "abc123",
    "files": [{"name": "photo.jpg", "mime": "image/jpeg", "data": "<base64, no data: prefix>"}]
  }
  ```
  Response:
  ```json
  {
    "session_id": "abc123",
    "response": "There are 3 zebras...",
    "tasks": [
      {"id": 0, "task": "object-detection", "model": "facebook/detr-resnet-50",
       "status": "done", "text": "Detected 3 objects: zebra x3", "file_url": null, "mime": null, "error": null}
    ]
  }
  ```
  `file_url` (when present) is a full public URL to a generated image/audio file.
  Generate the `session_id` on the client (random 16+ chars, letters/digits/`-`/`_`) and persist it.
- `GET /history/{session_id}` -> `{"session_id": "...", "messages": [{"role":"user"|"assistant","content":"..."}]}`
- Error responses are JSON `{"error": "message"}` with status 400, 401, 413, 500, or 503. Show the message in a dismissible toast.

## Important behaviors

- **Cold starts:** the free Render server may be asleep. Before the first request after the app opens (or after 10 minutes of inactivity), call `/health` and, if it fails or takes longer than 5 seconds, show "Waking up the server..." and retry every 5 seconds for up to 90 seconds. Use a 180-second timeout for `/chat` because multi-model requests are slow.
- Resize images client-side to a max of 1600px on the long side (JPEG quality 0.85) before base64 encoding, to keep uploads small.
- Keep a local chat cache (Capacitor Preferences or localStorage) so the conversation is visible offline.
- Handle: no internet, wrong token (401), file too large (413), server error, with friendly messages.
- Android permissions only for camera/storage as needed (Capacitor Camera plugin).

## Design

- Mobile-first, clean, dark and light themes following the system setting
- Rounded bubbles, safe-area padding, large touch targets, keyboard-aware input bar
- App name "JARVIS", simple robot/spark icon and splash screen

## Deliverables

1. Complete source code
2. Exact commands to build a debug APK for free:
   ```
   npm install
   npm run build
   npx cap add android
   npx cap sync
   cd android && ./gradlew assembleDebug
   ```
   (APK output: `android/app/build/outputs/apk/debug/app-debug.apk`)
3. A GitHub Actions workflow (`.github/workflows/build-apk.yml`) that builds the debug APK on every push using Ubuntu, Node 20, and Java 17, and uploads it as a downloadable artifact, so I can build without installing Android Studio
4. Short setup notes for entering the Backend URL and token in Settings
