"""
UHF LIGO Hunter — Matched-Filter Search: GR vs UHF Dispersive Template
========================================================================
Downloads real LIGO Hanford (H1) strain data for GW150914 and runs a
matched-filter comparison between:

  1. Standard GR template   (IMRPhenomD, m1=36 M_sun, m2=29 M_sun)
  2. UHF Dispersive template (GR + Bogoliubov phase: +16.67 μs lead)

The UHF viscoelastic vacuum model predicts a frequency-dependent
dispersion arising from the superfluid phonon branch:

    δt(f) = δt_max · (f² − f_carrier²) / (f_max² − f_carrier²)

which imprints an additional phase Δφ = 2π f δt(f) on the waveform.
"""

from __future__ import annotations

import numpy as np
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend for WSL
import matplotlib.pyplot as plt

from gwpy.timeseries import TimeSeries
from pycbc.waveform import get_fd_waveform
from pycbc.filter import matched_filter
from pycbc.psd import interpolate, inverse_spectrum_truncation
from pycbc.types import FrequencySeries

print("=" * 70)
print("  UHF Empirical Strike — Matched-Filter on GWOSC GW150914 Data")
print("=" * 70)

# ─────────────────────────────────────────────────────────────────────
#  1. Download real LIGO data for GW150914 (Hanford detector)
# ─────────────────────────────────────────────────────────────────────
print("\n[1] Downloading GW150914 raw strain data (H1, 32s segment)...")
data = TimeSeries.fetch_open_data('H1', 1126259446, 1126259478)
pycbc_data = data.to_pycbc()
print(f"    Sample rate : {pycbc_data.sample_rate} Hz")
print(f"    Duration    : {pycbc_data.duration} s")
print(f"    Data points : {len(pycbc_data):,}")

# ─────────────────────────────────────────────────────────────────────
#  2. Condition the data (PSD estimation and whitening)
# ─────────────────────────────────────────────────────────────────────
print("\n[2] Conditioning data (PSD estimation, whitening)...")
psd = pycbc_data.psd(4)
psd = interpolate(psd, pycbc_data.delta_f)
psd = inverse_spectrum_truncation(
    psd,
    int(4 * pycbc_data.sample_rate),
    low_frequency_cutoff=20.0
)
print(f"    PSD delta_f : {psd.delta_f} Hz")
print(f"    PSD length  : {len(psd):,}")

# ─────────────────────────────────────────────────────────────────────
#  3. Generate the standard General Relativity (GR) Template
# ─────────────────────────────────────────────────────────────────────
print("\n[3] Generating standard GR template (IMRPhenomD)...")
mass1 = 36.0  # Solar masses
mass2 = 29.0  # Solar masses

