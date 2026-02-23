import os
import sys
import json
import pytest

# Ensure repo root is importable so `from workflow_lambda import handler` works
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from workflow_lambda import handler as workflow_handler_module


def test_handle_event_success(monkeypatch: pytest.MonkeyPatch) -> None:
    event = {"body": json.dumps({"required_field": "value", "x": 1})}

    class DummyS3:
        def put_object(self, Bucket, Key, Body):  # type: ignore[no-untyped-def]
            return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    # Monkeypatch the s3 client object in the handler module
    monkeypatch.setattr(workflow_handler_module, "s3", DummyS3())

    resp = workflow_handler_module.handle_event(event, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert "request_id" in body
    assert body["status"] == "accepted"

