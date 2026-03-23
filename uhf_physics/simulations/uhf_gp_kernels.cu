// =====================================================================
// UHF Gross-Pitaevskii CUDA Kernels
// =====================================================================
// Time-dependent 3D GP equation with FINITE speed of sound:
//   i hbar d_psi/dt = [-hbar^2/(2m) nabla^2 + g|psi|^2 - mu] psi
//
// In natural units (hbar=m=1, xi=1/sqrt(2)):
//   i d_psi/dt = [-0.5 nabla^2 + g|psi|^2 - mu] psi
//
// Speed of sound: c_s = sqrt(g*rho0/m) = sqrt(g*rho0) (finite!)
// Healing length: xi = hbar / sqrt(2*m*g*rho0) = 1/sqrt(2*g*rho0)
//
// Split-step spectral method:
//   Step 1: Nonlinear half-kick  psi *= exp(-i dt/2 * V_nl)
//   Step 2: FFT -> k-space
//   Step 3: Linear full-kick     psi_k *= exp(-i dt * k^2/2)
//   Step 4: IFFT -> x-space
//   Step 5: Nonlinear half-kick  psi *= exp(-i dt/2 * V_nl)
// =====================================================================

#define PI_F 3.14159265358979323846f

// -------------------------------------------------------------------
// Kernel: Nonlinear half-step
//   psi *= exp(-i * (dt/2) * (g*|psi|^2 - mu))
// Applied in-place to the complex field (interleaved Re,Im)
// -------------------------------------------------------------------
extern "C" __global__
void nonlinear_halfstep(
    float* __restrict__ psi_re,
    float* __restrict__ psi_im,
    float g, float mu, float half_dt,
    int N)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= N) return;

    float re = psi_re[idx];
    float im = psi_im[idx];
    float rho = re * re + im * im;
    float phase = -half_dt * (g * rho - mu);
    float c = cosf(phase);
    float s = sinf(phase);

    psi_re[idx] = re * c - im * s;
    psi_im[idx] = re * s + im * c;
}

// -------------------------------------------------------------------
// Kernel: Linear (kinetic) full-step in k-space
//   psi_k *= exp(-i * dt * k^2 / 2)
// k^2 is precomputed and stored in k2_grid
// -------------------------------------------------------------------
extern "C" __global__
void kinetic_fullstep(
    float* __restrict__ psi_k_re,
    float* __restrict__ psi_k_im,
    const float* __restrict__ k2_grid,
    float dt,
    int N)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= N) return;

    float re = psi_k_re[idx];
    float im = psi_k_im[idx];
    float phase = -dt * 0.5f * k2_grid[idx];
    float c = cosf(phase);
    float s = sinf(phase);

    psi_k_re[idx] = re * c - im * s;
    psi_k_im[idx] = re * s + im * c;
}

// -------------------------------------------------------------------
// Kernel: Compute density field rho = |psi|^2
// -------------------------------------------------------------------
extern "C" __global__
void compute_density(
    const float* __restrict__ psi_re,
    const float* __restrict__ psi_im,
    float* __restrict__ rho,
    int N)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= N) return;

    float re = psi_re[idx];
    float im = psi_im[idx];
    rho[idx] = re * re + im * im;
}

// -------------------------------------------------------------------
// Kernel: Compute phase field theta = atan2(Im, Re)
// -------------------------------------------------------------------
extern "C" __global__
void compute_phase(
    const float* __restrict__ psi_re,
    const float* __restrict__ psi_im,
    float* __restrict__ theta,
    int N)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= N) return;

    theta[idx] = atan2f(psi_im[idx], psi_re[idx]);
}

