#!/usr/bin/env python3
"""Prometheus exporter for Kasa smart plugs and strips.

Polls Kasa devices for power, energy, voltage, current, and state metrics,
exposing them as Prometheus gauges for scraping.
"""
import asyncio
import ipaddress
import logging
import os
import socket
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfoNotFoundError

import yaml
from prometheus_client import start_http_server, Gauge
from kasa import Device

CONFIG_PATH = os.getenv("CONFIG_PATH", "config.yaml")
CONNECT_TIMEOUT = int(os.getenv("KASA_CONNECT_TIMEOUT", "10"))


# ─── Timezone Compatibility Fix ──────────────────────────────────────
# Kasa devices report POSIX timezone strings (e.g. PST8PDT) that Python's
# zoneinfo cannot resolve when tzdata is incomplete in Docker containers.
# This patches the CachedZoneInfo to gracefully handle missing zones.
def _patch_timezone_lookup():
    """Patch kasa's timezone handling to not crash on missing tzdata."""
    try:
        from kasa import cachedzoneinfo
        original_get = cachedzoneinfo._get_zone_info  # noqa: SLF001

        def safe_get_zone_info(time_zone_str):
            """Safe wrapper for timezone lookup, falling back to UTC."""
            try:
                return original_get(time_zone_str)
            except (ZoneInfoNotFoundError, KeyError):
                logging.debug(
                    "Timezone '%s' not found in system tzdata, defaulting "
                    "to UTC. Install tzdata or set TZ to fix.",
                    time_zone_str,
                )
                from datetime import UTC  # noqa: PLC0415
                return UTC

        cachedzoneinfo._get_zone_info = safe_get_zone_info  # noqa: SLF001
    except (ImportError, AttributeError) as exc:
        logging.warning("Could not patch timezone lookup: %s", exc)


_patch_timezone_lookup()


# ─── Edge-case Gauges: exporter health tracking ──────────────────────
GAUGE_EXPORTER_UP = Gauge(
    "kasa_exporter_up",
    "Whether the exporter is healthy and config is valid",
    [],
)
GAUGE_DEVICE_REACHABLE = Gauge(
    "kasa_device_reachable",
    "Whether the device responded to the last poll (1=yes, 0=no)",
    ["device_ip", "device_name"],
)
GAUGE_LAST_ERROR_CODE = Gauge(
    "kasa_last_error_code",
    "Numeric code of the last error encountered per device (0=none)",
    ["device_ip", "device_name", "error_type"],
)


# ─── Config Loading with Validation ─────────────────────────────────
def load_config():
    """Load and validate the YAML configuration file."""
    if not os.path.exists(CONFIG_PATH):
        logging.critical("Config file not found: %s", CONFIG_PATH)
        raise FileNotFoundError(
            f"Config not found at {CONFIG_PATH}. "
            "Mount config.yaml or set CONFIG_PATH."
        )
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            cfg = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        logging.critical("Malformed YAML in %s: %s", CONFIG_PATH, exc)
        raise ValueError("Invalid YAML") from exc
    if not cfg:
        logging.critical("Config file %s is empty.", CONFIG_PATH)
        raise ValueError(
            "Config file is empty or contains only comments."
        )
    devices = cfg.get("devices")
    if not devices:
        logging.warning("No 'devices' section found in config.")
    for dev_idx, dev in enumerate(devices):
        ip_addr = dev.get("ip")
        host = dev.get("host")
        if not ip_addr and not host:
            raise ValueError(
                f"Device #{dev_idx} missing required field: "
                "either 'ip' or 'host' must be provided."
            )
        if ip_addr:
            try:
                ipaddress.ip_address(ip_addr)
            except ValueError as val_exc:
                raise ValueError(
                    f"'{ip_addr}' is not a valid IP address "
                    f"for device #{dev_idx}."
                ) from val_exc
    return cfg


try:
    config = load_config()
except (FileNotFoundError, ValueError) as exc:
    logging.critical("Fatal config error: %s", exc)
    sys.exit(1)

# Signal healthy startup
GAUGE_EXPORTER_UP.set(1)

global_labels = config.get("global_labels", {})

# Extract custom outlet label keys from config
outlet_custom_keys = set()
for device_cfg in config.get("devices", []):
    for _idx, outlet_info in device_cfg.get("outlets", {}).items():
        if isinstance(outlet_info, dict) and "labels" in outlet_info:
            for key in outlet_info["labels"].keys():
                outlet_custom_keys.add(key)
