import os
import threading
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from octopus.services import trace_writer


def _load_env_file():
    llm_file = Path(__file__).resolve()
    package_root = llm_file.parents[1]
    repo_root = llm_file.parents[2]

    explicit_env_file = os.getenv("OCTOPUS_ENV_FILE")
    candidate_paths = []
    if explicit_env_file:
        candidate_paths.append(Path(explicit_env_file).expanduser())
    candidate_paths.extend([
        repo_root / ".env",
        package_root / ".env",
    ])

    for env_path in candidate_paths:
        if env_path.exists():
            load_dotenv(dotenv_path=env_path)
            return str(env_path)

    # Fallback: keep compatibility with shell environment variables.
    load_dotenv()
    return None


LOADED_ENV_FILE = _load_env_file()

def _env_file_hint():
    return (
        f" (loaded env file: {LOADED_ENV_FILE})" if LOADED_ENV_FILE else
        " (no .env file found, checked OCTOPUS_ENV_FILE / repository .env / package .env)"
    )


def _looks_like_openai_model(model_name: str):
    model_name = (model_name or "").strip().lower()
    openai_prefixes = (
        "gpt-",
        "chatgpt-",
        "o1",
        "o3",
        "o4",
    )
    return model_name.startswith(openai_prefixes)


def _resolve_provider():
    provider = os.getenv("OCTOPUS_LLM_PROVIDER", "gemini").strip().lower()
    if provider == "auto":
        model_name = os.getenv("OCTOPUS_LLM_MODEL", "").strip()
        if _looks_like_openai_model(model_name):
            return "openai"
        return "gemini"

    if provider not in ("gemini", "openai"):
        raise RuntimeError(
            "Invalid OCTOPUS_LLM_PROVIDER. Expected one of: gemini, openai, auto."
            + _env_file_hint()
        )
    return provider


def _build_client(provider: str):
    if provider == "gemini":
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        gemini_base_url = os.getenv(
            "GEMINI_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        if not gemini_api_key:
            raise RuntimeError(
                "Missing GEMINI_API_KEY while OCTOPUS_LLM_PROVIDER=gemini."
                + _env_file_hint()
            )
        return OpenAI(
            api_key=gemini_api_key,
            base_url=gemini_base_url,
        )

    # provider == "openai"
    openai_api_key = os.getenv("OPENAI_API_KEY") or os.getenv("GPT_API_KEY")
    openai_base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not openai_api_key:
        raise RuntimeError(
            "Missing OPENAI_API_KEY (or GPT_API_KEY) while OCTOPUS_LLM_PROVIDER=openai."
            + _env_file_hint()
        )
    return OpenAI(
        api_key=openai_api_key,
        base_url=openai_base_url,
    )


ACTIVE_PROVIDER = _resolve_provider()
DEFAULT_MODEL = os.getenv(
    "OCTOPUS_LLM_MODEL",
    "gpt-4.1-mini" if ACTIVE_PROVIDER == "openai" else "gemini-2.5-flash"
)
_CLIENT = None
_USAGE_LOCK = threading.RLock()
_USAGE = {
    "llm_call_count": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
}


def reset_usage():
    with _USAGE_LOCK:
        for key in _USAGE:
            _USAGE[key] = 0


def get_usage():
    with _USAGE_LOCK:
        return dict(_USAGE)


def _record_call():
    with _USAGE_LOCK:
        _USAGE["llm_call_count"] += 1


def _record_tokens(response):
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    values = {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }
    if not values["total_tokens"]:
        values["total_tokens"] = values["prompt_tokens"] + values["completion_tokens"]
    with _USAGE_LOCK:
        for key, value in values.items():
            _USAGE[key] += value
    return values


def _get_client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _build_client(ACTIVE_PROVIDER)
    return _CLIENT


class GeneralGPT:
    max_tokens: int = 2048
    temperature: float = 0.2
    model_type: str = DEFAULT_MODEL
    n: int = 1
    streaming: bool = False
    history = []

    def __init__(self, model_type=None):
        super().__init__()
        self.model_type = model_type or DEFAULT_MODEL

    @staticmethod
    def _normalize_messages(messages):
        normalized = []
        for message in messages:
            if "role" in message:
                normalized.append(message)
                continue
            if "system" in message:
                normalized.append({"role": "system", "content": message["system"]})
                continue
            if "user" in message:
                normalized.append({"role": "user", "content": message["user"]})
                continue
            if "assistant" in message:
                normalized.append({"role": "assistant", "content": message["assistant"]})
                continue
        return normalized

    @staticmethod
    def _extract_text(response_message):
        content = response_message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, str):
                    chunks.append(item)
                    continue
                text = None
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, dict):
                        text = text.get("value") or text.get("text")
                    if text is None and item.get("type") == "text":
                        text = item.get("value")
                else:
                    text = getattr(item, "text", None)
                    if isinstance(text, dict):
                        text = text.get("value") or text.get("text")
                if text is not None:
                    chunks.append(str(text))
            return "\n".join(chunks).strip()
        return ""

    @staticmethod
    def _sanitize_messages_for_log(messages):
        sanitized = []
        for message in messages:
            role = message.get("role", "")
            content = message.get("content", "")
            if isinstance(content, list):
                safe_items = []
                for item in content:
                    if not isinstance(item, dict):
                        safe_items.append(item)
                        continue
                    item_type = item.get("type")
                    if item_type == "image_url":
                        safe_items.append({"type": "image_url", "image_url": {"url": "[omitted_data_url]"}})
                    else:
                        safe_items.append(item)
                sanitized.append({"role": role, "content": safe_items})
            else:
                sanitized.append(message)
        return sanitized

    def ask_gpt_message(self, prompt="", messages=None):
        if messages is None:
            messages = [{"role": "user", "content": prompt}]

        normalized_messages = self._normalize_messages(messages)
        trace_writer.log_event(
            "llm_request",
            model=self.model_type,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            messages=self._sanitize_messages_for_log(normalized_messages) if trace_writer.get_log_path() else []
        )

        _record_call()
        response = _get_client().chat.completions.create(
            model=self.model_type,
            messages=normalized_messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        content = self._extract_text(response.choices[0].message)
        usage = _record_tokens(response)
        trace_writer.log_event(
            "llm_response",
            model=self.model_type,
            content=content,
            usage=usage,
            cumulative_usage=get_usage(),
        )
        return {"role": "assistant", "content": content, "usage": usage}
