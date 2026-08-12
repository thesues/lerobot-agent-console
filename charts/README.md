# Helm charts

Replaces the hand-maintained manifests in `k8s/` (which is gitignored, so those files only ever
existed on one laptop — a chart in git is the point of this move). Two charts:

| chart | what it deploys |
|---|---|
| `lerobot-agent-console` | the console StatefulSet + its headless Service. Dev and test are the **same chart**, differing only by `nameOverride`. |
| `livekit` | the LiveKit SFU: Deployment + ConfigMap + its own public CLB. |

## Install / upgrade

```bash
# dev
helm upgrade --install lerobot-console      charts/lerobot-agent-console
# test — same chart, one value different
helm upgrade --install lerobot-console-test charts/lerobot-agent-console -f charts/lerobot-agent-console/values-test.yaml

# livekit: nodeIp is REQUIRED (the CLB's public IP; the chart refuses to render without it)
helm upgrade --install livekit charts/livekit --set nodeIp=$(kubectl get svc livekit-clb -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
```

Bump the image without touching the chart:

```bash
helm upgrade lerobot-console charts/lerobot-agent-console --set image.tag=<commit-sha> --reuse-values
```

## Adopting the consoles that are already running

They were created with `kubectl apply`, so Helm does not own them yet — and because
`persistence.size` is now `1Ti` while their (immutable) templates still say `128Gi`, a plain
upgrade is **rejected**:

    The StatefulSet "lerobot-console" is invalid: spec: Forbidden: updates to statefulset spec
    for fields other than 'replicas', 'ordinals', 'template', ... are forbidden

So adoption needs the StatefulSet object recreated. `--cascade=orphan` deletes only that object
and **leaves the pods running and the PVCs intact**; the new StatefulSet then adopts the same
pods by selector, and reuses the existing `hermes-home-*` PVCs because they already exist:

```bash
kubectl delete sts lerobot-console --cascade=orphan     # pods keep running
helm upgrade --install lerobot-console charts/lerobot-agent-console
kubectl rollout status sts/lerobot-console
```

⚠️ Between those two commands nothing is managing the pods — if one dies in that window it is
not recreated. Keep it to seconds, and do the two consoles one at a time.

For the Service (no immutable problem) plain adoption is enough:

```bash
kubectl annotate svc lerobot-console meta.helm.sh/release-name=lerobot-console --overwrite
kubectl annotate svc lerobot-console meta.helm.sh/release-namespace=default    --overwrite
kubectl label    svc lerobot-console app.kubernetes.io/managed-by=Helm         --overwrite
```

**Check with the right command.** `kubectl diff` writes an immutable-field rejection to
**stderr** and exits 2 — a pipeline that discards stderr reports "no differences" for a change
that cannot be applied at all. Use an apply dry-run, which says so on stdout:

```bash
helm template lerobot-console charts/lerobot-agent-console | kubectl apply --dry-run=server -f -
```

## Two immutable fields that will bite

**`volumeClaimTemplates` cannot change on an existing StatefulSet.** `persistence.size` is
`1Ti`, which is what the bound PVCs actually are (spec and status both). The live StatefulSet
templates still say `128Gi` because the disks were grown by editing the PVCs directly, and that
does not update the template — the same drift that made `kubectl apply -f k8s/statefulset.yaml`
fail. A fresh install gets 1Ti with no ceremony; the already-running consoles need the
orphan-delete above. To grow a disk later, edit the PVC **and** this value, or the next
recreate silently goes back to the smaller size.

**`spec.selector` cannot change either.** The live consoles use a bare `app: <name>`, so
`console.selectorLabels` emits exactly that. Adding the conventional `app.kubernetes.io/*` or
`helm.sh/chart` labels there would break every upgrade — put extra labels on metadata instead.

## Secrets are not in the charts

Both charts reference Secrets by name and never create them:

```bash
kubectl create secret generic lerobot-console-auth --from-literal=user=<u> --from-literal=password=<p>
kubectl create secret generic livekit-auth --from-literal=keys='<api_key>: <api_secret>'
```

`livekit` has **no credential fallback on purpose** — that SFU sits on a public CLB, so a key
committed to this repo would be readable by anyone who can read the repo. A missing Secret holds
the pod in `CreateContainerConfigError`, which is the intended failure.

## Publishing to the OCI registry

The registry accepts charts alongside images:

```bash
helm registry login iaas-us-cn-beijing.cr.volces.com -u <user>
helm package charts/lerobot-agent-console --version <chart-version>
helm push lerobot-agent-console-<chart-version>.tgz oci://iaas-us-cn-beijing.cr.volces.com/physicalai
```

Bump `version:` in `Chart.yaml` for any template change — pushing over an existing version is
how you get two different charts answering to one number.

The chart is named after the product, so it lands in the **same repo as the image**
(`physicalai/lerobot-agent-console`). They coexist: image tags are 40-char commit SHAs, chart
tags are semver, so they cannot collide. Note the chart name is NOT the resource name — the
running objects are `lerobot-console` / `lerobot-console-test`, pinned by `nameOverride` so
that installing under any release name still targets the right console.

## Not converted

`k8s/apig-ingress*.yaml` and `k8s/apig-instance-test.yaml` stay as raw manifests. They create
Volcengine `APIGInstance` CRDs that own **provisioned gateways with public domains**; a stray
`helm uninstall` would delete the gateway and the domain with it. They change roughly never, so
templating them buys nothing and risks a lot.
