from mcp.server.fastmcp import FastMCP
from .service import Service
from .adapter import capabilities


def build_server(service: Service):
    mcp = FastMCP('codex-dph')

    @mcp.tool()
    def list_projects() -> list:
        """List projects registered via CLI and approved check configurations."""
        return service.projects()

    @mcp.tool()
    def executor_status() -> dict:
        """Check installed DeepSeek interface compatibility without starting a model."""
        return capabilities()

    @mcp.tool()
    def submit_task(spec: dict, idempotency_key: str) -> dict:
        """Queue a coding task; optional spec.context supplies requirements_summary, design_decisions, constraints and document paths. Project context and documents are frozen automatically. Worker must be running separately. Does not wait for completion."""
        return service.status(service.submit(spec,idempotency_key)['run_id'])

    @mcp.tool()
    def get_run(run_id: str) -> dict:
        """Get current state. awaiting_review requires Codex to inspect artifacts and diff."""
        return service.status(run_id)

    @mcp.tool()
    def list_runs(project_id: str | None = None, limit: int = 50, offset: int = 0) -> list:
        """Recover task IDs across chats, with bounded pagination."""
        return service.list_runs(project_id,limit,offset)

    @mcp.tool()
    def read_events(run_id: str, after_sequence: int = 0, limit: int = 100) -> dict:
        """Read persisted execution events using a monotonic cursor."""
        return service.events(run_id,after_sequence,limit)

    @mcp.tool()
    def cancel_run(run_id: str) -> dict:
        """Request cancellation; only cancelled confirms shutdown. Poll get_run."""
        return service.cancel(run_id)

    @mcp.tool()
    def get_artifacts(run_id: str) -> dict:
        """Get task contract, candidate version, changed files and independent check evidence."""
        return service.artifacts(run_id)

    @mcp.tool()
    def get_context(run_id: str, offset: int = 0, limit: int = 20000) -> dict:
        """Read pinned requirements, decisions, documents and review history with a stable context hash."""
        return service.context(run_id,offset,limit)

    @mcp.tool()
    def get_diff(run_id: str, offset: int = 0, limit: int = 20000) -> dict:
        """Read a bounded page of the candidate diff, including new files."""
        return service.diff(run_id,offset,limit)

    @mcp.tool()
    def submit_review(run_id: str, candidate_commit: str, decision: str,
                      criteria: list[str], issues: list[str], idempotency_key: str) -> dict:
        """Submit Codex review: one pass/fail per criterion, plus actionable issues. Not human merge approval."""
        return service.review(run_id,candidate_commit,decision,criteria,issues,idempotency_key)

    @mcp.tool()
    def revise_task(run_id: str, review_id: str, idempotency_key: str) -> dict:
        """Queue a bounded revision from a changes_requested review."""
        return service.status(service.revise(run_id,review_id,idempotency_key)['run_id'])

    return mcp
