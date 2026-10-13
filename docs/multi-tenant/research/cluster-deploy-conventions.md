===== deploy-conventions
The home-cluster repo (clone at commit 0bff2f8, 2026-10-13) confirms the owner's platform claims: Kubernetes v1.36.5 is the version tuppr is set to install, Cilium is chart 1.20.2, Envoy Gateway is chart 1.9.2 with Envoy v1.39.3, and the Gateway API experimental CRDs are v1.6.3. The repo shows only the desired state, not what is running. The 8–24 GB browser budget is not written down anywhere; the repo only says there are 3 nodes with 96 GB each. There are no ResourceQuota, LimitRange, PriorityClass, ValidatingAdmissionPolicy or Pod Security Admission labels anywhere, and Kyverno is not deployed. Every one of those guardrails would be new.

How apps are deployed: Flux runs cluster-meta (CRDs and repositories) and then cluster-apps, which picks up every namespace directory under kubernetes/apps. Each namespace directory pulls in components/common. Every app has a Flux Kustomization (ks.yaml: dependsOn, components, postBuild vars, targetNamespace, wait) pointing at an app/ folder. The HelmRelease usually uses bjw-s app-template 5.3.0. cluster-apps patches into every HelmRelease: CRDs replaced on install and upgrade, a failed upgrade rolled back, and a postBuild substitution from cluster-secrets.

Routes are app-template 'route' entries attached to envoy-internal or envoy-external in the networking namespace. On envoy-internal, Authelia checks every request through a Gateway-wide SecurityPolicy that fails closed and passes Remote-User/Remote-Groups/Remote-Name/Remote-Email to the app. envoy-external has no auth in front.

Secrets come from ExternalSecrets that extract 1Password items by name from the ClusterSecretStore 'onepassword'. That store has no namespace restrictions.

Network policies follow one pattern: a Kubernetes NetworkPolicy that admits only the gateway's Envoy pods, plus a CiliumNetworkPolicy that admits the 'kube-apiserver' entity, because Envoy traffic from another node is SNATed to a node IP. Egress allows DNS plus the whole internet except private ranges.

ferry-watch today is one app-template pod in the default namespace. It runs ghcr.io/mchestr/ferry-watch:0.5.1 (tag only, no digest) with a 4Gi memory limit and a 1Gi VolSync PVC. Dashboard and VNC share one origin: ferry.chestr.dev, with /vnc going to port 6080. It is on envoy-internal, where the Authelia rules allow only the admin group (two-factor). Secrets come from the 1Password items 'ferry-watch' and 'pushover'.

Findings that matter most for the multi-tenant plan:
- (a) Any namespace created the standard way gets a Flux GitHub token, ghcr/gitlab pull credentials and the Cloudflare IDs as Secrets from components/common. So the browser namespace must not use that pattern.
- (b) The certificate covers only chestr.dev and *.chestr.dev, so every VNC origin must be a one-label subdomain.
- (c) external-dns and gatus-sidecar (--auto-httproute) react to every HTTPRoute. Per-trip hostnames would create and delete DNS records and monitoring checks for each trip.
- (d) The trusted-header identity only exists on envoy-internal, which is LAN/VPN only. Invited users on the internet need envoy-external and app-owned auth such as OIDC through Authelia.
- (e) smtp-relay accepts mail only from Authelia and LLDAP pods, and only from @chestr.dev or @controlforensics.com senders.
- (f) openclaw has cluster-admin and can therefore read every Secret.

The operators deployed here are third-party, not the owner's own. litellm-operator and tuppr come from home-operations: OCI Helm charts on ghcr, CRDs in the chart, v1alpha1 API groups. litellm-operator's GOMEMLIMIT setting says it is written in Go. The dragonfly operator is deployed with app-template, raw RBAC and the upstream CRD URL. No operator by the owner (mchestr) is in the repo. The ferry-watch app itself is Python (aiohttp and Playwright).

## verify: kubernetes version
The tuppr KubernetesUpgrade pins kubelet v1.36.5, and the TalosUpgrade pins Talos v1.13.10.
refs: kubernetes/apps/system-upgrade/tuppr/upgrades/kubernetes.yaml:7-9, kubernetes/apps/system-upgrade/tuppr/upgrades/talos.yaml:7-9
=> 'Kubernetes >= 1.36' is confirmed as the desired state; the running version is not in the repo. ValidatingAdmissionPolicy (GA since 1.30) can be used. Whether hostUsers:false (user namespaces) works on this Talos is not in the repo.

## verify: cilium
Cilium chart 1.20.2 with values from a ConfigMap: kubeProxyReplacement true, routingMode native, bpf masquerade, envoy.enabled false, gatewayAPI disabled, hubble enabled through the HelmRelease. Pod CIDR is 10.244.0.0/16.
refs: kubernetes/apps/kube-system/cilium/app/helmrelease.yaml:13-21, kubernetes/apps/kube-system/cilium/app/resources/values.yaml:29-40,64, talos/controlplane.yaml:122
=> CiliumNetworkPolicy is available. No toFQDNs policy exists anywhere yet, so locking egress to bcferries.com is a new pattern. The existing DNS egress rule has no L7 'dns' rules (ferry-watch networkpolicy.yaml:53-60), so it would need them before any FQDN policy can work.

