"""Offline smoke test for the end-to-end benchmark harness."""

from src import bench


def test_percentiles_are_nearest_rank():
    assert bench._percentile([], 0.5) == 0.0
    assert bench._percentile([10.0], 0.95) == 10.0
    assert bench._percentile([10.0, 20.0, 30.0, 40.0], 0.5) == 20.0
    assert bench._percentile([10.0, 20.0, 30.0, 40.0], 0.95) == 40.0


def test_benchmark_drives_workflows_to_completion():
    report = bench.run_benchmark(requests=3)
    assert report.requests == 3
    assert report.completed == 3
    assert report.failed == 0
    assert report.steps_per_request == 4
    assert report.wall_seconds > 0
    assert report.requests_per_second > 0
    assert len(report.latencies_ms) == 3
    assert report.p50_ms > 0
    summary = report.summary()
    assert "latencies_ms" not in summary
    assert "throughput" in bench.format_report(report)
