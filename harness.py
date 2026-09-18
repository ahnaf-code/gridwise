"""Standalone judge simulator for the GridWise /optimize-energy service.

Usage:
    python harness.py --url https://my-service.example.com
    python harness.py --url http://localhost:8000 --cases cases/public_cases.json

Stdlib-only (plus the canonical DirectiveType list from contracts.py) so it can
run against any remote deployment without the service's own dependencies.

See cases/README.md for the case-file format and the full judging contract.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from typing import Any, get_args

from contracts import DirectiveType

TOL = 0.01
HOURS = 24

DIRECTIVE_TYPES = set(get_args(DirectiveType))

REQUIRED_TOP_LEVEL_KEYS = {
    "scenario_id",
    "directive_interpretation",
    "hourly_plan",
    "total_grid_kwh",
    "total_cost_bdt",
    "peak_grid_kwh",
}

HOURLY_PLAN_KEYS = {
    "hour",
    "grid_kwh",
    "solar_used_kwh",
    "battery_action",
    "battery_kwh",
    "battery_energy_after_kwh",
}

BATTERY_ACTIONS = {"charge", "discharge", "idle"}

# Exact structured_adjustment keys required per directive type (no_op => null).
REQUIRED_ADJUSTMENT_KEYS: dict[str, set[str]] = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}

NUMERIC_ADJUSTMENT_KEY: dict[str, str] = {
    "solar_reduction": "factor",
    "minimum_battery_reserve": "minimum_energy_kwh",
    "max_grid_window": "max_grid_kwh",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    if not math.isfinite(x):
        return None
    return float(x)


def _percentile(data: list[float], p: float) -> float:
    """Linear-interpolation percentile (numpy 'linear' method)."""
    if not data:
        return 0.0
    xs = sorted(data)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def post_json(url: str, payload: Any, timeout: float) -> tuple[Any, float, str | None]:
    """POST JSON, return (parsed_body, latency_seconds, error_message)."""
    endpoint = url.rstrip("/") + "/optimize-energy"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            latency = time.perf_counter() - start
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        latency = time.perf_counter() - start
        try:
            detail = exc.read().decode("utf-8")[:200]
        except Exception:
            detail = ""
        return None, latency, f"HTTP {exc.code} {exc.reason} {detail}".strip()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        latency = time.perf_counter() - start
        return None, latency, f"request failed: {exc}"

    try:
        return json.loads(raw), latency, None
    except json.JSONDecodeError as exc:
        return None, latency, f"response is not valid JSON: {exc}"


# ---------------------------------------------------------------------------
# Step 2: response schema checks
# ---------------------------------------------------------------------------
def _check_hours_array(hours: Any, ctx: str) -> list[str]:
    v: list[str] = []
    if not isinstance(hours, list):
        return [f"{ctx}: hours must be an array"]
    for h in hours:
        if not _is_int(h):
            v.append(f"{ctx}: hour {h!r} is not an integer")
        elif h < 0 or h > 23:
            v.append(f"{ctx}: hour {h} out of range 0..23")
    if hours != sorted(hours) or len(set(hours)) != len(hours):
        v.append(f"{ctx}: hours must be unique ascending, got {hours}")
    return v


def _check_interpretation_entry(i: int, entry: Any) -> list[str]:
    ctx = f"directive_interpretation[{i}]"
    v: list[str] = []
    if not isinstance(entry, dict):
        return [f"{ctx}: must be an object"]

    if not _is_int(entry.get("note_index")):
        v.append(f"{ctx}: note_index must be an integer")

    dtype = entry.get("directive_type")
    if dtype not in DIRECTIVE_TYPES:
        v.append(f"{ctx}: unknown directive_type {dtype!r}")
        return v

    applies = entry.get("applies")
    expected_applies = dtype != "no_op"
    if not isinstance(applies, bool):
        v.append(f"{ctx}: applies must be a boolean")
    elif applies != expected_applies:
        v.append(
            f"{ctx}: applies must be {expected_applies} when directive_type is {dtype!r}"
        )

    if "structured_adjustment" not in entry:
        v.append(f"{ctx}: missing structured_adjustment")
        return v
    sa = entry["structured_adjustment"]

    if dtype == "no_op":
        if sa is not None:
            v.append(f"{ctx}: structured_adjustment must be null for no_op")
        return v

    if sa is None:
        v.append(f"{ctx}: structured_adjustment must be an object for {dtype!r}")
        return v
    if not isinstance(sa, dict):
        v.append(f"{ctx}: structured_adjustment must be an object")
        return v

    required = REQUIRED_ADJUSTMENT_KEYS[dtype]
    if set(sa.keys()) != required:
        v.append(
            f"{ctx}: structured_adjustment keys must be exactly {sorted(required)}, "
            f"got {sorted(sa.keys())}"
        )
    if "hours" in sa:
        v.extend(_check_hours_array(sa["hours"], f"{ctx}.structured_adjustment"))
    numeric_key = NUMERIC_ADJUSTMENT_KEY.get(dtype)
    if numeric_key and numeric_key in sa and _num(sa[numeric_key]) is None:
        v.append(f"{ctx}: {numeric_key} must be a finite number")
    return v


def check_response_schema(case_input: dict, response: Any) -> list[str]:
    v: list[str] = []
    if not isinstance(response, dict):
        return ["response body is not a JSON object"]

    for key in sorted(REQUIRED_TOP_LEVEL_KEYS):
        if key not in response:
            v.append(f"missing top-level key {key!r}")

    expected_sid = case_input.get("scenario_id")
    if "scenario_id" in response and response["scenario_id"] != expected_sid:
        v.append(
            f"scenario_id mismatch: expected {expected_sid!r}, "
            f"got {response['scenario_id']!r}"
        )

    notes = case_input.get("operator_notes", [])
    if "directive_interpretation" in response:
        di = response["directive_interpretation"]
        if not isinstance(di, list):
            v.append("directive_interpretation must be an array")
        else:
            if len(di) != len(notes):
                v.append(
                    f"directive_interpretation has {len(di)} entries, expected "
                    f"{len(notes)} (exactly one per operator note)"
                )
            for i, entry in enumerate(di):
                v.extend(_check_interpretation_entry(i, entry))
            idxs = [e.get("note_index") for e in di if isinstance(e, dict)]
            if all(_is_int(x) for x in idxs):
                if idxs != sorted(idxs):
                    v.append("directive_interpretation entries not in note_index order")
                if sorted(idxs) != list(range(len(notes))):
                    v.append(
                        f"note_index values must be exactly 0..{len(notes) - 1} "
                        f"with no gaps or duplicates"
                    )

    if "hourly_plan" in response:
        hp = response["hourly_plan"]
        if not isinstance(hp, list):
            v.append("hourly_plan must be an array")
        else:
            if len(hp) != HOURS:
                v.append(f"hourly_plan must have {HOURS} entries, got {len(hp)}")
            for i, entry in enumerate(hp):
                if not isinstance(entry, dict):
                    v.append(f"hourly_plan[{i}] must be an object")
                    continue
                missing = HOURLY_PLAN_KEYS - set(entry.keys())
                if missing:
                    v.append(f"hourly_plan[{i}] missing keys {sorted(missing)}")
                    continue
                if not _is_int(entry["hour"]):
                    v.append(f"hourly_plan[{i}].hour must be an integer")
                if entry["battery_action"] not in BATTERY_ACTIONS:
                    v.append(
                        f"hourly_plan[{i}].battery_action must be one of "
                        f"{sorted(BATTERY_ACTIONS)}"
                    )
                for key in (
                    "grid_kwh",
                    "solar_used_kwh",
                    "battery_kwh",
                    "battery_energy_after_kwh",
                ):
                    if _num(entry[key]) is None:
                        v.append(f"hourly_plan[{i}].{key} must be a finite number")

    for key in ("total_grid_kwh", "total_cost_bdt", "peak_grid_kwh"):
        if key in response and _num(response[key]) is None:
            v.append(f"{key} must be a finite number")

    return v


# ---------------------------------------------------------------------------
# Step 3: interpretation comparison (expected vs returned)
# ---------------------------------------------------------------------------
def _returned_hours(entry: dict) -> Any:
    sa = entry.get("structured_adjustment")
    if isinstance(sa, dict):
        return sa.get("hours", [])
    return []


def compare_interpretation(
    expected: list[dict], returned: Any
) -> tuple[int, int, list[str]]:
    """Return (notes_passed, notes_total, failure_messages)."""
    failures: list[str] = []
    if not isinstance(returned, list):
        return 0, len(expected), ["directive_interpretation is not an array"]

    returned_by_index: dict[int, dict] = {}
    for entry in returned:
        if isinstance(entry, dict) and _is_int(entry.get("note_index")):
            returned_by_index[entry["note_index"]] = entry

    passed = 0
    for exp in expected:
        idx = exp.get("note_index")
        label = f"note {idx}"
        exp_type = exp.get("directive_type")
        exp_applies = exp_type != "no_op"
        exp_hours = exp.get("hours", [])

        ret = returned_by_index.get(idx)
        if ret is None:
            failures.append(f"{label}: no returned entry with note_index {idx}")
            continue

        problems: list[str] = []
        ret_type = ret.get("directive_type")
        ret_applies = ret.get("applies")
        if ret_applies != exp_applies:
            problems.append(f"applies expected {exp_applies}, got {ret_applies!r}")
        if ret_type != exp_type:
            problems.append(f"directive_type expected {exp_type!r}, got {ret_type!r}")

        ret_hours = _returned_hours(ret)
        if ret_hours != exp_hours:
            problems.append(f"hours expected {exp_hours}, got {ret_hours}")

        numeric_key = NUMERIC_ADJUSTMENT_KEY.get(exp_type)
        if numeric_key and ret_type == exp_type:
            exp_val = _num(exp.get(numeric_key))
            sa = ret.get("structured_adjustment")
            ret_val = _num(sa.get(numeric_key)) if isinstance(sa, dict) else None
            if exp_val is not None:
                if ret_val is None:
                    problems.append(f"{numeric_key} missing or not numeric")
                elif abs(ret_val - exp_val) > TOL:
                    problems.append(
                        f"{numeric_key} expected {exp_val}, got {ret_val}"
                    )

        if problems:
            failures.append(f"{label}: " + "; ".join(problems))
        else:
            passed += 1

    return passed, len(expected), failures


# ---------------------------------------------------------------------------
# Step 4: independent physical replay against EXPECTED directives
# ---------------------------------------------------------------------------
def _effective_solar(solar: list[float], directives: list[dict]) -> list[float]:
    factors = [1.0] * HOURS
    for d in directives:
        if d.get("directive_type") == "solar_reduction":
            factor = _num(d.get("factor"))
            if factor is None:
                continue
            for h in d.get("hours", []):
                if 0 <= h < HOURS:
                    factors[h] *= factor
    return [solar[h] * factors[h] for h in range(HOURS)]


def _reserve_floor(minimum: float, directives: list[dict]) -> list[float]:
    floor = [minimum] * HOURS
    for d in directives:
        if d.get("directive_type") == "minimum_battery_reserve":
            val = _num(d.get("minimum_energy_kwh"))
            if val is None:
                continue
            for h in d.get("hours", []):
                if 0 <= h < HOURS:
                    floor[h] = max(floor[h], val)
    return floor


def _window_hours(directives: list[dict], dtype: str) -> set[int]:
    hours: set[int] = set()
    for d in directives:
        if d.get("directive_type") == dtype:
            hours.update(h for h in d.get("hours", []) if 0 <= h < HOURS)
    return hours


def _grid_caps(directives: list[dict]) -> dict[int, float]:
    caps: dict[int, float] = {}
    for d in directives:
        if d.get("directive_type") == "max_grid_window":
            val = _num(d.get("max_grid_kwh"))
            if val is None:
                continue
            for h in d.get("hours", []):
                if 0 <= h < HOURS:
                    caps[h] = min(caps[h], val) if h in caps else val
    return caps


def replay_hourly_plan(
    case_input: dict, expected_directives: list[dict], response: Any
) -> list[str]:
    v: list[str] = []
    if not isinstance(response, dict):
        return ["cannot replay: response is not an object"]
    hourly = response.get("hourly_plan")
    if not isinstance(hourly, list) or len(hourly) != HOURS:
        return [f"cannot replay: hourly_plan must be an array of {HOURS} entries"]

    battery = case_input.get("battery", {})
    demand = case_input.get("demand", [])
    solar = case_input.get("solar", [])
    tariff = case_input.get("tariff", [])
    if not (
        isinstance(battery, dict)
        and len(demand) == HOURS
        and len(solar) == HOURS
        and len(tariff) == HOURS
    ):
        return ["cannot replay: case input is missing demand/solar/tariff/battery"]

    capacity = _num(battery.get("capacity_kwh")) or 0.0
    initial = _num(battery.get("initial_energy_kwh")) or 0.0
    minimum = _num(battery.get("minimum_energy_kwh")) or 0.0
    max_charge = _num(battery.get("max_charge_kwh_per_hour")) or 0.0
    max_discharge = _num(battery.get("max_discharge_kwh_per_hour")) or 0.0

    eff_solar = _effective_solar(solar, expected_directives)
    floor = _reserve_floor(minimum, expected_directives)
    no_charge = _window_hours(expected_directives, "no_charge_window")
    no_discharge = _window_hours(expected_directives, "no_discharge_window")
    grid_caps = _grid_caps(expected_directives)

    seen: set[int] = set()
    energy_by_hour: dict[int, float] = {}
    signed_by_hour: dict[int, float] = {}
    grid_by_hour: dict[int, float] = {}

    for i, entry in enumerate(hourly):
        if not isinstance(entry, dict):
            v.append(f"hourly_plan[{i}] is not an object")
            continue
        hour = entry.get("hour")
        grid = _num(entry.get("grid_kwh"))
        solar_used = _num(entry.get("solar_used_kwh"))
        battery_kwh = _num(entry.get("battery_kwh"))
        energy_after = _num(entry.get("battery_energy_after_kwh"))
        action = entry.get("battery_action")

        if not _is_int(hour) or hour < 0 or hour >= HOURS:
            v.append(f"hourly_plan[{i}]: invalid hour {hour!r}")
            continue
        if hour in seen:
            v.append(f"duplicate hour {hour}")
            continue
        seen.add(hour)

        if None in (grid, solar_used, battery_kwh, energy_after):
            v.append(f"hour {hour}: non-numeric value(s)")
            continue
        assert grid is not None and solar_used is not None
        assert battery_kwh is not None and energy_after is not None

        for name, val in (
            ("grid_kwh", grid),
            ("solar_used_kwh", solar_used),
            ("battery_kwh", battery_kwh),
            ("battery_energy_after_kwh", energy_after),
        ):
            if val < -TOL:
                v.append(f"hour {hour}: {name} is negative ({val})")

        if action == "charge":
            signed = battery_kwh
            if battery_kwh > max_charge + TOL:
                v.append(
                    f"hour {hour}: charge {battery_kwh} exceeds max_charge {max_charge}"
                )
        elif action == "discharge":
            signed = -battery_kwh
            if battery_kwh > max_discharge + TOL:
                v.append(
                    f"hour {hour}: discharge {battery_kwh} exceeds max_discharge "
                    f"{max_discharge}"
                )
        elif action == "idle":
            signed = 0.0
            if abs(battery_kwh) > 1e-9:
                v.append(
                    f"hour {hour}: battery_kwh must be 0.0 when idle, got {battery_kwh}"
                )
        else:
            v.append(f"hour {hour}: invalid battery_action {action!r}")
            continue

        signed_by_hour[hour] = signed
        energy_by_hour[hour] = energy_after
        grid_by_hour[hour] = grid

        balance = grid + solar_used - signed - demand[hour]
        if abs(balance) > TOL:
            v.append(
                f"hour {hour}: energy balance off by {balance:.4f} "
                f"(grid + solar - net_battery should equal demand)"
            )
        if solar_used > eff_solar[hour] + TOL:
            v.append(
                f"hour {hour}: solar_used {solar_used} exceeds effective solar "
                f"{eff_solar[hour]:.4f}"
            )
        if energy_after > capacity + TOL:
            v.append(
                f"hour {hour}: battery energy {energy_after} exceeds capacity {capacity}"
            )
        if energy_after < floor[hour] - TOL:
            v.append(
                f"hour {hour}: battery energy {energy_after} below reserve floor "
                f"{floor[hour]}"
            )
        if hour in no_charge and signed > TOL:
            v.append(f"hour {hour}: charging during no_charge_window")
        if hour in no_discharge and signed < -TOL:
            v.append(f"hour {hour}: discharging during no_discharge_window")
        if hour in grid_caps and grid > grid_caps[hour] + TOL:
            v.append(
                f"hour {hour}: grid {grid} exceeds max_grid limit {grid_caps[hour]}"
            )

    for hour in range(HOURS):
        if hour not in energy_by_hour:
            v.append(f"hour {hour}: missing from hourly_plan")
            continue
        prev = initial if hour == 0 else energy_by_hour.get(hour - 1)
        if prev is None:
            continue
        expected = prev + signed_by_hour[hour]
        if abs(energy_by_hour[hour] - expected) > TOL:
            v.append(
                f"hour {hour}: state transition off by "
                f"{energy_by_hour[hour] - expected:.4f}"
            )

    if HOURS - 1 in energy_by_hour:
        final = energy_by_hour[HOURS - 1]
        if abs(final - initial) > TOL:
            v.append(f"final battery energy {final} != initial {initial}")

    if len(grid_by_hour) == HOURS:
        expected_total_grid = sum(grid_by_hour[h] for h in range(HOURS))
        expected_total_cost = sum(grid_by_hour[h] * tariff[h] for h in range(HOURS))
        expected_peak = max(grid_by_hour[h] for h in range(HOURS))
        for key, recomputed in (
            ("total_grid_kwh", expected_total_grid),
            ("total_cost_bdt", expected_total_cost),
            ("peak_grid_kwh", expected_peak),
        ):
            reported = _num(response.get(key))
            if reported is None:
                v.append(f"{key} must be a finite number")
            elif abs(reported - recomputed) > TOL:
                v.append(f"{key} {reported} != recomputed {recomputed:.4f}")

    return v


# ---------------------------------------------------------------------------
# Case runner
# ---------------------------------------------------------------------------
def load_cases(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("cases", [])
    if not isinstance(data, list):
        raise ValueError("case file must be a JSON array or an object with a 'cases' array")
    return data


def run_case(case: dict, url: str, timeout: float) -> dict:
    case_id = case.get("case_id") or case.get("id") or "unnamed"
    result: dict[str, Any] = {
        "case_id": case_id,
        "latency_s": None,
        "error": None,
        "schema_violations": [],
        "interp_passed": 0,
        "interp_total": 0,
        "interp_failures": [],
        "replay_violations": [],
        "cost_ratio": None,
    }

    case_input = case.get("input")
    expected = case.get("expected", {})
    if not isinstance(case_input, dict):
        result["error"] = "case has no 'input' object"
        return result
    expected_directives = expected.get("directive_interpretation", [])
    reference_cost = _num(expected.get("reference_cost_bdt"))

    body, latency, error = post_json(url, case_input, timeout)
    result["latency_s"] = latency
    if error is not None:
        result["error"] = error
        return result

    result["schema_violations"] = check_response_schema(case_input, body)

    passed, total, failures = compare_interpretation(
        expected_directives, body.get("directive_interpretation") if isinstance(body, dict) else None
    )
    result["interp_passed"] = passed
    result["interp_total"] = total
    result["interp_failures"] = failures

    result["replay_violations"] = replay_hourly_plan(case_input, expected_directives, body)

    if reference_cost is not None and isinstance(body, dict):
        ours = _num(body.get("total_cost_bdt"))
        if ours is not None:
            result["cost_ratio"] = 1.0 if ours <= 0 else min(1.0, reference_cost / ours)

    return result


def is_fully_valid(result: dict) -> bool:
    return (
        result["error"] is None
        and not result["schema_violations"]
        and result["interp_passed"] == result["interp_total"]
        and not result["replay_violations"]
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_table(results: list[dict]) -> None:
    header = (
        f"{'case_id':<22} {'http':<5} {'schema':<9} {'interp':<8} "
        f"{'replay':<9} {'cost_ratio':<10} {'latency_ms':<10}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        http = "ok" if r["error"] is None else "ERR"
        schema = "ok" if not r["schema_violations"] else f"fail({len(r['schema_violations'])})"
        interp = f"{r['interp_passed']}/{r['interp_total']}"
        replay = "ok" if not r["replay_violations"] else f"fail({len(r['replay_violations'])})"
        ratio = f"{r['cost_ratio']:.3f}" if r["cost_ratio"] is not None else "-"
        latency = f"{r['latency_s'] * 1000:.1f}" if r["latency_s"] is not None else "-"
        print(
            f"{str(r['case_id']):<22} {http:<5} {schema:<9} {interp:<8} "
            f"{replay:<9} {ratio:<10} {latency:<10}"
        )


def print_details(results: list[dict], max_items: int = 8) -> None:
    for r in results:
        problems: list[str] = []
        if r["error"]:
            problems.append(f"request error: {r['error']}")
        problems += [f"schema: {m}" for m in r["schema_violations"]]
        problems += [f"interpretation: {m}" for m in r["interp_failures"]]
        problems += [f"replay: {m}" for m in r["replay_violations"]]
        if not problems:
            continue
        print(f"\n{r['case_id']}:")
        for m in problems[:max_items]:
            print(f"  - {m}")
        if len(problems) > max_items:
            print(f"  ... and {len(problems) - max_items} more")


def print_summary(results: list[dict]) -> None:
    total = len(results)
    valid = sum(1 for r in results if is_fully_valid(r))
    notes_passed = sum(r["interp_passed"] for r in results)
    notes_total = sum(r["interp_total"] for r in results)
    ratios = [r["cost_ratio"] for r in results if r["cost_ratio"] is not None]
    latencies = [r["latency_s"] * 1000 for r in results if r["latency_s"] is not None]

    accuracy = f"{notes_passed}/{notes_total}"
    if notes_total:
        accuracy += f" ({100.0 * notes_passed / notes_total:.1f}%)"

    print("\n=== summary ===")
    print(f"cases fully valid      : {valid}/{total}")
    print(f"interpretation accuracy: {accuracy}")
    print(
        "mean cost ratio        : "
        + (f"{sum(ratios) / len(ratios):.4f}" if ratios else "n/a")
    )
    print(
        f"p50 latency            : {_percentile(latencies, 50):.1f} ms"
        if latencies
        else "p50 latency            : n/a"
    )
    print(
        f"p95 latency            : {_percentile(latencies, 95):.1f} ms"
        if latencies
        else "p95 latency            : n/a"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GridWise judge simulator")
    parser.add_argument("--url", required=True, help="base URL of the service")
    parser.add_argument(
        "--cases",
        default="cases/public_cases.json",
        help="path to the cases JSON file (default: cases/public_cases.json)",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="per-request timeout in seconds"
    )
    args = parser.parse_args(argv)

    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"harness error: cannot load cases from {args.cases}: {exc}")
        return 2

    if not cases:
        print(f"harness error: no cases found in {args.cases}")
        return 2

    results = [run_case(case, args.url, args.timeout) for case in cases]

    print_table(results)
    print_details(results)
    print_summary(results)

    return 0 if all(is_fully_valid(r) for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
