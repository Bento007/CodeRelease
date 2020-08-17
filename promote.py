#!/usr/bin/env python
"""
A tool for promoting server code to different stages, and minting new releases in github.

In order to release you must have a github access token with permissions to write to your repository. Set
environment variable `GITHUB_TOKEN_PATH` to the path of a file which contains github access token, or set environment
variable `GITHUB_TOKEN_SECRET_NAME` to the path of the AWS secret which contains github access token.

`./promote.py staging` promotes integration to staging and creates a prerelease in github.
`./promote.py prod` promotes staging to prod and creates a release in github.

Versioning follows https://semver.org/ standard
"""
import argparse
import json
import os
import subprocess

import requests
import semver

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "stage",
    metavar="stage",
    type=str,
    help="The stage you would like to create a release to."
)
parser.add_argument(
    "--release",
    "-r",
    type=str,
    choices=["major", "minor", "patch", "prerelease"],
    default="prerelease",
    required=False,
    help="The type of release to produce.",
)
parser.add_argument("--force", "-f", action="store_true")
parser.add_argument(
    "--release-notes", type=str, required=False, help="The path to a text file containing the release notes."
)
parser.add_argument("--dry-run", "-d", action="store_true")
parser.add_argument("--path", "-p", type=str, default='', help="The path to the local git repository.")
cmd_args = parser.parse_args()

# change path if path is provided.
if cmd_args.path:
    os.chdir(cmd_args.path)

# read configuration.
with open("./release_config.json") as fp:
    config = json.load(fp)

if cmd_args.stage == "prod" and cmd_args.release:
    print(
        f'Warning: cannot release "prod" with a release type.\n'
        f"Specify no release type to produce a finalized version."
    )
    exit(1)


