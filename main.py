"""Zelos Serial extension entry point.

Nothing runs at import: packaging and standalone action runs import this module. Importing
`app` imports `actions`, which registers the actions.
"""

import logging
import time

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
