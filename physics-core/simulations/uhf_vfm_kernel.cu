#define PI 3.14159265358979323846f

// Rosenhead regularization parameter to avoid singularity at r=0
#define DELTA 1.0e-2f

// ------------------------------------------------------------------
// Kernel: Biot-Savart Law (N-Body)
// ------------------------------------------------------------------
// Calculates the velocity at each node induced by all other segments.
// Complexity: O(N^2) - "Every node feels every other node"
// ------------------------------------------------------------------
extern "C" __global__
void biot_savart_general_kernel(
    const float* __restrict__ src_x, const float* __restrict__ src_y, const float* __restrict__ src_z,
    const float* __restrict__ src_gamma, const int* __restrict__ src_next, int N_src,
    const float* __restrict__ trg_x, const float* __restrict__ trg_y, const float* __restrict__ trg_z,
    float* __restrict__ out_vx, float* __restrict__ out_vy, float* __restrict__ out_vz, int N_trg)
{
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= N_trg) return;

    float px = trg_x[i];
    float py = trg_y[i];
    float pz = trg_z[i];

    float vx = 0.0f;
    float vy = 0.0f;
    float vz = 0.0f;

    for (int j = 0; j < N_src; j++) {
        int k = src_next[j];
        if (k < 0) continue; 

        float ax = src_x[j];  float ay = src_y[j];  float az = src_z[j];
        float bx = src_x[k];  float by = src_y[k];  float bz = src_z[k];

        float dlx = bx - ax;
        float dly = by - ay;
        float dlz = bz - az;

        float mx = 0.5f * (ax + bx);
        float my = 0.5f * (ay + by);
        float mz = 0.5f * (az + bz);

        float rx = px - mx;
        float ry = py - my;
        float rz = pz - mz;

        float r2 = rx*rx + ry*ry + rz*rz + DELTA*DELTA;
        float r_inv3 = rsqrtf(r2 * r2 * r2);

        float cx = dly * rz - dlz * ry;
        float cy = dlz * rx - dlx * rz;
        float cz = dlx * ry - dly * rx;

        float factor = (src_gamma[j] * 0.07957747f) * r_inv3; // 1/(4pi) ~ 0.079577

        vx += cx * factor;
        vy += cy * factor;
        vz += cz * factor;
    }

    out_vx[i] = vx;
    out_vy[i] = vy;
    out_vz[i] = vz;
}

// ------------------------------------------------------------------
// Kernel: Perturb Knot
// ------------------------------------------------------------------
// Applies a localized high-frequency perturbation to a subset of nodes
// ------------------------------------------------------------------
extern "C" __global__
void perturb_kernel(
    float* pos_x, float* pos_y, float* pos_z,
    int start_idx, int end_idx,
    float time, float amplitude, float frequency)
{
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < start_idx || i >= end_idx) return;

    // Apply a specialized "kink" perturbation
    float phase = frequency * time + (float)(i - start_idx) * 0.5f;
    float envelope = 1.0f / (1.0f + 0.001f * (float)((i - start_idx) * (i - start_idx)));
    float perturbation = amplitude * sinf(phase) * envelope;

    // Perturb in Z direction for simplicity
    pos_z[i] += perturbation;
}

// ------------------------------------------------------------------
// Kernel: Boundary Holonomy (Wilson Loop)
// ------------------------------------------------------------------
// Computes the phase accumulation (velocity circulation) along a
// fixed 2D boundary surface dV enclosing the tangle.
// In the VFM, the phase phi is related to the velocity potential.
// The holonomy is integral v . dl around the loop.
// ------------------------------------------------------------------
extern "C" __global__
void boundary_holonomy_kernel(
    const float* __restrict__ vel_x,
    const float* __restrict__ vel_y,
    const float* __restrict__ vel_z,
    const float* __restrict__ boundary_x,
    const float* __restrict__ boundary_y,
    const float* __restrict__ boundary_z,
    float* __restrict__ partial_sums,
    int boundary_points)
{
    // This kernel assumes we have computed 'vel' on the boundary points too.
    // So 'vel' arrays usually must include the boundary probes.
    
    // Simple parallel reduction
    extern __shared__ float sdata[];
    
    int tid = threadIdx.x;
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    
    float v_dot_dl = 0.0f;

    if (i < boundary_points) {
        int next = (i + 1) % boundary_points;
        float dx = boundary_x[next] - boundary_x[i];
        float dy = boundary_y[next] - boundary_y[i];
        float dz = boundary_z[next] - boundary_z[i];
        
        // v . dl (Trapezoidal integration)
        // Velocity at boundary point i is needed. 
        // We assume vel_x contains velocity field evaluated at boundary nodes.
        v_dot_dl = vel_x[i] * dx + vel_y[i] * dy + vel_z[i] * dz;
    }

    sdata[tid] = v_dot_dl;
    __syncthreads();

    // Reduction in shared memory
    for (unsigned int s = blockDim.x / 2; s > 0; s >>= 1) {
        if (tid < s) {
            sdata[tid] += sdata[tid + s];
        }
        __syncthreads();
    }

    if (tid == 0) partial_sums[blockIdx.x] = sdata[0];
}
