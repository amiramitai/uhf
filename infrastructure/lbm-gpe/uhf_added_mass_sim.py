#!/usr/bin/env python3
"""
UHF Added-Mass Visualization — Real-Time 3D GPU Simulation
===========================================================
Demonstrates: Inertial Mass (m) is an Emergent Added-Mass Effect
              m = ρ_vacuum × V_defect

A topological vortex knot (trefoil) sits in a superfluid vacuum.
When pushed, it drags surrounding fluid — the entrained fluid IS
the inertial mass.  Higher ρ_vacuum → more entrained fluid → more
mass → slower acceleration under the same impulse.

Controls:
  - Slider: Vacuum Density ρ (0.1 – 2.0)
  - Button: Apply Impulse (push the knot)
  - Mouse: Orbit camera (LMB drag)
  - Scroll: Zoom
  - R: Reset knot to center
  - Space: Pause/resume

Requires: taichi (pip install taichi), RTX 3090
"""

import taichi as ti
import numpy as np
import math

ti.init(arch=ti.cuda, default_fp=ti.f32)

# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────
RES        = 1200                 # window resolution
N_KNOT     = 2000                 # vertices on trefoil knot
N_TUBE     = 24                   # tube cross-section samples
N_FLUID    = 400_000              # background fluid particles
N_ENTRAIN  = 200_000              # entrained-mass particles
DT         = 0.008                # physics timestep
IMPULSE_F  = 8.0                  # push force magnitude
DRAG_COEFF = 0.15                 # viscous-like damping on knot
KNOT_R     = 0.35                 # trefoil major radius
TUBE_R     = 0.045                # tube thickness

# Defect volume (constant) — trefoil tube cross-section × path length
# Trefoil path length ≈ 2π × R × √(p²+q²) for (p,q)=(2,3) torus knot
V_DEFECT   = 0.25                 # normalised volume units

# ─────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────
knot_pos      = ti.Vector.field(3, dtype=ti.f32, shape=())       # center of knot
knot_vel      = ti.Vector.field(3, dtype=ti.f32, shape=())       # knot velocity
knot_acc_disp = ti.field(dtype=ti.f32, shape=())                 # displayed acceleration

# Trefoil mesh
knot_verts   = ti.Vector.field(3, dtype=ti.f32, shape=(N_KNOT * N_TUBE,))
knot_colors  = ti.Vector.field(3, dtype=ti.f32, shape=(N_KNOT * N_TUBE,))
knot_indices = ti.field(dtype=ti.i32, shape=(N_KNOT * N_TUBE * 6,))

# Fluid particles
fluid_pos    = ti.Vector.field(3, dtype=ti.f32, shape=(N_FLUID,))
fluid_color  = ti.Vector.field(3, dtype=ti.f32, shape=(N_FLUID,))

# Entrained (added-mass) particles
entrain_pos   = ti.Vector.field(3, dtype=ti.f32, shape=(N_ENTRAIN,))
entrain_color = ti.Vector.field(3, dtype=ti.f32, shape=(N_ENTRAIN,))
entrain_alive = ti.field(dtype=ti.i32, shape=(N_ENTRAIN,))
n_entrain_vis = ti.field(dtype=ti.i32, shape=())

# Parameters (host-side, synced to GUI)
rho_vacuum  = 1.0   # will be controlled by slider
pushing     = False
paused      = False

# ─────────────────────────────────────────────────────────────
# Trefoil Knot Geometry
# ─────────────────────────────────────────────────────────────
@ti.func
def trefoil_point(t: ti.f32) -> ti.types.vector(3, ti.f32):
    """Trefoil knot parametric curve on torus, t in [0, 2π)."""
    R = KNOT_R
    r = KNOT_R * 0.45
    x = (R + r * ti.cos(3.0 * t)) * ti.cos(2.0 * t)
    y = (R + r * ti.cos(3.0 * t)) * ti.sin(2.0 * t)
    z = r * ti.sin(3.0 * t)
    return ti.Vector([x, y, z])

@ti.func
def trefoil_tangent(t: ti.f32) -> ti.types.vector(3, ti.f32):
    """Numerical tangent via finite difference."""
    eps = 1e-4
    return (trefoil_point(t + eps) - trefoil_point(t - eps)).normalized()

