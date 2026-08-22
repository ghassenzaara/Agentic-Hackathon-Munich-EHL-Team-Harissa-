"""Parser for CalculiX .frd result files.

Written rather than delegated to ccx2paraview on purpose: this is the last hop
before a number becomes a verdict, and a wrong column here produces a
believable value rather than a crash. ccx2paraview is pinned in the environment
and tests/test_frd_crosscheck.py runs it against this parser on the same file,
so a regression in either one shows up as a disagreement.

The .frd format is FIXED WIDTH, not whitespace-separated. Adjacent negative
values in E12.5 run together with no space between them:

    -1.23456E+02-4.56789E+01

`.split()` reads that as one token and silently drops a component. Every value
here is sliced by column.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

_VALUE_WIDTH = 12


@dataclass(frozen=True)
class Block:
    """One nodal result field, e.g. DISP or STRESS."""

    name: str
    components: tuple[str, ...]
    node_ids: np.ndarray  # (n,) as written by CalculiX, 1-based
    values: np.ndarray  # (n, len(components))


@dataclass(frozen=True)
class FrdResult:
    """Everything read out of one .frd, keyed by block name."""

    blocks: dict[str, Block]
    node_ids: np.ndarray  # the mesh node ids from the 2C block, in file order

    def field(self, name: str) -> np.ndarray:
        """Values for `name`, reordered to match the mesh's node order.

        CalculiX is free to write result nodes in a different order from the
        node block, and on a renumbered mesh it does. Aligning here means every
        caller can index results with the same integer it uses for `points`.
        """
        if name not in self.blocks:
            raise KeyError(f"{name} not in .frd; have {sorted(self.blocks)}")
        blk = self.blocks[name]
        lookup = {nid: i for i, nid in enumerate(blk.node_ids.tolist())}
        try:
            rows = [lookup[nid] for nid in self.node_ids.tolist()]
        except KeyError as exc:  # pragma: no cover - malformed solver output
            raise RuntimeError(f"node {exc} missing from {name} block") from exc
        return blk.values[rows]


def _header_name(line: str) -> str:
    """Block/component name from a -4 or -5 line: 1X,'-N',2X,A8."""
    return line[5:13].strip()


def _split_fixed(line: str, n_values: int) -> tuple[int, list[float]]:
    """(node_id, values) from a ' -1' data line, by column.

    CalculiX writes the node id as either I5 or I10 depending on how it was
    built. Rather than trusting a format flag in the header, the width is
    deduced from the payload: everything after the id must be exactly
    `n_values * 12` characters, and only one of the two widths can satisfy that.
    """
    body = line.rstrip("\n").rstrip()
    span = n_values * _VALUE_WIDTH
    for id_end in (13, 8):  # I10 then I5, both after the 3-char ' -1'
        if len(body) - id_end == span:
            node = int(body[3:id_end])
            vals = [
                float(body[id_end + i * _VALUE_WIDTH : id_end + (i + 1) * _VALUE_WIDTH])
                for i in range(n_values)
            ]
            return node, vals
    raise ValueError(
        f"cannot column-split a {len(body)}-char line for {n_values} values: {body!r}"
    )


def read_frd(path: Path | str) -> FrdResult:
    """Read node coordinates and every nodal result block from a .frd."""
    lines = Path(path).read_text(errors="replace").splitlines()

    node_ids: list[int] = []
    blocks: dict[str, Block] = {}

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]

        # Node block: '    2C' then ' -1' id x y z until ' -3'.
        if line.startswith("    2C"):
            i += 1
            while i < n and not lines[i].startswith(" -3"):
                if lines[i].startswith(" -1"):
                    nid, _ = _split_fixed(lines[i], 3)
                    node_ids.append(nid)
                i += 1

        # Result block: ' -4' name, then ' -5' components, then ' -1' data.
        elif line.startswith(" -4"):
            name = _header_name(line)
            comps: list[str] = []
            i += 1
            while i < n and lines[i].startswith(" -5"):
                cname = _header_name(lines[i])
                # 'ALL' is a summary entry in the header with no data column.
                if cname and cname != "ALL":
                    comps.append(cname)
                i += 1

            ids: list[int] = []
            vals: list[list[float]] = []
            while i < n and not lines[i].startswith(" -3"):
                if lines[i].startswith(" -1"):
                    nid, v = _split_fixed(lines[i], len(comps))
                    ids.append(nid)
                    vals.append(v)
                i += 1

            if ids:
                # A second *STEP would overwrite the first; we solve one step.
                blocks[name] = Block(
                    name=name,
                    components=tuple(comps),
                    node_ids=np.asarray(ids, dtype=np.int64),
                    values=np.asarray(vals, dtype=float),
                )
        i += 1

    if not node_ids:
        raise RuntimeError(f"{path}: no node block -- solver probably aborted")
    return FrdResult(blocks=blocks, node_ids=np.asarray(node_ids, dtype=np.int64))
