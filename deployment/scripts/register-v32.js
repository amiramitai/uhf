/**
 * register-v32.js — Hash paper.md and register v3.2 on the existing UHFPaperRegistry
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
    // --- Task 1: SHA-256 hash ---
    const paperContent = fs.readFileSync("./paper.md");
    const hash = crypto.createHash("sha256").update(paperContent).digest("hex");
    console.log("SHA-256:", hash);

    // --- Task 2: Connect & register ---
    const provider = new ethers.JsonRpcProvider(
        "https://polygon-bor-rpc.publicnode.com", 137, { staticNetwork: true }
    );
    const wallet = new ethers.Wallet(process.env.DEPLOYER_PRIVATE_KEY, provider);
    const contract = new ethers.Contract(CONTRACT_ADDRESS, ABI, wallet);

    console.log("\nRegistering v3.2 on UHFPaperRegistry...");
    const tx = await contract.registerPaper(hash, "Amir Benjamin Amitay", "3.2");
    console.log("TX sent:", tx.hash);

    const receipt = await tx.wait();
    console.log("Confirmed in block:", receipt.blockNumber);

    // --- Task 3: Output ---
    console.log("\n" + "=".repeat(70));
    console.log("SUCCESS — UHF v3.2 REGISTERED ON POLYGON MAINNET");
    console.log("=".repeat(70));
    console.log("  SHA-256 Hash:    ", hash);
    console.log("  Transaction Hash:", tx.hash);
    console.log("  PolygonScan URL: ", `https://polygonscan.com/tx/${tx.hash}`);
    console.log("=".repeat(70));

    // Update deployment.json
    const deployment = JSON.parse(fs.readFileSync("./deployment.json", "utf8"));
    if (!deployment.v31Registry.registrations) {
        deployment.v31Registry.registrations = [deployment.v31Registry.registration];
    }
    deployment.v31Registry.registrations.push({
        txHash: tx.hash,
        blockNumber: receipt.blockNumber,
        paperHash: hash,
        author: "Amir Benjamin Amitay",
        version: "3.2",
        timestamp: new Date().toISOString()
    });
    fs.writeFileSync("./deployment.json", JSON.stringify(deployment, null, 2));
    console.log("\n  ✓ deployment.json updated.");
}

main().then(() => process.exit(0)).catch(e => { console.error(e); process.exit(1); });
