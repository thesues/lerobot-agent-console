# Helm charts

Replaces the hand-maintained manifests in `k8s/` (which is gitignored, so those files only ever
existed on one laptop — a chart in git is the point of this move). Two charts:

| chart | what it deploys |
|---|---|
| `lerobot-console` | the console StatefulSet + its headless Service. Dev and test are the **same chart**, differing only by `nameOverride`. |
| `livekit` | the LiveKit SFU: Deployment + ConfigMap + its own public CLB. |

## Install / upgrade

```bash
# dev
helm upgrade --install lerobot-console      charts/lerobot-console
# test — same chart, one value different
helm upgrade --install lerobot-console-test charts/lerobot-console -f charts/lerobot-console/values-test.yaml

# livekit: nodeIp is REQUIRED (the CLB's public IP; the chart refuses to render without it)
helm upgrade --install livekit charts/livekit --set nodeIp=$(kubectl get svc livekit-clb -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
```

Bump the image without touching the chart:

```bash
helm upgrade lerobot-console charts/lerobot-console --set image.tag=<commit-sha> --reuse-values
```

## Adopting the consoles that are already running

They were created with `kubectl apply`, so Helm does not own them yet. Adoption is safe **only
because the rendered output currently matches the live objects field for field** — verified with
`kubectl diff`, which came back empty for both. Re-check before adopting:

```bash
helm template lerobot-console charts/lerobot-console | kubectl diff -f -   # expect: no output
```

Then hand ownership to Helm without recreating anything:

```bash
for k in Service StatefulSet; do
  kubectl annotate $k lerobot-console meta.helm.sh/release-name=lerobot-console --overwrite
  kubectl annotate $k lerobot-console meta.helm.sh/release-namespace=default   --overwrite
  kubectl label    $k lerobot-console app.kubernetes.io/managed-by=Helm        --overwrite
done
helm upgrade --install lerobot-console charts/lerobot-console
```

## Two immutable fields that will bite

**`volumeClaimTemplates` cannot change on an existing StatefulSet.** `persistence.size` is
`128Gi` because that is what the live templates say — even though the bound PVCs are actually
**1Ti**, because they were resized by editing the PVCs directly, which does not update the
template. Setting `1Ti` here would make `helm upgrade` fail on the running consoles (this is the
same reason `kubectl apply -f k8s/statefulset.yaml` had been failing). Raise it only for a fresh
install; to grow an existing disk, edit the PVC, not the chart.

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
helm package charts/lerobot-console --version <chart-version>
helm push lerobot-console-<chart-version>.tgz oci://iaas-us-cn-beijing.cr.volces.com/physicalai
```

Bump `version:` in `Chart.yaml` for any template change — pushing over an existing version is
how you get two different charts answering to one number.

## Not converted

`k8s/apig-ingress*.yaml` and `k8s/apig-instance-test.yaml` stay as raw manifests. They create
Volcengine `APIGInstance` CRDs that own **provisioned gateways with public domains**; a stray
`helm uninstall` would delete the gateway and the domain with it. They change roughly never, so
templating them buys nothing and risks a lot.
