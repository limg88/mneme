from mcp.server import MCPServer

from .store import Store


def create_server(store: Store) -> MCPServer:
    server = MCPServer("Work Memory")
    # Same functions as HTTP. No source credentials or mutation tools are exposed to consumers.
    server.tool(
        name="memory_status",
        structured_output=True,
        description="Coverage, freshness, run and extraction errors.",
    )(store.status)
    server.tool(
        name="memory_search",
        structured_output=True,
        description=(
            "Evidence search with mode lexical, semantic or hybrid. Includes historical "
            "versions; hybrid fallback and index coverage are explicit. Similarity is not truth."
        ),
    )(store.search)
    server.tool(
        name="memory_facts",
        structured_output=True,
        description=(
            "Facts at real-world valid_at and system known_at instants. Unknown validity and "
            "conflicts are explicit; do not treat all returned candidates as current truths."
        ),
    )(store.facts)
    server.tool(
        name="memory_observation",
        structured_output=True,
        description="Full source observation behind a fact.",
    )(store.observation)
    server.tool(
        name="memory_history",
        structured_output=True,
        description="Fact property history and knowledge revisions.",
    )(store.history)
    return server
