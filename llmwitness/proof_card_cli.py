"""Local file edge for report Proof Cards; exclusive output, no network."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

from .proof_card import (
    MAX_REPORT_BYTES,
    REPORT_KINDS,
    ProofCardError,
    render_proof_card,
)


def add_proof_card_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "proof-card", help="Summarize supplied local reports offline; not verification"
    )
    for kind in REPORT_KINDS:
        parser.add_argument("--" + kind, help="Local JSON report path")
    parser.add_argument("--output", default="-")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")


def run_proof_card(args: argparse.Namespace) -> None:
    temporary_path = None
    try:
        reports = {}
        for kind in REPORT_KINDS:
            path = getattr(args, kind)
            if path is not None:
                with open(path, "rb") as source:
                    reports[kind] = source.read(MAX_REPORT_BYTES + 1)
        content = render_proof_card(reports, format=args.format)
        if args.output == "-":
            sys.stdout.write(content)
            return
        output = Path(args.output)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=output.parent, delete=False
        ) as target:
            temporary_path = Path(target.name)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.link(temporary_path, output)
    except (OSError, UnicodeError):
        raise ProofCardError("proof card local I/O failed") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning(
                    "proof card temporary metadata cleanup failed"
                )
