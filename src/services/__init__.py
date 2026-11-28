"""Service layer: business logic invoked by routes, workers, and CLIs.

Modules here own their callables outright — routes, workers, and tests
import (and monkeypatch) them here, never through wrapper re-exports
(Standing Lesson: patch targets must live where the callable lives).
"""
