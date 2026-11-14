"""Typed errors used across the application.

Every error carries a human-readable message that is safe to show in the GUI
and to write into a log file. No secret (password, private key material) is
ever embedded in these messages.
"""

from __future__ import annotations


class OpcUaExplorerError(Exception):
    """Base class for all application errors."""


class NotConnectedError(OpcUaExplorerError):
    """Raised when an operation requires an active session but none exists."""


class ConnectionFailedError(OpcUaExplorerError):
    """Raised when a session could not be established."""


class EndpointNotAdvertisedError(OpcUaExplorerError):
    """Raised when the requested security policy/mode is not advertised.

    Deliberately fatal: the application never silently falls back to a weaker
    security configuration.
    """


class CertificateApprovalRequiredError(OpcUaExplorerError):
    """Raised when a secure connection needs an explicitly approved certificate."""


class CertificateRejectedError(OpcUaExplorerError):
    """Raised when the user (or the trust store policy) rejects a certificate."""


class InvalidNodeIdError(OpcUaExplorerError):
    """Raised when a NodeId string cannot be parsed."""


class ExportError(OpcUaExplorerError):
    """Raised when a snapshot could not be written."""


class OperationCancelledError(OpcUaExplorerError):
    """Raised when a user-cancelled operation is observed by the caller."""


class MonitorNotActiveError(OpcUaExplorerError):
    """Raised when a monitored-item operation needs a running subscription."""
