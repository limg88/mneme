"""Fail-closed PreToolUse guard for selected source objects (stdlib only)."""

import base64
import json
import os
import sys


def permitted(event: dict, targets: list[dict]) -> bool:
    name = event.get("tool_name")
    args = event.get("tool_input", {})
    if name == "ToolSearch":
        return True  # Definitions only; no source content.
    for target in targets:
        if target["source"] == "request":
            read_tools = {
                "mcp__claude_ai_Atlassian_Rovo__searchJiraIssuesUsingJql",
                "mcp__claude_ai_Atlassian_Rovo__getJiraIssue",
                "mcp__claude_ai_Microsoft_365__sharepoint_search",
                "mcp__claude_ai_Microsoft_365__read_resource",
                "mcp__claude_ai_Microsoft_365__outlook_email_search",
                "mcp__claude_ai_Microsoft_365__chat_message_search",
                "mcp__claude_ai_Microsoft_365__outlook_calendar_search",
            }
            if (
                name in read_tools
                and name == target.get("tool")
                and args == target.get("input")
                and (
                    name != "mcp__claude_ai_Atlassian_Rovo__getJiraIssue"
                    or args.get("updateHistory") is False
                )
            ):
                return True
            continue
        if (
            target["source"] == "microsoft365"
            and name == "mcp__claude_ai_Microsoft_365__read_resource"
        ):
            expected = "file:///" + target["source_id"].removeprefix("file:///")
            if set(args) == {"uri"} and args["uri"] == expected:
                return True
        if target["source"] == "jira" and name == "mcp__claude_ai_Atlassian_Rovo__getJiraIssue":
            allowed = {
                "summary",
                "description",
                "status",
                "assignee",
                "created",
                "updated",
                "duedate",
                "resolutiondate",
            }
            fields = args.get("fields")
            if (
                set(args) <= {"cloudId", "issueIdOrKey", "fields", "updateHistory"}
                and args.get("cloudId") == target.get("cloud_id")
                and args.get("issueIdOrKey") == target["source_id"]
                and args.get("updateHistory") is False
                and isinstance(fields, list)
                and fields
                and set(fields) <= allowed
            ):
                return True
    return False


def main():
    try:
        targets = json.loads(base64.b64decode(sys.argv[1], validate=True))
        event = json.load(sys.stdin)
        allowed = permitted(event, targets)
        audit = os.environ.get("WORK_MEMORY_GUARD_AUDIT", "/work/guard-audit.jsonl")
        with open(audit, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"tool": event.get("tool_name"), "allowed": allowed}) + "\n")
    except Exception:
        allowed = False
    if not allowed:
        print("Tool request is outside the selected read-only source scope", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
