"""hil-host: the reusable host side of a HIL test bench.

Subpackages (README and docs/configuration.md): bench (serial link,
programmer, board session, outcomes), protocol (the protocol interface and
its implementations), fake (fake firmware for tests without a board),
plugins (pytest plugins), core (TUI logic, no Textual import) and ui (Textual
screens). Every logger of hil-host is a child of LOGGER, so a run log or the
TUI monitor follows the whole bench through one logger.

Version: 1.0
Date: 19/11/2026
Author: Z. BEN-AKKA
History:
  1.0  19/11/2026  Initial version.
"""

__version__ = "0.1.0"  # Same as pyproject.toml

LOGGER = "hil"  # Root of hil.serial, hil.board, hil.programmer, hil.fixtures
