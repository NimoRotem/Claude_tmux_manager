import browser_mcp_lease_proxy as proxy


def test_only_browser_tool_calls_are_leased():
    assert proxy._request_id_for_browser_call(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "browser_navigate"}}
    ) == 7
    assert proxy._request_id_for_browser_call(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}}
    ) is None
    assert proxy._request_id_for_browser_call(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "unrelated"}}
    ) is None


def test_notifications_without_request_ids_do_not_hold_leases():
    assert proxy._request_id_for_browser_call(
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "browser_snapshot"}}
    ) is None
