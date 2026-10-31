"""Regression coverage for safe exception logging."""

import io
import logging

from src import logging_config


def test_log_exception_omits_exception_text_traceback_and_unreviewed_fields(monkeypatch):
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger("flowops")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagate = logger.propagate
    monkeypatch.setattr(logging_config, "_CONFIGURED", True)
    try:
        logger.handlers[:] = [handler]
        logger.setLevel(logging.ERROR)
        logger.propagate = False

        secret = "customer-token-should-not-appear"
        try:
            raise RuntimeError(secret)
        except RuntimeError as error:
            logging_config.log_exception(
                "ticket create failed",
                error,
                run_id="run-123",
                correlation_id="corr-456",
                arbitrary=secret,
            )

        rendered = output.getvalue()
        assert rendered.strip() == (
            "ticket create failed | run_id=run-123 | correlation_id=corr-456"
        )
        assert secret not in rendered
    finally:
        logger.handlers[:] = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate
