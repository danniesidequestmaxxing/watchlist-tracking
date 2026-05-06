"""Tool JSON schemas exposed to Claude for the catalyst agent (spec §5.4).

Phase 5 only registers `get_token_unlocks` and `get_macro_events`. Phase 6
adds `get_earnings_calendar`, `read_sec_filings`, `search_news`, and
`get_options_expiry`.
"""

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_token_unlocks",
        "description": (
            "Return upcoming token unlock events for a crypto symbol within a date "
            "window. Source: Token Unlocks API."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "days_ahead": {"type": "integer", "default": 30},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_macro_events",
        "description": (
            "Return upcoming macro events (FOMC, CPI, NFP, etc.) within a date "
            "window. Source: Trading Economics."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "default": 14},
                "importance_min": {"type": "integer", "default": 2},
            },
        },
    },
]
