# Deploying personal-mcp to AWS (Lambda + S3)

Result: an HTTPS Function URL you add as a custom connector on claude.ai; it then appears in the
Android app. Only the owner's Google account can sign in. Cost is effectively zero.

## One-time prep
1. **Google Web OAuth client** (Google Cloud console, same project, Credentials -> Create -> Web application).
   Authorized redirect URI is added after step 3 (`<FunctionUrl>/google/callback`). Note client id + secret.
2. **Google consent screen must be "In production"**, otherwise Gmail refresh tokens expire after 7 days.
3. **SSM parameters** (SecureString) under `/personal-mcp/`: `GOOGLE_WEB_CLIENT_ID`, `GOOGLE_WEB_CLIENT_SECRET`,
   `OWNER_EMAIL` (your Google address), `MS_CLIENT_ID`. Plus `google_client.json` content is NOT needed remotely:
   logins are made on the Mac and transferred.

## Deploy
```
deploy/build.sh
cd deploy && sam deploy --guided      # stack name personal-mcp; BudgetEmail=you@...; PublicUrl empty
```
Take the `FunctionUrl` output, add `<FunctionUrl>/google/callback` to the Google Web client's redirect URIs,
then redeploy with `PublicUrl=<FunctionUrl>` (no trailing slash).
If `sam deploy` complains about reserved concurrency (new accounts have a small quota), lower or remove
`ReservedConcurrentExecutions`.

## Transfer your existing logins (run on the Mac, for each account in accounts.json)
```
.venv/bin/python login.py --export hotmail | TOKEN_BACKEND=s3 TOKEN_BUCKET=<Bucket output> .venv/bin/python login.py --import hotmail
```
The blob is a refresh token: do not save it to a file or paste it anywhere.

## Connect
claude.ai (web) -> Settings -> Connectors -> Add custom connector -> `<FunctionUrl>/mcp`. Sign in with Google.
It then shows up in the Android app.

## Switch off
`aws lambda put-function-concurrency --function-name <fn> --reserved-concurrent-executions 0` (instant),
or delete the stack (the bucket holds tokens: empty it first).
