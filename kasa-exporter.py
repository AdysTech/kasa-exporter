import os
import sys
import yaml
import asyncio
import logging
import socket
import ipaddress
from datetime import datetime, timezone
from prometheus_client import start_http_server, Gauge
from kasa import Device

CONFIG_PATH = os.getenv("CONFIG_PATH", "config.yaml")
CONNECT_TIMEOUT = int(os.getenv("KASA_CONNECT_TIMEOUT", "10"))

# ─── Edge-case Gauges: exporter health tracking ──────────────────────
GAUGE_EXPORTER_UP = Gauge(
    'kasa_exporter_up',
    'Whether the exporter is healthy and config is valid', []
)
GAUGE_DEVICE_REACHABLE = Gauge(
    'kasa_device_reachable',
    'Whether the device responded to the last poll (1=yes, 0=no)',
    ['device_ip', 'device_name']
)
GAUGE_LAST_ERROR_CODE = Gauge(
    'kasa_last_error_code',
    'Numeric code of the last error encountered per device (0=none)',
    ['device_ip', 'device_name', 'error_type']
)

# ─── Config Loading with Validation ─────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_PATH):
        logging.critical(f"Config file not found: {CONFIG_PATH}")
        raise FileNotFoundError(f"Config not found at {CONFIG_PATH}. Mount config.yaml or set CONFIG_PATH.")
    try:
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        logging.critical(f"Malformed YAML in {CONFIG_PATH}: {exc}")
        raise ValueError("Invalid YAML") from exc
    if not config:
        logging.critical(f"Config file {CONFIG_PATH} is empty.")
        raise ValueError("Config file is empty or contains only comments.")
    devices = config.get("devices")
    if not devices:
        logging.warning("No 'devices' section found in config.")
    for idx, dev in enumerate(devices):
        ip = dev.get("ip")
        host = dev.get("host")
        if not ip and not host:
            raise ValueError(f"Device #{idx} missing required field: either 'ip' or 'host' must be provided.")
        if ip:
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                raise ValueError(f"'{ip}' is not a valid IP address for device #{idx}.")
    return config

try:
    config = load_config()
except (FileNotFoundError, ValueError) as exc:
    logging.critical(f"Fatal config error: {exc}")
    sys.exit(1)

# Signal healthy startup
GAUGE_EXPORTER_UP.set(1)

global_labels = config.get("global_labels", {})

# Extract custom outlet label keys from config
outlet_custom_keys = set()
for dev in config.get("devices", []):
    for idx, outlet_info in dev.get("outlets", {}).items():
        if isinstance(outlet_info, dict) and "labels" in outlet_info:
            for k in outlet_info["labels"].keys():
                outlet_custom_keys.add(k)
for k in global_labels.keys():
    outlet_custom_keys.add(k)

GLOBAL_LABEL_KEYS = sorted(list(global_labels.keys()))
DEVICE_LABEL_KEYS = ['device_ip', 'device_name'] + GLOBAL_LABEL_KEYS
OUTLET_CUSTOM_LABEL_KEYS = sorted(list(outlet_custom_keys))
OUTLET_LABEL_KEYS = ['device_ip', 'device_name', 'outlet_index', 'outlet_name'] + OUTLET_CUSTOM_LABEL_KEYS

# ─── Prometheus Gauges ──────────────────────────────────────────────
GAUGE_INFO = Gauge('kasa_device_info', 'Device metadata',
    ['device_ip', 'device_name', 'model', 'firmware', 'hardware', 'mac'])
GAUGE_RSSI = Gauge('kasa_device_rssi_dbm', 'Wi-Fi signal strength in dBm', DEVICE_LABEL_KEYS)
GAUGE_UPTIME = Gauge('kasa_device_uptime_seconds', 'Device uptime in seconds', DEVICE_LABEL_KEYS)

# Root Device Aggregate (Whole Strip / Plug Total)
GAUGE_DEV_POWER = Gauge('kasa_device_power_watts', 'Total real-time power draw for entire strip in Watts', DEVICE_LABEL_KEYS)
GAUGE_DEV_VOLTAGE = Gauge('kasa_device_voltage_volts', 'Main line voltage in Volts', DEVICE_LABEL_KEYS)
GAUGE_DEV_CURRENT = Gauge('kasa_device_current_amps', 'Total current draw across whole strip in Amps', DEVICE_LABEL_KEYS)
GAUGE_DEV_ENERGY = Gauge('kasa_device_total_kwh', 'Total cumulative energy consumption in kWh', DEVICE_LABEL_KEYS)
GAUGE_DEV_ENERGY_TODAY = Gauge('kasa_device_energy_today_kwh', "Today's energy consumption for entire device in kWh", DEVICE_LABEL_KEYS)
GAUGE_DEV_ENERGY_MONTH = Gauge('kasa_device_energy_month_kwh', "This month's energy consumption for entire device in kWh", DEVICE_LABEL_KEYS)

