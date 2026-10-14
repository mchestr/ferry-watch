"""ferry-control: the operator. It runs browser pods (the fleet loop) and is the only peer they talk to (the hub).

It holds no tenant secrets and has no way to steer a browser: the protocol has no such messages.
"""
