# Payable

**A merchant's transactability layer for AI buyers.**

A Razorpay merchant's catalog, wrapped in an interface an AI agent can actually
transact against: structured search that answers in constraints, signed spending
mandates the merchant verifies, Razorpay order + payment, and an append-only
audit log of every decision either side made.

Then the part that matters — a benchmark that measures whether any of it works.

---

## The result

16 buyer tasks, run against three merchant surfaces. Same tasks, same catalog,
same prices, same seeded payment declines.

| Arm | Txn success | Wrong-item rate | Decision accuracy | Money misspent |
|---|---|---|---|---|
| **`payable`** — structured MCP surface, cautious agent | **100.0%** | **0.0%** | **100.0%** | **₹0** |
| `legacy-strict` — HTML storefront, same cautious agent | 60.0% | 14.3% | 68.8% | ₹6,478 |
| `legacy-optimistic` — HTML storefront, agent that proceeds when unsure | 80.0% | 27.3% | 75.0% | ₹14,912 |

The interesting finding is not that structured beats HTML. It is the shape of
the trade-off in the two legacy rows:

> On an unstructured storefront, a buyer agent has to choose between **losing the
> sale** (cautious: 60% success) and **buying the wrong thing** (confident: 27%
> of its purchases are wrong, ₹14,912 of real money). It cannot have both.
>
> The transactability layer removes the choice. The merchant tells the agent what
> it cannot verify, so the agent stops guessing.

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

The full benchmark:

```bash
python -m payable.bench.runner --md-out docs/benchmark.md
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
signature (HMAC-SHA256)  ·  expiry  ·  amount cap  ·  category scope
```

Editing `max_amount_paise` client-side invalidates the signature
([`tests/test_mandate.py`](tests/test_mandate.py) proves it). A misreasoning
agent fails closed rather than overspending.

The shared-secret HMAC stands in for what would be an asymmetric,
registry-anchored credential in production — the merchant would hold the
principal's public key, not a symmetric secret.

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
| Mandate signing and verification | **Real** HMAC-SHA256. |
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
   failures.
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
  mandate.py          issue and verify signed spending authorizations
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
  catalog.json        14 SKUs with typed specs
  tasks.json          16 buyer tasks with ground truth
tests/                100 tests
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
| `GET /health` | which backend each subsystem is using |

---

## Honest limitations

- **Capture is simulated**, for the reason given above. A full roundtrip with a
  real captured payment needs a browser driving Razorpay's hosted checkout.
- **One merchant, 14 SKUs.** The advisory logic is tuned against a catalog small
  enough to reason about by hand. Cross-merchant discovery and a registry are the
  obvious next thing and are not built.
- **The deterministic planner is not an LLM.** That is deliberate for benchmark
  stability, but it means these numbers measure the *merchant surface* under a
  well-behaved buyer. An LLM buyer would add its own error rate on top; the
  `payable` arm's headroom over legacy would shrink somewhat, and quantifying
  that is the honest next experiment.
- **The mandate is a shared-secret HMAC**, not the asymmetric, registry-anchored
  credential a production authorization scheme needs.
- **16 tasks is small.** The per-arm differences are large enough to be visible,
  but this is a demonstration of a measurement method, not a statistically
  powered study.
