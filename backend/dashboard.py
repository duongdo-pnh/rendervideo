"""Fleet dashboard settings — lets the UI configure INGEST_TOKEN/NODE_NAME instead
of hand-editing .env (queue_worker.py's dashboard_reporter background thread is
what actually pushes on a schedule; see that module and doc_api/API_INTEGRATION.md
for the API this connects to).

URL dashboard KHÔNG sửa được qua đây — cố định trong
dashboard_reporter._DEFAULT_DASHBOARD_URL vì mọi máy render đẩy về cùng một chỗ.
`status()` vẫn trả URL đang dùng để giao diện hiện cho người dùng thấy.
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
    dashboard_reporter.save_settings(body.ingest_token, body.node_name)
    try:
        dashboard_reporter.test_push()
    except Exception as e:
        return DashboardStatus(**dashboard_reporter.status(), error=str(e))
    return DashboardStatus(**dashboard_reporter.status())
