import os

import pytest

# Set before boto3/moto are imported anywhere so nothing can reach a real account.
os.environ.update({
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_CONFIG_FILE": os.devnull,
    "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_EC2_METADATA_DISABLED": "true",
    "MOTO_EC2_LOAD_DEFAULT_AMIS": "false",
})
os.environ.pop("AWS_PROFILE", None)


@pytest.fixture
def aws():
    """A fully mocked AWS; yields a boto3 session bound to fake credentials."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        yield boto3.Session(region_name="us-east-1")
