/**
 * register-v33.js — Register UHF v3.3 (The Spinor Topology Update) on Polygon Mainnet
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
    // --- Verify hash ---
    const paperContent = fs.readFileSync("./paper.md");
    const hash = crypto.createHash("sha256").update(paperContent).digest("hex");
    console.log("SHA-256:", hash);

    const expectedHash = "d7d49068026f91195642d528814b0b8542b835b8c5e005d32381d72650cd9f47";
    if (hash !== expectedHash) {
        console.error("ERROR: Hash mismatch!");
        console.error("  Expected:", expectedHash);
        console.error("  Got:     ", hash);
        process.exit(1);
    }
    console.log("✓ Hash matches expected value.\n");

    // --- Connect & register ---
    const provider = new ethers.JsonRpcProvider(
        "https://polygon-bor-rpc.publicnode.com", 137, { staticNetwork: true }
    );
    const wallet = new ethers.Wallet(process.env.DEPLOYER_PRIVATE_KEY, provider);
    const contract = new ethers.Contract(CONTRACT_ADDRESS, ABI, wallet);

    console.log("Registering v3.3 on UHFPaperRegistry...");
    const tx = await contract.registerPaper(hash, "Amir Benjamin Amitay", "3.3");
    console.log("TX sent:", tx.hash);

    const receipt = await tx.wait();
    console.log("Confirmed in block:", receipt.blockNumber);

    // --- Output ---
    console.log("\n" + "=".repeat(70));
    console.log("SUCCESS — UHF v3.3 REGISTERED ON POLYGON MAINNET");
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
        version: "3.3",
        timestamp: new Date().toISOString()
    });
    fs.writeFileSync("./deployment.json", JSON.stringify(deployment, null, 2));
    console.log("\n  ✓ deployment.json updated.");
}

main().catch(err => { console.error(err); process.exit(1); });
