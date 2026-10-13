===== networking
Networking facts verified from the repo at github.com/mchestr/home-cluster (HEAD 0bff2f8, 2026-10-13).

The owner's stated versions check out. Kubernetes is v1.36.5 and Talos is v1.13.10. The CNI is Cilium 1.20.2 with kube-proxy replacement. Ingress is Envoy Gateway 1.9.2 (Envoy v1.39.3). Gateway API CRDs are v1.6.3 experimental. Memory (8-24 GB for browsers) is outside this area; AGENTS.md:7 only says 3 nodes with 96GB each.

**Gateways.** There are two Gateways in `networking`, both with listeners that have no hostname. Their HTTPS listeners accept routes from any namespace and serve the same certs: a single-level wildcard `*.chestr.dev` and `*.mikeflix.lol`, issued by Let's Encrypt with DNS-01 via Cloudflare.
- `envoy-internal` (10.0.20.100) is LAN/VPN only. It is protected gateway-wide by one SecurityPolicy, `internal-secure`. That policy calls Authelia 4.39.28 by ext-auth, fails closed, and passes Remote-User, Remote-Groups, Remote-Name and Remote-Email to backends.
- `envoy-external` (10.0.20.200) is the only thing the Cloudflare tunnel reaches (`*.chestr.dev` goes to envoy-external). It has no auth at all. The repo convention is that apps on it do their own auth, for example OIDC through Authelia.

**Consequence for invitees.** Users who are not on the LAN can only arrive through envoy-external. So the "trusted headers from the auth proxy" identity model does not exist on the external path today. It must be added, either as a route-scoped ext-auth SecurityPolicy or as app-level OIDC.

**WebSockets.** WebSockets work end to end:
- Cloudflare has websockets on.
- The Envoy request timeouts are 0s (disabled).
- The current single-tenant ferry-watch already serves KasmVNC WebSocket under `/vnc` on envoy-internal.

**Authelia today.** Access is admin-group two-factor, and everyone else is denied on `*.chestr.dev`. Its session cookie domain is `chestr.dev`.

**DNS.** external-dns writes proxied Cloudflare CNAMEs only for HTTPRoutes on envoy-external. It writes UniFi records for all HTTPRoutes.

**Cilium.**
- Native routing, netkit datapath, BPF masquerade, maglev DSR load balancing, BGP to the UDM.
- Bandwidth manager is on with BBR.
- Hubble is on (relay, UI, and dns/drop/http metrics).
- `envoy.enabled: false`. Policy enforcement mode, l7Proxy and the DNS proxy are not set, so they are chart defaults.
- No egress gateway and no cluster-wide default-deny.
- `toFQDNs` is not used anywhere in the repo.
- The existing ferry-watch CiliumNetworkPolicy allows 0.0.0.0/0 minus private ranges. Its DNS rule has no L7 dns rule, so it could not drive `toFQDNs` as written.
- The repo documents that cross-node Envoy traffic arrives SNATed to the node IP and is classified as the `kube-apiserver` entity. Policies therefore have to allow `fromEntities: kube-apiserver` on backend ports.

**Recommended VNC routing.** Use a dedicated VNC origin with single-label hostnames under chestr.dev:
- Either path-per-trip on one host, e.g. `ferry-vnc.chestr.dev/t/<opaque-token>/`, which needs one static DNS record and no per-trip DNS.
- Or a per-trip single-label host, e.g. `fwv-<token>.chestr.dev`, which needs DNS per host or a wildcard record.

Do not use `<x>.vnc.chestr.dev`. The gateway cert and the cloudflared `*.chestr.dev` rule only cover one level. Based on Cloudflare's own behaviour (not in the repo), Universal SSL also only covers one level.

**Egress allowlisting.** Use a per-browser-pod CiliumNetworkPolicy:
- a DNS rule to kube-dns that includes `rules.dns`;
- `toFQDNs` for bcferries.com, *.bcferries.com, *.queue-it.net (the app uses queue-it.net) and the checkout dependencies;
- TCP/UDP 443 only;
- an allow to the control plane's report endpoint.

Then remove the 0.0.0.0/0 rule. Confirm on the live cluster that the agent's DNS proxy is active.

**Blockers or required changes before multi-tenant works:**
1. No auth on envoy-external, and the Authelia ReferenceGrant only admits SecurityPolicies from the `networking` namespace.
2. No Authelia rule or group for non-admin ferry users, and invitees would need LLDAP accounts.
3. The `kube-apiserver`-entity SNAT allowance also admits apiserver pod/service proxy traffic. The operator's RBAC must exclude pods/proxy, services/proxy, exec, port-forward and attach, or the SNAT must be fixed.
4. The gateway listeners accept HTTPRoutes from any namespace for any hostname. An operator that creates HTTPRoutes could hijack other apps' hosts unless an admission policy constrains it.
5. smtp-relay only admits Authelia and LLDAP pods.
6. Cloudflare Rocket Loader and email obfuscation are on zone-wide and may break the PWA or KasmVNC JS.
7. The ClientTrafficPolicy trusts X-Forwarded-For from 10.42.0.0/16, but the pod CIDR is 10.244.0.0/16. Per-client-IP controls on tunnel traffic would see the cloudflared pod IP.

