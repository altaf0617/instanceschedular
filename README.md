# AWS EC2 Auto Scheduler — Web Dashboard

A central controller dashboard to start/stop EC2 instances on a schedule.
Runs on one machine (your controller), controls all your other instances remotely.

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure AWS credentials on the controller machine

**Option A — Environment variables:**
```bash
export AWS_ACCESS_KEY_ID=your_key
export AWS_SECRET_ACCESS_KEY=your_secret
export AWS_DEFAULT_REGION=us-east-1
```

**Option B — AWS credentials file (recommended):**
```bash
aws configure
# or manually edit ~/.aws/credentials
```

**Option C — IAM Role (if controller is an EC2 instance):**
Attach an IAM role with these permissions:
```json
{
  "Effect": "Allow",
  "Action": ["ec2:DescribeInstances", "ec2:StartInstances", "ec2:StopInstances"],
  "Resource": "*"
}
```

### 3. Run the dashboard
```bash
python app.py
```
Open your browser: **http://localhost:5000**  
(Or http://YOUR_CONTROLLER_IP:5000 from another machine on the same network)

---

## How It Works

1. Open the dashboard in your browser
2. Set your **AWS Region** and **Timezone** in the settings bar → Save
3. Click **+ Add Instance** and enter:
   - Instance ID (e.g. `i-0abc123def456789`)
   - A friendly label (e.g. "Dev Server")
   - Start time, Stop time, and which days
4. Toggle the **Auto-schedule** switch ON for each instance
5. The scheduler runs in the background — instances start/stop automatically

---

## Run on startup (Linux/systemd)

Create `/etc/systemd/system/ec2-scheduler.service`:
```ini
[Unit]
Description=EC2 Scheduler Dashboard
After=network.target

[Service]
WorkingDirectory=/path/to/aws-scheduler
ExecStart=/usr/bin/python3 /path/to/aws-scheduler/app.py
Restart=always
Environment=AWS_DEFAULT_REGION=us-east-1

[Install]
WantedBy=multi-user.target
```
Then:
```bash
sudo systemctl enable ec2-scheduler
sudo systemctl start ec2-scheduler
```

---

## Config file

Schedules are saved to `scheduler_config.json` next to `app.py`.
You can back it up or edit it directly:
```json
{
  "region": "us-east-1",
  "timezone": "Asia/Karachi",
  "instances": [
    {
      "id": "i-0abc123def456789",
      "label": "Dev Server",
      "start_time": "09:00",
      "stop_time": "18:00",
      "days": "mon-fri",
      "schedule_enabled": true
    }
  ]
}
```

---

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask backend + scheduler engine |
| `templates/index.html` | Web dashboard UI |
| `requirements.txt` | Python dependencies |
| `scheduler_config.json` | Auto-created on first save |
