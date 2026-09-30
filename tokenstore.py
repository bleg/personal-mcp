"""Credential blob storage. TOKEN_BACKEND=keychain (default, local) or s3 (AWS Lambda).

S3 backend: bucket TOKEN_BUCKET, one small object per (service, account), private bucket
with default encryption. boto3 and keyring are imported lazily so each deploy needs only one.
"""
import os


def _backend() -> str:
    return os.environ.get("TOKEN_BACKEND", "keychain")


def _s3():
    import boto3

    return boto3.client("s3"), os.environ["TOKEN_BUCKET"]


def _key(service: str, account: str) -> str:
    return f"tokens/{service}/{account}.json"


def get(service: str, account: str) -> str | None:
    if _backend() == "s3":
        client, bucket = _s3()
        try:
            return client.get_object(Bucket=bucket, Key=_key(service, account))["Body"].read().decode()
        except client.exceptions.NoSuchKey:
            return None
    import keyring

    return keyring.get_password(service, account)


def set(service: str, account: str, blob: str) -> None:
    if _backend() == "s3":
        client, bucket = _s3()
        client.put_object(Bucket=bucket, Key=_key(service, account), Body=blob.encode())
        return
    import keyring

    keyring.set_password(service, account, blob)


def delete(service: str, account: str) -> None:
    if _backend() == "s3":
        client, bucket = _s3()
        client.delete_object(Bucket=bucket, Key=_key(service, account))
        return
    import keyring

    try:
        keyring.delete_password(service, account)
    except keyring.errors.PasswordDeleteError:
        pass
