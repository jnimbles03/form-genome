# app/services/llm_router.py
# Enhanced error logging for debugging LLM issues
from __future__ import annotations
import os, requests, json, time
from typing import Any, List, Dict

class LLMError(RuntimeError): ...
def _raise(res: requests.Response) -> None:
    try:
        j = res.json()
    except Exception:
        j = res.text
    raise LLMError(f"{res.status_code} {j}")

def _timeout(retries:int, attempt:int) -> float:
    return 0.8 * (attempt + 1)

def _as_openai(messages: List[Dict[str,str]]) -> List[Dict[str,str]]:
    # Already OpenAI-style: [{"role": "...", "content": "..."}]
    return messages

def _as_anthropic(messages: List[Dict[str,str]]) -> Dict:
    # Anthropic Messages API: system + messages
    # Supports both text-only and vision (image) messages
    system = ""
    msgs = []
    for m in messages:
        role = m.get("role")
        content = m.get("content","")
        if role == "system":
            system = (system + "\n" + content).strip()
        elif role in ("user","assistant"):
            # If content is already a list (vision message with images), pass through as-is
            # Otherwise wrap as text string
            if isinstance(content, list):
                msgs.append({"role": role, "content": content})
            else:
                msgs.append({"role": role, "content": content})
    return {"system": system, "messages": msgs}

