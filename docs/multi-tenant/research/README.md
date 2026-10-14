# Planning research notes

Raw research gathered while planning multi-tenant Ferry Watch (October 2026). The design reference cites
these as `[R:<name>]`. Each note lists findings with their sources (code `file:line` in this repo, paths
in `mchestr/home-cluster`, or upstream URLs) and the confidence of anything that could not be checked
directly.

They are evidence, not the plan. Several recommendations in them were superseded by later owner decisions
or by the adversarial review: Pushover was dropped, SQLite gave way to Postgres, and app-native OIDC gave
way to trusted headers. Payments were dropped for a Ko-fi link, so the Stripe material in
`billing-paid-service-implications.md` is moot. Where a note and [../design-reference.md](../design-reference.md)
disagree, the design reference wins.

| Note | Covers |
|---|---|
| `map-core-runtime.md` | State inventory, cross-trip coupling, the agent/operator protocol, fat vs thin agent |
| `map-browser-flow.md` | The bcferries.com flow in `site.py`, what moves into the pod, cart/close/lost semantics |
| `map-api-surface.md` | `server.py`/`views.py`/`notify.py` and what multi-tenancy changes in the API and WebSocket hub |
| `map-frontend.md` | `web/` changes and sizes |
| `map-packaging-ci.md` | Image split, uv workspace, CI matrix, envtest/kind, Helm chart |
| `map-k8s-research.md` | Operator frameworks, VAP sketch, RBAC leak paths, TokenReview, gateways, KasmVNC, measured pod footprint, egress |
| `map-threat-model.md` | Threat model, control-plane split, VNC origin, credential intake ranking |
| `limits-quotas-admin.md` | Identity and roles, per-user limits, cluster caps, queueing, storage |
| `limits-reaping-lifecycle.md` | Pod states, reaping rules, hold/VNC limits, disruptions, failure handling |
| `limits-browser-lockdown.md` | Egress allowlist, Chromium policies, kiosk mode, KasmVNC flags, VNC gating and revocation |
| `billing-guest-mode-credentials.md` | Guest mode, attended sign-in, the live spike plan |
| `billing-paid-service-implications.md` | BC Ferries terms, alert channels (Pushover vs Web Push), identity options, isolation |
| `cluster-deploy-conventions.md` | How home-cluster deploys apps and how ferry-watch is deployed today |
| `cluster-networking.md` | Gateways, Authelia ext-auth, Cloudflare tunnel, Cilium, the SNAT quirk |
| `cluster-identity-security.md` | Authelia/lldap, admission, Talos posture, secrets |
| `cluster-platform-services.md` | CNPG Postgres, logs, alerts, email relay, backups |
