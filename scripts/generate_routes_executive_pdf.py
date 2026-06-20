#!/usr/bin/env python3
"""Generate docs/vchf-menace-routes-executive.pdf — executive route diagram deck."""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
sys.path.insert(0, str(ROOT))

from src.scanner.routes import ALL_DIRECTIONS, active_directions

PDF_STEM = "vchf-menace-routes-executive"
GITHUB = "vchf_menace"
BOT = "VCHF Menace"
TOKEN = "VCHF"
N_ROUTES = 10
VNX_MIN = 30.0

ROUTE_FLOWS: dict[str, list[tuple[str, list[tuple[str, str, str, str | None]]]]] = {
    "celo_to_solana": [("celo→sol", [
        ("hub_celo", "Celo", "USDT", None), ("act", "Buy", TOKEN, "CeloSwap"),
        ("hub_vnx", "VNX", "bridge", "VNX"), ("act", "Sell", TOKEN, "Jupiter"),
        ("hub_sol", "Sol", "USDC", None), ("recon", "Wormhole", "USDT", "WH"),
    ])],
    "solana_to_celo": [("sol→celo", [
        ("hub_sol", "Sol", "USDC", None), ("act", "Buy", TOKEN, "Jupiter"),
        ("hub_vnx", "VNX", "bridge", "VNX"), ("act", "Sell", TOKEN, "CeloSwap"),
        ("hub_celo", "Celo", "USDT", None), ("recon", "Wormhole", "USDT", "WH"),
    ])],
    "base_to_solana": [("base→sol", [
        ("hub_base", "Base", "USDC", None), ("act", "Buy", TOKEN, "Kyber"),
        ("hub_vnx", "VNX", "bridge", "VNX"), ("act", "Sell", TOKEN, "Jupiter"),
        ("hub_sol", "Sol", "USDC", None), ("recon", "Wormhole", "USDC", "WH"),
    ])],
    "solana_to_base": [("sol→base", [
        ("hub_sol", "Sol", "USDC", None), ("act", "Buy", TOKEN, "Jupiter"),
        ("hub_vnx", "VNX", "bridge", "VNX"), ("act", "Sell", TOKEN, "Kyber"),
        ("hub_base", "Base", "USDC", None), ("recon", "Wormhole", "USDC", "WH"),
    ])],
    "celo_to_vnx": [("celo→vnx", [
        ("hub_celo", "Celo", "USDT", None), ("act", "Buy", TOKEN, "CeloSwap"),
        ("hub_vnx", "VNX", "deposit", "VNX"), ("act", "Sell", TOKEN, "Platform"),
        ("hub_vnx", "VNX", "USDC", None), ("recon", "Hub ETH", "WH+swap", "WH"),
    ])],
    "vnx_to_celo": [("vnx→celo", [
        ("hub_vnx", "VNX", "USDC", None), ("act", "Buy", TOKEN, "Platform"),
        ("hub_vnx", "VNX", "withdraw", "VNX"), ("act", "Sell", TOKEN, "CeloSwap"),
        ("hub_celo", "Celo", "USDT", None),
    ])],
    "base_to_vnx": [("base→vnx", [
        ("hub_base", "Base", "USDC", None), ("act", "Buy", TOKEN, "Kyber"),
        ("hub_vnx", "VNX", "deposit", "VNX"), ("act", "Sell", TOKEN, "Platform"),
        ("hub_vnx", "VNX", "USDC", None), ("recon", "Hub ETH", "WH+swap", "WH"),
    ])],
    "vnx_to_base": [("vnx→base", [
        ("hub_vnx", "VNX", "USDC", None), ("act", "Buy", TOKEN, "Platform"),
        ("hub_vnx", "VNX", "withdraw", "VNX"), ("act", "Sell", TOKEN, "Kyber"),
        ("hub_base", "Base", "USDC", None),
    ])],
    "solana_to_vnx": [("sol→vnx", [
        ("hub_sol", "Sol", "USDC", None), ("act", "Buy", TOKEN, "Jupiter"),
        ("hub_vnx", "VNX", "deposit", "VNX"), ("act", "Sell", TOKEN, "Platform"),
        ("hub_vnx", "VNX", "USDC", None), ("recon", "CCTP", "Sol→ETH", "CCTP"),
    ])],
    "vnx_to_solana": [("vnx→sol", [
        ("hub_eth", "ETH", "USDC", None), ("act", "Buy", TOKEN, "Platform"),
        ("hub_vnx", "VNX", "withdraw", "VNX"), ("act", "Sell", TOKEN, "Jupiter"),
        ("hub_sol", "Sol", "USDC", None), ("recon", "CCTP", "ETH→Sol", "CCTP"),
    ])],
}

