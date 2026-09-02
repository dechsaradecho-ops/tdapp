"""Goal Engine endpoint — assess feasibility of a monthly return target."""
from __future__ import annotations

from fastapi import APIRouter

from app.engine.goal_engine import GoalEngine
from app.models.schemas import GoalAssessment, GoalInput

router = APIRouter()
engine = GoalEngine()


@router.post("/assess", response_model=GoalAssessment)
async def assess_goal(goal: GoalInput) -> GoalAssessment:
    """Evaluate probability (High/Moderate/Low) + Best/Normal/Worst case scenarios.

    Emits a Risk Warning when the target exceeds what the profile can deliver.
    """
    return engine.assess(goal)
