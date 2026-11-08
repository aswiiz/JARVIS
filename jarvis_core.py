"""Lightweight JARVIS / HuggingGPT pipeline for online mode.

Stages (same idea as the original paper):
  1. Task planning    - an OpenAI model turns the request into a task list (JSON)
  2. Model selection  - each task has an ordered list of Hugging Face models; first working one wins
  3. Task execution   - calls the Hugging Face Inference API (nothing runs locally)
  4. Response         - an OpenAI model writes the final answer from the results
"""
import io
import json
import logging
import os
import re
import time
from collections import Counter

from huggingface_hub import InferenceClient
from openai import OpenAI

from prompts import PLANNER_SYSTEM, RESPONSE_SYSTEM

log = logging.getLogger("jarvis.core")


class MissingConfig(RuntimeError):
    pass


# Free / OpenAI-compatible LLM providers (see github.com/mnfst/awesome-free-llm-apis).
# name: (api key env var, base URL, default model, key required)
LLM_PROVIDERS = {
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b", True),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash", True),
    "mistral": ("MISTRAL_API_KEY", "https://api.mistral.ai/v1", "mistral-small-latest", True),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "google/gemma-4-31b-it:free", True),
    "nvidia": ("NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1", "openai/gpt-oss-20b", True),
    "llm7": ("LLM7_API_KEY", "https://api.llm7.io/v1", "gpt-oss:20b", False),  # works without a key
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini", True),  # optional, paid
}
DEFAULT_ORDER = "groq,gemini,mistral,openrouter,nvidia,llm7,openai,custom"


TASK_REGISTRY = {
    "image-to-text": {
        "input": "image",
        "desc": "Describe or caption an image",
        "models": [
            "Salesforce/blip-image-captioning-large",
            "Salesforce/blip-image-captioning-base",
            "nlpconnect/vit-gpt2-image-captioning",
        ],
    },
    "object-detection": {
        "input": "image",
        "desc": "Detect and count objects in an image",
        "models": ["facebook/detr-resnet-50", "hustvl/yolos-small"],
    },
    "image-classification": {
        "input": "image",
        "desc": "Classify what an image shows",
        "models": ["google/vit-base-patch16-224"],
    },
    "text-to-image": {
        "input": "text",
        "desc": "Generate an image from a text prompt",
        "models": [
            "black-forest-labs/FLUX.1-schnell",
            "stabilityai/stable-diffusion-xl-base-1.0",
            "runwayml/stable-diffusion-v1-5",
        ],
    },
    "automatic-speech-recognition": {
        "input": "audio",
        "desc": "Transcribe speech from an audio file",
        "models": ["openai/whisper-large-v3", "openai/whisper-small"],
    },
    "token-classification": {
        "input": "text",
        "desc": "Named entity recognition (people, places, organizations)",
        "models": ["dslim/bert-base-NER"],
    },
    "text-classification": {
        "input": "text",
        "desc": "Sentiment analysis / text classification",
        "models": ["distilbert/distilbert-base-uncased-finetuned-sst-2-english"],
    },
    "summarization": {
        "input": "text",
        "desc": "Summarize a long text",
        "models": ["facebook/bart-large-cnn", "sshleifer/distilbart-cnn-12-6"],
    },
}


def _g(obj, key, default=None):
    """Read a field from a dict-like or attribute-style HF output."""
    if isinstance(obj, dict) and key in obj:
        return obj[key]
    return getattr(obj, key, default)


def _parse_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
    return {}


