"""Text and SVG views of a finished mission (ground truth path, fire, robot's estimate)."""
from __future__ import annotations

from .runner import MissionResult


def ascii_map(res: MissionResult, cell: float = 0.5) -> str:
    w = res.world
    nx, ny = int(w.width / cell), int(w.height / cell)
    g = [[" "] * nx for _ in range(ny)]
    for o in w.obstacles:
        for i in range(int(o.x0 / cell), int(o.x1 / cell)):
            for j in range(int(o.y0 / cell), int(o.y1 / cell)):
                g[j][i] = "#"
    for px, py in w.path:
        i, j = min(nx - 1, int(px / cell)), min(ny - 1, int(py / cell))
        if g[j][i] == " ":
            g[j][i] = "."

    def put(p, ch):
        if p:
            g[min(ny - 1, max(0, int(p[1] / cell)))][min(nx - 1, max(0, int(p[0] / cell)))] = ch
    put(w.start[:2], "S")
    put(res.true_fire, "F")
    put(res.estimated_fire, "E")
    put((w.x, w.y), "R")
    rows = ["+" + "-" * nx + "+"] + ["|" + "".join(r) + "|" for r in reversed(g)] + ["+" + "-" * nx + "+"]
    return "\n".join(rows) + "\nS start  R robot end  F true fire  E estimated fire  # obstacle  . path"


def svg(res: MissionResult, scale: float = 70.0) -> str:
    w = res.world
    W, H = w.width * scale, w.height * scale
    Y = lambda y: H - y * scale  # noqa: E731
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H + 46:.0f}" viewBox="0 0 {W:.0f} {H + 46:.0f}" '
           f'font-family="sans-serif" font-size="12">',
           f'<defs><clipPath id="room"><rect width="{W:.0f}" height="{H:.0f}"/></clipPath></defs>',
           f'<rect width="{W:.0f}" height="{H:.0f}" fill="#f6f4ef"/>', '<g clip-path="url(#room)">']
    for o in w.obstacles:
        out.append(f'<rect x="{o.x0 * scale:.1f}" y="{Y(o.y1):.1f}" width="{(o.x1 - o.x0) * scale:.1f}" '
                   f'height="{(o.y1 - o.y0) * scale:.1f}" fill="#8a8a8a"/>')
    for f in w.fires:
        out.append(f'<circle cx="{f.x * scale:.1f}" cy="{Y(f.y):.1f}" r="{f.sigma_m * scale * 1.6:.1f}" fill="#ff5a1f" opacity="0.15"/>')
        out.append(f'<circle cx="{f.x * scale:.1f}" cy="{Y(f.y):.1f}" r="9" fill="#e8420c"/>')
    pts = " ".join(f"{x * scale:.1f},{Y(y):.1f}" for x, y in w.path[::3])
    out.append(f'<polyline points="{pts}" fill="none" stroke="#2b6cb0" stroke-width="2" opacity="0.8"/>')
    out.append(f'<circle cx="{w.start[0] * scale:.1f}" cy="{Y(w.start[1]):.1f}" r="6" fill="#2f855a"/>')
    out.append(f'<circle cx="{w.x * scale:.1f}" cy="{Y(w.y):.1f}" r="{w.radius * scale:.1f}" fill="#2b6cb0" opacity="0.5"/>')
    if res.estimated_fire:
        ex, ey = res.estimated_fire
        out.append(f'<path d="M{ex * scale - 8:.1f},{Y(ey) - 8:.1f} l16,16 m0,-16 l-16,16" stroke="#d69e2e" stroke-width="3"/>')
    out.append(f'<rect width="{W:.0f}" height="{H:.0f}" fill="none" stroke="#444" stroke-width="3"/>')
    label = f"{res.scenario} seed {res.seed}: {res.outcome} in {res.sim_time:.0f}s"
    if res.error_m is not None:
        label += f", fire position error {res.error_m:.2f} m"
    out.append("</g>")
    out.append(f'<text x="6" y="{H + 18:.0f}" fill="#222">{label}</text>')
    out.append(f'<text x="6" y="{H + 36:.0f}" fill="#555">green = start, blue = path, red = true fire, yellow X = robot estimate</text>')
    out.append("</svg>")
    return "\n".join(out)
