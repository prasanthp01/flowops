import json
from pathlib import Path

import pytest

from tests.conftest import drain, start

CASES = json.loads(Path("evaluations/cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_ground_truth_outcomes(system, case):
    client = system[2]
    run = start(client, changes=case["input"]).json()
    result = drain(system, run["run_id"])
    assert result["output"]["outcome"] == case["outcome"]
    if case["outcome"] != "SIMULATED_APPLIED":
        assert result["output"]["receipt"] is None
