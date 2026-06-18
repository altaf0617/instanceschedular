"""
AWS EC2 Scheduler — Central Controller
=======================================
Flask web dashboard to manage EC2 instance schedules from one machine.

Setup:
    pip install flask boto3 apscheduler pytz

Run:
    python app.py

Then open: http://localhost:5000

AWS credentials can be set via:
    - Environment variables: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
    - OR ~/.aws/credentials file (recommended)
    - OR IAM role if running on EC2
"""

import json
import os
import logging
from datetime import datetime
from pathlib import Path

import boto3
import pytz
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, jsonify, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ─── Config ───────────────────────────────────────────────────────────────────
CONFIG_FILE = Path("scheduler_config.json")
DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
DEFAULT_TZ = "Asia/Karachi"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config helpers ───────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"instances": [], "region": DEFAULT_REGION, "timezone": DEFAULT_TZ}


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ─── AWS helpers ──────────────────────────────────────────────────────────────

def get_ec2(region: str):
    return boto3.client("ec2", region_name=region)


def fetch_instance_states(instance_ids: list[str], region: str) -> dict:
    """Return {instance_id: state_name} for given IDs."""
    if not instance_ids:
        return {}
    ec2 = get_ec2(region)
    try:
        resp = ec2.describe_instances(InstanceIds=instance_ids)
        states = {}
        for r in resp["Reservations"]:
            for inst in r["Instances"]:
                iid = inst["InstanceId"]
                name_tag = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), "")
                states[iid] = {
                    "state": inst["State"]["Name"],
                    "name": name_tag,
                    "type": inst.get("InstanceType", ""),
                    "az": inst.get("Placement", {}).get("AvailabilityZone", ""),
                }
        return states
    except (BotoCoreError, ClientError) as e:
        log.error("describe_instances failed: %s", e)
        return {}


def do_action(instance_ids: list[str], action: str, region: str) -> dict:
    ec2 = get_ec2(region)
    try:
        if action == "start":
            resp = ec2.start_instances(InstanceIds=instance_ids)
            changes = resp["StartingInstances"]
        else:
            resp = ec2.stop_instances(InstanceIds=instance_ids)
            changes = resp["StoppingInstances"]
        return {"ok": True, "changes": [
            {"id": c["InstanceId"], "from": c["PreviousState"]["Name"], "to": c["CurrentState"]["Name"]}
            for c in changes
        ]}
    except (BotoCoreError, ClientError) as e:
        return {"ok": False, "error": str(e)}


# ─── Scheduler ────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.start()


def rebuild_jobs():
    """Remove all existing schedule jobs and recreate from config."""
    for job in scheduler.get_jobs():
        if job.id.startswith("sched_"):
            job.remove()

    cfg = load_config()
    tz_str = cfg.get("timezone", DEFAULT_TZ)
    region = cfg.get("region", DEFAULT_REGION)

    for inst in cfg.get("instances", []):
        iid = inst["id"]
        if not inst.get("schedule_enabled"):
            continue

        start_time = inst.get("start_time", "09:00")
        stop_time  = inst.get("stop_time",  "18:00")
        days       = inst.get("days", "mon-fri")

        sh, sm = map(int, start_time.split(":"))
        eh, em = map(int, stop_time.split(":"))

        # Map "mon-fri" → APScheduler day_of_week string
        day_map = {"mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu",
                   "fri": "fri", "sat": "sat", "sun": "sun"}
        parts = [d.strip().lower()[:3] for d in days.replace("-", ",").split(",")]
        # Expand ranges like mon-fri
        if "-" in days.lower():
            all_days = ["mon","tue","wed","thu","fri","sat","sun"]
            raw = days.lower().split("-")
            if len(raw) == 2:
                s = all_days.index(raw[0].strip()[:3])
                e = all_days.index(raw[1].strip()[:3])
                parts = all_days[s:e+1]
        dow = ",".join(parts)

        scheduler.add_job(
            func=lambda ids=[iid], r=region: do_action(ids, "start", r),
            trigger=CronTrigger(hour=sh, minute=sm, day_of_week=dow, timezone=tz_str),
            id=f"sched_start_{iid}",
            replace_existing=True,
            name=f"Start {iid}",
        )
        scheduler.add_job(
            func=lambda ids=[iid], r=region: do_action(ids, "stop", r),
            trigger=CronTrigger(hour=eh, minute=em, day_of_week=dow, timezone=tz_str),
            id=f"sched_stop_{iid}",
            replace_existing=True,
            name=f"Stop {iid}",
        )
        log.info("Scheduled %s: start=%s stop=%s days=%s tz=%s", iid, start_time, stop_time, dow, tz_str)


# Build jobs on startup
rebuild_jobs()


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def api_save_config():
    data = request.json
    cfg = load_config()
    if "region" in data:
        cfg["region"] = data["region"]
    if "timezone" in data:
        cfg["timezone"] = data["timezone"]
    save_config(cfg)
    rebuild_jobs()
    return jsonify({"ok": True})


@app.route("/api/instances", methods=["GET"])
def api_instances():
    cfg = load_config()
    ids = [i["id"] for i in cfg.get("instances", [])]
    states = fetch_instance_states(ids, cfg.get("region", DEFAULT_REGION))
    result = []
    for inst in cfg.get("instances", []):
        iid = inst["id"]
        live = states.get(iid, {})
        result.append({**inst, **live})
    return jsonify(result)


@app.route("/api/instances", methods=["POST"])
def api_add_instance():
    data = request.json
    iid = data.get("id", "").strip()
    if not iid:
        return jsonify({"ok": False, "error": "Instance ID required"}), 400
    cfg = load_config()
    if any(i["id"] == iid for i in cfg["instances"]):
        return jsonify({"ok": False, "error": "Already exists"}), 400
    cfg["instances"].append({
        "id": iid,
        "label": data.get("label", ""),
        "start_time": data.get("start_time", "09:00"),
        "stop_time": data.get("stop_time", "18:00"),
        "days": data.get("days", "mon-fri"),
        "schedule_enabled": data.get("schedule_enabled", True),
    })
    save_config(cfg)
    rebuild_jobs()
    return jsonify({"ok": True})


@app.route("/api/instances/<iid>", methods=["PUT"])
def api_update_instance(iid):
    data = request.json
    cfg = load_config()
    for inst in cfg["instances"]:
        if inst["id"] == iid:
            inst.update({k: v for k, v in data.items() if k != "id"})
            break
    save_config(cfg)
    rebuild_jobs()
    return jsonify({"ok": True})


@app.route("/api/instances/<iid>", methods=["DELETE"])
def api_delete_instance(iid):
    cfg = load_config()
    cfg["instances"] = [i for i in cfg["instances"] if i["id"] != iid]
    save_config(cfg)
    rebuild_jobs()
    return jsonify({"ok": True})


@app.route("/api/action", methods=["POST"])
def api_action():
    data = request.json
    iid    = data.get("id")
    action = data.get("action")
    cfg    = load_config()
    if action not in ("start", "stop"):
        return jsonify({"ok": False, "error": "Invalid action"}), 400
    result = do_action([iid], action, cfg.get("region", DEFAULT_REGION))
    return jsonify(result)


@app.route("/api/jobs", methods=["GET"])
def api_jobs():
    jobs = []
    for job in scheduler.get_jobs():
        next_run = job.next_run_time.isoformat() if job.next_run_time else None
        jobs.append({"id": job.id, "name": job.name, "next_run": next_run})
    return jsonify(jobs)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
