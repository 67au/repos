#!/usr/bin/env python
from __future__ import annotations
import argparse
import asyncio
import dataclasses
import lzma
import logging
import operator
import os
import pathlib
import shutil
import subprocess
import time

import aiofiles

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

PKGBUILD_DIRECTORY_BASE = "repo"

CI_COMMIT_BRANCH = os.getenv("CI_COMMIT_BRANCH", "")
CI_DEFAULT_BRANCH = os.getenv("CI_DEFAULT_BRANCH", "")
CI_COMMIT_TITLE = os.getenv("CI_COMMIT_TITLE", "")
BUILD_OVERRIDE = os.getenv("BUILD_OVERRIDE", "")
TRUST_GPG_SCRIPT = pathlib.Path("./utils/trust_gpg.sh").resolve()
GNUPG_HOME = os.getenv("HOME")
FAIL_REPOS = []


async def get_arch() -> str:
    p = await asyncio.create_subprocess_exec("uname", "-m", stdout=subprocess.PIPE)
    stdout, _ = await (p.communicate())
    return stdout.decode().lower().strip()


ARCH = asyncio.run(get_arch())


@dataclasses.dataclass
class PackageVersionWithPath:
    _version: PackageVersion
    path: pathlib.Path

    @property
    def name(self) -> str:
        return self._version.name

    @property
    def version(self) -> str:
        return self._version.version

    @property
    def arch(self) -> bool:
        return self._version.arch_match


@dataclasses.dataclass
class PackageVersion:
    name: str
    version: str
    arch_match: bool

    def __eq__(self, other: PackageVersion) -> bool:
        return other.version == self.version

    def __gt__(self, other: PackageVersion) -> bool:
        return self.version > other.version

    def __lt__(self, other: PackageVersion) -> bool:
        return self.version < other.version

    def __le__(self, other: PackageVersion) -> bool:
        return self < other or self == other

    def __ge__(self, other: PackageVersion) -> bool:
        return self > other or self == other

    def __ne__(self, other: PackageVersion) -> bool:
        return not self == other

    def __str__(self) -> str:
        return f"{self.name} {self.version}"


def fetch_database(pkg_db: str) -> dict[str, str]:
    name_read = False
    version_read = False
    pkg_name = ""
    package_storage = {}
    with lzma.open(pkg_db) as fin:
        for line in fin:
            line = line.strip()
            if name_read:
                pkg_name = line.decode()
                name_read = False
            elif version_read:
                package_storage.update({pkg_name: line.decode()})
                # logger.debug("%s %s", pkg_name, line.decode())
                version_read = False
                pkg_name = ""
            if line == b"%NAME%":
                assert not name_read
                name_read = True
            if line == b"%VERSION%":
                assert not version_read
                version_read = True
            assert (name_read is False and version_read is False) or (
                version_read != name_read
            )
    return package_storage


@dataclasses.dataclass(init=False)
class BuildEnvs:
    src_dest: str
    pkg_dest: str
    makepkg_conf: str
    signing_arg: list[str]

    def __init__(self):
        cwd = pathlib.Path(os.getcwd())
        self.pkg_dest = str(cwd.joinpath("packages", ARCH).resolve())
        self.src_dest = str(cwd.joinpath("build").resolve())
        self.makepkg_conf = str(cwd.joinpath("makepkg_current.conf").resolve())
        if CI_COMMIT_BRANCH == CI_DEFAULT_BRANCH and CI_COMMIT_BRANCH != "":
            self.signing_arg = ["--sign"]
        else:
            logger.info("Skip signing package")

    def get_dict(self) -> dict[str, str | list[str]]:
        return {
            "src_dest": self.src_dest,
            "pkg_dest": self.pkg_dest,
            "makepkg_conf": self.makepkg_conf,
            "signing_arg": self.signing_arg,
        }


def get_src_info(s: str) -> PackageVersion:
    base = ""
    ver = ""
    rel = ""
    arch = []
    for line in s.splitlines(False):
        if (line := line.strip()).startswith("pkgver"):
            ver = line.split("=", maxsplit=2)[1]
        elif line.startswith("pkgrel"):
            rel = line.split("=", maxsplit=2)[1]
        elif line.startswith("pkgbase"):
            base = line.split("=", maxsplit=2)[1]
        elif line.startswith("arch"):
            arch.append(line.split("=", maxsplit=2)[1].strip().lower())
    return PackageVersion(
        base.strip(), f"{ver.strip()}-{rel.strip()}", ARCH in arch or "any" in arch
    )