// -------------------------------------------------------------------
// Kernel: Inject localized perturbation at vortex core
//   Displaces the vortex by shifting psi in a neighborhood of
//   (cx, cy, cz) with Gaussian envelope of width sigma.
//   Perturbation: psi(r) *= exp(i * delta_phase * G(r))
//   where G(r) = exp(-|r-r0|^2 / (2*sigma^2))
// -------------------------------------------------------------------
extern "C" __global__
void inject_perturbation(
    float* __restrict__ psi_re,
    float* __restrict__ psi_im,
    float cx, float cy, float cz,
    float sigma, float delta_phase,
    float x0, float y0, float z0,
    float dx,
    int Nx, int Ny, int Nz)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    int total = Nx * Ny * Nz;
    if (idx >= total) return;

    // 3D index from flat
    int iz = idx / (Nx * Ny);
    int rem = idx - iz * Nx * Ny;
    int iy = rem / Nx;
    int ix = rem - iy * Nx;

    float x = x0 + ix * dx;
    float y = y0 + iy * dx;
    float z = z0 + iz * dx;

    float rx = x - cx;
    float ry = y - cy;
    float rz = z - cz;
    float r2 = rx * rx + ry * ry + rz * rz;
    float sigma2 = sigma * sigma;

    float envelope = expf(-r2 / (2.0f * sigma2));
    float phase = delta_phase * envelope;
    float c = cosf(phase);
    float s = sinf(phase);

    float re = psi_re[idx];
    float im = psi_im[idx];
    psi_re[idx] = re * c - im * s;
    psi_im[idx] = re * s + im * c;
}

// -------------------------------------------------------------------
// Kernel: Sample density and phase on a spherical shell
//   For each sample point (given in Cartesian), trilinearly interpolate
//   the density and phase fields.
// -------------------------------------------------------------------
extern "C" __global__
void sample_sphere(
    const float* __restrict__ rho_field,
    const float* __restrict__ phase_field,
    const float* __restrict__ sample_x,
    const float* __restrict__ sample_y,
    const float* __restrict__ sample_z,
    float* __restrict__ out_rho,
    float* __restrict__ out_phase,
    float x0, float y0, float z0,
    float dx_inv,
    int Nx, int Ny, int Nz,
    int N_samples)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= N_samples) return;

    // Map to grid coordinates
    float gx = (sample_x[idx] - x0) * dx_inv;
    float gy = (sample_y[idx] - y0) * dx_inv;
    float gz = (sample_z[idx] - z0) * dx_inv;

    // Clamp to grid
    gx = fminf(fmaxf(gx, 0.0f), (float)(Nx - 2));
    gy = fminf(fmaxf(gy, 0.0f), (float)(Ny - 2));
    gz = fminf(fmaxf(gz, 0.0f), (float)(Nz - 2));

    int ix = (int)gx; int iy = (int)gy; int iz = (int)gz;
    float fx = gx - ix; float fy = gy - iy; float fz = gz - iz;

    // Trilinear interpolation for density
    float val = 0.0f;
    for (int dz = 0; dz < 2; dz++) {
        for (int dy = 0; dy < 2; dy++) {
            for (int dxi = 0; dxi < 2; dxi++) {
                int jx = ix + dxi;
                int jy = iy + dy;
                int jz = iz + dz;
                int jidx = jz * Nx * Ny + jy * Nx + jx;
                float wx = dxi ? fx : (1.0f - fx);
                float wy = dy ? fy : (1.0f - fy);
                float wz = dz ? fz : (1.0f - fz);
                val += wx * wy * wz * rho_field[jidx];
            }
        }
    }
    out_rho[idx] = val;

    // Same for phase
    val = 0.0f;
    for (int dz = 0; dz < 2; dz++) {
        for (int dy = 0; dy < 2; dy++) {
            for (int dxi = 0; dxi < 2; dxi++) {
                int jx = ix + dxi;
                int jy = iy + dy;
                int jz = iz + dz;
                int jidx = jz * Nx * Ny + jy * Nx + jx;
                float wx = dxi ? fx : (1.0f - fx);
                float wy = dy ? fy : (1.0f - fy);
                float wz = dz ? fz : (1.0f - fz);
                val += wx * wy * wz * phase_field[jidx];
            }
        }
    }
    out_phase[idx] = val;
}

// -------------------------------------------------------------------
// Kernel: Deinterleave complex CuPy FFT output (complex64)
//   CuPy FFT returns interleaved [re0,im0,re1,im1,...].
//   Split into separate re[] and im[] arrays for our kernels.
// -------------------------------------------------------------------
extern "C" __global__
void deinterleave_complex(
    const float* __restrict__ interleaved,
    float* __restrict__ out_re,
    float* __restrict__ out_im,
    int N)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= N) return;
    out_re[idx] = interleaved[2 * idx];
    out_im[idx] = interleaved[2 * idx + 1];
}

