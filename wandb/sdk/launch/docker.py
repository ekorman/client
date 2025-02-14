import json
import logging
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, Optional, Tuple


from dockerpycreds.utils import find_executable  # type: ignore
import pkg_resources
from six.moves import shlex_quote
import wandb
from wandb.apis.internal import Api
import wandb.docker as docker
from wandb.errors import DockerError, LaunchError

from ._project_spec import (
    create_metadata_file,
    EntryPoint,
    get_entry_point_command,
    LaunchProject,
)
from .utils import _is_wandb_dev_uri, _is_wandb_local_uri, sanitize_wandb_api_key
from ..lib.git import GitRepo

_logger = logging.getLogger(__name__)

_GENERATED_DOCKERFILE_NAME = "Dockerfile.wandb-autogenerated"

DEFAULT_CUDA_VERSION = "10.0"


def validate_docker_installation() -> None:
    """Verify if Docker is installed on host machine."""
    _logger.info("Validating docker installation")
    if not find_executable("docker"):
        raise LaunchError(
            "Could not find Docker executable. "
            "Ensure Docker is installed as per the instructions "
            "at https://docs.docker.com/install/overview/."
        )


def get_docker_user(launch_project: LaunchProject, runner_type: str) -> Tuple[str, int]:
    import getpass

    username = getpass.getuser()

    if runner_type == "sagemaker" and not launch_project.docker_image:
        # unless user has provided their own image, sagemaker must run as root but keep the name for workdir etc
        return username, 0

    userid = launch_project.docker_user_id or os.geteuid()
    return username, userid


DOCKERFILE_TEMPLATE = """
# ----- stage 1: build -----
FROM {py_build_image} as build

# requirements section depends on pip vs conda, and presence of buildx
{requirements_section}

# ----- stage 2: base -----
{base_setup}

COPY --from=build /env /env
ENV PATH="/env/bin:$PATH"

ENV SHELL /bin/bash

# some resources (eg sagemaker) must run on root
{user_setup}

WORKDIR {workdir}
RUN chown -R {uid} {workdir}

# make artifacts cache dir unrelated to build
RUN mkdir -p {workdir}/.cache && chown -R {uid} {workdir}/.cache

# copy code/etc
COPY --chown={uid} src/ {workdir}

ENV PYTHONUNBUFFERED=1

# sagemaker requires a fixed entrypoint to a `train` file in the dockerfile
{sagemaker_entrypoint}
"""

# this goes into base_setup in TEMPLATE
PYTHON_SETUP_TEMPLATE = """
FROM {py_base_image} as base
"""

# this goes into base_setup in TEMPLATE
CUDA_SETUP_TEMPLATE = """
FROM {cuda_base_image} as base
RUN apt-get update -qq && apt-get install -y software-properties-common && add-apt-repository -y ppa:deadsnakes/ppa

# install python
RUN apt-get update -qq && apt-get install --no-install-recommends -y \
    {python_packages} \
    && apt-get -qq purge && apt-get -qq clean \
    && rm -rf /var/lib/apt/lists/*

# make sure `python` points at the right version
RUN update-alternatives --install /usr/bin/python python /usr/bin/python{py_version} 1 \
    && update-alternatives --install /usr/local/bin/python python /usr/bin/python{py_version} 1
"""

# this goes into requirements_section in TEMPLATE
PIP_TEMPLATE = """
RUN python -m venv /env
# make sure we install into the env
ENV PATH="/env/bin:$PATH"
COPY {requirements_files} .
{buildx_optional_prefix} {pip_install}
"""

# this goes into requirements_section in TEMPLATE
CONDA_TEMPLATE = """
COPY src/environment.yml .
{buildx_optional_prefix} conda env create -f environment.yml -n env

# pack the environment so that we can transfer to the base image
RUN conda install -c conda-forge conda-pack
RUN conda pack -n env -o /tmp/env.tar && \
    mkdir /env && cd /env && tar xf /tmp/env.tar && \
    rm /tmp/env.tar
RUN /env/bin/conda-unpack
"""

USER_CREATE_TEMPLATE = """
RUN useradd \
    --create-home \
    --no-log-init \
    --shell /bin/bash \
    --gid 0 \
    --uid {uid} \
    {user} || echo ""
"""