## verify: envoy gateway / gateway api
Envoy Gateway chart 1.9.2 with Envoy proxy v1.39.3, deploy type GatewayNamespace (proxies run in the networking namespace). Gateway API v1.6.3 experimental CRDs are installed twice: by Flux cluster-meta and by Talos extraManifests.
refs: kubernetes/apps/networking/envoy-gateway/app/ocirepository.yaml:12-14, kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:29, kubernetes/apps/networking/envoy-gateway/app/helmrelease.yaml:16-22, kubernetes/flux/meta/crds/kustomization.yaml:5-6, talos/controlplane.yaml:133-135
=> Confirmed. Browser-pod NetworkPolicies can select Envoy pods in the networking namespace by the label gateway.envoyproxy.io/owning-gateway-name.

## verify: memory budget
No memory budget for browser pods is written anywhere. The repo says 3x MS-01 nodes with 96GB each. There are no ResourceQuota, LimitRange or PriorityClass objects (only system-*-critical used by rook csi). Today ferry-watch has a 512Mi request and a 4Gi limit for 'one browser per watched trip, up to six'.
refs: AGENTS.md:7, kubernetes/apps/default/ferry-watch/app/helmrelease.yaml:53-59, kubernetes/apps/rook-ceph/rook-ceph/csi-drivers/helmrelease.yaml:29-31
=> 8-24 GB: not in repo. The plan has to encode the cap itself, as a ResourceQuota on the browser namespace and the operator's own admission check.

## flux entrypoint
kubernetes/flux/cluster/ks.yaml defines cluster-meta (path kubernetes/flux/meta: CRDs and repositories, wait: true) and cluster-apps (path ./kubernetes/apps, dependsOn cluster-meta, wait: false). There is no top-level apps kustomization; new namespace directories are discovered automatically.
refs: kubernetes/flux/cluster/ks.yaml:1-33, AGENTS.md:101
=> A new namespace only needs a new directory kubernetes/apps/<ns>/ with a kustomization.yaml; nothing else has to be registered.

## flux patches injected into every Kustomization/HelmRelease
cluster-apps patches every child Kustomization with postBuild.substituteFrom Secret cluster-secrets. It also patches every HelmRelease: install.crds CreateReplace with retries -1; upgrade cleanupOnFail, crds CreateReplace, retries 2, remediateLastFailure; uninstall keepHistory false.
refs: kubernetes/flux/cluster/ks.yaml:34-85, AGENTS.md:107
=> CRDs in a chart's crds/ folder are replaced on every upgrade. Do not add remediation blocks. If the new control plane migrates a database, set upgrade.strategy.name: RetryOnFailure, as Authelia does (authelia/app/helmrelease.yaml:12-15). Any ${...} in the manifests or HelmRelease values is substituted by Flux.

## postBuild escaping
Literal ${...} must be written as $$ (openclaw), or the generated ConfigMap gets the annotation kustomize.toolkit.fluxcd.io/substitute: disabled (smtp-relay).
refs: kubernetes/apps/ai/openclaw/app/helmrelease.yaml:41,51-56,133, kubernetes/apps/default/smtp-relay/app/kustomization.yaml:8-11
=> Any shell or JS template in the ferry-watch HelmRelease values or ConfigMaps needs this escaping.

## namespace pattern / components/common
Each namespace directory has a kustomization.yaml with `namespace: <ns>`, `components: [../../components/common]` and a list of ./<app>/ks.yaml. common creates the Namespace (renamed from not-used, prune disabled), Flux Alerts/Providers (alertmanager and a GitHub commit-status provider), the app-template OCIRepository, and ExternalSecrets for cluster-secrets (CLOUDFLARE_ACCOUNT_ID/TUNNEL_ID), ghcr-docker-secret, gitlab-docker-secret and github-token-secret (FLUX_GITHUB_TOKEN).
refs: kubernetes/apps/default/kustomization.yaml:1-29, kubernetes/apps/ai/kustomization.yaml:1-12, kubernetes/components/common/kustomization.yaml:1-8, kubernetes/components/common/namespace.yaml:1-7, kubernetes/components/common/secrets/cluster-secrets.yaml:1-22, kubernetes/components/common/alerts/github/externalsecret.yaml:1-19
=> Any namespace built this way holds a GitHub token, registry credentials and Cloudflare IDs as Secrets. An operator that can create pods in that namespace can mount them. The browser namespace must not include components/common; give it its own Namespace manifest (with the prune-disabled annotation).

