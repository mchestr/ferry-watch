===== identity-security
Verified against github.com/mchestr/home-cluster at commit 0bff2f8.

**Platform versions and nodes.** Kubernetes is v1.36.5 and Talos is v1.13.10, both set in the tuppr upgrade manifests. Cilium is 1.20.2, Envoy Gateway is 1.9.2 and the Gateway API CRDs are v1.6.3. There are 3 MS-01 nodes (i9-13900H, 96GB each), all control-plane, with workloads scheduled on them. The repo cannot confirm the 8-24 GB free for browser pods; that is runtime state.

**Identity today.**
- Authelia 4.39.28 uses lldap as its LDAP backend.
- Authelia is already an OpenID Connect (OIDC) provider with 5 clients. Its ID tokens carry groups, email, preferred_username and name.
- There is a documented 1Password procedure for adding a client.
- No client has a group-restricted authorization policy, so any lldap user who passes 2FA can complete OIDC login to any client.
- Trusted headers (Remote-User, Remote-Groups, Remote-Name, Remote-Email) exist only on the envoy-internal gateway. That gateway is LAN/WireGuard only, via SecurityPolicy internal-secure.
- envoy-external, reached through the Cloudflare tunnel, has no auth in front. Repo convention says an external app must do its own auth, e.g. OIDC through Authelia.
- ferry.chestr.dev is currently internal and matches only the `*.chestr.dev` group:admin two_factor rule. Invitees would be denied.
- Authelia session cookies are scoped to the whole chestr.dev and mikeflix.lol domains. Any VNC origin under those domains would forward users' Authelia session cookie to browser pods.

**Users and email.**
- lldap (image 2024-01-05) has an internal-only web UI. Users and groups are created by hand; the repo has no declarative bootstrap. Groups referenced: admin, media, documents, people.
- Its password-reset email links to an internal-only host, and Authelia's own password reset is disabled. Remote invitees therefore have no self-service way to set a password.
- Email goes through an in-cluster maddy relay to AWS SES. It accepts chestr.dev senders with no auth. Its NetworkPolicy only admits the Authelia and lldap pods.

**Admission and sandboxing.**
- There is no admission policy at all: no ValidatingAdmissionPolicy, no Pod Security labels, no Gatekeeper. Kyverno has a HelmRepository defined but is not installed.
- Privileged pods run in the unlabelled `default` namespace.
- No RuntimeClass, and no gVisor or Kata extension in the Talos schematic.
- Kernel args turn off AppArmor, SELinux, auditd and CPU mitigations (`mitigations=off`).
- The kubelet applies the RuntimeDefault seccomp profile to every pod.
- No user-namespace sysctl or feature gates are set.

**Network policy and secrets.**
- There is no default-deny NetworkPolicy anywhere.
- Envoy-to-pod traffic that crosses nodes is source-NATed to the node IP, which Cilium classifies as kube-apiserver. Network policy alone therefore cannot prove a connection came from the gateway.
- Secrets use ClusterSecretStore `onepassword` (1Password vault "Kubernetes"), with one item per app named after the app. The store has no namespace restriction and is also used for PushSecret writes, so its Connect token can write to the vault.

**Recommendations (detailed below).**
- Use OIDC to Authelia now, behind a pluggable identity interface, so the identity provider can be swapped later.
- Enforce invite-only and admin role in the app, seeded from lldap group claims.
- Put browser pods in a dedicated namespace with Pod Security `restricted`, ValidatingAdmissionPolicies and default-deny networking.
- Put VNC on a separate registrable domain or strip cookies, and authorize each trip at the gateway.
- Treat gVisor and user namespaces as optional Talos changes.

## versions
Kubernetes is v1.36.5 and Talos is v1.13.10, set in the tuppr upgrade CRs. The Talos taskfile reads both versions from those files to render machine config. The owner's 'Kubernetes >= 1.36' claim is verified.
refs: kubernetes/apps/system-upgrade/tuppr/upgrades/kubernetes.yaml:9, kubernetes/apps/system-upgrade/tuppr/upgrades/talos.yaml:24, .taskfiles/talos/Taskfile.yml:8-12
=> Built-in ValidatingAdmissionPolicy (GA) and Pod Security Admission are available with no extra install. Pod user namespaces (hostUsers:false) exist at the API level, but node-level enablement is not in the repo (see sandbox facts).

