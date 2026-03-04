# DOM Maintenance (Playwright)

When Google Flights changes markup, use this repeatable flow before editing parsers.

## 1. Capture browser evidence

Use local fallback mode to confirm page still renders flight cards:

```python
from fast_flights import FlightData, Passengers, get_flights

result = get_flights(
    flight_data=[FlightData(date="2026-04-05", from_airport="HND", to_airport="SIN")],
    trip="one-way",
    seat="economy",
    passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
    fetch_mode="local",
)
print(result.current_price, len(result.flights))
```

If this fails, inspect `fast_flights/local_playwright.py` selectors first.

## 2. Validate parser anchor points

Current parser relies on:
- result group root: `div[jsname="IWWDBc"], div[jsname="YdtKid"]`
- item rows: `ul.Rk10dc li`
- primary aria source: `div[role="link"][aria-label]`

If any of these disappear, treat as parser drift.

## 3. Field triage order

When debugging extraction failures, check in order:
1. `name`, `departure`, `arrival`
2. `duration`, `stops`, `price`
3. extended metadata:
   - `self_transfer`
   - `emissions`
   - `layovers`
   - `flight_numbers`
   - `operated_by`, `aircraft`, `amenities`

## 4. Change policy

- Keep extractor changes minimal and additive.
- Do not remove existing locale patterns unless proven dead.
- Re-run at least one Japanese and one English route after changes.
