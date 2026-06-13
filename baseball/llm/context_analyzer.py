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

    # Strip raw_odds before sending — keeps the prompt lean
    slim_legs = [
        {k: v for k, v in leg.items() if k != "raw_odds"}
        for leg in legs
    ]

    prompt = f"""You are an MLB betting analyst assistant.

Here are today's value legs our quantitative model has identified:
{json.dumps(slim_legs, indent=2)}

Latest news context:
{news_summary or "No news context provided."}

Review each leg for qualitative issues the statistical model cannot catch in real time:
- Late lineup scratches or injuries announced after stats were pulled
- Pitcher on a pitch count restriction or recently returned from IL
- Bullpen heavily used last night (extra innings, blowout)
- Weather update changed significantly since morning pull
- Motivation spots or known situational factors

Return ONLY valid JSON in this exact format:
{{
  "actions": [
    {{
      "game_id": "<game_id>",
      "bet_type": "ml" or "total",
      "action": "remove" or "downgrade",
      "edge_multiplier": 0.5,
      "reason": "<one sentence>"
    }}
  ]
}}

Rules:
- Only include legs you are flagging. Legs not listed are approved as-is.
- "remove" = drop the leg entirely.
- "downgrade" = reduce confidence; set edge_multiplier between 0.1 and 0.9.
- Do not add new legs. The quantitative model is the source of truth for what qualifies.
- If no legs need flagging, return {{"actions": []}}"""

    try:
        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        return _apply_llm_adjustments(legs, response.choices[0].message.content)
    except Exception as e:
        print(f"[context_analyzer] LLM call failed ({e}) — returning legs unchanged")
        return legs


def _apply_llm_adjustments(legs: list[dict], llm_response: str) -> list[dict]:
    try:
        data    = json.loads(llm_response)
        actions = {
            (a["game_id"], a["bet_type"]): a
            for a in data.get("actions", [])
        }
    except (json.JSONDecodeError, KeyError):
        print("[context_analyzer] Could not parse LLM response — returning legs unchanged")
        return legs

    result = []
    for leg in legs:
        key    = (leg.get("game_id"), leg.get("bet_type"))
        action = actions.get(key)

        if action is None:
            result.append(leg)
            continue

        if action["action"] == "remove":
            print(f"[context_analyzer] REMOVED  {leg['display']} — {action.get('reason')}")
            continue

        if action["action"] == "downgrade":
            multiplier = float(action.get("edge_multiplier", 0.5))
            updated    = {
                **leg,
                "edge":           round(leg["edge"] * multiplier, 4),
                "llm_downgraded": True,
                "llm_reason":     action.get("reason", ""),
            }
            print(f"[context_analyzer] DOWNGRADED {leg['display']} ×{multiplier} — {action.get('reason')}")
            result.append(updated)

    return result
