# ac-picam-control

Minimal control agent for Pi camera streaming, intended to run **only on Tailscale**.  
It exposes a tiny HTTP API to start/stop/restart a systemd service.

## Endpoints

- `GET /health` → basic health check
- `GET /status` → `systemctl status` output
- `POST /start` → `systemctl start <service>`
- `POST /stop` → `systemctl stop <service>`
- `POST /restart` → `systemctl restart <service>`

## Requirements

- Python 3 (stdlib only)
- systemd on the device
- Tailscale installed and connected

## Install (device)

1) Copy the repo to the device (example path):
```
/home/ac/ac-picam-control
```

2) Update the service file if needed:
- `User`
- `WorkingDirectory`
- `ExecStart`
- `PICAM_SERVICE_NAME`

3) Install the systemd unit:
```
sudo cp /home/ac/ac-picam-control/picam-control.service /etc/systemd/system/picam-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now picam-control
```

4) Check status:
```
systemctl status picam-control
```

## Using it over Tailscale

From a tailnet machine:
```
curl http://<tailscale-ip>:8080/health
curl http://<tailscale-ip>:8080/status
curl -X POST http://<tailscale-ip>:8080/start
curl -X POST http://<tailscale-ip>:8080/stop
```

## Tailscale ACL guidance (recommended)

Restrict access to the agent by ACLs, so only admins can reach the port.

Example ACL snippet (adjust groups/tags to match your tailnet):

```/dev/null/acl.json#L1-15
{
  "acls": [
    {
      "action": "accept",
      "src": ["group:ops-admins"],
      "dst": ["tag:picam:8080"]
    }
  ]
}
```

Then tag each device `tag:picam` and ensure only ops admins can connect.

## Notes

- This agent does **no auth** beyond Tailscale ACLs.
- Bind to a Tailscale IP if you want to avoid LAN exposure:
  set `PICAM_AGENT_HOST` to the device’s Tailscale IP.