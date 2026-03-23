const axios = require('axios');
const fs = require('fs');
const crypto = require('crypto');
const FormData = require('form-data');
require('dotenv').config();

const PINATA_JWT = process.env.PINATA_JWT;

function sha256(filePath) {
    const data = fs.readFileSync(filePath);
    return crypto.createHash('sha256').update(data).digest('hex');
}

async function uploadFile(filePath, name) {
    const url = 'https://uploads.pinata.cloud/v3/files';
    const data = new FormData();
    data.append('file', fs.createReadStream(filePath));
    data.append('name', name);

    const response = await axios.post(url, data, {
        maxBodyLength: Infinity,
        headers: {
            'Authorization': `Bearer ${PINATA_JWT}`,
            ...data.getHeaders()
        }
    });

    return response.data;
}

async function main() {
    console.log('=== IPFS Upload & Hash Registry ===\n');

    // Compute SHA-256 hashes
    const mdHash = sha256('./paper.md');
    const pdfHash = sha256('./paper.pdf');
    console.log('SHA-256 (paper.md) :', mdHash);
    console.log('SHA-256 (paper.pdf):', pdfHash);
    console.log('');

    // Upload paper.md
    console.log('Uploading paper.md to IPFS via Pinata v3...');
    const mdResult = await uploadFile('./paper.md', 'Unified_Hydrodynamic_Framework_Source');
    const mdCid = mdResult.data?.cid || mdResult.IpfsHash;
    console.log('  CID:', mdCid);
    console.log('');

    // Upload paper.pdf
    console.log('Uploading paper.pdf to IPFS via Pinata v3...');
    const pdfResult = await uploadFile('./paper.pdf', 'Unified_Hydrodynamic_Framework_PDF');
    const pdfCid = pdfResult.data?.cid || pdfResult.IpfsHash;
    console.log('  CID:', pdfCid);
    console.log('');

    // Save manifest for smart contract
    const manifest = {
        title: 'A Unified Hydrodynamic Framework (UHF) v2.0',
        author: 'Amir Benjamin Amitay',
        version: '2.0',
        revision: 'The Cosmological Edition',
        date: '2026-02-21',
        revisionNote: 'Quantitative Cosmological Verification Phase. Corrected Vacuum Energy scaling and MOND acceleration. Integrated CTMU logic and institutional critique.',
        philosophicalSeal: 'Acid is Truth. Trust in Trance.',
        acknowledgments: {
            christopherLangan: 'CTMU/SCSPL meta-logical structure',
            ericWeinstein: 'Exposing the Shadow Gatekeepers; impetus for anti-censorship scientific inquiry'
        },
        previousVersion: {
            v1_markdown: {
                sha256: 'cca6c7f3a50b21d47701a24144126444246875077fa28109128b8c8db6b2c877',
                ipfsCid: 'bafkreigmu3d7hjilehkhoancifcbezceeruhkb37ukaqseulrsg3nmwio4'
            },
            v1_pdf: {
                sha256: '6e18dd6231fd24437bdb5f9ca8eb11d7687bbc483db1a8c8a31c29e3ebf4fd02',
                ipfsCid: 'bafybeidp5ndzdwvur2arssox57ak6uhlkjloowvcdpmieolg5syswmwjce'
            }
        },
        files: {
            markdown: { cid: mdCid, sha256: mdHash },
            pdf: { cid: pdfCid, sha256: pdfHash }
        },
        timestamp: new Date().toISOString()
    };

    fs.writeFileSync('./ipfs-manifest.json', JSON.stringify(manifest, null, 2));
    console.log('Manifest saved to ipfs-manifest.json');
    console.log('\n=== Ready for on-chain registration ===');
    console.log('PDF SHA-256 (for smart contract): 0x' + pdfHash);
}

main().catch(err => {
    console.error('Upload failed:', err.response?.data || err.message);
    process.exit(1);
});
