/**
 * register-v70.js — Register UHF v7.0 (The Unified Mathematical & Phenomenological Synthesis) on Polygon Mainnet
 */
const { ethers } = require("ethers");
const fs = require("fs");
const crypto = require("crypto");
require("dotenv").config();

const CONTRACT_ADDRESS = "0xe0bB4bC3116e19F2c0c183eFf8802C4F707B0054";
const ABI = [
    "function registerPaper(string memory _hash, string memory _author, string memory _version) public",
    "event PaperRegistered(address indexed authorAddress, string documentHash, uint256 timestamp, string author, string version)"
];

async function main() {
    const paperContent = fs.readFileSync("./paper.md");
    const hash = crypto.createHash("sha256").update(paperContent).digest("hex");
    console.log("SHA-256:", hash);

    const expectedHash = "7382c923a789b98568ef1fa927ff832d2d4f37fa3171647934a5550ded836a28";
    if (hash !== expectedHash) {
        console.error("ERROR: Hash mismatch!");
        console.error("  Expected:", expectedHash);
        console.error("  Got:     ", hash);
        process.exit(1);
    }
    console.log("✓ Hash matches expected value.\n");

    const provider = new ethers.JsonRpcProvider(
        "https://polygon-bor-rpc.publicnode.com", 137, { staticNetwork: true }
    );
    const wallet = new ethers.Wallet(process.env.DEPLOYER_PRIVATE_KEY, provider);
    const contract = new ethers.Contract(CONTRACT_ADDRESS, ABI, wallet);

    console.log("Registering v7.0 on UHFPaperRegistry...");
    const tx = await contract.registerPaper(hash, "Amir Benjamin Amitay", "7.0");
    console.log("TX sent:", tx.hash);

    const receipt = await tx.wait();
    console.log("Confirmed in block:", receipt.blockNumber);

    console.log("\n" + "=".repeat(70));
    console.log("SUCCESS — UHF v7.0 REGISTERED ON POLYGON MAINNET");
    console.log("=".repeat(70));
    console.log("  SHA-256 Hash:    ", hash);
    console.log("  Transaction Hash:", tx.hash);
    console.log("  PolygonScan URL: ", `https://polygonscan.com/tx/${tx.hash}`);
    console.log("  Block Number:    ", receipt.blockNumber);
    console.log("=".repeat(70));

    // Append to on-chain registry log
    const logEntry = `\nv7.0 | ${new Date().toISOString()} | Block: ${receipt.blockNumber} | TX: ${tx.hash} | Hash: ${hash}`;
    fs.appendFileSync("./scripts/on-chain-log.txt", logEntry);
    console.log("\nAppended to on-chain-log.txt");
}

main().catch(e => { console.error(e); process.exit(1); });
