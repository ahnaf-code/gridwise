from dataclasses import dataclass
from typing import Literal, Optional

DirectiveType = Literal[
    "solar_reduction", "minimum_battery_reserve", "no_charge_window",
    "no_discharge_window", "max_grid_window", "no_op",
]

@dataclass
class Directive:
    note_index: int
    directive_type: DirectiveType
    hours: list[int]                      # [] for no_op
    factor: Optional[float] = None        # solar_reduction
    minimum_energy_kwh: Optional[float] = None
    max_grid_kwh: Optional[float] = None
    explanation: str = ""

@dataclass
class Battery:
    capacity_kwh: float
    initial_energy_kwh: float
    minimum_energy_kwh: float
    max_charge_kwh_per_hour: float
    max_discharge_kwh_per_hour: float

@dataclass
class Scenario:
    scenario_id: str
    operator_notes: list[str]
    demand: list[float]       # length 24
    solar: list[float]        # length 24
    tariff: list[float]       # length 24
    battery: Battery