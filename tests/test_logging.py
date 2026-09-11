"""Unit tests for controller and operation logging."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from privateWorkerReplacement import logging_component
from privateWorkerReplacement.runtime_paths import controller_log_path, operations_log_path


class LoggingTests(unittest.TestCase):
    """Verify predictable daily paths, append behavior, and structured content."""

    def setUp(self) -> None:
        self._reset_logging()

    def tearDown(self) -> None:
        self._reset_logging()

    def _reset_logging(self) -> None:
        """Reset the process-global logger so each test gets its own temp config."""

        logger = logging.getLogger(logging_component.LOGGER_NAME)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        logging_component._CONFIGURED = False
        logging_component._LOG_PATH = None

    def _flush(self) -> None:
        for handler in logging.getLogger(logging_component.LOGGER_NAME).handlers:
            handler.flush()

    def _config(self, root: Path) -> dict[str, str]:
        config_path = root / "dev.config"
        config_path.write_text("[dummy]\n", encoding="utf-8")
        return {"config_path": str(config_path)}

    def test_controller_log_uses_fixed_daily_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            path = logging_component.configure_logging(config)

            expected_name = f"controller-{datetime.now(timezone.utc):%Y%m%d}.log"
            self.assertEqual(path.name, expected_name)
            self.assertEqual(path.parent, root / "logs" / "controller")

    def test_controller_log_appends_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            path = controller_log_path(Path(config["config_path"]))
            path.parent.mkdir(parents=True)
            path.write_text("PREVIOUS\n", encoding="utf-8")

            logging_component.configure_logging(config)
            logging_component.log_event("test.append")
            self._flush()

            content = path.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("PREVIOUS\n"))
            self.assertIn("test.append", content)

    def test_controller_log_contains_structured_event_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            path = logging_component.configure_logging(config)
            logging_component.log_event(
                "test.event",
                deployment="SC9",
                shard_count=3,
            )
            self._flush()

            lines = path.read_text(encoding="utf-8").splitlines()
            payload = json.loads(lines[-1])
            self.assertEqual(payload["event"], "test.event")
            self.assertEqual(payload["deployment"], "SC9")
            self.assertEqual(payload["shard_count"], 3)

    def test_process_diagnostic_goes_to_daily_operations_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            path = logging_component.append_process_diagnostic(
                config,
                ["terraform", "apply"],
                returncode=0,
                stdout="Apply complete!\n",
                label="Terraform apply",
            )

            self.assertEqual(
                path,
                operations_log_path(Path(config["config_path"])),
            )
            self.assertEqual(path.parent, root / "logs" / "operations")
            content = path.read_text(encoding="utf-8")
            self.assertIn("Terraform apply", content)
            self.assertIn("$ terraform apply", content)
            self.assertIn("Apply complete!", content)

    def test_operation_log_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            path = operations_log_path(Path(config["config_path"]))

            logging_component.append_process_diagnostic(
                config,
                ["git", "fetch"],
                returncode=0,
                stdout="first\n",
                label="Git fetch",
            )
            logging_component.append_process_diagnostic(
                config,
                ["terraform", "init"],
                returncode=0,
                stdout="second\n",
                label="Terraform initialization",
            )

            content = path.read_text(encoding="utf-8")
            self.assertIn("first", content)
            self.assertIn("second", content)
            self.assertLess(content.index("first"), content.index("second"))


if __name__ == "__main__":
    unittest.main()
