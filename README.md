# ac-picam-control

Minimal control agent for Pi camera streaming, intended to run **only on Tailscale**.  
It exposes a tiny HTTP API to start/stop/restart a systemd service.

## Endpoints

- `GET /health` → basic health check
- `GET /status` → `systemctl status` output
- `POST /start` → `systemctl start <service>`
- `POST /stop` → `systemctl stop <service>`
- `POST /restart` → `systemctl restart <service>`
- `POST /stream/start` → start a stream with provided `ffmpeg_url` + `stream_key`
- `POST /stream/stop` → stop the active stream

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
sudo systemctl start picam-control
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

## Central control server (optional)

This repo also ships a tiny central server (`server.py`) that aggregates device status and provides a simple web UI with start/stop/restart buttons. It can also create YouTube streams/broadcasts centrally and push stream credentials to devices. When YouTube credentials are configured, the UI will attempt to look up existing broadcasts by title to show thumbnail previews.

### Configure
Set environment variables (example):
```
PICAM_SERVER_HOST=127.0.0.1
PICAM_SERVER_PORT=8081
PICAM_AGENT_PORT=8080
PICAM_DEVICES=cam-a.tailnet.example.ts.net,cam-b.tailnet.example.ts.net

# YouTube credentials (server-only)
YT_CLIENT_ID=...
YT_CLIENT_SECRET=...
YT_REFRESH_TOKEN=...
YT_APP_NAME=ac-picam-control
YT_PRIVACY=private
```

### Run manually
```
python3 /home/ac/ac-picam-control/server.py
```

### systemd unit
Install the provided unit:
```
sudo cp /home/ac/ac-picam-control/picam-control-server.service /etc/systemd/system/picam-control-server.service
sudo systemctl daemon-reload
sudo systemctl start picam-control-server
```

Then visit:
```
http://<tailscale-ip>:8081/
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
