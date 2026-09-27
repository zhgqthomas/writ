#!/usr/bin/env python3
"""Validate the release checkout and choose versioned image/promotion tags."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess


TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?")


def parse_tag(tag: str) -> tuple[tuple[int, int, int], str]:
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError("Expected vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-prerelease")
    prerelease = match[4] or ""
    if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease.split(".")):
        raise ValueError("Numeric prerelease identifiers cannot have leading zeroes")
    return (int(match[1]), int(match[2]), int(match[3])), prerelease


def promotes_latest(tag: str, repository_tags: list[str], current_version: str = "absent") -> bool:
    version, prerelease = parse_tag(tag)
    if prerelease:
        return False
    if current_version != "absent":
        try:
            current, current_pre = parse_tag(current_version)
        except ValueError:
            # A legacy/unlabelled alias needs explicit migration, not guessing.
            return False
        if current_pre or current > version:
            return False
    for other in repository_tags:
        try:
            other_version, other_prerelease = parse_tag(other)
        except ValueError:
            continue
        if not other_prerelease and other_version > version:
            return False
    return True


def image_tags(owner: str, component: str, tag: str, sha: str, repository_tags: list[str], current_version: str = "absent") -> list[str]:
    parse_tag(tag)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", owner):
        raise ValueError("Invalid repository owner")
    if component not in ("coordinator", "doc-extract"):
        raise ValueError("Invalid image component")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected an exact commit SHA")
    image = f"ghcr.io/{owner.lower()}/writ-{component}"
    result = [f"{image}:{tag}", f"{image}:{tag[1:]}", f"{image}:sha-{sha}"]
    if promotes_latest(tag, repository_tags, current_version):
        result.append(f"{image}:latest")
    return result


def git(directory: str, *args: str) -> str:
    return subprocess.check_output(["git", "-C", directory, *args], text=True).strip()


def validate_tag_sha(directory: str, tag: str, sha: str) -> None:
    parse_tag(tag)
    try:
        actual = subprocess.check_output(
            ["git", "-C", directory, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
            text=True, stderr=subprocess.PIPE,
        ).strip()
    except subprocess.CalledProcessError as error:
        raise ValueError("The release tag no longer exists") from error
    if actual != sha:
        raise ValueError("The release tag no longer identifies the validated commit")


def resolve_ref(directory: str, ref: str, publish: bool) -> tuple[str, str]:
    sha = git(directory, "rev-parse", "HEAD")
    if not publish:
        return sha, ""
    tag = ref.removeprefix("refs/tags/")
    parse_tag(tag)
    validate_tag_sha(directory, tag, sha)
    return sha, tag


def output(name: str, value: str) -> None:
    if "\n" in value:
        raise ValueError("Output values must be single-line")
    print(f"{name}={value}")
    if os.getenv("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(f"{name}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--ref", required=True)
    prepare.add_argument("--publish", choices=("true", "false"), required=True)
    prepare.add_argument("--directory", default=".")
    tags = commands.add_parser("tags")
    tags.add_argument("--tag", required=True)
    tags.add_argument("--owner", required=True)
    tags.add_argument("--component", required=True)
    tags.add_argument("--sha", required=True)
    tags.add_argument("--directory", default=".")
    tags.add_argument("--current-version", default="unknown")
    verify = commands.add_parser("verify")
    verify.add_argument("--tag", required=True)
    verify.add_argument("--sha", required=True)
    verify.add_argument("--directory", default=".")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            sha, tag = resolve_ref(args.directory, args.ref, args.publish == "true")
            output("sha", sha)
            output("tag", tag)
            output("publish", args.publish)
        elif args.command == "verify":
            validate_tag_sha(args.directory, args.tag, args.sha)
        else:
            validate_tag_sha(args.directory, args.tag, args.sha)
            repository_tags = git(args.directory, "tag", "--list", "v*").splitlines()
            for value in image_tags(args.owner, args.component, args.tag, args.sha, repository_tags, args.current_version):
                print(value)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release policy failed: {error}\n")


if __name__ == "__main__":
    main()
