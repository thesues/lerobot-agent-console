#!/usr/bin/env bash
# Deploy the self-hosted LiveKit SFU SHARING the console's CLB — console + livekit
# on ONE CLB / ONE public IP (no second load balancer). It reads the console CLB's
# id + public IP, then:
#   - the livekit Service reuses that CLB by id (volcengine-loadbalancer-id), adding
#     7880/7881/7882 listeners alongside the console's :80
#   - LiveKit runs with --node-ip = that shared CLB IP (ICE points clients there)
#
#   KUBECONFIG=~/Downloads/kube.conf ./scripts/deploy-livekit.sh
#
# Requires the console (Service lerobot-console-clb) deployed FIRST — it owns the CLB.
# `kubectl delete pod -l app=livekit` recreates livekit with the same spec (node-ip is
# baked in). Only a new console CLB (new id/IP) needs a re-run.
set -euo pipefail
: "${KUBECONFIG:=$HOME/Downloads/kube.conf}"; export KUBECONFIG
NS="${NS:-default}"
DIR="$(cd "$(dirname "$0")/../k8s/livekit" && pwd)"
CONSOLE_SVC="${CONSOLE_SVC:-lerobot-console-clb}"
ID_ANN='service.beta.kubernetes.io/system-volcengine-loadbalancer-create-response-id'

echo "==> reading the shared CLB (id + public IP) from $CONSOLE_SVC ..."
CLB_IP=""
for _ in $(seq 1 80); do
  CLB_IP=$(kubectl get svc "$CONSOLE_SVC" -n "$NS" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  [ -n "$CLB_IP" ] && break
  sleep 3
done
CLB_ID=$(kubectl get svc "$CONSOLE_SVC" -n "$NS" -o "jsonpath={.metadata.annotations.${ID_ANN//./\\.}}" 2>/dev/null || true)
[ -n "$CLB_ID" ] || { echo "ERROR: no CLB id on $CONSOLE_SVC — is the console deployed and its CLB up?"; exit 1; }
[ -n "$CLB_IP" ] || { echo "ERROR: no public IP on $CONSOLE_SVC"; exit 1; }
echo "    shared CLB: id=$CLB_ID  ip=$CLB_IP"

echo "==> LiveKit config + listeners on the shared CLB"
kubectl apply -f "$DIR/configmap.yaml"
sed "s/__SHARED_CLB_ID__/$CLB_ID/" "$DIR/service-clb.yaml" | kubectl apply -f -

echo "==> LiveKit server with --node-ip=$CLB_IP (the shared CLB IP)"
sed "s/__NODE_IP__/$CLB_IP/" "$DIR/deployment.yaml" | kubectl apply -f -

cat <<EOF

==================== LiveKit READY (shared CLB) ====================
Console + LiveKit share ONE CLB / ONE public IP: $CLB_IP
Mac dials OUT to:   ws://$CLB_IP:7880   (tcp 7881 / udp 7882 on the same IP)
LiveKit key/secret: devkey / lerobotlivekitsecret0123456789abcd  (CHANGE for prod)

NEXT — on the home Mac (real SO-100 + cameras):
  .venv/bin/python -m lerobot.robots.webrtc_proxy.mac_daemon \\
    --transport livekit --session so100 \\
    --livekit-url ws://$CLB_IP:7880 \\
    --livekit-api-key devkey --livekit-api-secret lerobotlivekitsecret0123456789abcd \\
    --robot.type=so100_follower --robot.port=/dev/tty.usbmodemXXXX \\
    --robot.id=my_follower --robot.cameras="{ front: {type: opencv, index_or_path: 1, width: 640, height: 480, fps: 30}, wrist: {type: opencv, index_or_path: 0, width: 640, height: 480} }"

THEN — in the console terminal (in-cluster it dials the internal service name):
  cd /lerobot && setsid nohup ./.venv/bin/python \\
    examples/webrtc_remote_so100/cloud_teleop_so100.py \\
    --mode web --transport livekit --session so100 --cameras "front,wrist" --web-port 8088 \\
    --livekit-url ws://livekit-clb:7880 \\
    --livekit-api-key devkey --livekit-api-secret lerobotlivekitsecret0123456789abcd \\
    > /tmp/teleop.log 2>&1 < /dev/null & echo started
====================================================================
EOF
