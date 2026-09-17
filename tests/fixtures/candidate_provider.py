import json
import sys

request = json.loads(sys.stdin.read().rsplit("\n", 1)[-1])
sources = {entry["path"]: entry for entry in request["input"]["files"]}
path = request["allowed_output_paths"][0]
base = sources.get(path)
print(
    json.dumps(
        {
            "contract": "torq-candidate-output-v1",
            "plan_hash": request["plan_hash"],
            "input_hash": request["input_hash"],
            "operations": [
                {
                    "operation": "replace" if base else "create",
                    "path": path,
                    "base_hash": base["content_hash"] if base else None,
                    "content": "def add(left, right):\n    return left + right\n",
                }
            ],
        },
        separators=(",", ":"),
    )
)