## Kubernetes / Talos version
Kubernetes is pinned to v1.36.5 and Talos to v1.13.10 via tuppr. kube-proxy is disabled in Talos. The API is reached through KubePrism at 127.0.0.1:7445.
refs: kubernetes/apps/system-upgrade/tuppr/upgrades/kubernetes.yaml:9, kubernetes/apps/system-upgrade/tuppr/upgrades/talos.yaml:9, talos/controlplane.yaml:160-161, talos/controlplane.yaml:91-93, kubernetes/apps/kube-system/cilium/app/resources/values.yaml:38-39
=> Owner's claim of Kubernetes >= 1.36 is VERIFIED. ValidatingAdmissionPolicy (GA) and native sidecars are available to the operator design.

## Cilium version and install
Cilium chart 1.20.2 is used by both the Flux HelmRelease and the bootstrap helmfile. Its values come from resources/values.yaml via valuesFrom, and HelmRelease spec.values add Hubble on top. Cilium's own Gateway API is disabled (`gatewayAPI.enabled: false`).
refs: kubernetes/apps/kube-system/cilium/app/helmrelease.yaml:16, kubernetes/apps/kube-system/cilium/app/helmrelease.yaml:23-25, bootstrap/helmfile.yaml:19-34, kubernetes/apps/kube-system/cilium/app/resources/values.yaml:31-32
=> Owner's claim of Cilium is VERIFIED (1.20.2). Envoy Gateway is the only Gateway API implementation, so Cilium is not used for ingress.

## Cilium datapath values
The datapath is configured as follows:
- kubeProxyReplacement: true
- routingMode: native, ipv4NativeRoutingCIDR 10.244.0.0/16, autoDirectNodeRoutes
- bpf.datapathMode: netkit, bpf.masquerade: true
- loadBalancer maglev/dsr, bgpControlPlane enabled, l2announcements disabled
- endpointRoutes enabled, MTU 9000, localRedirectPolicy true
- LB-IPAM pool 10.0.20.0/24, BGP ASN 64514 peering with the UDM (64513)
refs: kubernetes/apps/kube-system/cilium/app/resources/values.yaml:2-9, kubernetes/apps/kube-system/cilium/app/resources/values.yaml:12-13, kubernetes/apps/kube-system/cilium/app/resources/values.yaml:27-28, kubernetes/apps/kube-system/cilium/app/resources/values.yaml:37-48, kubernetes/apps/kube-system/cilium/app/resources/values.yaml:64, kubernetes/apps/kube-system/cilium/config/pool.yaml:7-8, kubernetes/apps/kube-system/cilium/config/bgp.yaml:41-49
=> With pod egress masqueraded to the node IP and no egress gateway, every browser pod reaches bcferries.com from the single home WAN IP. Rate-limiting or bot-detection by BC Ferries/Queue-it applies to all tenants combined.

## Cilium policy enforcement / L7 / DNS proxy
`envoy.enabled: false` is set, so there is no cilium-envoy DaemonSet. `policyEnforcementMode`, `l7Proxy` and `dnsProxy` are not set anywhere and fall to chart defaults. There is no CiliumClusterwideNetworkPolicy, and no `toFQDNs`, `serverNames` or L7 HTTP rule anywhere in the repo.
refs: kubernetes/apps/kube-system/cilium/app/resources/values.yaml:29-30
=> Enforcement is the default mode: a pod is allow-all until some policy selects it, so every browser pod must be selected by explicit ingress and egress policies. toFQDNs depends on the agent DNS proxy (part of the L7 proxy feature, default on), but it is UNTESTED in this cluster. Verify live before relying on it: `cilium config view | grep -E 'enable-l7-proxy|dnsproxy'` and `cilium status`. Whether L7 HTTP rules or TLS SNI (`serverNames`) rules work with `envoy.enabled: false` on 1.20 is not in the repo; verify live.