// -------------------------------------------------------------------
// Kernel: Interleave separate re/im into complex64
//   Pack re[] and im[] back into [re0,im0,re1,im1,...] for IFFT.
// -------------------------------------------------------------------
extern "C" __global__
void interleave_complex(
    const float* __restrict__ in_re,
    const float* __restrict__ in_im,
    float* __restrict__ interleaved,
    int N)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= N) return;
    interleaved[2 * idx] = in_re[idx];
    interleaved[2 * idx + 1] = in_im[idx];
}

// -------------------------------------------------------------------
// Kernel: Imprint trefoil vortex T(2,3)
//   For each grid point, compute min distance to the curve and
//   accumulated solid angle (phase winding).
//   Curve points are in constant memory-sized arrays passed as args.
// -------------------------------------------------------------------
extern "C" __global__
void imprint_trefoil_kernel(
    float* __restrict__ psi_re,
    float* __restrict__ psi_im,
    const float* __restrict__ curve_x,
    const float* __restrict__ curve_y,
    const float* __restrict__ curve_z,
    int N_curve,
    float x0, float dx, int Nx, int Ny, int Nz,
    float xi, float rho0)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    int total = Nx * Ny * Nz;
    if (idx >= total) return;

    int iz = idx / (Nx * Ny);
    int rem = idx - iz * Nx * Ny;
    int iy = rem / Nx;
    int ix = rem - iy * Nx;

    float x = x0 + ix * dx;
    float y = x0 + iy * dx;
    float z = x0 + iz * dx;

    // Find minimum distance to curve
    float dist2_min = 1e10f;
    float phase_acc = 0.0f;

    float xi2 = xi * xi;
    float reg2 = 4.0f * xi2;  // regularization for phase

    for (int s = 0; s < N_curve; s++) {
        float cx = curve_x[s];
        float cy = curve_y[s];
        float cz = curve_z[s];

        float rx = x - cx;
        float ry = y - cy;
        float rz = z - cz;

        float d2 = rx * rx + ry * ry + rz * rz;
        if (d2 < dist2_min) dist2_min = d2;

        // Phase winding: use tangent vector for solid angle
        int s_next = (s + 1) % N_curve;
        float tx = curve_x[s_next] - cx;
        float ty = curve_y[s_next] - cy;
        float tz = curve_z[s_next] - cz;
        float t_norm = sqrtf(tx*tx + ty*ty + tz*tz) + 1e-12f;

        // Perpendicular distance
        float rdott = (rx*tx + ry*ty + rz*tz) / (t_norm*t_norm);
        float px = rx - rdott*tx;
        float py = ry - rdott*ty;
        float pz = rz - rdott*tz;
        float rp2 = px*px + py*py + pz*pz + reg2;

        phase_acc += atan2f(ry*tz - rz*ty, rx*t_norm) * t_norm / rp2 * reg2;
    }

    float dist = sqrtf(dist2_min);
    float rho_frac = tanhf(dist / (1.41421356f * xi));
    float amp = sqrtf(fmaxf(rho_frac * rho_frac * rho0, 1e-8f));

    psi_re[idx] = amp * cosf(phase_acc);
    psi_im[idx] = amp * sinf(phase_acc);
}

