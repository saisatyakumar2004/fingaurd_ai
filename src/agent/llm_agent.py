"""Real-LLM agent behind a swappable provider interface (Anthropic, OpenAI-compatible, native Ollama).

Design points
- Provider contract: ``complete(system, messages) -> str`` plus optional ``last_usage`` / ``last_latency`` attributes and ``health_check()``.
- Failures are classified, never converted into answers:
    ProviderTimeout      no response within the read timeout        (retried ``timeout_retries`` times)
    ProviderUnavailable  cannot connect / HTTP 429 / HTTP 5xx        (retried up to ``max_attempts``)
    ProviderError        HTTP 4xx, non-JSON reply (bad request/model) (never retried)
    MalformedOutput      reply is not one JSON object               (one corrective re-ask, then error)
    InvalidAgentResponse final answer violates the response schema   (one corrective re-ask, then error)
    MaxStepsExceeded     no final answer within the step budget
- Responses are cached on disk only after a VALID final answer, keyed by case, model, prompt version, dataset snapshot,
  generation settings and input hash, and restored together with the tool trace and the evidence refs that were issued.
- Ctrl+C (KeyboardInterrupt) is never swallowed here.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

import requests
from pydantic import ValidationError

from src.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from src.agent.schemas import AgentResponse
from src.agent.tools import ReferenceStore
from src.common.util import canonical_json, stable_id


# ------------------------------------------------------------------ errors
class AgentError(Exception):
    kind = "agent_error"


class ProviderUnavailable(AgentError):
    kind = "provider_unavailable"


class ProviderTimeout(AgentError):
    kind = "provider_timeout"


class ProviderError(AgentError):
    kind = "provider_error"


class MalformedOutput(AgentError):
    kind = "malformed_json"


class InvalidAgentResponse(AgentError):
    kind = "invalid_agent_response"


class MaxStepsExceeded(AgentError):
    kind = "max_steps_exceeded"


# ------------------------------------------------------------------ settings
@dataclass(frozen=True)
class LLMSettings:
    max_output_tokens: int = 700
    temperature: float = 0.0
    seed: int = 42
    num_ctx: int | None = 4096
    connect_timeout: float = 10.0
    read_timeout: float = 300.0
    json_mode: bool = True
    max_attempts: int = 3
    timeout_retries: int = 1
    max_tool_result_chars: int = 3500
    keep_alive: str = "10m"

    ENV = {  # field -> environment variable (env overrides config/settings.yaml `llm:`)
        "max_output_tokens": "FINGUARD_MAX_OUTPUT_TOKENS", "temperature": "FINGUARD_TEMPERATURE", "seed": "FINGUARD_SEED",
        "num_ctx": "FINGUARD_NUM_CTX", "connect_timeout": "FINGUARD_CONNECT_TIMEOUT", "read_timeout": "FINGUARD_READ_TIMEOUT",
        "json_mode": "FINGUARD_JSON_MODE", "max_attempts": "FINGUARD_MAX_ATTEMPTS", "timeout_retries": "FINGUARD_TIMEOUT_RETRIES",
        "max_tool_result_chars": "FINGUARD_MAX_TOOL_RESULT_CHARS", "keep_alive": "FINGUARD_KEEP_ALIVE",
    }

    @classmethod
    def load(cls, cfg: dict[str, Any] | None = None, env: Any = None) -> "LLMSettings":
        env = os.environ if env is None else env
        vals: dict[str, Any] = {}
        defaults = cls()
        for f in fields(cls):
            if f.name == "ENV":
                continue
            raw = env.get(cls.ENV[f.name]) if f.name in cls.ENV else None
            if raw is None or raw == "":
                raw = (cfg or {}).get(f.name, getattr(defaults, f.name))
            default = getattr(defaults, f.name)
            if f.name == "num_ctx":
                vals[f.name] = None if str(raw).lower() in ("none", "0", "") else int(raw)
            elif isinstance(default, bool):
                vals[f.name] = str(raw).lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                vals[f.name] = int(raw)
            elif isinstance(default, float):
                vals[f.name] = float(raw)
            else:
                vals[f.name] = str(raw)
        s = cls(**vals)
        if s.max_output_tokens < 1 or s.max_attempts < 1 or s.read_timeout <= 0:
            raise ValueError("invalid LLM settings: max_output_tokens/max_attempts >= 1 and read_timeout > 0 required")
        return s

    def fingerprint(self) -> str:
        """Only settings that change what the model generates are part of the cache key (not timeouts/retries)."""
        return stable_id(self.max_output_tokens, self.temperature, self.seed, self.num_ctx, self.json_mode, length=10)


# ------------------------------------------------------------------ providers
class LLMProvider(Protocol):
    model_id: str

    def complete(self, system: str, messages: list[dict[str, str]]) -> str: ...


class _HttpProvider:
    """Shared HTTP plumbing and error classification. Request bodies and headers are never logged or put in errors."""

    def __init__(self, model_id: str, llm: LLMSettings, session: Any | None = None) -> None:
        self.model_id = model_id
        self.llm = llm
        self.session = session or requests.Session()
        self.last_usage: dict[str, int] = {}
        self.last_latency: float = 0.0

    def settings_fingerprint(self) -> str:
        return self.llm.fingerprint()

    def _request(self, method: str, url: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                 read_timeout: float | None = None) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            r = self.session.request(method, url, json=payload, headers=headers,
                                     timeout=(self.llm.connect_timeout, read_timeout or self.llm.read_timeout))
        except requests.exceptions.ReadTimeout:
            raise ProviderTimeout(f"no response from {urlparse(url).netloc} within {read_timeout or self.llm.read_timeout:.0f}s") from None
        except requests.exceptions.ConnectionError:       # includes ConnectTimeout
            raise ProviderUnavailable(f"cannot connect to {urlparse(url).netloc} (is the server running?)") from None
        except requests.exceptions.Timeout:
            raise ProviderTimeout(f"request to {urlparse(url).netloc} timed out") from None
        self.last_latency = time.monotonic() - t0
        if r.status_code == 429 or r.status_code >= 500:
            raise ProviderUnavailable(f"HTTP {r.status_code} from {urlparse(url).netloc}")
        if r.status_code >= 400:
            raise ProviderError(f"HTTP {r.status_code}: {(getattr(r, 'text', '') or '')[:200]}")
        try:
            return r.json()
        except ValueError:
            raise ProviderError("provider returned a non-JSON body") from None


class OpenAICompatibleProvider(_HttpProvider):
    """OpenAI-compatible /chat/completions (OpenAI, Ollama's /v1, vLLM, LM Studio ...)."""

    def __init__(self, model: str | None = None, llm: LLMSettings | None = None, session: Any | None = None) -> None:
        self.base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.key = os.environ.get("OPENAI_API_KEY", "")
        if not self.key:
            raise RuntimeError("OPENAI_API_KEY is not set (for Ollama any non-empty value, e.g. 'ollama', works)")
        name = model or os.environ.get("FINGUARD_LLM_MODEL", "")
        if not name:
            raise RuntimeError("set FINGUARD_LLM_MODEL for the OpenAI-compatible provider")
        super().__init__(name, llm or LLMSettings.load(), session)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int | None = None) -> str:
        payload: dict[str, Any] = {"model": self.model_id, "messages": [{"role": "system", "content": system}] + messages,
                                   "max_tokens": max_tokens or self.llm.max_output_tokens, "temperature": self.llm.temperature,
                                   "seed": self.llm.seed, "stream": False}
        if self.llm.json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = self._request("POST", f"{self.base}/chat/completions", payload, self._headers())
        u = body.get("usage") or {}
        self.last_usage = {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}
        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError("unexpected response shape from /chat/completions") from None

    def health_check(self) -> tuple[bool, str]:
        try:
            models = self._request("GET", f"{self.base}/models", headers=self._headers(), read_timeout=15)
            ids = [m.get("id") for m in models.get("data", [])]
            if ids and self.model_id not in ids:
                return False, f"model '{self.model_id}' not served at {self.base}; available: {ids[:8]}"
            t0 = time.monotonic()
            self.complete("Reply with the single word OK.", [{"role": "user", "content": "ping"}], max_tokens=8)
            return True, f"model '{self.model_id}' answered a 8-token probe in {time.monotonic() - t0:.1f}s ({self.base})"
        except AgentError as e:
            return False, f"{type(e).__name__}: {e}"


