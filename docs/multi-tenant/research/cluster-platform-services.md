===== platform-services
I checked the platform services in github.com/mchestr/home-cluster at commit 0bff2f8, dated 2026-10-13. The owner's version claims hold: Kubernetes v1.36.5, Talos v1.13.10, Cilium chart 1.20.2, Envoy Gateway 1.9.2, and 3 nodes with 96GB each. The 8-24 GB available for browser pods is not in the repo.

**Postgres.** Apps don't get their own CNPG Cluster or declarative managed roles. They share `postgres17` (3 instances on local `openebs-hostpath` disks) through `components/cnpg`. The component builds secrets from the 1Password item `cloudnative-pg`, and a `postgres-init` init container creates the role and database. 15 apps already use it. Backups come for free: continuous WAL plus daily base backups to R2 kept 30 days, and a pg_dump every 4 hours to NFS. One catch: the component copies the shared cluster's superuser password into a Secret in the app's namespace.

**Dragonfly.** Each app gets its own instance from a component, with no password and no persistence. Its NetworkPolicy lets any pod in the same namespace reach port 6379.

**VolSync.** Backups are opt-in per app through a component. Nothing applies them cluster-wide, so browser pods that use only emptyDir are never backed up. The current ferry-watch backs up its 1Gi config PVC this way.

**Storage classes.** Only `ceph-block` (default, 3 replicas) and `openebs-hostpath` exist. There is no CephFS, Longhorn or local-path.

**Monitoring and alerts.**
- Prometheus picks up every ServiceMonitor, PodMonitor and PrometheusRule in any namespace with no label needed, and keeps 14 days.
- Alertmanager sends everything to Pushover by default, warnings included. Only Watchdog and InfoInhibitor go elsewhere, and there is no email receiver.
- A cluster-wide `OomKilled` alert is set to critical, so browser pods that crash, get OOM-killed or sit Pending will send the owner Pushover alerts.

**Logs.** Fluent Bit collects the logs of every pod in the cluster, including pod labels and annotations, and sends them to VictoriaLogs (kept 14 days). Inside the cluster, VictoriaLogs answers queries with no authentication and has no NetworkPolicy. The openclaw AI agent has a skill for querying it. So browser pod output would be readable by any pod that isn't egress-restricted, including the control plane. That breaks the rule that the control plane must never get secrets out of browser pods. Fluent Bit already honours a per-pod exclude annotation; the stronger fix is an `Exclude_Path` setting in the Fluent Bit config for the browser namespace.

**Uptime checks.** Gatus adds a check for every HTTPRoute automatically. Routes on `envoy-external` land in the `external` group, which raises critical alerts, and they appear on the public status page at status.chestr.dev. The operator must mark every per-trip VNC route `gatus.home-operations.com/enabled: "false"` (this comes from the gatus-sidecar README, not the repo).

**Email.** There is an SMTP relay at `smtp-relay.default:2525` (maddy) that relays through AWS SES. It has no TLS or authentication, accepts sender addresses @chestr.dev or @controlforensics.com, and its NetworkPolicy only lets Authelia and LLDAP connect. Ferry Watch needs a new allow rule there.

**Descheduler.** It can evict pods that use local storage, so a browser holding a cart could be moved to rebalance nodes.

**Recommendation.** Use Postgres on the shared `postgres17` through `components/cnpg`, not SQLite. Run the control plane in its own namespace (not `default`) and browser pods in a separate namespace. Expose low-cardinality Prometheus metrics with app-shipped PrometheusRules, keep browser logs out of Fluent Bit at the source, rely on the CNPG backups and never back up browser state. Reuse the SMTP relay, and add Dragonfly only if the API runs more than one replica.

## cluster-versions
Kubernetes v1.36.5 and Talos v1.13.10 are driven by tuppr. Cilium chart is 1.20.2 and Envoy Gateway is 1.9.2. There are 3 MS-01 nodes with 96GB RAM each. kubelet maxPods is 150. The 8-24 GB budget for browser pods: not in repo.
refs: kubernetes/apps/system-upgrade/tuppr/upgrades/kubernetes.yaml:8-9, kubernetes/apps/system-upgrade/tuppr/upgrades/talos.yaml:9, kubernetes/apps/kube-system/cilium/app/helmrelease.yaml:16, kubernetes/apps/networking/envoy-gateway/app/ocirepository.yaml:13, AGENTS.md:7, README.md:138, talos/controlplane.yaml:16-17
=> The owner's 'Kubernetes >= 1.36, Cilium, Envoy Gateway' claim is confirmed. The 8-24 GB figure is an owner decision that must be enforced in the operator, ideally with a ResourceQuota on the browser namespace.

