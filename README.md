# VFL Data Collector

Collects Virtual Football League round results and market odds, saves to Postgres.

## Endpoints
- `GET /` — health check + round count
- `GET /rounds` — last 200 rounds (JSON)
- `GET /rounds/latest` — most recent round
- `GET /rounds/<round_id>` — specific round

## Deploy on Railway
1. Add this repo as a Railway service
2. Add a Postgres plugin — `DATABASE_URL` is set automatically
3. Deploy
