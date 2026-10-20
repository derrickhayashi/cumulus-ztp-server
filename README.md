# Cumulus ZTP Server

Provision NVIDIA Cumulus Linux switches using a Docker Compose stack. The server
assigns reserved management addresses through DHCP, supplies the ZTP script,
installs the selected OS when necessary, applies each switch's NVUE configuration,
and displays reported provisioning progress in a web UI.

For a new deployment, supply **one inventory JSON file, one `.env` file, and one
NVUE YAML file per switch**. The JSON includes the target OS, SSH public keys,
management networks, and switch identities. Download an OS installer if switches
need an upgrade. DHCP configuration is generated automatically.

## What you need to provide

| Input | Location | What belongs here |
| --- | --- | --- |
| Deployment settings | `.env` | Server address, HTTP port, switch password, and optional published image tag |
| Inventory and provisioning settings | `deployment/ztp/devices.json` | Target OS, SSH public keys, subnets, gateways, hostnames, management IPs, MACs, and serials |
| Switch configurations | `deployment/web/configs/<hostname>.yaml` | The NVUE configuration to apply to each switch |
| OS installer, when needed | `deployment/web/cumulus-linux-<version>-mlx-amd64.bin` | Download with the included utility; see step 5 |

You do not need to write `dnsmasq.conf`, DHCP reservation entries, or provisioning
scripts. The app generates the DHCP files, and the script templates ship inside
the app image. SSH public keys in the JSON remove the need for a separate
`authorized_keys` file.

## Prerequisites and network layout

Use a Linux host with Docker Engine and the Docker Compose plugin. The host must
be reachable from the switch management networks, have UDP 67 available for DHCP,
and have the selected HTTP port available (TCP 80 by default). Running dnsmasq
requires host networking and the `NET_ADMIN` capability.

| Service | Network mode | How it is reached |
| --- | --- | --- |
| dnsmasq | Host network | Receives DHCP requests directly or through a DHCP relay |
| nginx | Docker bridge network | Publishes `NGINX_PORT` on the host and serves HTTP files and app routes |
| ZTP app | Docker bridge network | nginx forwards UI, API, and ZTP script requests to port 8080 internally |

`ZTP_SERVER_ADDRESS` is the address advertised to switches. It does **not** select
a host interface or restrict which interfaces listen. HTTP is currently published
on all host interfaces. The generated dnsmasq configuration does not specify an
interface allowlist. A dedicated DHCP interface setting is not implemented.

For multiple management subnets, the server needs a configured interface on each
subnet or DHCP relay connectivity from those subnets. Switches must also be able
to reach the advertised HTTP address. Adding networks to the JSON does not create
host interfaces, VLANs, routes, or relays.

This stack starts a DHCP server. Use it on the intended provisioning network and
check for an existing DHCP server before starting it. The UI and API have no
login or bearer-token requirement.

## Set up and start

Run the following commands from the repository root. The examples use
`192.168.200.1` as the server address; replace addresses, identities, and keys
with values for your deployment.

### 1. Configure `.env`

```bash
cp .env.example .env
mkdir -p deployment/ztp deployment/dnsmasq deployment/web/configs
```

Edit `.env`:

```dotenv
ZTP_SERVER_ADDRESS=192.168.200.1
NGINX_PORT=80
CUMULUS_PASSWORD=replace-me
ZTP_STACK_IMAGE_TAG=latest
```

| Setting | Meaning |
| --- | --- |
| `ZTP_SERVER_ADDRESS` | The host's switch-reachable IPv4 address, without `http://` or a port |
| `NGINX_PORT` | Host HTTP port for the UI, API, installer, and switch configurations; default `80` |
| `CUMULUS_PASSWORD` | Single-line password assigned to `cumulus`; defaults to `cumulus`, matching Cumulus Linux 4.2 and later; override for your deployment |
| `ZTP_STACK_IMAGE_TAG` | Image tag used by `compose.yaml`; unused with `deploy.sh --build` |
| `CUMULUS_IMAGE_DOWNLOADER_BASE_URL` | Installer download directory; defaults to NVIDIA's download URL shown in `.env.example` |

