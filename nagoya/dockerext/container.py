#
# Copyright (C) 2014 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import importlib
import logging
import uuid
import pprint

import docker
import requests

logger = logging.getLogger("nagoya.dockerext")

class ContainerExitError(Exception):
    def __init__(self, code, logs, inspect):
        self.code = code
        self.logs = logs
        self.inspect = inspect
        message = "Error code {0}\n\nLogs:\n{1}\n\nInspect:\n{2}\n".format(code, logs, pprint.pformat(inspect))
        super(ContainerExitError, self).__init__(message)

class Env(object):
    def __init__(self, key, value):
        self.key = key
        self.value = value

    @classmethod
    def from_text(cls, text):
        k,v = text.split("=", 1)
        return cls(k, v)

    def api_formatted(self):
        return self.key + "=" + self.value

    def __str__(self):
        return self.api_formatted()

class VolumeLink(object):
    def __init__(self, host_path, container_path, read_only=False):
        self.host_path = host_path
        self.container_path = container_path
        self.read_only = read_only

    @classmethod
    def from_text(cls, text):
        s = text.split(":")
        if len(s) == 1:
            h = None
            c, = s
        else:
            h, c = s
        return cls(h, c)

    def bind_formatted(self):
        return {self.host_path: {"bind": self.container_path, "ro": self.read_only}}

    def __str__(self):
        return ":".join([self.container_path, self.host_path, self.read_only])

class VolumeFromLink(object):
    def __init__(self, container_name, mode):
        self.container_name = container_name
        self.mode = mode

    @classmethod
    def from_text(cls, text):
        c, m = text.split(":")
        return cls(c, m)

    def api_formatted(self):
        return self.container_name + ":" + self.mode

    def __str__(self):
        return self.api_formatted()

class NetworkLink(object):
    def __init__(self, container_name, alias):
        self.container_name = container_name
        self.alias = alias

    @classmethod
    def from_text(cls, text):
        c, a = text.split(":")
        return cls(c, a)

    def api_formatted(self):
        return (self.container_name, self.alias)

    def __str__(self):
        return ":".join(self.api_formatted())

class Callspec(object):
    valid_events = {"init", "create", "start", "stop", "remove"}
    valid_event_parts = {"pre", "post"}

    def __init__(self, event_part, event, callback_func):
        if not event_part in self.valid_event_parts:
            ValueError("Event part '{0}' is not valid".format(event_part))
        if not event in self.valid_events:
            ValueError("Event '{0}' is not valid".format(event))

        self.event_part = event_part
        self.event = event
        self.callback_func = callback_func

    @classmethod
    def from_text(cls, text):
        event_spec, cb_coord = text.split(":")
        event_part, event = event_spec.split("_")

        if cb_coord.startswith("."):
            raise ValueError("Callback coordinate '{0}' cannot be relative".format(cb_coord))
        else:
            module, cb_name = cb_coord.rsplit(".", 1)
            cb_module = importlib.import_module(module)
            callback_func = getattr(cb_module, cb_name)

        return cls(event_part, event, callback_func)

