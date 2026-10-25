"""Console entry point."""

from __future__ import annotations

import logging
import sys

from .config import get_settings
from .server import mcp


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, settings.log_level, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
