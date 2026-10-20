import json
import shlex
import sqlite3

from progress import ProgressEvent, ProgressStore
from app_logging import log_event
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from device import (
    Device,
    Network,
    DeviceInfo,
    InventoryConflictError,
    StaleInventoryError,
)


cumulus_target_release = "5.16.0"
cumulus_password = os.environ.get("CUMULUS_PASSWORD", "cumulus")
if any(character in cumulus_password for character in ("\n", "\r", "\0")):
    raise ValueError("CUMULUS_PASSWORD must not contain line breaks or NUL")
ztp_server_hostname = os.environ.get("ZTP_SERVER_ADDRESS", "192.168.200.1")
nginx_port = os.environ.get("NGINX_PORT", "80")
device_config_file = os.environ.get(
    "DEVICE_CONFIG_FILE", "/deployment/ztp/devices.json"
)
dhcp_hosts_file = os.environ.get(
    "DHCP_HOSTS_FILE", "/deployment/dnsmasq/dhcp-hosts.conf"
)
static_dir = Path(__file__).with_name("static")
switch_config_dir = Path("/deployment/web/configs")

device_info = DeviceInfo(
    config_file=device_config_file,
    dhcp_hosts_file=dhcp_hosts_file,
    dnsmasq_config_file=os.environ.get("DNSMASQ_CONFIG_FILE", "/deployment/dnsmasq/dnsmasq.conf"),
    ztp_url=f"http://{ztp_server_hostname}:{nginx_port}/cumulus-ztp",
)

progress_store = ProgressStore(os.environ.get("PROGRESS_DB_PATH", "/var/lib/ztp/progress.sqlite3"))

@asynccontextmanager
async def lifespan(application: FastAPI):
    device_info.load_from_json()
    device_info.reconcile_dhcp()
    for device in device_info.list_devices():
        config_path = switch_config_dir / f"{device.hostname}.yaml"
        try:
            exists = config_path.is_file()
        except OSError as error:
            log_event(logging.WARNING, "switch_config_check_failed", device=device.hostname,
                      path=config_path, reason=str(error))
            continue
        if not exists:
            log_event(logging.WARNING, "switch_config_missing", device=device.hostname,
                      path=config_path)
    yield


app = FastAPI(title="Cumulus ZTP Server", lifespan=lifespan)

script_renderer = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().with_name("scripts")),
    autoescape=False,
)
script_renderer.filters["shellquote"] = shlex.quote

def progress_unavailable(error, *, device=None, attempt=None, operation="query"):
    log_event(logging.ERROR, "progress_storage_unavailable", device=device,
              attempt=attempt, operation=operation, reason=str(error))
    return HTTPException(status_code=503, detail="Progress storage unavailable")


@app.post("/api/v1/provisioning/events", status_code=201)
def report_progress(event: ProgressEvent):
    by_mac = device_info.find(mgmt_mac=event.mgmt_mac) if event.mgmt_mac else None
    by_serial = device_info.find(serial=event.serial) if event.serial else None
    context = {"attempt": event.attempt_id, "mgmt_mac": event.mgmt_mac,
               "serial": event.serial, "stage": event.stage, "result": event.result}
    if by_mac and by_serial and by_mac.hostname != by_serial.hostname:
        log_event(logging.WARNING, "progress_rejected", device=by_mac.hostname,
                  serial_device=by_serial.hostname, reason="identity_conflict", **context)
        raise HTTPException(status_code=409, detail="MAC and serial identify different devices")
    device = by_mac or by_serial
    if not device:
        log_event(logging.WARNING, "progress_rejected", reason="device_unmatched", **context)
        raise HTTPException(status_code=404, detail="Device not found")
    try:
        progress_store.report(device.hostname, event)
    except KeyError as error:
        log_event(logging.WARNING, "progress_rejected", device=device.hostname,
                  reason="unknown_attempt", **context)
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        log_event(logging.WARNING, "progress_rejected", device=device.hostname,
                  reason="invalid_stage", **context)
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (sqlite3.Error, OSError) as error:
        raise progress_unavailable(error, device=device.hostname,
                                   attempt=event.attempt_id, operation="record") from error
    log_event(logging.ERROR if event.result == "failed" else logging.INFO,
              "progress", device=device.hostname, attempt=event.attempt_id,
              stage=event.stage, result=event.result, exit_code=event.exit_code)
    return {"recorded": True}


