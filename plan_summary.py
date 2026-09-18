# plan_summary.py
from contracts import Directive

def generate_plan_summary(directives: list[Directive], is_baseline: bool = False) -> str:
    """Generates a dynamic 1-2 sentence plan summary based on active directives."""
    if is_baseline:
        return "Grid-only baseline strategy applied due to plan validation constraints."

    # Filter out non-operational notes
    active_types = list(set(
        d.directive_type.replace("_", " ")
        for d in directives
        if d.directive_type != "no_op"
    ))

    if not active_types:
        return (
            "Optimizes hourly grid import costs by charging the battery during low-tariff "
            "hours and discharging during peak periods while maintaining end-of-day energy balance."
        )

    constraints_list = ", ".join(active_types)
    return (
        f"Optimizes energy schedule while respecting active constraints ({constraints_list}), "
        "shifting battery energy to reduce expensive grid imports and maintaining end-of-day energy balance."
    )