/**
 * register-v82.js — Register UHF v8.2 FINAL on Polygon Mainnet
 * Updates: Analytic Bogoliubov Constant +16.67μs (Part I),
 *          Vacuum dissipation Q_vac=0.31% (Part II),
 *          Torsional α=1.2599 & μc=4.81 (Part III)
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

const PAPERS = [
    {
        file: "./UHF_Part_I_Core.md",
        version: "8.2-Part-I",
        label: "Part I: The Physical Core",
        expectedHash: "b44119252bbc3e25c81c7e42188b17e95edba0a6167d12da001d55c6c5f17697"
    },
    {
        file: "./UHF_Part_II_Mathematical_Foundations.md",
        version: "8.2-Part-II",
        label: "Part II: Mathematical Foundations",
        expectedHash: "9aa2becc3445423bb7fe958de340a02c0d125ff3d8044c22a6e875f40e541fc3"
    },
    {
        file: "./UHF_Part_III_Standard_Model.md",
        version: "8.2-Part-III",
        label: "Part III: Standard Model Extension",
        expectedHash: "bc206a645e7d230cd9ff57426df082cfaaee23ba6797bcafda7d22f290c37646"
    }
];

async function main() {
    // Verify all hashes first
    console.log("Verifying SHA-256 hashes...\n");
    for (const paper of PAPERS) {
        const content = fs.readFileSync(paper.file);
        const hash = crypto.createHash("sha256").update(content).digest("hex");
        console.log(`${paper.label}`);
        console.log(`  SHA-256: ${hash}`);
        if (hash !== paper.expectedHash) {
            console.error(`  ERROR: Hash mismatch!`);
            console.error(`  Expected: ${paper.expectedHash}`);
            console.error(`  Got:      ${hash}`);
            process.exit(1);
        }
        console.log(`  ✓ Hash verified.\n`);
        paper.hash = hash;
    }

    const provider = new ethers.JsonRpcProvider(
        "https://polygon-bor-rpc.publicnode.com", 137, { staticNetwork: true }
    );
    const wallet = new ethers.Wallet(process.env.DEPLOYER_PRIVATE_KEY, provider);
    const contract = new ethers.Contract(CONTRACT_ADDRESS, ABI, wallet);

    const results = [];

    for (const paper of PAPERS) {
        console.log(`Registering ${paper.version} on UHFPaperRegistry...`);
        const tx = await contract.registerPaper(paper.hash, "Amir Benjamin Amitay", paper.version);
        console.log(`  TX sent: ${tx.hash}`);

        const receipt = await tx.wait();
        console.log(`  Confirmed in block: ${receipt.blockNumber}\n`);

        results.push({
            version: paper.version,
            label: paper.label,
            hash: paper.hash,
            txHash: tx.hash,
            block: receipt.blockNumber
        });
    }

    console.log("\n" + "=".repeat(70));
    console.log("SUCCESS — UHF v8.2 FINAL (ALL 3 PARTS) REGISTERED ON POLYGON MAINNET");
    console.log("=".repeat(70));
    for (const r of results) {
        console.log(`\n  ${r.label} (${r.version})`);
        console.log(`    SHA-256:     ${r.hash}`);
        console.log(`    TX Hash:     ${r.txHash}`);
        console.log(`    Block:       ${r.block}`);
        console.log(`    PolygonScan: https://polygonscan.com/tx/${r.txHash}`);
    }
    console.log("=".repeat(70));

    // Append to on-chain registry log
    for (const r of results) {
        const logEntry = `\n${r.version} | ${new Date().toISOString()} | Block: ${r.block} | TX: ${r.txHash} | Hash: ${r.hash}`;
        fs.appendFileSync("./scripts/on-chain-log.txt", logEntry);
    }
    console.log("\nAppended to on-chain-log.txt");
}

main().catch(e => { console.error(e); process.exit(1); });