# Per-Outlet Child Metrics
GAUGE_STATE = Gauge('kasa_outlet_state', 'Outlet power state (1=ON, 0=OFF)', OUTLET_LABEL_KEYS)
GAUGE_POWER = Gauge('kasa_outlet_power_watts', 'Real-time outlet power draw in Watts', OUTLET_LABEL_KEYS)
GAUGE_VOLTAGE = Gauge('kasa_outlet_voltage_volts', 'Outlet voltage in Volts', OUTLET_LABEL_KEYS)
GAUGE_CURRENT = Gauge('kasa_outlet_current_amps', 'Outlet current draw in Amperes', OUTLET_LABEL_KEYS)
GAUGE_ENERGY = Gauge('kasa_outlet_total_kwh', 'Cumulative outlet energy consumption in kWh', OUTLET_LABEL_KEYS)
GAUGE_ENERGY_TODAY = Gauge('kasa_outlet_energy_today_kwh', "Today's energy consumption per outlet in kWh", OUTLET_LABEL_KEYS)
GAUGE_ENERGY_MONTH = Gauge('kasa_outlet_energy_month_kwh', "This month's energy consumption per outlet in kWh", OUTLET_LABEL_KEYS)


# ─── DNS Resolution Helper ──────────────────────────────────────────
def resolve_host(host):
    """Resolve a hostname to an IP address, or return the host if it's already an IP."""
    try:
        ipaddress.ip_address(host)
        return host  # Already an IP
    except ValueError:
        pass
    try:
        info = socket.getaddrinfo(host, None, socket.AF_INET)
        return info[0][4][0] if info else host
    except (socket.gaierror, IndexError):
        return host

# ─── Error Classification Helper ────────────────────────────────────
def classify_error(exc):
    """Return (error_type, error_code) for GAUGE_LAST_ERROR_CODE."""
    msg = str(exc).lower()
    if "auth" in msg or "credential" in msg:
        return "auth_failure", 3
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, OSError, socket.gaierror)):
        return "unreachable", 1
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout", 2
    return "unknown", 99


async def connect_device(address):
    try:
        dev = await Device.connect(host=address)
        await dev.update()
        return dev
    except (TimeoutError, asyncio.TimeoutError):
        logging.warning(f"Connection timed out after {CONNECT_TIMEOUT}s for {address}")
        raise


