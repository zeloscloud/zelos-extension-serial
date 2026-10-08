#!/usr/bin/env python3
"""Zelos Serial extension entry point.

Nothing runs at import: packaging and standalone action runs import this module. Importing
`app` imports `actions`, which registers the actions.
"""

import logging
import time

# Re-exported: the packaged action inventory reads the namespace off the entry module.
from zelos_extension_serial import ACTION_PREFIX as ACTION_PREFIX
from zelos_extension_serial import app

if __name__ == "__main__":
    # UTC with milliseconds, matching the SDK's own lines in the same extension.log.
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03dZ %(levelname)5s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    app.run()