for key in global_labels.keys():
    outlet_custom_keys.add(key)

GLOBAL_LABEL_KEYS = sorted(list(global_labels.keys()))
DEVICE_LABEL_KEYS = ["device_ip", "device_name"] + GLOBAL_LABEL_KEYS
OUTLET_CUSTOM_LABEL_KEYS = sorted(list(outlet_custom_keys))
OUTLET_LABEL_KEYS = (
    ["device_ip", "device_name", "outlet_index", "outlet_name"]
    + OUTLET_CUSTOM_LABEL_KEYS
)

# ─── Prometheus Gauges ──────────────────────────────────────────────
GAUGE_INFO = Gauge(
    "kasa_device_info",
    "Device metadata",
    ["device_ip", "device_name", "model", "firmware", "hardware", "mac"],
)
GAUGE_RSSI = Gauge(
    "kasa_device_rssi_dbm",
    "Wi-Fi signal strength in dBm",
    DEVICE_LABEL_KEYS,
)
GAUGE_UPTIME = Gauge(
    "kasa_device_uptime_seconds",
    "Device uptime in seconds",
    DEVICE_LABEL_KEYS,
)

# Root Device Aggregate (Whole Strip / Plug Total)
GAUGE_DEV_POWER = Gauge(
    "kasa_device_power_watts",
    "Total real-time power draw for entire strip in Watts",
    DEVICE_LABEL_KEYS,
)
GAUGE_DEV_VOLTAGE = Gauge(
    "kasa_device_voltage_volts",
    "Main line voltage in Volts",
    DEVICE_LABEL_KEYS,
)
GAUGE_DEV_CURRENT = Gauge(
    "kasa_device_current_amps",
    "Total current draw across whole strip in Amps",
    DEVICE_LABEL_KEYS,
)
GAUGE_DEV_ENERGY = Gauge(
    "kasa_device_total_kwh",
    "Total cumulative energy consumption in kWh",
    DEVICE_LABEL_KEYS,
)
GAUGE_DEV_ENERGY_TODAY = Gauge(
    "kasa_device_energy_today_kwh",
    "Today's energy consumption for entire device in kWh",
    DEVICE_LABEL_KEYS,
)
GAUGE_DEV_ENERGY_MONTH = Gauge(
    "kasa_device_energy_month_kwh",
    "This month's energy consumption for entire device in kWh",
    DEVICE_LABEL_KEYS,
)

# Per-Outlet Child Metrics
GAUGE_STATE = Gauge(
    "kasa_outlet_state",
    "Outlet power state (1=ON, 0=OFF)",
    OUTLET_LABEL_KEYS,
)
GAUGE_POWER = Gauge(
    "kasa_outlet_power_watts",
    "Real-time outlet power draw in Watts",
    OUTLET_LABEL_KEYS,
)
GAUGE_VOLTAGE = Gauge(
    "kasa_outlet_voltage_volts",
    "Outlet voltage in Volts",
    OUTLET_LABEL_KEYS,
)
GAUGE_CURRENT = Gauge(
    "kasa_outlet_current_amps",
    "Outlet current draw in Amperes",
    OUTLET_LABEL_KEYS,
)
GAUGE_ENERGY = Gauge(
    "kasa_outlet_total_kwh",
    "Cumulative outlet energy consumption in kWh",
    OUTLET_LABEL_KEYS,
)
GAUGE_ENERGY_TODAY = Gauge(
    "kasa_outlet_energy_today_kwh",
    "Today's energy consumption per outlet in kWh",
    OUTLET_LABEL_KEYS,
)
GAUGE_ENERGY_MONTH = Gauge(
    "kasa_outlet_energy_month_kwh",
    "This month's energy consumption per outlet in kWh",
    OUTLET_LABEL_KEYS,
)


# ─── DNS Resolution Helper ──────────────────────────────────────────
def resolve_host(host):
    """Resolve a hostname to an IP address, or return if already an IP."""
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
    if isinstance(exc, (ConnectionRefusedError,
                        ConnectionResetError, OSError, socket.gaierror)):
        return "unreachable", 1
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout", 2
    return "unknown", 99


async def connect_device(address):
    """Connect to a Kasa device and fetch fresh state."""
    try:
        dev = await Device.connect(host=address)
        await dev.update()
        return dev
    except (TimeoutError, asyncio.TimeoutError):
        logging.warning(
            "Connection timed out after %ds for %s",
            CONNECT_TIMEOUT, address,
        )
        raise


