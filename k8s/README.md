# Deploying on Volcengine VKE

The manifests here bring up the **LeRobot Agent Console** (one container: lerobot +
slim hermes, serving HTTPS natively) behind an **L4 CLB**, plus an optional
self-hosted **LiveKit** SFU for remote-robot teleop.

```
browser ──https/wss──► CLB (lerobot-console-clb, :443) ──► console pod :8080 (native TLS)
Mac robot ──dial out──► CLB (livekit-clb, 7880/7881/7882) ──► LiveKit ◄── controller (in console pod)
```

## Files

| file | what |
|---|---|
| `deployment.yaml` | the console (native TLS, no sidecar); mounts the hermes PVC + TLS cert |
| `service-lb.yaml` | public **CLB**: `443 → console:8080` |
| `pvc.yaml` | `HERMES_HOME=/opt/data` PVC (Ark key + sessions + skill persist) |
| `secret.example.yaml` | login Secret template (don't commit real values) |
| `livekit/` | self-hosted LiveKit SFU + its 3-port CLB (optional; for teleop) |

The TLS cert is a **runtime-generated Secret** (`lerobot-console-tls`), not a file.

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

# 2) self-signed TLS cert (no domain needed; browser warns once)
openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
  -keyout tls.key -out tls.crt -subj "/CN=lerobot-console" \
  -addext "subjectAltName=DNS:lerobot-console"
kubectl create secret tls lerobot-console-tls --cert=tls.crt --key=tls.key

# 3) storage + workload + public CLB
kubectl apply -f pvc.yaml
kubectl apply -f deployment.yaml
kubectl apply -f service-lb.yaml

# 4) get the CLB public IP, then open it
kubectl get svc lerobot-console-clb -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
# open https://<that-ip>/   (accept the self-signed warning; login = the secret above)
```

On first chat the UI asks for your **Volcengine Ark API key** (chat-only; persisted
on the PVC). `kubectl exec -it deploy/lerobot-console -- bash` drops you into the
same lerobot env the console drives.

### Notes
- **No default StorageClass** on VKE and ESSD has a ~20Gi minimum — `pvc.yaml`
  sets `ebs-essd` + `20Gi` (a smaller/unset request fails to bind).
- HTTPS is **self-signed** (browser warns once). For a *trusted* cert you'd upload
  one to 火山 证书中心 and front the console with an ALB ingress using its cert-id —
  that needs the console/API, not kubectl.
- The console runs as **root** and serves HTTPS itself; the CLB is plain **L4**.

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
kubectl delete secret lerobot-console-auth lerobot-console-tls
kubectl delete -f pvc.yaml            # WARNING: deletes the Ark key + sessions
# livekit:
kubectl delete -f livekit/ ; kubectl delete deploy livekit
```
