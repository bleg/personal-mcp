"""Remote config: load SSM SecureString parameters under SSM_PREFIX into os.environ at startup."""
import os


def load_ssm() -> None:
    prefix = os.environ.get("SSM_PREFIX")
    if not prefix:
        return
    import boto3

    ssm = boto3.client("ssm")
    for page in ssm.get_paginator("get_parameters_by_path").paginate(
        Path=prefix, WithDecryption=True
    ):
        for p in page["Parameters"]:
            os.environ.setdefault(p["Name"].rsplit("/", 1)[-1], p["Value"])