class Container(object):
    @staticmethod
    def random_name():
        return str(uuid.uuid4())

    def __init__(self, image, name=None, detach=True, entrypoint=None,
                 run_once=False, working_dir=None, add_capabilities=None,
                 drop_capabilities=None, callbacks=None, commands=None,
                 envs=None, links=None, volumes=None, volumes_from=None):

        # For mutable defaults
        def mdef(candidate, default):
            return candidate if candidate is not None else default

        self.image = image
        self.name = mdef(name, self.random_name())
        self.detach = detach
        self.entrypoint = entrypoint
        self.run_once = run_once
        self.working_dir = working_dir
        self.add_capabilities = mdef(add_capabilities, [])
        self.drop_capabilities = mdef(drop_capabilities, [])
        self.callbacks = mdef(callbacks, [])
        self.commands = mdef(commands, [])
        self.envs =  mdef(envs, [])
        self.links = mdef(links, [])
        self.volumes = mdef(volumes, [])
        self.volumes_from = mdef(volumes_from, [])

    @classmethod
    def from_dict(cls, name, d):
        params = dict()

        params["name"] = name
        for required in ["image"]:
            params[required] = d[required]

        def copy(key):
            return d[key]
        def plural_ft(to_type):
            def makelist(key):
                lines = map(str.strip, d[key].split("\n"))
                return [to_type.from_text(l) for l in lines]
            return makelist
        def split_lines(key):
            return d[key].split("\n")

        optionals = {"detach" : copy,
                     "entrypoint" : copy,
                     "run_once" : copy,
                     "working_dir" : copy,
                     "add_capabilities" : split_lines,
                     "drop_capabilities" : split_lines,
                     "callbacks" : plural_ft(Callspec),
                     "commands" : split_lines,
                     "envs" : plural_ft(Env),
                     "links" : plural_ft(NetworkLink),
                     "volumes" : plural_ft(VolumeLink),
                     "volumes_from" : plural_ft(VolumeFromLink)}

        for optional,valuefunc in optionals.items():
            if optional in d:
                params[optional] = valuefunc(optional)

        return cls(**params)

    @property
    def client(self):
        if not hasattr(self, "_client"):
            self._client = docker.Client(timeout=10)
        return self._client

    @client.setter
    def client(self, value):
        self._client = value

    def _process_callbacks(self, event_part, event):
        for callspec in self.callbacks:
            if callspec.event_part == event_part and callspec.event == event:
                callspec.callback_func(self)

    def init(self):
        self._process_callbacks("pre", "init")
        logger.debug("Initializing container {0}".format(self))
        self.create()
        self.start()
        self._process_callbacks("post", "init")

    def create(self, exists_ok=True):
        try:
            self._process_callbacks("pre", "create")
            logger.debug("Attempting to create container {0}".format(self))
            self.client.create_container(name=self.name,
                                         image=self.image,
                                         detach=self.detach, # Doesn't seem to do anything
                                         volumes=self.volumes_api_container_paths(),
                                         entrypoint=self.entrypoint,
                                         working_dir=self.working_dir,
                                         environment=self.envs_api_formatted(),
                                         command=[""] if self.commands == [] else self.commands)
            logger.info("Created container {0}".format(self))
            self._process_callbacks("post", "create")
        except docker.errors.APIError as e:
            if exists_ok and e.response.status_code == 409:
                logger.debug("Container {0} already exists".format(self))
            else:
                raise

    def start(self):
        def start():
            self._process_callbacks("pre", "start")
            logger.debug("Attempting to start container {0}".format(self))
            self.client.start(container=self.name,
                              cap_add=self.add_capabilities,
                              cap_drop=self.drop_capabilities,
                              binds=self.volumes_api_binds(),
                              links=self.links_api_formatted(),
                              volumes_from=self.volumes_from_api_formatted())
            if not self.detach:
                logger.info("Waiting for container {0} to finish".format(self))
                status_code = self.wait(error_ok=False)
                logger.info("Container {0} exited ok".format(self))
            else:
                logger.info("Started container {0}".format(self))
            self._process_callbacks("post", "start")

        if self.run_once:
            container_info = self.client.inspect_container(container=self.name)
            if container_info["State"]["StartedAt"] == "0001-01-01T00:00:00Z":
                start()
            else:
                logger.debug("Container {0} is configured to run only once and has been started before".format(self))
        else:
            start()

    def stop(self, not_exists_ok=True):
        logger.debug("Attempting to stop container {0}".format(self))

        try:
            container_info = self.client.inspect_container(container=self.name)
            pid = container_info["State"]["Pid"]
            if pid == 0:
                logger.debug("Container {0} is not running".format(self))
            else:
                self._process_callbacks("pre", "stop")
                self.client.kill(container=self.name, signal=15)
                try:
                    self.wait(timeout=20, error_ok=True)
                    logger.info("Stopped container {0}".format(self))
                    self._process_callbacks("post", "stop")
                except requests.exceptions.Timeout:
                    self.client.kill(container=self.name, signal=9)
                    try:
                        self.wait(timeout=20, error_ok=True)
                        logger.info("Killed container {0}".format(self))
                        self._process_callbacks("post", "stop")
                    except requests.exceptions.Timeout as e:
                        logger.error("Unable to kill container {0}: {1}".format(self, e))
        except docker.errors.APIError as e:
            if not_exists_ok and e.response.status_code == 404:
                logger.debug("Container {0} does not exist".format(self))
            else:
                raise

    def remove(self, not_exists_ok=True):
        try:
            self._process_callbacks("pre", "remove")
            logger.debug("Attempting to remove container {0}".format(self))
            self.client.remove_container(self.name, force=True)
            logger.info("Removed container {0}".format(self))
            self._process_callbacks("post", "remove")
        except docker.errors.APIError as e:
            if not_exists_ok and e.response.status_code == 404:
                logger.debug("Container {0} doesn't exist".format(self))
            else:
                raise

    def wait(self, timeout=None, error_ok=False):
        url = self.client._url("/containers/{0}/wait".format(self.name))
        res = self.client._post(url, timeout=timeout)
        self.client._raise_for_status(res)
        d = res.json()
        status = d["StatusCode"] if "StatusCode" in d else -1
        if error_ok or status == 0:
            return status
        else:
            raise ContainerExitError(status, self.logs(), self.inspect())

    def logs(self, not_exists_ok=True):
        try:
            return self.client.logs(self.name)
        except docker.errors.APIError as e:
            if not_exists_ok and e.response.status_code == 404:
                logger.debug("Container {0} does not exist".format(self))
            else:
                raise

    def inspect(self, not_exists_ok=True):
        try:
            return self.client.inspect_container(self.name)
        except docker.errors.APIError as e:
            if not_exists_ok and e.response.status_code == 404:
                logger.debug("Container {0} does not exist".format(self))
            else:
                raise

    def dependency_names(self):
        deps = set()

        for link in self.links:
            deps.add(link.container_name)

        for vf in self.volumes_from:
            deps.add(vf.container_name)

        return deps

    def envs_api_formatted(self):
        return [e.api_formatted() for e in self.envs]

    def volumes_api_container_paths(self):
        return [v.container_path for v in self.volumes]

    def volumes_api_binds(self):
        binds = dict()
        for v in self.volumes:
            binds.update(v.bind_formatted())
        return binds

    def volumes_from_api_formatted(self):
        return [v.api_formatted() for v in self.volumes_from]

    def links_api_formatted(self):
        return [l.api_formatted() for l in self.links]

    def add_env(self, *args, **kwargs):
        env = Env(*args, **kwargs)
        self.envs.append(env)
        return env

    def add_volume(self, *args, **kwargs):
        link = VolumeLink(*args, **kwargs)
        self.volumes.append(link)
        return link

    def add_volume_from(self, *args, **kwargs):
        link = VolumeFromLink(*args, **kwargs)
        self.volumes_from.append(link)
        return link

    def add_link(self, *args, **kwargs):
        link = NetworkLink(*args, **kwargs)
        self.links.append(link)
        return link

    def __str__(self):
        return self.name

class TempContainer(Container):
    def __init__(self, image, name=None, **kwargs):
        image_name = image.split(":")[0]
        if name is None:
            name = image_name + "." + self.random_name()[:8]
        super(TempContainer, self).__init__(image, name=name, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.remove()
