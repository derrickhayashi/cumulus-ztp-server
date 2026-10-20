import base64
import hashlib
import ipaddress
import json
import os
import re
import stat
import tempfile
import threading
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator


MAC_ADDRESS_PATTERN = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")
HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)


class InventoryConflictError(ValueError):
    pass


class StaleInventoryError(ValueError):
    pass


class ProvisioningSettings(BaseModel):
    model_config = {"extra": "forbid"}
    target_os: str | None = Field(default=None, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    authorized_keys: list[str] | None = None

    @field_validator("authorized_keys")
    @classmethod
    def validate_public_keys(cls, keys):
        if keys is None:
            return None
        normalized = []
        for key in keys:
            if "\n" in key or "\r" in key:
                raise ValueError("each authorized_keys entry must be one SSH public key line")
            parts = key.strip().split()
            if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
                raise ValueError("authorized_keys entries must be SSH public keys, not private keys")
            try:
                decoded = base64.b64decode(parts[1], validate=True)
                length = int.from_bytes(decoded[:4], "big")
                if length == 0 or decoded[4:4 + length].decode("ascii") != parts[0] or len(decoded) <= 4 + length:
                    raise ValueError("invalid SSH key encoding")
            except (ValueError, UnicodeError) as error:
                raise ValueError("invalid SSH public key encoding") from error
            normalized.append(key.strip())
        return normalized


class Network(BaseModel):
    model_config = {"extra": "forbid"}

    subnet: str
    gateway: str

    @model_validator(mode="after")
    def validate_subnet(self):
        subnet = ipaddress.IPv4Network(self.subnet)
        gateway = ipaddress.IPv4Address(self.gateway)
        if gateway not in subnet or gateway in (subnet.network_address, subnet.broadcast_address):
            raise ValueError("gateway must be a usable address in the subnet")
        self.subnet = str(subnet)
        self.gateway = str(gateway)
        return self


class Device(BaseModel):
    hostname: str
    target_os: str | None = Field(default=None, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    network: str | None = None
    mgmt_ip: str | None = Field(default=None)
    serial: str | None = Field(default=None)
    eth0_mac: str | None = Field(default=None)

    @field_validator("hostname", mode="before")
    @classmethod
    def normalize_hostname(cls, value):
        hostname = str(value).strip()
        if not HOSTNAME_PATTERN.fullmatch(hostname):
            raise ValueError(
                "hostname must start and end with a letter or digit and contain "
                "only letters, digits, dots, underscores, or hyphens"
            )
        return hostname

    @field_validator("mgmt_ip", mode="before")
    @classmethod
    def normalize_mgmt_ip(cls, value):
        if value is None or str(value).strip() == "":
            return None
        return str(ipaddress.IPv4Interface(str(value).strip()).ip)

    @field_validator("serial", mode="before")
    @classmethod
    def normalize_serial(cls, value):
        if value is None or str(value).strip() == "":
            return None
        return str(value).strip()

    @field_validator("eth0_mac", mode="before")
    @classmethod
    def normalize_eth0_mac(cls, value):
        if value is None or str(value).strip() == "":
            return None
        mac = str(value).strip().lower()
        if not MAC_ADDRESS_PATTERN.fullmatch(mac):
            raise ValueError("eth0_mac must use six colon-separated octets")
        return mac

    @model_validator(mode="after")
    def require_identifier(self):
        if not self.eth0_mac and not self.serial:
            raise ValueError("at least one of eth0_mac or serial is required")
        return self


class DeviceInfo:
    def __init__(
        self,
        config_file="configs/devices.json",
        dhcp_hosts_file: str | None = None,
        dnsmasq_config_file: str | None = None,
        default_dhcp_range: str = "192.168.200.0,static,255.255.255.0",
        default_gateway: str = "192.168.200.1",
        ztp_url: str = "http://192.168.200.1:80/cumulus-ztp",
    ):
        self.by_hostname: dict[str, Device] = {}
        self.by_serial: dict[str, Device] = {}
        self.by_mac: dict[str, Device] = {}
        self.config_file = Path(config_file)
        self.dhcp_hosts_file = Path(dhcp_hosts_file) if dhcp_hosts_file else None
        self.dnsmasq_config_file = Path(dnsmasq_config_file) if dnsmasq_config_file else None
        self.default_dhcp_range = default_dhcp_range
        self.default_gateway = default_gateway
        self.ztp_url = ztp_url
        self.networks: dict[str, Network] = {}
        self.settings = ProvisioningSettings()
        self._structured = False
        self._revision = ""
        self._lock = threading.RLock()
        self.load_from_json()

    @staticmethod
    def _canonical_data(devices: dict[str, Device]) -> dict[str, dict]:
        return {
            hostname: devices[hostname].model_dump()
            for hostname in sorted(devices)
        }

    @classmethod
    def _revision_for(cls, devices: dict[str, Device], networks: dict[str, Network], settings: ProvisioningSettings) -> str:
        encoded = json.dumps(
            {"devices": cls._canonical_data(devices),
             "networks": {name: network.model_dump() for name, network in networks.items()},
             "settings": settings.model_dump(exclude_unset=True)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _validate_unique(devices: dict[str, Device]) -> None:
        seen: dict[str, dict[str, str]] = {
            "mgmt_ip": {},
            "serial": {},
            "eth0_mac": {},
        }
        for hostname, device in devices.items():
            for field in seen:
                value = getattr(device, field)
                if value is None:
                    continue
                previous = seen[field].get(value)
                if previous is not None:
                    raise InventoryConflictError(
                        f"{field} {value!r} is already used by {previous!r}"
                    )
                seen[field][value] = hostname

    @classmethod
    def _parse(cls, data: object) -> dict[str, Device]:
        if not isinstance(data, dict) or not data:
            raise ValueError("inventory must be a non-empty JSON object")

        devices: dict[str, Device] = {}
        for key, info in data.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("inventory keys must be non-empty hostnames")
            if not isinstance(info, dict):
                raise ValueError(f"inventory entry {key!r} must be an object")
            embedded_hostname = info.get("hostname")
            if embedded_hostname is not None and embedded_hostname != key:
                raise ValueError(
                    f"inventory key {key!r} does not match hostname "
                    f"{embedded_hostname!r}"
                )
            devices[key] = Device(
                hostname=key,
                **{
                    field: info.get(field)
                    for field in ("mgmt_ip", "serial", "eth0_mac", "network", "target_os")
                },
            )

        cls._validate_unique(devices)
        return devices

    def _install(self, devices: dict[str, Device]) -> None:
        self.by_hostname = dict(devices)
        self.by_serial = {
            device.serial: device
            for device in devices.values()
            if device.serial
        }
        self.by_mac = {
            device.eth0_mac: device
            for device in devices.values()
            if device.eth0_mac
        }
        self._revision = self._revision_for(devices, self.networks, self.settings)

    def load_from_json(self) -> None:
        with self._lock:
            with self.config_file.open(encoding="utf-8") as config:
                data = json.load(config)
            structured = (
                isinstance(data, dict)
                and ("networks" in data or "settings" in data)
                and isinstance(data.get("devices"), dict)
            )
            raw_networks = data.get("networks", {}) if structured else {}
            if not isinstance(raw_networks, dict):
                raise ValueError("networks must be a name-keyed JSON object")
            networks = {
                name: Network.model_validate(value)
                for name, value in raw_networks.items()
            }
            settings = ProvisioningSettings.model_validate(data.get("settings", {}) if structured else {})
            devices = self._parse(data["devices"] if structured else data)
            self._validate_networks(devices, networks)
            self.networks = networks
            self.settings = settings
            self._structured = structured
            self._install(devices)

    def refresh(self) -> None:
        self.load_from_json()

    @property
    def revision(self) -> str:
        with self._lock:
            return self._revision

    def list_devices(self, refresh: bool = False) -> list[Device]:
        if refresh:
            self.refresh()
        with self._lock:
            return [self.by_hostname[name] for name in sorted(self.by_hostname)]

    def find(
        self,
        mgmt_mac: str | None = None,
        serial: str | None = None,
    ) -> Device | None:
        with self._lock:
            if mgmt_mac:
                device = self.by_mac.get(mgmt_mac.lower())
                if device:
                    return device
            if serial:
                return self.by_serial.get(serial)
            return None

    @staticmethod
    def _dhcp_contents(devices: dict[str, Device]) -> bytes:
        lines = [
            "# Generated from devices.json by the ZTP management API.",
            "# Manual changes are replaced the next time the inventory is saved.",
        ]
        lines.extend(
            f"{device.eth0_mac},{device.mgmt_ip},{device.hostname}"
            for device in sorted(devices.values(), key=lambda item: item.hostname)
            if device.eth0_mac and device.mgmt_ip
        )
        return ("\n".join(lines) + "\n").encode("utf-8")

    @staticmethod
    def _atomic_replace(path: Path, contents: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.stat() if path.exists() else None
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(contents)
                temporary.flush()
                os.fsync(temporary.fileno())
            if existing:
                os.chmod(temporary_path, stat.S_IMODE(existing.st_mode))
                try:
                    os.chown(temporary_path, existing.st_uid, existing.st_gid)
                except PermissionError:
                    pass
            else:
                os.chmod(temporary_path, 0o644)
            os.replace(temporary_path, path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_networks(devices: dict[str, Device], networks: dict[str, Network]) -> None:
        subnets = []
        for name, network in networks.items():
            if not HOSTNAME_PATTERN.fullmatch(name):
                raise InventoryConflictError("network names must use hostname characters")
            subnet = ipaddress.IPv4Network(network.subnet)
            if any(subnet.overlaps(other) for other in subnets):
                raise InventoryConflictError("network subnets must not overlap")
            subnets.append(subnet)
        for device in devices.values():
            if device.network and device.network not in networks:
                raise InventoryConflictError(f"{device.hostname}: unknown network {device.network!r}")
            if not device.mgmt_ip or not networks:
                continue
            address = ipaddress.IPv4Address(device.mgmt_ip)
            candidates = [networks[device.network]] if device.network else networks.values()
            for network in candidates:
                subnet = ipaddress.IPv4Network(network.subnet)
                reserved = (subnet.network_address, subnet.broadcast_address,
                            ipaddress.IPv4Address(network.gateway))
                if address in subnet and address not in reserved:
                    break
            else:
                raise InventoryConflictError(
                    f"{device.hostname}: management IP must be a usable "
                    "non-gateway address in its network"
                )

    def _dnsmasq_contents(self, networks: dict[str, Network]) -> bytes:
        lines = [
            "# Generated by the ZTP management API. Do not edit.",
            "no-hosts", "no-resolv", "log-dhcp", "bind-interfaces", "port=0",
            f"dhcp-hostsfile={self.dhcp_hosts_file}",
            "dhcp-leasefile=/var/lib/misc/dnsmasq.leases",
        ]
        if networks:
            for index, (name, network) in enumerate(sorted(networks.items())):
                subnet = ipaddress.IPv4Network(network.subnet)
                tag = f"network{index}"
                lines.extend([
                    f"# {name}",
                    f"dhcp-range=set:{tag},{subnet.network_address},static,{subnet.netmask}",
                    f"dhcp-option=tag:{tag},option:router,{network.gateway}",
                ])
        else:
            lines.extend([
                f"dhcp-range={self.default_dhcp_range}",
                f"dhcp-option=option:router,{self.default_gateway}",
            ])
        lines.append(f"dhcp-option=239,{self.ztp_url}")
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _persist(self, devices: dict[str, Device], networks: dict[str, Network] | None = None) -> None:
        selected = self.networks if networks is None else networks
        self._validate_networks(devices, selected)
        data = self._canonical_data(devices)
        if self._structured or networks is not None:
            data = {"networks": {name: network.model_dump() for name, network in sorted(selected.items())},
                    "devices": data}
            if self.settings.model_fields_set:
                data["settings"] = self.settings.model_dump(exclude_unset=True)
        updates = [(self.config_file, (json.dumps(data, indent=2) + "\n").encode())]
        if self.dhcp_hosts_file:
            updates.append((self.dhcp_hosts_file, self._dhcp_contents(devices)))
        if self.dnsmasq_config_file:
            updates.append((self.dnsmasq_config_file, self._dnsmasq_contents(selected)))
        previous = {path: path.read_bytes() if path.exists() else None for path, _ in updates}
        written = []
        try:
            for path, contents in updates:
                if contents != previous[path]:
                    self._atomic_replace(path, contents)
                    written.append(path)
        except Exception:
            for path in reversed(written):
                if previous[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    self._atomic_replace(path, previous[path])
            raise

    def reconcile_dhcp(self) -> None:
        with self._lock:
            updates = []
            if self.dhcp_hosts_file:
                updates.append((self.dhcp_hosts_file, self._dhcp_contents(self.by_hostname)))
            if self.dnsmasq_config_file:
                updates.append((self.dnsmasq_config_file, self._dnsmasq_contents(self.networks)))
            for path, contents in updates:
                if not path.exists() or path.read_bytes() != contents:
                    self._atomic_replace(path, contents)

    def replace_networks(self, networks: dict[str, Network], expected_revision: str) -> None:
        with self._lock:
            devices = self._prepare_mutation(expected_revision)
            self._persist(devices, networks)
            self.networks = networks
            self._structured = True
            self._install(devices)

    def _prepare_mutation(self, expected_revision: str) -> dict[str, Device]:
        self.load_from_json()
        if expected_revision != self._revision:
            raise StaleInventoryError(
                "inventory changed; refresh it before saving again"
            )
        return dict(self.by_hostname)

    def create(self, device: Device, expected_revision: str) -> Device:
        with self._lock:
            devices = self._prepare_mutation(expected_revision)
            if device.hostname in devices:
                raise InventoryConflictError(
                    f"device {device.hostname!r} already exists"
                )
            devices[device.hostname] = device
            self._validate_unique(devices)
            self._persist(devices)
            self._install(devices)
            return device

    def update(
        self,
        hostname: str,
        device: Device,
        expected_revision: str,
    ) -> Device:
        with self._lock:
            devices = self._prepare_mutation(expected_revision)
            if hostname not in devices:
                raise KeyError(hostname)
            if device.hostname != hostname and device.hostname in devices:
                raise InventoryConflictError(
                    f"device {device.hostname!r} already exists"
                )
            del devices[hostname]
            devices[device.hostname] = device
            self._validate_unique(devices)
            self._persist(devices)
            self._install(devices)
            return device

    def delete(self, hostname: str, expected_revision: str) -> None:
        with self._lock:
            devices = self._prepare_mutation(expected_revision)
            if hostname not in devices:
                raise KeyError(hostname)
            if len(devices) == 1:
                raise InventoryConflictError(
                    "the final inventory device cannot be deleted"
                )
            del devices[hostname]
            self._persist(devices)
            self._install(devices)
