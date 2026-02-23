"""
tests/conftest.py

Pytest configuration and session-scoped fixtures.

boto3 and botocore are stubbed into sys.modules before any test file imports
the handler. This allows the test suite to run in environments where the AWS
SDK is not installed (e.g. a fresh CI runner before the pip install step).

When boto3 IS installed (dev machine, Lambda, CI after pip install), the stubs
are skipped automatically — so this file is safe in all environments.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock


def _make_boto3_stub() -> types.ModuleType:
    boto3_mod = types.ModuleType("boto3")
    boto3_mod.client = MagicMock(return_value=MagicMock())
    return boto3_mod


def _make_botocore_stubs():
    botocore_mod = types.ModuleType("botocore")
    exceptions_mod = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        def __init__(self, error_response: dict, operation_name: str) -> None:
            self.response = error_response
            self.operation_name = operation_name
            code = error_response.get("Error", {}).get("Code", "")
            msg = error_response.get("Error", {}).get("Message", "")
            super().__init__(f"An error occurred ({code}) when calling {operation_name}: {msg}")

    exceptions_mod.ClientError = ClientError
    botocore_mod.exceptions = exceptions_mod
    return botocore_mod, exceptions_mod


if "boto3" not in sys.modules:
    sys.modules["boto3"] = _make_boto3_stub()

if "botocore" not in sys.modules:
    botocore_stub, exceptions_stub = _make_botocore_stubs()
    sys.modules["botocore"] = botocore_stub
    sys.modules["botocore.exceptions"] = exceptions_stub

