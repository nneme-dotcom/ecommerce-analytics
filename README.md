# ecommerce-analytics

**Marketplace intelligence for Latin American e-commerce:** how a marketplace performs, and how sellers compete on price and delivery.

> 🚧 Work in progress — see [docs/PLAN.md](docs/PLAN.md) for the roadmap and [docs/PROGRESS.md](docs/PROGRESS.md) for current status.

## Business questions

**Marketplace performance** (Olist, ~100k orders, 2016–2018)
- Where does revenue come from (categories, regions, sellers), and how is it trending?
- How often are orders delivered late, and how much does lateness hurt review scores?
- Do customers come back? What do monthly cohorts look like?

**Competitive pricing** (Mercado Libre Argentina, captured daily via the official API)
- How do prices of best-selling products evolve over time?
- Are advertised discounts real, or is the "original price" inflated?
- How many sellers compete per product, and do official stores price higher?
- Do free shipping and fulfillment type relate to price or ranking?

## Architecture

```
Mercado Libre API ──(daily, GitHub Actions)──► raw snapshots ─┐
Olist CSVs ──────────────────────────────────► raw tables ────┼─► PostgreSQL: staging → star schema → marts ─► API ─► React dashboard
ECB FX rates · public holidays ──────────────► raw tables ────┘
```

## Stack

Python · PostgreSQL · SQL · React + TypeScript · GitHub Actions · AWS (free tier)

## How this repo is built

Built with a team of AI agents (data, analytics, backend, frontend, DevOps and an independent reviewer)
coordinated through shared project memory in [`CLAUDE.md`](CLAUDE.md) and [`docs/`](docs/).
Every change goes through a pull request, CI and review. Design decisions and their reasoning
are recorded in [docs/DECISIONS.md](docs/DECISIONS.md).

## Data sources and terms

- [Brazilian E-Commerce Public Dataset by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) (Kaggle).
- Mercado Libre data is obtained through the official API with an authorized application.
  Raw API data is not redistributed in this repository; only code and aggregated analyses are published.
