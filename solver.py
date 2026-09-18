from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional

import pulp

from contracts import Battery, Directive, Scenario

HOURS = 24
EPS = 1e-6
TOL = 0.01
ROUND_DP = 4

BatteryAction = Literal["charge", "discharge", "idle"]


@dataclass
class LPSolution:
    grid: list[float]
    solar_used: list[float]
    charge: list[float]
    discharge: list[float]
    energy_after: list[float]


def _clean(x: float) -> float:
    return 0.0 if abs(x) < EPS else x


def _r4(x: float) -> float:
    return round(_clean(float(x)), ROUND_DP)


# ---------------------------------------------------------------------------
# Directive preprocessing
# ---------------------------------------------------------------------------
def solar_reduction_factors(directives: list[Directive]) -> list[float]:
    factors = [1.0] * HOURS
    for dr in directives:
        if dr.directive_type == "solar_reduction" and dr.factor is not None:
            for h in dr.hours:
                if 0 <= h < HOURS:
                    factors[h] *= dr.factor
    return factors


def effective_solar(scenario: Scenario, directives: list[Directive]) -> list[float]:
    factors = solar_reduction_factors(directives)
    return [scenario.solar[h] * factors[h] for h in range(HOURS)]


def reserve_floor(scenario: Scenario, directives: list[Directive]) -> list[float]:
    floor = [scenario.battery.minimum_energy_kwh] * HOURS
    for dr in directives:
        if dr.directive_type == "minimum_battery_reserve" and dr.minimum_energy_kwh is not None:
            for h in dr.hours:
                if 0 <= h < HOURS:
                    floor[h] = max(floor[h], dr.minimum_energy_kwh)
    return floor


def no_charge_hours(directives: list[Directive]) -> set[int]:
    hours: set[int] = set()
    for dr in directives:
        if dr.directive_type == "no_charge_window":
            hours.update(h for h in dr.hours if 0 <= h < HOURS)
    return hours


def no_discharge_hours(directives: list[Directive]) -> set[int]:
    hours: set[int] = set()
    for dr in directives:
        if dr.directive_type == "no_discharge_window":
            hours.update(h for h in dr.hours if 0 <= h < HOURS)
    return hours


def max_grid_limits(directives: list[Directive]) -> dict[int, float]:
    limits: dict[int, float] = {}
    for dr in directives:
        if dr.directive_type == "max_grid_window" and dr.max_grid_kwh is not None:
            for h in dr.hours:
                if 0 <= h < HOURS:
                    if h in limits:
                        limits[h] = min(limits[h], dr.max_grid_kwh)
                    else:
                        limits[h] = dr.max_grid_kwh
    return limits


# ---------------------------------------------------------------------------
# LP construction and solve
# ---------------------------------------------------------------------------
def build_lp(
    scenario: Scenario,
    directives: list[Directive],
    *,
    include_max_grid: bool,
) -> tuple[pulp.LpProblem, dict[str, list[pulp.LpVariable]]]:
    battery = scenario.battery
    prob = pulp.LpProblem("gridwise_schedule", pulp.LpMinimize)

    g = [pulp.LpVariable(f"g_{h}", lowBound=0) for h in range(HOURS)]
    s = [pulp.LpVariable(f"s_{h}", lowBound=0) for h in range(HOURS)]
    c = [pulp.LpVariable(f"c_{h}", lowBound=0) for h in range(HOURS)]
    d = [pulp.LpVariable(f"d_{h}", lowBound=0) for h in range(HOURS)]
    energy = [pulp.LpVariable(f"E_{h}", lowBound=0) for h in range(HOURS)]

    prob += pulp.lpSum(g[h] * scenario.tariff[h] for h in range(HOURS))

    eff = effective_solar(scenario, directives)
    floor = reserve_floor(scenario, directives)
    nc = no_charge_hours(directives)
    nd = no_discharge_hours(directives)
    mg = max_grid_limits(directives)

    for h in range(HOURS):
        prob += g[h] + s[h] + d[h] == scenario.demand[h] + c[h]
        if h == 0:
            prob += energy[h] == battery.initial_energy_kwh + c[h] - d[h]
        else:
            prob += energy[h] == energy[h - 1] + c[h] - d[h]
        prob += s[h] <= eff[h]
        prob += c[h] <= battery.max_charge_kwh_per_hour
        prob += d[h] <= battery.max_discharge_kwh_per_hour
        prob += energy[h] >= floor[h]
        prob += energy[h] <= battery.capacity_kwh
        if h in nc:
            prob += c[h] == 0
        if h in nd:
            prob += d[h] == 0
        if include_max_grid and h in mg:
            prob += g[h] <= mg[h]

    prob += energy[HOURS - 1] == battery.initial_energy_kwh

    return prob, {"g": g, "s": s, "c": c, "d": d, "E": energy}


