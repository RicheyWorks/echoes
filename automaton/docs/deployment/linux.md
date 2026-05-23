# Linux deployment (systemd)

The `deploy/systemd/` directory contains three unit files that mirror the three automaton processes.

## One-time setup

```bash
# 1. Create the automaton system user and directories
sudo useradd -r -s /usr/sbin/nologin automaton
sudo mkdir -p /opt/automaton /var/lib/automaton /var/log/automaton /etc/automaton
sudo chown automaton:automaton /var/lib/automaton /var/log/automaton

# 2. Install into a venv
sudo -u automaton python3 -m venv /opt/automaton/venv
sudo -u automaton /opt/automaton/venv/bin/pip install automaton-engine

# 3. Copy and edit the env file
sudo cp /opt/automaton/venv/lib/python3.*/site-packages/deploy/automaton.env.example \
        /etc/automaton/automaton.env
sudo chmod 600 /etc/automaton/automaton.env
sudo chown root:automaton /etc/automaton/automaton.env
# Edit: set AUTOMATON_DB, AUTOMATON_TOKEN, log paths

# 4. Apply migrations
sudo -u automaton AUTOMATON_DB=/var/lib/automaton/automaton.db \
    /opt/automaton/venv/bin/automaton migrate

# 5. Install systemd units
sudo cp deploy/systemd/automaton-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now automaton-worker automaton-scheduler automaton-ui

# 6. Verify
sudo systemctl status automaton-worker automaton-scheduler automaton-ui
curl http://127.0.0.1:8080/healthz
```

## Multiple workers

The worker unit is a systemd template. To run three workers in parallel:

```bash
sudo systemctl enable --now automaton-worker@1 automaton-worker@2 automaton-worker@3
```

Workers cooperate via the queue — no configuration needed.

## Useful commands

```bash
# Tail live logs
sudo journalctl -fu automaton-worker

# Restart after a config change
sudo systemctl restart automaton-worker automaton-scheduler automaton-ui

# Check all three
sudo systemctl status 'automaton-*'
```

## Monitoring

The UI exposes `/metrics` in Prometheus text format. Point your scraper at `http://127.0.0.1:8080/metrics` (no auth required, same as `/healthz`).

See [Metrics →](../operations/metrics.md)
