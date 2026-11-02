# Phoenix (tracing) on the dev server

Phoenix keeps a trace of every AI call: one trace per job (an article, a compose run), with each step
inside it (prompt, answer, tokens, cost, time) and the totals at the top. It runs as its own container,
next to NewsAgent, and any service on the server can send to it.

## Start it (once)

```bash
cd /var/www/NewsAgent/deploy/phoenix
docker network inspect propt-tracing >/dev/null 2>&1 || docker network create propt-tracing
docker compose up -d
```

Then, once, the prices Phoenix does not know (embedding models), so every call shows its cost:

```bash
./add_model_prices.sh
```

NewsAgent's deploy also creates the `propt-tracing` network if it is missing, and never restarts Phoenix.
Traces are kept in the `phoenix_phoenix_data` volume for 30 days (`PHOENIX_RETENTION_DAYS` to change).

## Send traces from a service

1. Join the network in the service's `docker-compose.yml` (NewsAgent's api, worker and scheduler do):

   ```yaml
   networks:
     - default
     - tracing
   # and at the end of the file:
   networks:
     tracing:
       name: propt-tracing
       external: true
   ```

2. In the service's `.env`:

   ```
   PHOENIX_COLLECTOR_ENDPOINT=http://phoenix:6006/v1/traces
   PHOENIX_PROJECT_NAME=newsagent/dev        # one per app per environment
   ```

   Empty `PHOENIX_COLLECTOR_ENDPOINT` = tracing off; the service works exactly as before.

3. Restart the service. Its log shows `[TRACING] Phoenix tracing registered`.

## Look at the traces

From your computer:

```bash
ssh -L 6006:localhost:6006 ubuntu@<dev-server>
```

then open http://localhost:6006. Phoenix has no login here: it is reachable only from the server.

## Check it from the server

```bash
docker logs --tail 20 phoenix
curl -s -X POST localhost:6006/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{projects(first:20){edges{node{name traceCount}}}}"}'
```

## Production (ECS Fargate)

Not this file: Phoenix runs as its own ECS service in the cluster (service discovery name `phoenix`,
port 6006 open to the cluster's services only, a Postgres database on RDS because a task's disk is
wiped on restart, 1 vCPU / 2 GB, image pinned and kept in ECR). See the Phoenix integration guide, section 8.
