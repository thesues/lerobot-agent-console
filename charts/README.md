# Helm charts

Replaces the hand-maintained manifests in `k8s/`. Those were gitignored, so they only ever
existed on one laptop, and the two console StatefulSets were kept in sync by editing both —
they turned out to be identical apart from the name, which is what one chart plus two values
files is for.

| chart | deploys | notes |
|---|---|---|
| `lerobot-agent-console` | console StatefulSet + headless Service | dev and test are the **same chart**, differing only by `nameOverride` |
| `livekit` | LiveKit SFU: Deployment + ConfigMap + its own public CLB | **shared by both consoles** — see below |

## Install / upgrade

```bash
export KUBECONFIG=~/Downloads/kube.conf

# dev
helm upgrade --install lerobot-agent-console charts/lerobot-agent-console

# test — same chart, one value different
helm upgrade --install lerobot-agent-console-test charts/lerobot-agent-console \
  -f charts/lerobot-agent-console/values-test.yaml

# livekit — nodeIp is REQUIRED (the chart refuses to render without it: a wrong value makes
# ICE hand clients an unroutable address and media never connects)
helm upgrade --install livekit charts/livekit \
  --set nodeIp=$(kubectl get svc livekit-clb -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
```

Ship a new image without touching the chart:

```bash
helm upgrade lerobot-agent-console charts/lerobot-agent-console \
  --set image.tag=$(git rev-parse HEAD) --reuse-values
```

**Resource names come from `nameOverride`, not the release name.** That value decides the PVC
name (`hermes-home-<name>-0`) and the pod DNS the multi-node training dials, so it is pinned in
values rather than inherited — installing under a different release name still produces the
right objects.

## Inspecting what is actually deployed

```bash
helm list                                    # releases, revision, status
helm get manifest lerobot-agent-console      # the YAML really submitted — the useful one
helm get values  lerobot-agent-console       # values in effect
helm history     lerobot-agent-console       # revisions; input to `helm rollback <name> <rev>`

# has anyone edited around Helm?
helm get manifest lerobot-agent-console | kubectl diff -f -
```

Helm keeps no local state: each release is a Secret in the namespace
(`kubectl get secret -l owner=helm`), so any machine with the same kubeconfig sees the same
releases. `STATUS: deployed` means the Helm operation succeeded — it says nothing about whether
the pods are healthy.

## Why LiveKit is a separate chart

The console does depend on it — the in-pod controller dials the SFU for teleoperation — so a
subchart is tempting. Two things argue against:

- **`nodeIp` is chicken-and-egg.** LiveKit starts with `--node-ip = the CLB's public IP`, and
  that IP does not exist until the Service has been provisioned. Bundled, one `helm install`
  cannot finish the job.
- **The CLB outlives any console.** It is billed, slow to provision, and its IP is baked into
  every client's `--livekit-url`. Bundling means `helm uninstall` of a console takes the SFU's
  address with it.

**One SFU serves both consoles.** LiveKit isolates by room and the teleop commands already pass
one (`--session so100`) — point dev at `so100` and test at `so100-test`. A second SFU only earns
its CLB when you need to change LiveKit itself without disturbing what dev is running.
`livekit-isaac` is already that shape: a second SFU paired with no console at all (it serves
isaaclab), which is the other reason this chart has to stand alone.

The CLB Service carries `helm.sh/resource-policy: keep`, so even `helm uninstall livekit` leaves
it — and the public IP — in place. Remove it deliberately: `kubectl delete svc <name>-clb`.

## Two immutable fields that will bite

**`volumeClaimTemplates` cannot change on an existing StatefulSet.** `persistence.size` is
`1Ti`, matching the real disks. If a running StatefulSet's template says something else, the
API server rejects the upgrade outright — recreate the object with
`kubectl delete sts <name> --cascade=orphan` (pods keep running, PVCs survive) and install
again. To grow a disk later, edit the PVC **and** this value, or the next recreate silently
goes back down.

**`spec.selector` cannot change either.** The consoles use a bare `app: <name>`, so
`console.selectorLabels` emits exactly that. Adding the conventional `app.kubernetes.io/*` or
`helm.sh/chart` labels there breaks every upgrade — extra labels belong on metadata.

**Check with an apply dry-run, not `kubectl diff`.** `kubectl diff` writes an immutable-field
rejection to *stderr* and exits 2, so a pipeline that discards stderr reports "no differences"
for a change the cluster will not accept:

```bash
helm template lerobot-agent-console charts/lerobot-agent-console | kubectl apply --dry-run=server -f -
```

## Secrets are not in the charts

Both charts reference Secrets by name and never create them:

```bash
kubectl create secret generic lerobot-console-auth --from-literal=user=<u> --from-literal=password=<p>
kubectl create secret generic livekit-auth --from-literal=keys='<api_key>: <api_secret>'
```

`livekit` has **no credential fallback on purpose** — that SFU is on a public CLB, so a key
committed here would be readable by anyone who can read the repo. A missing Secret holds the pod
in `CreateContainerConfigError`, which is the intended failure.

## Publishing to the OCI registry

```bash
helm registry login iaas-us-cn-beijing.cr.volces.com -u <account>@<account-id>
helm package charts/lerobot-agent-console
helm push lerobot-agent-console-<version>.tgz oci://iaas-us-cn-beijing.cr.volces.com/physicalai
```

The chart is named after the product, so it lands in the **same repo as the image**
(`physicalai/lerobot-agent-console`). They coexist: image tags are 40-char commit SHAs, chart
tags are semver. Bump `version:` in `Chart.yaml` for any template change — pushing over an
existing version is how you get two different charts answering to one number.

Credentials come from the macOS keychain (`credsStore`), so `auths` in the config JSON is empty
even when you are logged in. A read probe returning **404 rather than 401** means auth worked.

## Not converted

`k8s/apig-ingress*.yaml` and `k8s/apig-instance-test.yaml` stay raw. They create Volcengine
`APIGInstance` CRDs that own **provisioned gateways with public domains**; a stray
`helm uninstall` would delete the gateway and the domain with it. They change roughly never.
