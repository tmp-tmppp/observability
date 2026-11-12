#!/usr/bin/env bash
# The values schema must refuse what would break the stack. Each case below is a value that has to make "helm template" fail
# with a schema error; the baseline (the prod values) has to pass, and so has the one flag the shared workflow adds on every deploy.
# Run from the repository root: scripts/schema-refusals.sh
# Exit 0 = every refusal happened and the pipeline's flag was accepted.
set -u
cd "$(dirname "$0")/.." || exit 2
chart=helm
base=(--namespace monitoring -f "$chart/values-prod.yaml")
fail=0

if helm template observability "$chart" "${base[@]}" > /dev/null 2> /tmp/obs-schema-base.err; then
  echo "ok   baseline (prod values) renders"
else
  echo "FAIL baseline does not render:"; cat /tmp/obs-schema-base.err; exit 1
fi

# What the shared workflow passes on every deploy that creates the Secret observability-env (service-ci-cd.yml, "Deploy with
# Helm"): this value must render, with "helm lint --strict" as well, or no deploy of the stack can start.
flag="podAnnotations.checksum/app-env=0123abcd"
if helm template observability "$chart" "${base[@]}" --set-string "$flag" > /dev/null 2> /tmp/obs-schema-flag.err \
  && helm lint "$chart" --namespace monitoring --strict -f "$chart/values-prod.yaml" --set-string "$flag" > /dev/null 2>> /tmp/obs-schema-flag.err; then
  echo "ok   accepted: the shared workflow's --set-string $flag (template and strict lint)"
else
  echo "FAIL the shared workflow's flag --set-string $flag is refused:"; head -n 5 /tmp/obs-schema-flag.err; fail=1
fi

# case name | --set argument
cases=(
  "a password in the values|grafana.adminPassword=hunter2"
  "Grafana as a LoadBalancer|grafana.service.type=LoadBalancer"
  "Grafana with an Ingress|grafana.ingress.enabled=true"
  "Prometheus as a NodePort|prometheus.server.service.type=NodePort"
  "Alertmanager switched on|prometheus.alertmanager.enabled=true"
  "node exporter switched on|prometheus.prometheus-node-exporter.enabled=true"
  "Loki without its claim|loki.singleBinary.persistence.enabled=false"
  "Loki service renamed|loki.fullnameOverride=logs"
  "Prometheus service renamed|prometheus.server.fullnameOverride=prom"
  "Loki with its own tenants|loki.loki.auth_enabled=true"
  "no admin Secret|grafana.admin.existingSecret="
  "a misspelled key of this chart|networkPolcy.enabled=true"
  "a smoke test image without a digest|smokeTest.image=docker.io/library/busybox:1.37.0"
  "an e-mail address that is not one|alerting.emailAddresses[0]=not-an-address"
  "retention that is not a duration|prometheus.server.retention=forever"
)
for c in "${cases[@]}"; do
  name=${c%%|*}
  arg=${c#*|}
  if helm template observability "$chart" "${base[@]}" --set "$arg" > /dev/null 2> /tmp/obs-schema-case.err; then
    echo "FAIL $name: --set $arg was accepted"; fail=1
  elif grep -q -i -E "schema|values don't meet" /tmp/obs-schema-case.err; then
    echo "ok   refused: $name ($(grep -m1 -E "^- at|^- |additional|must|not " /tmp/obs-schema-case.err | head -c 110))"
  else
    echo "FAIL $name: failed, but not with a schema error:"; head -n 3 /tmp/obs-schema-case.err; fail=1
  fi
done
exit "$fail"