def get_current_python_version() -> Tuple[str, str]:
    full_version = sys.version.split()[0].split(".")
    major = full_version[0]
    version = ".".join(full_version[:2]) if len(full_version) >= 2 else major + ".0"
    return version, major


def get_base_setup(
    launch_project: LaunchProject, py_version: str, py_major: str
) -> str:
    """Fill in the Dockerfile templates for stage 2 of build. CPU version is built on python, GPU
    version is built on nvidia:cuda"""

    python_base_image = "python:{}-buster".format(py_version)
    if launch_project.cuda:
        cuda_version = launch_project.cuda_version or DEFAULT_CUDA_VERSION
        # cuda image doesn't come with python tooling
        if py_major == "2":
            python_packages = [
                "python{}".format(py_version),
                "libpython{}".format(py_version),
                "python-pip",
                "python-setuptools",
            ]
        else:
            python_packages = [
                "python{}".format(py_version),
                "libpython{}".format(py_version),
                "python3-pip",
                "python3-setuptools",
            ]
        base_setup = CUDA_SETUP_TEMPLATE.format(
            cuda_base_image="nvidia/cuda:{}-runtime".format(cuda_version),
            python_packages=" \\\n".join(python_packages),
            py_version=py_version,
        )
    else:
        python_packages = [
            "python3-dev" if py_major == "3" else "python-dev",
            "gcc",
        ]  # gcc required for python < 3.7 for some reason
        base_setup = PYTHON_SETUP_TEMPLATE.format(py_base_image=python_base_image)
    return base_setup


def get_env_vars_dict(launch_project: LaunchProject, api: Api) -> Dict[str, str]:
    """Generates environment variables for the project.

    Arguments:
    launch_project: LaunchProject to generate environment variables for.

    Returns:
        Dictionary of environment variables.
    """
    env_vars = {}
    if _is_wandb_local_uri(api.settings("base_url")) and sys.platform == "darwin":
        _, _, port = api.settings("base_url").split(":")
        base_url = "http://host.docker.internal:{}".format(port)
    elif _is_wandb_dev_uri(api.settings("base_url")):
        base_url = "http://host.docker.internal:9002"
    else:
        base_url = api.settings("base_url")
    env_vars["WANDB_BASE_URL"] = base_url

    env_vars["WANDB_API_KEY"] = api.api_key
    env_vars["WANDB_PROJECT"] = launch_project.target_project
    env_vars["WANDB_ENTITY"] = launch_project.target_entity
    env_vars["WANDB_LAUNCH"] = "True"
    env_vars["WANDB_RUN_ID"] = launch_project.run_id
    if launch_project.docker_image:
        env_vars["WANDB_DOCKER"] = launch_project.docker_image

    # TODO: handle env vars > 32760 characters
    env_vars["WANDB_CONFIG"] = json.dumps(launch_project.override_config)
    env_vars["WANDB_ARTIFACTS"] = json.dumps(launch_project.override_artifacts)

    return env_vars


def get_requirements_section(launch_project: LaunchProject) -> str:
    buildx_installed = docker.is_buildx_installed()
    if not buildx_installed:
        wandb.termwarn(
            "Docker BuildX is not installed, for faster builds upgrade docker: https://github.com/docker/buildx#installing"
        )
        prefix = "RUN WANDB_DISABLE_CACHE=true"

    if launch_project.deps_type == "pip":
        requirements_files = []
        if launch_project.project_dir is not None and os.path.exists(
            os.path.join(launch_project.project_dir, "requirements.txt")
        ):
            requirements_files += ["src/requirements.txt"]
            pip_install_line = "pip install -r requirements.txt"

        if launch_project.project_dir is not None and os.path.exists(
            os.path.join(launch_project.project_dir, "requirements.frozen.txt")
        ):
            # if we have frozen requirements stored, copy those over and have them take precedence
            requirements_files += ["src/requirements.frozen.txt", "_wandb_bootstrap.py"]
            pip_install_line = (
                _parse_existing_requirements(launch_project)
                + "python _wandb_bootstrap.py"
            )

        if buildx_installed:
            prefix = "RUN --mount=type=cache,mode=0777,target=/root/.cache/pip"

        requirements_line = PIP_TEMPLATE.format(
            buildx_optional_prefix=prefix,
            requirements_files=" ".join(requirements_files),
            pip_install=pip_install_line,
        )
    elif launch_project.deps_type == "conda":
        if buildx_installed:
            prefix = "RUN --mount=type=cache,mode=0777,target=/opt/conda/pkgs"
        requirements_line = CONDA_TEMPLATE.format(buildx_optional_prefix=prefix)
    else:
        # this means no deps file was found
        requirements_line = ""

    return requirements_line


