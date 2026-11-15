"""Windows Task Scheduler entry point for the local Commenter service."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import socket

import uvicorn


if __name__ == "__main__":
    logs = Path(__file__).resolve().parent / "backups"
    logs.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[RotatingFileHandler(logs / "commenter-background.log", maxBytes=1_000_000,
                                      backupCount=2, encoding="utf-8")],
    )
    # HTTPX's INFO records include Graph URLs and their access-token query values.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", 8080))
        except OSError:
            logging.exception("Could not reserve the local Commenter port")
            raise SystemExit(1)
        server = uvicorn.Server(uvicorn.Config(
            "app:app", host="127.0.0.1", port=8080, log_config=None, access_log=False,
        ))
        server.run(sockets=[listener])
