"""Configuration and result validation used by the local/CI release gate."""
import argparse
import json
from pathlib import Path
import re
import sys


def validate_release(repo, tag):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("agent repository must be owner/repository")
    if tag and (tag.lower() == "latest" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+/-]*", tag)):
        raise ValueError("agent tag must be an exact release tag, not latest")


def configure_env(text, base, host_port, doc_port, repo, tag):
    validate_release(repo, tag)
    for key, value in (("WRIT_PUBLIC_URL", base), ("WRIT_HOST_PORT", host_port),
                       ("WRIT_DOC_EXTRACT_HOST_PORT", doc_port),
                       ("DOC_EXTRACT_URL", f"http://127.0.0.1:{doc_port}"),
                       ("WRIT_AGENT_REPO", repo), ("WRIT_AGENT_TAG", tag)):
        if "\n" in value or "\r" in value:
            raise ValueError(f"invalid newline in {key}")
        line = f"{key}={value}"
        text, count = re.subn(rf"^{key}=.*$", lambda _: line, text, flags=re.M)
        if not count:
            text = text.rstrip("\n") + "\n" + line + "\n"
    return text


def mcp_payload(response):
    if not isinstance(response, dict) or "error" in response:
        raise ValueError("MCP JSON-RPC error or invalid response")
    result = response.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise ValueError("MCP tool returned an error or no result")
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        raise ValueError("expected one MCP JSON content block")
    block = content[0]
    if block.get("type") != "text" or not isinstance(block.get("text"), str):
        raise ValueError("expected MCP text containing JSON")
    try:
        data = json.loads(block["text"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("MCP content is not JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("MCP content is not a JSON object")
    return data


def validate_run(data, previous_task=None, workflow_id=None):
    if not isinstance(data, dict) or data.get("status") != "success":
        raise ValueError("run did not reach status=success")
    if data.get("error") or data.get("errors") or data.get("success") is False:
        raise ValueError("run reported errors")
    if (data.get("_cache") or {}).get("hit"):
        raise ValueError("run reused cached data")
    task_id = data.get("task_id")
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
        raise ValueError("run returned no valid task identity")
    if previous_task is not None and task_id == previous_task:
        raise ValueError("MCP reused the preceding REST task")
    if workflow_id is not None and data.get("workflow_id") != workflow_id:
        raise ValueError("MCP returned a different workflow")
    return task_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    env = sub.add_parser("configure-env")
    for name in ("path", "base", "host_port", "doc_port", "repo", "tag"):
        env.add_argument(name)
    release = sub.add_parser("validate-release")
    release.add_argument("repo")
    release.add_argument("tag", nargs="?", default="")
    sub.add_parser("release-tag")
    run = sub.add_parser("validate-run")
    run.add_argument("--mcp", action="store_true")
    run.add_argument("--previous-task", type=int)
    run.add_argument("--workflow-id", type=int)
    args = parser.parse_args()
    try:
        if args.command == "configure-env":
            path = Path(args.path)
            path.write_text(configure_env(path.read_text(), args.base, args.host_port,
                                          args.doc_port, args.repo, args.tag))
        elif args.command == "validate-release":
            validate_release(args.repo, args.tag)
        elif args.command == "release-tag":
            tag = json.load(sys.stdin).get("tag_name")
            if not isinstance(tag, str) or not tag:
                raise ValueError("latest release returned no tag_name")
            validate_release("owner/repo", tag)
            print(tag)
        else:
            data = json.load(sys.stdin)
            if args.mcp:
                data = mcp_payload(data)
            print(validate_run(data, args.previous_task, args.workflow_id))
    except (ValueError, TypeError, AttributeError) as exc:
        parser.exit(1, f"release gate: {exc}\n")


if __name__ == "__main__":
    main()