class Jarvis:
    def __init__(self):
        self.hf_token = os.getenv("HUGGINGFACE_ACCESS_TOKEN", "").strip()
        self.max_tasks = int(os.getenv("MAX_TASKS", "6"))
        self._hf = None
        self._cooldown = {}  # provider name -> time when it may be tried again
        self.providers = self._build_providers()

        self.registry = {k: dict(v) for k, v in TASK_REGISTRY.items()}
        override = os.getenv("HF_MODELS_JSON", "").strip()
        if override:
            try:
                for task, models in json.loads(override).items():
                    if task in self.registry and isinstance(models, list) and models:
                        self.registry[task]["models"] = models
            except Exception as exc:
                log.warning("Ignoring invalid HF_MODELS_JSON: %s", exc)

    # ---------- clients ----------
    def _build_providers(self):
        order = [x.strip().lower() for x in os.getenv("LLM_PROVIDER_ORDER", DEFAULT_ORDER).split(",") if x.strip()]
        built = []
        for name in order:
            if name == "custom":
                base = os.getenv("CUSTOM_LLM_BASE_URL", "").strip()
                key = os.getenv("CUSTOM_LLM_API_KEY", "").strip()
                model = os.getenv("CUSTOM_LLM_MODEL", "").strip()
                if base and model:
                    built.append({"name": "custom", "base": base, "key": key or "unused", "model": model})
                continue
            spec = LLM_PROVIDERS.get(name)
            if not spec:
                continue
            env_key, base, default_model, required = spec
            key = os.getenv(env_key, "").strip()
            if required and not key:
                continue
            model = os.getenv(f"LLM_{name.upper()}_MODEL", "").strip() or default_model
            built.append({"name": name, "base": base, "key": key or "unused", "model": model})
        for p in built:
            p["client"] = OpenAI(api_key=p["key"], base_url=p["base"], timeout=60, max_retries=0)
        return built

    @property
    def hf(self):
        if not self.hf_token:
            raise MissingConfig("HUGGINGFACE_ACCESS_TOKEN is not set on the server")
        if self._hf is None:
            self._hf = InferenceClient(token=self.hf_token, timeout=120)
        return self._hf

    def status(self):
        return {"llm_providers": [p["name"] for p in self.providers], "huggingface": bool(self.hf_token)}

    # ---------- LLM ----------
    def _chat(self, messages, json_mode=False, temperature=0.2):
        """Try each configured provider in order; skip ones that just failed."""
        if not self.providers:
            raise MissingConfig(
                "No LLM provider configured. Set at least one key, e.g. GROQ_API_KEY or GEMINI_API_KEY"
            )
        now = time.time()
        ready = [p for p in self.providers if self._cooldown.get(p["name"], 0) <= now]
        candidates = ready or self.providers  # if all are cooling down, try anyway

        last = None
        for p in candidates:
            attempts = []
            base = {"model": p["model"], "messages": messages}
            if json_mode:
                attempts.append({**base, "temperature": temperature, "response_format": {"type": "json_object"}})
            attempts.append({**base, "temperature": temperature})
            attempts.append(base)  # bare fallback for models that reject extra params
            for kwargs in attempts:
                try:
                    r = p["client"].chat.completions.create(**kwargs)
                    text = r.choices[0].message.content or ""
                    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                    if text:
                        return text
                    raise RuntimeError("empty response")
                except Exception as exc:
                    last = exc
                    log.warning("LLM %s (%s) failed: %s", p["name"], p["model"], str(exc)[:160])
            self._cooldown[p["name"]] = time.time() + 30
        raise RuntimeError(f"All LLM providers failed. Last error: {str(last)[:200]}")

    # ---------- stage 1: planning ----------
    def _plan(self, message, history, files):
        task_list = "\n".join(
            f'- {name}: {spec["desc"]} (input type: {spec["input"]})'
            for name, spec in self.registry.items()
        )
        file_list = "\n".join(
            f'- file:{i} = {f["name"]} ({f["mime"]})' for i, f in enumerate(files)
        ) or "(none)"
        system = (
            PLANNER_SYSTEM.replace("__TASK_LIST__", task_list)
            .replace("__FILE_LIST__", file_list)
            .replace("__MAX__", str(self.max_tasks))
        )
        msgs = [{"role": "system", "content": system}] + history[-6:]
        msgs.append({"role": "user", "content": message})
        try:
            data = _parse_json(self._chat(msgs, json_mode=True, temperature=0))
        except MissingConfig:
            raise
        except Exception as exc:
            log.warning("Planning failed, answering without tools: %s", exc)
            return []

        tasks = data.get("tasks", []) if isinstance(data, dict) else []
        clean = []
        for i, t in enumerate(tasks[: self.max_tasks]):
            if not isinstance(t, dict) or t.get("task") not in self.registry:
                continue
            try:
                tid = int(t.get("id", i))
            except (TypeError, ValueError):
                tid = i
            args = t.get("args") if isinstance(t.get("args"), dict) else {}
            clean.append({"id": tid, "task": t["task"], "args": args})
        return clean

    # ---------- stage 3: execution ----------
    def _resolve(self, ref, kind, results, files):
        if isinstance(ref, str):
            s = ref.strip().strip("<>") if ref.strip().startswith("<file:") else ref.strip()
            m = re.fullmatch(r"<GENERATED>-(\d+)", s)
            if m:
                r = results.get(int(m.group(1)))
                if not r or r["status"] != "done":
                    raise ValueError(f"dependency {m.group(1)} did not finish")
                if kind in ("image", "audio"):
                    if r.get("bytes") is None:
                        raise ValueError(f"task {m.group(1)} produced no {kind}")
                    return r["bytes"]
                return r["text"]
            m = re.fullmatch(r"file:(\d+)", s)
            if m:
                idx = int(m.group(1))
                if idx >= len(files):
                    raise ValueError(f"file:{idx} was not uploaded")
                f = files[idx]
                if kind == "text":
                    if not f["mime"].startswith("text/"):
                        raise ValueError("text task needs a text file")
                    return f["data"].decode("utf-8", "replace")
                return f["data"]
        if kind == "text":
            return str(ref)
        raise ValueError(f"invalid {kind} reference: {ref!r}")

    def _call_hf(self, task, model, value):
        """Returns (text_summary, bytes_or_None, mime_or_None)."""
        c = self.hf
        if task == "image-to-text":
            out = c.image_to_text(value, model=model)
            return _g(out, "generated_text", str(out)), None, None
        if task == "object-detection":
            out = c.object_detection(value, model=model)
            counts = Counter(_g(o, "label") for o in out if _g(o, "score", 0) >= 0.5)
            if not counts:
                return "No objects detected with confidence >= 0.5", None, None
            parts = ", ".join(f"{k} x{v}" for k, v in counts.most_common())
            return f"Detected {sum(counts.values())} objects: {parts}", None, None
        if task == "image-classification":
            out = c.image_classification(value, model=model)
            top = ", ".join(f'{_g(o, "label")} ({_g(o, "score", 0):.2f})' for o in out[:3])
            return f"Top labels: {top}", None, None
        if task == "text-to-image":
            img = c.text_to_image(value, model=model)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return f"Generated an image from the prompt: {value[:200]}", buf.getvalue(), "image/png"
        if task == "automatic-speech-recognition":
            out = c.automatic_speech_recognition(value, model=model)
            return _g(out, "text", str(out)), None, None
        if task == "token-classification":
            out = c.token_classification(value, model=model)
            ents = [f'{_g(o, "word")} ({_g(o, "entity_group") or _g(o, "entity")})' for o in out]
            return ("Entities: " + ", ".join(ents)) if ents else "No entities found", None, None
        if task == "text-classification":
            out = c.text_classification(value, model=model)
            top = ", ".join(f'{_g(o, "label")} ({_g(o, "score", 0):.2f})' for o in out[:3])
            return f"Classification: {top}", None, None
        if task == "summarization":
            out = c.summarization(value, model=model)
            return _g(out, "summary_text", str(out)), None, None
        raise ValueError(f"unsupported task {task}")

    def _execute(self, task, results, files, store, base_url):
        spec = self.registry[task["task"]]
        kind = spec["input"]
        rec = {
            "id": task["id"], "task": task["task"], "model": None, "status": "failed",
            "text": "", "file_url": None, "mime": None, "error": None, "bytes": None,
        }
        try:
            ref = task["args"].get(kind)
            if ref is None and task["args"]:
                ref = next(iter(task["args"].values()))
            value = self._resolve(ref, kind, results, files)

            last_err = None
            for model in spec["models"]:
                try:
                    text, data, mime = self._call_hf(task["task"], model, value)
                    rec.update(model=model, status="done", text=text, bytes=data, mime=mime)
                    if data:
                        token = store.save_file(data, mime)
                        rec["file_url"] = f"{base_url}/files/{token}"
                    break
                except MissingConfig:
                    raise
                except Exception as exc:
                    last_err = exc
                    log.warning("%s via %s failed: %s", task["task"], model, exc)
            else:
                raise last_err or RuntimeError("no models configured")
        except Exception as exc:
            rec["error"] = str(exc)[:300]
        results[rec["id"]] = rec
        return rec

    # ---------- main entry ----------
    def run(self, message, history, files, store, base_url):
        plan = self._plan(message, history, files)
        results, executed = {}, []
        for task in plan:
            executed.append(self._execute(task, results, files, store, base_url))

        if executed:
            lines = []
            for r in executed:
                if r["status"] == "done":
                    extra = " (generated file is attached)" if r["file_url"] else ""
                    lines.append(f'[{r["id"]}] {r["task"]} via {r["model"]}: {r["text"][:2000]}{extra}')
                else:
                    lines.append(f'[{r["id"]}] {r["task"]} FAILED: {r["error"]}')
            user_content = f"{message}\n\nExpert model results:\n" + "\n".join(lines)
        else:
            user_content = message

        msgs = [{"role": "system", "content": RESPONSE_SYSTEM}] + history[-10:]
        msgs.append({"role": "user", "content": user_content})
        answer = self._chat(msgs, temperature=0.4)

        public = [{k: v for k, v in r.items() if k != "bytes"} for r in executed]
        return {"response": answer, "tasks": public}
