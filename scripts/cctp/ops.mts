/**
 * VCHF Menace CCTP ops — burn/receive on Solana only (EVM handled in Python).
 * Usage:
 *   npx tsx ops.mts burn-sol --amount 10000000 --maxFee 1000000 --minFinalityThreshold 1000
 *   npx tsx ops.mts receive-sol --message 0x... --attestation 0x...
 */
import { minimist } from "zx";
import { BN } from "@coral-xyz/anchor";
import { depositForBurnSol, receiveMessageSol } from "./v2/solana";

const cmd = process.argv[2];
const raw = minimist(process.argv.slice(3), {
  string: ["message", "attestation"],
});

async function main() {
  if (cmd === "burn-sol") {
    const amount = new BN(Number(raw.amount));
    const maxFee = new BN(Number(raw.maxFee ?? 0));
    const minFinalityThreshold = Number(raw.minFinalityThreshold ?? 1000);
    const tx = await depositForBurnSol(amount, maxFee, minFinalityThreshold);
    console.log(JSON.stringify({ ok: true, tx }));
    return;
  }
  if (cmd === "receive-sol") {
    const message = String(raw.message ?? "");
    const attestation = String(raw.attestation ?? "");
    if (!message || !attestation) {
      throw new Error("message and attestation required");
    }
    const tx = await receiveMessageSol(message, attestation);
    console.log(JSON.stringify({ ok: true, tx }));
    return;
  }
  throw new Error(`Unknown command: ${cmd}. Use burn-sol or receive-sol`);
}

main().catch((err) => {
  console.error(JSON.stringify({ ok: false, error: String(err?.message ?? err) }));
  process.exit(1);
});
