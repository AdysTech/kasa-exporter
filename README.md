# Kasa Smart Power Exporter

A Prometheus exporter for TP-Link Kasa smart plugs and power strips that exposes real-time power consumption, voltage, current, and energy metrics.

## 🔒 Pure Local & Offline-First Design

### Local Control & Air-Gapped Networks

This exporter communicates exclusively over the local network (LAN) using direct socket calls via `python-kasa`. It is specifically designed for devices that are blocked from WAN access / TP-Link Cloud via firewall rules or isolated on dedicated IoT VLANs.

All device communication happens over your internal network. No external API endpoints, no telemetry phone-home, and no dependency on public DNS or internet connectivity. This makes it ideal for:

- **Air-gapped environments** where smart devices must never touch the internet
- **IoT VLAN deployments** where devices are firewalled from WAN
- **Privacy-first setups** that block all TP-Link cloud outbound traffic
- **Edge / remote sites** with no or unreliable internet connectivity

## ⚠️ Cloud Auth Disclaimer

### No TP-Link Cloud Support

This exporter does **not** handle TP-Link Cloud account authentication or remote cloud API polling. Devices must be reachable via their local IP address on your LAN.

While the `python-kasa` library does support cloud authentication for certain device types, this exporter has not been tested with cloud-authenticated devices and is designed around direct local communication. If your devices require TP-Link Cloud credentials (which require authentication via `--username` and `--password` in python-kasa) this exporter would need enhancements to pass them.

## Architecture & Design Decisions

### 1. Native Compatibility with Label-Based Auto-Discovery

