#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK

import logging
import sys
import os
import stat
import shutil
import tempfile
import re
import collections
import uuid

import docker

import nagoya.docker.build
import nagoya.build.consys

logger = logging.getLogger("nagoya.build")

#
# Exceptions
#

# TODO remove / refactor these?

class BuildException(Exception):
    pass

class ContainerExitError(BuildException):
    pass

class InvalidFormat(BuildException):
    pass

#
# Helpers
#

def uuid4():
    return str(uuid.uuid4())

def line_split(string):
    return map(str.strip, string.split("\n"))

def optional_plural(cfg, key):
    if key in cfg:
        logger.debug("Optional config key {key} exists".format(**locals()))
        for elem in line_split(cfg[key]):
            yield elem
    else:
        logger.debug("Optional config key {key} does not exist".format(**locals()))

#
# Container system image build
#

container_system_option_names = {"volumes_from", "links", "commit"}

volume_spec_pattern = re.compile(r'^(?P<image>[^ ]+) then (discard$|persist to (?P<persistimage>[^: ]+)$)')
VolImg = collections.namedtuple("VolImg", ["image", "persist_image"])
def parse_volume_spec(spec, opt_name, image_name):
    match = volume_spec_pattern.match(spec)
    if match:
        gd = match.groupdict()
        image = gd["image"]
        if "persistimage" in gd:
            persist_image = gd["persistimage"]
        else:
            persist_image = None
        return VolImg(image, persist_image)
    else:
        raise InvalidFormat("Invalid {opt_name} specification '{spec}' for image {image_name}".format(**locals()))

link_spec_pattern = re.compile(r'^(?P<image>[^ ]+) alias (?P<alias>[^ ]+) then (discard$|commit to (?P<commitimage>[^: ]+)$)')
LinkImg = collections.namedtuple("LinkImg", ["image", "alias", "commit_image"])
def parse_link_spec(spec, opt_name, image_name):
    match = link_spec_pattern.match(spec)
    if match:
        gd = match.groupdict()
        image = gd["image"]
        alias = gd["alias"]
        if "commitimage" in gd:
            commit_image = gd["commitimage"]
        else:
            commit_image = None
        return LinkImg(image, alias, commit_image)
    else:
        raise InvalidFormat("Invalid {opt_name} specification '{spec}' for image {image_name}".format(**locals()))

ContainerWithDest = collections.namedtuple("ContainerWithDest", ["container", "destimage"])

