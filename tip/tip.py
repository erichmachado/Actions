#!/usr/bin/env python3

import re
import sys
from sys import argv, stdout
from os import environ, getenv
from subprocess import check_call
from glob import glob
from pathlib import Path
from github import Github
from github import GithubException

print("· Get list of artifacts to be uploaded")

args = []
files = []

if "INPUT_FILES" in environ:
    args = environ["INPUT_FILES"].split()

if len(argv) > 1:
    args = args + argv[1:]

if len(args) == 1 and args[0] == "none":
    files = []
    print("! Skipping 'files' because it's set to 'none")
else:
    if len(args) == 0:
        stdout.flush()
        raise (Exception("Glob patterns need to be provided as positional arguments or through envvar 'INPUT_FILES'!"))

    for item in args:
        items = [fname for fname in glob(item, recursive=True) if not Path(fname).is_dir()]
        print(f"  glob({item!s}):")
        for fname in items:
            if Path(fname).stat().st_size == 0:
                print(f"  - ! Skipping empty file {fname!s}")
                continue
            print(f"  - {fname!s}")
            files += [fname]

    if len(files) < 1:
        stdout.flush()
        raise (Exception("Empty list of files to upload/update!"))

print("· Get GitHub API handler (authenticate)")

if "GITHUB_TOKEN" in environ:
    gh = Github(environ["GITHUB_TOKEN"])
elif "INPUT_TOKEN" in environ:
    gh = Github(environ["INPUT_TOKEN"])
else:
    if "GITHUB_USER" not in environ or "GITHUB_PASS" not in environ:
        stdout.flush()
        raise (
            Exception(
                "Need credentials to authenticate! Please, provide 'GITHUB_TOKEN', 'INPUT_TOKEN', or 'GITHUB_USER' and 'GITHUB_PASS'"
            )
        )
    gh = Github(environ["GITHUB_USER"], environ["GITHUB_PASS"])

print("· Get Repository handler")

if "GITHUB_REPOSITORY" not in environ:
    stdout.flush()
    raise (Exception("Repository name not defined! Please set 'GITHUB_REPOSITORY"))

gh_repo = gh.get_repo(environ["GITHUB_REPOSITORY"])

print("· Get Release handler")

tag = getenv("INPUT_TAG", "tip")

env_tag = None
gh_ref = environ["GITHUB_REF"]
is_prerelease = True
is_draft = False

if gh_ref[0:10] == "refs/tags/":
    env_tag = gh_ref[10:]
    if env_tag != tag:
        rexp = r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
        semver = re.search(rexp, env_tag)
        if semver == None and env_tag[0] == "v":
            semver = re.search(rexp, env_tag[1:])
        tag = env_tag
        if semver == None:
            print(f"! Could not get semver from {gh_ref!s}")
            print(f"! Treat tag '{tag!s}' as a release")
            is_prerelease = False
        else:
            if semver.group("prerelease") is None:
                # is a regular semver compilant tag
                is_prerelease = False
            elif getenv("INPUT_SNAPSHOTS", "true") == "true":
                # is semver compilant prerelease tag, thus a snapshot (we skip it)
                print("! Skipping snapshot prerelease")
                sys.exit()

gh_tag = None
try:
    gh_tag = gh_repo.get_git_ref(f"tags/{tag!s}")
except Exception as e:
    stdout.flush()
    pass
if gh_tag:
    try:
        gh_release = gh_repo.get_release(tag)
    except Exception as e:
        gh_release = gh_repo.create_git_release(tag, tag, "", draft=True, prerelease=is_prerelease)
        is_draft = True
        pass
else:
    err_msg = f"Tag/release '{tag!s}' does not exist and could not create it!"
    if "GITHUB_SHA" not in environ:
        raise (Exception(err_msg))
    try:
        gh_release = gh_repo.create_git_tag_and_release(
            tag, "", tag, "", environ["GITHUB_SHA"], "commit", draft=True, prerelease=is_prerelease
        )
        is_draft = True
    except Exception as e:
        raise (Exception(err_msg))

print("· Cleanup and/or upload artifacts")

artifacts = files

assets = gh_release.get_assets()


def delete_asset_by_name(name):
    for asset in assets:
        if asset.name == name:
            asset.delete_asset()
            return


def upload_asset(artifact, name):
    try:
        return gh_release.upload_asset(artifact, name=name)
    except GithubException as ex:
        if "already_exists" in [err["code"] for err in ex.data["errors"]]:
            print(f"   - {name} exists already! deleting...")
            delete_asset_by_name(name)
        else:
            print(f"   - uploading failed: {ex}")
    except Exception as ex:
        print(f"   - uploading failed: {ex}")

    print(f"   - retry uploading {name}...")
    return gh_release.upload_asset(artifact, name=name)


def replace_asset(artifacts, asset):
    print(f" > {asset!s}\n   {asset.name!s}:")
    for artifact in artifacts:
        aname = str(Path(artifact).name)
        if asset.name == aname:
            print(f"   - uploading tmp.{aname!s}...")
            new_asset = upload_asset(artifact, name=f"tmp.{aname!s}")
            print(f"   - removing...{aname!s}")
            asset.delete_asset()
            print(f"   - renaming tmp.{aname!s} to {aname!s}...")
            new_asset.update_asset(aname, label=aname)
            artifacts.remove(artifact)
            return
    print("   - keep")


if getenv("INPUT_RM", "false") == "true":
    print("· RM set. All previous assets are being cleared...")
    for asset in assets:
        print(f" - {asset.name}")
        asset.delete_asset()
else:
    for asset in assets:
        replace_asset(artifacts, asset)

for artifact in artifacts:
    print(f" > {artifact!s}:\n   - uploading...")
    gh_release.upload_asset(artifact)

stdout.flush()
print("· Update Release reference (force-push tag)")

if is_draft:
    # Unfortunately, it seems not possible to update fields 'created_at' or 'published_at'.
    print(" > Update (pre-)release")
    gh_release.update_release(
        gh_release.title,
        "" if gh_release.body is None else gh_release.body,
        draft=False,
        prerelease=is_prerelease,
        tag_name=gh_release.tag_name,
        target_commitish=gh_release.target_commitish,
    )

if ("GITHUB_SHA" in environ) and (env_tag is None):
    sha = environ["GITHUB_SHA"]
    print(f" > Force-push '{tag!s}' to {sha!s}")
    gh_repo.get_git_ref(f"tags/{tag!s}").edit(sha)
