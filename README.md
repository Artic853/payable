# Payable

[![CI](https://github.com/Artic853/payable/actions/workflows/ci.yml/badge.svg)](https://github.com/Artic853/payable/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**A merchant's transactability layer for AI buyers.**

A Razorpay merchant's catalog, wrapped in an interface an AI agent can actually
transact against: structured search that answers in constraints, signed spending
mandates the merchant verifies, Razorpay order + payment, and an append-only
audit log of every decision either side made.

Then the part that matters — a benchmark that measures whether any of it works.

---

## The result

29 buyer tasks against a 23-SKU catalog, run on three merchant surfaces and
repeated across 5 payment seeds. Same tasks, same catalog, same prices, same
seeded declines in every arm.

Mean ± standard deviation across seeds, with the range in brackets:

| Arm | Txn success | Wrong-item rate | Decision accuracy | Money misspent |
|---|---|---|---|---|
| **`payable`** — structured MCP surface, cautious agent | **94.7% ± 6.5** <br><sub>84–100</sub> | **0.0% ± 0.0** <br><sub>0–0</sub> | **96.6% ± 4.2** <br><sub>90–100</sub> | **₹0** |
| `legacy-strict` — HTML storefront, same cautious agent | 49.5% ± 2.9 <br><sub>47–53</sub> | 7.6% ± 4.3 <br><sub>0–10</sub> | 64.1% ± 1.9 <br><sub>62–66</sub> | ₹5,182 <br><sub>₹0–₹6,478</sub> |
| `legacy-optimistic` — HTML storefront, agent that proceeds when unsure | 80.0% ± 4.4 <br><sub>74–84</sub> | 33.3% ± 1.1 <br><sub>32–35</sub> | 67.6% ± 1.9 <br><sub>66–69</sub> | ₹42,411 <br><sub>₹37,836–₹44,314</sub> |

`payable` is not perfect and the spread says so: seeded payment declines cost it
real transactions, which is why success ranges 84–100% rather than sitting at a
suspicious 100. What does hold on **every** seed is the wrong-item rate — 0.0%
with zero variance. It loses sales to the payment network; it never buys the
wrong thing.

The interesting finding is not that structured beats HTML. It is the shape of
the trade-off in the two legacy rows:

> On an unstructured storefront, a buyer agent has to choose between **losing the
> sale** (cautious: 49.5% success) and **buying the wrong thing** (confident: a
> third of its purchases are wrong, ~₹42,000 of real money across 29 tasks). It
> cannot have both.
>
> The transactability layer removes the choice. The merchant tells the agent what
> it cannot verify, so the agent stops guessing.

Note the direction of the trade: going from cautious to optimistic on the HTML
storefront buys **+30 points of transaction success** at the cost of **+26 points
of wrong-item rate**. Decision accuracy barely moves (64.1% → 67.6%), because the
extra sales and the extra mistakes very nearly cancel out. That is the trap:
the optimistic agent looks dramatically better on the metric a merchant watches,
and is dramatically worse on the one a buyer cares about.

Full report with per-task detail: [`docs/benchmark.md`](docs/benchmark.md).

---

## Run it

No API keys, no Redis, no Docker. Everything degrades to an offline-deterministic
mode and `GET /health` tells you which mode each subsystem is in.

```bash
pip install -r requirements-dev.txt
```

One narrated purchase, end to end, replayed from the audit log:

```bash
python scripts/demo.py
```

The same agent refusing to buy, because two products fit equally well:

```bash
python scripts/demo.py --refuse
```

A forced payment decline and the bounded fallback that recovers it:

```bash
python scripts/demo.py --decline
```

The full benchmark, repeated across 5 payment seeds so the numbers carry a
variance rather than a single lucky run:

```bash
python -m payable.bench.runner --repeats 5 --md-out docs/benchmark.md
```

The merchant service, with all its surfaces:

```bash
python -m uvicorn payable.server.app:app --reload
```

Tests:

```bash
python -m pytest
```

---

## What "transactable" means here

Five verbs, exposed as MCP tools at `POST /mcp`. The contract is narrow on
purpose: an agent that can only do these five things cannot wander.

| Tool | What it guarantees |
|---|---|
| `search_products` | Ranked candidates, each annotated with the constraints it **met**, **missed**, and whether the misses disqualify it. Plus an `advisory_code`. |
| `get_product` | Full typed spec, live stock, JSON-LD. |
| `create_quote` | Unit price, GST, shipping, total. Expires in 120s. |
| `place_order` | Verifies the signed mandate, reserves inventory, creates the Razorpay order. Idempotent. |
| `pay_order` | Returns `captured` / `failed` / `pending` with a failure code and a `retriable` flag. |
| `get_order_status` | Current order state. |

### The one design decision that produces the result

Search returns `advisory_code`, and the buyer is required to read it:

- **`ambiguous`** — two or more products satisfy every stated constraint and
  differ on something the buyer never specified. The merchant names the differing
  field. *Buying here is a coin flip the buyer never agreed to.*
- **`no_satisfying_candidate`** — nothing clears the hard constraints. Buying the
  closest thing is a wrong purchase, not a partial success.
- **`empty`** — nothing matched the filters at all.
- **`none`** — one clear winner.

A scraper cannot produce any of this, because the merchant is the only party that
knows what it *didn't* say.

---

## Authorization: signed mandates

An agent never gets an open-ended right to spend. It carries a mandate signed by
its principal, and the merchant re-verifies it server-side before an order
exists:

```
Ed25519 signature  ·  expiry  ·  amount cap  ·  category scope
```

**The principal holds the private key; the merchant holds only the public half**
(`GET /api/principals`). That asymmetry is the point — a merchant compromise
cannot mint mandates, because the merchant never had the power to sign one.

Editing `max_amount_paise` client-side invalidates the signature. So does
reassigning the mandate to another principal, or editing the `alg` field, which
is itself inside the signed payload.

There is a symmetric HMAC fallback for environments without `cryptography`, and
it comes with a specific hazard: its secret has a documented default. So the
merchant **refuses a symmetric mandate for any principal that has enrolled a
public key** — otherwise anyone holding that default secret could spend as
anyone. [`tests/test_mandate.py`](tests/test_mandate.py) covers each of these,
including that downgrade attempt.

---

## Audit log

Every step either side takes is appended under one `run_id`, so any purchase can
be asked *"why did you buy that?"* after the fact:

```
GET /api/audit/runs            # recent runs
GET /api/audit/runs/{run_id}   # every decision in one run
```

```
[buyer_agent] run_start       -> payable
[merchant   ] search          -> 3 candidates          why: no advisory
[buyer_agent] select          -> HP-ANC-OVR-01         why: satisfies every stated constraint
[merchant   ] quote           -> qt_a9e15296b6eb4e14   why: Aether Hush ANC Over-Ear x1 = INR 10608.20
[merchant   ] mandate_check   -> accepted              why: amount 1060820 within cap 1200000
[merchant   ] order_created   -> ord_5cdaa556fe6e41c3  why: gateway=razorpay-simulated
[gateway    ] payment_attempt -> captured              why: upi capture via razorpay-simulated
[buyer_agent] run_complete    -> purchased             why: HP-ANC-OVR-01 for 10608.20 INR
```

Backed by Redis Streams when `REDIS_URL` is set (append-only, consumer groups,
trimming — the shape you want in production), JSONL otherwise. Same interface.

---

## What is real and what is simulated

Stated plainly, because it changes how the numbers should be read.

| Piece | Status |
|---|---|
| MCP JSON-RPC server, tool schemas, A2A agent card, JSON-LD feed | **Real.** Any MCP client or crawler can consume them. |
| Constraint search, advisories, quoting, GST/shipping maths, inventory reservation, idempotency | **Real.** |
| Mandate signing and verification | **Real** Ed25519 (asymmetric), with a dev-only HMAC fallback the merchant refuses for enrolled principals. |
| Audit log | **Real**, both backends. |
| Razorpay **order creation** | **Real** against test mode when `RAZORPAY_KEY_ID` is set — returns a real `order_...` id visible in the dashboard. Payment Links too. |
| Razorpay **capture** | **Simulated.** Razorpay completes payment through hosted checkout, which needs a browser and a payer. With keys set, the result is labelled `razorpay-api+simulated-capture`; nothing in the audit log ever claims a capture that did not happen. |
| Payment declines | **Injected**, seeded and reproducible. Decline reasons are modelled on the ones Razorpay actually returns. |
| Buyer reasoning | **Deterministic by default.** An LLM path exists (`PAYABLE_USE_LLM=1`), but the benchmark stays deterministic so its numbers measure the merchant surface, not model sampling. |

---

## Benchmark methodology

The claim is only as good as the fairness of the comparison, so the harness
enforces these and the tests check them:

1. **Identical inputs.** Every arm gets the same 16 tasks, same constraints, same
   budgets. The buyer's constraints are *its own* understanding of its brief —
   not something the merchant handed it. What differs between arms is only
   whether the merchant can be *queried* with them.
2. **Identical starting state.** Catalog inventory and gateway retry history are
   reset before every single `(arm, task)` pair, so no task can be influenced by
   what an earlier one bought.
3. **Identical payment luck.** Declines are seeded on `(sku, amount, attempt)`,
   not on a random order id — so every arm meets the same declines at the same
   points. Without this an arm could look better purely by drawing luckier
   failures. `--repeats N` then re-runs the whole suite across N seeds and
   reports the spread, so no single seed carries the claim.
4. **The baseline is not a strawman.** The legacy scraper follows the real
   purchase path, parses Indian-format prices correctly, filters by category from
   the listing, normalises spec labels through a synonym table, and coerces units
   and Yes/No values. It fails where prose genuinely loses type information —
   `"Active noise cancellation"` is not the field `anc`, and `"Hurry, only a few
   left!"` is not a quantity.
5. **Ground truth is withheld.** `BuyerIntent.agent_view()` excludes
   `expected_sku`; a test asserts the answer never appears in what the agent sees.
6. **The answer key is checked.** Tests verify that every expected SKU is in
   stock, within budget, and the *unique* satisfier of its own constraints — and
   that every trap task is genuinely unbuyable for a stated reason. A benchmark
   whose ground truth is quietly wrong measures nothing.

### Scoring

- **Transaction success** — of the tasks where something *should* have been
  bought, how often the right thing was bought and paid for end to end.
- **Wrong-item rate** — of everything it *did* buy, how often it was wrong. This
  is the number that costs real money.
- **Decision accuracy** — right action on every task, buying and refusing alike.
  6 of the 16 tasks are traps where refusing is the correct answer.

### Failure taxonomy

Every unsuccessful run ends in exactly one code: `no_match`, `ambiguous`,
`spec_mismatch`, `over_budget`, `out_of_stock`, `quote_expired`,
`mandate_rejected`, `payment_declined`, `gateway_error`, `parse_error`,
`timeout`.

The taxonomy is diagnostic, not decorative — `legacy-strict`'s
`parse_error`×3 are exactly the specs it could see but not type.

---

## Layout

```
payable/
  catalog.py          constraint search, ranking, ambiguity advisories, JSON-LD
  commerce.py         quote -> order -> payment, GST, idempotency, inventory
  mandate.py          Ed25519 mandates, keyring, downgrade refusal
  audit.py            append-only decision log (Redis Streams | JSONL)
  models.py           the transactability contract
  payments/           razorpay_api.py (live test mode) | simulated.py (offline)
  server/
    app.py            discovery, MCP endpoint, REST mirror, audit API
    mcp_http.py       MCP tool definitions and JSON-RPC dispatch
    legacy.py         the control arm: same shop as plain HTML
  agent/
    payable_buyer.py  five-node buyer over MCP
    legacy_buyer.py   the scraping baseline
    base.py           intents, policy, run results
    llm.py            optional LLM constraint extraction
  bench/              harness, metrics, reporting
data/
  catalog.json        23 SKUs with typed specs
  tasks.json          29 buyer tasks with ground truth
tests/                135 tests
```

### Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /mcp` | MCP tool server (JSON-RPC) |
| `GET /.well-known/payable.json` | capability manifest |
| `GET /.well-known/agent-card.json` | A2A agent card |
| `GET /catalog.jsonld` | schema.org feed with `BuyAction` pointing at `/mcp` |
| `GET /api/audit/runs/{run_id}` | replay any run |
| `GET /legacy/` | the control-arm storefront |
| `GET /api/principals` | public keys the merchant verifies mandates against |
| `GET /health` | which backend each subsystem is using |

---

## Honest limitations

- **Capture is simulated**, for the reason given above. A full roundtrip with a
  real captured payment needs a browser driving Razorpay's hosted checkout.
- **One merchant, 23 SKUs.** The advisory logic is tuned against a catalog small
  enough to reason about by hand. Cross-merchant discovery and a registry are the
  obvious next thing and are not built.
- **The deterministic planner is not an LLM.** That is deliberate for benchmark
  stability, but it means these numbers measure the *merchant surface* under a
  well-behaved buyer. An LLM buyer would add its own error rate on top; the
  `payable` arm's headroom over legacy would shrink somewhat, and quantifying
  that is the honest next experiment. This is now the single biggest gap.
- **The key registry is in-process.** Ed25519 signing and verification are real,
  but `PrincipalKeyring` holds keys in memory and — for demo convenience only —
  will generate a keypair on first use. A production deployment enrols public
  keys out of band and never generates a principal's private key.
- **29 tasks across 5 seeds is a small study.** The seed variance is reported, so
  the payment-luck component is quantified. What is *not* quantified is
  task-selection variance: the tasks were hand-authored by the same person who
  built the surface being measured, which is the most likely place for
  unconscious bias to enter. Ground-truth self-consistency is machine-checked
  ([`tests/test_bench.py`](tests/test_bench.py)); representativeness is not, and
  cannot be by these tests alone.