def build_container_system(image_name, image_config, client, quiet):
    logger.info("Creating container system for {image_name}".format(**locals()))

    with nagoya.build.consys.BuildContainerSystem(root_image=image_config["from"], client=client, cleanup="remove") as bcs:
        if "commit" in image_config and image_config["commit"]:
            logger.debug("Root container {root} will be committed".format(**locals()))
            bcs.commit(bcs.root)

        if "entrypoint" in image_config:
            entrypoint_spec = image_config["entrypoint"]
            res_paths = parse_dir_spec(entrypoint_spec, "entrypoint", image_name)
            bcs.root.working_dir = res_paths.dest_dir
            bcs.volume_include(bcs.root, res_paths.src_path, res_paths.dest_path, executable=True)

        for lib_spec in optional_plural(image_config, "libs"):
            res_paths = parse_dir_spec(lib_spec, "lib", image_name)
            bcs.volume_include(bcs.root, res_paths.src_path, dest_dir.dest_path)

        for volume_spec in optional_plural(image_config, "volumes_from"):
            vol = parse_volume_spec(volume_spec, "volume_from", image_name)
            vol_container = bcs.container(image=vol.image, detach=False)
            logger.debug("Root container will have volumes from container {vol_container}".format(**locals()))
            bcs.root.add_volume_from(vol_container.name, "rw")
            if vol.persist_image is not None:
                logger.debug("Container {vol_container} will be persisted to {vol.persist_image}".format(**locals()))
                bcs.persist(vol_container, vol.persist_image)

        for link_spec in optional_plural(image_config, "links"):
            link = parse_link_spec(image_spec, "link", image_name)
            link_container = bcs.container(image=link.image, detach=True)
            logger.debug("Root container will be linked to container {link_container}".format(**locals()))
            bcs.root.add_link(link_container.name, "rw")
            if link.commit_image is not None:
                logger.debug("Container {link_container} will be committed to {vol.commit_image}".format(**locals()))
                bcs.persist(link_container, link.commit_image)


    # TODO old below this line, remove once refactoring is done

    containers = []
    # Docker volumes don't work with the docker commit operation
    commit_containers = []
    # Only volume containers can be "persisted", as it is a workaround to the docker volume limitations
    persist_containers = []

    with TempResourceDirectory(image_root=os.path.join("/", uuid4()[:8])) as vol_host_dir:
        root = nagoya.toji.TempContainer(image=image_config["from"], detach=False)
        containers.append(root)
        if "commit" in image_config and image_config["commit"]:
            logger.debug("Root container {root} will be committed".format(**locals()))
            commit_containers.append(root)
        else:
            logger.debug("Root container {root} will be discarded".format(**locals()))

        # Container-time override of image's entrypoint
        if "entrypoint" in image_config:
            path = os.path.join(image_name, image_config["entrypoint"])
            root.entrypoint = vol_host_dir.include(path, executable=True)

        for lib_path in optional_plural(image_config, "libs"):
            vol_host_dir.include(lib_path)

        # Container-time override of image's working dir
        root.working_dir = vol_host_dir.image_root
        # Container-time definition of container volume
        root.volumes.append(nagoya.toji.VolumeLink(vol_host_dir.name, vol_host_dir.image_root))
        # TODO ^^^ does not work because selinux does not allow container processes access to anything outside their own data. Fix is blocked pending a release with this PR in it: https://github.com/docker/docker/pull/5910

        for volume_spec in optional_plural(image_config, "volumes_from"):
            match = volume_spec_pattern.match(volume_spec)
            if match:
                spec = match.groupdict()
                container = nagoya.toji.TempContainer(image=spec["image"], detach=False)
                logger.debug("Root container will have volumes from container {container}".format(**locals()))
                root.volumes_from.append(nagoya.toji.VolumeFromLink(container.name, "rw"))
                containers.append(container)
                if "persistimage" in spec:
                    logger.debug("Container {container} will be persisted".format(**locals()))
                    persist_containers.append(ContainerWithDest(container, spec["persistimage"]))
            else:
                raise InvalidFormat("Invalid volume from specification '{volume_spec}' for image {image_name}".format(**locals()))

        for link_spec in optional_plural(image_config, "links"):
            match = link_spec_pattern.match(link_spec)
            if match:
                spec = match.groupdict()
                container = nagoya.toji.TempContainer(image=spec["image"], detach=True)
                logger.debug("Root container will be linked to container {container}".format(**locals()))
                root.links.append(toji.NetworkLink(container.name, spec["alias"]))
                containers.append(container)
                if "commitimage" in spec:
                    logger.debug("Container {container} will be committed".format(**locals()))
                    commit_containers.append(ContainerWithDest(container, spec["commitimage"]))
            else:
                raise InvalidFormat("Invalid link specification '{link_spec}' for image {image_name}".format(**locals()))

        logger.info("Starting temporary container system")
        temp_system = nagoya.toji.Toji(containers)
        temp_system.init_containers()

        logger.info("Waiting for the root container to finish")
        status_code = docker_client.wait(root.name)
        if not status_code == 0:
            raise ContainerExitError("Root container did not run sucessfully. Exit code: {status_code}".format(**locals()))

        temp_system.stop_containers()

    for container,commitimage in commit_containers:
        logger.info("Commiting {container} container to image {commitimage}".format(**locals()))
        docker_client.commit(container, commitimage)

    for container,persistimage in persist_containers:
        logger.info("Persisting {container} container to image {persistimage}".format(**locals()))
        with TempResourceDirectory(image_root=os.path.join("/", uuid4())) as extract_dest_dir:
            source_data = docker_client.inspect_container(container=container.name)
            source_volumes = source_data["Volumes"].keys()
            # busybox's tar won't accept file/dir arguments with a starting slash
            volume_paths = [v.lstrip("/") for v in source_volumes]

            logger.info("Extracting volume data from {container} container".format(**locals()))
            image_tar_path = os.path.join(extract_dest_dir.image_root, "extract.tar")
            host_tar_path = os.path.join(extract_dest_dir.name, "extract.tar")

            extract_container_name = uuid4()
            # Mount host volume in container
            volumes = [extract_dest_dir.name + ":" + extract_dest_dir.image_root]
            command = ["tar", "-cf", image_tar_path] + volume_paths
            docker_client.create_container(name=extract_container_name,
                                           image="busybox:latest",
                                           volumes=volumes,
                                           command=command)
            # Mount volumes from source container read-only
            volumes_from = [container.name + ":ro"]
            docker_client.start(container=extract_container_name,
                                volumes_from=volumes_from)
            extract_status = docker_client.wait(extract_container_name)
            if not extract_status == 0:
                raise ContainerExitError("Extract container did not run sucessfully. Exit code: {extract_status}".format(**locals()))

            logger.info("Building image {persistimage} with volume data from {container} container".format(**locals()))
            with nagoya.docker.build.BuildContext(persistimage, container.image, docker_client, quiet) as context:
                context.include(host_tar_path, "/")

    temp_system.remove_containers()

