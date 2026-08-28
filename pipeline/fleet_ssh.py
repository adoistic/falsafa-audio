#!/usr/bin/env python3
"""
Falsafa narration fleet — SSH orchestration.

Creates plain RunPod pods (NO dockerStartCmd — that override disables sshd and was the
cause of a silent-failure fleet), waits for SSH, then installs and launches the worker
over SSH. Every step is observable, which the container-entrypoint approach was not.

  export RUNPOD_API_KEY=... ; source ~/.config/falsafa-deploy.env
  python3 pipeline/fleet_ssh.py --count 12 --procs 2
  python3 pipeline/fleet_ssh.py --status
  python3 pipeline/fleet_ssh.py --kill
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, threading, time, urllib.request

API = "https://rest.runpod.io/v1"
GQL = "https://api.runpod.io/graphql"
KEY = os.environ.get("RUNPOD_API_KEY", "")
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
KEEP = {"5ykheibdwdieug"}          # already-working pod, never touch

GPUS = ["NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 3090", "NVIDIA GeForce RTX 4080 SUPER",
        "NVIDIA GeForce RTX 4080", "NVIDIA RTX A4500", "NVIDIA RTX 4000 Ada Generation",
        "NVIDIA GeForce RTX 4070 Ti", "NVIDIA GeForce RTX 3090 Ti", "NVIDIA GeForce RTX 5080",
        "NVIDIA GeForce RTX 3080 Ti", "NVIDIA GeForce RTX 5090", "NVIDIA L40S"]

SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=15", "-o", "LogLevel=ERROR",
            "-i", os.path.expanduser("~/.ssh/id_ed25519")]


def req(url, data=None, method=None):
    r = urllib.request.Request(url, method=method or ("POST" if data else "GET"))
    r.add_header("Authorization", f"Bearer {KEY}")
    r.add_header("Content-Type", "application/json")
    r.add_header("User-Agent", "falsafa-audio/1.0")
    try:
        with urllib.request.urlopen(r, json.dumps(data).encode() if data is not None else None,
                                    timeout=90) as f:
            t = f.read().decode()
            return json.loads(t) if t.strip() else {}
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read().decode())
        except Exception: return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def pods():
    d = req(f"{API}/pods")
    return d if isinstance(d, list) else d.get("data", [])


def ssh_addr(pid):
    """REST /v1/pods/<id> often returns an empty runtime.ports; GraphQL is authoritative."""
    q = ('query{ pod(input:{podId:"%s"}){ runtime{ ports{ ip publicPort privatePort } } } }' % pid)
    d = req(f"{GQL}?api_key={KEY}", {"query": q})
    try:
        for p in (((d["data"]["pod"] or {}).get("runtime") or {}).get("ports") or []):
            if p.get("privatePort") == 22:
                return p.get("ip"), p.get("publicPort")
    except Exception:
        pass
    d = req(f"{API}/pods/{pid}")
    for p in ((d.get("runtime") or {}).get("ports") or []):
        if (p.get("private") or p.get("privatePort")) == 22:
            return p.get("ip"), (p.get("public") or p.get("publicPort"))
    return None, None


def ssh(ip, port, cmd, timeout=300):
    return subprocess.run(["ssh", *SSH_OPTS, "-p", str(port), f"root@{ip}", cmd],
                          capture_output=True, text=True, timeout=timeout)


def scp(ip, port, local, remote):
    return subprocess.run(["scp", *SSH_OPTS, "-P", str(port), local, f"root@{ip}:{remote}"],
                          capture_output=True, text=True, timeout=180)


SETUP = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq ffmpeg espeak-ng unzip curl >/dev/null 2>&1
curl -sSLo /tmp/rc.zip https://downloads.rclone.org/rclone-current-linux-amd64.zip
unzip -qo /tmp/rc.zip -d /tmp && cp /tmp/rclone-*/rclone /usr/local/bin/ && chmod +x /usr/local/bin/rclone
pip install -q click kokoro soundfile "transformers<5" >/dev/null 2>&1
python3 -m spacy download en_core_web_sm >/dev/null 2>&1 || true
python3 -c "import kokoro,torch;assert torch.cuda.is_available()"
echo SETUP_OK
"""

