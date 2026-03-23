/**
 * register-v50.js — Register UHF v5.0 (The Unified Standard Model Update) on Polygon Mainnet
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

    const expectedHash = "fe52ac96333d19740673f82f0d5e71dd6eff9fccee01098ab839157870de340e";
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

    console.log("Registering v5.0 on UHFPaperRegistry...");
    const tx = await contract.registerPaper(hash, "Amir Benjamin Amitay", "5.0");
    console.log("TX sent:", tx.hash);

    const receipt = await tx.wait();
    console.log("Confirmed in block:", receipt.blockNumber);

    console.log("\n" + "=".repeat(70));
    console.log("SUCCESS — UHF v5.0 REGISTERED ON POLYGON MAINNET");
    console.log("=".repeat(70));
    console.log("  SHA-256 Hash:    ", hash);
    console.log("  Transaction Hash:", tx.hash);
    console.log("  PolygonScan URL: ", `https://polygonscan.com/tx/${tx.hash}`);
    console.log("  Block Number:    ", receipt.blockNumber);
    console.log("=".repeat(70));

    // Save deployment info
    const deployInfo = {
        version: "5.0",
        subtitle: "The Unified Standard Model Update",
        hash: hash,
        txHash: tx.hash,
        blockNumber: receipt.blockNumber,
        timestamp: new Date().toISOString(),
        chain: "Polygon Mainnet (137)",
        contract: CONTRACT_ADDRESS,
        pdfHash: "91091d1abcd91ce47510ebb698fa054c63e7f27fe37b2216511ccb90dbd0904d"
    };
    fs.writeFileSync("deployment.json", JSON.stringify(deployInfo, null, 2));
    console.log("\n✓ Deployment info saved to deployment.json");
}

main().catch((err) => { console.error(err); process.exit(1); });