async def parse_srcinfo(path: pathlib.PurePath) -> PackageVersionWithPath:
    if (src_path := pathlib.Path(path.joinpath(".SRCINFO"))).is_file():
        async with aiofiles.open(str(src_path)) as fin:
            info = get_src_info(await fin.read())
    else:
        p = await asyncio.create_subprocess_exec(
            "makepkg", "--printsrcinfo", cwd=src_path.parent, stdout=subprocess.PIPE
        )
        (stdout, stderr) = await p.communicate()
        info = get_src_info(stdout.decode())
    return PackageVersionWithPath(info, pathlib.Path(path).resolve())


async def get_build_target(
    pkg_db: str,
    home_directory: pathlib.Path,
    build_target: list[str] | None,
    build_all: bool,
) -> list[PackageVersionWithPath]:

    repo = fetch_database(pkg_db)
    tasks = []
    pending_build = []

    repo_directory = home_directory.joinpath(PKGBUILD_DIRECTORY_BASE)

    for folder in os.listdir(PKGBUILD_DIRECTORY_BASE):
        if folder.startswith("."):
            continue

        if (pkg_dir := repo_directory.joinpath(folder).resolve()).is_file():
            continue

        tasks.append(asyncio.create_task(parse_srcinfo(pkg_dir)))

    finished, _ = await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)

    if len(BUILD_OVERRIDE):
        logger.warning("BUILD OVERRIDE: %s", BUILD_OVERRIDE)
        override = BUILD_OVERRIDE.split(";")
        return [
            future.result() for future in finished if future.result().name in override
        ]

    if build_all:
        return [future.result() for future in finished]

    if len(build_target):
        return [
            future.result()
            for future in finished
            if future.result().name in build_target
        ]

    for future in finished:
        pkg = future.result()
        if pkg.name in repo:
            # print(pkg.name, repo[pkg.name], pkg.version)
            if repo[pkg.name] < pkg.version:
                pending_build.append(pkg)
        else:
            pending_build.append(pkg)

    # print(pending_build)
    return pending_build


async def run_hook(origin_path: pathlib.Path, target: str) -> None:
    if (
        hook_script := origin_path.parent.joinpath(".hook").joinpath(target).resolve()
    ).is_file():
        logger.info("Running %s hook script", target)
        p = await asyncio.create_subprocess_exec(
            str(hook_script), stdout=None, stderr=subprocess.STDOUT
        )
        await p.wait()
        if p.returncode != 0:
            logger.error(
                "Hook script return code: %d, skip other operation", p.returncode
            )
            return


async def run_build_with_install(
    src_dest: str, pkg_dest: str, makepkg_conf: str, signing_arg: list[str]
) -> int:
    return await run_build(src_dest, pkg_dest, makepkg_conf, signing_arg, "-i")


async def run_build(
    src_dest: str,
    pkg_dest: str,
    makepkg_conf: str,
    signing_arg: list[str],
    *other_args: str,
) -> int:
    env = os.environ
    env.update(
        {
            "SRCPKGDEST": src_dest,
            "SRCDEST": src_dest,
            "PKGDEST": pkg_dest,
            "MAKEPKG_CONF": makepkg_conf,
        }
    )
    p = await asyncio.create_subprocess_exec(
        "makepkg",
        "--clean",
        "-s",
        *other_args,
        *signing_arg,
        "--asdeps",
        "--noconfirm",
        "--needed",
        "--noprogressbar",
        env=env,
        stdout=None,
    )
    await p.wait()
    return p.returncode


async def install_dependency_via_yay(dep: str) -> None:
    await (
        await asyncio.create_subprocess_exec(
            "yay",
            "--noconfirm",
            "--asdeps",
            "--needed",
            "--noprogressbar",
            "-S",
            dep,
            stdout=None,
        )
    ).wait()


