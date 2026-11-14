"""Defaults must agree across the model, project state and NodeSet2 config."""

from __future__ import annotations

from opcua_explorer.defaults import (
    DEFAULT_LATENCY_SAMPLES,
    DEFAULT_MAX_DEPENDENCIES,
    DEFAULT_MAX_NODES,
    DEFAULT_MONITOR_KEEP_ROWS,
    DEFAULT_NOTIFICATION_WAIT_SECONDS,
    DEFAULT_PUBLISHING_INTERVAL_MS,
    DEFAULT_SAMPLING_INTERVAL_MS,
)
from opcua_explorer.models import MonitorConfig, TestConfig, TraversalConfig
from opcua_explorer.opcua.nodeset2 import NodeSet2Config
from opcua_explorer.project import ExportState, MonitorState, TestsState


def test_traversal_and_export_defaults_agree() -> None:
    assert TraversalConfig().max_nodes == DEFAULT_MAX_NODES
    assert ExportState().max_nodes == DEFAULT_MAX_NODES


def test_monitor_defaults_agree() -> None:
    assert MonitorConfig().publishing_interval_ms == DEFAULT_PUBLISHING_INTERVAL_MS
    assert MonitorConfig().sampling_interval_ms == DEFAULT_SAMPLING_INTERVAL_MS
    assert MonitorState().publishing_interval_ms == DEFAULT_PUBLISHING_INTERVAL_MS
    assert MonitorState().sampling_interval_ms == DEFAULT_SAMPLING_INTERVAL_MS
    assert MonitorState().keep_rows == DEFAULT_MONITOR_KEEP_ROWS


def test_test_defaults_agree() -> None:
    config = TestConfig()
    assert config.publishing_interval_ms == DEFAULT_PUBLISHING_INTERVAL_MS
    assert config.sampling_interval_ms == DEFAULT_SAMPLING_INTERVAL_MS
    assert config.notification_wait_seconds == DEFAULT_NOTIFICATION_WAIT_SECONDS
    assert config.latency_samples == DEFAULT_LATENCY_SAMPLES
    state = TestsState()
    assert state.publishing_interval_ms == DEFAULT_PUBLISHING_INTERVAL_MS
    assert state.sampling_interval_ms == DEFAULT_SAMPLING_INTERVAL_MS
    assert state.notification_wait_seconds == DEFAULT_NOTIFICATION_WAIT_SECONDS
    assert state.latency_samples == DEFAULT_LATENCY_SAMPLES


def test_nodeset_dependency_default_agrees() -> None:
    assert NodeSet2Config().max_dependency_nodes == DEFAULT_MAX_DEPENDENCIES
    assert ExportState().max_dependency_nodes == DEFAULT_MAX_DEPENDENCIES