Many Prometheus exporters use the [Multi-Target Probe Pattern](https://prometheus.io/docs/prometheus/latest/configuration/query_examples/) (`/scrape?target=192.168.1.X`). This breaks standard auto-discovery engines (Grafana Alloy, Kubernetes Prometheus Operator, Docker Swarm label rules) because collectors expect a simple `http://<container_ip>:<port>/metrics` target.

This container behaves as a standard 12-factor microservice exposing a single `/metrics` endpoint. Grafana Alloy or Prometheus auto-discovery picks it up immediately via container labels (`prometheus-port=9233`) without requiring target-relabeling rules. All configured devices are exposed as distinct label sets within the same scrape — no dynamic target injection needed.

### 2. Decoupled Asynchronous Polling (Zero Scrape Timeouts)

Many lightweight exporters poll physical Kasa hardware synchronously when Prometheus initiates an HTTP scrape. If local Wi-Fi drops a frame or the plug delays its response, the scrape times out, resulting in missed metrics or false alerting.

This exporter separates the background `asyncio` poll loop from the Prometheus HTTP server. A scrape request to `/metrics` returns in under 5ms directly from in-memory gauges. Wi-Fi hiccups do not cause Prometheus scrape timeouts. The exporter continues polling silently in the background and updates gauges on the next successful poll cycle.

### 3. Offloaded Protocol Maintenance (python-kasa Core)

Older standalone exporters written in Go or C often break when TP-Link updates local firmware or switches security protocols (e.g., legacy XOR port 9999 vs. modern KLAP, AES, or SMART protocols).

This exporter relies on `python-kasa` for all protocol communication, delegating maintenance to the active Home Assistant and python-kasa developer community. When TP-Link changes firmware behavior, a single `pip install --upgrade python-kasa` in the Docker build restores compatibility. No custom protocol handshakes are maintained in-house.

### 4. Out-of-the-Box Support for Multi-Outlet Strips & Unit Normalization

Simple single-plug exporters fail or only read Outlet 0 on 6-port strips (HS300, KP303) because they do not query child device contexts. Raw TP-Link firmware returns stats in millivolts (e.g., `110000 mV`), leaving users with raw values unless they write Prometheus recording rules.

`python-kasa` handles parent-child socket iteration (`strip.children`) and normalizes milli-units to standard Volts (V), Watts (W), Amps (A), and kWh. Every child outlet is cleanly labeled with `outlet_index` and `outlet_name`, making dashboard queries straightforward without post-processing.

### 5. 12-Factor / Swarm-Native Configuration

The exporter follows [12-factor app](https://12factor.net/config) principles. All configuration is delivered via environment variables or a single mounted YAML file, making it easy to deploy as:

- Docker Compose service with `environment:` keys
- Docker Swarm task with constraints and labels
- Kubernetes Pod with ConfigMap volumes and env refs
- Bare-metal systemd service with `EnvironmentFile=`

The YAML volume mount is read-only (`:ro`) for container security.

## Metrics

All metrics are exposed as Prometheus `Gauge` types.

### Exporter Health

| Metric | Description | Labels |
|--------|-------------|--------|
| `kasa_exporter_up` | Whether the exporter is healthy and config is valid (`1`=up, `0`=down) | *(none)* |
| `kasa_device_reachable` | Whether the device responded to the last poll (`1`=reachable, `0`=unreachable) | `device_ip`, `device_name` |
| `kasa_last_error_code` | Numeric code of the last error encountered per device (`0`=none, `1`=unreachable, `2`=timeout, `3`=auth_failure, `99`=unknown) | `device_ip`, `device_name`, `error_type` |

### System Metadata & Health

| Metric | Description | Labels |
|--------|-------------|--------|
| `kasa_device_info` | Device metadata (static info metric set to `1`) | `device_ip`, `device_name`, `model`, `firmware`, `hardware`, `mac` |
| `kasa_device_rssi_dbm` | Wi-Fi signal strength in dBm | `device_ip`, `device_name`, + global labels |
| `kasa_device_uptime_seconds` | Device uptime in seconds | `device_ip`, `device_name`, + global labels |

### Root Device Aggregate (Whole Strip / Power Plug Total)

| Metric | Description | Labels |
|--------|-------------|--------|
| `kasa_device_power_watts` | Total real-time power draw for entire strip in Watts | `device_ip`, `device_name`, + global labels |
| `kasa_device_voltage_volts` | Main line voltage in Volts | `device_ip`, `device_name`, + global labels |
| `kasa_device_current_amps` | Total current draw across whole strip in Amps | `device_ip`, `device_name`, + global labels |
| `kasa_device_total_kwh` | Total cumulative energy consumption in kWh | `device_ip`, `device_name`, + global labels |

### Per-Outlet Child Metrics (Individual Sockets)

| Metric | Description | Labels |
|--------|-------------|--------|
| `kasa_outlet_state` | Outlet power state (`1`=ON, `0`=OFF) | `device_ip`, `device_name`, `outlet_index`, `outlet_name`, + custom labels |
| `kasa_outlet_power_watts` | Real-time outlet power draw in Watts | `device_ip`, `device_name`, `outlet_index`, `outlet_name`, + custom labels |
| `kasa_outlet_voltage_volts` | Outlet voltage in Volts | `device_ip`, `device_name`, `outlet_index`, `outlet_name`, + custom labels |
| `kasa_outlet_current_amps` | Outlet current draw in Amperes | `device_ip`, `device_name`, `outlet_index`, `outlet_name`, + custom labels |
| `kasa_outlet_total_kwh` | Cumulative outlet energy consumption in kWh | `device_ip`, `device_name`, `outlet_index`, `outlet_name`, + custom labels |

### Labels

All metrics include the following core labels:

- `device_ip` — IP address of the Kasa device
- `device_name` — Alias or override name for the device

Additional **global labels** and **per-outlet custom labels** can be defined in `config.yaml` and will be attached to all metrics. Example custom labels from the default config:

- `environment` — Global label applied to all metrics
- `site` — Global label applied to all metrics
- `role` — Per-outlet label describing the outlet purpose
- `target_app` — Per-outlet label for application mapping

### Error Codes (`kasa_last_error_code`)

| Code | `error_type` label | Meaning |
|------|-------------------|---------|
| `0` | `none` | No error; device polled successfully |
| `1` | `unreachable` | Device refused connection or network-level failure |
| `2` | `timeout` | Connection exceeded `KASA_CONNECT_TIMEOUT` (default 10s) |
| `3` | `auth_failure` | Credentials rejected by the device |
| `99` | `unknown` | Unclassified error |

## Configuration

Configuration is handled via a `config.yaml` file (default path, override with `CONFIG_PATH` environment variable).

```yaml
server:
  port: 9233
  poll_interval: 10  # Seconds between background polls

# Global extra labels applied to ALL metrics exported by this container
global_labels:
  environment: "home-lab"
  site: "brampton"

devices:
  - ip: "192.168.1.100"       # IP address of the device
    # host: "kasa-strip.local"  # OR hostname (either ip or host is required)
    name_override: "Core-Rack-Strip"
    outlets:
      0:
        name: "UPS-Main"
        labels:
          role: "infrastructure"
          target_app: "nut-server"
      1:
        name: "Proxmox-Node-01"
        labels:
          role: "compute"
          target_app: "proxmox"
```

### Device Configuration Fields

Each device entry supports the following fields:

| Field | Required | Description |
|-------|----------|-------------|
| `ip` | One of `ip` or `host` | IP address of the Kasa device (e.g., `"192.168.1.100"`) |
| `host` | One of `ip` or `host` | Hostname of the Kasa device (e.g., `"kasa-strip.local"`). Useful when devices have dynamic IPs but stable mDNS/DNS names. If both `ip` and `host` are provided, `host` takes precedence for connection. |
| `name_override` | No | Custom name for the device, used in the `device_name` label. Defaults to `kasa_<address>` if not set. |
| `outlets` | No | Map of outlet index to outlet configuration (name, custom labels) |

### Configuration Validation (Edge Cases)

The exporter validates `config.yaml` at startup and exits with a non-zero code on fatal errors:

| Edge Case | Behavior |
|-----------|----------|
| **Config file missing** | Fatal error, exits with `sys.exit(1)` |
| **Malformed YAML** | Fatal error, logs parse details then exits |
| **Empty / comment-only config** | Fatal error, exits with descriptive message |
| **Missing `devices` section** | Warning logged; exporter continues (no devices to poll) |
| **Device entry missing both `ip` and `host`** | Fatal error, exits |
| **Invalid IP address format** | Fatal error, exits with details (only validated if `ip` is provided) |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_PATH` | `config.yaml` | Path to the YAML config file |
| `KASA_CONNECT_TIMEOUT` | `10` | Seconds to wait before marking device connection as timeout |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |

## Docker

### Build Locally

```bash
docker build -t kasa-exporter .
```

### Run

```bash
docker run -d \
  --name kasa-exporter \
  --network host \
  -v ./config.yaml:/app/config.yaml:ro \
  kasa-exporter
```

The exporter listens on port `9233` by default. Adjust the `server.port` setting in `config.yaml` as needed.

### Pull from GitHub Container Registry

```bash
docker pull ghcr.io/adystech/kasa-exporter:latest
```

### Docker Swarm Deploy

```yaml
services:
  kasa-exporter:
    image: ghcr.io/adystech/kasa-exporter:latest
    networks:
      - monitoring  # Must match network used by Grafana Alloy / Prometheus
    volumes:
      - ./config.yaml:/app/config.yaml:ro
    deploy:
      mode: replicated
      replicas: 1
      labels:
        - prometheus-job=kasa_power_exporter
        - prometheus-port=9233
        - prometheus-scrape-interval=15s
```

## Dependencies

- [python-kasa](https://github.com/python-kasa/python-kasa) — Local protocol communication with Kasa devices
- [prometheus_client](https://pypi.org/project/prometheus_client/) — Prometheus metric exposition
- [PyYAML](https://pypi.org/project/pyyaml/) — Configuration parsing

## License

MIT