@ti.kernel
def build_knot_mesh(offset: ti.types.vector(3, ti.f32)):
    """Build trefoil tube mesh displaced by offset."""
    for i in range(N_KNOT):
        t = 2.0 * 3.14159265 * ti.cast(i, ti.f32) / ti.cast(N_KNOT, ti.f32)
        center = trefoil_point(t) + offset
        T = trefoil_tangent(t)

        # Build local frame (Frenet-like)
        up = ti.Vector([0.0, 0.0, 1.0])
        if ti.abs(T.dot(up)) > 0.95:
            up = ti.Vector([1.0, 0.0, 0.0])
        N_vec = T.cross(up).normalized()
        B = T.cross(N_vec).normalized()

        for j in range(N_TUBE):
            theta = 2.0 * 3.14159265 * ti.cast(j, ti.f32) / ti.cast(N_TUBE, ti.f32)
            local = N_vec * (TUBE_R * ti.cos(theta)) + B * (TUBE_R * ti.sin(theta))
            idx = i * N_TUBE + j
            knot_verts[idx] = center + local
            # Iridescent coloring based on tube angle and path position
            hue = (t / (2.0 * 3.14159265) + theta / (2.0 * 3.14159265)) * 0.5
            knot_colors[idx] = ti.Vector([
                0.15 + 0.85 * (0.5 + 0.5 * ti.sin(6.28 * hue)),
                0.15 + 0.55 * (0.5 + 0.5 * ti.sin(6.28 * hue + 2.09)),
                0.4 + 0.6 * (0.5 + 0.5 * ti.sin(6.28 * hue + 4.19))
            ])

    # Build triangle strip indices
    for i in range(N_KNOT):
        i_next = (i + 1) % N_KNOT
        for j in range(N_TUBE):
            j_next = (j + 1) % N_TUBE
            base = (i * N_TUBE + j) * 6
            knot_indices[base + 0] = i * N_TUBE + j
            knot_indices[base + 1] = i_next * N_TUBE + j
            knot_indices[base + 2] = i * N_TUBE + j_next
            knot_indices[base + 3] = i * N_TUBE + j_next
            knot_indices[base + 4] = i_next * N_TUBE + j
            knot_indices[base + 5] = i_next * N_TUBE + j_next

# ─────────────────────────────────────────────────────────────
# Background Fluid Particles
# ─────────────────────────────────────────────────────────────
@ti.kernel
def init_fluid_particles():
    for i in range(N_FLUID):
        # Uniform random in a cube [-1.5, 1.5]^3
        x = ti.random() * 3.0 - 1.5
        y = ti.random() * 3.0 - 1.5
        z = ti.random() * 3.0 - 1.5
        fluid_pos[i] = ti.Vector([x, y, z])
        fluid_color[i] = ti.Vector([0.2, 0.35, 0.55])

@ti.kernel
def update_fluid_particles(rho: ti.f32, kpos: ti.types.vector(3, ti.f32),
                           kvel: ti.types.vector(3, ti.f32)):
    """Advect fluid particles: slow Brownian drift + density-dependent
    brightness + exclusion from knot interior."""
    for i in range(N_FLUID):
        p = fluid_pos[i]

        # Gentle Brownian motion (represents thermal vacuum fluctuations)
        p += ti.Vector([ti.random() - 0.5, ti.random() - 0.5, ti.random() - 0.5]) * 0.003

        # Near the knot, get pushed/dragged by its motion
        dp = p - kpos
        dist = dp.norm() + 1e-6
        if dist < 0.8:
            # Velocity coupling — fluid near knot follows it
            coupling = ti.exp(-dist * 3.0) * 0.3
            p += kvel * DT * coupling

        # Wrap-around box
        for d in ti.static(range(3)):
            if p[d] > 1.5:
                p[d] -= 3.0
            elif p[d] < -1.5:
                p[d] += 3.0

        fluid_pos[i] = p

        # Brightness correlates to local ρ_vacuum (uniform base + fluctuation near knot)
        brightness = 0.08 * rho + 0.04 * rho * ti.exp(-dist * 2.0)
        brightness = ti.min(brightness, 0.8)
        fluid_color[i] = ti.Vector([
            0.15 * rho + brightness * 0.3,
            0.25 * rho + brightness * 0.4,
            0.35 * rho + brightness * 0.6
        ])

# ─────────────────────────────────────────────────────────────
# Entrained (Added-Mass) Particles
# ─────────────────────────────────────────────────────────────
@ti.kernel
def init_entrain_particles(kpos: ti.types.vector(3, ti.f32)):
    for i in range(N_ENTRAIN):
        entrain_pos[i] = kpos + ti.Vector([
            ti.random() - 0.5,
            ti.random() - 0.5,
            ti.random() - 0.5
        ]) * 0.01
        entrain_alive[i] = 0
        entrain_color[i] = ti.Vector([1.0, 0.8, 0.3])

