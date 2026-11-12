"""A stand-in for one Chance service, for the compose demo of the logging and metrics stack.

It does what the services agreed to do and nothing else:
  - one JSON object per line on stdout, pino-style: level (30 info, 40 warn, 50 error), time (epoch milliseconds), service,
    tenantId, correlationId, msg; a few lines carry no tenantId (start-up, health checks), as in a real service;
  - GET /metrics on METRICS_PORT (9464): http_requests_total (counter) and http_request_duration_seconds (histogram) with the
    labels method, route, status_code, tenant, in the Prometheus text format.
It makes up its traffic: DEMO_RATE requests a second over two tenants and four routes, DEMO_ERROR_RATE of them end in a 500, and
the route /v1/orders has a slow tail (the thing a RED dashboard shows and the optimization of REQ-20 starts from). Every
DEMO_WITHDRAWAL_FAILED_EVERY seconds it writes a WithdrawalFailed line, which the alert rule of REQ-19 watches.
Standard library only.
"""
import json
import math
import os
import random
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = os.environ.get("DEMO_SERVICE", "demo-service")
PORT = int(os.environ.get("METRICS_PORT", "9464"))
RATE = float(os.environ.get("DEMO_RATE", "8"))
ERROR_RATE = float(os.environ.get("DEMO_ERROR_RATE", "0.02"))
WITHDRAWAL_EVERY = float(os.environ.get("DEMO_WITHDRAWAL_FAILED_EVERY", "90"))
BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]  # the defaults of prom-client
TENANTS = ["tenant-a", "tenant-b"]
ROUTES = [("GET", "/v1/markets", 0.020), ("GET", "/v1/markets/:id", 0.015), ("POST", "/v1/orders", 0.060), ("GET", "/v1/portfolio", 0.030)]

lock = threading.Lock()
counts = {}  # (method, route, status_code, tenant) -> int
hist = {}  # same key -> [bucket counts..., +Inf count]
sums = {}  # same key -> sum of durations


def log(level, msg, tenant=None, correlation_id=None, **extra):
    line = {"level": level, "time": int(time.time() * 1000), "service": SERVICE, "msg": msg}
    if tenant is not None:
        line["tenantId"] = tenant
    if correlation_id is not None:
        line["correlationId"] = correlation_id
    line.update(extra)
    sys.stdout.write(json.dumps(line, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def observe(method, route, status, tenant, seconds):
    key = (method, route, str(status), tenant)
    with lock:
        counts[key] = counts.get(key, 0) + 1
        b = hist.setdefault(key, [0] * (len(BUCKETS) + 1))
        for i, upper in enumerate(BUCKETS):
            if seconds <= upper:
                b[i] += 1
        b[-1] += 1
        sums[key] = sums.get(key, 0.0) + seconds


def render():
    out = ["# HELP http_requests_total Total HTTP requests.", "# TYPE http_requests_total counter"]
    with lock:
        for (m, r, s, t), n in sorted(counts.items()):
            out.append('http_requests_total{method="%s",route="%s",status_code="%s",tenant="%s"} %d' % (m, r, s, t, n))
        out += ["# HELP http_request_duration_seconds Duration of HTTP requests in seconds.", "# TYPE http_request_duration_seconds histogram"]
        for key, b in sorted(hist.items()):
            m, r, s, t = key
            lab = 'method="%s",route="%s",status_code="%s",tenant="%s"' % (m, r, s, t)
            for i, upper in enumerate(BUCKETS):
                out.append('http_request_duration_seconds_bucket{%s,le="%s"} %d' % (lab, upper, b[i]))
            out.append('http_request_duration_seconds_bucket{%s,le="+Inf"} %d' % (lab, b[-1]))
            out.append("http_request_duration_seconds_sum{%s} %.6f" % (lab, sums[key]))
            out.append("http_request_duration_seconds_count{%s} %d" % (lab, b[-1]))
    return ("\n".join(out) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = render()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # no access log of the metrics endpoint
        pass


def traffic():
    next_failed = time.time() + (WITHDRAWAL_EVERY if WITHDRAWAL_EVERY > 0 else math.inf)
    while True:
        time.sleep(random.expovariate(RATE))
        tenant = random.choice(TENANTS)
        method, route, median = random.choice(ROUTES)
        cid = str(uuid.uuid4())
        seconds = random.lognormvariate(math.log(median), 0.5)
        if route == "/v1/orders" and random.random() < 0.15:
            seconds += random.uniform(0.4, 1.2)  # the slow tail
        status = 500 if random.random() < ERROR_RATE else (404 if random.random() < 0.01 else 200)
        observe(method, route, status, tenant, seconds)
        if status >= 500:
            log(50, "request failed", tenant, cid, method=method, route=route, statusCode=status, responseTime=round(seconds * 1000))
        elif random.random() < 0.3:  # not every request is logged at info level
            log(30, "request completed", tenant, cid, method=method, route=route, statusCode=status, responseTime=round(seconds * 1000))
        if random.random() < 0.05:
            log(30, "health check")  # no tenant
        if time.time() >= next_failed:
            log(40, "WithdrawalFailed: payout was declined, the credits are refunded", tenant, cid, event="WithdrawalFailed", withdrawalId=str(uuid.uuid4()))
            next_failed = time.time() + WITHDRAWAL_EVERY


if __name__ == "__main__":
    log(30, "listening on %d (metrics at /metrics)" % PORT)  # no tenant
    threading.Thread(target=traffic, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
