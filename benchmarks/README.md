# Local benchmarks

Run the benchmark harness from a source checkout:

```bash
python -m benchmarks.benchmark_local --iterations 1000 --warmup 100
```

To retain raw JSON locally:

```bash
python -m benchmarks.benchmark_local --output benchmark_results.local.json
```

The output records the commit, timestamp, Python runtime, dependency versions, operating system, logical CPU count, workload size, sample count, warmup count, and latency distribution. Percentiles use the nearest-rank observation selected from sorted samples. Standard deviation is the sample standard deviation.

## Published reference

- [Reference report](REFERENCE_RESULTS.md)
- [Raw JSON](results/windows-python314-ddb2cd5.json)

The reference measures bounded local operations: pattern scrubbing, Ed25519
signing/verification, local Passport issue/verification/Runtime mapping, SDK
event recording into a local bounded queue with a stubbed delivery client, and
an in-process mock-gateway request. Passport cases use in-process keys and
caller-supplied status: they do not measure external issuer trust, revocation
network latency, standard interoperability, sustained throughput, multi-user
operation, production capacity, or competing products.

Results describe only the recorded run and are not service-level objectives or universal performance claims. Rerun the harness on your own target system before making engineering decisions.