@ti.kernel
def update_entrained(rho: ti.f32, kpos: ti.types.vector(3, ti.f32),
                     kvel: ti.types.vector(3, ti.f32), speed: ti.f32):
    """Entrained particles form a glowing cloud that follows the knot.
    Cloud size and particle count scale with ρ_vacuum (= added mass)."""
    # Entrained cloud radius scales as ρ^(1/3) × base_radius
    cloud_r = 0.15 + 0.55 * ti.pow(rho, 0.333)
    # Number of visible particles scales with ρ
    n_vis = ti.cast(ti.min(rho * 0.6, 1.0) * N_ENTRAIN, ti.i32)
    n_entrain_vis[None] = n_vis

    for i in range(N_ENTRAIN):
        if i < n_vis and speed > 0.01:
            entrain_alive[i] = 1
            p = entrain_pos[i]

            # Particles orbit and follow the knot
            dp = p - kpos
            dist = dp.norm() + 1e-6

            # Spring force toward knot center + random jitter
            spring = -dp * 2.0
            jitter = ti.Vector([
                ti.random() - 0.5,
                ti.random() - 0.5,
                ti.random() - 0.5
            ]) * 0.15

            # Follow knot velocity (rigidly dragged)
            p += kvel * DT + (spring + jitter) * DT

            # Respawn if too far
            if dist > cloud_r * 1.5:
                theta = ti.random() * 6.283
                phi = ti.acos(2.0 * ti.random() - 1.0)
                r = cloud_r * ti.pow(ti.random(), 0.333)
                p = kpos + ti.Vector([
                    r * ti.sin(phi) * ti.cos(theta),
                    r * ti.sin(phi) * ti.sin(theta),
                    r * ti.cos(phi)
                ])

            entrain_pos[i] = p

            # Glow color: brighter with higher ρ and closer to knot
            glow = ti.exp(-dist / cloud_r) * rho * 0.8
            entrain_color[i] = ti.Vector([
                0.95 + 0.05 * glow,
                0.6 + 0.3 * glow,
                0.1 + 0.4 * ti.exp(-glow)
            ])
        else:
            entrain_alive[i] = 0
            # Park invisible particles at knot center
            entrain_pos[i] = kpos

# ─────────────────────────────────────────────────────────────
# Physics: Added-Mass Dynamics
# ─────────────────────────────────────────────────────────────
@ti.kernel
def physics_step(force_x: ti.f32, force_y: ti.f32, force_z: ti.f32,
                 rho: ti.f32):
    """m = ρ × V_defect.  a = F/m.  Leapfrog integration."""
    m = rho * V_DEFECT
    if m < 0.01:
        m = 0.01  # prevent division by zero

    F = ti.Vector([force_x, force_y, force_z])
    v = knot_vel[None]
    # Drag force (viscous-like, stabilises motion)
    F_drag = -DRAG_COEFF * m * v
    a = (F + F_drag) / m

    knot_acc_disp[None] = a.norm()

    # Leapfrog
    v_new = v + a * DT
    knot_vel[None] = v_new
    knot_pos[None] = knot_pos[None] + v_new * DT

    # Soft boundary: keep knot in [-1.3, 1.3]
    pos = knot_pos[None]
    for d in ti.static(range(3)):
        if pos[d] > 1.3:
            pos[d] = 1.3
            v_new[d] = -0.3 * v_new[d]
        elif pos[d] < -1.3:
            pos[d] = -1.3
            v_new[d] = -0.3 * v_new[d]
    knot_pos[None] = pos
    knot_vel[None] = v_new