class OllamaProvider(_HttpProvider):
    """Native Ollama /api/chat: exposes num_ctx / num_predict / keep_alive, which the OpenAI-compatible endpoint cannot set."""

    def __init__(self, model: str | None = None, llm: LLMSettings | None = None, session: Any | None = None) -> None:
        base = os.environ.get("OLLAMA_HOST_URL") or os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
        self.root = base.rstrip("/").removesuffix("/v1")
        name = model or os.environ.get("FINGUARD_LLM_MODEL", "")
        if not name:
            raise RuntimeError("set FINGUARD_LLM_MODEL (e.g. qwen2.5:3b) for the Ollama provider")
        super().__init__(name, llm or LLMSettings.load(), session)

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int | None = None) -> str:
        opts: dict[str, Any] = {"num_predict": max_tokens or self.llm.max_output_tokens, "temperature": self.llm.temperature,
                                "seed": self.llm.seed}
        if self.llm.num_ctx:
            opts["num_ctx"] = self.llm.num_ctx
        payload: dict[str, Any] = {"model": self.model_id, "stream": False, "keep_alive": self.llm.keep_alive, "options": opts,
                                   "messages": [{"role": "system", "content": system}] + messages}
        if self.llm.json_mode:
            payload["format"] = "json"
        body = self._request("POST", f"{self.root}/api/chat", payload)
        self.last_usage = {"input_tokens": body.get("prompt_eval_count", 0), "output_tokens": body.get("eval_count", 0)}
        try:
            return body["message"]["content"] or ""
        except (KeyError, TypeError):
            raise ProviderError("unexpected response shape from /api/chat") from None

    def health_check(self) -> tuple[bool, str]:
        try:
            tags = self._request("GET", f"{self.root}/api/tags", read_timeout=15)
            names = [m.get("name") for m in tags.get("models", [])]
            if self.model_id not in names:
                return False, f"model '{self.model_id}' is not pulled; available: {names[:8]} (run: ollama pull {self.model_id})"
            t0 = time.monotonic()
            self.complete("Reply with the single word OK.", [{"role": "user", "content": "ping"}], max_tokens=8)
            return True, f"model '{self.model_id}' answered a probe in {time.monotonic() - t0:.1f}s ({self.root})"
        except AgentError as e:
            return False, f"{type(e).__name__}: {e}"


