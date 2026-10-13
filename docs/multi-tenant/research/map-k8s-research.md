===== k8s-research
Research mostly used live sources. WebSearch worked. kubernetes.io, kasmweb.com, authelia.com, gateway.envoyproxy.io and queue-it.com did not resolve through WebFetch, so I read the official docs and source as raw files from GitHub (kubernetes/website, kubernetes/kubernetes, kasmtech/KasmVNC, kasmtech/noVNC, envoyproxy/gateway, authelia, oauth2-proxy, nolar/kopf, projectcalico) and checked release dates on PyPI. I also measured the real pod footprint by building the repo's base image with KasmVNC 1.5.0 and matchbox, then driving headed Chromium through Playwright 1.56. bcferries.com itself was blocked, so the test page was a lighter one.

Main conclusions:
(1) Kubernetes building blocks exist for this design. ValidatingAdmissionPolicy has been GA since 1.30, MutatingAdmissionPolicy since 1.36, the 'create' check on WebSocket exec since 1.35, and bound tokens carry pod name and UID. kopf 1.45.0 shipped on 2026-10-13 and fits the existing aiohttp process.
(2) 'create pods' effectively means 'read every Secret in the namespace'. The pod spec has many ways to leak data: exec probes whose output lands in Events, lifecycle hooks, volumeMounts into KasmVNC's -httpd directory, env such as NODE_OPTIONS or DEBUG=pw:*, hostAliases, image swap via 'patch pods', and pods/proxy. An admission allowlist must cover all of these, or pod construction should move out of the operator.
(3) If the operator makes the VNC authorization decision, it can grant itself the logged-in session. Static per-user Authelia rules (named group User) avoid that.
(4) If credentials are typed into the operator's own UI, the operator sees them, whatever the RBAC says.
(5) Per-pod headed browsers cost about 0.52–0.57 GB each (measured) versus about 35–80 MB for each extra context in one shared browser. 'Scales better' holds for isolation and spreading across nodes, but costs roughly 5–8x the memory. Every pod still leaves through one home IP that sits behind Queue-it.

## (a) Operator frameworks: kopf is maintained and fits the codebase; Go controller-runtime is stronger on caching and leader election
kopf: PyPI shows 1.44.5 (2026-04-01), 1.44.6 (2026-06-03) and 1.45.0 (2026-10-13). The README says it is 'production-ready and stable', with no new major features planned and only small changes and bug fixes. kopf has no shared informer cache. Each watched kind gets its own watch stream, plus in-memory @kopf.index indexes. There is no Lease-based leader election. Instead, 'peering' (KopfPeering/ClusterKopfPeering CRDs) pauses lower-priority instances, and replicas with the same priority all freeze (docs/peering.rst). kopf can be embedded: run kopf.operator(), or spawn_tasks()/run_tasks(), next to the existing aiohttp server (docs/embedding.rst). kopf uses aiohttp itself.

Important RBAC catch: by default kopf writes handler progress and diffbase annotations and adds finalizers on every object its on.create/update/delete handlers manage (docs/configuration.rst ~542-629). Handling Pods that way would require 'patch pods', which is dangerous (see c). Put the persistent handlers on a CRD such as BrowserSession, and watch Pods only with @kopf.on.event, which writes nothing.

kubernetes_asyncio: 36.1.0 (2026-06-04). It is a generated async client with watch support and a leaderelection module with a LeaseLock (kubernetes_asyncio/leaderelection/resourcelock/leaselock.py exists), but no informer cache. lightkube 1.0.1 (2026-08-12) is a typed async alternative.

Go controller-runtime (v0.23.x/v0.24.x lines in 2026, medium confidence on the newest): shared informer cache, Lease leader election built into the manager, webhook server, and Runnables that can host an HTTP API. It is the most robust choice, but it would split this all-Python repo into two languages.

For a single-replica home operator, leader election is not needed: use replicas: 1 with strategy Recreate.
refs: https://pypi.org/pypi/kopf/json, https://github.com/nolar/kopf, https://raw.githubusercontent.com/nolar/kopf/main/docs/peering.rst, https://raw.githubusercontent.com/nolar/kopf/main/docs/embedding.rst, https://raw.githubusercontent.com/nolar/kopf/main/docs/configuration.rst, https://pypi.org/pypi/kubernetes-asyncio/json, https://raw.githubusercontent.com/tomplus/kubernetes_asyncio/master/kubernetes_asyncio/leaderelection/resourcelock/leaselock.py, https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.23.1

## (b) Admission: VAP is GA, MAP is GA in 1.36, PSA covers only part of it, Kyverno is the alternative
ValidatingAdmissionPolicy has been stable since v1.30. CEL can read object, oldObject, request (including request.userInfo and request.subResource), params, namespaceObject and authorizer. matchConditions cannot use namespaceObject or variables.