# ─────────────────────────────────────────────────────────────
# Main Loop
# ─────────────────────────────────────────────────────────────
def main():
    global rho_vacuum, pushing, paused

    # Initialise
    knot_pos[None] = [0.0, 0.0, 0.0]
    knot_vel[None] = [0.0, 0.0, 0.0]
    init_fluid_particles()
    init_entrain_particles(knot_pos[None])
    build_knot_mesh(knot_pos[None])

    # Window
    window = ti.ui.Window("UHF Added-Mass: m = ρ_vacuum × V_defect",
                          (RES, RES), vsync=True)
    canvas = window.get_canvas()
    scene  = window.get_scene()
    camera = ti.ui.Camera()
    camera.position(0.0, 0.0, 3.5)
    camera.lookat(0.0, 0.0, 0.0)
    camera.up(0.0, 1.0, 0.0)
    camera.projection_mode(ti.ui.ProjectionMode.Perspective)
    camera.fov(55)

    # GUI state
    impulse_cooldown = 0
    push_dir = np.array([1.0, 0.3, 0.0], dtype=np.float32)
    push_dir /= np.linalg.norm(push_dir)
    step_count = 0

    while window.running:
        # ── GUI Panel ──
        gui = window.get_gui()
        with gui.sub_window("UHF Controls", 0.01, 0.01, 0.38, 0.42) as w:
            rho_vacuum = w.slider_float("ρ_vacuum", rho_vacuum, 0.1, 2.0)
            m_emergent = rho_vacuum * V_DEFECT
            a_measured = knot_acc_disp[None]
            speed = (knot_vel[None].to_numpy() ** 2).sum() ** 0.5

            w.text(f"Vacuum Density  ρ = {rho_vacuum:.3f}")
            w.text(f"Defect Volume   V = {V_DEFECT:.3f}  (constant)")
            w.text(f"─────────────────────────────")
            w.text(f"Emergent Mass   m = ρ×V = {m_emergent:.4f}")
            w.text(f"Applied Force   F = {IMPULSE_F:.1f}")
            w.text(f"Predicted  a=F/m = {IMPULSE_F/m_emergent:.3f}")
            w.text(f"Measured Accel  a = {a_measured:.3f}")
            w.text(f"─────────────────────────────")
            w.text(f"Knot Speed      |v| = {speed:.4f}")
            w.text(f"Entrained Cloud ≈ {rho_vacuum*100:.0f}% capacity")
            w.text("")

            if w.button("  ▶ Apply Impulse (Push)  "):
                pushing = True
                impulse_cooldown = 30  # frames of push

            if w.button("  ⟲ Reset Position  "):
                knot_pos[None] = [0.0, 0.0, 0.0]
                knot_vel[None] = [0.0, 0.0, 0.0]

            if w.button("  ⏸ Pause / Resume  "):
                paused = not paused

            w.text("")
            w.text("Mouse-drag: orbit | Scroll: zoom")
            w.text("Try: Low ρ=0.2 → fast | High ρ=1.8 → slow")

        # ── Keyboard ──
        if window.get_event(ti.ui.PRESS):
            if window.event.key == ti.ui.SPACE:
                paused = not paused
            elif window.event.key == 'r':
                knot_pos[None] = [0.0, 0.0, 0.0]
                knot_vel[None] = [0.0, 0.0, 0.0]

        # ── Physics ──
        if not paused:
            # Compute force
            fx, fy, fz = 0.0, 0.0, 0.0
            if pushing and impulse_cooldown > 0:
                fx = IMPULSE_F * push_dir[0]
                fy = IMPULSE_F * push_dir[1]
                fz = IMPULSE_F * push_dir[2]
                impulse_cooldown -= 1
                if impulse_cooldown <= 0:
                    pushing = False
                    # Randomize next push direction for variety
                    push_dir = np.random.randn(3).astype(np.float32)
                    push_dir /= np.linalg.norm(push_dir) + 1e-8

            physics_step(fx, fy, fz, rho_vacuum)

            # Update knot mesh
            build_knot_mesh(knot_pos[None])

            # Update particles
            kp = knot_pos[None]
            kv = knot_vel[None]
            spd = (kv.to_numpy() ** 2).sum() ** 0.5
            update_fluid_particles(rho_vacuum, kp, kv)
            update_entrained(rho_vacuum, kp, kv, spd)

            step_count += 1

        # ── Camera ──
        camera.track_user_inputs(window, movement_speed=0.02, hold_key=ti.ui.LMB)
        scene.set_camera(camera)

        # ── Lighting ──
        scene.ambient_light((0.15, 0.15, 0.2))
        scene.point_light(pos=(2.0, 2.0, 3.0), color=(1.0, 1.0, 0.9))
        scene.point_light(pos=(-2.0, -1.0, 2.0), color=(0.3, 0.4, 0.7))

        # ── Render Background Fluid ──
        scene.particles(fluid_pos, radius=0.004,
                        per_vertex_color=fluid_color)

        # ── Render Entrained Cloud (Added-Mass glow) ──
        n_vis = n_entrain_vis[None]
        if n_vis > 0:
            scene.particles(entrain_pos, radius=0.008,
                            per_vertex_color=entrain_color)

        # ── Render Knot Mesh ──
        scene.mesh(knot_verts,
                   indices=knot_indices,
                   per_vertex_color=knot_colors,
                   two_sided=True)

        # ── Composite ──
        canvas.scene(scene)
        window.show()

    print("\nSimulation ended.")

if __name__ == "__main__":
    main()
