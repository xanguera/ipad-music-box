"""Start the server: `uv run python -m musicbox`.

Settings come from the environment:
  MUSICBOX_DATA  folder for the database, settings and downloaded songs (default: ./data)
  MUSICBOX_PORT  port to listen on (default: 8080)
  MUSICBOX_HOST  address to listen on (default: 0.0.0.0, so the iPad on the home network can reach it)
"""

import logging
import os
import socket

from waitress import serve

from .server import create_app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)
    host = os.environ.get("MUSICBOX_HOST", "0.0.0.0")
    port = int(os.environ.get("MUSICBOX_PORT", "8080"))
    app = create_app()
    name = socket.gethostname().removesuffix(".local")
    logging.info("music box on http://%s.local:%d/ (admin: /admin/)", name, port)
    # Many threads: video files are served in long-running byte-range requests.
    serve(app, host=host, port=port, threads=16, channel_timeout=300)


if __name__ == "__main__":
    main()
