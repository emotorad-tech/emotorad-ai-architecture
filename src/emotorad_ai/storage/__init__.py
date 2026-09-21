"""Object storage for media: keys (pure), the S3 client, and the presign→attach
registry. Split so the key rules can be tested without AWS and boto3 is imported
in exactly one file."""
