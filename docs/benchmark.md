# Payable — agent transactability benchmark

- **Tasks:** 29
- **Payments:** simulated (no RAZORPAY_KEY_ID)
- **Buyer reasoning:** deterministic-planner
- **Seed:** 1733
- **Injected decline rate:** 12%


## Headline

| Arm | Txn success | Wrong-item rate | Decision accuracy | p50 latency | p95 latency | HTTP calls/task |
|---|---|---|---|---|---|---|
| `payable` | 100.0% | 0.0% | 100.0% | 154 ms | 256 ms | 4.1 |
| `legacy-strict` | 52.6% | 9.1% | 65.5% | 60 ms | 198 ms | 4.1 |
| `legacy-optimistic` | 84.2% | 33.3% | 69.0% | 178 ms | 273 ms | 5.3 |

## Outcomes

| Arm | Purchases | Right | Wrong | Correct abstentions | Declines | Recovered | Money misspent |
|---|---|---|---|---|---|---|---|
| `payable` | 19 | 19 | 0 | 10/10 | 4 | 4 | ₹0.00 |
| `legacy-strict` | 11 | 10 | 1 | 9/10 | 1 | 1 | ₹6,478.00 |
| `legacy-optimistic` | 24 | 16 | 8 | 4/10 | 6 | 6 | ₹44,314.00 |

## Failure taxonomy

| Arm | Terminal failure codes |
|---|---|
| `payable` | `no_match`×4, `spec_mismatch`×3, `ambiguous`×2, `out_of_stock`×1 |
| `legacy-strict` | `parse_error`×12, `spec_mismatch`×5, `out_of_stock`×1 |
| `legacy-optimistic` | `spec_mismatch`×4, `out_of_stock`×1 |

## Variance across 5 seeds (1733–1737)

Selection is seed-independent by construction, so this spread is purely payment luck. Reported so that no single lucky seed carries the claim.

| Arm | Txn success | Wrong-item rate | Decision accuracy | Money misspent |
|---|---|---|---|---|
| `payable` | 94.7% ± 6.5 <br><sub>84–100</sub> | 0.0% ± 0.0 <br><sub>0–0</sub> | 96.6% ± 4.2 <br><sub>90–100</sub> | ₹0 <br><sub>₹0–₹0</sub> |
| `legacy-strict` | 49.5% ± 2.9 <br><sub>47–53</sub> | 7.6% ± 4.3 <br><sub>0–10</sub> | 64.1% ± 1.9 <br><sub>62–66</sub> | ₹5,182 <br><sub>₹0–₹6,478</sub> |
| `legacy-optimistic` | 80.0% ± 4.4 <br><sub>74–84</sub> | 33.3% ± 1.1 <br><sub>32–35</sub> | 67.6% ± 1.9 <br><sub>66–69</sub> | ₹42,411 <br><sub>₹37,836–₹44,314</sub> |

## Per-task detail

_Seed 1733 shown; the other 4 differ only in payment luck._

