"""Unit tests for structured controller logging."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime as RealDatetime
from pathlib import Path
from unittest.mock import patch

from terraform_controller import logging_component


class LoggingTests(unittest.TestCase):
    """Verify file naming, append/overwrite rules, and structured content."""

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

    def _flush(self) -> None:
        """Flush all active controller handlers before a test reads the file."""
        for handler in logging.getLogger(
            logging_component.LOGGER_NAME
        ).handlers:
            handler.flush()

    def test_blank_filename_format_uses_controller_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = {
                "logging_enabled": True,
                "logging_level": "INFO",
                "logging_directory": temp,
                "logging_mode": "append",
                "logging_filename_format": "",
            }
            path = logging_component.configure_logging(config)
            self.assertEqual(Path(path).name, "Controller.log")

    def test_formatted_filename_uses_standard_strftime_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = {
                "logging_enabled": True,
                "logging_level": "INFO",
                "logging_directory": temp,
                "logging_mode": "append",
                "logging_filename_format": "Controller-%Y%m%d-%H%M.log",
            }

            class FixedDatetime:
                """Minimal datetime stand-in with a deterministic now()."""

                @classmethod
                def now(cls, tz=None):
                    return RealDatetime(2026, 9, 8, 16, 7, 42, tzinfo=tz)

            with patch.object(
                logging_component,
                "datetime",
                FixedDatetime,
            ):
                path = logging_component.configure_logging(config)

            self.assertEqual(
                Path(path).name,
                "Controller-20260908-1607.log",
            )

    def test_append_keeps_existing_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Controller.log"
            path.write_text("PREVIOUS\n", encoding="utf-8")

            config = {
                "logging_enabled": True,
                "logging_level": "INFO",
                "logging_directory": temp,
                "logging_mode": "append",
                "logging_filename_format": "",
            }
            logging_component.configure_logging(config)
            logging_component.log_event("test.append")
            self._flush()

            content = path.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("PREVIOUS\n"))
            self.assertIn("test.append", content)

    def test_overwrite_replaces_existing_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Controller.log"
            path.write_text("PREVIOUS\n", encoding="utf-8")

            config = {
                "logging_enabled": True,
                "logging_level": "INFO",
                "logging_directory": temp,
                "logging_mode": "overwrite",
                "logging_filename_format": "",
            }
            logging_component.configure_logging(config)
            logging_component.log_event("test.overwrite")
            self._flush()

            content = path.read_text(encoding="utf-8")
            self.assertNotIn("PREVIOUS", content)
            self.assertIn("test.overwrite", content)

    def test_log_contains_structured_event_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = {
                "logging_enabled": True,
                "logging_level": "INFO",
                "logging_directory": temp,
                "logging_mode": "append",
                "logging_filename_format": "",
            }
            path = logging_component.configure_logging(config)
            logging_component.log_event(
                "test.event",
                deployment="SC9",
                shard_count=3,
            )
            self._flush()

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
