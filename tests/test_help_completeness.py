"""Regression tests that keep every user-visible command help screen useful.

These tests do not try to validate every sentence. They enforce the minimum
contract that every public and administrator subcommand has a meaningful
multi-word description plus at least one runnable example. Command-specific
tests in test_cli.py/test_admin_cli.py cover important safety/default wording.
"""

from __future__ import annotations

import unittest

from privateWorkerReplacement import admin_cli, cli


def _subcommand_parsers(parser):
    """Return argparse subcommand parsers from one top-level parser."""

    return next(
        action.choices
        for action in parser._actions
        if getattr(action, "choices", None)
    )


class HelpCompletenessTests(unittest.TestCase):
    """Prevent any supported command from shipping thin or missing help."""

    def test_every_public_command_has_description_and_examples(self) -> None:
        commands = _subcommand_parsers(cli.build_parser(cli.DEFAULT_CONFIG))

        for command, command_parser in commands.items():
            with self.subTest(command=command):
                help_text = command_parser.format_help()
                self.assertTrue(command_parser.description)
                self.assertGreater(len(command_parser.description.split()), 8)
                self.assertIn("Examples:", help_text)
                self.assertIn("privateWorkerReplacement.py", help_text)

    def test_every_admin_command_has_description_and_examples(self) -> None:
        commands = _subcommand_parsers(admin_cli.build_parser())

        for command, command_parser in commands.items():
            with self.subTest(command=command):
                help_text = command_parser.format_help()
                self.assertTrue(command_parser.description)
                self.assertGreater(len(command_parser.description.split()), 8)
                self.assertIn("Examples:", help_text)
                self.assertIn("privateWorkerReplacementAdmin.py", help_text)


if __name__ == "__main__":
    unittest.main()
