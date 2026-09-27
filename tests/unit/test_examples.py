"""The committed examples must be exactly what the generator produces.

`make examples` regenerates `examples/diagnostics.json` and `examples/report.md`
from the real report generator. This test regenerates the same bytes and fails
when they diverge, so a new or changed report section cannot silently leave the
examples stale.
"""

import importlib.util
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "generate_examples.py"
_EXAMPLES = _REPO_ROOT / "examples"


def _generator():
    spec = importlib.util.spec_from_file_location("generate_examples", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestExamplesAreCurrent:
    def test_report_matches_generator(self):
        committed = (_EXAMPLES / "report.md").read_text()
        assert _generator().build_report() == committed, (
            "examples/report.md is stale; run `make examples`"
        )

    def test_diagnostics_matches_generator(self):
        committed = (_EXAMPLES / "diagnostics.json").read_text()
        assert _generator().build_diagnostics() == committed, (
            "examples/diagnostics.json is stale; run `make examples`"
        )

    def test_example_exercises_key_sections(self):
        report = (_EXAMPLES / "report.md").read_text()
        for heading in ("## Executive summary", "## Snap services", "## Snap logs:"):
            assert heading in report, f"{heading} missing from the example report"