class AnthropicProvider:
    def __init__(self, model: str | None = None, max_tokens: int = 2000) -> None:
        try:
            import anthropic  # optional dependency
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pip install anthropic to use AnthropicProvider") from e
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.client = anthropic.Anthropic(timeout=60.0, max_retries=0)   # retries are handled (boundedly) by LLMAgent
        self.last_usage: dict[str, int] = {}
        self.last_latency = 0.0
        self.model_id = model or os.environ.get("FINGUARD_LLM_MODEL", "claude-sonnet-5-5")
        self.max_tokens = max_tokens

    def settings_fingerprint(self) -> str:
        return stable_id(self.max_tokens, length=10)

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        t0 = time.monotonic()
        r = self.client.messages.create(model=self.model_id, max_tokens=self.max_tokens, system=system, messages=messages)
        self.last_latency = time.monotonic() - t0
        u = getattr(r, "usage", None)
        self.last_usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0, "output_tokens": getattr(u, "output_tokens", 0) or 0}
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")

    def health_check(self) -> tuple[bool, str]:
        return True, "API key configured (not probed, to avoid spending tokens)"


def build_provider(name: str, llm: LLMSettings, model: str | None = None):
    if name == "anthropic":
        return AnthropicProvider(model)
    if name == "openai":
        return OpenAICompatibleProvider(model, llm)
    if name == "ollama":
        return OllamaProvider(model, llm)
    raise ValueError(f"unknown provider {name!r} (anthropic | openai | ollama)")


# ------------------------------------------------------------------ output handling
def extract_json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise MalformedOutput("no JSON object in model output")
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError as e:
        raise MalformedOutput(f"invalid JSON in model output: {e}") from e
    if not isinstance(obj, dict):
        raise MalformedOutput("model output JSON is not an object")
    return obj


_TOOL_NAMES = {"lookup_company", "get_observations", "get_provenance", "compare_values", "lookup_instrument"}


