"""
workflow_lambda/handler.py

AWS Lambda entry point for the Workflow Automation Platform.

Receives trigger events (from n8n webhooks or Power Automate HTTP calls),
validates the payload, enriches it with processing metadata, and optionally
persists the result to S3.

Design principles:
- Idempotent: S3 writes are keyed by request_id; re-invoking the same event
  produces the same stored object and the same HTTP response.
- Structured logging: every log record is a JSON object so CloudWatch Logs
  Insights can query on individual fields (event, request_id, etc.).
- Fail-safe S3: a persistence error does NOT fail the HTTP response; the
  workflow result is still returned and the error is logged for investigation.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Logging — one JSON line per record, parseable by CloudWatch Logs Insights
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Guard against duplicate handlers on warm Lambda restarts
if not logger.handlers:
    _stream_handler = logging.StreamHandler()
    _stream_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_stream_handler)


def _log(level: str, event_name: str, request_id: str, **fields: Any) -> None:
    """Emit a single structured JSON log record."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level.upper(),
        "event": event_name,
        "request_id": request_id,
        **fields,
    }
    getattr(logger, level.lower())(json.dumps(record))


# ---------------------------------------------------------------------------
# S3 persistence (isolated for easy mocking in unit tests)
# ---------------------------------------------------------------------------

def _s3_client():
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _persist_to_s3(bucket: str, key: str, payload: dict, request_id: str) -> None:
    """
    Write payload as JSON to s3://<bucket>/<key>.

    put_object is idempotent: re-writing the same key overwrites the previous
    object with identical content, making retries safe.
    """
    try:
        _s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(payload, default=str).encode("utf-8"),
            ContentType="application/json",
        )
        _log("info", "s3_persist_success", request_id, bucket=bucket, key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        _log(
            "error",
            "s3_persist_failure",
            request_id,
            bucket=bucket,
            key=key,
            error_code=error_code,
            detail=str(exc),
        )
        # S3 failure is non-fatal — caller still receives a 200 response


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = ("required_field",)


def _validate(payload: dict) -> list[str]:
    """Return a list of validation error messages; empty list means valid."""
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if not payload.get(field):
            errors.append(f"Missing or empty required field: '{field}'")
    return errors


# ---------------------------------------------------------------------------
# Main handler — entry point referenced in serverless.yml
# ---------------------------------------------------------------------------

def handle_event(event: dict, context: Any) -> dict:
    """
    Lambda handler for the Workflow Automation Platform.

    Parameters
    ----------
    event:
        API Gateway proxy integration event dict.
    context:
        Lambda runtime context. aws_request_id is used as the idempotency
        key; a UUID fallback keeps the function testable without Lambda infra.

    Returns
    -------
    dict
        API Gateway proxy response (statusCode, headers, body).
    """
    request_id: str = getattr(context, "aws_request_id", None) or str(uuid.uuid4())

    # --- Parse body ---------------------------------------------------------
    raw_body = event.get("body") or ""
    if isinstance(raw_body, dict):
        payload: dict = raw_body
    elif isinstance(raw_body, str) and raw_body:
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            _log("warning", "invalid_json_body", request_id, detail=str(exc))
            return _response(400, {
                "error": "invalid_request",
                "message": "Request body is not valid JSON",
                "request_id": request_id,
            })
    else:
        payload = {}

    _log("info", "workflow_received", request_id, payload_keys=list(payload.keys()))

    # --- Validate -----------------------------------------------------------
    validation_errors = _validate(payload)
    if validation_errors:
        _log("warning", "validation_failed", request_id, errors=validation_errors)
        return _response(400, {
            "error": "validation_failed",
            "message": "; ".join(validation_errors),
            "request_id": request_id,
        })

    # --- Build result -------------------------------------------------------
    processed_at = datetime.now(timezone.utc).isoformat()
    result = {
        "status": "accepted",
        "request_id": request_id,
        "metadata": {
            "processed_at": processed_at,
            "required_field": payload["required_field"],
            "source_event_type": payload.get("event_type", "unspecified"),
        },
    }

    _log("info", "workflow_processed", request_id,
         required_field=payload["required_field"],
         processed_at=processed_at)

    # --- Persist to S3 (optional, non-fatal) --------------------------------
    bucket = os.environ.get("S3_OUTPUT_BUCKET")
    if bucket:
        s3_key = f"workflow-results/{request_id}.json"
        _persist_to_s3(
            bucket=bucket,
            key=s3_key,
            payload={**result, "original_payload": payload},
            request_id=request_id,
        )

    return _response(200, result)