def _set_device_metadata(dev, dev_name, resolved_ip):
    """Set device info/metadata gauges."""
    dev_info = getattr(dev, "device_info", None)
    firmware_str = (
        str(dev_info.firmware_version)
        if dev_info and dev_info.firmware_version else ""
    )
    hardware_str = (
        str(dev_info.hardware_version)
        if dev_info and dev_info.hardware_version else ""
    )
    sys_info = getattr(dev, "sys_info", None) or {}
    mac_str = str(sys_info.get("mac", "")) or ""

    GAUGE_INFO.labels(
        device_ip=resolved_ip,
        device_name=dev_name,
        model=dev.alias,
        firmware=firmware_str,
        hardware=hardware_str,
        mac=mac_str,
    ).set(1)


def _set_device_rssi_uptime(dev, dev_name, resolved_ip, gl):
    """Set RSSI and uptime gauges for a device."""
    state_info = getattr(dev, "state_information", None) or {}
    rssi_val = state_info.get("RSSI")
    if rssi_val is not None:
        GAUGE_RSSI.labels(
            device_ip=resolved_ip, device_name=dev_name, **gl
        ).set(rssi_val)

    on_since = state_info.get("On since")
    if on_since is not None:
        try:
            now = (
                datetime.now(timezone.utc)
                if on_since.tzinfo else datetime.now()
            )
            uptime_seconds = (now - on_since).total_seconds()
            if uptime_seconds > 0:
                GAUGE_UPTIME.labels(
                    device_ip=resolved_ip,
                    device_name=dev_name,
                    **gl,
                ).set(uptime_seconds)
        except Exception as exc:  # noqa: BLE001
            logging.debug("Could not compute uptime: %s", exc)


def _set_aggregate_energy(dev, dev_name, resolved_ip, gl):
    """Set aggregate (root device) energy/emeter gauges."""
    energy = dev.modules.get("Energy")
    if not energy:
        return

    labels = {"device_ip": resolved_ip, "device_name": dev_name}
    labels.update(gl)

    val = energy.current_consumption
    if val is not None:
        GAUGE_DEV_POWER.labels(**labels).set(val)

    val = energy.voltage
    if val is not None:
        GAUGE_DEV_VOLTAGE.labels(**labels).set(val)

    val = energy.current
    if val is not None:
        GAUGE_DEV_CURRENT.labels(**labels).set(val)

    val = energy.consumption_total
    if val is not None:
        GAUGE_DEV_ENERGY.labels(**labels).set(val)

    val = energy.consumption_today
    if val is not None:
        GAUGE_DEV_ENERGY_TODAY.labels(**labels).set(val)

    val = energy.consumption_this_month
    if val is not None:
        GAUGE_DEV_ENERGY_MONTH.labels(**labels).set(val)


def _process_outlet(child, idx, outlet_cfg, resolved_ip, dev_name):
    """Build labels and set metrics for a single child outlet."""
    o_cfg = outlet_cfg.get(idx) or outlet_cfg.get(str(idx), {})
    o_name = o_cfg.get("name", f"Outlet_{idx}")
    custom = {
        k: o_cfg.get("labels", {}).get(k, global_labels.get(k, ""))
        for k in OUTLET_CUSTOM_LABEL_KEYS
    }
    out_labels = {
        "device_ip": resolved_ip,
        "device_name": dev_name,
        "outlet_index": idx,
        "outlet_name": o_name,
        **custom,
    }
    _set_outlet_metrics(child, idx, out_labels)


def _set_outlet_metrics(child, idx_val, out_labels):
    """Set state and emeter gauges for a single child outlet."""
    try:
        GAUGE_STATE.labels(**out_labels).set(1 if child.is_on else 0)
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "Could not read state for child %s on %s: %s",
            idx_val, out_labels["device_ip"], exc,
        )

    try:
        c_energy = child.modules.get("Energy")
        if not c_energy:
            return

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
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "Could not read emeter for child %s on %s: %s",
            idx_val, out_labels["device_ip"], exc,
        )