def _as_gemini(messages: List[Dict[str,str]]) -> Dict:
    """Convert OpenAI/Anthropic-shaped messages to Gemini generateContent.

    Supports two content shapes:
      - String content (text only):  {"role": "...", "content": "hello"}
      - List content (multimodal):   {"role": "...", "content": [
            {"type": "text", "text": "..."},
            {"type": "image", "source": {"type": "base64",
                                          "media_type": "image/png",
                                          "data": "..."}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        ]}

    Anthropic and OpenAI image blocks are normalised to Gemini's
    `inline_data: {mime_type, data}` part.
    """
    contents = []
    for m in messages:
        role = "user" if m.get("role") in ("system","user") else "model"
        content = m.get("content", "")

        if isinstance(content, list):
            parts: List[Dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    parts.append({"text": block.get("text","")})
                elif btype == "image":
                    # Anthropic-shaped: source.{type:base64, media_type, data}
                    src = block.get("source") or {}
                    if src.get("type") == "base64":
                        parts.append({"inline_data": {
                            "mime_type": src.get("media_type", "image/png"),
                            "data": src.get("data", ""),
                        }})
                elif btype == "image_url":
                    # OpenAI-shaped: image_url.url = "data:<mime>;base64,<data>"
                    url = (block.get("image_url") or {}).get("url", "")
                    if url.startswith("data:") and ";base64," in url:
                        head, b64 = url.split(";base64,", 1)
                        mime = head[5:]  # strip "data:"
                        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
            if parts:
                contents.append({"role": role, "parts": parts})
        else:
            contents.append({"role": role, "parts": [{"text": str(content)}]})

    return {"contents": contents}

def chat_complete(*, provider:str, model:str, messages:List[Dict[str,str]], max_tokens:int=800, temperature:float=0.0, timeout:float=25.0, retries:int=2, fallback:bool=True) -> str:
    """Return assistant text from provider with automatic fallback to other providers if rate limited."""
    provider = (provider or "").lower().strip()
    if provider not in ("xai","openai","anthropic","gemini"):
        provider = "openai"  # default

    # Define fallback chain based on primary provider
    fallback_chain = {
        "openai": ["anthropic", "gemini", "xai"],
        "anthropic": ["openai", "gemini", "xai"],
        "gemini": ["openai", "anthropic", "xai"],
        "xai": ["openai", "anthropic", "gemini"]
    }

    providers_to_try = [provider]
    if fallback:
        providers_to_try.extend(fallback_chain.get(provider, []))

    last_err = None
    for current_provider in providers_to_try:
        for attempt in range(retries+1):
            try:
                # Use current_provider for this attempt
                if current_provider == "xai":
                    key = os.getenv("XAI_API_KEY","").strip()
                    url = os.getenv("XAI_ENDPOINT","https://api.x.ai/v1/chat/completions").strip()
                    if not key: raise LLMError("XAI_API_KEY missing")
                    payload = {"model": model or os.getenv("GROK_MODEL","grok-4-latest"),
                               "temperature": temperature, "max_tokens": max_tokens,
                               "messages": _as_openai(messages)}
                    headers = {"Authorization": f"Bearer {key}", "Content-Type":"application/json"}
                    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
                    if not r.ok: _raise(r)
                    j = r.json()
                    return j["choices"][0]["message"]["content"]

                elif current_provider == "openai":
                    key = os.getenv("OPENAI_API_KEY","").strip()
                    if not key: raise LLMError("OPENAI_API_KEY missing")
                    payload = {"model": model or "gpt-4o-mini",
                               "temperature": temperature, "max_tokens": max_tokens,
                               "messages": _as_openai(messages)}
                    headers = {"Authorization": f"Bearer {key}", "Content-Type":"application/json"}
                    r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=timeout)
                    if not r.ok: _raise(r)
                    j = r.json()
                    return j["choices"][0]["message"]["content"]

                elif current_provider == "anthropic":
                    key = os.getenv("ANTHROPIC_API_KEY","").strip()
                    if not key: raise LLMError("ANTHROPIC_API_KEY missing")
                    payload = _as_anthropic(messages)

                    # Auto-detect vision requests: check if any message has image content
                    is_vision = False
                    for msg in payload.get("messages", []):
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            # Check if any content block is an image
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "image":
                                    is_vision = True
                                    break
                        if is_vision:
                            break

                    # Auto-select vision model if needed
                    if is_vision and not model:
                        payload["model"] = "claude-3-5-sonnet-20241022"  # Vision-capable model
                    else:
                        payload["model"] = model or "claude-3-5-sonnet-20241022"

                    payload["max_tokens"] = max_tokens
                    payload["temperature"] = temperature
                    headers = {
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                        "content-type":"application/json"
                    }
                    r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=timeout)
                    if not r.ok: _raise(r)
                    j = r.json()
                    # response.text is in content[0].text
                    parts = j.get("content") or []
                    for p in parts:
                        if p.get("type") == "text":
                            return p.get("text","")
                    return ""

                elif current_provider == "gemini":
                    key = os.getenv("GEMINI_API_KEY","").strip()
                    if not key: raise LLMError("GEMINI_API_KEY missing")
                    # Default to Flash — used as a scout for triage/discovery
                    # AND as a vision model for scanned PDFs. Pro is reserved
                    # for explicit overrides (set GEMINI_MODEL or pass model=).
                    mdl = (model or os.getenv("GEMINI_MODEL") or "gemini-1.5-flash").strip()
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={key}"
                    payload = _as_gemini(messages)
                    payload["generationConfig"] = {"temperature": temperature, "maxOutputTokens": max_tokens}
                    headers = {"content-type":"application/json"}
                    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
                    if not r.ok: _raise(r)
                    j = r.json()
                    # text is in candidates[0].content.parts[0].text
                    cands = j.get("candidates") or []
                    if cands:
                        parts = (cands[0].get("content") or {}).get("parts") or []
                        for p in parts:
                            if "text" in p: return p["text"]
                    return ""

            except Exception as e:
                last_err = e
                err_msg = str(e)
                # Log the actual error for debugging
                print(f"[LLM ERROR] {current_provider.upper()} failed: {err_msg[:200]}")

                # Check if rate limit error - if so, try next provider immediately
                if "429" in err_msg or "rate" in err_msg.lower():
                    print(f"[LLM] {current_provider} rate limited, trying fallback...")
                    break  # Break retry loop, move to next provider
                # For other errors, retry with same provider
                if attempt < retries:
                    time.sleep(_timeout(retries, attempt))
                    continue
                # If all retries failed, try next provider
                print(f"[LLM] {current_provider} exhausted retries, trying next provider")
                break

    # If we've exhausted all providers, raise the last error
    if last_err:
        raise last_err
    raise LLMError("No LLM providers available")