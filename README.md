# JARVIS Backend (online mode) for Render

Flask backend that runs a lightweight JARVIS / HuggingGPT pipeline:
A free LLM API (Groq, Gemini, Mistral, OpenRouter, NVIDIA, LLM7, with automatic fallback) plans the tasks and writes the answer; Hugging Face models run the tasks.
MongoDB stores chat history and generated files. A background loop keeps the free Render service awake.

## Files
- `app.py` routes: `/health`, `/chat`, `/history/<session>`, `/files/<token>`
- `jarvis_core.py` the plan, execute, respond pipeline
- `prompts.py` planner and response prompts
- `db.py` MongoDB (GridFS for files) with in-memory fallback
- `keepalive.py` the continuous self-ping loop
- `render.yaml` Render blueprint
- `APK_BUILD_PROMPT.md` prompt for building the Android app
- `.github/workflows/keepalive-backup.yml` optional external pinger

## Deploy (free)
1. **MongoDB Atlas**: create a free M0 cluster, add a database user, and under Network Access allow `0.0.0.0/0`. Copy the connection string.
2. **GitHub**: put the contents of this folder in a new repo.
3. **Render**: New, then Blueprint (or Web Service), pick the repo.
4. In the Render dashboard, set the secret env vars (never commit them):
   - At least one LLM key: `GROQ_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY` or `OPENROUTER_API_KEY` (free keys from each provider's console; list: github.com/mnfst/awesome-free-llm-apis). `llm7` works with no key at all.
   - `HUGGINGFACE_ACCESS_TOKEN` (needs permission to call Inference Providers)
   - `MONGODB_URI`
   - `APP_API_TOKEN` (a long random string; the APK sends it as `X-API-Token`)
5. Open `https://<your-service>.onrender.com/health`.

## Test
```bash
curl -s https://<service>.onrender.com/health
curl -s -X POST https://<service>.onrender.com/chat \
  -H "Content-Type: application/json" -H "X-API-Token: <APP_API_TOKEN>" \
  -d '{"message":"Generate an image of a red fox in snow"}'
```

## Keep-alive loop
`keepalive.py` starts with the app and requests `<service>/health` every `PING_INTERVAL_SECONDS` (default 240, minimum 1 second).
- Anything under ~14 minutes is enough; faster only wastes bandwidth.
- It can't wake a service that is already asleep. For that, add the optional GitHub Actions workflow (set repo variable `SERVICE_URL`) or UptimeRobot.
- The free tier has a monthly hour limit, so one always-on service uses most of it. Check Render's current terms.
- Keep `--workers 1` in the start command so only one loop runs.

## Notes
- Model availability on Hugging Face changes. If a task fails, override models with `HF_MODELS_JSON`, e.g. `{"text-to-image":["black-forest-labs/FLUX.1-schnell"]}`.
- LLM fallback: providers are tried in `LLM_PROVIDER_ORDER`; one that errors or hits its rate limit is skipped for 30 seconds. Override a model with `LLM_<NAME>_MODEL` (e.g. `LLM_GROQ_MODEL`). Add any other OpenAI-compatible provider with `CUSTOM_LLM_BASE_URL`, `CUSTOM_LLM_API_KEY`, `CUSTOM_LLM_MODEL`.
- Each chat message uses 2 LLM calls (plan + answer). Free tiers are rate limited, and several providers may use free-tier prompts for training, so avoid sending private data.
- Free model names change often. If a provider starts failing, check its model list and set `LLM_<NAME>_MODEL`.
- Generated files are deleted after `FILE_TTL_DAYS` (default 7) on startup, to stay inside Atlas's free 512 MB.
- Local run: `pip install -r requirements.txt`, copy `.env.example` to `.env`, set `ALLOW_NO_AUTH=true` if you skip the token, then `python app.py`.

## MongoDB setup (recommended)
Create a dedicated Atlas database user for this project instead of reusing an admin or shared one:
Atlas, Database Access, Add New Database User, built-in role "Read and write to any database" (or a custom role limited to the `jarvis` database). Use a long random password.
Put the resulting `mongodb+srv://...` string in the Render env var `MONGODB_URI`. Never commit it.
The app only uses its own database (`MONGODB_DB`, default `jarvis`), so it will not touch other databases on the same cluster.
In Atlas, Network Access must allow `0.0.0.0/0` because Render's free IPs change.
