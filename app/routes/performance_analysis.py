"""
Analysis screen.

    GET  /admin/analysis/options      -> hackathons, evaluators, and top-N choices
    POST /admin/analysis/hackathons   -> campus, batch, NIAT, and project-quality numbers
    POST /admin/analysis/evaluators   -> day-wise assigned vs completed, and review time
"""

from fastapi import APIRouter, Depends

from app.dependencies import get_firebase
from app.middleware.auth_middleware import get_admin_user
from app.models.performance_analysis_model import (
    AnalysisOptionsResponse,
    EvaluatorAnalysisRequest,
    EvaluatorAnalysisResponse,
    HackathonAnalysisRequest,
    HackathonAnalysisResponse,
)
from app.models.user_model import CurrentUser
from app.services.firebase import FirebaseService
from app.services.performance_analysis_service import PerformanceAnalysisService
from app.utils.async_io import run_sync


router = APIRouter(prefix="/admin/analysis", tags=["analysis"])


def get_performance_analysis_service(
    firebase: FirebaseService = Depends(get_firebase),
) -> PerformanceAnalysisService:
    return PerformanceAnalysisService(firebase=firebase)


@router.get("/options", response_model=AnalysisOptionsResponse)
async def analysis_options(
    admin: CurrentUser = Depends(get_admin_user),
    service: PerformanceAnalysisService = Depends(get_performance_analysis_service),
) -> AnalysisOptionsResponse:
    """Hackathons and evaluators the Analysis filters can offer."""
    _ = admin
    payload = await run_sync(service.options)
    return AnalysisOptionsResponse(**payload)


@router.post("/hackathons", response_model=HackathonAnalysisResponse)
async def hackathon_analysis(
    request: HackathonAnalysisRequest,
    admin: CurrentUser = Depends(get_admin_user),
    service: PerformanceAnalysisService = Depends(get_performance_analysis_service),
) -> HackathonAnalysisResponse:
    """
    Compare one or more hackathons.

    ``top_n`` is 3, 5, or 10 on the screen. A score of 82 is a 8.2 project rating.
    """
    _ = admin
    payload = await run_sync(service.hackathon_analysis, request.hackathon_ids, request.top_n)
    return HackathonAnalysisResponse(**payload)


@router.post("/evaluators", response_model=EvaluatorAnalysisResponse)
async def evaluator_analysis(
    request: EvaluatorAnalysisRequest,
    admin: CurrentUser = Depends(get_admin_user),
    service: PerformanceAnalysisService = Depends(get_performance_analysis_service),
) -> EvaluatorAnalysisResponse:
    """
    Assigned versus completed work by IST day, and hours from the latest
    assignment to the evaluator's review submit.
    """
    _ = admin
    payload = await run_sync(
        service.evaluator_analysis,
        request.hackathon_ids,
        request.evaluator_ids,
        request.from_date,
        request.to_date,
    )
    return EvaluatorAnalysisResponse(**payload)
