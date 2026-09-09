from __future__ import annotations

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any, Dict

from engine.api import HermesApi


app = FastAPI(title="Hermes Core HTTP Bridge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://localhost:3000", "http://127.0.0.1", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ApprovalIn(BaseModel):
    approval_id: str
    choice: str


class ClarifyIn(BaseModel):
    clarify_id: str
    response: str


class HermesBridge:
    def __init__(self, data_dir: str | None = None):
        self.api = HermesApi(data_dir=data_dir)

    def diagnostics(self) -> Dict[str, Any]:
        # Build a simple diagnostics snapshot compatible with WebUI expectations
        runs = list(self.api.service.run_store.list())
        active_runs = [r for r in runs if str(r.get("status") or "").upper() not in {"COMPLETED", "FAILED", "CANCELLED"}]
        last_finished = None
        finished = [r for r in runs if r.get("completed_at")]
        if finished:
            # pick latest completed_at
            try:
                last_finished = max((r.get("completed_at") for r in finished if r.get("completed_at")), default=None)
            except Exception:
                last_finished = finished[-1].get("completed_at")
        return {
            "streams": {"active_streams": 0, "total_subscribers": 0, "total_offline_buffered_events": 0},
            "runs": {"active_runs": len(active_runs), "last_run_finished_at": last_finished},
        }


bridge = HermesBridge()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/runtime/diagnostics")
def get_diagnostics():
    return bridge.diagnostics()


@app.get("/api/runtime/run/{run_id}")
def get_run(run_id: str):
    try:
        return bridge.api.get_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"error": "adapter_error", "detail": str(exc)})


@app.get("/api/runtime/run/{run_id}/artifacts")
def get_run_artifacts(run_id: str):
    try:
        return bridge.api.get_run_artifacts(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/runtime/run/{run_id}/cancel")
def post_cancel(run_id: str):
    try:
        return bridge.api.cancel_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/runtime/run/{run_id}/approval")
def post_approval(run_id: str, body: ApprovalIn):
    try:
        return bridge.api.respond_approval(run_id, body.choice)
    except KeyError:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/runtime/run/{run_id}/clarify")
def post_clarify(run_id: str, body: ClarifyIn):
    try:
        # Use generic handle_action for clarify
        return bridge.api.handle_action(run_id, "clarify", {"request": body.response})
    except KeyError:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    uvicorn.run("web_api:app", host="127.0.0.1", port=8001, log_level="info")
