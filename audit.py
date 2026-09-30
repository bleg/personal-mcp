"""Append-only audit log: what the MCP did, never message bodies or tokens.
Local: audit.log file. Remote (Lambda, read-only disk): stdout, which goes to CloudWatch."""
import datetime
import os
import pathlib

LOG_PATH = pathlib.Path(__file__).parent / "audit.log"


def log(account: str, service: str, operation: str, **fields: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    extra = " ".join(f'{k}="{v}"' for k, v in fields.items())
    line = f"{ts} account={account} service={service} operation={operation} {extra}\n"
    if os.environ.get("MCP_PUBLIC_URL"):
        print("AUDIT " + line, end="", flush=True)
        return
    with LOG_PATH.open("a") as f:
        f.write(line)
