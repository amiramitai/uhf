const { ethers } = require('ethers');

const rpcs = [
    'https://polygon.llamarpc.com',
    'https://polygon-bor-rpc.publicnode.com',
    'https://1rpc.io/matic',
    'https://polygon.drpc.org',
    'https://rpc.ankr.com/polygon',
];

const addr = '0x82A877BA14B5b42FD1727aECc6b238F361343612';

async function main() {
    for (const url of rpcs) {
        try {
            const p = new ethers.JsonRpcProvider(url, 137, { staticNetwork: true });
            const b = await Promise.race([
                p.getBalance(addr),
                new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 5000))
            ]);
            console.log(`${url} -> ${ethers.formatEther(b)} MATIC`);
            console.log(`WORKING_RPC=${url}`);
            return;
        } catch (e) {
            console.log(`${url} -> FAILED: ${e.message?.slice(0, 60)}`);
        }
    }
    console.log('All RPCs failed');
}

main();
