import json
import tiktoken
from typing import AsyncGenerator, Optional
from groq import AsyncGroq
from app.config import get_settings
from app.core import logger

settings = get_settings()

# Token counter
_encoding = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding.encode(text))


class LLMClient:
    """Unified LLM client with Groq and streaming support."""

    def __init__(self):
        self.client = AsyncGroq(api_key=settings.groq_api_key)
        self.primary_model = settings.default_model
        self.fallback_model = settings.fallback_model

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        json_mode: bool = True,
    ) -> dict:
        """Generate a completion, returns {content, tokens_used, model_used}."""
        model = model or self.primary_model

        try:
            response = await self._call(
                model, system_prompt, user_prompt, temperature, max_tokens, json_mode
            )
            return response
        except Exception as e:
            logger.warning("llm_primary_failed", model=model, error=str(e))
            if model != self.fallback_model:
                response = await self._call(
                    self.fallback_model, system_prompt, user_prompt,
                    temperature, max_tokens, json_mode
                )
                return response
            raise

    async def _call(
        self, model: str, system_prompt: str, user_prompt: str,
        temperature: float, max_tokens: int, json_mode: bool
    ) -> dict:
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = await self.client.chat.completions.create(**kwargs)

        content = response.choices[0].message.content
        tokens = response.usage.total_tokens if response.usage else count_tokens(content)

        return {
            "content": content,
            "tokens_used": tokens,
            "model_used": model,
        }

    async def stream(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> AsyncGenerator[str, None]:
        """Stream tokens one by one."""
        model = model or self.primary_model

        stream = await self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )

        async for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


def parse_llm_json(content: str) -> dict:
    """Safely parse JSON from LLM output, handling common issues."""
    content = content.strip()
    # Remove markdown code fences if present
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
        if content.startswith("json"):
            content = content[4:].strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(content[start:end])
            except json.JSONDecodeError:
                pass
        return {"raw_content": content, "parse_error": True}