## Hubble
values.yaml sets `hubble.enabled: false`, but HelmRelease spec.values override it to enabled. Relay and UI are on, and metrics include dns:query, drop, tcp, flow, port-distribution, icmp and http, with ServiceMonitor and dashboards. The Hubble UI is at hubble.chestr.dev on envoy-internal. Bootstrap uses only values.yaml, so Hubble is off until Flux takes over.
refs: kubernetes/apps/kube-system/cilium/app/resources/values.yaml:33-34, kubernetes/apps/kube-system/cilium/app/helmrelease.yaml:27-51, kubernetes/apps/kube-system/cilium/app/httproute.yaml:15-20, bootstrap/helmfile.yaml:32-33
=> Hubble `dns:query` and `drop` metrics can be used in a learning phase to enumerate checkout dependency domains. Afterwards they can alert on browser-pod egress denials.

## Bandwidth manager
`bandwidthManager.enabled: true` with `bbr: true`.
refs: kubernetes/apps/kube-system/cilium/app/resources/values.yaml:3-5
=> The `kubernetes.io/egress-bandwidth` pod annotation is usable to cap per-browser egress. Ingress-bandwidth support, and bandwidth manager behaviour with the netkit datapath, are not in the repo; check with `cilium status | grep BandwidthManager`.

## Cross-node SNAT quirk
Two existing policies document that cross-node Envoy traffic is SNATed to the node IP, which Cilium classifies as the `kube-apiserver` entity because all nodes are control-plane. Both add a CiliumNetworkPolicy with `fromEntities: kube-apiserver` on the backend ports.
refs: kubernetes/apps/default/ferry-watch/app/networkpolicy.yaml:29-51, kubernetes/apps/ai/openclaw/app/networkpolicy.yaml:24-47
=> A browser-pod ingress policy that allows only Envoy pods by label will drop cross-node gateway traffic, so the VNC port must also allow `kube-apiserver`. That entity also matches the apiserver's pod/service proxy and other node-originated traffic. The operator ServiceAccount must therefore have no pods/proxy, services/proxy, pods/exec, pods/portforward, pods/attach or secrets access in browser namespaces. The repo does not say whether other cross-node pod-to-pod traffic (e.g. control plane to browser) is also SNATed; verify with Hubble.

## Envoy Gateway version and deployment
Envoy Gateway is chart 1.9.2 from docker.io OCI, with provider deploy type GatewayNamespace. GatewayClass `envoy` points to EnvoyProxy `envoy`, which runs 2 replicas of envoy v1.39.3 (1Gi memory limit) with externalTrafficPolicy Local and a 180s drain.
refs: kubernetes/apps/networking/envoy-gateway/app/ocirepository.yaml:12-14, kubernetes/apps/networking/envoy-gateway/app/helmrelease.yaml:17-22, kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:1-43
=> Owner's claim of Envoy Gateway is VERIFIED (1.9.2). Envoy pods live in `networking` with label `gateway.envoyproxy.io/owning-gateway-name=<gateway>`, which browser-pod ingress policies should select.

## Gateways and listeners
There are two Gateways in `networking`:
- `envoy-external`: LB 10.0.20.200, external-dns target external.chestr.dev.
- `envoy-internal`: LB 10.0.20.100, target internal.chestr.dev.
Both have listener `http`:80 (allowedRoutes from Same, used only by the https redirect route) and listener `https`:443 (allowedRoutes from All). Neither listener sets a hostname. Both use certificateRefs chestr-dev-tls and mikeflix-lol-tls.
refs: kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:44-80, kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:81-120, kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:221-241
=> A new namespace (e.g. ferry-watch) can attach per-trip HTTPRoutes to either gateway without editing the Gateway. The same openness lets anything that can create HTTPRoutes claim any chestr.dev host or path, so an operator with HTTPRoute RBAC needs an admission guard, e.g. a ValidatingAdmissionPolicy limiting hostnames, parentRefs and backendRefs.

## Certificates
Certificate `chestr-dev` covers [chestr.dev, *.chestr.dev] and `mikeflix-lol` covers [mikeflix.lol, *.mikeflix.lol], both from ClusterIssuer letsencrypt-production. chestr-dev-tls is also pushed to 1Password with a PushSecret. There is no multi-level wildcard cert.
refs: kubernetes/apps/networking/envoy-gateway/app/certificate.yaml:1-26, kubernetes/apps/networking/envoy-gateway/app/pushsecret.yaml:1-29
=> `<anything>.chestr.dev` (one label) is already covered by TLS. `<x>.vnc.chestr.dev` is NOT covered; it would need a new Certificate for `*.vnc.chestr.dev` added to the listener certificateRefs or a new listener.