OS versions, DHCP subnets, and gateways are configured in the JSON, not in `.env`.
The app no longer reads `CUMULUS_TARGET_RELEASE`, `DHCP_RANGE`, or
`SWITCH_MGMT_DEFAULT_GW` environment variables. Keep `.env` local; it is ignored
by Git. Password records are shell-escaped before provisioning; apostrophes,
spaces, and shell metacharacters are preserved. Line breaks and NUL are rejected
at app startup because `chpasswd` accepts one account/password record per line.

### 2. Supply the inventory JSON

Save your inventory at `deployment/ztp/devices.json`. Use this
complete structure, with as many networks and devices as needed:

```json
{
  "settings": {
    "target_os": "5.16.0",
    "authorized_keys": []
  },
  "networks": {
    "rack-a": {
      "subnet": "192.168.200.0/24",
      "gateway": "192.168.200.1"
    },
    "rack-b": {
      "subnet": "192.168.201.0/24",
      "gateway": "192.168.201.1"
    }
  },
  "devices": {
    "leaf01": {
      "hostname": "leaf01",
      "network": "rack-a",
      "mgmt_ip": "192.168.200.11",
      "eth0_mac": "02:00:00:00:01:01",
      "serial": "EXAMPLE-LEAF01"
    },
    "leaf02": {
      "hostname": "leaf02",
      "network": "rack-b",
      "mgmt_ip": "192.168.201.11",
      "eth0_mac": "02:00:00:00:02:01",
      "serial": "EXAMPLE-LEAF02"
    }
  }
}
```

Replace the sample switch identities and addresses. Put each complete public-key
line from your `.pub` files into `settings.authorized_keys` as a separate JSON
string. An empty array, as shown above, deliberately installs an empty
SSH authorized-keys file. Do not put private keys in the inventory.

#### Global settings

- **`settings.target_os`**: Default Cumulus Linux version, such as
  `5.16.0`. A switch can override it with its own `target_os`.
- **`settings.authorized_keys`**: An array of single-line SSH public keys. The
  app validates their encoding and embeds them in the configuration script.
  Device and network edits in the UI preserve these settings.

#### Networks

Each network has a name, an IPv4 subnet in CIDR notation, and a gateway inside
that subnet. Subnets must not overlap. Each network generates a static DHCP range
and a gateway option for that range. The server can provision switches on
different subnets through the network connections described above.

#### Devices

- The key under `devices` must match the embedded `hostname` and the NVUE
  filename, including case: `leaf01` uses `configs/leaf01.yaml`.
- At least one of `eth0_mac` or `serial` is required to identify a switch.
  Management MAC matching is case-insensitive and takes precedence over serial.
- A DHCP reservation requires **both `eth0_mac` and `mgmt_ip`**. A serial-only
  entry can identify a switch to the app but cannot obtain a lease from this
  static-only DHCP server.
- Use a plain IPv4 address for `mgmt_ip`, without a CIDR suffix. It must be a
  usable host address in the selected network, distinct from the gateway.
- `target_os` optionally overrides the default for this switch, for example
  `"target_os": "5.16.6"`. Omit it or use `null` to inherit
  `settings.target_os` (or `5.16.0` when the global setting is absent).
- `network` selects a named network. If omitted, the app matches the device's
  management IP to a configured subnet.
- Hostnames, management IPs, MAC addresses, and serial numbers must be unique.
  The inventory must contain at least one device.

For additional examples, see [examples/README.md](examples/README.md) and
[devices-with-networks.json](examples/devices-with-networks.json). The
[JSON schema](deployment/ztp/devices.schema.json) documents the
file structure; the application also validates network membership and uniqueness.

### 3. Supply each switch's NVUE configuration

Create `deployment/web/configs/<hostname>.yaml` for every switch. On startup,
the app logs a `WARNING` with `event=switch_config_missing`, the hostname, and
expected path for each missing file. The warning does not block startup or mark
the service unhealthy. This checks file presence, not NVUE syntax or whether the
configuration applies successfully. New devices added after startup are checked
on the next app startup.

