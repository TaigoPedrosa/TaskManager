"""Hermetic terminal for the suite.

`rich` reads the colour variables when its console is first built, so they are settled here, before
anything imports it: a caller's `FORCE_COLOR` would otherwise split `Imported Plan 1` with escape
codes and turn every plain-text assertion on rich output red.
"""

import os

os.environ.pop("FORCE_COLOR", None)
os.environ["NO_COLOR"] = "1"
os.environ["COLUMNS"] = "200"
