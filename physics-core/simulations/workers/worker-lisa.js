// worker-lisa.js — LISA Acoustic Echo: FDTD wave equation in gravastar cavity
// Pre-computes the full time evolution for slider-driven playback
// Echo period = 2*R/c tuned to ~44.7 time units

self.onmessage = function(e) {
    if (e.data.type === 'precompute') {
        var Nr = e.data.resolution || 200;
        var rMax = 20.0;
        var dr = rMax / Nr;
        var c = 2 * rMax / 44.7;
        var CFL = 0.8;
        var dt = CFL * dr / c;
        var nFrames = e.data.frames || 400;
        var stepsPerFrame = e.data.stepsPerFrame || 8;

        var u = new Float64Array(Nr);
        var uPrev = new Float64Array(Nr);
        var uNext = new Float64Array(Nr);

        var r0 = 5.0, sigma2 = 1.5, amp = 3.0;
        for (var i = 0; i < Nr; i++) {
            var r = (i + 0.5) * dr;
            u[i] = amp * Math.exp(-((r - r0) * (r - r0)) / sigma2);
            uPrev[i] = amp * Math.exp(-((r - r0 - c * dt) * (r - r0 - c * dt)) / sigma2);
        }

        var alpha2 = (c * dt / dr) * (c * dt / dr);
        var frames = [];
        var time = 0;
        var damping = 0.99998;

        var rho0 = [];
        for (var i = 0; i < Nr; i++) rho0.push(1.0 + u[i]);
        frames.push({ rho: rho0, coreDensity: 1.0 + u[1], time: 0 });

        for (var f = 0; f < nFrames - 1; f++) {
            for (var s = 0; s < stepsPerFrame; s++) {
                for (var i = 1; i < Nr - 1; i++) {
                    uNext[i] = 2 * u[i] - uPrev[i] + alpha2 * (u[i+1] - 2*u[i] + u[i-1]);
                    uNext[i] *= damping;
                }
                uNext[0] = uNext[1];
                uNext[Nr-1] = uNext[Nr-2];
                for (var i = 0; i < Nr; i++) {
                    uPrev[i] = u[i];
                    u[i] = uNext[i];
                }
                time += dt;
            }
            var rho = [];
            for (var i = 0; i < Nr; i++) rho.push(1.0 + u[i]);
            frames.push({ rho: rho, coreDensity: 1.0 + u[1], time: time });
        }

        self.postMessage({
            type: 'precomputed', frames: frames,
            totalTime: time, dr: dr, rMax: rMax
        });
    }
};