## cert-manager issuers
cert-manager is v1.21.2. ClusterIssuers letsencrypt-production and letsencrypt-staging both use ACME DNS-01 via the Cloudflare API token, selected for dnsZones chestr.dev, controlforensics.com and mikeflix.lol. DNS-01 checks use DoH recursive resolvers 1.1.1.1/1.0.0.1 only.
refs: kubernetes/apps/cert-manager/cert-manager/app/helmrepository.yaml:12-14, kubernetes/apps/cert-manager/cert-manager/issuers/letsencrypt-production-issuer.yaml:6-22, kubernetes/apps/cert-manager/cert-manager/issuers/letsencrypt-staging-issuer.yaml:6-22, kubernetes/apps/cert-manager/cert-manager/app/resources/values.yaml:5-6
=> Wildcard certs at any depth (e.g. `*.vnc.chestr.dev`) can be issued in-cluster. The limiting factor for multi-level subdomains is the Cloudflare edge and tunnel, not cert-manager.

## Authelia ext-auth wiring
SecurityPolicy `internal-secure` targets the whole Gateway `envoy-internal`, so it applies gateway-wide rather than per route. Its settings:
- extAuth.http backend: Service default/authelia:80, path /api/authz/ext-authz/
- failOpen: false
- headersToExtAuth: X-Forwarded-Proto, authorization, proxy-authorization, accept, cookie
- headersToBackend: Remote-User, Remote-Groups, Remote-Name, Remote-Email
A ReferenceGrant in `default` allows only SecurityPolicies from namespace `networking` to reference Service authelia. This is the only SecurityPolicy in the repo.
refs: kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:121-151, kubernetes/apps/default/authelia/app/referencegrant.yaml:1-14
=> On envoy-internal every route, including WebSocket upgrades and new per-trip VNC routes, automatically gets Authelia, and the backend receives Remote-User, Remote-Groups and Remote-Email. A SecurityPolicy created in a ferry namespace that points at Authelia needs the ReferenceGrant extended to that namespace.

## Gateway auth model per repo convention
AGENTS.md and the networking docs state: envoy-internal (the default) is LAN/VPN only and sits behind Authelia. envoy-external is the internet via the Cloudflare tunnel with no auth in front, so only apps with solid auth of their own (e.g. OIDC through Authelia) go there. App NetworkPolicies should only admit Envoy pods of their gateway.
refs: AGENTS.md:111-121, docs/src/notes/networking.md:192-207
=> Invite-only users who are not on the home network can only arrive via envoy-external, where no trusted Remote-* headers exist today. The plan must either add a route-scoped ext-auth SecurityPolicy on envoy-external (a new pattern in this repo) or make the app an OIDC client of Authelia (the existing convention). Header-trust without one of those would be spoofable on external.

## Authelia access control and session
Authelia is 4.39.28 with an LLDAP backend.
- default_policy is two_factor.
- The last two rules are `*.chestr.dev`: group:admin gets two_factor, everyone else is denied.
- ferry.chestr.dev has no specific rule.
- Session cookie domains are chestr.dev (authelia_url auth.chestr.dev) and mikeflix.lol.
- Authelia is also an OIDC provider with clients (Grafana, Immich, ...).
- Authelia itself is on envoy-external at auth.chestr.dev.
refs: kubernetes/apps/default/authelia/app/helmrelease.yaml:50, kubernetes/apps/default/authelia/app/resources/configuration.yaml:5-22, kubernetes/apps/default/authelia/app/resources/configuration.yaml:32-44, kubernetes/apps/default/authelia/app/resources/configuration.yaml:45-104, kubernetes/apps/default/authelia/app/resources/configuration.yaml:114-130, kubernetes/apps/default/authelia/app/helmrelease.yaml:147-153
=> Today only admin-group users can reach ferry hosts. Invitees need LLDAP accounts, a group (e.g. `ferry`), and access_control rules for the dashboard and VNC hosts placed above the `*.chestr.dev` deny. Because the session cookie is scoped to chestr.dev, every per-trip VNC host under chestr.dev receives the user's Authelia session cookie unless the route strips it. Authelia only gates by user/group; it cannot express 'trip X belongs to user Y'. A later OIDC swap is easy because Authelia is already an OIDC provider.

## Envoy traffic policies (timeouts, WebSocket-relevant)
BackendTrafficPolicy `envoy` applies to all Gateways via targetSelectors. It sets compression (zstd/brotli/gzip), tcpKeepalive and http requestTimeout 0s, and replaces 502/503/504 bodies with an HTML error page. ClientTrafficPolicy `envoy` sets XFF trustedCIDRs [10.42.0.0/16], http3 enabled, requestReceivedTimeout 0s, TLS min 1.2 and ALPN h2/http1.1.
refs: kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:153-193, kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:195-219
=> Long-lived VNC WebSockets are not cut by route timeouts. The 502-504 override replaces any JSON 5xx body from the control plane API with HTML, so API clients should not rely on 5xx bodies. The trusted XFF CIDR does not match the pod CIDR (see next fact).

