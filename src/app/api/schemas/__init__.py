"""Pydantic v2 request and response models for the frozen `/v1` contract (D071).

Four modules, split by direction rather than by endpoint: `common` holds what every response
shares, `requests` holds everything a client may send, and `jobs` and `artifacts` hold what
comes back. Inbound models forbid extras and strip whitespace; outbound models are frozen.
"""
