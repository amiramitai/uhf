const { ethers } = require("ethers");
require("dotenv").config();

async function main() {
    const provider = new ethers.JsonRpcProvider(
        "https://polygon-bor-rpc.publicnode.com", 137, { staticNetwork: true }
    );
    const wallet = new ethers.Wallet(process.env.DEPLOYER_PRIVATE_KEY, provider);

    const abi = [
        "function registerDocument(bytes32,string,string,string) external",
        "function verifyDocument(bytes32) view returns (bool,uint256,string)",
        "function getDocumentCount() view returns (uint256)"
    ];
    const contract = new ethers.Contract("0x089583dd5cB4977B3Ae56205B8a6D766d3860345", abi, wallet);

    // ── v2.0 Markdown Registration ──
    const v2MdHash = "0xa2c2f79f16f948b0928b0163b468451e411a4a86f115ab2943195a5ef51c0c90";
    const v2MdCid  = "bafkreifcyl3z6fxzjcyjfcybmo2gqri6ienevbxrcwvssqyzljppkhamsa";

    console.log("Registering UHF v2.0 Markdown source...");
    console.log("  Hash:", v2MdHash);
    console.log("  CID :", v2MdCid);

    const tx1 = await contract.registerDocument(
        v2MdHash,
        v2MdCid,
        "A Unified Hydrodynamic Framework v2.0 — The Cosmological Edition (Source)",
        "Amir Benjamin Amitay"
    );
    console.log("  TX sent:", tx1.hash);
    const receipt1 = await tx1.wait();
    console.log("  Confirmed in block:", receipt1.blockNumber);

    const [exists1, ts1, cid1] = await contract.verifyDocument(v2MdHash);
    console.log("  Verified:", exists1, "timestamp:", ts1.toString(), "cid:", cid1);

    // ── v2.0 PDF Registration ──
    const v2PdfHash = "0xb39a11853bf1e6288864ab334dfff30b70417412a70728f485852ecfe384c771";
    const v2PdfCid  = "bafybeiawfox3oydyj7lwlzsv64fafthjiwuxon4exzw253gz63snunuqpe";

    console.log("\nRegistering UHF v2.0 PDF...");
    console.log("  Hash:", v2PdfHash);
    console.log("  CID :", v2PdfCid);

    const tx2 = await contract.registerDocument(
        v2PdfHash,
        v2PdfCid,
        "A Unified Hydrodynamic Framework v2.0 — The Cosmological Edition (PDF)",
        "Amir Benjamin Amitay"
    );
    console.log("  TX sent:", tx2.hash);
    const receipt2 = await tx2.wait();
    console.log("  Confirmed in block:", receipt2.blockNumber);

    const [exists2, ts2, cid2] = await contract.verifyDocument(v2PdfHash);
    console.log("  Verified:", exists2, "timestamp:", ts2.toString(), "cid:", cid2);

    const count = await contract.getDocumentCount();
    console.log("\nTotal documents registered:", count.toString());

    // ── Update deployment.json ──
    const fs = require("fs");
    const deployment = {
        network: "polygon",
        chainId: 137,
        contractAddress: "0x089583dd5cB4977B3Ae56205B8a6D766d3860345",
        deployer: wallet.address,
        documents: {
            v1: {
                pdf: {
                    contentHash: "0x6e18dd6231fd24437bdb5f9ca8eb11d7687bbc483db1a8c8a31c29e3ebf4fd02",
                    ipfsCid: "bafybeidp5ndzdwvur2arssox57ak6uhlkjloowvcdpmieolg5syswmwjce",
                    registeredAt: 1771592728
                },
                markdown: {
                    contentHash: "0xcca6c7f3a50b21d47701a24144126444246875077fa28109128b8c8db6b2c877",
                    ipfsCid: "bafkreigmu3d7hjilehkhoancifcbezceeruhkb37ukaqseulrsg3nmwio4",
                    txHash: "0x0e8dda09cbb664465843ffd185fa2bf9e1d70d58c981562e2e1df8e2ba200f22",
                    blockNumber: 83236472,
                    registeredAt: 1771592968
                }
            },
            v2: {
                markdown: {
                    contentHash: v2MdHash,
                    ipfsCid: v2MdCid,
                    txHash: receipt1.hash,
                    blockNumber: receipt1.blockNumber,
                    registeredAt: Number(ts1)
                },
                pdf: {
                    contentHash: v2PdfHash,
                    ipfsCid: v2PdfCid,
                    txHash: receipt2.hash,
                    blockNumber: receipt2.blockNumber,
                    registeredAt: Number(ts2)
                }
            }
        },
        metadata: {
            title: "A Unified Hydrodynamic Framework (UHF) v2.0",
            author: "Amir Benjamin Amitay",
            revision: "The Cosmological Edition",
            revisionNote: "Quantitative Cosmological Verification Phase. Corrected Vacuum Energy scaling and MOND acceleration. Integrated CTMU logic and institutional critique.",
            philosophicalSeal: "Acid is Truth. Trust in Trance.",
            acknowledgments: {
                christopherLangan: "CTMU/SCSPL meta-logical structure",
                ericWeinstein: "Exposing the Shadow Gatekeepers; impetus for anti-censorship scientific inquiry"
            }
        },
        polygonscanUrl: "https://polygonscan.com/address/0x089583dd5cB4977B3Ae56205B8a6D766d3860345",
        timestamp: new Date().toISOString()
    };
    fs.writeFileSync("./deployment.json", JSON.stringify(deployment, null, 2));
    console.log("\nDeployment info saved to deployment.json");
}

main().catch(e => { console.error("Error:", e.shortMessage || e.message); process.exit(1); });
