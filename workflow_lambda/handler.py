import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    _handler.setFormatter(_formatter)
    logger.addHandler(_handler)

# Module-level S3 client to allow simple monkeypatching in tests.
s3 = boto3.client("s3")


class ValidationError(Exception):
    """Raised when the incoming payload fails validation."""


def _parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse the request body from an API Gateway style event.

    Supports both string JSON bodies and already-deserialized dictionaries.
    """
    body = event.get("body")

    if body is None:
        raise ValidationError("Request body is required.")

    if isinstance(body, dict):
        return body

    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValidationError("Request body must be valid JSON.") from exc

    raise ValidationError("Unsupported body format.")


def _get_s3_client() -> boto3.client:
    """
    Return a boto3 S3 client.

    Separated for easier mocking in tests; tests monkeypatch the module-level
    `s3` object, which this function returns.
    """
    return s3


def _build_metadata(request_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build processing metadata for the request.

    The metadata is designed to be CloudWatch-friendly and idempotent-safe.
    """
    return {
        "request_id": request_id,
        "received_at_epoch": time.time(),
        "source": "n8n-webhook",
        "required_field": payload.get("required_field"),
    }


def _persist_result_to_s3(
    bucket: Optional[str],
    payload: Dict[str, Any],
    metadata: Dict[str, Any],
    s3_client: Optional[Any] = None,
) -> None:
    """
    Persist the processed result to S3 if a bucket is configured.

    Idempotency:
        The S3 object key is derived from the stable request_id. If the same
        Lambda invocation is retried with the same request_id, this results
        in an overwrite of the same object, which is safe and idempotent.
    """
    if not bucket:
        logger.info(
            "S3_OUTPUT_BUCKET not configured; skipping S3 persistence.",
            extra={"event": "s3_persist_skipped", "request_id": metadata["request_id"]},
        )
        return

    if s3_client is None:
        s3_client = _get_s3_client()

    key = f"workflow-results/{metadata['request_id']}.json"
    body = json.dumps(
        {
            "payload": payload,
            "metadata": metadata,
        },
        separators=(",", ":"),
    )

    try:
        s3_client.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"))
        logger.info(
            "Persisted workflow result to S3.",
            extra={
                "event": "s3_persist_success",
                "request_id": metadata["request_id"],
                "bucket": bucket,
                "key": key,
            },
        )
    except (BotoCoreError, ClientError) as exc:
        # The function is retry-safe: failure to persist is logged but does not
        # change the external response code. Upstream systems may choose to
        # retry based on their own policies.
        logger.error(
            "Failed to persist workflow result to S3.",
            extra={
                "event": "s3_persist_failure",
                "request_id": metadata["request_id"],
                "bucket": bucket,
                "error": str(exc),
            },
        )


def _response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Build a standard API Gateway-compatible HTTP response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body),
    }


def workflow_handler(event: Dict[str, Any], context: Optional[Any]) -> Dict[str, Any]:
    """
    Lambda entrypoint for the workflow webhook.

    Responsibilities:
        * Accept webhook payload from API Gateway.
        * Validate that `required_field` is present.
        * Enrich payload with processing metadata.
        * Emit structured, CloudWatch-friendly logs.
        * Persist result to S3 when configured.
        * Return HTTP responses suitable for API Gateway.
        * Be safe for retries and idempotent where possible.
    """
    request_id = getattr(context, "aws_request_id", None) if context else None
    if not request_id:
        request_id = str(uuid.uuid4())

    try:
        payload = _parse_body(event)

        if "required_field" not in payload:
            raise ValidationError("Missing required_field in payload.")

        metadata = _build_metadata(request_id, payload)

        logger.info(
            "Received workflow payload.",
            extra={
                "event": "workflow_received",
                "request_id": request_id,
                "metadata": metadata,
            },
        )

        bucket = os.getenv("S3_OUTPUT_BUCKET")
        _persist_result_to_s3(bucket=bucket, payload=payload, metadata=metadata)

        logger.info(
            "Successfully processed workflow payload.",
            extra={
                "event": "workflow_processed",
                "request_id": request_id,
            },
        )

        return _response(
            200,
            {
                "status": "accepted",
                "request_id": request_id,
                "metadata": metadata,
            },
        )

    except ValidationError as exc:
        logger.warning(
            "Validation failed for incoming payload.",
            extra={
                "event": "validation_failed",
                "request_id": request_id,
                "error": str(exc),
            },
        )
        return _response(
            400,
            {
                "error": "validation_failed",
                "message": str(exc),
                "request_id": request_id,
            },
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception(
            "Unexpected error while processing workflow.",
            extra={
                "event": "workflow_error",
                "request_id": request_id,
                "error": str(exc),
            },
        )
        return _response(
            500,
            {
                "error": "internal_error",
                "message": "Unexpected error while processing workflow.",
                "request_id": request_id,
            },
        )


def handle_event(event: Dict[str, Any], context: Optional[Any]) -> Dict[str, Any]:
    """
    Thin alias used by Serverless and tests.

    Having a separate function name keeps the public handler surface explicit
    while allowing internal refactoring of `workflow_handler`.
    """
    return workflow_handler(event, context)


# Alias kept for backwards compatibility with earlier configuration.
handler = workflow_handler

