/**
 * register-simulation.js — Register UHF Simulation Suite on Polygon Mainnet
 * Builds the zip from simulation/ sources, then registers its SHA-256 on-chain.
 *
 * Usage:
 *   ./scripts/build-simulation-zip.sh   # build first
 *   node scripts/register-simulation.js  # then register
 */
const { ethers } = require("ethers");
const fs = require("fs");
const crypto = require("crypto");
const { execSync } = require("child_process");
require("dotenv").config();

const CONTRACT_ADDRESS = "0xe0bB4bC3116e19F2c0c183eFf8802C4F707B0054";
const ABI = [
    "function registerPaper(string memory _hash, string memory _author, string memory _version) public",
    "event PaperRegistered(address indexed authorAddress, string documentHash, uint256 timestamp, string author, string version)"
];

const ZIP_PATH = "./out/UHF_Simulation_Suite.zip";
const VERSION = "3.1-Simulation";
const LABEL = "UHF Simulation Suite (RTX 3090 Verification Code)";
const EXPECTED_HASH = "9efd662522207c6a83dfbac34f67bf1f25df62e73a95157b4f15f81b35db1331";

async function main() {
    // Build the zip first
    console.log("Building simulation zip...\n");
    execSync("bash scripts/build-simulation-zip.sh", { stdio: "inherit" });

    // Verify hash
    console.log("\nVerifying SHA-256 hash...\n");
    const content = fs.readFileSync(ZIP_PATH);
    const hash = crypto.createHash("sha256").update(content).digest("hex");
    console.log(`${LABEL}`);
    console.log(`  SHA-256: ${hash}`);
    if (hash !== EXPECTED_HASH) {
        console.error(`  ERROR: Hash mismatch!`);
        console.error(`  Expected: ${EXPECTED_HASH}`);
        console.error(`  Got:      ${hash}`);
        process.exit(1);
    }
    console.log(`  ✓ Hash verified.\n`);

    // Register on Polygon
    const provider = new ethers.JsonRpcProvider(
        "https://polygon-bor-rpc.publicnode.com", 137, { staticNetwork: true }
    );
    const wallet = new ethers.Wallet(process.env.DEPLOYER_PRIVATE_KEY, provider);
    const contract = new ethers.Contract(CONTRACT_ADDRESS, ABI, wallet);

    console.log(`Registering ${VERSION} on UHFPaperRegistry...`);
    const tx = await contract.registerPaper(hash, "Amir Benjamin Amitay", VERSION);
    console.log(`  TX sent: ${tx.hash}`);

    const receipt = await tx.wait();
    console.log(`  Confirmed in block: ${receipt.blockNumber}\n`);

    console.log("=".repeat(70));
    console.log("SUCCESS — UHF SIMULATION SUITE REGISTERED ON POLYGON MAINNET");
    console.log("=".repeat(70));
    console.log(`\n  ${LABEL} (${VERSION})`);
    console.log(`    SHA-256:     ${hash}`);
    console.log(`    TX Hash:     ${tx.hash}`);
    console.log(`    Block:       ${receipt.blockNumber}`);
    console.log(`    PolygonScan: https://polygonscan.com/tx/${tx.hash}`);
    console.log("=".repeat(70));

    // Append to on-chain registry log
    const logEntry = `\n${VERSION} | ${new Date().toISOString()} | Block: ${receipt.blockNumber} | TX: ${tx.hash} | Hash: ${hash}`;
    fs.appendFileSync("./scripts/on-chain-log.txt", logEntry);
    console.log("\nAppended to on-chain-log.txt");
}

main().catch(e => { console.error(e); process.exit(1); });
