#!/usr/bin/env python3
"""
Cohomological coherence check for CI/CD pipelines.

Reads a JSON fixture describing a build → test → deploy pipeline. Constructs
a cellular sheaf over the pipeline graph. Computes dim H^1 — the dimension
of the first cohomology group of the sheaf. Non-zero H^1 means the evidence
cannot be glued into a single consistent global story; we name the exact
edges where the inconsistency lives.

Writes certificate.json (machine-readable) and prints a human summary.
Zero external dependencies — pure Python. Not production-grade; a validation
artifact for a weekend experiment. See the companion blog post for the math.
"""

from __future__ import annotations
import json
import sys
import time
from pathlib import Path
from typing import Callable

PRIME = (1 << 61) - 1


def hash_scalar(s: str) -> float:
    """Deterministically encode a hex/string into a float. Collisions possible
    in principle; cryptographically irrelevant here because we only compare
    for equality, not security."""
    if s.startswith("sha256:"):
        s = s[7:]
    try:
        n = int(s[:16], 16)
    except ValueError:
        n = hash(s) & ((1 << 61) - 1)
    return float(n % PRIME)


# Stalk shape per cell type. Order defines coordinates in the vector space.
STALK_SHAPE = {
    "Commit": ["ts", "sha_scalar"],
    "Build": ["ts", "commit_sha_scalar", "artifact_hash_scalar"],
    "Test": ["ts", "artifact_hash_scalar", "result_code"],
    "Deploy": ["ts", "artifact_hash_scalar", "env_code"],
}


def stalk_vector(ev: dict) -> list[float]:
    t = ev["type"]
    ts = float(ev["ts"])
    if t == "Commit":
        return [ts, hash_scalar(ev["sha"])]
    if t == "Build":
        return [ts, hash_scalar(ev["commit_sha"]), hash_scalar(ev["artifact_hash"])]
    if t == "Test":
        return [
            ts,
            hash_scalar(ev["artifact_hash"]),
            1.0 if ev["result"] == "pass" else 0.0,
        ]
    if t == "Deploy":
        return [ts, hash_scalar(ev["artifact_hash"]), hash_scalar(ev["environment"])]
    raise ValueError(f"unknown cell type: {t}")


# Pure-Python matrix helpers.
def mat_vec(M: list[list[float]], v: list[float]) -> list[float]:
    return [sum(row[i] * v[i] for i in range(len(v))) for row in M]


def vec_sub(a: list[float], b: list[float]) -> list[float]:
    return [a[i] - b[i] for i in range(len(a))]


def vec_norm(v: list[float]) -> float:
    return sum(x * x for x in v) ** 0.5


def matrix_rank(M: list[list[float]], tol: float = 1e-8) -> int:
    """Row-reduce to echelon form; count non-zero rows."""
    if not M or not M[0]:
        return 0
    A = [row[:] for row in M]
    rows, cols = len(A), len(A[0])
    rank = 0
    for c in range(cols):
        # Find pivot row at or below rank.
        pivot = -1
        pmax = tol
        for r in range(rank, rows):
            if abs(A[r][c]) > pmax:
                pmax = abs(A[r][c])
                pivot = r
        if pivot < 0:
            continue
        A[rank], A[pivot] = A[pivot], A[rank]
        pv = A[rank][c]
        # Eliminate below AND above (full reduction for stability).
        for r in range(rows):
            if r != rank and abs(A[r][c]) > tol:
                factor = A[r][c] / pv
                for cc in range(c, cols):
                    A[r][cc] -= factor * A[rank][cc]
        rank += 1
        if rank == rows:
            break
    return rank


