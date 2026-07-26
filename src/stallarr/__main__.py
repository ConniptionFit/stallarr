"""Entry point: `python -m stallarr`."""

from __future__ import annotations

import uvicorn

from . import config as config_module
from .logging_setup import setup


def main() -> None:
    cfg = config_module.load()
    setup(cfg.log_level)
    uvicorn.run(
        "stallarr.main:app",
        host=cfg.web.host,
        port=cfg.web.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