## versions
Cilium chart is 1.20.2, Envoy Gateway chart 1.9.2, Envoy proxy v1.39.3, and the Gateway API experimental CRDs v1.6.3 (installed via Talos extraManifests). Cilium runs kubeProxyReplacement with the netkit datapath; its standalone envoy DaemonSet and hubble are disabled. Owner's 'Cilium + Envoy Gateway' claim is verified.
refs: kubernetes/apps/kube-system/cilium/app/helmrelease.yaml:16, kubernetes/apps/networking/envoy-gateway/app/ocirepository.yaml:13, kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:29, talos/controlplane.yaml:134-135, kubernetes/apps/kube-system/cilium/app/resources/values.yaml:29-35
=> Envoy Gateway SecurityPolicy features (extAuth, oidc, jwt) and CiliumNetworkPolicy (including toFQDNs for the bcferries.com egress allowlist) are the available tools. Hubble is off, so there is no flow visibility for debugging egress denials unless it is enabled.

## nodes
There are 3 nodes (m0, m1, m2), each an MS-01 i9-13900H with 96GB RAM. All are control-plane nodes and workloads are scheduled on them. Kubelet maxPods is 150. Each node reserves vm.nr_hugepages 1024 (2MiB pages, about 2GiB) for PostgreSQL. The repo does not record free or allocatable memory.
refs: AGENTS.md:7, README.md:138, talos/controlplane.yaml:6, talos/controlplane.yaml:17, talos/controlplane.yaml:79, talos/controlplane.yaml:191
=> Raw RAM is about 288GB, but the 8-24GB budget for browser pods is not in the repo and must be measured live. Browser pods share nodes with etcd and the apiserver, and the repo defines no PriorityClass (only system-* is used by rook-ceph csi). The plan should add a low PriorityClass for browser pods so memory pressure evicts them first.

## authelia-oidc
Authelia 4.39.28 (2 replicas) is already an OIDC provider. identity_providers.oidc defines JWKS and clients Autobrr, Grafana, Immich, Paperless and Windmill (ErsatzTV is commented out). All use PKCE S256, authorization_policy two_factor and claims_policy 'default'. The default claims policy puts groups, email, email_verified, alt_emails, preferred_username and name in the id_token.
refs: kubernetes/apps/default/authelia/app/helmrelease.yaml:104, kubernetes/apps/default/authelia/app/resources/configuration.yaml:114-214, kubernetes/apps/default/authelia/app/resources/configuration.yaml:123-126
=> Ferry Watch can use standard OIDC against https://auth.chestr.dev today, with no new infrastructure. The groups claim is available for admin-role mapping. Because the app would then use plain OIDC (issuer, client id, client secret), it stays IdP-agnostic and swapping providers later is a config change.

## authelia-oidc
No identity_providers.oidc.authorization_policies (custom per-client subject rules) are defined. Every client uses the built-in 'two_factor' policy, which admits any authenticated lldap user regardless of group.
refs: kubernetes/apps/default/authelia/app/resources/configuration.yaml:114-214
=> Adding invitees to lldap gives them OIDC login to every client. Immich (photos.chestr.dev) and Windmill are on envoy-external. Their auto-registration settings are not in the repo. Paperless has PAPERLESS_SOCIAL_AUTO_SIGNUP=true but is internal-only. Before inviting outsiders, add Authelia OIDC authorization_policies that restrict each client to its intended group(s), and give Ferry Watch a policy limited to a ferry group plus admin.

## authelia-oidc
Adding a client follows a documented procedure. Generate the client id and argon2 secret, then store <APP>_OAUTH_CLIENT_ID, <APP>_OAUTH_CLIENT_SECRET and <APP>_OAUTH_CLIENT_SECRET_HASH in the 1Password item 'authelia'. Authelia's ExternalSecret maps the HASH into its env. Consumer apps read the plaintext secret from the same 'authelia' item (Grafana is the example).
refs: kubernetes/apps/default/authelia/app/README.md:10-22, kubernetes/apps/default/authelia/app/externalsecret.yaml:25-46, kubernetes/apps/observability/grafana/instance/externalsecret.yaml:20-38
=> A Ferry Watch client needs: (1) FERRY_WATCH_* fields in the 'authelia' 1Password item, (2) new lines in Authelia's externalsecret.yaml and a clients entry in configuration.yaml, and (3) an ExternalSecret in the ferry-watch namespace extracting the plaintext from 'authelia'. All of these are Flux/Git changes by the owner, not something the operator does.

## authelia-rbac-precedent
Grafana maps OIDC groups to roles with role_attribute_path: groups containing 'admin' gives Admin, 'people' gives Viewer. It uses scopes 'openid profile email groups' and preferred_username as login.
refs: kubernetes/apps/observability/grafana/instance/grafana.yaml:20-35
=> There is precedent for mapping the lldap 'admin' group claim to an app admin role. Ferry Watch can do the same, and should persist the role in its own DB so it survives an IdP swap.

