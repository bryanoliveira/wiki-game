"""Colours and matplotlib settings shared by the notebook figures, and where the figures for the docs are saved.

The colours are the light-mode tokens of the task viewer (viewer/index.html), so a figure in docs/ and the
viewer show the same thing in the same colour: blue for optimal or "good", red for a step with regret, grey for
everything else.
"""

import matplotlib.pyplot as plt

from .world import ROOT

FIGURES = ROOT / "docs" / "figures"

INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, MID = "#e1e0d9", "#c3c2b7", "#f0efec"
BLUE, BLUE_WASH, BLUE_DARK = "#2a78d6", "#cde2fb", "#104281"
RED, RED_WASH = "#e34948", "#fbe1e0"
SEQUENTIAL_3 = ["#86b6ee", "#2a78d6", "#104281"]  # light to dark blue, for an ordered variable with three values


def use():
    plt.rcParams.update({
        "figure.facecolor": "white", "savefig.facecolor": "white", "savefig.dpi": 200, "savefig.bbox": "tight",
        "font.size": 10, "axes.titlesize": 10, "axes.titleweight": "normal", "axes.titlecolor": INK,
        "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
        "lines.linewidth": 2, "legend.frameon": False,
    })


def save(fig, name: str):
    """Write a figure to docs/figures/<name>.png, where the Markdown docs link to it."""
    FIGURES.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURES / f"{name}.png")
