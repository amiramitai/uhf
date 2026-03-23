// worker-trefoil.js — Trefoil Reconnection: slider-driven snapshot
// Generates trefoil knot geometry at any decay level (deterministic, no iteration)

self.onmessage = function(e) {
    if (e.data.type === 'compute' || e.data.type === 'snapshot') {
        var N = e.data.resolution || 150;
        var decay = (e.data.decay !== undefined) ? e.data.decay : 1.0;
        var points = [];
        for (var i = 0; i < N; i++) {
            var t = (i / N) * 2 * Math.PI;
            var x = (Math.sin(t) + 2 * Math.sin(2 * t)) * decay;
            var y = (Math.cos(t) - 2 * Math.cos(2 * t)) * decay;
            var z = (-Math.sin(3 * t)) * decay;
            if (decay < 1) {
                var d = 1 - decay;
                x += d * 1.5 * Math.sin(7*t + d*5) * (0.5 + 0.5*Math.cos(3*t));
                y += d * 1.5 * Math.cos(5*t + d*3) * (0.5 + 0.5*Math.sin(4*t));
                z += d * 2.0 * Math.sin(9*t + d*7) * Math.cos(2*t);
                if (d > 0.4) {
                    x += d*d * Math.sin(23*t) * 0.8;
                    y += d*d * Math.cos(19*t) * 0.8;
                    z += d*d * Math.sin(17*t) * 0.8;
                }
            }
            points.push({ x: x, y: y, z: z });
        }
        var H = 0, H0 = 0;
        for (var i = 0; i < N; i++) {
            var p = points[i], pn = points[(i+1) % N];
            H += (pn.x-p.x)*(pn.x-p.x) + (pn.y-p.y)*(pn.y-p.y) + (pn.z-p.z)*(pn.z-p.z);
            var t0 = (i / N) * 2 * Math.PI, t1 = ((i+1) / N) * 2 * Math.PI;
            var x0 = Math.sin(t0)+2*Math.sin(2*t0), x1 = Math.sin(t1)+2*Math.sin(2*t1);
            var y0 = Math.cos(t0)-2*Math.cos(2*t0), y1 = Math.cos(t1)-2*Math.cos(2*t1);
            var z0 = -Math.sin(3*t0), z1 = -Math.sin(3*t1);
            H0 += (x1-x0)*(x1-x0) + (y1-y0)*(y1-y0) + (z1-z0)*(z1-z0);
        }
        var conservation = ((H - H0) / H0) * 100;
        self.postMessage({
            type: e.data.type === 'snapshot' ? 'snapshot' : 'geometry',
            points: points, conservation: conservation, decay: decay
        });
    }
};
