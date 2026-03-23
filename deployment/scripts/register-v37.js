/**
 * register-v37.js — Register UHF v3.7 (The S-Matrix & Positivity Update) on Polygon Mainnet
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

    const expectedHash = "1264d8c960121c430bfebdbb840e8527c37efa749035a9d6ac078a9b424aeaa8";
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

    console.log("Registering v3.7 on UHFPaperRegistry...");
    const tx = await contract.registerPaper(hash, "Amir Benjamin Amitay", "3.7");
    console.log("TX sent:", tx.hash);

    const receipt = await tx.wait();
    console.log("Confirmed in block:", receipt.blockNumber);

    console.log("\n" + "=".repeat(70));
    console.log("SUCCESS — UHF v3.7 REGISTERED ON POLYGON MAINNET");
    console.log("=".repeat(70));
    console.log("  SHA-256 Hash:    ", hash);
    console.log("  Transaction Hash:", tx.hash);
    console.log("  PolygonScan URL: ", `https://polygonscan.com/tx/${tx.hash}`);
    console.log("  Block Number:    ", receipt.blockNumber);
    console.log("=".repeat(70));

    let deployment = {};
    try { deployment = JSON.parse(fs.readFileSync("./deployment.json", "utf-8")); } catch(e) {}
    deployment["v3.7"] = {
        version: "3.7",
        title: "The S-Matrix & Positivity Update",
        hash, txHash: tx.hash,
        blockNumber: receipt.blockNumber,
        contract: CONTRACT_ADDRESS,
        network: "Polygon Mainnet (chainId: 137)",
        timestamp: new Date().toISOString()
    };
    fs.writeFileSync("./deployment.json", JSON.stringify(deployment, null, 2) + "\n");
    console.log("deployment.json updated.");
}

main().catch((err) => { console.error("Registration failed:", err.message); process.exit(1); });