## authelia-access-control
access_control has default_policy two_factor. The ordered rules are: bypass rules for apps with their own login (including lldap.chestr.dev), group:media one_factor on the *arr apps, group:documents on paperless, then '*.chestr.dev' group:admin two_factor, then '*.chestr.dev' deny. ferry.chestr.dev has no specific rule. A 'networks: internal' list (10/8, 172.16/12, 192.168/16) is defined but no rule uses it.
refs: kubernetes/apps/default/authelia/app/resources/configuration.yaml:45-104
=> Today only lldap 'admin' members can reach Ferry Watch. If trusted headers are kept, invitees need a new group and a rule for the Ferry Watch host(s), placed before line 99. Never add bypass paths on Ferry Watch hosts while the app trusts Remote-* headers. Under Envoy ext_authz semantics (not shown in the repo; verify with a forged-header test), a bypassed request gets no Remote-* from Authelia, so a client-supplied Remote-User would pass through.

## authelia-session
Session cookie 'authelia-home-ops' is scoped to domain chestr.dev (portal auth.chestr.dev) and mikeflix.lol (portal auth.mikeflix.lol). Inactivity is 3d, expiration 7d, remember_me 6M. Sessions are stored in authelia-dragonfly. Authelia itself is routed on envoy-external at auth.chestr.dev and auth.mikeflix.lol.
refs: kubernetes/apps/default/authelia/app/resources/configuration.yaml:32-43, kubernetes/apps/default/authelia/app/helmrelease.yaml:201-211
=> Any per-trip VNC origin under chestr.dev or mikeflix.lol receives the user's Authelia session cookie on every request. For an admin, that cookie unlocks every internal app. ext-auth forwards the cookie to Authelia (envoy.yaml:129-134), but the original request, cookie included, still goes upstream to the browser pod. Mitigation: put VNC on a separate registrable domain, or remove the Cookie header with an HTTPRoute RequestHeaderModifier on browser-pod routes. A PWA behind Authelia must also tolerate a 3-day inactivity re-login.

## authelia-regulation
Regulation is max_retries 3, find_time 1h, ban_time 1d. Authelia password reset is disabled (password_reset.disable: true).
refs: kubernetes/apps/default/authelia/app/resources/configuration.yaml:23-30
=> An invitee who mistypes their password 3 times is banned for a day. The Authelia portal also offers no self-service password reset, so onboarding needs another password-setting path.

## envoy-ext-auth
The only SecurityPolicy is 'internal-secure'. It targets the envoy-internal Gateway as a whole, with extAuth failOpen:false to Service default/authelia:80 at path /api/authz/ext-authz/. It sends X-Forwarded-Proto, authorization, proxy-authorization, accept and cookie to Authelia, and returns Remote-User, Remote-Groups, Remote-Name and Remote-Email to the backend. envoy-external has no SecurityPolicy.
refs: kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:122-151, AGENTS.md:113-121, docs/src/notes/networking.md:196-207
=> Trusted headers only exist on envoy-internal, which is LAN/WireGuard only (10.0.20.100). Remote invitees cannot reach it. To keep trusted headers for internet users, the plan would need a new route-level SecurityPolicy on the Ferry Watch HTTPRoute on envoy-external; there is no precedent for one in the repo. Repo convention (AGENTS.md:119) instead says external apps should use their own auth, e.g. OIDC through Authelia.

## envoy-ext-auth
A ReferenceGrant in the authelia namespace (default) lets SecurityPolicy objects only from namespace 'networking' reference Service authelia.
refs: kubernetes/apps/default/authelia/app/referencegrant.yaml:1-13
=> A SecurityPolicy created in a new ferry-watch namespace (e.g. Authelia ext-auth on the external route) cannot reference Authelia until this ReferenceGrant is extended. The same applies if the operator creates per-trip SecurityPolicies that point at the control plane in another namespace: it needs its own ReferenceGrant.

## envoy-client-ip
ClientTrafficPolicy trusts X-Forwarded-For only from 10.42.0.0/16, but the cluster pod CIDR is 10.244.0.0/16, and cloudflared runs as pods.
refs: kubernetes/apps/networking/envoy-gateway/app/envoy.yaml:200-204, talos/controlplane.yaml:122, kubernetes/apps/kube-system/cilium/app/resources/values.yaml:37
=> This looks mismatched (verify live). Tunnelled requests may show the cloudflared pod IP as the client, which falls inside Authelia's 'internal' 10.0.0.0/8 network. Do not build IP-based rate limits, quotas or Authelia network-based bypass rules for Ferry Watch on client IP until this is checked.