async def do_build(target: PackageVersionWithPath) -> int:
    try:
        os.chdir(str(target.path))
        logger.info("Building %s (%s)", target.name, target.path.name)
    except OSError:
        logger.exception("Got exception while chdir to %s", target.path)

    base_dir_name = target.path.name

    await run_hook(target.path, base_dir_name)

    if (
        yay_deps := target.path.parent.joinpath(".yaydeps")
        .joinpath(base_dir_name)
        .resolve()
    ).is_file():
        async with aiofiles.open(str(yay_deps)) as fin:
            while dep := (await fin.readline()).strip():
                logger.debug("Find dep %s, build first", dep)
                if (dep_dir := target.path.parent.joinpath(dep).resolve()).is_dir():
                    os.chdir(str(dep_dir))
                    await run_hook(target.path, dep)
                    if not (
                        ret := await run_build_with_install(**BUILD_ENVS.get_dict())
                    ):
                        logger.error(
                            "Build dependencies package error, skipped next step"
                        )
                        return ret
                else:
                    await install_dependency_via_yay(dep)

    if (
        gpg_keys := target.path.parent.joinpath(".gpg_keys")
        .joinpath(base_dir_name)
        .resolve()
    ).is_file():
        await (
            await asyncio.create_subprocess_exec(
                "/bin/bash", str(gpg_keys), stdout=None
            )
        ).wait()
        await (
            await asyncio.create_subprocess_exec(str(TRUST_GPG_SCRIPT), stdout=None)
        ).wait()

    os.chdir(str(target.path))
    build_ret = await run_build(**BUILD_ENVS.get_dict())

    if build_ret != 0:
        if build_ret == 13:
            logger.warning("Already build %s, skipped.", base_dir_name)
        FAIL_REPOS.append(base_dir_name)
        logger.warning("Build %s fail: %d", base_dir_name, build_ret)

    return build_ret


async def clean_installed_packages() -> int:
    p = await asyncio.create_subprocess_exec(
        "/bin/bash", "-c", "sudo pacman --noconfirm -Rcns $(pacman -Qdtq)", stdout=None
    )
    await p.wait()
    return p.returncode


async def upload_packages() -> None:
    remote_path = os.getenv("REMOTE_PATH")
    upload_token = os.getenv("UPLOAD_TOKEN")
    if remote_path is None or upload_token is None:
        logger.error("$REMOTE_PATH OR $UPLOAD_TOKEN is None, skipped upload")
    await (
        await asyncio.create_subprocess_exec(
            "./utils/ng/upload.py",
            remote_path,
            upload_token,
            ARCH,
            "--directory",
            BUILD_ENVS.pkg_dest,
            stdout=None,
        )
    ).wait()


async def do_work(
    pkg_db: str,
    build_all: bool,
    build_target: list[str],
    fail_fast: bool = False,
    run_auto_remove: bool = True,
) -> None:

    logger.info(
        "build_all: %s, fail_fast: %s, auto_remove: %s",
        build_all,
        fail_fast,
        run_auto_remove,
    )

    home_directory = pathlib.Path(os.getcwd())

    build_target = sorted(
        await get_build_target(pkg_db, home_directory, build_target, build_all),
        key=operator.attrgetter("name"),
    )
    logger.debug(
        "Pending build summary (%d): %s",
        len(build_target),
        ", ".join(map(lambda x: x.name, build_target)),
    )

    for target in build_target:
        if not target.arch:
            logger.info("Skipped %s (Architecture not match)", target.name)
            continue
        if (await do_build(target)) != 0 and fail_fast:
            break
        if run_auto_remove:
            await clean_installed_packages()

    if (src_dst := pathlib.Path(BUILD_ENVS.src_dest).resolve()).is_dir():
        shutil.rmtree(str(src_dst), ignore_errors=True)

    async with aiofiles.open(
        str(pathlib.Path(BUILD_ENVS.pkg_dest).joinpath("LASTBUILD").resolve()), "w"
    ) as fout:
        await fout.write(f"{int(time.time())}")

    if len(FAIL_REPOS):
        logger.error("Build failed repositories:")
        for repo in FAIL_REPOS:
            logger.error("%s", repo)

    os.chdir(str(home_directory))
    if ret := await upload_packages():
        exit(ret)


def check_build_target(args: argparse.Namespace) -> bool:
    if args.build_all or len(args.build_target):
        return True
    if "REBUILD ALL" in CI_COMMIT_TITLE or "REBUILD_ALL" in CI_COMMIT_TITLE:
        return True
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="pkgbuild.py")
    parser.add_argument("db_file")
    parser.add_argument("--build_all", action="store_true")
    parser.add_argument("--build_target", nargs="*")
    parser.add_argument("--sign", action="store_true")
    parser.add_argument("--fail_fast", action="store_true")
    args_ = parser.parse_args()

    try:
        import coloredlogs

        coloredlogs.install(
            logging.DEBUG,
            fmt="%(asctime)s - %(levelname)s - %(funcName)s - %(lineno)d - %(message)s",
        )
    except ModuleNotFoundError:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s - %(levelname)s - %(funcName)s - %(lineno)d - %(message)s",
        )

    BUILD_ENVS = BuildEnvs()
    if args_.sign:
        BUILD_ENVS.signing_arg = ["--sign"]
    asyncio.run(
        do_work(
            args_.db_file,
            check_build_target(args_),
            args_.build_target,
            args_.fail_fast,
        )
    )