For `leaf01`,
a minimal example is:

```yaml
---
- set:
    system:
      hostname: leaf01
    interface:
      eth0:
        ip:
          address:
            192.168.200.11/24: {}
```

Save it as `deployment/web/configs/leaf01.yaml`. Create a corresponding file for
`leaf02`, using its hostname and `192.168.201.11/24` address. Supply the complete
NVUE configuration required for your deployment, including any necessary routes
and other switch settings; the example only shows the hostname and address.

The JSON creates DHCP reservations. It does **not** generate or rewrite NVUE
YAML files. Keep management addresses, prefixes, and routing in your YAML
consistent with the inventory. The script uses `nv config replace`, applies the
configuration, and saves it, so provide the configuration you intend to install.

### 4. Choose published images or a local build

```bash
./scripts/build-images.sh
```

For local development, this builds the app (including UI, API, and templates), nginx, and dnsmasq from
this checkout. Docker needs access to the base-image registries and package
repositories during the build.

| Compose file | Image source | When to use it |
| --- | --- | --- |
| `compose.build.yaml` | Builds the source in this repository | Use for this repository's current features |
| `compose.yaml` | Pulls GHCR images using `ZTP_STACK_IMAGE_TAG` | Default deployment; requires published images |

The default deployment pulls `ghcr.io/derrickhayashi/cumulus-ztp-server`,
`ghcr.io/derrickhayashi/cumulus-ztp-server-nginx`, and
`ghcr.io/derrickhayashi/cumulus-ztp-server-dnsmasq`. Set `ZTP_STACK_IMAGE_TAG=1.0.0`
in `.env` to select the version, or use `latest`. For private packages, first run
`docker login ghcr.io -u derrickhayashi` using a GitHub personal access token
with `read:packages` as the password. Keep the token out of this repository.

### 5. Deploy with automatic OS fetching

```bash
./scripts/deploy.sh
```

The deployment script validates Compose, pulls all images (including the
installer-fetch tool) from GHCR, then runs the tool and starts the stack,
waiting for healthy services. A failed pull or installer fetch stops deployment
before the running stack is updated. To build from this checkout instead, run
`./scripts/deploy.sh --build`; this uses `compose.build.yaml`.

The fetch tool
reads the inventory and downloads each distinct effective target OS, including
per-switch overrides, from NVIDIA or your configured mirror into:

```text
deployment/web/cumulus-linux-<version>-mlx-amd64.bin
```

Existing nonempty installers are reused. Failed or empty downloads stop deployment
before the running stack is updated; partial files are removed. Downloads use HTTPS
and HTTP error checking, but do not perform a publisher checksum verification.
Supply trusted installers in advance when deploying without internet access.
For staging and production, use the same approved installer artifacts.

Fetching runs during deployment, not app startup or switch requests. Changing a
target in JSON or the UI does not download an installer. Run the deployment script
again before provisioning switches that require the new version. This may also
recreate services if their images or configuration changed.

To fetch all required versions without starting DHCP or updating the running stack:

```bash
docker compose -f compose.yaml run --rm fetch-cumulus-os
```

To fetch a single version explicitly:

```bash
docker compose -f compose.yaml run --rm fetch-cumulus-os 5.16.6
```

To download from a mirror, set `CUMULUS_IMAGE_DOWNLOADER_BASE_URL` in `.env`, for example
`CUMULUS_IMAGE_DOWNLOADER_BASE_URL=https://images.example.com/cumulus`. The downloader appends
`/cumulus-linux-<version>-mlx-amd64.bin`. A trailing slash is optional. Leave the
variable unset or empty to use NVIDIA's default download directory.

The argument affects only the download, not the inventory target. Existing nonempty
files are reused; remove an installer first if it must be downloaded again. No
installer is needed for a switch that already runs its effective target, but the
deployment script prepares every required version without querying switches.

### 6. Check the running stack

```bash
docker compose -f compose.yaml ps
docker compose -f compose.yaml logs --tail=50
```

