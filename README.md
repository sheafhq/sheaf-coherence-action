# sheaf-coherence-action

GitHub Action: **cohomological coherence certificate** for CI/CD pipelines.

Reads a JSON describing your pipeline (commit, build, test, deploy events). Constructs a cellular sheaf over it. Computes the first cohomology group $H^1$. Writes a certificate stating whether the evidence glues into a single consistent global story — and if not, exactly which edge is the seam.

Catches what SLSA + Sigstore don't: **the artifact you tested is not the artifact you deployed** (stale cache, artifact drift, mismatched hashes between stages).

## Use

```yaml
- uses: sheafhq/sheaf-coherence-action@v0.1.0
  with:
    evidence: path/to/your/evidence.json
    fail-on-incoherent: 'true'
```

## What it checks

- **I2 artifact coherence** — the artifact hash must be the same at build, test, and deploy
- **I3 lineage** — the commit SHA claimed by build must match the committed SHA
- **Temporal monotonicity** — pipeline stages in order

## Evidence schema

See [fixtures/coherent.json](./fixtures/coherent.json) for a happy-path example and [fixtures/incoherent_artifact_drift.json](./fixtures/incoherent_artifact_drift.json) for a stale-artifact failure.

## Local run

```bash
python3 cert.py fixtures/coherent.json              # exit 0
python3 cert.py fixtures/incoherent_artifact_drift.json   # exit 1
```

Pure Python, zero dependencies.

## The math, briefly

For each pipeline event (vertex), a stalk $\mathcal{E}(v) \in \mathbb{R}^{d_v}$. For each adjacency (edge), a restriction map projecting both endpoints into a shared edge stalk. A **seam** on edge $e$ means $\rho_u(s_u) \neq \rho_v(s_v)$ — the two endpoints disagree on a field they're supposed to share.

$\dim H^1$ counts independent cycle-level obstructions. For linear pipelines (tree-shaped), $H^1 = 0$ — residual-per-edge is equivalent. For DAGs with fan-in (parallel builds, pre-flight checks), $\dim H^1 > 0$ becomes diagnostically distinct from pairwise checks.

Full math + source at [github.com/sheafhq/sheaf/tree/main/packages/coherence](https://github.com/sheafhq/sheaf/tree/main/packages/coherence).

## License

MIT.
