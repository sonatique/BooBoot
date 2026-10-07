# Remote access

How to use a BooBoot board from another network than its own: from home
through a company VPN, from another site, or through SSH only.

## Names and addresses

`booboot.local` is a multicast DNS name: it works only on the local network
of the board. Through a VPN, or from another network, it is not found. Use
instead, in this order:

1. **The name without `.local`**, like `http://booboot:8080`. Many company
   networks register the names of their computers in their DNS, so this name
   works at the office and through the VPN. Try `ping booboot`.
2. **The address of the board**, like `http://10.1.2.3:8080`. The board
   shows it: `booboot status` (line `network`), the web page and BooBoot
   Console (`also at 10.1.2.3:8080`, when they use a name). Note it at the
   office. Ask IT for a fixed address for the board (a DHCP reservation), or
   a DNS name, so that it does not change.

The clients do this by themselves. When the name of the URL is not found,
`booboot.py`, the MCP server and BooBoot Console try the name without
`.local`, then the addresses that the board reported last time they reached
it, and go on with the first one that answers. They say which one they use.
So the default `http://booboot.local:8080` keeps working away from the
office, once used there.

Use the same address everywhere: the web page, BooBoot Console (address
box), the command line (`--url` or `BOOBOOT_URL`), and agents ("the unit at
10.1.2.3").

## When nothing answers

Check that the VPN reaches the board and lets its port through: 8080, for
all the DUTs of the board, and the own port of a DUT that has one.

```sh
curl http://10.1.2.3:8080/api/v1/status
```

On Windows: `Test-NetConnection 10.1.2.3 -Port 8080`.

**SSH tunnel.** If only SSH gets through, forward the port and use
`http://localhost:8080`:

```sh
ssh -L 8080:localhost:8080 USER@10.1.2.3
```

All the DUTs of the board come through it. Through a jump host:
`ssh -J USER@JUMPHOST -L 8080:localhost:8080 USER@10.1.2.3`.

**A VPN on the board itself.** For access from anywhere without the company
VPN, the board can join a VPN of its own (WireGuard, or a mesh VPN service).
The server listens on all its network interfaces (`host = 0.0.0.0` in
`server.ini`), so it answers on the VPN address too, and shows it in its
status.

## Over a slow link

- `expect`, `run`, `deploy --expect` and the boot times wait on the board:
  their timing does not depend on the network.
- The viewers keep their connection open, with a message every 10 s, so that
  VPNs do not close it for being idle. When it closes anyway, they connect
  again and lose no output.
- Typing in a viewer has the delay of the network. Image writes take its
  bandwidth: compressed images (gz, xz) cross it faster.

## Agents

An agent reaches the board only from a computer on its network or VPN, like
an agent that runs on your computer. Agents that run in the cloud do not
reach a company network.

## Security

BooBoot has no password: whoever reaches its port controls the DUT. Use it
through a VPN or an SSH tunnel only. Never forward its port from the
internet.