## XFF trusted CIDR mismatch
The ClientTrafficPolicy trusts X-Forwarded-For from 10.42.0.0/16. The cluster pod subnet is 10.244.0.0/16, which is also Cilium's ipv4NativeRoutingCIDR. Cloudflared pods therefore sit outside the trusted range.
refs: kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:201-204, talos/controlplane.yaml:122-123, kubernetes/apps/kube-system/cilium/app/resources/values.yaml:37
=> For tunnel traffic Envoy will most likely treat the cloudflared pod IP as the client. Per-client-IP rate limiting or abuse controls at the gateway (e.g. BackendTrafficPolicy rateLimit by source IP) would not work as intended until this is fixed, or until Cf-Connecting-IP is used.

## Cloudflare tunnel / external access
cloudflared 2026.10.0 runs as 2 replicas with TUNNEL_ORIGIN_ENABLE_HTTP2 and post-quantum enabled, using a locally managed config (Terraform `config_src = local`). Ingress rules send chestr.dev and `*.chestr.dev` (and the mikeflix.lol equivalents) to https://envoy-external.networking.svc.cluster.local, with originServerName external.chestr.dev; the catch-all is http_status:404. DNSEndpoint creates external.chestr.dev as a CNAME to `<tunnel-id>.cfargotunnel.com`. No Cloudflare Access applications exist in Terraform.
refs: kubernetes/apps/networking/cloudflared/app/helmrelease.yaml:14-35, kubernetes/apps/networking/cloudflared/app/resources/config.yaml:1-20, kubernetes/apps/networking/cloudflared/app/dnsendpoint.yaml:8-14, terraform/cloudflare/tunnel.tf:1-6
=> Only envoy-external is reachable from the internet. Any single-label `<x>.chestr.dev` host works through the tunnel with no tunnel change. A two-level `<x>.vnc.chestr.dev` needs a new cloudflared ingress rule, and (Cloudflare behaviour, not in repo) an edge certificate beyond Universal SSL, such as Advanced Certificate Manager.

## WebSockets end to end
The Cloudflare zone has websockets on. The current ferry-watch serves KasmVNC's viewer and WebSocket on :6080 under /vnc on ferry.chestr.dev via envoy-internal, as a PathPrefix /vnc rule to the vnc service port with no rewrite. Home Assistant is on envoy-external. Inference, not proven in repo: its frontend uses WebSockets, so WebSockets through the tunnel likely work.
refs: terraform/cloudflare/zone_settings.tf:71-75, kubernetes/apps/default/ferry-watch/app/helmrelease.yaml:25-27, kubernetes/apps/default/ferry-watch/app/helmrelease.yaml:84-112, kubernetes/apps/default/home-assistant/app/helmrelease.yaml:99-112, Dockerfile:36-38, entrypoint.sh:4-5, entrypoint.sh:20-22
=> KasmVNC over Envoy Gateway already works internally, including WebSocket upgrades through ext-auth. KasmVNC currently expects to be served under `/vnc`. A path-per-trip scheme needs a URLRewrite (ReplacePrefixMatch) or a per-pod httpd path; a host-per-trip scheme can keep `/vnc` unchanged.

## Cloudflare zone settings that affect a PWA/VNC
Zone-wide settings: rocket_loader on, email_obfuscation on, browser_check on, ssl strict, min TLS 1.2, http3 on. A custom firewall rule blocks every country except CA, US and CN. Cloudflare records created by external-dns are proxied (`--cloudflare-proxied`).
refs: terraform/cloudflare/zone_settings.tf:6-15, terraform/cloudflare/zone_settings.tf:31-35, terraform/cloudflare/zone_settings.tf:56-60, terraform/cloudflare/zone_settings.tf:91-95, terraform/cloudflare/rulesets.tf:14-19, kubernetes/apps/networking/external-dns/cloudflare/helmrelease.yaml:31
=> Rocket Loader rewrites script tags and may break the PWA, service-worker registration or the KasmVNC client JS. Email obfuscation rewrites addresses in HTML. Both should be disabled for ferry hosts via a Terraform configuration rule. Invitees outside CA/US/CN are blocked at the edge.

## external-dns
external-dns-cloudflare (chart 1.23.0, image v0.23.0):
- sources crd (DNSEndpoint) and gateway-httproute, with `--gateway-name=envoy-external` and `--cloudflare-proxied`
- policy sync, txtPrefix `k8s.`
- domainFilters chestr.dev, controlforensics.com, mikeflix.lol

external-dns-unifi (webhook v0.9.0):
- sources gateway-httproute and service, with no gateway filter
- triggerLoopOnEvent, policy sync
- domainFilters chestr.dev, mikeflix.lol

