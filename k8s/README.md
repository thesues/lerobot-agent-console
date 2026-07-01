# Deploying on Volcengine VKE

The manifests here bring up the **LeRobot Agent Console** (one container: lerobot +
slim hermes, serving plain **HTTP** by default) behind an **L4 CLB**, plus an optional
self-hosted **LiveKit** SFU for remote-robot teleop.

```
browser ──http/ws──► CLB (lerobot-console-clb, :80) ──► console pod :8080 (plain HTTP)
Mac robot ──dial out──► CLB (livekit-clb, 7880/7881/7882) ──► LiveKit ◄── controller (in console pod)
```

The UI shows a small **"未加密 (HTTP)"** warning while TLS is off. To serve HTTPS
instead, see *Enabling HTTPS* below.

## Files

| file | what |
|---|---|
| `deployment.yaml` | the console (plain HTTP; no sidecar); mounts the hermes PVC |
| `service-lb.yaml` | public **CLB**: `80 → console:8080` |
| `pvc.yaml` | `HERMES_HOME=/opt/data` PVC (Ark key + sessions + skill persist) |
| `secret.example.yaml` | login Secret template (don't commit real values) |
| `livekit/` | self-hosted LiveKit SFU + its 3-port CLB (optional; for teleop) |

## Prerequisites

- `kubectl` pointed at the cluster (`export KUBECONFIG=~/Downloads/kube.conf`).
- A **gzip-pushed** console image (VKE node containerd 1.6.x rejects zstd).
- Fill the placeholders:
  - `deployment.yaml` → `image:` your console tag.
  - `service-lb.yaml` (and `livekit/service-clb.yaml`) → `…loadbalancer-subnet-id` =
    a subnet in your cluster's VPC (e.g. `subnet-13g74uz5gigw03n6nu4qbfl9s`).
  - `pvc.yaml` → `storageClassName` (`kubectl get storageclass`; e.g. `ebs-essd`).

## Deploy the console

```bash
export KUBECONFIG=~/Downloads/kube.conf

# 1) login secret (single-user HTTP Basic)
kubectl create secret generic lerobot-console-auth \
  --from-literal=user=lerobot --from-literal=password='<strong-password>'

# 2) storage + workload + public CLB (set the subnet-id in service-lb.yaml first)
kubectl apply -f pvc.yaml
kubectl apply -f deployment.yaml
kubectl apply -f service-lb.yaml

# 3) get the CLB public IP, then open it
kubectl get svc lerobot-console-clb -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
# open http://<that-ip>/   (login = the secret above)
```

On first chat the UI asks for your **Volcengine Ark API key** (chat-only; persisted
on the PVC). `kubectl exec -it deploy/lerobot-console -- bash` drops you into the
same lerobot env the console drives.

### Notes
- **No default StorageClass** on VKE and ESSD has a ~20Gi minimum — `pvc.yaml`
  sets `ebs-essd` + `20Gi` (a smaller/unset request fails to bind).
- Traffic is **plain HTTP** by default (the UI shows a "未加密" warning); the CLB is
  plain **L4**. Fine for a trusted network; put it behind a VPN/bastion if exposed.
- The console runs as **root**.

### Enabling HTTPS (optional)
The console can serve TLS itself — no sidecar. Create a cert Secret and set the two
env vars back in `deployment.yaml`:

```bash
# self-signed cert (no domain needed; browser warns once). Put the CLB IP in the SAN.
openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
  -keyout tls.key -out tls.crt -subj "/CN=lerobot-console" \
  -addext "subjectAltName=IP:<your-clb-ip>,DNS:lerobot-console"
kubectl create secret tls lerobot-console-tls --cert=tls.crt --key=tls.key
```

Then in `deployment.yaml` re-add `CONSOLE_TLS_CERT` + `CONSOLE_TLS_KEY`, the `tls`
volume/mount, and flip both probes to `scheme: HTTPS`; in `service-lb.yaml` change
the listener to `port: 443`. Re-apply, and open `https://<clb-ip>/`. (A *trusted*,
no-warning cert needs a 火山 证书中心 cert-id fronted by an ALB ingress — that needs
the console/API, not kubectl.)

## Remote robot teleop (optional)

LiveKit lets the cloud reach a SO-100 on your home Mac (both ends dial **out**).
Use the helper (it solves the `--node-ip = CLB public IP` chicken-and-egg):

```bash
KUBECONFIG=~/Downloads/kube.conf ../scripts/deploy-livekit.sh
```

It prints the Mac `mac_daemon` command and the in-console `cloud_teleop_so100.py`
command. The controller runs **in the console pod** (start it from the terminal);
its panel shows in the left viewer via "+ 打开" → its port. See the repo README's
"Remote robot teleop" section for the full walkthrough.

## Teardown

```bash
kubectl delete -f service-lb.yaml -f deployment.yaml
kubectl delete secret lerobot-console-auth   # + lerobot-console-tls if you enabled HTTPS
kubectl delete -f pvc.yaml            # WARNING: deletes the Ark key + sessions
# livekit:
kubectl delete -f livekit/ ; kubectl delete deploy livekit
```