def get_user_setup(username: str, userid: int, runner_type: str) -> str:
    if runner_type == "sagemaker":
        # sagemaker must run as root
        return "USER root"
    user_create = USER_CREATE_TEMPLATE.format(uid=userid, user=username)
    user_create += "\nUSER {}".format(username)
    return user_create


def generate_dockerfile(launch_project: LaunchProject, runner_type: str,) -> str:
    # get python versions truncated to major.minor to ensure image availability
    if launch_project.python_version:
        spl = launch_project.python_version.split(".")[:2]
        py_version, py_major = (".".join(spl), spl[0])
    else:
        py_version, py_major = get_current_python_version()

    # ----- stage 1: build -----
    if launch_project.deps_type == "pip" or launch_project.deps_type is None:
        python_build_image = "python:{}".format(
            py_version
        )  # use full python image for package installation
    elif launch_project.deps_type == "conda":
        # neither of these images are receiving regular updates, latest should be pretty stable
        python_build_image = (
            "continuumio/miniconda3:latest"
            if py_major == "3"
            else "continuumio/miniconda:latest"
        )
    requirements_section = get_requirements_section(launch_project)

    # ----- stage 2: base -----
    python_base_setup = get_base_setup(launch_project, py_version, py_major)

    # set up user info
    username, userid = get_docker_user(launch_project, runner_type)
    user_setup = get_user_setup(username, userid, runner_type)
    workdir = "/home/{user}".format(user=username)

    sagemaker_entrypoint = ""
    if runner_type == "sagemaker":
        sagemaker_entrypoint = 'ENTRYPOINT ["sh", "train"]'

    dockerfile_contents = DOCKERFILE_TEMPLATE.format(
        py_build_image=python_build_image,
        requirements_section=requirements_section,
        base_setup=python_base_setup,
        uid=userid,
        user_setup=user_setup,
        workdir=workdir,
        sagemaker_entrypoint=sagemaker_entrypoint,
    )

    return dockerfile_contents


def generate_docker_image(
    launch_project: LaunchProject,
    image_uri: str,
    entrypoint: Optional[EntryPoint],
    docker_args: Dict[str, Any],
    runner_type: str,
) -> str:
    if entrypoint is None:
        raise LaunchError("No entrypoint found while building image")

    dockerfile_str = generate_dockerfile(launch_project, runner_type)
    entry_cmd = get_entry_point_command(entrypoint, launch_project.override_args)
    create_metadata_file(
        launch_project,
        image_uri,
        sanitize_wandb_api_key(entry_cmd),
        docker_args,
        sanitize_wandb_api_key(dockerfile_str),
    )
    if runner_type == "sagemaker" and launch_project.project_dir is not None:
        # sagemaker automatically appends train after the entrypoint
        # by redirecting to running a train script we can avoid issues
        # with argparse, and hopefully if the user intends for the train
        # argument to be present it is captured in the original jobs
        # command arguments
        with open(os.path.join(launch_project.project_dir, "train"), "w") as fp:
            fp.write(entry_cmd)
    build_ctx_path = _create_docker_build_ctx(launch_project, dockerfile_str)
    dockerfile = os.path.join(build_ctx_path, _GENERATED_DOCKERFILE_NAME)
    try:
        docker.build(tags=[image_uri], file=dockerfile, context_path=build_ctx_path)
    except DockerError as e:
        raise LaunchError("Error communicating with docker client: {}".format(e))

    try:
        os.remove(build_ctx_path)
    except Exception:
        _logger.info(
            "Temporary docker context file %s was not deleted.", build_ctx_path
        )
    return image_uri


_inspected_images = {}


def docker_image_exists(docker_image: str, should_raise: bool = False) -> bool:
    """Checks if a specific image is already available,
    optionally raising an exception"""
    try:
        data = docker.run(["docker", "image", "inspect", docker_image])
        # always true, since return stderr defaults to false
        assert isinstance(data, str)
        parsed = json.loads(data)[0]
        _inspected_images[docker_image] = parsed
        return True
    except (DockerError, ValueError) as e:
        if should_raise:
            raise e
        return False


