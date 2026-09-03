# Payable — agent transactability benchmark

- **Tasks:** 16
- **Payments:** simulated (no RAZORPAY_KEY_ID)
- **Buyer reasoning:** deterministic-planner
- **Seed:** 1733
- **Injected decline rate:** 12%


## Headline

| Arm | Txn success | Wrong-item rate | Decision accuracy | p50 latency | p95 latency | HTTP calls/task |
|---|---|---|---|---|---|---|
| `payable` | 100.0% | 0.0% | 100.0% | 122 ms | 216 ms | 4.1 |
| `legacy-strict` | 60.0% | 14.3% | 68.8% | 45 ms | 161 ms | 4.5 |
| `legacy-optimistic` | 80.0% | 27.3% | 75.0% | 122 ms | 168 ms | 5.1 |

## Outcomes

| Arm | Purchases | Right | Wrong | Correct abstentions | Declines | Recovered | Money misspent |
|---|---|---|---|---|---|---|---|
| `payable` | 10 | 10 | 0 | 6/6 | 2 | 2 | ₹0.00 |
| `legacy-strict` | 7 | 6 | 1 | 5/6 | 0 | 0 | ₹6,478.00 |
| `legacy-optimistic` | 11 | 8 | 3 | 4/6 | 1 | 1 | ₹14,912.00 |

## Failure taxonomy

| Arm | Terminal failure codes |
|---|---|
| `payable` | `spec_mismatch`×2, `no_match`×2, `ambiguous`×1, `out_of_stock`×1 |
| `legacy-strict` | `spec_mismatch`×5, `parse_error`×3, `out_of_stock`×1 |
| `legacy-optimistic` | `spec_mismatch`×4, `out_of_stock`×1 |

## Per-task detail

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
