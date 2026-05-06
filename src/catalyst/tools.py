"""Tool JSON schemas exposed to Claude for the catalyst agent (spec §5.4).

Phase 5 registered token_unlocks + macro. Phase 6 adds earnings_calendar,
read_sec_filings, search_news. `get_options_expiry` is not implemented yet.
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
    {
        "name": "get_earnings_calendar",
        "description": ("Return earnings dates for a US equity ticker. Source: Finnhub."),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days_ahead": {"type": "integer", "default": 60},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "search_news",
        "description": ("Return recent news items for a ticker. Source: Finnhub or Polygon News."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "days_back": {"type": "integer", "default": 7},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_sec_filings",
        "description": ("Return recent SEC filings (8-K, 10-Q, S-1) for a ticker via EDGAR. Free."),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "form_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["8-K", "10-Q"],
                },
                "days_back": {"type": "integer", "default": 30},
            },
            "required": ["ticker"],
        },
    },
]
