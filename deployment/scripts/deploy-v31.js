/**
 * deploy-v31.js — Deploy UHFPaperRegistry and register the v3.1 paper hash.
 *
 * Uses Hardhat + ethers to:
 *   1. Deploy UHFPaperRegistry.sol to Polygon Mainnet
 *   2. Call registerPaper() with the v3.1 SHA-256 hash
 *   3. Log all on-chain details
 */

const hre = require("hardhat");
const fs = require("fs");

async function main() {
    console.log("=".repeat(70));
    console.log("UHF v3.1 — Blockchain Deployment & Registration");
    console.log("=".repeat(70));

    // ── Step 1: Deploy the contract ──
    console.log("\n[1/4] Compiling UHFPaperRegistry...");
    const Factory = await hre.ethers.getContractFactory("UHFPaperRegistry");

    console.log("[2/4] Deploying to Polygon Mainnet...");
    const registry = await Factory.deploy();
    await registry.waitForDeployment();
    const contractAddress = await registry.getAddress();
    const deployTx = registry.deploymentTransaction();
    const deployReceipt = await deployTx.wait();

    console.log("  ✓ Contract deployed!");
    console.log("  Address:    ", contractAddress);
    console.log("  Deploy TX:  ", deployTx.hash);
    console.log("  Block:      ", deployReceipt.blockNumber);
    console.log("  Gas used:   ", deployReceipt.gasUsed.toString());

    // ── Step 2: Register the paper hash ──
    const paperHash = "d4d1f5cfb3081a24579913983bf9b60dff4b8cd7ecfc6c21fbd6fd61b838ba71";
    const author = "Amir Benjamin Amitay";
    const version = "3.1";

    console.log("\n[3/4] Registering paper on-chain...");
    console.log("  Hash:    ", paperHash);
    console.log("  Author:  ", author);
    console.log("  Version: ", version);

    const tx = await registry.registerPaper(paperHash, author, version);
    console.log("  TX sent: ", tx.hash);

    const receipt = await tx.wait();
    console.log("  ✓ Paper registered!");
    console.log("  Block:   ", receipt.blockNumber);
    console.log("  Gas used:", receipt.gasUsed.toString());

    // ── Step 3: Parse the emitted event ──
    const event = receipt.logs[0];
    console.log("\n[4/4] Event emitted:");
    console.log("  Topic (PaperRegistered):", event.topics[0]);

    // ── Step 4: Output summary ──
    const deployer = (await hre.ethers.getSigners())[0].address;

    console.log("\n" + "=".repeat(70));
    console.log("DEPLOYMENT COMPLETE — SUMMARY");
    console.log("=".repeat(70));
    console.log(`  Network:          Polygon Mainnet (chainId 137)`);
    console.log(`  Deployer:         ${deployer}`);
    console.log(`  Contract Address: ${contractAddress}`);
    console.log(`  Register TX Hash: ${tx.hash}`);
    console.log(`  Paper SHA-256:    ${paperHash}`);
    console.log(`  Author:           ${author}`);
    console.log(`  Version:          ${version}`);
    console.log(`\n  PolygonScan Contract: https://polygonscan.com/address/${contractAddress}`);
    console.log(`  PolygonScan TX:       https://polygonscan.com/tx/${tx.hash}`);
    console.log("=".repeat(70));

    // ── Update deployment.json ──
    let deployment = {};
    try {
        deployment = JSON.parse(fs.readFileSync("./deployment.json", "utf8"));
    } catch (e) {}

    deployment.v31Registry = {
        network: "polygon",
        chainId: 137,
        contractName: "UHFPaperRegistry",
        contractAddress: contractAddress,
        deployer: deployer,
        deployTxHash: deployTx.hash,
        deployBlock: deployReceipt.blockNumber,
        registration: {
            txHash: tx.hash,
            blockNumber: receipt.blockNumber,
            paperHash: paperHash,
            author: author,
            version: version,
            timestamp: new Date().toISOString()
        },
        polygonscanUrl: `https://polygonscan.com/address/${contractAddress}`,
        registerTxUrl: `https://polygonscan.com/tx/${tx.hash}`
    };

    fs.writeFileSync("./deployment.json", JSON.stringify(deployment, null, 2));
    console.log("\n  ✓ deployment.json updated with v3.1 registry data.");
}

main()
    .then(() => process.exit(0))
    .catch((error) => {
        console.error("ERROR:", error);
        process.exit(1);
    });