#
# Standard image build
#

dir_spec_pattern = re.compile(r'^(?P<sourcepath>.+) (?:in (?P<inpath>.+)|at (?P<atpath>.+))$')

ResPaths = collections.namedtuple("ResCopyPaths", ["src_path", "dest_path", "dest_dir"])

def parse_dir_spec(spec, opt_name, image_name):
    match = dir_spec_pattern.match(spec)
    if match:
        gd = match.groupdict()
        src_path = gd["sourcepath"]
        src_basename = os.path.basename(src_path)

        if inpath in gd:
            image_dir = gd["inpath"]
            image_path = os.path.join(image_dir, src_basename)
        elif atpath in gd:
            image_path = gd["atpath"]
            image_dir = os.path.dirname(image_path)
        else:
            raise Exception("dir_spec_pattern is broken")

        return ResPaths(src_path, image_path, image_dir)
    else:
        raise InvalidFormat("Invalid {opt_name} specification '{spec}' for image {image_name}".format(**locals()))

def build_image(image_name, image_config, client, quiet):
    logger.info("Generating files for {image_name}".format(**locals()))
    with nagoya.docker.build.BuildContext(image_name, image_config["from"], client, quiet) as context:
        context.maintainer(image_config["maintainer"])

        for port in optional_plural(image_config, "exposes"):
            context.expose(port)

        for volume in optional_plural(image_config, "volumes"):
            context.volume(volume)

        for lib_spec in optional_plural(image_config, "libs"):
            res_paths = parse_dir_spec(lib_spec, "lib", image_name)
            context.include(res_paths.src_path, res_paths.dest_path)

        previous_workdir = ""
        def add_workdir(image_dir):
            if not previous_workdir == image_dir:
                context.workdir(image_dir)
                previous_workdir = image_dir

        for run_spec in optional_plural(image_config, "runs"):
            res_paths = parse_dir_spec(run_spec, "run", image_name)
            context.include(res_paths.src_path, res_paths.dest_path, executable=True)
            add_workdir(res_paths.dest_dir)
            context.run(res_paths.dest_path)

        if "entrypoint" in image_config:
            entrypoint_spec = image_config["entrypoint"]
            res_paths = parse_dir_spec(entrypoint_spec, "entrypoint", image_name)
            context.include(res_paths.src_path, res_paths.dest_path, executable=True)
            add_workdir(res_paths.dest_dir)
            context.entrypoint(res_paths.dest_path)

#
# Build images
#

def build_images(config, images, quiet):
    num_img = len(images)
    logger.info("Building {0} image{1}".format(num_img, "s" if num_img > 1 else ""))

    docker_client = docker.Client(timeout=5)
    docker_client.ping()

    for image in images:
        logger.debug("Processing image {image}".format(**locals()))
        image_config = config[image]

        if not container_system_option_names.isdisjoint(image_config.keys()):
            build_container_system(image, image_config, docker_client, quiet)
        else:
            build_image(image, image_config, docker_client, quiet)

    logger.info("Done")

#
# Main
#

# TODO might have to put this in a seperate file outside of the module if running the module doesn't work properly

default_config_paths = ["images.cfg"]
boolean_config_options = ["commit"]

def sc_build(args):
    config = nagoya.cfg.read_config(args.config, default_config_paths, boolean_config_options)
    return build_images(config, args.images, args.quiet_build)

def scargs_build(parser):
    parser.add_argument("-b", "--quiet-build", action="store_true", help="Do not print the builds' stdout/stderr")
    imgs = parser.add_argument("images", metavar="IMAGE", nargs="+", help="Image to build")
    if nagoya.args.argcomplete_available:
        imgs.completer = nagoya.args.ConfigSectionsCompleter(default_config_paths)

def sc_clean(args):
    pass

if __name__ == "__main__":
    parser = nagoya.args.create_default_argument_parser(description="Build docker images")
    nagoya.args.add_subcommand_subparsers(parser)
    nagoya.args.attempt_autocomplete(parser)
    args = parser.parse_args()

    nagoya.log.setup_logger(args.quiet, args.verbose)

    nagoya.args.run_subcommand_func(args, parser)