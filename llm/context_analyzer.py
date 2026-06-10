import os
import json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL = "gpt-4o"


def analyze_context(legs: list[dict], news_summary: str = "") -> list[dict]:
    if not legs:
        return legs

    prompt = f"""You are an MLB betting analyst assistant.

Here are today's value legs our model has identified:
{json.dumps(legs, indent=2)}

Here is the latest news context:
{news_summary}

Flag any legs that should be removed or downgraded based on qualitative context
the statistical model would not capture. Be specific and concise.
Return JSON only."""

    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    return _apply_llm_adjustments(legs, response.choices[0].message.content)


def _apply_llm_adjustments(legs: list[dict], llm_response: str) -> list[dict]:
    pass