## cnpg-operator
The CloudNative-PG operator is HelmRelease chart 0.29.1 with replicaCount 2 and crds.create true. PodMonitor and a Grafana dashboard are enabled. The operator app version is not pinned in the repo.
refs: kubernetes/apps/database/cloudnative-pg/app/helmrelease.yaml:10-25
=> CNPG is the established Postgres platform. Check the operator version with `helm show chart` before relying on newer CRDs such as declarative Database.

## cnpg-clusters
There are two Clusters. postgres17 is shared: 3 instances, ghcr.io/cloudnative-pg/postgresql:17, 20Gi on openebs-hostpath (local disk, not Ceph), max_connections 400, 6Gi memory limit, switchover updates. immich is dedicated (vectorchord image). Both reuse the same superuserSecret, cloudnative-pg-secret. postgres17 is also a LAN LoadBalancer at 10.0.20.17 (postgres.chestr.dev).
refs: kubernetes/apps/database/cloudnative-pg/cluster/cluster17.yaml:6-33, kubernetes/apps/database/cloudnative-pg/cluster/immich.yaml:6-21, kubernetes/apps/database/cloudnative-pg/cluster/service.yaml:5-13, kubernetes/apps/database/cloudnative-pg/ks.yaml:35-39
=> There is spare connection headroom on the shared cluster. Postgres data is replicated by its 3 CNPG instances, not by Ceph. A dedicated per-app Cluster exists only for immich's special extension.

## cnpg-how-apps-get-a-db
Apps add `components/cnpg` to ks.yaml with APP (and optional CNPG_NAME, default postgres17) and dependsOn cloudnative-pg-cluster. The component creates two ExternalSecrets from the 1Password item `cloudnative-pg` (fields `${APP}_postgres_username/password`). `${APP}-initdb-secret` is used by the init container ghcr.io/home-operations/postgres-init:18 to create the role and DB. `${APP}-pguser-secret` carries the keys host, ro_host, user, password, db, uri, async_uri and dsn. It does not use CNPG managed.roles or Database CRs. 15 apps use it: grafana, subs, bazarr, prowlarr, autobrr, sonarr, jellyseerr, radarr, teslamate, atuin, windmill, immich, authelia, lldap and paperless.
refs: kubernetes/components/cnpg/externalsecret.yaml:1-52, kubernetes/components/cnpg/README.md:11-21, kubernetes/apps/default/atuin/ks.yaml:11-20, kubernetes/apps/default/atuin/app/helmrelease.yaml:53-60,75-79, AGENTS.md:131
=> The control plane can get Postgres with one component line plus two 1Password fields. Point DATABASE_URL at the `uri` or `async_uri` key of ferry-watch-pguser-secret.

## cnpg-superuser-exposure
The cnpg component puts INIT_POSTGRES_SUPER_PASS, the shared postgres17 superuser password, into `${APP}-initdb-secret` in the app's own namespace.
refs: kubernetes/components/cnpg/externalsecret.yaml:17-21
=> Any identity that can read Secrets or create Pods in the control-plane namespace gets superuser on the DB that also holds authelia, lldap, immich, paperless and the others. The operator's ServiceAccount must not have secrets get/list or pods create in its own namespace. Pod-create permission belongs only in a separate browser namespace, and the control plane must not live in `default`.

## cnpg-hyphen-risk
No existing cnpg consumer has a hyphen in APP. The component uses `${APP}` as the DB name, the URI user and in 1Password field names.
refs: kubernetes/components/cnpg/externalsecret.yaml:17,46-49, kubernetes/apps/default/ferry-watch/ks.yaml:22
=> APP=ferry-watch would create a database named `ferry-watch`, which no existing consumer has tested. Not in repo: whether postgres-init quotes identifiers. Verify that, or use a hyphen-free DB app name such as ferrywatch.

