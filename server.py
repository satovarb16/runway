import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")

from mcp.server.fastmcp import FastMCP

from tools.analyze import analyze_job
from tools.jobs_store import (
    delete_job,
    get_job,
    list_jobs,
    save_job_analysis,
    set_application_status,
)
from tools.resumes import get_resume_version, list_resume_versions, save_resume_version
from tools.work_auth import set_work_authorization

mcp = FastMCP("runway-mcp")

mcp.tool()(analyze_job)
mcp.tool()(save_job_analysis)
mcp.tool()(get_job)
mcp.tool()(list_jobs)
mcp.tool()(set_application_status)
mcp.tool()(delete_job)
mcp.tool()(save_resume_version)
mcp.tool()(get_resume_version)
mcp.tool()(list_resume_versions)
mcp.tool()(set_work_authorization)


def main() -> None:
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        pass  # clean exit on Ctrl+C when run interactively


if __name__ == "__main__":
    main()
