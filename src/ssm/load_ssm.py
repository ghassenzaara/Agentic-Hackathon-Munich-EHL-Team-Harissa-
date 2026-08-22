"""Read the ssm_tibia statistical shape model from Python (no MATLAB).

The published dataset ships the shape models as MATLAB .mat files and all its
example code is MATLAB (GIBBON + geom3d). This module is the Python door into
the same data, so the build pipeline never needs MATLAB.

Verified against ShapeModels/tibia/tibiaShapeModel.mat: scipy.io.loadmat reads
it directly (pre-v7.3 format), no h5py needed.

Only numpy + scipy are required.
"""

from pathlib import Path

import numpy as np
import scipy.io as sio

DATA = Path(__file__).resolve().parents[1] / "data" / "ssm_tibia"

MODELS = {
    "tibia": ("ShapeModels/tibia/tibiaShapeModel.mat", "tibiaShapeModel"),
    "tibia-fibula": (
        "ShapeModels/tibia-fibula/tibiaFibulaShapeModel.mat",
        "tibiaFibulaShapeModel",
    ),
    "tibia-plus-trabecular": (
        "ShapeModels/tibia-plus-trabecular/tibTrabShapeModel.mat",
        "tibTrabShapeModel",
    ),
    "trabecular": (
        "ShapeModels/trabecular-tibia/trabShapeModel.mat",
        "trabShapeModel",
    ),
}


class ShapeModel:
    """A loaded SSM: mean shape, PC loadings, and the reference triangulation.

    Field shapes for the 'tibia' model (30 training surfaces, 3500 points):
        mean         (1, 10500)   flattened mean point cloud
        loadings     (10500, 29)  principal components, columns
        latent       (29, 1)      PC variances
        score        (30, 29)     training-case scores
        F            (6996, 3)    triangle connectivity, 1-based (MATLAB)
        retainPCs    scalar       5 PCs -> 90.3% of variance
    """

    def __init__(self, name="tibia", root=DATA):
        rel, var = MODELS[name]
        raw = sio.loadmat(str(Path(root) / rel))[var][0, 0]
        self.name = name
        self.mean = np.asarray(raw["mean"]).ravel()
        self.loadings = np.asarray(raw["loadings"])
        self.latent = np.asarray(raw["latent"]).ravel()
        self.score = np.asarray(raw["score"])
        # MATLAB faces are 1-based; convert once, here.
        self.faces = np.asarray(raw["F"]).astype(np.int64) - 1
        self.retain_pcs = int(np.asarray(raw["retainPCs"]).ravel()[0])
        self.n_points = self.mean.size // 3

    def points(self, weights=None):
        """Reconstruct a surface. weights are in standard deviations per PC."""
        pts = self.mean.copy()
        if weights is not None:
            w = np.zeros(self.loadings.shape[1])
            w[: len(weights)] = weights
            # errstate: Accelerate BLAS raises spurious FP flags on this matmul;
            # outputs are finite and validated against the stored training scores.
            with np.errstate(all="ignore"):
                pts = pts + self.loadings @ (w * np.sqrt(self.latent))
        return pts.reshape(self.n_points, 3)

    def sample(self, rng, n_pcs=None, sigma=1.5):
        """Draw an unseen anatomy: truncated-normal weights on the kept PCs."""
        k = n_pcs or self.retain_pcs
        w = np.clip(rng.standard_normal(k), -sigma, sigma)
        return self.points(w), w

    def training_case(self, i):
        """Reconstruct training surface i from its stored scores."""
        with np.errstate(all="ignore"):
            pts = self.mean + self.loadings @ self.score[i]
        return pts.reshape(self.n_points, 3)


def write_stl(path, verts, faces, name="surface"):
    """Minimal binary STL writer, so sampling needs no mesh library."""
    tri = verts[faces]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, ln, out=np.zeros_like(n), where=ln > 0)
    rec = np.zeros(len(faces), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
    rec["n"], rec["v"] = n, tri
    with open(path, "wb") as f:
        f.write(name.encode()[:80].ljust(80, b"\0"))
        f.write(np.uint32(len(faces)).tobytes())
        f.write(rec.tobytes())
    return path


if __name__ == "__main__":
    import sys

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("out/ssm_samples")
    out.mkdir(parents=True, exist_ok=True)
    m = ShapeModel("tibia")
    print(f"{m.name}: {m.n_points} points, {len(m.faces)} faces, "
          f"{m.loadings.shape[1]} PCs, {m.retain_pcs} retained")
    write_stl(out / "mean.stl", m.points(), m.faces, "tibia-mean")
    rng = np.random.default_rng(0)
    for i in range(5):
        pts, w = m.sample(rng)
        write_stl(out / f"sample_{i:02d}.stl", pts, m.faces, f"tibia-sample-{i}")
        print(f"  sample {i}: weights {np.round(w, 2)}")
    print(f"wrote mean.stl + 5 samples to {out}")
