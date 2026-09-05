"""Fleet dashboard settings — lets the UI configure DASHBOARD_URL/INGEST_TOKEN/
NODE_NAME instead of hand-editing .env (queue_worker.py's dashboard_reporter
background thread is what actually pushes on a schedule; see that module and
doc_api/API_INTEGRATION.md for the API this connects to).
"""
from fastapi import APIRouter

import dashboard_reporter

from .models import DashboardConfigRequest, DashboardStatus

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/status", response_model=DashboardStatus)
def dashboard_status():
    return DashboardStatus(**dashboard_reporter.status())


@router.post("/config", response_model=DashboardStatus)
def save_config(body: DashboardConfigRequest):
    dashboard_reporter.save_settings(body.dashboard_url, body.ingest_token, body.node_name)
    try:
        dashboard_reporter.test_push()
    except Exception as e:
        return DashboardStatus(**dashboard_reporter.status(), error=str(e))
    return DashboardStatus(**dashboard_reporter.status())
