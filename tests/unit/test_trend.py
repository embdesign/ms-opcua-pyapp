"""Unit tests for the numeric trend extraction (pure, no Qt, no server).

The chart may only draw what the server returned, so these tests pin down what is
plotted, what is skipped and which axis the series uses.
"""

from __future__ import annotations

from opcua_explorer.models import ValueSnapshot
from opcua_explorer.trend import SPARK_CHARS, build_trend_series, is_bad_status, sparkline


def _snapshot(value: object, *, source: str | None = None, server: str | None = None, status: str = "Good"):
    return ValueSnapshot(
        value=value, status=status, source_timestamp=source, server_timestamp=server
    )


def test_plain_numbers_become_points_in_order() -> None:
    series = build_trend_series([_snapshot(1.5), _snapshot(2), _snapshot(0.25)])

    assert [point.value for point in series.points] == [1.5, 2.0, 0.25]
    assert [point.row for point in series.points] == [1, 2, 3]
    assert series.min_value == 0.25
    assert series.max_value == 2.0
    assert series.skipped == 0
    assert series.is_empty is False


def test_booleans_plot_as_zero_and_one() -> None:
    series = build_trend_series([_snapshot(True), _snapshot(False)])

    assert [point.value for point in series.points] == [1.0, 0.0]


def test_numeric_text_plots_but_other_values_are_skipped_and_counted() -> None:
    series = build_trend_series(
        [
            _snapshot("3.5"),
            _snapshot("not a number"),
            _snapshot([1.0, 2.0]),
            _snapshot(None),
            _snapshot({"Low": 1.0}),
            _snapshot("7"),
        ]
    )

    assert [point.value for point in series.points] == [3.5, 7.0]
    # Row numbers keep the table positions, so a gap in the plot is visible.
    assert [point.row for point in series.points] == [1, 6]
    assert series.skipped == 4


def test_timestamps_decide_the_axis_and_fall_back_to_row_order() -> None:
    timed = build_trend_series(
        [
            _snapshot(1.0, source="2026-11-21T10:00:00.000Z"),
            _snapshot(2.0, source="2026-11-21T10:00:10.000Z"),
        ]
    )
    assert timed.time_based is True
    assert timed.start is not None and timed.end is not None
    assert (timed.end - timed.start).total_seconds() == 10

    # One point without any timestamp: the x axis becomes the row number instead
    # of inventing a time for it.
    mixed = build_trend_series(
        [_snapshot(1.0, source="2026-11-21T10:00:00.000Z"), _snapshot(2.0)]
    )
    assert mixed.time_based is False
    assert len(mixed.points) == 2


def test_server_timestamp_is_the_fallback_for_a_source_timestamp() -> None:
    series = build_trend_series([_snapshot(1.0, server="2026-11-21T10:00:05.000Z")])

    assert series.points[0].timestamp is not None
    assert series.time_based is True


def test_a_bad_status_is_counted_and_keeps_the_value() -> None:
    series = build_trend_series(
        [_snapshot(1.0), _snapshot(2.0, status="BadNoData"), _snapshot(None, status="BadNoData")]
    )

    assert [point.value for point in series.points] == [1.0, 2.0]
    assert series.bad_status == 1
    assert series.skipped == 1
    assert is_bad_status("BadTimeout") is True
    assert is_bad_status("UncertainLastUsableValue") is True
    assert is_bad_status("Good") is False


def test_summary_text_states_what_is_not_plotted() -> None:
    empty = build_trend_series([])
    assert empty.is_empty is True
    assert empty.summary_text() == "No numeric values to plot."

    series = build_trend_series([_snapshot(1.0), _snapshot(2.0), _snapshot("x")])
    text = series.summary_text()
    assert "2 numeric value(s)" in text
    assert "min 1, max 2" in text
    assert "1 non-numeric value(s) not plotted" in text


def test_sparkline_draws_the_same_points_in_one_line() -> None:
    series = build_trend_series([_snapshot(0.0), _snapshot(5.0), _snapshot(10.0)])

    text = sparkline(series)

    assert len(text) == 3
    assert text[0] == SPARK_CHARS[0]
    assert text[-1] == SPARK_CHARS[-1]
    # A constant series is drawn at mid height, not as a false edge.
    flat = sparkline(build_trend_series([_snapshot(3.0), _snapshot(3.0)]))
    assert flat == SPARK_CHARS[len(SPARK_CHARS) // 2] * 2
    assert sparkline(build_trend_series([])) == ""
    assert sparkline(build_trend_series([_snapshot("nope")])) == ""


def test_sparkline_buckets_a_long_series_to_the_requested_width() -> None:
    series = build_trend_series([_snapshot(float(index)) for index in range(50)])

    assert len(sparkline(series, width=10)) == 10
    # Bucketing preserves the direction of the trend.
    text = sparkline(series, width=10)
    assert text[0] == SPARK_CHARS[0]
    assert text[-1] == SPARK_CHARS[-1]