MutatingAdmissionPolicy feature gate: alpha 1.30–1.33, beta 1.34–1.35 (off by default), stable 1.36 (on by default).

Pod Security Admission ('restricted', set with a namespace label) blocks privileged containers, hostPath, host namespaces and hostPorts, and requires runAsNonRoot, dropped capabilities and a seccomp profile. It does NOT restrict the image, command, env, secret names, mount paths, probes or hostAliases. The operator must not be able to patch Namespaces, because those labels are the PSA control.

Kyverno graduated in the CNCF in March 2026. Its 1.17 release made CEL ValidatingPolicy GA and adds image-signature verification, which is better than hand-editing pinned digests. But it is a webhook (availability and failurePolicy concerns), while VAP runs inside the API server.

Two gotchas:
1. Matching on request.userInfo.username only catches pods the operator creates directly. Pods created through Deployments or Jobs are submitted by controller-manager service accounts, so the operator must create bare Pods, or the policy must also cover workload templates.
2. Validation runs after mutation, so the VAP sees the injected kube-api-access volume unless automountServiceAccountToken is false.

VAP sketch (one container; tenant = namespace; params are an admin-owned ConfigMap holding the image digest):
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata: {name: ferry-browser-pods}
spec:
  failurePolicy: Fail
  paramKind: {apiVersion: v1, kind: ConfigMap}
  matchConstraints:
    namespaceSelector: {matchLabels: {ferry-watch.io/tenant: "true"}}
    resourceRules: [{apiGroups: [""], apiVersions: ["v1"], operations: ["CREATE","UPDATE"], resources: ["pods","pods/ephemeralcontainers"]}]
  matchConditions:
  - name: operator-only
    expression: request.userInfo.username == 'system:serviceaccount:ferry-watch:operator'
  variables:
  - {name: c, expression: object.spec.containers[0]}
  - {name: owner, expression: "namespaceObject.metadata.labels['ferry-watch.io/owner']"}
  validations:
  - expression: request.operation == 'CREATE' && request.subResource == ''
  - expression: object.spec.containers.size() == 1 && !has(object.spec.initContainers) && !has(object.spec.ephemeralContainers)
  - expression: params.data.image.contains('@sha256:') && variables.c.image == params.data.image
  - expression: "!has(variables.c.command) && !has(variables.c.args) && !has(variables.c.lifecycle) && !has(variables.c.envFrom) && (!has(variables.c.livenessProbe) || !has(variables.c.livenessProbe.exec)) && (!has(variables.c.readinessProbe) || !has(variables.c.readinessProbe.exec)) && (!has(variables.c.startupProbe) || !has(variables.c.startupProbe.exec))"
  - expression: "!has(variables.c.env) || variables.c.env.all(e, e.name in ['TRIP_ID','OPERATOR_URL'] && !has(e.valueFrom))"
  - expression: "object.spec.volumes.all(v, (v.name == 'creds' && has(v.secret) && v.secret.secretName.matches('^bcf-creds(-[a-z0-9-]{1,40})?$')) || (v.name in ['tmp','profile'] && has(v.emptyDir)) || (v.name == 'op-token' && has(v.projected) && v.projected.sources.all(s, has(s.serviceAccountToken) && s.serviceAccountToken.audience == 'ferry-watch-operator')))"
  - expression: "variables.c.volumeMounts.all(m, !has(m.subPath) && ((m.name == 'creds' && m.mountPath == '/run/secrets/bcf' && m.readOnly) || (m.name == 'tmp' && m.mountPath == '/tmp') || (m.name == 'profile' && m.mountPath == '/profile') || (m.name == 'op-token' && m.mountPath == '/run/secrets/op')))"
  - expression: "object.spec.serviceAccountName == 'browser' && object.spec.automountServiceAccountToken == false && !has(object.spec.hostAliases) && !has(object.spec.dnsConfig) && object.spec.dnsPolicy == 'ClusterFirst' && !has(object.spec.runtimeClassName) && !has(object.spec.nodeName) && !(has(object.spec.shareProcessNamespace) && object.spec.shareProcessNamespace) && !(has(object.spec.hostNetwork) && object.spec.hostNetwork) && !(has(object.spec.hostPID) && object.spec.hostPID)"
  - expression: "!(has(variables.c.securityContext.privileged) && variables.c.securityContext.privileged) && variables.c.securityContext.allowPrivilegeEscalation == false && variables.c.terminationMessagePolicy == 'File'"
  - expression: "object.metadata.labels['ferry-watch.io/owner'] == variables.owner"

Bind it with a ValidatingAdmissionPolicyBinding: validationActions [Deny], paramRef {name: browser-pod-policy, namespace: ferry-watch-system, parameterNotFoundAction: Deny}. Add PSA 'restricted' on the tenant namespaces as a backstop for hostPath and privileged. Add sibling VAPs for Services (ClusterIP only, no ExternalName) and HTTPRoutes (hostname or path must embed variables.owner, backendRefs must stay in the same namespace).