def solve_lp(
    scenario: Scenario,
    directives: list[Directive],
    *,
    include_max_grid: bool,
) -> Optional[LPSolution]:
    prob, v = build_lp(scenario, directives, include_max_grid=include_max_grid)
    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus.get(prob.status) != "Optimal":
        return None

    def values(key: str) -> list[float]:
        return [float(var.value() or 0.0) for var in v[key]]

    return LPSolution(
        grid=values("g"),
        solar_used=values("s"),
        charge=values("c"),
        discharge=values("d"),
        energy_after=values("E"),
    )


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------
def net_action(charge_h: float, discharge_h: float) -> tuple[BatteryAction, float]:
    net = charge_h - discharge_h
    if net > EPS:
        return "charge", net
    if net < -EPS:
        return "discharge", net
    return "idle", 0.0


def grid_only_baseline(scenario: Scenario, directives: list[Directive]) -> LPSolution:
    eff = effective_solar(scenario, directives)
    solar_used = [min(eff[h], scenario.demand[h]) for h in range(HOURS)]
    grid = [scenario.demand[h] - solar_used[h] for h in range(HOURS)]
    battery = scenario.battery
    return LPSolution(
        grid=grid,
        solar_used=solar_used,
        charge=[0.0] * HOURS,
        discharge=[0.0] * HOURS,
        energy_after=[battery.initial_energy_kwh] * HOURS,
    )


