"""Classify only explicit object-level provider errors, never messages or search absence."""

import json


class ConfirmedSourceRevocation(ValueError):
    """Explicit deletion/access evidence; authentication failures are not revocations."""

    def __init__(self, message, *, source=None, source_id=None):
        super().__init__(message)
        self.source = source
        self.source_id = source_id


def raise_confirmed_access_loss(entry):
    response = entry.get("response", {})
    tool = entry.get("tool")
    if tool not in {
        "mcp__claude_ai_Microsoft_365__read_resource",
        "mcp__claude_ai_Atlassian_Rovo__getJiraIssue",
    }:
        return
    if response.get("is_error") is not True and response.get("isError") is not True:
        return
    content = response.get("content")
    if isinstance(content, list):
        if (
            len(content) != 1
            or not isinstance(content[0], dict)
            or content[0].get("type") != "text"
        ):
            return
        content = content[0].get("text")
    try:
        body = json.loads(content)
    except (ValueError, TypeError):
        return
    if not isinstance(body, dict):
        return
    if tool == "mcp__claude_ai_Atlassian_Rovo__getJiraIssue":
        args = entry.get("input", {})
        key = args.get("issueIdOrKey")
        if (
            args.get("updateHistory") is False
            and isinstance(key, str)
            and key
            and body.get("error") is True
            and type(body.get("statusCode")) is int
            and body["statusCode"] in {403, 404, 410}
        ):
            raise ConfirmedSourceRevocation(
                "Provider denied or removed the scoped Jira issue", source="jira", source_id=key
            )
        return
    uri = entry.get("input", {}).get("uri")
    if (
        not isinstance(uri, str)
        or not uri
        or set(body) != {"error"}
        or not isinstance(body["error"], dict)
    ):
        return
    error = body["error"]
    codes = []
    for _ in range(8):
        code = error.get("code")
        if isinstance(code, str):
            codes.append(code.casefold())
        child = error.get("innerError", error.get("innererror"))
        if not isinstance(child, dict):
            break
        if "code" not in child:
            if set(child) <= {"date", "request-id", "client-request-id"}:
                break
            return
        error = child
    else:
        return
    denied = {"accessdenied", "itemnotfound", "resourcenotfound"}
    # An unrecognized inner code can represent conditional access, expired login,
    # unsupported routing, or a transient failure. Do not elevate its parent code.
    if codes and codes[-1] in denied:
        raise ConfirmedSourceRevocation(
            "Provider explicitly denied or removed the scoped object",
            source="microsoft365",
            source_id=uri.removeprefix("file:///"),
        )
