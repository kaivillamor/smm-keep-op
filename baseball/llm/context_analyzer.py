import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-opus-5"

# Thinking is on by default on Opus 5 and shares max_tokens with the response,
# so this is sized for reasoning headroom — not for the small JSON payload.
MAX_TOKENS = 16000

# Structured outputs: the response is schema-validated, so a malformed-JSON
# reply isn't a failure mode we have to handle. Note the schema can't express
# numeric bounds — edge_multiplier is range-checked in _apply_llm_adjustments.
_ACTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "game_id":         {"type": "string"},
                    "bet_type":        {"type": "string", "enum": ["ml", "total"]},
                    "action":          {"type": "string", "enum": ["remove", "downgrade"]},
                    "edge_multiplier": {"type": "number"},
                    "reason":          {"type": "string"},
                },
                "required": ["game_id", "bet_type", "action", "edge_multiplier", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["actions"],
    "additionalProperties": False,
}


def analyze_context(legs: list[dict], news_summary: str = "") -> list[dict]:
    if not legs:
        return legs

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("[context_analyzer] No ANTHROPIC_API_KEY set — returning legs unchanged")
        return legs

    client = anthropic.Anthropic(api_key=api_key)

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

Rules:
- Only include legs you are flagging. Legs not listed are approved as-is.
- "remove" = drop the leg entirely (set edge_multiplier to 0).
- "downgrade" = reduce confidence; set edge_multiplier between 0.1 and 0.9.
- Do not add new legs. The quantitative model is the source of truth for what qualifies.
- If no legs need flagging, return an empty actions list.
- game_id and bet_type must match a leg above exactly."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": _ACTIONS_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )

        if response.stop_reason == "refusal":
            print("[context_analyzer] Model declined the request — returning legs unchanged")
            return legs

        # With thinking on, content may lead with thinking blocks — take the text one.
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            print("[context_analyzer] No text block in response — returning legs unchanged")
            return legs

        return _apply_llm_adjustments(legs, text)
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
    except (json.JSONDecodeError, KeyError, TypeError):
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
            # Schema can't bound numbers, so clamp to the documented range here.
            multiplier = min(max(float(action.get("edge_multiplier", 0.5)), 0.1), 0.9)
            updated    = {
                **leg,
                "edge":           round(leg["edge"] * multiplier, 4),
                "llm_downgraded": True,
                "llm_reason":     action.get("reason", ""),
            }
            print(f"[context_analyzer] DOWNGRADED {leg['display']} ×{multiplier} — {action.get('reason')}")
            result.append(updated)

    return result
