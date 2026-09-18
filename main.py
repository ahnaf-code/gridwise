import os
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI(title="GridWise API Stub")

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.post("/optimize-energy")
async def optimize_energy(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    scenario_id = body.get("scenario_id", "default_scenario") if isinstance(body, dict) else "default_scenario"

    hourly_plan = [
        {
            "hour": hour,
            "grid_kwh": 0.0,
            "solar_used_kwh": 0.0,
            "battery_action": "idle",
            "battery_kwh": 0.0,
            "battery_energy_after_kwh": 0.0
        }
        for hour in range(24)
    ]

    return {
        "scenario_id": scenario_id,
        "directive_interpretation": [],
        "hourly_plan": hourly_plan,
        "total_grid_kwh": 0.0,
        "total_cost_bdt": 0.0,
        "peak_grid_kwh": 0.0,
        "plan_summary": "Baseline stub plan initialized."
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)