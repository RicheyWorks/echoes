# TLS setup

By default `automaton serve` binds to `127.0.0.1:8080` over plain HTTP. That's safe for local access. To reach it from another device you need TLS.

## Generate a self-signed certificate

```bash
automaton tls init --hostname automaton.local
# Or for a Tailscale hostname:
automaton tls init --hostname automaton.your-tailnet.ts.net
```

This writes `tls/cert.pem` and `tls/key.pem` into your current directory (or `$AUTOMATON_HOME/tls/`).

## Start the server with TLS

```bash
automaton serve \
    --host 0.0.0.0 \
    --port 8443 \
    --tls-cert tls/cert.pem \
    --tls-key  tls/key.pem
```

Or via environment variables:

```bash
export AUTOMATON_TLS_CERT=tls/cert.pem
export AUTOMATON_TLS_KEY=tls/key.pem
automaton serve --host 0.0.0.0 --port 8443
```

## Trust the certificate on each device

=== "macOS"
    ```bash
    sudo security add-trusted-cert -d -r trustRoot \
        -k /Library/Keychains/System.keychain tls/cert.pem
    ```

=== "iOS"
    1. AirDrop `cert.pem` to your iPhone.
    2. Settings → General → VPN & Device Management → install the profile.
    3. Settings → General → About → Certificate Trust Settings → enable full trust.

=== "Android"
    1. Copy `cert.pem` to your phone.
    2. Settings → Security → Install a certificate → CA certificate.

=== "Windows"
    ```powershell
    Import-Certificate -FilePath tls\cert.pem `
        -CertStoreLocation Cert:\LocalMachine\Root
    ```

## Let's Encrypt (public internet)

If you expose automaton on a public domain, use `certbot` to provision a real cert:

```bash
certbot certonly --standalone -d automaton.example.com
automaton serve \
    --host 0.0.0.0 \
    --tls-cert /etc/letsencrypt/live/automaton.example.com/fullchain.pem \
    --tls-key  /etc/letsencrypt/live/automaton.example.com/privkey.pem
```

Add a systemd timer or cron job for `certbot renew`, then restart the UI.

## Tailscale Serve (recommended for Tailscale users)

Tailscale Serve auto-provisions a real Let's Encrypt cert and proxies HTTPS traffic to your local process — no cert management needed:

```bash
tailscale serve --bg 8080
```

Your server is then reachable at `https://<machine>.your-tailnet.ts.net` with a trusted cert on all your Tailscale devices.
