"""Application error hierarchy: control-flow errors must share the base class."""

from __future__ import annotations

from opcua_explorer.errors import OpcUaExplorerError
from opcua_explorer.opcua.method_support import MetadataUnavailable
from opcua_explorer.opcua.sequence_runner import SequenceAborted, SequenceBusyError


def test_sequence_and_metadata_errors_share_the_application_base() -> None:
    for error in (SequenceBusyError, SequenceAborted, MetadataUnavailable):
        assert issubclass(error, OpcUaExplorerError), error.__name__


def test_error_messages_stay_operator_safe() -> None:
    # Every application error carries a message; the CLI/status bar prints ``str(exc)``.
    for error in (SequenceBusyError("busy"), SequenceAborted("stopped"), MetadataUnavailable("unavailable")):
        assert str(error)
