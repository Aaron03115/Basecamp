# Basecamp

Trip planning and summit log for backcountry hikers, backpackers, and mountaineers.
Plan a trip around an objective, attach a gear loadout, and log completed summits/trips.

**Stack:** Python 3, Flask, SQLite, Jinja2, Bootstrap 5, gunicorn + nginx on AWS EC2 (Ubuntu).

## Run it locally

1. Install these first (Ubuntu/Debian):
   ```bash
   sudo apt install python3 python3-venv
   ```
2. Clone and start:
   ```bash
   git clone https://github.com/Aaron03115/Basecamp.git
   cd Basecamp
   ./start.sh
   ```
3. Open http://localhost:5000.

`start.sh` checks that Python is installed, creates a virtual environment, installs
`requirements.txt`, creates the SQLite database with sample objectives, and starts the app.
It is safe to run again. Use `PORT=8080 ./start.sh` to pick another port.

## Deploy to AWS EC2

You need the server's public IP, your `.pem` key (never commit it), and the login user

1. Copy the deploy folder to a fresh server and run the one-time setup:
   ```bash
   scp -i ~/keys/mykey.pem -r deploy ubuntu@<PUBLIC-IP>:~
   ssh -i ~/keys/mykey.pem ubuntu@<PUBLIC-IP>
   sudo bash deploy/setup-ec2.sh
   ```
2. From your laptop, deploy the latest code (needs `rsync`):
   ```bash
   ./deploy/deploy.sh -h <PUBLIC-IP> -i ~/keys/mykey.pem
   ```
3. Open `http://<PUBLIC-IP>/`. The service is enabled in systemd, so it also comes back
   after `sudo reboot`.

Make sure the instance's security group allows inbound HTTP (port 80) and SSH (port 22).

## Files

| File | Purpose |
|---|---|
| `start.sh` | Installs dependencies and starts the app on any Linux machine |
| `deploy/setup-ec2.sh` | One-time server setup (runs on the server with sudo) |
| `deploy/basecamp.service` | systemd unit: starts on boot, restarts on crash |
| `deploy/nginx-basecamp.conf` | nginx forwards port 80 to the app |
| `deploy/deploy.sh` | Copies code to the server and restarts it (runs on your laptop) |
| `app.py`, `schema.sql`, `templates/` | The Flask app, database schema, and pages |
