"""Optional model-assisted extraction.

Off unless a key is configured and USE_LLM is set. When on, the model only ever
proposes facts: every proposal is checked against the page text by the caller
and dropped if its quote is not literally there, so the model cannot introduce a
number the document does not contain.
"""

import json
import logging
import urllib.request
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
MAX_PROMPT_CHARS = 6000

PROVIDERS = (
    ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions",
     "llama-3.3-70b-versatile"),
    ("openai", "OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
)


class LLMProvider:
    """A thin client over whichever provider happens to be configured."""

    @classmethod
    def active_provider(cls) -> Optional[str]:
        if not settings.USE_LLM:
            return None
        for name, key_attr, _url, _model in PROVIDERS:
            if getattr(settings, key_attr, ""):
                return name
        if settings.GEMINI_API_KEY:
            return "gemini"
        return None

    @classmethod
    def is_llm_available(cls) -> bool:
        return cls.active_provider() is not None

    @classmethod
    def build_prompt(cls, text: str, page_number: int, doc_name: str) -> str:
        return (
            "Extract measured numerical facts and key semantic facts (such as architecture, "
            "components, roles, statuses, definitions, methodologies, institutions, and relationships) "
            'from the page text below. Return JSON with a key "facts" holding a list. Each fact needs: '
            "subject, predicate (attribute or metric name), value_raw (exactly as stated/printed), "
            "unit (if applicable, else null), time_period, scope, qualifier, evidence_quote, confidence.\n\n"
            "evidence_quote must be copied character for character from the text. A "
            "fact whose quote is not present verbatim will be discarded.\n\n"
            f"Document: {doc_name}\nPage: {page_number}\n\nText:\n\"\"\"\n{text}\n\"\"\"\n"
        )

    @classmethod
    def extract_facts_llm(
        cls, text: str, page_number: int, doc_name: str
    ) -> Optional[List[Dict[str, Any]]]:
        provider = cls.active_provider()
        if provider is None:
            return None

        prompt = cls.build_prompt(text[:MAX_PROMPT_CHARS], page_number, doc_name)
        try:
            if provider == "gemini":
                return cls._call_gemini(prompt)
            for name, key_attr, url, model in PROVIDERS:
                if name == provider:
                    return cls._call_openai_compatible(
                        url, getattr(settings, key_attr), model, prompt)
        except Exception as exc:  # noqa: BLE001 - extraction continues without it
            logger.warning("%s extraction failed on page %s: %s", provider, page_number, exc)
        return None

    @staticmethod
    def _request(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))

    @classmethod
    def _call_openai_compatible(
        cls, url: str, api_key: str, model: str, prompt: str
    ) -> List[Dict[str, Any]]:
        data = cls._request(url, {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }, {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
        content = json.loads(data["choices"][0]["message"]["content"])
        return content.get("facts", [])

    @classmethod
    def _call_gemini(cls, prompt: str) -> List[Dict[str, Any]]:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-1.5-flash:generateContent?key={settings.GEMINI_API_KEY}")
        data = cls._request(url, {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        }, {"Content-Type": "application/json"})
        content = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
        return content.get("facts", [])
