#!/usr/bin/env python3
"""
Generate docs/vchf-menace-routes.pdf — full route map for VCHF Menace.

Usage:
  python scripts/generate_routes_pdf.py           # static route doc
  python scripts/generate_routes_pdf.py --live    # include live round-trip PnL
"""
from __future__ import annotations

import argparse
import asyncio
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
    "base_to_solana": [
        "Spend Celo USDT $\\rightarrow$ buy VCHF (Ubeswap)",
        "Deposit VCHF to VNX (CELO, min 5 VCHF cumulative)",
        "Withdraw VCHF to Solana",
        "Sell VCHF for Sol USDC (Jupiter)",
        "Wormhole USDT rebalance Celo $\\leftrightarrow$ Sol (stable leg)",
    ],
    "solana_to_base": [
        "Spend Sol USDC $\\rightarrow$ buy VCHF (Jupiter)",
        "Deposit VCHF to VNX (SOL, min 5 VCHF)",
        "Withdraw VCHF to Celo",
        "Sell VCHF for Celo USDT (Ubeswap)",
        "Wormhole USDT rebalance",
    ],
    "base_to_vnx": [
        "Spend Celo USDT $\\rightarrow$ buy VCHF on Celo",
        "Deposit VCHF to VNX platform (CELO)",
        "Platform sell VCHF for USDC",
        "Platform sell VCHF for USDC",
    ],
    "vnx_to_base": [
        "Platform buy VCHF (min 40 VCHF order)",
        "Withdraw VCHF to Celo",
        "Sell VCHF for Celo USDT",
    ],
    "solana_to_vnx": [
        "Spend Sol USDC $\\rightarrow$ buy VCHF (Jupiter)",
        "Deposit VCHF to VNX (SOL, min 5 VCHF)",
        "Platform sell VCHF for USDC",
    ],
    "vnx_to_solana": [
        "Platform buy VCHF (min 40 VCHF order)",
        "Withdraw VCHF to Solana",
        "Sell VCHF for Sol USDC (Jupiter)",
    ],
    CCTP_SOL_USDC_TO_VNX: [
        "Burn Sol USDC via Circle CCTP $\\rightarrow$ Ethereum",
        "Claim USDC on ETH hot wallet",
        "Deposit USDC to VNX platform (ETH, min 5 USDC)",
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
    ("celo\\_usdt\\_to\\_vnx\\_usdc", "Celo USDT $\\rightarrow$ Wormhole $\\rightarrow$ ETH USDC $\\rightarrow$ VNX USDC"),
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
        for size in (40.0, cfg.min_trade_vchf):
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
        rf"{{\color{{muted}}\small Generated {now} · github.com/Giansensey007/gbp\_menace\_-}}\\[12pt]",
        r"\end{center}",
        r"\section*{Configuration snapshot}",
        r"\begin{tabular}{@{}ll@{}}",
        rf"Treasury VCHF home & platform-only (idle VCHF on VNX) \\",
        rf"Closed loop & after every arb (return leg always runs) \\",
        rf"Trade size & {cfg.min_trade_vchf:.0f}--{cfg.max_trade_vchf:.0f} VCHF \\",
        rf"Min profit & \${cfg.min_profit_usd:.2f} round-trip \\",
        rf"VNX platform order min & {VNX_MIN_VCHF:.0f} VCHF \\",
        rf"VNX deposit min (CELO/SOL VCHF) & 5 VCHF cumulative \\",
        rf"VNX deposit min (ETH USDC) & {min_deposit_usdc('ETH'):.0f} USDC \\",
        rf"enable\_vnx\_cctp\_routes & {str(cfg.enable_vnx_cctp_routes).lower()} \\",
        rf"enable\_vnx\_arb\_routes & {str(cfg.enable_vnx_arb_routes).lower()} \\",
        r"\end{tabular}",
        r"\section*{Chain inventory model}",
        r"\begin{itemize}[nosep]",
        r"\item \textbf{Platform (VNX):} all idle VCHF + USDC for \texttt{vnx\_to\_*} buys",
        r"\item \textbf{Celo:} USDT only (no idle VCHF; dust $\leq$ 0.5 VCHF swept to platform)",
        r"\item \textbf{Solana:} USDC only (no idle VCHF)",
        r"\item \textbf{Ethereum:} USDC/USDT hub buffers + gas (no VCHF)",
        r"\end{itemize}",
        r"\section*{Arbitrage routes (6 directed pairs)}",
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
            (r"Sol USDC & solana\_to\_vnx & vnx\_to\_solana & Sol USDC \\"),
            (r"Sol USDC & solana\_to\_celo & celo\_to\_solana & Sol USDC \\"),
            (r"Platform & vnx\_to\_celo & celo\_to\_vnx & Platform USDC \\"),
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="Fetch live round-trip PnL (requires network)")
    p.add_argument("--no-compile", action="store_true", help="Write .tex only")
    args = p.parse_args()

    DOCS.mkdir(parents=True, exist_ok=True)
    live_rows = asyncio.run(_live_scan()) if args.live else None
    tex = _build_latex(live_rows)
    tex_path = DOCS / "vchf-menace-routes.tex"
    pdf_path = DOCS / "vchf-menace-routes.pdf"
    tex_path.write_text(tex, encoding="utf-8")
    print(f"Wrote {tex_path}")

    if args.no_compile:
        return 0

    for _ in range(2):
        proc = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-output-directory", str(DOCS), str(tex_path.name)],
            cwd=DOCS,
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        print(proc.stdout[-2000:] if proc.stdout else "")
        print(proc.stderr[-2000:] if proc.stderr else "")
        print("pdflatex failed", file=sys.stderr)
        return 1

    if pdf_path.exists():
        print(f"Wrote {pdf_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
