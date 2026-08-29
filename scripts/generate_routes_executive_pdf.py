#!/usr/bin/env python3
"""
Generate docs/gbp-menace-routes-executive.pdf — single-page executive loop deck.

Platform-first, same-asset round-trip model: every route is a loop that starts and
ends with the VNX token on the platform. Content is generated directly from
``src/scanner/routes.catalog_loops()`` so it never drifts from the code.

Usage:
  python scripts/generate_routes_executive_pdf.py
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
sys.path.insert(0, str(ROOT))

from src.config_loader import load_tokens
from src.scanner.routes import (
    LOOP1_OUTBOUND,
    LOOP2_INBOUND,
    LOOP3_CROSS,
    _bot_token,
    catalog_loops,
)

# ---- per-bot constants -------------------------------------------------------
PDF_STEM = "vchf-menace-routes-executive"
BOT_NAME = "VCHF"
# ------------------------------------------------------------------------------

FAMILY_LABEL = {
    LOOP1_OUTBOUND: "Loop 1 · Outbound",
    LOOP2_INBOUND: "Loop 2 · Inbound",
    LOOP3_CROSS: "Loop 3 · Cross",
}
CHAIN_DISP = {"celo": "Celo", "base": "Base", "solana": "Solana", "ethereum": "Ethereum"}
# bridge mechanism -> (label, colour)
BRIDGE = {
    "cctp": ("CCTP", "routeb"),
    "wormhole": ("Wormhole", "primary"),
    "eth_triangle": ("ETH 2-hop", "warnstroke"),
    "none": ("direct (ETH)", "profit"),
}


def _disp(chain: str) -> str:
    return CHAIN_DISP.get(chain, chain.title())


def _mech(loop) -> str:
    mechs = sorted({s.mechanism for s in loop.bridge_legs if s.mechanism})
    if not mechs:
        return "none"
    return mechs[0] if len(mechs) == 1 else "+".join(mechs)


def collect():
    tok = _bot_token(load_tokens())
    loops = catalog_loops(tok)
    rows = []
    for lp in loops:
        route = (
            f"{_disp(lp.chain_a)} $\\rightarrow$ {_disp(lp.chain_b)}"
            if lp.chain_b
            else _disp(lp.chain_a)
        )
        rows.append((lp.family, FAMILY_LABEL[lp.family], route, _mech(lp), len(lp.steps())))
    has_eth_trading = any(
        lp.chain_a == "ethereum" and lp.family in (LOOP1_OUTBOUND, LOOP2_INBOUND)
        for lp in loops
    )
    return tok.symbol, rows, has_eth_trading


PREAMBLE = r"""
% !TeX program = pdflatex
\documentclass[10pt,a4paper,landscape]{article}
\usepackage[a4paper,landscape,margin=9mm]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{tgheros}
\renewcommand*\familydefault{\sfdefault}
\usepackage{microtype}
\usepackage{xcolor}
\usepackage{array}
\usepackage{booktabs}
\usepackage{adjustbox}
\usepackage{tikz}
\usetikzlibrary{arrows.meta}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\definecolor{ink}{RGB}{25,35,55}
\definecolor{surface}{RGB}{245,248,252}
\definecolor{primary}{RGB}{0,82,155}
\definecolor{profit}{RGB}{21,122,78}
\definecolor{profitbg}{RGB}{220,245,230}
\definecolor{warnstroke}{RGB}{197,48,48}
\definecolor{routeb}{RGB}{88,55,160}
\newcommand{\statlabel}[1]{{\fontsize{6.5}{8}\selectfont\bfseries\textls[120]{\textcolor{ink!55}{\MakeUppercase{#1}}}}}
\newcommand{\bridgebadge}[2]{\tikz[baseline=(b.base)]{\node[draw=#2,fill=#2!10,rounded corners=1mm,inner xsep=1.6mm,inner ysep=0.5mm](b){{\fontsize{6.6}{8}\selectfont\bfseries\textcolor{#2}{#1}}};}}
\newcommand{\stepnum}[1]{\tikz[baseline=(s.base)]{\node[circle,fill=primary,inner sep=0.5mm,minimum size=3.3mm](s){{\fontsize{5.8}{6.2}\selectfont\bfseries\color{white}#1}};}}
"""


def _family_card(title: str, color: str, steps: list[str]) -> list[str]:
    out = [
        rf"\noindent\fcolorbox{{{color}!35}}{{{color}!6}}{{\begin{{minipage}}{{\dimexpr\linewidth-2\fboxsep-2\fboxrule\relax}}",
        rf"{{\fontsize{{8.5}}{{10}}\selectfont\bfseries\color{{{color}}} {title}}}\\[1.2mm]",
        r"{\fontsize{7}{9}\selectfont",
    ]
    for i, st in enumerate(steps, 1):
        out.append(rf"\stepnum{{{i}}}~{st}\\[0.5mm]")
    out.append(r"}\end{minipage}}")
    return out


def build_latex(token: str, rows, has_eth: bool) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n1 = sum(1 for r in rows if r[0] == LOOP1_OUTBOUND)
    n2 = sum(1 for r in rows if r[0] == LOOP2_INBOUND)
    n3 = sum(1 for r in rows if r[0] == LOOP3_CROSS)
    total = len(rows)
    tally: dict[str, int] = {}
    for _f, _l, _r, mech, _s in rows:
        tally[mech] = tally.get(mech, 0) + 1
    used = [m for m in ("cctp", "wormhole", "eth_triangle", "none") if m in tally]
    bridges_short = " · ".join(BRIDGE[m][0] for m in used)

    eth_note = r" \textit{(skipped if X = ETH)}" if has_eth else ""

    lines = [
        PREAMBLE.strip(),
        r"\begin{document}",
        r"\color{ink}",
        rf"{{\fontsize{{16}}{{18}}\selectfont\bfseries\color{{primary}} {BOT_NAME} Menace --- Loop Routes}}"
        rf"~{{\fontsize{{10}}{{12}}\selectfont\textcolor{{ink!55}}{{({token})}}}}\\[1pt]",
        r"{\fontsize{8.5}{10.5}\selectfont Platform-first treasury · same-asset round trips · "
        rf"{token} held on VNX · chains hold hub stables only}}\\[1pt]",
        rf"{{\fontsize{{6.8}}{{8.2}}\selectfont\textcolor{{ink!55}}{{{now} · docs/{PDF_STEM}.pdf · generated from src/scanner/routes.active\_loops()}}}}",
        r"\vspace{1.5mm}\noindent\rule{\linewidth}{0.3pt}\vspace{1.5mm}",
        # stat strip
        r"\noindent\begin{tabular}{@{}>{\centering\arraybackslash}p{0.185\linewidth}"
        r">{\centering\arraybackslash}p{0.185\linewidth}"
        r">{\centering\arraybackslash}p{0.185\linewidth}"
        r">{\centering\arraybackslash}p{0.185\linewidth}"
        r">{\centering\arraybackslash}p{0.185\linewidth}@{}}",
        rf"\fcolorbox{{profit!35}}{{profitbg}}{{\begin{{minipage}}[c][11mm][c]{{0.9\linewidth}}\statlabel{{Loops}}{{\fontsize{{11}}{{13}}\selectfont\bfseries\textcolor{{profit}}{{{total}}}}}\end{{minipage}}}} &",
        rf"\fcolorbox{{primary!25}}{{surface}}{{\begin{{minipage}}[c][11mm][c]{{0.9\linewidth}}\statlabel{{Loop 1}}{{\fontsize{{10}}{{12}}\selectfont\bfseries {n1}}}\end{{minipage}}}} &",
        rf"\fcolorbox{{primary!25}}{{surface}}{{\begin{{minipage}}[c][11mm][c]{{0.9\linewidth}}\statlabel{{Loop 2}}{{\fontsize{{10}}{{12}}\selectfont\bfseries {n2}}}\end{{minipage}}}} &",
        rf"\fcolorbox{{primary!25}}{{surface}}{{\begin{{minipage}}[c][11mm][c]{{0.9\linewidth}}\statlabel{{Loop 3}}{{\fontsize{{10}}{{12}}\selectfont\bfseries {n3}}}\end{{minipage}}}} &",
        rf"\fcolorbox{{primary!25}}{{surface}}{{\begin{{minipage}}[c][11mm][c]{{0.9\linewidth}}\statlabel{{Bridges}}{{\fontsize{{7}}{{8.5}}\selectfont\bfseries {bridges_short}}}\end{{minipage}}}} \\",
        r"\end{tabular}",
        r"\vspace{1.6mm}\noindent\rule{\linewidth}{0.3pt}\vspace{1.5mm}",
        # two columns
        r"\noindent\begin{minipage}[t]{0.46\linewidth}",
        r"{\fontsize{9}{11}\selectfont\bfseries\color{primary} Loop families}\\[1.5mm]",
    ]
    lines += _family_card(
        "Loop 1 · Outbound", "primary",
        [
            r"Withdraw token: \textbf{platform $\rightarrow$ chain X}",
            r"Sell token for stable on chain X",
            rf"Bridge stable \textbf{{X $\rightarrow$ ETH hub}}{eth_note}",
            r"Deposit USDC \textbf{ETH $\rightarrow$ VNX}",
            r"\textbf{Platform buy-back} $\Rightarrow$ same token on platform",
        ],
    )
    lines.append(r"\\[1.4mm]")
    lines += _family_card(
        "Loop 2 · Inbound", "profit",
        [
            r"Platform sell token for USDC",
            r"Withdraw USDC \textbf{platform $\rightarrow$ ETH}",
            rf"Bridge stable \textbf{{ETH $\rightarrow$ chain X}}{eth_note}",
            r"\textbf{On-chain buy-back} token on chain X",
            r"Deposit token \textbf{X $\rightarrow$ VNX}",
        ],
    )
    lines.append(r"\\[1.4mm]")
    lines += _family_card(
        "Loop 3 · Cross", "routeb",
        [
            r"Withdraw token: \textbf{platform $\rightarrow$ chain A}",
            r"Sell token for stable on chain A",
            r"\textbf{Direct bridge} stable \textbf{A $\rightarrow$ B} (CCTP preferred)",
            r"\textbf{On-chain buy-back} token on chain B",
            r"Deposit token \textbf{B $\rightarrow$ VNX}",
        ],
    )
    lines += [
        r"\end{minipage}\hfill\begin{minipage}[t]{0.50\linewidth}",
        r"{\fontsize{9}{11}\selectfont\bfseries\color{primary} Loops}\\[1.5mm]",
        r"{\fontsize{7.4}{9.4}\selectfont\renewcommand{\arraystretch}{1.25}",
        r"\adjustbox{max width=\linewidth}{",
        r"\begin{tabular}{@{}clll@{}}\toprule",
        r"\textbf{\#} & \textbf{Family} & \textbf{Route} & \textbf{Bridge} \\\midrule",
    ]
    for i, (_fam, fam_label, route, mech, _steps) in enumerate(rows, 1):
        label, color = BRIDGE[mech]
        lines.append(rf"{i} & {fam_label} & {route} & \bridgebadge{{{label}}}{{{color}}} \\")
    lines += [
        r"\bottomrule\end{tabular}}}",
        r"\\[3mm]",
        r"{\fontsize{8.5}{10.5}\selectfont\bfseries\color{primary} Bridge legend}\\[1.5mm]",
        r"\begin{tikzpicture}[x=1mm,y=1mm,baseline=(a.base)]",
    ]
    legend_text = {
        "cctp": r"Circle USDC $\leftrightarrow$ USDC direct (preferred)",
        "wormhole": r"Any Celo USDT stable leg",
        "eth_triangle": r"Celo $\leftrightarrow$ Base cross-stable via ETH hub",
        "none": r"No bridge --- USDC already on ETH",
    }
    y = 0.0
    first = True
    for m in used:
        label, color = BRIDGE[m]
        anchor = "(a)" if first else ""
        node_id = "(a)" if first else ""
        lines.append(
            rf"\node[anchor=west]{node_id} at(0,{y}){{\bridgebadge{{{label}}}{{{color}}}}};"
            rf"\node[anchor=west,font=\fontsize{{6.6}}{{8}}\selectfont]at(19,{y}){{{legend_text[m]}}};"
        )
        first = False
        y -= 5.0
    lines += [
        r"\end{tikzpicture}",
        r"\end{minipage}",
        r"\vfill",
        r"{\fontsize{6.4}{7.8}\selectfont\textcolor{ink!55}{Same asset in / same asset out · bot never opens inventory · buy-backs only close loops · "
        r"live execution gated by \texttt{ENABLE\_LOOP\_PIPELINE} + \texttt{ENABLE\_LOOP\_EXECUTOR}.}}",
        r"\end{document}",
    ]
    return "\n".join(lines)


def main() -> int:
    token, rows, has_eth = collect()
    tex_path = DOCS / f"{PDF_STEM}.tex"
    pdf_path = DOCS / f"{PDF_STEM}.pdf"
    tex_path.write_text(build_latex(token, rows, has_eth), encoding="utf-8")
    print(f"Wrote {tex_path} ({len(rows)} loops)")

    pdf_path.unlink(missing_ok=True)
    proc = None
    for _ in range(2):
        proc = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-output-directory", str(DOCS), tex_path.name],
            cwd=DOCS,
            capture_output=True,
            text=True,
            errors="replace",
        )
    if not pdf_path.exists():
        print((proc.stdout if proc else "")[-3000:])
        print("pdflatex failed", file=sys.stderr)
        return 1
    print(f"Wrote {pdf_path} ({pdf_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