## ks.yaml conventions
Every ks.yaml uses: metadata name &app and namespace &namespace; commonMetadata label app.kubernetes.io/name; optional components (../../../../components/...); dependsOn on cross-namespace Kustomizations (onepassword-store/external-secrets, rook-ceph-cluster/rook-ceph, volsync/volsync-system, cloudnative-pg-cluster/database, dragonfly/database); interval 30m; postBuild.substitute APP plus component variables; prune true; retryInterval 1m; sourceRef GitRepository flux-system; targetNamespace *namespace; timeout 5m. Apps use wait: false; operators use wait: true so dependents can wait on them (litellm dependsOn litellm-operator).
refs: kubernetes/apps/default/ferry-watch/ks.yaml:1-34, kubernetes/apps/ai/memini/ks.yaml:1-41, kubernetes/apps/ai/litellm-operator/ks.yaml:1-21, kubernetes/apps/ai/litellm/ks.yaml:13-19, kubernetes/apps/database/dragonfly/ks.yaml:21
=> The new ferry-watch ks should set wait: true, because it installs CRDs. targetNamespace overrides the namespace of every object it applies, so anything that must land in the browser namespace needs its own Kustomization (targetNamespace: browser ns) or has to come from the chart.

## volsync component
Adds the PVC ${VOLSYNC_CLAIM:-${APP}} (ceph-block), populated through dataSourceRef from ReplicationDestination ${APP}-bootstrap (manual trigger restore-once), and a ReplicationSource (default daily 0 11 * * *, keeps 7 dailies). The restic repository is {{RESTIC_REPOSITORY_BUCKET}}/${APP}: keyed on APP only, not on the namespace. The mover runs as APP_UID/APP_GID (default 1000; ferry-watch sets 1001).
refs: kubernetes/components/volsync/pvc.yaml:1-16, kubernetes/components/volsync/r2/replicationdestination.yaml:1-23, kubernetes/components/volsync/r2/replicationsource.yaml:1-26, kubernetes/components/volsync/r2/externalsecret.yaml:24, kubernetes/apps/default/ferry-watch/ks.yaml:20-25
=> For the migration: if APP=ferry-watch is reused in a new namespace, the new PVC is restored automatically from the old default-namespace backup. If the old and new deployments overlap, two ReplicationSources write to the same restic repo. Take a snapshot first, then cut over, or use a different APP name.

## cnpg component
Adds ExternalSecrets ${APP}-initdb-secret and ${APP}-pguser-secret (host, ro_host, user, password, db, uri, async_uri, dsn), built from fields ${APP}_postgres_username/password in the 1Password item 'cloudnative-pg'. The default cluster is postgres17 in the database namespace. Also adds a 4-hourly pg_dump CronJob to NFS. No app that uses it today has a hyphen in APP, and the database name is ${APP}.
refs: kubernetes/components/cnpg/externalsecret.yaml:1-52, kubernetes/components/cnpg/cronjob.yaml:1-51, kubernetes/components/cnpg/README.md:9-21
=> A Postgres-backed multi-tenant control plane (users, invites, quotas, trips) fits this component. With APP=ferry-watch, the database and user would be named 'ferry-watch'; a hyphen is untested in this repo.

## dragonfly component
A Dragonfly CR named ${APP}-dragonfly (DRAGONFLY_REPLICAS default 1) with a NetworkPolicy that admits port 6379 from any pod in the same namespace, plus a PodMonitor.
refs: kubernetes/components/dragonfly/cluster.yaml:1-33, kubernetes/components/dragonfly/networkpolicy.yaml:1-27
=> Available if the control plane needs Redis-style pub/sub or sessions. Because of the same-namespace rule, browser pods must not share a namespace with it.

## app-template HelmRelease conventions
chartRef is OCIRepository app-template (tag 5.3.0); interval 30m; maxHistory 2. Controllers carry the annotation reloader.stakater.com/auto 'true'. Containers set image repository/tag, envFrom secretRef, and custom startup/liveness/readiness probes on a real health endpoint. Resources set a memory limit but no CPU limit. Container securityContext: allowPrivilegeEscalation false, readOnlyRootFilesystem true, drop ALL. defaultPodOptions.securityContext: runAsUser/runAsGroup/fsGroup, runAsNonRoot, fsGroupChangePolicy OnRootMismatch, seccomp RuntimeDefault. app-template v5 sets automountServiceAccountToken false by default. Talos makes kubelet default to RuntimeDefault seccomp.
refs: kubernetes/components/common/repos/app-template/ocirepository.yaml:6-14, kubernetes/apps/default/ferry-watch/app/helmrelease.yaml:6-76, kubernetes/apps/default/saidcompany/app/helmrelease.yaml:13-87, kubernetes/apps/database/dragonfly/app/helmrelease.yaml:54-67, talos/controlplane.yaml:24, AGENTS.md:105-107
=> The current pod spec already meets Pod Security 'restricted', so the browser namespace can enforce restricted. The operator's browser pod template should copy these settings: automountServiceAccountToken false, read-only root filesystem, emptyDir /tmp and Memory-medium /dev/shm.