The annotation `external-dns.kubernetes.io/controller: none` is used to opt a route out.
refs: kubernetes/apps/networking/external-dns/cloudflare/helmrelease.yaml:17-45, kubernetes/apps/networking/external-dns/unifi/helmrelease.yaml:18-51, kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:221-227
=> Every per-trip HTTPRoute with its own hostname would create and delete a Cloudflare CNAME (if on envoy-external) and a UniFi record. That means DNS churn and propagation latency during a time-critical cart hold. Prefer static hostnames (path-per-trip), or mark per-trip routes `controller: none` and pre-create DNS (a DNSEndpoint for the VNC host, or a wildcard record).

## DNS resolution path for pods
CoreDNS is a separate Helm release with label k8s-app=kube-dns and service IP 10.245.0.10. It forwards to /etc/resolv.conf, which is Talos hostDNS and then 10.0.40.1 (the UDM). forwardKubeDNSToHost is false.
refs: kubernetes/apps/kube-system/coredns/app/resources/values.yaml:4-9, kubernetes/apps/kube-system/coredns/app/resources/values.yaml:32-33, talos/controlplane.yaml:44, talos/controlplane.yaml:94-97
=> A toFQDNs policy's DNS rule should select `k8s:io.kubernetes.pod.namespace: kube-system, k8s-app: kube-dns` on port 53, as the existing policy already does, and must add `rules.dns`. The UDM split-horizon answers chestr.dev names with internal LB IPs.

## Existing ferry-watch networking
The app runs in namespace `default` on envoy-internal at ferry.chestr.dev. A k8s NetworkPolicy admits only envoy-internal Envoy pods on 8765/6080, and a CiliumNetworkPolicy additionally allows the kube-apiserver entity on 8765/6080. Egress is: kube-dns port 53 ANY with no `rules.dns`, plus toCIDRSet 0.0.0.0/0 except 10/8, 172.16/12, 192.168/16, 100.64/10 and 169.254/16. Alerts use Pushover secrets from 1Password.
refs: kubernetes/apps/default/ferry-watch/app/networkpolicy.yaml:1-69, kubernetes/apps/default/ferry-watch/app/externalsecret.yaml:14-24, kubernetes/apps/default/ferry-watch/ks.yaml:5-6
=> There is no FQDN allowlisting today. Browser egress to any public host is allowed, and DNS is not proxied, so toFQDNs would not learn IPs from this rule as written. Migrating to multi-tenant should replace this with FQDN-based policies and drop the Pushover secret.

## Email relay
An in-cluster maddy smtp-relay listens at smtp-relay.default.svc.cluster.local:2525. Authelia uses it. Its NetworkPolicy admits SMTP only from authelia and lldap pods in `default`, plus metrics from observability.
refs: kubernetes/apps/default/authelia/app/helmrelease.yaml:55-57, kubernetes/apps/default/smtp-relay/app/helmrelease.yaml:89-123
=> Email alerts from the control plane can use this relay, but its ingress rules must be extended to admit the control-plane pod. The control plane's own egress policy must allow it.

## Gateway API CRDs
Gateway API v1.6.3 experimental-install is applied by both Flux meta CRDs and Talos extraManifests.
refs: kubernetes/flux/meta/crds/kustomization.yaml:5-6, talos/controlplane.yaml:134-135
=> HTTPRoute, URLRewrite and RequestHeaderModifier filters are available, plus experimental kinds such as TLSRoute. Per-trip routing can be done entirely with HTTPRoute.

## Multus
Multus v4.3.1 is installed, with a single macvlan NetworkAttachmentDefinition `multus-iot` on bond0.10 (IoT VLAN 10, gateway 10.0.10.1).
refs: kubernetes/apps/networking/multus/app/helmrelease.yaml:13-36, kubernetes/apps/networking/multus/config/iot.yaml:1-27
=> Not useful for browser egress isolation, and it would bypass Cilium policy on the secondary interface. Do not attach browser pods to it.

## App egress dependencies seen in app repo
The ferry-watch app references bcferries.com / www.bcferries.com and Queue-it (`QUEUE_HOST = "queue-it.net"`; bcferries.queue-it.net appears in docs and tests).
refs: src/ferry_watch/bcferries/site.py:43
=> The browser egress allowlist must include *.queue-it.net in addition to bcferries.com and *.bcferries.com. Payment and checkout third parties are not listed anywhere and must be discovered, e.g. with Hubble dns:query in audit mode.

