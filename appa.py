"""
AWS EC2 Scheduler — Multi-Account Central Controller
=====================================================
Manages EC2 instances across multiple AWS accounts and regions.

Setup:
    pip install flask boto3 apscheduler pytz

Run:
    python app.py  →  open http://localhost:5000
"""

import json
import os
import logging
from functools import wraps
from pathlib import Path

import boto3
import pytz
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, jsonify, render_template, request, session, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ─── Config ───────────────────────────────────────────────────────────────────
CONFIG_FILE  = Path("scheduler_config.json")
DEFAULT_TZ   = "Asia/Karachi"

# ─── AUTH CONFIG — change username/password here ──────────────────────────────
AUTH_USERNAME = os.environ.get("SCHEDULER_USER",     "admin")
AUTH_PASSWORD = os.environ.get("SCHEDULER_PASSWORD", "admin123")
SECRET_KEY    = os.environ.get("SCHEDULER_SECRET",   "ec2-scheduler-fixed-secret-2024")
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = SECRET_KEY

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Unauthorized"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

# ─── Config helpers ───────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {
        "timezone": DEFAULT_TZ,
        "accounts": [],     # [{id, label, access_key, secret_key}]
        "instances": [],    # [{id, account_id, region, label, start_time, stop_time, days, schedule_enabled}]
    }

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ─── AWS helpers ──────────────────────────────────────────────────────────────

def get_ec2(account_id: str, region: str):
    """Return a boto3 EC2 client for the given account and region."""
    cfg = load_config()

    # Account 1 = "default" — uses IAM role attached to this EC2 (no keys needed)
    acct = next((a for a in cfg.get("accounts", []) if a["id"] == account_id), None)

    if acct and acct.get("access_key") and acct.get("secret_key"):
        return boto3.client(
            "ec2",
            region_name=region,
            aws_access_key_id=acct["access_key"],
            aws_secret_access_key=acct["secret_key"],
        )
    # Fallback: use instance IAM role / environment credentials
    return boto3.client("ec2", region_name=region)


def fetch_instance_states(instances_cfg: list) -> dict:
    """Batch describe instances grouped by (account, region). Returns {iid: {...}}."""
    # Group by (account_id, region)
    groups = {}
    for inst in instances_cfg:
        key = (inst["account_id"], inst["region"])
        groups.setdefault(key, []).append(inst["id"])

    result = {}
    for (acct_id, region), ids in groups.items():
        ec2 = get_ec2(acct_id, region)
        try:
            resp = ec2.describe_instances(InstanceIds=ids)
            for r in resp["Reservations"]:
                for i in r["Instances"]:
                    iid = i["InstanceId"]
                    result[iid] = {
                        "state": i["State"]["Name"],
                        "aws_name": next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), ""),
                        "type": i.get("InstanceType", ""),
                        "az": i.get("Placement", {}).get("AvailabilityZone", ""),
                    }
        except (BotoCoreError, ClientError) as e:
            log.error("describe_instances failed (%s / %s): %s", acct_id, region, e)
            for iid in ids:
                result[iid] = {"state": "error", "aws_name": "", "type": "", "az": ""}
    return result


def do_action(instance_id: str, account_id: str, region: str, action: str) -> dict:
    ec2 = get_ec2(account_id, region)
    try:
        if action == "start":
            resp    = ec2.start_instances(InstanceIds=[instance_id])
            changes = resp["StartingInstances"]
        else:
            resp    = ec2.stop_instances(InstanceIds=[instance_id])
            changes = resp["StoppingInstances"]
        return {"ok": True, "changes": [
            {"id": c["InstanceId"], "from": c["PreviousState"]["Name"], "to": c["CurrentState"]["Name"]}
            for c in changes
        ]}
    except (BotoCoreError, ClientError) as e:
        log.error("action %s on %s failed: %s", action, instance_id, e)
        return {"ok": False, "error": str(e)}