`config --quiet` checks the Compose configuration. The app validates the inventory
JSON at startup; invalid inventory prevents the app from becoming healthy, and
nginx and dnsmasq wait for that health check during initial Compose startup.
All three services use `restart: unless-stopped` and have health checks. nginx
checks its HTTP path through the app; dnsmasq checks its daemon PID. These checks
do not prove switch or fabric readiness. Docker restarts exited services; an
unhealthy status alone does not trigger a Docker restart.

Check HTTP access using your server address and port:

```bash
curl --fail http://192.168.200.1:80/healthz
curl --fail http://192.168.200.1:80/configs/leaf01.yaml
```

Open **`http://192.168.200.1:80/admin/`** in a browser. No token is required.
Verify that your devices and networks appear before provisioning switches.

### 7. Provision switches and monitor progress

Start ZTP on the switches according to your Cumulus Linux workflow. The normal
sequence is:

1. The switch requests DHCP. Its management MAC selects a reserved IP, and DHCP
   option 239 supplies `http://<server>:<port>/cumulus-ztp`.
2. The switch requests that script with its management MAC, serial, and OS
   version. The app identifies the device from inventory.
3. If its OS differs from the target, the script installs the matching image and
   reboots. The switch requests ZTP again to continue configuration.
4. The configuration script installs SSH keys, sets the `cumulus` password,
   downloads `<hostname>.yaml`, replaces and applies the NVUE configuration, and
   saves it. The script also configures passwordless sudo for `cumulus`.

The inventory shows switch, IP, MAC, serial, and provisioning columns. Target OS,
network, and DHCP reservation details appear beneath each hostname. Narrow
windows use stacked rows without horizontal scrolling. Click a column heading
to sort; click again to reverse it. IP addresses and numbered hostnames use
numeric ordering, and missing values stay last. Narrow windows provide a
**Sort by** selector and direction button.

The UI shows each switch's latest stage, reported result, event time, and failure
exit code. Status refreshes every five seconds while the tab is visible. Use
**View history** for events across attempts, **Load older events** to page back,
and **Refresh history** to retrieve newer events. Browser times use your local
time zone.

**No reports yet** means no events are recorded. **Completed** means
the switch script reported completion after saving configuration; it does not
independently verify switch health or fabric readiness. Missing reports do not
prove failure or success. If progress becomes unavailable, the UI displays an
error and retains its last received statuses. Reporting failures do not stop
provisioning.

## Change configuration after startup

| Change | Where to make it | How it takes effect |
| --- | --- | --- |
| Add, edit, or delete a switch | UI: **Add device**, **Edit**, **Delete** | Saves JSON and regenerates reservations; dnsmasq reloads them automatically |
| Add or edit subnets and gateways | UI: **Edit networks** | Saves JSON, generates server configuration, and triggers a validated dnsmasq restart |
| Change default OS or SSH keys | Edit `settings` in the JSON | Refresh the UI or restart the app; fetch newly required OS installers before provisioning |
| Override one switch’s target OS | UI: **Edit** → **Target OS override**, or device `target_os` in JSON | Save the override and fetch the installer before provisioning; blank inherits the default |
| Edit inventory JSON directly | The host's `deployment/ztp` directory | Refresh the inventory UI or restart the app to load and reconcile it |
| Change a switch's NVUE configuration | Edit `deployment/web/configs/<hostname>.yaml` | nginx serves it on the next download |
| Change server address, port, or password | Edit `.env` | Run `docker compose -f compose.yaml up -d` to recreate affected services |
| Change application source | This repository | Run `./scripts/deploy.sh --build` |
| Change published image version | `ZTP_STACK_IMAGE_TAG` in `.env` | Run `./scripts/deploy.sh` |

Saving inventory or changing YAML does **not** push configuration to a running
switch or initiate ZTP. Changes are used when a switch next requests provisioning.
Changing a DHCP reservation does not immediately change an address already held
by a switch. Fetch the new installer before provisioning with a new target OS.

