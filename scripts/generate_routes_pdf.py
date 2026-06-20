#!/usr/bin/env python3
"""
Generate VCHF Menace route PDFs.

Usage:
  python scripts/generate_routes_pdf.py              # full route doc (pdflatex)
  python scripts/generate_routes_pdf.py --live       # include live round-trip PnL
  python scripts/generate_routes_pdf.py --executive  # one-page executive overview (lualatex)
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.config_loader import load_bot_config
from src.scanner.routes import (
    ALL_DIRECTIONS,
    CCTP_SOL_USDC_TO_VNX,
    active_directions,
    route_for_direction,
)
from src.scanner.simulator import VNX_MIN_VCHF
from src.treasury.loops import (
    LEG_END_STABLE,
    inverse_direction,
    return_leg_direction,
    use_cctp_usdc_return,
)
from src.vnx.deposits import min_deposit_usdc, min_deposit_vchf


def _texttt(s: str) -> str:
    return r"\texttt{" + s.replace("_", r"\_").replace("#", r"\#") + "}"


def _tex(s: str) -> str:
    return (
        s.replace("\\", "\\textbackslash{}")
        .replace("&", "\\&")
        .replace("%", "\\%")
        .replace("$", "\\$")
        .replace("#", "\\#")
        .replace("_", "\\_")
        .replace("↔", "$\\leftrightarrow$")
        .replace("→", "$\\rightarrow$")
    )


ROUTE_STEPS: dict[str, list[str]] = {
    "celo_to_solana": [
        "Spend Celo USDT $\\rightarrow$ buy VCHF (CeloSwap)",
        "Deposit VCHF to VNX (CELO, min 5 VCHF cumulative)",
        "Withdraw VCHF to Solana",
        "Sell VCHF for Sol USDC (Jupiter)",
        "Wormhole USDT rebalance Celo $\\leftrightarrow$ Sol (stable leg)",
    ],
    "solana_to_celo": [
        "Spend Sol USDC $\\rightarrow$ buy VCHF (Jupiter)",
        "Deposit VCHF to VNX (SOL, min 5 VCHF)",
        "Withdraw VCHF to Celo",
        "Sell VCHF for Celo USDT (CeloSwap)",
        "Wormhole USDT rebalance",
    ],
    "base_to_solana": [
        "Spend Base USDC $\\rightarrow$ buy VCHF (KyberSwap)",
        "Deposit VCHF to VNX (BASE, min 5 VCHF cumulative)",
        "Withdraw VCHF to Solana",
        "Sell VCHF for Sol USDC (Jupiter)",
        "Wormhole USDC rebalance Base $\\leftrightarrow$ Sol (stable leg)",
    ],
    "solana_to_base": [
        "Spend Sol USDC $\\rightarrow$ buy VCHF (Jupiter)",
        "Deposit VCHF to VNX (SOL, min 5 VCHF)",
        "Withdraw VCHF to Base",
        "Sell VCHF for Base USDC (KyberSwap)",
        "Wormhole USDC rebalance",
    ],
    "celo_to_vnx": [
        "Spend Celo USDT $\\rightarrow$ buy VCHF on Celo (CeloSwap)",
        "Deposit VCHF to VNX platform (CELO)",
        "Platform sell VCHF for USDC",
        "Hub return: Wormhole Celo USDT $\\rightarrow$ ETH USDC $\\rightarrow$ VNX deposit",
    ],
    "vnx_to_celo": [
        "Platform buy VCHF (min 30 VCHF order)",
        "Withdraw VCHF to Celo",
        "Sell VCHF for Celo USDT (CeloSwap)",
    ],
    "base_to_vnx": [
        "Spend Base USDC $\\rightarrow$ buy VCHF on Base (KyberSwap)",
        "Deposit VCHF to VNX platform (BASE)",
        "Platform sell VCHF for USDC",
        "Hub return: Wormhole Base USDC $\\rightarrow$ ETH $\\rightarrow$ VNX deposit",
    ],
    "vnx_to_base": [
        "Platform buy VCHF (min 30 VCHF order)",
        "Withdraw VCHF to Base",
        "Sell VCHF for Base USDC (KyberSwap)",
    ],
    "solana_to_vnx": [
        "Spend Sol USDC $\\rightarrow$ buy VCHF (Jupiter)",
        "Deposit VCHF to VNX (SOL, min 5 VCHF)",
        "Platform sell VCHF for USDC",
        "CCTP reconcile (Sol USDC $\\leftrightarrow$ ETH USDC probe)",
    ],
    "vnx_to_solana": [
        "Platform buy VCHF (min 30 VCHF order)",
        "Withdraw VCHF to Solana",
        "Sell VCHF for Sol USDC (Jupiter)",
    ],
    CCTP_SOL_USDC_TO_VNX: [
        "Burn Sol USDC via Circle CCTP $\\rightarrow$ Ethereum",
        "Claim USDC on ETH hot wallet",
        f"Deposit USDC to VNX platform (ETH, min {min_deposit_usdc('ETH'):.0f} USDC)",
        "Platform buy VCHF with credited USDC",
        "\\textit{Return leg after vnx\\_to\\_solana when origin = platform}",
    ],
}

HUB_ROUTES = [
    ("eth\\_to\\_vnx", "ETH USDC $\\rightarrow$ VNX platform USDC deposit"),
    ("vnx\\_to\\_eth", "VNX platform USDC $\\rightarrow$ ETH hot wallet withdraw"),
    ("cctp\\_sol\\_to\\_eth", "Sol USDC $\\rightarrow$ ETH USDC (Circle CCTP)"),
    ("cctp\\_eth\\_to\\_sol", "ETH USDC $\\rightarrow$ Sol USDC (Circle CCTP)"),
    ("wormhole\\_celo\\_to\\_eth", "Celo USDT $\\rightarrow$ ETH USDT (Wormhole) + USDT$\\rightarrow$USDC swap"),
    ("wormhole\\_eth\\_to\\_celo", "ETH USDT $\\rightarrow$ Celo USDT (Wormhole)"),
    ("wormhole\\_base\\_to\\_eth", "Base USDC $\\rightarrow$ ETH USDC (Wormhole)"),
    ("wormhole\\_eth\\_to\\_base", "ETH USDC $\\rightarrow$ Base USDC (Wormhole)"),
    ("celo\\_usdt\\_to\\_vnx\\_usdc", "Celo USDT $\\rightarrow$ Wormhole $\\rightarrow$ ETH USDC $\\rightarrow$ VNX USDC"),
    ("base\\_usdc\\_to\\_vnx\\_usdc", "Base USDC $\\rightarrow$ Wormhole $\\rightarrow$ ETH USDC $\\rightarrow$ VNX USDC"),
]


async def _live_scan() -> list[dict]:
    from src.config_loader import load_chains, load_tokens
    from src.quotes.http_client import build_client
    from src.scanner.simulator import simulate_round_trip
    from src.treasury.loops import origin_for_direction

    cfg = load_bot_config()
    chains = load_chains()
    token = load_tokens()["VCHF"]
    active = set(active_directions(cfg))
    rows: list[dict] = []

    async with build_client() as client:
        for size in (VNX_MIN_VCHF + 1.0, cfg.min_trade_vchf):
            for direction in ALL_DIRECTIONS:
                origin = origin_for_direction(direction)
                rt = await simulate_round_trip(
                    client, chains, token, cfg, direction, size, origin=origin
                )
                ret_p = rt.return_sim.net_profit_usd if rt.return_sim else 0.0
                rows.append(
                    {
                        "size": size,
                        "direction": direction,
                        "active": direction in active,
                        "origin": origin,
                        "primary_p": rt.primary.net_profit_usd,
                        "return_dir": rt.return_direction or "—",
                        "return_p": ret_p,
                        "round_p": rt.round_trip_profit_usd,
                        "go": rt.profitable,
                    }
                )
    return rows


def _build_latex(live_rows: list[dict] | None) -> str:
    cfg = load_bot_config()
    active = set(active_directions(cfg))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        r"\documentclass[11pt,a4paper]{article}",
        r"\usepackage[margin=18mm]{geometry}",
        r"\usepackage[T1]{fontenc}",
        r"\usepackage{lmodern}",
        r"\usepackage{booktabs}",
        r"\usepackage{longtable}",
        r"\usepackage{array}",
        r"\usepackage{xcolor}",
        r"\usepackage{hyperref}",
        r"\usepackage{enumitem}",
        r"\definecolor{accent}{HTML}{1a365d}",
        r"\definecolor{muted}{HTML}{4a5568}",
        r"\definecolor{okgreen}{HTML}{276749}",
        r"\definecolor{warnred}{HTML}{c53030}",
        r"\hypersetup{colorlinks=true,linkcolor=accent,urlcolor=accent}",
        r"\pagestyle{empty}",
        r"\begin{document}",
        r"\begin{center}",
        r"{\LARGE\bfseries\color{accent} VCHF Menace --- Route Map}\\[6pt]",
        rf"{{\color{{muted}}\small Generated {now} · github.com/Giansensey007/vchf\_menace}}\\[12pt]",
        r"\end{center}",
        r"\section*{Configuration snapshot}",
        r"\begin{tabular}{@{}ll@{}}",
        rf"Treasury VCHF home & platform-only (idle VCHF on VNX) \\",
        rf"Closed loop & after every arb (return leg always runs) \\",
        rf"Trade size & {cfg.min_trade_vchf:.0f}--{cfg.max_trade_vchf:.0f} VCHF \\",
        rf"Min profit & \${cfg.min_profit_usd:.2f} round-trip \\",
        rf"VNX platform order min & {VNX_MIN_VCHF:.0f} VCHF \\",
        rf"VNX deposit min (CELO/BASE/SOL VCHF) & 5 VCHF cumulative \\",
        rf"VNX deposit min (ETH USDC) & {min_deposit_usdc('ETH'):.0f} USDC \\",
        rf"enable\_vnx\_cctp\_routes & {str(cfg.enable_vnx_cctp_routes).lower()} \\",
        rf"enable\_vnx\_arb\_routes & {str(cfg.enable_vnx_arb_routes).lower()} \\",
        r"\end{tabular}",
        r"\section*{Chain inventory model}",
        r"\begin{itemize}[nosep]",
        r"\item \textbf{Platform (VNX):} all idle VCHF + USDC for \texttt{vnx\_to\_*} buys",
        r"\item \textbf{Celo:} USDT only (no idle VCHF; dust $\leq$ 0.5 VCHF swept to platform)",
        r"\item \textbf{Base:} USDC only (no idle VCHF)",
        r"\item \textbf{Solana:} USDC only (no idle VCHF)",
        r"\item \textbf{Ethereum:} USDC/USDT hub buffers + gas (no VCHF)",
        r"\end{itemize}",
        rf"\section*{{Arbitrage routes ({len(ALL_DIRECTIONS)} directed pairs)}}",
    ]

    for direction in ALL_DIRECTIONS:
        route = route_for_direction(direction)
        if not route:
            continue
        end = LEG_END_STABLE.get(direction, ("?", "?"))
        inv = inverse_direction(direction)
        origin = route.buy_chain
        ret = return_leg_direction(origin, direction, enable_cctp=cfg.enable_vnx_cctp_routes)
        status = "ACTIVE" if direction in active else "DISABLED"
        color = "okgreen" if direction in active else "warnred"
        lines.append(rf"\subsection*{{\textcolor{{{color}}}{{{_tex(direction)}}} ({status})}}")
        lines.append(r"\begin{tabular}{@{}ll@{}}")
        lines.append(rf"Group & {_tex(route.route_group)} \\")
        lines.append(rf"Buy leg & {_tex(route.buy_chain)} \\")
        lines.append(rf"Sell leg & {_tex(route.sell_chain)} \\")
        lines.append(rf"Ends on & {_tex(end[0])} {_tex(end[1].upper())} \\")
        lines.append(rf"Legacy inverse & {_tex(inv or '—')} \\")
        if ret == CCTP_SOL_USDC_TO_VNX:
            lines.append(rf"Closed-loop return & \textbf{{{_tex(CCTP_SOL_USDC_TO_VNX)}}} (CCTP USDC path) \\")
        else:
            lines.append(rf"Closed-loop return & {_tex(ret or inv or '—')} \\")
        lines.append(r"\end{tabular}")
        lines.append(r"\begin{enumerate}[nosep,leftmargin=*]")
        for step in ROUTE_STEPS.get(direction, []):
            lines.append(rf"\item {step}")
        lines.append(r"\end{enumerate}")

    lines.extend(
        [
            r"\section*{Synthetic return route}",
            rf"\subsection*{{{_tex(CCTP_SOL_USDC_TO_VNX)}}}",
            r"\begin{enumerate}[nosep,leftmargin=*]",
        ]
    )
    for step in ROUTE_STEPS[CCTP_SOL_USDC_TO_VNX]:
        lines.append(rf"\item {step}")
    lines.append(r"\end{enumerate}")

    lines.extend(
        [
            r"\section*{Closed-loop matrix}",
            r"\small",
            r"\begin{longtable}{@{}llll@{}}",
            r"\toprule",
            r"Origin & Primary & Return leg & Ends on \\",
            r"\midrule",
            r"\endhead",
            (r"Celo USDT & celo\_to\_solana & solana\_to\_celo & Celo USDT \\"),
            (r"Celo USDT & celo\_to\_vnx & vnx\_to\_celo & Celo USDT \\"),
            (r"Base USDC & base\_to\_solana & solana\_to\_base & Base USDC \\"),
            (r"Base USDC & base\_to\_vnx & vnx\_to\_base & Base USDC \\"),
            (r"Sol USDC & solana\_to\_vnx & vnx\_to\_solana & Sol USDC \\"),
            (r"Sol USDC & solana\_to\_celo & celo\_to\_solana & Sol USDC \\"),
            (r"Sol USDC & solana\_to\_base & base\_to\_solana & Sol USDC \\"),
            (r"Platform & vnx\_to\_celo & celo\_to\_vnx & Platform USDC \\"),
            (r"Platform & vnx\_to\_base & base\_to\_vnx & Platform USDC \\"),
            (
                r"Platform & vnx\_to\_solana & \textbf{cctp\_sol\_usdc\_to\_vnx} & Platform VCHF \\"
            ),
            r"\bottomrule",
            r"\end{longtable}",
            r"\normalsize",
            r"\section*{Hub \& rebalance routes (matrix test steps, not scanner arb)}",
            r"\begin{tabular}{@{}ll@{}}",
            r"\toprule",
            r"Step ID & Flow \\",
            r"\midrule",
        ]
    )
    for step_id, flow in HUB_ROUTES:
        lines.append(rf"{step_id} & {flow} \\")
    lines.append(r"\bottomrule")
    lines.append(r"\end{tabular}")

    if live_rows:
        lines.extend(
            [
                r"\section*{Live round-trip simulation}",
                r"\small",
                r"\begin{longtable}{@{}rrlrrrrc@{}}",
                r"\toprule",
                r"Size & Dir & Act & Primary\$ & Return & Ret\$ & Round\$ & Go \\",
                r"\midrule",
                r"\endhead",
            ]
        )
        for r in live_rows:
            act = "Y" if r["active"] else "N"
            go = "Y" if r["go"] else "n"
            lines.append(
                rf"{r['size']:.0f} & {_texttt(r['direction'])} & {act} & "
                rf"{r['primary_p']:+.2f} & {_texttt(str(r['return_dir']))} & "
                rf"{r['return_p']:+.2f} & {r['round_p']:+.2f} & {go} \\"
            )
        lines.extend([r"\bottomrule", r"\end{longtable}", r"\normalsize"])
    else:
        lines.append(
            r"\section*{Live PnL}" + "\n"
            r"\textit{Run \texttt{python scripts/generate\_routes\_pdf.py --live} to embed current quotes.}"
        )

    lines.extend(
        [
            r"\vfill",
            r"\begin{center}",
            r"\color{muted}\small Scanner uses fixed-size VNX economics + CCTP return for platform closed loops.",
            r"\end{center}",
            r"\end{document}",
        ]
    )
    return "\n".join(lines)


# ── Executive one-pager (dual-hub Celo + Base, 10 routes) ─────────────────────

_HUB_STYLE = {
    "celo": ("celostroke", "celofill", "Celo"),
    "base": ("basestroke", "basefill", "Base"),
    "solana": ("solstroke", "solfill", "Sol"),
    "vnx": ("vnxstroke", "vnxfill", "VNX"),
    "ethereum": ("ethstroke", "ethfill", "ETH"),
}

_STABLE_LABEL = {
    ("celo", "usdt"): "USDT",
    ("base", "usdc"): "USDC",
    ("solana", "usdc"): "USDC",
    ("vnx", "usdc"): "USDC",
    ("ethereum", "usdc"): "USDC",
}

_EXEC_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Celo $\\leftrightarrow$ Sol", "celostroke", ("celo_to_solana", "solana_to_celo")),
    ("Base $\\leftrightarrow$ Sol", "basestroke", ("base_to_solana", "solana_to_base")),
    ("Celo $\\leftrightarrow$ VNX", "celostroke", ("celo_to_vnx", "vnx_to_celo")),
    ("Base $\\leftrightarrow$ VNX", "basestroke", ("base_to_vnx", "vnx_to_base")),
    ("Sol $\\leftrightarrow$ VNX", "solstroke", ("solana_to_vnx", "vnx_to_solana")),
)


def _exec_direction_label(direction: str) -> str:
    buy, sell = direction.split("_to_")
    short = {"solana": "sol", "ethereum": "eth"}.get(buy, buy)
    short2 = {"solana": "sol", "ethereum": "eth"}.get(sell, sell)
    return rf"{short}$\rightarrow${short2}"


def _exec_rebalance(direction: str) -> str:
    route = route_for_direction(direction)
    if not route:
        return ""
    if route.route_group in ("celo_sol", "base_sol"):
        hub = "Celo" if route.route_group == "celo_sol" else "Base"
        return rf"Wormhole {hub}$\leftrightarrow$Sol"
    if route.route_group == "vnx_sol":
        if direction == "vnx_to_solana":
            return r"CCTP Sol$\rightarrow$ETH$\rightarrow$VNX"
        return r"VNX + deposit"
    return r"VNX platform"


def _exec_flow_cells(direction: str) -> tuple[str, list[str], str, str]:
    """Return (start hub tex, middle steps, end hub tex, rebalance tex)."""
    route = route_for_direction(direction)
    if not route:
        return "", [], "", ""
    end = LEG_END_STABLE.get(direction, ("?", "?"))
    buy_style = _HUB_STYLE.get(route.buy_chain, ("ink", "surface", route.buy_chain))
    buy_stable = _STABLE_LABEL.get((route.buy_chain, "usdt" if route.buy_chain == "celo" else "usdc"), "?")
    end_stable = _STABLE_LABEL.get((end[0], end[1]), end[1].upper())

    start = rf"{buy_style[2]}\\{buy_stable}"

    if route.buy_chain == "vnx":
        steps = [r"VNX buy", r"Withdraw", r"Sell VCHF"]
    elif route.sell_chain == "vnx":
        steps = [r"Buy VCHF", r"VNX dep.", r"VNX sell"]
    else:
        steps = [r"Buy VCHF", r"VNX br.", r"Sell VCHF"]

    end_hub = _HUB_STYLE.get(end[0], ("ink", "surface", end[0]))
    finish = rf"{end_hub[2]}\\{end_stable}"
    return start, steps, finish, _exec_rebalance(direction)


def _exec_route_row(y: float, direction: str, *, active: bool) -> list[str]:
    label = _exec_direction_label(direction)
    start, steps, finish, rebalance = _exec_flow_cells(direction)
    route = route_for_direction(direction)
    if not route:
        return []
    buy_chain = route.buy_chain
    stroke, fill, _ = _HUB_STYLE.get(buy_chain, ("ink", "surface", buy_chain))
    act_style = stroke
    dim = "" if active else r", opacity=0.45"
    nodes: list[str] = []
    prefix = direction.replace("_", "")
    nodes.append(rf"\node[rowlbl{dim}] at (-2,{y}) {{{label}}};")
    nodes.append(
        rf"\node[hub={stroke}, fill={fill}{dim}, anchor=west] ({prefix}s) at (\FxA,{y}) {{{start}}};"
    )
    cols = ["B", "C", "D"]
    prev = f"{prefix}s"
    end_chain = LEG_END_STABLE[direction][0]
    end_stroke, end_fill, _ = _HUB_STYLE.get(end_chain, ("ink", "surface", end_chain))
    for i, step in enumerate(steps):
        col = cols[i]
        nid = f"{prefix}{col.lower()}"
        nodes.append(
            rf"\node[act={act_style}{dim}, anchor=west] ({nid}) at (\Fx{col},{y}) {{{step}}};"
        )
        nodes.append(rf"\draw[arr{dim}] ({prev})--({nid});")
        prev = nid
    nodes.append(
        rf"\node[hub={end_stroke}, fill={end_fill}{dim}, anchor=west] ({prefix}e) at (\FxE,{y}) {{{finish}}};"
    )
    nodes.append(rf"\draw[arr{dim}] ({prev})--({prefix}e);")
    nodes.append(
        rf"\node[recon{dim}, anchor=west] ({prefix}r) at (\FxR,{y}) {{{rebalance}}};"
    )
    nodes.append(rf"\draw[arr{dim}] ({prefix}e)--({prefix}r);")
    return nodes


def _build_executive_latex() -> str:
    cfg = load_bot_config()
    active = set(active_directions(cfg))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n_active = len(active)
    subtitle = (
        f"10 directed routes · {n_active}/10 active · closed-loop return after every arb"
    )
    trade_range = f"{cfg.min_trade_vchf:.0f}\\,--\\,{cfg.max_trade_vchf:.0f}"
    min_profit = f"\\$\\geq\\${cfg.min_profit_usd:.2f}"
    poll_sec = f"{cfg.poll_interval_sec:.0f}\\,s"
    cfg_flags = (
        f"\\texttt{{enable\\_vnx\\_arb\\_routes}}={str(cfg.enable_vnx_arb_routes).lower()} · "
        f"\\texttt{{enable\\_vnx\\_cctp\\_routes}}={str(cfg.enable_vnx_cctp_routes).lower()}"
    )

    y = 7.0
    row_step = 5.0
    group_gap = 2.5
    flow_lines: list[str] = []
    for gi, (group_title, group_color, directions) in enumerate(_EXEC_GROUPS):
        if gi:
            y += group_gap
        flow_lines.append(
            rf"\node[anchor=west, font=\fontsize{{8}}{{9.5}}\selectfont\bfseries\color{{{group_color}}}] "
            rf"at (0,{y - 2.8}) {{{group_title}}};"
        )
        for direction in directions:
            flow_lines.extend(_exec_route_row(y, direction, active=direction in active))
            y += row_step

    ymax = y + 2

    return "\n".join(
        [
            r"% !TeX program = lualatex",
            r"\documentclass[9pt,a4paper,landscape]{article}",
            r"\usepackage[a4paper,landscape,margin=8mm]{geometry}",
            r"\usepackage{fontspec}",
            r"\usepackage{microtype}",
            r"\defaultfontfeatures{Ligatures=TeX}",
            r"\IfFontExistsTF{Inter}{\usepackage[sfdefault,tabular]{inter}}{",
            r"  \IfFontExistsTF{Helvetica Neue}{\setmainfont{Helvetica Neue}}{",
            r"    \setmainfont{TeX Gyre Heros}",
            r"  }",
            r"}",
            r"\usepackage{xcolor}",
            r"\usepackage{array}",
            r"\usepackage{adjustbox}",
            r"\usepackage{tikz}",
            r"\usetikzlibrary{arrows.meta,calc,positioning}",
            r"\pagestyle{empty}",
            r"\setlength{\parindent}{0pt}",
            r"\definecolor{ink}{RGB}{25,35,55}",
            r"\definecolor{surface}{RGB}{245,248,252}",
            r"\definecolor{primary}{RGB}{0,82,155}",
            r"\definecolor{celofill}{RGB}{253,236,200}",
            r"\definecolor{celostroke}{RGB}{166,124,0}",
            r"\definecolor{basefill}{RGB}{214,234,248}",
            r"\definecolor{basestroke}{RGB}{0,82,155}",
            r"\definecolor{solfill}{RGB}{234,218,255}",
            r"\definecolor{solstroke}{RGB}{122,43,210}",
            r"\definecolor{ethfill}{RGB}{221,228,250}",
            r"\definecolor{ethstroke}{RGB}{67,85,187}",
            r"\definecolor{vnxfill}{RGB}{224,242,240}",
            r"\definecolor{vnxstroke}{RGB}{0,107,98}",
            r"\definecolor{profit}{RGB}{21,122,78}",
            r"\definecolor{profitbg}{RGB}{220,245,230}",
            r"\definecolor{decision}{RGB}{232,240,250}",
            r"\definecolor{warnbg}{RGB}{254,235,235}",
            r"\definecolor{warnstroke}{RGB}{197,48,48}",
            r"\newcommand{\statlabel}[1]{{\fontsize{6.5}{8}\selectfont\bfseries\textcolor{ink!55}{\MakeUppercase{#1}}}}",
            r"\newcommand{\badge}[2]{\tikz[baseline=(b.base)]{\node[draw=#2!40,fill=#2,rounded corners=1mm,inner xsep=1.8mm,inner ysep=0.6mm](b){{\fontsize{7}{8.5}\selectfont\bfseries #1}};}}",
            r"\def\FxA{0}\def\FxB{17}\def\FxC{33}\def\FxD{49}\def\FxE{65}\def\FxR{81}",
            r"\tikzset{",
            r"  hub/.style={draw=#1,thick,rounded corners=2pt,minimum height=6.5mm,minimum width=12.5mm,align=center,font=\fontsize{6.2}{7.5}\selectfont\bfseries,inner sep=0pt},",
            r"  hub/.default=ink,",
            r"  act/.style={draw=#1!55,fill=white,rounded corners=2pt,minimum height=6.5mm,minimum width=11mm,align=center,font=\fontsize{5.8}{7}\selectfont,inner sep=0pt},",
            r"  act/.default=ink,",
            r"  recon/.style={draw=primary!55,fill=primary!6,dashed,thick,rounded corners=2pt,minimum width=14mm,minimum height=6.5mm,align=center,font=\fontsize{5.5}{6.5}\selectfont\bfseries,text=primary,inner sep=0.5pt},",
            r"  rowlbl/.style={anchor=east,font=\fontsize{6}{7.2}\selectfont\bfseries,text=ink!65,minimum width=16mm,align=right},",
            r"  arr/.style={-{Stealth[length=1.6mm]},thick,draw=ink!50},",
            r"  bridge/.style={-{Stealth[length=2mm]},thick,draw=ink!45},",
            r"}",
            r"\begin{document}",
            r"\color{ink}",
            r"\noindent\begin{tabular}{@{}>{\raggedright\arraybackslash}p{0.62\linewidth}@{\hspace{0.02\linewidth}}>{\raggedright\arraybackslash}p{0.35\linewidth}@{}}",
            r"\begin{minipage}[t]{\linewidth}",
            r"  {\fontsize{17}{19}\selectfont\bfseries\color{primary} VCHF Menace}\\[1pt]",
            r"  {\fontsize{9}{11}\selectfont Dual-hub VCHF arbitrage — Celo (USDT) + Base (USDC) · Solana · VNX}\\[1pt]",
            rf"  {{\fontsize{{7}}{{8.5}}\selectfont\textcolor{{ink!60}}{{{subtitle}}}}}",
            r"\end{minipage}&",
            r"\begin{minipage}[t]{\linewidth}\raggedleft",
            rf"  \badge{{{n_active} routes active}}{{profitbg}}\hspace{{1.5mm}}\badge{{dual hub}}{{decision}}\\[2pt]",
            rf"  {{\fontsize{{7}}{{8.5}}\selectfont\textcolor{{ink!50}}{{{now} · github.com/Giansensey007/vchf\_menace}}}}",
            r"\end{minipage}\\",
            r"\end{tabular}",
            r"\vspace{1mm}\noindent\rule{\linewidth}{0.3pt}\vspace{1mm}",
            r"\noindent\renewcommand{\arraystretch}{1.1}",
            r"\begin{tabular}{@{}>{\centering\arraybackslash}p{0.23\linewidth}>{\centering\arraybackslash}p{0.23\linewidth}>{\centering\arraybackslash}p{0.23\linewidth}>{\centering\arraybackslash}p{0.24\linewidth}@{}}",
            rf"\fcolorbox{{primary!25}}{{surface}}{{\begin{{minipage}}[c][13mm][c]{{0.9\linewidth}}\statlabel{{Trade size}}\\{{\fontsize{{9.5}}{{11}}\selectfont\bfseries {trade_range}}}\\{{\fontsize{{6.5}}{{8}}\selectfont\textcolor{{ink!65}}{{VCHF per route}}}}\end{{minipage}}}}&",
            rf"\fcolorbox{{primary!25}}{{surface}}{{\begin{{minipage}}[c][13mm][c]{{0.9\linewidth}}\statlabel{{Min profit}}\\{{\fontsize{{9.5}}{{11}}\selectfont\bfseries\textcolor{{profit}}{{{min_profit}}}}}\\{{\fontsize{{6.5}}{{8}}\selectfont\textcolor{{ink!65}}{{net round-trip}}}}\end{{minipage}}}}&",
            rf"\fcolorbox{{primary!25}}{{surface}}{{\begin{{minipage}}[c][13mm][c]{{0.9\linewidth}}\statlabel{{VNX order min}}\\{{\fontsize{{9.5}}{{11}}\selectfont\bfseries {VNX_MIN_VCHF:.0f}}}\\{{\fontsize{{6.5}}{{8}}\selectfont\textcolor{{ink!65}}{{VCHF/USDC platform}}}}\end{{minipage}}}}&",
            rf"\fcolorbox{{primary!25}}{{surface}}{{\begin{{minipage}}[c][13mm][c]{{0.9\linewidth}}\statlabel{{Poll cycle}}\\{{\fontsize{{9.5}}{{11}}\selectfont\bfseries {poll_sec}}}\\{{\fontsize{{6.5}}{{8}}\selectfont\textcolor{{ink!65}}{{between scans}}}}\end{{minipage}}}}\\",
            r"\end{tabular}",
            r"\vspace{1.5mm}\noindent\rule{\linewidth}{0.3pt}\vspace{1.5mm}",
            r"\noindent\begin{tabular}{@{}>{\raggedright\arraybackslash}p{0.62\linewidth}@{\hspace{0.02\linewidth}}>{\raggedright\arraybackslash}p{0.35\linewidth}@{}}",
            r"\begin{minipage}[t]{\linewidth}\vspace{0pt}",
            r"\adjustbox{width=\linewidth,valign=t}{\begin{tikzpicture}[x=1mm,y=-1mm]",
            rf"\useasboundingbox (-18,0) rectangle (97,{ymax});",
            r"\node[anchor=west,font=\fontsize{8}{9.5}\selectfont\bfseries\color{primary}] at (0,0) {Hub topology};",
            r"\node[hub=celostroke,fill=celofill] (hubC) at (18,3.5) {Celo\\USDT};",
            r"\node[hub=basestroke,fill=basefill] (hubB) at (38,3.5) {Base\\USDC};",
            r"\node[hub=solstroke,fill=solfill] (hubS) at (58,3.5) {Sol\\USDC};",
            r"\node[hub=vnxstroke,fill=vnxfill] (hubV) at (78,3.5) {VNX\\VCHF};",
            r"\draw[bridge] (hubC) -- node[above,font=\fontsize{5}{6}\selectfont]{VNX VCHF} (hubS);",
            r"\draw[bridge] (hubB) -- node[above,font=\fontsize{5}{6}\selectfont]{VNX VCHF} (hubS);",
            r"\draw[bridge] (hubC) -- node[above,font=\fontsize{5}{6}\selectfont]{deposit} (hubV);",
            r"\draw[bridge] (hubB) -- node[above,font=\fontsize{5}{6}\selectfont]{deposit} (hubV);",
            r"\draw[bridge,dashed] (hubC) to[bend left=12] node[below,font=\fontsize{5}{6}\selectfont]{Wormhole USDT} (hubS);",
            r"\draw[bridge,dashed] (hubB) to[bend right=12] node[below,font=\fontsize{5}{6}\selectfont]{Wormhole} (hubS);",
            r"\node[anchor=west,font=\fontsize{5.5}{6.5}\selectfont,text=ink!55] at (0,6.5) {Solid = VCHF bridge · Dashed = stable rebalance (Wormhole / CCTP)};",
            *flow_lines,
            rf"\node[anchor=west,font=\fontsize{{5.5}}{{6.5}}\selectfont,text=ink!55,text width=95mm] at (0,{ymax - 1.5}) {{Each row = one scanner direction. Greyed rows = disabled via config. Token: VCHF on all legs.}};",
            r"\end{tikzpicture}}",
            r"\end{minipage}&",
            r"\begin{minipage}[t]{\linewidth}\vspace{0pt}",
            r"\noindent\fcolorbox{primary}{decision}{\begin{minipage}[t][38mm][t]{\dimexpr\linewidth-2\fboxsep-2\fboxrule\relax}",
            r"\centering{\fontsize{8}{9.5}\selectfont\bfseries\color{primary} Dual EVM hubs}\\[3pt]",
            r"{\fontsize{6.2}{7.5}\selectfont\raggedright",
            r"\textbf{Celo} spends USDT, Ubeswap VCHF/USDT.\\[1pt]",
            r"\textbf{Base} spends USDC, Kyber/Uniswap VCHF/USDC.\\[1pt]",
            r"\textbf{Solana} spends USDC via Jupiter.\\[1pt]",
            r"\textbf{VNX} platform buy/sell VCHF (min 30).\\[3pt]",
            r"\centering\renewcommand{\arraystretch}{1.1}",
            r"\begin{tabular}{@{}l@{\hspace{2mm}}l@{}}",
            r"\textbf{Bridge} & \textbf{Used for} \\",
            r"VNX VCHF & cross-chain VCHF \\",
            r"Wormhole & Celo/Base $\leftrightarrow$ Sol stables \\",
            r"CCTP & Sol USDC $\rightarrow$ ETH $\rightarrow$ VNX \\",
            r"\end{tabular}}",
            r"\end{minipage}}\\[2mm]",
            r"\noindent\fcolorbox{warnstroke}{warnbg}{\begin{minipage}[t][14mm][t]{\dimexpr\linewidth-2\fboxsep-2\fboxrule\relax}",
            r"\centering{\fontsize{8}{9.5}\selectfont\bfseries\color{warnstroke} VNX ETH accepts USDC only}\\[2pt]",
            r"{\fontsize{6}{7.2}\selectfont\raggedright Never deposit USDT on Ethereum to VNX. "
            r"CCTP and hub paths land USDC on ETH before \texttt{eth\_to\_vnx}.}",
            r"\end{minipage}}\\[2mm]",
            r"\noindent\fcolorbox{ink!18}{surface}{\begin{minipage}[t][14mm][t]{\dimexpr\linewidth-2\fboxsep-2\fboxrule\relax}",
            r"{\fontsize{7}{8.5}\selectfont\bfseries Closed-loop return}\\[1pt]",
            r"{\fontsize{6}{7.2}\selectfont After every arb the return leg runs. Platform origin after \texttt{vnx\_to\_solana} uses \texttt{cctp\_sol\_usdc\_to\_vnx} instead of inverse.}",
            r"\end{minipage}}\\[2mm]",
            r"\noindent\fcolorbox{ink!18}{surface}{\begin{minipage}[t][12mm][t]{\dimexpr\linewidth-2\fboxsep-2\fboxrule\relax}",
            r"{\fontsize{7}{8.5}\selectfont\bfseries Config flags}\\[1pt]",
            rf"{{\fontsize{{6}}{{7.2}}\selectfont {cfg_flags}}}",
            r"\end{minipage}}",
            r"\end{minipage}\\",
            r"\end{tabular}",
            r"\vspace{1mm}\noindent\rule{\linewidth}{0.3pt}",
            r"\vspace{0.5mm}",
            r"\noindent{\fontsize{6.5}{8}\selectfont\textbf{Deploy:} \texttt{python -m src.main} \quad|\quad \textbf{Regenerate:} \texttt{python scripts/generate\_routes\_pdf.py --executive}}",
            r"\end{document}",
        ]
    )


def _compile_tex(tex_path: Path, *, engine: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    for _ in range(2):
        proc = subprocess.run(
            [engine, "-interaction=nonstopmode", "-output-directory", str(DOCS), tex_path.name],
            cwd=DOCS,
            capture_output=True,
            text=True,
        )
    log_path = DOCS / f"{tex_path.stem}.log"
    if proc.returncode != 0 and log_path.exists():
        log = log_path.read_text(encoding="utf-8", errors="replace")
        if "Output written on" in log and (DOCS / f"{tex_path.stem}.pdf").exists():
            proc = subprocess.CompletedProcess(args=proc.args, returncode=0, stdout=proc.stdout, stderr=proc.stderr)
    return proc


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="Fetch live round-trip PnL (requires network)")
    p.add_argument(
        "--executive",
        action="store_true",
        help="Generate docs/vchf-menace-routes-executive.pdf (one-page overview)",
    )
    p.add_argument("--no-compile", action="store_true", help="Write .tex only")
    args = p.parse_args()

    DOCS.mkdir(parents=True, exist_ok=True)

    if args.executive:
        tex = _build_executive_latex()
        tex_path = DOCS / "vchf-menace-routes-executive.tex"
        pdf_path = DOCS / "vchf-menace-routes-executive.pdf"
        engine = "lualatex"
        tex_path.write_text(tex, encoding="utf-8")
        print(f"Wrote {tex_path}")
    else:
        live_rows = asyncio.run(_live_scan()) if args.live else None
        tex = _build_latex(live_rows)
        tex_path = DOCS / "vchf-menace-routes.tex"
        pdf_path = DOCS / "vchf-menace-routes.pdf"
        engine = "pdflatex"

    if not args.executive:
        tex_path.write_text(tex, encoding="utf-8")
        print(f"Wrote {tex_path}")

    if args.no_compile:
        return 0

    proc = _compile_tex(tex_path, engine=engine)
    if proc.returncode != 0:
        print(proc.stdout[-3000:] if proc.stdout else "")
        print(proc.stderr[-3000:] if proc.stderr else "")
        print(f"{engine} failed", file=sys.stderr)
        return 1

    built_pdf = tex_path.with_suffix(".pdf")
    if built_pdf.exists():
        if built_pdf.resolve() != pdf_path.resolve():
            shutil.copy2(built_pdf, pdf_path)
        print(f"Wrote {pdf_path}")
    else:
        print(f"{engine} finished but {built_pdf} not found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
