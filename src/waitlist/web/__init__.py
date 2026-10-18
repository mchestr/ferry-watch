"""waitlist-web: the internet-facing app. Sign-in (OIDC), each user's API, checkout tickets and the outbox.

It has no Kubernetes access at all: it reads and writes rows, and waitlist-control acts on them.
"""
