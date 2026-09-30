"""Remote entrypoint: serves the MCP over streamable HTTP with owner-only OAuth.

Runs under AWS Lambda Web Adapter (PORT 8080) or locally for testing. Requires the env
described in oauth_provider.py plus TOKEN_BACKEND=s3 and TOKEN_BUCKET on AWS.
"""
import os

import sys

import uvicorn

if not os.environ.get("MCP_PUBLIC_URL"):
    # Without it server.py builds an unauthenticated MCP; never serve that publicly.
    sys.exit("MCP_PUBLIC_URL is not set; refusing to start without OAuth")

import server  # noqa: E402

app = server.mcp.streamable_http_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
