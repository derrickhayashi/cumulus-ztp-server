# Configuration examples

These files demonstrate a complete two-switch ZTP configuration without using
addresses, serial numbers, or names from a real project.

Safety properties of the sample data:

- Management addresses use `192.0.2.0/24`, which is reserved for documentation.
- MAC addresses begin with `02`, marking them as locally administered unicast
  addresses rather than vendor-assigned addresses.
- Hostnames and serial numbers are explicitly prefixed with `example` or
  `EXAMPLE`.

Do not use these values on a production or shared network.

## Files

- `devices-with-networks.json` demonstrates two subnets with separate gateways
  and global provisioning settings, with an OS override on `example-leaf02`. Add your SSH public keys to
  `settings.authorized_keys`; the sample empty array installs no keys.
- `devices.json` is a sample ZTP device inventory.
- `dhcp-hosts.conf` contains matching dnsmasq `--dhcp-hostsfile` entries without
  `dhcp-host=` prefixes or CIDR suffixes.
- `nvue/` contains one minimal per-switch NVUE configuration for each inventory
  entry.

Manage devices through `/admin/` or edit the JSON inventory directly, then
refresh the UI. The application generates DHCP reservations from the inventory;
do not copy the sample DHCP file over the generated deployment file.

Copy and rename the files you need rather than pointing a deployment directly at
`examples/`.