def _subprocess(args, **kwargs):
    print(f"RUN: {' '.join(args)}")
    response = subprocess.run(args, **kwargs, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if response.stdout:
        print(f'RUN STDOUT:\n{response.stdout.decode("utf-8")}')
    if response.stderr:
        print(f'RUN STDERR:\n{response.stderr.decode("utf-8")}')
    print("\n")
    return response.stdout.decode("utf-8")


def check_diff(src, dst):
    """
    Check that there are no commits in the src branch that will be overwritten by rebasing on the dst.
    :param src: the source branch
    :param dst: the destination branch
    """
    result = _subprocess(
        [
            "git",
            "--no-pager",
            "log",
            "--graph",
            "--abbrev-commit",
            "--pretty=oneline",
            "--no-merges",
            "--",
            f"{src}",
            f"^{dst}",
        ]
    )

    if result:
        print(f"Warning: the following commits are present on {dst} but not on {src}: \n{result}")
        if cmd_args.force:
            print(f"Warning: they will be overwritten on {dst} and discarded.")
        else:
            print(f"Warning: run with --force to overwrite and discard these commits from {dst}")
            exit(1)


def check_working_tree():
    """Check that are not changes in the current working tree before changing branches."""
    result = _subprocess(["git", "--no-pager", "diff", "--ignore-submodules=untracked"])
    if result:
        print(result)
        print(
            f"Warning: Working tree contains changes to tracked files. Please commit or discard "
            f"your changes and try again."
        )
        exit(1)


def check_requirements():
    if _subprocess(["diff", "<(pip freeze)", f'<(tail -n +2 {os.environ["FUS_HOME"]}/requirements-dev.txt)']):
        if cmd_args.force:
            print(
                f"Warning: Your installed Python packages differ from requirements-dev.txt. Forcing deployment anyway."
            )
        else:
            print(
                f"Warning: Your installed Python packages differ from requirements-dev.txt. Please update your "
                f"virtualenv. Run {cmd_args.prog} with --force to deploy anyway."
            )
            exit(1)


def make_release_notes(src, dst) -> str:
    """
    Produce release notes by retrieving the different commits from src to dst.
    :param src: the source branch
    :param dst: the destination branch
    :return:
    """
    result = _subprocess(["git", "log", '--pretty=format:"%s"', f"origin/{src}...origin/{dst}"])
    commits = "\n".join([f"- {i[1:-1]}" for i in result.split("\n")])

    if cmd_args.release_notes:
        with open(cmd_args.release_notes, "w") as f:
            f.write(commits)

    return commits


def commit(repo, src, dst):
    print(_subprocess(["git", "remote", "set-url", "origin", f"https://{token}@github.com/{repo}.git"]))
    print(_subprocess(["git", "-c", "advice.detachedHead=false", "checkout", f"origin/{src}"]))
    print(_subprocess(["git", "checkout", "-B", dst]))
    print(_subprocess(["git", "push", "--force", "origin", dst]))


def get_current_version(repo: str, _stage: str = None) -> str:
    """check the latest release from github"""
    _stage = _stage if _stage else cmd_args.stage
    version_url = f"https://api.github.com/repos/{repo}/releases"
    releases = requests.get(version_url).json()
    versions = [semver.VersionInfo(0, 0, 0)]

    # would use version['target_commitish'] to grab the stage, but in use it grabs unexpected stages
    if releases and _stage == "staging":
        versions = [
            semver.parse_version_info(version["tag_name"])
            for version in releases
            if semver.parse_version_info(version["tag_name"]).prerelease
               and semver.parse_version_info(version["tag_name"]).prerelease.startswith("rc")
        ]
    elif releases and _stage == "prod":
        versions = [
            semver.parse_version_info(version["tag_name"])
            for version in releases
            if not semver.parse_version_info(version["tag_name"]).prerelease
        ]
    return str(max(versions))


def update_version(repo, src, dst) -> str:
    """
    Retrieves the current version from github, bumps the version, and updates the values in service_config.json before
    committing to the dst branch
    :return: The new version.
    """
    cur_version = get_current_version(repo, cmd_args.stage)

    if src == "prod":
        prv_version = get_current_version(repo, _stage=dst)
        _new_version = semver.finalize_version(prv_version)
    else:
        _new_version = getattr(semver, f"bump_{cmd_args.release}")(str(cur_version))
        _new_version = (
            _new_version
            if semver.parse_version_info(_new_version).prerelease
            else semver.bump_prerelease(_new_version, token="rc")
        )

    if cur_version == _new_version:
        print("Nothing to promote")
        exit(0)
    else:
        print(f"Upgrading: {cur_version} -> {_new_version}")
        return _new_version


if __name__ == "__main__":
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        token_path = os.environ.get('GITHUB_TOKEN_PATH') or "promote-token.txt"
        if token_path and token_path != "None":
            with open(os.path.expanduser(token_path), "r") as fp:
                token = fp.read().strip()
        else:
            secret_id = os.environ["GITHUB_TOKEN_SECRET_NAME"]
            import boto3

            SM = boto3.client("secretsmanager")
            token = SM.get_secret_value(SecretId=secret_id)["SecretString"]

    stage = config["release_map"][cmd_args.stage]
    repository = config['repository'].replace("https://github.com/", '')
    source = stage['source']
    destination = stage['destination']
    prerelease = stage['prerelease']
    dry_run = "(dry run)" if cmd_args.dry_run else ""
    print(f"Releasing {source} to {destination} {dry_run}")
    check_working_tree()
    check_diff(source, destination)
    release_notes = make_release_notes(source, destination)
    new_version = update_version(repository, source, destination)
    if not cmd_args.dry_run:
        old_branch = _subprocess(["git", "rev-parse", f"origin/{destination}"])
        commit(repository, source, destination)
        body = dict(
            tag_name=str(new_version),
            name="{dst} {new_version}".format(dst=destination, new_version=new_version),
            prerelease=prerelease,
            draft=True,
            target_commitish=destination,
            body=release_notes,
        )

        resp = requests.post(
            f"https://api.github.com/repos/{repository}/releases",
            headers={"Authorization": f"token {token}"},
            data=json.dumps(body),
        )
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as ex:
            print(f"ERROR: Failed to create release!  Changes were:\n{release_notes}")
            print(f"Rolling back changes:")
            commit(repository, old_branch, destination)
            raise ex