hp_gr, _ = get_fd_waveform(
    approximant="IMRPhenomD",
    mass1=mass1,
    mass2=mass2,
    delta_f=pycbc_data.delta_f,
    f_lower=20.0
)
hp_gr.resize(len(pycbc_data) // 2 + 1)
print(f"    Masses      : {mass1} + {mass2} M_sun")
print(f"    Approximant : IMRPhenomD")
print(f"    Template len: {len(hp_gr):,} freq bins")

# ─────────────────────────────────────────────────────────────────────
#  4. Generate the UHF Dispersive Template (Bogoliubov phase injection)
# ─────────────────────────────────────────────────────────────────────
print("\n[4] Generating UHF Dispersive template (Bogoliubov phase)...")
f = hp_gr.sample_frequencies
uhf_phase_shift = np.zeros(len(f))

f_carrier = 200.0           # Hz — carrier frequency
f_max     = 1000.0           # Hz — normalisation frequency
delta_t_max = 16.67e-6       # 16.67 microseconds anomalous lead

for i, freq in enumerate(f):
    if freq > 20.0:
        time_shift = delta_t_max * (
            (freq**2 - f_carrier**2) / (f_max**2 - f_carrier**2)
        )
        uhf_phase_shift[i] = 2.0 * np.pi * freq * time_shift

hp_uhf = FrequencySeries(
    hp_gr.numpy() * np.exp(1j * uhf_phase_shift),
    delta_f=hp_gr.delta_f
)
print(f"    δt_max      : {delta_t_max*1e6:.2f} μs")
print(f"    f_carrier   : {f_carrier} Hz")
print(f"    Max phase   : {np.max(np.abs(uhf_phase_shift)):.4f} rad")

# ─────────────────────────────────────────────────────────────────────
#  5. Run the Matched Filters against real LIGO noise
# ─────────────────────────────────────────────────────────────────────
print("\n[5] Running Matched Filters against real LIGO noise...")

snr_gr = matched_filter(
    hp_gr, pycbc_data, psd=psd, low_frequency_cutoff=20.0
)
snr_uhf = matched_filter(
    hp_uhf, pycbc_data, psd=psd, low_frequency_cutoff=20.0
)

# Crop edges to avoid filter transients
snr_gr  = snr_gr.crop(4, 4)
snr_uhf = snr_uhf.crop(4, 4)

peak_gr  = abs(snr_gr).max()
peak_uhf = abs(snr_uhf).max()

print("\n" + "=" * 70)
print("  RESULTS — Matched-Filter Peak SNR")
print("=" * 70)
print(f"  Standard GR  Template Max SNR : {peak_gr:.4f}")
print(f"  UHF Dispersive Template Max SNR: {peak_uhf:.4f}")
print(f"  Δ(SNR)                         : {peak_uhf - peak_gr:+.4f}")

if peak_uhf > peak_gr:
    improvement = ((peak_uhf - peak_gr) / peak_gr) * 100
    print(f"\n  >>> UHF Template outperforms GR on raw data!")
    print(f"  >>> Improvement: {improvement:.2f}%")
else:
    deficit = ((peak_gr - peak_uhf) / peak_gr) * 100
    print(f"\n  GR Template outperformed by {deficit:.2f}%.")
    print(f"  The +16.67 μs dispersion may be below the O1 noise floor,")
    print(f"  or τ_M requires adjustment.")

print("=" * 70)

# ─────────────────────────────────────────────────────────────────────
#  6. Plotting the Comparison
# ─────────────────────────────────────────────────────────────────────
print("\n[6] Generating comparison plot...")

fig, ax = plt.subplots(figsize=(10, 5))
ax.plot(snr_gr.sample_times, abs(snr_gr),
        label='GR Template', alpha=0.8, linewidth=0.6)
ax.plot(snr_uhf.sample_times, abs(snr_uhf),
        label='UHF Template', linestyle='dashed', alpha=0.9, linewidth=0.6)

ax.set_xlim(1126259462.0, 1126259463.0)
ax.set_title('Matched Filter SNR: GR vs. UHF on GW150914 (H1)')
ax.set_xlabel('GPS Time (s)')
ax.set_ylabel('Signal-to-Noise Ratio (SNR)')
ax.legend(loc='upper left')
ax.grid(True, alpha=0.3)

plt.tight_layout()
plt.savefig('UHF_vs_GR_LIGO_GW150914.png', dpi=300)
print("  Plot saved as 'UHF_vs_GR_LIGO_GW150914.png'")

# ─────────────────────────────────────────────────────────────────────
#  7. Detailed diagnostics
# ─────────────────────────────────────────────────────────────────────
print("\n" + "-" * 70)
print("  Diagnostics")
print("-" * 70)

# Times of peak SNR
idx_gr  = np.argmax(np.array(abs(snr_gr)))
idx_uhf = np.argmax(np.array(abs(snr_uhf)))
t_peak_gr  = float(snr_gr.sample_times[idx_gr])
t_peak_uhf = float(snr_uhf.sample_times[idx_uhf])

print(f"  GR  peak time : {t_peak_gr:.6f} s")
print(f"  UHF peak time : {t_peak_uhf:.6f} s")
print(f"  Time offset   : {(t_peak_uhf - t_peak_gr)*1e6:+.1f} μs")

# Phase at peak
phase_gr  = float(np.angle(snr_gr[idx_gr]))
phase_uhf = float(np.angle(snr_uhf[idx_uhf]))
print(f"  GR  peak phase: {phase_gr:.4f} rad")
print(f"  UHF peak phase: {phase_uhf:.4f} rad")
print(f"  Phase diff    : {phase_uhf - phase_gr:+.4f} rad")

# Overlap (match) between templates
from pycbc.filter import overlap_cplx
match = abs(overlap_cplx(hp_gr, hp_uhf, psd=psd, low_frequency_cutoff=20.0,
                          normalized=True))
print(f"  Template match: {match:.6f}")
print(f"  Mismatch      : {1 - match:.2e}")

print("\n  Analysis complete.\n")
