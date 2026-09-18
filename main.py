import logging
from typing import List
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from contracts import Battery, Scenario
from guardrails import to_directives
from llm import LLMUnavailable, extract_intents
import solver

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gridwise")

app = FastAPI(title="GridWise Energy Optimization API")


class BatteryModel(BaseModel):
    capacity_kwh: float
    initial_energy_kwh: float
    minimum_energy_kwh: float
    max_charge_kwh_per_hour: float
    max_discharge_kwh_per_hour: float

    def to_dataclass(self) -> Battery:
        return Battery(
            capacity_kwh=self.capacity_kwh,
            initial_energy_kwh=self.initial_energy_kwh,
            minimum_energy_kwh=self.minimum_energy_kwh,
            max_charge_kwh_per_hour=self.max_charge_kwh_per_hour,
            max_discharge_kwh_per_hour=self.max_discharge_kwh_per_hour,
        )


class ScenarioModel(BaseModel):
    scenario_id: str
    operator_notes: List[str]
    demand: List[float] = Field(..., min_length=24, max_length=24)
    solar: List[float] = Field(..., min_length=24, max_length=24)
    tariff: List[float] = Field(..., min_length=24, max_length=24)
    battery: BatteryModel

    def to_dataclass(self) -> Scenario:
        return Scenario(
            scenario_id=self.scenario_id,
            operator_notes=self.operator_notes,
            demand=self.demand,
            solar=self.solar,
            tariff=self.tariff,
            battery=self.battery.to_dataclass(),
        )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": "Invalid request payload or missing required fields."},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": "Malformed request or invalid JSON payload."},
    )


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/optimize-energy")
async def optimize_energy(payload: ScenarioModel):
    try:
        scenario = payload.to_dataclass()

        # 1. Extract intents from LLM service (falls back to empty list on LLMUnavailable)
        try:
            intents = await extract_intents(scenario.operator_notes)
        except LLMUnavailable:
            logger.warning("LLM service unavailable; falling back to empty intent list.")
            intents = []

        # 2. Map intents to validated directives and structured interpretations
        directives, interpretations = to_directives(
            scenario.operator_notes, intents, scenario.battery
        )

        # 3. Solve optimization problem
        plan = solver.solve(scenario, directives)

        # 4. Validate plan against physical & operational rules
        violations = solver.validate_plan(scenario, directives, plan)
        if violations:
            logger.warning(
                f"Plan validation failed with violations: {violations}. "
                "Falling back to baseline grid-only plan."
            )
            baseline_sol = solver.grid_only_baseline(scenario, directives)
            plan = solver._assemble(scenario, baseline_sol)
            summary = "Grid-only baseline strategy applied due to plan validation constraints."
        else:
            summary = "Energy schedule optimized using linear programming to minimize cost while honoring all directives."

        # 5. Return schema-compliant output
        return {
            "scenario_id": scenario.scenario_id,
            "directive_interpretation": interpretations,
            "hourly_plan": plan["hourly_plan"],
            "total_grid_kwh": plan["total_grid_kwh"],
            "total_cost_bdt": plan["total_cost_bdt"],
            "peak_grid_kwh": plan["peak_grid_kwh"],
            "plan_summary": summary,
        }

    except Exception as e:
        logger.error(f"Unexpected processing error: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "An unexpected error occurred during request processing."},
        )