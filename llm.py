import hashlib
import json
import os
import httpx

class LLMUnavailable(Exception):
    """Raised when the external LLM service fails, times out, or returns malformed data."""
    pass

# In-memory cache keyed by MD5 hash of operator notes JSON string
_INTENT_CACHE: dict[str, list[dict]] = {}

SYSTEM_PROMPT = """You are an expert energy management parser. You analyze natural-language operator notes for a campus energy schedule and extract structured INTENTS.

For each note provided, extract INTENT parameters. Do NOT calculate final hours or factors; extract start/end hours and percentage descriptions directly as instructed.

Output MUST be a single, valid JSON object with the key "directives", containing a list with EXACTLY one entry per input note.

Allowed directive_type values:
- "solar_reduction": Scheduled reduction in solar production.
- "minimum_battery_reserve": Minimum battery backup requirement in kWh.
- "no_charge_window": Period when battery charging is prohibited.
- "no_discharge_window": Period when battery discharging is prohibited.
- "max_grid_window": Maximum grid intake cap in kWh during a window.
- "no_op": Note is non-operational, informational, or irrelevant.

JSON Schema per directive entry:
{
  "note_index": int (0-indexed position corresponding to input note order),
  "directive_type": string (one of the 6 allowed types above),
  "start_hour_24": int (0 to 23, start of window inclusive, or null if N/A),
  "end_hour_24": int (0 to 24, end of window EXCLUSIVE, or null if N/A),
  "percent_kind": string ("remaining" | "reduction" | null),
  "percent_value": number (0-100 or null),
  "numeric_value": number (kWh value for minimum reserve or max grid cap, or null),
  "explanation": string (short description of the extracted action)
}

Rules:
1. "start_hour_24" (inclusive) and "end_hour_24" (exclusive) use a 24-hour clock. E.g., 2 PM to 4 PM is start=14, end=16. Midnight to 6 AM is start=0, end=6. 10 PM to 2 AM is start=22, end=2.
2. If a note specifies solar dropping BY 30%, percent_kind="reduction", percent_value=30.
3. If a note specifies solar dropping TO 30%, percent_kind="remaining", percent_value=30.
4. Process all notes provided in the input array into a matching JSON element array.
"""

def _hash_notes(notes: list[str]) -> str:
    """Generates a unique hash key for a list of notes."""
    serialized = json.dumps(notes, sort_keys=True)
    return hashlib.md5(serialized.encode("utf-8")).hexdigest()

async def extract_intents(notes: list[str]) -> list[dict]:
    """
    Extracts structured intent dicts from operator notes using Fireworks API.
    Handles caching, timeouts, retries, and errors without exposing sensitive API details.
    """
    if not notes:
        return []

    cache_key = _hash_notes(notes)
    if cache_key in _INTENT_CACHE:
        return _INTENT_CACHE[cache_key]

    api_key = os.getenv("FIREWORKS_API_KEY", "")
    model = os.getenv("FIREWORKS_MODEL", "accounts/fireworks/models/mixtral-8x7b-instruct")

    if not api_key:
        raise LLMUnavailable("API key is not configured.")

    url = "https://api.fireworks.ai/inference/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    user_content = json.dumps({"operator_notes": notes})
    payload = {
        "model": model,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }

    timeout_config = httpx.Timeout(12.0)
    attempts = 2  # Initial attempt + 1 retry

    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                
                directives = parsed.get("directives", [])
                if isinstance(directives, list):
                    _INTENT_CACHE[cache_key] = directives
                    return directives
                else:
                    raise ValueError("Key 'directives' is not a list")

        except Exception:
            if attempt == attempts - 1:
                raise LLMUnavailable("Failed to retrieve intents from LLM provider after retry.")
    
    raise LLMUnavailable("Unexpected execution path in LLM intent extraction.")