STYLES = r"""
\documentclass[9pt,a4paper,landscape]{article}
\usepackage[a4paper,landscape,margin=7mm]{geometry}
\usepackage{fontspec}
\usepackage{microtype}
\defaultfontfeatures{Ligatures=TeX}
\IfFontExistsTF{Inter}{\usepackage[sfdefault,tabular]{inter}}{
  \IfFontExistsTF{Helvetica Neue}{\setmainfont{Helvetica Neue}}{\setmainfont{TeX Gyre Heros}}}
\usepackage{xcolor}
\usepackage{tikz}
\usetikzlibrary{arrows.meta}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\definecolor{ink}{RGB}{25,35,55}
\definecolor{primary}{RGB}{0,82,155}
\definecolor{celofill}{RGB}{253,236,200}\definecolor{celostroke}{RGB}{166,124,0}
\definecolor{basefill}{RGB}{232,245,233}\definecolor{basestroke}{RGB}{46,125,50}
\definecolor{solfill}{RGB}{234,218,255}\definecolor{solstroke}{RGB}{122,43,210}
\definecolor{ethfill}{RGB}{221,228,250}\definecolor{ethstroke}{RGB}{67,85,187}
\definecolor{vnxfill}{RGB}{224,242,240}\definecolor{vnxstroke}{RGB}{0,107,98}
\definecolor{callout}{RGB}{255,243,224}\definecolor{calloutstroke}{RGB}{230,126,34}
\tikzset{
  hub_celo/.style={draw=celostroke,thick,fill=celofill,rounded corners=2pt,minimum height=6.5mm,minimum width=12mm,align=center,font=\fontsize{6}{7.5}\selectfont\bfseries,inner sep=0.5pt},
  hub_base/.style={draw=basestroke,thick,fill=basefill,rounded corners=2pt,minimum height=6.5mm,minimum width=12mm,align=center,font=\fontsize{6}{7.5}\selectfont\bfseries,inner sep=0.5pt},
  hub_sol/.style={draw=solstroke,thick,fill=solfill,rounded corners=2pt,minimum height=6.5mm,minimum width=12mm,align=center,font=\fontsize{6}{7.5}\selectfont\bfseries,inner sep=0.5pt},
  hub_eth/.style={draw=ethstroke,thick,fill=ethfill,rounded corners=2pt,minimum height=6.5mm,minimum width=12mm,align=center,font=\fontsize{6}{7.5}\selectfont\bfseries,inner sep=0.5pt},
  hub_vnx/.style={draw=vnxstroke,thick,fill=vnxfill,rounded corners=2pt,minimum height=6.5mm,minimum width=12mm,align=center,font=\fontsize{6}{7.5}\selectfont\bfseries,inner sep=0.5pt},
  act/.style={draw=ink!40,fill=white,rounded corners=2pt,minimum height=6.5mm,minimum width=10mm,align=center,font=\fontsize{5.8}{7}\selectfont,inner sep=0.5pt},
  recon/.style={draw=primary!55,fill=primary!8,dashed,thick,rounded corners=2pt,minimum height=6.5mm,minimum width=11mm,align=center,font=\fontsize{5.5}{7}\selectfont\bfseries,text=primary,inner sep=0.5pt},
  arr/.style={-{Stealth[length=1.4mm]},thick,draw=ink!45},
  rowlbl/.style={anchor=east,font=\fontsize{6}{7.5}\selectfont\bfseries,text=ink!70,minimum width=14mm,align=right},
}
\def\FxA{0}\def\FxB{15}\def\FxC{29}\def\FxD{43}\def\FxE{57}\def\FxF{71}\def\FxG{85}
"""


def _flow_row(y: float, label: str, nodes: list[tuple], active: bool) -> list[str]:
    cols = ["A", "B", "C", "D", "E", "F", "G"]
    lines: list[str] = []
    color = "primary" if active else "ink!40"
    lines.append(rf"\node[rowlbl,text={color}] at (-2,{y}) {{{label}}};")
    prev = None
    for i, (kind, l1, l2, tag) in enumerate(nodes):
        if i >= len(cols):
            break
        nid = f"n{int(y)}_{i}"
        tag_tex = rf"\\{{\fontsize{{4.8}}{{6}}\selectfont\textcolor{{ink!45}}{{{tag}}}}}" if tag else ""
        lines.append(rf"\node[{kind},anchor=west] ({nid}) at (\Fx{cols[i]},{y}) {{{l1}\\{l2}{tag_tex}}};")
        if prev:
            lines.append(rf"\draw[arr] ({prev})--({nid});")
        prev = nid
    return lines