def _assemble(scenario: Scenario, solution: LPSolution) -> dict:
    battery = scenario.battery
    hourly_plan: list[dict] = []

    prev_energy = battery.initial_energy_kwh
    for h in range(HOURS):
        action, _net = net_action(solution.charge[h], solution.discharge[h])
        if action == "idle":
            battery_kwh = 0.0
            signed = 0.0
        else:
            battery_kwh = _r4(abs(_net))
            signed = battery_kwh if action == "charge" else -battery_kwh

        solar_used = _r4(solution.solar_used[h])
        energy_after = _r4(prev_energy + signed)
        grid = _r4(scenario.demand[h] + signed - solar_used)

        hourly_plan.append(
            {
                "hour": h,
                "grid_kwh": grid,
                "solar_used_kwh": solar_used,
                "battery_action": action,
                "battery_kwh": battery_kwh,
                "battery_energy_after_kwh": energy_after,
            }
        )
        prev_energy = prev_energy + signed

    total_grid = _r4(sum(row["grid_kwh"] for row in hourly_plan))
    total_cost = _r4(
        sum(row["grid_kwh"] * scenario.tariff[row["hour"]] for row in hourly_plan)
    )
    peak_grid = _r4(max(row["grid_kwh"] for row in hourly_plan))

    return {
        "hourly_plan": hourly_plan,
        "total_grid_kwh": total_grid,
        "total_cost_bdt": total_cost,
        "peak_grid_kwh": peak_grid,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def solve(scenario: Scenario, directives: list[Directive]) -> dict:
    solution = solve_lp(scenario, directives, include_max_grid=True)
    if solution is None:
        solution = solve_lp(scenario, directives, include_max_grid=False)
    if solution is None:
        solution = solve_lp(scenario, [], include_max_grid=False)
    if solution is None:
        solution = grid_only_baseline(scenario, directives)
    return _assemble(scenario, solution)


# ---------------------------------------------------------------------------
# Independent validation
# ---------------------------------------------------------------------------
def _as_number(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def validate_plan(scenario: Scenario, directives: list[Directive], plan: dict) -> list[str]:
    violations: list[str] = []

    if not isinstance(plan, dict):
        return [f"plan must be a dict, got {type(plan).__name__}"]

    hourly = plan.get("hourly_plan")
    if not isinstance(hourly, list):
        return ["plan['hourly_plan'] must be a list"]
    if len(hourly) != HOURS:
        violations.append(f"hourly_plan must have {HOURS} entries, got {len(hourly)}")

    battery = scenario.battery
    eff = effective_solar(scenario, directives)
    floor = reserve_floor(scenario, directives)
    nc = no_charge_hours(directives)
    nd = no_discharge_hours(directives)
    mg = max_grid_limits(directives)

    seen_hours: set[int] = set()
    energy_by_hour: dict[int, float] = {}
    signed_by_hour: dict[int, float] = {}

    for index, entry in enumerate(hourly):
        if not isinstance(entry, dict):
            violations.append(f"entry {index} is not a dict")
            continue

        hour = entry.get("hour")
        if isinstance(hour, bool) or not isinstance(hour, int):
            violations.append(f"entry {index}: hour must be an integer, got {hour!r}")
            continue
        if hour < 0 or hour >= HOURS:
            violations.append(f"entry {index}: hour {hour} out of range 0..23")
            continue
        if hour in seen_hours:
            violations.append(f"duplicate hour {hour}")
            continue
        seen_hours.add(hour)

        grid = _as_number(entry.get("grid_kwh"))
        solar_used = _as_number(entry.get("solar_used_kwh"))
        battery_kwh = _as_number(entry.get("battery_kwh"))
        energy_after = _as_number(entry.get("battery_energy_after_kwh"))

        for name, value in (
            ("grid_kwh", grid),
            ("solar_used_kwh", solar_used),
            ("battery_kwh", battery_kwh),
            ("battery_energy_after_kwh", energy_after),
        ):
            if value is None:
                violations.append(f"hour {hour}: {name} must be a finite number")
            elif value < -EPS:
                violations.append(f"hour {hour}: {name} is negative ({value})")

        action = entry.get("battery_action")
        if action not in ("charge", "discharge", "idle"):
            violations.append(f"hour {hour}: invalid battery_action {action!r}")
            continue

        if grid is None or solar_used is None or battery_kwh is None or energy_after is None:
            continue

        if action == "idle" and battery_kwh != 0.0:
            violations.append(
                f"hour {hour}: battery_kwh must be 0.0 when idle, got {battery_kwh}"
            )

        if action == "charge":
            signed = battery_kwh
            if battery_kwh > battery.max_charge_kwh_per_hour + TOL:
                violations.append(
                    f"hour {hour}: charge {battery_kwh} exceeds max_charge "
                    f"{battery.max_charge_kwh_per_hour}"
                )
        elif action == "discharge":
            signed = -battery_kwh
            if battery_kwh > battery.max_discharge_kwh_per_hour + TOL:
                violations.append(
                    f"hour {hour}: discharge {battery_kwh} exceeds max_discharge "
                    f"{battery.max_discharge_kwh_per_hour}"
                )
        else:
            signed = 0.0

        signed_by_hour[hour] = signed
        energy_by_hour[hour] = energy_after

        if solar_used > eff[hour] + TOL:
            violations.append(
                f"hour {hour}: solar_used {solar_used} exceeds effective_solar {eff[hour]}"
            )

        balance = grid + solar_used - signed - scenario.demand[hour]
        if abs(balance) > TOL:
            violations.append(
                f"hour {hour}: energy balance off by {balance:.4f} "
                f"(grid + solar - net_battery should equal demand)"
            )

        if energy_after < floor[hour] - TOL:
            violations.append(
                f"hour {hour}: battery energy {energy_after} below required floor {floor[hour]}"
            )
        if energy_after > battery.capacity_kwh + TOL:
            violations.append(
                f"hour {hour}: battery energy {energy_after} exceeds capacity "
                f"{battery.capacity_kwh}"
            )

        if hour in nc and signed > TOL:
            violations.append(f"hour {hour}: charging during no_charge_window")
        if hour in nd and signed < -TOL:
            violations.append(f"hour {hour}: discharging during no_discharge_window")
        if hour in mg and grid > mg[hour] + TOL:
            violations.append(
                f"hour {hour}: grid {grid} exceeds max_grid limit {mg[hour]}"
            )

    for hour in range(HOURS):
        if hour not in energy_by_hour:
            violations.append(f"hour {hour}: missing from hourly_plan")
            continue
        prev = battery.initial_energy_kwh if hour == 0 else energy_by_hour.get(hour - 1)
        if prev is None:
            continue
        expected = prev + signed_by_hour[hour]
        if abs(energy_by_hour[hour] - expected) > TOL:
            violations.append(
                f"hour {hour}: state transition off by "
                f"{energy_by_hour[hour] - expected:.4f}"
            )

    if energy_by_hour:
        final = energy_by_hour.get(HOURS - 1)
        if final is not None and abs(final - battery.initial_energy_kwh) > TOL:
            violations.append(
                f"final battery energy {final} != initial {battery.initial_energy_kwh}"
            )

    total_grid = _as_number(plan.get("total_grid_kwh"))
    total_cost = _as_number(plan.get("total_cost_bdt"))
    peak_grid = _as_number(plan.get("peak_grid_kwh"))

    if len(energy_by_hour) == HOURS:
        grids = [
            float(entry["grid_kwh"])
            for entry in hourly
            if isinstance(entry, dict) and _as_number(entry.get("grid_kwh")) is not None
        ]
        if len(grids) == HOURS:
            expected_total_grid = sum(grids)
            expected_total_cost = sum(
                entry["grid_kwh"] * scenario.tariff[entry["hour"]]
                for entry in hourly
                if isinstance(entry, dict)
            )
            expected_peak = max(grids)

            if total_grid is None:
                violations.append("total_grid_kwh must be a finite number")
            elif abs(total_grid - expected_total_grid) > TOL:
                violations.append(
                    f"total_grid_kwh {total_grid} != recomputed {expected_total_grid:.4f}"
                )
            if total_cost is None:
                violations.append("total_cost_bdt must be a finite number")
            elif abs(total_cost - expected_total_cost) > TOL:
                violations.append(
                    f"total_cost_bdt {total_cost} != recomputed {expected_total_cost:.4f}"
                )
            if peak_grid is None:
                violations.append("peak_grid_kwh must be a finite number")
            elif abs(peak_grid - expected_peak) > TOL:
                violations.append(
                    f"peak_grid_kwh {peak_grid} != recomputed {expected_peak:.4f}"
                )
    else:
        for name, value in (
            ("total_grid_kwh", total_grid),
            ("total_cost_bdt", total_cost),
            ("peak_grid_kwh", peak_grid),
        ):
            if value is None:
                violations.append(f"{name} must be a finite number")

    return violations


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    smoke_battery = Battery(
        capacity_kwh=10.0,
        initial_energy_kwh=5.0,
        minimum_energy_kwh=1.0,
        max_charge_kwh_per_hour=3.0,
        max_discharge_kwh_per_hour=3.0,
    )
    smoke_scenario = Scenario(
        scenario_id="smoke",
        operator_notes=[],
        demand=[
            1.5, 1.2, 1.0, 1.0, 1.2, 2.0, 3.0, 3.5, 3.0, 2.5, 2.2, 2.0,
            2.4, 2.8, 3.0, 3.2, 3.5, 4.0, 4.2, 3.8, 3.0, 2.5, 2.0, 1.8,
        ],
        solar=[
            0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 5.5,
            6.0, 5.5, 5.0, 4.0, 3.0, 1.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0,
        ],
        tariff=[
            5.0, 5.0, 4.5, 4.5, 5.0, 6.0, 8.0, 10.0, 12.0, 12.0, 10.0, 9.0,
            8.0, 8.0, 9.0, 10.0, 12.0, 14.0, 15.0, 13.0, 10.0, 8.0, 6.0, 5.0,
        ],
        battery=smoke_battery,
    )
    smoke_directives = [
        Directive(
            note_index=0,
            directive_type="solar_reduction",
            hours=[10, 11, 12, 13],
            factor=0.5,
            explanation="panel shading at midday",
        ),
        Directive(
            note_index=1,
            directive_type="no_charge_window",
            hours=[17, 18, 19],
            explanation="peak hours, do not charge",
        ),
    ]

    smoke_plan = solve(smoke_scenario, smoke_directives)
    print(json.dumps(smoke_plan, indent=2))
    smoke_violations = validate_plan(smoke_scenario, smoke_directives, smoke_plan)
    print("\nvalidate_plan violations:")
    print(smoke_violations if smoke_violations else "none")
