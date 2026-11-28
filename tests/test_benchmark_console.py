"""Deterministic checks for benchmark evidence aggregation."""

from scripts.benchmark_console import summarize


def test_summary_keeps_failures_out_of_latency_but_in_throughput():
    result = summarize([(float(i), "200") for i in range(1, 101)] + [(9999.0, "503")], 2.0)
    assert result["requests"] == 101
    assert result["successful"] == 100
    assert result["errors"] == 1
    assert result["status_counts"] == {"200": 100, "503": 1}
    assert result["p50_ms"] == 50.5
    assert result["p95_ms"] == 95
    assert result["p99_ms"] == 99
    assert result["max_ms"] == 100
    assert result["requests_per_second"] == 50.5
    assert result["successful_requests_per_second"] == 50
    assert result["within_provisional_p95_target"] is True


def test_all_failed_samples_have_no_success_latency():
    result = summarize([(1.0, "exception:RuntimeError"), (2.0, "500")], 1.0)
    assert result["errors"] == 2
    assert result["p95_ms"] is None
    assert result["p99_ms"] is None
    assert result["within_provisional_p95_target"] is False
