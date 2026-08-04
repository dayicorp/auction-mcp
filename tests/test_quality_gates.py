"""Static contracts for the coverage and mutation gate definitions."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mutation_gate = _load("mutation_gate", ROOT / "scripts" / "mutation_gate.py")
coverage_gate = _load("coverage_gate", ROOT / "scripts" / "coverage_gate.py")


def test_coverage_contract_freezes_real_baseline_and_critical_100_percent():
    contract = json.loads(
        (ROOT / "coverage_contract.json").read_text(encoding="utf-8")
    )
    assert contract["baseline"]["commit"] == (
        "ec4e050031a0c038aebf26b06d4643592fa01592"
    )
    baseline = contract["baseline"]
    assert baseline["measured_statement_coverage_percent"] == 75.59726962457339
    assert baseline["measured_branch_coverage_percent"] == 66.25
    assert baseline["measured_combined_coverage_percent"] == 72.88135593220339
    assert contract["minimum_statement_coverage_percent"] >= 75.5
    assert contract["minimum_branch_coverage_percent"] >= 66.2
    assert contract["minimum_combined_coverage_percent"] >= 72.8
    critical = contract["critical_files"]["safety_core.py"]
    assert critical["minimum_branch_coverage_percent"] == 100.0
    assert critical["minimum_statement_coverage_percent"] == 100.0
    evidence_critical = contract["critical_files"]["evidence_safety.py"]
    assert evidence_critical["minimum_branch_coverage_percent"] == 100.0
    assert evidence_critical["minimum_statement_coverage_percent"] == 100.0


def test_mutation_manifest_covers_every_required_operator_without_ambiguity():
    required_categories = {
        "boundary_deletion",
        "condition_inversion",
        "default_change",
        "error_code_replacement",
        "guard_bypass",
        "schema_drift",
    }
    assert len(mutation_gate.MUTANTS) >= 12
    assert {mutant.category for mutant in mutation_gate.MUTANTS} == required_categories
    assert len({mutant.name for mutant in mutation_gate.MUTANTS}) == len(
        mutation_gate.MUTANTS
    )
    for mutant in mutation_gate.MUTANTS:
        source = (ROOT / mutant.path).read_text(encoding="utf-8")
        assert source.count(mutant.original) == 1, mutant.name
        assert mutant.tests


def test_coverage_diagnostic_keeps_global_and_critical_percentages_separate():
    contract = coverage_gate.load_contract()
    report = {
        "totals": {
            "num_statements": 100,
            "covered_lines": 80,
            "num_branches": 20,
            "covered_branches": 15,
            "percent_covered": 79.167,
        },
        "files": {
            "evidence_safety.py": {
                "summary": {
                    "num_statements": 20,
                    "covered_lines": 20,
                    "num_branches": 8,
                    "covered_branches": 8,
                    "missing_lines": 0,
                    "missing_branches": 0,
                }
            },
            "safety_core.py": {
                "summary": {
                    "num_statements": 10,
                    "covered_lines": 10,
                    "num_branches": 4,
                    "covered_branches": 4,
                    "missing_lines": 0,
                    "missing_branches": 0,
                }
            }
        },
    }
    diagnostic = coverage_gate.verify_report(report, contract)
    assert diagnostic["statement_percent"] == 80.0
    assert diagnostic["branch_percent"] == 75.0
    assert diagnostic["critical"]["safety_core.py"] == {
        "statement_percent": 100.0,
        "branch_percent": 100.0,
    }
    assert diagnostic["critical"]["evidence_safety.py"] == {
        "statement_percent": 100.0,
        "branch_percent": 100.0,
    }
