"""An in-process DynamoDB (moto) with the production table shape.

Fake credentials and no AWS_PROFILE, so a test can never reach a real account
even on a machine that is signed in to one.
"""

import os
from unittest import mock

import boto3
from moto import mock_aws

TABLE = "emotorad-ai-conversations"
FAKE_ENV = {
    "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "ap-south-1",
}


class MotoTable:
    def setUp(self):
        super().setUp()
        self._env = mock.patch.dict(os.environ, FAKE_ENV)
        self._env.start()
        os.environ.pop("AWS_PROFILE", None)
        self._aws = mock_aws()
        self._aws.start()
        self.table = TABLE
        self.client = boto3.client("dynamodb", region_name="ap-south-1")
        self.client.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        )

    def tearDown(self):
        self._aws.stop()
        self._env.stop()
        super().tearDown()
