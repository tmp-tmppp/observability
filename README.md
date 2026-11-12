# observability

The logging (REQ-19) and metrics (REQ-20) stacks of **Chance**, for one small AKS node: **Fluent Bit** ships the JSON logs of
every service to **Loki**, one plain **Prometheus** server scrapes the services' `/metrics`, and **Grafana** shows both, with
the dashboards and the alert rules provisioned from files (ADR-035, ADR-036). There is no code here: the repository is a Helm
chart that wraps four official charts, two ConfigMaps of provisioning files, three NetworkPolicies, a smoke test, a compose
file with a demo and the checks that keep them honest. Dominik owns the setup (plan Q-28, Q-29).

## What this repository holds

| Path | What |
|---|---|
| [`helm/Chart.yaml`](helm/Chart.yaml), [`helm/Chart.lock`](helm/Chart.lock) | The chart `observability` and its four dependencies, each one exact version (below) |
| [`helm/charts/`](helm/charts/) | The four packaged charts, committed, so the pipeline needs no network for them |
| [`helm/values.yaml`](helm/values.yaml), [`values-dev.yaml`](helm/values-dev.yaml), [`values-prod.yaml`](helm/values-prod.yaml) | The settings, each with its reason in a comment |
| [`helm/values.schema.json`](helm/values.schema.json) | Refuses a value that would break the contract of the stack (see "Checks") |
| [`helm/dashboards/`](helm/dashboards/) | Three dashboards: `red-overview.json`, `red-service.json`, `logs.json` (copied into the ConfigMap `chance-dashboards`) |
| [`helm/alerting/rules.yaml`](helm/alerting/rules.yaml) | The two alert rules (copied into the ConfigMap `chance-alerting`, with the contact point and the policy of [`templates/alerting.yaml`](helm/templates/alerting.yaml)) |
| [`helm/templates/`](helm/templates/) | The two ConfigMaps, the three NetworkPolicies and the `helm test` smoke test ([`tests/smoke-test.yaml`](helm/templates/tests/smoke-test.yaml)) |
| [`compose.yaml`](compose.yaml), [`demo/`](demo/) | The same four components for a local run, with a demo service that makes logs and metrics |
| [`scripts/`](scripts/) | `schema-refusals.sh` (the schema must refuse 15 values and accept the one flag the pipeline adds) and `cluster-chain-check.py` (runs the rendered Fluent Bit filter and output against a Loki) |
| [`.github/`](.github/) | Pipeline (the shared workflow with `build-image: false`, plus the stack's own checks), CODEOWNERS, Dependabot |

| Component | Chart (repository) | App version | Image (pinned by tag and digest in `values.yaml`) |
|---|---|---|---|
| Fluent Bit | `fluent-bit` 0.58.3 (`fluent.github.io/helm-charts`) | 5.1.3 | `cr.fluentbit.io/fluent/fluent-bit` |
| Loki | `loki` 21.2.1 (`grafana-community.github.io/helm-charts`) | 3.7.9 | `docker.io/grafana/loki` |
| Prometheus | `prometheus` 29.47.1 (`prometheus-community.github.io/helm-charts`) | 3.16.0 | `quay.io/prometheus/prometheus` (and the config reloader 0.95.2) |
| Grafana | `grafana` 14.4.1 (`grafana-community.github.io/helm-charts`) | 13.3.1 | `docker.io/grafana/grafana:13.3.1-distroless` |

Why these repositories: read from the repository indexes on 2026-11-11. The old `grafana/helm-charts` `loki` chart is now the Grafana
Enterprise Logs chart (its description says so) and its `grafana` chart is marked deprecated; the Grafana Loki and Grafana charts
continue in `grafana-community/helm-charts`. All four are the newest stable versions of 2026-11-11 (Loki's and Prometheus's are
patch releases of that day or the day before; if a fresh patch proves bad, pin the one before, see "Update a chart").

## Architecture

```
 every Pod in prod, gateway, monitoring  node: one Fluent Bit Pod (DaemonSet)          namespace monitoring
 ┌────────────────────────────┐   files   ┌─────────────────────────────┐  push   ┌──────────────────────────┐
 │ service: JSON lines on     ├──────────►│ tail /var/log/containers    ├────────►│ Loki (single binary)     │
 │ stdout (pino)              │ (kubelet) │ parse JSON, add k8s labels  │ :3100   │ filesystem, PVC 2 GiB,   │
 │ GET /metrics on :9464      │           └─────────────────────────────┘         │ retention 72 h           │
 └─────────────┬──────────────┘                                                   └────────────┬─────────────┘
               │ scrape every 30 s (Pod annotations, node cAdvisor)                            │ query :3100
               ▼                                                                               ▼
 ┌──────────────────────────────────────┐     query :9090         ┌──────────────────────────────────────────┐
 │ Prometheus server (no Alertmanager)  │◄────────────────────────┤ Grafana: dashboards and alert rules from │
 │ PVC 2 GiB, retention 3 d or 1 GB     │                         │ files; contact point = e-mail (or logs)  │
 └──────────────────────────────────────┘                         └─────────────────────┬────────────────────┘
                                                                                        │ kubectl port-forward (no Ingress)
                                                                                        ▼ the developer
```

Parts: Fluent Bit (DaemonSet, reads the log files the kubelet writes, no agent in the services), Loki (the only place logs are
stored), Prometheus (the only place metrics are stored), Grafana (no storage: everything it shows or alerts on is provisioned).
State is kept in two claims (Loki 2 GiB, Prometheus 2 GiB) and on each node in Fluent Bit's small position file and its buffer of unsent chunks
(`/var/log/flb-storage`, at most 100 MB, used only while Loki is away). What is external:
nothing (no Azure service, no outside address); Grafana's e-mail contact point needs an SMTP server only if mail is wanted.

### Namespace

Everything runs in the namespace **`monitoring`** (a separate namespace from the services' `prod`, plan section 9). Prometheus calls into
`prod` to scrape the services (port 9464; identity, Keycloak, serves its metrics on its management port 9000) and into `gateway` to scrape Traefik
(port 9464, its own entry point `metrics`); Fluent Bit reads the log files of the Pods of `prod`, `gateway` (Traefik and the token proxy, so the
edge's 5xx, time-outs and failed logins are in Loki) and `monitoring` on the node. Nothing in `prod` or `gateway` calls the stack. The pipeline passes
`namespace: monitoring` to the shared workflow ([`ci-cd.yml`](.github/workflows/ci-cd.yml)); the namespace is created once by hand,
as `prod` is (the workflow checks that it exists). The charts create three cluster-wide objects with it (ClusterRoles for Fluent Bit,
Prometheus and an empty one of Loki, see "Follow-ups"); the deploy identity has an admin-level kubeconfig until after M2 (ADR-052).

### How the services' NetworkPolicies reach it

Only Prometheus calls a service; Fluent Bit reads files and calls nothing in `prod` or `gateway`. So each service chart carries one ingress rule,
switched by a chart value. This is what the charts of payments and identity contain (read 2026-11-11; the charts of tenants, audit and payment-provider
carry the same rule, found by search, not read through): the **whole namespace** named by `networkPolicy.metrics.namespace` (value `monitoring`) is
admitted, on the container port named `metrics` (9464), or `management` (9000) in identity. It does not narrow it to the Prometheus Pod:

```yaml
- from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: monitoring   # set by Kubernetes on every namespace
  ports:
    - port: metrics        # identity: management
      protocol: TCP
```

The gateway needs no rule: according to `docs/erik-functionalities/010-metrics.md` (not re-read in the gateway chart) its NetworkPolicy selects only the
token proxy Pods and Traefik's chart has none, so Traefik's metrics entry point, which is not in its Service, is reachable at the Pod address. A service chart that adds a policy selecting its Pods denies what it does not list, so
the rule above is what every service that serves metrics must have; all eight application services have it since 2026-11-12
(see "How the services hook in", which services serve metrics today). Without a network policy engine in the cluster none of this is enforced (follow-up 2).

## Sizing (the reasoning)

**The node** (plan INF-03, ADR-039): one Standard_DS2_v2, 7 GiB. About 5.3 GiB is left for Pods after Kubernetes' own reservation
`[assumed: AKS reserved-memory formula, 25 % of the first 4 GiB and 20 % of the next 4 GiB plus the eviction threshold; not read from the
cluster, ADR-039 measures it at M1]`.

**What is already there** (memory requests in the `helm/values.yaml` of the repositories, read 2026-11-11): identity (Keycloak) 768 Mi,
market-data 128 Mi, audit, markets, trading, market-maker, payments, tenants and the broker 96 Mi each, payment-provider 64 Mi, the
gateway's token proxy 32 Mi, web 16 Mi: **1 680 Mi**. Not yet in a repository: PostgreSQL and Traefik; **512 Mi** for both is an assumption
`[assumed]`.

**What this stack asks for:**

| Component | CPU request | Memory request | Memory limit | Disk | Why |
|---|---|---|---|---|---|
| Fluent Bit (one Pod per node) | 25m | 16 Mi | 96 Mi | none (on the node, in `/var/log`: a position file, and while Loki is away up to 100 MB of unsent chunks) | 4.5 MiB in use in the demo; the buffer of the tail input is 5 MB and at most 8 chunks are held in memory; the limit allows a burst of logs |
| Loki | 50m | 128 Mi | 384 Mi | 2 GiB claim | 104 MiB in use in the demo; a single binary at this volume; the 50 MB results cache is inside; the Go soft limit is 85 % of the limit |
| Prometheus server (+ reloader) | 50m (+5m) | 96 Mi (+16 Mi) | 384 Mi (+32 Mi) | 2 GiB claim | 45 MiB in use in the demo with about 1 100 series, about 4 000 are estimated below; 30 s scrapes, 3 days or 1 GB |
| Grafana | 50m | 256 Mi | 512 Mi | none | 277 MiB in use in the demo (the first guess of 320 Mi limit was too tight); everything is provisioned, sqlite lives in an emptyDir |
| **Sum** | 175m | **512 Mi** | 1 408 Mi | 4 GiB | |

Total requests with the existing pieces: 1 680 + 512 + 512 = **2 704 Mi of about 5.3 GiB** (about 50 %), so the stack fits on one node with
room for the services' growth; the limits add up to more than that on purpose (a limit is a ceiling that costs nothing until used, and
all of them are never reached together). The ADR estimates (Loki 256-600 Mi, Prometheus 256-512 Mi) were estimates; the requests above
are set from what was measured in the compose demo with the same limits (see "Checks", the table of what the containers used), and
from the volume estimates below. At M1 the real requests of every component are measured (ADR-039): read the dashboard
`Chance / RED overview`, row "Resources of the containers in the namespace prod", and move a request here if it is far off.

**Log volume and the 72 hours** `[assumed: model knowledge for the line sizes; the line count of the real services is not known]`:
a pino line with the agreed fields is about 300 to 500 bytes. Ordinary use of the demo is a few lines a second (about 1 GB a month
uncompressed at 400 bytes and 1 line/s across all services). A load test of 200 requests a second for ten minutes writes 120 000 lines,
about 48 MB; Loki compresses chunks several times over. Three days of the generous case (a load test a day plus ordinary use) are
well under 1 GiB of chunks, so the 2 GiB claim has more than twice the room it needs and is as small as it usefully gets (Azure bills
a disk by size tier, so the claim is read in the cost view, not from this number, as for the broker). The compose demo records how many
bytes per line Loki received, as a check of the line size. Retention is `limits_config.retention_period: 72h`, enforced by Loki's compactor.

**Metric series and the 3 days** `[assumed: model knowledge for prom-client's defaults]`: prom-client's default metrics are about 40
series per service; `http_requests_total` and the duration histogram have 12 series per label combination (11 buckets plus sum and count,
the counter 1). With 8 services, 4 routes, 3 tenants and 3 status codes that is about 8 x 4 x 3 x 3 = 288 combinations x 13 series =
3 750 series, plus the defaults (320), cAdvisor (the four kept metrics for about 30 containers, 120 series) and the services' own counters
(under 100 series, every label bounded, listed in "Who serves metrics today"): **about 4 300 series**. At
about 3 KB of memory a series that is 12 MB of head block, and at 30 s scrapes about 1.5 bytes a sample and 2 880 samples a series a day the disk
use is about 17 MB a day: the 2 GiB claim and the 1 GB size limit are far above what 3 days need. The label `tenant` multiplies the series:
that is why `route` is the route template (`/v1/markets/:id`), never the URL (a service that puts ids in `route` breaks the budget, see
"How the services hook in").

## How the services hook in

Agreed between the two members (2026-11-11); nothing in a service needs to know Loki, Prometheus or Fluent Bit.

| What | Convention |
|---|---|
| Logs | One JSON object per line on **stdout** (pino): `level`, `time`, `msg`, **`service`**, **`tenantId`** (where there is one), **`correlationId`**. A line that is not JSON (a stack trace) is kept as text. Nothing is written to a file |
| Metrics | `GET /metrics` on **`METRICS_PORT` 9464**: prom-client 15 defaults plus `http_requests_total` (counter) and `http_request_duration_seconds` (histogram), labels `{method, route, status_code, tenant}` |
| Discovery | Pod annotations `prometheus.io/scrape: "true"`, `prometheus.io/port: "9464"`, `prometheus.io/path: "/metrics"` (nothing else to register) |
| Network | The service chart's NetworkPolicy admits the namespace `monitoring` on the port named `metrics` (the snippet above) |

**Who serves metrics today** (read from the working trees of the repositories on 2026-11-12; look again at the first deploy): all eight
application services have prom-client 15.1.3, port 9464, the three annotations and the NetworkPolicy rule: tenants, payments, audit and payment-provider
since 2026-11-11, markets, trading, market-maker and market-data since 2026-11-12; identity is scraped on 9000 (`management`); Traefik on 9464 through its
own chart. Notes on the four added last:

- trading serves one `/metrics` for both its HTTP apps (public 8080, internal 8081); the internal app's requests carry `tenant="none"` (a service token
  names no tenant), and the gRPC debit port 8082 is not in the HTTP metrics. market-maker has no user requests (only its health app), so its `tenant` is always `none`.
- A request Fastify refuses before routing (a malformed URL, 400 or 414) is counted as `route="unmatched"`, `tenant="none"` in these four; the
  other four services do not count it yet (their `metrics.ts` lacks `recordFrameworkError`).
- Their own counters, every label bounded: `markets_auto_close_total{outcome}`; `trading_trades_total{kind}` and
  `trading_orders_refused_total{kind,reason,market_status}`; `graphql_errors_total{code}`, `market_data_projection_events_total{result}` and the histogram
  `market_data_projection_trade_sequence_gap` (how far the projection is behind trading's trade sequence); `market_maker_quote_runs_total{outcome}` and
  `market_maker_refused_puts_total`. No dashboard panel uses them yet; they are there for the REQ-20 work and for Explore.

What the stack does with them:

- **Loki labels** (few and stable, because Loki indexes labels only): `namespace` (`prod`, `gateway` or `monitoring`), `app` (the Pod's `app.kubernetes.io/name`, the chart
  name), `tenant` (the JSON field `tenantId`; a line without one has no `tenant` label), `job`. **`correlationId` is not a label**: it
  is searched in the line (`|= "<id>"`), which is why the logs dashboard has a text box for it. Rule of thumb: a label gets a handful of
  values; with hundreds of tenants move `tenant` to structured metadata.
- **Prometheus labels**: the metric's own (`method`, `route`, `status_code`, `tenant`) plus `service` (the Pod's chart name, set by the
  scrape job), `namespace` and `pod`. The dashboards group by `service`.
- **Cost of a label**: every new combination of label values is a new time series. `route` must be the matched route template, `tenant` the
  tenant id, `status_code` the number; never a URL, user id or market id.

Fields the dashboards read: logs `level` (`error`, `fatal`, or pino's numbers `50`, `60`), `correlationId`, the text `WithdrawalFailed`;
metrics `http_requests_total`, `http_request_duration_seconds_bucket`; and in the last row of `RED overview` (the edge and identity), which the services'
convention does not cover, `traefik_entrypoint_requests_total`, `traefik_entrypoint_request_duration_seconds_bucket` (Traefik) and `http_server_requests_seconds_count`
(Keycloak). Those three names are from the products' documentation and the review of this chart; no real Traefik or Keycloak has been scraped yet `[UNVERIFIED]`.

## Open Grafana

There is no Ingress and no public address (plan: the gateway is the only public entry point, ADR-022). Grafana is a ClusterIP Service;
open it from a laptop with access to the cluster:

```bash
kubectl port-forward -n monitoring svc/grafana 3000:80      # then http://localhost:3000
```

Log in as `admin` with the password from the organization secret `OBSERVABILITY_ENV` (the pipeline put it into the Secret
`observability-env`; the pipeline never prints it and no file here holds it). To read it back: `kubectl get secret observability-env -n monitoring
-o jsonpath='{.data.admin-password}'` (base64). The dashboards are in the folder **Chance**: `RED overview` (all services side by side),
`RED of one service` (pick a service, filter by tenant and route), `Logs` (filter by namespace, service and tenant, type a correlation id; the namespace `gateway` holds the
edge's lines, which have no tenant, so a chosen tenant leaves them out). Prometheus and
Loki can be reached the same way (`svc/prometheus-server` 9090:80, `svc/loki` 3100) for a raw query.

## Alerts

Grafana's unified alerting evaluates two provisioned rules every minute ([`helm/alerting/rules.yaml`](helm/alerting/rules.yaml)):

| Rule | Query | Fires when |
|---|---|---|
| **WithdrawalFailed in the logs** (REQ-19) | Loki: `sum(count_over_time({namespace="prod"} \|= "WithdrawalFailed" [5m]))` | at least one such line in the last 5 minutes, at once (`for: 0s`); no line is the normal case (no data = OK) |
| **A service answers with errors** (REQ-20) | Prometheus: the 5xx share of `http_requests_total` per service over 5 minutes, for services that take more than 0.05 requests a second | above 5 % for 2 minutes |

**Which line fires the first rule** (read in the code of payments and trading on 2026-11-11, not run in a cluster): payments publishes `WithdrawalFailed` only into its outbox
(`payments/src/usecases/withdrawals.ts`) and logs no line with that name. The line that matches is the info line of trading's consumer, "A payment event was handled", whose field
`eventType` is `si.chance.payments.WithdrawalFailed.v1` (`trading/src/adapters/nats/message-handlers.ts`). So the alert depends on trading consuming the event (a trading Pod that is
down or behind the broker raises it late) and on trading's log level staying at `info` (a level of `warn` hides the line); a redelivery of the event fires it again. The rule
watches `{namespace="prod"}`; Grafana's own lines are in `monitoring`, so the alert cannot fire on itself, and its label `service: payments` names the owner of the event, not the
service that logged the line. **Open**: payments should log one `warn` line naming `WithdrawalFailed` when it settles a failed payout, which removes both dependencies; that is a
change in the payments repository, not made here.

**Seeing it fire in the cluster** (the demo for REQ-19; not run, the stack is not deployed): the payment provider fails payouts on purpose in its test mode
(`payment-provider/README.md`): `PUT /v1/test-mode` with `{"declinePayouts": true}` and its API key (reach the provider's Service with `kubectl port-forward`), then make a
withdrawal as a user; the payout job fails it, payments publishes the event, trading logs it and the rule is evaluated every minute, so the alert shows as *Firing* in
Grafana under Alerting > Alert rules within about a minute or two of the payout `[assumed: not timed]`. Put `{"declinePayouts": false}` back afterwards. The line itself is in
the dashboard `Chance / Logs`, last panel, even without waiting for the rule.

**Contact point.** One e-mail contact point `chance-alerts` with the addresses of `alerting.emailAddresses` (a placeholder, not a secret) and
one notification policy (grouped by alert name and service, first notification after 30 s, repeated every 4 hours). Grafana has **no SMTP
server configured** (`smtp.enabled: false`), so by default a notification cannot be sent, and the alert is visible as *Firing* under
Alerting > Alert rules (in the compose demo both rules were seen firing through Grafana's API; what Grafana writes to its log about
the failed send was not captured `[UNVERIFIED]`). **To e-mail**: set
`grafana.grafana.ini.smtp.enabled: true`, `host` and `from_address` in `values-prod.yaml`, put `smtp-user=` and `smtp-password=` lines into
the organization secret `OBSERVABILITY_ENV` (the pipeline turns them into keys of the Secret `observability-env`, which Grafana reads when they exist),
and set the real addresses in `alerting.emailAddresses`. No credential is in any values file.

## REQ-20: one documented optimization based on the metrics

The requirement asks that the collected metrics are used to make something faster, and that this is shown. It needs real traffic, so
it is done after the first deploy and written into this section (what was measured, what changed, the measurement after). The method:

1. **Generate load.** A k6 (or `hey`) script against the gateway, with a fixed rate (for example 20 requests a second for 10 minutes, two
   tenants), the same before and after. Note the start and end time.
2. **Find the slowest thing.** Dashboard `RED of one service`, each service in turn, range = the test: the table *Slowest routes* is the
   95th percentile per route over the range; the panel *Duration* shows p50/p95/p99. Pick the route with the worst p95 **and** a real
   share of the traffic (a slow route nobody calls is not worth it). Also read *Errors* by route (a route whose p95 is high because it
   times out on a dependency is a different problem) and the memory and CPU rows (a Pod near its limit throttles).
3. **Find where the time goes.** Logs dashboard, same time range, the service and a `correlationId` of one slow request: the log lines
   show which calls it made and how long each took (`responseTime`); look for the same query repeated per row (N+1), a call made in a
   loop, a missing index (`EXPLAIN` on the statement) or a synchronous call that could be an event (EP-2).
4. **Change one thing**, in one pull request that says in its description which metric it moves. Candidates the plan already names: a
   missing index on the ledger or read model, an HTTP call replaced by the local copy of the data (EP-2), a connection pool size, a cache
   on a read route, a request or limit that throttles the Pod (CPU throttling shows as a flat p95 under load).
5. **Measure again** with the same load, same range length; record the numbers and attach the two dashboard screenshots.
6. **Write it here**: route, p95/p99 before and after, the error share, the change, the commit. Until then this section is the method only and **REQ-20's
   optimization item is not met**; `CHECKLIST.md` says so.

## Run locally

```bash
docker compose up -d              # Grafana http://localhost:3000 (no login, this machine only), Prometheus :9090, Loki :3100
docker compose down -v            # stop and delete the stored logs and metrics
DEMO_ERROR_RATE=0.3 docker compose up -d     # make the error-rate alert fire (about 4 minutes)
```

The four images are the app versions of the vendored charts, pinned by tag and digest, with the chart's memory limits. The **demo
service** ([`demo/service/server.py`](demo/service/server.py), Python standard library, no build) stands in for a Chance service: it
writes pino-style JSON lines with `tenantId` and `correlationId` and serves `/metrics` with the two agreed metric names, over two
tenants and four routes, a slow tail on `POST /v1/orders`, and a `WithdrawalFailed` line every 90 seconds. Its logs reach Fluent Bit through
Docker's `fluentd` logging driver (on a cluster Fluent Bit tails files instead; the parsing and the labels are the same). The Grafana of
the demo loads **the same dashboards and alert rules** as the chart (the compose file mounts `helm/dashboards` and `helm/alerting`).

**Pointing it at a real service.** Start the service with its own compose file with `logging: {driver: fluentd, options:
{fluentd-address: "127.0.0.1:24224", fluentd-async: "true", tag: chance}}` added to its service, uncomment the second target in
[`demo/prometheus.yml`](demo/prometheus.yml) (`host.docker.internal:9464`, with the service's chart name as the label `service`), and stop the
demo service. The line's `service` field becomes the label `app`.

## Checks

Run on 2026-11-11 with Helm 4.3.1, kubeconform 0.8.0 and Docker 29.9.3; nothing was installed into a cluster.

```bash
helm dependency list helm                                  # four rows, status "ok": the vendored charts match Chart.yaml
helm lint helm --namespace monitoring --strict [-f helm/values-dev.yaml | -f helm/values-prod.yaml]
helm template observability helm --namespace monitoring -f helm/values-prod.yaml > render-prod.yaml    # the same for dev
kubeconform -strict -summary -kubernetes-version 1.35.0 render-prod.yaml
bash scripts/schema-refusals.sh                            # 15 values the schema must refuse, and the pipeline's flag it must accept
DEMO_ERROR_RATE=0.3 docker compose up -d                   # then the queries below; docker compose down -v
```

| Check | Result |
|---|---|
| `helm dependency list` | four dependencies, all `ok` (exit 0) |
| `helm lint --strict`, default, dev and prod values | 1 chart linted, 0 failed, three times (exit 0); only the info line "icon is recommended" |
| `kubeconform -strict`, dev and prod renders | dev 30 resources, prod 33: valid 30/33, invalid 0, errors 0, **skipped 0** (the kinds are core and `networking.k8s.io`; nothing needed a CRD or was left out) |
| `scripts/schema-refusals.sh` | baseline renders; the pipeline's flag `--set-string podAnnotations.checksum/app-env=...` accepted (template and strict lint; before the fix both failed with "additional properties 'podAnnotations' not allowed", so every deploy would have been refused); 15 of 15 values refused with a schema error (a password, a LoadBalancer, an Ingress, a NodePort, Alertmanager or the node exporter switched on, Loki without its claim, a renamed Loki or Prometheus Service, Loki auth on, no admin Secret, a misspelled key, a smoke-test image without a digest, a bad address, a retention that is no duration) |
| The steps of [`chart-ci.yml`](.github/workflows/chart-ci.yml): dependency pin, provisioning files, rules of the stack, manifests | cut out of the workflow file and run locally: exit 0 each (the workflow itself has not run on GitHub: the shared workflow's input `build-image` is not pushed yet, see "Deploy") |

**The compose demo** (compose up with `DEMO_ERROR_RATE=0.3`, 150 seconds of traffic, then the queries; the stack was then taken down):

| What | Observed |
|---|---|
| Readiness | Loki `ready`, Prometheus `Prometheus Server is Ready.`, Grafana `"database": "ok"`, version 13.3.1 |
| Metrics | both Prometheus targets `up`; 8.04 requests a second for `demo-service`, 31 % of them 5xx (set to 30 %), p95 per route 0.04 s to 0.09 s and **0.93 s for `/v1/orders`** (the demo's slow tail: the shape the REQ-20 optimization starts from); 1 095 series |
| Logs | Loki labels `app`, `job`, `namespace`, `tenant`, plus `service_name`, which Loki 3 adds by itself from `app`; `tenant` values `tenant-a`, `tenant-b`; **a line without `tenantId` is stored without the `tenant` label** (three streams); 748 lines in 150 s; Loki counted 165 938 bytes for them: **222 bytes a line** (the fields that became labels are removed from the line) |
| Search | `|= "<correlationId>"` found the one line; the empty text filters of the dashboard's defaults match every line; `| json | level=~"error|fatal|50|60"` gave 1.34 error lines a second |
| Grafana | datasources Loki and Prometheus `OK`; the three dashboards in the folder Chance (15, 9 and 4 panels, variables `service, tenant` / `service, tenant, route` / `namespace, app, tenant, correlationId, search`); two alert rules, the contact point `chance-alerts` and the policy provisioned; one query through Grafana's `/api/ds/query` to each datasource answered 200 with a frame |
| Alerts | **both fired**: *WithdrawalFailed in the logs* was Firing by the first evaluation after the first such line (the alert's query returned 1), *A service answers with errors* went Pending, then Firing about 4 minutes after the start (the 2-minute `for`); both showed in Grafana's alert list. The log text of the failed e-mail send was not captured `[UNVERIFIED]` |
| Smoke test | the chart's own smoke-test script (cut out of the rendered Pod, only the two addresses changed to the compose ports) ran in a busybox container against the stack: four `ok`, exit 0; with Prometheus stopped it printed two `FAIL` lines after 60 s each and **exited 1** |
| What the containers used | Grafana 277 MiB (limit then 320 MiB: too tight, raised to 512), Loki 104 MiB, Prometheus 46 MiB, demo service 13 MiB, Fluent Bit 4.5 MiB; CPU under 1.5 % each; the volumes held 0.9 MB of Loki data and 0.3 MB of Prometheus data after 8 minutes |

**After the review of 2026-11-11** (the gateway's logs, the buffer, the pipeline's flag; same tools, Docker images pinned as in the chart): the lint, the renders (dev 30 and prod 33
resources, none skipped) and the schema script above were run again after the changes (exit 0). The compose demo was run again for 140 s with `DEMO_ERROR_RATE=0.3`: the three
dashboards loaded (15, 9 and 4 panels), the Loki queries of the logs dashboard with `namespace=~"prod|gateway"` answered (series for the rate, the error rate and the log panel; only
`prod` exists in the demo), the four queries of the new edge row ran in Prometheus without error and with no series (the demo has no Traefik or Keycloak), and both alert rules
were *firing* at the end. Two more local runs, one image each: (1) the rendered `fluent-bit.conf` passes `fluent-bit --dry-run` ("configuration test is successful"); with a Loki that
is not there, the old output (`Retry_Limit 5`, memory only) logged "chunk ... cannot be retried" and dropped chunks within seconds (scheduler timings cut to 1-2 s for the test), the
new one (`Retry_Limit no_limits`, `storage.type filesystem`) dropped none in the same 40 seconds and held 28 chunk files on disk (`storage.max_chunks_up 8`, so memory stayed
bounded; its growth is capped by `storage.total_limit_size 100M`, a cap that was configured but not filled in the test); (2) the rendered Loki configuration (3.7.9) with
`compactor_grpc_address: localhost:9095` started, took a push (204), answered a query and the delete-request listing (200), with the runtime-config file and the memberlist seed that
only a cluster has stubbed out or left to fail; the same run with the old Service address also answered, so the failure that finding was afraid of (a policy engine that filters
the Pod's call to its own Service) was **not reproduced**: the change takes the call out of the network and no longer depends on it. The Kubernetes filter's default `Buffer_Size`
is 32K (`fluent-bit -F kubernetes -h` of the pinned image); the effect of a larger Pod object was not tested.

**The cluster's Fluent Bit filter chain** ([`scripts/cluster-chain-check.py`](scripts/cluster-chain-check.py)): the rendered ConfigMap's `fluent-bit.conf`
passes `fluent-bit --dry-run` ("configuration test is successful"); with the tail input and the kubernetes filter replaced by two `dummy` inputs that emit
what the kubernetes filter would produce (one record with `tenantId`, one without), the rendered `nest` filter and Loki output stored them with the labels
`app=markets, namespace=prod, tenant=tenant-a` and, for the second, without `tenant`, and the line kept `k8s_pod_name` and `k8s_container_name` but
not `k8s_labels` and the other keys of `Remove_Keys`.

**What was not run** (so it is the first thing to check after the first deploy): anything in a cluster: the DaemonSet, the real `tail` input of
containerd's CRI files and the position file, the buffer in `/var/log/flb-storage` on a real node, the gateway's log lines (no Traefik log was read; whether it writes JSON or text is
not known here, text stays in the field `log`), the edge row of `RED overview` against a real Traefik and Keycloak, `scripts/cluster-chain-check.py` against the changed
`[SERVICE]` and output (it ran before the change only), the Kubernetes filter's metadata, the `kubernetes-pods` and cAdvisor scrape jobs and their RBAC
(the rendered Prometheus configuration was not passed through `promtool`), Kubernetes' handling of the claims' ownership, the probes, the
NetworkPolicies, `helm test` itself, and the alert for the e-mail path. The dashboards were loaded and queried through Grafana's API, not looked
at in a browser. Scraping `/metrics` of the real services waits for their charts.

## Deploy

The cluster work is approved when it is run (plan INF-03, ADR-039); nothing was installed into a cluster, and the pipeline below has not
run yet.

**The path** is the organization's shared workflow (ADR-051, `RSO-ekipa10/.github`, `service-ci-cd.yml`), like every service and the broker, called by
[`ci-cd.yml`](.github/workflows/ci-cd.yml) with `build-image: false` and `namespace: monitoring`. On a push to `main` it runs the chart checks, logs
in to Azure with OIDC, creates the Secret `observability-env` from the organization secret `OBSERVABILITY_ENV`, installs the chart with Helm 4 as release
`observability` into `monitoring` with `values-prod.yaml`, runs `helm test` and rolls the release back when it fails (ADR-047, ADR-048). On every deploy that creates the
Secret it also passes `--set-string podAnnotations.checksum/app-env=<hash>`; the values schema accepts that key (the schema script checks it), but nothing in this chart carries it to a Pod
(an umbrella chart's top-level value does not reach a subchart), so **a changed admin password does not restart Grafana by itself**: after changing `OBSERVABILITY_ENV`, run
`kubectl rollout restart deployment/grafana -n monitoring` (Grafana keeps no state, so the restart applies the new password at once). By hand,
with the same approval, the deploy is what the workflow runs: the `helm upgrade --install observability ./helm -n monitoring -f helm/values-prod.yaml
--rollback-on-failure --wait --timeout 5m` command and then `helm test observability -n monitoring --timeout 2m --logs` (the Secret must exist first).

**Before the first deploy:**

1. The input `build-image` of the shared workflow exists only after `org-github` is pushed and merged (as for the broker's README, "Deploy").
2. The namespace is created once by hand (`monitoring`), and the organization secret `OBSERVABILITY_ENV` holds the lines `admin-user=admin` and
   `admin-password=<a long random password>` (optionally `smtp-user=`, `smtp-password=`). Without the Secret Grafana's Pod cannot start and the deploy rolls back.
3. In the infrastructure repository: one line `observability = "production"` in `local.deploy_repositories` (the federated credential and the
   Azure variables for this repository, as for the broker) and the `production` environment limited to `main`.
4. The services' charts carry the three annotations and the NetworkPolicy value of "How the services hook in", and the services serve
   `/metrics`; a service that does not has no series in the RED dashboards. Since 2026-11-12 all eight application services do (see "Who serves
   metrics today"); what is still missing is a scrape in a real cluster.
5. The first pull of the Grafana image is large (about 2 GB on disk, 1.96 GB measured as the image size on 2026-11-11): on a small node the first
   rollout may take longer than the 5 minutes of `--timeout`; if it rolls back for that reason, pull the image by starting the Pod once or raise the timeout for the first run.

## Update a chart

Dependabot is not set up for the dependencies (a bump must also replace the packaged chart in `helm/charts/`, which only `helm dependency
update` does), so the charts are updated by hand: find the new stable version in the repository index (`<repository>/index.yaml`, newest entry
that is not a pre-release), set the exact version in `helm/Chart.yaml`, run `helm dependency update helm` once, commit `helm/charts/` and
`helm/Chart.lock`, raise the chart `version`, and move the image tag and digest in `helm/values.yaml` and `compose.yaml` to the new app version
(`docker pull`, then the digest from `docker image inspect`). Read the chart's changelog first (Loki's and Grafana's charts have breaking
changes between majors). The pipeline fails when a vendored chart does not match `Chart.yaml` or the lock.

## Follow-ups and open questions

1. **Not run in a cluster.** `helm template` and `kubeconform` pass and the components ran in Docker, but the DaemonSet, the Kubernetes
   filter and the `kubernetes-pods` and cAdvisor scrape jobs need a cluster: the first deploy is the missing evidence (the checks list what ran).
2. **A network policy engine.** The policies of this chart and of the services do nothing without one (chosen at cluster creation); check at M1.
3. **Grafana's e-mail.** Decide whether alerts go to an address (needs an SMTP server; Mailpit locally and a relay for the identity service are
   already in the plan) or stay as visible alert state and a log line. **Open question for the team.**
4. **No backup of the two claims**, and no authentication between the components (the NetworkPolicy is the only control). Fine for a demo's
   logs and metrics.
5. **Loki's ClusterRole.** The Loki chart creates a ClusterRole with no rules and a ServiceAccount for a memcached that is switched off: harmless,
   noted so nobody searches for what they are for.
6. **Fluent Bit runs as root** with `/var/log` mounted read-write (it must read the root-owned log files and keeps its position file there).
7. **The label `tenant`** is one stream per tenant per service: fine for the handful of tenants of the demo (plan section 9); see "How the services hook in".
8. **The optimization of REQ-20** is not done: it needs traffic after the first deploy (the section above). All eight services serve the numbers it starts from since 2026-11-12.
9. **The WithdrawalFailed alert rests on one info line of trading** (see "Alerts"): payments should log a `warn` line when it settles a failed payout. A change in the payments repository.
10. **The edge row of `RED overview`** (Traefik, Keycloak) uses metric names that no real scrape has confirmed; check them at the first deploy.
11. **The buffer on the node** (`/var/log/flb-storage`, at most 100 MB per Fluent Bit Pod) is written by the Pod that runs as root (follow-up 6); after an outage the unsent chunks are sent when Loki is back.