## internet-exposure
cloudflared forwards chestr.dev, *.chestr.dev, mikeflix.lol and *.mikeflix.lol to envoy-external. The Cloudflare WAF rule blocks every country except CA, US and CN. The wildcard TLS certs cover only one label (*.chestr.dev, *.mikeflix.lol).
refs: kubernetes/apps/networking/cloudflared/app/resources/config.yaml:5-20, terraform/cloudflare/rulesets.tf:20-25, kubernetes/apps/networking/envoy-gateway/app/certificate.yaml:12-13, kubernetes/apps/networking/envoy-gateway/app/certificate.yaml:25-26
=> Invitees outside CA/US/CN cannot reach the dashboard or VNC. Per-trip VNC hosts must be single-label (e.g. t-<id>.<domain>), or need new certs and tunnel entries. A separate registrable VNC domain needs a new Cloudflare zone, a cert and listener certificateRefs on the gateway, and cloudflared ingress entries; none exist.

## current-ferry-watch
Ferry Watch is deployed single-tenant: ghcr.io/mchestr/ferry-watch:0.5.1 at ferry.chestr.dev on envoy-internal. VNC is served on the same origin under /vnc (port 6080) with no own login and relies on Authelia. The pod runs non-root 1001, drops ALL capabilities, has a read-only rootfs and RuntimeDefault seccomp. Chromium's sandbox is off. The memory limit is 4Gi for up to 6 trips. Secrets come from 1Password items 'ferry-watch' (BCF_USER, BCF_PASS) and 'pushover'.
refs: kubernetes/apps/default/ferry-watch/app/helmrelease.yaml:18-29, kubernetes/apps/default/ferry-watch/app/helmrelease.yaml:53-76, kubernetes/apps/default/ferry-watch/app/helmrelease.yaml:87-112, kubernetes/apps/default/ferry-watch/app/externalsecret.yaml:14-24
=> The securityContext already meets Pod Security 'restricted', so browser pods can copy it and run in a restricted-enforced namespace. Pushover secrets and the shared BCF credentials go away in the multi-tenant design. The /vnc same-origin route must be replaced with per-trip origins.

## network-policy
The existing Ferry Watch NetworkPolicy admits only envoy-internal Envoy pods on 8765 and 6080. A CiliumNetworkPolicy additionally admits the kube-apiserver entity on those ports, because cross-node Envoy traffic is source-NATed to node IPs, which Cilium classifies as kube-apiserver. Egress allows only kube-dns and 0.0.0.0/0 minus RFC1918, CGNAT and link-local. openclaw documents the same SNAT workaround.
refs: kubernetes/apps/default/ferry-watch/app/networkpolicy.yaml:1-69, kubernetes/apps/ai/openclaw/app/networkpolicy.yaml:24-47
=> Network policy cannot prove VNC traffic came from the gateway, because node-originated (SNAT) traffic is also admitted. Each trip's VNC therefore needs app or gateway-layer authorization: a short-lived per-trip token validated by an Envoy Gateway jwt or extAuth SecurityPolicy. The egress pattern is reusable, tightened to toFQDNs for bcferries.com and checkout dependencies.

## network-policy
There is no default-deny NetworkPolicy or CiliumClusterwideNetworkPolicy anywhere. Per-app policies exist only for ferry-watch, openclaw, home-assistant, paperless, smtp-relay and the dragonfly component. The dragonfly component admits port 6379 from every pod in the same namespace (podSelector: {}). Authelia's session store is authelia-dragonfly in namespace default.
refs: kubernetes/components/dragonfly/networkpolicy.yaml:1-27, kubernetes/apps/default/authelia/ks.yaml:11-13, kubernetes/apps/default/authelia/app/resources/configuration.yaml:42-43
=> Browser pods must NOT live in namespace 'default'. Any pod there can reach Authelia's session store on 6379, and there is no default-deny. Use a dedicated namespace with explicit default-deny ingress and egress for browser pods.

