"""Runtime fixture function."""

import json
import os


def handler(event, context):
    return {"statusCode": 200, "body": json.dumps({"message": f"{os.environ.get('GREETING', 'hello')}, rlp"})}