// -------------------------------------------------------------------
// Kernel: GP RHS with 4th-order central finite-difference Laplacian
//   Computes dpsi/dt = -i * H * psi   where H = -0.5*nabla^2 + V
//   V = g*|psi|^2 - mu
//
//   4th-order stencil for f''(x):
//     [-f_{i-2} + 16*f_{i-1} - 30*f_i + 16*f_{i+1} - f_{i+2}]/(12*dx^2)
//
//   Resulting equations (split real/imag):
//     dpsi_re/dt = -0.5 * lap_im + V * psi_im
//     dpsi_im/dt = +0.5 * lap_re - V * psi_re
//
//   STRICT LOCALITY: stencil reach = 2 cells.  Information propagates
//   at most 2*dx per dt.  No FFT, no global coupling.
// -------------------------------------------------------------------
extern "C" __global__
void gp_rhs_fd4(
    const float* __restrict__ psi_re,
    const float* __restrict__ psi_im,
    float* __restrict__ dpsi_re,
    float* __restrict__ dpsi_im,
    float g, float mu, float inv_12dx2,
    int Nx, int Ny, int Nz)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    int total = Nx * Ny * Nz;
    if (idx >= total) return;

    int NxNy = Nx * Ny;
    int iz = idx / NxNy;
    int rem = idx - iz * NxNy;
    int iy = rem / Nx;
    int ix = rem - iy * Nx;

    float cre = psi_re[idx];
    float cim = psi_im[idx];

    // ---- 4th-order Laplacian along x ----
    int ixm2 = (ix + Nx - 2) % Nx;
    int ixm1 = (ix + Nx - 1) % Nx;
    int ixp1 = (ix + 1) % Nx;
    int ixp2 = (ix + 2) % Nx;
    int base_yz = iz * NxNy + iy * Nx;

    float lap_re = -psi_re[base_yz + ixm2] + 16.0f * psi_re[base_yz + ixm1]
                   - 30.0f * cre
                   + 16.0f * psi_re[base_yz + ixp1] - psi_re[base_yz + ixp2];
    float lap_im = -psi_im[base_yz + ixm2] + 16.0f * psi_im[base_yz + ixm1]
                   - 30.0f * cim
                   + 16.0f * psi_im[base_yz + ixp1] - psi_im[base_yz + ixp2];

    // ---- 4th-order Laplacian along y ----
    int iym2 = (iy + Ny - 2) % Ny;
    int iym1 = (iy + Ny - 1) % Ny;
    int iyp1 = (iy + 1) % Ny;
    int iyp2 = (iy + 2) % Ny;

    int base_xz = iz * NxNy + ix;
    lap_re += -psi_re[base_xz + iym2 * Nx] + 16.0f * psi_re[base_xz + iym1 * Nx]
              - 30.0f * cre
              + 16.0f * psi_re[base_xz + iyp1 * Nx] - psi_re[base_xz + iyp2 * Nx];
    lap_im += -psi_im[base_xz + iym2 * Nx] + 16.0f * psi_im[base_xz + iym1 * Nx]
              - 30.0f * cim
              + 16.0f * psi_im[base_xz + iyp1 * Nx] - psi_im[base_xz + iyp2 * Nx];

    // ---- 4th-order Laplacian along z ----
    int izm2 = (iz + Nz - 2) % Nz;
    int izm1 = (iz + Nz - 1) % Nz;
    int izp1 = (iz + 1) % Nz;
    int izp2 = (iz + 2) % Nz;

    int base_xy = iy * Nx + ix;
    lap_re += -psi_re[izm2 * NxNy + base_xy] + 16.0f * psi_re[izm1 * NxNy + base_xy]
              - 30.0f * cre
              + 16.0f * psi_re[izp1 * NxNy + base_xy] - psi_re[izp2 * NxNy + base_xy];
    lap_im += -psi_im[izm2 * NxNy + base_xy] + 16.0f * psi_im[izm1 * NxNy + base_xy]
              - 30.0f * cim
              + 16.0f * psi_im[izp1 * NxNy + base_xy] - psi_im[izp2 * NxNy + base_xy];

    // Scale: lap *= 1/(12*dx^2)
    lap_re *= inv_12dx2;
    lap_im *= inv_12dx2;

    // Nonlinear potential V = g*|psi|^2 - mu
    float rho = cre * cre + cim * cim;
    float V = g * rho - mu;

    // GP RHS:  dpsi/dt = -i * H * psi
    //   H*psi = (-0.5*lap + V*psi)
    //   Re(H*psi) = -0.5*lap_re + V*cre
    //   Im(H*psi) = -0.5*lap_im + V*cim
    //   -i*(A + iB) = B - iA
    dpsi_re[idx] = -0.5f * lap_im + V * cim;
    dpsi_im[idx] =  0.5f * lap_re - V * cre;
}

