"""
tests/test_handler.py

Unit tests for workflow_lambda.handler.
All AWS calls are mocked — no real credentials or SDK needed.
"""

from __future__ import annotations

import json
import os
import types
import unittest
from unittest.mock import MagicMock, patch

import pytest

from workflow_lambda.handler import handle_event, _validate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(request_id: str = "test-request-id-001") -> types.SimpleNamespace:
    return types.SimpleNamespace(aws_request_id=request_id)


def _api_event(body=None) -> dict:
    if body is None:
        serialized = ""
    elif isinstance(body, dict):
        serialized = json.dumps(body)
    else:
        serialized = body
    return {"body": serialized, "httpMethod": "POST", "path": "/webhook"}


def _parse_body(response: dict) -> dict:
    return json.loads(response["body"])


# ---------------------------------------------------------------------------
# Validation unit tests (pure, no I/O)
# ---------------------------------------------------------------------------

class TestValidate:
    def test_valid_payload(self):
        assert _validate({"required_field": "abc"}) == []

    def test_missing_required_field(self):
        errors = _validate({})
        assert len(errors) == 1
        assert "required_field" in errors[0]

    def test_empty_string_required_field(self):
        assert len(_validate({"required_field": ""})) == 1

    def test_none_required_field(self):
        assert len(_validate({"required_field": None})) == 1


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHandleEventHappyPath:

    @patch("workflow_lambda.handler._s3_client")
    def test_returns_200_for_valid_payload(self, _mock_s3):
        event = _api_event({"required_field": "customer-123", "event_type": "order_created"})
        response = handle_event(event, _make_context())
        assert response["statusCode"] == 200
        body = _parse_body(response)
        assert body["status"] == "accepted"
        assert body["request_id"] == "test-request-id-001"
        assert body["metadata"]["required_field"] == "customer-123"
        assert body["metadata"]["source_event_type"] == "order_created"

    @patch("workflow_lambda.handler._s3_client")
    def test_uses_context_request_id(self, _mock_s3):
        event = _api_event({"required_field": "x"})
        body = _parse_body(handle_event(event, _make_context("unique-req-999")))
        assert body["request_id"] == "unique-req-999"

    @patch("workflow_lambda.handler._s3_client")
    def test_unspecified_event_type_defaults(self, _mock_s3):
        event = _api_event({"required_field": "x"})
        body = _parse_body(handle_event(event, _make_context()))
        assert body["metadata"]["source_event_type"] == "unspecified"

    @patch("workflow_lambda.handler._s3_client")
    def test_content_type_header_set(self, _mock_s3):
        event = _api_event({"required_field": "x"})
        response = handle_event(event, _make_context())
        assert response["headers"]["Content-Type"] == "application/json"

    @patch("workflow_lambda.handler._s3_client")
    def test_direct_dict_body(self, _mock_s3):
        event = {"body": {"required_field": "direct-dict"}}
        body = _parse_body(handle_event(event, _make_context()))
        assert body["status"] == "accepted"


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------

class TestHandleEventValidationFailure:

    @patch("workflow_lambda.handler._s3_client")
    def test_missing_required_field_returns_400(self, _mock_s3):
        response = handle_event(_api_event({"event_type": "ping"}), _make_context())
        assert response["statusCode"] == 400
        body = _parse_body(response)
        assert body["error"] == "validation_failed"
        assert "required_field" in body["message"]

    @patch("workflow_lambda.handler._s3_client")
    def test_empty_body_returns_400(self, _mock_s3):
        assert handle_event(_api_event(None), _make_context())["statusCode"] == 400

    @patch("workflow_lambda.handler._s3_client")
    def test_400_includes_request_id(self, _mock_s3):
        body = _parse_body(handle_event(_api_event({}), _make_context("req-400")))
        assert body["request_id"] == "req-400"

    def test_invalid_json_returns_400(self):
        response = handle_event({"body": "not-json{{"}, _make_context())
        assert response["statusCode"] == 400
        assert _parse_body(response)["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# S3 persistence
# ---------------------------------------------------------------------------

class TestHandleEventS3Persistence:

    @patch.dict(os.environ, {"S3_OUTPUT_BUCKET": "test-bucket"})
    @patch("workflow_lambda.handler._s3_client")
    def test_s3_put_called_with_correct_key(self, mock_s3_factory):
        mock_s3 = MagicMock()
        mock_s3_factory.return_value = mock_s3
        handle_event(_api_event({"required_field": "s3-test"}), _make_context("req-s3-001"))
        mock_s3.put_object.assert_called_once()
        kwargs = mock_s3.put_object.call_args[1]
        assert kwargs["Bucket"] == "test-bucket"
        assert kwargs["Key"] == "workflow-results/req-s3-001.json"
        assert kwargs["ContentType"] == "application/json"

    @patch.dict(os.environ, {"S3_OUTPUT_BUCKET": "test-bucket"})
    @patch("workflow_lambda.handler._s3_client")
    def test_s3_payload_includes_original_payload(self, mock_s3_factory):
        mock_s3 = MagicMock()
        mock_s3_factory.return_value = mock_s3
        handle_event(_api_event({"required_field": "abc", "amount": 42}), _make_context("req-s3-002"))
        stored = json.loads(mock_s3.put_object.call_args[1]["Body"].decode())
        assert stored["original_payload"]["required_field"] == "abc"
        assert stored["original_payload"]["amount"] == 42

    @patch("workflow_lambda.handler._s3_client")
    def test_s3_not_called_when_bucket_unset(self, mock_s3_factory):
        os.environ.pop("S3_OUTPUT_BUCKET", None)
        handle_event(_api_event({"required_field": "no-s3"}), _make_context())
        mock_s3_factory.assert_not_called()

    @patch.dict(os.environ, {"S3_OUTPUT_BUCKET": "test-bucket"})
    @patch("workflow_lambda.handler._s3_client")
    def test_s3_error_does_not_affect_200_response(self, mock_s3_factory):
        from botocore.exceptions import ClientError
        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "PutObject"
        )
        mock_s3_factory.return_value = mock_s3
        response = handle_event(_api_event({"required_field": "resilience"}), _make_context())
        assert response["statusCode"] == 200
        assert _parse_body(response)["status"] == "accepted"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:

    @patch.dict(os.environ, {"S3_OUTPUT_BUCKET": "test-bucket"})
    @patch("workflow_lambda.handler._s3_client")
    def test_duplicate_invocation_is_safe(self, mock_s3_factory):
        mock_s3 = MagicMock()
        mock_s3_factory.return_value = mock_s3
        event = _api_event({"required_field": "idempotent"})
        ctx = _make_context("idempotent-req-id")
        r1 = handle_event(event, ctx)
        r2 = handle_event(event, ctx)
        assert r1["statusCode"] == r2["statusCode"] == 200
        assert mock_s3.put_object.call_count == 2
        keys = [c[1]["Key"] for c in mock_s3.put_object.call_args_list]
        assert keys[0] == keys[1]  # same key both times — last-write-wins