RECOMMENDATIONS:
- Gateway placement: invitees are off-LAN, so put the multi-tenant dashboard/API and the VNC routes on envoy-external, using a new dedicated namespace rather than `default`. Never attach them there without auth.
- External auth, option A (keeps the trusted-header model): one static SecurityPolicy in the ferry namespace whose targetSelectors pick HTTPRoutes by label (e.g. `ferry-watch.io/auth: authelia`), with the same extAuth block as `internal-secure` (envoy.yaml:127-147). Extend the ReferenceGrant at kubernetes/apps/default/authelia/app/referencegrant.yaml:7-10 to admit SecurityPolicies from that namespace, so the operator never has to create SecurityPolicies itself. targetSelectors are already used by the BackendTrafficPolicy at envoy.yaml:163-165; confirm SecurityPolicy supports them in EG 1.9.
- External auth, option B (matches the AGENTS.md convention): make the control plane an OIDC client of Authelia, which is already an OIDC provider. Either way, put identity behind one IdentityProvider interface: TrustedHeaderProvider (Remote-User, Remote-Groups, Remote-Email) today, OIDC later.
- Authelia: create an LLDAP group (e.g. `ferry`) and add access_control rules for the dashboard and VNC hosts for `group:ferry` above the `*.chestr.dev` deny (configuration.yaml:99-104). Decide one_factor vs two_factor for invitees. Derive admin from Remote-Groups containing `admin`.
- VNC routing, recommended: one dedicated single-label VNC origin, separate from the dashboard origin, e.g. `ferry-vnc.chestr.dev/t/<128-bit-opaque-token>/`. Create the DNS record once (static HTTPRoute or DNSEndpoint), and have the operator create and delete one HTTPRoute per cart hold with a URLRewrite ReplacePrefixMatch to `/vnc/` and the annotation `external-dns.kubernetes.io/controller: none`. It is covered by the existing `*.chestr.dev` cert, the cloudflared `*.chestr.dev` rule and the Cloudflare edge cert, and adds no DNS latency.
- VNC routing, alternative if per-trip origin isolation is required: single-label hosts like `fwv-<token>.chestr.dev`. Avoid `<x>.vnc.chestr.dev`: it needs a new cert plus listener change, a cloudflared ingress rule, and a paid Cloudflare edge cert. Per-host DNS via external-dns adds latency during a cart hold, so pre-create a proxied wildcard record if you go this way.
- Per-trip authorization: Authelia can only check the group, so add owner binding. Options: the unguessable token alone as a capability, or EG SecurityPolicy JWT validation of a short-lived per-trip token minted by the control plane (from a query param or cookie, JWKS served by the control plane), so the data path stays gateway to browser pod.
- Strip `Cookie` and `Authorization` on VNC routes with a RequestHeaderModifier so browser pods never receive the user's chestr.dev Authelia session cookie. Verify in EG 1.9 that ext-auth still sees the cookie, i.e. the route header mutation applies after ext_authz.
- Add a ValidatingAdmissionPolicy (GA in K8s 1.36) on HTTPRoutes in the ferry namespace that only allows the VNC hostname pattern, parentRef envoy-external/https, and same-namespace backendRefs. This stops a compromised operator from hijacking other `*.chestr.dev` apps on the shared `from: All` listener.
- Browser pod ingress: a CiliumNetworkPolicy selecting browser pods that allows only Envoy pods of the chosen gateway (label `gateway.envoyproxy.io/owning-gateway-name`) on the VNC port. Add `fromEntities: kube-apiserver` on that port only, because of the documented cross-node SNAT. Optionally create or delete a per-trip ingress CNP together with the HTTPRoute so VNC is blocked at L3/L4 when no cart is held. Never allow ingress from the control plane: browser pods report outbound to it.
- Operator RBAC, because of the kube-apiserver entity allowance: the operator ServiceAccount must not have pods/proxy, services/proxy, pods/exec, pods/attach, pods/portforward or secrets get/list in browser namespaces. Browser credentials should be delivered by a path the operator cannot read, e.g. ExternalSecret into a per-user Secret mounted only by the pod, with RBAC deny for the operator.
- Browser egress policy: an egress CiliumNetworkPolicy with (1) toEndpoints kube-dns on 53/UDP+TCP with `rules: dns: [{matchPattern: '*'}]` (or a narrower list), so the DNS proxy learns IPs; (2) toFQDNs matchName bcferries.com and www.bcferries.com, matchPattern *.bcferries.com and *.queue-it.net, plus discovered checkout dependencies, on TCP 443 (and UDP 443 only if QUIC is wanted); (3) toEndpoints or toServices for the control plane report endpoint. Remove the 0.0.0.0/0 toCIDRSet. Disable Chromium DoH and, optionally, QUIC.
- Discover checkout dependencies first: run a browser pod with a permissive FQDN pattern and collect Hubble dns:query flows (`hubble observe --namespace <ns> --protocol dns`), then lock down. Add a PrometheusRule on hubble_drop_total for the browser namespace to catch breakage when BC Ferries adds a third party.
- Before relying on toFQDNs, verify on the live cluster that the L7 and DNS proxy are active (`cilium status`, `cilium config view | grep -E 'l7-proxy|dnsproxy'`). Note that toFQDNs is L3: if bcferries.com sits on shared CDN IPs, other CDN-hosted sites on those IPs remain reachable. SNI (`serverNames`) filtering requires Envoy and its availability with `envoy.enabled: false` must be verified.
- Use `kubernetes.io/egress-bandwidth` on browser pods (bandwidth manager is on) after checking `cilium status | grep BandwidthManager` with netkit.
- Control-plane egress policy: allow the Web Push services (FCM, Mozilla autopush, Apple web push, WNS), the smtp-relay on :2525 (and extend smtp-relay's NetworkPolicy at helmrelease.yaml:96-123 to admit it), and the kube-apiserver entity for the operator. Allow nothing toward browser pods.
- Cloudflare (Terraform): add a configuration rule turning off Rocket Loader and email obfuscation for the ferry hosts. Optionally consider a rate-limiting rule for the API.
- Fix ClientTrafficPolicy trustedCIDRs (10.42.0.0/16 to 10.244.0.0/16), or use Cf-Connecting-IP, before building any per-client-IP limits.
- Keep VNC routes on envoy-external only if the per-route SecurityPolicy (or app OIDC) and per-trip token are in place. Otherwise the KasmVNC viewer, which has no password (entrypoint.sh:4-5), would be open to the internet.

GAPS:
- No authentication on envoy-external. The only SecurityPolicy (internal-secure) targets envoy-internal, and the Authelia ReferenceGrant only admits SecurityPolicies from the `networking` namespace.
- No Authelia group or access_control rule for non-admin ferry users. `*.chestr.dev` is admin-only, everyone else is denied, and invitees would need LLDAP accounts.
- No toFQDNs / DNS-proxy policy anywhere in the repo, so it is unproven on this cluster. The existing ferry-watch DNS egress rule lacks `rules.dns`.
- No CiliumClusterwideNetworkPolicy or default-deny. Policy enforcement mode is the chart default.
- No cert, cloudflared rule or Cloudflare edge coverage for multi-level subdomains (`*.vnc.chestr.dev`).
- No wildcard DNS record in Cloudflare or UniFi. external-dns creates one record per HTTPRoute hostname.
- No admission control (VAP or Kyverno deployment) restricting HTTPRoute hostnames. Listeners accept routes from all namespaces for any host. A Kyverno HelmRepository exists in flux/meta but nothing deploys it.
- No Cilium egress gateway or alternate egress IP. All browsers egress from the single home WAN IP.
- smtp-relay NetworkPolicy does not admit any ferry/control-plane pod.
- ClientTrafficPolicy XFF trustedCIDRs (10.42.0.0/16) does not match the pod CIDR (10.244.0.0/16).
- No Cloudflare configuration rule to disable Rocket Loader or email obfuscation for app hosts.
- Root cause of cross-node Envoy SNAT is not documented in the repo, only the workaround (fromEntities: kube-apiserver).

OPEN_QUESTIONS:
- Is the Cilium L7/DNS proxy actually running with `envoy.enabled: false` on 1.20.2? Check `cilium status` and `cilium config view` on the live cluster. Are `serverNames` (TLS SNI) rules usable?
- Why is cross-node Envoy-to-pod traffic SNATed to node IPs? Is all cross-node pod-to-pod traffic affected, e.g. control plane to browser pod? This decides whether L3 isolation between the control plane and browser pods holds.
- Should the multi-tenant app be on envoy-external with a gateway ext-auth SecurityPolicy (new pattern), or should the app do OIDC against Authelia itself (the AGENTS.md convention for external apps)?
- In Envoy Gateway 1.9, does a route-targeted SecurityPolicy fully override or merge with the Gateway-targeted `internal-secure` on envoy-internal? Does SecurityPolicy support targetSelectors by label?
- Does Authelia's `*.chestr.dev` rule match multi-level subdomains? This only matters if `<x>.vnc.chestr.dev` is chosen.
- Is a proxied wildcard DNS record (or per-trip DNS churn) acceptable in the chestr.dev Cloudflare zone, or is path-per-trip on one VNC host preferred?
- Which third-party domains does the BC Ferries checkout load (payment processor, CAPTCHA, CDNs, analytics that block on failure)? Not in either repo.
- Will BC Ferries or Queue-it throttle or flag many concurrent headed browsers from one residential IP? Is an egress IP strategy needed?
- Should invitees require two_factor (Authelia default) or one_factor?
- Is the per-trip owner check enforced at the gateway (JWT SecurityPolicy) or by capability token only?
