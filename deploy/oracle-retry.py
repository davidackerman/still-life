#!/usr/bin/env python3
"""Keep asking Oracle Cloud for the free Arm VM until one is free, then stop.

    caffeinate -i python3 deploy/oracle-retry.py   # on a Mac (stays awake)
    python3 deploy/oracle-retry.py                 # on an always-on Linux box, inside
                                                   # tmux so it survives logging out

Free Arm capacity comes and goes, so this tries every availability domain in
turn, then waits two minutes and tries again. It creates at most one instance
(named still-life-stream; it stops if one already exists), waits until it is
running, prints its public IP and (on a Mac) shows a notification.

Needs the OCI CLI (`brew install oci-cli`, or `pip install oci-cli` in a venv)
with an API key in ~/.oci/config (see SETUP.md), the network made by the VCN
wizard (public subnet "public subnet-still-life-vcn"), and the public half of
the SSH key from the console at ~/.ssh/still-life-stream.key.pub. Only the
public half is needed here; if just the private key is present, the public
half is derived from it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

NAME = "still-life-stream"
SUBNET = "public subnet-still-life-vcn"
SHAPE = "VM.Standard.A1.Flex"
OCPUS, MEMORY_GB = int(os.environ.get("OCPUS", "2")), int(os.environ.get("MEMORY_GB", "12"))  # the free limit
KEY = os.path.expanduser("~/.ssh/still-life-stream.key")
WAIT_S = 120


def say(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def oci(*args: str) -> tuple[int, dict | None, str]:
    # PYTHONWARNINGS: the CLI's own SyntaxWarnings on newer Pythons are noise
    r = subprocess.run(["oci", *args], capture_output=True, text=True, env={**os.environ, "PYTHONWARNINGS": "ignore"})
    data = None
    if r.returncode == 0 and not r.stdout.strip():
        data = {"data": []}  # the CLI prints nothing at all for an empty list
    elif r.stdout.strip():
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            pass
    return r.returncode, data, r.stderr


def need(*args: str) -> dict:
    code, data, err = oci(*args)
    if code or data is None:
        sys.exit(f"oci {' '.join(args[:3])} failed:\n{err.strip()[:800]}")
    return data


def tenancy() -> str:
    path = os.path.expanduser(os.environ.get("OCI_CLI_CONFIG_FILE", "~/.oci/config"))
    for line in open(path, encoding="utf-8"):
        k, _, v = line.partition("=")
        if k.strip() == "tenancy":
            return v.strip()
    sys.exit(f"no tenancy= line in {path}")


def existing(comp: str) -> dict | None:
    data = need("compute", "instance", "list", "--compartment-id", comp, "--display-name", NAME, "--all")
    alive = [i for i in data.get("data", []) if i["lifecycle-state"] not in ("TERMINATED", "TERMINATING")]
    return alive[0] if alive else None


def public_ip(instance_id: str) -> str | None:
    data = need("compute", "instance", "list-vnics", "--instance-id", instance_id)
    vnics = data.get("data", [])
    return vnics[0].get("public-ip") if vnics else None


def notify(msg: str) -> None:
    if shutil.which("osascript"):  # macOS only
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Oracle VM" sound name "Glass"'],
                       capture_output=True)


def finish(inst: dict) -> None:
    say(f"instance {inst['lifecycle-state']}: waiting for it to run…")
    while inst["lifecycle-state"] != "RUNNING":
        time.sleep(15)
        inst = need("compute", "instance", "get", "--instance-id", inst["id"])["data"]
    ip = public_ip(inst["id"])
    say(f"RUNNING. Public IP: {ip}")
    say(f"log in (from wherever the private key is) with: ssh -i ~/.ssh/still-life-stream.key ubuntu@{ip}")
    notify(f"still-life-stream is running: {ip}")


def main() -> None:
    pub = KEY + ".pub"
    if not os.path.exists(pub):
        if not os.path.exists(KEY):
            sys.exit(f"put the SSH public key at {pub} (or the private key from the console at {KEY})")
        r = subprocess.run(["ssh-keygen", "-y", "-f", KEY], capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"could not read {KEY}: {r.stderr.strip()} (is it chmod 600?)")
        with open(pub, "w", encoding="utf-8") as f:
            f.write(r.stdout)

    comp = tenancy()  # the root compartment
    if inst := existing(comp):
        say("an instance named still-life-stream already exists")
        finish(inst)
        return
    ads = [a["name"] for a in need("iam", "availability-domain", "list", "--compartment-id", comp)["data"]]
    subnets = need("network", "subnet", "list", "--compartment-id", comp, "--display-name", SUBNET)["data"]
    if not subnets:
        sys.exit(f'no subnet named "{SUBNET}": run the VCN wizard first (see SETUP.md)')
    images = need("compute", "image", "list", "--compartment-id", comp, "--operating-system", "Canonical Ubuntu",
                  "--operating-system-version", "24.04", "--shape", SHAPE, "--sort-by", "TIMECREATED",
                  "--sort-order", "DESC", "--limit", "1")["data"]
    if not images:
        sys.exit("no Ubuntu 24.04 image for the Arm shape found")
    say(f"image {images[0]['display-name']}, {OCPUS} OCPU / {MEMORY_GB} GB, trying {len(ads)} availability domains "
        f"every {WAIT_S // 60} min. Leave this running; Ctrl-C to stop.")

    tries = 0
    while True:
        for ad in ads:
            tries += 1
            code, data, err = oci("compute", "instance", "launch", "--compartment-id", comp,
                                  "--availability-domain", ad, "--display-name", NAME, "--shape", SHAPE,
                                  "--shape-config", json.dumps({"ocpus": OCPUS, "memoryInGBs": MEMORY_GB}),
                                  "--image-id", images[0]["id"], "--subnet-id", subnets[0]["id"],
                                  "--assign-public-ip", "true", "--ssh-authorized-keys-file", pub)
            if code == 0 and data:
                say(f"GOT ONE in {ad.split(':')[-1]} after {tries} tries")
                finish(data["data"])
                return
            low = err.lower()
            if "capacity" in low:
                continue
            if "toomanyrequests" in low or "429" in low:
                say("Oracle says slow down: waiting 10 min")
                time.sleep(600)
                continue
            sys.exit(f"launch failed for another reason (not capacity):\n{err.strip()[:800]}")
        say(f"all full ({tries} tries so far); next round in {WAIT_S // 60} min")
        time.sleep(WAIT_S)
        if inst := existing(comp):  # e.g. a launch that went through but whose reply was lost
            finish(inst)
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