# Sheaf cohomology measures EQUALITY constraints: two events referring to
# the same fact (artifact hash, commit sha) must agree on it. Temporal
# monotonicity (ts_u < ts_v) is an INEQUALITY — it sits outside the sheaf
# and is checked separately as a DAG property. Keep that distinction clean.
#
# Each edge projects ONLY the coherence-critical field(s) from each
# endpoint's stalk into the shared edge stalk. Residual = ρ_u(s_u) - ρ_v(s_v).
def edge_spec(src_type: str, dst_type: str):
    if (src_type, dst_type) == ("Commit", "Build"):
        return (
            ["I3_lineage"],
            1,
            [[0, 1]],  # Commit[ts, sha] -> [sha]
            [[0, 1, 0]],  # Build[ts, commit, art] -> [commit]
        )
    if (src_type, dst_type) == ("Build", "Test"):
        return (
            ["I2_artifact_coherence"],
            1,
            [[0, 0, 1]],  # Build[ts, commit, art] -> [art]
            [[0, 1, 0]],  # Test[ts, art, result] -> [art]
        )
    if (src_type, dst_type) == ("Test", "Deploy"):
        return (
            ["I2_artifact_coherence"],
            1,
            [[0, 1, 0]],  # Test[ts, art, result] -> [art]
            [[0, 1, 0]],  # Deploy[ts, art, env] -> [art]
        )
    raise ValueError(f"no restriction map for edge {src_type} -> {dst_type}")


def check_temporal_monotonicity(events: list[dict], edges: list[dict]) -> list[dict]:
    """Separate DAG check: for each edge, dst.ts must be strictly greater
    than src.ts. Returns list of violations (empty if coherent)."""
    by_id = {e["id"]: e for e in events}
    violations = []
    for e in edges:
        src_ts = float(by_id[e["src"]]["ts"])
        dst_ts = float(by_id[e["dst"]]["ts"])
        if dst_ts <= src_ts:
            violations.append(
                {
                    "edge_id": e["id"],
                    "from": e["src"],
                    "to": e["dst"],
                    "edge_type": f"{e['src_type']}->{e['dst_type']}",
                    "src_ts": src_ts,
                    "dst_ts": dst_ts,
                    "violation": "temporal_non_monotone",
                }
            )
    return violations


def build_edges(events: list[dict]):
    by_id = {e["id"]: e for e in events}
    edges = []
    for ev in events:
        if "parent_id" not in ev:
            continue
        parent = by_id[ev["parent_id"]]
        inv, edim, rho_src, rho_dst = edge_spec(parent["type"], ev["type"])
        edges.append(
            {
                "id": f"e_{parent['id']}_{ev['id']}",
                "src": parent["id"],
                "dst": ev["id"],
                "src_type": parent["type"],
                "dst_type": ev["type"],
                "invariants": inv,
                "edge_dim": edim,
                "rho_src": rho_src,
                "rho_dst": rho_dst,
            }
        )
    return edges


def compute_cohomology(events, edges, section):
    v_ids = [e["id"] for e in events]
    v_types = {e["id"]: e["type"] for e in events}
    v_dims = {vid: len(STALK_SHAPE[v_types[vid]]) for vid in v_ids}
    v_offset = {}
    off = 0
    for vid in v_ids:
        v_offset[vid] = off
        off += v_dims[vid]
    total_v_dim = off
    total_e_dim = sum(e["edge_dim"] for e in edges)

    # Build coboundary matrix δ^0 of shape (total_e_dim, total_v_dim).
    delta0 = [[0.0] * total_v_dim for _ in range(total_e_dim)]
    row = 0
    for e in edges:
        src_off, dst_off = v_offset[e["src"]], v_offset[e["dst"]]
        src_d, dst_d = v_dims[e["src"]], v_dims[e["dst"]]
        for i in range(e["edge_dim"]):
            for j in range(src_d):
                delta0[row + i][src_off + j] = float(e["rho_src"][i][j])
            for j in range(dst_d):
                delta0[row + i][dst_off + j] = -float(e["rho_dst"][i][j])
        row += e["edge_dim"]

    # Compute residuals on the observed section (per-edge). Any nonzero
    # residual means the invariant (equality constraint) is violated.
    seams = []
    for e in edges:
        s_src = section[e["src"]]
        s_dst = section[e["dst"]]
        rs = mat_vec(e["rho_src"], s_src)
        rd = mat_vec(e["rho_dst"], s_dst)
        residual = vec_sub(rs, rd)
        norm = vec_norm(residual)
        if norm > 1e-6:
            broken = []
            for i, v in enumerate(residual):
                if abs(v) > 1e-6:
                    if "I2_artifact_coherence" in e["invariants"]:
                        broken.append("I2_artifact_hash_mismatch")
                    elif "I3_lineage" in e["invariants"]:
                        broken.append("I3_commit_sha_mismatch")
                    else:
                        broken.append(f"coord_{i}_nonzero")
            seams.append(
                {
                    "edge_id": e["id"],
                    "from": e["src"],
                    "to": e["dst"],
                    "edge_type": f"{e['src_type']}->{e['dst_type']}",
                    "residual_norm": norm,
                    "residual": residual,
                    "invariants_broken": broken,
                }
            )

    rank = matrix_rank(delta0)
    h0_dim = total_v_dim - rank
    h1_dim = total_e_dim - rank
    return {
        "delta0_shape": [total_e_dim, total_v_dim],
        "coboundary_rank": rank,
        "H0_dim": h0_dim,
        "H1_dim": h1_dim,
        "total_edge_dim": total_e_dim,
        "total_vertex_dim": total_v_dim,
        "seams": seams,
    }


