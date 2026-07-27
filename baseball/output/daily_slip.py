from datetime import date

from parlay.parlay_builder import recommended_stake, profit_for_stake

# "Double-up, else $25 floor" staking (see parlay_builder.recommended_stake).
BASE_STAKE   = 25.0
PROFIT_FLOOR = 25.0


def print_slip(parlay: dict, num: int | None = None) -> None:
    if not parlay:
        print("\nNo value legs found today.")
        return

    legs         = parlay.get("legs", [])
    combined     = parlay.get("combined_odds", 0)
    total_edge   = parlay.get("total_edge", 0)
    num_legs     = parlay.get("num_legs", 0)

    combined_str = f"+{combined}" if combined > 0 else str(combined)
    stake        = recommended_stake(combined, BASE_STAKE, PROFIT_FLOOR)
    to_win       = profit_for_stake(combined, stake)

    print()
    header = f"=== TODAY'S VALUE PARLAY #{num} ===" if num else "=== TODAY'S VALUE PARLAY ==="
    print(header)
    print(f"Date: {date.today()}")
    print()

    for i, leg in enumerate(legs, 1):
        _print_leg(i, leg)

    print(f"Combined Odds:    {combined_str}")
    print(f"Legs:             {num_legs}")
    print(f"Total Edge Score: {total_edge * 100:.1f}%")  # sum of normalized edges per leg
    print(f"Recommended stake: ${stake:.2f} → win ${to_win:.2f}  (double-up / ${PROFIT_FLOOR:.0f} floor)")
    print("=" * 28)
    print()


def _print_leg(index: int, leg: dict) -> None:
    display  = leg.get("display", "")
    book     = leg.get("book", "").replace("_", " ").title()
    llm_flag = " ⚠ downgraded" if leg.get("llm_downgraded") else ""

    if leg.get("bet_type") == "total":
        run_edge = leg.get("run_edge", 0)
        sign     = "+" if run_edge >= 0 else ""
        edge_str = f"{sign}{run_edge:.1f} runs"
    else:
        edge = leg.get("edge", 0)
        edge_str = f"+{edge * 100:.1f}%" if edge >= 0 else f"{edge * 100:.1f}%"

    print(f"LEG {index}: {display} @ {book} | Edge: {edge_str}{llm_flag}")