## cnpg-backups
postgres17 has Barman backups to R2 (s3://cloudnative-pg/, serverName postgres17-v1) with bzip2 WAL archiving and retentionPolicy 30d, plus a ScheduledBackup @daily. The cnpg component also adds a `${APP}-pg-backups` CronJob (prodrigestivill/postgres-backup-local:17) every 4h at :05 that writes pg_dumps to NFS nebula.chestr.dev:/var/nfs/shared/cluster/apps/postgres-backup-local. The pg_dump retention is the image's defaults, which are not in repo. Restore works by bumping serverName and bootstrap.recovery.
refs: kubernetes/apps/database/cloudnative-pg/cluster/cluster17.yaml:35-66, kubernetes/apps/database/cloudnative-pg/cluster/scheduledbackup.yaml:1-12, kubernetes/components/cnpg/cronjob.yaml:5-51
=> The control-plane DB gets 30-day point-in-time recovery on R2 plus 4-hourly logical dumps with no extra work. Everything stored in it is copied off-cluster for a long time, so BC Ferries credentials, cookies and session data must never go in it. The pg_dump CronJob runs in the app's namespace and needs egress to postgres17-rw and to NFS.

## cnpg-alerts
Postgres alerts already exist: LongRunningTransaction, BackendsWaiting, PGReplication, LastFailedArchiveTime, ReplicaFailingReplication and others, all severity warning. CNPG PodMonitors are enabled.
refs: kubernetes/apps/database/cloudnative-pg/cluster/prometheusrule.yaml:1-76, kubernetes/apps/database/cloudnative-pg/cluster/cluster17.yaml:32-33
=> The control plane doesn't need its own DB-health alerts.

## dragonfly
The Dragonfly operator is v1.7.0 in the database namespace. `components/dragonfly` creates `${APP}-dragonfly`: image v2.0.2, replicas ${DRAGONFLY_REPLICAS:-1}, 512Mi limit, cluster_mode=emulated. No password and no snapshot or persistence are configured. It has a PodMonitor and a NetworkPolicy that allows 6379 from any pod in the same namespace and 9999 from Prometheus. Consumers are litellm, subs, immich, authelia (3 replicas) and paperless. Apps connect at redis://<app>-dragonfly.<ns>.svc.cluster.local:6379.
refs: kubernetes/apps/database/dragonfly/app/helmrelease.yaml:19-22, kubernetes/components/dragonfly/cluster.yaml:1-33, kubernetes/components/dragonfly/networkpolicy.yaml:1-27, kubernetes/components/dragonfly/podmonitor.yaml:1-14, kubernetes/apps/default/paperless/app/helmrelease.yaml:61, kubernetes/apps/default/authelia/ks.yaml:28
=> Dragonfly can back a WebSocket fan-out hub or rate-limit counters, but only as a throwaway cache. Browser pods must never share its namespace, because they could read and write it without auth.

## volsync
VolSync is chart 0.16.0. Backups are opt-in per app through `components/volsync`, which adds a PVC (default ceph-block, 5Gi), a restic ReplicationSource to R2 at `${RESTIC_REPOSITORY_BUCKET}/${APP}` (daily at 11:00, keeps 7 dailies, prunes every 14 days) and a bootstrap ReplicationDestination. Nothing applies it cluster-wide: the Kyverno HelmRepository exists but no Kyverno release or policies do. VolSyncVolumeOutOfSync raises a critical alert.
refs: kubernetes/apps/volsync-system/volsync/app/helmrelease.yaml:10-11, kubernetes/components/volsync/pvc.yaml:1-16, kubernetes/components/volsync/r2/replicationsource.yaml:1-26, kubernetes/components/volsync/r2/externalsecret.yaml:24, kubernetes/apps/volsync-system/volsync/app/prometheusrule.yaml:18-27, kubernetes/flux/meta/repositories/helm/kyverno.yaml
=> Browser state is not backed up as long as the operator never creates PVCs or ReplicationSources for browser pods. Use emptyDir only.

## current-ferry-watch-deployment
The existing ferry-watch runs in the `default` namespace. It uses components/volsync (1Gi config PVC, UID/GID 1001), Pushover secrets (PUSHOVER_GATUS_TOKEN reused), a 4Gi memory limit for up to 6 browsers, and emptyDir /tmp (holding the Chromium profile) and /dev/shm. It is on envoy-internal behind Authelia, with VNC on /vnc. Its CiliumNetworkPolicy allows DNS plus 0.0.0.0/0 except private ranges, and ingress from the `kube-apiserver` entity, because Envoy traffic crossing nodes is SNATed to the node IP.
refs: kubernetes/apps/default/ferry-watch/ks.yaml:11-25, kubernetes/apps/default/ferry-watch/app/externalsecret.yaml:16-24, kubernetes/apps/default/ferry-watch/app/helmrelease.yaml:53-126, kubernetes/apps/default/ferry-watch/app/networkpolicy.yaml:1-69
=> Once config moves to Postgres, drop the volsync component and PVC. The egress policy has to narrow from 'whole internet' to bcferries.com plus checkout dependencies. The kube-apiserver-entity workaround will also be needed for the per-trip VNC ingress.

## storage-classes
`ceph-block` (Rook-Ceph RBD) is the default class, 3x host-replicated, reclaimPolicy Delete and expandable, with VolumeSnapshotClass csi-ceph-blockpool. `openebs-hostpath` is local at /var/mnt/extra/openebs/local and used by CNPG, VolSync caches and runners. There is no CephFS or object store (cephFileSystems: [], cephObjectStores: []), so there is no RWX class. No longhorn or local-path: not in repo.
refs: kubernetes/apps/rook-ceph/rook-ceph/cluster/helmrelease.yaml:75-103, kubernetes/apps/openebs-system/openebs/app/helmrelease.yaml:13-22, AGENTS.md:134-137, kubernetes/apps/actions-runner-system/actions-runner-controller/runners/home-cluster/helmrelease.yaml:22
=> If SQLite were chosen, it would sit on an RWO ceph-block volume: one pod at a time and Recreate rollouts. That rules out an HA API. Browser pods should use emptyDir with ephemeral-storage requests and limits.

## prometheus
kube-prometheus-stack (OCI 92.3.0) has all *SelectorNilUsesHelmValues set to false, so it picks up every ServiceMonitor, PodMonitor, PrometheusRule, ScrapeConfig and Probe in every namespace with no label needed. Retention is 14d/50GB on 50Gi ceph-block, with a 2000Mi memory limit. The convention is app-template's `serviceMonitor:` block (port metrics, /metrics, 1m). PrometheusRules sit in the app dir with a `severity` label. prometheus-operator CRDs (v0.94.1) are installed through Talos extraManifests.
refs: kubernetes/apps/observability/kube-prometheus-stack/app/helmrelease.yaml:60-80, kubernetes/apps/observability/kube-prometheus-stack/app/ocirepository.yaml:13, kubernetes/apps/default/atuin/app/helmrelease.yaml:99-117, kubernetes/apps/observability/gatus/app/prometheusrule.yaml:1-29, talos/controlplane.yaml:136-137
=> The control plane only needs a metrics port, a serviceMonitor block and a PrometheusRule file next to its HelmRelease. Keep metric labels low-cardinality, because Prometheus memory is capped at 2Gi.

## alertmanager-routing
There is one global AlertmanagerConfig. The root receiver is `pushover`. Only alertname=InfoInhibitor goes to blackhole and Watchdog goes to the heartbeat webhook. severity=critical routes explicitly to pushover, and everything else falls to the root, so it also goes to Pushover. There is no email receiver. The only inhibit rule is critical over warning per alertname and namespace. Flux error events from every namespace also reach Alertmanager through the common component.
refs: kubernetes/apps/observability/kube-prometheus-stack/app/alertmanagerconfig.yaml:7-91, kubernetes/apps/observability/kube-prometheus-stack/app/helmrelease.yaml:29-32, kubernetes/apps/observability/kube-prometheus-stack/app/externalsecret.yaml:18-25, kubernetes/components/common/alerts/alertmanager/alert.yaml:1-27, kubernetes/components/common/alerts/alertmanager/provider.yaml:6-8
=> Any PrometheusRule the control plane ships pages the owner on Pushover. The owner's 'no Pushover' rule for the product doesn't extend to cluster ops alerts unless the AlertmanagerConfig changes. Not in repo: whether namespaced AlertmanagerConfigs from other namespaces are picked up (alertmanagerConfigSelector isn't set).

## cluster-wide-pod-alerts
A custom OomKilled rule (severity critical) covers every namespace. The stock kube-prometheus-stack rules (KubePodCrashLooping, KubePodNotReady, KubeQuota*, and so on) are also active for all namespaces. The silence-operator is installed, with Silence CRs declared in its HelmRelease extraDeploy.
refs: kubernetes/apps/observability/kube-prometheus-stack/app/helmrelease.yaml:93-113, kubernetes/apps/observability/silence-operator/app/helmrelease.yaml:17-60
=> Per-trip browser pods that get OOM-killed, crash-loop, stay Pending or fill a ResourceQuota would page the owner through Pushover. Suppress these for the browser namespace with Silence CRs or a blackhole route, and let the control plane raise aggregated fleet alerts instead.

## fluent-bit
Fluent Bit tails /var/log/containers/*.log, which covers every pod in the cluster with no namespace exclusions. The kubernetes filter has k8s-logging.exclude on, so a `fluentbit.io/exclude` pod annotation is honoured, and annotations on. Pod labels are lifted into fields. Records go to VictoriaLogs with stream fields k_namespace_name, k_pod_name and app. kubelet containerLogMaxSize is not set (kubelet defaults).
refs: kubernetes/apps/observability/fluent-bit/app/helmrelease.yaml:29-91, kubernetes/apps/observability/fluent-bit/app/ocirepository.yaml:13-14, talos/controlplane.yaml:12-24
=> Browser pod stdout and stderr, plus pod labels and annotations, would be collected by default. You can exclude by annotation today. The stronger fix is a home-cluster change adding `Exclude_Path` for the browser namespace to the tail input.

## victoria-logs-access
VictoriaLogs single-node (chart 0.13.10) keeps 14d on 40Gi ceph-block. Its UI is victoria-logs.chestr.dev on envoy-internal behind Authelia. The in-cluster API on :9428 has no auth, and no NetworkPolicy exists in the observability or database namespaces (only 6 files in the repo define network policies). The openclaw AI agent, which uses hosted LLMs, has a victorialogs skill documented as 'in-cluster, no auth'.
refs: kubernetes/apps/observability/victoria-logs/app/helmrelease.yaml:13-41, kubernetes/apps/ai/openclaw/app/resources/skills/victorialogs.md:1-40, kubernetes/apps/ai/openclaw/app/configmap.yaml:137, kubernetes/apps/ai/openclaw/app/helmrelease.yaml:298-299, AGENTS.md:7
=> If browser logs are collected, the control plane (or any unrestricted pod) could query them, which breaks the rule that the control plane can't get secrets out of browser pods. They would also be kept 14 days and could reach an external LLM through openclaw. Exclude browser logs at the source, and block both Ferry Watch namespaces from egress to observability:9428.

## log-alerts
VMAlert `logs` evaluates VMRule objects labelled vmalert-logs.io/enabled: "true" (type vlogs) from any namespace and notifies Alertmanager, which means Pushover. Several apps use this. No VictoriaMetrics operator HelmRelease or CRDs were found in kubernetes/ or the Talos extraManifests.
refs: kubernetes/apps/observability/victoria-logs/app/vmalert.yaml:1-22, kubernetes/apps/default/immich/app/vmrule.yaml:1-17
=> Log-based alerts are possible, but how the operator behind VMAlert is installed is not in repo. Prefer PrometheusRules on exported metrics for Ferry Watch.

## grafana
Grafana runs through grafana-operator 5.25.0 with Authelia OIDC (group admin maps to Admin, people to Viewer). Datasources are prometheus, alertmanager and victoriametrics-logs. Apps in any namespace ship GrafanaDashboard or GrafanaFolder CRs with allowCrossNamespaceImport: true and instanceSelector grafana.internal/instance: grafana.
refs: kubernetes/apps/observability/grafana/app/ocirepository.yaml:30, kubernetes/apps/observability/grafana/instance/grafana.yaml:16-35, kubernetes/apps/observability/grafana/instance/grafanadatasource.yaml:1-50, kubernetes/apps/volsync-system/volsync/app/grafanadashboard.yaml:1-18, kubernetes/apps/default/teslamate/app/grafanadashboard.yaml:1-20
=> An owner-facing fleet dashboard (capacity, reaper actions, failures) can ship as a GrafanaDashboard CR in the Ferry Watch app dir, with no new auth work.

## gatus
The gatus-sidecar runs with --auto-httproute, so every HTTPRoute becomes an uptime endpoint and inherits its Gateway's annotation. envoy-external's template is group: external and does not hide the hostname. envoy-internal's is group internal, expects 401, and hides the hostname. GatusEndpointDown (group external, 5m) is critical, so it reaches Pushover. The Gatus status page is public at status.chestr.dev on envoy-external. Per the gatus-sidecar README (not in repo), the annotation gatus.home-operations.com/enabled with any value other than "true" excludes a route.
refs: kubernetes/apps/observability/gatus/app/helmrelease.yaml:19-34,94-111, kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:47-55,84-95, kubernetes/apps/observability/gatus/app/prometheusrule.yaml:11-19, kubernetes/apps/default/atuin/app/helmrelease.yaml:121-124
=> Dynamic per-trip VNC HTTPRoutes would be listed publicly with their hostnames and could raise false critical pages. The operator must annotate them enabled "false". The main control-plane route should carry a `gatus.home-operations.com/endpoint` annotation with conditions on its health path.

## smtp-relay
smtp-relay is maddy 0.9.6 with 3 replicas in `default`, at smtp-relay.default.svc.cluster.local:2525. It has no TLS and no auth, and accepts only sender domains chestr.dev and controlforensics.com. mikeflix.lol is set in env but not used in maddy.conf. It relays to AWS SES (email-smtp.us-west-2.amazonaws.com:587, STARTTLS) and its queue is a memory emptyDir. Its NetworkPolicy allows 2525 only from authelia and lldap pods in `default`, plus observability on metrics port 9749. SES DKIM, SPF and DMARC (p=none) records are managed by DNSEndpoint. LLDAP sends as no-reply@chestr.dev and Authelia as auth@chestr.dev.
refs: kubernetes/apps/default/smtp-relay/app/helmrelease.yaml:22-128, kubernetes/apps/default/smtp-relay/app/resources/maddy.conf:1-36, kubernetes/apps/default/smtp-relay/app/externalsecret.yaml:16-22, kubernetes/apps/default/smtp-relay/app/dnsendpoint.yaml:1-41, kubernetes/apps/default/lldap/app/helmrelease.yaml:53-57, kubernetes/apps/default/authelia/app/resources/configuration.yaml:106-108
=> Email alerts can reuse this relay once the smtp-relay NetworkPolicy gains a rule for the control-plane pods (a home-cluster change) and mail is sent From an @chestr.dev address. Browser pods must never be allowed to reach it, since it is an unauthenticated relay. Not in repo: whether SES is out of sandbox, which is needed to email arbitrary invited users.

## network-policy-baseline
Only 6 files in the repo define NetworkPolicies, and there is no cluster-wide default deny. The observability (Prometheus 9090, Alertmanager 9093, VictoriaLogs 9428) and database (postgres17 5432, password auth) namespaces have no policies.
refs: kubernetes/apps/ai/openclaw/app/networkpolicy.yaml, kubernetes/apps/default/ferry-watch/app/networkpolicy.yaml, kubernetes/apps/default/paperless/app/helmrelease.yaml, kubernetes/apps/default/home-assistant/app/helmrelease.yaml, kubernetes/apps/default/smtp-relay/app/helmrelease.yaml:89-128, kubernetes/components/dragonfly/networkpolicy.yaml
=> Ferry Watch has to bring its own default-deny egress and ingress for both namespaces. If it adds ingress policies, it must allow Prometheus to scrape the control plane (the dragonfly policy is a template) and allow the Envoy gateway pods in.

## common-component-secrets
Every namespace directory applies components/common. It brings a Namespace (prune disabled), Flux Alerts to Alertmanager and GitHub, the app-template OCIRepository 5.3.0, and the ExternalSecrets cluster-secrets (Cloudflare account and tunnel IDs), ghcr-docker-secret and gitlab-docker-secret.
refs: kubernetes/components/common/kustomization.yaml:1-8, kubernetes/components/common/secrets/cluster-secrets.yaml:1-22, kubernetes/components/common/secrets/ghcr-docker-secret.yaml:1-21, kubernetes/components/common/repos/app-template/ocirepository.yaml:13, kubernetes/apps/default/kustomization.yaml:4-6
=> A browser namespace created as kubernetes/apps/<ns>/ with the common component would contain registry credentials that any pod-creator there (the operator) can mount. Create the browser namespace without those secrets, for example as a Namespace resource inside the ferry-watch app.

## descheduler
The descheduler (0.37.0) has DefaultEvictor evictLocalStoragePods: true and evictSystemCriticalPods: true. Its balance plugins are LowNodeUtilization (pod-count thresholds 50%/90%) and RemovePodsViolatingTopologySpreadConstraint. Neither has a namespace exclusion.
refs: kubernetes/apps/kube-system/descheduler/app/helmrelease.yaml:19-67
=> Operator-owned browser pods using emptyDir can be evicted to rebalance nodes, which could kill a session holding a cart. Exclude the browser namespace in the descheduler config, or avoid topologySpread constraints on browser pods.

## renovate-ci
Renovate automerges minor, patch and digest updates. 0.x minors stay as PRs. CNI, storage, Flux, app-template, cloudnative-pg, postgres, immich and windmill always get PRs. ghcr.io images skip minimumReleaseAge. An inline `# renovate: datasource=docker depName=...` comment above a `key: value` line is handled by a custom regex manager. image-pull.yaml pre-pulls on the nodes (self-hosted runner, talosctl) only the images flux-local finds in manifests. flux-local and flux-diff run on kubernetes/** PRs. HelmReleases get rollback-on-failure injected, and apps with DB migrations set upgrade.strategy RetryOnFailure.
refs: .github/renovate/autoMerge.json5:1-70, .github/renovate/customManagers.json5:24-28, kubernetes/apps/system-upgrade/tuppr/upgrades/kubernetes.yaml:8-9, .github/workflows/image-pull.yaml:47-57,92-107, kubernetes/flux/cluster/ks.yaml:55-91, AGENTS.md:107, kubernetes/apps/default/atuin/app/helmrelease.yaml:41-44
=> The browser image the operator launches is not a container in any manifest. Put its tag in a HelmRelease value with a renovate comment so it gets bumped, and expect no node pre-pull, which means slow cold starts for a big Chromium + KasmVNC image. Add /ferry-watch/ to the always-PR list and set RetryOnFailure once Postgres migrations exist.

## other-reusable
Also present: metrics-server, stakater reloader (annotation reloader.stakater.com/auto), spegel (peer-to-peer image mirror), external-dns, and a cert-manager wildcard cert for chestr.dev and *.chestr.dev used by both gateways. There is no Pod Security Admission namespace labelling and no admission policy engine: not in repo (Talos defaults apply).
refs: kubernetes/apps/kube-system/metrics-server/app/helmrelease.yaml, kubernetes/apps/kube-system/reloader, kubernetes/apps/kube-system/spegel/app/helmrelease.yaml, kubernetes/apps/networking/envoy-gateway/app/certificate.yaml:8-13, talos/controlplane.yaml:155
=> The operator can read live pod usage from metrics.k8s.io for capacity decisions without depending on Prometheus. Per-trip origins should be one-level subdomains of chestr.dev to be covered by the wildcard cert. Browser-namespace pod hardening has to be set explicitly, for example PSA labels on the namespace.

RECOMMENDATIONS:
- Control-plane storage: use Postgres on the shared `postgres17` through `components/cnpg`, not SQLite. 15 apps already do this, and it brings 30-day point-in-time recovery to R2, 4-hourly pg_dumps to NFS and existing DB alerts. It also allows 2+ API replicas and a leader-elected operator. SQLite would need an RWO ceph-block PVC with Recreate rollouts and VolSync. Steps: add the 1Password fields per components/cnpg/README.md; give the ks.yaml components [cnpg] and dependsOn cloudnative-pg-cluster; use a hyphen-free DB app name or first confirm postgres-init quotes identifiers; set `upgrade.strategy.name: RetryOnFailure` and real startup, liveness and readiness probes; read `uri` or `async_uri` from <app>-pguser-secret.
- Namespace split: control plane and its DB secrets in a dedicated namespace (not `default`, which holds authelia, lldap and other secrets); browser pods in a second namespace created without components/common, so no registry or cluster secrets sit there. The operator's ServiceAccount gets pods, services, httproutes and networkpolicies create/delete only in the browser namespace. It gets no secrets get/list anywhere, no pods/exec, attach, portforward, log or ephemeralcontainers, and no pod-create in its own namespace, because the cnpg initdb Secret there holds the shared Postgres superuser password.
- Metrics: expose /metrics on a separate port from the API and wire it with app-template's `serviceMonitor` block. Prometheus selects it with no labels needed. Suggested series: ferrywatch_browsers{state}, ferrywatch_browser_memory_budget_bytes and ferrywatch_browser_memory_requested_bytes, ferrywatch_quota_rejections_total{reason}, ferrywatch_reaper_actions_total{reason=idle|expired|failed|orphan}, ferrywatch_browser_failures_total{phase}, ferrywatch_cart_holds_active, ferrywatch_alert_deliveries_total{channel,result}, ferrywatch_reconcile_errors_total. Never use user, trip or pod IDs as labels (Prometheus has a 2Gi memory limit and 14d retention). Don't scrape browser pods: they report to the control plane, which aggregates.
- Admin alerts: ship a PrometheusRule next to the HelmRelease, for example FerryWatchCapacityExhausted (requested/budget > 0.9 for 10m, warning), FerryWatchBrowserFailureRate (critical), FerryWatchReaperStuck, FerryWatchAlertDeliveryFailing, FerryWatchReconcileErrors. Today these all page the owner through Pushover, since the root receiver is pushover. If the owner wants no Pushover for ops alerts too, add a route in alertmanagerconfig.yaml matching namespace=<ferry-watch ns> to a webhook receiver on the control plane, which then sends Web Push or email to admins. That route needs a NetworkPolicy allowing Alertmanager in. Optionally ship a GrafanaDashboard CR for the fleet.
- Noise control for the browser namespace: per-trip pods will trip the cluster-wide OomKilled (critical), KubePodCrashLooping, KubePodNotReady and KubeQuota* alerts and page Pushover. Add Silence CRs to silence-operator's extraDeploy, or a blackhole route in the AlertmanagerConfig, scoped to namespace=<browser ns>. Run browser pods with restartPolicy Never so they never crash-loop. Have the operator only create pods within its own budget and reap Pending and Failed pods quickly; the descheduler only removes failed pods after 30 minutes.
- Logs: keep browser pod logs out of VictoriaLogs. Layer 1, a home-cluster change: add `Exclude_Path /var/log/containers/*_<browser-ns>_*.log` to the tail INPUT in observability/fluent-bit/app/helmrelease.yaml. Layer 2: set the pod annotation `fluentbit.io/exclude: "true"`, which already works because k8s-logging.exclude is on. Layer 3: the browser agent logs only minimal, scrubbed events and reports structured status to the control plane. Layer 4: egress policies on both Ferry Watch namespaces deny observability:9428, 9090 and 9093, so the control plane can't query logs. Use opaque, non-PII pod names, labels and annotations, since pod names still land in Prometheus and kube-state-metrics for 14 days, and the control plane's own logs ARE collected. Have the reaper delete pods rather than leave them Completed, so kubelet removes their log files from node disk.
- Backup policy: the control-plane DB is covered by CNPG (Barman to R2 30d plus daily base, and pg_dump every 4h to NFS). Rehearse one restore using the serverName-bump procedure documented in cluster17.yaml. Never store BC Ferries credentials, cookies or browser session data in the DB. Put the VAPID private key and any SMTP settings in 1Password through an ExternalSecret, not in the DB. Browser pods use emptyDir only (memory-backed /dev/shm plus a disk tmp with ephemeral-storage limits), never PVCs or VolSync. VolSync is opt-in, so they stay unbacked by default. Remove components/volsync from kubernetes/apps/default/ferry-watch/ks.yaml once config.json moves to Postgres.
- Email: reuse smtp-relay.default.svc.cluster.local:2525 with no TLS or auth, sending From an @chestr.dev address such as ferry-watch@chestr.dev. This needs one ingress rule for the control-plane pods added to smtp-relay's `networkpolicies` in its helmrelease. Treat the relay as best-effort, since its queue is in memory, and record delivery failures in metrics. Confirm SES production access before inviting outside users. Browser namespace egress must never allow the relay.
- Dragonfly: not needed for v1. With one API replica, use an in-process WebSocket hub; with 2+ replicas, Postgres LISTEN/NOTIFY is enough. If rate limiting or fan-out outgrows that, add `components/dragonfly` to the control-plane ks, where 1 replica is fine as a throwaway cache. It must never share a namespace with browser pods, because it has no auth and its policy admits any same-namespace pod.
- Gatus: annotate the control-plane HTTPRoute with `gatus.home-operations.com/endpoint` and conditions against /healthz. The operator must add `gatus.home-operations.com/enabled: "false"` to every per-trip VNC HTTPRoute. Otherwise each per-trip hostname appears on the public status.chestr.dev page, and a VNC origin that doesn't return 200 to the probe raises a critical GatusEndpointDown page.
- Descheduler: as a home-cluster change, exclude the browser namespace from LowNodeUtilization and RemovePodsViolatingTopologySpreadConstraint, or don't give browser pods topologySpreadConstraints. Per-pod PDBs would also block tuppr node drains, so prefer the exclusion.
- Renovate and CI: put the browser image tag in the control-plane HelmRelease values as a plain value with a `# renovate: datasource=docker depName=ghcr.io/mchestr/<browser-image>` comment above it, following the tuppr/upgrades/kubernetes.yaml pattern, and pin a digest if possible. Add `/ferry-watch/` to the always-PR list in .github/renovate/autoMerge.json5 once migrations exist. Consider having the operator pre-pull the browser image, since image-pull.yaml won't see it.

GAPS:
- No NetworkPolicy lets Ferry Watch reach smtp-relay; its ingress currently allows only authelia and lldap.
- No Alertmanager email receiver and no route other than Pushover. Admin alerts sent anywhere else need a change to alertmanagerconfig.yaml or a namespaced AlertmanagerConfig, and whether the latter would be picked up is not in repo.
- Fluent Bit has no exclusion for a browser namespace. Without one, every browser pod's stdout and stderr goes to VictoriaLogs for 14 days.
- No NetworkPolicies protect observability or database. VictoriaLogs, Prometheus, Alertmanager and Postgres are reachable from any pod without egress limits, so Ferry Watch must bring default-deny egress for both of its namespaces.
- No Silence or blackhole route exists for expected browser-pod churn: OomKilled, KubePodNotReady, KubePodCrashLooping, KubeQuota*.
- The descheduler has no exclusion for the browser namespace (it evicts local-storage pods).
- No Ferry Watch ServiceMonitor, PrometheusRule or GrafanaDashboard exists, and the current app exposes no /metrics.
- No 1Password fields for a Ferry Watch Postgres user, and no VAPID keypair secret. Pushover secrets are the only notification secrets wired today.
- The VictoriaMetrics operator behind the VMAlert and VMRule objects is not in the repo.
- No RWX storage class (no CephFS), which only matters if shared volumes were ever wanted.
- No Pod Security Admission labels or admission policy engine are in the repo to enforce hardening on the browser namespace.

OPEN_QUESTIONS:
- Does 'no Pushover' cover only end-user trip alerts, or also owner/admin ops alerts? Today every cluster alert, including any Ferry Watch PrometheusRule, goes to Pushover.
- Is the AWS SES account behind smtp-relay out of the sandbox? In the sandbox it can only email verified addresses, which blocks emailing invited users.
- Will the owner accept changes to shared platform files in home-cluster: the fluent-bit Exclude_Path, the descheduler exclusion, the smtp-relay NetworkPolicy rule, and an Alertmanager route or Silence CRs for the browser namespace?
- Shared `postgres17` (simplest, but the superuser password sits in the app namespace) or a dedicated CNPG Cluster like `immich`? A dedicated one still uses the same superuserSecret today and needs its own ScheduledBackup.
- Does ghcr.io/home-operations/postgres-init quote identifiers, so an APP or DB named `ferry-watch` (hyphenated) works? No existing consumer has a hyphen.
- What retention does the 4-hourly pg_dump CronJob on NFS use? postgres-backup-local's defaults apply; nothing is set in the repo.
- How is the VictoriaMetrics operator that reconciles VMAlert and VMRule installed? Only matters if log-based alerts are wanted.
- Namespace names and gateway choice for the control plane and per-trip VNC routes. If they are on envoy-external, Authelia is not in front (networking area), and the Gatus opt-out becomes mandatory.
- Is it acceptable that browser pod names, labels and resource usage stay in Prometheus for 14 days? This drives the requirement for opaque, non-PII pod naming.
