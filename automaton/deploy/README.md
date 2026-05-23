# Deploying automaton

These files match the layout LIVE-TEST-READINESS.md prescribes.

## One-time setup on the test machine

```bash
# 1. Make the automaton user & paths
sudo useradd -r -s /usr/sbin/nologin automaton
sudo mkdir -p /opt/automaton /var/lib/automaton /var/log/automaton /etc/automaton
sudo chown automaton:automaton /var/lib/automaton /var/log/automaton

# 2. Install into a venv
sudo -u automaton python3 -m venv /opt/automaton/venv
sudo -u automaton /opt/automaton/venv/bin/pip install -e /path/to/automaton

# 3. Copy the env file and edit
sudo cp deploy/automaton.env.example /etc/automaton/automaton.env
sudo chmod 600 /etc/automaton/automaton.env
sudo chown root:automaton /etc/automaton/automaton.env
# Then: sudo nano /etc/automaton/automaton.env  - paste a real token

# 4. Install and enable the units
sudo cp deploy/systemd/automaton-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now automaton-worker automaton-scheduler automaton-ui

# 5. Confirm
sudo systemctl status automaton-worker automaton-scheduler automaton-ui
curl http://127.0.0.1:8080/healthz
```

## Multiple workers

The worker unit is a template (`automaton-worker@.service`). To run three:

```bash
sudo systemctl enable --now automaton-worker@1 automaton-worker@2 automaton-worker@3
```

They cooperate on the queue automatically (SQLite WAL + lease-based claiming).

## Generating a fresh token

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste into `/etc/automaton/automaton.env` next to `AUTOMATON_TOKEN=`.
