from datetime import date


def print_slip(parlay: dict) -> None:
    if not parlay:
        print("No value legs found today.")
        return

    print(f"\n=== TODAY'S VALUE PARLAY ===")
    print(f"Date: {date.today()}")
    print()

    for i, leg in enumerate(parlay.get("legs", []), 1):
        _print_leg(i, leg)

    print(f"\nCombined Odds: {parlay.get('combined_odds')}")
    print(f"Legs: {parlay.get('num_legs')}")
    print(f"Total Edge Score: {parlay.get('total_edge', 0) * 100:.1f}%")
    print("===========================\n")


def _print_leg(index: int, leg: dict) -> None:
    pass
