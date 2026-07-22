import json
from pathlib import Path

from backend.app import app


output = Path(__file__).resolve().parents[1] / "openapi.json"
output.write_text(json.dumps(app.openapi_schema.to_schema(), indent=2))
print(output)
