"""Helper CLI.

  python -m app.tools gen-secret        -> SECRET_KEY value
  python -m app.tools gen-key [name]    -> API key entry for API_KEYS
  python -m app.tools hash-password     -> WEB_PASSWORD_HASH value (prompts)
  python -m app.tools test-connection   -> read the controller once using .env settings
  python -m app.tools version           -> print the app version
"""
import getpass
import json
import secrets
import sys


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd in ("version", "--version", "-V"):
        from . import __version__
        print(__version__)
    elif cmd == "gen-secret":
        print(secrets.token_hex(32))
    elif cmd == "gen-key":
        name = argv[2] if len(argv) > 2 else "client"
        print(f"{name}:{secrets.token_urlsafe(32)}")
    elif cmd == "hash-password":
        from werkzeug.security import generate_password_hash
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Repeat: "):
            sys.exit("passwords do not match")
        if len(pw) < 8:
            sys.exit("use at least 8 characters")
        # single quotes keep the $ signs literal in .env
        print(f"WEB_PASSWORD_HASH='{generate_password_hash(pw)}'")
    elif cmd == "test-connection":
        from .config import Config
        from .controller import make_controller
        cfg = Config()
        ctl = make_controller(cfg)
        print(json.dumps({"info": ctl.read_info(), "snapshot": ctl.snapshot()}, indent=2, default=str))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
