"""Job lifecycle endpoints.

POST   /v1/curate/jobs              — submit curation request
GET    /v1/curate/jobs/:jobId       — get job status
GET    /v1/curate/jobs              — list jobs (filtered, paginated)
DELETE /v1/curate/jobs/:jobId       — cancel/delete job
POST   /v1/curate/jobs/:jobId/retry — re-run pipeline
GET    /v1/curate/jobs/:jobId/package — get completed output
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from cce.api.auth import auth_dependency
from cce.api.middleware import get_request_id
from cce.api.schemas import (
    APIEnvelope,
    JobCreateRequest,
    JobListResponse,
    JobResponse,
    envelope,
    error_envelope,
    job_to_response,
)
from cce.engine import run_pipeline_task
from cce.models.job import Job, JobStatus
from cce.models.request import CurationConstraints, CurationRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/curate/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=202, response_model=APIEnvelope[JobResponse])
async def create_job(
    body: JobCreateRequest,
    request: Request,
    _auth: str | None = Depends(auth_dependency),
) -> JSONResponse:
    """Submit a curation request. Returns 202 with job ID."""
    state = request.app.state

    # Resolve policy
    policy = state.policies.get(body.policy_id)
    if policy is None:
        return JSONResponse(
            status_code=404,
            content=error_envelope(
                code="policy_not_found",
                message=f"Policy not found: {body.policy_id}",
                request_id=get_request_id(),
            ).model_dump(mode="json"),
        )

    # Convert to CurationRequest
    constraints = None
    if body.jurisdiction:
        constraints = CurationConstraints(jurisdiction=body.jurisdiction)

    curation_request = CurationRequest(
        topic=body.topic,
        subtopics=body.subtopics,
        paths=body.paths,
        audience=body.audience,
        constraints=constraints,
        policy_id=body.policy_id,
        taxonomy_id=body.taxonomy_id,
        path_config_id=body.path_config_id,
        risk_profile=body.risk_profile,
    )

    # Create and persist job
    job = Job(
        id=f"job_{uuid.uuid4().hex[:12]}",
        request=curation_request,
    )
    await state.job_store.create_job(job)

    # Launch background pipeline task
    task = asyncio.create_task(
        run_pipeline_task(
            job.id,
            curation_request,
            policy,
            pipeline=state.pipeline,
            job_store=state.job_store,
            semaphore=state.semaphore,
            running_tasks=state.running_tasks,
        )
    )
    state.running_tasks[job.id] = task

    return JSONResponse(
        status_code=202,
        content=envelope(data=job_to_response(job)).model_dump(mode="json"),
    )


@router.get("/{job_id}", response_model=APIEnvelope[JobResponse])
async def get_job(job_id: str, request: Request) -> JSONResponse:
    """Get job status."""
    job = await request.app.state.job_store.get_job(job_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content=error_envelope(
                code="job_not_found",
                message=f"Job not found: {job_id}",
                request_id=get_request_id(),
            ).model_dump(mode="json"),
        )
    return JSONResponse(
        content=envelope(data=job_to_response(job)).model_dump(mode="json")
    )


@router.get("", response_model=APIEnvelope[JobListResponse])
async def list_jobs(
    request: Request,
    status: str | None = Query(default=None),
    topic: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """List jobs with optional filters and pagination."""
    store = request.app.state.job_store

    # Convert string status to enum
    status_enum = None
    if status is not None:
        try:
            status_enum = JobStatus(status)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content=error_envelope(
                    code="invalid_status",
                    message=f"Invalid status: {status}",
                    request_id=get_request_id(),
                ).model_dump(mode="json"),
            )

    jobs = await store.list_jobs(
        status=status_enum, topic=topic, limit=limit, offset=offset
    )
    total = await store.count_jobs(status=status_enum, topic=topic)

    return JSONResponse(
        content=envelope(
            data=JobListResponse(
                jobs=[job_to_response(j) for j in jobs],
                total=total,
                limit=limit,
                offset=offset,
            )
        ).model_dump(mode="json")
    )


@router.delete("/{job_id}", response_model=APIEnvelope[dict])
async def delete_job(
    job_id: str,
    request: Request,
    _auth: str | None = Depends(auth_dependency),
) -> JSONResponse:
    """Cancel and delete a job."""
    state = request.app.state

    # Cancel running task if active
    task = state.running_tasks.pop(job_id, None)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    deleted = await state.job_store.delete_job(job_id)
    if not deleted:
        return JSONResponse(
            status_code=404,
            content=error_envelope(
                code="job_not_found",
                message=f"Job not found: {job_id}",
                request_id=get_request_id(),
            ).model_dump(mode="json"),
        )

    return JSONResponse(
        content=envelope(data={"deleted": job_id}).model_dump(mode="json")
    )


@router.post(
    "/{job_id}/retry", status_code=202, response_model=APIEnvelope[JobResponse]
)
async def retry_job(
    job_id: str,
    request: Request,
    _auth: str | None = Depends(auth_dependency),
) -> JSONResponse:
    """Re-run a completed or failed job."""
    state = request.app.state

    job = await state.job_store.get_job(job_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content=error_envelope(
                code="job_not_found",
                message=f"Job not found: {job_id}",
                request_id=get_request_id(),
            ).model_dump(mode="json"),
        )

    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        return JSONResponse(
            status_code=409,
            content=error_envelope(
                code="already_running",
                message="Job is already queued or running",
                request_id=get_request_id(),
            ).model_dump(mode="json"),
        )

    # Reset job state
    job.status = JobStatus.QUEUED
    job.error = None
    job.stage = None
    job.completed_at = None
    await state.job_store.update_job(job)

    # Resolve policy and re-launch
    policy = state.policies.get(job.request.policy_id)
    if policy is None:
        return JSONResponse(
            status_code=404,
            content=error_envelope(
                code="policy_not_found",
                message=f"Policy not found: {job.request.policy_id}",
                request_id=get_request_id(),
            ).model_dump(mode="json"),
        )

    task = asyncio.create_task(
        run_pipeline_task(
            job.id,
            job.request,
            policy,
            pipeline=state.pipeline,
            job_store=state.job_store,
            semaphore=state.semaphore,
            running_tasks=state.running_tasks,
        )
    )
    state.running_tasks[job.id] = task

    return JSONResponse(
        status_code=202,
        content=envelope(data=job_to_response(job)).model_dump(mode="json"),
    )


@router.get("/{job_id}/package", response_model=APIEnvelope[dict])
async def get_package(job_id: str, request: Request) -> JSONResponse:
    """Get the completed pipeline output for a job."""
    package = await request.app.state.job_store.get_package(job_id)
    if package is None:
        return JSONResponse(
            status_code=404,
            content=error_envelope(
                code="package_not_found",
                message=f"Package not found for job: {job_id}",
                request_id=get_request_id(),
            ).model_dump(mode="json"),
        )
    return JSONResponse(content=envelope(data=package).model_dump(mode="json"))