How hard this is to keep correct, given the exfiltration list in (c), is the reason to consider a small trusted 'pod-spawner' controller that builds pods from a fixed template, with the operator only writing a BrowserSession CR.
refs: https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/reference/access-authn-authz/validating-admission-policy.md (line 12 stable v1.30; 326-347 CEL variables; 391-420 matchConditions), https://raw.githubusercontent.com/kubernetes/website/main/content/en/examples/access/validating-admission-policy-match-conditions.yaml, https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/reference/command-line-tools-reference/feature-gates/MutatingAdmissionPolicy.md, https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/concepts/security/rbac-good-practices.md (100-118), https://www.cncf.io/announcements/2026/03/24/cloud-native-computing-foundation-announces-kyvernos-graduation/, https://www.cncf.io/blog/2026/02/18/announcing-kyverno-1-17/

## (c) RBAC: create-only Secrets works, but resourceNames can't scope create, and 'create pods' implies reading Secrets. Full list of leak paths
RBAC is purely additive, so a Role with verbs [create] on secrets grants only create. The create response echoes only what the caller sent.

resourceNames cannot restrict top-level 'create' (or 'deletecollection') because the name may not be known when the request is authorized. That limit does not apply to subresources: resourceNames works with pods/exec (rbac.md:219-220). So you cannot write 'may create only bcf-creds-*'. That needs an admission policy on Secrets if it matters.

Verbs that reveal Secret contents:
- get, list and watch (good-practices 93-98).
- update and patch, because the response is the full stored object (high confidence).
- deletecollection: in the API server's generic store, DeleteCollection returns the list of matched objects. The Secret store does not set ReturnDeletedObject, so a single 'delete' returns only a Status (store.go; secret/storage/storage.go). Medium-high confidence; verify on your version. Prefer garbage collection through ownerReferences over granting delete.

