"""Secret redaction in the logging pipeline."""

from __future__ import annotations

import logging

import pytest

from opcua_explorer.logging_setup import (
    REDACTED,
    SecretRedactor,
    get_log_bus,
    register_secret,
    setup_logging,
)


@pytest.fixture(autouse=True)
def clean_bus():
    setup_logging("DEBUG")  # idempotent: installs the redaction filter once
    bus = get_log_bus()
    bus.clear()
    yield
    bus.clear()


def test_redactor_replaces_registered_value() -> None:
    redactor = SecretRedactor()
    redactor.register("hunter2-password")
    assert redactor.redact("password hunter2-password used") == f"password {REDACTED} used"


def test_redactor_handles_short_values_as_words() -> None:
    redactor = SecretRedactor()
    redactor.register("abc")
    assert redactor.redact("abc is short") == f"{REDACTED} is short"
    assert redactor.redact("abcdef") == "abcdef"  # only whole words


def test_redactor_masks_key_value_patterns() -> None:
    redactor = SecretRedactor()
    assert REDACTED in redactor.redact("password=topsecret")
    assert REDACTED in redactor.redact("private_key: /home/user/secret.pem")
    assert "topsecret" not in redactor.redact('{"password": "topsecret"}')


def test_redactor_masks_pem_private_key_blocks() -> None:
    redactor = SecretRedactor()
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    assert "MIIEow" not in redactor.redact(text)
    assert REDACTED in redactor.redact(text)


def test_log_bus_redacts_formatted_arguments() -> None:
    register_secret("s3cr3t-value")
    logging.getLogger("redaction-test").warning("connecting with password %s", "s3cr3t-value")

    lines = get_log_bus().snapshot()
    assert lines, "the log bus should have received the record"
    assert all("s3cr3t-value" not in line for line in lines)
    assert any(REDACTED in line for line in lines)


def test_log_bus_redacts_exception_tracebacks() -> None:
    register_secret("traceback-secret")
    try:
        raise RuntimeError("inner failure traceback-secret")
    except RuntimeError:
        logging.getLogger("redaction-test").exception("operation failed")

    text = "\n".join(get_log_bus().snapshot())
    assert "traceback-secret" not in text
    assert "operation failed" in text