def main(argv):
    if len(argv) < 2:
        print("usage: cert.py <fixture.json> [--out cert.json]", file=sys.stderr)
        sys.exit(2)
    fixture_path = Path(argv[1])
    out_path = Path("cert.json")
    if "--out" in argv:
        out_path = Path(argv[argv.index("--out") + 1])

    data = json.loads(fixture_path.read_text())
    events = data["events"]
    section = {e["id"]: stalk_vector(e) for e in events}
    edges = build_edges(events)
    cohom = compute_cohomology(events, edges, section)
    temporal_violations = check_temporal_monotonicity(events, edges)

    verdict = (
        "coherent"
        if not cohom["seams"] and not temporal_violations
        else "incoherent"
    )
    lines = []
    if verdict == "coherent":
        lines.append(
            f"OK coherent: {len(edges)} edge(s), 0 seams. Evidence glues into a single consistent story."
        )
        lines.append(
            f"   H^0 dim = {cohom['H0_dim']} (dimension of consistent global sections)"
        )
        lines.append(
            f"   H^1 dim = {cohom['H1_dim']} (zero sheaf obstructions)"
        )
    else:
        n = len(cohom["seams"]) + len(temporal_violations)
        lines.append(
            f"FAIL incoherent: {n} edge-level invariant violation(s). Evidence cannot all be true at once."
        )
        for s in cohom["seams"]:
            lines.append(
                f"   sheaf seam at {s['edge_id']} ({s['edge_type']}): {', '.join(s['invariants_broken'])}"
            )
            lines.append(
                f"     residual = [{', '.join(f'{x:.3e}' for x in s['residual'])}]"
            )
        for v in temporal_violations:
            lines.append(
                f"   DAG violation at {v['edge_id']} ({v['edge_type']}): dst_ts {v['dst_ts']} <= src_ts {v['src_ts']} — events not in monotone order"
            )
        lines.append(
            f"   H^1 dim = {cohom['H1_dim']}, coboundary rank = {cohom['coboundary_rank']}, temporal violations = {len(temporal_violations)}"
        )

    certificate = {
        "version": "sheaf-cert/1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "vertical": "ci-cd-coherence",
        "fixture": data.get("fixture_name"),
        "cell_complex": {
            "vertices": [
                {
                    "id": e["id"],
                    "type": e["type"],
                    "stalk_dim": len(STALK_SHAPE[e["type"]]),
                    "stalk_fields": STALK_SHAPE[e["type"]],
                }
                for e in events
            ],
            "edges": [
                {
                    "id": e["id"],
                    "from": e["src"],
                    "to": e["dst"],
                    "type": f"{e['src_type']}->{e['dst_type']}",
                    "invariants": e["invariants"],
                    "edge_dim": e["edge_dim"],
                }
                for e in edges
            ],
        },
        "cohomology": {
            "H0_dim": cohom["H0_dim"],
            "H1_dim": cohom["H1_dim"],
            "coboundary_rank": cohom["coboundary_rank"],
            "coboundary_shape": cohom["delta0_shape"],
        },
        "verdict": verdict,
        "sheaf_obstructions": cohom["seams"],
        "temporal_violations": temporal_violations,
        "human_readable": {"summary": "\n".join(lines)},
    }

    out_path.write_text(json.dumps(certificate, indent=2))
    print("\n".join(lines))
    print(f"\ncertificate: {out_path}")
    sys.exit(0 if verdict == "coherent" else 1)


if __name__ == "__main__":
    main(sys.argv)