def test_account_credentials(access_key: str, secret_key: str, region: str) -> dict:
    """Quick check: try to call describe_instances with given credentials."""
    try:
        ec2 = boto3.client(
            "ec2", region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        ec2.describe_instances(MaxResults=5)
        return {"ok": True}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        return {"ok": False, "error": f"{code}: {e.response['Error']['Message']}"}
    except BotoCoreError as e:
        return {"ok": False, "error": str(e)}


# ─── Scheduler ────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.start()

ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

def expand_days(days_str: str) -> str:
    """Convert 'mon-fri' → 'mon,tue,wed,thu,fri' for APScheduler."""
    days_str = days_str.lower().strip()
    if "-" in days_str:
        parts = days_str.split("-")
        if len(parts) == 2:
            try:
                s = ALL_DAYS.index(parts[0][:3])
                e = ALL_DAYS.index(parts[1][:3])
                return ",".join(ALL_DAYS[s:e+1])
            except ValueError:
                pass
    return days_str


def rebuild_jobs():
    for job in scheduler.get_jobs():
        if job.id.startswith("sched_"):
            job.remove()

    cfg    = load_config()
    tz_str = cfg.get("timezone", DEFAULT_TZ)

    for inst in cfg.get("instances", []):
        if not inst.get("schedule_enabled"):
            continue

        iid        = inst["id"]
        acct_id    = inst["account_id"]
        region     = inst["region"]
        start_time = inst.get("start_time", "09:00")
        stop_time  = inst.get("stop_time",  "18:00")
        days_str   = inst.get("days", "mon-fri")
        dow        = expand_days(days_str)

        sh, sm = map(int, start_time.split(":"))
        eh, em = map(int, stop_time.split(":"))

        scheduler.add_job(
            func=lambda i=iid, a=acct_id, r=region: do_action(i, a, r, "start"),
            trigger=CronTrigger(hour=sh, minute=sm, day_of_week=dow, timezone=tz_str),
            id=f"sched_start_{iid}",
            replace_existing=True,
            name=f"Start {iid}",
        )
        scheduler.add_job(
            func=lambda i=iid, a=acct_id, r=region: do_action(i, a, r, "stop"),
            trigger=CronTrigger(hour=eh, minute=em, day_of_week=dow, timezone=tz_str),
            id=f"sched_stop_{iid}",
            replace_existing=True,
            name=f"Stop {iid}",
        )
        log.info("Scheduled %s (%s/%s): %s–%s %s", iid, acct_id, region, start_time, stop_time, dow)


rebuild_jobs()

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET"])
def login_page():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def do_login():
    data     = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if username == AUTH_USERNAME and password == AUTH_PASSWORD:
        session["logged_in"] = True
        session.permanent    = True
        log.info("Successful login from %s", request.remote_addr)
        return jsonify({"ok": True})
    log.warning("Failed login attempt from %s", request.remote_addr)
    return jsonify({"ok": False, "error": "Invalid username or password"}), 401


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


# ── Global config ──
@app.route("/api/config", methods=["GET"])
@login_required
def api_get_config():
    cfg = load_config()
    # Never expose secret keys to frontend
    safe_accounts = []
    for a in cfg.get("accounts", []):
        safe_accounts.append({
            "id": a["id"],
            "label": a["label"],
            "has_keys": bool(a.get("access_key")),
        })
    return jsonify({"timezone": cfg.get("timezone", DEFAULT_TZ), "accounts": safe_accounts})


@app.route("/api/config/timezone", methods=["POST"])
@login_required
def api_save_timezone():
    cfg = load_config()
    cfg["timezone"] = request.json.get("timezone", DEFAULT_TZ)
    save_config(cfg)
    rebuild_jobs()
    return jsonify({"ok": True})


# ── Accounts ──
@app.route("/api/accounts", methods=["GET"])
@login_required
def api_get_accounts():
    cfg = load_config()
    return jsonify([
        {"id": a["id"], "label": a["label"], "has_keys": bool(a.get("access_key"))}
        for a in cfg.get("accounts", [])
    ])


@app.route("/api/accounts", methods=["POST"])
@login_required
def api_add_account():
    data       = request.json
    label      = data.get("label", "").strip()
    access_key = data.get("access_key", "").strip()
    secret_key = data.get("secret_key", "").strip()
    test_region= data.get("test_region", "us-east-1").strip()

    if not label or not access_key or not secret_key:
        return jsonify({"ok": False, "error": "Label, Access Key and Secret Key are required"}), 400

    # Test credentials before saving
    test = test_account_credentials(access_key, secret_key, test_region)
    if not test["ok"]:
        return jsonify({"ok": False, "error": f"Credentials invalid: {test['error']}"}), 400

    cfg = load_config()
    import uuid
    acct_id = "acct_" + str(uuid.uuid4())[:8]
    cfg.setdefault("accounts", []).append({
        "id":         acct_id,
        "label":      label,
        "access_key": access_key,
        "secret_key": secret_key,
    })
    save_config(cfg)
    return jsonify({"ok": True, "id": acct_id})


@app.route("/api/accounts/<acct_id>", methods=["DELETE"])
@login_required
def api_delete_account(acct_id):
    cfg = load_config()
    cfg["accounts"]  = [a for a in cfg.get("accounts", []) if a["id"] != acct_id]
    cfg["instances"] = [i for i in cfg.get("instances", []) if i.get("account_id") != acct_id]
    save_config(cfg)
    rebuild_jobs()
    return jsonify({"ok": True})


# ── Instances ──
@app.route("/api/instances", methods=["GET"])
@login_required
def api_instances():
    cfg       = load_config()
    inst_cfgs = cfg.get("instances", [])
    states    = fetch_instance_states(inst_cfgs)

    # Build account label map
    acct_labels = {a["id"]: a["label"] for a in cfg.get("accounts", [])}

    result = []
    for inst in inst_cfgs:
        iid  = inst["id"]
        live = states.get(iid, {})
        result.append({
            **inst,
            **live,
            "account_label": acct_labels.get(inst.get("account_id"), "Default (IAM Role)"),
        })
    return jsonify(result)


@app.route("/api/instances", methods=["POST"])
@login_required
def api_add_instance():
    data = request.json
    iid  = data.get("id", "").strip()
    if not iid:
        return jsonify({"ok": False, "error": "Instance ID required"}), 400

    cfg = load_config()
    if any(i["id"] == iid for i in cfg.get("instances", [])):
        return jsonify({"ok": False, "error": "Instance already exists"}), 400

    cfg.setdefault("instances", []).append({
        "id":               iid,
        "account_id":       data.get("account_id", "default"),
        "region":           data.get("region", "us-east-1"),
        "label":            data.get("label", ""),
        "start_time":       data.get("start_time", "09:00"),
        "stop_time":        data.get("stop_time", "18:00"),
        "days":             data.get("days", "mon-fri"),
        "schedule_enabled": data.get("schedule_enabled", True),
    })
    save_config(cfg)
    rebuild_jobs()
    return jsonify({"ok": True})


@app.route("/api/instances/<iid>", methods=["PUT"])
@login_required
def api_update_instance(iid):
    data = request.json
    cfg  = load_config()
    for inst in cfg.get("instances", []):
        if inst["id"] == iid:
            inst.update({k: v for k, v in data.items() if k != "id"})
            break
    save_config(cfg)
    rebuild_jobs()
    return jsonify({"ok": True})


@app.route("/api/instances/<iid>", methods=["DELETE"])
@login_required
def api_delete_instance(iid):
    cfg = load_config()
    cfg["instances"] = [i for i in cfg.get("instances", []) if i["id"] != iid]
    save_config(cfg)
    rebuild_jobs()
    return jsonify({"ok": True})


@app.route("/api/action", methods=["POST"])
@login_required
def api_action():
    data   = request.json
    iid    = data.get("id")
    action = data.get("action")
    cfg    = load_config()
    inst   = next((i for i in cfg.get("instances", []) if i["id"] == iid), None)
    if not inst:
        return jsonify({"ok": False, "error": "Instance not found"}), 404
    if action not in ("start", "stop"):
        return jsonify({"ok": False, "error": "Invalid action"}), 400
    result = do_action(iid, inst["account_id"], inst["region"], action)
    return jsonify(result)


@app.route("/api/jobs", methods=["GET"])
@login_required
def api_jobs():
    jobs = []
    for job in scheduler.get_jobs():
        next_run = job.next_run_time.isoformat() if job.next_run_time else None
        jobs.append({"id": job.id, "name": job.name, "next_run": next_run})
    return jsonify(sorted(jobs, key=lambda j: j["next_run"] or ""))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
