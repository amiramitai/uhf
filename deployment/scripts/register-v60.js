/**
 * register-v60.js — Register UHF v6.0 (The Academic Refactor) on Polygon Mainnet
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

    const expectedHash = "818e4a447c5db86d9f49abf92bffc25c4b64127307fb9e1f01cb5cca80dc4630";
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

    console.log("Registering v6.0 on UHFPaperRegistry...");
    const tx = await contract.registerPaper(hash, "Amir Benjamin Amitay", "6.0");
    console.log("TX sent:", tx.hash);

    const receipt = await tx.wait();
    console.log("Confirmed in block:", receipt.blockNumber);

    console.log("\n" + "=".repeat(70));
    console.log("SUCCESS — UHF v6.0 REGISTERED ON POLYGON MAINNET");
    console.log("=".repeat(70));
    console.log("  SHA-256 Hash:    ", hash);
    console.log("  Transaction Hash:", tx.hash);
    console.log("  PolygonScan URL: ", `https://polygonscan.com/tx/${tx.hash}`);
    console.log("  Block Number:    ", receipt.blockNumber);
    console.log("=".repeat(70));

    // Save deployment info
    const deployInfo = {
        version: "6.0",
        subtitle: "The Academic Refactor",
        hash: hash,
        txHash: tx.hash,
        blockNumber: receipt.blockNumber,
        timestamp: new Date().toISOString(),
        chain: "Polygon Mainnet (137)",
        contract: CONTRACT_ADDRESS,
        pdfHash: "a5127319cee765abbb22ad951c6d8e52fd4c437c92cf1938eeb9de1f6157546f"
    };
    fs.writeFileSync("deployment.json", JSON.stringify(deployInfo, null, 2));
    console.log("\n✓ Deployment info saved to deployment.json");
}

main().catch((err) => { console.error(err); process.exit(1); });