def normalize_dialect(obj: dict[str, Any], stats: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map unambiguous shape slips of small models onto the protocol, and count them (never invents content):
       {"action":"<tool_name>", ...args}  ->  tool call;  {"tool":..., "args":...}  ->  tool call;
       {"response":{...}} / flat {"status":..., "explanation":...}  ->  final.  Schema validation still applies afterwards."""
    def note() -> None:
        if stats is not None:
            stats["lenient_parses"] = stats.get("lenient_parses", 0) + 1

    action = obj.get("action")
    if action in _TOOL_NAMES:
        note()
        args = obj.get("args") if isinstance(obj.get("args"), dict) else {k: v for k, v in obj.items() if k not in ("action", "tool", "args")}
        return {"action": "tool", "tool": action, "args": args}
    if action is None and obj.get("tool") in _TOOL_NAMES:
        note()
        return {"action": "tool", "tool": obj["tool"], "args": obj.get("args") if isinstance(obj.get("args"), dict) else {}}
    if action is None and isinstance(obj.get("response"), dict):
        note()
        return {"action": "final", "response": obj["response"]}
    if action is None and "status" in obj and "explanation" in obj:
        note()
        return {"action": "final", "response": obj}
    return obj


_DROP_KEYS = {"source_url", "sec_entity_name", "sector", "xbrl_tag", "snapshot_id"}


def compact_tool_result(result: Any, max_chars: int) -> str:
    """Shrink tool output before it goes back into a small model's context: drop bulky provenance fields, cap lists."""
    def walk(x: Any) -> Any:
        if isinstance(x, dict):
            out = {k: walk(v) for k, v in x.items() if k not in _DROP_KEYS}
            if isinstance(out.get("alternate_reports"), list) and len(out["alternate_reports"]) > 2:
                out["alternate_reports"] = out["alternate_reports"][:2] + [f"... {len(x['alternate_reports']) - 2} more"]
            if isinstance(out.get("observations"), list) and len(out["observations"]) > 8:
                out["observations"] = out["observations"][-8:]
                out["note"] = "only the 8 most recent periods are shown"
            return out
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    text = json.dumps(walk(result), default=str, separators=(",", ":"))
    return text if len(text) <= max_chars else text[:max_chars] + "...[truncated]"


# ------------------------------------------------------------------ agent
class LLMAgent:
    prompt_version = PROMPT_VERSION

    def __init__(self, provider: LLMProvider, cache_dir: Path, dataset_snapshot: str = "", max_steps: int = 8,
                 max_attempts: int | None = None, sleep: Callable[[float], None] = time.sleep,
                 llm: LLMSettings | None = None, on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.provider = provider
        self.llm = llm or getattr(provider, "llm", None) or LLMSettings()
        self.model_id = provider.model_id
        self.cache_dir = Path(cache_dir)
        self.dataset_snapshot = dataset_snapshot
        self.max_steps = max_steps
        self.max_attempts = max_attempts or self.llm.max_attempts
        self._sleep = sleep
        self.on_event = on_event
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "cache_hits": 0, "cache_misses": 0, "errors": 0,
                      "latency_seconds": 0.0, "corrective_retries": 0, "duplicate_tool_calls": 0}
        self.case_stats: dict[str, Any] = {}

    # ---- helpers
    def estimated_cost_usd(self) -> float | None:
        """Needs FINGUARD_PRICE_IN_PER_MTOK / FINGUARD_PRICE_OUT_PER_MTOK (USD per million tokens); local models cost ~0 but
        this stays 'unknown' unless prices are configured."""
        try:
            pin, pout = float(os.environ["FINGUARD_PRICE_IN_PER_MTOK"]), float(os.environ["FINGUARD_PRICE_OUT_PER_MTOK"])
        except (KeyError, ValueError):
            return None
        return (self.usage["input_tokens"] * pin + self.usage["output_tokens"] * pout) / 1e6

    def _emit(self, **ev: Any) -> None:
        if self.on_event:
            self.on_event(ev)

    def _fingerprint(self) -> str:
        fp = getattr(self.provider, "settings_fingerprint", None)
        return fp() if callable(fp) else self.llm.fingerprint()

    def _key(self, agent_input: dict[str, Any]) -> str:
        return stable_id(agent_input["case_id"], self.model_id, self.prompt_version, self.dataset_snapshot, self._fingerprint(),
                         self.max_steps, canonical_json(agent_input), length=32)

    def _complete(self, messages: list[dict[str, str]]) -> str:
        """One model call with the retry policy: timeouts get ``timeout_retries`` extra tries, transient failures
        (connection / 429 / 5xx / unknown) up to ``max_attempts``, ProviderError none."""
        attempts_left_timeout = self.llm.timeout_retries
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            t0 = time.monotonic()
            try:
                text = self.provider.complete(SYSTEM_PROMPT, messages)
            except ProviderError:
                self.usage["errors"] += 1
                raise
            except ProviderTimeout as e:
                last = e
                self.usage["errors"] += 1
                if attempts_left_timeout <= 0:
                    raise
                attempts_left_timeout -= 1
                self._emit(type="retry", reason="timeout", attempt=attempt + 1)
                continue
            except Exception as e:                      # transient / unknown provider failure
                last = e
                self.usage["errors"] += 1
                self._emit(type="retry", reason=type(e).__name__, attempt=attempt + 1)
                self._sleep(min(30.0, 2.0 ** attempt))
                continue
            dt = getattr(self.provider, "last_latency", None) or (time.monotonic() - t0)
            u = getattr(self.provider, "last_usage", None) or {}
            self.usage["calls"] += 1
            self.usage["input_tokens"] += int(u.get("input_tokens", 0) or 0)
            self.usage["output_tokens"] += int(u.get("output_tokens", 0) or 0)
            self.usage["latency_seconds"] += dt
            self.case_stats["model_calls"] += 1
            self.case_stats["latency_s"] += dt
            self._emit(type="model_call", n=self.case_stats["model_calls"], seconds=round(dt, 2),
                       output_tokens=int(u.get("output_tokens", 0) or 0))
            return text
        if isinstance(last, ProviderTimeout):
            raise last
        raise ProviderUnavailable(f"model provider unavailable after {self.max_attempts} attempts: {type(last).__name__}")

    def _load_cache(self, path: Path, store: ReferenceStore) -> AgentResponse | None:
        if not path.exists():
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            resp = AgentResponse.model_validate(blob["response"])
        except (ValueError, KeyError, ValidationError):
            return None                                  # corrupt/stale entry: treated as a miss and overwritten on success
        store.issued.update(blob.get("issued", {}))
        store.trace.extend(blob.get("trace", []))
        return resp

    # ---- main loop
    def run(self, agent_input: dict[str, Any], store: ReferenceStore) -> AgentResponse:
        self.case_stats = {"model_calls": 0, "latency_s": 0.0, "cache": "miss", "corrective_retries": 0, "duplicate_tool_calls": 0,
                           "lenient_parses": 0}
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{self._key(agent_input)}.json"
        cached = self._load_cache(path, store)
        if cached is not None:
            self.usage["cache_hits"] += 1
            self.case_stats["cache"] = "hit"
            return cached
        self.usage["cache_misses"] += 1

        case_id = agent_input["case_id"]
        messages = [{"role": "user", "content": json.dumps({"records": agent_input["records"]}, default=str)}]
        seen: dict[str, str] = {}
        corrected = False
        for _ in range(self.max_steps):
            text = self._complete(messages)
            problem: str | None = None
            try:
                obj = extract_json(text)
                obj = normalize_dialect(obj, self.case_stats)
                action = obj.get("action")
                if action == "tool":
                    tool, args = str(obj.get("tool")), obj.get("args") or {}
                    sig = canonical_json([tool, args])
                    if sig in seen:                       # redundant call: do not re-run, tell the model to move on
                        self.usage["duplicate_tool_calls"] += 1
                        self.case_stats["duplicate_tool_calls"] += 1
                        payload = json.dumps({"note": "duplicate call; this result was already returned above - proceed to the next step "
                                                      "or give the final answer", "tool": tool})
                    else:
                        payload = compact_tool_result(store.call(tool, args if isinstance(args, dict) else {}), self.llm.max_tool_result_chars)
                        seen[sig] = payload
                    messages.append({"role": "assistant", "content": text[:1500]})
                    messages.append({"role": "user", "content": f'{{"tool_result":{payload}}}'})
                    continue
                if action == "final" and isinstance(obj.get("response"), dict):
                    try:
                        resp = AgentResponse.model_validate({**obj["response"], "case_id": case_id})
                    except ValidationError as e:
                        fields_bad = sorted({".".join(str(p) for p in err["loc"]) for err in e.errors()})
                        raise InvalidAgentResponse(f"final answer violates the schema in fields: {fields_bad}") from None
                    tmp = path.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"response": resp.model_dump(), "issued": store.issued, "trace": store.trace}, default=str),
                                   encoding="utf-8")
                    os.replace(tmp, path)
                    return resp
                raise MalformedOutput("model output lacks a valid 'action' (tool|final)")
            except (MalformedOutput, InvalidAgentResponse) as e:
                if corrected:
                    raise
                corrected = True
                self.usage["corrective_retries"] += 1
                self.case_stats["corrective_retries"] += 1
                problem = str(e)
                messages.append({"role": "assistant", "content": text[:800]})
                messages.append({"role": "user", "content": f"Your reply was rejected: {problem}. Reply again with ONE valid JSON object only, "
                                                            "exactly in the required format."})
        raise MaxStepsExceeded(f"no final answer within {self.max_steps} model calls")