## lldap
lldap runs image ghcr.io/lldap/lldap:2024-01-05-alpine-rootless (2 replicas, CNPG Postgres), base DN dc=home,dc=arpa. Its web UI is lldap.chestr.dev on envoy-internal only, and Authelia bypasses it because it has its own login. SMTP password reset is enabled via smtp-relay. Authelia binds as uid=admin,ou=people with LLDAP_LDAP_USER_PASS, and thelounge uses the same admin password. Groups referenced in the repo are admin, media, documents and people. Users and groups are created by hand; the repo has no declarative bootstrap.
refs: kubernetes/apps/default/lldap/app/helmrelease.yaml:44-107, kubernetes/apps/default/lldap/app/externalsecret.yaml:14-24, kubernetes/apps/default/authelia/app/resources/configuration.yaml:5-22, kubernetes/apps/default/authelia/app/resources/configuration.yaml:78, kubernetes/apps/default/thelounge/app/externalsecret.yaml:69-87, kubernetes/apps/observability/grafana/instance/grafana.yaml:34
=> An invite can map to: admin creates the lldap user (email) and adds a 'ferry-watch' group membership in the lldap UI. But lldap's password-reset link points to the LAN-only lldap.chestr.dev, and Authelia's reset is disabled, so remote invitees cannot set their own password. The only lldap credential in the repo is the directory admin bind password. Giving it to the Ferry Watch control plane to automate invites would grant full directory admin; do not do this.

## email
smtp-relay (maddy 0.9.6, 3 replicas) listens on smtp-relay.default.svc.cluster.local:2525 with tls off and no client auth. It accepts senders from chestr.dev and controlforensics.com and rejects others. SMTP_DOMAIN_3 mikeflix.lol is set but unused in maddy.conf. It relays with STARTTLS and auth to AWS SES email-smtp.us-west-2.amazonaws.com:587 (1Password item 'aws-ses'). Authelia sends as 'Authelia <auth@chestr.dev>'. The relay's NetworkPolicy (named 'paperless') admits only authelia and lldap pods on 2525, plus observability on metrics.
refs: kubernetes/apps/default/smtp-relay/app/resources/maddy.conf:6-35, kubernetes/apps/default/smtp-relay/app/helmrelease.yaml:29-47, kubernetes/apps/default/smtp-relay/app/helmrelease.yaml:89-128, kubernetes/apps/default/smtp-relay/app/externalsecret.yaml:16-22, kubernetes/apps/default/authelia/app/helmrelease.yaml:109-111, kubernetes/apps/default/authelia/app/resources/configuration.yaml:106-108
=> The relay can carry Ferry Watch email alerts with a sender such as ferry@chestr.dev. It needs a new ingress rule in smtp-relay's networkpolicies for the control-plane pod. Browser pods must never get egress to it: it is an unauthenticated relay for chestr.dev. Whether SES is out of sandbox, which is required to email arbitrary invitees, is not in the repo.

