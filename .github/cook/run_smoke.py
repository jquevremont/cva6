#!/usr/bin/env python3
# Copyright 2026 OpenHW Foundation
# SPDX-License-Identifier: Apache-2.0
"""Run the committed two-Hello smoke through Cook and validate its evidence."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from flows.utils.logged_process import run_logged_process

TARGET = "cv32a65x_axi"
TESTLIST = "verif/tests/testlist_verilator_testharness_smoke.yaml"
TESTS = ("hello-original_0", "hello-uart_0")
GREETING = "0: Hello World !"
SIMULATION = Path("build") / TARGET / "simulation"
RUNS = SIMULATION / "sim_rtl_verilator_testharness"
REPORT = (
    SIMULATION / "testharness_verilator_testlist_verilator_testharness_smoke_report.yml"
)
SUMMARY = REPORT.with_name(REPORT.name.replace("_report.yml", "_summary.yml"))


def read_yaml(path):
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def require_fields(data, expected, label):
    if not isinstance(data, dict) or any(data.get(k) != v for k, v in expected.items()):
        raise ValueError(f"Missing or inconsistent {label}")


def checked_run(directory, name):
    result = read_yaml(directory / "result.yml")
    require_fields(
        result, {"target": TARGET, "test_name": name, "status": "PASS"}, "run result"
    )
    if result.get("iss_enabled") is not False or not isinstance(
        result.get("detail"), str
    ):
        raise ValueError("Invalid run result detail or ISS setting")
    manifest = read_yaml(directory / "cook_manifest.yml")
    options = {
        "target": TARGET,
        "test_name": name,
        "comp_mode": "rtl",
        "trace_mode": "notrace",
        "iss_enabled": False,
        "interactive_gui": False,
    }
    require_fields(
        manifest,
        {"recipe": "verilator-testharness-run", "options": options},
        "run manifest",
    )
    if any(
        manifest["options"].get(key) is not False
        for key in ("iss_enabled", "interactive_gui")
    ):
        raise ValueError("Invalid run manifest booleans")
    log = (directory / "testharness.log").read_text(encoding="utf-8")
    if "*** SUCCESS *** (tohost = 0)" not in log or any(
        marker in log
        for marker in (
            "*** FAILED ***",
            "SIMULATION FAILED",
            "[FAILED]",
            "UVM_ERROR",
            "UVM_FATAL",
        )
    ):
        raise ValueError(f"Missing successful termination or failure marker in {name}")
    # Only UART Hello promises visible text with this bare-metal runtime.
    if name == "hello-uart_0" and GREETING not in log:
        raise ValueError("UART Hello did not print the expected greeting")
    return result


def checked_batch(summary, report, receipts):
    require_fields(
        summary,
        {
            "schema_version": 1,
            "target": TARGET,
            "testlist": TESTLIST,
            "simulator": "verilator",
            "comp_mode": "rtl",
            "trace_mode": "notrace",
            "status": "PASS",
        },
        "testlist summary",
    )
    if summary.get("iss_enabled") is not False:
        raise ValueError("Summary unexpectedly enables ISS")
    for key, value in {"total": 2, "passed": 2, "failed": 0}.items():
        if type(summary.get(key)) is not int or summary[key] != value:
            raise ValueError(f"Incorrect testlist count: {key}")
    cases = summary.get("cases")
    expected = [
        {"test_name": name, "status": "PASS", "detail": receipts[name]["detail"]}
        for name in TESTS
    ]
    if cases != expected:
        raise ValueError("Summary disagrees with the two batch receipts")
    require_fields(report, {"status": "pass"}, "Cook report")
    metrics = report.get("metrics")
    if not isinstance(metrics, list) or len(metrics) != 1:
        raise ValueError("Expected one Cook report metric")
    rows = [
        {
            "status": "pass",
            "label": "PASS",
            "col": [TARGET, case["test_name"], case["detail"]],
        }
        for case in expected
    ]
    require_fields(
        metrics[0],
        {"status": "pass", "type": "table_status", "value": rows},
        "Cook report rows",
    )


def cook_commands():
    cook = [sys.executable, "cook.py"]
    run_options = [
        "-t",
        TARGET,
        "--trace-mode",
        "notrace",
        "--no-iss-enabled",
        "--quiet",
    ]
    return [
        (
            "compile-software",
            cook
            + [
                "sw-compile-testlist",
                "-t",
                TARGET,
                "-c",
                "github_actions_gcc",
                "-l",
                TESTLIST,
            ],
        ),
        (
            "compile-testharness",
            cook
            + [
                "verilator-testharness-comp",
                "-t",
                TARGET,
                "--trace-mode",
                "notrace",
                "--quiet",
            ],
        ),
        *[
            (name, cook + ["verilator-testharness-run", "-n", name] + run_options)
            for name in TESTS
        ],
        (
            "testlist",
            cook
            + ["testharness-run-testlist", "--simulator", "verilator", "-l", TESTLIST]
            + run_options,
        ),
    ]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_summary(evidence, destination):
    lines = [
        f"## Two-Hello smoke: {evidence['status']}",
        "",
        f"- Actual checkout: `{evidence.get('source_revision', 'unavailable')}`",
        f"- Event head: `{evidence.get('event_head', 'unavailable')}`",
        "- Target: `cv32a65x_axi`; RTL-only, notrace, no ISS/tandem.",
        "",
        "| Execution | Original Hello: normal exit | UART Hello: normal exit + text |",
        "| --- | --- | --- |",
    ]
    for phase in ("single", "batch"):
        results = evidence["checks"][phase]
        lines.append(f"| {phase} | {results[TESTS[0]]} | {results[TESTS[1]]} |")
    lines += [
        "",
        f"Batch report consistency: **{evidence['report_check']}**.",
        "",
        "Original Hello output is not required. UART output is checked in the simulation log.",
        "This is an execution smoke, not a full ISA regression or an interrupt-handling test.",
    ]
    if "error" in evidence:
        lines += [
            "",
            "Failure details are in `ci-results/evidence.json` and the step logs.",
        ]
    with destination.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def main():
    root = Path.cwd()
    output = root / "ci-results"
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    evidence = {
        "schema_version": 1,
        "status": "FAIL",
        "target": TARGET,
        "testlist": TESTLIST,
        "validation_mode": "rtl-only",
        "reference_model": None,
        "iss_enabled": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "commands": [],
        "checks": {
            phase: dict.fromkeys(TESTS, "NOT CHECKED") for phase in ("single", "batch")
        },
        "report_check": "NOT CHECKED",
        "sha256": {},
    }
    rc = 1
    try:
        # A fresh hosted checkout must never accept previous build products.
        if (root / "build" / TARGET).exists():
            raise ValueError(
                "Use a fresh checkout without existing target build outputs"
            )
        evidence["source_revision"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, timeout=10
        ).strip()
        for key in ("event-head", "event-base"):
            path = output / f"{key}.txt"
            evidence[key.replace("-", "_")] = (
                path.read_text().strip() if path.exists() else ""
            )
        entries = read_yaml(root / TESTLIST)["testlist"]
        if [entry["test"] for entry in entries] != [
            "hello-original",
            "hello-uart",
        ] or any(
            type(entry.get("iterations")) is not int or entry["iterations"] != 1
            for entry in entries
        ):
            raise ValueError(
                "Expected the committed two-entry Hello testlist with one iteration each"
            )
        for relative in (
            TESTLIST,
            "verif/tests/custom/hello_world/hello_world.c",
            "verif/tests/custom/hello_world/testharness_uart_hello_world.c",
        ):
            evidence["sha256"][relative] = sha256(root / relative)
        metadata = Path(env["CONFIG_DIR"]) / "environment.yml"
        environment = read_yaml(metadata)
        require_fields(
            environment,
            {"validation_mode": "rtl-only", "required_toolchain": "github_actions_gcc"},
            "tool environment",
        )
        evidence["environment"] = environment
        shutil.copy2(metadata, output / "toolchain-environment.yml")
        for label, command in cook_commands():
            log = output / f"{label}.log"
            print("Running:", " ".join(command), flush=True)
            code, timed_out = run_logged_process(
                command, cwd=root, env=env, log=log, timeout=2100
            )
            evidence["commands"].append(
                {
                    "stage": label,
                    "argv": command,
                    "cwd": str(root),
                    "exit_code": code,
                    "timed_out": timed_out,
                    "log": log.name,
                }
            )
            if code != 0 or timed_out:
                if label in TESTS:
                    evidence["checks"]["single"][label] = "FAIL"
                raise ValueError(
                    f"{label} failed: exit={code}, timed_out={timed_out}; see {log.name}"
                )
            if label in TESTS:
                evidence["checks"]["single"][label] = "FAIL"
                checked_run(root / RUNS / label, label)
                evidence["checks"]["single"][label] = "PASS"
                saved = output / "single" / label
                saved.mkdir(parents=True)
                for name in ("testharness.log", "result.yml", "cook_manifest.yml"):
                    shutil.copy2(root / RUNS / label / name, saved / name)
        receipts = {}
        for name in TESTS:
            evidence["checks"]["batch"][name] = "FAIL"
            receipts[name] = checked_run(root / RUNS / name, name)
            evidence["checks"]["batch"][name] = "PASS"
        evidence["report_check"] = "FAIL"
        summary, report = read_yaml(root / SUMMARY), read_yaml(root / REPORT)
        checked_batch(summary, report, receipts)
        evidence["report_check"] = "PASS"
        evidence["results"] = summary
        for path in (REPORT, SUMMARY):
            shutil.copy2(root / path, output / path.name)
        for name in TESTS:
            path = Path("build") / TARGET / "compile" / name / f"{name}.elf"
            evidence["sha256"][str(path)] = sha256(root / path)
        binary = (
            Path("build")
            / TARGET
            / "elab/sim_rtl_verilator_testharness/Variane_testharness"
        )
        evidence["sha256"][str(binary)] = sha256(root / binary)
        evidence["status"], rc = "PASS", 0
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        yaml.YAMLError,
        subprocess.SubprocessError,
    ) as error:
        evidence["error"] = str(error)
        print(f"ERROR: {error}", file=sys.stderr)
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        (output / "evidence.json").write_text(
            json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
        )
        (output / "exit_code").write_text(f"{rc}\n", encoding="utf-8")
        if env.get("GITHUB_STEP_SUMMARY"):
            write_summary(evidence, Path(env["GITHUB_STEP_SUMMARY"]))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
