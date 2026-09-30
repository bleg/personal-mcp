"""Account login and credential transfer (account names live in accounts.json).

  python login.py <account>            interactive browser login (on the Mac)
  python login.py --export <account>   print that account's stored credential blob (keep it secret)
  python login.py --import <account>   read a blob from stdin into the current TOKEN_BACKEND

To push a Mac login to AWS:
  .venv/bin/python login.py --export hotmail | TOKEN_BACKEND=s3 TOKEN_BUCKET=<bucket> .venv/bin/python login.py --import hotmail
"""
import sys

import server
import tokenstore
from providers import google_auth, outlook

args = sys.argv[1:]
mode = args.pop(0) if args and args[0] in ("--export", "--import") else None
account = args[0] if args else "hotmail"
provider = server.ACCOUNTS[account]
service = outlook.KEYRING_SERVICE if provider.name == "outlook" else google_auth.KEYRING_SERVICE

if mode == "--export":
    print(tokenstore.get(service, account) or sys.exit(f"No stored login for '{account}'"))
elif mode == "--import":
    blob = sys.stdin.read().strip()
    if not blob:
        sys.exit("No credential blob on stdin")
    tokenstore.set(service, account, blob)
    print(f"Imported '{account}' into backend", tokenstore._backend())
else:
    print("Logged in as:", provider.login(account))