| Task | `payable` | `legacy-strict` | `legacy-optimistic` |
|---|---|---|---|
| `kb-tkl-brown-bt` | ✅ bought `KB-MECH-87-BRN` | ✅ bought `KB-MECH-87-BRN` | ✅ bought `KB-MECH-87-BRN` |
| `kb-wired-hotswap-rgb` | ✅ bought `KB-MECH-104-BRN` | ✅ bought `KB-MECH-104-BRN` | ✅ bought `KB-MECH-104-BRN` |
| `kb-ambiguous-tkl` | ✅ abstained (`ambiguous`) | ❌ bought `KB-MECH-87-BRN` | ❌ bought `KB-MECH-87-BRN` |
| `hp-anc-depth-40` | ✅ bought `HP-ANC-OVR-01` | ⚠️ abstained (`parse_error`) | ❌ bought `HP-ANC-TWS-01` |
| `hp-ldac-overear` | ✅ bought `HP-ANC-OVR-01` | ⚠️ abstained (`parse_error`) | ✅ bought `HP-ANC-OVR-01` |
| `hp-tws-30h-budget` | ✅ abstained (`spec_mismatch`) | ✅ abstained (`spec_mismatch`) | ✅ abstained (`spec_mismatch`) |
| `ms-silent-office` | ✅ bought `MS-OFFICE-SIL-01` | ✅ bought `MS-OFFICE-SIL-01` | ✅ bought `MS-OFFICE-SIL-01` |
| `ms-vertical-bt` | ✅ bought `MS-ERGO-VERT-01` | ✅ bought `MS-ERGO-VERT-01` | ✅ bought `MS-ERGO-VERT-01` |
| `ms-8k-gaming-oos` | ✅ abstained (`no_match`) | ✅ abstained (`spec_mismatch`) | ✅ abstained (`spec_mismatch`) |
| `mon-4k-usbc` | ✅ bought `MON-27-4K-IPS` | ✅ bought `MON-27-4K-IPS` | ✅ bought `MON-27-4K-IPS` |
| `mon-165hz-qhd` | ✅ bought `MON-27-QHD-165` | ✅ bought `MON-27-QHD-165` | ✅ bought `MON-27-QHD-165` |
| `mon-4k-under-20k` | ✅ abstained (`spec_mismatch`) | ✅ abstained (`spec_mismatch`) | ✅ abstained (`spec_mismatch`) |
| `dock-dual-hdmi-100w` | ✅ bought `DOCK-USBC-11P` | ⚠️ abstained (`spec_mismatch`) | ⚠️ abstained (`spec_mismatch`) |
| `cable-240w-bulk` | ✅ bought `CAB-USBC-2M-240W` | ⚠️ abstained (`parse_error`) | ✅ bought `CAB-USBC-2M-240W` |
| `kb-red-bulk-shortfall` | ✅ abstained (`out_of_stock`) | ✅ failed (`out_of_stock`) | ✅ failed (`out_of_stock`) |
| `nomatch-numpad` | ✅ abstained (`no_match`) | ✅ abstained (`spec_mismatch`) | ❌ bought `MS-ERGO-VERT-01` |
| `cam-4k-60fps` | ✅ bought `CAM-4K-60` | ✅ bought `CAM-4K-60` | ✅ bought `CAM-4K-60` |
| `cam-wide-fov` | ✅ bought `CAM-4K-60` | ⚠️ abstained (`parse_error`) | ❌ bought `CAM-1080-30` |
| `cam-1080-budget` | ✅ bought `CAM-1080-30` | ✅ bought `CAM-1080-30` | ✅ bought `CAM-1080-30` |
| `mic-xlr-phantom` | ✅ bought `MIC-XLR-COND` | ✅ bought `MIC-XLR-COND` | ✅ bought `MIC-XLR-COND` |
| `mic-usb-monitoring` | ✅ bought `MIC-USB-CARD` | ⚠️ abstained (`parse_error`) | ✅ bought `MIC-USB-CARD` |
| `mic-boom-trap` | ✅ abstained (`no_match`) | ✅ abstained (`parse_error`) | ❌ bought `MIC-USB-CARD` |
| `ssd-2tb-fast` | ✅ bought `SSD-2TB-NVME` | ⚠️ abstained (`parse_error`) | ✅ bought `SSD-2TB-NVME` |
| `ssd-encrypted-1tb` | ✅ bought `SSD-1TB-NVME` | ⚠️ abstained (`parse_error`) | ✅ bought `SSD-1TB-NVME` |
| `ssd-3000-read-trap` | ✅ abstained (`no_match`) | ✅ abstained (`parse_error`) | ❌ bought `SSD-1TB-NVME` |
| `ssd-ambiguous` | ✅ abstained (`ambiguous`) | ✅ abstained (`parse_error`) | ❌ bought `SSD-1TB-NVME` |
| `stand-alu-8kg` | ✅ bought `STAND-ALU-ADJ` | ⚠️ abstained (`parse_error`) | ✅ bought `STAND-ALU-ADJ` |
| `hub-powered-trap` | ✅ abstained (`spec_mismatch`) | ✅ abstained (`parse_error`) | ❌ bought `HUB-USBA-4P` |
| `kvm-4k-2port` | ✅ bought `KVM-2P-4K` | ✅ bought `KVM-2P-4K` | ✅ bought `KVM-2P-4K` |