## routes
Routes are declared under app-template `route.<name>`: hostnames; parentRefs {name: envoy-internal|envoy-external, namespace: networking, sectionName: https}; rules with matches/backendRefs; annotations gethomepage.dev/* and gatus.home-operations.com/endpoint. A standalone HTTPRoute file is also used (windmill). Both Gateways' https listeners allow routes from any namespace. TLS uses chestr-dev-tls (chestr.dev, *.chestr.dev) and mikeflix-lol-tls.
refs: kubernetes/apps/default/ferry-watch/app/helmrelease.yaml:87-112, kubernetes/apps/default/saidcompany/app/helmrelease.yaml:96-110, kubernetes/apps/default/windmill/app/httproute.yaml:1-24, kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:45-120, kubernetes/apps/networking/envoy-gateway/app/certificate.yaml:1-13
=> Routes created by the operator in a browser namespace can attach to the gateways with no Gateway change. Their hostnames must be one-label subdomains of chestr.dev, or a new Certificate plus a certificateRef on both listeners is needed. Because any namespace can attach routes, nothing stops an operator from claiming another app's hostname (for example auth.chestr.dev); that needs a guardrail.

## internal vs external auth
SecurityPolicy internal-secure targets the whole envoy-internal Gateway: extAuth to Service authelia (default namespace, port 80) at /api/authz/ext-authz/, failOpen false, headersToBackend Remote-User, Remote-Groups, Remote-Name, Remote-Email. A ReferenceGrant in default allows the cross-namespace reference. envoy-external has no auth policy. AGENTS.md: internal is LAN/VPN only; external must do its own authentication; never put admin UIs or shells there.
refs: kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:122-151, kubernetes/apps/default/authelia/app/referencegrant.yaml:1-14, AGENTS.md:111-121, docs/src/notes/networking.md:192-207
=> Trusted-header identity is only safe on envoy-internal. On envoy-external a client can forge Remote-User, so external mode must turn header trust off (for example OIDC through Authelia). An ext-auth backed by the control plane for VNC routes would need a ReferenceGrant like Authelia's.

## authelia access rules today
ferry.chestr.dev is not listed. It falls through to `*.chestr.dev` with subject group:admin and policy two_factor, and everything else is denied. Users and groups come from LLDAP; groups are not declared in git. Authelia is also an OIDC provider with per-app clients; secrets are generated with the procedure in the README and stored in the 1Password item 'authelia'.
refs: kubernetes/apps/default/authelia/app/resources/configuration.yaml:5-22,45-104,114-214, kubernetes/apps/default/authelia/app/README.md:1-23
=> Today only admins can reach ferry-watch. Invited users need an LLDAP group (for example 'ferry') plus an access rule for the ferry and VNC hostnames (internal mode), or an OIDC client named ferry-watch (external mode).

## dns + monitoring side effects of HTTPRoutes
external-dns-cloudflare watches HTTPRoutes attached to envoy-external (records proxied). external-dns-unifi watches gateway-httproute and service with triggerLoopOnEvent. cloudflared sends *.chestr.dev to envoy-external. gatus-sidecar runs with --auto-httproute, which creates a Gatus endpoint for every HTTPRoute.
refs: kubernetes/apps/networking/external-dns/cloudflare/helmrelease.yaml:29-44, kubernetes/apps/networking/external-dns/unifi/helmrelease.yaml:46-51, kubernetes/apps/networking/cloudflared/app/resources/config.yaml:5-20, kubernetes/apps/observability/gatus/app/helmrelease.yaml:19-27
=> Per-trip hostnames would add and remove DNS records (Cloudflare or UniFi) and Gatus checks while a time-limited cart hold is running. One fixed VNC hostname with per-trip HTTPRoutes split by path avoids both. The gatus-sidecar opt-out annotation is not in the repo; check its docs.

## gateway timeouts / client IP
BackendTrafficPolicy sets requestTimeout 0s, compression and a 502/503/504 override page. ClientTrafficPolicy sets requestReceivedTimeout 0s, http3, TLS 1.2 minimum, and trusts X-Forwarded-For only from 10.42.0.0/16, while podSubnets is 10.244.0.0/16.
refs: kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:153-219, talos/controlplane.yaml:122
=> Long-lived VNC WebSockets are not cut off by the gateway. The trusted CIDR does not match the pod CIDR, so client IPs behind cloudflared are probably not recovered. Any per-IP rate limiting on login or invite redemption on envoy-external would see the proxy's IP. The 503 override page will also replace any 503 the app returns itself.

## networkpolicy pattern
There are only 3 Kubernetes NetworkPolicies and 2 CiliumNetworkPolicies in the repo (ferry-watch, openclaw, dragonfly component), plus app-template `networkpolicies:` blocks in paperless, home-assistant and smtp-relay. The pattern: a NetworkPolicy admitting pods in the networking namespace with gateway.envoyproxy.io/owning-gateway-name=<gw>, plus a CiliumNetworkPolicy fromEntities kube-apiserver on the same ports ('cross-node Envoy traffic gets SNATed to the node IP... Cilium classifies their IPs as kube-apiserver'). ferry-watch egress is kube-dns:53 plus toCIDRSet 0.0.0.0/0 except 10/8, 172.16/12, 192.168/16, 100.64/10, 169.254/16. Comments in openclaw's CiliumNetworkPolicy say kube-apiserver is not covered by 'world'. There is no default-deny or CiliumClusterwideNetworkPolicy anywhere.
refs: kubernetes/apps/default/ferry-watch/app/networkpolicy.yaml:1-69, kubernetes/apps/ai/openclaw/app/networkpolicy.yaml:1-57, kubernetes/apps/default/paperless/app/helmrelease.yaml:229-265, kubernetes/apps/default/home-assistant/app/helmrelease.yaml:166-186
=> The browser namespace needs a new default-deny. The kube-apiserver entity allowance means a NetworkPolicy cannot limit the VNC port to Envoy alone; node-SNATed traffic gets in too. Per-trip VNC therefore needs its own authorization (token or route authz), not just Authelia plus a NetworkPolicy. The operator Deployment needs explicit egress to the kube-apiserver entity.

## vnc today
KasmVNC runs with -DisableBasicAuth -SecurityTypes None on :6080, and the HelmRelease relies on Authelia plus the NetworkPolicy ('the remote viewer has no password'). VNC is served on the same origin as the dashboard (/vnc), and BCF_VNC_URL points there.
refs: entrypoint.sh:20-27, kubernetes/apps/default/ferry-watch/app/helmrelease.yaml:25-27,96-112
=> The new design's 'own origin, only while holding a cart' needs a per-trip credential (KasmVNC auth or route authz) and a host-only dashboard session cookie. Authelia's cookie is scoped to the whole chestr.dev domain (configuration.yaml:37-39), so it is sent to every subdomain.

## email path
smtp-relay (maddy) listens on smtp-relay.default.svc.cluster.local:2525 with no auth and relays to AWS SES. It accepts sources chestr.dev and controlforensics.com and rejects everything else. Its ingress NetworkPolicy admits only the authelia and lldap pods in default, plus metrics scraping from observability. The policy is named 'paperless', apparently copied from that app.
refs: kubernetes/apps/default/smtp-relay/app/resources/maddy.conf:9-24, kubernetes/apps/default/smtp-relay/app/helmrelease.yaml:89-128, kubernetes/apps/default/authelia/app/helmrelease.yaml:57
=> Email alerts can use the in-cluster relay with a From address at @chestr.dev, but smtp-relay's NetworkPolicy has to be changed in this repo to admit the ferry-watch control-plane pods.

## secrets
ClusterSecretStore 'onepassword' (1Password Connect, vault 'Kubernetes') has no namespace conditions. ExternalSecret convention: name &app, target <app>-secret, creationPolicy Owner, template engineVersion v2, dataFrom.extract.key = 1Password item name. ferry-watch extracts the items 'ferry-watch' (BCF_USER, BCF_PASS) and 'pushover' (PUSHOVER_USER_KEY, PUSHOVER_GATUS_TOKEN mapped to PUSHOVER_TOKEN) into ferry-watch-secret. The docs page still names the old store 'onepassword-connect'.
refs: kubernetes/apps/external-secrets/onepassword/store/clustersecretstore.yaml:1-17, kubernetes/apps/default/ferry-watch/app/externalsecret.yaml:1-24, docs/src/notes/secrets-management.md:23-40
=> An ExternalSecret in any namespace can read any item in the vault. The operator ServiceAccount must never be able to create externalsecrets or pushsecrets. In the new design the pushover extract goes away, BCF credentials become per-user and leave 1Password, and new keys (VAPID, session/signing) go into the 'ferry-watch' item.

## cluster-admin workloads
openclaw's ServiceAccount is bound to the cluster-admin ClusterRole, and its egress allows cluster, kube-apiserver and world.
refs: kubernetes/apps/ai/openclaw/app/rbac.yaml:1-18, kubernetes/apps/ai/openclaw/app/networkpolicy.yaml:53-57
=> Anything a browser pod keeps in a Kubernetes Secret can be read by another workload in the cluster. To meet 'secrets never leave browser pods', keep user credentials only in the pod's memory or emptyDir, never in Secrets.

## image pinning & renovate
38 of 108 `tag:` lines use @sha256. The owner's own images are mixed: saidcompany-api and plex-manager use `main@sha256:...`, while ferry-watch (0.5.1) and gotenberg use plain semver. OCIRepository chart refs use plain tags (memini v0.7.32, litellm-operator 0.0.24). Renovate runs the flux, helm-values and kubernetes managers on kubernetes/**, plus regex '# renovate: datasource= depName=' comments. It automerges minor/patch/digest after 2 days, but images on non-Docker-Hub registries have no age gate. 0.x minor bumps always open a PR. Core infrastructure and apps with DB migrations (immich, windmill, postgres) always open a PR.
refs: kubernetes/apps/default/saidcompany/app/helmrelease.yaml:33-34, kubernetes/apps/media/subs/app/helmrelease.yaml:43-44, kubernetes/apps/default/ferry-watch/app/helmrelease.yaml:21-22, .github/renovate.json5:30-50, .github/renovate/customManagers.json5:14-29, .github/renovate/autoMerge.json5:3-69
=> Pin both images (control plane and browser) as X.Y.Z@sha256:... in the HelmRelease values, where the helm-values manager can update them. The admission policy can then allow exactly that digest. Once DB migrations exist, add '/ferry-watch/' to the always-PR list.

## CI constraint
The flux-local workflow runs `flux-local test --enable-helm --all-namespaces` on PRs and on renovate/** pushes, rendering every HelmRelease.
refs: .github/workflows/flux-local.yaml:31-49
=> A ferry-watch chart pulled from ghcr must be anonymously pullable (a public package), or every home-cluster PR fails.

## CRD install patterns
Five patterns are in use. (a) Cluster-wide CRDs in kubernetes/flux/meta/crds, currently only Gateway API, applied before all apps. (b) CRDs in a chart's crds/ folder, replaced on every upgrade by the cluster patch (litellm-operator). (c) The raw upstream CRD URL in the app kustomization with a renovate comment (dragonfly). (d) A separate '-crds' chart HelmRelease with HelmRelease.dependsOn (silence-operator; note its ks dependsOn points the other way). (e) Talos extraManifests (Gateway API, prometheus-operator CRDs).
refs: kubernetes/flux/meta/crds/kustomization.yaml:1-6, kubernetes/flux/cluster/ks.yaml:68-74, kubernetes/apps/database/dragonfly/app/kustomization.yaml:5-9, kubernetes/apps/observability/silence-operator/app/helmrelease.yaml:13-15, kubernetes/apps/observability/silence-operator/ks.yaml:34-36, talos/controlplane.yaml:133-139
=> The simplest fit is pattern (b): ship the ferry-watch CRDs in the chart's crds/ folder. Flux then creates and replaces them, and flux/meta/crds stays reserved for cluster-wide APIs.

## existing operators / preferred stack
litellm-operator: OCI chart oci://ghcr.io/home-operations/charts/litellm-operator 0.0.24 with its own app/ocirepository.yaml. Values: controller.leaderElection, webhook.enabled, env GOMEMLIMIT=200MiB (a Go runtime setting), and a comment that it 'caches every Secret cluster-wide'. CRDs litellm.home-operations.com/v1alpha1 (LiteLLMProxy, LiteLLMModel), with the CRs kept in git. tuppr follows the same pattern (ghcr.io/home-operations/charts/tuppr 0.5.7, tuppr.home-operations.com/v1alpha1). The dragonfly operator is deployed with app-template, a raw ClusterRole/Binding and upstream CRDs. No owner-authored (mchestr) operator or API group exists in the repo, and the shallow clone has no history to show who wrote these.
refs: kubernetes/apps/ai/litellm-operator/app/ocirepository.yaml:1-13, kubernetes/apps/ai/litellm-operator/app/helmrelease.yaml:8-32, kubernetes/apps/ai/litellm/app/litellmproxy.yaml:1-7, kubernetes/apps/ai/litellm/app/models/memini-chat.yaml:1-14, kubernetes/apps/system-upgrade/tuppr/app/ocirepository.yaml:1-13, kubernetes/apps/database/dragonfly/app/rbac.yaml:1-46, pyproject.toml:1-12
=> The style used here: a controller-runtime-like operator (leader election, webhook) shipped as a versioned OCI Helm chart on ghcr with CRDs in the chart and a v1alpha1 group, consumed through an app-local OCIRepository plus HelmRelease. Whether the owner writes Go or Python (kopf, to share the Python agent code) is not in the repo.

## admission / pod security
No pod-security.kubernetes.io labels exist anywhere, and the Talos config has no admissionControl override (only disablePodSecurityPolicy). A Kyverno HelmRepository is defined but no Kyverno app is deployed (0 references under kubernetes/apps).
refs: talos/controlplane.yaml:150-155, kubernetes/flux/meta/repositories/helm/kyverno.yaml:1-10
=> Guardrails have to use built-in ValidatingAdmissionPolicy plus Pod Security Admission namespace labels. There is no policy engine to rely on. The default Pod Security level the cluster applies is not in the repo.

## scheduling
maxPods is 150 per node. All 3 nodes are control plane with scheduling allowed. The scheduler adds a default PodTopologySpread on hostname with ScheduleAnyway.
refs: talos/controlplane.yaml:17,162-191
=> Browser pods spread across m0-m2 by default. There is no PriorityClass precedent, so a low or negative class with preemptionPolicy: Never would be new, and would let home-critical apps win under memory pressure.

## current ferry-watch deployment
Flux Kustomization ferry-watch in the default namespace (listed in default/kustomization.yaml:11), with the volsync component (APP_UID/GID 1001, 1Gi) and dependsOn onepassword-store and rook-ceph-cluster (not volsync, unlike memini and openclaw). Its app/ folder has externalsecret.yaml, helmrelease.yaml and networkpolicy.yaml. The HelmRelease runs a single controller with Chromium's sandbox off ('the pod boundary is the sandbox'), shm as a 1Gi Memory emptyDir, tmp as a 1Gi emptyDir, the PVC mounted as config, and a homepage annotation on the route.
refs: kubernetes/apps/default/ferry-watch/ks.yaml:1-34, kubernetes/apps/default/kustomization.yaml:11, kubernetes/apps/default/ferry-watch/app/kustomization.yaml:1-7, kubernetes/apps/default/ferry-watch/app/helmrelease.yaml:66-126
=> Migration means removing this Kustomization (prune: true deletes the HelmRelease, ExternalSecret, NetworkPolicies, PVC and VolSync objects) after its state has been captured or imported.

RECOMMENDATIONS:
- Namespaces: create a new directory kubernetes/apps/ferry-watch/ with the standard kustomization.yaml (namespace: ferry-watch, components: [../../components/common], resources: [./ferry-watch/ks.yaml]) for the control plane. Do not stay in 'default': it is shared with Authelia, LLDAP, smtp-relay and about 20 other apps, so RBAC, quota and NetworkPolicy scoping would bleed across.
- Add a second namespace, ferry-watch-browsers, that does NOT include components/common. Common would drop github-token-secret, the ghcr/gitlab pull credentials and cluster-secrets into it, and an operator with pod-create rights there could mount them. Give it its own Namespace manifest with kustomize.toolkit.fluxcd.io/prune: disabled and pod-security.kubernetes.io/enforce: restricted. Deliver it through a second Flux Kustomization in kubernetes/apps/ferry-watch/ferry-watch/ks.yaml (name ferry-watch-browsers, path ./kubernetes/apps/ferry-watch/ferry-watch/browsers, targetNamespace ferry-watch-browsers, dependsOn ferry-watch), or let the chart template it.
- Packaging: publish ferry-watch's own Helm chart from the app repo as a public OCI chart, for example oci://ghcr.io/mchestr/charts/ferry-watch. Consume it the memini/litellm-operator way: app/ocirepository.yaml with ref.tag, and a HelmRelease with chartRef kind OCIRepository name ferry-watch, interval 30m, maxHistory 2. app-template cannot cleanly carry CRDs, ValidatingAdmissionPolicies or a Role in a second namespace. Building the chart on the bjw-s common library would keep the values shape the owner already uses (controllers/route/networkpolicies). Put CRDs in the chart's crds/ folder; the cluster patch already sets CreateReplace. Set the ks to wait: true.
- Split of responsibilities. The chart (generic, versioned with the operator's pod template) holds: the CRDs; the operator Deployment, Service and ServiceAccount; a Role/RoleBinding in the browser namespace for pods, services and httproutes only, with no secrets, configmaps, pods/exec, pods/attach, pods/portforward, pods/log, pods/ephemeralcontainers, or external-secrets.io/* (externalsecrets, pushsecrets); a ValidatingAdmissionPolicy plus binding scoped to the operator ServiceAccount; a default-deny NetworkPolicy and ResourceQuota/LimitRange for the browser namespace built from values; and an optional low-priority PriorityClass. The policy should pin browser images to an allowed digest and forbid Secret, projected-token and hostPath volumes, automountServiceAccountToken, privilege, and missing required labels. It should also force HTTPRoute hostnames to match the configured VNC host, parentRefs to the configured gateway and listener, and backendRefs to the same namespace. This repo (values plus small manifests) holds: the memory cap (quota within the 8-24Gi budget), hostnames, gateway names, the FQDN allowlist, the Cilium-only policies (toFQDNs egress and the kube-apiserver-entity SNAT allowance) unless the chart has a cilium.enabled toggle, the Authelia rule or OIDC client, the smtp-relay NetworkPolicy change, and the ExternalSecret.
- Browser-namespace network policy: ingress only on the VNC port, from pods in the networking namespace with owning-gateway-name=<chosen gateway>, plus fromEntities kube-apiserver for the SNAT quirk. No ingress at all from the ferry-watch namespace, so the control plane cannot connect into browser pods. Egress: kube-dns with Cilium L7 dns rules, toFQDNs bcferries.com and the checkout dependencies, and only the control plane's report port in ferry-watch. Control-plane policy: ingress from Envoy and from the browser namespace on the report port. Egress to the kube-apiserver entity (the operator needs it; openclaw's comments say 'world' does not cover it), postgres17 in the database namespace, smtp-relay.default:2525, and world:443 for Web Push endpoints.
- VNC routing: since NetworkPolicy cannot keep node-SNATed traffic off the VNC port, give every trip's VNC a per-trip credential (KasmVNC auth, or a route-level ext-auth that asks the control plane 'does this user own trip X and does it hold a cart'). Use a one-label hostname covered by the existing *.chestr.dev certificate. Prefer one fixed VNC origin, for example ferry-vnc.chestr.dev, with per-trip HTTPRoutes split by path in the browser namespace, which avoids external-dns record churn and certificate changes. Use per-trip hostnames such as fwv-<id>.chestr.dev only if strict per-trip origins are required. Routes created by the operator must carry no homepage annotations and must opt out of gatus-sidecar auto-discovery.
- Identity and exposure: if invited users are on the LAN or WireGuard, stay on envoy-internal and add an Authelia rule (subject group:ferry, an LLDAP group) for the ferry and VNC hostnames, placed before the *.chestr.dev admin catch-all. Trusted Remote-* headers are valid there. If invited users are on the internet, move the dashboard route to envoy-external and have the app do OIDC against Authelia (add a client following authelia/app/README.md and store the secrets in the 1Password items 'authelia' and 'ferry-watch'). In that mode the app must ignore Remote-* headers, so make header trust a config switch that is enabled only for internal mode.
- Secrets: keep the 1Password item 'ferry-watch' as the one source for the control plane. Add VAPID public/private keys and subject, a session/signing secret, the SMTP From address at @chestr.dev, and the OIDC client id/secret later. Drop the 'pushover' extract, and drop BCF_USER/BCF_PASS once credentials are per-user. Never hand the operator create rights on ExternalSecrets, because the ClusterSecretStore has no namespace conditions.
- Database: if Postgres, use components/cnpg (dependsOn cloudnative-pg-cluster in database). Either accept a hyphenated database name for APP=ferry-watch (untested here) or pick a hyphen-free APP. Add ferry_watch_* fields to the 1Password item 'cloudnative-pg' first, set upgrade.strategy.name: RetryOnFailure, and add '/ferry-watch/' to the always-PR list in .github/renovate/autoMerge.json5.
- Image pinning: in the HelmRelease values, set both the control-plane image and the browser image as repository plus tag 'X.Y.Z@sha256:...', so the Renovate helm-values manager tracks them and the admission policy can allowlist the exact browser digest. Pass the browser image to the operator through values, not as a hard-coded default.
- Migration order: 1) Publish the chart and make the ghcr package public (flux-local CI renders it). 2) Run `task volsync:snapshot APP=ferry-watch NS=default` and export the current config/state for an import script. 3) Add kubernetes/apps/ferry-watch/ (control plane plus browser Kustomization), the Authelia or OIDC change, and the smtp-relay NetworkPolicy change in one PR, so flux-local shows the diff. 4) Cut over the hostname: remove './ferry-watch/ks.yaml' from kubernetes/apps/default/kustomization.yaml:11 and delete kubernetes/apps/default/ferry-watch/ in the same PR that brings up the new route on ferry.chestr.dev, or run the new stack on a temporary hostname first, because two HTTPRoutes cannot both own ferry.chestr.dev. 5) If VolSync is reused with APP=ferry-watch, the new PVC restores from the old restic repo; otherwise drop the volsync component and clean up the R2 path. 6) Remove the pushover keys from the 'ferry-watch' item afterwards.

GAPS:
- No ResourceQuota, LimitRange, PriorityClass, ValidatingAdmissionPolicy or Pod Security Admission labels exist anywhere in the repo; they are all new.
- No default-deny NetworkPolicy and no Cilium toFQDNs policy exist to copy. The existing DNS egress rule has no L7 dns rules that FQDN policies need.
- There is no Helm chart for ferry-watch. The app repo CI (.github/workflows/ci.yml) only builds and pushes the image.
- No certificate covers multi-label subdomains; only chestr.dev and *.chestr.dev (plus mikeflix.lol).
- No Authelia rule, LLDAP group or OIDC client exists for non-admin ferry-watch users.
- smtp-relay's NetworkPolicy does not admit ferry-watch, and maddy accepts only chestr.dev and controlforensics.com senders.
- No secrets exist for Web Push (VAPID) or session keys. Pushover is wired in through the 1Password item 'pushover'.
- There is no example of a route-level SecurityPolicy or of an ext-auth backed by an app, and no gatus-sidecar opt-out annotation for dynamically created HTTPRoutes.
- The 8-24 GB budget for browser pods is not recorded anywhere, and the running Kubernetes version (as opposed to the desired one) is not in the repo.
- No operator written by the owner (mchestr) is in the repo to use as a template. litellm-operator and tuppr come from home-operations, and the shallow clone has no history.

OPEN_QUESTIONS:
- Will invited users reach Ferry Watch only from the LAN/WireGuard (envoy-internal plus Authelia headers), or from the internet (envoy-external plus app-owned OIDC)? This decides the identity design and where routes attach.
- Does 'own origin' mean one VNC origin separate from the dashboard (one fixed hostname with per-trip paths and per-trip authorization), or a separate origin per trip (per-trip one-label hostnames, with DNS record and Gatus churn)?
- Should the guardrails (admission policy, browser NetworkPolicies, quota, PriorityClass) ship in the public chart for other self-hosters, or live only in home-cluster?
- Should the control plane use Postgres through components/cnpg (hyphenated APP name untested) or SQLite on a VolSync PVC? Will it run schema migrations, which means RetryOnFailure and always-PR in Renovate?
- Operator language: Go/controller-runtime to match litellm-operator and tuppr, or Python (kopf) to share code with the Python agent? The repo only shows that litellm-operator is Go (GOMEMLIMIT) and gives no authorship.
- Is ghcr.io/mchestr/ferry-watch public, and will the chart package be public? flux-local CI renders all HelmReleases without registry credentials.
- Can user namespaces (hostUsers: false) be enabled on this Talos/Kubernetes 1.36 so Chromium's sandbox can be turned back on? The repo has no admissionControl or user-namespace configuration.
- Should the ClientTrafficPolicy trusted X-Forwarded-For range (10.42.0.0/16) be fixed to the real pod CIDR (10.244.0.0/16) before relying on client IPs for login or invite rate limiting?
- Cutover: keep ferry.chestr.dev and switch in one PR, or run the new stack side by side on a temporary hostname first?
