# Cases

`public_cases.json` holds the judge cases consumed by `harness.py` (the
standalone judge simulator). The harness POSTs each case's `input` to
`{url}/optimize-energy`, checks the response, and independently re-verifies
the returned plan against the case's **expected** directives.

## Running

```bash
python harness.py --url https://my-service.example.com
python harness.py --url http://localhost:8000 --cases cases/public_cases.json --timeout 30
```

The harness uses only the Python standard library (plus `contracts.py` for the
canonical directive-type list), so it runs without the service's dependencies.

Exit codes:

- `0` — every case fully valid
- `1` — at least one case failed (schema, interpretation, or replay)
- `2` — harness error (case file missing/malformed)

## Case file format

Top level is either a JSON array of cases or an object with a `cases` array.

```json
{
  "cases": [
    {
      "case_id": "public_001",
      "input": {
        "scenario_id": "dhaka_summer_01",
        "operator_notes": [
          "Dust storm expected 10:00-13:00, panels at half output.",
          "Do not charge the battery during 17:00-19:00 peak.",
          "Everything else is fine."
        ],
        "demand":  [1.5, 1.2, 1.0, 1.0, 1.2, 2.0, 3.0, 3.5, 3.0, 2.5, 2.2, 2.0,
                    2.4, 2.8, 3.0, 3.2, 3.5, 4.0, 4.2, 3.8, 3.0, 2.5, 2.0, 1.8],
        "solar":   [0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 5.5,
                    6.0, 5.5, 5.0, 4.0, 3.0, 1.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0],
        "tariff":  [5.0, 5.0, 4.5, 4.5, 5.0, 6.0, 8.0, 10.0, 12.0, 12.0, 10.0, 9.0,
                    8.0, 8.0, 9.0, 10.0, 12.0, 14.0, 15.0, 13.0, 10.0, 8.0, 6.0, 5.0],
        "battery": {
          "capacity_kwh": 10.0,
          "initial_energy_kwh": 5.0,
          "minimum_energy_kwh": 1.0,
          "max_charge_kwh_per_hour": 3.0,
          "max_discharge_kwh_per_hour": 3.0
        }
      },
      "expected": {
        "directive_interpretation": [
          {"note_index": 0, "directive_type": "solar_reduction",
           "hours": [10, 11, 12, 13], "factor": 0.5},
          {"note_index": 1, "directive_type": "no_charge_window",
           "hours": [17, 18, 19]},
          {"note_index": 2, "directive_type": "no_op", "hours": []}
        ],
        "reference_cost_bdt": 158.9
      }
    }
  ]
}
```

- `input` mirrors `contracts.Scenario` (scenario_id, operator_notes, demand[24],
  solar[24], tariff[24], battery{...}).
- `expected.directive_interpretation` has exactly one entry per operator note,
  `note_index` equal to the note's 0-based position. Numeric keys appear only
  where the type needs them (`factor`, `minimum_energy_kwh`, `max_grid_kwh`).
  Free-text `explanation` is allowed and ignored by the judge.
- `expected.reference_cost_bdt` is the reference optimal LP cost for the
  expected directives (e.g. produced by `solver.solve`). Used only for the
  reported cost ratio `min(1, reference / ours)`; it does not fail a case.

## Response contract the service must satisfy

`POST /optimize-energy` returns a JSON object with required top-level keys:

- `scenario_id` — must echo the request's `scenario_id`
- `directive_interpretation` — one entry per operator note, in `note_index`
  order (note_index values exactly `0..n-1`, ascending, no gaps/duplicates)
- `hourly_plan` — 24 entries, each with `hour`, `grid_kwh`, `solar_used_kwh`,
  `battery_action` (`charge`/`discharge`/`idle`), `battery_kwh`,
  `battery_energy_after_kwh`; `battery_kwh` is exactly `0.0` when `idle`
- `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh` — finite numbers that
  match recomputation from `hourly_plan`
- `plan_summary` — optional free text

Each `directive_interpretation` entry:

- `note_index` (int), `applies` (bool), `directive_type` (string),
  `structured_adjustment` (object or null), `explanation` (free text, ignored)
- `applies` is `true` exactly when `directive_type != "no_op"`
- `structured_adjustment` is `null` exactly for `no_op`; otherwise an object
  with **exactly** these keys:

| directive_type            | required keys                     |
|---------------------------|-----------------------------------|
| `solar_reduction`         | `hours`, `factor`                 |
| `minimum_battery_reserve` | `hours`, `minimum_energy_kwh`     |
| `no_charge_window`        | `hours`                           |
| `no_discharge_window`     | `hours`                           |
| `max_grid_window`         | `hours`, `max_grid_kwh`           |

- every `hours` array is unique ascending integers in `0..23`

## What the harness checks per case

1. **Request**: POST `input`, measure latency.
2. **Schema**: all rules in the contract above.
3. **Interpretation**: per-note pass/fail vs the expected interpretation on
   `applies`, `directive_type`, `hours` (exact), and numeric values
   (tolerance 0.01). `explanation` ignored.
4. **Replay**: independently replays `hourly_plan` against the **expected**
   directives (not the returned ones): effective solar after `solar_reduction`
   factors (multiplied per hour), energy balance per hour (0.01),
   `solar_used <= effective_solar`, battery capacity/reserve bounds, charge/
   discharge rate limits, state transitions from `initial_energy_kwh`, final
   energy == initial (0.01), no-charge / no-discharge / reserve / grid-cap
   windows, and the three reported totals vs recomputation.
5. **Cost**: `cost_ratio = min(1, reference_cost_bdt / total_cost_bdt)`
   (reported only, never fails a case).

A case is **fully valid** iff the request succeeded, the schema has zero
violations, every note passes interpretation, and the replay has zero
violations.

## Summary metrics

- cases fully valid (`valid/total`)
- interpretation accuracy (`notes passed / notes total`)
- mean cost ratio
- p50 and p95 request latency