@app.get("/api/v1/provisioning")
def list_progress(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
    try:
        return {"devices": progress_store.latest(limit=limit, offset=offset)}
    except (sqlite3.Error, OSError) as error:
        raise progress_unavailable(error) from error


@app.get("/api/v1/provisioning/{hostname}")
def get_progress(hostname: str):
    try:
        rows = progress_store.latest(hostname)
    except (sqlite3.Error, OSError) as error:
        raise progress_unavailable(error, device=hostname) from error
    if not rows:
        raise HTTPException(status_code=404, detail="No progress recorded for device")
    return rows[0]


@app.get("/api/v1/provisioning/{hostname}/events")
def get_progress_events(hostname: str, limit: int = Query(100, ge=1, le=1000),
                        offset: int = Query(0, ge=0)):
    try:
        return {"events": progress_store.history(hostname, limit, offset)}
    except (sqlite3.Error, OSError) as error:
        raise progress_unavailable(error, device=hostname) from error


def request_revision(request: Request) -> str:
    value = request.headers.get("If-Match")
    if not value:
        raise HTTPException(
            status_code=428,
            detail="If-Match is required; refresh the inventory and retry",
        )
    if value.startswith("W/"):
        value = value[2:]
    return value.strip('"')


def inventory_response(payload: dict, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    response.headers["ETag"] = f'"{device_info.revision}"'
    return response


def mutation_error(error: Exception) -> HTTPException:
    if isinstance(error, StaleInventoryError):
        return HTTPException(status_code=412, detail=str(error))
    if isinstance(error, InventoryConflictError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail="Device not found")
    raise error


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


@app.get("/api/v1/devices")
async def list_devices():
    devices = device_info.list_devices(refresh=True)
    device_info.reconcile_dhcp()
    return inventory_response(
        {
            "devices": [device.model_dump() for device in devices],
            "revision": device_info.revision,
            "networks": {name: network.model_dump() for name, network in device_info.networks.items()},
            "settings": device_info.settings.model_dump(exclude_unset=True),
        }
    )


@app.put("/api/v1/networks")
async def replace_networks(networks: dict[str, Network], request: Request):
    try:
        device_info.replace_networks(networks, request_revision(request))
    except (InventoryConflictError, StaleInventoryError) as error:
        raise mutation_error(error) from error
    return inventory_response({"revision": device_info.revision})


@app.post("/api/v1/devices")
async def create_device(device: Device, request: Request):
    try:
        created = device_info.create(device, request_revision(request))
    except (InventoryConflictError, StaleInventoryError) as error:
        raise mutation_error(error) from error
    return inventory_response(
        {"device": created.model_dump(), "revision": device_info.revision},
        status_code=201,
    )


@app.put("/api/v1/devices/{hostname}")
async def update_device(hostname: str, device: Device, request: Request):
    try:
        updated = device_info.update(
            hostname,
            device,
            request_revision(request),
        )
    except (InventoryConflictError, StaleInventoryError, KeyError) as error:
        raise mutation_error(error) from error
    return inventory_response(
        {"device": updated.model_dump(), "revision": device_info.revision}
    )


@app.delete("/api/v1/devices/{hostname}")
async def delete_device(hostname: str, request: Request):
    try:
        device_info.delete(hostname, request_revision(request))
    except (InventoryConflictError, StaleInventoryError, KeyError) as error:
        raise mutation_error(error) from error
    return inventory_response({"revision": device_info.revision})


@app.get("/cumulus-ztp")
async def cumulus_ztp(request: Request):
    identity_headers = {
        "arch": request.headers.get("CUMULUS-ARCH"),
        "base_mac": request.headers.get("CUMULUS-BASE-MAC"),
        "mgmt_mac": request.headers.get("CUMULUS-MGMT-MAC"),
        "model": request.headers.get("CUMULUS-PRODUCTNAME"),
        "serial": request.headers.get("CUMULUS-SERIAL"),
        "observed_os": request.headers.get("CUMULUS-VERSION"),
    }
    user_agent = request.headers.get("User-Agent")
    if user_agent is None or "CumulusLinux-AutoProvision" not in user_agent:
        log_event(logging.WARNING, "payload_rejected", reason="unsupported_user_agent",
                  **identity_headers)
        return Response(
            content="User agent not CumulusLinux-AutoProvision",
            status_code=501,
            media_type="text/plain",
        )

    mgmt_mac = identity_headers["mgmt_mac"]
    serial = identity_headers["serial"]
    os_version = identity_headers["observed_os"]
    device = device_info.find(mgmt_mac=mgmt_mac, serial=serial)
    if not device:
        log_event(logging.WARNING, "device_unmatched", **identity_headers)
        return Response(content="Device not found", status_code=404, media_type="text/plain")

    target_release = device.target_os or device_info.settings.target_os or cumulus_target_release

    # Tracking failures must not prevent serving the provisioning payload.
    report_prefix = ""
    attempt_id = None
    try:
        attempt_id = progress_store.payload(
            device.hostname, target_release, os_version,
            not check_os_version(os_version, target_release),
        )
        identity = {"attempt_id": attempt_id}
        if device.eth0_mac:
            identity["mgmt_mac"] = device.eth0_mac
        if device.serial:
            identity["serial"] = device.serial
        report_prefix = json.dumps(identity)[:-1]
    except (sqlite3.Error, OSError) as error:
        log_event(logging.ERROR, "progress_storage_unavailable", device=device.hostname,
                  operation="payload", reason=str(error), provisioning_continues="true")
    reporting = {"REPORT_PREFIX_SHELL": shlex.quote(report_prefix)}

    if not check_os_version(os_version, target_release):
        content = script_renderer.get_template("upgrade-os.sh.j2").render(
            IMAGE_SERVER_HOSTNAME=ztp_server_hostname,
            NGINX_PORT=nginx_port,
            CUMULUS_TARGET_RELEASE=target_release,
            **reporting,
        )
        log_event(logging.INFO, "payload_served", device=device.hostname, attempt=attempt_id,
                  type="upgrade", observed_os=os_version, target_os=target_release)
        return Response(
            content=content.encode("utf-8"),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": 'attachment; filename="cumulus-ztp.sh"',
            },
        )

    content = script_renderer.get_template("configure-switch.sh.j2").render(
        IMAGE_SERVER_HOSTNAME=ztp_server_hostname,
        NGINX_PORT=nginx_port,
        DEVICE_HOSTNAME=device.hostname,
        DEVICE_HOSTNAME_SHELL=shlex.quote(device.hostname),
        CUMULUS_PASSWORD=cumulus_password,
        AUTHORIZED_KEYS_SHELL=(
            shlex.quote("\n".join(device_info.settings.authorized_keys) + ("\n" if device_info.settings.authorized_keys else ""))
            if device_info.settings.authorized_keys is not None else None
        ),
        **reporting,
    )

    log_event(logging.INFO, "payload_served", device=device.hostname, attempt=attempt_id,
              type="configuration", observed_os=os_version, target_os=target_release)
    return Response(
        content=content.encode("utf-8"),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="cumulus-ztp.sh"',
        },
    )


def check_os_version(current: str | None, target: str) -> bool:
    return current == target


app.mount("/admin", StaticFiles(directory=static_dir, html=True), name="admin")
