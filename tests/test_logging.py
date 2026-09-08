"""Unit tests for JSON Lines operational logging."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from terraform_controller import logging_component


class LoggingTests(unittest.TestCase):
    """Verify logs are structured and contain operational fields."""

    def setUp(self) -> None:
        # configure_logging is intentionally process-global in production.
        # Reset it between unit tests so each test gets a fresh temp directory.
        logging_component._CONFIGURED = False
        logging_component._LOG_PATH = None
        logger = logging.getLogger(logging_component.LOGGER_NAME)
        logger.handlers.clear()

    def tearDown(self) -> None:
        logger = logging.getLogger(logging_component.LOGGER_NAME)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        logging_component._CONFIGURED = False
        logging_component._LOG_PATH = None

    def test_jsonl_log_contains_event_and_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = {
                "logging_enabled": True,
                "logging_level": "INFO",
                "logging_directory": temp,
                "logging_mode": "per-run",
                "logging_filename_pattern": "test-{pid}.jsonl",
                "logging_retention_days": 30,
            }
            path = logging_component.configure_logging(config)
            logging_component.log_event(
                "test.event",
                deployment="SC9",
                shard_count=3,
            )

            # Flush the file handler before reading the log back.
            for handler in logging.getLogger(
                logging_component.LOGGER_NAME
            ).handlers:
                handler.flush()

            self.assertIsNotNone(path)
            lines = Path(path).read_text(encoding="utf-8").splitlines()
            payload = json.loads(lines[-1])
            self.assertEqual(payload["event"], "test.event")
            self.assertEqual(payload["deployment"], "SC9")
            self.assertEqual(payload["shard_count"], 3)

    def test_disabled_logging_returns_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = {
                "logging_enabled": False,
                "logging_directory": temp,
            }
            path = logging_component.configure_logging(config)
            self.assertIsNone(path)
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