The UI edits the host JSON directly; there is no separate inventory database.
It preserves global settings when saving devices and networks. The UI edits
devices and networks; global settings are currently edited in the JSON.
Refresh before saving after another user or process changes the file. Stale
writes are rejected rather than overwriting newer inventory.

Add all needed subnets before saving network changes. A referenced network cannot
be removed or renamed until its devices are updated. The final device cannot be
deleted. Keep separately supplied NVUE files in sync when renaming devices.

## Generated files and persistent data

```text
.
├── .env                                      # User: deployment settings
└── deployment/
    ├── ztp/
    │   ├── devices.json         # User: settings, networks, devices
    │   └── devices.schema.json  # Project: schema
    ├── dnsmasq/
    │   ├── dnsmasq.conf                      # Generated: DHCP server settings
    │   └── dhcp-hosts.conf                   # Generated: reservations
    └── web/
        ├── configs/<hostname>.yaml           # User: NVUE config per switch
        ├── cumulus-linux-<version>-mlx-amd64.bin  # Optional OS installer
        └── authorized_keys                  # Legacy alternative to JSON keys
```

The app mounts `deployment/` read/write so it can atomically replace managed
files. dnsmasq mounts it read-only; nginx mounts `deployment/web` read-only.
The host directories must be writable by the app container. Directory mounts
allow the other containers to see atomic file replacements.

Do not edit generated DHCP files: startup, an inventory refresh, or an inventory
save can replace them, including manual entries and comments. A reservation line
looks like `02:00:00:00:01:01,192.168.200.11,leaf01`.

The dnsmasq watcher detects directory changes and periodically checks contents
(approximately every five seconds). Reservation changes send `SIGHUP`. Network
configuration changes are validated with `dnsmasq --test`; accepted changes stop
dnsmasq and Docker restarts its container using `restart: unless-stopped`. This
briefly interrupts DHCP replies. Unchanged content does not cause a restart.
Invalid configuration is logged and rejected while the active process continues.
A configuration is saved as `dnsmasq-working.conf` in the persistent lease volume
only after dnsmasq completes its actual startup. If a new configuration passes
syntax validation but fails at startup, the wrapper restores the previous working
server configuration and starts it. The failed candidate is quarantined as
`dnsmasq-rejected.conf` so it does not cause a restart loop; corrected configuration
content is tried again. Saved inventory remains unchanged, so check the logs to
confirm which server configuration is active. If no working snapshot exists, or
rollback also fails, the container exits with an error.

The wrapper uses dnsmasq's native daemon startup handshake to detect startup
failure, forwards shutdown signals, and allows lease writes to finish. It monitors
the daemon and exits if the daemon dies (normally within about five seconds), so
Docker can restart the container. It does not restart services in a loop inside
the container, and no s6 dependency is required. Compose's `init: true` reaps the
daemon's child processes. Do not add `-k`, `--keep-in-foreground`, `-d`, or
`--no-daemon` to the dnsmasq command; foreground mode bypasses its startup handshake.

Successful startup still does not establish that requests can reach the server
from every management network. Verify interfaces, relays, routing, and actual
switch leases during deployment acceptance.

| Named volume | Contents |
| --- | --- |
| `dnsmasq-leases` | Issued DHCP leases and the watcher's configuration snapshots in `/var/lib/misc` |
| `ztp-progress` | Provisioning attempts and events in `/var/lib/ztp/progress.sqlite3` |

Use the same Compose project name to reuse these volumes. Ordinary container
restarts and `down` preserve them; `down --volumes` deletes them. Include the
inventory, `.env`, NVUE files, and persistent volumes in backups. Event history
has no automatic retention.

## Read application logs

```bash
docker compose -f compose.yaml logs -f ztp
```

Application events use UTC timestamps and consistent `device`, `attempt`, and
`event` fields. Full attempt IDs match the API and progress history. For example:

```text
2026-10-19T07:12:00Z INFO    device=leaf01 attempt=abc123 event=payload_served type=upgrade observed_os=5.15.0 target_os=5.16.0
2026-10-19T07:12:04Z INFO    device=leaf01 attempt=abc123 event=progress stage=os_install result=started exit_code=0
2026-10-19T07:12:08Z ERROR   device=leaf02 attempt=def456 event=progress stage=config_apply result=failed exit_code=1
```

Attempt IDs above are shortened for illustration. A served payload means the
script was delivered; completion is recorded only when the switch reports it.
Rejected reports and storage errors include the device and attempt when known.
An unmatched switch produces a warning with `event=device_unmatched` and its
Cumulus architecture, base MAC, management MAC, model, serial, and OS headers.
Missing identities appear as `-`. Values containing whitespace or control
characters are quoted and escaped so events stay on a single line. The app does
not dump arbitrary HTTP headers, passwords, public keys, or script contents.

Successful health checks and progress GET requests are suppressed from routine
app and nginx access logs. Other requests, including failed polls and POST
reports, remain visible. App logs go to stdout for Docker to collect. Rebuild the
app and nginx images to apply logging changes.

## HTTP API

The UI and direct API clients use the same app and persisted data. No bearer token
is required. For example:

```bash
curl --fail http://192.168.200.1:80/api/v1/devices
curl --fail http://192.168.200.1:80/api/v1/provisioning
curl --fail http://192.168.200.1:80/api/v1/provisioning/leaf01/events
```

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/devices` | Devices, networks, global settings, and current inventory revision |
| POST | `/api/v1/devices` | Add a device |
| PUT | `/api/v1/devices/{hostname}` | Replace a device, including rename |
| DELETE | `/api/v1/devices/{hostname}` | Delete a device and its reservation |
| PUT | `/api/v1/networks` | Replace the complete name-keyed network map |
| GET | `/api/v1/provisioning` | Latest recorded status per switch |
| GET | `/api/v1/provisioning/{hostname}` | Latest recorded attempt and status for one switch |
| GET | `/api/v1/provisioning/{hostname}/events` | Events across attempts, newest first |
| POST | `/api/v1/provisioning/events` | Record a script's event for a server-issued attempt |

Inventory GET responses include a revision in the body and `ETag`. Inventory and
network mutations require that revision in `If-Match`. Missing revisions return
428; stale revisions return 412. The UI handles these headers automatically.

Progress list and history endpoints accept `limit` (default 100, maximum 1000)
and `offset`. A switch with no recorded events is absent from the progress list;
its individual status endpoint returns 404. API timestamps are server receipt
times in UTC. Latest status includes target and observed OS versions, attempt ID,
stage, result, exit code, and timestamps. A progress storage failure returns 503
without blocking ZTP payload delivery.

Progress reports are informational device claims. They cannot edit inventory or
initiate provisioning. Scripts report fixed stage/result codes and exit codes,
without passwords or command output. An upgrade attempt continues into its first
configuration request; later attempts and late events remain separated in history.

## Compatibility and optional workflows

### Older inventories and key files

Flat hostname-keyed inventories remain readable. Saving networks through the UI
converts the file to the structured format. If no networks are defined, built-in
legacy defaults use a static range on `192.168.200.0/24` with gateway
`192.168.200.1`. If `settings.target_os` is omitted or null, the default target is `5.16.0`;
a switch’s `target_os` still takes precedence.
For new deployments, explicitly supply settings and networks in the JSON.

If `settings.authorized_keys` is omitted or null, the configuration script still
downloads `deployment/web/authorized_keys`. To use that legacy option:

```bash
cp ~/.ssh/id_ed25519.pub deployment/web/authorized_keys
```

An explicit JSON array, including an empty array, takes precedence over that file.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| App does not become healthy | Review `docker compose -f compose.yaml logs ztp` for inventory validation or directory-write errors |
| Switch receives no lease | Confirm its management MAC and IP, generated reservation, subnet/relay connectivity, UDP 67 availability, and competing DHCP servers |
| Script returns `Device not found` | Match the switch's reported management MAC or serial to the JSON |
| Configuration download returns 404 | Confirm `configs/<hostname>.yaml` exists and matches the inventory hostname exactly |
| OS installer download returns 404 | Fetch the JSON target version and check the exact installer filename |
| UI save fails | Confirm host-directory write access; refresh after manual edits or stale-revision errors |
| Network settings save but DHCP fails | Check dnsmasq validation/startup logs and host interfaces or relay setup |
| HTTP cannot be reached | Check the advertised server address, published HTTP port, routing, and firewall |
| No progress appears | Check that the switch requested a payload, can reach reporting endpoints, and is using the current built app image |
| Progress becomes unavailable | Check app logs and writable persistent storage; the UI retains last received status |

## Development

Run application tests with the project environment:

```bash
uv run python -m unittest discover -s ztp/app -v
```

Run UI component tests with Node.js (no extra packages required):

```bash
node --test ztp/tests/progress-ui.test.cjs
```

Run dnsmasq reload/restart tests in an isolated container with networking disabled:

```bash
docker build -t cumulus-ztp-dnsmasq:test dnsmasq
DNSMASQ_TEST_IMAGE=cumulus-ztp-dnsmasq:test \
  python3 -m unittest discover -s dnsmasq/tests -v
