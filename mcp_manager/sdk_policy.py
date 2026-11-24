"""Public SDK single-request candidate. Not wired to a real Runtime in C-3B-1."""


async def sdk_call_once(client, name, arguments):
    """Bypass high-level refresh/retry and input-required driver, without private APIs.

    A protocol error/input-required result is terminal to this attempt. It never
    authorizes a resend. This is not a remote exactly-once execution guarantee.
    """
    from mcp.types import CallToolResult
    result = await client.session.call_tool(name, arguments,
        allow_input_required=False, allow_claimed=False)
    if not isinstance(result, CallToolResult):
        raise RuntimeError('Additional protocol rounds require separate policy review')
    return result