async def poll_device(device_cfg, poll_interval):
    ip = device_cfg.get("ip", "")
    host = device_cfg.get("host", "")
    # Use host if provided, otherwise fall back to ip
    device_address = host or ip
    outlet_cfg = device_cfg.get("outlets", {})
    dev_name = device_cfg.get("name_override") or f"kasa_{device_address}"

    # Resolve hostname to actual IP address for all label values
    resolved_ip = resolve_host(device_address) if device_address else ""

    # Initialize gauges for error tracking
    GAUGE_DEVICE_REACHABLE.labels(device_ip=resolved_ip, device_name=dev_name).set(0)
    GAUGE_LAST_ERROR_CODE.labels(device_ip=resolved_ip, device_name=dev_name, error_type='none').set(0)

    while True:
        try:
            dev = await connect_device(device_address)

            # Update reachable status on success
            GAUGE_DEVICE_REACHABLE.labels(device_ip=resolved_ip, device_name=dev_name).set(1)
            GAUGE_LAST_ERROR_CODE.labels(device_ip=resolved_ip, device_name=dev_name, error_type='none').set(0)

            # Build global label dict
            gl = {k: global_labels.get(k, "") for k in GLOBAL_LABEL_KEYS}

            # ─── System Metadata ──────────────────────────────────────
            try:
                dev_info = getattr(dev, 'device_info', None)
                firmware_str = str(dev_info.firmware_version) if dev_info and dev_info.firmware_version else ''
                hardware_str = str(dev_info.hardware_version) if dev_info and dev_info.hardware_version else ''
                sys_info = getattr(dev, 'sys_info', None) or {}
                mac_str = str(sys_info.get('mac', '')) or ''

                GAUGE_INFO.labels(
                    device_ip=resolved_ip, device_name=dev_name,
                    model=dev.alias, firmware=firmware_str,
                    hardware=hardware_str, mac=mac_str
                ).set(1)
            except Exception as e:
                logging.warning(f"Could not set metadata for {device_address}: {e}")

            # ─── Device-level gauges ──────────────────────────────────
            try:
                state_info = getattr(dev, 'state_information', None) or {}
                rssi_val = state_info.get('RSSI')
                if rssi_val is not None:
                    GAUGE_RSSI.labels(device_ip=resolved_ip, device_name=dev_name, **gl).set(rssi_val)

                on_since = state_info.get('On since')
                try:
                    if on_since is not None:
                        now = datetime.now(timezone.utc) if on_since.tzinfo else datetime.now()
                        uptime_seconds = (now - on_since).total_seconds()
                        if uptime_seconds > 0:
                            GAUGE_UPTIME.labels(device_ip=resolved_ip, device_name=dev_name, **gl).set(uptime_seconds)
                except Exception:
                    pass

            except Exception as e:
                logging.warning(f"Could not read rssi/uptime for {device_address}: {e}")

            # ─── Aggregate (root device) metrics ──────────────────────
            try:
                energy = dev.modules.get("Energy")
                if energy:
                    val = energy.current_consumption
                    if val is not None:
                        GAUGE_DEV_POWER.labels(device_ip=resolved_ip, device_name=dev_name, **gl).set(val)
                    val = energy.voltage
                    if val is not None:
                        GAUGE_DEV_VOLTAGE.labels(device_ip=resolved_ip, device_name=dev_name, **gl).set(val)
                    val = energy.current
                    if val is not None:
                        GAUGE_DEV_CURRENT.labels(device_ip=resolved_ip, device_name=dev_name, **gl).set(val)
                    val = energy.consumption_total
                    if val is not None:
                        GAUGE_DEV_ENERGY.labels(device_ip=resolved_ip, device_name=dev_name, **gl).set(val)
                    val = energy.consumption_today
                    if val is not None:
                        GAUGE_DEV_ENERGY_TODAY.labels(device_ip=resolved_ip, device_name=dev_name, **gl).set(val)
                    val = energy.consumption_this_month
                    if val is not None:
                        GAUGE_DEV_ENERGY_MONTH.labels(device_ip=resolved_ip, device_name=dev_name, **gl).set(val)
            except Exception as e:
                logging.warning(f"Could not read aggregate emeter for {device_address}: {e}")

            # ─── Per-outlet metrics (child plugs) ─────────────────────
            children = getattr(dev, 'children', [])
            if children:
                for idx_val, child in enumerate(children):
                    o_cfg = outlet_cfg.get(idx_val) or outlet_cfg.get(str(idx_val), {})
                    o_name = o_cfg.get("name", f"Outlet_{idx_val}")
                    custom = {k: o_cfg.get("labels", {}).get(k, global_labels.get(k, "")) for k in OUTLET_CUSTOM_LABEL_KEYS}
                    out_labels = dict(device_ip=resolved_ip, device_name=dev_name, outlet_index=idx_val, outlet_name=o_name, **custom)

                    # On/Off state
                    try:
                        GAUGE_STATE.labels(**out_labels).set(1 if child.is_on else 0)
                    except Exception as e:
                        logging.warning(f"Could not read state for child {idx_val} on {device_address}: {e}")

                    # Child real-time emeter (also captures cumulative energy)
                    try:
                        c_energy = child.modules.get("Energy")
                        if c_energy:
                            val = c_energy.current_consumption
                            if val is not None:
                                GAUGE_POWER.labels(**out_labels).set(val)
                            val = c_energy.voltage
                            if val is not None:
                                GAUGE_VOLTAGE.labels(**out_labels).set(val)
                            val = c_energy.current
                            if val is not None:
                                GAUGE_CURRENT.labels(**out_labels).set(val)
                            val = c_energy.consumption_total
                            if val is not None:
                                GAUGE_ENERGY.labels(**out_labels).set(val)
                            val = c_energy.consumption_today
                            if val is not None:
                                GAUGE_ENERGY_TODAY.labels(**out_labels).set(val)
                            val = c_energy.consumption_this_month
                            if val is not None:
                                GAUGE_ENERGY_MONTH.labels(**out_labels).set(val)
                    except Exception as e:
                        logging.warning(f"Could not read emeter for child {idx_val} on {device_address}: {e}")

        except Exception as e:
            # ─── Edge case: any connection/poll error ────────────────
            err_type, err_code = classify_error(e)
            GAUGE_DEVICE_REACHABLE.labels(device_ip=resolved_ip, device_name=dev_name).set(0)
            GAUGE_LAST_ERROR_CODE.labels(device_ip=resolved_ip, device_name=dev_name, error_type=err_type).set(err_code)
            logging.error(f"Error polling {device_address}: [{err_type}] {e}")

        await asyncio.sleep(poll_interval)


async def main():
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format='%(asctime)s %(levelname)s %(message)s')

    srv = config.get("server", {})
    port = int(srv.get("port", 9233))
    poll_interval = int(srv.get("poll_interval", 10))

    start_http_server(port)
    logging.info(f"Prometheus metrics listening on port {port}")

    tasks = []
    for d in config.get("devices", []):
        t = asyncio.create_task(poll_device(d, poll_interval))
        tasks.append(t)

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logging.info("Shutting down.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down.")
