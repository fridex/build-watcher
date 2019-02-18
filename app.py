#!/usr/bin/env python3
# thoth-build-watcher
# Copyright(C) 2019 Fridolin Pokorny
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""A build watch - watch for builds and submit images to Thoth for analysis."""

import os
import sys
import logging
import time
from multiprocessing import Process
from multiprocessing import Queue
from multiprocessing import Manager

import click

from thamos.lib import image_analysis
from thamos.config import config as configuration
from thoth.common import init_logging
from thoth.common import OpenShift
from thoth.analyzer import run_command


init_logging()

_LOGGER = logging.getLogger("thoth.build_watcher")

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SKOPEO_EXEC_PATH = os.getenv(
    "SKOPEO_EXEC_PATH", os.path.join(_HERE_DIR, "bin", "skopeo")
)


def _existing_producer(queue: Queue, build_watcher_namespace: str):
    """Query for existing images in image streams and queue them for analysis."""
    openshift = OpenShift()
    v1_imagestreams = openshift.ocp_client.resources.get(
        api_version="image.openshift.io/v1", kind="ImageStream"
    )
    for item in v1_imagestreams.get(namespace=build_watcher_namespace).items:
        _LOGGER.debug("Listing tags available for %r", item["metadata"]["name"])
        for tag_info in item.status.tags or []:
            output_reference = f"{item.status.dockerImageRepository}:{tag_info.tag}"
            _LOGGER.info(
                "Queueing already existing image %r for analysis", output_reference
            )
            queue.put(output_reference)

    _LOGGER.info("Queuing existing images for analyses has finished, all of them were scheduled for analysis")


def _event_producer(queue: Queue, build_watcher_namespace: str):
    """Accept events from the cluster and queue them into work queue processed by the main process."""
    _LOGGER.info("Starting event producer")
    openshift = OpenShift()
    v1_build = openshift.ocp_client.resources.get(api_version="v1", kind="Build")
    for event in v1_build.watch(namespace=build_watcher_namespace):
        if event["object"].status.phase != "Complete":
            _LOGGER.debug(
                "Ignoring build event for %r - not completed phase %r",
                event["object"].metadata.name,
                event["object"].status.phase,
            )
            continue

        event_name = event["object"].metadata.name
        output_reference = event["object"].status.outputDockerImageReference
        _LOGGER.info("Queueing %r based on build event %r for further processing", output_reference, event_name)
        queue.put(output_reference)


def _do_analyze_image(
    output_reference: str,
    push_registry: str = None,
    *,
    registry_user: str = None,
    registry_password: str = None,
    src_verify_tls: bool = True,
    dst_verify_tls: bool = True,
) -> str:
    if push_registry:
        _LOGGER.info(
            "Pushing image %r to an external push registry %r",
            output_reference,
            push_registry,
        )
        output_reference = _push_image(
            output_reference,
            push_registry,
            registry_user,
            registry_password,
            src_verify_tls=src_verify_tls,
            dst_verify_tls=dst_verify_tls,
        )
        _LOGGER.info("Successfully pushed image to %r", output_reference)

    analysis_id = image_analysis(
        image=output_reference,
        registry_user=registry_user,
        registry_password=registry_password,
        verify_tls=dst_verify_tls,
        nowait=True,
    )

    _LOGGER.info(
        "Successfully submitted %r to Thoth for analysis; analysis id: %s",
        output_reference,
        analysis_id,
    )

    return analysis_id


def _push_image(
    image: str,
    push_registry: str,
    registry_user: str = None,
    registry_password: str = None,
    src_verify_tls: bool = True,
    dst_verify_tls: bool = True,
) -> str:
    """Push the given image (fully specified with registry info) into another registry."""
    cmd = f"{_SKOPEO_EXEC_PATH} copy "

    if not src_verify_tls:
        cmd += "--src-tls-verify=false "

    if not dst_verify_tls:
        cmd += "--dest-tls-verify=false "

    if registry_user:
        cmd += f"--dest-creds={registry_user}"

        if registry_password:
            cmd += f":{registry_password}"

        cmd += " "

    image_name = image.rsplit("/", maxsplit=1)[1]
    output = f"{push_registry}/{image_name}"
    _LOGGER.debug(
        "Pushing image %r from %r to registry %r, output is %r",
        image_name,
        image,
        push_registry,
        output,
    )
    cmd += f"docker://{image} docker://{output}"

    _LOGGER.debug("Running: %s", cmd.replace(registry_password, "***"))
    command = run_command(cmd)
    _LOGGER.debug(
        "%s stdout:\n%s\n%s", _SKOPEO_EXEC_PATH, command.stdout, command.stderr
    )

    return output


def _submitter(
    queue: Queue,
    push_registry: str,
    registry_user: str = None,
    registry_password: str = None,
    no_registry_tls_verify: bool = False,
) -> None:
    """Read messages from queue and submit each message with image to Thoth for analysis."""
    while True:
        output_reference = queue.get()
        _LOGGER.info("Handling analysis of image %r", output_reference)

        try:
            _do_analyze_image(
                output_reference,
                push_registry,
                registry_user=registry_user,
                registry_password=registry_password,
                src_verify_tls=not no_registry_tls_verify,
                dst_verify_tls=not no_registry_tls_verify,
            )
        except Exception as exc:
            _LOGGER.exception(
                "Failed to submit image %r for analysis to Thoth: %s",
                output_reference,
                str(exc),
            )


@click.command()
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    envvar="THOTH_VERBOSE_BUILD_WATCHER",
    help="Be verbose about what is going on.",
)
@click.option(
    "--build-watcher-namespace",
    "-n",
    type=str,
    required=True,
    envvar="THOTH_WATCHED_NAMESPACE",
    help="Namespace to connect to to wait for events.",
)
@click.option(
    "--thoth-api-host",
    "-a",
    type=str,
    required=True,
    envvar="THOTH_USER_API_HOST",
    help="Host to Thoth's User API - API endpoint discovery will be transparently done.",
)
@click.option(
    "--no-tls-verify",
    "-T",
    is_flag=True,
    envvar="THOTH_NO_TLS_VERIFY",
    help="Do not check for TLS certificates when communicating with Thoth.",
)
@click.option(
    "--no-registry-tls-verify",
    "-R",
    is_flag=True,
    envvar="THOTH_NO_REGISTRY_TLS_VERIFY",
    help="Do not check for TLS certificates of registry when pulling images on Thoth side or "
    "when pushing to a remote push registry.",
)
@click.option(
    "--pass-token",
    "-p",
    is_flag=True,
    envvar="THOTH_PASS_TOKEN",
    help="Pass OpenShift token to User API to enable image pulling "
    "(disjoint with --registry-user and --registry-password).",
)
@click.option(
    "--registry-user",
    "-u",
    type=str,
    envvar="THOTH_REGISTRY_USER",
    help="Registry user used to pull images on Thoth User API; if push registry is specified, this user "
    "is also used to push images to push registry.",
)
@click.option(
    "--registry-password",
    "-u",
    type=str,
    envvar="THOTH_REGISTRY_PASSWORD",
    help="Registry password used to pull images on Thoth User API, if push registry is specified, "
    "this password is also used to push images to push registry.",
)
@click.option(
    "--push-registry",
    "-r",
    type=str,
    envvar="THOTH_PUSH_REGISTRY",
    help="Push images from the original registry into another registry and use this registry as a source for Thoth. "
    "This option is suitable if you want to analyze images from different cluster in which an internal registry "
    "is used without route being exposed. This way you can copy images from internal registry to a remote one "
    "where Thoth has access to. Thoth will use the push registry specified instead of the original one where "
    "images were pushed to. If credentials are required to push into push registry, "
    "see --registry-{user,password} configuration options.",
)
@click.option(
    "--analyze-existing",
    is_flag=True,
    envvar="THOTH_ANALYZE_EXISTING",
    help="List images which were already built in the cluster and submit them to Thoth for analysis. "
    "This is applicable for OpenShift's image streams only.",
)
@click.option(
    "--workers-count",
    type=int,
    default=1,
    envvar="THOTH_BUILD_WATCHER_WORKERS",
    help="Number of worker processes to submit image analysis in parallel.",
)
def cli(
    build_watcher_namespace: str,
    thoth_api_host: str = None,
    verbose: bool = False,
    no_tls_verify: bool = False,
    no_registry_tls_verify: bool = False,
    pass_token: bool = False,
    registry_user: str = None,
    registry_password: str = None,
    push_registry: str = None,
    analyze_existing: bool = None,
    workers_count: int = None,
):
    """Build watcher bot for analyzing image builds done in cluster."""
    if verbose:
        _LOGGER.setLevel(logging.DEBUG)

    _LOGGER.info(
        "Build watcher is watching namespace %r and submitting resulting images to Thoth at %r",
        build_watcher_namespace,
        thoth_api_host,
    )

    # All the images to be processed are submitted onto this queue by producers.
    manager = Manager()
    queue = manager.Queue()

    if analyze_existing:
        # We do this in a standalone process, but reuse worker queue to process images.
        existing_producer = Process(
            target=_existing_producer, args=(queue, build_watcher_namespace)
        )
        existing_producer.start()

    configuration.explicit_host = thoth_api_host
    configuration.tls_verify = not no_tls_verify
    openshift = OpenShift()

    if pass_token:
        if registry_password:
            raise ValueError(
                "Flag --pass-token is disjoint with explicit password propagation"
            )
        registry_password = openshift.token

    producer = Process(target=_event_producer, args=(queue, build_watcher_namespace))
    producer.start()

    args = [queue, push_registry, registry_user, registry_password, no_registry_tls_verify]
    # We do not use multiprocessing's Pool here as we manage lifecycle of workers on our own. If any fails, give
    # up and report errors.
    process_pool = []
    _LOGGER.info("Starting worker processes, number of workers is set to: %d", workers_count)
    for worker in range(workers_count):
        p = Process(target=_submitter, args=args)
        p.start()
        _LOGGER.info("Started a new worker with PID: %d", p.pid)
        process_pool.append(p)

    # Check if all the processes is still alive.
    while True:
        if any(not process.is_alive() for process in process_pool):
            raise RuntimeError("One of the processes failed")

        time.sleep(5)

    # Always fail, this should be run forever.
    sys.exit(1)


if __name__ == "__main__":
    sys.exit(cli())