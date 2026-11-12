"""Builds a test configuration from the Fluent Bit configuration that the chart really renders, so the parts that cannot run
without a cluster (the nest filter and the Loki output with its labels) can be run against a Loki.

It reads the rendered ConfigMap on stdin (helm template ... --show-only charts/fluent-bit/templates/configmap.yaml), takes
the key fluent-bit.conf, and:
  - replaces the [INPUT] (tail) by two `dummy` inputs that emit what the kubernetes filter would have produced: a record with
    the application's JSON fields merged in and a `kubernetes` map (one with tenantId, one without);
  - drops the [FILTER] kubernetes (it needs the API server);
  - keeps everything else as rendered (the nest filter, the Loki output and its labels and Remove_Keys), changing only the Loki
    host to the one given as the first argument.
The result goes to stdout. Standard library only.
"""
import json
import re
import sys

host = sys.argv[1] if len(sys.argv) > 1 else "loki"
text = sys.stdin.read()

m = re.search(r"^  fluent-bit\.conf: \|\n((?:    .*\n|\n)+)", text, re.M)
if not m:
    sys.exit("fluent-bit.conf not found in the ConfigMap")
conf = "\n".join(line[4:] for line in m.group(1).split("\n"))

blocks = re.split(r"(?m)^(?=\[(?:SERVICE|INPUT|FILTER|OUTPUT)\])", conf)
keep = []
for b in blocks:
    head = b.split("\n", 1)[0].strip()
    if head == "[INPUT]":
        continue
    if head == "[FILTER]" and re.search(r"(?m)^\s*Name\s+kubernetes\s*$", b):
        continue
    keep.append(b)
conf = "".join(keep)
conf = re.sub(r"(?m)^(\s*Host\s+).*$", lambda mm: mm.group(1) + host, conf)
# The tail input is gone, so the position database and the HTTP server are not needed either; the server stays for the health check.

k8s = {
    "pod_name": "markets-6d9f7c5b8-x2x4q",
    "namespace_name": "prod",
    "pod_id": "11111111-2222-3333-4444-555555555555",
    "labels": {"app.kubernetes.io/name": "markets", "app.kubernetes.io/instance": "markets", "pod-template-hash": "6d9f7c5b8"},
    "host": "aks-nodepool1-00000000-vmss000000",
    "container_name": "markets",
    "docker_id": "abcdef",
    "container_hash": "docker.io/example/markets@sha256:0000",
    "container_image": "docker.io/example/markets:sha-0000",
}
with_tenant = {"level": 30, "msg": "request completed", "service": "markets", "tenantId": "tenant-a", "correlationId": "chain-check-1", "kubernetes": k8s}
without_tenant = {"level": 30, "msg": "listening on 3000", "service": "markets", "correlationId": "chain-check-2", "kubernetes": k8s}
inputs = ""
for rec, tag in ((with_tenant, "kube.chain.a"), (without_tenant, "kube.chain.b")):
    inputs += "[INPUT]\n    Name dummy\n    Tag %s\n    Rate 1\n    Samples 5\n    Dummy %s\n\n" % (tag, json.dumps(rec, separators=(",", ":")))

# The chart's [SERVICE] block points at parsers files of the image; keep it as rendered, put the inputs after it.
head, _, rest = conf.partition("[FILTER]")
sys.stdout.write(head + inputs + "[FILTER]" + rest if rest else head + inputs)
