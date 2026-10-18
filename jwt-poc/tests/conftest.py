import os

import pytest

RESULTS = os.environ.get("RESULTS_TSV", "/results/jwt-results.tsv")


@pytest.fixture
def check():
    """check(id, title, expected, actual, notes='') -> records a row in RESULTS and asserts actual == expected."""
    def _check(cid, title, expected, actual, notes=""):
        verdict = "PASS" if actual == expected else "FAIL"
        clean = lambda s: str(s).replace("\t", " ").replace("\r", "").replace("\n", " ")
        with open(RESULTS, "a") as f:
            f.write("\t".join(clean(x) for x in (cid, title, expected, actual, verdict, notes)) + "\n")
        assert actual == expected, f"{cid} {title}: expected {expected}, got {actual} ({notes})"
    return _check