'create pods' in a namespace implicitly grants every Secret, ConfigMap and PV there, plus the API rights of every ServiceAccount there (good-practices 100-118). That is why admission policy is mandatory. Concrete leak paths the policy must close:
1. Mount the Secret (volume or env secretKeyRef) into a pod you control.
2. An exec liveness or startup probe that prints the file and fails. The probe output shows up in Events, which anyone with get events can read.
3. Lifecycle postStart/preStop exec hooks.
4. A volumeMount inside KasmVNC's -httpd directory (/opt/vnc-web or /usr/share/kasmvnc/www, Dockerfile:36-38, entrypoint.sh:22), which then serves the secret file over the VNC route.
5. Env such as NODE_OPTIONS=--inspect=0.0.0.0:9229. That opens a debugger on Playwright's Node driver, which handles the credentials.
6. DEBUG=pw:* or similar, which logs values passed to fill() into pod logs.
7. hostAliases or dnsConfig redirecting www.bcferries.com. TLS should stop this, but block it anyway.
8. A second container, shareProcessNamespace, or an ephemeral container (pods/ephemeralcontainers can mount the pod's existing volumes).

Other verbs:
- 'patch/update pods': spec.containers[*].image is mutable on a running Pod, so an image swap gets an attacker image into a pod that already has the secret and the cookies. Also labels and activeDeadlineSeconds can change.
- pods/exec, attach and portforward: direct access. Before 1.35, 'get' was enough over WebSocket. The AuthorizePodWebsocketUpgradeCreatePermission gate (beta, on by default from 1.35) now requires 'create', so on older clusters avoid any wildcard get on pods/*.
- pods/log: anything the app logs.
- pods/proxy and services/proxy: HTTP to :6080 through the API server, which sidesteps your NetworkPolicy and authorization layer.
- nodes/proxy GET: reaches the kubelet's exec endpoints. KEP-2862 fine-grained kubelet authorization is GA in 1.36.
- endpoints/endpointslices create: point a Service the operator owns at another tenant's pod IP.
- serviceaccounts/token create.
- Namespace patch: changes PSA labels.

Minimal operator role: pods create/get/list/watch/delete; secrets create; services and httproutes create/delete (constrained by VAP); events create; tokenreviews create. Nothing else.
refs: https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/reference/access-authn-authz/rbac.md (219-229), https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/concepts/security/rbac-good-practices.md (93-118, 143-150), https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/reference/command-line-tools-reference/feature-gates/AuthorizePodWebsocketUpgradeCreatePermission.md, https://kubernetes.io/blog/2026/04/24/kubernetes-v1-36-fine-grained-kubelet-authorization-ga, https://raw.githubusercontent.com/kubernetes/kubernetes/master/staging/src/k8s.io/apiserver/pkg/registry/generic/registry/store.go (ReturnDeletedObject ~216; DeleteCollection returns listObj), https://raw.githubusercontent.com/kubernetes/kubernetes/master/pkg/registry/core/secret/storage/storage.go:37, Dockerfile:36-38, entrypoint.sh:20-26

## (d) Pod-to-operator auth: projected SA token with a custom audience, checked by TokenReview
Give the browser pod a projected serviceAccountToken with audience 'ferry-watch-operator'. expirationSeconds defaults to 1h and must be at least 600 (projected-volumes.md). The kubelet refreshes the token once it is 80% through its TTL or older than 24h, and the app must re-read the file each time (configure-service-account.md:477-478). Set automountServiceAccountToken: false so the pod has no API-server token at all.

The operator calls TokenReview with spec.audiences=['ferry-watch-operator']. That needs create on tokenreviews.authentication.k8s.io (the system:auth-delegator ClusterRole). status.user.extra returns authentication.kubernetes.io/pod-name, pod-uid, node-name, node-uid and credential-id (JTI=...) (service-accounts-admin.md ~120-160). The JWT carries kubernetes.io.{namespace,pod{name,uid},serviceaccount,node} (~182-200). Pod/node info in tokens has been GA since 1.32 (ServiceAccountTokenPodNodeInfo).

Bound tokens become invalid when the pod is deleted (service-accounts-admin.md ~268-298). The operator should record the pod UID when it creates the pod and map pod-uid to the trip. Cache review results briefly, keyed by token hash. A token for this audience is rejected by the API server, so a compromised operator cannot replay pod tokens against Kubernetes.

Local JWT checking through /openid/v1/jwks is cheaper, but it does not notice pod deletion until the token expires.

Let the pod open the connection (long-poll or WebSocket to the operator). Then NetworkPolicy can deny all ingress to browser pods except gateway to :6080.

Alternatives:
- mTLS with cert-manager's csi-driver (per-pod certificates templated with the pod name), or SPIFFE/SPIRE. Stronger channel binding, more moving parts.
- Per-pod bootstrap Secrets. They require the operator to create mountable Secrets, need manual rotation, and give no pod binding.
refs: https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/reference/access-authn-authz/service-accounts-admin.md, https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/concepts/storage/projected-volumes.md, https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/tasks/configure-pod-container/configure-service-account.md (477-478), https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/reference/command-line-tools-reference/feature-gates/ServiceAccountTokenPodNodeInfo.md, https://www.kubernetes.dev/resources/keps/4193/

## (e) Per-pod VNC routing with authorization at the gateway (traffic never goes through the operator)
Envoy Gateway turns on WebSocket upgrade for every route by default: defaultUpgradeConfig {UpgradeType: 'websocket'} in internal/xds/translator/route.go ~40-46. GEP-1911 (Standard) defines appProtocol kubernetes.io/ws.

SecurityPolicy.extAuth can target a single HTTPRoute, with http or grpc backends and the fields headersToExtAuth, failOpen (default false), recomputeRoute, timeout, contextExtensions and includeRouteMetadata. The last two pass route identity to the authorization service on main; check them against your installed version. For an HTTP authorization service, only Host, Method, Path, Content-Length and Authorization are forwarded unless you list more headers (ext_auth_types.go). Authelia's Envoy Gateway integration uses path '/api/authz/ext-authz/' with headersToBackend Remote-User and Remote-Groups.

ext_authz runs only on the HTTP upgrade request. The open socket is never re-checked: Traefik forum and Envoy docs say this, medium-high confidence. So revocation needs KasmVNC -IdleTimeout or -MaxConnectionTime, or tearing down the pod.

Traefik ForwardAuth (address, authResponseHeaders) also checks only the handshake. Traefik proxies WebSockets natively.

ingress-nginx auth-url works with WebSockets, but the project was retired: best-effort maintenance ended in March 2026. Do not build on it.

oauth2-proxy: /oauth2/auth accepts the query parameters allowed_groups, allowed_emails and allowed_email_domains, so a per-route auth URL can encode the owner. --proxy-websockets defaults to true, but only in reverse-proxy mode, which does carry the traffic.

Authelia access_control supports the named regex groups (?P<User>...) (must equal the username) and (?P<Group>...) (must be one of the user's groups) in domain_regex and resources. For example, domain_regex '^(?P<User>\w+)-[a-z0-9]+\.vnc\.example\.com$' means only that user ever reaches their pods, with no operator involvement.

The KasmVNC client builds its WebSocket URL as ws(s)://host[:port]/ + the 'path' setting. The default is 'websockify' at the host root (ui.js:346, 1942-1977), and it can be overridden with ?path=... because initSetting checks the query string first (ui.js:1466-1470). Xvnc accepts the upgrade on any path: parse_handshake copies the path without checking it (websocket.c). Static files are served relative to -httpd, so a path prefix needs URLRewrite ReplacePrefixMatch or the current symlink trick (Dockerfile:36-38).

A subdomain per pod avoids rewrites and gives each tenant its own browser origin. The cost is wildcard DNS plus a wildcard certificate (DNS-01).
refs: https://raw.githubusercontent.com/envoyproxy/gateway/main/internal/xds/translator/route.go (~35-46), https://raw.githubusercontent.com/envoyproxy/gateway/main/api/v1alpha1/ext_auth_types.go, https://raw.githubusercontent.com/envoyproxy/gateway/main/site/content/en/latest/tasks/security/ext-auth.md, https://raw.githubusercontent.com/kubernetes-sigs/gateway-api/main/geps/gep-1911/index.md, https://raw.githubusercontent.com/authelia/authelia/master/docs/content/integration/kubernetes/envoy/gateway.md (72-136), https://raw.githubusercontent.com/authelia/authelia/master/docs/content/configuration/security/access-control.md (207-223, 707-725), https://raw.githubusercontent.com/oauth2-proxy/oauth2-proxy/master/docs/docs/features/endpoints.md, https://raw.githubusercontent.com/oauth2-proxy/oauth2-proxy/master/docs/docs/configuration/overview.md (proxy_websockets default true)

## (f) KasmVNC: built-in auth, behaviour when basic auth is off, timeouts, one Xvnc per pod
The repo pins KasmVNC 1.5.0 (Dockerfile:20), the newest release on GitHub. It runs Xvnc with -DisableBasicAuth -SecurityTypes None -interface 0.0.0.0 -httpd /opt/vnc-web (entrypoint.sh:20-26).

Basic auth is on by default and reads -KasmPasswordFile (~/.kasmpasswd), written by kasmvncpasswd. Users are view-only by default; -w grants keyboard and mouse, and -o grants owner (manage users). The man page says the stored password is only obfuscated.

With -disableBasicAuth, owner stays 0, so every /api/* owner endpoint returns 401 (websocket.c ~2001-2104). That covers get_screenshot, create_user, update_user, downloads and the rest, so the screenshot and user APIs are NOT exposed by the current flags.

Useful man-page options:
- -IdleTimeout: drops an idle connection.
- -MaxConnectionTime, -MaxDisconnectionTime, -MaxIdleTime: these terminate the server. Combined with entrypoint.sh:38-39 (wait -n; exit 1) they would restart the pod, so avoid them.
- -BlacklistThreshold/-BlacklistTimeout: key on the first X-Forwarded-For entry.
- DLP clipboard and keystroke limits.

The man page documents no path-prefix option; see (e) for the ?path= override.

One Xvnc per pod is sensible. Measured about 60-86 MB proportional memory and about 0% CPU with no viewer connected. A view-only kasmpasswd user could provide 'watch only' links.
refs: https://raw.githubusercontent.com/kasmtech/KasmVNC/master/unix/xserver/hw/vnc/Xvnc.man, https://raw.githubusercontent.com/kasmtech/KasmVNC/master/unix/vncpasswd/vncpasswd.man, https://raw.githubusercontent.com/kasmtech/KasmVNC/master/common/network/websocket.c (parse_handshake; auth ~2001-2104; /api handlers ~1120-1850), https://github.com/kasmtech/KasmVNC/releases, Dockerfile:20, entrypoint.sh:20-39

## (g) Measured footprint: about 0.52-0.57 GB per pod, versus about 35-80 MB per extra context in a shared browser
Method: the repo's base image (playwright/python v1.56.0-noble, same digest as Dockerfile:14) plus KasmVNC 1.5.0 and matchbox, running the same Xvnc flags and the same Chromium launch args as site.py:154-157. Headed, no VNC viewer attached, 4 vCPU host. Memory is proportional set size (PSS) per process group. The test pages were pypi.org pages because bcferries.com was blocked by the egress proxy; real BC Ferries plus Queue-it pages are heavier, so treat these as lower bounds.

Baseline before Chromium: Xvnc ~86 MB, matchbox ~19 MB, Python ~22 MB.
After Chromium launches: ~430-440 MB total. That is Chromium ~240-270 MB across 8 processes, plus the Playwright Node driver ~80 MB.
One context with a page loaded: ~520-570 MB total.
Four contexts in one browser: ~715 MB. Each extra context adds ~35-80 MB.
CPU: idle ~1.5-2.7% of one core. A page load or reload averages ~15-60% of one core while it runs.

The app's real Python process will be larger than the 22 MB stub. Playwright 1.56 already passes --disable-dev-shm-usage (so /dev/shm at 64 MB is fine). It also passes --disable-background-timer-throttling and --disable-renderer-backgrounding, which keep idle tabs using CPU. And because site.py does not set chromium_sandbox, Playwright adds --no-sandbox (chromiumSwitches.ts; chromium.ts:324-325).

Low-memory flags (--renderer-process-limit=1, --disable-gpu, --disable-software-rasterizer, --js-flags=--max-old-space-size=256, --disable-features=site-per-process, --enable-low-end-device-mode) saved only about 7% (566 to 524 MB), and turning off site isolation weakens security. Not worth it.

Bigger levers:
- Run Xvnc, the window manager and headed Chromium only when a cart is held. Searches don't log in (watcher.py:299-300).
- Share anonymous searches across tenants.
- Block heavy resource types via page.route. This may interact with bot detection.

Suggested starting requests and limits: memory 768Mi / 1.5Gi, CPU 50m / 1. Encoding cost with a viewer connected was not measured; estimate 5-25% of a core while the screen changes, low confidence.
refs: Dockerfile:14, src/ferry_watch/bcferries/site.py:154-157, src/ferry_watch/bcferries/site.py:166-168, src/ferry_watch/watcher.py:299-300, https://raw.githubusercontent.com/microsoft/playwright/v1.56.0/packages/playwright-core/src/server/chromium/chromiumSwitches.ts, https://raw.githubusercontent.com/microsoft/playwright/v1.56.0/packages/playwright-core/src/server/chromium/chromium.ts (324-325), https://pyba.readthedocs.io/en/latest/performance.html

## (h) Egress to bcferries.com: only Cilium (open source) or an egress proxy can restrict by hostname
Standard NetworkPolicy only matches ipBlock CIDRs, which is impractical for a site behind a CDN.

Cilium: CiliumNetworkPolicy toFQDNs (matchName or matchPattern) only works alongside a DNS L7 rule (toPorts.rules.dns) on the kube-dns egress rule, so Cilium's DNS proxy learns the IPs. '*.bcferries.com' does not match the bare bcferries.com, so list both.

Calico Open Source: the v3 EntityRule fields are nets, notNets, selector, namespaceSelector, serviceAccounts, services and ports. There is no 'domains' field. DNS policy is Calico Enterprise/Cloud only.

Egress proxy alternative, works with any CNI: Squid, smokescreen or Envoy with a CONNECT allowlist by hostname. Point Chromium at it with Playwright's launch(proxy=...), and add a NetworkPolicy allowing the browser pod's egress only to the proxy, the operator and DNS. That also blocks WebRTC/UDP leaks and gives a hostname audit log.

The allowlist must include queue-it.net (site.py:43) and whatever CDN and analytics hosts BC Ferries uses, which is unknown. Run in audit mode first and collect the hostnames.
refs: https://docs.cilium.io/en/stable/security/dns/, https://raw.githubusercontent.com/projectcalico/calico/master/api/pkg/apis/projectcalico/v3/policy_common.go (EntityRule ~150-215), https://docs.tigera.io/calico-cloud/network-policy/domain-based-policy, src/ferry_watch/bcferries/site.py:43

## (i) Queue-it and bot detection from a single egress IP: what's public and what isn't
The code confirms the Queue-it waiting room (site.py:3, 43, 240-249) and spoofs a desktop Chrome user agent (site.py:44-47).

Public Queue-it countermeasures:
- Data-centre IP lists, with a CAPTCHA or 403 option.
- Proof-of-Work challenges and CAPTCHA.
- IP binding (the same IP must enter and leave the queue).
- Visitor identification (one queue spot per email or login).
- Traffic Access Rules by IP range, ASN and user agent.

I found no public information on which of these BC Ferries enables, any per-IP session limit, or any other bot vendor in front of bcferries.com. Unknown, low confidence either way.

A home residential IP is unlikely to be on a data-centre list. Per-pod browsers do not change the egress IP: every tenant's polling NATs through the same address, so load grows linearly. The default is a 30 s interval (models.py:284), minimum 15 s (models.py:19), ±15% jitter (watcher.py:46), and a 5 s cart poll (watcher.py:47). A block or CAPTCHA would hit every tenant, and the owner's own browsing, at once.

Mitigations:
- A global, operator-enforced rate budget (a token bucket that hands pods 'check now' tickets).
- Deduplicate searches per route and date across tenants. Search is anonymous; login happens only for auto_cart (watcher.py:299-300).
- A higher minimum interval for multi-tenant use.

BC Ferries' terms of use on automated access could not be retrieved; review them.
refs: src/ferry_watch/bcferries/site.py:3-6, src/ferry_watch/bcferries/site.py:43-52, src/ferry_watch/bcferries/site.py:240-249, src/ferry_watch/models.py:19-20, src/ferry_watch/models.py:284, src/ferry_watch/watcher.py:46-47, https://queue-it.com/bot-detection-protection/, https://queue-it.com/blog/proof-of-work-block-bad-bots/

## Cross-cutting: Chromium sandbox and user namespaces
Today all trips share one Chromium (site.py:166-168, one context per trip) running with --no-sandbox, because Playwright adds it whenever chromium_sandbox isn't set. A renderer exploit therefore reaches the process that holds BCF_USER/BCF_PASS (settings.py:40).

Per-pod isolation limits a compromise to one tenant. User namespaces for pods (hostUsers: false) are GA and locked on in 1.36 (UserNamespacesSupport). Re-enabling Chromium's own sandbox inside a container usually still needs a Localhost seccomp profile that allows creating user namespaces. RuntimeDefault typically blocks it. Medium confidence; test it.
refs: src/ferry_watch/bcferries/site.py:154-168, src/ferry_watch/settings.py:40-45, https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/reference/command-line-tools-reference/feature-gates/UserNamespacesSupport.md, https://raw.githubusercontent.com/microsoft/playwright/v1.56.0/packages/playwright-core/src/server/chromium/chromium.ts

IMPLICATIONS:
- Use namespace-per-tenant. Secret volumes resolve only inside their own namespace, so a pod in ns-alice cannot mount Bob's credentials no matter what the operator sends. VAPs can then compare pod, Service and HTTPRoute fields against admin-set namespace labels (namespaceObject.metadata.labels['ferry-watch.io/owner']). The operator gets a ClusterRole bound into tenant namespaces, but no namespace patch.
- Operator RBAC should be: pods create/get/list/watch/delete; secrets create only; services and httproutes create/delete; events create; tokenreviews create. Explicitly NOT: pods update/patch (image is mutable), pods/exec|attach|portforward|proxy|log|ephemeralcontainers|resize, services/proxy, nodes/proxy, endpoints/endpointslices, serviceaccounts/token, secrets get/list/watch/update/patch/deletecollection, namespaces patch, referencegrants.
- The operator must create bare Pods directly, not Deployments or Jobs. Otherwise the pod CREATE request comes from a controller-manager service account and the VAP's request.userInfo match never fires.
- The browser-pod VAP must be a strict allowlist, not a denylist: pinned digest from an admin-owned param; one container; no command/args/lifecycle/exec probes/envFrom; env names from a fixed list with no valueFrom; exact volume and volumeMount pairs (nothing under KasmVNC's -httpd directory); fixed serviceAccountName with automountServiceAccountToken: false; no hostAliases/dnsConfig/runtimeClassName/nodeName/shareProcessNamespace; terminationMessagePolicy File; CREATE only. Add PSA 'restricted' as a backstop. If keeping that correct is too much, move pod construction into a tiny trusted spawner controller and have the operator write only a BrowserSession CR.
- If kopf is used, put persistent handlers on a CRD, such as a BrowserSession whose status the browser pod reports into, or the operator database. Watch Pods only with @kopf.on.event. kopf's progress annotations and finalizers on Pods would force 'patch pods'. Embed kopf.operator() in the existing aiohttp event loop and run one replica, so peering and leader election aren't needed.
- Browser pods authenticate to the operator with a projected token (audience ferry-watch-operator, expirationSeconds about 600-3600, re-read from file). The operator runs TokenReview and maps status.user.extra pod-uid to the trip whose pod it created. Browser pods dial out to the operator, so NetworkPolicy can deny all ingress to them except gateway pods to :6080.
- The pod-to-operator protocol must be typed and minimal: status, availability, cart state. No arbitrary URLs or JS from the operator, and nothing secret upward. Debug snapshots (site.py:407-415) and logs stay in the pod, because pods/log is denied.
- Do not let the operator make the VNC authorization decision if the goal is that the operator 'can't get secrets out'. VNC access is full control of the logged-in session. Prefer static Authelia rules with the named group (?P<User>...) on a per-pod hostname such as <trip>-<user>.vnc.example.com, behind Envoy Gateway's SecurityPolicy extAuth to Authelia /api/authz/ext-authz/. Add a VAP so an HTTPRoute's hostnames must contain the namespace owner and its backendRefs stay in the same namespace.
- Pushover links become https://<trip>-<user>.vnc.example.com/vnc.html?autoconnect=1, with a per-pod subdomain, a wildcard certificate, and the WebSocket at /websockify at that host's root. With path prefixes instead, add ?path=<prefix>/websockify and a URLRewrite. The existing checkout_url plumbing (settings.py:24,63; notify.py:235-244) becomes per-trip.
- Bound live sessions: ext_authz checks only the upgrade request, so set KasmVNC -IdleTimeout and delete the browser pod when the cart is released or expires. Do not use -MaxDisconnectionTime or -MaxIdleTime: they kill Xvnc, which with entrypoint.sh's 'wait -n; exit 1' restarts the pod and loses the cart.
- Credential intake must not pass through the operator's UI if the operator is distrusted. Options: the user logs in to bcferries.com inside the headed browser over VNC, and the session lives in the pod's emptyDir or PVC profile; or a separate, minimal intake service. Either way the operator only creates or holds a reference.
- Split roles to recover scale. Anonymous searching (no credentials needed, per watcher.py:299-300) runs in a shared or headless, rate-limited pool, deduplicated by route and date. A per-tenant headed browser pod with KasmVNC is started only when auto-cart or login is needed, or when a cart is held. Measured cost is about 0.55 GB per headed pod versus about 35-80 MB per extra context.
- The operator should enforce a global request budget, because all pods share one egress IP behind Queue-it.
- Egress: if the CNI is Cilium, use toFQDNs plus a DNS rule for bcferries.com, *.bcferries.com and *.queue-it.net. Otherwise run a hostname-allowlisting forward proxy and pass it to Chromium via Playwright's launch(proxy=...), with NetworkPolicy allowing egress only to the proxy, the operator and DNS.
- The gateway layer cannot be ingress-nginx (retired March 2026). Envoy Gateway or Traefik both handle WebSockets and per-route external authorization.

RISKS:
- If the operator makes VNC authorization decisions, a compromised operator can grant itself or a colluding user the VNC session, which means full control of a tenant's logged-in BC Ferries session. This defeats the 'operator can't get secrets' goal.
- Typing credentials into the operator-served dashboard gives the operator plaintext once, whatever the RBAC. The operator also serves the frontend JS, so it could change the form.
- The pod spec has many ways to leak a mounted secret: exec probes whose output lands in Events, lifecycle hooks, mounts into KasmVNC's -httpd directory, NODE_OPTIONS --inspect on Playwright's Node driver, DEBUG=pw:* logging fill() values, ephemeral containers, an extra container or shareProcessNamespace, and an image swap via patch. Missing any one of these in the VAP breaks isolation.
- pods/proxy and services/proxy reach :6080 through the API server, past NetworkPolicy and the gateway's authorization. They must never be granted. On clusters older than 1.35, 'get' on pods/exec, attach or portforward is enough over WebSocket.
- deletecollection (and update/patch) on secrets return Secret contents in the response; only get/list/watch obviously look like reads. Medium-high confidence for deletecollection, based on API server source.
- Measured cost is about 0.52-0.57 GB of memory per headed pod, against about 35-80 MB per extra context in a shared browser. Real BC Ferries pages are heavier. For a home cluster this is roughly 5-8x the memory at 10+ trips.
- Every browser pod shares one residential egress IP behind Queue-it. Polling grows linearly with tenants and trips, and if it triggers IP rules, CAPTCHA or Proof-of-Work, every tenant fails together, the owner's own browsing included. BC Ferries' bot policy and terms are unknown.
- Chromium runs with --no-sandbox (Playwright's default), so a renderer exploit from any loaded page gets the pod's credentials and cookies. Per-pod isolation contains this but doesn't remove it.
- Envoy, Traefik and NGINX authorization services check only the WebSocket upgrade. Revoking access does not close an open VNC socket.
- kopf peering freezes all replicas of equal priority, and kopf's default progress storage writes annotations and finalizers to handled objects, which tempts adding 'patch pods'.
- Version-dependent features: VAP needs 1.30+, MAP is GA only in 1.36, the WebSocket exec 'create' check arrived in 1.35, and user namespaces are GA in 1.36. Hostname-based egress needs Cilium (or Calico Enterprise) or a forward proxy. The owner's cluster version, CNI and gateway are unknown.
- Losing a pod (eviction, node drain, OOM) loses the held cart and the logged-in session unless the profile sits on a per-tenant PVC. That PVC is then another mountable resource the VAP has to scope.

OPEN_QUESTIONS:
- Which Kubernetes version, CNI (Cilium or Calico/other) and gateway (Envoy Gateway, Traefik, or the retired ingress-nginx) does the home cluster run? This decides whether VAP/MAP, the WebSocket exec check, user namespaces and hostname egress policies are available.
- Should the operator be trusted to make VNC authorization decisions, or should access be static per-user Authelia rules (named group User) so the operator never controls who sees a session?
- Where do tenants enter BC Ferries credentials? Options: the operator UI (operator sees plaintext once); logging in inside the headed browser over VNC, so the operator never sees them; a separate intake service; or no stored credentials at all.
- How trusted are tenants (family and friends versus strangers)? That sets how strict the allowlist must be and whether a separate trusted pod-spawner controller is worth it.
- Namespace-per-tenant (recommended) or one shared namespace with name-pattern policies?
- Per-pod subdomains (wildcard DNS and TLS via cert-manager DNS-01) or path prefixes with URLRewrite and ?path= on the VNC link?
- Accept the measured roughly 0.55 GB per headed pod, or split anonymous shared searching from per-tenant headed 'cart holder' pods started only when needed?
- Should browser profiles and cookies survive pod restarts (a per-tenant PVC, more admission scope) or is re-login after a restart acceptable?
- What polling budget is acceptable from the single home IP? Should the operator deduplicate searches for the same route and date across tenants?
- Stay in Python with kopf, embedded in the aiohttp server and one language, or write the operator in Go with controller-runtime for a shared cache and leader election, accepting a second language?
- Has BC Ferries' terms of use been checked for automated access by multiple accounts from one IP?
