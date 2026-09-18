import math
from typing import Any, Optional
from contracts import Battery, Directive, DirectiveType

VALID_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}

def _make_noop_pair(note_index: int, text_explanation: str = "No valid operation required.") -> tuple[Directive, dict]:
    """Generates a fallback no_op Directive dataclass and interpretation payload."""
    directive = Directive(
        note_index=note_index,
        directive_type="no_op",
        hours=[],
        factor=None,
        minimum_energy_kwh=None,
        max_grid_kwh=None,
        explanation=text_explanation,
    )
    interpretation = {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": text_explanation,
    }
    return directive, interpretation

def _compute_hours(start: Any, end: Any) -> Optional[list[int]]:
    """Deterministically computes 0-23 sorted hours array including midnight wrap-around."""
    if start is None or end is None:
        return None
    try:
        start_i = int(start)
        end_i = int(end)
    except (ValueError, TypeError):
        return None

    if not (0 <= start_i <= 23 and 0 <= end_i <= 24):
        return None

    if start_i == end_i:
        return None

    if start_i < end_i:
        raw_hours = set(range(start_i, end_i))
    else:  # Midnight wrap-around (e.g. 22 to 2)
        raw_hours = set(range(start_i, 24)).union(set(range(0, end_i)))

    sorted_hours = sorted(raw_hours)
    if not sorted_hours or any(h < 0 or h > 23 for h in sorted_hours):
        return None

    return sorted_hours

def to_directives(
    notes: list[str], 
    intents: list[dict], 
    battery: Battery
) -> tuple[list[Directive], list[dict]]:
    """
    Deterministically transforms raw LLM intent dicts into validated Directive dataclasses
    and structured API response interpretations.
    """
    total_notes = len(notes)
    # Pre-populate map with default no_op values for all note indices 0..N-1
    result_map: dict[int, tuple[Directive, dict]] = {
        i: _make_noop_pair(i, "Default fallback no_op directive.") for i in range(total_notes)
    }

    if not isinstance(intents, list):
        intents = []

    for intent in intents:
        if not isinstance(intent, dict):
            continue

        note_idx = intent.get("note_index")
        if not isinstance(note_idx, int) or not (0 <= note_idx < total_notes):
            continue

        raw_type = intent.get("directive_type")
        explanation = str(intent.get("explanation") or "")

        if raw_type not in VALID_DIRECTIVE_TYPES or raw_type == "no_op":
            result_map[note_idx] = _make_noop_pair(note_idx, explanation or "No operation requested.")
            continue

        d_type: DirectiveType = raw_type  # type: ignore

        # Validate hourly window
        hours = _compute_hours(intent.get("start_hour_24"), intent.get("end_hour_24"))
        if hours is None:
            result_map[note_idx] = _make_noop_pair(note_idx, explanation or "Invalid or missing hour range.")
            continue

        factor: Optional[float] = None
        min_kwh: Optional[float] = None
        max_grid: Optional[float] = None
        structured_adjustment: Optional[dict] = None

        # Validate type-specific constraints
        if d_type == "solar_reduction":
            p_kind = intent.get("percent_kind")
            p_val = intent.get("percent_value")
            try:
                p_val_float = float(p_val)
                if p_kind == "remaining":
                    calc_factor = p_val_float / 100.0
                elif p_kind == "reduction":
                    calc_factor = 1.0 - (p_val_float / 100.0)
                else:
                    raise ValueError("Invalid percent_kind")

                if not (0.0 <= calc_factor <= 1.0) or math.isnan(calc_factor):
                    raise ValueError("Factor out of range [0, 1]")

                factor = round(calc_factor, 4)
                structured_adjustment = {"hours": hours, "factor": factor}
            except (ValueError, TypeError):
                result_map[note_idx] = _make_noop_pair(note_idx, explanation or "Invalid solar reduction parameters.")
                continue

        elif d_type == "minimum_battery_reserve":
            num_val = intent.get("numeric_value")
            try:
                num_float = float(num_val)
                if math.isnan(num_float) or math.isinf(num_float):
                    raise ValueError("Non-finite numeric value")
                if not (0.0 <= num_float <= battery.capacity_kwh):
                    raise ValueError("Reserve exceeds capacity or is negative")

                min_kwh = float(num_float)
                structured_adjustment = {"hours": hours, "minimum_energy_kwh": min_kwh}
            except (ValueError, TypeError):
                result_map[note_idx] = _make_noop_pair(note_idx, explanation or "Invalid minimum battery reserve value.")
                continue

        elif d_type == "max_grid_window":
            num_val = intent.get("numeric_value")
            try:
                num_float = float(num_val)
                if math.isnan(num_float) or math.isinf(num_float) or num_float < 0.0:
                    raise ValueError("Invalid max grid intake value")

                max_grid = float(num_float)
                structured_adjustment = {"hours": hours, "max_grid_kwh": max_grid}
            except (ValueError, TypeError):
                result_map[note_idx] = _make_noop_pair(note_idx, explanation or "Invalid max grid window value.")
                continue

        elif d_type == "no_charge_window":
            structured_adjustment = {"hours": hours}

        elif d_type == "no_discharge_window":
            structured_adjustment = {"hours": hours}

        # Build valid Directive dataclass & interpretation output
        directive_obj = Directive(
            note_index=note_idx,
            directive_type=d_type,
            hours=hours,
            factor=factor,
            minimum_energy_kwh=min_kwh,
            max_grid_kwh=max_grid,
            explanation=explanation,
        )

        interpretation_obj = {
            "note_index": note_idx,
            "applies": True,
            "directive_type": d_type,
            "structured_adjustment": structured_adjustment,
            "explanation": explanation,
        }

        result_map[note_idx] = (directive_obj, interpretation_obj)

    # Collect sorted, guaranteed 0..N-1 output arrays
    final_directives: list[Directive] = []
    final_interpretations: list[dict] = []

    for i in range(total_notes):
        directive, interpretation = result_map[i]
        final_directives.append(directive)
        final_interpretations.append(interpretation)

    return final_directives, final_interpretations