## secrets
The ClusterSecretStore is named 'onepassword' (1Password Connect at http://onepassword:8080, vault 'Kubernetes': 1) and has no namespace conditions. Items are named after the app (e.g. 'ferry-watch', 'authelia', 'lldap'), with shared items such as 'pushover', 'cloudflare' and 'aws-ses'; fields are UPPER_SNAKE env names. The same store receives PushSecret writes (the chestr-dev-tls wildcard cert), so the Connect token can write. The docs name the store 'onepassword-connect', which is stale.
refs: kubernetes/apps/external-secrets/onepassword/store/clustersecretstore.yaml:1-17, kubernetes/apps/networking/envoy-gateway/app/pushsecret.yaml:4-13, AGENTS.md:125, docs/src/notes/secrets-management.md:33
=> Anyone who can create an ExternalSecret or PushSecret in any namespace can read or write the whole 'Kubernetes' vault. The operator's RBAC must exclude external-secrets.io resources. Consider adding spec.conditions to the store to exclude the browser namespace. Per-user BC Ferries credentials should not flow through 1Password/ESO. App secrets (OIDC client secret, VAPID keys, session key) belong in the 'ferry-watch' item, consumed by a Flux-managed ExternalSecret.

## admission
The repo has no ValidatingAdmissionPolicy, MutatingAdmissionPolicy, Gatekeeper, pod-security.kubernetes.io labels, RuntimeClass, ResourceQuota, LimitRange or hostUsers. A Kyverno HelmRepository is defined, but no HelmRelease uses it, so Kyverno is not installed. The Talos apiServer config sets no admissionControl. Privileged pods run in the unlabelled 'default' namespace (frigate, zigbee2mqtt, zwave-js-ui).
refs: kubernetes/flux/meta/repositories/helm/kyverno.yaml:1-10, talos/controlplane.yaml:150-155, kubernetes/apps/default/frigate/app/helmrelease.yaml:46, kubernetes/apps/default/zigbee2mqtt/app/helmrelease.yaml:55, kubernetes/apps/default/zwave-js-ui/app/helmrelease.yaml:30
=> Effective Pod Security is permissive cluster-wide, and 'default' cannot be switched to restricted. Admission controls for Ferry Watch must be new: Pod Security labels on a dedicated namespace plus built-in ValidatingAdmissionPolicy. Kyverno is optional; installing it is a new dependency, though its Helm repo is already registered.

## namespaces
Each namespace directory gets its Namespace object from components/common/namespace.yaml (name 'not-used', prune disabled), renamed by the namespace's kustomization.yaml 'namespace:' field. New namespace directories under kubernetes/apps/ are discovered automatically.
refs: kubernetes/components/common/namespace.yaml:1-7, kubernetes/apps/default/kustomization.yaml:4-6, AGENTS.md:101
=> A new kubernetes/apps/ferry-watch/ directory gives a dedicated namespace for free. Pod Security labels (enforce=restricted) need a kustomize labels or patch entry on that Namespace object in the new namespace's kustomization.yaml.

## sandbox-kernel
The Talos schematic's system extensions are only siderolabs/i915, intel-ucode, mei and thunderbolt; there is no gVisor or Kata. Kernel args include apparmor=0, security=none, -selinux, mitigations=off, init_on_alloc=0, init_on_free=0 and talos.auditd.disabled=1 ('Less security, faster puter'). The installer is the secureboot image from factory.talos.dev.
refs: talos/schematic.yaml:2-19, talos/controlplane.yaml:47
=> There is no AppArmor or SELinux confinement (pod appArmorProfile is a no-op) and no RuntimeClass sandbox. CPU side-channel mitigations are off on nodes that would run many tenants' Chromium against third-party web content; the owner must accept or change this. gVisor needs siderolabs/gvisor added to the schematic, which produces a new schematic ID and a Talos upgrade or reinstall of each node, plus a RuntimeClass (handler runsc).

## sandbox-kubelet
The kubelet sets defaultRuntimeSeccompProfileEnabled: true. Machine sysctls cover only inotify, networking and hugepages; user.max_user_namespaces is not set. No kubelet or apiserver feature gates and no machine.seccompProfiles are set. diskQuotaSupport is true. kubernetesTalosAPIAccess (os:admin) is granted only to the actions-runner-system and system-upgrade namespaces. Talos config changes are not applied by Flux; they need 'task talos:apply-node' per node.
refs: talos/controlplane.yaml:24, talos/controlplane.yaml:67-79, talos/controlplane.yaml:82-90, talos/controlplane.yaml:85-88, AGENTS.md:139-141
=> RuntimeDefault seccomp already applies to every pod. Project quotas (diskQuotaSupport) back emptyDir sizeLimit for Chromium profiles and /dev/shm. Pod user namespaces (hostUsers:false), and with them Chromium's own sandbox, would need Talos changes: the Talos user-namespace guide sets user.max_user_namespaces (e.g. 11255) plus UserNamespacesSupport gates, and possibly a Localhost seccomp profile via machine.seccompProfiles. All are manual node applies. The live value of the sysctl is not in the repo. Never add the Ferry Watch namespace to kubernetesTalosAPIAccess.

RECOMMENDATIONS:
- Identity integration: build a pluggable identity interface in the control plane and ship OIDC against Authelia now. Use a confidential client 'ferry-watch' with PKCE S256, scopes openid/profile/email/groups, claims_policy default, redirect https://<dashboard-host>/auth/callback, following kubernetes/apps/default/authelia/app/README.md:10-22. Serve the dashboard on envoy-external with the app's own host-only session cookie, which matches AGENTS.md:119. Key users by (iss, sub). Only keep a trusted-header implementation (Remote-User/Remote-Groups/Remote-Email/Remote-Name from envoy.yaml:142-147) as a LAN-only mode for when the route is on envoy-internal. In that mode: accept Remote-* only on the user-facing port, never on the port browser pods report to; add no Authelia bypass paths on Ferry Watch hosts; and test that a forged Remote-User header is overwritten.
- Swapping to social login later: Authelia does not federate to upstream social IdPs. This is general Authelia knowledge, not in the repo; verify for the release in use. A swap therefore means pointing the app's OIDC issuer at a different IdP or broker that supports Google/GitHub/Apple. Because the app speaks plain OIDC and stores invites and roles itself, the swap is a config change (issuer URL, client id/secret, which claim carries groups) plus an optional one-time re-link of (iss, sub) by verified email at first login.
- Invite-only mapping: keep the source of truth in the app. An admin creates an invite (email plus quota) and the first OIDC login with a matching verified email binds (iss, sub). While Authelia/lldap is the IdP, also require a lldap group 'ferry-watch'. Enforce it in two places: an Authelia identity_providers.oidc.authorization_policies entry for the ferry client (subject group:ferry-watch or group:admin, default deny), and the app's check of the groups claim. If trusted headers are used instead, add an access_control rule for the Ferry Watch hosts with those subjects before configuration.yaml:99. The admin creates the lldap user and group membership by hand in the lldap UI. Do not give the control plane the lldap admin bind password (LLDAP_LDAP_USER_PASS) to automate this.
- Before inviting anyone outside the household into lldap, add OIDC authorization_policies restricting the existing clients (Immich and Windmill are internet-facing on envoy-external) to their intended groups. Otherwise any lldap user who passes 2FA can complete OIDC login to them.
- Invitee onboarding: lldap's reset email links to the LAN-only lldap.chestr.dev and Authelia password reset is disabled (configuration.yaml:23-24). Choose one of: (a) the admin sets an initial password and shares it out of band, then the user changes it and enrols TOTP or WebAuthn in the Authelia portal; or (b) enable Authelia's reset_password flow (it already has a notifier and a JWT secret). Do not expose the lldap UI on envoy-external.
- Admin role: map it from the groups claim containing 'admin', the same lldap group Authelia and Grafana already treat as admin (configuration.yaml:99-102, grafana.yaml:34), or from a dedicated 'ferry-watch-admin' group. Persist the role in the app DB, seeded from the claim, so it survives an IdP swap. Per-user quotas live in the app DB. Add a namespace ResourceQuota (max pods, memory) on the browser namespace as a hard ceiling, plus a LimitRange. None exist in the repo today.
- Namespace and network: create a dedicated namespace (e.g. kubernetes/apps/ferry-watch/), never 'default'. In 'default', Authelia's Dragonfly session store accepts 6379 from any pod (components/dragonfly/networkpolicy.yaml:23-27), and privileged pods run there. Label it pod-security.kubernetes.io/enforce=restricted, with audit and warn also restricted. Give browser pods a default-deny CiliumNetworkPolicy. Egress allows only kube-dns plus toFQDNs for bcferries.com and the checkout dependencies; deny the cluster, smtp-relay, the control plane's user-facing port and private ranges. Ingress allows only Envoy pods plus the kube-apiserver entity (the documented SNAT workaround) on the VNC port. Give the control plane an explicit egress rule to smtp-relay and a matching ingress rule in smtp-relay's networkpolicies.
- Per-trip VNC origin: prefer a separate registrable domain so the chestr.dev and mikeflix.lol Authelia session cookies are never sent to browser pods. If VNC must live under chestr.dev, use single-label hosts (the wildcard cert covers one level) and add an HTTPRoute RequestHeaderModifier removing Cookie and Authorization on browser-pod routes. Authorize each trip at the gateway, not just at the network: attach an Envoy Gateway SecurityPolicy to each per-trip HTTPRoute. Either use a jwt provider that validates a short-lived trip-scoped token minted by the control plane (remoteJWKS, extracted from a cookie or query param), or use extAuth to a control-plane authz endpoint. Either way the WebSocket stream goes gateway to pod and only the authorization decision touches the control plane. Cross-namespace backendRefs need ReferenceGrants; the existing one for Authelia admits only the networking namespace.
- Keep the control plane from extracting secrets from browser pods, via admission plus RBAC. Use built-in ValidatingAdmissionPolicies scoped to the browser namespace. (1) Deny CONNECT on pods/exec, pods/attach and pods/portforward, and deny UPDATE of pods/ephemeralcontainers, for everyone except a break-glass group. (2) Constrain operator-created pods to the pinned browser image digest, automountServiceAccountToken=false, no hostPath/hostNetwork/hostPID, no secret or configMap volumes beyond an allowlist, required resource limits within quota bounds, runAsNonRoot, and a required runtimeClassName if gVisor is adopted. Give the operator RBAC for only pods create/delete/get/list/watch, services, and gateway.networking.k8s.io httproutes plus gateway.envoyproxy.io securitypolicies in that namespace. It gets no secrets read, no pods/log if logs may carry secrets, and nothing in external-secrets.io, because the ClusterSecretStore 'onepassword' is unrestricted and writable. Browser pods should authenticate to the control plane's report port with projected, audience-bound ServiceAccount tokens checked via TokenReview, not with shared secrets.
- Sandbox options, from least to most invasive. (1) Pod Security restricted plus the existing securityContext (non-root, drop ALL, read-only rootfs, RuntimeDefault seccomp, already the kubelet default). This works today. (2) Pod user namespaces (hostUsers:false). This needs the Talos sysctl user.max_user_namespaces and possibly the UserNamespacesSupport gates in controlplane.yaml, applied per node with task talos:apply-node. It would also allow re-enabling Chromium's sandbox with a Localhost seccomp profile via Talos machine.seccompProfiles. Test on 1.36. (3) gVisor: add siderolabs/gvisor to talos/schematic.yaml, roll a Talos upgrade per node, and add a RuntimeClass. Benchmark Chromium and KasmVNC under runsc. Flag to the owner that mitigations=off and apparmor=0 in the schematic weaken isolation between tenants' browsers. Add a low PriorityClass for browser pods so node pressure on these control-plane nodes evicts browsers first.
- Email and push: reuse smtp-relay (smtp-relay.default.svc.cluster.local:2525, sender @chestr.dev) for alert and invite email. Confirm the SES account is out of sandbox before relying on it to email invitees. Store the Web Push VAPID key pair, the app session key and the OIDC client secret in the 1Password item 'ferry-watch' (client id and secret per the 'authelia' item convention), read by a Flux-managed ExternalSecret using ClusterSecretStore 'onepassword'. Ignore the stale 'onepassword-connect' name in the docs. If the dashboard stays behind Authelia ext-auth, load the PWA manifest with crossorigin="use-credentials" so the manifest fetch isn't redirected to the login page.

GAPS:
- No admission policy exists: no ValidatingAdmissionPolicy, no pod-security.kubernetes.io namespace labels, no Gatekeeper. Kyverno has only a HelmRepository (kubernetes/flux/meta/repositories/helm/kyverno.yaml) and no HelmRelease.
- No RuntimeClass and no gVisor or Kata system extension in talos/schematic.yaml. AppArmor and SELinux are disabled by kernel args.
- User namespaces are not enabled: no user.max_user_namespaces sysctl, no UserNamespacesSupport feature gates, no machine.seccompProfiles in talos/controlplane.yaml.
- No default-deny NetworkPolicy anywhere, and no dedicated namespace for multi-tenant Ferry Watch (the current app runs in 'default').
- No Authelia OIDC client for Ferry Watch, no 'ferry-watch' lldap group, and no identity_providers.oidc.authorization_policies (OIDC clients are not group-restricted).
- No externally reachable password-setting path for invitees: lldap UI and reset link are envoy-internal only, and Authelia password_reset is disabled.
- smtp-relay's NetworkPolicy admits only authelia and lldap pods (kubernetes/apps/default/smtp-relay/app/helmrelease.yaml:89-128), so the Ferry Watch control plane cannot send mail until it is extended.
- No route-level SecurityPolicy precedent on envoy-external. The Authelia ReferenceGrant admits SecurityPolicies only from the 'networking' namespace.
- No separate registrable domain, cert or cloudflared entry for a VNC origin. The wildcard certs cover only single-label *.chestr.dev and *.mikeflix.lol.
- No PriorityClass, ResourceQuota or LimitRange for workload classes, so nothing protects control-plane nodes from browser memory pressure.
- ClusterSecretStore 'onepassword' has no namespace conditions, and its Connect token is used for PushSecret writes, so any namespace can read and write the whole vault through ESO.
- Envoy ClientTrafficPolicy trusts XFF from 10.42.0.0/16 while the pod CIDR is 10.244.0.0/16. Real client IP handling for tunnelled traffic is unverified.

OPEN_QUESTIONS:
- Will invited users reach Ferry Watch from the internet (envoy-external through the Cloudflare tunnel) or only over LAN/WireGuard (envoy-internal)? This decides whether trusted headers are workable at all; they exist only on envoy-internal.
- Is the AWS SES account out of sandbox mode? If not, email to arbitrary invitee addresses will fail. Not in repo.
- What is the live value of /proc/sys/user/max_user_namespaces on m0, m1 and m2 (check with talosctl read)? Is the owner willing to apply Talos config changes (sysctls, seccompProfiles, gVisor extension) to the nodes?
- Does the owner accept running multi-tenant headless browsers on nodes with mitigations=off, apparmor=0 and security=none, or should those kernel args change for this workload?
- Is a separate registrable domain available (and addable to Cloudflare, cert-manager and cloudflared) for per-trip VNC origins, so Authelia cookies are never sent to browser pods?
- Should invitees become lldap directory users at all? That exposes them to other OIDC clients until authorization_policies exist. Or should Ferry Watch move straight to a social or other OIDC IdP for non-household users?
- Which social IdP(s) are wanted later (Google, Apple, GitHub), and would the owner run a broker (e.g. Dex, Keycloak, Zitadel, Pocket-ID) alongside or instead of Authelia?
- How much memory is actually free across the 3 nodes today (allocatable minus requests)? The repo can't confirm the stated 8-24 GB for browser pods.
- Do Immich and Windmill auto-create accounts on first OIDC login? Their settings are not in the repo, and this determines the risk of adding invitees to lldap.
