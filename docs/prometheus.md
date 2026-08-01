Prometheus metrics and alerts
=============================

Quickstart
----------

1. Install the Prometheus client into your runtime environment (venv):

```bash
.venv/bin/pip install prometheus_client
```

2. Enable Prometheus exposition and wire metrics into the `ProviderHealthRegistry`:

```python
from engine.provider_health import ProviderHealthRegistry
from engine.prometheus_helper import enable_prometheus_for_registry

registry = ProviderHealthRegistry()
# attach and start exposition on port 8000
sink = enable_prometheus_for_registry(registry, port=8000)
# now registry.record_failure / record_success will emit metrics to Prometheus
```

3. Verify metrics are exposed:

```bash
curl http://localhost:8000/metrics
```

Alert rules
-----------

We include a minimal set of alert rules you can load into Prometheus: `prometheus/alerts.yml`.
These cover provider circuit opens and probe failure spikes which are the most useful early warnings.

Notes
-----
- The code provides a `PrometheusMetricsSink` implementation in `engine/prometheus_sink.py` and a helper in `engine/prometheus_helper.py`.
- If you run Prometheus, add `prometheus/alerts.yml` to your Prometheus `rule_files` and reload.
