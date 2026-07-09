# Reaching automaton from outside your LAN

## Quick path (< 5 minutes if Tailscale is already installed)

```bash
# 1. Start automaton with auth on
AUTOMATON_TOKEN=<your-token> automaton serve

# 2. In another terminal — expose it through Tailscale Serve (free Let's Encrypt cert)
tailscale serve https / http://localhost:8080 --bg

# 3. Find your URL and verify
automaton mesh status
# → prints something like:
#   access URL:
#     open in browser   https://your-host.your-tailnet.ts.net
```

Open that URL on your phone. It loads over HTTPS with a real cert, protected
by your `AUTOMATON_TOKEN`. Done.

The rest of this document explains the options in more depth and how to set up
Tailscale from scratch.

---

`automaton serve` listens on `127.0.0.1:8080` by default. That keeps it
safe on a multi-user box, but it also means your phone on cellular can't
hit it. There are three ways to fix that, ranked by how much your
ops-future-self will thank you:

1. **Join every device to a private mesh** (Tailscale or Headscale).
   Your phone, laptop, and the automaton host get stable `100.x.y.z` IPs
   that work over NAT, Wi-Fi changes, and cellular. ACLs gate who can
   talk to what. This is the right answer for personal infrastructure.

2. **Tailscale Serve / Funnel.** A thin layer on top of (1) that gets you
   a real Let's Encrypt cert and (optionally) a public-internet endpoint
   on `*.ts.net`. Skip the self-signed-cert install dance from
   [Phase 4](../../README.md#tls).

3. **Public exposure + reverse proxy + real cert.** Open port 443 on
   your router, terminate TLS at nginx/Caddy, restrict by IP if you can.
   This works but it's strictly more attack surface than (1) and (2).
   Don't reach for it unless you have a specific reason.

This guide walks through path 1 (with the Serve option from path 2). See
the very end for the public-exposure path if you really want it.

## Pick: Tailscale (managed) vs Headscale (self-hosted)

| | Tailscale | Headscale |
|---|---|---|
| Control plane | Tailscale's servers | Your own VM |
| Free for personal use? | Yes, up to 100 devices | Yes, MIT licensed |
| Third party in the auth path? | Yes (Tailscale + your IdP) | No - you run the coordinator |
| Setup time | ~5 minutes | ~30 minutes |
| Maintenance | None | You patch and back up the control plane |
| Wire protocol | WireGuard | WireGuard (same as Tailscale) |

The wire protocol is identical, so the decision is reversible at any
time: you can run a few hosts on Tailscale, switch to Headscale later,
and the client install on each host stays the same package.

If "no third-party in the loop" matters to you, pick Headscale. Otherwise
pick Tailscale - it's strictly less work.

## Path A: Tailscale (the easy one)

### 1. Install on the automaton host

```bash
# Linux (Debian/Ubuntu)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# macOS
brew install --cask tailscale
open -a Tailscale  # then sign in via the menu bar

# Windows
# Download from https://tailscale.com/download/windows; install; sign in.
```

After `tailscale up`, the box has a stable Tailscale IP. Confirm:

```bash
tailscale ip -4
# -> 100.64.x.y

tailscale status
# automaton-host   100.64.x.y   you@   ...
```

### 2. Install on every client device

The phones (iOS/Android) and laptops you want to reach the UI from.
There's a free Tailscale app on each store; sign in with the same
identity provider. Each one gets a `100.x.y.z` IP too.

### 3. Bind `automaton serve` to all interfaces

```bash
automaton serve \
    --host 0.0.0.0 \
    --port 8443 \
    --tls-cert ./tls/cert.pem \
    --tls-key ./tls/key.pem
```

`--host 0.0.0.0` lets the process accept connections from the Tailscale
interface (not just `127.0.0.1`). Tailscale ACLs (next step) decide who
can actually reach it; without ACLs, *every* device in your tailnet can
hit `100.x.y.z:8443`, which is fine for a one-person setup.

### 4. Restrict with ACLs

Edit your tailnet's ACL in the Tailscale admin UI:

```
https://login.tailscale.com/admin/acls
```

A minimal "only my devices can hit automaton on 8443" policy is in
[`tailscale-acl.json`](./tailscale-acl.json). It uses tags to mark hosts
that run automaton (`tag:automaton`) versus clients that should be
allowed to hit them (`tag:owner`), and denies everything else.

### 5. (Optional) MagicDNS + Tailscale Serve for a real cert

Tailscale's MagicDNS gives you stable hostnames like
`automaton.your-tailnet.ts.net`. Tailscale Serve auto-provisions a
Let's Encrypt cert for them.

```bash
# On the automaton host, with the engine running on localhost:8443 already:
sudo tailscale serve --bg --https=443 https://localhost:8443
```

Now hit `https://automaton.your-tailnet.ts.net` from any tailnet device.
Real cert, no manual trust install on phones. Skips the bulk of Phase 4
in this README.

If you go this route, you can drop `--tls-cert/--tls-key` from
`automaton serve` and bind to `127.0.0.1:8443` - Tailscale Serve handles
TLS termination upstream.

### 6. Verify from a phone over cellular

Turn Wi-Fi off, leave Tailscale on, open `https://automaton.your-tailnet.ts.net`
(or `https://100.x.y.z:8443`) in the phone's browser. You should see the
runs dashboard. If it works on cellular it'll work on anything.

`automaton mesh status` on the host prints the Tailscale IP plus a
reachability check against the local serve port - useful when you're
debugging.

## Path B: Headscale (self-hosted control plane)

You're running the coordination server. Devices still use the standard
Tailscale client; they just point at your Headscale server instead of
`controlplane.tailscale.com`.

### 1. Run Headscale somewhere reachable

Cheapest: a $5/mo VPS with a public IP. Headscale itself is tiny.

```bash
# On the VPS
sudo apt install headscale
sudo headscale users create my-stuff
```

A starter config that fits one person on one tailnet is in
[`headscale-config.yaml`](./headscale-config.yaml). The important knobs:

- `server_url`: the public URL your devices will register with
  (`https://headscale.example.com:443`). Needs a real cert.
- `listen_addr`: where Headscale's HTTP API binds (typically `0.0.0.0:8080`).
- `ip_prefixes`: the `100.64.0.0/10` block Tailscale uses by convention.

### 2. Issue a preauth key for each device

```bash
sudo headscale --user my-stuff preauthkeys create --reusable --expiration 24h
# -> nodekey:abc123...
```

### 3. Register each device

```bash
# On the automaton host, the laptop, the phone:
sudo tailscale up --login-server https://headscale.example.com --authkey nodekey:abc123...
```

After that, all the Tailscale commands work identically. ACLs, MagicDNS,
exit nodes, subnet routers - same controls, just managed via the
`headscale` CLI instead of Tailscale's admin UI.

### 4. Maintenance

You're on the hook for:

- Patching Headscale (`apt upgrade` or pull the latest container).
- Backing up `/var/lib/headscale/db.sqlite` - that's your tailnet.
- Renewing the TLS cert on the control plane (Let's Encrypt + certbot).
- Updating the ACL file when devices come and go.

For a one-person tailnet, total time is maybe an hour a quarter.

## Path C: Just expose it on the public internet

Don't.

Seriously, the mesh approach is cheaper to operate, harder to attack,
and faster to set up. The one scenario where public exposure makes sense
is hosting automaton on a VPS *and* terminating TLS at a battle-tested
reverse proxy (nginx, Caddy) *and* fronting it with a WAF.

If you must:

1. Get a real cert (Let's Encrypt + certbot).
2. Terminate TLS at the proxy, not in automaton itself.
3. Bind `automaton serve` to `127.0.0.1:8443` so only the proxy can hit it.
4. Set `AUTOMATON_TOKEN` to something long and random.
5. Use HTTP Basic Auth or oauth2-proxy in front of the UI - bearer-token
   auth on POST is the only protection on writes; everything GET is open
   by default.
6. Watch your logs.

## Common gotchas

**"My phone connects on cellular but not on Wi-Fi at home."** Your home
router's NAT may not allow hairpinning. Your phone is trying to reach
your tailnet IP via your router's external interface. Solution: install
Tailscale on the router itself, or rely on Tailscale's DERP relays
(which kicks in automatically and adds ~20ms latency).

**"Tailscale shows direct connection at first, then falls back to DERP."**
This is normal. NAT traversal needs ongoing STUN; if either device's
network drops a needed UDP port, the connection moves to DERP. Latency
goes up, the connection still works.

**"The UI loads but pages take forever."** Probably DERP latency from
the previous bullet. Check `tailscale ping <peer>` - if both directions
say `via DERP`, that's the cause. Often fixed by opening UDP 41641 on the
home router.

**"I rotated my key and now the host shows as offline."** `tailscale
logout && tailscale up` on the host. The IP usually stays the same.

**"Can I run automaton on a Tailscale subnet router and reach other LAN
hosts through it?"** Yes - `tailscale up --advertise-routes=192.168.1.0/24`
on the automaton host, accept the routes in the admin UI. Now the
workflows can reach LAN-only services without those services needing
Tailscale themselves.