```

Tests use synthetic data and fake provisioning commands; they do not contact
switches. The Docker test verifies reloads, automatic restarts, syntax rejection,
runtime startup rollback, corrected configuration recovery, daemon crash recovery,
persistent leases, and startup fallback.

Test app/nginx crash recovery and health checks on an internal-only Docker network
with no published host ports:

```bash
docker build -t cumulus-ztp-app:recovery-test ztp
docker build -t cumulus-ztp-nginx:recovery-test nginx
ZTP_RECOVERY_APP_IMAGE=cumulus-ztp-app:recovery-test \
  ZTP_RECOVERY_NGINX_IMAGE=cumulus-ztp-nginx:recovery-test \
  python3 -m unittest discover -s tests -v
```

The test uses the restart policies and health-check commands from
`compose.build.yaml`, kills each service, and checks that HTTP access and the
persisted provisioning attempt recover.

To rebuild a single service without the build cache:

```bash
./scripts/build-images.sh --no-cache ztp
```

The release script reads the version from `pyproject.toml`. It runs application
tests and builds and tags local images by default. With `--push-images`, it
publishes `linux/amd64` and `linux/arm64` images to the GHCR repositories above,
tagged with the version and `latest`. Published images link to
`https://github.com/derrickhayashi/cumulus-ztp-server`. Authenticate to GHCR
with a token with `write:packages` before publishing. `--push` also pushes
the commit and version tag to `origin`; no GitHub Release page is created.
See the available options with:

```bash
./scripts/release.sh --help
```

For custom deployments, the app supports `DEVICE_CONFIG_FILE`, `DHCP_HOSTS_FILE`,
`DNSMASQ_CONFIG_FILE`, and `PROGRESS_DB_PATH` as container-side paths. Change the
Compose environment and mounts together; referenced paths must be visible to the
services that use them. The standard layout above needs no path overrides.

## Deployment acceptance on real switches

Automated tests do not replace a lab acceptance run. Before calling a deployment
production-ready, use explicitly selected lab switches and verify:

- Reserved DHCP addresses, subnet masks, gateways, and option 239 on every
  intended directly connected subnet and DHCP relay path.
- A switch already on the target release downloads and applies the correct NVUE
  configuration and can be reached with the configured credentials afterward.
- An OS upgrade completes, reboots, resumes ZTP, and saves the final configuration.
- Reported stages, failures, and completion agree with switch-side observations.
- A Docker host reboot restores all three services, HTTP access, leases, and
  progress history without manual intervention. Keep the Docker service enabled
  and use the same Compose project name and persistent volumes.
- A backup can restore the inventory, NVUE files, `.env`, leases, working dnsmasq
  configuration, and progress database in a separate test deployment.

Use disposable or approved lab switches: these checks can change configuration,
install an OS, and reboot devices. Real-switch, relay, and host-reboot acceptance
are separate from the isolated automated recovery tests.
