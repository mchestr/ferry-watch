"""waitlist-fetcher: the stateless service that makes HTTP providers' requests (design.md §4.4).

waitlist-control decides when a watch is due and asks this service, over the closed fetch wire
(`waitlist.fetchwire`), for one provider query at a time. The fetcher makes the request (https to the provider's
declared hosts, public addresses only), parses the answer with the provider's client, and replies with the typed
inventory or an error kind. It holds no database credential, no Kubernetes token and no secret, so a parser
compromised by a hostile answer can forge only the inventories it returns (design.md §4.1, I6, I7).

- `config`: the JSON config (`WL_FETCHER_CONFIG`).
- `registry`: the provider clients it runs.
- `transport`: the only way a client reaches the network.
- `app`: `POST /v1/fetch`, `GET /livez`, `GET /readyz`, the per-provider caps.
- `main`: the `waitlist-fetcher run` entry point.
"""
