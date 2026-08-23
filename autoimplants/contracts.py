"""FROZEN CONTRACTS. Do not change without telling all four owners.

Two things are frozen in this file:

  1. The ``Report`` / ``Check`` JSON shape that validators emit and Devin reads.
  2. The validator signature:  ``validate(implant_path: str, case: dict) -> Report``

Everything else in the repo may churn freely. This file is the reason four people
can build at once without integrating.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"  # the validator itself blew up; never let this crash the loop
SKIP = "SKIP"    # check exists but is not implemented yet

_OK_STATUSES = (PASS, SKIP)


@dataclass
class Check:
    """One numeric, thresholded, located verdict.

    Field order matches the agreed JSON exactly. Everything after ``status`` is
    optional so a check can be purely qualitative when it has to be -- but prefer
    emitting ``value``/``limit``/``location``: those three are what let Devin fix
    the design instead of guessing at it.
    """

    id: str
    status: str
    value: float | None = None
    limit: float | None = None
    unit: str = ""
    location: list[float] | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in _OK_STATUSES

    def one_line(self) -> str:
        val = "-" if self.value is None else f"{self.value:.6g}"
        lim = "-" if self.limit is None else f"{self.limit:.6g}"
        loc = ""
        if self.location:
            loc = "  at (" + ", ".join(f"{c:.4g}" for c in self.location) + ")"
        return f"{self.id:<28} {self.status:<6} {val:>10} / {lim:<10} {self.unit:<5}{loc}"


@dataclass
class Report:
    """The full verdict on one exported implant.

    ``passed`` and ``checks`` are the frozen core. ``iteration``/``params``/``meta``
    are additive with defaults, so the agreed two-field shape stays a valid subset.
    """

    passed: bool = False
    checks: list[Check] = field(default_factory=list)
    iteration: int = 0
    params: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Accept raw dicts so from_dict()/JSON round-trips need no special casing.
        self.checks = [c if isinstance(c, Check) else Check(**c) for c in self.checks]

    # -- construction ----------------------------------------------------------

    @classmethod
    def from_checks(cls, checks: list[Check], **kw: Any) -> Report:
        """Preferred constructor: ``passed`` is derived, never asserted by hand.

        A validator cannot accidentally report success while emitting failures.
        """
        checks = list(checks)
        return cls(passed=all(c.ok for c in checks), checks=checks, **kw)

    @classmethod
    def errored(cls, check_id: str, message: str, **kw: Any) -> Report:
        """A validator that raised. Always a failing report, never an exception."""
        return cls.from_checks([Check(id=check_id, status=ERROR, message=message)], **kw)

    @classmethod
    def merge(cls, reports: list[Report], **kw: Any) -> Report:
        """Concatenate several validators' checks into one verdict."""
        checks: list[Check] = []
        meta: dict = {}
        for r in reports:
            checks.extend(r.checks)
            meta.update(r.meta)
        merged = cls.from_checks(checks, **kw)
        merged.meta = {**meta, **merged.meta}
        return merged

    # -- queries ---------------------------------------------------------------

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def by_id(self, check_id: str) -> Check | None:
        return next((c for c in self.checks if c.id == check_id), None)

    # -- serialisation ---------------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        d["schema_version"] = SCHEMA_VERSION
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    @classmethod
    def from_dict(cls, d: dict) -> Report:
        d = dict(d)
        d.pop("schema_version", None)
        return cls(**d)

    @classmethod
    def load(cls, path: str | Path) -> Report:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # -- the text Devin actually reads ----------------------------------------

    def summary(self) -> str:
        """Human/agent readable failure table.

        This string is pasted verbatim into Devin's prompt, so it is a real
        interface: keep it dense, aligned, and unambiguous.
        """
        fails = self.failures()
        counts = {
            status: sum(check.status == status for check in self.checks)
            for status in (PASS, SKIP, FAIL, ERROR)
        }
        verdict = "PASS" if self.passed else "FAIL"
        head = (
            f"Iteration {self.iteration}: {verdict} "
            f"({counts[PASS]} PASS, {counts[SKIP]} SKIP, "
            f"{counts[FAIL]} FAIL, {counts[ERROR]} ERROR; {len(self.checks)} total)"
        )
        lines = [head, "=" * len(head)]

        if not self.checks:
            lines.append("(no checks were run)")
            return "\n".join(lines)

        lines.append("")
        lines.append(f"{'CHECK':<28} {'STATUS':<6} {'VALUE':>10} / {'LIMIT':<10} {'UNIT':<5}")
        lines.append("-" * 78)
        for c in self.checks:
            lines.append(c.one_line())
            if not c.ok and c.message:
                lines.append(f"    -> {c.message}")

        if fails:
            lines.append("")
            lines.append("FAILING: " + ", ".join(c.id for c in fails))
        return "\n".join(lines)

    def __str__(self) -> str:  # so `print(report)` is useful
        return self.summary()