def docker_image_inspect(docker_image: str) -> Dict[str, Any]:
    """Get the parsed json result of docker inspect image_name"""
    if _inspected_images.get(docker_image) is None:
        docker_image_exists(docker_image, True)
    return _inspected_images.get(docker_image, {})


def pull_docker_image(docker_image: str) -> None:
    """Pulls the requested docker image"""
    if docker_image_exists(docker_image):
        # don't pull images if they exist already, eg if they are local images
        return
    try:
        _logger.info("Pulling user provided docker image")
        docker.run(["docker", "pull", docker_image])
    except DockerError as e:
        raise LaunchError("Docker server returned error: {}".format(e))


def construct_local_image_uri(launch_project: LaunchProject) -> str:
    if launch_project.project_dir is None and launch_project.docker_image is None:
        raise LaunchError("Must specify either project_dir or docker_image")
    assert launch_project.project_dir is not None
    image_uri = _get_docker_image_uri(
        name=launch_project.image_name,
        work_dir=launch_project.project_dir,
        image_id=launch_project.run_id,
    )
    return image_uri


def construct_gcp_image_uri(
    launch_project: LaunchProject, gcp_repo: str, gcp_project: str, gcp_registry: str,
) -> str:
    base_uri = construct_local_image_uri(launch_project)
    return "/".join([gcp_registry, gcp_project, gcp_repo, base_uri])


def _parse_existing_requirements(launch_project: LaunchProject) -> str:
    requirements_line = ""
    assert launch_project.project_dir is not None
    base_requirements = os.path.join(launch_project.project_dir, "requirements.txt")
    if os.path.exists(base_requirements):
        include_only = set()
        with open(base_requirements) as f:
            iter = pkg_resources.parse_requirements(f)
            while True:
                try:
                    pkg = next(iter)
                    if hasattr(pkg, "name"):
                        name = pkg.name.lower()  # type: ignore
                    else:
                        name = str(pkg)
                    include_only.add(shlex_quote(name))
                except StopIteration:
                    break
                # Different versions of pkg_resources throw different errors
                # just catch them all and ignore packages we can't parse
                except Exception as e:
                    _logger.warn(f"Unable to parse requirements.txt: {e}")
                    continue
        requirements_line += "WANDB_ONLY_INCLUDE={} ".format(",".join(include_only))
    return requirements_line


def _get_docker_image_uri(name: Optional[str], work_dir: str, image_id: str) -> str:
    """
    Returns an appropriate Docker image URI for a project based on the git hash of the specified
    working directory.
    :param name: The URI of the Docker repository with which to tag the image. The
                           repository URI is used as the prefix of the image URI.
    :param work_dir: Path to the working directory in which to search for a git commit hash
    """
    name = name.replace(" ", "-") if name else "wandb-launch"
    # Optionally include first 7 digits of git SHA in tag name, if available.

    git_commit = GitRepo(work_dir).last_commit
    version_string = (
        ":" + str(git_commit[:7]) + image_id if git_commit else ":" + image_id
    )
    return name + version_string


def _create_docker_build_ctx(
    launch_project: LaunchProject, dockerfile_contents: str,
) -> str:
    """Creates build context temp dir containing Dockerfile and project code, returning path to temp dir."""
    assert launch_project.project_dir is not None
    directory = tempfile.mkdtemp()
    dst_path = os.path.join(directory, "src")
    shutil.copytree(
        src=launch_project.project_dir, dst=dst_path, symlinks=True,
    )
    shutil.copy(
        os.path.join(os.path.dirname(__file__), "templates", "_wandb_bootstrap.py"),
        os.path.join(directory),
    )
    if launch_project.python_version:
        runtime_path = os.path.join(dst_path, "runtime.txt")
        with open(runtime_path, "w") as fp:
            fp.write(f"python-{launch_project.python_version}")
    # TODO: we likely don't need to pass the whole git repo into the container
    # with open(os.path.join(directory, ".dockerignore"), "w") as f:
    #    f.write("**/.git")
    with open(os.path.join(directory, _GENERATED_DOCKERFILE_NAME), "w") as handle:
        handle.write(dockerfile_contents)
    return directory
