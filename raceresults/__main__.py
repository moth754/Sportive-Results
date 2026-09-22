"""Run the app: python -m raceresults [--port 8080] [--data DIR]"""

import argparse
import logging
import logging.handlers
import os
import socket
from pathlib import Path

from .app import create_app, default_data_dir


def _setup_logging(data_dir):
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(log_dir / "app.log", maxBytes=1_000_000, backupCount=3)
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)
    logging.getLogger("urllib3").setLevel(logging.WARNING)  # its debug lines would include the API token
    logging.getLogger("paramiko").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(prog="raceresults", description="Race Results web app")
    parser.add_argument("--host", default=os.environ.get("RACERESULTS_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RACERESULTS_PORT", "8080")))
    parser.add_argument("--data", default=str(default_data_dir()), help="folder for the database, logos and logs")
    args = parser.parse_args()

    _setup_logging(args.data)
    app = create_app(args.data)
    logging.getLogger("raceresults").info("Open http://%s.local:%s in a browser", socket.gethostname(), args.port)

    from waitress import serve

    serve(app, host=args.host, port=args.port, threads=8, ident="RaceResults")


if __name__ == "__main__":
    main()
