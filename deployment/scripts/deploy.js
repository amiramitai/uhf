const hre = require("hardhat");
const fs = require("fs");

async function main() {
    const manifest = JSON.parse(fs.readFileSync("./ipfs-manifest.json", "utf8"));

    console.log("=== Deploying ProofOfExistence ===\n");

    // Deploy the contract
    const ProofOfExistence = await hre.ethers.getContractFactory("ProofOfExistence");
    const poe = await ProofOfExistence.deploy();
    await poe.waitForDeployment();
    const contractAddress = await poe.getAddress();
    console.log("Contract deployed to:", contractAddress);

    // Register the PDF
    const pdfHash = "0x" + manifest.files.pdf.sha256;
    const pdfCid = manifest.files.pdf.cid;
    console.log("\nRegistering PDF document...");
    console.log("  Hash:", pdfHash);
    console.log("  CID :", pdfCid);

    const tx1 = await poe.registerDocument(
        pdfHash,
        pdfCid,
        manifest.title,
        manifest.author
    );
    const receipt1 = await tx1.wait();
    console.log("  TX  :", receipt1.hash);
    console.log("  Block:", receipt1.blockNumber);

    // Register the Markdown source
    const mdHash = "0x" + manifest.files.markdown.sha256;
    const mdCid = manifest.files.markdown.cid;
    console.log("\nRegistering Markdown source...");
    console.log("  Hash:", mdHash);
    console.log("  CID :", mdCid);

    const tx2 = await poe.registerDocument(
        mdHash,
        mdCid,
        manifest.title + " (Source)",
        manifest.author
    );
    const receipt2 = await tx2.wait();
    console.log("  TX  :", receipt2.hash);
    console.log("  Block:", receipt2.blockNumber);

    // Verify both documents
    console.log("\n=== Verification ===");
    const [exists1, ts1, cid1] = await poe.verifyDocument(pdfHash);
    console.log("PDF  : exists=" + exists1 + ", timestamp=" + ts1 + ", cid=" + cid1);

    const [exists2, ts2, cid2] = await poe.verifyDocument(mdHash);
    console.log("MD   : exists=" + exists2 + ", timestamp=" + ts2 + ", cid=" + cid2);

    const count = await poe.getDocumentCount();
    console.log("Total documents:", count.toString());

    // Save deployment info
    const network = hre.network.name;
    const deployment = {
        network,
        contractAddress,
        deployer: (await hre.ethers.provider.getSigner()).address,
        transactions: {
            pdf: { hash: receipt1.hash, blockNumber: receipt1.blockNumber },
            markdown: { hash: receipt2.hash, blockNumber: receipt2.blockNumber }
        },
        documents: {
            pdf: { contentHash: pdfHash, ipfsCid: pdfCid },
            markdown: { contentHash: mdHash, ipfsCid: mdCid }
        },
        timestamp: new Date().toISOString()
    };

    fs.writeFileSync("./deployment.json", JSON.stringify(deployment, null, 2));
    console.log("\nDeployment info saved to deployment.json");
    console.log("\n=== Proof-of-Existence Complete ===");
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
