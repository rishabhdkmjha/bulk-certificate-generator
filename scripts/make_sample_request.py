"""Create a bulk request body for load/demo testing.

    python scripts/make_sample_request.py 500 > bulk_500.json
"""
import json
import sys

n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
recipients = [{"name": f"Participant Number {i}", "email": f"participant{i}@example.com"} for i in range(n)]
# sprinkle in a few bad rows so the failure reporting is visible
recipients[1] = {"name": "", "email": "bad-row@example.com"}
recipients[2] = {"name": "No Email Person"}
print(json.dumps({"event_name": "PyCon India 2026", "issuer": "Python Society",
                  "recipients": recipients}, indent=2))
