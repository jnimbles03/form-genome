# run.py – entry point for local development

import os
from app import create_app


def main() -> None:
    # The .env file is loaded inside app/__init__.py, but you can set extra vars here if needed.
    # Example: os.environ["FLASK_DEBUG"] = "1"
    app = create_app()
    # Cloud Run uses port 8080; binding to 0.0.0.0 makes it reachable from localhost and LAN.
    app.run(host="0.0.0.0", port=8080, debug=True)


if __name__ == "__main__":
    main()