def _poll_device_once(dev, dev_name, resolved_ip, outlet_cfg, gl):
    """Collect all metrics from a device that is already connected."""
    # Mark reachable on success
    GAUGE_DEVICE_REACHABLE.labels(
        device_ip=resolved_ip, device_name=dev_name
    ).set(1)
    GAUGE_LAST_ERROR_CODE.labels(
        device_ip=resolved_ip,
        device_name=dev_name,
        error_type="none",
    ).set(0)

    # Metadata
    try:
        _set_device_metadata(dev, dev_name, resolved_ip)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not set metadata for %s: %s", resolved_ip, exc)

    # RSSI / Uptime
    try:
        _set_device_rssi_uptime(dev, dev_name, resolved_ip, gl)
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "Could not read rssi/uptime for %s: %s", resolved_ip, exc
        )

    # Aggregate emeter
    try:
        _set_aggregate_energy(dev, dev_name, resolved_ip, gl)
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "Could not read aggregate emeter for %s: %s",
            resolved_ip, exc,
        )

    # Per-outlet metrics (child plugs)
    for idx_val, child in enumerate(getattr(dev, "children", [])):
        _process_outlet(child, idx_val, outlet_cfg, resolved_ip, dev_name)


async def poll_device(dev_config, poll_interval):
    """Continuously poll a single Kasa device at the configured interval."""
    ip_addr = dev_config.get("ip", "")
    host = dev_config.get("host", "")
    # Use host if provided, otherwise fall back to ip
    device_address = host or ip_addr
    outlet_cfg = dev_config.get("outlets", {})
    dev_name = dev_config.get("name_override") or f"kasa_{device_address}"

    # Resolve hostname to actual IP address for all label values
    resolved_ip = resolve_host(device_address) if device_address else ""

    # ─── Startup: Attempt initial connection to validate device ────────
    logging.info(
        "Initializing device '%s' -> %s (resolved: %s)",
        dev_name, device_address, resolved_ip,
    )
    try:
        dev = await connect_device(device_address)
        model_str = getattr(dev, "alias", "Unknown") or "Unknown"
        dev_info = getattr(dev, "device_info", None)
        firmware_str = (
            str(dev_info.firmware_version)
            if dev_info and dev_info.firmware_version else "N/A"
        )
        logging.info(
            "  Device found: %s, firmware=%s, "
            "MAC=%s",
            model_str,
            firmware_str,
            getattr(dev, "sys_info", {}).get("mac", "N/A"),
        )
    except Exception as exc:  # noqa: BLE001
        err_type, _ = classify_error(exc)
        logging.critical(
            "  Cannot connect to '%s' at %s (%s): "
            "[%s] %s — this device will NOT be polled.",
            dev_name, device_address, resolved_ip, err_type, exc,
        )
        # Still initialize gauges so Prometheus sees them as unreachable
        GAUGE_DEVICE_REACHABLE.labels(
            device_ip=resolved_ip, device_name=dev_name
        ).set(0)
        GAUGE_LAST_ERROR_CODE.labels(
            device_ip=resolved_ip,
            device_name=dev_name,
            error_type=err_type,
        ).set(99)
    finally:
        GAUGE_DEVICE_REACHABLE.labels(
            device_ip=resolved_ip, device_name=dev_name
        ).set(0)
        GAUGE_LAST_ERROR_CODE.labels(
            device_ip=resolved_ip,
            device_name=dev_name,
            error_type="none",
        ).set(0)

    while True:
        try:
            dev = await connect_device(device_address)
            # Build global label dict
            gl = {k: global_labels.get(k, "") for k in GLOBAL_LABEL_KEYS}
            _poll_device_once(dev, dev_name, resolved_ip, outlet_cfg, gl)
        except Exception as exc:  # noqa: BLE001
            err_type, err_code = classify_error(exc)
            GAUGE_DEVICE_REACHABLE.labels(
                device_ip=resolved_ip, device_name=dev_name
            ).set(0)
            GAUGE_LAST_ERROR_CODE.labels(
                device_ip=resolved_ip,
                device_name=dev_name,
                error_type=err_type,
            ).set(err_code)
            logging.error(
                "Error polling %s: [%s] %s",
                device_address, err_type, exc,
            )

        await asyncio.sleep(poll_interval)


async def main():
    """Entry point: start the Prometheus server and device pollers."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    srv = config.get("server", {})
    port = int(srv.get("port", 9233))
    poll_interval = int(srv.get("poll_interval", 10))

    start_http_server(port)
    logging.info("Prometheus metrics listening on port %d", port)

    tasks = []
    for device in config.get("devices", []):
        t = asyncio.create_task(poll_device(device, poll_interval))
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