// -------------------------------------------------------------------
// Kernel: Combined phase twist + density void injection
//   Phase: psi *= exp(i * delta_phase * G(r))
//   Void:  psi *= sqrt(void_min + (1 - void_min) * (1 - G(r)))
//   where  G(r) = exp(-|r-r0|^2 / (2*sigma^2))
//
//   At center: amplitude *= sqrt(void_min) => rho *= void_min
//   Phase kick = delta_phase at center, decays Gaussian.
//   This creates a massive density shockwave (cavity collapse).
// -------------------------------------------------------------------
extern "C" __global__
void inject_phase_and_void(
    float* __restrict__ psi_re,
    float* __restrict__ psi_im,
    float cx, float cy, float cz,
    float sigma, float delta_phase, float void_min,
    float x0, float y0, float z0,
    float dx,
    int Nx, int Ny, int Nz)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    int total = Nx * Ny * Nz;
    if (idx >= total) return;

    int NxNy = Nx * Ny;
    int iz = idx / NxNy;
    int rem = idx - iz * NxNy;
    int iy = rem / Nx;
    int ix = rem - iy * Nx;

    float x = x0 + ix * dx;
    float y = y0 + iy * dx;
    float z = z0 + iz * dx;

    float rx = x - cx;
    float ry = y - cy;
    float rz = z - cz;
    float r2 = rx * rx + ry * ry + rz * rz;
    float sigma2 = sigma * sigma;

    float envelope = expf(-r2 / (2.0f * sigma2));

    // Phase rotation
    float phase = delta_phase * envelope;
    float c = cosf(phase);
    float s = sinf(phase);

    float re = psi_re[idx];
    float im = psi_im[idx];
    float re2 = re * c - im * s;
    float im2 = re * s + im * c;

    // Density void: scale amplitude by sqrt(density_factor)
    //   density_factor = void_min + (1 - void_min)*(1 - envelope)
    //                  = 1 - (1 - void_min)*envelope
    float dfactor = 1.0f - (1.0f - void_min) * envelope;
    float amp_scale = sqrtf(fmaxf(dfactor, 1e-6f));

    psi_re[idx] = re2 * amp_scale;
    psi_im[idx] = im2 * amp_scale;
}


// -------------------------------------------------------------------
// inject_compact_void:
//   Same as inject_phase_and_void but with COMPACT support.
//   Uses C^inf bump function: f(r) = exp(1 - 1/(1 - (r/R_cut)^2))
//   EXACTLY ZERO for r >= R_cut. No tails whatsoever.
//   This eliminates local-drift contamination at measurement shells.
// -------------------------------------------------------------------
extern "C" __global__
void inject_compact_void(
    float* __restrict__ psi_re,
    float* __restrict__ psi_im,
    float cx, float cy, float cz,
    float R_cut, float delta_phase, float void_min,
    float x0, float y0, float z0,
    float dx,
    int Nx, int Ny, int Nz)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    int total = Nx * Ny * Nz;
    if (idx >= total) return;

    int NxNy = Nx * Ny;
    int iz = idx / NxNy;
    int rem = idx - iz * NxNy;
    int iy = rem / Nx;
    int ix = rem - iy * Nx;

    float x = x0 + ix * dx;
    float y = y0 + iy * dx;
    float z = z0 + iz * dx;

    float rx = x - cx;
    float ry = y - cy;
    float rz = z - cz;
    float r2 = rx * rx + ry * ry + rz * rz;
    float R2 = R_cut * R_cut;

    // Strictly zero outside R_cut
    if (r2 >= R2) return;

    float u = r2 / R2;   // u in [0, 1)

    // C^inf bump: exp(1 - 1/(1 - u))
    // At u=0: exp(0) = 1.  As u->1: exp(-inf) = 0.
    float envelope = expf(1.0f - 1.0f / (1.0f - u));

    // Phase rotation
    float angle = delta_phase * envelope;
    float c = cosf(angle);
    float s = sinf(angle);

    float re = psi_re[idx];
    float im = psi_im[idx];
    float re2 = re * c - im * s;
    float im2 = re * s + im * c;

    // Density void
    float dfactor = 1.0f - (1.0f - void_min) * envelope;
    float amp_scale = sqrtf(fmaxf(dfactor, 1e-6f));

    psi_re[idx] = re2 * amp_scale;
    psi_im[idx] = im2 * amp_scale;
}