# Alignment fleet: no TTS deps; torch/torchaudio ship in the RunPod image.
# The MMS model (1.2 GB) is prefetched at setup so the two shard workers on a
# pod don't race the same torch-hub download.
SETUP_ALIGN = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq ffmpeg unzip curl >/dev/null 2>&1
curl -sSLo /tmp/rc.zip https://downloads.rclone.org/rclone-current-linux-amd64.zip
unzip -qo /tmp/rc.zip -d /tmp && cp /tmp/rclone-*/rclone /usr/local/bin/ && chmod +x /usr/local/bin/rclone
pip install -q num2words >/dev/null 2>&1
python3 - <<'PYEOF'
import torch, torchaudio
assert torch.cuda.is_available()
torchaudio.pipelines.MMS_FA.get_model(with_star=True)
PYEOF
echo SETUP_OK
"""


def launch(ip, port, shard, shards_total, i, track, voice, worker="tts"):
    """Start one worker, fire-and-forget. `ssh -n` + setsid detaches the worker, but the
    ssh CLIENT still sometimes blocks the full timeout before returning even though the
    remote process has already started. A raised TimeoutExpired here previously escaped
    the per-pod loop and left the SECOND shard on every pod unlaunched -> silent half
    coverage. So a timeout is EXPECTED and swallowed; coverage is verified separately."""
    if worker == "align":
        cmd = (f"cd /workspace && source env.sh && "
               f"export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True && "
               f"setsid nohup python3 -u align_worker.py "
               f"--track both --shard {shard} --of {shards_total} "
               f"--workdir /workspace/align/w{i} "
               f"> /workspace/align_{shard}.log 2>&1 < /dev/null & echo started")
    else:
        extra = f" --track {track}" + (f" --voice {voice}" if voice else "")
        cmd = (f"cd /workspace && source env.sh && setsid nohup python3 -u pod_worker.py "
               f"--shard {shard} --of {shards_total}{extra} "
               f"--bucket r2:falsafa-audio --workdir /workspace/{track}/w{i} "
               f"> /workspace/{track}_{shard}.log 2>&1 < /dev/null & echo started")
    try:
        subprocess.run(
            ["ssh", "-n", *SSH_OPTS, "-p", str(port), f"root@{ip}", cmd],
            capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        pass                       # detached; verify with --verify, never abort the loop


def bring_up(pid, shard_base, shards_total, procs, results, track="narration", voice="",
             worker="tts"):
    tag = f"pod{shard_base}"
    try:
        for _ in range(40):                      # wait for sshd
            ip, port = ssh_addr(pid)
            if ip and port:
                r = ssh(ip, port, "echo UP", timeout=25)
                if "UP" in r.stdout:
                    break
            time.sleep(15)
        else:
            results[pid] = f"{tag}: SSH never came up"; return

        env = (f"export RCLONE_CONFIG_R2_TYPE=s3\n"
               f"export RCLONE_CONFIG_R2_PROVIDER=Cloudflare\n"
               f"export RCLONE_CONFIG_R2_ACCESS_KEY_ID={os.environ['R2_ACCESS_KEY_ID']}\n"
               f"export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY={os.environ['R2_SECRET_ACCESS_KEY']}\n"
               f"export RCLONE_CONFIG_R2_ENDPOINT={os.environ['R2_S3_ENDPOINT']}\n")
        ssh(ip, port, f"mkdir -p /workspace && cat > /workspace/env.sh <<'XEOF'\n{env}XEOF\nchmod 600 /workspace/env.sh")
        here = os.path.dirname(os.path.abspath(__file__))
        if worker == "align":
            scp(ip, port, os.path.join(here, "align_worker.py"), "/workspace/align_worker.py")
            scp(ip, port, os.path.join(here, "align_common.py"), "/workspace/align_common.py")
        else:
            scp(ip, port, os.path.join(here, "pod_worker.py"), "/workspace/pod_worker.py")

        r = ssh(ip, port, SETUP_ALIGN if worker == "align" else SETUP, timeout=900)
        if "SETUP_OK" not in r.stdout:
            results[pid] = f"{tag}: SETUP FAILED {r.stdout[-200:]} {r.stderr[-200:]}"; return

        for i in range(procs):
            launch(ip, port, shard_base + i, shards_total, i, track, voice, worker)
        # log shipper
        ssh(ip, port,
            "cd /workspace && source env.sh && nohup bash -c 'while true; do "
            "rclone copy /workspace r2:falsafa-audio/_logs/$(hostname)/ --include \"*.log\" "
            "--s3-no-check-bucket -q 2>/dev/null; sleep 30; done' >/dev/null 2>&1 & disown; echo logger",
            timeout=60)
        results[pid] = f"{tag}: RUNNING shards {shard_base}..{shard_base+procs-1} @ {ip}:{port}"
    except Exception as e:
        results[pid] = f"{tag}: ERROR {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--procs", type=int, default=2)
    ap.add_argument("--shards-total", type=int, default=0)
    ap.add_argument("--shard-offset", type=int, default=0)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--kill", action="store_true")
    ap.add_argument("--adopt", action="store_true", help="bring up EXISTING falsafa-ssh-* pods")
    ap.add_argument("--relaunch", action="store_true",
                    help="start a new track on already-provisioned pods (no setup)")
    ap.add_argument("--track", default="narration", choices=["narration", "verse"])
    ap.add_argument("--voice", default="", help="override the track's default voice")
    ap.add_argument("--worker", default="tts", choices=["tts", "align"],
                    help="align = forced-alignment fleet (align_worker.py, --track both)")
    ap.add_argument("--verify", action="store_true",
                    help="report which --shard N processes are actually running fleet-wide")
    args = ap.parse_args()
    if not KEY: sys.exit("set RUNPOD_API_KEY")

    if args.status:
        for p in pods():
            print(f"{p.get('id')} {p.get('name'):<20} {p.get('desiredStatus'):<9} ${p.get('costPerHr')}/h")
        return

    if args.verify:
        live = [p for p in pods() if (p.get("name") or "").startswith("falsafa-ssh-")]
        running = set()
        for p in sorted(live, key=lambda p: int(p["name"].rsplit("-", 1)[1])):
            ip, port = ssh_addr(p["id"])
            if not ip:
                print(f"  {p['name']}: no addr"); continue
            r = ssh(ip, port, "ps aux | grep -E '[p]od_worker|[a]lign_worker' | "
                              "grep -oE -- '--shard [0-9]+' | sort -u",
                    timeout=30)
            sh = sorted(int(x.split()[1]) for x in r.stdout.split("\n") if x.startswith("--shard"))
            running.update(sh)
            print(f"  {p['name']:16} shards {sh}")
        total = args.shards_total or (len(live) * args.procs)
        missing = sorted(set(range(total)) - running)
        print(f"\nrunning {len(running)}/{total} shards; MISSING: {missing or 'none'}")
        return
    if args.kill:
        for p in pods():
            if p["id"] in KEEP: print("keep", p["id"]); continue
            req(f"{API}/pods/{p['id']}", method="DELETE"); print("killed", p["id"])
        return

    shards_total = args.shards_total or (args.count * args.procs + args.shard_offset)

    if args.relaunch:
        live = sorted(((p["id"], int((p.get("name") or "-1").rsplit("-", 1)[1]))
                       for p in pods() if (p.get("name") or "").startswith("falsafa-ssh-")),
                      key=lambda x: x[1])
        print(f"relaunching {args.worker}/{args.track} on {len(live)} pods, {shards_total} shards")
        here = os.path.dirname(os.path.abspath(__file__))
        files = (["align_worker.py", "align_common.py"] if args.worker == "align"
                 else ["pod_worker.py"])
        for pid, base in live:
            ip, port = ssh_addr(pid)
            if not ip:
                print(f"  pod{base}: no ssh addr"); continue
            # pods provisioned earlier are running whatever worker they were
            # given at setup; ship the current one before starting a new track
            bad = False
            for f in files:
                r = scp(ip, port, os.path.join(here, f), f"/workspace/{f}")
                if r.returncode != 0:
                    print(f"  pod{base}: scp failed {r.stderr[-120:]}"); bad = True; break
            if bad:
                continue
            for i in range(args.procs):
                launch(ip, port, base + i, shards_total, i, args.track, args.voice, args.worker)
            print(f"  pod{base}: shards {base}..{base+args.procs-1} @ {ip}:{port}", flush=True)
        return

    pk = open(os.path.expanduser("~/.ssh/id_ed25519.pub")).read().strip()

    if args.adopt:
        created = []
        for p in pods():
            n = p.get("name") or ""
            if n.startswith("falsafa-ssh-"):
                try: base = int(n.rsplit("-", 1)[1])
                except Exception: continue
                created.append((p["id"], base))
        created.sort(key=lambda x: x[1])
        print(f"adopting {len(created)} existing pods", flush=True)
        results, threads = {}, []
        for pid, base in created:
            t = threading.Thread(target=bring_up,
                                 args=(pid, base, shards_total, args.procs, results,
                                       args.track, args.voice, args.worker))
            t.start(); threads.append(t)
        for t in threads: t.join()
        for pid, msg in sorted(results.items(), key=lambda kv: kv[1]): print(" ", msg)
        print(f"\n{sum(1 for m in results.values() if 'RUNNING' in m)}/{len(created)} pods running")
        return

    created = []
    for i in range(args.count):
        got = None
        for gpu in GPUS:
            d = req(f"{API}/pods", {
                "name": f"falsafa-ssh-{args.shard_offset + i*args.procs}",
                "imageName": IMAGE, "gpuTypeIds": [gpu], "cloudType": "SECURE",
                "gpuCount": 1, "containerDiskInGb": 30, "volumeInGb": 0,
                "ports": ["22/tcp"], "env": {"PUBLIC_KEY": pk}})
            if d.get("id"): got = (d["id"], gpu, d.get("costPerHr")); break
            d = req(f"{API}/pods", {
                "name": f"falsafa-ssh-{args.shard_offset + i*args.procs}",
                "imageName": IMAGE, "gpuTypeIds": [gpu], "cloudType": "COMMUNITY",
                "gpuCount": 1, "containerDiskInGb": 30, "volumeInGb": 0,
                "ports": ["22/tcp"], "env": {"PUBLIC_KEY": pk}})
            if d.get("id"): got = (d["id"], gpu, d.get("costPerHr")); break
        if not got:
            print(f"pod {i}: no capacity"); break
        created.append((got[0], args.shard_offset + i*args.procs))
        print(f"pod {i}: {got[0]} {got[1]} ${got[2]}/h shards "
              f"{args.shard_offset + i*args.procs}..{args.shard_offset + i*args.procs + args.procs - 1}",
              flush=True)

    print(f"\n{len(created)} pods created; bringing up over SSH (parallel)...", flush=True)
    results, threads = {}, []
    for pid, base in created:
        t = threading.Thread(target=bring_up,
                             args=(pid, base, shards_total, args.procs, results,
                                   args.track, args.voice, args.worker))
        t.start(); threads.append(t)
    for t in threads: t.join()
    for pid, msg in sorted(results.items(), key=lambda kv: kv[1]):
        print(" ", msg)
    ok = sum(1 for m in results.values() if "RUNNING" in m)
    print(f"\n{ok}/{len(created)} pods running, {shards_total} shards total")


if __name__ == "__main__":
    main()