def build_latex() -> str:
    active = set(active_directions())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [STYLES, r"\begin{document}", r"\color{ink}",
        rf"{{\fontsize{{15}}{{17}}\selectfont\bfseries\color{{primary}} {BOT}}}\\[1pt]",
        rf"{{\fontsize{{8.5}}{{10.5}}\selectfont {N_ROUTES} directed {TOKEN} routes · dual EVM hub (Celo USDT + Base USDC)}}\\[1pt]",
        rf"{{\fontsize{{6.5}}{{8}}\selectfont\textcolor{{ink!55}}{{{now} · github.com/Giansensey007/{GITHUB}}}}}",
        r"\vspace{1.5mm}\noindent\rule{\linewidth}{0.3pt}\vspace{1.5mm}",
        r"\noindent\begin{tikzpicture}[x=1mm,y=-1mm]",
        r"\node[anchor=west,font=\fontsize{7}{8.5}\selectfont\bfseries\color{celostroke}] at (0,0) {Celo hub (USDT)};",
        r"\node[hub_celo] (hc) at (22,5) {Celo\\USDT};",
        r"\node[hub_sol] (hs) at (55,5) {Sol\\USDC};",
        r"\node[anchor=west,font=\fontsize{7}{8.5}\selectfont\bfseries\color{basestroke}] at (75,0) {Base hub (USDC)};",
        r"\node[hub_base] (hb) at (97,5) {Base\\USDC};",
        r"\node[hub_eth] (he) at (130,5) {ETH\\USDC};",
        r"\node[hub_vnx] (hv) at (155,5) {VNX\\USDC};",
        r"\draw[arr] (hc)--node[above,font=\fontsize{4.5}{5.5}\selectfont]{VNX VCHF} (hs);",
        r"\draw[arr] (hb)--node[above,font=\fontsize{4.5}{5.5}\selectfont]{VNX VCHF} (hs);",
        r"\draw[arr,dashed] (hc)--node[below,font=\fontsize{4.5}{5.5}\selectfont]{Wormhole} (he);",
        r"\draw[arr,dashed] (hb)--node[below,font=\fontsize{4.5}{5.5}\selectfont]{Wormhole} (he);",
        r"\draw[arr,dashed] (hs)--node[below,font=\fontsize{4.5}{5.5}\selectfont]{CCTP} (he);",
        r"\draw[arr] (he)--node[above,font=\fontsize{4.5}{5.5}\selectfont]{VNX USDC} (hv);",
        r"\draw[arr] (hc)--node[below,font=\fontsize{4.5}{5.5}\selectfont]{VNX} (hv);",
        r"\draw[arr] (hb)--node[below,font=\fontsize{4.5}{5.5}\selectfont]{VNX} (hv);",
        r"\end{tikzpicture}\vspace{2mm}",
        r"\noindent\begin{tikzpicture}[x=1mm,y=-1mm]",
    ]
    y = 0.0
    for direction in ALL_DIRECTIONS:
        for label, nodes in ROUTE_FLOWS.get(direction, []):
            y += 7.5
            lines += _flow_row(y, label, nodes, direction in active)
    lines += [
        r"\end{tikzpicture}",
        r"\vspace{1.5mm}\noindent\rule{\linewidth}{0.3pt}\vspace{1mm}",
        r"\noindent\begin{minipage}[t]{0.48\linewidth}{\fontsize{6.5}{8}\selectfont\bfseries Legend}\\[0.5pt]",
        r"{\fontsize{6}{7.5}\selectfont \textbf{VNX} VCHF bridge + platform · \textbf{Wormhole} stable rebalance · \textbf{CCTP} Sol$\leftrightarrow$ETH}",
        r"\end{minipage}\hfill\begin{minipage}[t]{0.48\linewidth}\raggedleft",
        rf"{{\fontsize{{6.5}}{{8}}\selectfont\bfseries Minimum sizes}}\\[0.5pt]",
        rf"{{\fontsize{{6}}{{7.5}}\selectfont Deposit 5 {TOKEN} · Platform {VNX_MIN:.0f} {TOKEN} · ETH USDC 20 · Deploy 200 {TOKEN}}}",
        r"\end{minipage}",
        r"\vspace{1.5mm}",
        r"\noindent\fcolorbox{calloutstroke}{callout}{\begin{minipage}{0.98\linewidth}",
        r"{\fontsize{6.5}{8}\selectfont\bfseries VNX Ethereum accepts USDC only} --- dual-hub Celo USDT and Base USDC settle via Wormhole to ETH before VNX credit.",
        r"\end{minipage}}",
        r"\end{document}",
    ]
    return "\n".join(lines)


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)
    tex_path = DOCS / f"{PDF_STEM}.tex"
    pdf_path = DOCS / f"{PDF_STEM}.pdf"
    tex_path.write_text(build_latex(), encoding="utf-8")
    print(f"Wrote {tex_path}")
    for _ in range(2):
        proc = subprocess.run(
            ["lualatex", "-interaction=nonstopmode", "-output-directory", str(DOCS), tex_path.name],
            cwd=DOCS, capture_output=True, text=True,
        )
    if not pdf_path.exists():
        print((proc.stdout or "")[-3000:])
        return 1
    print(f"Wrote {pdf_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
