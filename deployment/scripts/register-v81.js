/**
 * register-v81.js — Register UHF v8.1 on Polygon Mainnet
 * Updates: The Wilsonian-Hydrodynamic Bridge Release
 *          - M.4/M.5/N.4/N.5/O.4/O.5 Deleted
 *          - Lemma P, Lemma Q, Lemma R Inserted
 *          - Rebuilt PDFs
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
        version: "8.1-Part-I",
        label: "Part I: The Physical Core",
        expectedHash: "123467883e650cb7c32cb1ea9222d6e3e6074bfc0d11e2f784be627a8857257b"
    },
    {
        file: "./UHF_Part_II_Mathematical_Foundations.md",
        version: "8.1-Part-II",
        label: "Part II: Mathematical Foundations",
        expectedHash: "701dba111bae81d78f1c818c47c5097a2049f0a7392401ccf137da5c4534a1ae"
    },
    {
        file: "./UHF_Part_III_Standard_Model.md",
        version: "8.1-Part-III",
        label: "Part III: Standard Model Extension",
        expectedHash: "1d23cd41b225b4101173ab816e2d35fe26647252afd851dcaa6938d972499a8d"
    }
];

async function main() {
    // Verify all hashes first
    console.log("Verifying SHA-256 hashes...\n");
    for (const paper of PAPERS) {
        if (!fs.existsSync(paper.file)) {
            console.error(`ERROR: File not found: ${paper.file}`);
            process.exit(1);
        }
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

    // Check for private key
    if (!process.env.DEPLOYER_PRIVATE_KEY) {
        console.error("ERROR: DEPLOYER_PRIVATE_KEY not set in environment.");
        process.exit(1);
    }

    const provider = new ethers.JsonRpcProvider(
        "https://polygon-bor-rpc.publicnode.com", 
        137, 
        { staticNetwork: true }
    );
    const wallet = new ethers.Wallet(process.env.DEPLOYER_PRIVATE_KEY, provider);
    const contract = new ethers.Contract(CONTRACT_ADDRESS, ABI, wallet);

    const results = [];

    for (const paper of PAPERS) {
        console.log(`Registering ${paper.version} on UHFPaperRegistry...`);
        try {
            const tx = await contract.registerPaper(paper.hash, "Amir Benjamin Amitay", paper.version);
            console.log(`  TX sent: ${tx.hash}`);

            console.log(`  Waiting for confirmation...`);
            const receipt = await tx.wait();
            console.log(`  Confirmed in block: ${receipt.blockNumber}\n`);
            
            results.push({
                version: paper.version,
                label: paper.label,
                hash: paper.hash,
                txHash: tx.hash,
                block: receipt.blockNumber
            });
        } catch (err) {
            console.error(`  ERROR registering ${paper.version}: ${err.message}`);
            // If already registered or other error, we might continue or stop. 
            // For now, let's stop to be safe, or just log it.
        }
    }

    console.log("\n" + "=".repeat(70));
    console.log("In-Memory Summary (Not committed yet)");
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
    if (results.length > 0) {
        console.log("\nAppended to on-chain-log.txt");
    }
}

main().catch(e => { console.error(e); process.exit(1); });
