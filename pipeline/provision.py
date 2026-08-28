#!/usr/bin/env python3
"""
Provision RunPod GPU pods for the Falsafa narration fleet.

Each pod self-bootstraps from R2 (no SSH needed): installs rclone/ffmpeg/kokoro, pulls
pod_worker.py + the corpus, runs its shard(s), uploads finished HLS to R2, ships logs
to r2:falsafa-audio/_logs/<pod>/ every 30s.

  python3 pipeline/provision.py --count 1 --procs 1 --calibrate
  python3 pipeline/provision.py --count 16 --procs 2
  python3 pipeline/provision.py --list
  python3 pipeline/provision.py --kill-all
"""
from __future__ import annotations
import argparse, json, os, sys, urllib.request

API = "https://rest.runpod.io/v1"
GQL = "https://api.runpod.io/graphql"
KEY = os.environ.get("RUNPOD_API_KEY", "")

# cheapest-first; Kokoro is 82M params so VRAM is irrelevant, only availability matters
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

GPU_PREFERENCE = [
    "NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 3090", "NVIDIA GeForce RTX 4080 SUPER",
    "NVIDIA GeForce RTX 4080", "NVIDIA RTX A4500", "NVIDIA RTX 4000 Ada Generation",
    "NVIDIA GeForce RTX 4070 Ti", "NVIDIA RTX A5000", "NVIDIA GeForce RTX 3090 Ti",
    "NVIDIA RTX A6000", "NVIDIA A40", "NVIDIA GeForce RTX 5090", "NVIDIA GeForce RTX 5080",
    "NVIDIA GeForce RTX 3080 Ti", "NVIDIA L4", "NVIDIA L40S", "NVIDIA A30",
]

BOOT = (
    "curl -sSLo /tmp/rc.zip https://downloads.rclone.org/rclone-current-linux-amd64.zip "
    "&& (unzip -qo /tmp/rc.zip -d /tmp 2>/dev/null || (apt-get update -qq && "
    "apt-get install -y -qq unzip >/dev/null && unzip -qo /tmp/rc.zip -d /tmp)) "
    "&& cp /tmp/rclone-*/rclone /usr/local/bin/ && chmod +x /usr/local/bin/rclone "
    "&& export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare "
    "RCLONE_CONFIG_R2_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID "
    "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY "
    "RCLONE_CONFIG_R2_ENDPOINT=$R2_S3_ENDPOINT "
    "&& mkdir -p /workspace && rclone copy r2:falsafa-audio/_bootstrap/pod_bootstrap.sh "
    "/workspace/ --s3-no-check-bucket && chmod +x /workspace/pod_bootstrap.sh "
    "&& /workspace/pod_bootstrap.sh; sleep infinity"
)


def req(url, data=None, method=None):
    r = urllib.request.Request(url, method=method or ("POST" if data else "GET"))
    r.add_header("Authorization", f"Bearer {KEY}")
    r.add_header("Content-Type", "application/json")
    r.add_header("User-Agent", "falsafa-audio/1.0")
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(r, body, timeout=90) as f:
            raw = f.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def stock():
    """GPU -> (price, stockStatus), only those with any stock."""
    out = {}
    for g in GPU_PREFERENCE:
        q = ('query{ gpuTypes(input:{id:"%s"}){ displayName lowestPrice'
             '(input:{gpuCount:1,secureCloud:false}){ uninterruptablePrice stockStatus } } }' % g)
        d = req(f"{GQL}?api_key={KEY}", {"query": q})
        try:
            t = d["data"]["gpuTypes"][0]
            lp = t.get("lowestPrice") or {}
            if lp.get("stockStatus"):
                out[g] = (lp.get("uninterruptablePrice") or 0, lp["stockStatus"])
        except Exception:
            pass
    return out


def make_pod(gpu, cloud, shard_base, shards_total, procs, extra, disk):
    env = {
        "R2_ACCESS_KEY_ID": os.environ["R2_ACCESS_KEY_ID"],
        "R2_SECRET_ACCESS_KEY": os.environ["R2_SECRET_ACCESS_KEY"],
        "R2_S3_ENDPOINT": os.environ["R2_S3_ENDPOINT"],
        "SHARD_BASE": str(shard_base),
        "SHARDS_TOTAL": str(shards_total),
        "PROCS": str(procs),
        "WORKER_EXTRA": extra,
    }
    payload = {
        "name": f"falsafa-narr-{shard_base}",
        "imageName": IMAGE,
        "gpuTypeIds": [gpu],
        "cloudType": cloud,
        "gpuCount": 1,
        "containerDiskInGb": disk,
        "volumeInGb": 0,
        "env": env,
        "dockerStartCmd": ["bash", "-lc", BOOT],
    }
    return req(f"{API}/pods", payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--procs", type=int, default=2, help="worker processes per pod")
    ap.add_argument("--shards-total", type=int, default=0, help="default: count*procs")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--disk", type=int, default=20)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--kill-all", action="store_true")
    ap.add_argument("--stock", action="store_true")
    args = ap.parse_args()

    if not KEY:
        sys.exit("set RUNPOD_API_KEY")

    if args.stock:
        for g, (p, s) in stock().items():
            print(f"{g:<36} ${p:>6.2f}/h  {s}")
        return

    if args.list:
        pods = req(f"{API}/pods")
        pods = pods if isinstance(pods, list) else pods.get("data", [])
        if not pods:
            print("(no pods)")
        for p in pods:
            print(f"{p.get('id')}  {p.get('name'):<22} {p.get('desiredStatus'):<10} "
                  f"${p.get('costPerHr')}/h  {(p.get('machine') or {}).get('gpuTypeId','')}")
        return

    if args.kill_all:
        pods = req(f"{API}/pods")
        pods = pods if isinstance(pods, list) else pods.get("data", [])
        for p in pods:
            r = req(f"{API}/pods/{p['id']}", method="DELETE")
            print(f"terminated {p['id']} {p.get('name')} {r if r else ''}")
        return

    shards_total = args.shards_total or (args.count * args.procs)
    extra = "--calibrate --limit 3" if args.calibrate else ""
    avail = stock()
    print("available:", ", ".join(f"{g.split()[-2]+' '+g.split()[-1]}({s})"
                                  for g, (_, s) in avail.items()) or "NONE")

    created, shard = [], 0
    for i in range(args.count):
        got = None
        for gpu in [g for g in GPU_PREFERENCE if g in avail]:
            for cloud in ("COMMUNITY", "SECURE"):
                d = make_pod(gpu, cloud, shard, shards_total, args.procs, extra, args.disk)
                if d.get("id"):
                    got = (d["id"], gpu, cloud, d.get("costPerHr"))
                    break
                err = str(d.get("error", ""))[:60]
            if got:
                break
        if not got:
            print(f"pod {i}: NO CAPACITY anywhere (last: {err})")
            break
        pid, gpu, cloud, cost = got
        created.append(pid)
        print(f"pod {i}: {pid}  {gpu}  {cloud}  ${cost}/h  shards {shard}..{shard+args.procs-1}")
        shard += args.procs

    print(f"\ncreated {len(created)} pods, {shards_total} shards total")
    if created:
        open("/tmp/falsafa_pods.txt", "a").write("\n".join(created) + "\n")


if __name__ == "__main__":
    main()
