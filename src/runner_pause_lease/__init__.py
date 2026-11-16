"""Pause a self-hosted runner host for a bounded window, and put it back by
itself. See the README.

The command is the interface (cli.py). `pause` holds the commands and their
decisions, `lease` the issue-body record, and `labels` the runner label calls.
"""

import sys

# Checked here, before any other module of the package is imported, so an older
# interpreter gets this message rather than an import error from a newer name.
if sys.version_info < (3, 12):  # noqa: UP036 - the point is to run on older ones
    sys.exit("runner-pause-lease needs Python 3.12 or newer")
